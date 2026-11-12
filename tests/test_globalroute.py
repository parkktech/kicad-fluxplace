"""Global router (globalroute.py) and plan-route (planroute.py) on synthetic boards.  Pure text: any interpreter."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import constraints as C, escapeplan as EP, fluxmap as FM, globalroute as GR, netclass as NC
from fluxplace import planroute as PL, routediag as RD

PAIRCONS = {}


def channel_board(tmp, n_nets, gap=1.0, walls=True, pair=False):
    """n_nets 2-pad nets run from x=10 to x=90 across a channel `gap` mm tall between two keep-out walls."""
    body = ""
    if walls:
        y0, y1 = 40 - gap / 2, 40 + gap / 2
        body += PB.keepout(40, 0, 60, y0, "wallN") + PB.keepout(40, y1, 60, 80, "wallS")
    names = []
    for i in range(n_nets):
        names.append(f"SIG{i}")
    if pair:
        names = ["ETH_P0_P", "ETH_P0_N"]
    L = "".join(PB.fp(f"L{i}", 10, 40 + (i - (len(names) - 1) / 2) * 1.2, [PB.pad("1", 0, 0, n, 0.5, 0.5)]) for i, n in enumerate(names))
    R = "".join(PB.fp(f"R{i}", 90, 40 + (i - (len(names) - 1) / 2) * 1.2, [PB.pad("1", 0, 0, n, 0.5, 0.5)]) for i, n in enumerate(names))
    return PB.write(tmp, body + L + R)


def run(path, **kw):
    B = RD.load(path)
    return PL.run(B, {}, None, ignore_routing=True, **kw)


def test_narrow_channel_overflows_and_names_the_cell(tmp_path):
    R = run(channel_board(tmp_path, 16, gap=1.0))
    ov = [f for f in R["findings"] if f["code"] == "PLAN_OVERFLOW"]
    assert ov and R["gr"].summary()["overflow"] > 0
    f = ov[0]
    assert 38 <= f["at"][0] <= 62 and f["demand"] > f["capacity"]
    assert f["cells_xy"] and f["nets"] and f["suggestion"]
    assert PL.verdict(R)["result"] == "FAIL"


def test_removing_the_obstacle_clears_it(tmp_path):
    R = run(channel_board(tmp_path, 16, gap=1.0, walls=False))
    assert not [f for f in R["findings"] if f["code"] == "PLAN_OVERFLOW"]
    assert PL.verdict(R)["result"] == "ok"
    R2 = run(channel_board(tmp_path, 16, gap=30.0))              # a wide channel also passes
    assert not [f for f in R2["findings"] if f["code"] == "PLAN_OVERFLOW"]


def test_pair_stays_together_in_a_wide_channel_and_splits_in_a_pinched_one(tmp_path):
    R = run(channel_board(tmp_path, 2, gap=6.0, pair=True))
    assert [n for n in R["gr"].nets.values() if n["pair"]] and not R["gr"].pair_origin
    assert not [f for f in R["findings"] if f["code"] == "PLAN_PAIR_SPLIT"]
    R = run(channel_board(tmp_path, 2, gap=0.5, pair=True), iterations=8)
    assert [f for f in R["findings"] if f["code"] == "PLAN_PAIR_SPLIT"]


def test_score_is_worse_with_the_wall(tmp_path):
    a = run(channel_board(tmp_path, 16, gap=1.0))["score"]
    b = run(channel_board(tmp_path, 16, gap=1.0, walls=False))["score"]
    assert a > b + 20


def test_deterministic(tmp_path):
    p = channel_board(tmp_path, 12, gap=1.0)
    a, b = run(p), run(p)
    assert a["score"] == b["score"] and json.dumps(PL.plan_section(a), sort_keys=True) == json.dumps(PL.plan_section(b), sort_keys=True)


def test_plan_compare_prints_both_verdicts(tmp_path, capsys):
    import cli
    pa, pb = tmp_path / "a", tmp_path / "b"
    pa.mkdir(); pb.mkdir()
    A = channel_board(pa, 16, gap=1.0)
    Bd = channel_board(pb, 16, gap=1.0, walls=False)
    maps = []
    for p in (A, Bd):
        try:
            cli.main(["plan-route", p, "--ignore-routing", "-q"])
        except SystemExit:
            pass
        maps.append(FM.default_path(p))
    capsys.readouterr()
    cli.main(["plan-compare", maps[0], maps[1]])
    out = capsys.readouterr().out
    assert "PLAN_SCORE" in out and "plan-compare: B is better" in out and "PLAN_OVERFLOW" in out


def test_vias_charge_every_layer(tmp_path):
    """A keep-out closes F.Cu in the middle: the net drops to an inner layer and back; its vias (away from its pads) charge the layers it does
    not use in those cells: more usage than with via_units = 0."""
    wall = PB.keepout(40, 0, 60, 80, "fwall").replace('(layers "F.Cu" "B.Cu")', '(layers "F.Cu")')
    body = wall + PB.fp("A", 10, 40, [PB.pad("1", 0, 0, "X", 0.5, 0.5)]) + PB.fp("B", 90, 40, [PB.pad("1", 0, 0, "X", 0.5, 0.5)])
    f = tmp_path / "v.kicad_pcb"
    f.write_text(PB.HEAD.replace('(31 "B.Cu" signal)', '(4 "In1.Cu" signal) (6 "In2.Cu" signal) (31 "B.Cu" signal)') + PB.EDGE + body + ")\n")
    B = RD.load(str(f))
    g = PL.run(B, {}, None, ignore_routing=True)["gr"]
    assert sum(r["vias"] for r in g.routes.values()) >= 2 and sum(len(r["vuse"]) for r in g.routes.values()) >= 2
    g2 = PL.run(B, {"plan": {"via_units": 0}}, None, ignore_routing=True)["gr"]
    assert sum(sum(a) for a in g.uH) > sum(sum(a) for a in g2.uH)


def test_back_side_and_rotated_pads_are_planned_on_their_own_layer(tmp_path):
    back = PB.fp("A", 10, 40, [PB.pad("1", 0, 0, "X", 0.5, 0.5)]).replace('"F.Cu"', '"B.Cu"')
    rot = PB.fp("B", 90, 40, [PB.pad("1", 0, 0, "X", 2.0, 0.4)]).replace("(at 0 0) (size", "(at 0 0 90) (size")
    B = RD.load(PB.write(tmp_path, back + rot))
    q = B["parts"]["B"]["pads"][0]
    assert abs(q["ex"] - 0.2) < 1e-6 and abs(q["ey"] - 1.0) < 1e-6          # rotated 90: the long axis runs in y
    g = PL.run(B, {}, None, ignore_routing=True)["gr"]
    t = {x["pad"]: x for x in next(iter(g.nets.values()))["terms"]}
    assert t["A.1"]["layers"] == [g.lid["B.Cu"]] and t["B.1"]["layers"] == [g.lid["F.Cu"]]
    assert next(iter(g.routes.values()))["vias"] >= 1                         # the net must change layer


def test_thin_keepout_reduces_capacity(tmp_path):
    thin = PB.keepout(40, 39.9, 60, 40.1, "thin")                               # 0.2 mm tall, the 4x4 sampling of the old code missed it
    a = PL.run(RD.load(PB.write(tmp_path, ""  )), {}, None, ignore_routing=True)["gr"]
    b = PL.run(RD.load(PB.write(tmp_path, thin, "k.kicad_pcb")), {}, None, ignore_routing=True)["gr"]
    c = b.cell_of(50, 40)
    assert min(b.capH[0][c], b.capV[0][c]) < min(a.capH[0][c], a.capV[0][c]) or b.capH[0][c] < a.capH[0][c]


def test_rail_demand_is_width_over_design_pitch_scaled_once(tmp_path):
    body = PB.fp("A", 10, 40, [PB.pad("1", 0, 0, "VBUS", 1, 1)]) + PB.fp("B", 90, 40, [PB.pad("1", 0, 0, "VBUS", 1, 1)])
    cons = {"power": {"VBUS": {"max_current_ma": 3000}}, "plan": {"track_mm": 0.127, "clearance_mm": 0.127, "design_track_mm": 0.2, "design_clearance_mm": 0.2}}
    g = PL.run(RD.load(PB.write(tmp_path, body)), cons, None, ignore_routing=True)["gr"]
    nt = g.nets["VBUS"]
    from fluxplace import constraints as CS
    w = CS.power_width_mm(cons, "VBUS", 0.127)
    assert nt["units"] == max(1, -(-int((w + 0.2) / 0.4 * 1e6) // 1000000)) or nt["units"] * 0.4 >= w + 0.2 - 1e-6
    c = g.cell_of(50, 40)
    demand_mm = nt["units"] * g.mult[c] * g.cpitch[c]                         # tracks of the local pitch the rail occupies
    assert abs(demand_mm - nt["units"] * 0.4) < 1e-6                           # scaled exactly once: design width, not width x ratio^2


def test_route_ready_plan_gate_is_off_by_default_and_warn_only(tmp_path):
    from fluxplace import preroute as PR
    assert PR.use_plan({}) is False and PR.use_plan({"route_ready": {"use_plan": True}}) is True
    p = channel_board(tmp_path, 16, gap=1.0)
    B = RD.load(p)
    f0, c0 = PR.run(B, {}, None)
    assert not [x for x in f0 if x["code"].startswith("PLAN_")]
    f1, c1 = PR.run(B, {}, None, plan=True)
    plan_f = [x for x in f1 if x["code"].startswith("PLAN_")]
    assert plan_f and all(x["level"] == "WARN" for x in plan_f)
    f2, _c2 = PR.run(B, {"route_ready": {"plan_gate": "fail"}}, None, plan=True)
    assert any(x["code"].startswith("PLAN_") and x["level"] == "FAIL" for x in f2)


def test_one_predicate_for_every_verdict(tmp_path):
    R = run(channel_board(tmp_path, 16, gap=1.0))
    fail, why, hard = PL.predicate(R)
    v = PL.verdict(R)
    assert fail == (v["result"] == "FAIL") and v["reasons"] == why
    from fluxplace import planopts as PO
    assert PO.passes(R) == (not fail)
    assert v["confidence"] < 50 if fail else True
    R2 = run(channel_board(tmp_path, 4, gap=30.0, walls=False))
    assert PL.verdict(R2)["result"] == ("FAIL" if PL.predicate(R2)[0] else "ok")


def test_pair_partner_gets_stats_and_budget_check(tmp_path):
    R = run(channel_board(tmp_path, 2, gap=30.0, walls=False, pair=True))
    assert "ETH_P0_N" in R["ns"] and R["ns"]["ETH_P0_N"]["len"] == R["ns"]["ETH_P0_P"]["len"] > 0


def _bus_board(tmp):
    """Eight 2-pad nets of one bus; a wall forces them through a channel."""
    return channel_board(tmp, 8, gap=14.0, walls=False)


def test_confidence_and_variants(tmp_path):
    R = run(channel_board(tmp_path, 4, gap=30.0, walls=False))
    v = PL.verdict(R)
    assert 0 <= v["confidence"] <= 100 and set(v["conf_terms"]) == {"routable_share", "headroom", "slack", "risk_frac"}
    R2 = run(channel_board(tmp_path, 16, gap=1.0))
    assert PL.verdict(R2)["confidence"] < v["confidence"]
    rows = PL.run_variants(RD.load(channel_board(tmp_path, 16, gap=1.0)), {}, None, True)
    assert [r["variant"] for r in rows] == ["mixed", "plane3v3", "8layer"] and rows[2]["layers"] == rows[0]["layers"] + 2


def test_return_path_and_zoning_and_corridor(tmp_path):
    from fluxplace import planrules as PRL
    # an rf net whose ends are on opposite sides of a 2-layer board changes layer without a GND via -> PLAN_RETURN_PATH needs planes;
    # here no plane layer exists, so no reference change is reported
    R = run(channel_board(tmp_path, 4, gap=30.0, walls=False))
    assert not [f for f in R["findings"] if f["code"] == "PLAN_RETURN_PATH"]
    # zoning: a switch-node inductor 2 mm from an RF part
    body = (PB.fp("U1", 20, 40, [PB.pad("1", 0, 0, "U1_SW", 0.8, 0.8), PB.pad("2", 2, 0, "VIN", 0.8, 0.8), PB.pad("3", 4, 0, "FB", 0.8, 0.8), PB.pad("4", 6, 0, "EN", 0.8, 0.8), PB.pad("5", 8, 0, "GND", 0.8, 0.8)])
            + PB.fp("L1", 25, 40, [PB.pad("1", 0, 0, "U1_SW", 1, 1), PB.pad("2", 3, 0, "VOUT", 1, 1)])
            + PB.fp("A1", 33, 40, [PB.pad("1", 0, 0, "RF_ANT", 1, 1), PB.pad("2", 3, 0, "RF_OUT", 1, 1)])
            + PB.fp("A2", 70, 40, [PB.pad("1", 0, 0, "RF_ANT", 1, 1), PB.pad("2", 3, 0, "RF_OUT", 1, 1)]))
    B = RD.load(PB.write(tmp_path, body, "z.kicad_pcb"))
    from fluxplace import netclass as NC2
    cls = NC2.classify(B, {}, None)
    z = PRL.zoning(B, cls, {})
    assert z and z[0]["code"] == "PLAN_ZONING" and "keep-away" in z[0]["msg"]
    assert not PRL.zoning(B, cls, {"zoning": {"keepaway_mm": 0.5}})


def test_bus_weave_and_part_in_corridor_codes_exist(tmp_path):
    assert {"PLAN_BUS_WEAVE", "PLAN_PART_IN_CORRIDOR", "PLAN_RETURN_PATH", "PLAN_ZONING", "PLAN_ESCAPE_OVERSUBSCRIBED",
            "PLAN_POWER_NO_PLANE"} <= set(PL.CODES)
    # a narrow channel board is fully used: corridor cells exist
    from fluxplace import planrules as PRL
    R = run(channel_board(tmp_path, 16, gap=1.0))
    assert PRL.corridor_cells(R["gr"], util=0.5)


def test_calibrate_and_write_constants(tmp_path):
    p = channel_board(tmp_path, 4, gap=30.0, walls=False)
    c = PL.calibrate(RD.load(p), {}, None, p)
    assert c["best"]["util"] in (0.6, 0.7, 0.8, 0.9, 1.0) and "hit" in c and "miss" in c
    t = tmp_path / "c.toml"
    t.write_text("[plan]\nutil = 0.8\ncell_mm = 2.0\n\n[fab]\nmin_track_mm = 0.1\n")
    PL.write_constants(str(t), {"util": 0.6, "jog_frac": 0.4})
    txt = t.read_text()
    assert "util = 0.6" in txt and "jog_frac = 0.4" in txt and "cell_mm = 2.0" in txt and "[fab]" in txt


def test_swap_candidates_listed_not_applied(tmp_path):
    p = channel_board(tmp_path, 4, gap=30.0, walls=False)
    B = RD.load(p)
    cons = {"swap": {"group": {"g": {"pads": ["L0.1", "L3.1"]}}}}
    sw, base = PL.swap_candidates(B, cons, None)
    assert sw and sw[0]["group"] == "g" and {q["net"] for q in B["pads"] if q["ref"] == "L0"} == {"SIG0"}
