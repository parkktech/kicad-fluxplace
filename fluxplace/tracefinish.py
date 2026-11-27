"""Trace-finishing stage driver (TRACE-FINISH-SPEC 3.3, 4; RECIPE-TRACE-FINISH T1a).

T2 (B8): the stage ends with a guarded clean pass (`clean`): zero-length segments, duplicate segments, stacked vias and dangling
segments / vias are removed category by category, each kept only when the DRC key multiset (opens per net) does not get worse;
the ends KiCad flags "track not centred on via" are then moved onto the via centre (`centre`, snapiter fallbacks, same guard).

T1a: frame only. F0 base record (HARD rows that have a measure now), the corner / bend / junction measures of
tfmeasure, the score, events, and finish.json. No pass edits copper yet: F8 = F0 again and the output board is a byte
copy of the input. Disabled ([finish] enabled not true): the stage is a no-op and writes nothing.
"""
import collections
import json
import os
import shutil
import threading
import time

from . import events

RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trace_finish_rules.json")
STAGE = "trace-finish"
HEARTBEAT_S = 2.0
H_MIN_WIDTH = 0.10          # H2 fab floor (PCBWay / JLCPCB); spec section 1


def load_rules():
    with open(RULES_PATH, encoding="utf-8") as f:
        return json.load(f)["rules"]


def _drc(path, **kw):
    from . import drcguard
    return drcguard.drc_json(path, **kw)


def _heartbeat(stop, label, stage=STAGE):
    """progress every HEARTBEAT_S while the DRC child runs (the only long step)."""
    while not stop.wait(HEARTBEAT_S):
        events.emit("progress", stage, phase=label, note="drc running")


def _beating(label, fn, stage=STAGE):
    """fn() with a progress event every HEARTBEAT_S while it runs (DRC, pair lengths and the manifest seed are the long steps)."""
    stop = threading.Event()
    events.emit("progress", stage, phase=label, note="start")
    t = threading.Thread(target=_heartbeat, args=(stop, label, stage), daemon=True)
    t.start()
    try:
        return fn()
    finally:
        stop.set()
        t.join(timeout=HEARTBEAT_S + 1)


def _drc_base(board, kicad_cli):
    """One DRC run (drcguard.drc_json, a temp copy): key multiset, violations and opens."""
    from . import drcguard
    d = _beating("F0 drc", lambda: _drc(board, kicad_cli=kicad_cli))
    keys, (vio, unc) = drcguard.keys_from_report(d, unconnected=False)
    for u in d.get("unconnected_items", []):                     # opens per NET: kicad-cli names another item pair for the same open each run
        for n in drcguard.unc_nets(u) or ["?"]:
            keys[("Unet", (n,))] += 1
    return {"keys": {json.dumps(list(k[0:1]) + [list(k[1])]): n for k, n in keys.items()},
            "types": collections.Counter(k[0] for k in keys.elements()), "violations": vio, "opens": unc,
            "report": d.get("violations", [])}


def _count_type(types, name):
    return sum(n for t, n in types.items() if t == name)


HARD_DETAIL = ("rows", "pairs", "unmeasured", "over", "graded", "sites", "drc")   # measure detail kept in finish.json legality


def _hard_row(hid, m, src, limit=None):
    """a HARD row from a measure result {base, ...} or {not_measured}."""
    if "not_measured" in m:
        return {"id": hid, "base": None, "limit": None, "source": None, "ok": None, "not_measured": m["not_measured"]}
    return {"id": hid, "base": m["base"], "limit": m.get("limit", limit), "source": src, "ok": None,
            **{k: m[k] for k in HARD_DETAIL if k in m}}


