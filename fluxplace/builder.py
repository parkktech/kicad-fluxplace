"""Constructive route-as-you-place refinement — the 'human hands' pass.

An engineer doesn't scatter parts and hope: they drop the most important component
first, then work outward, imagining each trace before committing the next part,
nudging things so the routes they've already pictured stay clean. This module does
exactly that, mechanically:

  1. The quadratic solution (`prior`) is the engineer's mental map — where each part
     roughly belongs given everything it talks to.
  2. Parts commit ONE AT A TIME: hub first, then always the unplaced part most
     strongly connected to what's already down (criticality-weighted).
  3. Each part auditions candidate spots near its prior position. Every candidate is
     scored by actually estimating its traces on the routing grid (cheap L-route
     congestion probes — the eyeball check) plus a pull toward the mental map.
  4. The winner is committed, its body derates grid capacity, and its nets are
     REALLY routed (congestion-negotiated A*) to the placed set and reserved — so
     later parts physically cannot crowd out earlier committed traces without
     paying the congestion price.

The result is overlap-free by construction and comes with its own routing fabric.
Final acceptance still goes through route.score() — the independent gate.
"""
import math
from collections import defaultdict

from .graph import net_weight, kind_of, is_passive
from .placement import eff_size, _size
from .route import Grid, build_grid


def _overlaps_any(parts, angles, pad, committed, pos, r, x, y):
    w, h = eff_size(parts, r, angles.get(r, 0.0), pad)
    for s in committed:
        sw, sh = eff_size(parts, s, angles.get(s, 0.0), pad)
        if (abs(x - pos[s][0]) < (w + sw) / 2 and
                abs(y - pos[s][1]) < (h + sh) / 2):
            return True
    return False


def _candidates(parts, angles, pad, committed, pos, r, px, py, bounds, max_ring=10):
    """Spots to audition: the prior spot, then rings around it. Grows the search
    radius until at least one legal spot exists (guaranteed termination: the board
    region grows past all committed parts eventually)."""
    w, h = eff_size(parts, r, angles.get(r, 0.0), pad)
    x0, y0, x1, y1 = bounds

    def clamp(x, y):
        return (max(x0 + w / 2, min(x1 - w / 2, x)),
                max(y0 + h / 2, min(y1 - h / 2, y)))

    out = []
    ring = 0
    radii = [0.0, 2.0, 4.0, 7.0, 11.0, 16.0, 22.0, 30.0, 40.0, 55.0, 75.0, 100.0]
    while ring < len(radii) and (not out or ring <= max_ring):
        rad = radii[ring]
        pts = [(px, py)] if rad == 0 else [
            (px + rad * math.cos(2 * math.pi * k / 12),
             py + rad * math.sin(2 * math.pi * k / 12)) for k in range(12)]
        for x, y in pts:
            x, y = clamp(x, y)
            if not _overlaps_any(parts, angles, pad, committed, pos, r, x, y):
                out.append((x, y))
        if out and ring >= 2:
            break
        ring += 1
    if not out:
        # desperate: escape outward past everything (bounds grow implicitly)
        rad = radii[-1]
        while not out:
            rad *= 1.5
            for k in range(16):
                x = px + rad * math.cos(2 * math.pi * k / 16)
                y = py + rad * math.sin(2 * math.pi * k / 16)
                if not _overlaps_any(parts, angles, pad, committed, pos, r, x, y):
                    out.append((x, y))
    return out


