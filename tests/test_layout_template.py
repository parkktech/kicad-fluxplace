"""Placer step 5 S3: the datasheet layout fact (`layout`) steers the regulator seat, grades the budget and claims a keep-out."""
import json
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
import pcbgen as g
import powerboard as PW
from fluxplace import budget as BU, clusters as CL, elec as EL, floorplan2 as F2, geom as GM, modules as MD, partdocs as PD, routediag as RD

# the fixture of S1's rotation test: U1 (30, 30), pads VIN 1, GND 2, SW 3, BOOT 4, FB 5, EN 6, pitch 1.0
LAY = {"cin_loop_mm": 4.0, "sw_mm": 2.0, "fb_mm": 2.0, "cin_side": [["VIN", "GND"]], "inductor_keepout_mm": 0.5, "page": 1}
SPEC = {"components": [{"ref": "U1", "mpn": "REG1", "pinmap": {"VIN": "1", "GND": "2", "SW": "3", "BOOT": "4", "FB": "5", "EN": "6"}}]}
UNSEATED = {"cin_loop_mm": 5.193, "sw_mm": 7.5, "boot_mm": 6.265, "fb_mm": 9.708}


def _board(tmp_path, extra=""):
    b = g.ic("U1", 30, 30, ["VIN", "GND", "SW", "BOOT", "FB", "EN"], pitch=1.0)
    b += PW.cap("C1", 26, 30, "VIN", "GND", "10uF") + PW.cap("C2", 27, 34, "BOOT", "SW", "100nF")
    b += PW.cap("L1", 36, 31, "SW", "VOUT", "") + PW.cap("R1", 27, 38, "FB", "VOUT", "") + extra
    return g.board(tmp_path, b, outline=(0, 0, 80, 60))


def _facts(lay=LAY):
    return {"REG1": {"layout": lay, "_src": {"layout": "REG.pdf#p1"}}} if lay else {}


def _clusters(B, lay=LAY):
    cl, _ = CL.build(B, {}, SPEC, facts=_facts(lay), mpn_map={"U1": "REG1"})
    return cl[0]


def _seated(tmp_path, lay=LAY):
    B = RD.load(_board(tmp_path))
    c = _clusters(B, lay)
    mv = CL.seat(B, c)
    for r, (x, y, a) in mv.items():
        GM.place(B["parts"][r], x, y, a)
    return B, c, mv


def _datasheets(tmp_path, lay=LAY):
    ds = tmp_path / "ds"
    ds.mkdir()
    (ds / "datasheets.json").write_text(json.dumps({"REG1": {"file": "REG.pdf", "facts": {"layout": {"value": lay, "source": "REG.pdf#p1"}}} if lay else {"file": "REG.pdf"}}))
    return {"docs": {"datasheets": str(ds)}}


# ---- the fact store
def test_layout_is_a_fact_key_and_a_dict_fact_is_digit_checked_unless_derived(tmp_path, monkeypatch):
    (tmp_path / "X.pdf").write_bytes(b"x")
    (tmp_path / PD.MANIFEST).write_text(json.dumps({"X1": {"file": "X.pdf"}}))
    monkeypatch.setattr(PD, "page_text", lambda path, pages: "keep the loop under 5 mm; page 47")
    assert "layout" in EL.FACT_KEYS
    lay = {"cin_loop_mm": 5.0, "sw_mm": 3.5, "cin_side": [["VIN", "GND"]], "page": 47}
    with pytest.raises(ValueError, match="sw_mm=3.5"):
        EL.set_facts(str(tmp_path), "X1", {"layout": lay}, "X.pdf#p47")                # a number the page does not state
    ok = dict(lay, derived=["sw_mm"], derivation="courtyard geometry")
    assert EL.set_facts(str(tmp_path), "X1", {"layout": ok}, "X.pdf#p47") == (["layout"], [])
    assert EL.load_facts(str(tmp_path))["X1"]["layout"]["derived"] == ["sw_mm"]
    with pytest.raises(ValueError, match="cin_loop_mm=6"):                              # not derived, not on the page
        EL.set_facts(str(tmp_path), "X1", {"layout": dict(ok, cin_loop_mm=6.0)}, "X.pdf#p47")
    EL.set_facts(str(tmp_path), "X1", {"layout": {"cin_loop_mm": 9.0, "derived": True}}, "X.pdf#p47")      # derived = all of them
    with pytest.raises(ValueError, match="VDD=3"):                                      # a per-pin dict fact is checked too
        EL.set_facts(str(tmp_path), "X1", {"decap_mm": {"VDD": 3}}, "X.pdf#p47")


