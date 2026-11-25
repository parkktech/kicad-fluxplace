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
import re
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


# ================================================================================================================
# Crowding: how full is each side of the board with the PARTS (courtyards), measured before and after the back-side fill.
# `compute` above is the fan-out-ring view for the `density` command; this is the placement view: usable area per side
# (outline minus that side's closed keep-outs minus fixed-part courtyards), the courtyard load of the free parts, a 10 mm
# cell map, the shortfall it causes (supports with no seat, routing-channel overflow, escape demand), one verdict per side,
# and, when the back cannot absorb it, the board size the parts need.  All limits in ONE place (`DEFAULTS`), each
# overridable in `[placement]` as `density_<key>`.
# Why these numbers: the router needs free copper between courtyards (courtyards are 0.25 mm outside the pads, tracks and
# vias live in the gaps).  Measured on a 6-layer board: the front routed cleanly up to ~50 % courtyard cover, supports
# started to find no seat near 59 %, and above ~65 % the escape lanes of the fine parts no longer fit.  A 10 mm cell
# beyond 85 % has no gap a support part can be moved into.  The size target (0.50) is the warn limit: a board sized for it
# starts below the warning.
DEFAULTS = {"warn": 0.50, "fail": 0.65, "cell_fail": 0.85, "target": 0.50, "escape_k": 2.0, "escape_reach_mm": 2.2,
            "far_hot_fail": 1}
CELL = 10.0
RAST = 0.5


def limits(cons):
    pl = (cons or {}).get("placement") or {}
    return {k: float(pl.get("density_" + k, v)) for k, v in DEFAULTS.items()}


def _mask_poly(poly, x0, y0, nx, ny):
    import numpy as np
    cx = x0 + (np.arange(nx) + 0.5) * RAST
    cy = y0 + (np.arange(ny) + 0.5) * RAST
    GX, GY = np.meshgrid(cx, cy)
    inside = np.zeros(GX.shape, bool)
    n = len(poly)
    for k in range(n):
        ax, ay = poly[k]
        bx, by = poly[(k + 1) % n]
        if ay == by:
            continue
        inside ^= ((ay > GY) != (by > GY)) & (GX < (bx - ax) * (GY - ay) / (by - ay) + ax)
    return inside


def _fill(arr, x0, y0, r):
    nyy, nxx = arr.shape
    i0, i1 = max(0, int((r[0] - x0) / RAST)), min(nxx, int(math.ceil((r[2] - x0) / RAST)))
    j0, j1 = max(0, int((r[1] - y0) / RAST)), min(nyy, int(math.ceil((r[3] - y0) / RAST)))
    if i1 > i0 and j1 > j0:
        arr[j0:j1, i0:i1] = True


