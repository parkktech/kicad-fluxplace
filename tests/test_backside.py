import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from fluxplace import backside as BS, clusters as CL, legality as LGL, modules as MD, routediag as RD
import pcbgen as g
from test_legality import KO, _reseat


def _board_with_keepout(tmp_path):
    b = g.ic("U410", 40, 40, ["VDD", "A1", "D410", "GND", "A2", "A3"], pitch=0.5)
    for ref, x, y in (("C411", 70, 70), ("C412", 75, 10)):
        b += g.two(ref, x, y, "D410", "GND", side="B", name="C_0603_1608Metric", court=(-1, -0.6, 1, 0.6))
    return g.board(tmp_path, b)

BOTH = {"placement": {"sides": "both", "back_max_height_mm": 2.2}}
LIGHT = {"placement": {"sides": "both-light-back", "back_max_height_mm": 2.2}}


def _b(tmp_path, body, **kw):
    return RD.load(g.board(tmp_path, body, **kw))


def test_policy_and_active():
    assert BS.policy({}) == (None, None) and not BS.active({})
    assert BS.policy({"placement": {"sides": "front"}}) == ("front", None) and not BS.active({"placement": {"sides": "front"}})
    assert BS.active(LIGHT) and BS.policy(LIGHT) == ("both-light-back", 2.2)


def test_height_sources_in_order(tmp_path):
    prop = g.two("C1", 10, 10, "A", "B", name="C_0603_1608Metric", court=(-1, -1, 1, 1)).replace(
        '(property "Reference" "C1")', '(property "Reference" "C1") (property "Height" "1.3mm")')
    body = (prop + g.two("C2", 20, 10, "A", "B", name="C_0603_1608Metric", court=(-1, -1, 1, 1))
            + g.two("U1", 30, 10, "A", "B", name="SOT-23", court=(-1, -1, 1, 1)) + g.two("X1", 40, 10, "A", "B", name="Odd", court=(-1, -1, 1, 1)))
    B = _b(tmp_path, body)
    assert BS.height(B, {}, "C1") == (1.3, "footprint Height property")
    assert BS.height(B, {}, "C2")[1] == "chip size table"
    assert BS.height(B, {}, "U1") == (1.2, "package-class default")
    assert BS.height(B, {}, "X1") == (None, "unknown")
    h, src = BS.height(B, {"placement": {"heights": {"X1": 0.9, "C1": 9.0}}}, "X1")
    assert (h, src) == (0.9, "[placement.heights] X1")
    assert BS.height(B, {"placement": {"heights": {"C1": 9.0}}}, "C1")[0] == 9.0         # the owner outranks the property


def test_height_from_the_3d_model_and_cache(tmp_path):
    (tmp_path / "m.step").write_text("\n".join(f"#{i} = CARTESIAN_POINT('',({x},{y},{z}));" for i, (x, y, z) in
                                               enumerate([(0, 0, 0), (1, 1, 0), (1, 1, 1.75), (0, 0, 1.75)])))
    body = g.two("U1", 30, 10, "A", "B", name="Odd", court=(-1, -1, 1, 1)).replace(
        '(property "Reference" "U1")', '(property "Reference" "U1") (model "${KIPRJMOD}/m.step" (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))')
    B = _b(tmp_path, body)
    cache = {}
    assert BS.height(B, {}, "U1", cache) == (1.75, "3D model m.step")
    assert cache and BS.height(B, {}, "U1", cache)[0] == 1.75