def f0(board, B, cons, spec, cls, kicad_cli="kicad-cli", X=None, audit=None, phase="F0"):
    """Base record: HARD rows (H1-H10) and the sites of every PRACTICE rule. `audit` (pairaudit result) is passed back in for F8
    so the 2-minute kicad-cli length run happens once (no pass edits copper in T1b)."""
    from . import tfmeasure as TM
    base = _drc_base(board, kicad_cli)
    types = base["types"]
    X = X or TM.context(B, cons, spec, board)
    if audit is None:
        audit = _beating("F0 pair lengths", lambda: TM.pair_audit(B, X, cons))
    widths = collections.defaultdict(lambda: None)
    for s in B["segs"]:
        c = cls.get(s["net"], "GEN")
        widths[c] = s["w"] if widths[c] is None else min(widths[c], s["w"])
    events.emit("progress", STAGE, phase=f"{phase} measure", note="HARD rows")
    hard = [{"id": "H1", "base": _count_type(types, "clearance"), "limit": 0, "source": "DRC", "ok": None},
            {"id": "H2", "base": {k: v for k, v in widths.items()}, "limit": H_MIN_WIDTH, "source": "FAB",
             "ok": all(v >= H_MIN_WIDTH for v in widths.values() if v is not None)},
            _hard_row("H3", TM.h3(B, X), "FAB"),
            _hard_row("H4", TM.h4(B, X, _count_type(types, "hole_to_hole")), "FAB"),
            {"id": "H5", "base": _count_type(types, "copper_edge_clearance"), "limit": 0, "source": "DRC", "ok": None},
            _hard_row("H6", TM.h6(B, X, board), "FAB"),
            _hard_row("H7", TM.h7(B, X), "DATASHEET"),
            _hard_row("H8", TM.h8(audit), "STD"),
            _hard_row("H9", TM.h9(audit, cons), "STD"),
            {"id": "H10", "base": {"opens": base["opens"], "drc_keys": sum(types.values())},
             "limit": "base record F0", "source": "DRC", "ok": None}]
    sites = {}
    sites["C1"] = TM.c1_sites(B)
    sites["C2"] = TM.c2_sites(B)
    sites["C3"] = TM.c3_sites(B)
    sites["C4"] = TM.c4_sites(B)
    sites["C5"] = TM.c5_sites(B, cls)
    sites["C11"] = TM.c11_sites(X, audit)
    sites["C13"] = TM.c13_sites(B)
    sites["C14"] = TM.c14_sites(B)
    sites["C15"] = TM.c15_sites(B)
    sites["M1"] = TM.m1_sites(B, float((cons.get("fab") or {}).get("acid_trap_deg", 60.0)))
    sites["M9"] = TM.m9_sites(B)
    sites["M2"] = TM.drc_sites(base["report"], "copper_sliver")
    sites["M4"] = TM.drc_sites(base["report"], "isolated_copper")
    for i, (rid, fn) in enumerate(sorted(TM.SITES.items())):
        events.emit("progress", STAGE, phase=f"{phase} measure {rid}", note="rule", pct=round(100.0 * i / len(TM.SITES), 1))
        sites[rid] = fn(B, X)
    return {"hard": hard, "drc": {str(k): v for k, v in base["keys"].items()}, "sites": sites, "facts": TM.facts(B, X),
            "violations": base["violations"], "opens": base["opens"], "_audit": audit, "_X": X}


def practice_rows(rec, cls, rules):
    """One PRACTICE row per rule: score per class, counts, and the rules with no measure yet listed by name."""
    rows, not_measured = [], []
    for r in rules:
        if r["tag"] != "PRACTICE":
            continue
        sites = rec["sites"].get(r["id"])
        if sites is None:
            not_measured.append(r["id"])
            continue
        by_cls = collections.defaultdict(list)
        for s in sites:
            by_cls[cls.get(s.get("net"), "GEN")].append(s["state"])
        from . import tfmeasure as TM
        if not sites:
            rows.append({"id": r["id"], "ok": None, "value": None, "limit": 0, "cls": "-", "already": 0, "applied": 0,
                         "not_possible": 0, "refused": 0, "open": 0, "report_only": r.get("report_only", False)})
        for c, states in sorted(by_cls.items()):
            val, elig = TM.score_of(states)
            rows.append({"id": r["id"], "ok": None, "value": val, "limit": elig, "cls": c,
                         "already": states.count("already"), "applied": states.count("applied"),
                         "not_possible": states.count("not_possible"), "refused": states.count("refused"),
                         "open": states.count("open"), "report_only": r.get("report_only", False),
                         "reasons": dict(collections.Counter(s.get("reason") for s in sites if s["state"] == "not_possible"
                                                             and cls.get(s.get("net"), "GEN") == c))})
    return rows, not_measured


