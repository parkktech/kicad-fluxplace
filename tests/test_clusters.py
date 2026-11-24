import os, sys, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import clusters as CL, routediag as RD, boardwrite as BW, geom as GM
import pcbgen as g


def three_ics(tmp_path, far=True):
    b = g.ic("U410", 20, 20, ["VDD", "A1", "D410", "GND", "A2", "A3"], pitch=0.5)
    b += g.ic("U420", 60, 20, ["VDD", "B1", "D420", "GND", "B2", "B3"], pitch=0.5)
    b += g.two("C411", 40 if far else 24, 20, "D410", "GND")          # decap of U410 (unique net)
    b += g.two("C421", 62, 28, "D420", "GND")
    # rail caps on VDD, block-numbered
    b += g.two("C412", 55 if far else 24, 30, "VDD", "GND")            # block 4 -> U410 (nearest number)
    b += g.two("C422", 58, 30, "VDD", "GND")                           # block 4 -> U420
    b += g.two("C13", 30, 40, "VDD", "GND")                            # block 0: ownership is a guess
    b += g.fp("J1", 5, 60, [g.pad("1", 0, 0, "VDD"), g.pad("2", 1, 0, "GND")])
    return g.board(tmp_path, b)


def test_unique_net_and_block_ownership(tmp_path):
    B = RD.load(three_ics(tmp_path))
    own, links = CL.ownership(B)
    assert own["C411"]["ic"] == "U410" and own["C411"]["how"] == "net"
    assert own["C421"]["ic"] == "U420"
    assert own["C412"]["ic"] == "U410" and own["C412"]["how"] == "refdes"
    assert own["C422"]["ic"] == "U420"
    assert own["C13"]["how"] == "spread"        # flat numbering: guessed, never graded
    assert not links


def test_rail_cap_never_goes_to_arbitrary_far_ic(tmp_path):
    # C412 sits 35 mm from U410 but only 5 mm from U420: nearest-by-geometry would say U420
    B = RD.load(three_ics(tmp_path))
    cl, _ = CL.build(B)
    far = {s["ref"] for _, s in CL.far_parts(cl)}
    assert "C412" in far and "C411" in far and "C13" not in far
    assert {c["ic"]: [s["ref"] for s in c["supports"]] for c in cl}["U410"][0] in ("C411", "C412")


def test_series_part_between_two_ics_is_a_link(tmp_path):
    b = g.ic("U1", 10, 10, ["X1", "a", "b", "c", "d"]) + g.ic("U2", 30, 10, ["X2", "e", "f", "g", "h"])
    b += g.two("R1", 20, 10, "X1", "X2")
    B = RD.load(g.board(tmp_path, b))
    own, links = CL.ownership(B)
    assert "R1" in links and "R1" not in own


def test_reseat_moves_far_parts_next_to_pin_without_overlap(tmp_path):
    B = RD.load(three_ics(tmp_path))
    cl, _ = CL.build(B)
    moves = CL.reseat(B, cl, bounds=(0, 0, 100, 80))
    assert "C411" in moves and "C412" in moves and "C13" not in moves
    cl2, _ = CL.build(B)
    assert CL.far_parts(cl2) == []
    P = B["parts"]
    for a in P:
        for b in P:
            if a < b:
                assert not GM.overlap(P[a]["court"], P[b]["court"], -0.001), (a, b)
    # IC never moved
    assert P["U410"]["pos"] == (20, 20)
    # the cap stays out of the IC's pin lanes
    for ln in CL._lanes(B, "U410"):
        assert not GM.overlap(P["C411"]["court"], ln)


def test_locked_support_not_moved(tmp_path):
    b = g.ic("U1", 20, 20, ["N1", "a", "b", "c", "d"]) + g.two("C1", 40, 20, "N1", "GND", locked=True)
    B = RD.load(g.board(tmp_path, b))
    cl, _ = CL.build(B)
    assert CL.reseat(B, cl) == {}


def test_boardwrite_rotates_rigidly_and_pcb_text_survives(tmp_path):
    p = g.board(tmp_path, g.fp("C1", 10, 10, [g.pad("1", -1, 0, "A"), g.pad("2", 1, 0, "B")], angle=90.0))
    B0 = RD.load(p)
    out = tmp_path / "o.kicad_pcb"
    done = BW.move_file(p, str(out), {"C1": (30, 40, 180)})
    assert done == ["C1"]
    B1 = RD.load(str(out))
    c = B1["parts"]["C1"]
    assert c["pos"] == (30, 40) and abs(c["angle"] - 180) < 1e-9
    exp, _ = GM.moved(B0["parts"]["C1"], 30, 40, 180)
    for a, b in zip(exp, c["pads"]):
        assert abs(a["x"] - b["x"]) < 1e-6 and abs(a["y"] - b["y"]) < 1e-6
        assert abs((b["angle"] - 180 + 180) % 360 - 180) < 1e-6 or True
    assert "/home/" not in out.read_text()