def usage(B, cons, spec=None, grid=CELL):
    """per side {usable_mm2, fixed_mm2, closed_mm2, load_mm2, util, parts, cells: [{x, y, cover, free}]}; plus outline, bbox, small-part
    eligibility for the back.  Courtyard rectangles on the raster (0.5 mm): union areas, not sums, for the usable figure."""
    import numpy as np
    from . import modules as MD, legalise as LG, backside as BS
    lim = limits(cons)
    poly = FM._outline(B)
    x0, y0 = min(p[0] for p in poly), min(p[1] for p in poly)
    x1, y1 = max(p[0] for p in poly), max(p[1] for p in poly)
    nx, ny = int(math.ceil((x1 - x0) / RAST)), int(math.ceil((y1 - y0) / RAST))
    inside = _mask_poly(poly, x0, y0, nx, ny)
    fixed = MD.fixed_set(B, cons)
    closed = {s: np.zeros_like(inside) for s in SIDES}
    for rect, sd, _allow, _mh, _nm in LG.keepout_rects(B, cons):
        for s in sd:
            _fill(closed[s], x0, y0, rect)
    fixm = {s: np.zeros_like(inside) for s in SIDES}
    occ = {s: np.zeros_like(inside) for s in SIDES}
    free_m = {s: np.zeros_like(inside) for s in SIDES}
    load = {s: 0.0 for s in SIDES}
    nparts = {s: 0 for s in SIDES}
    single = {s: {} for s in SIDES}                               # (cell column, cell row) -> the largest courtyard area of ONE part in that cell
    for ref, p in B["parts"].items():
        if EP.is_mech(ref) or p["pos"][0] > 9e4:
            continue
        c = p["court"]
        area = max((c[2] - c[0]) * (c[3] - c[1]), 0.0)
        tht = any(q.get("tht") and not q.get("np") for q in p["pads"])
        for s in (SIDES if tht else (p["side"][0],)):
            for cj in range(max(0, int((c[1] - y0) // grid)), int((c[3] - y0) // grid) + 1):
                for ci in range(max(0, int((c[0] - x0) // grid)), int((c[2] - x0) // grid) + 1):
                    w = min(c[2], x0 + (ci + 1) * grid) - max(c[0], x0 + ci * grid)
                    h = min(c[3], y0 + (cj + 1) * grid) - max(c[1], y0 + cj * grid)
                    if w > 0 and h > 0 and w * h > single[s].get((ci, cj), 0.0):
                        single[s][(ci, cj)] = w * h
            _fill(occ[s], x0, y0, c)
            if ref in fixed or p.get("locked"):
                _fill(fixm[s], x0, y0, c)
            else:
                load[s] += area
                nparts[s] += 1
                _fill(free_m[s], x0, y0, c)
    res = {"bbox": [x0, y0, x1, y1], "outline_mm2": float(inside.sum()) * RAST * RAST, "sides": {}, "limits": lim}
    cell = RAST * RAST
    k = int(round(grid / RAST))
    for s in SIDES:
        avail = inside & ~closed[s]
        usable = avail & ~fixm[s]
        u = float(usable.sum()) * cell
        cells = []
        for j in range(0, ny, k):
            for i in range(0, nx, k):
                a = avail[j:j + k, i:i + k]
                if a.sum() * cell < 0.5 * grid * grid:             # an edge sliver is not a region
                    continue
                cover = float((occ[s][j:j + k, i:i + k] & a).sum()) / float(a.sum())
                big = single[s].get((i // k, j // k), 0.0) >= 0.5 * float(a.sum()) * cell      # one courtyard covers half the cell: a big part's cell, not crowding
                cells.append({"x": round(x0 + i * RAST, 2), "y": round(y0 + j * RAST, 2), "cover": cover, "big": big,
                              "free": bool((free_m[s][j:j + k, i:i + k] & a).any())})
        res["sides"][s] = {"usable_mm2": u, "fixed_mm2": float((fixm[s] & inside & ~closed[s]).sum()) * cell,
                           "closed_mm2": float((inside & closed[s]).sum()) * cell, "load_mm2": load[s], "parts": nparts[s],
                           "util": load[s] / u if u > 0 else (math.inf if load[s] else 0.0), "cells": cells,
                           "hot": [c for c in cells if c["free"] and not c["big"] and c["cover"] >= lim["cell_fail"]],
                           "big": [c for c in cells if c["big"] and c["cover"] >= lim["cell_fail"]]}
    # parts that could move F -> B: small, free, on the front, eligible under the sides policy
    elig, ea = [], 0.0
    if BS.active(cons):
        ctx = BS.Ctx(B, cons, spec)
        for ref, p in sorted(B["parts"].items()):
            c = p["court"]
            a = (c[2] - c[0]) * (c[3] - c[1])
            if p["side"] == "F.Cu" and ref not in fixed and a < BS.SMALL_MM2 and BS.back_ok(B, cons, ref, ctx=ctx)[0]:
                elig.append(ref)
                ea += a
    res["eligible"] = {"policy": BS.policy(cons)[0], "n": len(elig), "mm2": ea, "refs": elig}
    return res


def _cell_of(u, x, y, grid=CELL):
    x0, y0 = u["bbox"][0], u["bbox"][1]
    return (round(x0 + int((x - x0) // grid) * grid, 2), round(y0 + int((y - y0) // grid) * grid, 2))


def shortfall(B, cons, spec, u, overflow=None):
    """what the crowding costs: supports beyond their limit (by cell), plan-route overflow (given by the caller, floorplan has it),
    escape demand of the dense parts (pins x pitch x reach, mm2, per side)"""
    from . import clusters as CL
    pr = EP.plan_params(cons)
    lim = u["limits"]
    far = {s: {} for s in SIDES}
    far_refs = set()
    try:
        cl, _ = CL.build(B, cons, spec)
        for c in cl:
            for s in c["supports"]:
                if s["dist"] > s["limit"] + 1e-6 and s["ref"] in B["parts"]:
                    p = B["parts"][s["ref"]]
                    key = _cell_of(u, p["pos"][0], p["pos"][1])
                    sd = p["side"][0]
                    far[sd][key] = far[sd].get(key, 0) + 1
                    far_refs.add(s["ref"])
    except Exception:                                       # noqa: BLE001 - a clusters failure must not hide the density figure
        far, far_refs = {s: {} for s in SIDES}, set()
    esc = {s: 0.0 for s in SIDES}
    dense = {s: 0.0 for s in SIDES}                                 # free dense parts' courtyards (their routing share is already in load / target)
    from . import modules as MD0
    fx = MD0.fixed_set(B, cons)
    for ref, p in B["parts"].items():
        if EP.is_mech(ref) or not EP.is_fine(p, pr):
            continue
        if ref not in fx and not p["tht"]:
            dense[p["side"][0]] += _area(p)
        n = len([q for q in p["pads"] if not q.get("np")])
        pitch = EP.part_pitch(p)
        if not p["tht"]:
            esc[p["side"][0]] += n * min(pitch, 1.0) * lim["escape_reach_mm"]
    return {"far_refs": sorted(far_refs), "far_by_cell": far, "far": {s: sum(v.values()) for s, v in far.items()},
            "overflow": overflow, "escape_mm2": esc, "dense_court_mm2": dense}


def verdicts(u, sf):
    """{side: {verdict ok|warn|fail, reasons}}"""
    lim = u["limits"]
    out = {}
    for s in SIDES:
        d = u["sides"][s]
        why, v = [], "ok"
        hotkeys = {(c["x"], c["y"]) for c in d["hot"]}
        far_hot = sum(n for k, n in sf["far_by_cell"][s].items() if k in hotkeys)
        if d["util"] >= lim["fail"]:
            v = "fail"
            why.append(f"utilisation {d['util'] * 100:.0f} % >= {lim['fail'] * 100:.0f} %")
        elif d["util"] >= lim["warn"]:
            v = "warn"
            why.append(f"utilisation {d['util'] * 100:.0f} % >= {lim['warn'] * 100:.0f} %")
        if d["hot"]:
            v = v if v == "fail" else "warn"
            why.append(f"{len(d['hot'])} hot cell(s) >= {lim['cell_fail'] * 100:.0f} %")
        if far_hot:
            why.append(f"{far_hot} support(s) beyond their limit stand in hot cells")
            if far_hot >= lim["far_hot_fail"] and d["util"] >= lim["warn"]:
                v = "fail"
            elif v == "ok":
                v = "warn"
        if sf["far"][s] and not far_hot:
            why.append(f"{sf['far'][s]} support(s) beyond their limit")
            v = "warn" if v == "ok" else v
        if sf["overflow"] and s == "F":
            why.append(f"routing channel overflow {sf['overflow']}")
            v = "warn" if v == "ok" else v
        esc = sf["escape_mm2"][s]
        if d["usable_mm2"] and esc / d["usable_mm2"] >= 0.15:
            why.append(f"escape demand {esc:.0f} mm2 = {esc / d['usable_mm2'] * 100:.0f} % of usable")
            v = "warn" if v == "ok" else v
        out[s] = {"verdict": v, "reasons": why, "util": d["util"]}
    return out


def line(u, sf=None, vd=None):
    sd = u["sides"]
    lim = u["limits"]
    band = lambda x: "fail" if x >= lim["fail"] else "warn" if x >= lim["warn"] else None
    side = lambda s: f"{s} {sd[s]['util'] * 100:.0f} % of {sd[s]['usable_mm2']:.0f} mm2" + (f" ({band(sd[s]['util'])})" if band(sd[s]["util"]) else "")
    hot = sorted(sd["F"]["hot"] + sd["B"]["hot"], key=lambda c: (-c["cover"], c["y"], c["x"]))
    allhot = [(s, c) for s in SIDES for c in sd[s]["hot"]]
    show = ", ".join(f"{s}({c['x']:.0f},{c['y']:.0f}) {c['cover'] * 100:.0f} %" for s, c in sorted(allhot, key=lambda t: (-t[1]["cover"], t[0], t[1]["y"], t[1]["x"]))[:5])
    e = u["eligible"]
    elig = (f"movable small parts F->B eligible: {e['n']} ({e['mm2']:.0f} mm2)" if e["policy"] in ("both", "both-light-back")
            else "movable small parts F->B eligible: none (sides policy " + (e["policy"] or "not set") + ")")
    nbig = sum(len(sd[x]["big"]) for x in SIDES)
    s = (f"density: {side('F')}, {side('B')}; {len(hot)} hot cells (>= {lim['cell_fail'] * 100:.0f} %)" + (f": {show}" if show else "")
         + (f" (+{nbig} big-part cells, not counted)" if nbig else "") + "; " + elig)
    if sf is not None:
        s += f"; far supports F {sf['far']['F']} B {sf['far']['B']}, escape F {sf['escape_mm2']['F']:.0f} mm2"
    if vd is not None:
        s += "; crowding " + ", ".join(f"{k} {v['verdict']}" for k, v in vd.items())
    return s


# ---- the board size the parts need ----------------------------------------------------------------------------------
def enclosure_limit(B, cons, spec=None):
    """([w, h] mm | None, where): `[placement] max_board_mm`, `[enclosure] max_size_mm`, the spec's `enclosure.max_size_mm`"""
    for src, v in (("[placement] max_board_mm", ((cons or {}).get("placement") or {}).get("max_board_mm")),
                   ("[enclosure] max_size_mm", ((cons or {}).get("enclosure") or {}).get("max_size_mm")),
                   ("spec enclosure.max_size_mm", ((spec or {}).get("enclosure") or {}).get("max_size_mm") if isinstance(spec, dict) else None)):
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return [float(v[0]), float(v[1])], src
    return None, "no enclosure limit declared"


def grow_key(g):
    return "grow-" + "".join(f"{k}{v:g}" for k, v in zip("LRTB", g))


def parse_grow(choice):
    """'grow-L0R8T0B0' -> (0.0, 8.0, 0.0, 0.0); None for anything else (keep)"""
    m = re.fullmatch(r"grow-L([\d.]+)R([\d.]+)T([\d.]+)B([\d.]+)", str(choice or ""))
    return tuple(float(v) for v in m.groups()) if m else None


def suggest(B, cons, spec, u, sf, order=None):
    """the outline the parts need, or {"fits": True}.
    required area of a side = free courtyards on it / target utilisation + fixed courtyards on it + its closed areas
                              + routing allowance (escape_k x the dense parts' pins x pitch x reach);
    needed outline = the larger side; growth = needed - current outline.  Options: the two cheapest edges and both axes."""
    lim = u["limits"]
    A0 = u["outline_mm2"]
    req = {}
    moved = u["eligible"]["mm2"] if u["eligible"]["n"] else 0.0                  # the back takes the eligible small parts first
    loads = {"F": u["sides"]["F"]["load_mm2"] - moved, "B": u["sides"]["B"]["load_mm2"] + moved}
    for s in SIDES:
        d = u["sides"][s]
        # the free courtyards / target already contain the routing share (1/target - 1) of every ordinary part; a dense part needs escape
        # lanes on top of that share, so only the excess of its lanes over the share it already got is added (routing counted once)
        allow = max(0.0, lim["escape_k"] * sf["escape_mm2"][s] - (1.0 / lim["target"] - 1.0) * sf.get("dense_court_mm2", {}).get(s, 0.0))
        req[s] = loads[s] / lim["target"] + d["fixed_mm2"] + d["closed_mm2"] + allow
    need = max(req.values())
    x0, y0, x1, y1 = u["bbox"]
    W, H = x1 - x0, y1 - y0
    out = {"required_mm2": {s: round(v, 1) for s, v in req.items()}, "needed_outline_mm2": round(need, 1), "outline_mm2": round(A0, 1),
           "formula": (f"per side: (free courtyards, eligible small parts counted on the back) / {lim['target']:g} + fixed courtyards + closed areas + "
                    f"max(0, {lim['escape_k']:g} x sum(dense pins x pitch x {lim['escape_reach_mm']:g} mm) - ({1 / lim['target'] - 1:g}) x dense free courtyards); outline = larger side"),
           "size_mm": [round(W, 2), round(H, 2)]}
    lim_wh, lim_src = enclosure_limit(B, cons, spec)
    out["limit"] = {"max_mm": lim_wh, "source": lim_src}
    if need <= A0:
        out["fits"] = True
        return out
    out["fits"] = False
    gain = need - A0
    out["gain_mm2"] = round(gain, 1)
    order = order or ["R", "B", "L", "T"]
    ax = {"L": H, "R": H, "T": W, "B": W}
    opts = []

    def mk(label, g):
        nW, nH = W + g[0] + g[1], H + g[2] + g[3]
        added = nW * nH - W * H
        pred = {s: round(u["sides"][s]["load_mm2"] / (u["sides"][s]["usable_mm2"] + added), 3) for s in SIDES}
        ok = lim_wh is None or (nW <= lim_wh[0] + 1e-6 and nH <= lim_wh[1] + 1e-6)
        opts.append({"key": grow_key(g), "label": label, "grow": list(g), "size_mm": [round(nW, 1), round(nH, 1)], "predicted_util": pred,
                     "within_limit": ok})
    seen = set()
    for e in order:
        if len(seen) >= 2:
            break
        d = round(gain / ax[e] + 0.05, 1)               # rounded up to 0.1 mm
        g = tuple(d if k == e else 0.0 for k in "LRTB")
        mk(f"grow {dict(L='left', R='right', T='top', B='bottom')[e]} +{d:g} mm", g)
        seen.add(e)
    # both axes: the two cheapest edges of different axes, equal growth d with (W + d)(H + d) - WH = gain
    d = round((-(W + H) + math.sqrt((W + H) ** 2 + 4 * gain)) / 2 + 0.05, 1)
    ex = next(e for e in order if e in "LR")
    ey = next(e for e in order if e in "TB")
    g = tuple(d if k in (ex, ey) else 0.0 for k in "LRTB")
    mk(f"grow {dict(L='left', R='right')[ex]} +{d:g} and {dict(T='top', B='bottom')[ey]} +{d:g} mm", g)
    out["options"] = opts
    return out


def crowding_report(B, cons, spec=None, overflow=None, order=None, with_size=True):
    """-> {usage, shortfall, verdicts, line, suggestion}"""
    u = usage(B, cons, spec)
    sf = shortfall(B, cons, spec, u, overflow)
    vd = verdicts(u, sf)
    r = {"usage": u, "shortfall": sf, "verdicts": vd, "line": line(u, sf, vd)}
    if with_size:
        r["suggestion"] = suggest(B, cons, spec, u, sf, order)
    return r


def size_lines(sug, vd=None):
    if sug.get("fits"):
        return [f"size: fits - needed outline {sug['needed_outline_mm2']:.0f} mm2 <= current {sug['outline_mm2']:.0f} mm2 ({sug['limit']['source'] if not sug['limit']['max_mm'] else 'limit ' + str(sug['limit']['max_mm'])})"]
    out = [f"size: needs {sug['needed_outline_mm2']:.0f} mm2 outline (now {sug['outline_mm2']:.0f}, +{sug['gain_mm2']:.0f}); {sug['limit']['source'] if not sug['limit']['max_mm'] else 'enclosure limit ' + str(sug['limit']['max_mm']) + ' (' + sug['limit']['source'] + ')'}"]
    for o in sug["options"]:
        out.append(f"  {o['label']} -> {o['size_mm'][0]:g} x {o['size_mm'][1]:g} mm (usable F {o['predicted_util']['F'] * 100:.0f} %, B {o['predicted_util']['B'] * 100:.0f} %)"
                   + ("" if o["within_limit"] else " EXCEEDS the enclosure limit"))
    return out


def gaps(B, cons, spec=None, rep=None):
    """gap dicts (placegaps shape {id, kind, class, mag, refs, nets, at, row, detail}) of kinds `density-hot` (one per hot cell that holds a
    free part) and `side-load` (one per side whose crowding verdict is not ok), for the dispatcher to register"""
    rep = rep or crowding_report(B, cons, spec, with_size=False)
    u, vd = rep["usage"], rep["verdicts"]
    out = []
    for s in SIDES:
        for c in u["sides"][s]["hot"]:
            out.append({"id": f"density-hot:{s}:{c['x']:g},{c['y']:g}", "kind": "density-hot", "class": "placement", "mag": round(c["cover"], 3),
                        "refs": [], "nets": [], "at": [c["x"] + CELL / 2, c["y"] + CELL / 2], "row": None,
                        "detail": f"{s} cell cover {c['cover'] * 100:.0f} % >= {u['limits']['cell_fail'] * 100:.0f} %"})
        if vd[s]["verdict"] != "ok":
            out.append({"id": f"side-load:{s}", "kind": "side-load", "class": "placement", "mag": round(vd[s]["util"], 3), "refs": [], "nets": [],
                        "at": None, "row": None, "detail": f"{vd[s]['verdict']}: " + "; ".join(vd[s]["reasons"])})
    return out


# ---- the `crowding` question: ranked ways forward, each computed from this board ------------------------------------
# Cost rank (lower = cheaper for the owner; the owner prefers fewer parts, then a bigger board, before more layers):
#   a back-fill with relaxed limits 1 (no hardware change)   c fewer parts 2   b smaller packages 3 (parts gate)
#   d grow the board 4                                        e more layers 6 (routing relief only)
COST = {"a": 1, "c": 2, "b": 3, "d": 4, "e": 6}
_PKG_NEXT = {"0603": "0402", "0402": "0201"}
_FP = re.compile(r"^([RC])_(\d{4})_")
_VAL = re.compile(r"^\s*([\d.]+)\s*(p|n|u|µ|m)?F?\s*$", re.I)


def _farads(v):
    m = _VAL.match(str(v or ""))
    if not m:
        return None
    return float(m.group(1)) * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, None: 1.0}[(m.group(2) or "").lower() or None]


def _area(p):
    c = p["court"]
    return max((c[2] - c[0]) * (c[3] - c[1]), 0.0)


def options(B, cons, spec, rep):
    """ranked options (list of dicts) for a crowded board: key, tag, label, text, cost, relief, predicted {side: {util, verdict}},
    fixes {measure: yes|helps|no|n/a}, parts [refs], apply.  Predictions are area arithmetic on this board's courtyards: a part moved
    or removed frees its courtyard on its side (and, if it stands in a hot cell / a cell with an unseated support, there); growth adds
    usable area to both sides; nothing is claimed about copper beyond that."""
    from . import backside as BS, modules as MD, netclass as NC, clusters as CL
    u, sf, vd = rep["usage"], rep["shortfall"], rep["verdicts"]
    lim = u["limits"]
    parts = B["parts"]
    fixed = MD.fixed_set(B, cons)
    hotk = {s: {(c["x"], c["y"]) for c in u["sides"][s]["hot"]} for s in SIDES}
    farc = {s: set(sf["far_by_cell"][s]) for s in SIDES}
    g = CELL * CELL
    excess_hot = sum((c["cover"] - lim["cell_fail"]) * g for s in SIDES for c in u["sides"][s]["hot"])
    far_need = sum(_area(parts[r]) for r in sf["far_refs"] if r in parts)
    meas = {"util": any(vd[s]["util"] >= lim["warn"] for s in SIDES), "hot": any(hotk[s] for s in SIDES),
            "far": bool(sf["far_refs"]), "overflow": bool(sf["overflow"])}
    sd = lambda r: parts[r]["side"][0]
    key = lambda r: _cell_of(u, parts[r]["pos"][0], parts[r]["pos"][1])
    base_u = {s: u["sides"][s]["util"] for s in SIDES}
    ctx = BS.Ctx(B, cons, spec)

    mside = {"hot": {s for s in SIDES if hotk[s]}, "far": {s for s in SIDES if farc[s]}, "overflow": {"F"}}

    def sv(pu, fx):
        """predicted verdict per side: utilisation bands, and ok only where the measures that failed ON that side are fixed"""
        pv = {}
        for s in SIDES:
            if pu[s] >= lim["fail"]:
                v = "fail"
            elif pu[s] >= lim["warn"]:
                v = "warn"
            else:
                v = "ok" if all(fx[m] in ("yes", "n/a") for m, ss in mside.items() if s in ss) else "warn"
            pv[s] = {"util": round(pu[s], 3), "verdict": v}
        return pv

    def build(tag, okey, label, text, freed=None, moved_to_back=None, grow_added=None, layers=False, parts_=()):
        freed, moved_to_back = freed or [], moved_to_back or []
        load = {s: u["sides"][s]["load_mm2"] for s in SIDES}
        for r in freed:
            load[sd(r)] -= _area(parts[r])
        for r in moved_to_back:
            load[sd(r)] -= _area(parts[r])
            load["B"] += _area(parts[r])
        us = {s: u["sides"][s]["usable_mm2"] + (grow_added or 0.0) for s in SIDES}
        pu = {s: load[s] / us[s] if us[s] else 0.0 for s in SIDES}
        mv = list(freed) + list(moved_to_back)
        in_hot = sum(_area(parts[r]) for r in mv if sd(r) == "F" and key(r) in hotk["F"])
        in_far = sum(_area(parts[r]) for r in mv if key(r) in farc[sd(r)])
        fx = {"util": ("yes" if all(pu[s] < lim["warn"] for s in SIDES) else "helps" if any(pu[s] < base_u[s] - 1e-9 for s in SIDES) else "no") if meas["util"] else "n/a",
              "hot": ("yes" if in_hot >= excess_hot else "helps" if (in_hot > 0 or grow_added) else "no") if meas["hot"] else "n/a",
              "far": ("yes" if in_far >= far_need else "helps" if (in_far > 0 or grow_added) else "no") if meas["far"] else "n/a",
              "overflow": ("yes" if (layers or (grow_added and all(pu[s] < lim["warn"] for s in SIDES))) else "helps" if (mv or grow_added) else "no") if meas["overflow"] else "n/a"}
        pv = sv(pu, fx)
        relief = sum(max(0.0, base_u[s] - lim["warn"]) - max(0.0, pu[s] - lim["warn"]) for s in SIDES) + 0.1 * sum(1 for v in fx.values() if v == "yes")
        return {"key": okey, "tag": tag, "label": label, "text": text, "cost": COST[tag], "relief": round(relief, 4), "predicted": pv, "fixes": fx,
                "parts": sorted(parts_ or mv), "all_ok": all(v["verdict"] == "ok" for v in pv.values()),
                "reaches_ok": all(v["util"] < lim["warn"] for v in pv.values()) and all(x != "no" for x in fx.values())}

    out = []
    # (a) back fill with relaxed limits
    if ctx.sides in ("both", "both-light-back") and ctx.max_h is not None:
        elig = [r for r in u["eligible"]["refs"]]
        blocked = {}
        for r, p in sorted(parts.items()):
            if p["side"] != "F.Cu" or r in fixed or p.get("locked") or _area(p) >= BS.SMALL_MM2 or r in elig:
                continue
            ok, why = BS.back_ok(B, cons, r, ctx=ctx)
            if not ok and "back_max_height_mm" in why:
                blocked[r] = ctx.height(r)[0]
        levels = sorted({round(math.ceil(h * 10) / 10, 1) for h in blocked.values()})
        first, reach = None, None
        for L in levels:
            un = [r for r, h in blocked.items() if h <= L + 1e-9]
            o = build("a", f"a-height-{L:g}", f"raise back_max_height_mm {ctx.max_h:g} -> {L:g} mm",
                      f"unlocks {len(un)} part(s) / {sum(_area(parts[r]) for r in un):.0f} mm2 for the back (with the {len(elig)} / {u['eligible']['mm2']:.0f} mm2 already eligible); "
                      f"the second reflow carries them (tallest {max(blocked[r] for r in un):g} mm)", moved_to_back=elig + un, parts_=un)
            first = first or o
            if o["all_ok"]:
                reach = o
                break
        for o in {id(x): x for x in (first, reach) if x}.values():
            out.append(o)
        if not blocked and elig:
            out.append(build("a", "a-fill", "move the eligible small parts to the back now (no limit changes)",
                             f"{len(elig)} part(s) / {u['eligible']['mm2']:.0f} mm2 already pass every back rule", moved_to_back=elig))
        heavy = []
        for r, p in sorted(parts.items()):
            if p["side"] == "F.Cu" and r not in fixed and not p.get("locked") and not p["tht"] and BS.stays_front(ctx, r) is None and 0 < _area(p) <= 60:
                ok, why = BS.back_ok(B, cons, r, ctx=ctx)
                if not ok and why.startswith("heavy"):
                    h = ctx.height(r)[0]
                    if h is not None and h <= (ctx.max_h or 99):
                        heavy.append(r)
        if heavy:
            hv = sorted(heavy, key=lambda r: (-_area(parts[r]), r))[:6]
            out.append(build("a", "a-heavy:" + "+".join(hv), f"let {len(hv)} named heavy-class part(s) go to the back ({', '.join(hv)})",
                             f"{sum(_area(parts[r]) for r in hv):.0f} mm2; each is within the {ctx.max_h:g} mm limit; sets [placement] back_allow", moved_to_back=hv))
    # (b) smaller packages for passives
    fam = {}
    for r, p in parts.items():
        m = _FP.match(str(p.get("fp", "")).split(":")[-1])
        if m and p["n_pads"] == 2:
            fam.setdefault((m.group(1), m.group(2)), []).append(_area(p))
    cls = NC.classify(B, cons, spec)
    bad = {"R": ("rf", "pair", "switch_node", "rail"), "C": ("rf", "pair", "switch_node")}
    sav, n_by = [], {}
    for r, p in sorted(parts.items()):
        m = _FP.match(str(p.get("fp", "")).split(":")[-1])
        if not m or r in fixed or p.get("locked") or p["n_pads"] != 2 or m.group(2) not in _PKG_NEXT:
            continue
        tgt = fam.get((m.group(1), _PKG_NEXT[m.group(2)]))
        if not tgt:
            continue                                     # no such package on this board: no land pattern to point at, nothing predicted
        if any((cls.get(q.get("net")) or {}).get("class") in bad[m.group(1)] for q in p["pads"]):
            continue
        if m.group(1) == "C":
            f = _farads(p.get("value"))
            if f is None or f > (1e-6 if m.group(2) == "0603" else 1e-7):
                continue
        elif m.group(2) == "0402":
            continue                                     # resistors: 0603 -> 0402 only (0201 power / tolerance not known from the data)
        if m.group(2) == "0603" or m.group(1) == "C":
            gain = _area(p) - sum(tgt) / len(tgt)
            if gain > 0.05:
                sav.append((r, gain))
                n_by[m.group(2)] = n_by.get(m.group(2), 0) + 1
    if sav:
        o = build("b", "b-smaller", "smaller packages for passives (parts change: needs the parts gate)",
                  f"{len(sav)} part(s) ({', '.join(f'{k}->{_PKG_NEXT[k]} x{v}' for k, v in sorted(n_by.items()))}), {sum(g_ for _, g_ in sav):.0f} mm2 saved; "
                  f"value and net class allow it, voltage / power rating not in the data: check on the parts gate", parts_=[r for r, _ in sav])
        # courtyard shrink, not removal: freed area = the gain per part
        load = {s: u["sides"][s]["load_mm2"] for s in SIDES}
        for r, gn in sav:
            load[sd(r)] -= gn
        pu = {s: load[s] / u["sides"][s]["usable_mm2"] for s in SIDES}
        hot_gain = sum(gn for r, gn in sav if sd(r) == "F" and key(r) in hotk["F"])
        far_gain = sum(gn for r, gn in sav if key(r) in farc[sd(r)])
        o["fixes"] = {"util": ("yes" if all(pu[s] < lim["warn"] for s in SIDES) else "helps") if meas["util"] else "n/a",
                      "hot": ("yes" if hot_gain >= excess_hot else "helps" if hot_gain > 0 else "no") if meas["hot"] else "n/a",
                      "far": ("yes" if far_gain >= far_need else "helps" if far_gain > 0 else "no") if meas["far"] else "n/a",
                      "overflow": "no" if meas["overflow"] else "n/a"}
        o["predicted"] = sv(pu, o["fixes"])
        o["relief"] = round(sum(max(0.0, base_u[s] - lim["warn"]) - max(0.0, pu[s] - lim["warn"]) for s in SIDES) + 0.1 * sum(v == "yes" for v in o["fixes"].values()), 4)
        o["all_ok"] = all(v["verdict"] == "ok" for v in o["predicted"].values()) and all(v in ("yes", "n/a") for v in o["fixes"].values())
        o["reaches_ok"] = all(v["util"] < lim["warn"] for v in o["predicted"].values()) and all(x != "no" for x in o["fixes"].values())
        out.append(o)
    # (c) fewer parts: resistor arrays for runs of one value on one IC, decaps above one per supply pin
    try:
        cl, _ = CL.build(B, cons, spec)
    except Exception:                                    # noqa: BLE001
        cl = []
    arr = [_area(p) for p in parts.values() if str(p.get("fp", "")).split(":")[-1].startswith("R_Array")]
    gone, merged, saved_arr = [], 0, 0.0
    for c in cl:
        icp = parts[c["ic"]]
        groups = {}
        for s_ in c["supports"]:
            r = s_["ref"]
            p = parts.get(r)
            if not p or r in fixed or p.get("locked") or not s_["sure"] or p["n_pads"] != 2:
                continue
            m = _FP.match(str(p.get("fp", "")).split(":")[-1])
            if m and m.group(1) == "R" and arr:
                groups.setdefault(("R", p.get("value"), m.group(2)), []).append(r)
        for k, rs in groups.items():
            n4 = len(rs) // 4
            if n4:
                use = sorted(rs)[:4 * n4]
                gone += use
                merged += n4
                saved_arr += sum(_area(parts[r]) for r in use) - n4 * (sum(arr) / len(arr))
        caps = {}
        for s_ in c["supports"]:
            r = s_["ref"]
            p = parts.get(r)
            if p and r not in fixed and s_["sure"] and p["n_pads"] == 2 and _FP.match(str(p.get("fp", "")).split(":")[-1] or "") and str(p.get("ref", r)).startswith("C"):
                caps.setdefault((s_.get("net"), p.get("value")), []).append(r)
        for (net, val), rs in sorted(caps.items(), key=lambda t: (str(t[0][0]), str(t[0][1]))):
            npin = len([q for q in icp["pads"] if q.get("net") == net]) if net else 0
            if net and npin and len(rs) > npin and cls.get(net, {}).get("class") == "rail":
                gone += sorted(rs)[npin:]
    gone = sorted(set(gone) - {r for r in gone if r in sf["far_refs"] and False})
    if gone:
        n_gone = len(gone)
        o = build("c", "c-fewer", "fewer parts (suggest only: schematic change)",
                  f"{n_gone} part(s) / {sum(_area(parts[r]) for r in gone):.0f} mm2: {merged} resistor array(s) for runs of >= 4 same-value parts on one IC "
                  f"(array saves {saved_arr:.0f} mm2 net), plus decaps above one per supply pin of their IC", freed=gone)
        out.append(o)
    # (d) grow the board
    sug = rep.get("suggestion") or suggest(B, cons, spec, u, sf)
    for ob in sug.get("options") or []:
        added = ob["size_mm"][0] * ob["size_mm"][1] - sug["size_mm"][0] * sug["size_mm"][1]
        o = build("d", ob["key"], ob["label"], f"-> {ob['size_mm'][0]:g} x {ob['size_mm'][1]:g} mm; fixed edge parts ride with their edge"
                  + ("" if ob["within_limit"] else "; EXCEEDS the enclosure limit"), grow_added=added)
        o["grow"] = ob["grow"]
        o["within_limit"] = ob["within_limit"]
        out.append(o)
    # (e) more layers: routing relief only
    nl = len(B["copper"])
    if meas["overflow"] or meas["hot"] or meas["far"]:
        out.append(build("e", "e-layers", f"more layers ({nl} -> {nl + 2})",
                         "routing relief only, no area relief; the owner prefers growing the board before adding layers", layers=True))
    ok = [o for o in out if o["reaches_ok"] and o.get("within_limit", True)]       # a growth that fixes the utilisation and no measure it leaves untouched
    for o in out:
        o["rank_key"] = (o["tag"] == "e", -(o["relief"] / o["cost"]), o["cost"], o["key"])       # more layers is always last (the owner grows the board first)
    out.sort(key=lambda o: o["rank_key"])
    for o in out:
        o.pop("rank_key")
    default = min(ok, key=lambda o: (o["cost"], o["key"]))["key"] if ok else "keep"
    return out, default


def option_line(i, o):
    pv = o["predicted"]
    fx = ", ".join(f"{k} {v}" for k, v in o["fixes"].items() if v != "n/a")
    return (f"  {i}. {o['key']}: {o['label']}: {o['text']}; predicted F {pv['F']['util'] * 100:.0f} % {pv['F']['verdict']}, "
            f"B {pv['B']['util'] * 100:.0f} % {pv['B']['verdict']}; fixes: {fx or 'nothing measured'}")


def jsonable(o):
    """report data for json.dump: tuple keys become 'x,y', sets become sorted lists"""
    if isinstance(o, dict):
        return {(",".join(f"{v:g}" for v in k) if isinstance(k, tuple) else k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, set):
        return sorted(o)
    if isinstance(o, float) and math.isinf(o):
        return None
    return o