def _emit_criteria(phase, rec, rows, scores, worse=()):
    for h in rec["hard"]:
        ok = (h["id"] not in worse) if phase == "F8" else h.get("ok")
        text = h.get("not_measured") or f"{h['id']} base {h.get('base')} limit {h.get('limit')}"
        events.emit("criteria", STAGE, phase=phase, hard=True, id=h["id"], value=h.get("base"), text=text, base=h.get("base"),
                    limit=h.get("limit"), ok=ok, note=h.get("not_measured", ""))
    for r in rows:
        events.emit("criteria", STAGE, phase=phase, hard=False, id=r["id"], ok=None, value=r["value"],
                    limit=r["limit"], cls=r["cls"], already=r["already"], applied=r["applied"],
                    not_possible=r["not_possible"], refused=r["refused"], open=r["open"])
    for k, v in scores.items():
        events.emit("criteria", STAGE, phase=phase, hard=False, id="SCORE", ok=None, value=v, cls=k)


def _worse(f0_rec, f8_rec):
    """HARD rows (base numbers) that got worse from F0 to F8: the stage gate is 'not worse'."""
    worse = []
    a = {h["id"]: h.get("base") for h in f0_rec["hard"]}
    b = {h["id"]: h.get("base") for h in f8_rec["hard"]}
    for k in ("H1", "H3", "H4", "H5", "H6", "H7", "H8", "H9"):
        if isinstance(a.get(k), int) and isinstance(b.get(k), int) and b[k] > a[k]:
            worse.append(k)
    if isinstance(a.get("H2"), dict) and isinstance(b.get("H2"), dict) and \
            any(v is not None and b["H2"].get(c) is not None and b["H2"][c] < v for c, v in a["H2"].items()):
        worse.append("H2")
    if isinstance(a.get("H10"), dict) and isinstance(b.get("H10"), dict) and (
            b["H10"]["opens"] > a["H10"]["opens"] or
            any(n > f0_rec["drc"].get(k, 0) for k, n in f8_rec["drc"].items())):
        worse.append("H10")
    return worse


# ---- T2 clean pass (B8) ------------------------------------------------------------------------------------------------------

CLEAN_CATS = ("zero", "duplicate", "stacked", "dangling")      # order matters: dangling last (it sees the copper the others left)