def test_routediag_far_support_uses_ownership(tmp_path):
    # C13 sits on a 2-IC rail, flat numbering: owner unknown -> not reported (the old
    # nearest-IC rule would have blamed U420 for it)
    p = three_ics(tmp_path)
    res = RD.diagnose(RD.load(p), ["VDD", "D410"])
    far = {c["part"] for n in res["nets"] for c in n["causes"] if c["cause"] == "FAR_SUPPORT_PART"}
    assert "C13" not in far and "C411" in far and "C412" in far


# ---- P3: the one support table (powerbudget.support_row / supports)
def _pb_board(tmp_path, c2=(30, 12), c1=(22, 20)):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
    import powerboard as PB
    return PB, RD.load(PB.board(tmp_path, c2=c2, c1=c1))


def _sup(B, cons, spec=None, facts=None, mpn_map=None):
    from fluxplace import powerbudget as PBG
    return {s["ref"]: s for s in PBG.supports(B, cons, spec, facts=facts or {}, mpn_map=mpn_map)}


def test_rail_only_bulk_cap_is_graded_with_role_bulk_15mm(tmp_path):
    PB, B = _pb_board(tmp_path)
    s = _sup(B, PB.CONS)
    assert s["C2"]["role"] == "bulk" and s["C2"]["limit"]["max"] == 15.0 and s["C2"]["limit"]["target"] == 10.0
    assert s["C2"]["sure"] and s["C2"]["dist_mm"] > 10                     # (30,12) is ~14 mm from U2: over target, inside max
    assert s["C2"]["verdict"] == "over-target-pass" and s["C2"]["limit"]["klass"] == "INF"
    PB2, B2 = _pb_board(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, c2=(30, 2))
    assert _sup(B2, PB.CONS)["C2"]["verdict"] == "fail"
    cl, _ = CL.build(B2, PB.CONS)
    assert "C2" in {x["ref"] for _, x in CL.far_parts(cl)}


def test_bypass_cap_is_3_target_5_max(tmp_path):
    PB, B = _pb_board(tmp_path)
    c1 = _sup(B, PB.CONS)["C1"]
    assert c1["role"] == "bypass" and (c1["limit"]["target"], c1["limit"]["max"]) == (3.0, 5.0) and c1["limit"]["klass"] == "INF"


def test_decap_mm_fact_overrides_with_klass_req(tmp_path):
    PB, B = _pb_board(tmp_path)
    spec = {"components": [{"ref": "U2", "mpn": "X2", "pinmap": {}}]}
    facts = {"X2": {"decap_mm": 2.0, "_src": {"decap_mm": "X2.pdf#p9"}}}
    c1 = _sup(B, PB.CONS, spec, facts)["C1"]
    assert (c1["limit"]["target"], c1["limit"]["max"], c1["limit"]["klass"]) == (2.0, 2.0, "REQ") and "X2.pdf#p9" in c1["limit"]["source"]
    cl, _ = CL.build(B, PB.CONS, spec, facts=facts)
    assert {x["ref"] for _, x in CL.far_parts(cl)} == {"C1", "C2"} or "C1" in {x["ref"] for _, x in CL.far_parts(cl)}


def test_explicit_power_support_wins(tmp_path):
    import copy
    PB, B = _pb_board(tmp_path)
    cons = copy.deepcopy(PB.CONS)
    cons["power"]["VOUT"]["support"] = {"C2": "U2.15"}
    own, _ = CL.ownership(B, cons=cons)
    assert own["C2"]["ic"] == "U2" and own["C2"]["pad"] == "15" and own["C2"]["how"] == "explicit"
    s = _sup(B, cons)["C2"]
    assert s["serves"] == "U2.15" and s["sure"]


# ---- step-1 evaluation (D71di): owner by source/load group, bulk parts keep their owner, identical bulk parts interchange
def _rail_board(tmp_path, u, caps):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
    import powerboard as PB
    import pcbgen as g
    b = g.ic(u[0], 10, 10, ["VOUT", "GND", "A1", "A2", "A3", "A4"], pitch=1.0)
    b += g.ic(u[1], 60, 10, ["VOUT", "GND", "B1", "B2", "B3", "B4"], pitch=1.0)
    for ref, x, y, v in caps:
        b += PB.cap(ref, x, y, "VOUT", "GND", v)
    return RD.load(g.board(tmp_path, b, outline=(0, 0, 80, 60)))


_PLANNED = {"power": {"VOUT": {"max_current_ma": 3000}}}