# ---- seat
def test_no_fact_is_the_s1_result_byte_identical(tmp_path):
    B, c, mv = _seated(tmp_path, None)
    assert "layout" not in c
    assert mv == {"L1": (27.6, 32.0, 180.0), "C1": (27.6, 28.0, 180.0), "C2": (26.6, 30.5, 270.0), "R1": (33.4, 29.5, 90.0)}
    assert CL.loop_metrics(B, c) == {"cin_loop_mm": 4.198, "sw_mm": 1.005, "boot_mm": 1.4, "fb_mm": 1.4}


def test_unseated_template_numbers_are_the_s1_numbers_with_or_without_the_fact(tmp_path):
    B = RD.load(_board(tmp_path))
    c = _clusters(B)
    assert c["layout"]["pairs"] == [["1", "2"]]
    assert CL.loop_metrics(B, c) == dict(UNSEATED, boot_mm=3.041)       # CL.build supports: boot_mm 3.041; hand-made supports 6.265
    assert CL.loop_metrics(B, {k: v for k, v in c.items() if k != "layout"}) == CL.loop_metrics(B, c) | {"cin_loop_mm": 5.193}


def test_the_input_capacitor_seats_on_the_side_of_the_named_pins(tmp_path):
    B, c, mv = _seated(tmp_path)
    x0, y0, x1, y1 = B["parts"]["C1"]["court"]
    assert x1 <= 27.35 + 1e-6 and 28.0 - 1e-6 <= y0 and y1 <= 31.0 + 1e-6      # beyond the package courtyard on the pad 1/2 side
    m = CL.loop_metrics(B, c)
    assert m["cin_loop_mm"] <= LAY["cin_loop_mm"] and m["cin_loop_mm"] < 4.198    # the old seat (4.198) is over the fact and on the wrong side
    assert m["sw_mm"] == 1.005


def test_strapped_ground_pads_do_not_close_the_loop(tmp_path):
    # a second ground pad on the capacitor's side (a mode strap): the loop closes on the named pad 2 only
    b = g.ic("U1", 30, 30, ["VIN", "GND", "GND", "SW", "FB", "EN"], pitch=1.0) + PW.cap("C1", 26, 30.8, "VIN", "GND", "10uF")
    b += PW.cap("L1", 36, 31, "SW", "VOUT", "")
    B = RD.load(g.board(tmp_path, b, outline=(0, 0, 80, 60)))
    spec = {"components": [{"ref": "U1", "mpn": "REG1", "pinmap": {"VIN": "1", "GND": "2", "MODE": "3", "SW": "4", "FB": "5", "EN": "6"}}]}
    lay = {"cin_side": [["VIN", "GND"]]}
    cl, _ = CL.build(B, {}, spec, facts=_facts(lay), mpn_map={"U1": "REG1"})
    c = cl[0]
    pin = {q["num"]: q for q in B["parts"]["U1"]["pads"]}
    cap = {q["net"]: q for q in B["parts"]["C1"]["pads"]}
    d = lambda a, b_: math.hypot(a["x"] - b_["x"], a["y"] - b_["y"])        # noqa: E731
    want = d(pin["1"], cap["VIN"]) + d(cap["VIN"], cap["GND"]) + d(cap["GND"], pin["2"])
    assert d(cap["GND"], pin["3"]) < d(cap["GND"], pin["2"])                                # the strap is the nearer ground pad
    assert CL.loop_metrics(B, c)["cin_loop_mm"] == round(want, 3)
    assert CL.loop_metrics(B, {k: v for k, v in c.items() if k != "layout"})["cin_loop_mm"] < round(want, 3)       # the old metric is fooled


@pytest.mark.parametrize("th", (0.0, 90.0, 180.0, 270.0))
def test_template_numbers_are_identical_at_every_quarter_turn(tmp_path, th):
    B, c, _ = _seated(tmp_path)
    m0 = CL.loop_metrics(B, c)
    for r in ("U1", "C1", "C2", "L1", "R1"):
        p = B["parts"][r]
        rx, ry = GM.rot(p["pos"][0] - 30.0, p["pos"][1] - 30.0, th)
        GM.place(p, 30.0 + rx, 30.0 + ry, p["angle"] + th)
    assert CL.loop_metrics(B, c) == m0


