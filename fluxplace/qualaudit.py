"""Pair and RF quality: ONE implementation for routing (status JSON, route-status) and `review` (PAIR_* / RF_* codes).

pairs   both legs complete, same layer sequence, equal via count, skew <= the class limit (`pairaudit.audit`).
RF      per layer, the width of every RF segment (longer than `min_segment_mm`, not a neck) equals the geometry declared for it,
        else the impedance computed from the stackup is within `tolerance_pct` of `target_z`; layer changes (vias on the net)
        <= `max_vias` (class or `[nets.X] max_vias`).
        Declared geometry, first present wins: `[nets.X] width_mm`, the map's `rf_geometry` ({net or "*": width | {layer: width}}),
        `[rf] width_mm` (a width, or {layer = width}); tolerance `[rf] width_tol_mm` (0.02).
A via count over a limit the constraints state ([nets.X] / [rf] max_vias; level stays WARN unless the net has its own entry)
is NOT DONE too (cause via_count) unless `[review] waive` has RF_VIA_COUNT:^NET: for it.
A pair or RF net out of tolerance is NOT DONE: it does not count toward "nets routed", and the cause is reported.
`autotune` calls `tune` for pairs whose only failure is skew (bounded by `[route] tune_rounds`, 3, and `tune_pairs`, 8)."""
import math
import os
import re

from . import constraints as C
from . import netclass as NC
from . import pairaudit as PA

_RF_TOKENS = {"RF", "RFIN", "RFOUT", "COAX", "UFL", "LNA", "ANTENNA"}


def is_rf(net):
    return bool(set(re.split(r"[_\-.]", net.upper())) & _RF_TOKENS)


# ---------------------------------------------------------------- pairs
def pair_rows(B, cls, cons, open_nets):
    """[{pair, p, n, status, causes, why, skew, limit}] for every pair of the board (pairaudit.audit)."""
    res = PA.audit(B, PA.pair_list(cls), cons, open_nets)
    return [{"pair": f"{p}/{n}", "p": p, "n": n, "status": e["status"], "causes": e.get("causes", []), "why": e["why"],
             "skew": e.get("skew"), "limit": e.get("limit"), "target": e.get("target"), "over_target": e.get("over_target"),
             "paths": e.get("paths"), "stubs": e.get("stubs"), "unit": e.get("unit", "mm"), "code": e.get("code"),
             "basis": e.get("basis"), "skew_mm": e.get("skew_mm"), "skew_ps": e.get("skew_ps")} for (p, n), e in res.items()]


# ---------------------------------------------------------------- RF
def declared_width(cons, fmap, net, layer):
    def pick(v):
        if isinstance(v, dict):
            return v.get(layer)
        return v
    nets = ((cons or {}).get("nets") or {}).get(net) or {}
    for v in (nets.get("width_mm"), ((fmap or {}).get("rf_geometry") or {}).get(net), ((fmap or {}).get("rf_geometry") or {}).get("*"),
              ((cons or {}).get("rf") or {}).get("width_mm")):
        w = pick(v) if v is not None else None
        if w:
            return float(w)
    return None


def tracks_of(B, net):
    """{segments: [(layer, width, mm)] aggregated, vias: n} of a net from the parsed board."""
    agg = {}
    for s in B["segs"]:
        if s["net"] == net:
            k = (s["layer"], round(s["w"], 4))
            agg[k] = agg.get(k, 0.0) + math.hypot(s["a"][0] - s["b"][0], s["a"][1] - s["b"][1])
    return {"segments": [(l, w, mm) for (l, w), mm in sorted(agg.items())],
            "vias": sum(1 for v in B["vias"] if v["net"] == net)}


