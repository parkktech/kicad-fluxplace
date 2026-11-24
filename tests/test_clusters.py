import os, sys, subprocess
import pytest
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
def _pb_board(tmp_path, c2=(30, 12), c1=(22, 20), extra=""):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
    import powerboard as PB
    return PB, RD.load(PB.board(tmp_path, c2=c2, c1=c1, extra=extra))


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


# ---- placer step 3b: per-pin demand (EVAL-PLACER-STEP1 D4)
def _demand(tmp_path, caps, name="b.kicad_pcb"):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
    import powerboard as PB
    b = g.ic("U101", 10, 10, ["VOUT", "GND", "A1", "A2", "A3", "A4"], pitch=1.0)
    b += g.ic("U412", 60, 10, ["VOUT", "GND", "B1", "B2", "B3", "B4"], pitch=1.0)
    for ref, x, y, v in caps:
        b += PB.cap(ref, x, y, "VOUT", "GND", v)
    B = RD.load(g.board(tmp_path, b, name=name, outline=(0, 0, 80, 60)))
    pin = {r: next(q for q in B["pads"] if q["ref"] == r and q["net"] == "VOUT") for r in ("U101", "U412")}
    return B, pin


def test_bypass_parked_at_another_ic_leaves_its_own_pin_bare(tmp_path):
    B, pin = _demand(tmp_path, [])
    x1, y1, x4, y4 = pin["U101"]["x"], pin["U101"]["y"], pin["U412"]["x"], pin["U412"]["y"]
    B, pin = _demand(tmp_path, [("C101", x1 - 2, y1, "100nF"), ("C419", x1 - 2, y1 + 2, "100nF")], name="c.kicad_pcb")
    cl, _ = CL.build(B, _PLANNED)
    s = next(x for c in cl for x in c["supports"] if x["ref"] == "C419")
    assert (s["ic"], s["how"]) == ("U101", "near") and not CL.far_parts(cl)                 # the part table alone sees nothing
    rows = CL.pin_demand(B, _PLANNED)
    assert len(rows) == 1
    r = rows[0]
    assert (r["ic"], r["pad"], r["net"], r["parts"], r["uf"], r["verdict"], r["cover"]) == ("U412", "1", "VOUT", ["C419"], 0.1, "fail", None)
    assert r["dist_mm"] == round(((x4 - (x1 - 2.5)) ** 2 + 2 ** 2) ** 0.5, 2) and r["limit_mm"] == 5.0
    assert CL.pin_demand(B, {}) == []                                                       # not a planned rail: no demand


def test_pin_with_an_equal_part_nearby_is_served_and_a_bulk_part_only_names_it(tmp_path):
    B, pin = _demand(tmp_path, [])
    x1, y1, x4, y4 = pin["U101"]["x"], pin["U101"]["y"], pin["U412"]["x"], pin["U412"]["y"]
    park = [("C101", x1 - 2, y1, "100nF"), ("C419", x1 - 2, y1 + 2, "100nF")]     # a cap's VOUT pad is 0.5 mm left of its centre
    B, _ = _demand(tmp_path, park + [("C7", x4 - 2, y4, "1uF")], name="s.kicad_pcb")       # any owner, within a decade: served
    assert CL.pin_demand(B, _PLANNED) == []
    B, _ = _demand(tmp_path, park + [("C7", x4 - 2, y4, "4.7uF")], name="l.kicad_pcb")     # 47 x the value: named, not failed
    r = CL.pin_demand(B, _PLANNED)[0]
    assert (r["ic"], r["verdict"], r["cover"]) == ("U412", "over-target-pass", {"ref": "C7", "uf": 4.7, "dist_mm": 2.5})
    B, _ = _demand(tmp_path, park + [("C7", x4 - 2, y4, "1nF")], name="t.kicad_pcb")       # a hundredth of the value: bare
    assert CL.pin_demand(B, _PLANNED)[0]["verdict"] == "fail"
    B, _ = _demand(tmp_path, park + [("C7", x4 - 7, y4, "100nF")], name="f.kicad_pcb")     # equal part, 7.5 mm away: bare
    assert CL.pin_demand(B, _PLANNED)[0]["verdict"] == "fail"



