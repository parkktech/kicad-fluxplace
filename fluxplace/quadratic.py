"""Quadratic (analytic) global placement — the hub-central upgrade.

WHY: force-directed floats the hub to the periphery (each subsystem is internally
cohesive and touches the hub through few nets), and pinning the hub center is a
band-aid that worsens HPWL. Minimizing SUM w_ij * dist^2 instead makes the hub's
position the *weighted mean of everything it talks to* — central by math.

Model
-----
* Energy is over PAD positions, not body centers: pin offsets are constants, so
  they land in the RHS and the system stays linear. Decaps pull toward the pad
  on a module's edge instead of its center.
* Clique edges per signal net (net_weight already normalizes by fanout).
* Power-only parts (decaps, regulators' bulk) get weak cohesion edges to their
  cluster mates so the system is non-singular and they land near their IC.
* Small edge-I/O connectors (kind J, area <= big_area) are FIXED on the
  perimeter of a target-fill rectangle (their angular order taken from a radial
  seed). Big modules (CPU, M.2) stay MOVABLE — first-class objects the math
  places centrally/adjacently; they are never exiled.
* Spreading = SimPL-lite: solve -> legalize a snapshot (big parts settled
  against each other first, then everything with big+fixed frozen) -> re-solve
  with growing anchor springs toward the legal snapshot. Converges to a legal,
  wirelength-optimal, hub-central layout.

Solver: dense numpy if importable (n~200 is trivial), else Gauss-Seidel — the
system is strictly diagonally dominant thanks to the anchor/ridge terms.
"""
import math
from collections import defaultdict

from .graph import net_weight, kind_of
from .placement import _size, legalize, group_parts, radial


def _edges(parts, graph, topo):
    """Weighted pad-to-pad springs: list of (a, b, w, qax, qay, qbx, qby) where q* are
    pin offsets from each body center for the net that created the edge. Also returns
    per-ref weighted signal degree (to find power-only orphans)."""
    E, deg = [], defaultdict(float)
    for name, members in graph.signal_nets.items():
        m = [r for r in members if r in parts]
        if len(m) < 2:
            continue
        wt = net_weight(name, len(m))
        pin = {r: parts[r].get("pins", {}).get(name, (0.0, 0.0)) for r in m}
        for i in range(len(m)):
            for j in range(i + 1, len(m)):
                a, b = m[i], m[j]
                E.append((a, b, wt, pin[a][0], pin[a][1], pin[b][0], pin[b][1]))
                deg[a] += wt
                deg[b] += wt

    # orphan rescue: parts with no signal lanes tie weakly to their cluster mates
    # (prefer mates that DO have signal edges, so orphans follow the anchored core)
    for gid, mem in group_parts(parts, topo).items():
        mem = [r for r in mem if r in parts]
        anchored = [r for r in mem if deg[r] > 0]
        for r in mem:
            if deg[r] > 0:
                continue
            tgts = (anchored or [s for s in mem if s != r])[:12]
            if not tgts:
                continue
            w = 0.4 / len(tgts)
            for s in tgts:
                E.append((r, s, w, 0.0, 0.0, 0.0, 0.0))
                deg[r] += w
                deg[s] += w
    return E, deg


def _assemble(movable, E, fixed, anchors, alpha, ridge, init):
    """Per-movable-node accumulation of the normal equations:
    diag[i] * x_i - SUM w*x_j = rhs_x[i].  Returns (idx, diag, nbrs, rhs_x, rhs_y)
    where nbrs[i] = [(j, w), ...] over movable neighbours only."""
    idx = {r: i for i, r in enumerate(movable)}
    n = len(movable)
    diag = [0.0] * n
    rhsx = [0.0] * n
    rhsy = [0.0] * n
    nbrs = [[] for _ in range(n)]
    for a, b, w, qax, qay, qbx, qby in E:
        fa, fb = a in fixed, b in fixed
        if fa and fb:
            continue
        if not fa and not fb:
            i, j = idx[a], idx[b]
            diag[i] += w
            diag[j] += w
            nbrs[i].append((j, w))
            nbrs[j].append((i, w))
            rhsx[i] += w * (qbx - qax)
            rhsx[j] += w * (qax - qbx)
            rhsy[i] += w * (qby - qay)
            rhsy[j] += w * (qay - qby)
        else:
            # one end fixed: its pad position is a constant on the RHS
            if fa:
                mv, fx = b, a
                qmx, qmy, qfx, qfy = qbx, qby, qax, qay
            else:
                mv, fx = a, b
                qmx, qmy, qfx, qfy = qax, qay, qbx, qby
            i = idx[mv]
            diag[i] += w
            rhsx[i] += w * (fixed[fx][0] + qfx - qmx)
            rhsy[i] += w * (fixed[fx][1] + qfy - qmy)
    # anchor springs (spreading) + ridge (non-singularity even for stray islands)
    for r in movable:
        i = idx[r]
        if anchors is not None and alpha > 0.0:
            diag[i] += alpha
            rhsx[i] += alpha * anchors[r][0]
            rhsy[i] += alpha * anchors[r][1]
        diag[i] += ridge
        rhsx[i] += ridge * init[r][0]
        rhsy[i] += ridge * init[r][1]
    return idx, diag, nbrs, rhsx, rhsy


