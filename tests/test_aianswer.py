"""[ai] answer = optional (default) | required: a stage that writes AI packets and finds no answer goes on with its defaults and records
"N packets unanswered, defaults used" (state.json, ledger); required keeps the exit-5 stop; answered packets still apply."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from types import SimpleNamespace
from fluxplace import pipeline as PL, gaps as GP, critroute as CR, netclass as NC, routediag as RD, handroute as HR
from test_pipeline import _board, _args
from test_critroute import _Ctx


def _crit(monkeypatch, seen):
    monkeypatch.setattr(CR, "critical", lambda board, cons, spec, cons_path=None: [{"net": "BT1_RF", "class": "rf", "reason": ""}, {"net": "CAN_CLK", "class": "clk", "reason": ""}])
    monkeypatch.setattr(CR, "packet", lambda board, net, cons, spec, cons_path=None, **kw: {"v": 1, "net": net})
    monkeypatch.setattr(NC, "classify", lambda B, cons, spec: {})
    monkeypatch.setattr(RD, "load", lambda board: {})
    monkeypatch.setattr(HR, "open_nets", lambda board, constraints=None: {"connections": []})
    monkeypatch.setattr(GP, "copper_layers", lambda board: ["F.Cu", "B.Cu"])
    monkeypatch.setattr(CR, "apply", lambda board, out, acts, *a, **k: seen.append(acts) or {"board": board, "kept": [], "tried": 0, "open_nets": [], "lines": []})


def test_critroute_optional_uses_defaults_and_reports_count(tmp_path, monkeypatch):
    seen = []
    _crit(monkeypatch, seen)
    res = CR.stage(_Ctx(str(tmp_path / "w")), 3, "b.kicad_pcb")
    assert res["verdict"] == "pass" and seen == [None] and res["ai_unanswered"]["n"] == 2 and res["ai_unanswered"]["classes"]["default"] == 2
    assert all(os.path.exists(p) for p in res["ai_unanswered"]["packets"])          # packets stay on disk


def test_critroute_required_exits_5_and_answered_applies(tmp_path, monkeypatch):
    seen = []
    _crit(monkeypatch, seen)
    c = _Ctx(str(tmp_path / "w"), {"ai": {"answer": "required"}})
    assert CR.stage(c, 3, "b.kicad_pcb")["verdict"] == "packets" and seen == []
    with open(os.path.join(c.dir(3, "critroute"), "ai", "actions.json"), "w") as fh:
        json.dump({"order": ["CAN_CLK"], "nets": {}}, fh)
    res = CR.stage(c, 3, "b.kicad_pcb")
    assert res["verdict"] == "pass" and "ai_unanswered" not in res and seen[0]["order"] == ["CAN_CLK"]
    c2 = _Ctx(str(tmp_path / "w2"))                                                  # optional + answered: applies, nothing unanswered
    os.makedirs(os.path.join(c2.dir(3, "critroute"), "ai"))
    json.dump({"order": ["CAN_CLK"], "nets": {}}, open(os.path.join(c2.dir(3, "critroute"), "ai", "actions.json"), "w"))
    assert "ai_unanswered" not in CR.stage(c2, 3, "b.kicad_pcb")


def _gate(tmp_path, monkeypatch, cons):
    tmp_path.mkdir(exist_ok=True)
    gd = tmp_path / "gate"; gd.mkdir()
    a = tmp_path / "a.kicad_pcb"; a.write_text("A")
    monkeypatch.setattr(GP, "close", lambda board, d, *x, **k: {"stop": "packets", "best": str(a), "start": {"opens": 3}, "end": {"opens": 3},
                                                                "left": [{"id": "x", "kind": "short", "nets": ["N"], "at": None}],
                                                                "packets": [{"packet": "/p/1.packet.json"}, {"packet": "/p/2.packet.json"}]})
    c = SimpleNamespace(dir=lambda n, name: str(gd), cons_path=None, cons=cons, board_in=str(a))
    return PL._gate_check(c, 1, "pairs", str(a), ["short"], src=str(a))


def test_gate_optional_continues_required_stops(tmp_path, monkeypatch):
    g = _gate(tmp_path, monkeypatch, {})
    assert g["verdict"] == "warn" and g["ai_unanswered"]["n"] == 2 and "2 bounded search" in g["line"] and "STILL OPEN" in g["line"]
    g = _gate(tmp_path / "r", monkeypatch, {"ai": {"answer": "required"}})
    assert g["verdict"] == "packets" and "ai_unanswered" not in g


def _close_flow(tmp_path, monkeypatch, cons_text):
    b = _board(tmp_path)
    monkeypatch.setattr(GP, "GATE_DEFAULTS", {})
    monkeypatch.setattr(PL, "in_fresh_process", lambda mod, fn, *a, **k: {"stop": "packets", "best": a[0], "start": {"opens": 3, "errors": 0}, "end": {"opens": 3, "errors": 0},
                                                                           "attempts": 1, "packets": [{"packet": "/p/1.packet.json"}]} if fn == "close" else {})
    seen = []
    monkeypatch.setitem(PL.FUNCS, "trace-finish", lambda c, n, board: seen.append("tf") or {"verdict": "pass", "board": board})
    a = _args(tmp_path, b, flow="constructive", from_stage="close", to_stage="trace-finish")
    if cons_text:
        cp = tmp_path / "cons.toml"; cp.write_text(cons_text); a.constraints = str(cp)
    return PL.run(a), PL.load_state(a.workdir), seen


def test_close_optional_continues_and_records(tmp_path, monkeypatch):
    rc, st, seen = _close_flow(tmp_path, monkeypatch, None)
    assert rc == 0 and seen == ["tf"] and st["ai_unanswered"]["close"]["n"] == 1
    assert "1 bounded search" in open(os.path.join(st["paths"]["workdir"], "LEDGER.txt")).read()


def test_close_required_exits_5(tmp_path, monkeypatch):
    rc, st, seen = _close_flow(tmp_path, monkeypatch, '[ai]\nanswer = "required"\n')
    assert rc == 5 and seen == []


def test_packet_classes_default_search_owner(tmp_path):
    """B12 / OUTSIDE-REVIEW 9.3: closed by the default plan = default; still open = bounded search, named in the line; needs = owner question."""
    from fluxplace import packets as PK
    pk = {}
    for k, need in (("A", None), ("B", None), ("C", "board-size")):
        pk[k] = str(tmp_path / f"{k}.packet.json")
        json.dump({"net": k, **({"needs": need} if need else {})}, open(pk[k], "w"))
    r = PK.resolve([(pk[k], k) for k in "ABC"], {"B", "C"}, ai_dir=str(tmp_path / "ai"))
    assert r["classes"] == {"default": 1, "search": 1, "owner": 1}
    assert "STILL OPEN" in r["line"] and "B" in r["line"] and [q["id"] for q in r["questions"]] == ["packet:C"]
    assert json.load(open(tmp_path / "ai" / "owner-questions.json"))[0]["id"] == "packet:C"
    assert PK.resolve([(pk["C"], "C")], {"C"}, {"packet:C": "accept-open"})["questions"] == []        # answered: resumes


def test_critroute_owner_packet_suspends_with_question(tmp_path, monkeypatch):
    seen = []
    _crit(monkeypatch, seen)
    monkeypatch.setattr(CR, "packet", lambda board, net, cons, spec, cons_path=None, **kw: {"v": 1, "net": net, "needs": "stackup"})
    monkeypatch.setattr(CR, "apply", lambda board, out, acts, *a, **k: {"board": board, "kept": [], "tried": 2, "open_nets": ["BT1_RF", "CAN_CLK"], "lines": []})
    res = CR.stage(_Ctx(str(tmp_path / "w")), 3, "b.kicad_pcb")
    assert res["verdict"] == "questions" and len(res["ai_unanswered"]["questions"]) == 2 and "packet:BT1_RF" in res["next"][0]
