"""fabcaps: manufacturer rows by layer count, hard vs preferred floors, pair-gap judgement, advisor, power sizing."""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import constraints as C, fabcaps as FC, fabadvise as FA, impedance as IM, power as PW, review as R, rules as RU


def test_every_table_has_source_and_date():
    d = FC.data()
    for v, vd in d["vendors"].items():
        assert vd["source"].startswith("https://") and vd["retrieved"]
        for t, td in vd["tiers"].items():
            assert td["source"].startswith("https://") and td["retrieved"] and td["price_step"], (v, t)
    assert d["plating_um"]["source"]


def test_names_and_loud_error():
    assert "pcbway-standard" in FC.names() and "jlcpcb" in FC.names()
    with pytest.raises(FC.FabProfileError) as e:
        C.fab_profile({"fab": {"profile": "pcbway-6L"}})
    assert "pcbway-standard" in str(e.value)


def test_row_selection_by_layer_count():
    two, _ = FC.row_for("jlcpcb", "standard", 2)
    six, _ = FC.row_for("jlcpcb", "standard", 6)
    assert two["track_min"] == 0.1 and six["track_min"] == 0.09
    p4, _ = FC.row_for("pcbway", "standard", 4)
    p6, _ = FC.row_for("pcbway", "standard", 6)
    assert p4["thickness_min"] == 0.6 and p6["thickness_min"] == 0.9
    assert p4["hole_to_copper_inner"] < p6["hole_to_copper_inner"]
    with pytest.raises(FC.FabProfileError):
        FC.row_for("pcbway", "standard", 20)
    assert not FC.offers("pcbway", "standard", 16)


def test_inherits_and_floors():
    f = FC.limits("pcbway-fine-drill", 6)
    assert f["floors"]["min_drill_mm"] == 0.15 and f["floors"]["min_track_mm"] == 0.1
    assert f["floors"]["min_via_dia_mm"] == pytest.approx(0.15 + 2 * 0.127, abs=1e-3)


def test_profile_is_hard_and_stricter_explicit_wins():
    fp = C.fab_profile({"fab": {"profile": "jlcpcb-standard", "min_track_mm": 0.05}}, layers=6)
    assert fp["min_track_mm"] == 0.09                      # a looser explicit value cannot undercut the manufacturer
    fp = C.fab_profile({"fab": {"profile": "jlcpcb-standard", "min_track_mm": 0.15}}, layers=6)
    assert fp["min_track_mm"] == 0.15                      # a stricter one is kept
    assert fp["vendor"] == "jlcpcb" and fp["layers"] == 6
    assert C.fab_profile({"fab": {"profile": "jlcpcb"}})["min_track_mm"] == 0.10    # legacy names still work
    assert C.fab_profile({"fab": {"min_track_mm": 0.12}})["min_track_mm"] == 0.12   # no profile: unchanged behaviour


def test_judge_preferred_vs_hard():
    assert FC.judge(0.15, 0.127, 0.1) == "ok"
    assert FC.judge(0.107, 0.127, 0.1) == "below_preferred"
    assert FC.judge(0.09, 0.127, 0.1) == "illegal"


def _g():
    return {"layer": "F.Cu", "hu": None, "eu": None, "hd": 0.1, "ed": 4.2, "t": 0.035, "model": "microstrip"}


def test_pair_judgement_two_geometries():
    rows = IM.pair_judgement(100, _g(), 0.127, 0.1, 0.1, declared=(0.173, 0.107))
    by = {r["label"]: r for r in rows}
    assert by["declared"]["status"] == "below_preferred"
    assert by["preferred floor"]["gap_mm"] >= 0.127 - 1e-9 and by["preferred floor"]["status"] == "ok"
    assert by["fab limit"]["gap_mm"] >= 0.1 - 1e-9 and by["fab limit"]["gap_mm"] <= by["preferred floor"]["gap_mm"]
    for r in rows:
        assert abs(r["err_pct"]) < 1.0 or r["label"] == "declared"
    assert IM.pair_judgement(100, {"model": None}, 0.127, 0.1, 0.1) == []


