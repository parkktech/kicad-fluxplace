"""Last-mile patcher — close the final unrouted nets on a routed board.

Measured pattern (every fluxplace board to date): the bulk router finishes
with 2-4 open nets of the same class — a power branch, a GND pour island, a
slow GPIO, a crystal line. They are not hard nets; they are the tail a
capped batch rip-up router never gets back to, and the freerouting finisher
cannot load the board inside any practical budget. An incremental
single-net router closes them in seconds:

  - unrouted nets from the DRC report (the truth the fab gate uses)
  - obstacle grid per signal layer from the real copper (tracks, vias, pads
    of OTHER nets, dilated by clearance + half the patch track width);
    zone fills are NOT obstacles — pours re-flow around new copper and are
    refilled afterwards
  - the target net's copper is grouped into connected islands; multi-source
    Dijkstra routes island-to-island with a via cost (through-vias only,
    the cell must be free on every signal layer)
  - paths become real PCB_TRACK/PCB_VIA objects, zones are refilled, and
    the result is DRC-guarded: accept only if unconnected items went down
    and violations did not go up — otherwise revert

GND islands that remain after routing+refill get a stitching via to the
internal plane when one exists under the island.
"""
import heapq
import math
import os

import pcbnew


# ---------------------------------------------------------------- grid ----

def _cells_disk(cx, cy, r):
    rr = int(math.ceil(r))
    for dx in range(-rr, rr + 1):
        for dy in range(-rr, rr + 1):
            if dx * dx + dy * dy <= r * r:
                yield cx + dx, cy + dy


class Grid:
    """Per-layer occupancy at `cell` mm resolution over the board bbox."""

    def __init__(self, board, layers, cell=0.25):
        self.cell = cell
        bb = board.GetBoardEdgesBoundingBox()
        self.x0 = bb.GetLeft() / 1e6
        self.y0 = bb.GetTop() / 1e6
        self.nx = int(bb.GetWidth() / 1e6 / cell) + 1
        self.ny = int(bb.GetHeight() / 1e6 / cell) + 1
        self.layers = list(layers)            # kicad layer ids, signal only
        self.blocked = {l: set() for l in self.layers}
        # cells where a THROUGH-via may not land: foreign copper on ANY
        # copper layer (a through-via pierces inner plane-layer tracks too —
        # measured short: patch via vs GND track on the inner GND layer)
        self.via_blocked = set()

    def cxy(self, x_mm, y_mm):
        return (int((x_mm - self.x0) / self.cell),
                int((y_mm - self.y0) / self.cell))

    def mm(self, cx, cy):
        return (self.x0 + (cx + 0.5) * self.cell,
                self.y0 + (cy + 0.5) * self.cell)

    def inside(self, cx, cy):
        return 1 <= cx < self.nx - 1 and 1 <= cy < self.ny - 1

    def block_disk(self, layer, x_mm, y_mm, r_mm):
        cx, cy = self.cxy(x_mm, y_mm)
        for c in _cells_disk(cx, cy, r_mm / self.cell):
            self.blocked[layer].add(c)

    def block_seg(self, layer, x1, y1, x2, y2, r_mm):
        n = max(1, int(math.hypot(x2 - x1, y2 - y1) / (self.cell * 0.7)))
        for k in range(n + 1):
            t = k / n
            self.block_disk(layer, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t,
                            r_mm)

    def block_rect(self, layer, x_mm, y_mm, hw, hh, rot_deg, margin):
        """Block an oriented rectangle (pad body) + margin. A circumscribed
        disk overstates a 0.3x1.5 pad 5x and seals fine-pitch channels."""
        import math as _m
        r = _m.radians(rot_deg)
        c, sn = _m.cos(r), _m.sin(r)
        ext = _m.hypot(hw + margin, hh + margin)
        cx0, cy0 = self.cxy(x_mm, y_mm)
        rr = int(_m.ceil(ext / self.cell)) + 1
        for dx in range(-rr, rr + 1):
            for dy in range(-rr, rr + 1):
                px = (dx) * self.cell
                py = (dy) * self.cell
                lx = px * c + py * sn
                ly = -px * sn + py * c
                if abs(lx) <= hw + margin and abs(ly) <= hh + margin:
                    self.blocked[layer].add((cx0 + dx, cy0 + dy))

    def block_via_rect(self, x_mm, y_mm, hw, hh, rot_deg, margin):
        import math as _m
        r = _m.radians(rot_deg)
        c, sn = _m.cos(r), _m.sin(r)
        ext = _m.hypot(hw + margin, hh + margin)
        cx0, cy0 = self.cxy(x_mm, y_mm)
        rr = int(_m.ceil(ext / self.cell)) + 1
        for dx in range(-rr, rr + 1):
            for dy in range(-rr, rr + 1):
                px = dx * self.cell
                py = dy * self.cell
                lx = px * c + py * sn
                ly = -px * sn + py * c
                if abs(lx) <= hw + margin and abs(ly) <= hh + margin:
                    self.via_blocked.add((cx0 + dx, cy0 + dy))

    def block_via_disk(self, x_mm, y_mm, r_mm):
        cx, cy = self.cxy(x_mm, y_mm)
        for c in _cells_disk(cx, cy, r_mm / self.cell):
            self.via_blocked.add(c)

    def block_via_seg(self, x1, y1, x2, y2, r_mm):
        n = max(1, int(math.hypot(x2 - x1, y2 - y1) / (self.cell * 0.7)))
        for k in range(n + 1):
            t = k / n
            self.block_via_disk(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t,
                                r_mm)


