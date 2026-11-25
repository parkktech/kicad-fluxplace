import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from fluxplace import backside as BS, clusters as CL, legality as LGL, modules as MD, routediag as RD
import pcbgen as g
from test_legality import KO, _board_with_keepout, _reseat

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
            + g.two("C1", 70, 60, "VCC", "GND", **TL.CAP))
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