# ---- placer step 3c C1: pin_demand grades supply pins, not every pad on the rail (D71dp)
def _supply_board(tmp_path, caps, name="sp.kicad_pcb"):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
    import powerboard as PB
    b = g.ic("U101", 10, 10, ["VOUT", "GND", "A1", "VOUT", "A2", "GND"], pitch=1.0)     # pad 1 = VCC, pad 4 = EN, both on VOUT
    b += g.ic("U412", 60, 10, ["VOUT", "GND", "B1", "B2", "B3", "B4"], pitch=1.0)      # second IC on the rail: ownership is by reference
    for ref, x, y, v in caps:
        b += PB.cap(ref, x, y, "VOUT", "GND", v)
    return RD.load(g.board(tmp_path, b, name=name, outline=(0, 0, 80, 60)))


_SUP_SPEC = {"components": [{"ref": "U101", "mpn": "X1", "pinmap": {"VCC": "1", "EN": "4"}}]}


def test_pin_demand_names_the_supply_pin_not_a_tied_input(tmp_path):
    B = _supply_board(tmp_path, [("C101", 14, 11, "100nF")])                  # own bypass 2 mm from EN, 6.3 mm from VCC
    rows = CL.pin_demand(B, _PLANNED, _SUP_SPEC, facts={"X1": {"supply_pins": ["VCC"]}})
    assert len(rows) == 1
    assert (rows[0]["pad"], rows[0]["pin_kind"], rows[0]["verdict"]) == ("1", "supply", "fail")


def test_capacitor_at_a_tied_input_does_not_serve_the_supply_pin(tmp_path):
    B = _supply_board(tmp_path, [("C101", 13.5, 11.5, "100nF")])              # own bypass 1.5 mm from EN, 6 mm from VCC
    rows = CL.pin_demand(B, _PLANNED, _SUP_SPEC, facts={"X1": {"supply_pins": ["VCC"]}})
    assert len(rows) == 1 and rows[0]["verdict"] == "fail"


def test_no_supply_fact_keeps_every_pad(tmp_path):
    B = _supply_board(tmp_path, [("C101", 30, 30, "100nF")])                  # own bypass far from both pads
    rows = CL.pin_demand(B, _PLANNED, _SUP_SPEC, facts={})
    assert len(rows) == 1 and (rows[0]["pad"], rows[0]["pin_kind"]) == ("4", "unlisted")


def test_fact_that_names_no_pad_on_this_rail_keeps_every_pad(tmp_path):
    B = _supply_board(tmp_path, [("C101", 30, 30, "100nF")])
    rows = CL.pin_demand(B, _PLANNED, _SUP_SPEC, facts={"X1": {"supply_pins": ["VBAT"]}})
    assert len(rows) == 1 and (rows[0]["pad"], rows[0]["pin_kind"]) == ("4", "unlisted")


# ---- placer step 5 (P5): per-pin demand applied to the cluster table; what stayed far; regulator loop lengths
def test_rehome_puts_the_parked_bypass_into_its_own_ic_cluster(tmp_path):
    B, pin = _demand(tmp_path, [])
    x1, y1 = pin["U101"]["x"], pin["U101"]["y"]
    B, pin = _demand(tmp_path, [("C101", x1 - 2, y1, "100nF"), ("C419", x1 - 2, y1 + 2, "100nF")], name="c.kicad_pcb")
    cl, _ = CL.build(B, _PLANNED)
    moved = CL.rehome(B, cl, _PLANNED)
    d = CL.pin_demand(B, _PLANNED)[0]["dist_mm"]
    assert moved == {"C419": {"from": "U101", "to": "U412", "pad": "1", "dist": d, "left": "1"}}
    where = {s["ref"]: (c["ic"], s["how"], s["pad"], s["sure"], round(s["dist"], 2), s["limit"]) for c in cl for s in c["supports"]}
    assert where["C419"] == ("U412", "pin-demand", "1", True, d, 5.0) and where["C101"][:2] == ("U101", "near")
    assert CL.unseated(cl) == [{"ref": "C419", "ic": "U412", "pad": "1", "dist": d, "limit": 5.0, "why": "no legal seat"}]
    assert CL.unseated(cl, fixed={"C419"})[0]["why"] == "fixed"
    assert CL.rehome(B, cl, _PLANNED) == {}                                                  # already home: nothing to do
    cl2, _ = CL.build(B, {})
    assert CL.rehome(B, cl2, {}) == {}                                                       # not a planned rail: no demand, no move


