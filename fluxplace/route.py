"""Coarse global router — the routability gate and the builder's live trace fabric.

A placement that cannot route is a failure no matter how dense: this module makes
routability a first-class, *measured* property instead of a hope. It models the
board as a grid of cells (default 2 mm); each cell EDGE has a capacity = how many
traces can cross it (track+clearance pitch x signal layers, derated where a
footprint body sits). Nets are decomposed to 2-pin segments (Prim MST over real
pad positions) and routed with congestion-negotiated A* (PathFinder-lite):
overflowed edges get progressively expensive, offenders are ripped up and rerouted.

Two ways to use it:
  * score(...)     — route everything, return overflow/wirelength/hotspots. The
                     fitness gate: overflow == 0 means "globally routable".
  * Grid as a live fabric — the constructive builder places one part at a time,
    routes its nets to the already-placed set, and RESERVES the capacity, exactly
    like an engineer imagining the traces while dropping each part.

Pure Python, no pcbnew. Costs are heuristic-coarse on purpose: the goal is a
truthful congestion signal, not detailed geometry.
"""
import heapq
import math
from collections import defaultdict

from .graph import net_weight


class Grid:
    """Routing grid. Edges are (cell, neighbour-cell) pairs with usage/capacity.
    Cells are (ix, iy) tuples over [x0..x1] x [y0..y1]."""

    def __init__(self, x0, y0, x1, y1, cell=2.0, layers=2, pitch=0.35, util=0.8):
        self.cell = cell
        self.x0, self.y0 = x0, y0
        self.nx = max(2, int(math.ceil((x1 - x0) / cell)))
        self.ny = max(2, int(math.ceil((y1 - y0) / cell)))
        # tracks that fit through one cell face, per layer, derated by util
        base = max(1, int(cell / pitch * util))
        self.cap_base = base * layers
        self.block = defaultdict(float)   # cell -> blocked fraction [0..1]
        self.usage = defaultdict(int)     # edge -> committed traces
        self.history = defaultdict(float)  # edge -> PathFinder history cost

    # -- geometry ---------------------------------------------------------
    def cell_of(self, x, y):
        ix = min(self.nx - 1, max(0, int((x - self.x0) / self.cell)))
        iy = min(self.ny - 1, max(0, int((y - self.y0) / self.cell)))
        return (ix, iy)

    def block_rect(self, x, y, w, h, frac):
        """Derate capacity where a footprint body sits (SMD blocks its own layer,
        THT/connectors block more). Accumulates; clamped at 0.9 so a route is never
        impossible through a blocked region, just very expensive."""
        c0 = self.cell_of(x - w / 2, y - h / 2)
        c1 = self.cell_of(x + w / 2, y + h / 2)
        for ix in range(c0[0], c1[0] + 1):
            for iy in range(c0[1], c1[1] + 1):
                k = (ix, iy)
                self.block[k] = min(0.9, self.block[k] + frac)

    def cap(self, a, b):
        """Capacity of edge a-b: base derated by the blockage of BOTH endpoint cells."""
        blk = max(self.block.get(a, 0.0), self.block.get(b, 0.0))
        return max(1, int(self.cap_base * (1.0 - blk)))

    @staticmethod
    def _edge(a, b):
        return (a, b) if a <= b else (b, a)

    # -- costs ------------------------------------------------------------
    def edge_cost(self, a, b, extra=0):
        """Congestion-negotiated cost: cheap while under capacity, super-linear over.
        `extra` = trial traces this query would add (for what-if scoring)."""
        e = self._edge(a, b)
        cap = self.cap(a, b)
        use = self.usage[e] + extra
        over = use + 1 - cap
        cost = 1.0 + self.history[e]
        if over > 0:
            cost += 9.0 * (over ** 2)   # an overfull edge must lose to any sane detour
        elif use > 0.7 * cap:
            cost += 0.5 * (use / cap)
        return cost

    # -- routing ----------------------------------------------------------
    def astar(self, src, dst, extra=0):
        """Congestion-aware A* from cell src to cell dst. Returns list of cells."""
        if src == dst:
            return [src]
        openq = [(0.0, src)]
        g = {src: 0.0}
        came = {}
        while openq:
            f, c = heapq.heappop(openq)
            if c == dst:
                path = [c]
                while c in came:
                    c = came[c]
                    path.append(c)
                return path[::-1]
            if f - abs(c[0] - dst[0]) - abs(c[1] - dst[1]) > g.get(c, 1e18) + 1e-9:
                continue
            for nb in ((c[0] + 1, c[1]), (c[0] - 1, c[1]),
                       (c[0], c[1] + 1), (c[0], c[1] - 1)):
                if not (0 <= nb[0] < self.nx and 0 <= nb[1] < self.ny):
                    continue
                ng = g[c] + self.edge_cost(c, nb, extra)
                if ng < g.get(nb, 1e18):
                    g[nb] = ng
                    came[nb] = c
                    heapq.heappush(openq, (ng + abs(nb[0] - dst[0]) + abs(nb[1] - dst[1]), nb))
        return None  # unreachable (shouldn't happen on a connected grid)

    def l_estimate(self, src, dst):
        """Cheap what-if: cost of the better of the two L-shaped routes. This is the
        'engineer eyeballing the trace' — used to score candidate part positions
        without running a full A* per candidate."""
        def leg(a, b, horiz_first):
            cost = 0.0
            cur = a
            steps = []
            if horiz_first:
                stepx = 1 if b[0] > a[0] else -1
                for x in range(a[0], b[0], stepx):
                    steps.append(((x, a[1]), (x + stepx, a[1])))
                stepy = 1 if b[1] > a[1] else -1
                for y in range(a[1], b[1], stepy):
                    steps.append(((b[0], y), (b[0], y + stepy)))
            else:
                stepy = 1 if b[1] > a[1] else -1
                for y in range(a[1], b[1], stepy):
                    steps.append(((a[0], y), (a[0], y + stepy)))
                stepx = 1 if b[0] > a[0] else -1
                for x in range(a[0], b[0], stepx):
                    steps.append(((x, b[1]), (x + stepx, b[1])))
            for u, v in steps:
                cost += self.edge_cost(u, v)
            return cost
        if src == dst:
            return 0.0
        return min(leg(src, dst, True), leg(src, dst, False))

    def reserve(self, path, n=1):
        for i in range(len(path) - 1):
            self.usage[self._edge(path[i], path[i + 1])] += n

    def release(self, path, n=1):
        for i in range(len(path) - 1):
            e = self._edge(path[i], path[i + 1])
            self.usage[e] = max(0, self.usage[e] - n)

    def overflow(self):
        """Total and worst per-edge overflow across the grid."""
        tot = worst = 0
        for e, u in self.usage.items():
            over = u - self.cap(*e)
            if over > 0:
                tot += over
                worst = max(worst, over)
        return tot, worst

    def hot_cells(self):
        """Cells adjacent to overfull edges, with their overflow sum (feedback target)."""
        hot = defaultdict(int)
        for e, u in self.usage.items():
            over = u - self.cap(*e)
            if over > 0:
                hot[e[0]] += over
                hot[e[1]] += over
        return dict(hot)

    def cell_center(self, c):
        return (self.x0 + (c[0] + 0.5) * self.cell,
                self.y0 + (c[1] + 0.5) * self.cell)


