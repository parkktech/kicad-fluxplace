"""Readiness gate: is the design FIT to start placement and routing?  `fluxplace ready SPEC --constraints C [--board B]`.

Everything a later stage would otherwise guess, or find out about after hours of routing:

  parts        every part passes the part-entry gate (parts.py)
  stackup      declared in the constraints (`[stackup] layers = [...] planes = [...]` or `profile = "name"`), else read from the
               board file (noted), else a question
  classes      every controlled class (`[pairs.X] impedance_diff`, `[rf] target_z`, `[nets.N] impedance`) has a geometry (width,
               gap, layer) COMPUTED from the stackup (impedance.py): formula, inputs, result, stated accuracy
  rails        every power rail with >= `[route] rail_min_pads` pads has a plane/pour or an answer (tree | pour)
  fixed parts  anchors/mounting hardware locked or declared, or an answer
  outline      the board has an outline
  floors       design-rule floors exist ([fab]/[route], the project's rules, or an answer)

Output: a checklist, PASS / FAIL / OPEN (a question) / UNCHECKED per line, then the first blockers.  Exit 1 on a FAIL (distinct
from exit 3 = open questions, which are listed and written to `<stem>.questions.json`).  `--allow-incomplete` continues with exit 0
but writes `<board>.notready.json`: every later pipeline output is stamped `NOT READY: <reasons>` and `fab`/`deliver` refuse the
board."""
import fnmatch
import json
import os
import sys

from . import questions as QS

PASS, FAIL, OPEN, UNCHECKED, WAIVED = "PASS", "FAIL", "OPEN", "UNCHECKED", "WAIVED"


def notready_path(board):
    return board + ".notready.json"


def _line(name, status, detail, blockers=()):
    return {"name": name, "status": status, "detail": detail, "blockers": list(blockers)}


# ------------------------------------------------------------------ stackup
def resolve_stackup(cons, facts, sess):
    """-> (rows, planes, source) or (None, None, why); a missing stackup is a question."""
    from . import stackup as ST
    sc = (cons or {}).get("stackup") or {}
    if sc.get("layers"):
        rows = [{"name": l.get("name"), "type": l.get("type", "copper" if str(l.get("name", "")).endswith(".Cu") else "core"),
                 "thickness": float(l.get("thickness", 0) or 0), "epsilon_r": float(l.get("epsilon_r", 4.4) or 4.4)} for l in sc["layers"]]
        return rows, list(sc.get("planes") or (facts or {}).get("plane_layers") or []), "constraints [stackup]"
    if sc.get("profile") in ST.PROFILES:
        prof = ST.PROFILES[sc["profile"]]
        rows = [{"name": l.get("name"), "type": l["type"], "thickness": l["thickness"], "epsilon_r": l.get("epsilon_r", 4.4)}
                for l in prof["layers"]]
        return rows, list(sc.get("planes") or prof.get("reference_layers") or []), f"profile {sc['profile']}"
    if facts and facts.get("stackup"):
        sess.inferred("stackup from the board file", "the constraints declare none")
        return list(facts["stackup"]), list(sc.get("planes") or facts.get("plane_layers") or []), "board file"
    n = (facts or {}).get("copper_layers")
    opts = {k: v.get("description", k) for k, v in sorted(ST.PROFILES.items())}
    dflt = next((k for k in sorted(opts) if n and f"{n}l" in k.lower()), sorted(opts)[0])
    ch = sess.ask("stackup", "stackup", "no stackup in the constraints and none in the board file: impedance cannot be computed",
                  opts, dflt, "constraints [stackup] layers/planes (thickness, epsilon_r from the fab's stackup sheet)")
    prof = ST.PROFILES[ch]
    rows = [{"name": l.get("name"), "type": l["type"], "thickness": l["thickness"], "epsilon_r": l.get("epsilon_r", 4.4)}
            for l in prof["layers"]]
    return rows, list(prof.get("reference_layers") or []), f"profile {ch} (provisional, unanswered)"