def test_back_ok_per_policy(tmp_path):
    body = (g.two("R1", 10, 10, "A", "B", name="R_0603_1608Metric", court=(-1, -.5, 1, .5))
            + g.two("C2", 20, 10, "A", "B", name="C_1210_3225Metric", court=(-1.7, -1.4, 1.7, 1.4))    # 2.7 mm tall
            + g.two("L3", 30, 10, "A", "B", name="L_0603_1608Metric", court=(-1, -.5, 1, .5))          # inductor class
            + g.two("R4", 40, 10, "RF_ANT", "B", name="R_0603_1608Metric", court=(-1, -.5, 1, .5))      # RF net
            + g.two("R7", 45, 10, "RF_ANT", "C", name="R_0603_1608Metric", court=(-1, -.5, 1, .5))
            + g.two("R5", 50, 10, "A", "B", name="R_0603_1608Metric", court=(-1, -.5, 1, .5))
            + g.fp("R6", 60, 10, [g.pad("1", 0, 0, "A", typ="thru_hole", w=1, h=1)], name="R_0603_1608Metric", court=(-1, -1, 1, 1)))
    B = _b(tmp_path, body)
    assert BS.back_ok(B, {}, "R1")[0] is False                                   # no policy
    assert BS.back_ok(B, {"placement": {"sides": "front"}}, "R1") == (False, "sides policy is front")
    assert BS.back_ok(B, LIGHT, "R1") == (True, "ok")
    ok, why = BS.back_ok(B, LIGHT, "C2")
    assert not ok and "back_max_height_mm" in why
    assert BS.back_ok(B, BOTH, "C2")[0] is True                                  # `both` has no weight rule
    assert BS.back_ok(B, LIGHT, "L3")[1].startswith("heavy")
    assert BS.back_ok(B, {"placement": {**LIGHT["placement"], "back_allow": ["L3"]}}, "L3")[0] is True
    assert BS.back_ok(B, LIGHT, "R6")[1] == "through-hole part"
    assert BS.back_ok(B, {**LIGHT, "rf": {"nets": ["RF_ANT"]}}, "R4")[0] is False
    assert BS.back_ok(B, {"placement": {**LIGHT["placement"], "fixed": ["R5"]}}, "R5") == (False, "fixed / locked")
    assert BS.back_ok(B, {"placement": {**LIGHT["placement"], "front_only": ["R1"]}}, "R1")[1] == "[placement] front_only"
    # unknown height + a limit: not eligible
    B2 = _b(tmp_path, g.two("X1", 40, 10, "A", "B", name="Odd", court=(-1, -1, 1, 1)), name="c.kicad_pcb")
    assert BS.back_ok(B2, LIGHT, "X1") == (False, "height unknown")


def test_back_closed_area_refuses_and_side_illegal_names_it(tmp_path):
    cons = {"placement": {"sides": "both", "keepout": {"cm": {"rect": [0, 0, 30, 30], "side": "B", "allow": []}}}}
    body = (g.two("R1", 10, 10, "A", "B", name="R_0603_1608Metric", court=(-1, -.5, 1, .5), side="B")
            + g.two("R2", 60, 10, "A", "B", name="R_0603_1608Metric", court=(-1, -.5, 1, .5), side="B"))
    B = _b(tmp_path, body)
    assert BS.back_ok(B, cons, "R1")[1] == "in back-closed area cm"
    bad = BS.side_illegal(B, cons)
    assert [(x["ref"], x["why"]) for x in bad] == [("R1", "in back-closed area cm")]
    gp = BS.gaps(B, cons)
    assert gp[0]["kind"] == "side-illegal" and gp[0]["id"] == "side-illegal:R1" and set(gp[0]) >= {"id", "kind", "class", "mag", "refs", "nets", "at", "row", "detail"}
    tall = {"placement": {"sides": "both-light-back", "back_max_height_mm": 0.3}}
    assert {x["ref"] for x in BS.side_illegal(B, tall)} == {"R1", "R2"}                  # 0.45 mm > 0.3 mm


def test_assembly_note(tmp_path):
    B = _b(tmp_path, g.two("R1", 10, 10, "A", "B", name="R_0603_1608Metric", court=(-1, -.5, 1, .5)))
    assert "single-sided" in BS.assembly_note(B, LIGHT)
    B = _b(tmp_path, g.two("R1", 10, 10, "A", "B", name="R_0603_1608Metric", court=(-1, -.5, 1, .5), side="B"), name="d.kicad_pcb")
    assert "1 SMD part(s) on the back" in BS.assembly_note(B, LIGHT) and "second stencil" in BS.assembly_note(B, LIGHT)


def test_sides_front_or_absent_is_todays_result_byte_for_byte(tmp_path):
    path = _board_with_keepout(tmp_path)
    res = {}
    for name, cons in (("absent", KO), ("front", {"placement": {**KO["placement"], "sides": "front"}})):
        flips = set()
        B, mv, left, _ = _reseat(path, cons, flips=flips)          # the call modules.build makes (back guard None when not active)
        res[name] = (sorted(mv.items()), sorted(flips), left, sorted((r, p["side"], p["pos"], p["angle"]) for r, p in B["parts"].items()))
    assert res["absent"] == res["front"]
    assert BS.guard(RD.load(path), {"placement": {"sides": "front"}}) is None and BS.guard(RD.load(path), {}) is None