# -------------------------------------------------------------------- model build
def build_grid(parts, pos, angles=None, margin=4.0, cell=2.0, layers=2, pitch=0.35):
    """Grid over the placement extent. Footprint bodies derate capacity: connectors
    and other through-hole-ish parts block hard (both layers), SMD bodies block the
    top layer share. (Approximation: kind J + mounting = THT-ish.)"""
    from .placement import eff_size
    from .graph import kind_of
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
        # drilled parts wall off every layer; SMD bodies only crowd their own side
        # (routing under an M.2 card or a stood-off CPU module is normal practice)
        frac = 0.8 if parts[r].get("tht") else 0.4
        g.block_rect(x, y, w, h, frac)
    for r, (x, y) in pos.items():
        # pin cells are where a part's traces legitimately emerge — escape capacity
        # must survive there, or every high-pin connector drowns in false congestion
        for off in parts[r].get("pins", {}).values():
            c = g.cell_of(x + off[0], y + off[1])
            if c in g.block:
                g.block[c] *= 0.35
    return g


def net_pins(parts, pos, graph):
    """{net: [(ref, x, y)]} — real pad anchor positions per signal net."""
    out = {}
    for name, members in graph.signal_nets.items():
        pts = []
        for r in members:
            if r not in pos:
                continue
            off = parts[r].get("pins", {}).get(name, (0.0, 0.0))
            pts.append((r, pos[r][0] + off[0], pos[r][1] + off[1]))
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