# ------------------------------------------------------------------ controlled classes
def controlled_classes(cons, nets):
    """[{name, kind diff|se, target, nets, layers, gap, tol}] from the constraints (+ RF net names)."""
    from . import review as R
    cons = cons or {}
    out = []
    for fam, p in sorted((cons.get("pairs") or {}).items()):
        if "impedance_diff" in p:
            out.append({"name": f"pair {fam}", "kind": "diff", "target": float(p["impedance_diff"]),
                        "nets": sorted(n for n in nets if n.startswith(fam)),
                        "layers": _layers(p), "gap": p.get("gap_mm"), "tol": float(p.get("tolerance_pct", 10)), "fam": fam})
    rf = cons.get("rf") or {}
    if rf:
        names = sorted(set(rf.get("nets", [])) or {n for n in nets if R.is_rf(n)})
        if names:
            out.append({"name": "rf", "kind": "se", "target": float(rf.get("target_z", 50)), "nets": names,
                        "layers": _layers(rf), "gap": None, "tol": float(rf.get("tolerance_pct", 10))})
    for net, p in sorted((cons.get("nets") or {}).items()):
        z = p.get("impedance", p.get("target_z"))
        if z:
            out.append({"name": f"net {net}", "kind": "se", "target": float(z), "nets": [net], "layers": _layers(p), "gap": None,
                        "tol": float(p.get("tolerance_pct", 10))})
    return out


def _layers(p):
    v = p.get("layers") or p.get("layer")
    return [v] if isinstance(v, str) else list(v or [])


def board_geometry(facts, board, cls):
    """What the board uses for a class: the netclass assigned in the project (width/gap) and the dominant routed width per layer."""
    from . import stackup as ST
    got = {}
    if board:
        classes, pats = ST.read_netclasses(board)
        by = {c.get("name"): c for c in classes}
        for pt in pats:
            nc = by.get(pt.get("netclass"))
            if nc and any(fnmatch.fnmatchcase(n, pt.get("pattern", "")) for n in cls["nets"]):
                got["netclass"] = {"name": nc["name"], "track": nc.get("track_width"), "diff_width": nc.get("diff_pair_width"),
                                   "diff_gap": nc.get("diff_pair_gap")}
                break
    tracks = (facts or {}).get("net_tracks") or {}
    agg = {}
    for n in cls["nets"]:
        for layer, w, mm in (tracks.get(n) or {}).get("segments", []):
            agg[(layer, w)] = agg.get((layer, w), 0.0) + mm
    if agg:
        got["routed"] = sorted(([l, w, round(mm, 1)] for (l, w), mm in agg.items()), key=lambda r: -r[2])[:3]
    return got


