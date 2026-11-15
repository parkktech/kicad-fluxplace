"""One set of controlled-impedance trace geometries that holds on SEVERAL fabs' stackups.

`common(names, ...)` takes stackup template names (data/stackup_templates.json; one per fab, each tagged with the standard
build it matches), and for every controlled case (50 ohm single-ended, 90 / 100 ohm pairs) on every requested layer finds the
width (at a stated gap) whose impedance, computed by impedance.py's own model on EACH stackup, stays inside the tolerance on
all of them. A case with no such width says so, with the per-fab geometry and the error the common one would give.
"""
from . import impedance as IM, stackup as ST

CASES = (("50 ohm single-ended", 50.0, False), ("90 ohm pair (USB)", 90.0, True), ("100 ohm pair (Ethernet)", 100.0, True))
GAPS = (0.15, 0.2, 0.25)          # pair gaps tried, smallest first, never below the routing floor


def _geom(name, layer):
    t = ST.PROFILES[name]
    rows = [{"name": l.get("name"), "type": l["type"], "thickness": l["thickness"], "epsilon_r": l.get("epsilon_r", 4.4)}
            for l in t["layers"]]
    return IM.geometry(rows, t.get("reference_layers") or [], layer)


def _z(g, w, gap):
    return IM.z_diff(w, gap, g) if gap else IM.z_single(w, g)


def common(names, layers=("F.Cu", "In2.Cu"), tol_pct=10.0, margin_pct=5.0, gaps=GAPS, step=0.005, floor=0.127):
    """-> [{case, layer, gap, w, per_fab: {name: (z, err_pct)}, within_tol, within_margin, alone: {name: w}}]; one row per
    case/layer (pairs: the smallest gap in `gaps` >= `floor` whose common width is also >= `floor` and holds `margin_pct` on
    every stackup, else the best gap). Nothing is ever proposed below `floor` (the owner's routing floor)."""
    out = []
    for layer in layers:
        gs = {n: _geom(n, layer) for n in names}
        if any(g is None or g["model"] is None for g in gs.values()):
            continue
        for label, target, diff in CASES:
            best = None
            for gap in ([g_ for g_ in gaps if g_ >= floor - 1e-9] if diff else (None,)):
                alone = {n: (IM.solve_diff(target, g, gap) if diff else IM.solve_single(target, g)) for n, g in gs.items()}
                if any(w is None for w in alone.values()):
                    continue
                lo, hi = max(floor, min(alone.values()) * 0.85), max(floor, max(alone.values()) * 1.15)
                cand = None
                k = int(-(-lo // step))                        # first grid width >= lo (never below the floor)
                while k * step <= hi + 1e-9:
                    w = round(k * step, 6)
                    errs = {n: 100.0 * (_z(g, w, gap) - target) / target for n, g in gs.items()}
                    worst = max(abs(e) for e in errs.values())
                    if cand is None or worst < cand[0] - 1e-9:
                        cand = (worst, w, errs)
                    k += 1
                if cand is None:
                    continue
                worst, wr, _ = cand
                errs = {n: 100.0 * (_z(g, wr, gap) - target) / target for n, g in gs.items()}
                worst = max(abs(e) for e in errs.values())
                row = {"case": label, "target": target, "layer": layer, "gap": gap, "w": round(wr, 3),
                       "per_fab": {n: (round(_z(gs[n], wr, gap), 1), round(errs[n], 1)) for n in gs},
                       "worst_err_pct": round(worst, 1), "within_tol": worst <= tol_pct, "within_margin": worst <= margin_pct,
                       "alone": {n: round(w_, 3) for n, w_ in alone.items()}, "model": gs[names[0]]["model"]}
                if best is None or (row["within_margin"] and not best["within_margin"]):
                    best = row
                if row["within_margin"]:
                    best = row
                    break
                if not best["within_margin"] and worst < best["worst_err_pct"]:
                    best = row
            if best:
                out.append(best)
    return out


def format_text(rows, names):
    L = ["common controlled-impedance geometry on: " + ", ".join(names)]
    for r in rows:
        g = f" gap {r['gap']:g}" if r["gap"] else ""
        per = "; ".join(f"{n}: {z:g} ohm ({e:+.1f}%)" for n, (z, e) in r["per_fab"].items())
        v = "OK" if r["within_tol"] else "NO COMMON GEOMETRY"
        L.append(f"  {r['layer']:7s} {r['case']:24s} w {r['w']:g}{g}: {per} -> {v}"
                 + ("" if r["within_tol"] else "; per-fab widths " + ", ".join(f"{n} {w:g}" for n, w in r["alone"].items())))
    return "\n".join(L)
