"""PHASE3I B1: stage gates. A gated stage (pairs) that leaves high-speed pair gaps stops the flow with exit 5 and a
packet; the next stage does not start; the route stage never takes a gated handed-over pair as a plain net."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import pipeline as PL, gaps as GP, pairfb as PFB
from test_pipeline import _board, _args


def test_gate_defaults_and_override():
    assert GP.gate_kinds({}, "pairs") == ["pair-open", "pair-asym", "pair-skew"]
    assert GP.gate_kinds({}, "route") == []
    assert GP.gate_kinds({"flow": {"gates": {"pairs": []}}}, "pairs") == []


def test_gate_scope_pair_kinds_only_for_gated_pairs():
    g_hs = {"kind": "pair-open", "class": "hs-pair"}
    g_lo = {"kind": "pair-open", "class": "pair"}
    g_drc = {"kind": "open-gap", "class": "plain"}
    kinds = ["pair-open"]
    assert GP.in_scope(g_hs, kinds) and not GP.in_scope(g_lo, kinds) and not GP.in_scope(g_drc, kinds)
    assert GP.in_scope(g_lo, None)


def test_flow_stops_at_pairs_gate_with_exit_5(tmp_path, monkeypatch):
    b = _board(tmp_path)
    seen = []
    monkeypatch.setitem(PL.FUNCS, "pairs", lambda c, n, board: {"verdict": "pass", "line": "pairs ok", "board": board})
    monkeypatch.setitem(PL.FUNCS, "tune", lambda c, n, board: seen.append("tune") or {"verdict": "pass", "board": board})
    monkeypatch.setattr(PL, "_gate_check", lambda c, n, name, board, kinds: {
        "verdict": "packets", "board": board, "line": f"gate {name}: packets", "next": ["gap pair:A/B"],
        "gate": {"kinds": kinds, "stop": "packets", "dir": ""}})
    a = _args(tmp_path, b, flow="constructive", from_stage="pairs", to_stage="tune")
    assert PL.run(a) == 5
    st = PL.load_state(a.workdir)
    assert st["stages"]["pairs"]["verdict"] == "packets" and seen == []
    assert "tune" not in st["stages"]


def test_handoff_hs_nets_only_gated_handed_pairs(tmp_path):
    work = str(tmp_path / "W")
    os.makedirs(os.path.join(work, "3-pairs"))
    rep = {"pairs": [{"pair": ["USB_DP", "USB_DM"], "handoff": True}, {"pair": ["LED_P", "LED_N"], "handoff": True},
                     {"pair": ["PCIE_TX_P", "PCIE_TX_N"], "handoff": False}],
           "handoff": ["USB_DP", "USB_DM", "LED_P", "LED_N"]}
    json.dump(rep, open(os.path.join(work, "3-pairs", "board.pairs.json"), "w"))
    state = {"stages": {"pairs": {"dir": "3-pairs"}}}
    cons = {"pairs": {"USB": {"skew": 0.1}}}
    assert PFB.handoff_hs_nets(state, work, cons) == {"USB_DP", "USB_DM"}
    assert PFB.handoff_hs_nets(state, work, {}) == {"USB_DP", "USB_DM"}      # USB is a high-speed family without [pairs.*]


def test_scope_gaps_keeps_only_gate_kinds():
    mm = {"gaps": [{"kind": "pair-open", "class": "hs-pair"}, {"kind": "open-gap", "class": "plain"}]}
    GP._scope_gaps(mm, ["pair-open"])
    assert [g["kind"] for g in mm["gaps"]] == ["pair-open"]
    mm2 = {"gaps": [{"kind": "open-gap", "class": "plain"}]}
    GP._scope_gaps(mm2, None)
    assert len(mm2["gaps"]) == 1