def rf_grade(net, tr, stackup, planes, cons, fmap=None):
    """[{level, code, kind, msg}] for one RF net; the same code review.check_rf reports."""
    from . import review as R
    rf = (cons or {}).get("rf", {})
    target = float(rf.get("target_z", 50.0))
    tol = float(rf.get("tolerance_pct", 10.0))
    wtol = float(rf.get("width_tol_mm", 0.02))
    per_net = (cons or {}).get("nets", {})
    stated = "max_vias" in per_net.get(net, {}) or "max_vias" in rf       # a limit the constraints state, not the default
    lim = int(per_net.get(net, {}).get("max_vias", rf.get("max_vias", 1)))
    min_seg, neck = float(rf.get("min_segment_mm", 1.0)), float(rf.get("neck_max_mm", 1.5))
    out = []
    if tr.get("vias", 0) > lim:
        out.append({"level": "FAIL" if net in per_net else "WARN", "code": "RF_VIA_COUNT", "kind": "vias", "explicit": stated,
                    "msg": f"{net}: {tr['vias']} vias (limit {lim}) — every layer transition is an impedance discontinuity"})
    worst = None
    for layer, w, mm in tr.get("segments", []):
        if mm < min_seg or mm <= neck:
            continue
        dw = declared_width(cons, fmap, net, layer)
        if dw is not None:
            if abs(w - dw) > wtol and (worst is None or abs(w - dw) > worst[0]):
                worst = (abs(w - dw), layer, w, mm, dw)
            continue
        from . import impedance as IM, zgeom as ZG        # ONE model: impedance.py, the same one zgeom.single_width solves with
        geom = IM.geometry(stackup, set(planes), layer) if stackup else None
        if geom is None:
            continue
        z, model = (IM.z_single(w, geom), geom["model"]) if geom["model"] else (None, "unreferenced")
        if z is None:
            out.append({"level": "WARN", "code": "RF_NO_REFERENCE", "kind": "noref",
                        "msg": f"{net}: {mm:.1f}mm on {layer} with no reference plane on either side"})
            continue
        err = 100.0 * (z - target) / target
        if abs(err) > tol and (worst is None or abs(err) > worst[0]):
            worst = (abs(err), layer, w, mm, (z, model, err))
    if worst:
        a, layer, w, mm, d = worst
        if isinstance(d, tuple):
            z, model, err = d
            out.append({"level": "FAIL", "code": "RF_IMPEDANCE_OFF", "kind": "impedance",
                        "msg": f"{net}: {mm:.1f}mm of {w:.3f}mm trace on {layer} is ~{z:.0f} ohm ({model}, {err:+.0f}% of {target:.0f}) — "
                               f"it needs {ZG.single_width(cons, stackup, planes, net, layer)['w']} mm there (zgeom.single_width)"})
        else:
            out.append({"level": "FAIL", "code": "RF_WIDTH_OFF", "kind": "width",
                        "msg": f"{net}: {mm:.1f}mm of {w:.3f}mm trace on {layer}, the declared geometry is {d:.3f}mm"})
    return out


def rf_names(B, cons):
    names = set(((cons or {}).get("rf") or {}).get("nets", []))
    return names or {p["net"] for p in B["pads"] if p["net"] and is_rf(p["net"])}


def rf_rows(B, cons, stackup, planes, fmap=None, nets=None):
    """Findings of EVERY RF net of the board (or of `nets` when given), one row per finding, each row carrying its own `net`.
    It is a whole-board call: calling it once per net and reading row 0 gives the first net's finding for every net.
    Filter on `row["net"]` or pass `nets=[net]`."""
    rows = []
    want = None if nets is None else set(nets)
    for net in sorted(rf_names(B, cons)):
        if want is not None and net not in want:
            continue
        if not any(p["net"] == net for p in B["pads"]):
            continue
        for f in rf_grade(net, tracks_of(B, net), stackup, planes, cons, fmap):
            rows.append(dict(f, net=net))
    return rows


# ---------------------------------------------------------------- the routing view
def _ctx(board, B, cons):
    from . import review as R, preroute as PR
    stackup = []
    if board and os.path.isfile(board):
        stackup = R.parse_stackup_text(open(board, errors="replace").read())
    planes = set(PR.layer_split(B, cons)[1]) if B.get("zones") is not None else set()
    fmap = None
    if board:
        try:
            from . import fluxmap as FM
            fmap = FM.load(FM.default_path(board))
        except (OSError, ValueError):
            fmap = None
    return stackup, planes, fmap


def via_stated(row):
    """A via-count finding against a limit the constraints state ([nets.X] / [rf] max_vias) counts as NOT DONE even at level WARN."""
    return row["code"] == "RF_VIA_COUNT" and bool(row.get("explicit"))


def via_waived(cons, row):
    """`[review] waive = ["RF_VIA_COUNT:^NET:"]`: the review waiver form (CODE:REGEX against the message, which starts "NET: ")."""
    from . import review as R
    return R._waived({"code": row["code"], "msg": row["msg"]}, ((cons or {}).get("review") or {}).get("waive"))


