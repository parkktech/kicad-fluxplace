"""Controlled-impedance copper geometry from the board's ACTUAL stackup, one source for everyone.

`pair_geometry(...)` (re-exported as `pairs.geometry`) -> width / gap of a differential pair family on one layer;
`single_width(...)` -> width of a single-ended controlled net (RF) on one layer. The pairs stage, the pair fallback
ladder, review (PAIR_IMPEDANCE*, RF_*), the netclasses written into the project and the values passed to external routers
all call these, so every stage lays and checks the same copper. The hard-coded constants (constraints.PAIR_GEOM_BY_Z,
solved for a different 4-layer stackup) are only the last-resort fallback when no stackup/reference plane is known.

Resolution per family and layer (first that applies):
  1. explicit `[pairs.FAMILY] width_mm` / `gap_mm` (both: kept as written, impedance error reported; one: the other is solved)
  2. the geometry at the PREFERRED floor (`[route] min_track_mm / min_clearance_mm`, never below the fab limit): width and gap
     both >= the floor, the other solved for the target
  3. the FAB-LIMIT geometry (tightest coupling the manufacturer row allows), only when `[pairs] allow_fab_limit = true`
     (or `[pairs.FAMILY] allow_fab_limit = true`) AND the caller says the preferred one cannot be placed (`tight=True`)
  4. unreachable: the best achievable within the legal range, status "unreachable" (review: PAIR_IMPEDANCE_UNREACHABLE FAIL)
Result: {w, gap, z, err_pct, status: ok|off|unreachable|no_reference|class, source, target, tol_pct, model, layer}.
"""
import math

from . import impedance as IM


def resolve_stack(cons, board=None, facts=None, B=None):
    """-> (stackup rows, plane layers, source). Constraints [stackup] first, then the board file / facts."""
    cons = cons or {}
    sc = cons.get("stackup") or {}
    rows, src = None, None
    if sc.get("layers"):
        rows = [{"name": l.get("name"), "type": l.get("type", "copper" if str(l.get("name", "")).endswith(".Cu") else "core"),
                 "thickness": float(l.get("thickness", 0) or 0), "epsilon_r": float(l.get("epsilon_r", 4.4) or 4.4)} for l in sc["layers"]]
        src = "constraints [stackup]"
    elif sc.get("profile"):
        from . import stackup as ST
        if sc["profile"] in ST.PROFILES:
            rows = [{"name": l.get("name"), "type": l["type"], "thickness": l["thickness"], "epsilon_r": l.get("epsilon_r", 4.4)}
                    for l in ST.PROFILES[sc["profile"]]["layers"]]
            src = f"profile {sc['profile']}"
    if rows is None and facts and facts.get("stackup"):
        rows, src = list(facts["stackup"]), "board file"
    if rows is None and board:
        try:
            from . import review as RV
            rows = RV.parse_stackup_text(open(board, errors="replace").read()) or None
            src = "board file"
        except (OSError, ValueError):
            rows = None
    planes = sc.get("planes")
    if planes is None:
        planes = (cons.get("preroute") or {}).get("plane_layers")
    if planes is None and B is not None:
        from . import pairs as PA
        planes = PA.pair_layers(B, cons)[1]
    if planes is None:
        planes = (facts or {}).get("plane_layers") or []
    return rows or [], list(planes), src or "none"


def outer_geometry(cons, family, board=None, B=None, facts=None):
    """(w, gap) of `family` on the board's first copper layer (what a single-geometry consumer - a net class, route_diff.py,
    a Freerouting class - should be given), or None when no stackup/plane/target makes it computable."""
    rows, planes, _src = resolve_stack(cons, board, facts, B)
    if not rows:
        return None
    first = next((r["name"] for r in rows if r.get("type") == "copper" and r.get("name")), None)
    g = pair_geometry(cons, rows, planes, family, first, board=board) if first else None
    return (g["w"], g["gap"]) if g and g["status"] in ("ok", "off", "unreachable") and g["w"] else None


