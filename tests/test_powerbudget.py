import copy, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
import powerboard as PB
from fluxplace import channel as CH, powerbudget as PBG, routediag as RD


def test_sheet_resistance_and_squares():
    rows = PB.rows()
    assert abs(PBG.sheet_mohm_sq(rows, "F.Cu", 20.0) * 10 - 4.91) < 0.01          # 10 mm x 1 mm = 10 sq
    assert PBG.sheet_mohm_sq(rows, "F.Cu", 85.0) > PBG.sheet_mohm_sq(rows, "F.Cu", 20.0)


def _rail(**kw):
    r = {"net": "X", "current_a": 4.2, "sizing": {"width_outer": 2.2, "width_inner": 5.65}, "temp_c": 20.0, "detour": 1.0, "crowding": 1.0,
         "floor_mm": 0.127, "vias_needed": 1, "via_drill_mm": 0.2,
         "d": {"net": "X", "current_a": 4.2, "drop_mv_max": 150.0, "drop_mv_target": 75.0, "source": "t"}}
    r.update(kw)
    return r


def test_section0_table_and_reach_inverse():
    rows = PB.rows()
    r = _rail()
    # +5V: 150 mV / 4.2 A = 35.7 mohm = 72.7 squares, 10.9 mm of copper at 0.15 mm
    assert abs(r["d"]["drop_mv_max"] / 4.2 / PBG.sheet_mohm_sq(rows, "F.Cu", 20.0) - 72.7) < 0.1
    assert abs(PBG.reach_mm(r, "F.Cu", 0.15, "max", rows) - 10.9) < 0.05
    for tier in ("target", "max"):                                               # inverse of estimate, within 1e-6
        L = PBG.reach_mm(r, "F.Cu", 0.5, tier, rows)
        est = PBG.estimate(r, (0, 0), (L, 0), "F.Cu", 0.5, rows)
        assert abs(est["metrics"][0]["value"] - r["d"]["drop_mv_" + tier]) < 1e-6
    w = PBG.width_req_mm(r, 5.0, "F.Cu", "max", rows)
    assert abs(PBG.reach_mm(r, "F.Cu", w, "max", rows) - 5.0) < 1e-6 or w == 2.2   # IPC width floor may dominate


def test_half_ounce_halves_the_reach():
    r = _rail()
    a = PBG.reach_mm(r, "F.Cu", 1.0, "max", PB.rows(0.035))
    b = PBG.reach_mm(r, "F.Cu", 1.0, "max", PB.rows(0.0175))
    assert abs(b * 2 - a) < 1e-9


def test_vias_cost_reach_on_inner_layers():
    rows = PB.rows()
    r = _rail()
    assert PBG.reach_mm(r, "In2.Cu", 1.0, "max", rows) < PBG.reach_mm(r, "F.Cu", 1.0, "max", rows)
    assert PBG._r_vias(r, rows, "F.Cu") == 0.0 and PBG._r_vias(r, rows, "In2.Cu") > 0
    r4 = _rail(vias_needed=4)
    assert abs(PBG._r_vias(r4, rows, "In2.Cu") * 4 - PBG._r_vias(r, rows, "In2.Cu")) < 1e-9


def test_identity_planned_and_measured_share_dcdrop_row():
    rows = PB.rows()
    d = {"net": "X", "current_a": 3.0, "drop_mv_max": 100.0, "drop_mv_target": 50.0, "source": "t"}
    contrib = [{"layer": "F.Cu", "w": 0.5, "mm": 12.0, "sq": 24.0}, {"layer": "F.Cu", "w": 2.0, "mm": 6.0, "sq": 3.0}]
    measured = {"found": True, "layer_sq": {"F.Cu": 27.0}, "via_list": [], "contrib": contrib}
    ps = {}
    for c in contrib:
        ps[c["layer"]] = ps.get(c["layer"], 0.0) + c["mm"] / c["w"]
    planned = {"found": True, "layer_sq": {"F.Cu": sum(PBG.synth_path(c["layer"], c["mm"], c["w"])["layer_sq"]["F.Cu"] for c in contrib)},
               "via_list": []}
    a, b = CH.dcdrop_row(d, measured, rows), CH.dcdrop_row(d, planned, rows)
    assert abs(a["metrics"][0]["value"] - b["metrics"][0]["value"]) < 1e-9 and a["verdict"] == b["verdict"]


