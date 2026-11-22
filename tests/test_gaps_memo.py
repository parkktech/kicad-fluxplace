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
