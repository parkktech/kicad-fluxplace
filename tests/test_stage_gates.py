"""PHASE3I B1: stage gates. A gated stage (pairs) that leaves high-speed pair gaps stops the flow with exit 5 and a
packet; the next stage does not start; the route stage never takes a gated handed-over pair as a plain net."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import pipeline as PL, gaps as GP, pairfb as PFB
from test_pipeline import _board, _args


def test_gate_defaults_and_override():
    assert GP.gate_kinds({}, "pairs") == ["pair-open", "pair-asym", "pair-unmeasured", "pair-skew", "pair-stub", "pair-impedance"]
    assert GP.gate_kinds({}, "route") == ["power-width", "power-vias"]     # PHASE3I F2: rails are laid by route
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
    monkeypatch.setattr(PL, "_gate_check", lambda c, n, name, board, kinds, src=None, later=(): {
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


def test_gated_open_legs_gated_plain_and_not_pairs():
    """PHASE3I F1: both legs of an open gated pair; a plain pair and a not_pairs-declared pair are never returned."""
    from fluxplace import krtchain as K
    nets = ["USB_DP", "USB_DM", "LED_P", "LED_N", "ZZ_P", "ZZ_N"]
    cons = {"pairs": {"USB": {"skew": 0.1}}}
    assert PFB.gated_open_legs(cons, nets, ["USB_DM"]) == {"USB_DP", "USB_DM"}
    assert PFB.gated_open_legs(cons, nets, ["LED_N"]) == set()                       # plain pair: not gated
    assert PFB.gated_open_legs({**cons, "not_pairs": ["USB_DP"]}, nets, ["USB_DM"]) == set()
    assert PFB.gated_open_legs(cons, nets, [], extra=["USB_DP"]) == {"USB_DP", "USB_DM"}   # handed-over leg
    assert K.route_net_set(nets, set(), {"USB_DP", "USB_DM"}, []) == ("*",)            # nothing excluded
    got = K.route_net_set(nets, {"ZZ_P"}, {"USB_DP", "USB_DM"}, ["USB_DP"])
    assert got == sorted(set(nets) - {"ZZ_P", "USB_DP"}) and "USB_DM" in got          # still-open leg excluded
    assert K.route_net_set(nets, set(), {"USB_DP", "USB_DM"}, []) == ("*",)            # pair completed: back in


# ---- PHASE3I F2: gate scope selects targets, the guard sees every gap (close stubbed: no router, no kicad-cli) ----
import types

import pytest

pcbnew = pytest.importorskip("pcbnew")
from fluxplace import calllog as CL  # noqa: E402
from test_planetie import make  # noqa: E402


def _touch(base, out):
    """a real copper change on the candidate: one track added to the copy of `base`"""
    b = pcbnew.LoadBoard(base)
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(20), pcbnew.FromMM(20)))
    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(22), pcbnew.FromMM(20)))
    t.SetWidth(pcbnew.FromMM(0.2))
    t.SetLayer(b.GetLayerID("F.Cu"))
    b.Add(t)
    b.Save(out)


def _gap(gid, kind, cls, nets, sev=0):
    return {"id": gid, "kind": kind, "class": cls, "nets": nets, "at": None, "layer": None, "sev": sev}


def _stub_close(monkeypatch, base_gaps, cand_gaps=None):
    def measure(path, kicad_cli="kicad-cli"):
        gaps = cand_gaps if (cand_gaps is not None and "pairgroup" in os.path.basename(path)) else base_gaps
        return {"opens": 1, "shorts": 0, "errors": 0, "opens_by_net": {}, "gaps": [dict(g) for g in gaps]}

    def rung(name, base, out, gaps, ctx):
        if name == "pairgroup":
            _touch(base, out)
        return {}
    monkeypatch.setattr(GP, "_remeasure", measure)
    monkeypatch.setattr(GP, "_classify_into", lambda *a, **k: None)
    monkeypatch.setattr(GP, "_protect", lambda *a, **k: set())
    monkeypatch.setattr(GP, "_call_rung", rung)


def test_scoped_guard_sees_out_of_scope_rf_gap(tmp_path, monkeypatch):
    """kinds=["pair-open"]: the pair is the target, but a candidate that closes it and adds an rf gap is refused."""
    base = [_gap("p1", "pair-open", "hs-pair", ["A_P", "A_N"], sev=3), _gap("rf1", "open-gap", "rf", ["RF1"])]
    cand = [_gap("rf1", "open-gap", "rf", ["RF1"]), _gap("rf2", "open-gap", "rf", ["RF2"])]
    _stub_close(monkeypatch, base, cand)
    b = make(str(tmp_path / "in.kicad_pcb"))
    att = str(tmp_path / "att.jsonl")
    r = GP.close(b, str(tmp_path / "W"), cons={}, attempts=att, log=lambda *a: None, kinds=["pair-open"])
    rows = [json.loads(x) for x in open(att)]
    assert any(x["method"] == "pairgroup" and x["result"] == "refused" and x["why"] == "R6 rf worse 1->2" for x in rows)
    assert r["stop"] != "clean"


def test_scoped_gap_without_rung_keeps_gate_open(tmp_path, monkeypatch):
    """a gate-scoped gap no rung handles is not clean; unscoped, the same gap set counts as clean (as before)."""
    _stub_close(monkeypatch, [_gap("o1", "other", "plain", ["N"])])
    b = make(str(tmp_path / "in.kicad_pcb"))
    assert GP.close(b, str(tmp_path / "W1"), cons={}, log=lambda *a: None, kinds=["other"])["stop"] == "flat"
    assert GP.close(b, str(tmp_path / "W2"), cons={}, log=lambda *a: None)["stop"] == "clean"


def test_pair_impedance_kind_and_route_power_default():
    assert GP.DISPATCH["pair-impedance"] == ["pairgroup", "ai"]
    assert GP.ORDER.index("pair-impedance") == GP.ORDER.index("pair-stub") + 1 and GP.ORDER.index("pair-stub") == GP.ORDER.index("pair-skew") + 1
    assert "pair-impedance" in GP.gate_kinds({}, "pairs")
    g = {"kind": "pair-impedance", "class": "hs-pair", "sev": 1}
    assert GP.in_scope(g, ["pair-impedance"]) and not GP.in_scope({**g, "class": "pair"}, ["pair-impedance"])
    assert GP.score_terms({"shorts": 0, "opens": 0, "errors": 0}, [g])["hs_pair"] == 1
    assert GP.gate_kinds({}, "route") == ["power-width", "power-vias"]     # no `power` stage: rails are laid by `route`


def test_gate_budget_from_flow_constraint(tmp_path, monkeypatch):
    from fluxplace import pipeline as PL2
    seen = {}

    def fake_close(board, work, cons_path, cons, **kw):
        seen.update(kw)
        return {"stop": "clean", "start": {"opens": 0}, "end": {"opens": 0}, "best": board}
    monkeypatch.setattr(GP, "close", fake_close)
    c = types.SimpleNamespace(dir=lambda n, nm: str(tmp_path / f"{n}-{nm}"), board_in=str(tmp_path / "in"), cons_path=None,
                              cons={"flow": {"gate_budget_min": 7}})
    PL2._gate_check(c, 3, "pairs", str(tmp_path / "in"), ["pair-open"])
    assert seen["budget_min"] == 7.0 and seen["kinds"] == ["pair-open"]


def test_resume_past_unclean_gate_exits_5(tmp_path, monkeypatch):
    from fluxplace import pipeline as PL2
    ran = []
    monkeypatch.setitem(PL2.FUNCS, "close", lambda c, n, board: ran.append("close") or {"verdict": "pass", "board": board})
    b = _board(tmp_path)
    a = _args(tmp_path, b, flow="constructive", from_stage="close", to_stage="close")
    assert PL2.run(a) == 5 and ran == []


def test_ledger_gets_the_gated_verdict(tmp_path, monkeypatch):
    from fluxplace import pipeline as PL2
    seen = []
    orig_after = PL2._Ledger.after
    monkeypatch.setattr(PL2._Ledger, "after", lambda self, name, n, res: seen.append((name, res["verdict"])) or orig_after(self, name, n, res))
    monkeypatch.setitem(PL2.FUNCS, "pairs", lambda c, n, board: {"verdict": "pass", "line": "pairs ok", "board": board})
    monkeypatch.setattr(PL2, "_gate_check", lambda c, n, name, board, kinds, src=None, later=(): {
        "verdict": "packets", "board": board, "line": "gate", "next": [], "gate": {"kinds": kinds, "stop": "packets", "dir": ""}})
    b = _board(tmp_path)
    assert PL2.run(_args(tmp_path, b, flow="constructive", from_stage="pairs", to_stage="pairs")) == 5
    assert seen == [("pairs", "packets")]


def test_packets_answered_gate_is_rechecked_without_stage_rerun(tmp_path, monkeypatch):
    from fluxplace import pipeline as PL2
    import shutil as sh
    b = _board(tmp_path)
    n = PL2.CONSTRUCTIVE.index("pairs") + 1
    work = str(tmp_path / "W")
    gd = os.path.join(work, f"{n}-gate")
    os.makedirs(gd)
    sh.copy(b, os.path.join(gd, "best.kicad_pcb"))
    json.dump({"stop": "packets", "input_sha1": PL2._file_sha1(b)}, open(os.path.join(gd, "stop.json"), "w"))   # H1: this input
    ran, checked = [], []
    monkeypatch.setitem(PL2.FUNCS, "pairs", lambda c, n_, board: ran.append("pairs") or {"verdict": "pass", "board": board})

    def gate(c, n_, name, board, kinds, src=None, later=()):
        checked.append(os.path.basename(board))
        return {"verdict": "pass", "board": board, "line": "ok", "next": [], "gate": {"kinds": kinds, "stop": "clean", "dir": gd}}
    monkeypatch.setattr(PL2, "_gate_check", gate)
    assert PL2.run(_args(tmp_path, b, flow="constructive", from_stage="pairs", to_stage="pairs")) == 0
    assert ran == [] and checked == ["resume.kicad_pcb"] and os.path.exists(os.path.join(gd, "resume.kicad_pcb"))