def test_ipc2221_against_hand_values():
    # A = (I/(k dT^0.44))^(1/0.725) mil^2 ; 1 A, 10 C, 1 oz external: hand value 0.3000 mm (11.82 mil)
    assert PW.ipc2221_width_mm(1.0, 10, 1.0, False) == pytest.approx(0.300, abs=0.002)
    # internal: k halves, area x 2^(1/0.725)=2.60 : 0.6998 mm
    assert PW.ipc2221_width_mm(1.0, 10, 1.0, True) == pytest.approx(0.300 * 2 ** (1 / 0.725), abs=0.004)
    assert PW.ipc2221_width_mm(2.0, 10, 2.0, False) == pytest.approx(PW.ipc2221_width_mm(2.0, 10, 1.0, False) / 2, abs=1e-9)
    assert PW.ipc2221_width_mm(0, 10, 1.0) == 0.0


def test_via_ampacity_and_clearance_table():
    i = PW.via_current_a(0.3, 25, 10)                       # hand: A = pi/4((0.35)^2-(0.3)^2)=0.02553 mm^2 = 39.57 mil^2
    assert i == pytest.approx(0.024 * 10 ** 0.44 * 39.57 ** 0.725, rel=0.01)
    assert PW.clearance_for_v(12, "external") == 0.1 and PW.clearance_for_v(48, "external") == 0.6
    assert PW.clearance_for_v(200, "internal") == 0.2


CONS = {"fab": {"profile": "jlcpcb-standard"},
        "power": {"HI": {"max_current_ma": 4000}, "MID": {"load_a": 1.0, "max_v": 48}, "PLANE": {"max_current_ma": 100, "pour": True},
                  "NONE": {"max_v": 12}}}


def test_rail_policy_pour_track_unknown():
    hi, mid, pl, no = (PW.rail(CONS, n, layers=6) for n in ("HI", "MID", "PLANE", "NONE"))
    assert hi["pour"] and hi["pour_reason"].startswith("required width")      # 4 A needs more than 2 mm
    assert not mid["pour"] and mid["track_mm"] == pytest.approx(math.ceil(mid["width_inner"] / 0.05) * 0.05, abs=1e-6)
    assert mid["clearance_mm"]["external"] == 0.6                              # 48 V
    assert pl["pour"] and pl["pour_reason"] == "pour = true"
    assert no["track_mm"] is None and no["pour_reason"] == "unknown current"
    assert hi["oz_inner"] == 0.5 and hi["oz_outer"] == 1.0                      # jlcpcb 6L cheapest weights
    assert hi["vias_needed"] >= 4 and mid["vias_needed"] >= 1


def test_rules_cascade_carries_computed_width_pour_and_clearance():
    rs = RU.load(CONS)
    w, _ = rs.resolve({"net": "MID"}, "width_mm")
    assert w == PW.rail(CONS, "MID")["track_mm"]
    assert rs.resolve({"net": "HI"}, "pour")[0] is True
    assert rs.resolve({"net": "MID"}, "clearance_mm")[0] == 0.6
    assert C.power_width_mm(CONS, "MID", 0.2) == w and C.power_width_mm(CONS, "UNKNOWN", 0.2) == 0.2
    assert "HI" in C.pour_nets(CONS)


def _facts(tracks, vias, **kw):
    f = {"copper_layers": 6, "track_geom": tracks, "vias": vias, "zone_nets": [], "plane_layers": [],
         "net_pads": {"HI": [], "MID": [], "NONE": [], "+3V3": [], "GND": []}}
    f.update(kw)
    return f


def _tr(net, layer, w, ln):
    return {"net": net, "layer": layer, "width": w, "len": ln, "kind": "track", "x": 0, "y": 0}


