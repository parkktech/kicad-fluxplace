"""Trace-finishing stage driver (TRACE-FINISH-SPEC 3.3, 4; RECIPE-TRACE-FINISH T1a).

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


def _heartbeat(stop, label):
    """progress every HEARTBEAT_S while the DRC child runs (the only long step)."""
    while not stop.wait(HEARTBEAT_S):
        events.emit("progress", STAGE, phase=label, note="drc running")


def _beating(label, fn):
    """fn() with a progress event every HEARTBEAT_S while it runs (DRC and the kicad-cli pair lengths are the long steps)."""
    stop = threading.Event()
    events.emit("progress", STAGE, phase=label, note="start")
    t = threading.Thread(target=_heartbeat, args=(stop, label), daemon=True)
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
    keys, (vio, unc) = drcguard.keys_from_report(d)
    return {"keys": {json.dumps(list(k[0:1]) + [list(k[1])]): n for k, n in keys.items()},
            "types": collections.Counter(k[0] for k in keys.elements()), "violations": vio, "opens": unc,
            "report": d.get("violations", [])}


def _count_type(types, name):
    return sum(n for t, n in types.items() if t == name)


def _hard_row(hid, m, src, limit=None):
    """a HARD row from a measure result {base, ...} or {not_measured}."""
    if "not_measured" in m:
        return {"id": hid, "base": None, "limit": None, "source": None, "ok": None, "not_measured": m["not_measured"]}
    return {"id": hid, "base": m["base"], "limit": m.get("limit", limit), "source": src, "ok": None}


def f0(board, B, cons, spec, cls, kicad_cli="kicad-cli", X=None, audit=None):
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
    events.emit("progress", STAGE, phase="F0 measure", note="HARD rows")
    hard = [{"id": "H1", "base": _count_type(types, "clearance"), "limit": 0, "source": "DRC", "ok": None},
            {"id": "H2", "base": {k: v for k, v in widths.items()}, "limit": H_MIN_WIDTH, "source": "FAB",
             "ok": all(v >= H_MIN_WIDTH for v in widths.values() if v is not None)},
            _hard_row("H3", TM.h3(B, X), "FAB"),
            {"id": "H4", "base": _count_type(types, "hole_to_hole"), "limit": 0, "source": "DRC", "ok": None},
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
    for rid, fn in sorted(TM.SITES.items()):
        events.emit("progress", STAGE, phase=f"F0 measure {rid}", note="rule")
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


def _emit_criteria(phase, rec, rows, scores):
    for h in rec["hard"]:
        events.emit("criteria", STAGE, phase=phase, hard=True, id=h["id"], base=h.get("base"), limit=h.get("limit"),
                    ok=h.get("ok"), note=h.get("not_measured", ""))
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
    rec8 = f0(board, B, cons, getattr(c, "spec", None), cls, kicad_cli, X=rec0["_X"], audit=rec0["_audit"])   # F8 = F0 again (no pass edits copper yet)
    rows8, _ = practice_rows(rec8, cls, rules)
    sc8 = TM.class_scores(rows8, rules)
    _emit_criteria("F8", rec8, rows8, sc8)
    worse = _worse(rec0, rec8)
    out = os.path.join(d, "board.kicad_pcb")
    shutil.copyfile(board, out)
    fj = {"stage": STAGE, "passes": {"F0": "done", "F8": "done"},
          "not_built": ["F1", "F2", "F3", "F4", "F5", "F6", "F7"],
          "legality": rec8["hard"], "score": rows8, "not_measured_rules": nm, "facts": rec8["facts"],
          "degenerate": rec8["facts"]["degenerate"], "worse_hard": worse,
          "class_score": {"F0": sc0, "F8": sc8},
          "sites": {k: [x for x in v if x["state"] != "already"][:200] for k, v in rec8["sites"].items()}}
    fpath = os.path.join(d, "finish.json")
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(fj, f, indent=1, default=str)
    events.artifact("report", fpath, "finish.json")
    verdict = "fail" if worse else "pass"
    line = (f"trace-finish: {verdict} (F0 base: opens {rec0['opens']}, DRC violations {rec0['violations']}; "
            f"not_measured rules {len(nm)})" + (f" ILLEGAL: {','.join(worse)}" if worse else "")
            + f" score board {sc0.get('board')} -> {sc8.get('board')}")
    return {"verdict": verdict, "line": line, "board": out}