def _clean_worker(src, dst, cats, dangling=()):
    """Fresh-process worker of `clean`: remove the items of the categories `cats` from `src`, save as `dst`.
    -> {category: count, "nets": [net names of the removed items]}. `dangling`: the item uuids KiCad's DRC reports as track_dangling /
    via_dangling (KiCad's own connectivity decides, never a geometry of ours). Items are only marked while the passes run and removed at the end (a board.Remove mid-way decays pcbnew's bindings)."""
    import pcbnew
    b = pcbnew.LoadBoard(src)
    got = {c: 0 for c in cats}
    if b is None:                                       # a file pcbnew cannot read has nothing to clean (and nothing is written)
        return got
    gone = {}                                           # uuid -> item

    def uid(t):
        return t.m_Uuid.AsString()

    def items():
        trk = [t for t in b.GetTracks() if uid(t) not in gone]
        return ([t for t in trk if t.Type() == pcbnew.PCB_TRACE_T], [t for t in trk if t.Type() == pcbnew.PCB_VIA_T])

    def kill(t, cat):
        gone[uid(t)] = t
        got[cat] += 1

    tracks, vias = items()
    if "zero" in cats:
        for t in tracks:
            if t.GetStart() == t.GetEnd():
                kill(t, "zero")
        tracks, vias = items()
    if "duplicate" in cats:
        seen = {}
        for t in tracks:
            k = (t.GetNetCode(), t.GetLayer(), frozenset(((t.GetStart().x, t.GetStart().y), (t.GetEnd().x, t.GetEnd().y))))
            keep = seen.get(k)
            if keep is None:
                seen[k] = t
                continue
            loser = keep if (t.GetWidth(), t.IsLocked()) > (keep.GetWidth(), keep.IsLocked()) else t
            if loser is keep:
                seen[k] = t
            kill(loser, "duplicate")
        tracks, vias = items()
    if "stacked" in cats:
        seen = {}
        for v in vias:
            k = (v.GetNetCode(), v.GetPosition().x, v.GetPosition().y)
            keep = seen.get(k)
            if keep is None:
                seen[k] = v
                continue
            big = lambda x: (x.GetWidth(pcbnew.F_Cu), x.GetDrill(), x.IsLocked())
            loser = keep if big(v) > big(keep) else v
            if loser is keep:
                seen[k] = v
            kill(loser, "stacked")
    if "dangling" in cats:
        want = set(dangling)
        for t in tracks + vias:
            if uid(t) in want:
                kill(t, "dangling")
    got["nets"] = sorted({t.GetNetname() for t in gone.values()})
    for t in gone.values():
        b.Remove(t)
    if gone:
        pcbnew.SaveBoard(dst, b)
    return got


def _guard_ok(d0, d1):
    """True when the DRC report d1 is not worse than d0: no key (opens counted per net) that d0 does not have at least as often."""
    from . import drcguard as DG
    return not DG.new_keys(DG.keys_per_net(d0), DG.keys_per_net(d1))


DANGLING_TYPES = ("track_dangling", "via_dangling")


def _dangling_uuids(d, typ):
    return sorted({it["uuid"] for v in d.get("violations", []) if v.get("type") == typ for it in v.get("items", []) if it.get("uuid")})


def clean(board, out, kicad_cli="kicad-cli", cats=CLEAN_CATS):
    """T2 guarded clean pass: each category in `cats` is applied to the accepted board and kept only when `_guard_ok` (no DRC key that the
    accepted board does not have as often, opens counted per net); `out` is written (a copy of `board` when nothing was kept).
    "dangling" removes what KiCad's DRC reports as track_dangling, then via_dangling, repeated while something is removed (a removed
    stub can expose the next one), each batch its own guarded step.
    -> {"removed": {cat: n}, "refused": {cat: reason}, "nets": [nets touched], "drc_before": n, "drc_after": n, "report": DRC of out}."""
    from .freshproc import in_fresh_process
    cur = out + ".clean-cur.kicad_pcb"
    shutil.copyfile(board, cur)
    _pro_copy(board, cur)
    d_cur = _drc(cur, kicad_cli=kicad_cli)
    res = {"removed": {}, "refused": {}, "nets": [], "drc_before": len(d_cur.get("violations", []))}
    trial = out + ".clean-trial.kicad_pcb"

    def step(cat, uuids=()):
        """one guarded batch -> True when kept (the accepted board and its report advance)."""
        nonlocal d_cur
        r = in_fresh_process("fluxplace.tracefinish", "_clean_worker", cur, trial, [cat], uuids)
        n = r[cat]
        if not n or not os.path.exists(trial):
            return False
        _pro_copy(board, trial)
        d_new = _drc(trial, kicad_cli=kicad_cli)
        ok = _guard_ok(d_cur, d_new)
        if ok:
            res["removed"][cat] = res["removed"].get(cat, 0) + n
            res["nets"] = sorted(set(res["nets"]) | set(r["nets"]))
            shutil.copyfile(trial, cur)
            d_cur = d_new
        else:
            res["refused"][cat] = res["refused"].get(cat, 0) + n
        _rm(trial)
        return ok

    try:
        for cat in cats:
            if cat != "dangling":
                step(cat)
                continue
            for _ in range(3):
                kept = False
                for typ in DANGLING_TYPES:
                    ids = _dangling_uuids(d_cur, typ)
                    kept = (ids and step("dangling", ids)) or kept
                if not kept:
                    break
        shutil.copyfile(cur, out)
        _pro_copy(board, out)
        res["drc_after"] = len(d_cur.get("violations", []))
        res["report"] = d_cur
    finally:
        _rm(cur)
        _rm(trial)
    return res


