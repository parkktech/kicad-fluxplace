"""Wire-length refinement (wlrefine.py) on synthetic boards: the length drops, intent / pin lanes / support limits reject moves,
the result is deterministic and never worse than the input. Nothing here names a real board."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cli
from fluxplace import geom as GM, routediag as RD, wlrefine as WL
import pcbgen as g


def two(ref, x, y, n1, n2, **kw):
    return g.fp(ref, x, y, [g.pad("1", -0.5, 0, n1, 0.5, 0.5), g.pad("2", 0.5, 0, n2, 0.5, 0.5)], name="R_0402",
                court=(-1.0, -0.5, 1.0, 0.5), **kw)


def scattered():
    """a 6-pin IC, four series resistors far from the pins they hang on, one decoupling cap far from its pin."""
    b = g.ic("U1", 30, 40, ["VDD", "A1", "A2", "GND", "A3", "A4"], pitch=0.5, court=(-2.6, -1.2, 2.6, 1.2))
    b += g.ic("U2", 70, 40, ["B1", "B2", "GND", "B3", "B4", "VDD"], pitch=0.5, court=(-2.6, -1.2, 2.6, 1.2), locked=True)
    for i, (x, y) in enumerate(((50, 12), (55, 70), (20, 65), (85, 15))):
        b += two(f"R{i + 1}", x, y, f"A{i + 1}", f"B{i + 1}")
    b += two("C1", 12, 12, "VDD", "GND")
    return b


def run(tmp_path, body, cons=None, seed=0, **kw):
    B = RD.load(g.board(tmp_path, body, **kw))
    mv, rec = WL.refine(B, cons or {}, None, seed=seed, log=lambda m: None)
    return B, mv, rec


def test_wire_length_and_decap_distance_drop_and_the_gate_is_not_worse(tmp_path):
    B, mv, rec = run(tmp_path, scattered())
    b, a = rec["before"], rec["after"]
    assert {"R1", "R2", "R3", "R4"} <= set(mv)
    assert a["hpwl_weighted"] < 0.75 * b["hpwl_weighted"] and a["hpwl_all_mm"] < b["hpwl_all_mm"]
    assert a["decap_mean_mm"] < b["decap_mean_mm"] and a["decap_worst_mm"] < b["decap_worst_mm"]
    assert WL.worse(a, b) == [] and a["illegal"] == 0
    assert "U2" not in mv                                                   # locked parts never move
    line = WL.summary(dict(rec))
    assert line.startswith("wl-refine:") and "class-weighted" in line and "decap mean" in line and "intent met" in line
    for k in WL.RECORD_KEYS:                                                # the stage record carries before and after
        assert k in b and k in a


def test_same_seed_same_result(tmp_path):
    _B1, m1, r1 = run(tmp_path, scattered(), seed=3)
    _B2, m2, r2 = run(tmp_path, scattered(), seed=3)
    assert m1 == m2 and r1["after"] == r2["after"] and r1["kept"] == r2["kept"]


def test_a_move_into_an_intent_region_or_past_a_hard_near_limit_is_rejected(tmp_path):
    cons = {"intent": {"region": [{"name": "keep", "rect": [40, 30, 60, 50], "side": "F"}],
                       "near": [{"name": "r4", "refs": ["R4"], "to": "U2", "max_mm": 40.0, "hard": True}]}}
    B = RD.load(g.board(tmp_path, scattered()))
    R = WL.Refiner(B, cons, None)
    assert R.try_move("move", {"R1": (50.0, 40.0, 0)}) is False             # the region is a legality zone and an intent zone
    assert R.stats["rejected_intent"] + R.stats["rejected_illegal"] == 1
    assert B["parts"]["R1"]["pos"] == (50, 12)                              # put back
    assert R.try_move("move", {"R4": (10.0, 70.0, 0)}) is False and R.stats["rejected_intent"] >= 1
    assert R.try_move("move", {"R1": (50.0, 20.0, 0)}) is True              # outside the region: kept
    # the whole pass keeps every part out of the region and the near item met
    B, mv, rec = run(tmp_path, scattered(), cons)
    for r in mv:
        assert not GM.overlap(B["parts"][r]["court"], (40, 30, 60, 50), -0.02)
    assert rec["after"]["intent_hard_unmet"] == 0 and rec["after"]["intent_met"] >= rec["before"]["intent_met"]


def test_a_move_that_seals_a_fine_pitch_pin_or_breaks_a_support_limit_is_rejected(tmp_path):
    B = RD.load(g.board(tmp_path, scattered()))
    R = WL.Refiner(B, {}, None)
    court = B["parts"]["U1"]["court"]
    # courtyards clear (legal), but the part sits in the outward lane of the left pad row
    x = court[0] - 1.0 - 0.02
    assert R.try_move("move", {"R3": (round(x, 3), 40.0, 0)}) is False
    assert R.stats["rejected_lane"] + R.stats["rejected_escape"] == 1 and R.stats["rejected_illegal"] == 0
    # a decoupling cap inside its limit may not leave it
    assert R.try_move("move", {"C1": (27.0, 38.0, 0)}) is True
    n = R.stats["rejected_support"]
    assert R.try_move("move", {"C1": (12.0, 70.0, 0)}) is False and R.stats["rejected_support"] == n + 1


def test_never_worse_when_the_gate_objects(tmp_path, monkeypatch):
    B0 = RD.load(g.board(tmp_path, scattered()))
    monkeypatch.setattr(WL, "worse", lambda g1, g0, tol=None: ["forced"])
    B, mv, rec = run(tmp_path, scattered())
    assert mv == {} and rec["after"] == rec["before"]
    assert all(B["parts"][r]["pos"] == B0["parts"][r]["pos"] and B["parts"][r]["angle"] == B0["parts"][r]["angle"] for r in B["parts"])
    assert rec["kept"].get("dropped_gate", 0) > 0 and rec["kept"].get("move", 0) == 0


def test_command_writes_board_and_record_and_the_stage_switch(tmp_path, capsys):
    src = g.board(tmp_path, scattered())
    out = str(tmp_path / "o.kicad_pcb")
    assert cli.main(["wl-refine", src, "--out", out, "--seed", "1"]) in (0, None)
    text = capsys.readouterr().out
    assert "wl-refine:" in text and os.path.exists(out)
    rec = json.load(open(str(tmp_path / "o.wl-refine.json")))
    assert rec["before"]["hpwl_weighted"] > rec["after"]["hpwl_weighted"] and rec["line"].startswith("wl-refine:")
    B = RD.load(out)
    assert WL.measure(B)["hpwl_weighted"] == rec["after"]["hpwl_weighted"]  # the written board IS the measured result
    assert WL.stage_step(out, {}, None) is None and WL.enabled({}) is False            # off unless [placement.wl] enabled
    src2 = g.board(tmp_path, scattered(), name="s.kicad_pcb")
    rec = WL.stage_step(src2, {"placement": {"wl": {"enabled": True}}}, None, log=lambda m: None, check=lambda p: 0)
    assert rec["moved"] > 0 and rec["fanout_blocked"] == [0, 0] and not os.path.exists(str(tmp_path / "s.pre-wl.kicad_pcb"))
    before = open(src2).read()
    seen = iter([0, 2])                                                     # the flow's fan-out blocks more pins afterwards
    rec = WL.stage_step(src2, {"placement": {"wl": {"enabled": True}}}, None, log=lambda m: None, check=lambda p: next(seen, 2))
    assert rec is None or rec["moved"] == 0 or (rec.get("discarded") and open(src2).read() == before)


def test_allow_is_a_recorded_what_if_and_hard_items_never_have_one(tmp_path):
    g0 = {"illegal": 0, "pin_blocked": 0, "unreserved": 0, "sealed": 1, "intent_hard_unmet": 0, "intent_soft_unmet": 0, "intent_met": 3,
          "overflow": 2.0, "risk_nets": 1, "gate_score": 100.0, "plan_score": 50.0}
    assert WL.worse(dict(g0, overflow=2.5), g0) == ["overflow 2.0 -> 2.5"]
    assert WL.worse(dict(g0, overflow=2.5), g0, {"overflow": 1.0}) == []
    assert WL.worse(dict(g0, sealed=2), g0, {"sealed": 5, "overflow": 9}) == ["sealed 1 -> 2"]
    assert WL.worse(dict(g0, intent_met=2), g0, {"overflow": 9}) == ["intent met 3 -> 2"]
    B, mv, rec = run(tmp_path, scattered(), {"placement": {"wl": {"allow": {"overflow": 3.0, "sealed": 4}}}})
    assert rec["allowance"] == {"overflow": 3.0}