def _solve(movable, E, fixed, anchors, alpha, ridge, init):
    """Solve the two independent (x, y) linear systems. numpy dense if available,
    else Gauss-Seidel (converges: strictly diagonally dominant)."""
    idx, diag, nbrs, rhsx, rhsy = _assemble(movable, E, fixed, anchors, alpha, ridge, init)
    n = len(movable)
    try:
        import numpy as np
        A = np.zeros((n, n))
        for i in range(n):
            A[i, i] = diag[i]
            for j, w in nbrs[i]:
                A[i, j] -= w
        sol = np.linalg.solve(A, np.stack([rhsx, rhsy], axis=1))
        return {r: (float(sol[idx[r], 0]), float(sol[idx[r], 1])) for r in movable}
    except ImportError:
        x = [init[r][0] for r in movable]
        y = [init[r][1] for r in movable]
        for _ in range(400):
            delta = 0.0
            for i in range(n):
                sx, sy = rhsx[i], rhsy[i]
                for j, w in nbrs[i]:
                    sx += w * x[j]
                    sy += w * y[j]
                nx, ny = sx / diag[i], sy / diag[i]
                delta = max(delta, abs(nx - x[i]), abs(ny - y[i]))
                x[i], y[i] = nx, ny
            if delta < 1e-4:
                break
        return {r: (x[idx[r]], y[idx[r]]) for r in movable}


def _pin_perimeter(parts, refs, seed, cx, cy, W, H, pad):
    """Project each connector onto the W x H rectangle perimeter along its seed-position
    ray from the seed centroid — preserves the radial ordering so branches don't cross.
    Bodies are inset so the whole footprint stays inside the outline."""
    scx = sum(seed[r][0] for r in seed) / len(seed)
    scy = sum(seed[r][1] for r in seed) / len(seed)
    fixed = {}
    for r in refs:
        dx, dy = seed[r][0] - scx, seed[r][1] - scy
        if abs(dx) + abs(dy) < 1e-6:
            dx = 1.0
        ang = math.atan2(dy, dx)
        c, s = math.cos(ang), math.sin(ang)
        t = min((W / 2) / max(abs(c), 1e-9), (H / 2) / max(abs(s), 1e-9))
        w, h = _size(parts, r, pad)
        px = max(cx - W / 2 + w / 2, min(cx + W / 2 - w / 2, cx + c * t))
        py = max(cy - H / 2 + h / 2, min(cy + H / 2 - h / 2, cy + s * t))
        fixed[r] = (px, py)
    return fixed


def quad(parts, graph, topo, center=None, pad=0.45, rounds=7, fill=0.55,
         aspect=1.35, big_area=800.0, alpha0=0.02, alpha_growth=1.7,
         pin_connectors=True):
    """Analytic global placement + SimPL-lite spreading. Returns {ref: (x, y)} — legal
    (overlap-free at `pad` spacing) with big modules central/adjacent per connectivity."""
    cx, cy = center or (0.0, 0.0)
    refs = list(parts)
    area = {r: _size(parts, r, 0.0)[0] * _size(parts, r, 0.0)[1] for r in refs}
    total = sum(area.values())
    W = math.sqrt(total / fill * aspect)
    H = (total / fill) / W

    E, _deg = _edges(parts, graph, topo)
    seed = radial(parts, topo, (cx, cy), pad)

    fixed = {}
    if pin_connectors:
        conns = [r for r in refs if kind_of(r)[0] == "J" and area[r] <= big_area]
        fixed = _pin_perimeter(parts, conns, seed, cx, cy, W, H, pad)

    movable = [r for r in refs if r not in fixed]
    big = {r for r in movable if area[r] > big_area}
    init = {r: tuple(seed[r]) for r in movable}
    # bounds slightly beyond the pin rectangle: legalization resolves inward, so the
    # final extent tracks the target-fill rect instead of ballooning outward
    slack = 1.04
    bounds = (cx - W / 2 * slack, cy - H / 2 * slack,
              cx + W / 2 * slack, cy + H / 2 * slack)
    anchors, alpha = None, 0.0
    pos = {}
    for rd in range(rounds):
        sol = _solve(movable, E, fixed, anchors, alpha, 1e-4, init)
        pos = {r: list(sol[r]) for r in movable}
        for r, p in fixed.items():
            pos[r] = list(p)
        # legal snapshot: settle big modules against each other AND the fixed
        # connectors (minimal slide — the M.2 ends up beside the CPU, not on it),
        # then everything else with those anchors frozen
        if big:
            bigpos = {r: pos[r] for r in big | set(fixed)}
            legalize(parts, bigpos, pad, iters=300, frozen=set(fixed), bounds=bounds)
            for r in big:
                pos[r] = bigpos[r]
        legalize(parts, pos, pad, iters=500, frozen=big | set(fixed), bounds=bounds)
        anchors = {r: (pos[r][0], pos[r][1]) for r in movable}
        init = anchors
        alpha = alpha0 * (alpha_growth ** rd)
    return {r: (p[0], p[1]) for r, p in pos.items()}