def _changed_nets_run(a, b):
    from .freshproc import in_fresh_process
    return in_fresh_process("fluxplace.tracefinish", "_changed_nets", a, b)


def _changed_nets(a, b):
    """Fresh-process worker: names of the nets whose tracks / vias differ (position, layer, width) between boards `a` and `b`."""
    import pcbnew

    def sig(path):
        bd = pcbnew.LoadBoard(path)
        out = set()
        for t in bd.GetTracks():
            via = t.Type() == pcbnew.PCB_VIA_T
            out.add((t.GetNetname(), via, t.GetLayer() if not via else -1, t.GetStart().x, t.GetStart().y,
                     t.GetEnd().x if not via else 0, t.GetEnd().y if not via else 0, t.GetWidth() if not via else t.GetWidth(pcbnew.F_Cu)))
        return out
    sa, sb = sig(a), sig(b)
    sa, sb = {(x[0], x[1], x[2], frozenset(((x[3], x[4]), (x[5], x[6]))), x[7]) for x in sa}, {(x[0], x[1], x[2], frozenset(((x[3], x[4]), (x[5], x[6]))), x[7]) for x in sb}
    return sorted({x[0] for x in sa ^ sb})


def _centred(d):
    return sum(1 for v in d.get("violations", []) if v.get("type") == "track_not_centered_on_via")


def centre(board, out, d0, kicad_cli="kicad-cli", log=print):
    """Ends KiCad flags `track_not_centered_on_via` in the DRC report `d0` of `board`: snapiter's guarded fallbacks in order (end onto the
    via centre, then joiners), each DRC-guarded by snapiter (a via whose fix creates a new key is left alone). `out` is written.
    -> {"before": n, "after": n, "moves": n, "report": DRC report of out}."""
    from . import snapiter as SN
    shutil.copyfile(board, out)
    _pro_copy(board, out)
    n0, moved, d = _centred(d0), 0, d0
    if n0:
        for fn in (SN.centre_run, SN.via_run, SN.centre_run, SN.joiner_run):
            nxt = out + ".step.kicad_pcb"
            _pro_copy(board, nxt)
            r = fn(out, nxt, log=log, **({"locked_ok": True} if fn is not SN.centre_run else {}))
            if r.get("moved") and os.path.exists(nxt):
                shutil.copyfile(nxt, out)
                moved += r["moved"]
            _rm(nxt)
            d = _drc(out, kicad_cli=kicad_cli)
            if not _centred(d):
                break
    return {"before": n0, "after": _centred(d), "moves": moved, "report": d}


def _pro_copy(board, dst):
    s, d = os.path.splitext(board)[0] + ".kicad_pro", os.path.splitext(dst)[0] + ".kicad_pro"
    if os.path.exists(s) and os.path.abspath(s) != os.path.abspath(d):
        shutil.copyfile(s, d)


def _rm(path):
    for f in (path, os.path.splitext(path)[0] + ".kicad_pro", os.path.splitext(path)[0] + ".kicad_prl"):
        if os.path.exists(f):
            os.remove(f)