def class_lines(cons, facts, board, rows, planes, source, floor, nets):
    from . import impedance as IM
    lines, detail = [], []
    sig = IM.signal_layers(rows, planes)
    for cls in controlled_classes(cons, nets):
        diff = cls["kind"] == "diff"
        cand = cls["layers"] or sig
        per_layer = {}
        for L in cand:
            g = IM.geometry(rows, planes, L)
            per_layer[L] = IM.options(cls["target"], diff, g, floor=floor, gap=cls["gap"]) if g else []
        pick = next(((L, o[0]) for L, o in per_layer.items() if o), None)
        name = f"{cls['name']} ({'diff' if diff else 'single-ended'} {cls['target']:g} ohm, {len(cls['nets'])} net(s))"
        if not pick:
            why = "no reference plane on any candidate layer" if not any(IM.geometry(rows, planes, L) and
                                                                         IM.geometry(rows, planes, L)["model"] for L in cand) else \
                "target not reachable at a manufacturable width"
            lines.append(_line("class " + cls["name"], FAIL, f"{name}: no geometry ({why}; layers {', '.join(cand) or 'none'})",
                               [f"{cls['name']}: no computed geometry"]))
            detail.append({"class": cls["name"], "solved": False})
            continue
        L, o = pick
        used = board_geometry(facts, board, cls)
        note = ""
        tolerance = cls["tol"]
        nc = used.get("netclass")
        if nc:
            w = nc.get("diff_width") if diff else nc.get("track")
            s = nc.get("diff_gap") if diff else None
            g = IM.geometry(rows, planes, L)
            if w and g and (not diff or s):
                z = IM.z_diff(w, s, g) if diff else IM.z_single(w, g)
                err = 100.0 * (z - cls["target"]) / cls["target"]
                used["netclass_z"] = [round(z, 1), round(err, 1)]
                note = f"; declared netclass {nc['name']} {w}" + (f"/{s}" if s else "") + f" -> {z:.1f} ohm ({err:+.1f}%)"
        g0 = IM.geometry(rows, planes, L)
        rz = []
        for rl, rw, rmm in (used.get("routed") or [])[:2]:
            gg = IM.geometry(rows, planes, rl)
            if gg and gg["model"] and not diff:
                z = IM.z_single(rw, gg)
                rz.append(f"{rl} {rw:g}x{rmm:g}mm -> {z:.0f} ohm ({100.0 * (z - cls['target']) / cls['target']:+.0f}%)")
        if rz:
            note += "; routed: " + ", ".join(rz)
            used["routed_z"] = rz
        pj = []
        if diff and g0:
            from . import constraints as C, fabcaps as FC
            try:
                fab = C.fab_profile(cons, layers=sum(1 for r in rows if r.get("type") == "copper") or None)
            except FC.FabProfileError as e:
                fab = {"min_track_mm": 0.0, "min_clearance_mm": 0.0, "profile": None}
                note += f"; {e}"
            decl = (nc.get("diff_width"), nc.get("diff_gap")) if nc and nc.get("diff_width") and nc.get("diff_gap") else None
            pj = IM.pair_judgement(cls["target"], g0, floor, fab["min_track_mm"], fab["min_clearance_mm"], decl)
            if pj and decl and pj[0]["status"] == "below_preferred":
                tier = fab.get("profile") or "fab limit"
                note += (f"; declared gap {decl[1]:g} below preferred floor {floor:g}, within fab limit ({tier})")
        ok = not used.get("netclass_z") or abs(used["netclass_z"][1]) <= tolerance
        used["pair_geometry"] = pj
        st = PASS if ok else FAIL
        geo = f"{L} w {o['width_mm']}" + (f" gap {o['gap_mm']}" if o["gap_mm"] else "") + f" -> {o['z']} ohm, {o['accuracy']}"
        lines.append(_line("class " + cls["name"], st, f"{name}: {geo}{note}",
                           [] if ok else [f"{cls['name']}: the declared netclass is {used['netclass_z'][1]:+.1f}% off {cls['target']:g} ohm "
                                          f"(tolerance {tolerance:g}%); computed {geo}"]))
        detail.append({"class": cls["name"], "kind": cls["kind"], "target": cls["target"], "layer": L, "chosen": o,
                       "alternatives": {k: v for k, v in per_layer.items() if v}, "board": used, "stackup": source})
    return lines, detail