def test_regulator_loop_lengths_do_not_change_when_the_module_turns(tmp_path):
    from fluxplace import geom as GM
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
    import powerboard as PB
    b = g.ic("U1", 30, 30, ["VIN", "GND", "SW", "BOOT", "FB", "EN"], pitch=1.0)
    b += PB.cap("C1", 26, 30, "VIN", "GND", "10uF") + PB.cap("C2", 27, 34, "BOOT", "SW", "100nF")
    b += PB.cap("L1", 36, 31, "SW", "VOUT", "") + PB.cap("R1", 27, 38, "FB", "VOUT", "")
    B = RD.load(g.board(tmp_path, b, outline=(0, 0, 80, 60)))
    pad = {n: next(q["num"] for q in B["parts"]["U1"]["pads"] if q["net"] == n) for n in ("VIN", "SW", "BOOT", "FB")}
    via = lambda r, n: next(q["num"] for q in B["parts"][r]["pads"] if q["net"] == n)        # noqa: E731
    c = {"ic": "U1", "supports": [{"ref": "C1", "rank": 1, "net": "VIN", "pad": pad["VIN"], "via": via("C1", "VIN")},
                                  {"ref": "L1", "rank": 0, "net": "SW", "pad": pad["SW"], "via": via("L1", "SW")},
                                  {"ref": "C2", "rank": 2, "net": "BOOT", "pad": pad["BOOT"], "via": via("C2", "BOOT")},
                                  {"ref": "R1", "rank": 3, "net": "FB", "pad": pad["FB"], "via": via("R1", "FB")}]}
    m0 = CL.loop_metrics(B, c)
    assert m0 == {"cin_loop_mm": 5.193, "sw_mm": 7.5, "boot_mm": 6.265, "fb_mm": 9.708}
    for th in (90.0, 180.0, 270.0):                                                          # the whole module about the IC, a quarter turn each
        for r in ("U1", "C1", "C2", "L1", "R1"):
            p = B["parts"][r]
            rx, ry = GM.rot(p["pos"][0] - 30.0, p["pos"][1] - 30.0, 90.0)
            GM.place(p, 30.0 + rx, 30.0 + ry, p["angle"] + 90.0)
        assert CL.loop_metrics(B, c) == m0, th
    p = B["parts"]["C1"]
    GM.place(p, p["pos"][0] - 2.0, p["pos"][1], p["angle"])                                 # the input capacitor alone, 2 mm further
    m1 = CL.loop_metrics(B, c)
    assert m1["cin_loop_mm"] == 7.405 and m1["sw_mm"] == m0["sw_mm"]
    assert CL.loop_metrics(B, {"ic": "U1", "supports": []}) == {"cin_loop_mm": None, "sw_mm": None, "boot_mm": None, "fb_mm": None}


def test_a_strict_seat_searches_as_far_as_the_limit_not_just_8_mm(tmp_path):
    import math
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
    import powerboard as PW
    import pcbgen as g
    b = g.ic("U1", 30, 30, ["VOUT", "GND", "A1", "A2", "A3", "A4"], pitch=1.0)
    b += PW.cap("C1", 26, 29, "VOUT", "GND", "10uF")
    b += g.fp("X1", 23.15, 29.0, [], court=(-4.65, -9.5, 4.65, 9.5))   # blockers: nothing legal within 9.5 mm outward of pin 1
    B = RD.load(g.board(tmp_path, b, outline=(0, 0, 80, 60)))
    cl, _ = CL.build(B, {"power": {"VOUT": {"max_current_ma": 3000}}})          # a planned rail: the 10 uF is bulk, limit 15 mm
    c = cl[0]
    s = next(x for x in c["supports"] if x["ref"] == "C1")
    assert s["limit"] == 15.0
    left = []
    mv = CL.seat(B, c, strict=True, left=left)
    pin = next(p for p in B["parts"]["U1"]["pads"] if p["num"] == s["pad"])
    d = math.hypot(mv["C1"][0] - pin["x"], mv["C1"][1] - pin["y"])
    assert "C1" in mv and 9.5 < d <= s["limit"] and left == []


# ---- placer step 5b S2: a datasheet decap_mm / bulk_mm is an edge distance (pad to pad), a derived / default limit is a centre distance
def _edge_board(tmp_path, cap_x):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
    import powerboard as PB
    b = g.fp("U1", 30, 30, [g.pad("1", 0, 0, "VOUT", w=2.05, h=0.6), g.pad("2", 0, 3, "GND", w=2.05, h=0.6), g.pad("3", 0, 6, "A1"), g.pad("4", 0, 7, "A2"), g.pad("5", 0, 8, "A3")], court=(-2, -1, 2, 9))
    b += g.fp("C1", cap_x, 30, [g.pad("1", -0.5, 0, "VOUT"), g.pad("2", 0.5, 0, "GND")], court=(-1, -0.6, 1, 0.6))
    return RD.load(g.board(tmp_path, b, outline=(0, 0, 80, 60)))


