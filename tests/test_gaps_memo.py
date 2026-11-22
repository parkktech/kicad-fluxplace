"""PHASE3I B3: persistent memo across passes, stall watchdog, yield ranking. Synthetic: the close loop's measure and
rung calls are stubbed (no router, no kicad-cli); the board is a real pcbnew fixture for the copper keys."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pcbnew = pytest.importorskip("pcbnew")
from fluxplace import gaps as G
from test_planetie import make

GAP = {"id": "g1", "kind": "open-gap", "class": "plain", "nets": ["N1"], "at": [10.0, 10.0], "layer": None}


def _stub(monkeypatch):
    calls = []
    monkeypatch.setattr(G, "_remeasure", lambda path, cli="kicad-cli": {"opens": 1, "shorts": 0, "errors": 0,
                                                                        "gaps": [dict(GAP)], "opens_by_net": {}})
    monkeypatch.setattr(G, "_classify_into", lambda *a, **k: None)
    monkeypatch.setattr(G, "_protect", lambda *a, **k: set())
    monkeypatch.setattr(G, "_call_rung", lambda name, base, out, gaps, ctx: calls.append(name) or {})
    return calls


def _close(tmp_path, board, cons=None):
    return G.close(board, str(tmp_path / "work"), cons=cons or {}, runs=str(tmp_path / "runs.jsonl"),
                   attempts=str(tmp_path / "attempts.jsonl"), log=lambda *a: None)


def test_second_pass_replays_memo_and_makes_no_rung_calls(tmp_path, monkeypatch):
    calls = _stub(monkeypatch)
    board = make(str(tmp_path / "in.kicad_pcb"))
    _close(tmp_path, board)
    first = len(calls)
    assert first == len(G.DISPATCH["open-gap"])
    calls.clear()
    _close(tmp_path, board)
    assert calls == []


def test_stall_watchdog_stops_a_pass_with_no_accept(tmp_path, monkeypatch):
    calls = _stub(monkeypatch)
    board = make(str(tmp_path / "in.kicad_pcb"))
    res = _close(tmp_path, board, cons={"flow": {"stall_min": 0}})
    assert res["stop"] == "stalled" and calls == []


def test_rung_order_follows_measured_yield_and_promotes_ai(tmp_path):
    assert G._rank("open-gap", ["joingaps", "patch"], {"open-gap": {"joingaps": [4, 0], "patch": [4, 2]}}) == ["patch", "joingaps"]
    rungs = G.DISPATCH["open-free"]
    order = G._rank("open-free", rungs, {"open-free": {"prune": [3, 0]}})
    assert order.index("ai") < order.index("krt_region")


def test_window_hash_of_an_empty_window_is_the_empty_set(tmp_path):
    import hashlib
    board = make(str(tmp_path / "in.kicad_pcb"))
    assert G.window_hash(board, (500.0, 500.0)) == hashlib.sha1(b"[]").hexdigest()


# ---- PHASE3I F3: window key from the net pads, one board load, error memo, stall packet, answers-only yields ----
import json


def _edit(src, out, add=None, drop=None):
    """copy of a real board with one GND track added, or the tracks matching `drop` removed"""
    b = pcbnew.LoadBoard(src)
    if add:
        x0, y0, x1, y1, net = add
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x0), pcbnew.FromMM(y0)))
        t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
        t.SetWidth(pcbnew.FromMM(0.2))
        t.SetLayer(b.GetLayerID("F.Cu"))
        t.SetNet(b.FindNet(net))
        b.Add(t)
    if drop:
        for t in list(b.GetTracks()):
            if t.Type() == pcbnew.PCB_TRACE_T and drop(t):
                b.Remove(t)
    b.Save(out)
    return out


GND_GAP = {"id": "g", "kind": "open-gap", "class": "plain", "nets": ["GND"], "at": [10.0, 10.0]}


def test_window_key_ignores_copper_far_from_the_gap(tmp_path):
    base = make(str(tmp_path / "a.kicad_pcb"))
    far = _edit(base, str(tmp_path / "far.kicad_pcb"), add=(30, 20, 32, 20, "GND"))
    assert G.window_hash(base, GND_GAP) == G.window_hash(far, GND_GAP)


def test_window_key_sees_a_track_that_crosses_the_window(tmp_path):
    base = make(str(tmp_path / "a.kicad_pcb"))
    cross = _edit(base, str(tmp_path / "x.kicad_pcb"), add=(2, 10, 18, 10, "GND"))    # both ends outside the box
    assert G.window_hash(base, GND_GAP) != G.window_hash(cross, GND_GAP)


def test_window_key_sees_a_blocker_removed_on_the_net_path(tmp_path):
    base = _edit(make(str(tmp_path / "a.kicad_pcb")), str(tmp_path / "b.kicad_pcb"), add=(10, 10, 20, 10, "GND"))
    freed = _edit(base, str(tmp_path / "c.kicad_pcb"), drop=lambda t: abs(t.GetEnd().x - pcbnew.FromMM(20)) < 10)
    assert G.window_hash(base, GND_GAP) != G.window_hash(freed, GND_GAP)


def test_window_hash_loads_the_board_once_for_many_gaps(tmp_path, monkeypatch):
    board = make(str(tmp_path / "a.kicad_pcb"))
    loads, real = [], pcbnew.LoadBoard
    monkeypatch.setattr(pcbnew, "LoadBoard", lambda p: loads.append(p) or real(p))
    gaps = [{"id": str(i), "kind": "pair-open", "nets": ["GND"], "at": [10.0, 10.0]} for i in range(50)]
    assert len({G.window_hash(board, g) for g in gaps}) == 1
    assert len(loads) == 1


def _stub_pair(monkeypatch, ai_packet=False):
    gap = {"id": "p1", "kind": "pair-open", "class": "hs-pair", "nets": ["A_P", "A_N"], "at": None, "layer": None, "sev": 3}
    calls = []
    monkeypatch.setattr(G, "_remeasure", lambda path, cli="kicad-cli": {"opens": 1, "shorts": 0, "errors": 0,
                                                                        "gaps": [dict(gap)], "opens_by_net": {}})
    monkeypatch.setattr(G, "_classify_into", lambda *a, **k: None)
    monkeypatch.setattr(G, "_protect", lambda *a, **k: set())

    def rung(name, base, out, gaps, ctx):
        calls.append(name)
        if name == "ai" and ai_packet:
            return {"status": "packet"}
        if name == "pairgroup":
            raise RuntimeError("router timed out")
        return {}
    monkeypatch.setattr(G, "_call_rung", rung)
    return calls


def test_error_result_is_not_persisted_in_the_memo(tmp_path, monkeypatch):
    _stub_pair(monkeypatch)
    board = make(str(tmp_path / "in.kicad_pcb"))
    work = tmp_path / "work"
    G.close(board, str(work), cons={"ai": {"mode": "off"}}, runs=str(tmp_path / "runs.jsonl"),
            attempts=str(tmp_path / "att.jsonl"), log=lambda *a: None, kinds=["pair-open"])
    rows = [json.loads(x) for x in open(tmp_path / "att.jsonl")]
    assert any(x["method"] == "pairgroup" and x["result"] == "error" for x in rows)
    assert all(k[0] != "pairgroup" for k in json.load(open(work / "gaps.json"))["memo"])   # the error key is not saved


def test_stall_writes_the_ai_packet_and_stops_packets(tmp_path, monkeypatch):
    calls = _stub_pair(monkeypatch, ai_packet=True)
    board = make(str(tmp_path / "in.kicad_pcb"))
    work = tmp_path / "work"
    r = G.close(board, str(work), cons={"flow": {"stall_min": 1e-9}}, runs=str(tmp_path / "runs.jsonl"),
                attempts=str(tmp_path / "att.jsonl"), log=lambda *a: None, kinds=["pair-open"])
    assert r["stop"] == "packets" and r["exit"] == 5 and calls == ["ai"]
    assert "ai" not in json.load(open(work / "gaps.json"))["yields"].get("pair-open", {})   # R17: a packet is no try
