"""`fluxplace density`: how full is each side of the board, before anything is routed.

Demand per side = courtyard area + fan-out ring area.  Usable per side = outline area minus the keep-outs that forbid vias
(`no_vias`) on that side's copper layer.  Utilisation = demand / usable.  Bands, on the worse side: <= 60 % OK, 60-75 % TIGHT,
> 75 % FAIL (no room to re-seat or re-place; exit 1).

Fan-out ring estimate (mm^2) per part, slot = (via_mm + clearance_mm)^2 from `plan_params`:
  * a pad on a plane/pour/ground net needs one via site: slot * 1.0 (at most 6 sites per part and net, a thermal pad is
    one array, not one via per pad);
  * a signal pad of a fine part (`escapeplan.is_fine`: many pads or fine pitch) escapes by via: slot * 1.5 (staggering);
  * a signal pad of a coarse part escapes on the surface: 0;
  * a two-pad passive whose signal net is local (<= 3 pads, all within 6 mm of it) needs no via: 0;
  * through-hole pads need no fan-out via: 0.
The courtyard of a through-hole part (any thru pad) counts on both sides; an SMD part counts on its own side only.
Regions (`--grid` mm, default 10): courtyards are clipped exactly to each cell, a part's ring is spread over its courtyard;
usable per cell is rasterised at 0.5 mm.  Growing the board only adds usable strip (demand and keep-outs unchanged), so the
utilisation can never rise with growth.  `--target T` gives, for the worse side, the growth per edge (bbox of the outline) and
uniform growth that bring it to T.  Writes `<board>.density.json`.
"""
import json
import math
import os

from . import escapeplan as EP
from . import fluxmap as FM
from . import netclass as NC

BAND_OK, BAND_TIGHT = 0.60, 0.75
RES = 0.5
STAGGER = 1.5
LOCAL_MM = 6.0
SIDES = ("F", "B")


def band(u):
    return "OK" if u <= BAND_OK else ("TIGHT" if u <= BAND_TIGHT else "FAIL")


def _inside(poly, x, y):
    c = False
    n = len(poly)
    for i in range(n):
        (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % n]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            c = not c
    return c


def _ring(npads, part_ref, part, pr, planes):
    """Fan-out ring area (mm^2) of one part: see the module docstring."""
    slot = (pr["via_mm"] + pr["clearance_mm"]) ** 2
    pads = [q for q in part["pads"] if not q.get("np") and q.get("net")]
    if not pads or EP.is_mech(part_ref):
        return 0.0
    fine = EP.is_fine(part, pr)
    two = part["n_pads"] <= 2
    total, per = 0.0, {}
    for q in pads:
        if q.get("tht"):
            continue
        n = q["net"]
        if n in planes or NC.is_ground(n):
            if per.get(n, 0) < 6:
                per[n] = per.get(n, 0) + 1
                total += slot
            continue
        if two:
            ps = npads.get(n, [])
            if len(ps) <= 3 and all(math.hypot(o["x"] - q["x"], o["y"] - q["y"]) <= LOCAL_MM for o in ps):
                continue
            total += slot * STAGGER
        elif fine:
            total += slot * STAGGER
    return total


