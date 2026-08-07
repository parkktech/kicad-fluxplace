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


def place(parts, graph, topo, strategy="flux", rotate="ortho", center=None, pad=0.45,
          iters=260, compact_gaps=False, big_area=800.0):
    """One-call placement: run a strategy, orient parts, and legalize with rotation
    accounted for. Returns (positions {ref:(x,y)}, angles {ref:deg}). Every entry point
    (CLI, GUI plugin) goes through this so nothing forgets the final legalize."""
    frozen = set()
    if strategy == "radial":
        pos = radial(parts, topo, center=center, pad=pad)
    elif strategy == "flux":
        pos = flux(parts, graph, topo, iters=iters, center=center, pad=pad)
    elif strategy == "quad":
        from .quadratic import quad
        pos = quad(parts, graph, topo, center=center, pad=pad, big_area=big_area)
        # anchors (big modules) already sit where the math wants them — the final
        # settle must not shove them around for the convenience of a decap
        frozen = {r for r in pos
                  if _size(parts, r, 0.0)[0] * _size(parts, r, 0.0)[1] > big_area}
    else:
        pos = pack(parts, graph, topo, center=center, pad=pad)
    angles = orient(parts, graph, pos, mode=rotate)
    p = {r: list(v) for r, v in pos.items()}

    # locked parts are hard anchors: force them back to their real coords/orientation
    # and freeze them, so the settle legalizes movable parts AROUND them instead of
    # placing everything relative to where the strategy wished the anchors were.
    locked = {r for r in parts if parts[r].get("locked")}
    for r in locked:
        if r in p:
            p[r] = [parts[r]["x"], parts[r]["y"]]
            angles[r] = parts[r].get("angle0", angles.get(r, 0.0))
    frozen |= locked

    # for quad: hold the strategy's compact extent through the post-orient settle, so
    # rotation-induced fixups resolve inward instead of ballooning the board
    bounds = None
    if strategy == "quad":
        xs0 = [pos[r][0] - eff_size(parts, r, 0.0, pad)[0] / 2 for r in pos]
        ys0 = [pos[r][1] - eff_size(parts, r, 0.0, pad)[1] / 2 for r in pos]
        xs1 = [pos[r][0] + eff_size(parts, r, 0.0, pad)[0] / 2 for r in pos]
        ys1 = [pos[r][1] + eff_size(parts, r, 0.0, pad)[1] / 2 for r in pos]
        bounds = (min(xs0) - 1.0, min(ys0) - 1.0, max(xs1) + 1.0, max(ys1) + 1.0)

    def settle():
        for _ in range(6):
            legalize(parts, p, pad, iters=500, angles=angles, frozen=frozen, bounds=bounds)
            if count_overlaps(parts, p, 0.0, angles) == 0:
                return
            _shove_remaining(parts, p, angles, pad, frozen=frozen)
        # last resort: anchors and bounds are advisory, zero overlap is not
        if count_overlaps(parts, p, 0.0, angles):
            for _ in range(6):
                legalize(parts, p, pad, iters=500, angles=angles)
                if count_overlaps(parts, p, 0.0, angles) == 0:
                    return
                _shove_remaining(parts, p, angles, pad)

    settle()
    if compact_gaps:
        # close whitespace while keeping connected parts together (anchors frozen)
        compact(parts, p, angles, pad, graph=graph)
        settle()
    return {r: (x, y) for r, (x, y) in p.items()}, angles


