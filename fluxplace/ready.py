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

from . import questions as QS

PASS, FAIL, OPEN, UNCHECKED = "PASS", "FAIL", "OPEN", "UNCHECKED"


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
        ok = not used.get("netclass_z") or abs(used["netclass_z"][1]) <= tolerance
        st = PASS if ok else FAIL
        geo = f"{L} w {o['width_mm']}" + (f" gap {o['gap_mm']}" if o["gap_mm"] else "") + f" -> {o['z']} ohm, {o['accuracy']}"
        lines.append(_line("class " + cls["name"], st, f"{name}: {geo}{note}",
                           [] if ok else [f"{cls['name']}: the declared netclass is {used['netclass_z'][1]:+.1f}% off {cls['target']:g} ohm "
                                          f"(tolerance {tolerance:g}%); computed {geo}"]))
        detail.append({"class": cls["name"], "kind": cls["kind"], "target": cls["target"], "layer": L, "chosen": o,
                       "alternatives": {k: v for k, v in per_layer.items() if v}, "board": used, "stackup": source})
    return lines, detail


# ------------------------------------------------------------------ the gate
def evaluate(spec, cons, facts, board, ds_dir, sess, parts_res, api=False):
    """-> (lines, detail)."""
    from . import parts as P
    lines, detail = [], {}
    f = parts_res["findings"]
    t = P.totals(f)
    unc = []
    if not parts_res["lookup"]:
        unc.append("distributor/stock NOT CHECKED")
    lines.append(_line("parts", FAIL if t["FAIL"] else PASS,
                       f"{len(parts_res['rows'])} part(s): {t['FAIL']} FAIL, {t['WARN']} WARN" + (f" ({'; '.join(unc)})" if unc else ""),
                       [f"{x['code']} {x['msg']}" for x in P._spread([x for x in f if x["level"] == "FAIL"], 4)] if t["FAIL"] else []))
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
        has_outline = bool((facts or {}).get("outline_bbox"))
        lines.append(_line("outline", PASS if has_outline else FAIL,
                           "Edge.Cuts outline present" if has_outline else "no board outline (Edge.Cuts)",
                           [] if has_outline else ["board has no outline"]))
    else:
        lines.append(_line("board checks", UNCHECKED, "no --board: rails, fixed parts, outline and floors not checked"))
    return lines, detail


def _minpads(cons):
    from . import railroute as RL
    return RL.min_pads(cons)


def reasons(lines):
    return [f"{l['name']}: {l['detail'][:80]}" for l in lines if l["status"] in (FAIL, OPEN)]


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
    lines, detail = evaluate(spec, cons, facts, board, ds, sess, pres)
    nfail = sum(l["status"] == FAIL for l in lines)
    nopen = len(sess.open)
    rs = reasons(lines)
    verdict = ("ready: NOT READY (exit 1)" if nfail else "ready: OPEN QUESTIONS (exit 3)" if nopen else "ready: READY") + \
        f" - {sum(l['status'] == PASS for l in lines)} PASS, {nfail} FAIL, {nopen} open question(s)"
    stem = os.path.splitext(board or a.spec)[0]
    path = stem + ".ready.txt"
    try:
        with open(path, "w") as fh:
            fh.write(format_text(lines, full=True, verdict=verdict) + "\n")
        with open(stem + ".ready.json", "w") as fh:
            json.dump({"verdict": verdict, "checklist": lines, "classes": detail.get("classes", []),
                       "questions": sess.open}, fh, indent=1)
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
    if (nfail or nopen) and a.allow_incomplete:
        if board:
            try:
                with open(notready_path(board), "w") as fh:
                    json.dump({"reasons": rs, "command": "ready"}, fh)
            except OSError:
                pass
        print("NOT READY: " + "; ".join(rs)[:400] + "  (--allow-incomplete: continuing; later outputs are stamped, fab/deliver refuse)")
        return
    if board and not (nfail or nopen):
        try:
            os.remove(notready_path(board))
        except OSError:
            pass
    if nfail:
        raise SystemExit(1)
    if q:
        raise SystemExit(q)


def add_parser(sub):
    from . import partscli as PC
    p = sub.add_parser("ready", help="readiness gate before place/route: parts, stackup-computed impedance geometry, rails, anchors")
    p.add_argument("spec", help="spec JSON")
    PC._common(p)
    p.add_argument("--allow-incomplete", dest="allow_incomplete", action="store_true",
                   help="continue (exit 0) with every later output stamped NOT READY; fab/deliver refuse")
    p.set_defaults(fn=cmd_ready)