# ------------------------------------------------------------------ the gate
def evaluate(spec, cons, facts, board, ds_dir, sess, parts_res, api=False, allow_undocumented=False):
    """-> (lines, detail)."""
    from . import parts as P, partsgate as PG
    lines, detail = [], {}
    f = parts_res["findings"]
    t = P.totals(f)
    unc = []
    if not parts_res["lookup"]:
        unc.append("distributor/stock NOT CHECKED")
    lines.append(_line("parts", FAIL if t["FAIL"] else PASS,
                       f"{len(parts_res['rows'])} part(s): {t['FAIL']} FAIL, {t['WARN']} WARN" + (f" ({'; '.join(unc)})" if unc else ""),
                       [f"{x['code']} {x['msg']}" for x in P._spread([x for x in f if x["level"] == "FAIL"], 4)] if t["FAIL"] else []))
    g = PG.gate(spec, cons, facts, parts_res, qty=P.build_quantity(cons), ds_dir=ds_dir)
    detail["gate"] = g
    if g["ok"]:
        lines.append(_line("parts gate", PASS, PG.summary_line(g)[len("parts gate: "):]))
    elif allow_undocumented and not g["geometry"]:      # footprint geometry is never bypassed (fpcheck)
        lines.append(_line("parts gate", WAIVED, PG.bypass_note(g)))
        for l in lines:
            if l["name"] == "parts" and l["status"] == FAIL:      # the same documentation failures, already waived loudly above
                l["status"], l["blockers"] = WAIVED, []
    else:
        lines.append(_line("parts gate", FAIL, PG.summary_line(g)[len("parts gate: "):], PG.blockers(g)
                           + [u for u in g["unchecked"]]))
    nets = set((facts or {}).get("net_pads") or ()) or set((spec or {}).get("nets") or ())
    fab_min = float(((cons or {}).get("fab") or {}).get("min_track_mm", ((cons or {}).get("route") or {}).get("min_track_mm", 0.0)))
    floor = max(fab_min, float(((cons or {}).get("ready") or {}).get("min_feature_mm", 0.127)))   # never plan at the fab's bare minimum
    rows, planes, src = resolve_stackup(cons, facts, sess)
    if rows is None:
        lines.append(_line("stackup", OPEN, src))
    else:
        open_st = any(q["id"] == "stackup" for q in sess.open)
        lines.append(_line("stackup", OPEN if open_st else PASS,
                           f"{src}; {sum(1 for r in rows if r.get('type') == 'copper')} copper layers, reference planes "
                           f"{', '.join(planes) or 'none'}"))
        cl, cd = class_lines(cons, facts, board, rows, planes, src, floor, nets)
        if not cl:
            lines.append(_line("classes", PASS, "no controlled class declared ([pairs.X] impedance_diff, [rf], [nets.N] impedance)"))
        lines += cl
        detail["classes"] = cd
    # board-dependent decisions: the questions facility's own checks
    if board and os.path.exists(board):
        from . import routediag as RD, netclass as NC
        B = RD.load(board)
        cls = NC.classify(B, cons, spec)
        for key, label, fn in (("rails", "rails", QS.check_rail_pour), ("fixed", "fixed parts", QS.check_fixed_parts),
                               ("floors", "design-rule floors", QS.check_floors)):
            n0 = len(sess.open)
            fn(sess, B, cons, cls=cls)
            new = sess.open[n0:]
            ids = ", ".join(q["id"] for q in new[:4]) + (f" (+{len(new) - 4})" if len(new) > 4 else "")
            lines.append(_line(label, OPEN if new else PASS,
                               f"{len(new)} undecided: {ids}" if new else
                               {"rails": f"every rail of >= {_minpads(cons)} pads has a plane/pour or an answer",
                                "fixed": "anchors and mounting hardware are locked or declared",
                                "floors": "floors declared ([fab]/[route] or the project's rules)"}[key]))
        lines += power_plan_lines(spec, cons, facts, B, ds_dir, sess, rows, planes, board, detail)
        lines.append(fixed_reason_line(spec, cons, B, detail))
        lines.append(pair_class_line(cons, cls, detail))
        lines.append(budget_line(spec, cons, B, rows, planes, detail))
        has_outline = bool((facts or {}).get("outline_bbox"))
        lines.append(_line("outline", PASS if has_outline else FAIL,
                           "Edge.Cuts outline present" if has_outline else "no board outline (Edge.Cuts)",
                           [] if has_outline else ["board has no outline"]))
    else:
        lines.append(_line("board checks", UNCHECKED, "no --board: rails, fixed parts, outline and floors not checked"))
        lines += power_plan_lines(spec, cons, facts, None, ds_dir, sess, rows, planes, None, detail)
    from . import liveview as LV                  # F16: this stage's criteria list, when a stream is on
    LV.progress("ready", "gate", always=True, verdict="kept" if all(x["status"] in (PASS, WAIVED) for x in lines) else "refused",
                criteria=[{"id": x["name"], "ok": None if x["status"] == UNCHECKED else x["status"] in (PASS, WAIVED), "value": x["status"],
                           "limit": PASS, "text": x["detail"]} for x in lines])
    return lines, detail