def place_routed(parts, graph, topo, center=None, pad=0.45, big_area=800.0,
                 fill=0.65, aspect=1.35, rounds=9, feedback=6, seeds=1,
                 shrink=True, decaps=True):
    """The full route-aware pipeline — placement that is not allowed to be unroutable:

      quad (mental map) -> constructive builder (route-as-you-place) -> global-router
      gate -> congestion feedback -> shrink-to-smallest-routable -> decap adjacency.

    `seeds` > 1 runs perturbed attempts and keeps the best routable one.
    Returns (positions, angles, route_report). report['overflow'] == 0 means the
    coarse global router closed every net within capacity — routable."""
    from .quadratic import quad
    from . import route as R

    prior = quad(parts, graph, topo, center=center, pad=pad, big_area=big_area,
                 fill=fill, aspect=aspect, rounds=rounds)

    locked = {r for r in parts if parts[r].get("locked")}
    best = None
    for s in range(max(1, seeds)):
        pr = prior if s == 0 else _jitter(prior, s, locked)
        p, angles, rep, frozen = _pipeline_once(parts, graph, topo, pr, pad,
                                                big_area, feedback)
        ov = count_overlaps(parts, p, 0.0, angles)
        key = (rep["overflow"] > 0 or ov > 0, _extent_area(parts, p, angles, pad),
               hpwl(parts, graph, {r: tuple(v) for r, v in p.items()}))
        if best is None or key < best[0]:
            best = (key, p, angles, rep, frozen)
    _, p, angles, rep, frozen = best

    # order matters: decaps hug their ICs FIRST (inside the loose envelope), then
    # the shrink search compacts everything under the gate, then orientation tunes
    if decaps and rep["overflow"] == 0:
        p, rep = _decap_pass(parts, graph, p, angles, pad, R, rep)
    if shrink and rep["overflow"] == 0:
        p, angles, rep = _shrink_pass(parts, graph, topo, p, angles, frozen, pad, R)
    if rep["overflow"] == 0:
        p, rep = _flush_connectors(parts, graph, p, angles, pad, R, rep, big_area)
    if rep["overflow"] == 0:
        angles, rep = _orient_refine(parts, graph, p, angles, pad, R, rep)
    return {r: (v[0], v[1]) for r, v in p.items()}, angles, rep


def _flush_connectors(parts, graph, p, angles, pad, R, rep, big_area=800.0):
    """Slide the fixed I/O connectors flush to the nearest board edge. Perimeter
    pinning happens on quad's TARGET rectangle; shrink and settle drift them inward
    (the audit found J50/J51 ~20 mm interior) and nothing re-flushes. Cable-facing
    connectors belong at the wall. Gated: any slide the router dislikes reverts."""
    conns = [r for r in p if kind_of(r)[0] == "J"
             and _size(parts, r, 0.0)[0] * _size(parts, r, 0.0)[1] <= big_area]
    if not conns:
        return p, rep
    xs0 = [p[r][0] - eff_size(parts, r, angles.get(r, 0.0), pad)[0] / 2 for r in p]
    ys0 = [p[r][1] - eff_size(parts, r, angles.get(r, 0.0), pad)[1] / 2 for r in p]
    xs1 = [p[r][0] + eff_size(parts, r, angles.get(r, 0.0), pad)[0] / 2 for r in p]
    ys1 = [p[r][1] + eff_size(parts, r, angles.get(r, 0.0), pad)[1] / 2 for r in p]
    ex0, ey0, ex1, ey1 = min(xs0), min(ys0), max(xs1), max(ys1)

    q = {r: list(v) for r, v in p.items()}
    moved = []
    for r in sorted(conns):
        w, h = eff_size(parts, r, angles.get(r, 0.0), pad)
        # nearest wall and the flush coordinate against it
        walls = [(p[r][0] - ex0, 0, ex0 + w / 2), (ex1 - p[r][0], 0, ex1 - w / 2),
                 (p[r][1] - ey0, 1, ey0 + h / 2), (ey1 - p[r][1], 1, ey1 - h / 2)]
        dist, axis, target = min(walls)
        if dist < 1.0:
            continue                        # already flush
        cur = q[r][axis]
        # walk toward the wall until blocked (largest legal step wins)
        steps = 8
        best_val = cur
        for k in range(1, steps + 1):
            val = cur + (target - cur) * k / steps
            trial = list(q[r])
            trial[axis] = val
            clear = True
            for s in q:
                if s == r:
                    continue
                sw, sh = eff_size(parts, s, angles.get(s, 0.0), pad)
                if (abs(trial[0] - q[s][0]) < (w + sw) / 2 and
                        abs(trial[1] - q[s][1]) < (h + sh) / 2):
                    clear = False
                    break
            if clear:
                best_val = val
            else:
                break
        if abs(best_val - cur) > 1.0:
            q[r][axis] = best_val
            moved.append(r)
    if not moved:
        return p, rep
    rep2 = R.score(parts, {r: (v[0], v[1]) for r, v in q.items()}, graph, angles)
    if rep2["overflow"] == 0 and count_overlaps(parts, q, 0.0, angles) == 0:
        return q, rep2
    return p, rep


