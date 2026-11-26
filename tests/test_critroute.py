"""Phase 1c critroute: critical picks, packet keys and size, action validation, lay/keep rules, stage verdicts. Fakes only."""
import json, os, sys, types
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import critroute as CR, gaps, handroute as HR, netclass as NC, routediag as RD, density as DS


def _conn(net, i, layer="F.Cu"):
    a = {"kind": "pad", "x": 10.0 + i, "y": 20.0, "layer": layer}
    b = {"kind": "pad", "x": 30.0 + i, "y": 25.0, "layer": layer}
    return {"net": net, "a": a, "b": b, "rules": {"class": "Default", "width": 0.2, "clearance": 0.2,
                                                    "via_dia": 0.6, "via_drill": 0.3}}


def test_critical_picks_rf_clock_and_listed_open_only(monkeypatch):
    cls = {"BT1_RF": {"class": "rf", "reason": "rf budget"}, "XTAL_IN": {"class": "clock", "reason": "clk"},
           "GND": {"class": "power", "reason": ""}, "EXTRA": {"class": "plain", "reason": ""},
           "CLOSED_RF": {"class": "rf", "reason": ""}}
    monkeypatch.setattr(NC, "classify", lambda B, cons, spec: cls)
    monkeypatch.setattr(RD, "load", lambda board: {})
    monkeypatch.setattr(HR, "open_nets", lambda board, constraints=None: {"connections": [
        _conn("BT1_RF", 0), _conn("XTAL_IN", 1), _conn("EXTRA", 2), _conn("GND", 3)]})
    cons = {"critroute": {"nets": ["EXTRA", "NOT_THERE"]}}
    got = CR.critical("b.kicad_pcb", cons, None)
    assert [r["net"] for r in got] == ["BT1_RF", "XTAL_IN", "EXTRA"]          # rf, clock, listed; closed rf skipped
    assert [r["class"] for r in got] == ["rf", "clock", "plain"]


def test_packet_keys_and_size(monkeypatch):
    monkeypatch.setattr(NC, "classify", lambda B, cons, spec: {"BT1_RF": {"class": "rf", "reason": "rf budget"}})
    monkeypatch.setattr(RD, "load", lambda board: {})
    conns = [_conn("BT1_RF", i) for i in range(8)]
    monkeypatch.setattr(HR, "open_nets", lambda board, constraints=None: {"connections": conns})
    monkeypatch.setattr(HR, "suggest", lambda board, net, conn=None, constraints=None, **kw: {
        "found": False, "reason": "x" * 500})
    monkeypatch.setattr(DS, "compute", lambda B, cons, spec: {"regions": [
        {"side": "F", "x": 12.0, "y": 18.0, "util": 1.2}, {"side": "B", "x": 500.0, "y": 500.0, "util": 0.9}]})
    monkeypatch.setattr(gaps, "signal_layers", lambda board: ["F.Cu", "B.Cu"])
    p = CR.packet("b.kicad_pcb", "BT1_RF", {}, None)
    assert set(p) == {"v", "net", "class", "reason", "conns", "rules", "layers", "hot", "try", "ask"}
    assert p["v"] == 1 and p["class"] == "rf" and p["ask"] == CR.ASK
    assert set(p["conns"][0]) == {"i", "a", "b", "layers_a", "layers_b"} and len(p["conns"]) == 6
    assert p["rules"] == {"width": 0.2, "clearance": 0.2, "via": [0.6, 0.3]}
    assert p["hot"] == [{"side": "F", "x": 12.0, "y": 18.0, "util": 1.2}]          # far region dropped
    assert p["try"]["found"] is False and len(p["try"]["reason"]) <= 200
    assert len(json.dumps(p).encode()) <= CR.PACKET_MAX


