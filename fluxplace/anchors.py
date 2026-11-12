"""Anchor orientation: connectors, modules and sockets decide most of a board's
wirelength, and nothing in a force-directed placer ever re-evaluates their
rotation. For every anchor (or declared rotating group) try each allowed
rotation with everything else where it is and score it.

Anchors: refs listed in `[placement] anchors`, plus auto-detected connectors
(J/P/CN/X refs with >= 6 pads) and modules (U refs with >= 40 pads). Mounting
holes, fiducials and test points are never anchors.

    [placement]
    anchors = ["J200"]                      # explicit (adds to auto-detection)
    [placement.anchor.J200]
    rotations = [0, 90, 180, 270]           # DELTAS from the current orientation (default all four;
                                            # an unconfigured part within 1.5 mm of the outline is an
                                            # edge connector and only keeps its orientation)
    [placement.group.cm5]                   # parts that rotate together
    refs = ["J10", "J11"]
    centre = [30, 25]                       # rotate about this point (default: centroid)
    rotations = [0, 180]

A rotation is a true rigid rotation of the footprint (pads move with it, see
geom.moved). Score = sum over the nets the anchor touches of class weight x MST
length with the anchor's pads moved and every other pad fixed; "span" = the
longest pair-class net (any net if there is none). An option is infeasible when
the courtyard leaves the board outline or lands on another anchor. Side flips
are not evaluated (the text writer cannot mirror a footprint).
ANCHOR_MISORIENTED when the current orientation scores more than
`[preroute] anchor_tolerance_pct` (15) above the best feasible option.
"""
import math
import re
from collections import defaultdict

from . import geom as GM
from . import netclass as NC

_CONN = re.compile(r"^(J|P|CN|X)\d")
_MECH = re.compile(r"^(MH|MK|H|TP|FID)\d")
ROTATIONS = (0.0, 90.0, 180.0, 270.0)


def detect(B, cons=None):
    """[{name, refs, centre|None, rotations}] — groups first, then single anchors."""
    pl = (cons or {}).get("placement", {}) or {}
    parts = B["parts"]
    out, used = [], set()
    for name, g in sorted((pl.get("group") or {}).items()):
        refs = [r for r in g.get("refs", []) if r in parts]
        if refs:
            out.append({"name": name, "refs": refs, "centre": tuple(g["centre"]) if g.get("centre") else None,
                        "rotations": [float(x) for x in g.get("rotations", ROTATIONS)]})
            used |= set(refs)
    per = pl.get("anchor") or {}
    names = set(pl.get("anchors", []))
    for ref, part in sorted(parts.items()):
        if ref in used or _MECH.match(ref) or part["n_pads"] < 2:
            continue
        auto = (_CONN.match(ref) and part["n_pads"] >= 6) or (ref.startswith("U") and part["n_pads"] >= 40)
        if ref in names or ref in per or auto:
            rots = (per.get(ref) or {}).get("rotations")
            if rots is None:
                # a part hugging the outline is an edge connector: its mating face is
                # set by the enclosure, so it only rotates when the config says so
                bx0, by0, bx1, by1 = B["bbox"]
                c = part["court"]
                edge = min(c[0] - bx0, c[1] - by0, bx1 - c[2], by1 - c[3]) < 1.5
                rots = [0.0] if edge and ref not in per else ROTATIONS
            out.append({"name": ref, "refs": [ref], "centre": None,
                        "rotations": [float(x) for x in rots]})
    return out


def _centre(B, a):
    if a["centre"]:
        return a["centre"]
    if len(a["refs"]) == 1:
        return B["parts"][a["refs"][0]]["pos"]
    xs = [B["parts"][r]["pos"][0] for r in a["refs"]]
    ys = [B["parts"][r]["pos"][1] for r in a["refs"]]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def rotated(B, a, delta):
    """{ref: (x, y, angle, pads, court)} after rotating the anchor by `delta` degrees."""
    cx, cy = _centre(B, a)
    out = {}
    for r in a["refs"]:
        p = B["parts"][r]
        rx, ry = GM.rot(p["pos"][0] - cx, p["pos"][1] - cy, delta)
        x, y, ang = (cx + rx, cy + ry, p["angle"] + delta) if len(a["refs"]) > 1 else (
            p["pos"][0], p["pos"][1], p["angle"] + delta)
        pads, court = GM.moved(p, x, y, ang)
        out[r] = (x, y, ang, pads, court)
    return out