def test_unseated_set_is_identical_at_every_quarter_turn(tmp_path):
    B = RD.load(_board(tmp_path))
    c = _clusters(B)
    for th in (90.0, 180.0, 270.0, 0.0):
        for r in ("U1", "C1", "C2", "L1", "R1"):
            p = B["parts"][r]
            rx, ry = GM.rot(p["pos"][0] - 30.0, p["pos"][1] - 30.0, 90.0)
            GM.place(p, 30.0 + rx, 30.0 + ry, p["angle"] + 90.0)
        m = CL.loop_metrics(B, c)
        assert (m["cin_loop_mm"], m["sw_mm"], m["fb_mm"]) == (5.193, 7.5, 9.708), th


# ---- module keep-out and the plan
def _module(tmp_path, lay=LAY, extra=""):
    cons = _datasheets(tmp_path, lay)
    B = RD.load(_board(tmp_path, extra))
    return B, cons, MD.build(B, cons, SPEC, arrange=True)


def test_the_module_carries_the_inductor_keepout_relative_to_its_anchor(tmp_path):
    B, cons, M = _module(tmp_path)
    mod = next(m for m in M["modules"] if m["anchor"] == "U1")
    ax, ay = mod["pos"]
    assert mod["keepouts"] == [[round(26.6 - 0.5 - ax, 4), round(31.5 - 0.5 - ay, 4), round(28.6 + 0.5 - ax, 4), round(32.5 + 0.5 - ay, 4)]]
    assert (ax + mod["keepouts"][0][0], ay + mod["keepouts"][0][1], ax + mod["keepouts"][0][2], ay + mod["keepouts"][0][3]) == pytest.approx((26.1, 31.0, 29.1, 33.0))
    assert mod["template"]["U1"]["cin_loop_mm"] <= LAY["cin_loop_mm"]


def test_no_fact_no_keepout_and_the_module_is_s1s(tmp_path):
    _, _, M = _module(tmp_path, None)
    mod = next(m for m in M["modules"] if m["anchor"] == "U1")
    assert "keepouts" not in mod and mod["template"]["U1"]["cin_loop_mm"] == 5.193      # S1: the capacitor is within its limit, so the module does not seat it


def _plan(tmp_path, lay, side, at):
    x1 = g.fp("X1", 60, 50, [g.pad("1", -0.15, 0, "XA", 0.15, 0.15), g.pad("2", 0.15, 0, "XB", 0.15, 0.15), g.pad("3", 0.0, 0.2, "XC", 0.15, 0.15)],
              side=side, court=(-0.25, -0.25, 0.25, 0.25))
    B, cons, M = _module(tmp_path, lay, x1)
    P = F2.Plan(B, cons, SPEC, M, log=lambda *a, **k: None)
    P.set_claims({})
    P.set_keepouts({i: m["keepouts"] for i, m in enumerate(M["modules"]) if m.get("keepouts")})
    P.freeze_baseline()
    iu, ix = P.idx["U1"], P.idx["X1"]
    P.apply(ix, at[0], at[1], 0)
    return P, iu, ix


@pytest.mark.parametrize("side", ("F", "B"))
def test_a_foreign_part_in_the_keepout_makes_the_plan_illegal_on_both_sides(tmp_path, side):
    P, iu, ix = _plan(tmp_path, LAY, side, (29.0, 32.0))
    assert not P.legal(iu) and not P.legal(ix)
    P.apply(ix, 29.0, 33.6, 0)                                   # courtyard from y 33.35: out of the grown rectangle (to y 33.0)
    assert P.legal(iu) and P.legal(ix)


def test_the_same_part_is_legal_without_the_fact(tmp_path):
    P, iu, ix = _plan(tmp_path, None, "F", (29.0, 32.0))
    assert P.legal(iu) and P.legal(ix)


def test_own_parts_are_exempt_from_the_keepout(tmp_path):
    P, iu, ix = _plan(tmp_path, LAY, "F", (45.0, 40.0))
    grown = P.keepouts_world(iu)[0]
    own = [r for r in P.mods[iu]["members"] if GM.overlap(P.B["parts"][r]["court"], grown)]
    assert {"U1", "L1"} <= set(own) and P.legal(iu)