def stage(c, n, board):
    """Stage entry (pipeline FUNCS['trace-finish']). Disabled -> pass, nothing written. Enabled -> F0, F8, outputs."""
    cons = getattr(c, "cons", None) or {}
    fin = cons.get("finish") or {}
    if fin.get("enabled") is not True:
        return {"verdict": "pass", "line": "trace-finish: skipped ([finish] enabled is not set)", "board": board}
    from . import routediag, tfmeasure as TM
    d = c.dir(n, STAGE)
    kicad_cli = fin.get("kicad_cli", "kicad-cli")
    rules = load_rules()
    B = routediag.load(board, project=None)
    cls = TM.classes(B, cons, getattr(c, "spec", None))
    rec0 = f0(board, B, cons, getattr(c, "spec", None), cls, kicad_cli)
    rows0, nm = practice_rows(rec0, cls, rules)
    sc0 = TM.class_scores(rows0, rules)
    _emit_criteria("F0", rec0, rows0, sc0)
    clean_rec, centre_rec = {"removed": {}, "refused": {}}, {"before": 0, "after": 0, "moves": 0}
    cleaned = os.path.join(d, "clean.kicad_pcb")
    out = os.path.join(d, "board.kicad_pcb")
    events.emit("progress", STAGE, phase="T2 clean", note="guarded pass")
    clean_rec = _beating("T2 clean", lambda: clean(board, cleaned, kicad_cli))
    d_clean = clean_rec.pop("report", None) or {}
    centre_rec = _beating("T2 centre", lambda: centre(cleaned, out, d_clean, kicad_cli))
    centre_rec.pop("report", None)
    released = sorted(set(clean_rec.get("nets") or []) | (set(_changed_nets_run(cleaned, out)) if centre_rec["moves"] else set()))
    _rm(cleaned)
    changed = bool(clean_rec["removed"]) or bool(centre_rec["moves"])
    if changed:
        B8 = routediag.load(out, project=None)
        rec8 = f0(out, B8, cons, getattr(c, "spec", None), cls, kicad_cli, phase="F8")     # new copper: measured afresh
    else:
        rec8 = f0(board, B, cons, getattr(c, "spec", None), cls, kicad_cli, X=rec0["_X"], audit=rec0["_audit"], phase="F8")
    rows8, _ = practice_rows(rec8, cls, rules)
    sc8 = TM.class_scores(rows8, rules)
    worse = _worse(rec0, rec8)
    _emit_criteria("F8", rec8, rows8, sc8, worse)
    fj = {"stage": STAGE, "passes": {"F0": "done", "T2": "done", "F8": "done"}, "clean": clean_rec, "centre": centre_rec,
          "not_built": ["F1", "F2", "F3", "F4", "F5", "F6", "F7"],
          "legality": rec8["hard"], "score": rows8, "not_measured_rules": nm, "facts": rec8["facts"],
          "degenerate": rec8["facts"]["degenerate"], "worse_hard": worse,
          "class_score": {"F0": sc0, "F8": sc8}, "sites_total": {k: len(v) for k, v in rec8["sites"].items()},
          "sites": {k: [x for x in v if x["state"] != "already"][:200] for k, v in rec8["sites"].items()}}
    fpath = os.path.join(d, "finish.json")
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(fj, f, indent=1, default=str)
    events.artifact("report", fpath, "finish.json")
    verdict = "fail" if worse else "pass"
    line = (f"trace-finish: {verdict} (F0 base: opens {rec0['opens']}, DRC violations {rec0['violations']}; "
            f"not_measured rules {len(nm)})" + (f" ILLEGAL: {','.join(worse)}" if worse else "")
            + f"; T2 removed {sum(clean_rec['removed'].values())} {clean_rec['removed']}, off-centre {centre_rec['before']} -> {centre_rec['after']}"
            + f" score board {sc0.get('board')} -> {sc8.get('board')}")
    return {"verdict": verdict, "line": line, "board": out, "released": released}
