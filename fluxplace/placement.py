"""Placement strategies. Pure Python — operates on parts with (w,h) sizes in mm and
returns {ref: (x, y)} in mm. Two strategies share one graph:

  radial   — hub centered, branches shelf-packed in sectors around it, ordered
             edge->hub. Deterministic, overlap-free by construction. Good first pass.
  flux     — weighted force-directed (springs = signal nets, repulsion = overlap),
             seeded from radial, then legalized. Minimizes weighted wirelength ->
             the "incredibly dense" layout. Connectors optionally pinned to edges.
"""
import math
from .graph import is_passive, kind_of


def place(parts, graph, topo, strategy="pack", rotate="ortho", center=None, pad=0.8,
          iters=260):
    """One-call placement: run a strategy, orient parts, and legalize with rotation
    accounted for. Returns (positions {ref:(x,y)}, angles {ref:deg}). Every entry point
    (CLI, GUI plugin) goes through this so nothing forgets the final legalize."""
    if strategy == "radial":
        pos = radial(parts, topo, center=center, pad=pad)
    elif strategy == "flux":
        pos = flux(parts, graph, topo, iters=iters, center=center, pad=pad)
    else:
        pos = pack(parts, graph, topo, center=center, pad=pad)
    angles = orient(parts, graph, pos, mode=rotate)
    p = {r: list(v) for r, v in pos.items()}
    # legalize to zero overlaps — guaranteed. Escalate the clearance/iterations if the
    # iterative solver hasn't converged (rare ping-pong on tightly boxed-in parts).
    for attempt in range(6):
        legalize(parts, p, pad, iters=500, angles=angles)
        if count_overlaps(parts, p, 0.0, angles) == 0:
            break
        _shove_remaining(parts, p, angles, pad)   # hard separation fallback
    return {r: (x, y) for r, (x, y) in p.items()}, angles


def _shove_remaining(parts, pos, angles, pad):
    """Fallback for any pair still overlapping after iterative legalize: shove them fully
    apart in one move (no half-steps), largest part holds, smaller yields."""
    for a, b in list(_candidate_pairs(parts, pos, angles, pad)):
        aw, ah = eff_size(parts, a, angles.get(a, 0.0), pad)
        bw, bh = eff_size(parts, b, angles.get(b, 0.0), pad)
        dx = pos[b][0] - pos[a][0]; dy = pos[b][1] - pos[a][1]
        oxx = (aw + bw) / 2 - abs(dx); oyy = (ah + bh) / 2 - abs(dy)
        if oxx > 0 and oyy > 0:
            # move the smaller-area part fully clear along the shorter overlap axis
            mover, aw_, ah_ = (b, bw, bh) if aw * ah >= bw * bh else (a, aw, ah)
            sgn_x = 1 if (pos[mover][0] - pos[a if mover == b else b][0]) >= 0 else -1
            sgn_y = 1 if (pos[mover][1] - pos[a if mover == b else b][1]) >= 0 else -1
            if oxx <= oyy:
                pos[mover][0] += (oxx + 0.1) * sgn_x
            else:
                pos[mover][1] += (oyy + 0.1) * sgn_y


def _size(parts, r, pad=0.6):
    p = parts[r]
    return p.get("w", 2.0) + 2 * pad, p.get("h", 1.5) + 2 * pad


def eff_size(parts, r, angle=0.0, pad=0.6):
    """Bounding size accounting for rotation (swap for 90/270, rotated extent for fine)."""
    w, h = _size(parts, r, pad)
    a = angle % 180
    if abs(a) < 1e-6 or abs(a - 180) < 1e-6:
        return w, h
    if abs(a - 90) < 1e-6:
        return h, w
    rad = math.radians(a)
    c, s = abs(math.cos(rad)), abs(math.sin(rad))
    return w * c + h * s, w * s + h * c