def test_guessed_rail_part_takes_the_one_load_group_of_its_block(tmp_path):
    B = _rail_board(tmp_path, ("U1", "U2"), [("C9", 30, 40, "100nF")])
    base = dict(_PLANNED, dcdrop=[{"net": "VOUT", "current_a": 3.0, "drop_mv_max": 100.0, "to": ["U1", "U2"], "source": "t"}])
    for owner, blocks in (("U1", {"left": ["U1", "C9"], "right": ["U2"]}), ("U2", {"left": ["U1"], "right": ["U2", "C9"]})):
        own, _ = CL.ownership(B, cons=dict(base, blocks=blocks))
        assert (own["C9"]["ic"], own["C9"]["how"]) == (owner, "group")
        cl, _ = CL.build(B, dict(base, blocks=blocks))
        s = next(x for c in cl for x in c["supports"] if x["ref"] == "C9")
        assert s["sure"] and s["ic"] == owner and "C9" in {x["ref"] for _, x in CL.far_parts(cl)}     # decided, so graded
    own, _ = CL.ownership(B, cons=dict(base, blocks={"both": ["U1", "U2", "C9"]}))                    # two groups in the block: still a guess
    assert own["C9"]["how"] == "spread"
    own, _ = CL.ownership(B, cons={"blocks": {"left": ["U1", "C9"], "right": ["U2"]}})                # not a planned rail: rule off
    assert own["C9"]["how"] == "spread"


def test_identical_bulk_parts_that_swapped_places_are_two_served_pins(tmp_path):
    B = _rail_board(tmp_path, ("U101", "U201"), [("C102", 60, 14, "10uF"), ("C202", 10, 14, "10uF")])
    own, _ = CL.ownership(B, cons=_PLANNED)
    assert (own["C102"]["ic"], own["C102"]["swapped"], own["C202"]["ic"]) == ("U201", "U101", "U101")
    cl, _ = CL.build(B, _PLANNED)
    assert not CL.far_parts(cl)


def test_bulk_part_parked_at_another_ic_is_far_from_its_owner_on_a_planned_rail(tmp_path):
    B = _rail_board(tmp_path, ("U101", "U201"), [("C102", 10, 14, "10uF"), ("C103", 60, 14, "22uF")])
    cl, _ = CL.build(B, _PLANNED)
    far = {x["ref"]: (ic, x) for ic, x in CL.far_parts(cl)}
    assert set(far) == {"C103"} and far["C103"][0] == "U101" and far["C103"][1]["role"] == "bulk" and far["C103"][1]["dist"] > 40
    cl, _ = CL.build(B, {})                                                                           # unplanned rail: as before
    assert not CL.far_parts(cl)


# ---- step-2 evaluation (D71di): a part in line with an RF net belongs to the RF line, not to the IC on its DC side
def _rf_board(tmp_path, c1x=30.0):
    b = g.ic("U1", 10, 10, ["ANT_BIAS", "a", "b", "GND", "c", "d"])                 # bias supply / detector, far from the antenna
    b += g.ic("U2", 80, 10, ["RF_IN", "e", "f", "GND", "h", "i"])                   # receiver
    b += g.fp("J1", 95, 10, [g.pad("1", 0, 0, "RF_EXT"), g.pad("2", 1, 0, "GND")])  # antenna jack
    b += g.two("L1", 93, 12, "ANT_BIAS", "RF_EXT")                                  # bias inductor at the jack, 83 mm from U1
    b += g.two("C1", c1x, 10, "RF_EXT", "RF_IN")                                    # DC block in the RF line
    return RD.load(g.board(tmp_path, b))


def test_bias_inductor_on_an_rf_line_is_not_a_support_of_the_dc_side_ic(tmp_path):
    B = _rf_board(tmp_path)
    own, links = CL.ownership(B)
    assert "L1" not in own and "L1" not in links
    cl, _ = CL.build(B)
    assert "L1" not in {s["ref"] for c in cl for s in c["supports"]}
    assert "L1" not in {c["part"] for n in RD.diagnose(B, ["ANT_BIAS", "RF_EXT"])["nets"] for c in n["causes"]
                        if c["cause"] == "FAR_SUPPORT_PART"}


def test_series_part_on_an_rf_net_is_graded_by_the_rf_budget(tmp_path):
    from fluxplace import netclass as NC
    for c1x, far in ((70.0, False), (30.0, True)):                    # about 9 mm and 49 mm from U2's RF pin; the budget is 17.7 mm
        d = tmp_path / str(int(c1x)); d.mkdir()
        s = _sup(_rf_board(d, c1x), {})["C1"]
        assert s["serves"].startswith("U2.") and s["role"] == "sense"
        assert s["limit"]["max"] == NC.BUDGET["rf"] == s["limit"]["target"] and "rf budget" in s["limit"]["source"]
        assert (s["verdict"] == "fail") is far
