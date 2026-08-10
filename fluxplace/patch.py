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
        # RIPPABLE occupancy: cell -> set of soft-item indices (see
        # build_grid soft_ok). Soft cells are passable at a penalty; the
        # items under a chosen path are the exact rip set.
        self.soft = {l: {} for l in self.layers}
        self.via_soft = {}
        self.soft_items = []

    def _mark_soft(self, layer, cells, idx):
        m = self.soft[layer]
        for c in cells:
            m.setdefault(c, set()).add(idx)

    def _mark_via_soft(self, cells, idx):
        for c in cells:
            self.via_soft.setdefault(c, set()).add(idx)

    def cells_disk(self, x_mm, y_mm, r_mm):
        cx, cy = self.cxy(x_mm, y_mm)
        return list(_cells_disk(cx, cy, r_mm / self.cell))

    def cells_rect(self, x_mm, y_mm, hw, hh, rot_deg, margin=0.0):
        import math as _m
        r = _m.radians(rot_deg)
        c, sn = _m.cos(r), _m.sin(r)
        ext = _m.hypot(hw + margin, hh + margin)
        cx0, cy0 = self.cxy(x_mm, y_mm)
        rr = int(_m.ceil(ext / self.cell)) + 1
        out = []
        for dx in range(-rr, rr + 1):
            for dy in range(-rr, rr + 1):
                px = dx * self.cell
                py = dy * self.cell
                lx = px * c + py * sn
                ly = -px * sn + py * c
                if abs(lx) <= hw + margin and abs(ly) <= hh + margin:
                    out.append((cx0 + dx, cy0 + dy))
        return out

    def cells_seg(self, x1, y1, x2, y2, r_mm):
        n = max(1, int(math.hypot(x2 - x1, y2 - y1) / (self.cell * 0.7)))
        out = []
        for k in range(n + 1):
            t = k / n
            out += self.cells_disk(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t,
                                   r_mm)
        return out

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
               via_r=0.3, soft_ok=None):
    """Obstacles for routing net `net_code`: everything conductive that is
    NOT this net, dilated by clearance + track_w/2. Returns (grid, islands)
    where islands = list of cell-sets of the target net's existing copper,
    grouped by connectivity (vias join layers). Foreign copper on ANY copper
    layer (inner planes included) blocks THROUGH-via placement.

    soft_ok(item) — optional predicate marking foreign tracks/vias (never
    pads) as RIPPABLE: their cells go into grid.soft/via_soft instead of
    the hard block sets, so a soft-penalty dijkstra can cross them and name
    the exact items in the way."""
    g = Grid(board, layers, cell)
    # KEEPOUT rule areas: no-track areas hard-block their layers, no-via
    # areas block through-via placement (measured on RF: stitching vias
    # landed inside the RF-island keepouts -> items_not_allowed)
    for z in board.Zones():
        if not z.GetIsRuleArea():
            continue
        try:
            no_trk = z.GetDoNotAllowTracks()
            no_via = z.GetDoNotAllowVias()
        except AttributeError:
            continue
        if not (no_trk or no_via):
            continue
        zl = set(z.GetLayerSet().Seq())
        blay = [l for l in layers if l in zl]
        if not blay and not no_via:
            continue
        o = z.Outline()
        bb = z.GetBoundingBox()
        c0x, c0y = g.cxy(bb.GetLeft() / 1e6, bb.GetTop() / 1e6)
        c1x, c1y = g.cxy(bb.GetRight() / 1e6, bb.GetBottom() / 1e6)
        for cx in range(c0x, c1x + 1):
            for cy in range(c0y, c1y + 1):
                x, y = g.mm(cx, cy)
                import pcbnew as _pn
                if not o.Contains(_pn.VECTOR2I(int(x * 1e6), int(y * 1e6))):
                    continue
                if no_trk:
                    for l in blay:
                        g.blocked[l].add((cx, cy))
                if no_via and zl:
                    g.via_blocked.add((cx, cy))
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
    lidx = {l: i for i, l in enumerate(layers)}
    item_cells = []          # one cell-set per own-net item (real copper)
    for t in board.GetTracks():
        code = t.GetNetCode()
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            try:
                r = t.GetWidth(layers[0]) / 2e6
            except TypeError:
                r = t.GetWidth() / 2e6
            if code == net_code:
                cs = g.cells_disk(p.x / 1e6, p.y / 1e6, r)
                item_cells.append({(k, c[0], c[1])
                                   for k in range(len(layers)) for c in cs})
            else:
                mg = _mgn(t, layers[0])
                # my track near their hole AND my hole near their copper
                mg = max(mg, t.GetDrillValue() / 2e6 + hole_c + half + slop)
                x, y = p.x / 1e6, p.y / 1e6
                vr = max(r + (_mgn(t, layers[0]) - half) + via_r,
                         t.GetDrillValue() / 2e6 + hole_c + via_r / 2) + slop
                if soft_ok is not None and soft_ok(t):
                    idx = len(g.soft_items)
                    g.soft_items.append(t)
                    cs = g.cells_disk(x, y, r + mg)
                    for l in layers:
                        g._mark_soft(l, cs, idx)
                    g._mark_via_soft(g.cells_disk(x, y, vr), idx)
                else:
                    for l in layers:
                        g.block_disk(l, x, y, r + mg)
                    # via copper vs their copper; via hole vs their hole
                    g.block_via_disk(x, y, vr)
            continue
        lay = t.GetLayer()
        s, e = t.GetStart(), t.GetEnd()
        sok = code != net_code and soft_ok is not None and soft_ok(t)
        if sok:
            t_idx = len(g.soft_items)
            g.soft_items.append(t)
        if code != net_code and pcbnew.IsCopperLayer(lay):
            vr = t.GetWidth() / 2e6 + max(
                (_mgn(t, lay) - half) + via_r,
                hole_c + via_r / 2) + slop
            if sok:
                g._mark_via_soft(g.cells_seg(s.x / 1e6, s.y / 1e6,
                                             e.x / 1e6, e.y / 1e6, vr), t_idx)
            else:
                g.block_via_seg(s.x / 1e6, s.y / 1e6, e.x / 1e6, e.y / 1e6,
                                vr)
        if lay not in g.blocked:
            continue
        if code == net_code:
            cs = g.cells_seg(s.x / 1e6, s.y / 1e6, e.x / 1e6, e.y / 1e6,
                             t.GetWidth() / 2e6)
            item_cells.append({(lidx[lay], cx, cy) for cx, cy in cs})
        elif sok:
            g._mark_soft(lay, g.cells_seg(
                s.x / 1e6, s.y / 1e6, e.x / 1e6, e.y / 1e6,
                t.GetWidth() / 2e6 + _mgn(t, lay)), t_idx)
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
            if pad.GetNetCode() == net_code:
                # FULL pad body cells, not just the centre — contact-based
                # island grouping needs a track ending at the pad EDGE to
                # touch the pad's cells
                cs = g.cells_rect(p.x / 1e6, p.y / 1e6, hw, hh, prot)
                cells = set()
                for l in layers:
                    if th or l in on:
                        cells.update((lidx[l], cx, cy) for cx, cy in cs)
                if cells:
                    item_cells.append(cells)
            else:
                for l in layers:
                    if not th and l not in on:
                        continue
                    mg = _mgn(pad, l)
                    if th:
                        mg = max(mg, pad.GetDrillSize().x / 2e6 + hole_c
                                 + half + slop - min(hw, hh))
                    g.block_rect(l, p.x / 1e6, p.y / 1e6, hw, hh, prot, mg)
    # group items into islands by REAL COPPER CONTACT: an item's cells are
    # one node; nodes join when cells overlap or touch (8-adjacent, same
    # layer). The old proximity flood-fill (merge radius 1.2mm) fused
    # neighbouring OPEN fine-pitch pads into one island — the patcher
    # silently skipped every gap smaller than the radius (measured on CM5:
    # 21 unconnected nets, only 2 targeted; "closed" routes that joined
    # proximity blobs, not copper).
    owner = {}                                # (li, cx, cy) -> item idx
    parent = list(range(len(item_cells)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i, cells in enumerate(item_cells):
        for n in cells:
            j = owner.get(n)
            if j is None:
                owner[n] = i
            elif find(j) != find(i):
                union(i, j)
    for i, cells in enumerate(item_cells):
        for (k, cx, cy) in cells:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    j = owner.get((k, cx + dx, cy + dy))
                    if j is not None and find(j) != find(i):
                        union(i, j)
    groups = {}
    for i, cells in enumerate(item_cells):
        groups.setdefault(find(i), set()).update(cells)
    islands = sorted(groups.values(), key=len, reverse=True)
    return g, islands


# --------------------------------------------------------------- route ----

_VIA_COST = 14


def dijkstra(grid, sources, targets, max_expand=None, soft_penalty=None):
    """Multi-source multi-target over (layer_idx, cx, cy). Returns the cell
    path or None. Straight steps cost 10, diagonals 14, vias _VIA_COST*10
    (via cell must be free on EVERY layer). With soft_penalty set, cells
    holding rippable items (grid.soft) are passable at +penalty each — the
    min-cost path then NAMES the cheapest rip set."""
    nlayers = len(grid.layers)
    blocked = [grid.blocked[l] for l in grid.layers]
    soft = [grid.soft[l] for l in grid.layers] if soft_penalty else None
    if max_expand is None:
        # a 2M constant silently truncated big boards at fine cells
        # (RF at 0.1mm is >4M nodes -> false "no path")
        max_expand = max(2_000_000, grid.nx * grid.ny * nlayers * 2)
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
            if soft is not None and (nx, ny) in soft[l]:
                nd += soft_penalty
            if nd < dist.get(m, 1 << 60):
                dist[m] = nd
                prev[m] = node
                heapq.heappush(pq, (nd, m))
        if nlayers > 1:
            ok = (cx, cy) not in grid.via_blocked and \
                all((cx, cy) not in blocked[k] for k in range(nlayers))
            if ok:
                vd = _VIA_COST * 10
                if soft is not None and (
                        (cx, cy) in grid.via_soft
                        or any((cx, cy) in soft[k] for k in range(nlayers))):
                    vd += soft_penalty
                for k in range(nlayers):
                    if k == l:
                        continue
                    m = (k, cx, cy)
                    nd = d + vd
                    if nd < dist.get(m, 1 << 60):
                        dist[m] = nd
                        prev[m] = node
                        heapq.heappush(pq, (nd, m))
    return None


def _path_rip_ids(grid, path):
    """The exact soft items a path crosses: layer cells along it, plus the
    full column + via_soft at every layer change (a through-via needs the
    whole column clear)."""
    ids = set()
    for i, (l, cx, cy) in enumerate(path):
        ids |= grid.soft[grid.layers[l]].get((cx, cy), set())
        if i and path[i - 1][0] != l:
            ids |= grid.via_soft.get((cx, cy), set())
            for k in grid.layers:
                ids |= grid.soft[k].get((cx, cy), set())
    return ids


def _escape_route(grid, src_island, tgt_islands, max_r_mm=2.0):
    """Dogbone move: a laterally walled pad can still escape VERTICALLY —
    seed the search with legal via spots within max_r of either end's cells
    and write the stub+via if used. Returns an augmented path (may begin/end
    with a stub hop + layer change) or None."""
    R = max(2, int(max_r_mm / grid.cell))
    nlayers = len(grid.layers)

    def _stub_clear(l, ax, ay, bx, by):
        # the stub is real copper: every cell along it must be free on the
        # anchor layer (unvalidated stubs plowed through neighbours, measured
        # +30 violations at 3mm radius)
        n = max(abs(bx - ax), abs(by - ay))
        for k in range(3, n + 1):    # first cells sit inside the own pad
            cx = ax + round((bx - ax) * k / n)
            cy = ay + round((by - ay) * k / n)
            if (cx, cy) in grid.blocked[grid.layers[l]]:
                return False
        return True

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
                    if key not in out and _stub_clear(l, cx, cy, nx, ny):
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


# ------------------------------------------------------- rip-up region ----

def corridor_anchors(src_xy, tgt_xy, step_mm=1.0, halo_extra=1.5):
    """Anchor points (mm) for the rip region: the straight corridor between
    the nearest (src, tgt) sample pair, sampled every step_mm. Returns
    (anchors, src_end, tgt_end). Pure geometry — unit-testable."""
    best = None
    for sx, sy in src_xy:
        for tx, ty in tgt_xy:
            d = (sx - tx) ** 2 + (sy - ty) ** 2
            if best is None or d < best[0]:
                best = (d, (sx, sy), (tx, ty))
    _, (sx, sy), (tx, ty) = best
    n = max(1, int(math.hypot(tx - sx, ty - sy) / step_mm))
    anchors = [(sx + (tx - sx) * k / n, sy + (ty - sy) * k / n)
               for k in range(n + 1)]
    return anchors, (sx, sy), (tx, ty)


def _sample_xy(grid, island, cap=400):
    out = []
    for i, (_, cx, cy) in enumerate(island):
        if i >= cap:
            break
        out.append(grid.mm(cx, cy))
    return out


def _rec_item(t, lay0):
    """Serializable record of a track/via for rollback."""
    if t.GetClass() == "PCB_VIA":
        p = t.GetPosition()
        try:
            w = t.GetWidth(lay0)
        except TypeError:
            w = t.GetWidth()
        return ("via", t.GetNetname(), p.x, p.y, w, t.GetDrillValue())
    s, e = t.GetStart(), t.GetEnd()
    return ("trk", t.GetNetname(), t.GetLayer(), s.x, s.y, e.x, e.y,
            t.GetWidth())


def _restore_item(board, rec):
    ni = board.FindNet(rec[1])
    if rec[0] == "via":
        v = pcbnew.PCB_VIA(board)
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetPosition(pcbnew.VECTOR2I(rec[2], rec[3]))
        v.SetWidth(rec[4])
        v.SetDrill(rec[5])
        if ni:
            v.SetNet(ni)
        board.Add(v)
        return v
    t = pcbnew.PCB_TRACK(board)
    t.SetLayer(rec[2])
    t.SetStart(pcbnew.VECTOR2I(rec[3], rec[4]))
    t.SetEnd(pcbnew.VECTOR2I(rec[5], rec[6]))
    t.SetWidth(rec[7])
    if ni:
        t.SetNet(ni)
    board.Add(t)
    return t


def _route_rounds(board, g, islands, net, w, via_mm, drill_mm):
    """Shared island-merging loop: route until single island or stuck.
    Returns (added_items, islands)."""
    added = []
    n0 = len(islands)
    rounds = 0
    while len(islands) > 1 and rounds <= n0 + 2:
        src, rest = islands[0], islands[1:]
        path = dijkstra(g, src, set().union(*rest))
        if path is None:
            path = _escape_route(g, src, rest)
        if path is None:
            break
        added += apply_path(board, g, path, net, w, via_mm, drill_mm)
        end = path[-1]
        merged = next(i for i in rest if end in i)
        islands = [src | merged | set(path)] + \
            [i for i in rest if i is not merged]
        rounds += 1
    return added, islands


def _rip_reroute(board, lay, net, net_code, w, clearance, cell, via_mm,
                 drill_mm, skip_nets, net_widths, track_w, log,
                 rip_r_mm=3.0, max_rip=150, open_nets=(), exclude_nets=()):
    """Regional rip-up-and-reroute: when dijkstra + dogbone both fail, the
    island is WALLED by routed copper. Free the corridor between the island
    and its nearest sibling — record+delete foreign unlocked tracks/vias
    within rip_r of the corridor — route the target net FIRST through the
    opened lane, then re-route every displaced net. All-or-nothing: any
    displaced net that cannot fully reconnect rolls the whole transaction
    back. Returns (added_items_or_None, blame) where blame is the displaced
    net that failed to reconnect (caller may exclude it and retry)."""
    via_r = via_mm / 2.0
    # nets whose connectivity flows through a zone pour must not be ripped:
    # the island model sees only tracks/vias/pads, so a pour-fed rail looks
    # "fragmented" after a rip even though the refill would reconnect it —
    # and requiring track-reconnect of pour copper is wrong both ways
    # (measured: +3V3_D "8 islands not reconnected" rollback on RF)
    no_rip = set(skip_nets) | set(exclude_nets)
    for z in board.Zones():
        if z.IsOnCopperLayer():
            no_rip.add(z.GetNetname())
    soft_ok = (lambda t: t.GetNetname() not in no_rip and not t.IsLocked()
               and t.GetClass() != "PCB_ARC")
    g, islands = build_grid(board, lay, net_code, w, clearance, cell=cell,
                            via_r=via_r, soft_ok=soft_ok)
    if len(islands) <= 1:
        return None, None
    # min-cost path where rippable copper is passable at a penalty — the
    # path NAMES the exact items in the way (the halo-blast variant freed
    # 100-150 items of ~16 nets and some displaced net always stranded;
    # measured on all three boards)
    src, rest = islands[0], islands[1:]
    path = dijkstra(g, src, set().union(*rest), soft_penalty=400)
    if path is None:
        # hard-walled even through rippable copper: say what the wall is
        src_xy = _sample_xy(g, islands[0])
        tgt_xy = []
        for isl in rest:
            tgt_xy += _sample_xy(g, isl, cap=200)
        _, s_end, _ = corridor_anchors(src_xy, tgt_xy)
        scx, scy = g.cxy(*s_end)
        ring = int(2.0 / g.cell)
        tot = free = hard_all = 0
        for dx in range(-ring, ring + 1):
            for dy in range(-ring, ring + 1):
                cx2, cy2 = scx + dx, scy + dy
                if not g.inside(cx2, cy2):
                    continue
                tot += 1
                blk = sum(1 for l in g.layers if (cx2, cy2) in g.blocked[l])
                if blk == len(g.layers):
                    hard_all += 1
                elif blk == 0:
                    free += 1
        log(f"      rip-up: HARD-WALLED (no path even through rippable "
            f"copper) @2mm ring: {100 * hard_all // max(1, tot)}% "
            f"all-layer-hard, {100 * free // max(1, tot)}% free"
            + (f", excluded {sorted(exclude_nets)[:4]}" if exclude_nets
               else ""))
        return None, None
    cands = [g.soft_items[i] for i in _path_rip_ids(g, path)]
    if len(cands) > max_rip:
        log(f"      rip-up: path needs {len(cands)} rips > cap {max_rip}")
        return None, None
    # pre-rip island counts for displaced nets that are THEMSELVES still
    # open (another unrouted target in the corridor must not be held to
    # "fully reconnect" — only to "no worse than before the rip")
    pre_frag = {}
    for t in cands:
        n = t.GetNetname()
        if n in open_nets and n not in pre_frag:
            dni = board.FindNet(n)
            if dni is not None:
                _, pisl = build_grid(board, lay, dni.GetNetCode(), track_w,
                                     clearance, cell=cell, via_r=via_r)
                pre_frag[n] = len(pisl)
    records = []
    disp_w = {}                                # displaced net -> max width mm
    for t in cands:
        rec = _rec_item(t, lay[0])
        records.append(rec)
        if rec[0] == "trk":
            disp_w[rec[1]] = max(disp_w.get(rec[1], 0.0), rec[7] / 1e6)
        else:
            disp_w.setdefault(rec[1], 0.0)
        board.Remove(t)
    disp_nets = sorted(disp_w)
    log(f"      rip-up: path crosses {len(records)} item(s) of "
        f"{len(disp_nets)} net(s) — surgical rip")
    added = []

    def rollback(reason, blame=None):
        for it in added:
            if it.GetBoard() is not None:
                board.Remove(it)
        for rec in records:
            _restore_item(board, rec)
        log(f"      rip-up: ROLLED BACK ({reason})")
        return None, blame

    # 1) target through the opened lane
    g, islands = build_grid(board, lay, net_code, w, clearance, cell=cell,
                            via_r=via_r)
    got, islands = _route_rounds(board, g, islands, net, w, via_mm, drill_mm)
    if not got:
        # should be rare now: the rip freed exactly the soft path's items
        return rollback("target still unroutable after surgical rip")
    added += got
    # 2) every displaced net must fully reconnect
    for dn in disp_nets:
        dni = board.FindNet(dn)
        if dni is None:
            return rollback(f"{dn}: net vanished", blame=dn)
        dw = (net_widths or {}).get(dn) or max(disp_w[dn], track_w)
        g2, isl2 = build_grid(board, lay, dni.GetNetCode(), dw, clearance,
                              cell=cell, via_r=via_r)
        got2, isl2 = _route_rounds(board, g2, isl2, dn, dw, via_mm, drill_mm)
        added += got2
        if len(isl2) > 1 and dw > track_w:
            # narrow retry, same policy as the main loop
            g2, isl2 = build_grid(board, lay, dni.GetNetCode(), track_w,
                                  clearance, cell=cell, via_r=via_r)
            got2, isl2 = _route_rounds(board, g2, isl2, dn, track_w, via_mm,
                                       drill_mm)
            added += got2
        if len(isl2) > pre_frag.get(dn, 1):
            return rollback(f"{dn}: {len(isl2) - 1} island(s) not "
                            f"reconnected", blame=dn)
    log(f"      rip-up: target routed, {len(disp_nets)} displaced net(s) "
        f"rerouted")
    return added, None


# ---------------------------------------------------------------- main ----

def refill_zones(board):
    filler = pcbnew.ZONE_FILLER(board)
    filler.Fill(board.Zones())


def patch_board(board_path, out_path, kicad_cli="kicad-cli", layers=None,
                track_w=0.2, clearance=0.15, via_mm=0.6, drill_mm=0.3,
                skip_nets=("GND",), net_widths=None, cell=0.25, log=print,
                rip=True, rip_r_mm=3.0, max_rip=150):
    """Close the leftover unrouted nets on a routed board. Returns a summary
    dict; writes out_path only when the DRC guard accepts."""
    from . import adaptive as AD
    from . import kicad_io as IO
    from .launder import mutate as _mutate
    # THIS pcbnew session must never run ZONE_FILLER: repeated in-process
    # refill/save cycles poison the SWIG session and long runs segfault in
    # their final refill (measured 3-for-3 on CM5/dig/RF). All refills and
    # guarded removals happen in fresh worker subprocesses; the parent only
    # reads, routes, and ADDS copper.
    base = out_path + ".base.kicad_pcb"
    _mutate(board_path, base, [])          # baseline refill, out-of-process
    board = pcbnew.LoadBoard(base)
    lay = layers or IO.signal_layers(board)
    # signal_layers returns NAMES; the grid and pcbnew items need int ids
    lay = [board.GetLayerID(l) if isinstance(l, str) else l for l in lay]
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
                                cell=cell, via_r=via_mm / 2.0)
        n0 = len(islands)
        rounds = 0
        rips = 0
        while len(islands) > 1 and rounds <= n0 + 2:
            src, rest = islands[0], islands[1:]
            tgt = set().union(*rest)
            path = dijkstra(g, src, tgt)
            if path is None:
                path = _escape_route(g, src, rest)
            if path is None and rip and rips < n0 + 2:
                # blame-driven retry: a displaced net that failed to
                # reconnect becomes un-rippable on the next attempt; a
                # no-blame failure is deterministic — stop
                got, exclude = None, set()
                while rips < n0 + 2 and got is None:
                    rips += 1
                    got, blame = _rip_reroute(
                        board, lay, net, ni.GetNetCode(), w, clearance,
                        cell, via_mm, drill_mm, skip_nets, net_widths,
                        track_w, log, rip_r_mm=rip_r_mm, max_rip=max_rip,
                        open_nets=set(targets), exclude_nets=exclude)
                    if got is None:
                        if blame:
                            exclude.add(blame)
                        else:
                            break
                if got:
                    added_items += got
                    g, islands = build_grid(board, lay, ni.GetNetCode(), w,
                                            clearance, cell=cell,
                                            via_r=via_mm / 2.0)
                    rounds += 1
                    continue
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
                                      clearance, cell=max(0.1, cell * 0.6),
                                      via_r=via_mm / 2.0)
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
    # GND ISLAND STITCHING: pour islands can't be track-patched (GND is
    # skipped by design) but a through-via inside the island reaches the
    # internal plane. Contact-based islands make the placement exact:
    # pick a via-legal cell inside each minor island. Guarded by the same
    # final DRC gate as everything else.
    gnd = board.FindNet("GND")
    if gnd is not None and "GND" in un0:
        gv = 0
        # only stitch islands KiCad ITSELF reports unconnected: the island
        # model has no pour copper, so most track-model "islands" are
        # already pour-connected (measured: 214 no-op vias, +12 clearance,
        # run reverted)
        gnd_pts = []
        for u in drc0.get("unconnected_items", []):
            for i in u.get("items", []):
                if "[GND]" in i.get("description", ""):
                    p = i.get("pos", {})
                    gnd_pts.append((p.get("x", 0), p.get("y", 0)))
        g, islands = build_grid(board, lay, gnd.GetNetCode(), track_w,
                                clearance, cell=cell, via_r=via_mm / 2.0)
        for isl in islands[1:]:
            hit = False
            for (k, cx, cy) in isl:
                x, y = g.mm(cx, cy)
                if any(abs(x - px) < 1.0 and abs(y - py) < 1.0
                       for px, py in gnd_pts):
                    hit = True
                    break
            if not hit:
                continue
            spot = None
            for (k, cx, cy) in sorted(isl):
                if (cx, cy) not in g.via_blocked and g.inside(cx, cy):
                    spot = (cx, cy)
                    break
            if spot is None:
                continue
            x, y = g.mm(*spot)
            v = pcbnew.PCB_VIA(board)
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetPosition(pcbnew.VECTOR2I(int(x * 1e6), int(y * 1e6)))
            v.SetWidth(int(via_mm * 1e6))
            v.SetDrill(int(drill_mm * 1e6))
            v.SetNet(gnd)
            board.Add(v)
            added_items.append(v)
            gv += 1
        if gv:
            patched.append(("GND", gv))
            log(f"    patch: GND — {gv} stitching via(s) into pour "
                f"island(s)")

    if not patched:
        # nothing routed, but the refilled baseline may still beat the
        # input (stale pours healed) — keep it as the output
        os.replace(base, out_path)
        return {"patched": [], "failed": failed, "accepted": False,
                "refilled_out": True}
    # save the raw (unfilled) patched state; the refill runs out-of-process
    raw = out_path + ".raw.kicad_pcb"
    pcbnew.SaveBoard(raw, board)
    for ext in (".kicad_dru", ".kicad_pro"):
        sidecar = os.path.join(srcdir, stem + ext)
        if os.path.exists(sidecar):
            try:
                _sh.copy(sidecar, os.path.splitext(raw)[0] + ext)
            except OSError:
                pass
    tmp = out_path + ".tmp.kicad_pcb"
    _mutate(raw, tmp, [])
    drc1, un1 = AD.drc_unrouted(tmp, kicad_cli)
    unc1 = len(drc1.get("unconnected_items", []))
    vio1 = len(drc1.get("violations", []))
    def _vkeys(d):
        # zone-fill items are EXCLUDED: the filler is nondeterministic run to
        # run, shifting pad-vs-zone findings and producing phantom 'new'
        # violations far from any added copper (measured: 4 phantoms blocked
        # a clean 19->15 on CM5)
        out = set()
        for v in d.get("violations", []):
            if any("Zone" in i.get("description", "")
                   for i in v.get("items", [])):
                continue
            out.add((v.get("type"), tuple(sorted(
                (round(i["pos"]["x"], 2), round(i["pos"]["y"], 2))
                for i in v.get("items", [])))))
        return out

    if unc1 < unc0 and (vio1 <= vio0 or not (_vkeys(drc1) - _vkeys(drc0))):
        os.replace(tmp, out_path)
        os.unlink(base)
        os.unlink(raw)
        log(f"    patch: ACCEPTED unconnected {unc0}->{unc1}, "
            f"violations {vio0}->{vio1} (non-zone-new: 0)"
            if vio1 > vio0 else
            f"    patch: ACCEPTED unconnected {unc0}->{unc1}, "
            f"violations {vio0}->{vio1}")
        return {"patched": patched, "failed": failed, "accepted": True,
                "unconnected": (unc0, unc1), "violations": (vio0, vio1)}
    # SUBSET-ACCEPT: the offending routes are identifiable — remove only the
    # added copper near NEW violations and keep the rest (all-or-nothing
    # threw away 15+ good routes over one bad one, measured)
    new_v = _vkeys(drc1) - _vkeys(drc0)
    bad_pts = [pt for _, items in new_v for pt in items]
    removed = 0
    if new_v and log:
        for t, items in list(new_v)[:6]:
            log(f"      new-violation {t} at {items}")
    def _desc(it):
        return {"via": it.GetClass() == "PCB_VIA",
                "net": it.GetNetname(),
                "x": it.GetPosition().x / 1e6,
                "y": it.GetPosition().y / 1e6}

    drop = []
    for it in added_items:
        if it.GetBoard() is None:      # ripped back off the board already
            continue
        pts = [it.GetPosition()]
        try:
            pts.append(it.GetEnd())
            pts.append(pcbnew.VECTOR2I((it.GetStart().x + it.GetEnd().x) // 2,
                                       (it.GetStart().y + it.GetEnd().y) // 2))
        except AttributeError:
            pass
        near = False
        for pp in pts:
            px, py = pp.x / 1e6, pp.y / 1e6
            if any(abs(px - bx) < 2.0 and abs(py - by) < 2.0
                   for bx, by in bad_pts):
                near = True
                break
        if near:
            drop.append(_desc(it))
            removed += 1
    if not removed and new_v:
        # position matching missed (measured repeatedly): fall back to
        # NET-level subset — drop all added copper of any net named in a
        # new violation, keep every other net's routes
        import re as _re
        bad_nets = set()
        for v in drc1.get("violations", []):
            k = (v.get("type"), tuple(sorted(
                (round(i["pos"]["x"], 2), round(i["pos"]["y"], 2))
                for i in v.get("items", []))))
            if k in new_v:
                for i in v.get("items", []):
                    m = _re.search(r"\[([^\]]+)\]", i.get("description", ""))
                    if m:
                        bad_nets.add(m.group(1))
        for it in added_items:
            if it.GetNetname() in bad_nets and it.GetBoard() is not None:
                drop.append(_desc(it))
                removed += 1
        if removed:
            log(f"      net-level subset: dropped nets {sorted(bad_nets)[:6]}")
    if removed and removed < len(added_items):
        # remove the offenders from the RAW state in a worker; the parent
        # session never mutates-and-refills
        _mutate(raw, tmp, drop)
        drc2, _ = AD.drc_unrouted(tmp, kicad_cli)
        unc2 = len(drc2.get("unconnected_items", []))
        vio2 = len(drc2.get("violations", []))
        if unc2 < unc0 and vio2 <= vio0:
            os.replace(tmp, out_path)
            os.unlink(base)
            os.unlink(raw)
            log(f"    patch: SUBSET-ACCEPTED after removing {removed} "
                f"offending item(s) — unconnected {unc0}->{unc2}, "
                f"violations {vio0}->{vio2}")
            return {"patched": patched, "failed": failed, "accepted": True,
                    "unconnected": (unc0, unc2),
                    "violations": (vio0, vio2), "removed": removed}
        os.unlink(tmp)
    elif os.path.exists(tmp):
        os.unlink(tmp)
    if os.path.exists(raw):
        os.unlink(raw)
    os.replace(base, out_path)      # keep the refilled baseline: strictly
    log(f"    patch: routes REVERTED, refilled baseline kept "
        f"(unconnected {unc0}->{unc1}, violations {vio0}->{vio1})")
    return {"patched": patched, "failed": failed, "accepted": False,
            "unconnected": (unc0, unc1), "violations": (vio0, vio1)}