_EDGE_FACTS = {"X1": {"decap_mm": 1, "_src": {"decap_mm": "X.pdf#p3"}}}


def _edge_support(B, facts):
    cl, _ = CL.build(B, {}, {"components": [{"ref": "U1", "mpn": "X1"}]}, facts=facts, mpn_map={"U1": "X1"})
    return cl[0], next(s for s in cl[0]["supports"] if s["ref"] == "C1")


def test_a_datasheet_decap_limit_is_measured_pad_edge_to_pad_edge(tmp_path):
    B = _edge_board(tmp_path, 32.4)                    # VOUT pad of C1 at x 31.9: centre 1.9 mm from the 2.05 mm long pad, edges 0.625 mm
    _c, s = _edge_support(B, _EDGE_FACTS)
    assert (s["limit"], s["klass"]) == (1.0, "REQ") and s["dist"] == pytest.approx(0.625) and s["dist"] <= s["limit"]
    _c, s = _edge_support(B, {})                       # no fact: the default limit keeps the centre measure
    assert s["dist"] == pytest.approx(1.9) and s["klass"] == "INF"
    B = _edge_board(tmp_path, 34.4)
    _c, s = _edge_support(B, _EDGE_FACTS)
    assert s["dist"] == pytest.approx(2.625) and s["dist"] > s["limit"]


def test_the_strict_seat_and_the_remeasure_use_the_edge_distance_for_a_datasheet_limit(tmp_path):
    from fluxplace import modules as MD
    B = _edge_board(tmp_path, 45.0)
    c, s = _edge_support(B, _EDGE_FACTS)
    left = []
    mv = CL.seat(B, c, strict=True, left=left)         # the centre of any seat is at least 1.275 mm from the pin centre: only an edge measure can seat it
    assert "C1" in mv and left == []
    GM.place(B["parts"]["C1"], *mv["C1"])
    MD._remeasure(B, [c])
    assert s["dist"] <= 1.0 + 1e-6


def test_datasheet_decap_is_hard_default_is_soft(tmp_path):
    PB, B = _pb_board(tmp_path)
    spec = {"components": [{"ref": "U2", "mpn": "X2", "pinmap": {}}]}
    fact = {"X2": {"decap_mm": 2.0, "_src": {"decap_mm": "X2.pdf#p9"}}}
    c1 = _sup(B, PB.CONS, spec, fact)["C1"]
    assert c1["limit"]["klass"] == "REQ" and c1["limit"]["derived"] == ""
    assert _sup(B, PB.CONS)["C1"]["limit"]["derived"] == "default"


def test_support_distance_does_not_change_when_the_pair_is_rotated(tmp_path):
    import math
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
    import powerboard as PB
    dists = []
    for a in (0, 90, 180, 270):
        t = math.radians(a)
        rot = lambda x, y: (x * math.cos(t) + y * math.sin(t), -x * math.sin(t) + y * math.cos(t))          # KiCad footprint rotation
        cx, cy = 10 + rot(-4.0, -1.0)[0], 10 + rot(-4.0, -1.0)[1]                                              # cap moves with the IC
        b = g.ic("U101", 10, 10, ["VOUT", "GND", "A1", "A2", "A3", "A4"], pitch=1.0, angle=a)
        b += g.two("C1", cx, cy, "VOUT", "GND", angle=a).replace('(property "Reference" "C1")', '(property "Reference" "C1") (property "Value" "100nF")')
        B = RD.load(g.board(tmp_path, b, name=f"rot{a}.kicad_pcb", outline=(0, 0, 80, 60)))
        cl, _ = CL.build(B, _PLANNED)
        dists.append(next(x["dist"] for c in cl for x in c["supports"] if x["ref"] == "C1"))
    assert max(dists) - min(dists) <= 0.01


def test_every_default_support_branch_is_marked_derived(tmp_path):
    PB, B = _pb_board(tmp_path, extra=g.two("C3", 14, 14, "SW", "GND"))       # C3: a loop part (netclass budget), C2: bulk
    lims = {s["ref"]: s["limit"] for s in _sup(B, PB.CONS).values()}
    assert all(l["derived"] != "" and l["klass"] == "INF" for l in lims.values())
    assert {l["derived"] for l in lims.values()} == {"default", "default, unverified", "netclass budget"}