def _fixed_inputs(spec, cons, B):
    """-> (fixed refs, support rows by ref, detected-only refs, bare refs): what fixed_line and the stage rows both judge."""
    from . import preroute as PR, powerbudget as PB, modules as MD
    rows = {}
    for s in PB.supports(B, cons, spec):
        rows.setdefault(s["ref"], []).append(s)
    from . import netclass as NC
    explicit = PR.explicit_fixed(B, cons)
    fixed = {r for r in explicit | MD.fixed_set(B, cons) if r in B["parts"]}
    # the owner listed the fixed parts: whatever else a stage holds by detection is a silent hold (with no list at all the
    # "fixed parts" question above asks instead)
    detected = PR.fixed_parts(B, cons) - fixed if explicit else set()
    bare = {r for r in fixed if all(not q["net"] or NC.is_ground(q["net"]) for q in B["parts"][r]["pads"])}
    return fixed, rows, detected, bare


def fixed_reason_line(spec, cons, B, detail=None):
    """[the "fixed reasons" line] (F1): every part a stage will not move (locked, [lock.at], [placement] fixed / anchors, mounting
    hardware, group centres; and the detected anchors of preroute.fixed_parts) has a reason class, and a fixed support part over
    its max distance (powerbudget.supports) is OPEN FIXED_OVER_BUDGET whatever its reason."""
    fixed, rows, detected, bare = _fixed_inputs(spec, cons, B)
    return fixed_line(fixed, cons, rows, detected, detail, bare)


def fixed_line(fixed, cons, rows, detected=(), detail=None, unconnected=()):
    """the "fixed reasons" line from the fixed refs and their support rows (fixedreason.findings). Every finding goes to
    detail["fixed_reasons"] (ready.json); the line carries `open_items`, which the verdict counts like open questions."""
    from . import fixedreason as FR
    fs = FR.findings(fixed, cons, rows, detected, unconnected)
    if detail is not None:
        detail["fixed_reasons"] = fs
    if not fs:
        return _line("fixed reasons", PASS, f"{len(fixed)} fixed part(s), each with a reason class; none over its support max")
    n = len({f["ref"] for f in fs})
    return dict(_line("fixed reasons", OPEN, f"{len(fs)} finding(s) on {n} of {len(fixed)} fixed part(s)", FR.blockers(fs)),
                open_items=len(fs))


def pair_class_line(cons, cls, detail=None):
    """[the "pair classes" line] (F1): a _P/_N-style name pair in no [pairs.*] / [channel.*] family is OPEN PAIR_UNCLASSIFIED."""
    from . import fixedreason as FR
    un = FR.unclassified_pairs(cls, cons)
    if detail is not None:
        detail["pairs_unclassified"] = un
    npair = sum(1 for i in cls.values() if i["class"] == "pair") // 2
    if not un:
        return _line("pair classes", PASS, f"{npair} name pair(s), each in a [pairs] / [channel] family")
    return dict(_line("pair classes", OPEN, f"{len(un)} of {npair} name pair(s) without a class",
                      [f"PAIR_UNCLASSIFIED {len(un)} pair(s): {', '.join(un)}: add a [pairs.PREFIX] / [channel.PREFIX] family, "
                       f"or list both nets in not_pairs (plain signals, go to the router)"]), open_items=len(un))


def _budget_table(spec, cons, B, rows, planes, detail):
    """the one budget table (budget.table) of the placement as it is; also detail["budgets"]. -> (table, rail classes)."""
    from . import budget as BG, preroute as PR, modules as MD, pairs as PA
    sig = [l for l in PA.pair_layers(B, cons)[0]] if rows else []
    stack = (rows, list(planes or []), sig) if rows and sig else None
    fixed = PR.explicit_fixed(B, cons) | MD.fixed_set(B, cons)
    tab = BG.table(B, cons, spec, stack, detail.get("power_plan"), fixed)
    rc = BG.rail_classes(cons, detail.get("power_plan"))
    detail["budgets"] = {"rows": tab, "netclasses": BG.netclasses(cons, stack=stack), "rail_classes": rc}
    return tab, rc