def _orient_refine(parts, graph, p, angles, pad, R, rep, sweeps=2):
    """Post-placement orientation sweep. The builder picks each part's rotation
    against the half-built board; everything committed afterwards moves the optimum
    and nothing revisits — the audit found 35 parts (incl. an LQFP worth 36 weighted-
    mm) facing the wrong way. Re-audition all 4 rotations against FINAL positions,
    a couple of sweeps, then re-gate; revert wholesale if the router objects."""
    from .graph import net_weight, power_width
    from collections import defaultdict
    pn = defaultdict(list)
    for name, members in graph.signal_nets.items():
        pn_w = net_weight(name, len(members))
        for r in members:
            pn[r].append((name, members, pn_w))
    for name, members in getattr(graph, "power_traces", {}).items():
        for r in members:
            pn[r].append((name, members, 0.8 * power_width(name)))

    snapshot = dict(angles)
    changed_total = 0
    for _ in range(sweeps):
        changed = 0
        for r in sorted(p):
            if r not in pn:
                continue
            if _size(parts, r, 0.0)[0] * _size(parts, r, 0.0)[1] > 150.0:
                continue
            base = angles.get(r, parts[r].get("angle0", 0.0))

            def cost(ang):
                s = 0.0
                for name, members, wt in pn[r][:8]:
                    ox, oy = pin_at(parts, r, name, ang)
                    px, py = p[r][0] + ox, p[r][1] + oy
                    pts = []
                    for q in members:
                        if q == r or q not in p:
                            continue
                        qx, qy = pin_at(parts, q, name, angles.get(q))
                        pts.append((p[q][0] + qx, p[q][1] + qy))
                    if not pts:
                        continue
                    tx = sum(a for a, b in pts) / len(pts)
                    ty = sum(b for a, b in pts) / len(pts)
                    s += wt * (abs(px - tx) + abs(py - ty))
                return s

            best_a, best_c = base % 360.0, cost(base)
            for k in (90.0, 180.0, 270.0):
                a = (base + k) % 360.0
                w, h = eff_size(parts, r, a, pad)
                legal = True
                for s2 in p:
                    if s2 == r:
                        continue
                    sw, sh = eff_size(parts, s2, angles.get(s2, 0.0), pad)
                    if (abs(p[r][0] - p[s2][0]) < (w + sw) / 2 and
                            abs(p[r][1] - p[s2][1]) < (h + sh) / 2):
                        legal = False
                        break
                if not legal:
                    continue
                c = cost(a)
                if c < best_c - 0.3:
                    best_a, best_c = a, c
            if abs(best_a - base % 360.0) > 1e-6:
                angles[r] = best_a
                changed += 1
        changed_total += changed
        if not changed:
            break
    if not changed_total:
        return angles, rep
    rep2 = R.score(parts, {r: (v[0], v[1]) for r, v in p.items()}, graph, angles)
    if rep2["overflow"] > 0 or count_overlaps(parts, p, 0.0, angles):
        angles.clear()
        angles.update(snapshot)      # orientation never outranks routability
        return angles, rep
    return angles, rep2