def test_power_codes():
    cons = {"fab": {"profile": "jlcpcb-standard"}, "power": {"MID": {"load_a": 1.0}, "NONE": {"max_v": 12}}}
    need_out = PW.rail(cons, "MID", 6)["width_outer"]
    f = _facts([_tr("MID", "F.Cu", 0.2, 10.0), _tr("MID", "F.Cu", 0.2, 1.0)], [{"net": "MID"}])
    codes = {x["code"]: x for x in PW.check(f, cons)}
    assert "POWER_WIDTH" in codes and codes["POWER_WIDTH"]["level"] == "FAIL" and "11.0 mm" in codes["POWER_WIDTH"]["msg"]
    assert codes["POWER_VIAS"]["level"] == "FAIL"
    assert codes["POWER_CURRENT_UNKNOWN"]["level"] == "WARN"
    for n in ("NONE", "+3V3"):
        assert n in codes["POWER_CURRENT_UNKNOWN"]["refs"]
    ok = _facts([_tr("MID", "F.Cu", need_out + 0.05, 10.0), _tr("MID", "F.Cu", 0.1, 1.0)], [{"net": "MID"}] * 20)
    assert {x["code"] for x in PW.check(ok, cons)} == {"POWER_CURRENT_UNKNOWN"}          # short neck-down is exempt


def test_review_fab_capability_uses_profile_row_and_reports_error():
    cons = {"fab": {"profile": "pcbway-standard"}}
    f = _facts([_tr("N", "F.Cu", 0.09, 5.0)], [{"net": "V", "dia_mm": 0.45, "drill_mm": 0.2, "x": 0, "y": 0}])
    codes = [x["code"] for x in R.check_fab_capability(f, cons)]
    assert "FAB_MIN_TRACK" in codes and "FAB_PROFILE" in codes
    bad = R.check_fab_capability(f, {"fab": {"profile": "nope"}})
    assert bad[0]["code"] == "FAB_PROFILE_ERROR" and bad[0]["level"] == "FAIL"


def test_advisor_on_synthetic_board():
    base = dict(copper_layers=6, size_mm=(100.0, 80.0), parts={}, via_type_counts={}, design_min={}, board_path="/nonexistent.kicad_board",
                pth_pads=[], zone_nets=[])
    cons = {"route": {"min_track_mm": 0.127, "min_clearance_mm": 0.127}, "pairs": {"E_": {"impedance_diff": 100}}}
    ok = dict(base, track_geom=[_tr("a", "F.Cu", 0.15, 5)], vias=[{"net": "a", "dia_mm": 0.6, "drill_mm": 0.3, "x": 0, "y": 0}])
    r = FA.advise(ok, cons)
    assert {x["name"] for x in r["recommended"]} == {"jlcpcb-standard", "pcbway-standard"}
    fine = dict(base, track_geom=[_tr("a", "F.Cu", 0.15, 5)],
                vias=[{"net": "a", "dia_mm": 0.25, "drill_mm": 0.15, "x": 0, "y": 0}] * 7)
    r = FA.advise(fine, cons)
    assert {x["name"] for x in r["recommended"]} == {"jlcpcb-fine-drill"}         # 0.15 drill: pcbway-fine-drill fails the ring
    pcb = {t["name"]: t for t in r["tiers"]}
    assert not pcb["jlcpcb-standard"]["fit"] and "7" in str(pcb["jlcpcb-standard"]["fails"])   # 7 vias affected
    big = dict(ok, size_mm=(700.0, 700.0))
    assert FA.advise(big, cons)["recommended"] == []
    txt = FA.format_text(FA.advise(ok, cons))
    assert "RECOMMENDED" in txt and "price not published" in txt
    assert all(not t["offers"] for t in FA.advise(dict(ok, copper_layers=18), cons)["tiers"] if t["vendor"] == "pcbway")


def test_advisor_flags_inner_copper_weight_not_offered():
    st = [{"type": "copper", "thickness": 0.035}] + [{"type": "copper", "thickness": 0.0152}] * 4 + [{"type": "copper", "thickness": 0.035}]
    assert PW.oz_from_stackup(st) == (1.0, 0.5)
    base = dict(copper_layers=6, size_mm=(100.0, 80.0), parts={}, via_type_counts={}, design_min={}, board_path="/nonexistent",
                pth_pads=[], zone_nets=[], stackup=st, track_geom=[_tr("a", "F.Cu", 0.15, 5)],
                vias=[{"net": "a", "dia_mm": 0.6, "drill_mm": 0.3, "x": 0, "y": 0}])
    by = {t["name"]: t for t in FA.advise(base, {})["tiers"]}
    assert by["jlcpcb-standard"]["fit"] and not by["pcbway-standard"]["fit"]            # PCBWay inner copper starts at 1 oz
    assert "inner copper" in by["pcbway-standard"]["fails"][0][0]