def build_grid(board, layers, net_code, track_w, clearance, cell=0.25,
               via_r=0.3):
    """Obstacles for routing net `net_code`: everything conductive that is
    NOT this net, dilated by clearance + track_w/2. Returns (grid, islands)
    where islands = list of cell-sets of the target net's existing copper,
    grouped by connectivity (vias join layers). Foreign copper on ANY copper
    layer (inner planes included) blocks THROUGH-via placement."""
    g = Grid(board, layers, cell)
    ds = board.GetDesignSettings()
    hole_c = ds.m_HoleClearance / 1e6
    slop = cell * 0.75                    # grid quantization safety
    half = track_w / 2.0
    def _mgn(item, layer):
        # trust the CALLER's clearance (per-net derates in the board's .dru
        # make finer legal for the patched nets — netclass max() re-sealed
        # dig's escape lanes, measured); honor explicit per-item overrides
        # (e.g. an NPTH keepout pad) via the local clearance only
        loc = 0.0
        try:
            loc = (item.GetLocalClearance() or 0) / 1e6
        except (TypeError, AttributeError):
            pass
        return max(clearance, loc) + half + slop
    vmargin_base = via_r + slop
    net_cells = {l: set() for l in layers}
    net_vias = []
    for t in board.GetTracks():
        code = t.GetNetCode()
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            try:
                r = t.GetWidth(layers[0]) / 2e6
            except TypeError:
                r = t.GetWidth() / 2e6
            if code == net_code:
                for l in layers:
                    cx, cy = g.cxy(p.x / 1e6, p.y / 1e6)
                    net_cells[l].add((cx, cy))
                net_vias.append(g.cxy(p.x / 1e6, p.y / 1e6))
            else:
                mg = _mgn(t, layers[0])
                # my track near their hole AND my hole near their copper
                mg = max(mg, t.GetDrillValue() / 2e6 + hole_c + half + slop)
                for l in layers:
                    g.block_disk(l, p.x / 1e6, p.y / 1e6, r + mg)
                # via copper vs their copper; via hole vs their hole
                g.block_via_disk(p.x / 1e6, p.y / 1e6, max(
                    r + (_mgn(t, layers[0]) - half) + via_r,
                    t.GetDrillValue() / 2e6 + hole_c + via_r / 2) + slop)
            continue
        lay = t.GetLayer()
        s, e = t.GetStart(), t.GetEnd()
        if code != net_code and pcbnew.IsCopperLayer(lay):
            g.block_via_seg(s.x / 1e6, s.y / 1e6, e.x / 1e6, e.y / 1e6,
                            t.GetWidth() / 2e6 + max(
                                (_mgn(t, lay) - half) + via_r,
                                hole_c + via_r / 2) + slop)
        if lay not in g.blocked:
            continue
        if code == net_code:
            n = max(1, int(t.GetLength() / 1e6 / (cell * 0.7)))
            for k in range(n + 1):
                x = (s.x + (e.x - s.x) * k / n) / 1e6
                y = (s.y + (e.y - s.y) * k / n) / 1e6
                net_cells[lay].add(g.cxy(x, y))
        else:
            g.block_seg(lay, s.x / 1e6, s.y / 1e6, e.x / 1e6, e.y / 1e6,
                        t.GetWidth() / 2e6 + _mgn(t, lay))
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            p = pad.GetPosition()
            r = max(pad.GetSize().x, pad.GetSize().y) / 2e6
            on = set(pad.GetLayerSet().Seq())
            th = pad.GetDrillSize().x > 0
            hw = pad.GetSize().x / 2e6
            hh = pad.GetSize().y / 2e6
            prot = pad.GetOrientationDegrees()
            if pad.GetNetCode() != net_code and (th or any(
                    pcbnew.IsCopperLayer(l) and l not in g.blocked
                    for l in on)):
                g.block_via_rect(p.x / 1e6, p.y / 1e6, hw, hh, prot,
                                 _mgn(pad, layers[0]) - half + via_r + slop)
            for l in layers:
                if not th and l not in on:
                    continue
                if pad.GetNetCode() == net_code:
                    net_cells[l].add(g.cxy(p.x / 1e6, p.y / 1e6))
                else:
                    mg = _mgn(pad, l)
                    if th:
                        mg = max(mg, pad.GetDrillSize().x / 2e6 + hole_c
                                 + half + slop - min(hw, hh))
                    g.block_rect(l, p.x / 1e6, p.y / 1e6, hw, hh, prot, mg)
    # group target-net cells into islands (per-layer 8-connectivity at pad
    # scale; net vias join layers)
    li = {l: i for i, l in enumerate(layers)}
    all_cells = set()
    for l in layers:
        for c in net_cells[l]:
            all_cells.add((li[l], c[0], c[1]))
    for cx, cy in net_vias:
        for l in layers:
            all_cells.add((li[l], cx, cy))
    # flood-fill with a spatial hash: same-layer neighbours within R cells,
    # plus any-layer stack at the same (x, y) — O(N * R^2)
    islands = []
    seen = set()
    R = max(2, int(1.2 / cell))               # merge radius ~pad scale
    by_layer = {}
    by_xy = {}
    for n in all_cells:
        by_layer.setdefault((n[0], n[1] // R, n[2] // R), []).append(n)
        by_xy.setdefault((n[1], n[2]), []).append(n)
    for start in all_cells:
        if start in seen:
            continue
        comp = set()
        stack = [start]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            comp.add(n)
            nl, nx, ny = n
            bx, by = nx // R, ny // R
            for gx in (bx - 1, bx, bx + 1):
                for gy in (by - 1, by, by + 1):
                    for m in by_layer.get((nl, gx, gy), ()):
                        if m not in seen and abs(m[1] - nx) <= R \
                                and abs(m[2] - ny) <= R:
                            stack.append(m)
            for m in by_xy.get((nx, ny), ()):
                if m not in seen:
                    stack.append(m)
        islands.append(comp)
    islands.sort(key=len, reverse=True)
    return g, islands


# --------------------------------------------------------------- route ----

_VIA_COST = 14


def dijkstra(grid, sources, targets, max_expand=2_000_000):
    """Multi-source multi-target over (layer_idx, cx, cy). Returns the cell
    path or None. Straight steps cost 10, diagonals 14, vias _VIA_COST*10
    (via cell must be free on EVERY layer)."""
    nlayers = len(grid.layers)
    blocked = [grid.blocked[l] for l in grid.layers]
    tset = targets
    dist = {}
    prev = {}
    pq = []
    for s in sources:
        dist[s] = 0
        heapq.heappush(pq, (0, s))
    steps = ((1, 0, 10), (-1, 0, 10), (0, 1, 10), (0, -1, 10),
             (1, 1, 14), (1, -1, 14), (-1, 1, 14), (-1, -1, 14))
    expanded = 0
    while pq:
        d, node = heapq.heappop(pq)
        if dist.get(node, -1) != d:
            continue
        if node in tset:
            path = [node]
            while node in prev:
                node = prev[node]
                path.append(node)
            return path[::-1]
        expanded += 1
        if expanded > max_expand:
            return None
        l, cx, cy = node
        for dx, dy, c in steps:
            nx, ny = cx + dx, cy + dy
            if not grid.inside(nx, ny) or (nx, ny) in blocked[l]:
                continue
            m = (l, nx, ny)
            nd = d + c
            if nd < dist.get(m, 1 << 60):
                dist[m] = nd
                prev[m] = node
                heapq.heappush(pq, (nd, m))
        if nlayers > 1:
            ok = (cx, cy) not in grid.via_blocked and \
                all((cx, cy) not in blocked[k] for k in range(nlayers))
            if ok:
                for k in range(nlayers):
                    if k == l:
                        continue
                    m = (k, cx, cy)
                    nd = d + _VIA_COST * 10
                    if nd < dist.get(m, 1 << 60):
                        dist[m] = nd
                        prev[m] = node
                        heapq.heappush(pq, (nd, m))
    return None


def _escape_route(grid, src_island, tgt_islands, max_r_mm=1.5):
    """Dogbone move: a laterally walled pad can still escape VERTICALLY —
    seed the search with legal via spots within max_r of either end's cells
    and write the stub+via if used. Returns an augmented path (may begin/end
    with a stub hop + layer change) or None."""
    R = max(2, int(max_r_mm / grid.cell))
    nlayers = len(grid.layers)

    def seeds(island):
        out = {}
        cells = list(island)[:400]
        for (l, cx, cy) in cells:
            for dx in range(-R, R + 1):
                for dy in range(-R, R + 1):
                    nx, ny = cx + dx, cy + dy
                    if not grid.inside(nx, ny) or (nx, ny) in grid.via_blocked:
                        continue
                    key = (nx, ny)
                    if key not in out:
                        out[key] = (l, cx, cy)      # nearest-ish anchor cell
        return out

    s_seeds = seeds(src_island)
    tgt = set().union(*tgt_islands)
    t_seeds = seeds(tgt)
    if not s_seeds:
        return None
    # sources: every layer at each source seed; targets likewise
    src_nodes = {(k, sx, sy) for (sx, sy) in s_seeds for k in range(nlayers)}
    tgt_nodes = {(k, tx, ty) for (tx, ty) in t_seeds for k in range(nlayers)}
    tgt_nodes |= tgt
    path = dijkstra(grid, src_nodes, tgt_nodes)
    if path is None:
        return None
    # prepend stub from anchor pad cell if we started on a seed
    head = path[0]
    hk = (head[1], head[2])
    if head not in src_island and hk in s_seeds:
        al, ax, ay = s_seeds[hk]
        pre = [(al, ax, ay)]
        if al != head[0]:
            pre.append((al, head[1], head[2]))   # stub on pad layer, then via
        path = pre + path
    tail = path[-1]
    tk = (tail[1], tail[2])
    if tail not in tgt and tk in t_seeds:
        al, ax, ay = t_seeds[tk]
        post = []
        if al != tail[0]:
            post.append((al, tail[1], tail[2]))
        post.append((al, ax, ay))
        path = path + post
    return path


def _simplify(path):
    """Merge collinear runs; keep layer-change points."""
    out = [path[0]]
    for i in range(1, len(path) - 1):
        a, b, c = path[i - 1], path[i], path[i + 1]
        if a[0] == b[0] == c[0] and (b[1] - a[1], b[2] - a[2]) == \
                (c[1] - b[1], c[2] - b[2]):
            continue
        out.append(b)
    out.append(path[-1])
    return out


def apply_path(board, grid, path, net_code, width_mm, via_mm, drill_mm):
    """Write the cell path as tracks + through-vias."""
    ni = board.FindNet(net_code) if isinstance(net_code, str) else \
        board.GetNetInfo().GetNetItem(net_code)
    pts = _simplify(path)
    added = []
    for a, b in zip(pts, pts[1:]):
        ax, ay = grid.mm(a[1], a[2])
        bx, by = grid.mm(b[1], b[2])
        if a[0] != b[0]:                     # layer change -> via
            v = pcbnew.PCB_VIA(board)
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetPosition(pcbnew.VECTOR2I(int(ax * 1e6), int(ay * 1e6)))
            v.SetWidth(int(via_mm * 1e6))
            v.SetDrill(int(drill_mm * 1e6))
            v.SetNet(ni)
            board.Add(v)
            added.append(v)
            continue
        if (ax, ay) == (bx, by):
            continue
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(int(ax * 1e6), int(ay * 1e6)))
        t.SetEnd(pcbnew.VECTOR2I(int(bx * 1e6), int(by * 1e6)))
        t.SetWidth(int(width_mm * 1e6))
        t.SetLayer(grid.layers[a[0]])
        t.SetNet(ni)
        board.Add(t)
        added.append(t)
    return added


# ---------------------------------------------------------------- main ----

def refill_zones(board):
    filler = pcbnew.ZONE_FILLER(board)
    filler.Fill(board.Zones())


def patch_board(board_path, out_path, kicad_cli="kicad-cli", layers=None,
                track_w=0.2, clearance=0.15, via_mm=0.6, drill_mm=0.3,
                skip_nets=("GND",), net_widths=None, cell=0.25, log=print):
    """Close the leftover unrouted nets on a routed board. Returns a summary
    dict; writes out_path only when the DRC guard accepts."""
    from . import adaptive as AD
    from . import kicad_io as IO
    board = pcbnew.LoadBoard(board_path)
    lay = layers or IO.signal_layers(board)
    # signal_layers returns NAMES; the grid and pcbnew items need int ids
    lay = [board.GetLayerID(l) if isinstance(l, str) else l for l in lay]
    # BASELINE with fresh pours: refilling alone changes both DRC counts
    # (measured: -10 violations, -7 unconnected — stale pours heal GND
    # islands); the guard must compare like with like
    refill_zones(board)
    base = out_path + ".base.kicad_pcb"
    pcbnew.SaveBoard(base, board)
    # DRC truth needs the design-rule sidecars (per-net derates live in the
    # .kicad_dru; without it the guard judges fine copper against bare
    # netclass and everything looks illegal)
    import shutil as _sh
    srcdir = os.path.dirname(os.path.abspath(board_path))
    stem = os.path.splitext(os.path.basename(board_path))[0]
    for ext in (".kicad_dru", ".kicad_pro"):
        sidecar = os.path.join(srcdir, stem + ext)
        if os.path.exists(sidecar):
            for tgt in (base, out_path + ".tmp.kicad_pcb", out_path):
                d = os.path.splitext(tgt)[0] + ext
                try:
                    _sh.copy(sidecar, d)
                except OSError:
                    pass
    drc0, un0 = AD.drc_unrouted(base, kicad_cli)
    unc0 = len(drc0.get("unconnected_items", []))
    vio0 = len(drc0.get("violations", []))
    targets = sorted(un0 - set(skip_nets))
    added_items = []
    patched, failed = [], []
    for net in targets:
        ni = board.FindNet(net)
        if ni is None:
            failed.append((net, "net not on board"))
            continue
        w = (net_widths or {}).get(net, track_w)
        g, islands = build_grid(board, lay, ni.GetNetCode(), w, clearance,
                                cell=cell)
        n0 = len(islands)
        rounds = 0
        while len(islands) > 1 and rounds <= n0 + 2:
            src, rest = islands[0], islands[1:]
            tgt = set().union(*rest)
            path = dijkstra(g, src, tgt)
            if path is None:
                path = _escape_route(g, src, rest)
            if path is None:
                break
            added_items += apply_path(board, g, path, net, w, via_mm,
                                      drill_mm)
            end = path[-1]
            merged = next(i for i in rest if end in i)
            islands = [src | merged | set(path)] + \
                [i for i in rest if i is not merged]
            rounds += 1
        if len(islands) > 1 and w > track_w:
            # boxed-in islands: retry the leftovers at signal width — a
            # narrower neck beats an open rail (flagged for review)
            g2, islands2 = build_grid(board, lay, ni.GetNetCode(), track_w,
                                      clearance, cell=max(0.1, cell * 0.6))
            r2 = 0
            while len(islands2) > 1 and r2 <= len(islands2) + 2:
                src, rest = islands2[0], islands2[1:]
                path = dijkstra(g2, src, set().union(*rest))
                if path is None:
                    path = _escape_route(g2, src, rest)
                if path is None:
                    break
                added_items += apply_path(board, g2, path, net, track_w,
                                          via_mm, drill_mm)
                end = path[-1]
                merged = next(i for i in rest if end in i)
                islands2 = [src | merged | set(path)] + \
                    [i for i in rest if i is not merged]
                r2 += 1
            if r2:
                log(f"    patch: {net} — {r2} narrow ({track_w}mm) neck(s) "
                    f"REVIEW: rail necked below its ampacity width")
                rounds += r2
                islands = islands2
        if rounds:
            patched.append((net, rounds))
            left = len(islands) - 1
            log(f"    patch: {net} — {rounds} route(s)"
                + (f", {left} island(s) unreachable" if left else " (closed)"))
        if len(islands) > 1:
            failed.append((net, f"{len(islands) - 1} island(s) unreachable"))
            if not rounds:
                log(f"    patch: {net} — no path through remaining space")
    if not patched:
        # nothing routed, but the refilled baseline may still beat the
        # input (stale pours healed) — keep it as the output
        os.replace(base, out_path)
        return {"patched": [], "failed": failed, "accepted": False,
                "refilled_out": True}
    refill_zones(board)
    tmp = out_path + ".tmp.kicad_pcb"
    pcbnew.SaveBoard(tmp, board)
    drc1, un1 = AD.drc_unrouted(tmp, kicad_cli)
    unc1 = len(drc1.get("unconnected_items", []))
    vio1 = len(drc1.get("violations", []))
    if unc1 < unc0 and vio1 <= vio0:
        os.replace(tmp, out_path)
        os.unlink(base)
        log(f"    patch: ACCEPTED unconnected {unc0}->{unc1}, "
            f"violations {vio0}->{vio1}")
        return {"patched": patched, "failed": failed, "accepted": True,
                "unconnected": (unc0, unc1), "violations": (vio0, vio1)}
    # SUBSET-ACCEPT: the offending routes are identifiable — remove only the
    # added copper near NEW violations and keep the rest (all-or-nothing
    # threw away 15+ good routes over one bad one, measured)
    def _vkeys(d):
        out = set()
        for v in d.get("violations", []):
            out.add((v.get("type"), tuple(sorted(
                (round(i["pos"]["x"], 2), round(i["pos"]["y"], 2))
                for i in v.get("items", [])))))
        return out
    new_v = _vkeys(drc1) - _vkeys(drc0)
    bad_pts = [pt for _, items in new_v for pt in items]
    removed = 0
    if new_v and log:
        for t, items in list(new_v)[:6]:
            log(f"      new-violation {t} at {items}")
    for it in added_items:
        pos = it.GetPosition()
        px, py = pos.x / 1e6, pos.y / 1e6
        if any(abs(px - bx) < 2.0 and abs(py - by) < 2.0
               for bx, by in bad_pts):
            board.Remove(it)
            removed += 1
    if removed and removed < len(added_items):
        refill_zones(board)
        pcbnew.SaveBoard(tmp, board)
        drc2, _ = AD.drc_unrouted(tmp, kicad_cli)
        unc2 = len(drc2.get("unconnected_items", []))
        vio2 = len(drc2.get("violations", []))
        if unc2 < unc0 and vio2 <= vio0:
            os.replace(tmp, out_path)
            os.unlink(base)
            log(f"    patch: SUBSET-ACCEPTED after removing {removed} "
                f"offending item(s) — unconnected {unc0}->{unc2}, "
                f"violations {vio0}->{vio2}")
            return {"patched": patched, "failed": failed, "accepted": True,
                    "unconnected": (unc0, unc2),
                    "violations": (vio0, vio2), "removed": removed}
        os.unlink(tmp)
    elif os.path.exists(tmp):
        os.unlink(tmp)
    os.replace(base, out_path)      # keep the refilled baseline: strictly
    log(f"    patch: routes REVERTED, refilled baseline kept "
        f"(unconnected {unc0}->{unc1}, violations {vio0}->{vio1})")
    return {"patched": patched, "failed": failed, "accepted": False,
            "unconnected": (unc0, unc1), "violations": (vio0, vio1)}