def test_the_keepout_turns_with_the_module(tmp_path):
    P, iu, _ = _plan(tmp_path, LAY, "F", (60, 50))
    ax, ay = P.mods[iu]["pos"]
    for th in (0, 90, 180, 270):
        P.apply(iu, ax, ay, th)
        x0, y0, x1, y1 = P.keepouts_world(iu)[0]
        want = [GM.rot(px - ax, py - ay, th) for px, py in ((26.1, 31.0), (29.1, 33.0))]
        w = (ax + min(p[0] for p in want), ay + min(p[1] for p in want), ax + max(p[0] for p in want), ay + max(p[1] for p in want))
        assert (x0, y0, x1, y1) == pytest.approx(w)


# ---- budget rows
def test_template_rows_are_hard_only_for_numbers_the_page_states(tmp_path):
    B = RD.load(_board(tmp_path))
    cons = _datasheets(tmp_path, LAY)
    rows = {r["name"]: r for r in BU.template_rows(B, cons, SPEC)}
    assert set(rows) == {"U1 template cin_loop_mm", "U1 template sw_mm", "U1 template fb_mm", "U1 template cin_side"}
    r = rows["U1 template cin_loop_mm"]
    assert (r["value"], r["target"], r["max"], r["hard"], r["klass"], r["verdict"]) == (5.193, 4.0, 4.0, True, "REQ", "fail")
    (tmp_path / "d2").mkdir()
    cons2 = _datasheets(tmp_path / "d2", dict(LAY, derived=["cin_loop_mm", "sw_mm", "fb_mm"]))
    rows2 = {r["name"]: r for r in BU.template_rows(B, cons2, SPEC)}
    rows2 = {k: v for k, v in rows2.items() if not k.endswith("cin_side")}               # cin_side is a named datasheet REQ, never derived
    assert not any(r["hard"] for r in rows2.values()) and {r["klass"] for r in rows2.values()} == {"INF"}
    assert rows2["U1 template cin_loop_mm"]["verdict"] == "fail" and "derived" in rows2["U1 template cin_loop_mm"]["source"]
    assert BU.template_rows(B, {}, SPEC) == []                                                     # no fact, no row


def test_rows_pass_once_the_module_is_seated(tmp_path):
    B, c, _ = _seated(tmp_path)
    cons = _datasheets(tmp_path, dict(LAY, sw_mm=1.5, fb_mm=1.5))
    rows = {r["name"]: r for r in BU.template_rows(B, cons, SPEC)}
    assert all(r["verdict"] == "ok" for r in rows.values()), {k: (v["value"], v["verdict"]) for k, v in rows.items()}


@pytest.mark.parametrize("side", ("F", "B"))
def test_a_derived_keepout_is_soft_and_does_not_make_the_plan_illegal(tmp_path, side):
    lay = dict(LAY, derived=["inductor_keepout_mm"])
    P, iu, ix = _plan(tmp_path, lay, side, (29.0, 32.0))
    assert P.legal(iu) and P.legal(ix)
    mod = P.mods[iu]
    ax, ay = mod["pos"]
    assert [ax + mod["keepouts_soft"][0][0], ay + mod["keepouts_soft"][0][1], ax + mod["keepouts_soft"][0][2],
            ay + mod["keepouts_soft"][0][3]] == pytest.approx((26.1, 31.0, 29.1, 33.0))
    assert "keepouts" not in mod