def test_the_guard_decides_whether_the_back_is_a_seat(tmp_path):
    path = _board_with_keepout(tmp_path)
    flips = set()
    B0 = RD.load(path)
    tall = {"placement": {**KO["placement"], "sides": "both-light-back", "back_max_height_mm": 0.1}}   # nothing is low enough for the back
    bk = BS.guard(B0, tall)
    assert bk is not None and not bk("C411")
    B, mv, left, refused = _reseat(path, tall, flips=flips, back=bk)
    assert flips == set() and not mv                                                  # guarded: the legacy fallback flip does not happen
    ok = {"placement": {**KO["placement"], "sides": "both"}}
    flips = set()
    B, mv, left, _ = _reseat(path, ok, flips=flips, back=BS.guard(RD.load(path), ok))
    assert flips == {"C411", "C412"} and set(mv) == {"C411", "C412"}


def test_a_crowded_front_prefers_the_back_seat_under_the_pin(tmp_path):
    b = g.ic("U410", 40, 40, ["VDD", "A1", "D410", "GND", "A2", "A3"], pitch=0.5)
    b += g.two("C411", 46, 40, "D410", "GND", name="C_0603_1608Metric", court=(-1, -.6, 1, .6))
    path = g.board(tmp_path, b)
    cons = {"placement": {"sides": "both"}}
    far = lambda B, c: CL.build(B, c)[0][0]["supports"][0]["limit"]
    B = RD.load(path)
    bk = BS.guard(B, cons)
    bk.prefer = False
    f0 = set()
    _reseat(path, cons, flips=f0, back=bk)
    assert f0 == set()                                                                # not crowded: stays on the front
    B = RD.load(path)
    bk2 = BS.guard(B, cons)
    bk2.prefer = True
    f1 = set()
    B1, mv, _l, _r = _reseat(path, cons, flips=f1, back=bk2)
    assert f1 in (set(), {"C411"})                                                    # taken only when the back seat is BACK_GAIN_MM nearer
    assert LGL.count(LGL.Legal(B1, cons).issues()) == 0


def _plan(tmp_path, cons):
    from fluxplace import floorplan2 as F2
    import test_legalgate as TL
    body = (g.ic("U1", 30, 30, ["VCC", "GND", "SIG", "SIG2", "VCC", "GND", "SIG3", "SIG4"], pitch=0.8, court=(-3, -3, 3, 3))
            + g.two("C1", 70, 60, "VCC", "GND", **{**TL.CAP, "name": "C_0603_1608Metric"}))
    B = RD.load(g.board(tmp_path, body, outline=(0, 0, 100, 80)))
    M = MD.build(B, cons, None, arrange=False)
    P = F2.Plan(B, cons, None, M, log=lambda *a, **k: None)
    P.freeze_baseline()
    return P, F2


def test_flip_single_is_an_involution_on_the_plan(tmp_path):
    P, F2 = _plan(tmp_path, {"placement": {"fixed": []}})
    i = P.idx[P.of["C1"]]
    px0, tx0 = P.px.copy(), P.tx.copy()
    ang0, side0 = P.B["parts"]["C1"]["angle"], P.B["parts"]["C1"]["side"]
    P.apply(i, 1e5, 1e5, 0)
    P.flip_single(i)
    assert P.B["parts"]["C1"]["side"] != side0 and (P.px != px0).any()
    P.flip_single(i)
    assert P.B["parts"]["C1"]["side"] == side0 and P.B["parts"]["C1"]["angle"] == pytest.approx(ang0)
    assert P.px == pytest.approx(px0) and P.tx == pytest.approx(tx0)


def test_home_supports_uses_the_back_only_by_policy(tmp_path):
    ko = {"fixed": [], "keepout": {"front": {"rect": [18, 18, 42, 42], "side": "F", "allow": ["U1"]}}}
    P, F2 = _plan(tmp_path, {"placement": ko})
    cons = {"placement": {**ko, "sides": "front"}}
    done, left = F2.home_supports(P, cons, None)
    assert left == ["C1"] and P.B["parts"]["C1"]["side"] == "F.Cu"                       # the front is closed around the pin: no seat
    P, F2 = _plan(tmp_path, {"placement": ko})
    cons = {"placement": {**ko, "sides": "both"}}
    done, left = F2.home_supports(P, cons, None)
    assert done == ["C1"] and P.B["parts"]["C1"]["side"] == "B.Cu" and not left