def compute(B, cons=None, spec=None, grid=10.0):
    cons = cons or {}
    pr = EP.plan_params(cons)
    cls = NC.classify(B, cons, spec)
    planes = set(EP.plane_nets(B, cons, cls, pr))
    npads = NC.net_pads(B)
    poly = FM._outline(B)
    x0, y0, x1, y1 = (min(p[0] for p in poly), min(p[1] for p in poly), max(p[0] for p in poly), max(p[1] for p in poly))
    cu = B["copper"]
    ncx, ncy = max(1, math.ceil((x1 - x0) / grid)), max(1, math.ceil((y1 - y0) / grid))
    reg = {s: [[{"usable": 0.0, "court": 0.0, "ring": 0.0} for _ in range(ncx)] for _ in range(ncy)] for s in SIDES}
    kos = {s: [] for s in SIDES}
    for z in B["zones"]:
        if z["keepout"] and z["pts"] and z.get("no_vias"):
            xs, ys = [p[0] for p in z["pts"]], [p[1] for p in z["pts"]]
            for s, lay in (("F", cu[0]), ("B", cu[-1])):
                if lay in z["layers"]:
                    kos[s].append((min(xs), min(ys), max(xs), max(ys), z["pts"]))
    ko_area = {s: 0.0 for s in SIDES}
    out_area = 0.0
    nx, ny = math.ceil((x1 - x0) / RES), math.ceil((y1 - y0) / RES)
    cell = RES * RES
    for j in range(ny):
        y = y0 + (j + 0.5) * RES
        for i in range(nx):
            x = x0 + (i + 0.5) * RES
            if not _inside(poly, x, y):
                continue
            out_area += cell
            gi, gj = min(int((x - x0) / grid), ncx - 1), min(int((y - y0) / grid), ncy - 1)
            for s in SIDES:
                if any(a <= x <= c and b <= y <= d and _inside(pts, x, y) for a, b, c, d, pts in kos[s]):
                    ko_area[s] += cell
                else:
                    reg[s][gj][gi]["usable"] += cell
    tot = {s: {"court": 0.0, "ring": 0.0} for s in SIDES}
    for ref, p in B["parts"].items():
        if EP.is_mech(ref):
            continue
        c = p["court"]
        area = max((c[2] - c[0]) * (c[3] - c[1]), 1e-9)
        tht = any(q.get("tht") and not q.get("np") for q in p["pads"])
        ring = 0.0 if tht else _ring(npads, ref, p, pr, planes)
        for s in (SIDES if tht else (p["side"][0],)):
            tot[s]["court"] += area
            tot[s]["ring"] += ring
            for gj in range(max(0, int((c[1] - y0) // grid)), min(ncy, int((c[3] - y0) // grid) + 1)):
                for gi in range(max(0, int((c[0] - x0) // grid)), min(ncx, int((c[2] - x0) // grid) + 1)):
                    w = min(c[2], x0 + (gi + 1) * grid) - max(c[0], x0 + gi * grid)
                    h = min(c[3], y0 + (gj + 1) * grid) - max(c[1], y0 + gj * grid)
                    if w > 0 and h > 0:
                        reg[s][gj][gi]["court"] += w * h
                        reg[s][gj][gi]["ring"] += ring * w * h / area
    res = {"bbox": [x0, y0, x1, y1], "outline_mm2": out_area, "grid": grid, "sides": {}, "regions": []}
    D = U = 0.0
    for s in SIDES:
        d = tot[s]["court"] + tot[s]["ring"]
        u = out_area - ko_area[s]
        D, U = D + d, U + u
        res["sides"][s] = {"court_mm2": tot[s]["court"], "ring_mm2": tot[s]["ring"], "keepout_mm2": ko_area[s],
                           "usable_mm2": u, "util": d / u if u > 0 else (math.inf if d else 0.0)}
        res["sides"][s]["band"] = band(res["sides"][s]["util"])
        for gj in range(ncy):
            for gi in range(ncx):
                g = reg[s][gj][gi]
                dm = g["court"] + g["ring"]
                if g["usable"] < 0.25 * grid * grid:          # edge slivers are not regions
                    continue
                ut = dm / g["usable"] if g["usable"] > 0 else (math.inf if dm else 0.0)
                res["regions"].append({"side": s, "x": x0 + gi * grid, "y": y0 + gj * grid, "court_mm2": g["court"],
                                       "ring_mm2": g["ring"], "usable_mm2": g["usable"], "util": ut})
    res["total"] = {"court_mm2": sum(tot[s]["court"] for s in SIDES), "ring_mm2": sum(tot[s]["ring"] for s in SIDES),
                    "keepout_mm2": sum(ko_area.values()), "usable_mm2": U, "util": D / U if U else 0.0}
    worst = max(SIDES, key=lambda s: res["sides"][s]["util"])
    res["worst_side"], res["verdict"] = worst, res["sides"][worst]["band"]
    res["regions"].sort(key=lambda r: (-r["util"], r["side"], r["y"], r["x"]))
    return res


def growth(res, target):
    """Edge growth (mm) that brings the worse side to `target`.  New usable = old + strip (keep-outs unchanged)."""
    s = res["sides"][res["worst_side"]]
    x0, y0, x1, y1 = res["bbox"]
    W, H = x1 - x0, y1 - y0
    dem = s["usable_mm2"] * s["util"]
    if s["util"] <= target:
        return {"needed": False}
    gain = dem / target - s["usable_mm2"]
    uni = (-(2 * (W + H)) + math.sqrt(4 * (W + H) ** 2 + 16 * gain)) / 8      # 2x(W+H) + 4x^2 = gain
    return {"needed": True, "side": res["worst_side"], "gain_mm2": gain, "left": gain / H, "right": gain / H,
            "top": gain / W, "bottom": gain / W, "uniform": uni}


def _json(o):
    return None if isinstance(o, float) and math.isinf(o) else o


def cmd_density(a):
    import time
    from . import plancli as PC, routediag as RD
    t0 = time.time()
    cons, spec = PC._ctx(a)
    B = RD.load(a.board)
    res = compute(B, cons, spec, grid=float(a.grid))
    tg = growth(res, float(a.target)) if a.target is not None else None
    out = os.path.splitext(a.board)[0] + ".density.json"
    with open(out, "w") as fh:
        json.dump({"density": res, "target": tg}, fh, indent=1, default=_json)
    sd = res["sides"]
    line = (f"density: {res['verdict']} (exit {1 if res['verdict'] == 'FAIL' else 0}) - " +
            ", ".join(f"{s} {sd[s]['util'] * 100:.1f}% {sd[s]['band']}" for s in SIDES) +
            f", total {res['total']['util'] * 100:.1f}%; bands OK<=60 TIGHT<=75 FAIL>75; {time.time() - t0:.1f} s")
    counts = "\n".join(f"  {s}: court {sd[s]['court_mm2']:.0f} + ring {sd[s]['ring_mm2']:.0f} mm2, keep-out {sd[s]['keepout_mm2']:.0f}, "
                       f"usable {sd[s]['usable_mm2']:.0f} mm2" for s in SIDES)
    worst = [f"  {r['side']} ({r['x']:.0f},{r['y']:.0f}) {r['util'] * 100:.0f}%  court {r['court_mm2']:.0f} ring {r['ring_mm2']:.0f} "
             f"usable {r['usable_mm2']:.0f}" for r in res["regions"]]
    PC._emit(a, line, counts, worst, out)
    if tg is not None and not getattr(a, "quiet", False):
        if not tg["needed"]:
            print(f"target {a.target:g}: already met on both sides")
        else:
            print(f"target {a.target:g} on side {tg['side']} needs +{tg['gain_mm2']:.0f} mm2 usable:")
            for k in ("left", "right", "top", "bottom"):
                print(f"  grow {tg[k]:.1f} mm on the {k} edge")
            print(f"  grow {tg['uniform']:.1f} mm on every edge")
    if res["verdict"] == "FAIL":
        raise SystemExit(1)


def add_parser(sub):
    p = sub.add_parser("density", help="courtyard + fan-out ring utilisation per side and region; bands OK/TIGHT/FAIL, --target growth")
    p.add_argument("board")
    p.add_argument("--constraints", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--target", type=float, default=None, help="utilisation to reach (0.65): growth options per edge")
    p.add_argument("--grid", type=float, default=10.0, help="region size mm (default 10)")
    p.add_argument("--full", dest="full_output", action="store_true")
    p.add_argument("-q", "--quiet", dest="quiet", action="store_true")
    p.add_argument("--top", dest="top", type=int, default=None)
    p.set_defaults(fn=cmd_density)