def measure_rows(spec, cons, B, board_path, facts=None, ds_dir=None, sess=None):
    """C7b: the budget rows and fixed-reason findings of a placement, computed as `ready` computes them and returned without
    printing -> (rows, fixed findings). Stage-end use: nothing is asked, nothing is written. A stackup the constraints do not
    declare is taken from the board file when it can be read."""
    from . import questions as QS_, review as R, fixedreason as FR
    if facts is None and board_path and os.path.exists(board_path):
        try:
            facts = R.facts_from_board(board_path, backend="file")
        except Exception:                                   # noqa: BLE001 - a board facts cannot read still has rows
            facts = None
    sess = sess or QS_.Session(board_path or "rows.kicad_pcb", cons, {}, True, "rows")
    rows, planes, _src = resolve_stackup(cons, facts, sess)
    detail = {}
    power_plan_lines(spec, cons, facts, B, ds_dir, sess, rows, planes, board_path, detail)
    tab, _rc = _budget_table(spec, cons, B, rows, planes, detail)
    fixed, frows, detected, bare = _fixed_inputs(spec, cons, B)
    return tab, FR.findings(fixed, cons, frows, detected, bare)


def budget_line(spec, cons, B, rows, planes, detail):
    """[the "budgets" line] (F2): the one budget table (budget.table) of the placement as it is, written to <board>.budgets.json.
    OPEN on a limit without a source, a hard keep-out against flight-line row, an undecided swap proposal; everything else is
    information here (floorplan and PLACE_OK gate the rows)."""
    from . import budget as BG
    tab, rc = _budget_table(spec, cons, B, rows, planes, detail)
    under = [f"{n} {v['class_mm']:g} < {v['plan_mm']:g} mm" for n, v in rc.items() if v["under"]]
    note = "".join("; " + t for t in (BG.summary(tab), f"{len(under)} rail class(es) narrower than the plan ({', '.join(under)})" if under else "") if t)
    kinds = ", ".join(f"{sum(1 for r in tab if r['kind'] == k)} {k}" for k in BG.KINDS if any(r["kind"] == k for r in tab))
    items = BG.open_items(tab)
    if not items:
        return _line("budgets", PASS, f"{len(tab)} row(s): {kinds or 'none'}{note}")
    by = {}
    for code, text in items:
        by.setdefault(code, []).append(text)
    return dict(_line("budgets", OPEN, f"{len(items)} open item(s) in {len(tab)} row(s): {kinds}{note}",
                      [f"{c} {len(t)}: {'; '.join(t)}" for c, t in sorted(by.items())]), open_items=len(items))


def power_plan_lines(spec, cons, facts, B, ds_dir, sess, rows, planes, board, detail):
    """[the "power plan" line]: powerbudget.build on the placement as it is (D71dc/D71dd). FAIL: a rail with a [power] current and no
    [[dcdrop]] row, or copper that cannot carry the budget; OPEN: an unresolved source or a guessed support owner. The table goes to
    detail["power_plan"] (written as power-plan.json). Reach and support limits are information here (floorplan / preroute gate them)."""
    from . import powerbudget as PB, elec as EL, power as PW
    if not any(isinstance(p, dict) and PW.current_a(p) for p in ((cons or {}).get("power") or {}).values()):
        return []
    if rows is None:
        return [_line("power plan", UNCHECKED, "no stackup resolved: copper reach not planned")]
    mm = (facts or {}).get("mpn_map") or {}
    mm = {**{c["ref"]: c["mpn"] for c in (spec or {}).get("components", []) if isinstance(c, dict) and c.get("mpn")}, **mm}
    df = EL.load_facts(ds_dir) if ds_dir and os.path.isdir(ds_dir) else {}
    plan = PB.build(cons, B, spec, rows, df, mm, board_path=board, planes=planes)
    n0 = len(sess.open)
    QS.check_power_plan(sess, B, cons, plan=plan)
    new = sess.open[n0:]
    detail["power_plan"] = PB.plan_json(plan)
    bad = [f for f in plan["findings"] if f["level"] == "FAIL"]
    if bad:
        names = ", ".join(sorted({f["net"] for f in bad}))
        return [_line("power plan", FAIL, f"{len(bad)} rail(s) cannot be planned ({names}); {PB.summary(plan)}",
                      [f"{f['code']} {f['msg']}" for f in bad][:6])]
    ids = ", ".join(q["id"] for q in new[:4]) + (f" (+{len(new) - 4})" if len(new) > 4 else "")
    return [_line("power plan", OPEN if new else PASS, (f"{len(new)} undecided: {ids}; " if new else "") + PB.summary(plan))]