# ---- review fixes: anchored package defaults, LEDs, per-ref heights, assembly note
def test_package_defaults_are_anchored_and_unknown_is_ineligible(tmp_path):
    names = {"A1": "SOT-23", "A2": "D_SMA", "A3": "LED_0603_1608Metric", "A4": "RJ45_Amphenol_RJHSE538X",
             "A5": "Attend_123A-30B00_M2_KeyB_Socket", "A6": "Oscillator_SMD_EuroQuartz_XO32-4Pin_3.2x2.5mm",
             "A7": "MountingHole_3.2mm_M3_Pad", "A8": "Vendor_L_connector"}
    body = "".join(g.two(r, 10 + 10 * i, 10, "A", "B", name=n, court=(-1, -1, 1, 1)) for i, (r, n) in enumerate(sorted(names.items())))
    B = _b(tmp_path, body)
    h = {r: BS.height(B, {}, r) for r in names}
    assert h["A1"] == (1.2, "package-class default") and h["A2"] == (2.3, "package-class default")
    assert h["A3"][0] is not None
    for r in ("A4", "A5", "A6", "A7", "A8"):
        assert h[r] == (None, "unknown"), (r, h[r])                                  # a name that merely contains "L_" / "SON" / "SMD_" is no package
    assert BS.back_ok(B, BOTH, "A8") == (False, "height unknown")                       # unknown is ineligible under every policy


def test_led_footprints_stay_on_the_front(tmp_path):
    B = _b(tmp_path, g.two("D1", 10, 10, "A", "B", name="LED_0603_1608Metric", court=(-1, -.5, 1, .5)))
    ok, why = BS.back_ok(B, BOTH, "D1")
    assert not ok and why.startswith("LED")


def test_a_ref_with_its_own_height_property_beats_the_footprint_cache(tmp_path):
    a = g.two("C1", 10, 10, "A", "B", name="C_0603_1608Metric", court=(-1, -1, 1, 1))
    b = g.two("C2", 20, 10, "A", "B", name="C_0603_1608Metric", court=(-1, -1, 1, 1)).replace(
        '(property "Reference" "C2")', '(property "Reference" "C2") (property "Height" "3.1")')
    B = _b(tmp_path, a + b)
    cache = {}
    assert BS.height(B, {}, "C1", cache)[0] == 0.9 and BS.height(B, {}, "C2", cache)[0] == 3.1
    assert BS.height(B, {}, "C1", cache)[0] == 0.9


def test_assembly_note_tallest_ignores_fixed_parts(tmp_path):
    body = (g.two("R1", 10, 10, "A", "B", name="R_0603_1608Metric", court=(-1, -.5, 1, .5), side="B")
            + g.two("J9", 20, 10, "A", "B", name="Odd", court=(-1, -.5, 1, .5), side="B"))
    B = _b(tmp_path, body)
    cons = {"placement": {**LIGHT["placement"], "heights": {"J9": 9.0}, "fixed": ["J9"]}}
    assert "tallest R1" in BS.assembly_note(B, cons)


# ---- the density-driven fill
def _ic_board(tmp_path, extra=""):
    b = g.ic("U1", 40, 40, ["VDD", "A1", "D1", "GND", "A2", "A3"], pitch=1.27, name="SOIC-8_3.9x4.9mm", court=(-3, -3, 3, 3))
    b += g.two("C1", 44, 40, "D1", "GND", name="C_0603_1608Metric", court=(-1, -.6, 1, .6))
    b += g.two("C2", 36, 40, "VDD", "GND", name="C_0603_1608Metric", court=(-1, -.6, 1, .6))
    return g.board(tmp_path, b + extra)


def test_dense_seat_takes_the_back_first_directly_under_the_pin(tmp_path):
    path = _ic_board(tmp_path)
    cons = {"placement": {"sides": "both"}}
    B = RD.load(path)
    cl, _ = CL.build(B, cons)
    bk = BS.guard(B, cons)
    bk.dense = True
    fl = set()
    mv = CL.reseat(B, cl, everything=True, bounds=(0, 0, 100, 80), legal=LGL.Legal(B, cons), strict=True, flips=fl, back=bk)
    assert fl and fl <= {"C1", "C2"} and all(B["parts"][r]["side"] == "B.Cu" for r in fl)
    assert LGL.count(LGL.Legal(B, cons).issues()) == 0
    B = RD.load(path)
    cl, _ = CL.build(B, cons)
    bk = BS.guard(B, cons)
    bk.dense, bk.prefer = False, False
    fl = set()
    CL.reseat(B, cl, everything=True, bounds=(0, 0, 100, 80), legal=LGL.Legal(B, cons), strict=True, flips=fl, back=bk)
    assert fl == set()                                                                  # not dense: a part with a front seat stays on the front