def test_cin_side_has_a_hard_row_that_fails_off_side_and_passes_on_side(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    Bn, _, _ = _seated(tmp_path / "a", lay=None)                       # seated without the fact: C1 above the IC, off the VIN/GND side
    r = {x["name"]: x for x in BU.template_rows(Bn, _datasheets(tmp_path / "a", LAY), SPEC)}["U1 template cin_side"]
    assert (r["value"], r["target"], r["hard"], r["klass"], r["verdict"], r["refs"]) == (0, 1, True, "REQ", "fail", ["U1", "C1"])
    Bs, _, _ = _seated(tmp_path / "b")
    r2 = {x["name"]: x for x in BU.template_rows(Bs, _datasheets(tmp_path / "b", LAY), SPEC)}["U1 template cin_side"]
    assert (r2["value"], r2["verdict"], r2["hard"]) == (1, "ok", True)


def test_a_guessed_input_capacitor_is_not_the_regulators_part(tmp_path, monkeypatch):
    B = RD.load(_board(tmp_path))
    real = CL.build

    def guess(*a, **k):
        cl, rest = real(*a, **k)
        for c in cl:
            for s in c["supports"]:
                if s["ref"] == "C1":
                    s["sure"] = False
        return cl, rest
    monkeypatch.setattr(CL, "build", guess)
    rows = {x["name"]: x for x in BU.template_rows(B, _datasheets(tmp_path, LAY), SPEC)}
    assert rows["U1 template cin_loop_mm"]["value"] is None
    assert rows["U1 template cin_side"]["value"] == 0 and rows["U1 template cin_side"]["verdict"] == "fail"


# ---- step 5b S3: template parts are steered (sw_mm, fb_mm) and a regulator with a fact is laid out in isolation
def _reseated(tmp_path, lay, **kw):
    B = RD.load(_board(tmp_path))
    c = _clusters(B, lay)
    CL.reseat(B, [c], isolate=True, **kw)
    return B, c


def test_a_switch_node_part_over_sw_mm_is_reseated_and_without_the_fact_it_stays(tmp_path):
    B, c = _reseated(tmp_path, dict(LAY, sw_mm=1.2, fb_mm=1.6))
    m = CL.loop_metrics(B, c)
    assert m["sw_mm"] <= 1.2 and m["fb_mm"] <= 1.6
    (tmp_path / "n").mkdir()
    B0 = RD.load(_board(tmp_path / "n"))
    c0 = _clusters(B0, None)
    assert not CL._off_template(B0, c0, next(s for s in c0["supports"] if s["rank"] == 0))     # no fact, no steering


def test_a_neighbour_on_the_datasheet_side_does_not_hold_the_input_capacitor_off_it(tmp_path):
    # a foreign part sits where the datasheet puts the input capacitor: placed among neighbours the seat cannot use that side
    x = PW.cap("X9", 26.85, 29.5, "XA", "XB", "100nF")
    B = RD.load(_board(tmp_path, x))
    cons = _datasheets(tmp_path, LAY)
    M = MD.build(B, cons, SPEC, arrange=True)
    mod = next(m for m in M["modules"] if m["anchor"] == "U1")
    assert "C1" in mod["members"]
    assert mod["template"]["U1"]["cin_loop_mm"] <= LAY["cin_loop_mm"]
    r = {q["name"]: q for q in BU.template_rows(B, cons, SPEC)}
    assert r["U1 template cin_side"]["value"] == 1 and (tuple(B["parts"]["X9"]["pos"]) == (26.85, 29.5))


# ---- P5 R1: a template unit must be legal where it stands (the floorplan moves the unit, not the isolated pass)
def _hits(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def test_a_free_foreign_part_on_the_input_seat_makes_the_unit_illegal(tmp_path):
    x = PW.cap("X9", 26.85, 29.5, "XA", "XB", "100nF")
    B = RD.load(_board(tmp_path, x))
    M = MD.build(B, _datasheets(tmp_path, LAY), SPEC, arrange=True)
    hit = [u for u in M["unit_illegal"] if u["ref"] == "C1"]
    assert hit and any(i == "overlap X9" for i in hit[0]["issues"]), M["unit_illegal"]


def test_the_same_unit_without_the_foreign_part_is_legal(tmp_path):
    B = RD.load(_board(tmp_path))
    M = MD.build(B, _datasheets(tmp_path, LAY), SPEC, arrange=True)
    assert M["unit_illegal"] == []


def test_a_fixed_regulator_is_not_isolated_and_keeps_its_supports_off_a_foreign_part(tmp_path):
    x = PW.cap("X9", 26.85, 29.5, "XA", "XB", "100nF")
    B = RD.load(_board(tmp_path, x))
    cons = _datasheets(tmp_path, LAY)
    cons["placement"] = {"fixed": ["U1"]}
    M = MD.build(B, cons, SPEC, arrange=True)
    assert M["unit_illegal"] == []
    fx = B["parts"]["X9"]["court"]
    assert not any(_hits(B["parts"][r]["court"], fx) for r in ("C1", "C2", "L1", "R1") if r in B["parts"])