def sync_netclasses(board, cons, log=lambda *a, **k: None, write=False):
    """A4 / review 4: do the project's netclasses carry the budgets?  REPORT-ONLY unless `write`: the project file beside the user's board may
    be open in a GUI, so `ready` never writes it; a stage that owns a COPY of the project (the ready stage's board) calls this with
    write=True.  The check compares the class that actually applies to each pair leg (a user class too), not only our own classes.
    Written classes are fluxplace-managed (explicit marker) and set width / gap only: user classes are kept whole, clearance and via
    sizes are never touched.  -> a checklist line (FAIL NETCLASS_MISMATCH lists what differs)."""
    from . import constraints as C
    pro = os.path.splitext(board)[0] + ".kicad_pro" if board else None
    if not pro or not os.path.isfile(pro):
        return _line("netclasses", UNCHECKED, "no project file beside the board: netclasses not checked")
    if not ((cons or {}).get("pairs") or (cons or {}).get("power") or (cons or {}).get("rule")):
        return _line("netclasses", PASS, "no pair, rail or rule class declared")
    try:
        before = C.netclass_mismatch(pro, cons, board)
        managed = [x for x in before if x.startswith("managed")]
        if write and managed:
            C.inject_netclasses(pro, cons, log=log, board=board)
        after = C.netclass_mismatch(pro, cons, board) if (write and managed) else before
    except (OSError, ValueError) as e:
        return _line("netclasses", FAIL, f"NETCLASS_MISMATCH: project file not readable or writable ({e})", [f"NETCLASS_MISMATCH {e}"])
    if after:
        return _line("netclasses", FAIL, f"NETCLASS_MISMATCH: {len(after)} differ: {'; '.join(after[:3])}",
                     [f"NETCLASS_MISMATCH {x}" for x in after[:6]])
    return _line("netclasses", PASS, "project netclasses match the constraints" + (f" ({len(managed)} written)" if write and managed else ""))


def _minpads(cons):
    from . import railroute as RL
    return RL.min_pads(cons)


def reasons(lines):
    return [f"{l['name']}: {l['detail'][:80]}" for l in lines if l["status"] in (FAIL, OPEN, WAIVED)]


def format_text(lines, top=10, full=False, path=None, verdict=""):
    out = [verdict] if verdict else []
    for l in lines:
        out.append(f"  {l['status']:9s} {l['name']:22s} {l['detail']}"[:230])
    blockers = [b for l in lines for b in l["blockers"]]
    if blockers:
        out.append(f"first blockers ({min(top, len(blockers))} of {len(blockers)}):")
        out += [f"  - {b}"[:230] for b in (blockers if full else blockers[:top])]
    if path:
        out.append(f"full report: {path}")
    return "\n".join(out)


