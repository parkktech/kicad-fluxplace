"""tune budget, stall and clean exit: the best accepted board is written, temp and lock files are removed, and the
unfixable pairs (legs on different layer sequences / via counts) are named instead of tuned."""
import os
import signal
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fluxplace import tune as TU
from test_lengthmatch import build_board, NAMES, _stackup, LM

pcbnew = pytest.importorskip("pcbnew")


def _fake_drc(monkeypatch, on_call=None, new_key_after=None):
    calls = []

    def fake(path, kicad_cli="kicad-cli", timeout=600):
        calls.append(path)
        if on_call:
            on_call(len(calls))
        keys = Counter({("v", (str(len(calls)),)): 1}) if new_key_after and len(calls) > new_key_after else Counter()
        return keys, (0, 0)
    monkeypatch.setattr(TU, "drc_report", fake)
    monkeypatch.setattr(TU, "classify_pairs", lambda b, p: {})
    return calls


def _leftovers(tmp_path):
    return sorted(p for p in os.listdir(tmp_path) if "work" in p or ".try" in p or p.endswith(".lck") or ".drc" in p)


def _ntracks(path):
    return len(list(pcbnew.LoadBoard(path).GetTracks()))


def _clock():
    t = [0.0]
    return t, (lambda: t[0])


def test_budget_stop_writes_best_accepted_board_and_cleans_up(tmp_path, monkeypatch):
    src = build_board(str(tmp_path / "in.kicad_pcb"))
    t, clock = _clock()
    _fake_drc(monkeypatch, on_call=lambda n: t.__setitem__(0, 1000.0) if n == 2 else None)   # budget gone after 1st accepted bump
    out, res = str(tmp_path / "out.kicad_pcb"), {}
    rep = TU.tune_pairs(src, out, {"DATA_1": "DATA_0"}, 0.2, budget=TU.Budget(100, clock=clock), result=res, log=lambda m: None)
    assert res["stopped"] == "budget" and res["pairs"][0]["status"] == "over" and res["pairs"][0]["reason"] == "budget"
    assert _ntracks(out) > _ntracks(src)                       # the accepted step is in the output
    assert _leftovers(tmp_path) == []
    assert rep[0][0] == "DATA_0"


def test_sigterm_during_drc_leaves_a_usable_out_and_no_temp_files(tmp_path, monkeypatch):
    src = build_board(str(tmp_path / "in.kicad_pcb"))
    old = signal.getsignal(signal.SIGTERM)
    _fake_drc(monkeypatch, on_call=lambda n: os.kill(os.getpid(), signal.SIGTERM) if n == 3 else None)
    out, res = str(tmp_path / "out.kicad_pcb"), {}
    TU.tune_pairs(src, out, {"DATA_1": "DATA_0"}, 0.2, budget=TU.Budget(None), result=res, log=lambda m: None)
    assert res["stopped"] == "signal" and os.path.exists(out)
    assert _ntracks(out) > _ntracks(src)                       # two accepted bumps before the signal
    assert _leftovers(tmp_path) == [] and signal.getsignal(signal.SIGTERM) is old


def test_nothing_accepted_copies_the_input(tmp_path, monkeypatch):
    src = build_board(str(tmp_path / "in.kicad_pcb"))
    t, clock = _clock()
    _fake_drc(monkeypatch, on_call=lambda n: t.__setitem__(0, 1e6))     # baseline DRC eats the budget
    out = str(tmp_path / "out.kicad_pcb")
    res = {}
    TU.tune_pairs(src, out, {"DATA_1": "DATA_0"}, 0.2, budget=TU.Budget(10, clock=clock), result=res, log=lambda m: None)
    assert res["stopped"] == "budget" and _ntracks(out) == _ntracks(src) and _leftovers(tmp_path) == []
    assert res["accepted"] == 0 and open(out, "rb").read() == open(src, "rb").read()