def evaluate(B, cons=None, spec=None, cls=None):
    """One result per anchor: {name, refs, options: [{delta, cost, span, feasible, why}],
    current, best, worse_pct}."""
    cls = cls or NC.classify(B, cons, spec)
    pads = NC.net_pads(B)
    anchors = detect(B, cons)
    anchor_refs = {r for a in anchors for r in a["refs"]}
    pair_nets = {n for n, i in cls.items() if i["class"] == "pair"}
    x0, y0, x1, y1 = B["bbox"]
    out = []
    for a in anchors:
        mine = set(a["refs"])
        touched = sorted({p["net"] for r in a["refs"] for p in B["parts"][r]["pads"]
                          if p["net"] and cls.get(p["net"], {}).get("weight", 0) > 0 or
                          p["net"] and p["net"] in pair_nets})
        touched = [n for n in touched if n in cls]
        opts = []
        for d in sorted(set(a["rotations"])):
            mv = rotated(B, a, d)
            cost, span = 0.0, 0.0
            for n in touched:
                pts = [NC.pad_xy(p) for p in pads[n] if p["ref"] not in mine]
                pts += [NC.pad_xy(q) for r in mine for q in mv[r][3] if q["net"] == n]
                ln = NC.mst_length(pts)
                cost += cls[n]["weight"] * ln
                if n in pair_nets:
                    span = max(span, ln)
            why = []
            for r, (_, _, _, _, court) in mv.items():
                if not GM.inside(court, (x0, y0, x1, y1)):
                    why.append(f"{r} leaves the outline")
                for o in anchor_refs - mine:
                    op = B["parts"][o]
                    if op["side"] == B["parts"][r]["side"] and GM.overlap(court, op["court"], -0.05):
                        why.append(f"{r} lands on {o}")
            opts.append({"delta": d, "cost": cost, "span": span, "feasible": not why,
                         "why": "; ".join(why)})
        cur = next((o for o in opts if o["delta"] % 360 == 0), None)
        if cur is None:                       # current orientation not among the options: score it anyway
            cur = evaluate_one(B, a, 0.0, touched, pads, cls, pair_nets)
            opts.insert(0, cur)
        feas = [o for o in opts if o["feasible"]] or [cur]
        best = min(feas, key=lambda o: o["cost"])
        worse = 100.0 * (cur["cost"] - best["cost"]) / best["cost"] if best["cost"] > 0 else 0.0
        out.append({"name": a["name"], "refs": a["refs"], "centre": _centre(B, a), "options": opts,
                    "current": cur, "best": best, "worse_pct": worse})
    return out


def evaluate_one(B, a, d, touched, pads, cls, pair_nets):
    mv = rotated(B, a, d)
    mine = set(a["refs"])
    cost = span = 0.0
    for n in touched:
        pts = [NC.pad_xy(p) for p in pads[n] if p["ref"] not in mine]
        pts += [NC.pad_xy(q) for r in mine for q in mv[r][3] if q["net"] == n]
        ln = NC.mst_length(pts)
        cost += cls[n]["weight"] * ln
        if n in pair_nets:
            span = max(span, ln)
    return {"delta": d, "cost": cost, "span": span, "feasible": True, "why": ""}


def findings(B, cons=None, spec=None, cls=None):
    from .preroute import _f, FAIL
    tol = float(((cons or {}).get("preroute") or {}).get("anchor_tolerance_pct", 15.0))
    out = []
    for r in evaluate(B, cons, spec, cls):
        if r["worse_pct"] > tol and r["best"]["delta"] % 360 != 0:
            b, c = r["best"], r["current"]
            out.append(_f(FAIL, "ANCHOR_MISORIENTED",
                          f"{r['name']} ({', '.join(r['refs'])}): current orientation scores "
                          f"{c['cost']:.0f}, rotating {b['delta']:g} deg scores {b['cost']:.0f} "
                          f"({r['worse_pct']:.0f}% worse, limit {tol:g}%)"
                          + (f"; worst pair span {c['span']:.0f} -> {b['span']:.0f} mm" if c["span"] else ""),
                          refs=r["refs"], value=round(r["worse_pct"], 1), limit=tol))
    return out


def moves_for_best(B, results=None, cons=None, spec=None, tol=15.0, limit=20):
    """{ref: (x, y, angle)}: rotate misoriented anchors one at a time, the most
    improved first, re-scoring after each so two anchors that could each fix the
    same crossing are not both turned. MUTATES the board model B."""
    out = {}
    for _ in range(limit):
        res = results if results is not None else evaluate(B, cons, spec)
        results = None
        bad = [r for r in res if r["worse_pct"] > tol and r["best"]["delta"] % 360 != 0]
        if not bad:
            break
        r = max(bad, key=lambda r: r["current"]["cost"] - r["best"]["cost"])
        a = next(a for a in detect(B, cons) if a["name"] == r["name"])
        for ref, (x, y, ang, _p, _c) in rotated(B, a, r["best"]["delta"]).items():
            GM.place(B["parts"][ref], x, y, ang)
            out[ref] = (round(x, 4), round(y, 4), ang)
    return out


def format_text(results, tol=15.0):
    L = []
    for r in results:
        flag = "  ANCHOR_MISORIENTED" if (r["worse_pct"] > tol and r["best"]["delta"] % 360 != 0) else ""
        L.append(f"{r['name']}  ({', '.join(r['refs'])}) about ({r['centre'][0]:.1f}, {r['centre'][1]:.1f})"
                 f"  current {r['current']['cost']:.0f}, best {r['best']['cost']:.0f} "
                 f"(rot {r['best']['delta']:g}), {r['worse_pct']:.0f}% worse{flag}")
        L.append("    rot   weighted length   worst pair span   feasible")
        for o in r["options"]:
            L.append(f"   {o['delta']:4g}   {o['cost']:14.0f}   "
                     f"{('%.1f mm' % o['span']) if o['span'] else '      -':>14s}   "
                     f"{'yes' if o['feasible'] else 'NO: ' + o['why']}"
                     f"{'   <- current' if o is r['current'] else ''}{'   <- best' if o is r['best'] else ''}")
    return "\n".join(L)
