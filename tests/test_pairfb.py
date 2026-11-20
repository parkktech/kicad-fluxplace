"""pairs-stage completion ladder: every pair ends connected in one reported state; the last resort hands it to the router."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import planboards as PB
from test_pairs import duo, lay, clean, CLR, _stub
from fluxplace import pairfb as FB
from fluxplace import pairs as PR
from fluxplace import routediag as RD

CAGE = PB.keepout(17, 16.5, 23.5, 17) + PB.keepout(17, 23, 23.5, 23.5) + PB.keepout(17, 16.5, 17.5, 23.5) + PB.keepout(23, 16.5, 23.5, 23.5)


def test_unlaid_pair_is_state_c_and_handed_to_the_router(tmp_path):
    rep, B, txt = lay(tmp_path, duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 60, 40, "D_P", "D_N") + CAGE)
    r = rep["pairs"][0]
    assert r["state"] == "c" and r["handoff"] and rep["handoff"] == ["D_N", "D_P"] and rep["states"]["c"] == 1
    assert r["tried"] and r["tried"][0].startswith("b(i)")               # the ladder ran before the pair was given up
    assert not [s for s in B["segs"] if s["net"] in ("D_P", "D_N")]
    t = PR.format_text(rep)
    assert "PAIR_NOT_LAID D_N/D_P" in t and "PAIR_HANDOFF state c" in t and "pairs states: c 1" in t


def test_handoff_effort_reports_measured_causes_and_configuration_skip(tmp_path):
    rows = [
        {"pair": ["B_P", "B_N"], "why": "A>B: search budget for the coupled centre line", "blockers": []},
        {"pair": ["C_P", "C_N"], "why": "A>B: no path for the coupled centre line", "blockers": ["track X F.Cu"]},
        {"pair": ["E_P", "E_N"], "why": "A>B: no clear break-out for A.1", "blockers": ["pad A.2 (Y)"]},
        {"pair": ["A_P", "A_N"], "why": "taken back: pair audit says vias 2 vs 1", "blockers": []},
    ]
    rep = {"pairs": [dict(row, status="open", plan="not-laid", state=None, tried=[], secs=0, **{"class": "Default"}) for row in rows],
           "verdict": "PAIRS_WARN", "complete": 0, "total": 4, "secs": 0, "signal_layers": [], "plan_only": False}

    def unexpected(*args, **kwargs):
        raise AssertionError("fallback must be skipped by configuration")

    params = PR.params({"pairlay": {"fallback": False}})
    FB.ladder(rep, str(tmp_path / "unused.kicad_pcb"), {}, None, params, unexpected)
    counts = rep["handoff_effort"]["counts"]
    assert counts == {"budget_reached": 1, "no_corridor": 1, "escape_blocked": 1, "audit_rejected": 1,
                      "fallback_trials_skipped_by_configuration": 4}
    assert all(r["handoff_detail"]["fallback_trials_skipped_by_configuration"] for r in rep["pairs"])
    corridor = next(r for r in rep["pairs"] if r["handoff_reason"] == "no_corridor")
    assert corridor["handoff_detail"]["measured_blockers"] == ["track X F.Cu"]
    text = PR.format_text(rep)
    assert text.count("pairs handoff effort:") == 1
    assert "budget reached 1; no corridor 1; escape blocked 1; audit rejected 1; fallback trials skipped by configuration 4" in text
    assert "effort no_corridor, fallback_trials_skipped_by_configuration" in text


def test_half_laid_pair_leaves_no_partial_copper(tmp_path):
    # TX section is clear, the RX section (J1 side) is caged: the TX copper is not kept as a half-routed pair
    caps = PB.fp("C1", 30, 20, [PB.pad("1", 0, -0.5, "TX_P", 0.5, 0.5), PB.pad("2", 0, 0.5, "RX_P", 0.5, 0.5)]) + \
        PB.fp("C2", 31, 20, [PB.pad("1", 0, -0.5, "TX_N", 0.5, 0.5), PB.pad("2", 0, 0.5, "RX_N", 0.5, 0.5)])
    cage = PB.keepout(27, 28, 34, 28.5) + PB.keepout(27, 36, 34, 36.5) + PB.keepout(27, 28, 27.5, 36.5) + PB.keepout(33.5, 28, 34, 36.5)
    rep, B, _ = lay(tmp_path, duo("U1", 30, 10, "TX_P", "TX_N") + caps + duo("J1", 30, 32, "RX_P", "RX_N") + cage)
    st = {r["pair"][0][:2]: r for r in rep["pairs"]}
    assert st["RX"]["state"] == "c" and st["RX"]["handoff"] and st["TX"]["state"] == "a"
    assert not [s for s in B["segs"] if s["net"] in ("RX_P", "RX_N")]
    for r in rep["pairs"]:                                              # every pair has exactly one state
        assert r["state"] in FB.STATES + ("a-mismatch",)


def test_ladder_b_i_tries_cheaper_vias_and_keeps_the_trial_that_completes_the_pair(tmp_path):
    out = tmp_path / "o.kicad_pcb"
    out.write_text("(kicad_pcb)\n")
    rep = {"pairs": [{"pair": ["F_P", "F_N"], "status": "open", "state": None, "tried": [], "key": [1, 3, 5.0]}]}
    seen = []

    def run_fn(src, cons, spec, trial, only=None, fallback=True):
        seen.append(cons["pairlay"]["via_cost_mm"])
        open(trial, "w").write("(kicad_pcb (trial))\n")
        ok = cons["pairlay"]["via_cost_mm"] < 10
        return {"pairs": [{"pair": ["F_P", "F_N"], "status": "complete" if ok else "open"}]}
    FB.ladder(rep, str(out), {}, None, PR.params({}), run_fn, log=lambda m: None)
    r = rep["pairs"][0]
    assert r["state"] == "b(i)" and r["status"] == "complete" and not r.get("handoff") and seen == [8.0]
    assert out.read_text() == "(kicad_pcb (trial))\n" and rep["fallback"]["steps"][0] == ("b(i)", 1)


def test_ladder_gives_up_in_order_and_ends_in_c(tmp_path):
    out = tmp_path / "o.kicad_pcb"
    out.write_text("(kicad_pcb)\n")
    rep = {"pairs": [{"pair": ["F_P", "F_N"], "status": "open", "state": None, "tried": [], "key": [1, 3, 5.0], "blockers": []}]}
    seen = []

    def run_fn(src, cons, spec, trial, only=None, fallback=True):
        seen.append(dict(cons["pairlay"]))
        assert fallback is False
        return {"pairs": [{"pair": ["F_P", "F_N"], "status": "open"}]}
    FB.ladder(rep, str(out), {}, None, PR.params({}), run_fn, log=lambda m: None)
    assert rep["pairs"][0]["state"] == "c" and rep["pairs"][0]["handoff"]
    assert [x.get("max_uncoupled_mm") for x in seen] == [None, None, FB.DEFAULT_UNCOUPLED_MM]       # b(i) x2, then b(iii) with the bounded uncoupled break-out
    assert seen[-1]["skew_frac"] == 1.0
    assert rep["pairs"][0]["tried"] == ["b(i):no", "b(i):no", "b(iii):no"]


def test_single_ended_stub_blocking_a_pair_is_ripped_and_its_pin_escaped_again_b_ii(tmp_path, monkeypatch):
    # a keepout wall with a 1.4 mm gap; a locked fan-out stub of S1 seals the gap
    wall = PB.keepout(-1, 39.6, 20.3, 40.4) + PB.keepout(21.7, 39.6, 100, 40.4)
    stub = _stub("S1", 22.8, 40.0, 19.9, 40.0)
    body = duo("U1", 20.5, 20, "D_P", "D_N") + duo("J1", 20.5, 60, "D_P", "D_N") + PB.fp("R1", 22.8, 40, [PB.pad("1", 0, 0, "S1", 0.4, 0.4)]) + wall + stub
    cons = {"preroute": {"signal_layers": ["F.Cu"]}, "pairlay": {"negotiate": False, "via_exit": False, "dogbone": False}}
    escaped = []
    monkeypatch.setattr(PR, "_escape", lambda b, nets, cs, log=None: escaped.append(list(nets)) or {"nets": len(nets), "escaped": len(nets), "blocked": []})
    b = PB.write(tmp_path, body)
    off = PR.run(b, cons, None, str(tmp_path / "a.kicad_pcb"), fallback=False)
    assert off["complete"] == 0
    on = PR.run(b, cons, None, str(tmp_path / "b.kicad_pcb"), cs_args=["--x"])
    r = on["pairs"][0]
    assert r["status"] == "complete" and r["state"] == "b(ii)", (r["state"], r["why"], r["blockers"], r["tried"])
    assert escaped == [["S1"]] and not on["handoff"]
    B = RD.load(str(tmp_path / "b.kicad_pcb"))
    assert not [s for s in B["segs"] if s["net"] == "S1"]
    assert clean(B, {"D_P", "D_N"}) >= CLR


def test_stub_candidates_are_only_short_single_pad_stubs_near_the_terminals():
    B = {"pads": [{"net": "S1", "x": 0.0, "y": 0.0, "w": 0.4, "h": 0.4}, {"net": "S2", "x": 0.0, "y": 0.0, "w": 0.4, "h": 0.4},
                  {"net": "S2", "x": 30.0, "y": 0.0, "w": 0.4, "h": 0.4}],
         "segs": [{"net": "S1", "a": (0.0, 0.0), "b": (2.0, 0.0), "layer": "F.Cu", "w": 0.2},
                  {"net": "S2", "a": (0.0, 0.0), "b": (30.0, 0.0), "layer": "F.Cu", "w": 0.2}],
         "vias": [{"net": "S1", "x": 2.0, "y": 0.0, "d": 0.45}]}
    got = FB.stub_candidates(B, [(3.0, 0.0)], set(), set(), {"S1", "S2"}, 4.0)
    assert {o["net"] for k, o in got} == {"S1"} and len(got) == 2        # S2 is a real route, not a stub
    assert FB.stub_candidates(B, [(3.0, 0.0)], set(), {"S1"}, {"S1"}, 4.0) == []            # plane / rail nets are never ripped
    assert FB.stub_candidates(B, [(50.0, 0.0)], set(), set(), {"S1"}, 4.0) == []            # far from the terminal


def test_blocker_nets_and_uncoupled_limit():
    r = {"blockers": ["track MIC_N F.Cu", "via GND", "pad C1.2 (X)", "track BT3_DP (reserved)", "track  (corridor of A/B)"]}
    assert FB.blocker_nets(r) == {"MIC_N", "GND", "BT3_DP"}
    cons = {"pairs": {"USB_": {"max_uncoupled_mm": 3}}, "pairlay": {"max_uncoupled_mm": 7}}
    assert FB.uncoupled_limit(cons, ("USB_DP", "USB_DM")) == 3 and FB.uncoupled_limit(cons, ("X_P", "X_N")) == 7
    assert FB.uncoupled_limit({}, ("X_P", "X_N")) == FB.DEFAULT_UNCOUPLED_MM


def test_router_skip_keeps_handed_over_pairs_on_the_skip_list(tmp_path):
    todo = ["A_P", "A_N", "B_P", "B_N", "RF1", "X"]
    ctl = {n: "pair" for n in todo[:4]} | {"RF1": "rf"}
    assert FB.router_skip(todo, ctl, {"B_P", "B_N"}, "exclude") == ["A_P", "A_N", "B_P", "B_N", "RF1"]     # PHASE3C: handed-over legs too
    assert FB.router_skip(todo, ctl, set(), "route") == []
    d = tmp_path / "5-pairs"
    d.mkdir()
    json.dump({"handoff": ["B_N", "B_P"]}, open(d / "board.pairs.json", "w"))
    assert FB.handoff_nets({"stages": {"pairs": {"dir": "5-pairs"}}}, str(tmp_path)) == {"B_N", "B_P"}
    assert FB.handoff_nets({"stages": {}}, str(tmp_path)) == set()


def test_ladder_keeps_a_trial_only_when_it_completes_a_pair_and_the_ripped_pair_is_complete_again(tmp_path):
    out = tmp_path / "o.kicad_pcb"
    out.write_text("(kicad_pcb)\n")
    row = lambda p, st: {"pair": p, "status": "complete" if st else "open", "state": "a" if st else None, "tried": []}
    rep = {"pairs": [row(["V_P", "V_N"], True), row(["F_P", "F_N"], False)]}
    calls = []

    def run_fn(src, cons, spec, trial, only=None, fallback=True):
        calls.append(sorted(only))
        open(trial, "w").write("(kicad_pcb (trial))\n")
        return {"pairs": [{"pair": ["F_P", "F_N"], "status": "complete"}, {"pair": ["V_P", "V_N"], "status": "open"}]}
    assert FB._trial(rep, str(out), {}, None, run_fn, "t", "b(ii)", lambda m: None, victims=[("V_P", "V_N")]) == 0     # the victim stayed open: discarded
    assert out.read_text() == "(kicad_pcb)\n" and rep["pairs"][1]["state"] is None
    assert not os.path.exists(tmp_path / "fb-t.kicad_pcb")

    def run_ok(src, cons, spec, trial, only=None, fallback=True):
        open(trial, "w").write("(kicad_pcb (trial))\n")
        return {"pairs": [{"pair": ["F_P", "F_N"], "status": "complete"}, {"pair": ["V_P", "V_N"], "status": "complete"}]}
    assert FB._trial(rep, str(out), {}, None, run_ok, "t", "b(ii)", lambda m: None, victims=[("V_P", "V_N")]) == 1
    assert out.read_text() == "(kicad_pcb (trial))\n" and rep["pairs"][1]["state"] == "b(ii)" and rep["pairs"][0]["state"] == "a"