def test_load_actions_drops_bad_entries(tmp_path):
    f = tmp_path / "actions.json"
    f.write_text(json.dumps({
        "order": ["BT1_RF", "NOPE"],
        "nets": {"BT1_RF": {"layers": ["B.Cu", "Bad.Cu"], "avoid": [[1, 2, 3], [1, 2], [1, 2, 0], [5, 5, 1, 9]],
                            "why": "keep pocket"}},
        "tokens": 42}), encoding="utf-8")
    a = CR.load_actions(str(f), nets=["BT1_RF", "XTAL_IN"], layers=["F.Cu", "B.Cu"])
    assert a["order"] == ["BT1_RF"]
    assert a["nets"]["BT1_RF"]["layers"] == ["B.Cu"]
    assert a["nets"]["BT1_RF"]["avoid"] == [[1.0, 2.0, 3.0]]
    assert a["tokens"] == 42
    f.write_text(json.dumps({"nets": {"GHOST": {"layers": ["F.Cu"]}}}))
    assert CR.load_actions(str(f), nets=["BT1_RF"], layers=["F.Cu"])["nets"] == {}
    assert CR.load_actions(str(tmp_path / "missing.json")) is None
    many = {"nets": {"BT1_RF": {"avoid": [[i, i, 1] for i in range(9)]}}}
    f.write_text(json.dumps(many))
    assert len(CR.load_actions(str(f), nets=["BT1_RF"])["nets"]["BT1_RF"]["avoid"]) == CR.MAX_AVOID


def _fake_board(monkeypatch, opens_good, shorts_bad=()):
    """open_nets reports one BT1_RF connection until a board is laid that closes it; measure reads the fake state."""
    closed = set()
    lays = []

    def open_nets(board, constraints=None, **kw):
        conns = [] if board in closed else [_conn("BT1_RF", 0)]
        return {"connections": conns}

    def suggest(board, net, conn=None, constraints=None, layers=None, avoid=None, **kw):
        return {"found": True, "path": "P", "length_mm": 4.0, "reason": ""}

    def lay(board, net, path, out=None, constraints=None, lock=False, **kw):
        lays.append({"lock": lock, "out": out, "board": board})
        with open(out, "w") as fh:
            fh.write("x")
        if opens_good:
            closed.add(out)
        return {"accepted": True}

    def measure(path, kicad_cli="kicad-cli"):
        if path in closed:
            return {"opens": 0, "shorts": 1 if path in shorts_bad else 0}
        return {"opens": 1, "shorts": 0}

    monkeypatch.setattr(HR, "open_nets", open_nets)
    monkeypatch.setattr(HR, "suggest", suggest)
    monkeypatch.setattr(HR, "lay", lay)
    monkeypatch.setattr(gaps, "measure", measure)
    monkeypatch.setattr(gaps, "signal_layers", lambda board: ["F.Cu", "B.Cu"])
    return lays


def test_apply_keeps_locked_path_that_lowers_opens(tmp_path, monkeypatch):
    lays = _fake_board(monkeypatch, opens_good=True)
    board = str(tmp_path / "b.kicad_pcb")
    open(board, "w").write("base")
    out = str(tmp_path / "out" / "board.kicad_pcb")
    os.makedirs(os.path.dirname(out))
    acts = {"order": [], "nets": {"BT1_RF": {"layers": ["B.Cu"], "avoid": [(1.0, 2.0, 3.0)]}}, "tokens": 0}
    res = CR.apply(board, out, acts, None, {}, str(tmp_path / "attempts.jsonl"),
                   crit=[{"net": "BT1_RF", "class": "rf", "reason": ""}])
    assert lays and lays[0]["lock"] is True
    assert res["board"] == out and os.path.exists(out) and res["open_nets"] == []
    assert res["kept"][0]["net"] == "BT1_RF"
    lines = [json.loads(l) for l in open(tmp_path / "attempts.jsonl")]
    assert lines[0]["stage"] == "critroute" and lines[0]["method"] == "suggest+lay" and lines[0]["result"] == "kept"


def test_apply_drops_path_that_raises_shorts(tmp_path, monkeypatch):
    _fake_board(monkeypatch, opens_good=True)
    seen = {}
    monkeypatch.setattr(HR, "suggest", lambda board, net, conn=None, constraints=None, layers=None, avoid=None, **kw:
                        seen.update(layers=layers, avoid=avoid) or {"found": True, "path": "P", "length_mm": 1.0})
    monkeypatch.setattr(gaps, "measure", lambda path, kicad_cli="kicad-cli": (
        {"opens": 0, "shorts": 2} if "critroute" in os.path.basename(path) else {"opens": 1, "shorts": 0}))
    board = str(tmp_path / "b.kicad_pcb")
    open(board, "w").write("base")
    out = str(tmp_path / "board.kicad_pcb")
    acts = {"order": ["BT1_RF"], "nets": {"BT1_RF": {"layers": ["B.Cu"], "avoid": [(1.0, 2.0, 3.0)]}}}
    res = CR.apply(board, out, acts, None, {}, str(tmp_path / "attempts.jsonl"),
                   crit=[{"net": "BT1_RF", "class": "rf", "reason": ""}])
    assert res["kept"] == [] and res["board"] == board and not os.path.exists(out)
    assert seen["layers"] == ["B.Cu"] and seen["avoid"] == [(1.0, 2.0, 3.0)]
    assert not [f for f in os.listdir(tmp_path) if "critroute" in f]          # scratch board removed
    assert json.loads(open(tmp_path / "attempts.jsonl").readline())["result"] == "dropped"