def build(parts, graph, topo, prior, angles, pad=0.45, fixed=(), bounds=None,
          cell=2.0, layers=2, pitch=0.35, prior_pull=0.02, big_area=800.0):
    """Sequential route-aware construction. Returns (pos, grid, routed_paths).
    `prior` = quad positions (mental map); `fixed` refs commit first at prior spots."""
    angles = angles or {}
    refs = list(parts)
    if bounds is None:
        xs0 = [prior[r][0] - _size(parts, r, pad)[0] / 2 for r in refs]
        ys0 = [prior[r][1] - _size(parts, r, pad)[1] / 2 for r in refs]
        xs1 = [prior[r][0] + _size(parts, r, pad)[0] / 2 for r in refs]
        ys1 = [prior[r][1] + _size(parts, r, pad)[1] / 2 for r in refs]
        bounds = (min(xs0), min(ys0), max(xs1), max(ys1))

    grid = Grid(bounds[0] - 4, bounds[1] - 4, bounds[2] + 4, bounds[3] + 4,
                cell=cell, layers=layers, pitch=pitch)

    # per-part signal nets, heaviest first (these are the traces we imagine)
    pnets = defaultdict(list)
    for name, members in graph.signal_nets.items():
        wt = net_weight(name, len(members))
        for r in members:
            if r in parts:
                pnets[r].append((name, wt))
    for r in pnets:
        pnets[r].sort(key=lambda t: -t[1])

    # connection weight ref<->ref for the placement order
    wadj = defaultdict(lambda: defaultdict(float))
    for name, members in graph.signal_nets.items():
        m = [r for r in members if r in parts]
        wt = net_weight(name, len(m))
        for i in range(len(m)):
            for j in range(i + 1, len(m)):
                wadj[m[i]][m[j]] += wt
                wadj[m[j]][m[i]] += wt

    pos = {}
    committed = []
    routed = defaultdict(list)   # net -> [paths]
    net_placed = defaultdict(list)  # net -> [(ref, pinx, piny)] committed pins

    def commit(r, x, y):
        pos[r] = (x, y)
        committed.append(r)
        w, h = eff_size(parts, r, angles.get(r, 0.0), 0.0)
        grid.block_rect(x, y, w, h, 0.8 if parts[r].get("tht") else 0.4)
        for off in parts[r].get("pins", {}).values():
            c = grid.cell_of(x + off[0], y + off[1])
            if c in grid.block:
                grid.block[c] *= 0.35   # escape room at the pins
        # really route this part's nets to the nearest already-placed pin, and
        # reserve the capacity: the imagined trace becomes a fact on the grid
        for name, wt in pnets[r]:
            off = parts[r].get("pins", {}).get(name, (0.0, 0.0))
            mypin = (x + off[0], y + off[1])
            if net_placed[name]:
                tx, ty = min(((p[1], p[2]) for p in net_placed[name]),
                             key=lambda p: abs(p[0] - mypin[0]) + abs(p[1] - mypin[1]))
                path = grid.astar(grid.cell_of(*mypin), grid.cell_of(tx, ty))
                if path:
                    grid.reserve(path)
                    routed[name].append(path)
            net_placed[name].append((r, mypin[0], mypin[1]))

    def route_score(r, x, y):
        """The eyeball check: L-route congestion estimate for every net this part
        would have to close to the placed set, weighted by criticality."""
        s = 0.0
        for name, wt in pnets[r][:6]:
            if not net_placed[name]:
                continue
            off = parts[r].get("pins", {}).get(name, (0.0, 0.0))
            mypin = (x + off[0], y + off[1])
            best = min(net_placed[name],
                       key=lambda p: abs(p[1] - mypin[0]) + abs(p[2] - mypin[1]))
            s += wt * grid.l_estimate(grid.cell_of(*mypin), grid.cell_of(best[1], best[2]))
        return s

    # ---- order: fixed walls, hub, then strongest-connection-first ----------
    for r in fixed:
        if r in parts and r not in pos:
            commit(r, prior[r][0], prior[r][1])

    todo = [r for r in refs if r not in pos]
    hub = topo.hub if topo.hub in parts else None
    if hub and hub in todo:
        todo.remove(hub)
        commit(hub, prior[hub][0], prior[hub][1])

    conn_to_placed = defaultdict(float)
    for r in todo:
        for s in pos:
            conn_to_placed[r] += wadj[r].get(s, 0.0)

    remaining = set(todo)
    while remaining:
        # the part a human would reach for next: most strongly tied to what's down
        r = max(remaining,
                key=lambda q: (conn_to_placed[q],
                               sum(wadj[q].values()),
                               _size(parts, q, 0.0)[0] * _size(parts, q, 0.0)[1],
                               q))
        remaining.discard(r)
        px, py = prior[r]
        best, bestc = None, 1e18
        for x, y in _candidates(parts, angles, pad, committed, pos, r, px, py, bounds):
            c = route_score(r, x, y) + prior_pull * ((x - px) ** 2 + (y - py) ** 2)
            if c < bestc:
                best, bestc = (x, y), c
        commit(r, best[0], best[1])
        for q in remaining:
            if r in wadj[q]:
                conn_to_placed[q] += wadj[q][r]

    return pos, grid, routed
