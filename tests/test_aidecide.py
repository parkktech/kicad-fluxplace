"""w94-aidecide: the AI decider (decider.py) at the question choke point (questions.Session.ask) and the [pinswap]
sealed-pad pin choice (pinswap.py).  The model is a stub command; no network, no real model."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import decider as DC, pinswap as PS, questions as QS, routediag as RD

Q = {"id": "rule-floors", "topic": "rule-floors", "found": "no floors", "default": "relaxed", "settle": "fab sheet",
     "options": [{"choice": "standard", "consequence": "0.127 mm"}, {"choice": "relaxed", "consequence": "0.2 mm"}]}


def stub(answer):
    """[ai] constraints whose model command prints `answer` (a dict is JSON-encoded) after reading the prompt."""
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return {"ai": {"decide": "on", "decide_cmd": [sys.executable, "-c", "import sys; sys.stdin.read(); print(sys.argv[1])", text]}}


def test_reasoned_decision_is_logged_mirrored_and_cached(tmp_path, monkeypatch):
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    d = DC.decide(Q, ["fab sheet: 0.127 mm"], stub({"choice": "standard", "rationale": "fab allows it", "evidence": ["fab sheet"]}), str(tmp_path))
    assert d["choice"] == "standard" and d["reasoned"] and d["by"] == "ai" and d["evidence"] == ["fab sheet"]
    rows = [json.loads(l) for l in open(tmp_path / DC.LOG)]
    assert rows[-1]["hash"] == d["hash"] and rows[-1]["rationale"] == "fab allows it"
    assert '"rule-floors" = "standard"' in (tmp_path / DC.ANSWERS).read_text()
    # same question again, model now broken: answered from the log, no call
    d2 = DC.decide(Q, ["fab sheet: 0.127 mm"], {"ai": {"decide": "on", "decide_cmd": ["/nonexistent/model"]}}, str(tmp_path))
    assert d2["choice"] == "standard" and d2["by"] == "cache"


def test_retry_on_the_next_model_when_no_evidence_is_cited(tmp_path, monkeypatch):
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    code = ("import json, os, sys; sys.stdin.read(); m = os.environ['FLUXPLACE_AI_MODEL']; "
            "print(json.dumps({'result': json.dumps({'choice': 'relaxed' if m == 'haiku' else 'standard', 'rationale': m, "
            "'evidence': [] if m == 'haiku' else ['fab sheet']})}))")
    cons = {"ai": {"decide": "on", "decide_cmd": [sys.executable, "-c", code]}}
    d = DC.decide(Q, ["fab sheet"], cons, str(tmp_path))
    assert d["choice"] == "standard" and d["model"] == "sonnet" and d["evidence"] == ["fab sheet"] and d["cached"] is False
    d2 = DC.decide(Q, ["fab sheet"], cons, str(tmp_path))
    rows = [json.loads(l) for l in open(tmp_path / DC.LOG)]
    assert d2["cached"] and rows[-1]["cached"] and rows[-1]["options"] == ["standard", "relaxed"]


def test_offline_and_off_menu_fall_back_to_default_not_reasoned(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_AI_OFFLINE", "1")
    d = DC.decide(Q, [], stub({"choice": "standard"}), str(tmp_path))
    assert d["choice"] == "relaxed" and not d["reasoned"] and d["rationale"].startswith("default, not reasoned")
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE")
    d = DC.decide(Q, [], stub('{"choice": "advanced"} then {"choice": "nope"}'), str(tmp_path))
    assert d["choice"] == "relaxed" and not d["reasoned"]
    assert DC.parse('noise {"x": 1} {"choice": "standard", "rationale": "r"}', ["standard"])["choice"] == "standard"


def test_session_uses_the_decider_instead_of_stopping(tmp_path, monkeypatch):
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    opts = {o["choice"]: o["consequence"] for o in Q["options"]}
    S = QS.Session(str(board), stub({"choice": "standard", "rationale": "r"}), {}, False, "t")
    assert S.ask("rule-floors", "rule-floors", "no floors", opts, "relaxed", "fab") == "standard"
    out = []
    assert S.gate(out=out.append) == 0 and not S.open and S.used == {"rule-floors": "standard"}
    assert any(l.startswith("DECIDED: rule-floors = standard (ai)") for l in out)
    # offline: the default, not reasoned -> ASSUMED (fab refuses), still no stop
    monkeypatch.setenv("FLUXPLACE_AI_OFFLINE", "1")
    S = QS.Session(str(board), stub({"choice": "standard"}), {}, False, "t")
    assert S.ask("rule-floors", "rule-floors", "no floors", opts, "relaxed", "fab") == "standard"     # the log answers it
    (tmp_path / "o").mkdir()
    board = tmp_path / "o" / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    S = QS.Session(str(board), stub({"choice": "standard"}), {}, False, "t")
    assert S.ask("rule-floors", "rule-floors", "no floors", opts, "relaxed", "fab") == "relaxed"
    assert S.gate(out=lambda _l: None) == 0 and S.assumed == ["rule-floors"] and not S.used
    # decider off: unchanged (open question, exit 3)
    S = QS.Session(str(board), {}, {}, False, "t")
    S.ask("rule-floors", "rule-floors", "no floors", opts, "relaxed", "fab")
    assert S.gate(out=lambda _l: None) == QS.EXIT


SIG = [f"S{i}" for i in range(10)]


def sealed_board(tmp):
    """west column of U1 walled in (4 pins sealed), east column 7 nets + 3 free pads (18-20) that escape east."""
    east = [f"E{i}" for i in range(7)] + ["", "", ""]
    body = PB.column_ic("U1", 20.0, 40.0, SIG, east=east) + PB.wall("R1", 18.5 - 0.4 - 1.0 - 0.2, 40.0, 14)
    body += PB.fp("J1", 60.0, 40.0, [PB.pad(str(i + 1), 0, i * 1.5, s, 1.0, 1.0) for i, s in enumerate(SIG + east[:7])])
    return PB.write(tmp, body)


def group(**kw):
    return {"pinswap": {"gpio": dict({"pins": [f"U1.{i}" for i in range(1, 21)]}, **kw)}}


def test_pinswap_lists_only_free_escapable_pads(tmp_path):
    B = RD.load(sealed_board(tmp_path))
    rows = PS.probe(B, group(functions={"U1.18": "GPIO18"}), None)
    assert [r["pad"] for r in rows] == ["3", "5", "7", "9"]
    assert all({c["pad"] for c in r["cands"]} == {"18", "19", "20"} for r in rows)
    assert rows[0]["functions"]["18"] == "GPIO18"
    spec = {"components": [{"ref": "U1"}], "nets": {"X": [["U1", "19"]]}}          # the spec already uses pad 19
    assert all({c["pad"] for c in r["cands"]} == {"18", "20"} for r in PS.probe(B, group(), spec))
    assert PS.probe(B, {"pinswap": {"bad": {"pins": ["U1.1", "R1.1"]}}}, None) == []   # a group spans one part only


def test_pinswap_decided_by_ai_applies_to_spec_and_board(tmp_path, monkeypatch):
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    path = sealed_board(tmp_path)
    B = RD.load(path)
    cons = dict(group(), **stub({"choice": "U1.20", "rationale": "GPIO, free, escapes"}))
    S = QS.Session(path, cons, {}, False, "route")
    QS.check_pinswap(S, B, cons)
    # the first sealed pad takes U1.20; the decider's later answers name a pad no longer offered -> default keep (not reasoned)
    assert S.used == {"pinswap:U1.3": "U1.20"} and set(S.assumed) == {"pinswap:U1.5", "pinswap:U1.7", "pinswap:U1.9"}
    assert "U1.20" not in json.dumps([q for q in S.decided if q["id"] != "pinswap:U1.3"])
    assert any("function" in e for e in S.evidence["pinswap:U1.3"])
    spec = {"components": [{"ref": "U1"}], "nets": {"S2": [["U1", "3"], ["J1", "3"]]}}
    _c, spec2 = S.apply({}, spec)
    assert spec2["nets"]["S2"] == [["U1", "20"], ["J1", "3"]] and spec["nets"]["S2"][0] == ["U1", "3"]
    assert PS.moves(dict(S.used, **{"pinswap:U1.5": "keep"})) == [("U1", "3", "20")]


def test_apply_board_moves_the_pad_net(tmp_path):
    import pytest
    pytest.importorskip("pcbnew")
    path = sealed_board(tmp_path)
    out = str(tmp_path / "o" / "b.kicad_pcb")
    assert PS.apply_board(path, out, [("U1", "3", "20"), ("U1", "5", "11")]) == ["S2"]   # pad 11 carries E0: refused
    pads = {p["num"]: p["net"] for p in RD.load(out)["parts"]["U1"]["pads"]}
    assert pads["3"] == "" and pads["20"] == "S2" and pads["5"] == "S4"


def test_pipeline_entry_decides_and_renets_into_the_work_dir(tmp_path, monkeypatch):
    import types
    import pytest
    pytest.importorskip("pcbnew")
    from fluxplace import pipeline as P
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    path = sealed_board(tmp_path)
    work = str(tmp_path / "work")
    spec = {"components": [{"ref": "U1", "pinmap": {"IO2": "3", "IO20": "20"}}, {"ref": "J1"}],
            "nets": {"S2": [["U1", "IO2"], ["J1", "3"]]}}
    sp = tmp_path / "spec.json"
    sp.write_text(json.dumps(spec, indent=1) + "\n")
    c = types.SimpleNamespace(cons=dict(group(), **stub({"choice": "U1.20", "rationale": "r"})), answers={}, state={}, spec=spec,
                              spec_path=str(sp), a=types.SimpleNamespace(board=path))
    st = {}
    out = P._pinswap_entry(c, work, path, st)
    assert out == os.path.join(work, "0-pinswap", "board.kicad_pcb") and st["pinswap"] == [("U1", "3", "20")]
    assert c.answers == {"pinswap:U1.3": "U1.20"} and c.state["route_scope"]["nets"] == ["S2"]
    assert os.path.exists(os.path.join(work, DC.LOG)) and not os.path.exists(tmp_path / DC.LOG)
    pads = {p["num"]: p["net"] for p in RD.load(out)["parts"]["U1"]["pads"]}
    assert pads["3"] == "" and pads["20"] == "S2"
    # the spec follows the board (pin name of the new pad), the old spec is kept, the schematic gets a change list
    assert json.loads(sp.read_text())["nets"]["S2"] == [["U1", "IO20"], ["J1", "3"]] and (tmp_path / "spec.json.bak-pinswap").exists()
    rows = json.load(open(os.path.join(work, "0-pinswap", "schematic-changes.json")))["moves"]
    assert rows == [{"ref": "U1", "net": "S2", "from_pad": "3", "to_pad": "20", "from_pin": "IO2", "to_pin": "IO20"}]
    assert P._pinswap_entry(c, work, out, {}) == out           # re-run on the moved board: nothing sealed in the group, no change


def test_ai_rung_asks_the_model_and_applies_through_the_guard(tmp_path, monkeypatch):
    from fluxplace import critroute, gaps as GP, handroute as HR
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    monkeypatch.setattr(HR, "open_nets", lambda *a, **k: {"connections": []})
    seen = []
    monkeypatch.setattr(GP, "load_ai_action", lambda path, *a, **k: dict(json.load(open(path)), tokens=0))
    monkeypatch.setattr(GP, "_apply_ai", lambda base, out, a, ctx, sid: (seen.append(a["actions"]), ("applied", False, "gave up"))[1])
    gap = {"id": "open-free:SIG2:1:2", "kind": "open-free", "class": "plain", "nets": ["SIG2"]}
    ai_dir = tmp_path / "ai"

    def ctx(cons):
        return {"cons": cons, "cons_path": None, "work": str(tmp_path), "ai_dir": str(ai_dir), "ai_used": {}, "ai_mode": "packet",
                "budget_s": 60, "attempts": str(tmp_path / "att.jsonl")}
    env = {"actions": [{"do": "give_up", "why": "sealed"}], "why": "no lane"}
    res = GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "out.kicad_pcb"), [gap], ctx(stub(env)))
    assert res["status"] == "applied" and res["gap_id"] == gap["id"] and seen == [env["actions"]]
    row = [json.loads(l) for l in open(ai_dir / DC.LOG)][-1]
    assert row["id"] == "ai:" + GP.ai_key(gap) and row["envelope"]["actions"] == env["actions"] and not row["cached"]
    assert "ai:" not in (ai_dir / DC.ANSWERS).read_text()
    # no model (and no cached envelope in a fresh folder): the packet is left for an agent, exactly as before
    monkeypatch.setenv("FLUXPLACE_AI_OFFLINE", "1")
    ai_dir = tmp_path / "ai2"
    res = GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "out.kicad_pcb"), [gap], ctx(stub(env)))
    assert res == {"status": "packet", "n": 1}
    assert not (ai_dir / (critroute._safe(GP.ai_key(gap))[:80] + ".action.json")).exists()


# ---- the other human gates (w94 b-e) ----

def test_closure_pair_option_is_decided(tmp_path, monkeypatch):
    import types
    from fluxplace import closure as CL
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    o = {"type": "pair", "pad": "U1.3", "nets": ["D_P", "D_N"], "controlled_mm": 1.2, "controlled_vias": 0, "needs": "owner decision: re-lay D_P",
         "mm": 2.0, "cost": 1002.0, "then": {"type": "track", "mm": 2.0}}
    ctx = types.SimpleNamespace(cons=stub({"choice": "re-lay", "rationale": "1.2 mm is inside the skew budget", "evidence": ["x"]}))
    CL._decide_pair(ctx, str(tmp_path / "b.kicad_pcb"), "S2", o, [o])
    assert o["decision"]["choice"] == "re-lay" and CL._opt_line(o).startswith("AI DECISION (ai): re-lay")
    o.pop("decision")
    CL._decide_pair(types.SimpleNamespace(cons={}), str(tmp_path / "b.kicad_pcb"), "S2", o, [o])
    assert "decision" not in o and CL._opt_line(o).startswith("NEEDS OWNER DECISION")


def test_waiver_only_with_a_cited_datasheet_line(tmp_path, monkeypatch):
    from fluxplace import partdocs as PD, review as RV
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    (tmp_path / "ABC1.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(PD, "page_text", lambda pdf, pages=None: "Features\fOperating temperature -40 to +125 C")
    f = {"level": RV.FAIL, "code": "PART_TEMP_RANGE", "msg": "U1 ABC1 rated -20 C", "refs": ["U1"]}
    facts = {"mpn_map": {"U1": "ABC1"}, "board_path": str(tmp_path / "b.kicad_pcb")}
    cite = "ABC1.pdf#p2"
    out = RV.ai_waive([dict(f)], facts, stub({"choice": "waive", "rationale": "rated -40 C", "evidence": [cite + ": Operating temperature"]}), str(tmp_path))
    assert out[0]["level"] == RV.INFO and out[0]["code"] == "AI_WAIVED" and cite in out[0]["msg"]
    out = RV.ai_waive([dict(f, refs=["U1"], msg="U1 ABC1 rated -20 C.")], facts, stub({"choice": "waive", "rationale": "fine", "evidence": ["trust me"]}), str(tmp_path))
    assert out[0]["level"] == RV.FAIL and "AI waiver refused" in out[0]["msg"]
    assert RV.ai_waive([dict(f)], facts, {}, str(tmp_path)) == [f]                         # decider off: unchanged


def test_dropin_owner_stop_settled_by_the_decider(tmp_path, monkeypatch):
    from fluxplace import partsgate as PG
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    sp = tmp_path / "spec.json"
    sp.write_text(json.dumps({"components": [{"ref": "U1", "mpn": "OLD"}], "nets": {}}, indent=1) + "\n")
    seen = []

    def validate(*a, ai_ok=False):
        seen.append(ai_ok)
        return ("apply", [], {"refs": ["U1"], "mpn": "NEW"}) if ai_ok else ("owner", ["replaces OLD with NEW, not a verified drop-in: Vmax"], None)
    monkeypatch.setattr(PG, "validate", validate)
    prop = [{"ref": "U1", "mpn": "NEW"}]
    rep = PG.apply(str(sp), None, prop, stub({"choice": "apply", "rationale": "same pinout", "evidence": ["NEW.pdf#p3"]}), {}, None, None, None)
    assert seen == [False, True] and rep["applied"][0]["ai"] == "same pinout" and json.loads(sp.read_text())["components"][0]["mpn"] == "NEW"
    (tmp_path / "h").mkdir()                                  # a fresh folder: the first decision is cached beside the first spec
    sp2 = tmp_path / "h" / "spec.json"
    sp2.write_text(json.dumps({"components": [{"ref": "U1", "mpn": "OLD"}], "nets": {}}, indent=1) + "\n")
    rep = PG.apply(str(sp2), None, prop, stub({"choice": "hold", "rationale": "Vmax lower"}), {}, None, None, None)
    assert rep["owner"] and not rep["applied"]


def test_owner_packets_never_suspend(tmp_path, monkeypatch):
    import types
    from fluxplace import packets as PK, pipeline as P
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    q = PK.question({"key": "NET1", "needs": "board-size", "packet": str(tmp_path / "NET1.packet.json")})
    c = types.SimpleNamespace(cons=stub({"choice": "change", "rationale": "grow 2 mm"}), answers={})
    assert P._decide_packets(c, str(tmp_path), "route", [q]) == [] and c.answers == {"packet:NET1": "accept-open"}
    assert json.loads(open(tmp_path / DC.LOG).readline())["choice"] == "change"


def test_live_gate_decided_without_waiting(tmp_path, monkeypatch):
    from fluxplace import liveview as LV
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    g = {"candidates": [{"cand": "c1", "criteria": {"opens": 2}}, {"cand": "c2", "criteria": {"opens": 0}}]}
    r = LV._ai_gate("place", None, g, "stop", stub({"choice": "c2", "rationale": "0 opens", "evidence": ["candidate c2"]}), str(tmp_path))
    assert r == {"by": "ai", "action": "approve", "choice": "c2", "text": "0 opens", "reasoned": True}
    monkeypatch.setenv("FLUXPLACE_AI_OFFLINE", "1")
    r = LV._ai_gate("route", "PASS", g, "stop", stub({}), str(tmp_path / "x"))
    assert r["action"] == "stop" and r["by"] == "default" and not r["reasoned"]


def test_k21_act_passes_effort_and_survives_text_tokens(monkeypatch, tmp_path):
    seen = {}

    def fake(text, cons, m, **kw):
        seen.update(kw)
        return '{"actions":[{"do":"path"}],"tokens":"open-pour stitch"}', ""
    monkeypatch.setattr(DC, "call_model", fake)
    env = DC.act({"board": None}, {"ai": {"tools": False}}, str(tmp_path), "k")
    assert env["tokens"] == 0 and seen["effort"] == "medium"
    monkeypatch.setattr(DC, "call_model", lambda *a, **k: (seen.update(k) or ('{"actions":[{"do":"path"}]}', "")))
    DC.act({"board": None, "x": 1}, {"ai": {"tools": False, "effort": "low"}}, str(tmp_path), "k2")
    assert seen["effort"] == "low"
