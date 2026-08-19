"""Coarse global router — the routability gate and the builder's live trace fabric.

A placement that cannot route is a failure no matter how dense: this module makes
routability a first-class, *measured* property instead of a hope.

Model (v2 — layer-aware, power-aware, pair-aware):
  * Grid of cells (default 2 mm). Capacity is PER DIRECTION: on a 2-signal-layer
    board, horizontal runs live on one layer and vertical runs on the other (the
    classic H/V discipline), with a small leak factor for short wrong-way jogs.
    Turning costs a via.
  * Footprint bodies derate capacity (drilled pin fields wall off both layers, SMD
    bodies crowd their own side); pin cells keep escape capacity; fine-pitch parts
    project an extra ESCAPE ring (their fanout needs room the pads don't show).
  * High-current power nets that are real traces (28 V in, 12 V head feed, buck
    outputs — see graph.classify) are routed FIRST and eat `power_width()` track
    slots per edge. Pretending power is free is how boards become unroutable.
  * Differential pairs (graph.diff_pairs) route as master + hugged slave: the slave
    gets a discount on the master's edges, and the report scores how well each pair
    stayed together (pair_sep 1.0 = perfectly hugged).
  * Congestion negotiation is PathFinder-lite: overflowed edges get history cost,
    offenders are ripped up and rerouted.

score(...)['overflow'] == 0 means globally routable — the gate.
Pure Python, no pcbnew.
"""
import heapq
import math
from collections import defaultdict

from .graph import net_weight, power_width, diff_pairs

H, V = 0, 1