def _project_floors(board):
    """(min track, min clearance) mm from the board's sibling .kicad_pro (rule minimum; the Default net class's clearance when larger), else (0, 0).
    A `board` that is not a path (a pcbnew BOARD, None) gives (0, 0)."""
    import json
    import os
    if not isinstance(board, (str, os.PathLike)):
        return 0.0, 0.0
    try:
        d = json.load(open(os.path.splitext(os.fspath(board))[0] + ".kicad_pro"))
        rules = ((d.get("board") or {}).get("design_settings") or {}).get("rules") or {}
        dflt = next((c for c in ((d.get("net_settings") or {}).get("classes") or []) if c.get("name") == "Default"), {})
        return float(rules.get("min_track_width") or 0.0), max(float(rules.get("min_clearance") or 0.0), float(dflt.get("clearance") or 0.0))
    except (OSError, ValueError, TypeError):
        return 0.0, 0.0


def floors(cons, board=None, layers=None):
    """(preferred mm, fab hard track mm, fab hard space mm). Preferred = the larger [route] floor; hard = [fab] profile row."""
    from . import constraints as C
    r = (cons or {}).get("route") or {}
    fab = C.fab_profile(cons, layers=layers, board=board) if (cons or {}).get("fab") is not None else {}
    bt, bs = _project_floors(board)                 # constraints without [fab]/[route] floors: the board's own rules, not 0.05 mm
    ht = float(fab.get("min_track_mm") or 0.0) or float(r.get("min_track_mm") or 0.0) or bt or 0.05
    hs = float(fab.get("min_clearance_mm") or 0.0) or float(r.get("min_clearance_mm") or 0.0) or bs or 0.05
    pref = max(float(r.get("min_track_mm") or 0.0), float(r.get("min_clearance_mm") or 0.0), ht, hs)
    return pref, ht, hs


def family_of(cons, net):
    fams = (cons or {}).get("pairs") or {}
    return max((f for f in fams if net.startswith(f)), key=len, default=None)


def _gap_for(target, w, g, lo, hi=1.5):
    s = IM._solve_gap(target, w, g, lo=max(lo, 0.02), hi=hi)
    return s


def _best_effort(target, g, ht, hs):
    best = None
    for wi in range(0, 60):
        w = ht + wi * 0.01
        for si in range(0, 60):
            s = hs + si * 0.01
            e = abs(IM.z_diff(w, s, g) - target)
            if best is None or e < best[0]:
                best = (e, w, s)
    return best[1], best[2]


def pair_geometry(cons, rows, planes, family, layer, tight=False, board=None, layers=None, class_geom=None):
    """See the module docstring. `class_geom` = (w, gap) of the project net class, the fallback without a stackup/plane."""
    fv = ((cons or {}).get("pairs") or {}).get(family) or {}
    target = fv.get("impedance_diff")
    tol = float(fv.get("tolerance_pct", 10))
    pref, ht, hs = floors(cons, board, layers)
    allow = bool(fv.get("allow_fab_limit", ((cons or {}).get("pairs") or {}).get("allow_fab_limit", False)))
    out = {"layer": layer, "target": target, "tol_pct": tol, "family": family, "w": None, "gap": None, "z": None, "err_pct": None,
           "status": "class", "source": "class", "model": None, "preferred_mm": pref, "fab_limit_mm": (ht, hs)}
    g = IM.geometry(rows, planes, layer) if (rows and target) else None
    ew, eg = fv.get("width_mm"), fv.get("gap_mm")

    def fill(w, s, source):
        out.update(w=round(w, 3), gap=round(s, 3), source=source, model=g["model"] if g else None)
        if g and g["model"]:
            z = IM.z_diff(w, s, g)
            out.update(z=round(z, 1), err_pct=round(100.0 * (z - target) / target, 1) if target else None)
            out["status"] = "ok" if (target is None or abs(out["err_pct"]) <= tol) else "off"
        return out

    if ew and eg:
        return fill(float(ew), float(eg), "explicit [pairs]")
    if g is None or g["model"] is None:
        w, s = (float(ew) if ew else (class_geom or (None, None))[0]), (float(eg) if eg else (class_geom or (None, None))[1])
        if w is None or s is None:                       # last resort: the old constants (a different stackup)
            from . import constraints as C
            cw, cg = C.PAIR_GEOM_BY_Z.get(int(target or 100), C.PAIR_GEOM_BY_Z[100])
            w, s = w or cw, s or cg
            out["source"] = "constant PAIR_GEOM_BY_Z (no stackup/reference plane)"
        out.update(w=w, gap=s, status="no_reference" if target else "class")
        return out
    if ew:                                               # one stated: solve the other
        s = _gap_for(target, float(ew), g, max(hs, pref if not (tight and allow) else hs))
        return fill(float(ew), s if s else max(pref, hs), "explicit width_mm, gap solved")
    if eg:
        w = IM.solve_diff(target, g, float(eg))
        return fill(w if w else max(pref, ht), float(eg), "explicit gap_mm, width solved")
    tiers = [("preferred floor", pref, pref)]
    if tight and allow:
        tiers = [("fab limit", ht, hs)] + tiers[:0]
    for label, fw, fs in tiers:
        s = fs
        w = IM.solve_diff(target, g, s)
        if w is not None and w < fw:
            w = fw
            s = _gap_for(target, w, g, fs)
        if w is not None and s is not None and w >= fw - 1e-9 and s >= fs - 1e-9:
            return fill(w, s, label)
    w, s = _best_effort(target, g, pref if not (tight and allow) else ht, pref if not (tight and allow) else hs)
    fill(w, s, "best achievable")
    out["status"] = "unreachable" if abs(out["err_pct"]) > tol else "ok"
    return out