def test_build_rails_and_edges(tmp_path):
    p = PB.board(tmp_path)
    B = RD.load(p)
    out = PBG.build(PB.CONS, B, rows=PB.rows())
    r = out["rails"]["VOUT"]
    assert r["current_a"] == 3.0 and r["drop"]["max_mv"] == 100.0
    assert abs(r["r_allow_mohm"]["max"] - 100 / 3) < 1e-9
    assert [c["layer"] for c in r["copper"]] == ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"]            # planes are not candidates
    assert r["choice"]["layers"][0] in ("F.Cu", "B.Cu")
    assert {e["load"] for e in r["edges"]} == {"J1", "U2"} and all(e["est"]["metrics"] for e in r["edges"])
    assert not [f for f in out["findings"] if f["level"] == "FAIL"]
    # no placement: no edges
    assert PBG.build(PB.CONS, None, rows=PB.rows())["rails"]["VOUT"]["edges"] == []


def test_source_behind_series_part_resolves_to_its_net_pad(tmp_path):
    B = RD.load(PB.board(tmp_path))
    c = copy.deepcopy(PB.CONS)
    c["dcdrop"] = []
    src, loads = PBG.resolve_ends(B, c, "VOUT")                                  # [power] source = "U1": no pad on VOUT, L1 links SW
    assert src["spec"].startswith("L1.") and src["pads"] == [("L1", "2")] and "series" in src["how"]
    assert {x["spec"] for x in loads} == {"J1", "U2"} and all(x["how"] == "inferred" for x in loads)
    c["dcdrop"] = PB.CONS["dcdrop"]
    src, loads = PBG.resolve_ends(B, c, "VOUT")
    assert src["spec"] == "L1" and src["how"] == "dcdrop.from" and loads[0]["how"] == "dcdrop.to"


def test_no_budget_row_is_a_fail_finding(tmp_path):
    c = copy.deepcopy(PB.CONS)
    c["dcdrop"] = []
    out = PBG.build(c, RD.load(PB.board(tmp_path)), rows=PB.rows())
    assert [f["code"] for f in out["findings"]] == ["POWER_PLAN_NO_BUDGET"]


def test_identity_routed_fixture_contrib_rows_replayed_as_synth_paths():
    import pytest
    pytest.importorskip("pcbnew")
    import test_channel as TC
    d = TC.Bd()
    d.pad(1, 0, 0, "P", 0.2)
    d.pad(1, 10, 0, "P", 0.2, ref="U2")
    d.pad(2, 60, 0, "P", 0.2, ref="U2")
    d.seg((0, 0), (10, 0), "P", w=1.0)
    d.seg((10, 0), (40, 0), "P", w=0.3)
    d.seg((40, 0), (60, 0), "P", w=0.15)
    cons = {"dcdrop": [{"net": "P", "current_a": 3.0, "drop_mv_max": 90, "drop_mv_target": 40, "from": "U1", "to": ["U2"], "source": "t"}]}
    row = CH.dcdrop_rows(d.b, cons, TC.ROWS)[0]
    path = row["paths"][0]
    sq = {}
    for c in path["contrib"]:
        p = PBG.synth_path(c["layer"], c["mm"], c["w"])
        for k, v in p["layer_sq"].items():
            sq[k] = sq.get(k, 0.0) + v
    replay = CH.dcdrop_row(cons["dcdrop"][0], {"found": True, "layer_sq": sq, "via_list": path["via_list"]}, TC.ROWS)
    assert abs(replay["metrics"][0]["value"] - row["metrics"][0]["value"]) < 0.1                # mV, same code