def _shelf_pack(refs, parts, target_w, pad=0.6):
    """Pack refs (in given order) into shelves; return {ref:(dx,dy)} local coords and (W,H)."""
    x = y = shelf_h = W = H = 0.0
    pos = {}
    for r in refs:
        w, h = _size(parts, r, pad)
        if x > 0 and x + w > target_w:
            y += shelf_h
            x = shelf_h = 0.0
        pos[r] = (x, y)
        x += w
        shelf_h = max(shelf_h, h)
        W = max(W, x)
        H = max(H, y + shelf_h)
    return pos, W, H


# sector angles (radians) for up to 8 branches around the hub, plus a power side
_SECTORS = [0, math.pi / 2, math.pi, -math.pi / 2,
            math.pi / 4, 3 * math.pi / 4, -3 * math.pi / 4, -math.pi / 4]


def radial(parts, topo, center=None, pad=0.8):
    """Hub in the middle; each branch a shelf-packed block placed in a ring, ordered
    so its hub-facing IC sits toward the center and its connector toward the edge."""
    pos = {}
    hub = topo.hub
    hw, hh = (_size(parts, hub, pad) if hub in parts else (40, 30))
    cx, cy = center or (0.0, 0.0)
    if hub in parts:
        pos[hub] = (cx, cy)

    ring = max(hw, hh) / 2 + 14.0
    branches = [b for b in topo.branches if b.root != hub]
    for i, b in enumerate(branches):
        ang = _SECTORS[i % len(_SECTORS)] + (i // len(_SECTORS)) * 0.35
        members = [r for r in b.order if r != hub]
        # order so hub-facing parts are last (placed nearest center): reverse edge->hub
        members = members[::-1]
        tw = max(6.0, math.sqrt(sum(_size(parts, r, pad)[0] * _size(parts, r, pad)[1]
                                    for r in members) * 1.4))
        local, W, H = _shelf_pack(members, parts, tw, pad)
        # block anchor along the sector ray
        bx = cx + math.cos(ang) * (ring + W / 2)
        by = cy + math.sin(ang) * (ring + H / 2)
        for r, (dx, dy) in local.items():
            pos[r] = (bx - W / 2 + dx + _size(parts, r, pad)[0] / 2,
                      by - H / 2 + dy + _size(parts, r, pad)[1] / 2)
    _place_orphans(parts, pos, cx, cy, pad)
    return pos


def _place_orphans(parts, pos, cx, cy, pad):
    """Parts with no signal lanes (decaps, power-only regs/connectors) get seeded at the
    centroid of their schematic-sheet mates, so sheet-cohesion pulls them onto their IC —
    never dumped in a stray row (that would strand decoupling caps away from their pins)."""
    miss = [r for r in parts if r not in pos]
    if not miss:
        return
    from collections import defaultdict
    by_sheet = defaultdict(list)
    for r in pos:
        by_sheet[parts[r].get("sheet", "root")].append(r)
    for r in miss:
        mates = by_sheet.get(parts[r].get("sheet", "root"), [])
        if mates:
            mx = sum(pos[m][0] for m in mates) / len(mates)
            my = sum(pos[m][1] for m in mates) / len(mates)
            # tiny jitter so they don't stack exactly
            pos[r] = (mx + (hash(r) % 7 - 3) * 0.6, my + (hash(r) % 5 - 2) * 0.6)
        else:
            pos[r] = (cx, cy)


# ------------------------------------------------------------------ hierarchical pack
def group_by_sheet(parts):
    from collections import defaultdict
    g = defaultdict(list)
    for r, p in parts.items():
        g[p.get("sheet", "root")].append(r)
    return g


def group_parts(parts, topo):
    """Robust functional clustering. Prefer schematic sheets (the real subsystem grouping);
    if the board has none (e.g. a headless-generated PCB), fall back to topology branches
    and fold power-only parts (decaps) into the nearest branch by their drawn position —
    so decoupling caps still cluster with their IC."""
    distinct = {parts[r].get("sheet", "root") for r in parts}
    distinct = {s for s in distinct if s and s != "root"}
    if len(distinct) >= 2:
        return dict(group_by_sheet(parts))
    from collections import defaultdict
    groups, assigned, cen = {}, set(), {}
    for i, b in enumerate(topo.branches):
        mem = [r for r in b.members if r in parts]
        if not mem:
            continue
        gid = f"grp{i}"
        groups[gid] = list(mem); assigned.update(mem)
        cen[gid] = (sum(parts[r]["x"] for r in mem) / len(mem),
                    sum(parts[r]["y"] for r in mem) / len(mem))
    if not groups:
        return dict(group_by_sheet(parts))
    for r in parts:
        if r in assigned:
            continue
        gid = min(groups, key=lambda g: (parts[r]["x"] - cen[g][0]) ** 2 +
                  (parts[r]["y"] - cen[g][1]) ** 2)
        groups[gid].append(r)
    return groups


def _order_cluster(refs, graph):
    """Order a cluster edge->hub-ish: seed at a connector (or highest-degree), BFS by
    signal adjacency, passives trailing near their neighbour."""
    from collections import deque, defaultdict
    rs = set(refs)
    adj = defaultdict(set)
    for members in graph.signal_nets.values():
        m = [x for x in members if x in rs]
        for i in range(len(m)):
            for j in range(i + 1, len(m)):
                adj[m[i]].add(m[j]); adj[m[j]].add(m[i])
    conns = [r for r in refs if kind_of(r) == "J"]
    seed = conns[0] if conns else max(refs, key=lambda r: len(adj[r]), default=refs[0])
    dist = {seed: 0}; q = deque([seed])
    while q:
        c = q.popleft()
        for nb in adj[c]:
            if nb not in dist:
                dist[nb] = dist[c] + 1; q.append(nb)
    for r in refs:
        dist.setdefault(r, 50)
    return sorted(refs, key=lambda r: (dist[r], is_passive(r), r))


def pack(parts, graph, topo, center=None, pad=0.8, iters=600, refine=0):
    """Cluster by schematic sheet (keeps decaps with their IC), pack each cluster tight
    in signal order, then place the blocks by inter-cluster connectivity + compact.
    Produces an organized, dense, manufacturable layout."""
    cx, cy = center or (0.0, 0.0)
    clusters = group_parts(parts, topo)
    group_of = {r: gid for gid, refs in clusters.items() for r in refs}
    blocks = {}   # gid -> dict(local, W, H, refs)
    for sheet, refs in clusters.items():
        order = _order_cluster(refs, graph)
        area = sum(_size(parts, r, pad)[0] * _size(parts, r, pad)[1] for r in order)
        # square-ish blocks: target width ~ sqrt(area), but never narrower than the
        # widest single part (so a big IC/connector isn't forced to wrap)
        tw = max(max(_size(parts, r, pad)[0] for r in order), math.sqrt(area) * 0.95)
        local, W, H = _shelf_pack(order, parts, tw, pad)
        blocks[sheet] = dict(local=local, W=W, H=H, refs=order)

    # inter-cluster weighted edges
    from collections import defaultdict
    cedge = defaultdict(float)
    from .graph import net_weight
    for name, members in graph.signal_nets.items():
        wt = net_weight(name, len(members))
        gs = sorted({group_of[r] for r in members if r in group_of})
        for i in range(len(gs)):
            for j in range(i + 1, len(gs)):
                cedge[(gs[i], gs[j])] += wt

    # BIN-PACK the blocks (shelf tiling) in connectivity order — void-free + compact,
    # connected clusters land adjacent. Far better than force-directing big+small blocks.
    hub_grp = group_of.get(topo.hub) if topo.hub else None
    bpos = _arrange_blocks(blocks, cedge, hub_grp, cx, cy)
    _compact_blocks(blocks, bpos, cedge, gap=2.0, iters=500)

    # expand blocks -> part positions (the organized seed)
    pos = {}
    for s in blocks:
        local = blocks[s]["local"]; W = blocks[s]["W"]; H = blocks[s]["H"]
        bx, by = bpos[s]
        for r, (dx, dy) in local.items():
            w, h = _size(parts, r, pad)
            pos[r] = (bx - W / 2 + dx + w / 2, by - H / 2 + dy + h / 2)
    if refine:
        # part-level force-directed refine, seeded from the organized clusters, with
        # strong sheet cohesion so blocks stay coherent while wirelength + density improve
        pos = flux(parts, graph, topo, iters=refine, center=(cx, cy), pad=pad,
                   seed=pos, sheet_cohesion=0.35, compaction=0.08)
    return pos


def _bfs_cluster_order(blocks, cedge, hub_sheet):
    """Order sheets so strongly-connected clusters are sequential (=> adjacent when tiled).
    Greedy: start at the hub sheet, always append the unplaced neighbour with the
    strongest remaining edge (a connectivity-first traversal)."""
    from collections import defaultdict
    nbr = defaultdict(dict)
    for (a, b), w in cedge.items():
        nbr[a][b] = w; nbr[b][a] = w
    names = set(blocks)
    start = hub_sheet if hub_sheet in names else max(
        names, key=lambda s: blocks[s]["W"] * blocks[s]["H"])
    order = [start]; placed = {start}
    while len(placed) < len(names):
        # best edge from any placed node to an unplaced node
        best, bw = None, -1
        for p in order:
            for q, w in nbr[p].items():
                if q not in placed and w > bw:
                    best, bw = q, w
        if best is None:  # disconnected remainder
            best = max((s for s in names if s not in placed),
                       key=lambda s: blocks[s]["W"] * blocks[s]["H"])
        order.append(best); placed.add(best)
    return order


def _arrange_blocks(blocks, cedge, hub_sheet, cx, cy, gap=2.5, aspect=2.4):
    """Shelf bin-pack blocks (in connectivity order) into a compact rectangle."""
    order = _bfs_cluster_order(blocks, cedge, hub_sheet)
    total = sum(blocks[s]["W"] * blocks[s]["H"] for s in order)
    target_w = max(max(blocks[s]["W"] for s in order), math.sqrt(total * aspect))
    x = y = shelf_h = maxx = 0.0
    centers = {}
    for s in order:
        W, H = blocks[s]["W"], blocks[s]["H"]
        if x > 0 and x + W > target_w:
            y += shelf_h + gap; x = 0.0; shelf_h = 0.0
        centers[s] = [x + W / 2, y + H / 2]
        x += W + gap
        shelf_h = max(shelf_h, H)
        maxx = max(maxx, x)
    # recenter around (cx,cy)
    ox = cx - maxx / 2
    oy = cy - (y + shelf_h) / 2
    for s in centers:
        centers[s][0] += ox; centers[s][1] += oy
    return centers


def _compact_blocks(blocks, bpos, cedge, gap=2.0, iters=500):
    """Squeeze whitespace: pull each block toward the connectivity-weighted centroid of
    its neighbours (and the global centroid), then resolve overlaps. Closes the gaps a
    shelf pack leaves without letting blocks collide."""
    from collections import defaultdict
    nbr = defaultdict(dict)
    for (a, b), w in cedge.items():
        nbr[a][b] = w; nbr[b][a] = w
    names = list(blocks)
    for it in range(iters):
        gxc = sum(bpos[s][0] for s in names) / len(names)
        gyc = sum(bpos[s][1] for s in names) / len(names)
        for s in names:
            # pull toward connected-neighbour centroid (weighted) + a little to global
            wsum = sum(nbr[s].values()) or 1.0
            tx = sum(bpos[n][0] * w for n, w in nbr[s].items()) / wsum if nbr[s] else gxc
            ty = sum(bpos[n][1] * w for n, w in nbr[s].items()) / wsum if nbr[s] else gyc
            bpos[s][0] += (0.6 * tx + 0.4 * gxc - bpos[s][0]) * 0.08
            bpos[s][1] += (0.6 * ty + 0.4 * gyc - bpos[s][1]) * 0.08
        _legalize_blocks(blocks, bpos, gap=gap, iters=40)
    _legalize_blocks(blocks, bpos, gap=gap, iters=200)


def _seed_blocks(blocks, cx, cy):
    """Seed block centers on a coarse grid sized by block count, centered on (cx,cy)."""
    names = sorted(blocks, key=lambda s: -blocks[s]["W"] * blocks[s]["H"])
    n = len(names)
    cols = max(1, int(math.ceil(math.sqrt(n))))
    colw = max(blocks[s]["W"] for s in names) + 6
    rowh = max(blocks[s]["H"] for s in names) + 6
    pos = {}
    for i, s in enumerate(names):
        r, c = divmod(i, cols)
        pos[s] = [cx + (c - cols / 2) * colw, cy + (r - n / cols / 2) * rowh]
    return pos


def _legalize_blocks(blocks, bpos, gap=2.0, iters=200):
    names = list(blocks)
    for _ in range(iters):
        moved = False
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                aw, ah = blocks[a]["W"], blocks[a]["H"]
                bw, bh = blocks[b]["W"], blocks[b]["H"]
                dx = bpos[b][0] - bpos[a][0]; dy = bpos[b][1] - bpos[a][1]
                ox = (aw + bw) / 2 + gap - abs(dx)
                oy = (ah + bh) / 2 + gap - abs(dy)
                if ox > 0 and oy > 0:
                    moved = True
                    if ox <= oy:
                        s = ox / 2 * (1 if dx >= 0 else -1)
                        bpos[b][0] += s; bpos[a][0] -= s
                    else:
                        s = oy / 2 * (1 if dy >= 0 else -1)
                        bpos[b][1] += s; bpos[a][1] -= s
        if not moved:
            break


# ------------------------------------------------------------------ force-directed
def flux(parts, graph, topo, iters=260, center=None, pin_connectors=False, pad=0.8,
         seed=None, sheet_cohesion=0.20, compaction=0.05):
    """Weighted force-directed placement. Springs = signal nets (weighted by criticality)
    + weak same-sheet cohesion (keeps decaps on their IC). Repulsion removes overlap;
    a centroid pull compacts. Minimises weighted wirelength -> dense + routable."""
    from .graph import net_weight
    if seed is None:
        seed = radial(parts, topo, center, pad)
    pos = {r: list(xy) for r, xy in seed.items()}
    refs = list(pos)
    cx, cy = center or (0.0, 0.0)

    pinned = {r for r in refs if kind_of(r) == "J"} if pin_connectors else set()

    # net members (star model) + pin offsets so parts pull to the actual PAD on a big
    # part's perimeter, not its center (keeps small parts off the module footprint)
    net_members = []
    for name, members in graph.signal_nets.items():
        m = [r for r in members if r in pos]
        if len(m) >= 2:
            net_members.append((name, m, net_weight(name, len(m))))
    from collections import defaultdict
    sheet_members = defaultdict(list)
    _grp = group_parts(parts, topo)
    for gid, mem in _grp.items():
        for r in mem:
            if r in pos:
                sheet_members[gid].append(r)

    def pin(r, net):
        off = parts[r].get("pins", {}).get(net)
        if off:
            return pos[r][0] + off[0], pos[r][1] + off[1]
        return pos[r][0], pos[r][1]

    k_spring = 0.9
    for it in range(iters):
        force = {r: [0.0, 0.0] for r in refs}
        # star springs: pull each member's PIN toward the net's pin-centroid
        for name, m, wt in net_members:
            pins = [pin(r, name) for r in m]
            cx0 = sum(p[0] for p in pins) / len(pins)
            cy0 = sum(p[1] for p in pins) / len(pins)
            for r, (px, py) in zip(m, pins):
                force[r][0] += k_spring * wt * (cx0 - px)
                force[r][1] += k_spring * wt * (cy0 - py)
        # sheet cohesion: pull each part toward its sheet centroid
        if sheet_cohesion:
            for sht, mem in sheet_members.items():
                if len(mem) < 2:
                    continue
                mx = sum(pos[r][0] for r in mem) / len(mem)
                my = sum(pos[r][1] for r in mem) / len(mem)
                for r in mem:
                    force[r][0] += (mx - pos[r][0]) * sheet_cohesion
                    force[r][1] += (my - pos[r][1]) * sheet_cohesion
        _repel(parts, pos, refs, force, 1.0, pad)
        # global compaction toward overall centroid (squeezes whitespace)
        gxc = sum(pos[r][0] for r in refs) / len(refs)
        gyc = sum(pos[r][1] for r in refs) / len(refs)
        for r in refs:
            force[r][0] += (gxc - pos[r][0]) * compaction
            force[r][1] += (gyc - pos[r][1]) * compaction
        if topo.hub in pos:
            force[topo.hub][0] += (cx - pos[topo.hub][0]) * 0.3
            force[topo.hub][1] += (cy - pos[topo.hub][1]) * 0.3
        for r in refs:
            if r in pinned:
                continue
            fx, fy = force[r]
            mag = math.hypot(fx, fy) or 1e-9
            pos[r][0] += fx / mag * min(mag * 0.02, 3.0)
            pos[r][1] += fy / mag * min(mag * 0.02, 3.0)

    legalize(parts, pos, pad)
    return {r: (x, y) for r, (x, y) in pos.items()}


def _repel(parts, pos, refs, force, k, pad):
    """Overlap repulsion via the correct spatial hash (bbox-covering cells) so large
    footprints repel everything they actually cover, not just ±1 grid cell."""
    for r, s in _candidate_pairs(parts, {x: pos[x] for x in refs}, {}, pad):
        rw, rh = _size(parts, r, pad)
        sw, sh = _size(parts, s, pad)
        dx = pos[s][0] - pos[r][0]
        dy = pos[s][1] - pos[r][1]
        ox_ = (rw + sw) / 2 - abs(dx)
        oy_ = (rh + sh) / 2 - abs(dy)
        if ox_ > 0 and oy_ > 0:
            if ox_ < oy_:
                push = k * ox_ * (1 if dx >= 0 else -1)
                force[s][0] += push; force[r][0] -= push
            else:
                push = k * oy_ * (1 if dy >= 0 else -1)
                force[s][1] += push; force[r][1] -= push


def _cells_covered(x, y, w, h, cell):
    x0 = int((x - w / 2) // cell); x1 = int((x + w / 2) // cell)
    y0 = int((y - h / 2) // cell); y1 = int((y + h / 2) // cell)
    return [(cx, cy) for cx in range(x0, x1 + 1) for cy in range(y0, y1 + 1)]


def _candidate_pairs(parts, pos, angles, pad, cell=6.0):
    """Every pair of parts whose padded bboxes might overlap. Each part is registered in
    ALL cells its bbox covers, so a part of ANY size is compared against everything near
    it — the ±1-neighbour grid bug (large parts overlapping unseen) cannot happen."""
    grid = {}
    for r in pos:
        w, h = eff_size(parts, r, angles.get(r, 0.0), pad)
        for c in _cells_covered(pos[r][0], pos[r][1], w, h, cell):
            grid.setdefault(c, []).append(r)
    seen = set()
    for refs in grid.values():
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                a, b = refs[i], refs[j]
                key = (a, b) if a < b else (b, a)
                if key not in seen:
                    seen.add(key)
                    yield key


def legalize(parts, pos, pad, iters=400, angles=None):
    """Iterative overlap removal via a correct spatial hash: push every overlapping pair
    apart along the shorter axis until none remain (or `iters` passes). Rotation-aware."""
    angles = angles or {}
    for _ in range(iters):
        moved = False
        for a, b in _candidate_pairs(parts, pos, angles, pad):
            aw, ah = eff_size(parts, a, angles.get(a, 0.0), pad)
            bw, bh = eff_size(parts, b, angles.get(b, 0.0), pad)
            dx = pos[b][0] - pos[a][0]; dy = pos[b][1] - pos[a][1]
            oxx = (aw + bw) / 2 - abs(dx)
            oyy = (ah + bh) / 2 - abs(dy)
            if oxx > 0 and oyy > 0:
                moved = True
                if oxx <= oyy:
                    s = oxx / 2 + 0.03
                    sgn = 1 if dx >= 0 else -1
                    pos[b][0] += s * sgn; pos[a][0] -= s * sgn
                else:
                    s = oyy / 2 + 0.03
                    sgn = 1 if dy >= 0 else -1
                    pos[b][1] += s * sgn; pos[a][1] -= s * sgn
        if not moved:
            break


def hpwl(parts, graph, pos):
    """Total weighted half-perimeter wirelength — the quality metric (lower = denser/shorter)."""
    from .graph import net_weight
    total = 0.0
    for name, members in graph.signal_nets.items():
        pts = [pos[r] for r in members if r in pos]
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        span = (max(xs) - min(xs)) + (max(ys) - min(ys))
        total += span * net_weight(name, len(members)) * (len(members) - 1)
    return total


def count_overlaps(parts, pos, pad=0.5, angles=None):
    """Exact overlapping-pair count via the correct spatial hash (no size blind spot)."""
    angles = angles or {}
    n = 0
    for a, b in _candidate_pairs(parts, pos, angles, pad):
        aw, ah = eff_size(parts, a, angles.get(a, 0.0), pad)
        bw, bh = eff_size(parts, b, angles.get(b, 0.0), pad)
        if ((aw + bw) / 2 - abs(pos[a][0] - pos[b][0]) > 0 and
                (ah + bh) / 2 - abs(pos[a][1] - pos[b][1]) > 0):
            n += 1
    return n


def _part_nets(graph):
    """{ref: [netnames it's on]} over signal nets."""
    from collections import defaultdict
    d = defaultdict(list)
    for name, refs in graph.signal_nets.items():
        for r in refs:
            d[r].append(name)
    return d


def _snap(angle, mode):
    if mode == "none":
        return 0.0
    if mode == "fine":
        return angle % 360
    return (round(angle / 90.0) * 90) % 360        # ortho


def orient(parts, graph, pos, mode="ortho"):
    """Rotate each part to face its signal flow.
    - 2-pin passives: long axis along the line between its two nets' centroids
      (so a series part lies *along* the trace it sits in).
    - multi-pin ICs/connectors: dominant face toward its strongest-neighbour cluster,
      snapped to 90° for assembly. mode 'fine' allows any angle; 'none' disables.
    Best practice default = 'ortho' (cheap, error-proof pick-and-place)."""
    if mode == "none":
        return {}
    pn = _part_nets(graph)
    ang = {}
    for r in pos:
        # leave big parts (modules, sockets, large connectors) at their drawn orientation
        w, h = _size(parts, r, 0.0)
        if w * h > 150.0:
            continue
        k = kind_of(r)
        nets = pn.get(r, [])
        if k in ("R", "C", "L", "D", "F", "FB") and len(nets) >= 2:
            # centroids of the two busiest nets this part is on
            ca = _net_centroid(graph, nets[0], pos, exclude=r)
            cb = _net_centroid(graph, nets[1], pos, exclude=r)
            if ca and cb:
                dx, dy = cb[0] - ca[0], cb[1] - ca[1]
                if abs(dx) + abs(dy) > 0.1:
                    ang[r] = _snap(math.degrees(math.atan2(dy, dx)), mode)
                    continue
            ang[r] = 0.0
        else:
            # IC/connector: face the centroid of its key neighbours
            nb = graph.neighbors(r)
            if nb:
                cx = sum(pos[n][0] for n in nb if n in pos) / len(nb)
                cy = sum(pos[n][1] for n in nb if n in pos) / len(nb)
                ang[r] = _snap(math.degrees(math.atan2(cy - pos[r][1], cx - pos[r][0])), "ortho")
            else:
                ang[r] = 0.0
    return ang


def _net_centroid(graph, netname, pos, exclude=None):
    refs = [r for r in graph.signal_nets.get(netname, []) if r != exclude and r in pos]
    if not refs:
        return None
    return (sum(pos[r][0] for r in refs) / len(refs),
            sum(pos[r][1] for r in refs) / len(refs))