# -------------------------------------------------------------------- full score
def score(parts, pos, graph, angles=None, cell=2.0, layers=2, pitch=0.35, rounds=6):
    """Route the whole placement, critical nets first, with PathFinder rip-up rounds.
    Returns a report dict; report['overflow'] == 0 means globally routable."""
    g = build_grid(parts, pos, angles, cell=cell, layers=layers, pitch=pitch)
    pins = net_pins(parts, pos, graph)
    order = sorted(pins, key=lambda n: -net_weight(n, len(pins[n])))

    routed = {}   # net -> list of paths
    for name in order:
        pts = pins[name]
        paths = []
        for i, j in _mst_segments(pts):
            p = g.astar(g.cell_of(pts[i][1], pts[i][2]), g.cell_of(pts[j][1], pts[j][2]))
            if p:
                g.reserve(p)
                paths.append(p)
        routed[name] = paths

    for rnd in range(rounds):
        tot, _ = g.overflow()
        if tot == 0:
            break
        # PathFinder: bump history on overfull edges, rip up + reroute nets using them
        bad_edges = {e for e, u in g.usage.items() if u > g.cap(*e)}
        for e in bad_edges:
            g.history[e] += 1.0
        for name in order:
            uses_bad = any(
                Grid._edge(p[i], p[i + 1]) in bad_edges
                for p in routed[name] for i in range(len(p) - 1))
            if not uses_bad:
                continue
            for p in routed[name]:
                g.release(p)
            pts = pins[name]
            paths = []
            for i, j in _mst_segments(pts):
                p = g.astar(g.cell_of(pts[i][1], pts[i][2]), g.cell_of(pts[j][1], pts[j][2]))
                if p:
                    g.reserve(p)
                    paths.append(p)
            routed[name] = paths

    tot, worst = g.overflow()
    wl = sum((len(p) - 1) * g.cell for paths in routed.values() for p in paths)
    detour = {}
    for name, paths in routed.items():
        pts = pins[name]
        ideal = sum(abs(pts[i][1] - pts[j][1]) + abs(pts[i][2] - pts[j][2])
                    for i, j in _mst_segments(pts)) or 1.0
        got = sum((len(p) - 1) * g.cell for p in paths)
        detour[name] = got / ideal
    return dict(grid=g, routed=routed, overflow=tot, worst=worst, wirelength=wl,
                hot=g.hot_cells(), detour=detour,
                nets=len(routed), segments=sum(len(p) for p in routed.values()))


def summary(rep):
    hot = sorted(rep["hot"].items(), key=lambda kv: -kv[1])[:6]
    g = rep["grid"]
    lines = [
        f"nets routed: {rep['nets']}  segments: {rep['segments']}  "
        f"wirelength: {rep['wirelength']:.0f} mm",
        f"OVERFLOW: {rep['overflow']}  (worst edge {rep['worst']})  ->  "
        + ("ROUTABLE (global)" if rep["overflow"] == 0 else "CONGESTED — placement must change"),
    ]
    if hot:
        spots = ", ".join(f"({g.cell_center(c)[0]:.0f},{g.cell_center(c)[1]:.0f})x{v}"
                          for c, v in hot)
        lines.append(f"hotspots (mm): {spots}")
    worst_det = sorted(rep["detour"].items(), key=lambda kv: -kv[1])[:5]
    lines.append("worst detours: " + ", ".join(f"{n} x{d:.1f}" for n, d in worst_det))
    return "\n".join(lines)