# ---- step-1 evaluation (D71di): exemption, copper temperature, layer choice text
def test_exempt_rail_is_info_not_fail_and_blank_reason_still_fails(tmp_path):
    c = copy.deepcopy(PB.CONS)
    c["dcdrop"] = []
    c["power"]["VOUT"].pop("source")
    c["power"]["VOUT"]["dcdrop_exempt"] = "series sense resistor: the drop is the design"
    out = PBG.build(c, RD.load(PB.board(tmp_path)), rows=PB.rows())
    assert [(f["code"], f["level"]) for f in out["findings"]] == [("POWER_PLAN_EXEMPT", "INFO")]          # and no source question
    assert "series sense resistor" in out["findings"][0]["msg"] and out["rails"]["VOUT"]["exempt"]
    assert "1 exempt by design (VOUT)" in PBG.summary(out)
    c["power"]["VOUT"]["dcdrop_exempt"] = "  "                                                       # the reason is required
    out = PBG.build(c, None, rows=PB.rows())
    assert [(f["code"], f["level"]) for f in out["findings"]] == [("POWER_PLAN_NO_BUDGET", "FAIL")] and "dcdrop_exempt" in out["findings"][0]["msg"]
    c = copy.deepcopy(PB.CONS)                                                                       # a row wins over the exemption
    c["power"]["VOUT"]["dcdrop_exempt"] = "x"
    assert PBG.build(c, None, rows=PB.rows())["rails"]["VOUT"]["exempt"] is None


def test_copper_is_planned_at_ambient_plus_the_trace_rise_and_a_row_temp_wins():
    r = PBG.build(PB.CONS, None, rows=PB.rows())["rails"]["VOUT"]
    assert r["rise_c"] == r["sizing"]["rise_c"] > 0 and r["temp_c"] == 85.0 + r["rise_c"]
    f = next(c for c in r["copper"] if c["layer"] == "F.Cu")
    assert abs(f["mohm_sq"] - PBG.sheet_mohm_sq(PB.rows(), "F.Cu", r["temp_c"])) < 1e-3
    c = copy.deepcopy(PB.CONS)
    c["dcdrop"][0]["temp_c"] = 60.0
    assert PBG.build(c, None, rows=PB.rows())["rails"]["VOUT"]["temp_c"] == 60.0


def test_choice_says_inner_when_no_outer_layer_holds_the_edge():
    cu = [{"layer": "F.Cu", "kind": "outer", "width_ipc_mm": 0.3, "reach_mm": {"max": 10.0}},
          {"layer": "In2.Cu", "kind": "inner", "width_ipc_mm": 0.7, "reach_mm": {"max": 40.0}}]
    rail = {"copper": cu, "drop": {"max_mv": 100}, "sizing": {}}
    ch = PBG._choose(rail, [(None, None, None, 30.0)])
    assert ch["layers"] == ["In2.Cu"] and "inner" in ch["why"]
    assert "outer layer holding" in PBG._choose(rail, [(None, None, None, 5.0)])["why"]


def test_role_needs_a_ground_side_and_a_pin_capacitor_keeps_the_class_budget(tmp_path):
    B = RD.load(PB.board(tmp_path))
    series = {"value": "100nF", "pads": [{"net": "A"}, {"net": "B"}]}
    assert PBG.role_of(series, "C9", "A", None, PB.CONS) == "sense"                                  # a series capacitor is not a bypass
    assert PBG.role_of(B["parts"]["C1"], "C1", "VOUT", None, PB.CONS) == "bypass"
    info = {"class": "bypass", "budget": 3.0, "reason": "IC pin to cap to ground", "weight": 8.0}
    row = PBG.support_row(B, PB.CONS, None, "C1", "VOUT", info)
    assert (row["target"], row["max"]) == (3.0, 3.0)                                                 # the pre-P3 gate (3 mm) does not move
    assert (PBG.support_row(B, PB.CONS, None, "C1", "VOUT", None)["max"]) == 5.0