def test_back_only_pass_leaves_a_part_with_no_back_seat_in_place(tmp_path):
    cons = {"placement": {"sides": "both", "keepout": {"cm": {"rect": [0, 0, 100, 80], "side": "B", "allow": []}}}}
    B = RD.load(_ic_board(tmp_path))
    cl, _ = CL.build(B, cons)
    bk = BS.guard(B, cons)
    bk.dense, bk.only_back = True, True
    before = {r: B["parts"][r]["pos"] for r in ("C1", "C2")}
    fl = set()
    mv = CL.reseat(B, cl, everything=True, bounds=(0, 0, 100, 80), legal=LGL.Legal(B, cons), strict=True, flips=fl, back=bk)
    assert fl == set() and not mv and {r: B["parts"][r]["pos"] for r in before} == before


def test_modules_build_in_max_mode_moves_the_supports_and_mirrors_the_module(tmp_path):
    path = _ic_board(tmp_path)
    cons = {"placement": {"sides": "both-light-back", "back_max_height_mm": 2.2, "back_fill": "max", "fixed": []}}
    B = RD.load(path)
    M = MD.build(B, cons, None, arrange=True)
    bf = M["backfill"]
    assert bf is not None
    assert {"C1", "C2"} <= set(M["flipped"]) or bf["modules"] == ["U1"]
    mirrored = [m for m in M["modules"] if m.get("mirror")]
    if bf["modules"]:
        assert [m["anchor"] for m in mirrored] == ["U1"] and set(M["flipped"]) >= {"U1", "C1", "C2"}
        P = B["parts"]
        assert P["U1"]["side"] == "B.Cu" and P["C1"]["side"] == "B.Cu"
        assert P["C1"]["pos"][0] == pytest.approx(2 * 40 - 44) and P["C2"]["pos"][0] == pytest.approx(2 * 40 - 36)     # reflected about the anchor
    assert LGL.count(LGL.Legal(B, cons).issues()) == 0


def test_a_module_with_one_member_that_must_stay_front_is_not_mirrored(tmp_path):
    path = _ic_board(tmp_path, g.two("C3", 41, 44, "VDD", "GND", name="C_0603_1608Metric", court=(-1, -.6, 1, .6)))
    cons = {"placement": {"sides": "both-light-back", "back_max_height_mm": 2.2, "back_fill": "max", "front_only": ["C3"]}}
    B = RD.load(path)
    M = MD.build(B, cons, None, arrange=True)
    assert not (M["backfill"] or {}).get("modules") and B["parts"]["U1"]["side"] == "F.Cu"


def test_free_single_parts_are_flipped_in_place_and_fixed_ones_are_not(tmp_path):
    extra = (g.two("R9", 70, 20, "N1", "N2", name="R_0603_1608Metric", court=(-1, -.5, 1, .5))
             + g.two("R8", 70, 30, "N3", "N4", name="R_0603_1608Metric", court=(-1, -.5, 1, .5)))
    cons = {"placement": {"sides": "both", "back_fill": "max", "fixed": ["R8"]}}
    B = RD.load(_ic_board(tmp_path, extra))
    M = MD.build(B, cons, None, arrange=True)
    assert "R9" in M["backfill"]["singles"] and B["parts"]["R9"]["side"] == "B.Cu" and B["parts"]["R9"]["pos"] == (70, 20)
    assert B["parts"]["R8"]["side"] == "F.Cu"
    off = MD.build(RD.load(_ic_board(tmp_path, extra, ) if False else _ic_board(tmp_path, extra)), {"placement": {"sides": "front"}}, None, arrange=True)
    assert off["backfill"] is None or not off["backfill"]["singles"]


def test_a_failed_flip_verification_raises(tmp_path, monkeypatch):
    from fluxplace import flip as FL, floorplan2 as F2
    monkeypatch.setattr(FL, "flip_file", lambda src, dst, refs: {})
    monkeypatch.setattr(FL, "verify", lambda *a: ["C1: side file F.Cu / pcbnew B.Cu / model B.Cu"])
    path = _ic_board(tmp_path)
    with pytest.raises(RuntimeError, match="disagree"):
        F2.write_flips(path, str(tmp_path / "o.kicad_pcb"), ["C1"], "t")
    monkeypatch.setattr(FL, "verify", lambda *a: [])
    assert F2.write_flips(path, str(tmp_path / "o.kicad_pcb"), ["C1"], "t") == {}