def _jitter(prior, seed, locked=()):
    """Deterministic per-seed perturbation of the mental map (stable hash, no RNG).
    Locked anchors are never perturbed — they stay welded to their mate coords."""
    locked = set(locked)
    out = {}
    for r, (x, y) in prior.items():
        if r in locked:
            out[r] = (x, y)
            continue
        hh = sum(ord(ch) * 131 ** i for i, ch in enumerate(r)) + seed * 7919
        out[r] = (x + (hh % 9 - 4) * 0.5, y + ((hh // 9) % 9 - 4) * 0.5)
    return out


def _extent_area(parts, p, angles, pad):
    xs0 = [p[r][0] - eff_size(parts, r, angles.get(r, 0.0), pad)[0] / 2 for r in p]
    ys0 = [p[r][1] - eff_size(parts, r, angles.get(r, 0.0), pad)[1] / 2 for r in p]
    xs1 = [p[r][0] + eff_size(parts, r, angles.get(r, 0.0), pad)[0] / 2 for r in p]
    ys1 = [p[r][1] + eff_size(parts, r, angles.get(r, 0.0), pad)[1] / 2 for r in p]
    return (max(xs1) - min(xs0)) * (max(ys1) - min(ys0))


def _pipeline_once(parts, graph, topo, prior, pad, big_area, feedback):
    """builder -> gate -> congestion feedback. Returns (p, angles, rep, frozen)."""
    from .builder import build
    from . import route as R

    angles = orient(parts, graph, prior)
    area = {r: _size(parts, r, 0.0)[0] * _size(parts, r, 0.0)[1] for r in parts}
    locked = {r for r in parts if parts[r].get("locked")}
    for r in locked:                       # locked parts keep their real orientation
        angles[r] = parts[r].get("angle0", angles.get(r, 0.0))
    # locked anchors commit first at their real coords (prior carries them from quad)
    # and never move thereafter; small I/O connectors also seat first.
    fixed = [r for r in parts
             if (kind_of(r)[0] == "J" and area[r] <= big_area) or r in locked]

    # when locked anchors span a real board, confine the builder+legalize to the region
    # quad already laid the movable cloud into (its prior extent, anchor-centered and
    # fill-sized). This reins in the ring-search — which otherwise flings low-fanout parts
    # outward and balloons the board — without crushing parts below legal density.
    lbounds = None
    if locked:
        lx = [parts[r]["x"] for r in locked]; ly = [parts[r]["y"] for r in locked]
        if (max(lx) - min(lx)) * (max(ly) - min(ly)) > 1000.0:
            m = 2.0
            px0 = min(min(prior[r][0] for r in prior), min(lx)) - m
            py0 = min(min(prior[r][1] for r in prior), min(ly)) - m
            px1 = max(max(prior[r][0] for r in prior), max(lx)) + m
            py1 = max(max(prior[r][1] for r in prior), max(ly)) + m
            lbounds = (px0, py0, px1, py1)

    pos, grid, routed = build(parts, graph, topo, prior, angles, pad=pad,
                              fixed=fixed, bounds=lbounds, big_area=big_area)
    p = {r: list(v) for r, v in pos.items()}
    frozen = {r for r in p if area[r] > big_area} | set(fixed) | locked

    # overlap-free by construction — but verify, never assume
    for _ in range(4):
        if count_overlaps(parts, p, 0.0, angles) == 0:
            break
        legalize(parts, p, pad, iters=400, angles=angles, frozen=frozen, bounds=lbounds)
        _shove_remaining(parts, p, angles, pad, frozen=frozen)

    rep = R.score(parts, {r: (v[0], v[1]) for r, v in p.items()}, graph, angles)
    # KEEP THE BEST placement across feedback rounds and never accept a worse one.
    # The bloat-and-relegalize mechanism opens corridors on a moderately congested
    # board, but on a genuinely dense one it only SPREADS the board — which lengthens
    # every net and makes congestion worse, a positive-feedback divergence (measured:
    # 98x102/of82 -> 140x141/of308 in one round -> 317x356 over six). Reverting to the
    # best-seen and stopping returns a compact board for the real autorouter instead.
    best_p = {r: list(v) for r, v in p.items()}
    best_rep = rep
    prev_hot = set()
    for _ in range(feedback):
        if rep["overflow"] == 0:
            break
        # inflate every part sitting on a hot cell: the legalizer then opens a
        # corridor exactly where the router ran out of capacity. If a hot cell is
        # walled in by frozen modules only — or keeps re-heating round after round
        # (a seam between two frozen walls) — the smallest adjacent frozen module
        # is temporarily released: that corridor cannot widen any other way.
        g = rep["grid"]
        release = set()
        recurring = {c for c in rep["hot"] if c in prev_hot}
        prev_hot = set(rep["hot"])
        for c in rep["hot"]:
            hx, hy = g.cell_center(c)
            near, movable_near = [], []
            for r in p:
                w, h = eff_size(parts, r, angles.get(r, 0.0), pad)
                if abs(p[r][0] - hx) < w / 2 + g.cell and abs(p[r][1] - hy) < h / 2 + g.cell:
                    near.append(r)
                    if r not in frozen:
                        movable_near.append(r)
            for r in movable_near:
                parts[r]["bloat"] = min(2.4, parts[r].get("bloat", 0.0) + 0.6)
            if near and (not movable_near or c in recurring):
                cand = [q for q in near if q in frozen and q != topo.hub]
                if not cand:
                    continue          # never move the hub to fix an edge overflow
                small = min(cand, key=lambda q: area[q])
                release.add(small)
                parts[small]["bloat"] = min(2.4, parts[small].get("bloat", 0.0) + 0.6)
        legalize(parts, p, pad, iters=500, angles=angles, frozen=frozen - release, bounds=lbounds)
        _shove_remaining(parts, p, angles, pad, frozen=frozen - release)
        rep = R.score(parts, {r: (v[0], v[1]) for r, v in p.items()}, graph, angles)
        if rep["overflow"] < best_rep["overflow"] - 1e-6:
            best_p = {r: list(v) for r, v in p.items()}   # this round genuinely helped
            best_rep = rep
        else:
            break                                          # not helping -> stop, keep best
    for r in parts:
        parts[r].pop("bloat", None)
    p = {r: list(v) for r, v in best_p.items()}            # revert to the best placement seen
    rep = best_rep
    # the feedback moves parts — finish overlap-clean (frozen first, then free-for-all)
    if count_overlaps(parts, p, 0.0, angles):
        for fz in (frozen, set()):
            for _ in range(6):
                legalize(parts, p, pad, iters=500, angles=angles, frozen=fz, bounds=lbounds)
                if count_overlaps(parts, p, 0.0, angles) == 0:
                    break
                _shove_remaining(parts, p, angles, pad, frozen=fz)
            if count_overlaps(parts, p, 0.0, angles) == 0:
                break
        rep = R.score(parts, {r: (v[0], v[1]) for r, v in p.items()}, graph, angles)
    return p, angles, rep, frozen


def _shrink_pass(parts, graph, topo, p0, angles, frozen, pad, R, lo=0.80):
    """Binary-search the smallest routable board: scale every position toward the
    centroid, re-legalize inside the scaled bounds, and keep the tightest scale the
    router still passes. Density is only ever bought with proof."""
    cx = sum(v[0] for v in p0.values()) / len(p0)
    cy = sum(v[1] for v in p0.values()) / len(p0)
    locked = {r for r in p0 if parts[r].get("locked")}

    def try_scale(sx, sy=None):
        sy = sx if sy is None else sy
        q = {r: [cx + (p0[r][0] - cx) * sx, cy + (p0[r][1] - cy) * sy] for r in p0}
        for r in locked:                   # locked anchors never scale toward centroid
            q[r] = list(p0[r])
        xs0 = [q[r][0] - eff_size(parts, r, angles.get(r, 0.0), pad)[0] / 2 for r in q]
        ys0 = [q[r][1] - eff_size(parts, r, angles.get(r, 0.0), pad)[1] / 2 for r in q]
        xs1 = [q[r][0] + eff_size(parts, r, angles.get(r, 0.0), pad)[0] / 2 for r in q]
        ys1 = [q[r][1] + eff_size(parts, r, angles.get(r, 0.0), pad)[1] / 2 for r in q]
        bounds = (min(xs0) - 0.5, min(ys0) - 0.5, max(xs1) + 0.5, max(ys1) + 0.5)
        for _ in range(6):
            legalize(parts, q, pad, iters=500, angles=angles, frozen=locked, bounds=bounds)
            if count_overlaps(parts, q, 0.0, angles) == 0:
                break
            _shove_remaining(parts, q, angles, pad, frozen=locked)
        if count_overlaps(parts, q, 0.0, angles):
            return None, None
        rep = R.score(parts, {r: (v[0], v[1]) for r, v in q.items()}, graph, angles)
        return (q, rep) if rep["overflow"] == 0 else (None, None)

    best = None
    lo_s, hi_s = lo, 1.0
    for _ in range(4):
        mid = (lo_s + hi_s) / 2
        q, rep = try_scale(mid)
        if q is not None:
            best = (q, rep, mid)
            hi_s = mid            # routable — try tighter
        else:
            lo_s = mid            # too tight — back off
    if best is None:
        return p0, angles, R.score(parts, {r: (v[0], v[1]) for r, v in p0.items()},
                                   graph, angles)
    # anisotropic pass: the uniform search leaves per-axis slack (the audit found a
    # 4 mm strip along one edge) — squeeze each axis independently on top
    q0, rep0, s0 = best
    p0 = {r: (v[0], v[1]) for r, v in q0.items()}
    cx = sum(v[0] for v in p0.values()) / len(p0)
    cy = sum(v[1] for v in p0.values()) / len(p0)
    for axis in (0, 1):
        lo_s, hi_s = 0.90, 1.0
        for _ in range(3):
            mid = (lo_s + hi_s) / 2
            q, rep = try_scale(mid if axis == 0 else 1.0,
                               mid if axis == 1 else 1.0)
            if q is not None:
                best = (q, rep, mid)
                hi_s = mid
            else:
                lo_s = mid
        if best[0] is not q0 and hi_s < 1.0:
            p0 = {r: (v[0], v[1]) for r, v in best[0].items()}
    return best[0], angles, best[1]


def _decap_pass(parts, graph, p, angles, pad, R, rep):
    """Walk plane-only decoupling caps to the nearest free slot hugging their owner
    IC (nearest non-passive sharing one of their power nets). Moves are accepted in
    gated batches — one bad move must not cancel twenty-nine good ones — and slots
    are ranked by closeness to the OWNER (the audit found the old sort ranked by
    closeness to the decap's current spot, so nothing ever moved)."""
    signal_refs = {r for members in graph.signal_nets.values() for r in members}
    ptrace_refs = {r for name, members in getattr(graph, "power_traces", {}).items()
                   for r in members}
    owners_of = {}
    for r in p:
        if kind_of(r) != "C" or r in signal_refs or r in ptrace_refs:
            continue
        my_nets = set(parts[r].get("pins", {}).keys())
        cands = [q for q in p
                 if q != r and not is_passive(q)
                 and my_nets & set(parts[q].get("pins", {}).keys())]
        if cands:
            owners_of[r] = min(cands, key=lambda q: (abs(p[q][0] - p[r][0]) +
                                                     abs(p[q][1] - p[r][1]), q))
    if not owners_of:
        return p, rep

    q = {r: list(v) for r, v in p.items()}
    accepted = {r: (v[0], v[1]) for r, v in p.items()}
    best_rep = rep
    batch, moved = [], 0

    def flush_batch():
        nonlocal best_rep, moved, batch
        if not batch:
            return
        rep2 = R.score(parts, {r: (v[0], v[1]) for r, v in q.items()}, graph, angles)
        if rep2["overflow"] == 0 and count_overlaps(parts, q, 0.0, angles) == 0:
            for r in batch:
                accepted[r] = (q[r][0], q[r][1])
            best_rep = rep2
            moved += len(batch)
        else:
            for r in batch:
                q[r] = list(accepted[r])   # revert just this batch
        batch = []

    xs = [v[0] for v in p.values()]
    ys = [v[1] for v in p.values()]
    ex0, ex1, ey0, ey1 = min(xs), max(xs), min(ys), max(ys)
    # farthest-first: the worst-strung decaps get first pick of the good slots
    order = sorted(owners_of,
                   key=lambda r: -(abs(p[owners_of[r]][0] - p[r][0]) +
                                   abs(p[owners_of[r]][1] - p[r][1])))
    for r in order:
        owner = owners_of[r]
        ow, oh = eff_size(parts, owner, angles.get(owner, 0.0), pad)
        rw, rh = eff_size(parts, r, angles.get(r, 0.0), pad)
        ox, oy = q[owner]
        cur_d = abs(q[r][0] - ox) + abs(q[r][1] - oy)
        slots = []
        for k in range(-4, 5):
            step = k * max(rw, rh) * 1.05
            slots += [(ox + step, oy - (oh + rh) / 2 - 0.2),
                      (ox + step, oy + (oh + rh) / 2 + 0.2),
                      (ox - (ow + rw) / 2 - 0.2, oy + step),
                      (ox + (ow + rw) / 2 + 0.2, oy + step)]
        # rank by closeness to the OWNER — that is the objective
        slots.sort(key=lambda t: abs(t[0] - ox) + abs(t[1] - oy))
        for x, y in slots:
            if not (ex0 <= x <= ex1 and ey0 <= y <= ey1):
                continue                   # never stretch the board for a decap
            d = abs(x - ox) + abs(y - oy)
            if d >= cur_d - 1.0:
                continue                   # not a meaningful improvement
            clear = True
            for s in q:
                if s == r:
                    continue
                sw, sh = eff_size(parts, s, angles.get(s, 0.0), pad)
                if (abs(x - q[s][0]) < (rw + sw) / 2 and
                        abs(y - q[s][1]) < (rh + sh) / 2):
                    clear = False
                    break
            if clear:
                q[r] = [x, y]
                batch.append(r)
                break
        if len(batch) >= 10:
            flush_batch()
    flush_batch()
    if not moved:
        return p, rep
    return {r: list(accepted[r]) for r in accepted}, best_rep


def _shove_remaining(parts, pos, angles, pad, frozen=None):
    """Fallback for any pair still overlapping after iterative legalize: shove them fully
    apart in one move (no half-steps), largest part holds, smaller yields."""
    frozen = frozen or set()
    for a, b in list(_candidate_pairs(parts, pos, angles, pad)):
        aw, ah = eff_size(parts, a, angles.get(a, 0.0), pad)
        bw, bh = eff_size(parts, b, angles.get(b, 0.0), pad)
        dx = pos[b][0] - pos[a][0]; dy = pos[b][1] - pos[a][1]
        oxx = (aw + bw) / 2 - abs(dx); oyy = (ah + bh) / 2 - abs(dy)
        if oxx > 0 and oyy > 0:
            # move the smaller-area part fully clear along the shorter overlap axis;
            # a frozen part never yields (the other one takes the whole move)
            mover, aw_, ah_ = (b, bw, bh) if aw * ah >= bw * bh else (a, aw, ah)
            if mover in frozen:
                mover = a if mover == b else b
                if mover in frozen:
                    continue
            sgn_x = 1 if (pos[mover][0] - pos[a if mover == b else b][0]) >= 0 else -1
            sgn_y = 1 if (pos[mover][1] - pos[a if mover == b else b][1]) >= 0 else -1
            if oxx <= oyy:
                pos[mover][0] += (oxx + 0.1) * sgn_x
            else:
                pos[mover][1] += (oyy + 0.1) * sgn_y


def pin_at(parts, r, net, angle=None):
    """Pin-anchor offset from body center for `net`, at absolute `angle` degrees.
    Offsets were read at the part's as-drawn angle (`angle0`); KiCad rotation maps
    (x, y) -> (x cos t + y sin t, -x sin t + y cos t) — verified empirically."""
    off = parts[r].get("pins", {}).get(net)
    if not off:
        return (0.0, 0.0)
    a0 = parts[r].get("angle0", 0.0)
    t = ((angle if angle is not None else a0) - a0) % 360.0
    if t < 1e-6:
        return off
    rad = math.radians(t)
    c, s = math.cos(rad), math.sin(rad)
    return (off[0] * c + off[1] * s, -off[0] * s + off[1] * c)


def _size(parts, r, pad=0.6):
    """Padded keep-out size. `bloat` is per-part extra spacing set by the routability
    feedback loop (congested regions inflate so the router gets corridors)."""
    p = parts[r]
    b = p.get("bloat", 0.0)
    return p.get("w", 2.0) + 2 * (pad + b), p.get("h", 1.5) + 2 * (pad + b)


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
            # tiny jitter so they don't stack exactly (stable hash: reproducible runs)
            hh = sum(ord(ch) * 31 ** i for i, ch in enumerate(r))
            pos[r] = (mx + (hh % 7 - 3) * 0.6, my + (hh % 5 - 2) * 0.6)
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
        mem = sorted(r for r in b.members if r in parts)   # members is a SET — sort or salt leaks in
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


def _pack_dir(parts, pos, angles, pad, axis, gap=0.35, frozen=None):
    """Shove each movable part toward the minimum along `axis` until it hits the boundary
    or the edge of an already-placed part that overlaps it on the other axis. `frozen`
    parts don't move but still block (so the M.2/CPU anchors hold and small parts pack
    up against them). Preserves order along the axis; introduces no overlaps."""
    frozen = frozen or set()
    oaxis = 1 - axis
    minc = min(pos[r][axis] - eff_size(parts, r, angles.get(r, 0.0), pad)[axis] / 2 for r in pos)
    done = []
    for r in sorted(pos, key=lambda k: pos[k][axis]):
        if r in frozen:
            done.append(r)
            continue
        rw = eff_size(parts, r, angles.get(r, 0.0), pad)
        half = rw[axis] / 2; ohalf = rw[oaxis] / 2
        rside, rtht = parts[r].get("side", "F"), parts[r].get("tht")
        limit = minc + half
        for s in done:
            # opposite-side SMD parts share the 2D area but not the copper: they don't
            # block each other, so a back passive can pack straight under a front IC.
            if rside != parts[s].get("side", "F") and not rtht and not parts[s].get("tht"):
                continue
            sw = eff_size(parts, s, angles.get(s, 0.0), pad)
            if abs(pos[r][oaxis] - pos[s][oaxis]) < ohalf + sw[oaxis] / 2:   # overlap on other axis
                cand = pos[s][axis] + sw[axis] / 2 + gap + half
                if cand > limit:
                    limit = cand
        pos[r][axis] = limit
        done.append(r)


def compact(parts, pos, angles, pad, graph=None, rounds=10, big_area=800.0):
    """Close whitespace WITHOUT breaking connectivity. Only the TRUE anchor parts (CPU
    module, M.2 socket — big enough to matter) are frozen where wirelength placement put
    them, so the M.2 stays welded to the CPU it routes to. Everything else is pulled tight
    around them by an alternating directional pack (fills corners the way gravity can't),
    then a light gravity pass re-hugs each part to its net so routing stays short."""
    frozen = {r for r in pos if _size(parts, r, 0.0)[0] * _size(parts, r, 0.0)[1] > big_area}
    if len(frozen) == len(pos):
        return
    for _ in range(rounds):
        _pack_dir(parts, pos, angles, pad, axis=0, frozen=frozen)
        _pack_dir(parts, pos, angles, pad, axis=1, frozen=frozen)
    legalize(parts, pos, pad, iters=200, angles=angles, frozen=frozen)


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
                # two SMD parts on opposite copper sides overlap only in the 2D
                # projection — they cannot physically collide, so they are not a
                # legalization pair. A THT part (drilled pin field) pierces both
                # sides and DOES collide with anything.
                if (parts[a].get("side", "F") != parts[b].get("side", "F")
                        and not parts[a].get("tht") and not parts[b].get("tht")):
                    continue
                key = (a, b) if a < b else (b, a)
                if key not in seen:
                    seen.add(key)
                    yield key


def legalize(parts, pos, pad, iters=400, angles=None, frozen=None, bounds=None):
    """Iterative overlap removal via a correct spatial hash: push every overlapping pair
    apart along the shorter axis until none remain (or `iters` passes). Rotation-aware.
    `frozen` parts never move (anchors) — an overlapping movable part yields the full
    overlap so anchors keep their position and connectivity.
    `bounds` = (x0, y0, x1, y1): movable parts are clamped inside after every sweep, so
    overflow resolves inward (this is what keeps the board small instead of ballooning).
    Clamping stops for the final third of the passes if it is fighting convergence —
    zero overlaps beats a tidy outline."""
    angles = angles or {}
    frozen = frozen or set()
    for it in range(iters):
        moved = False
        if bounds and it < max(1, iters * 2 // 3):
            x0, y0, x1, y1 = bounds
            for r in pos:
                if r in frozen:
                    continue
                w, h = eff_size(parts, r, angles.get(r, 0.0), pad)
                pos[r][0] = max(x0 + w / 2, min(x1 - w / 2, pos[r][0]))
                pos[r][1] = max(y0 + h / 2, min(y1 - h / 2, pos[r][1]))
        for a, b in _candidate_pairs(parts, pos, angles, pad):
            aw, ah = eff_size(parts, a, angles.get(a, 0.0), pad)
            bw, bh = eff_size(parts, b, angles.get(b, 0.0), pad)
            dx = pos[b][0] - pos[a][0]; dy = pos[b][1] - pos[a][1]
            oxx = (aw + bw) / 2 - abs(dx)
            oyy = (ah + bh) / 2 - abs(dy)
            if oxx > 0 and oyy > 0:
                moved = True
                fa, fb = a in frozen, b in frozen
                if fa and fb:
                    continue
                # split the correction: 0.5/0.5, or 1.0 onto the movable if the other is frozen
                sa = 0.0 if fa else (1.0 if fb else 0.5)
                sb = 0.0 if fb else (1.0 if fa else 0.5)
                if oxx <= oyy:
                    s = oxx + 0.03; sgn = 1 if dx >= 0 else -1
                    pos[b][0] += s * sb * sgn; pos[a][0] -= s * sa * sgn
                else:
                    s = oyy + 0.03; sgn = 1 if dy >= 0 else -1
                    pos[b][1] += s * sb * sgn; pos[a][1] -= s * sa * sgn
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