def test_stall_stops_a_pair_after_n_rejected_tries(tmp_path, monkeypatch):
    src = build_board(str(tmp_path / "in.kicad_pcb"))
    calls = _fake_drc(monkeypatch, new_key_after=1)                     # every try adds a new DRC key: rejected
    out, res = str(tmp_path / "out.kicad_pcb"), {}
    TU.tune_pairs(src, out, {"DATA_1": "DATA_0"}, 0.2, stall=3, result=res, log=lambda m: None)
    assert len(calls) == 1 + 3                                          # baseline + 3 tries
    assert res["stopped"] == "stall" and res["pairs"][0]["reason"] == "stall"
    assert _ntracks(out) == _ntracks(src) and _leftovers(tmp_path) == []


def test_needs_reroute_is_reported_not_tuned(tmp_path, monkeypatch):
    src = build_board(str(tmp_path / "in.kicad_pcb"))
    calls = _fake_drc(monkeypatch)
    monkeypatch.setattr(TU, "classify_pairs", lambda b, p: {"DATA_0": "layers F.Cu vs F.Cu>B.Cu; vias 0 vs 1"})
    out, res = str(tmp_path / "out.kicad_pcb"), {}
    TU.tune_pairs(src, out, {"DATA_1": "DATA_0"}, 0.2, result=res, log=lambda m: None)
    assert res["pairs"][0]["status"] == "needs_reroute" and "vias 0 vs 1" in res["pairs"][0]["reason"]
    assert res["needs_reroute"] == 1 and len(calls) == 1                # only the baseline DRC ran


def test_classify_pairs_layer_and_via_mismatch_and_open_leg(monkeypatch):
    from fluxplace import pairaudit as PA, routediag as RD
    prof = {"A_P": {"path": True, "layers": ["F.Cu"], "vias": 0, "length": 9},
            "A_N": {"path": True, "layers": ["F.Cu", "In1.Cu", "F.Cu"], "vias": 2, "length": 9},
            "B_P": {"path": True, "layers": ["F.Cu"], "vias": 0, "length": 9},
            "B_N": {"path": True, "layers": ["F.Cu"], "vias": 0, "length": 9},
            "C_P": {"path": True, "layers": ["F.Cu"], "vias": 0, "length": 9},
            "C_N": {"path": False, "layers": [], "vias": 0, "length": 0}}
    monkeypatch.setattr(RD, "load", lambda p: {})
    monkeypatch.setattr(PA, "profile", lambda B, n: prof[n])
    out = TU.classify_pairs("x", {"A_N": "A_P", "B_N": "B_P", "C_N": "C_P"})
    assert set(out) == {"A_P", "C_P"} and "vias 0 vs 2" in out["A_P"] and "leg open" in out["C_P"]


def test_budget_default_scales_with_pairs():
    assert TU.default_budget(10) == 10 * TU.PER_PAIR_S and TU.default_budget(0) == TU.PER_PAIR_S


def test_tune_match_budget_stop_reports_not_reached(tmp_path, monkeypatch):
    src = build_board(str(tmp_path / "in.kicad_pcb"))
    t, clock = _clock()
    _fake_drc(monkeypatch, on_call=lambda n: t.__setitem__(0, 1e6) if n == 2 else None)
    g = LM.groups({"match": {"DATA": {"nets": ["DATA_\\d+"], "tolerance_mm": 0.2, "amplitude_mm": 0.6}}})[0]
    lens = lambda b, gg: LM.lengths_from_board(b, LM.members(gg, NAMES), _stackup(), [], False)
    tgt = max(v["mm"] for v in lens(pcbnew.LoadBoard(src), g).values())
    out, res = str(tmp_path / "out.kicad_pcb"), {}
    rep = TU.tune_match(src, out, [g], lens, {"DATA": (tgt, 0.2)}, budget=TU.Budget(50, clock=clock), result=res, log=lambda m: None)
    assert res["stopped"] == "budget" and any(r["status"] == "not_reached" for r in rep)
    assert len(rep) == len(NAMES) and _leftovers(tmp_path) == []


def test_cli_skips_zone_refill_when_no_track_was_accepted(monkeypatch):
    import cli
    from fluxplace import opengate as OG
    calls = []
    monkeypatch.setattr(OG, "refill", lambda path: calls.append(path))
    cli._tune_refill("unchanged", {"accepted": 0, "stopped": None})
    assert calls == []
    cli._tune_refill("changed", {"accepted": 1, "stopped": None})
    assert calls == ["changed"]