class _Ctx:
    def __init__(self, d, cons=None):
        self.d, self.cons, self.spec, self.cons_path = d, cons or {}, None, None

    def dir(self, n, name):
        p = os.path.join(self.d, f"{n}-{name}")
        os.makedirs(p, exist_ok=True)
        return p


def test_stage_packets_without_actions_and_defaults_in_off_mode(tmp_path, monkeypatch):
    crit = [{"net": "BT1_RF", "class": "rf", "reason": ""}]
    monkeypatch.setattr(CR, "critical", lambda board, cons, spec, cons_path=None: crit)
    monkeypatch.setattr(CR, "packet", lambda board, net, cons, spec, cons_path=None, **kw: {"v": 1, "net": net})
    monkeypatch.setattr(NC, "classify", lambda B, cons, spec: {})
    monkeypatch.setattr(RD, "load", lambda board: {})
    monkeypatch.setattr(HR, "open_nets", lambda board, constraints=None: {"connections": []})
    monkeypatch.setattr(gaps, "copper_layers", lambda board: ["F.Cu", "B.Cu"])
    seen = {}

    def fake_apply(board, out, acts, cons_path, cons, attempts, log=None, crit=None):
        seen["acts"] = acts
        return {"board": board, "kept": [], "tried": 0, "open_nets": [], "lines": []}
    monkeypatch.setattr(CR, "apply", fake_apply)
    c = _Ctx(str(tmp_path / "w"), {"ai": {"answer": "required"}})
    res = CR.stage(c, 3, "b.kicad_pcb")
    assert res["verdict"] == "packets" and "acts" not in seen
    assert os.path.exists(os.path.join(c.dir(3, "critroute"), "ai", "BT1_RF.packet.json"))
    c2 = _Ctx(str(tmp_path / "w2"), {"ai": {"mode": "off"}})
    res = CR.stage(c2, 3, "b.kicad_pcb")
    assert seen["acts"] is None and res["verdict"] == "pass"




def test_refused_lay_is_a_dropped_try_not_a_measure_of_the_empty_slot(tmp_path, monkeypatch):
    """W30 root cause: a refused lay writes nothing, so `tmp` stayed the empty mkstemp slot and measure() of it read
    "Unexpected end of input" (6 of 12 tries). A refusal is its own result and the slot is never measured."""
    _fake_board(monkeypatch, opens_good=True)
    monkeypatch.setattr(HR, "suggest", lambda board, net, conn=None, constraints=None, layers=None, avoid=None, **kw:
                        {"found": True, "path": "P", "length_mm": 1.0})
    monkeypatch.setattr(HR, "lay", lambda *a, **kw: {"accepted": False, "reason": "new DRC violation(s): 1"})
    measured = []
    monkeypatch.setattr(gaps, "measure", lambda path, kicad_cli="kicad-cli": measured.append(path) or
                        ({"opens": 1, "shorts": 0} if "critroute" not in path else (_ for _ in ()).throw(
                            RuntimeError("Unexpected end of input"))))
    board = str(tmp_path / "b.kicad_pcb")
    open(board, "w").write("base")
    out = str(tmp_path / "board.kicad_pcb")
    res = CR.apply(board, out, {"order": ["BT1_RF"], "nets": {}}, None, {}, str(tmp_path / "attempts.jsonl"),
                   crit=[{"net": "BT1_RF", "class": "rf", "reason": ""}])
    assert not [p for p in measured if "critroute" in p]
    rec = json.loads(open(tmp_path / "attempts.jsonl").readline())
    assert rec["result"] == "refused" and "new DRC violation" in rec["why"]
    assert res["kept"] == [] and not [f for f in os.listdir(tmp_path) if "critroute" in f]