def family_table(cons, rows, planes, signal_layers, family, tight=False, board=None, layers=None):
    """{layer: geometry} for every signal layer the family may use ([pairs.F] layers, else all signal layers)."""
    fv = ((cons or {}).get("pairs") or {}).get(family) or {}
    lay = fv.get("layers") or fv.get("layer") or signal_layers
    lay = [lay] if isinstance(lay, str) else list(lay)
    return {l: pair_geometry(cons, rows, planes, family, l, tight, board, layers) for l in lay if l in signal_layers}


def family_ok(table):
    """(ok, best layer) : a family is reachable when some allowed layer meets its target within tolerance."""
    good = [l for l, g in table.items() if g["status"] in ("ok", "class", "no_reference")]
    return bool(good), (min(good, key=lambda l: abs(table[l]["err_pct"] or 0)) if good else None)


def single_width(cons, rows, planes, net, layer, target=None, tight=False, board=None, layers=None):
    """Width of a single-ended controlled net on `layer` -> {w, z, err_pct, status, source, target, layer}.
    Target: argument, else [nets.NET] impedance, else [rf] target_z. Explicit [nets.NET] width_mm is kept (error reported)."""
    cons = cons or {}
    n = (cons.get("nets") or {}).get(net) or {}
    rf = cons.get("rf") or {}
    target = float(target or n.get("impedance", n.get("target_z", 0)) or rf.get("target_z", 50.0))
    tol = float(n.get("tolerance_pct", rf.get("tolerance_pct", 10)))
    pref, ht, hs = floors(cons, board, layers)
    allow = bool(n.get("allow_fab_limit", (cons.get("pairs") or {}).get("allow_fab_limit", False)))
    g = IM.geometry(rows, planes, layer) if rows else None
    out = {"layer": layer, "target": target, "tol_pct": tol, "net": net, "w": None, "z": None, "err_pct": None, "status": "no_reference",
           "source": None, "model": None}
    if g is None or g["model"] is None:
        out["w"] = float(n["width_mm"]) if n.get("width_mm") else None
        out["source"] = "explicit [nets]" if out["w"] else "no reference plane"
        return out
    out["model"] = g["model"]
    if n.get("width_mm"):
        w, src = float(n["width_mm"]), "explicit [nets]"
    else:
        w = IM.solve_single(target, g)
        floor = ht if (tight and allow) else pref
        src = "required width"
        if w is None:
            w, src = floor, "best achievable"
        elif w < floor:
            w, src = floor, ("fab limit" if floor == ht and ht < pref else "preferred floor") + " (required width is thinner)"
    z = IM.z_single(w, g)
    err = 100.0 * (z - target) / target
    out.update(w=round(w, 3), z=round(z, 1), err_pct=round(err, 1), source=src, status="ok" if abs(err) <= tol else "off")
    return out
