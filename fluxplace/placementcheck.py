"""`placement-check BOARD --constraints C [--json]` - every placement keep-out rule against the board, read-only.

Rules checked (all from the constraints): `[placement.keepout.*]` and `[[keepout]]` (part body of the rule's side in the
rect), `[placement.no_tht.*]` (through-hole pad in the rect), `[placement.hardware]` (courtyard or pad inside a mounting
hole's keep-out CIRCLE) and `[placement.module.*]` (expanded to a keep-out + no_tht, see placerules.py). One row per rule
with the offending parts (ref, face, overlap mm2 or distance vs required, fixed or movable), then the courtyard coverage
of each board quadrant (courtyard area / quadrant area, per face and both). Exit 1 on any violation.
"""
import fnmatch
import json
import os

from . import geom as GM
from . import legalise as LG
from . import placerules as PR

SIDES = ("F", "B")


def _face(p):
    return "F" if p["side"] == "F.Cu" else "B"


def _box(q):
    return (q["x"] - q["ex"], q["y"] - q["ey"], q["x"] + q["ex"], q["y"] + q["ey"])


def _area(a, b):
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _allowed(ref, allow):
    return any(fnmatch.fnmatch(ref, g) for g in allow)


def quadrants(B):
    x0, y0, x1, y1 = B["bbox"]
    xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
    quads = {"NW": (x0, y0, xm, ym), "NE": (xm, y0, x1, ym), "SW": (x0, ym, xm, y1), "SE": (xm, ym, x1, y1)}
    out = {}
    for n, q in quads.items():
        qa = (q[2] - q[0]) * (q[3] - q[1])
        by = {s: 0.0 for s in SIDES}
        for p in B["parts"].values():
            if not p["pads"]:
                continue
            by[_face(p)] += _area(p["court"], q)
            if p["tht"]:
                by["B" if _face(p) == "F" else "F"] += _area(p["court"], q)
        out[n] = {"area_mm2": round(qa, 1), "F_pct": round(100 * by["F"] / qa, 2), "B_pct": round(100 * by["B"] / qa, 2),
                  "total_pct": round(100 * (by["F"] + by["B"]) / qa, 2)}
    return out


def compute(B, cons):
    PR.expand(cons, B)
    M = LG._Model(B, cons, (), B["bbox"])
    fixed = set(M.fixed) | set(((cons or {}).get("placement") or {}).get("fixed", []) or [])
    rules = []

    def rule(kind, name, geom, rows, reason=""):
        rules.append({"kind": kind, "name": name, "geom": geom, "reason": reason, "violations": rows})

    # keep-outs (hardware squares are reported under hardware, exactly)
    for rect, sides, allow, mh, name in M.ko:
        if name.startswith("hardware:"):
            continue
        rows = []
        for ref, p in sorted(B["parts"].items()):
            if _allowed(ref, allow):
                continue
            if (M.body_side(ref) & sides) and (p["tht"] or mh is None) and GM.overlap(p["court"], rect, -0.001):
                rows.append({"ref": ref, "face": _face(p), "overlap_mm2": round(_area(p["court"], rect), 3),
                             "fixed": ref in fixed})
        rule("keepout", name, {"rect": list(rect), "sides": sorted(sides)}, rows)
    pl = (cons or {}).get("placement") or {}
    for name, k in sorted((pl.get("keepout") or {}).items()):
        for r in rules:
            if r["name"] == name and isinstance(k, dict):
                r["reason"] = k.get("reason", "")
    for name, rect, allow in __import__("fluxplace.legality", fromlist=["x"]).no_tht_regions(cons, B):
        rows = []
        for ref, p in sorted(B["parts"].items()):
            if _allowed(ref, allow):
                continue
            for q in p["pads"]:
                if q["tht"] and GM.overlap(_box(q), rect, -0.001):
                    rows.append({"ref": ref, "face": _face(p), "pad": q["num"] or "-", "at": [round(q["x"], 2), round(q["y"], 2)],
                                 "fixed": ref in fixed})
        rule("no_tht", name, {"rect": list(rect)}, rows, ((pl.get("no_tht") or {}).get(name) or {}).get("reason", ""))
    h = PR.hardware_cfg(cons)
    if h is not None:
        gallow = h.get("allow") or []
        for ref, c, d in PR.hardware(B, cons):
            rows = []
            for r2, p in sorted(B["parts"].items()):
                if r2 == ref or _allowed(r2, gallow):
                    continue
                dist = PR.circle_hit((c, d / 2), p["court"], [_box(q) for q in p["pads"]])
                if dist is not None:
                    rows.append({"ref": r2, "face": _face(p), "distance_mm": round(dist, 3), "required_mm": round(d / 2, 3),
                                 "fixed": r2 in fixed})
            rule("hardware", ref, {"centre": list(c), "diameter_mm": d}, rows)
    quads = quadrants(B)
    n = sum(len(r["violations"]) for r in rules)
    return {"ok": n == 0, "violations": n, "rules": rules, "quadrants": quads}


def lines(res):
    out = []
    for r in res["rules"]:
        v = r["violations"]
        g = r["geom"]
        where = (f"rect {[round(x, 2) for x in g['rect']]}" + (f" side {'/'.join(g['sides'])}" if "sides" in g else "")) if "rect" in g \
            else f"circle ({g['centre'][0]:.2f}, {g['centre'][1]:.2f}) d {g['diameter_mm']:g}"
        out.append(f"{r['kind']:8s} {r['name']:28s} {'FAIL ' + str(len(v)) if v else 'ok':8s} {where}")
        for x in v:
            if "overlap_mm2" in x:
                d = f"overlap {x['overlap_mm2']:.2f} mm2"
            elif "distance_mm" in x:
                d = f"{x['distance_mm']:.2f} mm from the hole centre, needs {x['required_mm']:.2f}"
            else:
                d = f"pad {x['pad']} at {tuple(x['at'])}"
            out.append(f"    {x['ref']:8s} {x['face']}  {d}{'  [FIXED]' if x['fixed'] else ''}")
    out.append("quadrant courtyard coverage (courtyard / quadrant area):")
    for n, q in res["quadrants"].items():
        out.append(f"  {n}  F {q['F_pct']:5.1f}%  B {q['B_pct']:5.1f}%  total {q['total_pct']:5.1f}%  ({q['area_mm2']:.0f} mm2)")
    return out


def cmd_placement_check(a):
    from . import plancli as PC, routediag as RD
    cons, _spec = PC._ctx(a)
    B = RD.load(a.board)
    res = compute(B, cons)
    out = os.path.splitext(a.board)[0] + ".placement-check.json"
    with open(out, "w") as fh:
        json.dump(res, fh, indent=1)
    if getattr(a, "json", False):
        print(json.dumps(res, indent=1))
    else:
        print(f"placement-check: {'OK' if res['ok'] else 'FAIL'} - {res['violations']} violation(s) over "
              f"{len(res['rules'])} rule(s)")
        for ln in lines(res):
            if not getattr(a, "quiet", False) or ln.startswith(("keepout", "no_tht", "hardware")) and " ok " not in ln:
                print(ln)
        print(f"full report: {out}")
    if not res["ok"]:
        raise SystemExit(1)


def add_parser(sub):
    p = sub.add_parser("placement-check", help="every keep-out, no_tht, hardware and module rule against the board, "
                       "offending parts, quadrant coverage; exit 1 on a violation")
    p.add_argument("board")
    p.add_argument("--constraints", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.add_argument("-q", "--quiet", dest="quiet", action="store_true")
    p.set_defaults(fn=cmd_placement_check)