def not_done(B, cls, cons, open_nets, board=None, ctx=None):
    """-> {pairs: [{pair, cause}], rf: [{net, cause}], nets: set} for a board in the middle of routing. Open nets are not here
    (they are open); a pair with one open leg is half-routed, reported by pairaudit."""
    stackup, planes, fmap = ctx or _ctx(board, B, cons)
    rows = pair_rows(B, cls, cons, open_nets)
    pairs = [{"pair": r["pair"], "cause": r["why"] or r["status"], "causes": r["causes"], "status": r["status"]}
             for r in rows if r["status"] in ("mismatch", "half")]
    rf = [{"net": r["net"], "cause": "via_count" if r["code"] == "RF_VIA_COUNT" else r["msg"], "code": r["code"], "msg": r["msg"]}
          for r in rf_rows(B, cons, stackup, planes, fmap)
          if r["net"] not in open_nets and (r["level"] == "FAIL" or via_stated(r))
          and not (r["code"] == "RF_VIA_COUNT" and via_waived(cons, r))]
    nets = {x for r in pairs for x in r["pair"].split("/")} | {r["net"] for r in rf}
    ungraded = [{"pair": r["pair"], "cause": r["why"]} for r in rows if r["status"] == "unknown"]
    return {"pairs": pairs, "rf": rf, "nets": nets, "ungraded": ungraded}


def autotune(board, cons, conn, log=lambda m: None):
    """tune pairs whose ONLY failure is skew (bounded), keep the result when no open connection appears and the pair improves.
    -> {tuned: [pairs], kept: bool}."""
    from . import routediag as RD, tune as TU, opengate as OG
    import shutil
    B = RD.load(board)
    cls = NC.classify(B, cons)
    open_nets = set(conn(board))
    rows = [r for r in pair_rows(B, cls, cons, open_nets) if r["status"] == "mismatch" and r["causes"] == ["skew"]]
    r_ = dict((cons or {}).get("route") or {})
    rows = rows[:int(r_.get("tune_pairs", 8))]
    if not rows:
        return {"tuned": [], "kept": False}
    tmp = board + ".tune.kicad_pcb"
    for ext in (".kicad_pro", ".kicad_dru"):
        side = os.path.splitext(board)[0] + ext
        if os.path.exists(side):
            shutil.copy(side, os.path.splitext(tmp)[0] + ext)
    lim = lambda master: (lambda v: float("inf") if v is None else v)(C.skew_limit_checked(cons or {}, master)[0])
    TU.tune_pairs(board, tmp, {r["n"]: r["p"] for r in rows}, lim, max_rounds=int(r_.get("tune_rounds", 3)), log=log, cons=cons)
    open1 = conn(tmp)
    B1 = RD.load(tmp)
    after = {r["pair"]: r for r in pair_rows(B1, cls, cons, set(open1))}
    better = all(a["status"] == "complete" or (a["skew"] or 9e9) < (next(r["skew"] for r in rows if r["pair"] == k) or 0)
                 for k, a in after.items() if k in {r["pair"] for r in rows})
    kept = sum(open1.values()) <= sum(conn(board).values()) and better
    if kept:
        shutil.copy(tmp, board)
        OG.refill(board)
    for ext in ("", ".kicad_pro", ".kicad_dru"):
        q = os.path.splitext(tmp)[0] + ext if ext else tmp
        if os.path.exists(q):
            os.unlink(q)
    return {"tuned": [r["pair"] for r in rows], "kept": kept}


def status_fields(B, cls, cons, open_now, open0, ctx=None, board=None):
    """the quality part of a routing status: nets_routed counts closed nets that are not NOT DONE."""
    nd = not_done(B, cls, cons, set(open_now), board=board, ctx=ctx)
    closed = [n for n in open0 if n not in open_now]
    return {"not_done": {"count": len(nd["nets"]), "pairs": nd["pairs"][:20], "rf": nd["rf"][:20]},
            "ungraded": nd["ungraded"][:20],      # pairs whose skew could not be graded: counted routed, but NOT verified
            "nets_routed": sum(1 for n in closed if n not in nd["nets"]), "nets_open0": len(open0)}
