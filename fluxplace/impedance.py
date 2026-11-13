"""Controlled-impedance geometry COMPUTED from a stackup (nothing fitted to a board).

`geometry(stackup, planes, layer)` gives the dielectric height and Er above and below a signal layer to the nearest reference
planes; the model follows: one plane = microstrip, two planes = stripline (symmetric when the two heights agree within 5 %,
else asymmetric).  Closed forms are the IPC-2141 ones already in stackup.py (re-used, not copied):

  microstrip           Z0 = 87/sqrt(er+1.41) ln(5.98 h/(0.8 w + t))
  edge-coupled         Zdiff = 2 Z0 (1 - 0.48 exp(-0.96 s/h))
  stripline            Z0 = 60/sqrt(er) ln(4 b/(0.67 pi (0.8 w + t))),  b = hu + hd + t
  edge-coupled strip.  Zdiff = 2 Z0 (1 - 0.347 exp(-2.9 s/b))
  asymmetric stripline Z0 = parallel combination of the two symmetric striplines (hu, hd)

Every result carries the formula, its inputs and a stated accuracy (+-10 % inside the formula's validity range, +-15..20 %
outside it or for asymmetric lines).  A closed form screens a geometry; the fab's field solver signs it off.
"""
import math

from . import review as R
from . import stackup as ST

FORMULAS = {
    "microstrip": "IPC-2141 Z0 = 87/sqrt(er+1.41) ln(5.98 h/(0.8 w+t))",
    "edge-coupled microstrip": "IPC-2141 Zdiff = 2 Z0 (1 - 0.48 exp(-0.96 s/h)), Z0 as microstrip",
    "stripline": "IPC-2141 Z0 = 60/sqrt(er) ln(4 b/(0.67 pi (0.8 w+t))), b = hu+hd+t",
    "edge-coupled stripline": "Zdiff = 2 Z0 (1 - 0.347 exp(-2.9 s/b)), Z0 as stripline",
    "asymmetric stripline": "Z0 = parallel combination of the two symmetric striplines (hu, hd)",
    "edge-coupled asymmetric stripline": "Zdiff = 2 Z0 (1 - 0.347 exp(-2.9 s/b)), Z0 as asymmetric stripline, b = hu+hd+t",
}
GAPS = (0.1, 0.127, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5)


def signal_layers(stackup, planes):
    return [r["name"] for r in stackup if r.get("type") == "copper" and r.get("name") and r["name"] not in planes]


def geometry(stackup, planes, layer):
    """{layer, hu, eu, hd, ed, t, model} or None for an unknown layer; model None = no reference plane at all."""
    g = R.layer_geometry(stackup, set(planes), layer)
    if g is None:
        return None
    hu, eu, hd, ed, t = g
    if hu is None and hd is None:
        model = None
    elif hu is None or hd is None:
        model = "microstrip"
    else:
        model = "stripline" if abs(hu - hd) <= 0.05 * max(hu, hd) else "asymmetric stripline"
    return {"layer": layer, "hu": hu, "eu": eu, "hd": hd, "ed": ed, "t": t, "model": model}


def _z0(w, g):
    if g["model"] == "microstrip":
        h, er = (g["hd"], g["ed"]) if g["hu"] is None else (g["hu"], g["eu"])
        return ST.microstrip_z0(w, h, g["t"], er)
    if g["model"] == "stripline":
        er = (g["eu"] + g["ed"]) / 2
        return ST.stripline_z0(w, g["hu"] + g["hd"] + g["t"], g["t"], er)
    z1 = ST.stripline_z0(w, 2 * g["hu"] + g["t"], g["t"], g["eu"])
    z2 = ST.stripline_z0(w, 2 * g["hd"] + g["t"], g["t"], g["ed"])
    return 2 * z1 * z2 / (z1 + z2)


def _height(g):
    """h for the coupling term: the dielectric height (microstrip) or b (stripline)."""
    if g["model"] == "microstrip":
        return g["hd"] if g["hu"] is None else g["hu"]
    return g["hu"] + g["hd"] + g["t"]


def z_single(w, g):
    return _z0(w, g)


def z_diff(w, s, g):
    z0 = _z0(w, g)
    h = _height(g)
    k = (0.48, 0.96) if g["model"] == "microstrip" else (0.347, 2.9)
    return 2.0 * z0 * (1.0 - k[0] * math.exp(-k[1] * s / h))


def model_name(g, diff):
    m = g["model"]
    return ("edge-coupled " + m) if diff else m


def accuracy(w, s, g, diff):
    """Stated accuracy (percent, text) of the closed form for this geometry."""
    h = _height(g)
    lo, hi = (0.1, 3.0) if g["model"] == "microstrip" else (0.02, 0.35)
    inside = lo <= w / h <= hi and (not diff or 0.2 <= s / h <= 3.5)
    if g["model"] == "asymmetric stripline":
        return 15, "+-15 % (asymmetric stripline: parallel-combination estimate)"
    if inside:
        return 10, "+-10 % (IPC-2141, w/h and s/h inside the formula's range)"
    return 20, f"+-20 % (w/h = {w / h:.2f}" + (f", s/h = {s / h:.2f}" if diff else "") + " is outside the formula's validity range)"


def _bisect(fn, target, lo, hi):
    return ST._bisect(fn, target, lo, hi)


def solve_single(target, g, lo=0.04, hi=4.0):
    return _bisect(lambda w: _z0(w, g), target, lo, hi)


def solve_diff(target, g, gap, lo=0.04, hi=4.0):
    return _bisect(lambda w: z_diff(w, gap, g), target, lo, hi)


def options(target, diff, g, floor=0.127, gap=None, max_options=4):
    """Manufacturable geometries on this layer: [{width, gap, z, accuracy, formula, inputs}] best first (>= floor, narrowest
    gap that keeps width >= floor).  Empty when no reference plane or the target is not reachable."""
    if g is None or g["model"] is None:
        return []
    inputs = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in g.items() if k != "model" and v is not None}
    out = []
    if not diff:
        w = solve_single(target, g)
        if w:
            acc, txt = accuracy(w, 0, g, False)
            out.append({"width_mm": round(w, 3), "gap_mm": None, "z": round(z_single(w, g), 1), "accuracy_pct": acc,
                        "accuracy": txt, "model": model_name(g, False), "formula": FORMULAS[model_name(g, False)],
                        "inputs": inputs, "manufacturable": w >= floor})
        return out
    for s in ([gap] if gap else GAPS):
        w = solve_diff(target, g, s)
        if not w:
            continue
        acc, txt = accuracy(w, s, g, True)
        m = model_name(g, True)
        out.append({"width_mm": round(w, 3), "gap_mm": s, "z": round(z_diff(w, s, g), 1), "accuracy_pct": acc, "accuracy": txt,
                    "model": m, "formula": FORMULAS[m], "inputs": inputs, "manufacturable": w >= floor and s >= floor})
    good = [o for o in out if o["manufacturable"]]
    if not good:                      # nothing clears the floor: best margin first, flagged by `manufacturable: False`
        out.sort(key=lambda o: -min(o["width_mm"], o["gap_mm"]))
    return (good or out)[:max_options]
