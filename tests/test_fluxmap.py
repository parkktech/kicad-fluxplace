"""fluxmap.py (the meta map), its slices and renderer, and `place --objective plan`.  Pure text: any interpreter."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
import test_escape as TE
from fluxplace import fluxmap as FM, planplace as PP, planroute as PL, routediag as RD

CONS = {"placement": {"fixed": ["U1", "J1", "J2"], "group": {"cm": {"refs": ["J1", "J2"], "centre": [70, 40]}}}}


def mapped(tmp, gap=1.0, cons=CONS):
    B = RD.load(TE.board(tmp, gap))
    m = FM.build(B, cons, None, "x.kicad_pcb")
    return B, m


def test_map_is_deterministic_and_versioned(tmp_path):
    B, a = mapped(tmp_path)
    _, b = mapped(tmp_path)
    assert FM.dumps(a) == FM.dumps(b)
    assert a["fluxmap"] == FM.VERSION == 1
    assert list(a)[:6] == ["fluxmap", "board", "parts", "relations", "groups", "nets"]
    assert all(k in a for k in FM.SECTIONS)
    p = FM.save(a, str(tmp_path / "m.fluxmap.json"))
    assert p < 300_000 and FM.load(str(tmp_path / "m.fluxmap.json")) == json.loads(FM.dumps(a))


def test_map_schema_parts_nets_groups(tmp_path):
    B, m = mapped(tmp_path)
    parts = {p["ref"]: p for p in m["parts"]}
    assert parts["U1"]["mode"] in ("fixed", "anchor") and parts["R1"]["mode"] == "movable"
    assert parts["J1"]["group"] == "cm" and parts["U1"]["pitch"] == 0.5
    assert set(parts["U1"]) >= {"ref", "v", "fp", "x", "y", "rot", "side", "court", "h", "pads", "mode", "group"}
    nets = {n["n"]: n for n in m["nets"]}
    assert nets["S0"]["pads"][0][0] in ("J1.1", "U1.1") and nets["S0"]["state"] == "unrouted"
    g = {x["name"]: x for x in m["groups"]}
    assert g["cm"]["members"] == ["J1", "J2"] and "ext_nets" in g["cm"]
    assert m["board"]["floors"]["track"] == 0.127 and [l["role"] for l in m["board"]["layers"]] == ["signal", "signal"]
    assert m["board"]["outline"] and m["board"]["bbox"] == [0.0, 0.0, 100.0, 80.0]


def test_show_slices_are_small(tmp_path):
    B, m = mapped(tmp_path)
    s = json.loads(FM.show(m, "parts", ref="U1"))
    assert s["part"]["ref"] == "U1" and "S0" in s["nets"]
    assert len(FM.show(m, "nets", net="S0")) < 1000
    assert "unknown section" in FM.show(m, "nope")


def test_map_with_plan_sections_and_render(tmp_path):
    B = RD.load(TE.board(tmp_path, 0.35))
    R = PL.run(B, CONS, None, ignore_routing=True)
    m = FM.build(B, CONS, None, "x.kicad_pcb", cls=R["cls"])
    m.update(PL.plan_section(R))
    assert m["verdict"]["score"] == R["score"] and m["escapes"]["pins"]["U1"]
    assert any(v[0] == "s" for v in m["escapes"]["pins"]["U1"].values())
    assert m["grid"]["cell"] == 2.0 and len(m["grid"]["capH"]["F.Cu"]) == m["grid"]["nx"] * m["grid"]["ny"]
    files = FM.render(m, str(tmp_path / "out.png"))
    assert [os.path.basename(f) for f in files] == ["out_F.png", "out_B.png", "out_overview.png"]
    assert all(os.path.getsize(f) > 1000 for f in files)
    files = FM.render(m, str(tmp_path / "out.svg"))
    assert open(files[0]).read().startswith("<svg")


def test_map_with_plan_sections_is_byte_identical_and_has_no_clock(tmp_path):
    def build():
        B = RD.load(TE.board(tmp_path, 0.35))
        R = PL.run(B, CONS, None, ignore_routing=True)
        m = FM.build(B, CONS, None, "x.kicad_pcb", cls=R["cls"])
        m.update(PL.plan_section(R))
        return FM.dumps(m)
    a, b = build(), build()
    assert a == b and "timing" not in a and "seconds" not in a


def test_lexicographic_key_rejects_a_hard_failure_for_a_better_score():
    from fluxplace.planplace import key_less
    assert not key_less((0, 1, 0.0, 50.0), (0, 0, 0.0, 900.0))          # one more hard failure never pays for a lower score
    assert key_less((0, 0, 0.0, 900.0), (0, 1, 0.0, 50.0))
    assert key_less((0, 0, 0.02, 500.0), (0, 0, 0.05, 100.0))           # lower risk_frac beats a lower score
    assert key_less((0, 0, 0.0, 10.0), (1, 0, 0.0, 10.0)) and not key_less((0, 0, 0.0, 10.0), (0, 0, 0.0, 10.0))
    assert key_less((0, 0, 0.0, 10.0), (0, 0, 0.0, 10.0), eq=True)


def test_plan_objective_moves_the_blocker_and_keeps_the_key_monotone(tmp_path):
    B = RD.load(TE.board(tmp_path, 0.35))
    mv, st = PP.plan_place(B, CONS, None, log=lambda m: None, max_evals=200)
    assert not st["reverted_to_input"] and st["score_confirmed"] < st["score_before"]
    assert st["components_before"]["sealed"] == 8 and st["components_after"]["sealed"] == 0
    assert "R1" in mv and "U1" not in mv and "J1" not in mv and "J2" not in mv        # the blocker moved; fixed parts never
    assert st["log"] and all(PP.key_less(e["key_after"], e["key_before"]) for e in st["log"])


def test_incremental_state_agrees_with_a_fresh_full_plan_after_moves(tmp_path):
    B = RD.load(TE.board(tmp_path, 0.35))
    pp = PP.PlanPlacer(B, CONS, None, max_evals=40)
    pp.do_rotation_pass = False
    pp.run()
    R = PL.run(B, CONS, None, ignore_routing=True)
    assert pp.evals > 0 and pp.ps.comp["sealed"] == R["comp"]["sealed"] and pp.ps.comp["oversub"] == R["comp"]["oversub"]
    assert abs(pp.ps.score - R["score"]) <= 0.10 * R["score"] + 5


def test_a_worse_full_plan_reverts_the_whole_placement(tmp_path, monkeypatch):
    B = RD.load(TE.board(tmp_path, 0.35))
    calls = {"n": 0}
    orig = PL.run

    def fake(*a, **k):
        R = orig(*a, **k)
        calls["n"] += 1
        if calls["n"] == 2:                      # the confirming full plan after the moves shows many more hard failures
            R["findings"] = R["findings"] + [{"code": "PLAN_PIN_SEALED", "part": "U1", "pin": "1", "net": "x", "at": [0, 0], "msg": "", "suggestion": ""}] * 30
        return R
    monkeypatch.setattr(PL, "run", fake)
    before = {r: p["pos"] for r, p in B["parts"].items()}
    mv, st = PP.plan_place(B, CONS, None, log=lambda m: None, max_evals=60)
    assert st["reverted_to_input"] and mv == {} and all(B["parts"][r]["pos"] == before[r] for r in before)


def test_oversubscribed_group_is_resolved_and_reported(tmp_path):
    B = RD.load(TE.board(tmp_path, 0.35))
    mv, st = PP.plan_place(B, CONS, None, log=lambda m: None, max_evals=200)
    assert any("U1/W" in e.get("resolved_oversub", []) for e in st["log"])           # the move that opened the fan-out names the group it fixed


def test_group_moves_pull_a_movable_group_to_a_fixed_part(tmp_path):
    ic = PB.fp("UA", 80, 60, [PB.pad("1", 0, 0, "RF_X", 0.8, 0.8), PB.pad("2", 1.5, 0, "VCC", 0.8, 0.8), PB.pad("3", 3, 0, "GND", 0.8, 0.8),
                              PB.pad("4", 4.5, 0, "EN", 0.8, 0.8), PB.pad("5", 6, 0, "OUT", 0.8, 0.8)])
    caps = PB.fp("C1", 83, 62, [PB.pad("1", 0, 0, "VCC", 0.8, 0.8), PB.pad("2", 1.2, 0, "GND", 0.8, 0.8)])
    far = PB.fp("J1", 10, 10, [PB.pad("1", 0, 0, "RF_X", 1.5, 1.5), PB.pad("2", 4, 0, "OUT", 1.5, 1.5), PB.pad("3", 8, 0, "EN", 1.5, 1.5),
                              PB.pad("4", 12, 0, "VCC", 1.5, 1.5), PB.pad("5", 16, 0, "GND", 1.5, 1.5)])
    cons = {"placement": {"fixed": ["J1"]}, "plan": {"group": {"blk": {"refs": ["UA", "C1"]}}}}
    B = RD.load(PB.write(tmp_path, ic + caps + far))
    R0 = PL.run(B, cons, None, ignore_routing=True)
    assert [f for f in R0["findings"] if f["code"] == "PLAN_NET_OVER_BUDGET" and f["net"] == "RF_X"]
    j1 = B["parts"]["J1"]["pos"]
    mv, st = PP.plan_place(B, cons, None, log=lambda m: None, max_evals=120)
    assert not st["reverted_to_input"] and "J1" not in mv and {"UA", "C1"} <= set(mv)
    assert any(e["kind"] == "group" for e in st["log"])
    rel = (B["parts"]["C1"]["pos"][0] - B["parts"]["UA"]["pos"][0], B["parts"]["C1"]["pos"][1] - B["parts"]["UA"]["pos"][1])
    assert abs(abs(rel[0]) + abs(rel[1]) - 5.0) < 1e-6                                # internal relative placement kept (rotated rigidly)
    R1 = PL.run(B, cons, None, ignore_routing=True)
    assert not [f for f in R1["findings"] if f["code"] == "PLAN_NET_OVER_BUDGET" and f["net"] == "RF_X"] and B["parts"]["J1"]["pos"] == j1