class Grid:
    """Routing grid. Edges are (cell, neighbour-cell) pairs with usage/capacity.
    Cells are (ix, iy) tuples. Horizontal and vertical edges draw on different
    layers' capacity; a turn implies a via."""

    def __init__(self, x0, y0, x1, y1, cell=2.0, layers=2, pitch=0.35, util=0.8,
                 leak=0.25, via_cost=1.6):
        self.cell = cell
        self.x0, self.y0 = x0, y0
        self.nx = max(2, int(math.ceil((x1 - x0) / cell)))
        self.ny = max(2, int(math.ceil((y1 - y0) / cell)))
        per_layer = max(1, int(cell / pitch * util))
        share = max(1, layers // 2)          # layers dedicated to each direction
        self.cap_dir = per_layer * share * (1.0 + leak)
        self.pitch = pitch
        self.via_cost = via_cost
        self.block = defaultdict(float)    # cell -> blocked fraction [0..1]
        self.usage = defaultdict(float)    # edge -> committed track slots
        self.history = defaultdict(float)  # edge -> PathFinder history cost

    # -- geometry ---------------------------------------------------------
    def cell_of(self, x, y):
        ix = min(self.nx - 1, max(0, int((x - self.x0) / self.cell)))
        iy = min(self.ny - 1, max(0, int((y - self.y0) / self.cell)))
        return (ix, iy)

    def cell_center(self, c):
        return (self.x0 + (c[0] + 0.5) * self.cell,
                self.y0 + (c[1] + 0.5) * self.cell)

    def block_rect(self, x, y, w, h, frac):
        # stacking clamp: two bodies sharing a cell is the worst occupant plus a
        # little, never additive — the same copper can't be blocked twice
        c0 = self.cell_of(x - w / 2, y - h / 2)
        c1 = self.cell_of(x + w / 2, y + h / 2)
        for ix in range(c0[0], c1[0] + 1):
            for iy in range(c0[1], c1[1] + 1):
                k = (ix, iy)
                old = self.block[k]
                self.block[k] = min(0.85, max(old, frac) + (0.15 if old > 0.05 else 0.0))

    @staticmethod
    def _edge(a, b):
        return (a, b) if a <= b else (b, a)

    def cap(self, a, b):
        # average of the endpoint blockages: an edge spans two half-cells, and the
        # last hop INTO a relieved pin cell must stay landable even for wide power
        # (arrival at a fat THT pad is physically guaranteed; max() denied it)
        blk = (self.block.get(a, 0.0) + self.block.get(b, 0.0)) / 2
        return max(1.0, self.cap_dir * (1.0 - blk))

    # -- costs ------------------------------------------------------------
    def edge_cost(self, a, b, width=1, discount=None):
        """Congestion-negotiated cost of adding `width` track slots to edge a-b.
        `discount` = edges that are cheap for this query (diff-pair hugging)."""
        e = self._edge(a, b)
        cap = self.cap(a, b)
        use = self.usage[e] + width
        over = use - cap
        cost = float(width) + self.history[e]
        if over > 0:
            cost += 9.0 * over * over    # an overfull edge must lose to any sane detour
        elif use > 0.7 * cap:
            cost += 0.5 * (use / cap)
        if discount and e in discount:
            cost *= 0.35
        return cost

    # -- routing ----------------------------------------------------------
    def _w_edge(self, a, b, src, dst, width):
        """Tapered width: a wide trace necks down over its last ~4 mm to land on its
        pads — edges within 2 cells of a terminal charge width 1, pass-through full."""
        if width <= 1:
            return width
        for t in (src, dst):
            if (max(abs(a[0] - t[0]), abs(a[1] - t[1])) <= 2 or
                    max(abs(b[0] - t[0]), abs(b[1] - t[1])) <= 2):
                return 1
        return width

    def astar(self, src, dst, width=1, discount=None):
        """Layer-aware A*: state = (cell, direction); changing direction costs a via.
        Returns the cell path or None."""
        if src == dst:
            return [src]
        h0 = abs(src[0] - dst[0]) + abs(src[1] - dst[1])
        openq = [(h0, src, -1)]            # (f, cell, dir); -1 = no direction yet
        g = {(src, -1): 0.0}
        came = {}
        best_end = None
        while openq:
            f, c, d = heapq.heappop(openq)
            if c == dst:
                best_end = (c, d)
                break
            if f - abs(c[0] - dst[0]) - abs(c[1] - dst[1]) > g.get((c, d), 1e18) + 1e-9:
                continue
            for nb, nd in (((c[0] + 1, c[1]), H), ((c[0] - 1, c[1]), H),
                           ((c[0], c[1] + 1), V), ((c[0], c[1] - 1), V)):
                if not (0 <= nb[0] < self.nx and 0 <= nb[1] < self.ny):
                    continue
                step = self.edge_cost(c, nb, self._w_edge(c, nb, src, dst, width), discount)
                if d != -1 and nd != d:
                    step += self.via_cost
                ng = g[(c, d)] + step
                if ng < g.get((nb, nd), 1e18):
                    g[(nb, nd)] = ng
                    came[(nb, nd)] = (c, d)
                    heapq.heappush(openq, (ng + abs(nb[0] - dst[0]) + abs(nb[1] - dst[1]),
                                           nb, nd))
        if best_end is None:
            return None
        path = [best_end[0]]
        cur = best_end
        while cur in came:
            cur = came[cur]
            path.append(cur[0])
        return path[::-1]

    def l_estimate(self, src, dst, width=1):
        """Cheap what-if: cost of the better of the two L-routes (one via each) —
        the 'engineer eyeballing the trace' check used to score candidate spots."""
        if src == dst:
            return 0.0

        def leg(a, b, horiz_first):
            cost = 0.0
            if horiz_first:
                sx = 1 if b[0] > a[0] else -1
                for x in range(a[0], b[0], sx):
                    cost += self.edge_cost((x, a[1]), (x + sx, a[1]), width)
                sy = 1 if b[1] > a[1] else -1
                for y in range(a[1], b[1], sy):
                    cost += self.edge_cost((b[0], y), (b[0], y + sy), width)
            else:
                sy = 1 if b[1] > a[1] else -1
                for y in range(a[1], b[1], sy):
                    cost += self.edge_cost((a[0], y), (a[0], y + sy), width)
                sx = 1 if b[0] > a[0] else -1
                for x in range(a[0], b[0], sx):
                    cost += self.edge_cost((x, b[1]), (x + sx, b[1]), width)
            return cost + (self.via_cost if (a[0] != b[0] and a[1] != b[1]) else 0.0)

        return min(leg(src, dst, True), leg(src, dst, False))

    def snap_terminal(self, c, max_r=3):
        """Route terminals land at the nearest reasonably-open cell: arrival WITHIN a
        part's own body region is the pad itself, not a global-routing problem —
        what's contested is the corridor outside. Deterministic spiral search."""
        if self.block.get(c, 0.0) <= 0.5:
            return c
        for rad in range(1, max_r + 1):
            best = None
            for dx in range(-rad, rad + 1):
                for dy in range(-rad, rad + 1):
                    if max(abs(dx), abs(dy)) != rad:
                        continue
                    nb = (c[0] + dx, c[1] + dy)
                    if not (0 <= nb[0] < self.nx and 0 <= nb[1] < self.ny):
                        continue
                    b = self.block.get(nb, 0.0)
                    if b <= 0.5 and (best is None or (b, nb) < best):
                        best = (b, nb)
            if best:
                return best[1]
        return c

    def reserve(self, path, n=1):
        src, dst = path[0], path[-1]
        for i in range(len(path) - 1):
            w = self._w_edge(path[i], path[i + 1], src, dst, n)
            self.usage[self._edge(path[i], path[i + 1])] += w

    def release(self, path, n=1):
        src, dst = path[0], path[-1]
        for i in range(len(path) - 1):
            w = self._w_edge(path[i], path[i + 1], src, dst, n)
            e = self._edge(path[i], path[i + 1])
            self.usage[e] = max(0.0, self.usage[e] - w)

    def overflow(self):
        tot = worst = 0.0
        for e, u in self.usage.items():
            over = u - self.cap(*e)
            if over > 0:
                tot += over
                worst = max(worst, over)
        return tot, worst

    def hot_cells(self):
        hot = defaultdict(float)
        for e, u in self.usage.items():
            over = u - self.cap(*e)
            if over > 0:
                hot[e[0]] += over
                hot[e[1]] += over
        return dict(hot)


def cut_overflow(g):
    """Aggregate overflow crossing each straight full-board cut. A vertical cut sits
    between grid column ix and ix+1 and is crossed by horizontal edges; a horizontal
    cut between row iy and iy+1 by vertical edges. Returns cuts sorted worst-first as
    (axis, index, total_over, need_tracks) where need_tracks is the worst single-edge
    deficit on that cut — the lane width (in track slots) the router is short there.
    A dominant cut = a congestion WALL: the placement needs a continuous lane, which
    per-part inflation (hot-cell bloat) cannot produce."""
    tot = defaultdict(float)
    need = defaultdict(float)
    for e, u in g.usage.items():
        a, b = e
        over = u - g.cap(a, b)
        if over <= 0:
            continue
        if a[1] == b[1]:                     # horizontal edge -> crosses a vertical cut
            k = ("v", min(a[0], b[0]))
        else:                                # vertical edge -> crosses a horizontal cut
            k = ("h", min(a[1], b[1]))
        tot[k] += over
        need[k] = max(need[k], over)
    cuts = [(ax, ix, t, need[(ax, ix)]) for (ax, ix), t in tot.items()]
    cuts.sort(key=lambda c: (-c[2], c[0], c[1]))
    return cuts


# -------------------------------------------------------------------- model build
def part_block_frac(part):
    """How hard a footprint body derates routing capacity where it sits."""
    return 0.8 if part.get("tht") else 0.4


def apply_part_blockage(g, parts, r, x, y, w, h):
    """Body blockage + fine-pitch escape ring + pin-cell escape relief, in that
    order — shared by the batch grid build and the builder's incremental commits."""
    # tiny series/shunt passives (0402/0603 R/C) sit ON the traces they serve —
    # their body doesn't consume a 2 mm routing cell, and charging it double-counts
    # against the very nets that terminate in their pads (PCIe AC caps!)
    npads = parts[r].get("npads", 0)
    if not (npads and npads <= 3 and w * h < 12.0):
        g.block_rect(x, y, w, h, part_block_frac(parts[r]))
    if npads and w * h > 0 and npads / (w * h) >= 0.8:
        # fine-pitch part: its fanout needs a ring of room the pads don't show
        g.block_rect(x, y, w + 2 * g.cell, h + 2 * g.cell, 0.2)
    # landing corridors: traces legitimately emerge at pin cells AND neck down
    # through the neighbouring body cells (a wide 28V trace must be able to LAND
    # on a THT pin without pretending to cross the whole connector body).
    # Relief applies once per cell, never stacked per pin.
    pin_cells, nbrs = set(), set()
    for off in parts[r].get("pins", {}).values():
        c = g.cell_of(x + off[0], y + off[1])
        pin_cells.add(c)
        nbrs.update(((c[0] + 1, c[1]), (c[0] - 1, c[1]),
                     (c[0], c[1] + 1), (c[0], c[1] - 1)))
    for c in pin_cells:
        if c in g.block:
            g.block[c] *= 0.3
    for c in nbrs - pin_cells:
        if c in g.block:
            g.block[c] *= 0.6


def build_grid(parts, pos, angles=None, margin=4.0, cell=2.0, layers=2, pitch=0.35):
    from .placement import eff_size
    angles = angles or {}
    xs0, ys0, xs1, ys1 = [], [], [], []
    for r, (x, y) in pos.items():
        w, h = eff_size(parts, r, angles.get(r, 0.0), 0.0)
        xs0.append(x - w / 2); ys0.append(y - h / 2)
        xs1.append(x + w / 2); ys1.append(y + h / 2)
    g = Grid(min(xs0) - margin, min(ys0) - margin,
             max(xs1) + margin, max(ys1) + margin, cell=cell, layers=layers, pitch=pitch)
    for r, (x, y) in pos.items():
        w, h = eff_size(parts, r, angles.get(r, 0.0), 0.0)
        apply_part_blockage(g, parts, r, x, y, w, h)
    return g


def net_pins(parts, pos, netdict, angles=None):
    """{net: [(ref, x, y)]} — rotation-correct pad anchor positions per net."""
    from .placement import pin_at
    angles = angles or {}
    out = {}
    for name, members in netdict.items():
        pts = []
        for r in members:
            if r not in pos:
                continue
            ox, oy = pin_at(parts, r, name, angles.get(r))
            pts.append((r, pos[r][0] + ox, pos[r][1] + oy))
        if len(pts) >= 2:
            out[name] = pts
    return out


def _mst_segments(pts):
    """Prim MST over pin points -> list of (i, j) index pairs (2-pin segments)."""
    n = len(pts)
    if n == 2:
        return [(0, 1)]
    in_tree = {0}
    best = {}
    for i in range(1, n):
        best[i] = (abs(pts[0][1] - pts[i][1]) + abs(pts[0][2] - pts[i][2]), 0)
    segs = []
    while len(in_tree) < n:
        i = min(best, key=lambda k: best[k][0])
        d, j = best.pop(i)
        segs.append((j, i))
        in_tree.add(i)
        for k in list(best):
            nd = abs(pts[i][1] - pts[k][1]) + abs(pts[i][2] - pts[k][2])
            if nd < best[k][0]:
                best[k] = (nd, i)
    return segs


def _route_net(g, pts, width=1, discount=None):
    paths = []
    for i, j in _mst_segments(pts):
        p = g.astar(g.snap_terminal(g.cell_of(pts[i][1], pts[i][2])),
                    g.snap_terminal(g.cell_of(pts[j][1], pts[j][2])), width, discount)
        if p:
            g.reserve(p, width)
            paths.append(p)
    return paths


def _path_edges(paths):
    out = set()
    for p in paths:
        for i in range(len(p) - 1):
            out.add(Grid._edge(p[i], p[i + 1]))
    return out


# -------------------------------------------------------------------- full score
def score(parts, pos, graph, angles=None, cell=2.0, layers=2, pitch=0.35, rounds=6):
    """Route the whole placement — wide power first, then critical signals with
    hugged diff pairs — with PathFinder rip-up rounds. report['overflow'] == 0
    means globally routable."""
    g = build_grid(parts, pos, angles, cell=cell, layers=layers, pitch=pitch)

    spins = net_pins(parts, pos, graph.signal_nets, angles)
    ppins = net_pins(parts, pos, getattr(graph, "power_traces", {}), angles)
    pairs = {s: m for s, m in diff_pairs(spins).items() if m in spins}
    widths = {n: power_width(n) for n in ppins}
    widths.update({n: 1 for n in spins})

    # order: power (widest first) -> masters + unpaired by weight, each slave
    # right after its master (so the hug discount sees the master's fresh edges)
    porder = sorted(ppins, key=lambda n: -widths[n])
    sorder = sorted((n for n in spins if n not in pairs),
                    key=lambda n: -net_weight(n, len(spins[n])))
    order = list(porder)
    for n in sorder:
        order.append(n)
        for s, m in pairs.items():
            if m == n:
                order.append(s)

    allpins = dict(ppins)
    allpins.update(spins)
    routed = {}
    for name in order:
        disc = _path_edges(routed.get(pairs.get(name), [])) if name in pairs else None
        routed[name] = _route_net(g, allpins[name], widths[name], disc)

    for rnd in range(rounds):
        tot, _ = g.overflow()
        if tot == 0:
            break
        bad = {e for e, u in g.usage.items() if u > g.cap(*e)}
        for e in bad:
            g.history[e] += 1.0
        for name in order:
            if not any(Grid._edge(p[i], p[i + 1]) in bad
                       for p in routed[name] for i in range(len(p) - 1)):
                continue
            for p in routed[name]:
                g.release(p, widths[name])
            disc = _path_edges(routed.get(pairs.get(name), [])) if name in pairs else None
            routed[name] = _route_net(g, allpins[name], widths[name], disc)

    tot, worst = g.overflow()
    wl = sum((len(p) - 1) * g.cell for paths in routed.values() for p in paths)
    detour = {}
    for name, paths in routed.items():
        pts = allpins[name]
        ideal = sum(abs(pts[i][1] - pts[j][1]) + abs(pts[i][2] - pts[j][2])
                    for i, j in _mst_segments(pts)) or 1.0
        got = sum((len(p) - 1) * g.cell for p in paths)
        detour[name] = got / ideal

    # pair hug quality: fraction of slave edges within one cell of the master path
    pair_scores = []
    for s, m in pairs.items():
        mcells = {c for e in _path_edges(routed.get(m, [])) for c in e}
        se = _path_edges(routed.get(s, []))
        if not se or not mcells:
            continue
        near = sum(1 for e in se
                   if any(abs(e[0][0] - c[0]) + abs(e[0][1] - c[1]) <= 1 for c in mcells))
        pair_scores.append(near / len(se))
    pair_sep = round(sum(pair_scores) / len(pair_scores), 2) if pair_scores else None

    return dict(grid=g, routed=routed, overflow=round(tot, 1), worst=round(worst, 1),
                wirelength=wl, hot=g.hot_cells(), detour=detour, pair_sep=pair_sep,
                power_nets=len(ppins), pairs=len(pair_scores),
                nets=len(routed), segments=sum(len(p) for p in routed.values()))


def summary(rep):
    hot = sorted(rep["hot"].items(), key=lambda kv: -kv[1])[:6]
    g = rep["grid"]
    lines = [
        f"nets routed: {rep['nets']} ({rep['power_nets']} power traces, "
        f"{rep['pairs']} diff pairs)  segments: {rep['segments']}  "
        f"wirelength: {rep['wirelength']:.0f} mm",
        f"OVERFLOW: {rep['overflow']}  (worst edge {rep['worst']})  ->  "
        + ("ROUTABLE (global)" if rep["overflow"] == 0 else "CONGESTED — placement must change"),
    ]
    if rep["pair_sep"] is not None:
        lines.append(f"diff-pair hug: {rep['pair_sep']:.2f} (1.0 = slave rides its master)")
    if hot:
        spots = ", ".join(f"({g.cell_center(c)[0]:.0f},{g.cell_center(c)[1]:.0f})x{v:.0f}"
                          for c, v in hot)
        lines.append(f"hotspots (mm): {spots}")
    worst_det = sorted(rep["detour"].items(), key=lambda kv: -kv[1])[:5]
    lines.append("worst detours: " + ", ".join(f"{n} x{d:.1f}" for n, d in worst_det))
    return "\n".join(lines)