def cmd_ready(a):
    from . import partscli as PC, parts as P
    spec, cons, facts, mm, ds = PC.load(a)
    board = getattr(a, "board", None)
    sess = PC._session(a, cons, "ready")
    cache = os.path.dirname(os.path.abspath(a.spec))
    pres = P.evaluate(spec, cons, facts=facts, mpn_map=mm, ds_dir=ds, api=not a.no_api, cache_dir=cache, session=sess)
    lines, detail = evaluate(spec, cons, facts, board, ds, sess, pres, allow_undocumented=getattr(a, "allow_undocumented", False))
    if board and os.path.exists(board):
        lines.append(sync_netclasses(board, cons))
    gate_fail = any(l["name"] == "parts gate" and l["status"] == FAIL for l in lines)
    nfail = sum(l["status"] == FAIL for l in lines)
    nopen = len(sess.open) + sum(l.get("open_items", 0) for l in lines)      # OPEN lines with no question behind them count too
    rs = reasons(lines)
    waived = any(l["status"] == WAIVED for l in lines)
    verdict = ("ready: NOT READY (exit 1)" if nfail else "ready: OPEN QUESTIONS (exit 3)" if nopen else
               "ready: NOT READY - parts gate bypassed by --allow-undocumented" if waived else "ready: READY") + \
        f" - {sum(l['status'] == PASS for l in lines)} PASS, {nfail} FAIL, {nopen} open question(s)"
    from . import reportdir as RD
    stem = RD.redirect(a, os.path.splitext(board or a.spec)[0])
    path = stem + ".ready.txt"
    try:
        with open(path, "w") as fh:
            fh.write(format_text(lines, full=True, verdict=verdict) + "\n")
        with open(stem + ".ready.json", "w") as fh:
            json.dump({"verdict": verdict, "checklist": lines, "classes": detail.get("classes", []),
                       "questions": sess.open, "fixed_reasons": detail.get("fixed_reasons", []),
                       "pairs_unclassified": detail.get("pairs_unclassified", [])}, fh, indent=1)
        if detail.get("power_plan"):
            with open(stem + ".power-plan.json", "w") as fh:
                json.dump(detail["power_plan"], fh, indent=1)
        if detail.get("budgets"):
            with open(stem + ".budgets.json", "w") as fh:
                json.dump(detail["budgets"], fh, indent=1, default=list)
    except OSError:
        path = None
    if getattr(a, "json", False):
        print(json.dumps({"verdict": verdict, "checklist": lines, "classes": detail.get("classes", [])}, indent=1))
    elif getattr(a, "quiet", False):
        print(verdict)
    else:
        print(format_text(lines, top=getattr(a, "top", 10) or 10, full=getattr(a, "full_output", False), path=path, verdict=verdict))
    q = sess.gate(want_json=False)
    if board and os.path.exists(board) and not sess.open:
        sess.persist()
    if gate_fail:                              # --allow-incomplete never skips the parts gate; only --allow-undocumented does
        print("parts gate: refusing to continue (--allow-incomplete does not bypass it; --allow-undocumented, recorded, does)")
        raise SystemExit(1)
    other = bool(nfail or nopen)
    if (other and a.allow_incomplete) or (not other and detail["gate"]["parts"] and getattr(a, "allow_undocumented", False)
                                          and not detail["gate"]["geometry"]):
        if board:
            try:
                with open(notready_path(board), "w") as fh:
                    json.dump({"reasons": rs, "command": "ready"}, fh)
            except OSError:
                pass
        print("NOT READY: " + "; ".join(rs)[:400] + "  (continuing by --allow-incomplete / --allow-undocumented; later outputs are stamped, fab/deliver refuse)",
              file=sys.stderr if getattr(a, "json", False) else sys.stdout)
        return
    if board and not (nfail or nopen) and not detail["gate"]["parts"]:
        try:
            os.remove(notready_path(board))
        except OSError:
            pass
    if nfail:
        raise SystemExit(1)
    if q or nopen:
        raise SystemExit(q or QS.EXIT)


def add_parser(sub):
    from . import partscli as PC
    p = sub.add_parser("ready", help="readiness gate before place/route: parts, stackup-computed impedance geometry, rails, anchors")
    p.add_argument("spec", help="spec JSON")
    PC._common(p)
    p.add_argument("--allow-incomplete", dest="allow_incomplete", action="store_true",
                   help="continue (exit 0) with every later output stamped NOT READY; fab/deliver refuse")
    p.add_argument("--allow-undocumented", dest="allow_undocumented", action="store_true",
                   help="the ONLY bypass of the parts gate (unresolved parts): exit 0, every later output stamped NOT READY, fab/deliver refuse")
    p.set_defaults(fn=cmd_ready)
