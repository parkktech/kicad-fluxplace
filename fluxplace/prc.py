"""Placement PRCs — physics rule checks gradeable BEFORE routing exists.

Quilter validates candidates with 8 primitive checks; exactly one (Pin
Distance) is placement-only, and its local-cluster bundle (bypass caps,
crystals, switching converters) is what a placer can be judged on pre-route
(docs/QUILTER-DOCS-DIGEST.md §6). This module grades a placement against the
comprehension output, in their report format: measured value + explicit
tolerance window ("0.13cm within (0cm to 1cm)").

Checks:
  pin-distance     pin-anchor Euclidean distance for every constraint pair
                   (cap->parent power pin, crystal->driver, hot-loop member->
                   converter). Windows scale with decap rank: the smallest cap
                   must be closest (rank 0 tightest window).
  cap-order        per (parent, net): distances sorted by capacitance rank
                   must be monotonic — smallest cap nearest the pin.
  loop-area        converter hot loop {U, L, Cin, Cout} bounding-box area.
  pair-adjacency   series elements of a diff pair (AC caps) sit side by side.

Pure python: parts/pos/angles as used across fluxplace, no pcbnew. Every row:
{check, constraint, refs, value, lo, hi, unit, ok}. `score()` returns
(rows, npass, nfail); `summarize()` prints the Quilter-style report.
"""
import math

from .placement import pin_at

__all__ = ["score", "summarize", "WINDOWS"]

# tolerance windows (mm / mm^2). Quilter publishes no absolutes — these are
# our engineering budgets, chosen so a good hand layout passes and a strung-out
# one fails. All overridable via score(windows=...).
WINDOWS = {
    "decap_rank0": 3.0,      # smallest cap: pin-edge within 3 mm
    "decap_rank1": 6.0,
    "decap_rank2": 12.0,     # bulk caps get room
    "crystal": 6.0,
    "hot_loop": 8.0,
    "loop_area": 120.0,      # mm^2, Cin->U->L->Cout bounding box
    "pair_adjacent": 4.0,    # series caps of a pair side by side
}


def _pin_xy(parts, pos, angles, ref, net):
    """Absolute pin-anchor position for `net` on `ref` (falls back to body
    center when the net has no anchor on that part)."""
    x, y = pos[ref][0], pos[ref][1]
    try:
        dx, dy = pin_at(parts, ref, net, angles.get(ref))
        return x + dx, y + dy
    except Exception:
        return x, y


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def score(parts, pos, angles, comp, windows=None):
    """Grade placement `pos` against comprehension `comp`."""
    W = dict(WINDOWS)
    W.update(windows or {})
    rows = []

    def add(check, constraint, refs, value, hi, unit="mm"):
        rows.append(dict(check=check, constraint=constraint, refs=list(refs),
                         value=round(value, 2), lo=0.0, hi=hi, unit=unit,
                         ok=value <= hi))

    # ---- bypass caps: pin distance, rank-scaled + capacitance ordering ----
    groups = {}
    for b in comp.get("bypass_caps", ()):
        cap, parent, net = b["cap"], b["parent"], b["net"]
        if cap not in pos or parent not in pos:
            continue
        d = _dist(_pin_xy(parts, pos, angles, cap, net),
                  _pin_xy(parts, pos, angles, parent, net))
        hi = W["decap_rank%d" % min(b.get("rank", 0), 2)]
        add("pin-distance", f"bypass {parent}.{net}", (cap, parent), d, hi)
        groups.setdefault((parent, net), []).append((b.get("rank", 0), d, cap))
    for (parent, net), g in sorted(groups.items()):
        if len(g) < 2:
            continue
        g.sort()
        by_rank = [d for _, d, _ in g]
        inversions = sum(1 for i in range(len(by_rank) - 1)
                         if by_rank[i] > by_rank[i + 1] + 0.5)  # 0.5mm slack
        add("cap-order", f"bypass {parent}.{net}",
            [c for _, _, c in g], float(inversions), 0.0, unit="inversions")

    # ---- crystals: crystal (+ series R, load caps) near the driver ----
    for xt in comp.get("crystals", ()):
        xr, parent = xt["crystal"], xt["parent"]
        if xr not in pos or parent not in pos:
            continue
        net = xt["nets"][0] if xt.get("nets") else ""
        d = _dist(_pin_xy(parts, pos, angles, xr, net),
                  _pin_xy(parts, pos, angles, parent, net))
        add("pin-distance", f"crystal {xr}", (xr, parent), d, W["crystal"])
        for member in list(xt.get("series_r", ())) + list(xt.get("load_caps", ())):
            if member in pos:
                d = _dist(pos[member][:2], pos[xr][:2])
                add("pin-distance", f"crystal {xr} cluster", (member, xr),
                    d, W["crystal"])

    # ---- switching converters: member distance + hot-loop area ----
    for cv in comp.get("converters", ()):
        u = cv["u"]
        if u not in pos:
            continue
        loop_pts = [pos[u][:2]]
        for member in cv.get("hot_loop", ()):
            if member == u or member not in pos:
                continue
            d = _dist(pos[member][:2], pos[u][:2])
            add("pin-distance", f"converter {u}", (member, u), d, W["hot_loop"])
            loop_pts.append(pos[member][:2])
        if len(loop_pts) >= 3:
            xs = [p[0] for p in loop_pts]
            ys = [p[1] for p in loop_pts]
            add("loop-area", f"converter {u}", cv["hot_loop"],
                (max(xs) - min(xs)) * (max(ys) - min(ys)),
                W["loop_area"], unit="mm^2")

    # ---- diff-pair series elements side by side ----
    for pr in comp.get("diff_pairs", ()):
        segs = pr.get("segments", ())
        if len(segs) <= 2:
            continue
        # series parts = 2-pin R/C touching two pair segments
        series = sorted({p for p in pos
                         if p[0] in "CRcr" and p[1:].isdigit()
                         and len(set(parts.get(p, {}).get("pins", {}))
                                 & set(segs)) == 2})
        for i in range(0, len(series) - 1, 2):
            a, b = series[i], series[i + 1]
            add("pair-adjacency", f"pair {pr['p']}", (a, b),
                _dist(pos[a][:2], pos[b][:2]), W["pair_adjacent"])

    npass = sum(1 for r in rows if r["ok"])
    return rows, npass, len(rows) - npass


def summarize(rows, log=print, failed_only=False):
    for r in rows:
        if failed_only and r["ok"]:
            continue
        state = "within" if r["ok"] else "OUTSIDE"
        log(f"  {'PASS' if r['ok'] else 'FAIL'} {r['check']:14s} "
            f"{r['constraint']:24s} {'+'.join(r['refs'])}: "
            f"{r['value']}{r['unit']} {state} "
            f"(0 to {r['hi']}{r['unit']})")
    npass = sum(1 for r in rows if r["ok"])
    log(f"PRC: {npass}/{len(rows)} pass")
    return npass, len(rows) - npass
