"""SWLOOP — the hot loop and switch node of every switching regulator, graded on a placed board.

For each regulator that clusters.build finds (an IC with a switch-node support):

  hot loop      closed polygon through pad centres: input-cap rail pad -> IC input pin ->
                IC ground pin -> input-cap ground pad. Perimeter (mm) and area (mm^2).
  switch node   largest pad-to-pad distance on the switch net.

Always an INFO SWLOOP with those numbers. WARN SWLOOP_LARGE when the loop perimeter or the
switch-node extent exceeds 3 x the IC body diagonal (the scale of the part itself, so no
board-specific constant). FAIL SWLOOP_LARGE when `[placement] hot_loop_max_mm` or
`[placement] sw_node_max_mm` is set and exceeded. (The existing `[placement.budget] switch_node`
is a multi-drop MST net budget graded by route-ready; it is not reused here because the
extent measured here is a pad-pair span.)

Needs geometry: the board file (routediag.load). Pure logic otherwise.
"""
import math
import os

from . import clusters as CL
from . import netclass as NC
from . import routediag as RD

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"
SCALE = 3.0


def _f(level, code, msg, refs=()):
    return {"level": level, "code": code, "msg": msg, "refs": sorted(set(refs))}


def _near(pads, x, y):
    return min(pads, key=lambda p: math.hypot(p["x"] - x, p["y"] - y))


def _perimeter_area(pts):
    per = sum(math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) for i in range(len(pts)))
    area = abs(sum(pts[i - 1][0] * pts[i][1] - pts[i][0] * pts[i - 1][1] for i in range(len(pts)))) / 2.0
    return per, area


def _ic_diag(B, ic):
    ps = B["parts"][ic]["pads"]
    x0 = min(p["x"] - p["ex"] for p in ps)
    x1 = max(p["x"] + p["ex"] for p in ps)
    y0 = min(p["y"] - p["ey"] for p in ps)
    y1 = max(p["y"] + p["ey"] for p in ps)
    return math.hypot(x1 - x0, y1 - y0)


def _sw_extent(pads):
    best = 0.0
    for i, p in enumerate(pads):
        for q in pads[i + 1:]:
            best = max(best, math.hypot(p["x"] - q["x"], p["y"] - q["y"]))
    return best


def _loop(B, ic, cap):
    """-> (points, rail net) for the input-cap hot loop, or None."""
    cps = B["parts"][cap]["pads"]
    rail = [p for p in cps if p["net"] and not NC.is_ground(p["net"])]
    gnd = [p for p in cps if p["net"] and NC.is_ground(p["net"])]
    if len(rail) != 1 or len(gnd) != 1:
        return None
    cr, cg = rail[0], gnd[0]
    ic_pads = B["parts"][ic]["pads"]
    ic_in = [p for p in ic_pads if p["net"] == cr["net"]]
    ic_gnd = [p for p in ic_pads if NC.is_ground(p["net"])]
    if not ic_in or not ic_gnd:
        return None
    pin_in = _near(ic_in, cr["x"], cr["y"])
    pin_gnd = _near(ic_gnd, cg["x"], cg["y"])
    return [(cr["x"], cr["y"]), (pin_in["x"], pin_in["y"]),
            (pin_gnd["x"], pin_gnd["y"]), (cg["x"], cg["y"])], cr["net"]


def check_switch_loops(facts, cons, spec=None):
    try:
        return _check(facts, cons, spec)
    except Exception as e:  # a board the clusterer cannot read is a WARN, never a crash of review
        return [_f(WARN, "SWLOOP_UNAVAILABLE", f"switch-loop check could not run: {e}")]


def _check(facts, cons, spec):
    board_path = facts.get("board_path") or facts.get("board")
    if not isinstance(board_path, str) or not os.path.isfile(board_path):
        return []
    B = RD.load(board_path)
    cons = cons or {}
    pl = cons.get("placement") or {}
    hot_max = pl.get("hot_loop_max_mm")
    sw_max = pl.get("sw_node_max_mm")
    cls = NC.classify(B, cons, spec)
    clusters, _links = CL.build(B, cons, spec)
    nets = NC.net_pads(B)
    out = []
    for cl in clusters:
        if not cl["regulator"]:
            continue
        ic = cl["ic"]
        sw_nets = sorted({n for n, info in cls.items() if info["class"] == "switch_node"
                          and any(p["ref"] == ic for p in nets.get(n, ()))})
        sw_pads = [p for n in sw_nets for p in nets.get(n, ())]
        sw = _sw_extent(sw_pads)
        diag = _ic_diag(B, ic)
        caps = [s for s in cl["supports"] if s["rank"] == 1 and s["ref"].startswith("C")]
        loop = _loop(B, ic, caps[0]["ref"]) if caps else None
        if loop is None:
            out.append(_f(INFO, "SWLOOP", f"{ic}: switch node {sw:.2f} mm; hot loop not measured "
                          f"({'no input capacitor' if not caps else 'input capacitor not a 2-pad rail/ground part'})",
                          refs=[ic]))
            per = area = None
        else:
            pts, _rail = loop
            per, area = _perimeter_area(pts)
            cap = caps[0]["ref"]
            out.append(_f(INFO, "SWLOOP", f"{ic}: hot loop {per:.2f} mm perimeter, {area:.2f} mm^2 "
                          f"({cap}); switch node {sw:.2f} mm; IC body diagonal {diag:.2f} mm",
                          refs=[ic, cap]))
        over = []
        if hot_max is not None and per is not None and per > float(hot_max):
            over.append(f"hot loop {per:.2f} mm > [placement] hot_loop_max_mm {float(hot_max):g}")
        if sw_max is not None and sw > float(sw_max):
            over.append(f"switch node {sw:.2f} mm > [placement] sw_node_max_mm {float(sw_max):g}")
        if over:
            out.append(_f(FAIL, "SWLOOP_LARGE", f"{ic}: " + "; ".join(over), refs=[ic]))
            continue
        large = []
        if per is not None and per > SCALE * diag:
            large.append(f"hot loop {per:.2f} mm > {SCALE:g} x IC diagonal {diag:.2f} mm")
        if sw > SCALE * diag:
            large.append(f"switch node {sw:.2f} mm > {SCALE:g} x IC diagonal {diag:.2f} mm")
        if large:
            out.append(_f(WARN, "SWLOOP_LARGE", f"{ic}: " + "; ".join(large), refs=[ic]))
    return out
