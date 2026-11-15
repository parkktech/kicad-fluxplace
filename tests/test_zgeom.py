"""zgeom: pair / single-ended controlled-impedance geometry from the actual stackup (the one source every stage reads)."""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import impedance as IM, pairs as PA, review as R, zgeom as Z

ROWS = [{"type": "copper", "name": "F.Cu", "thickness": 0.035, "epsilon_r": 4.4},
        {"type": "prepreg", "name": None, "thickness": 0.1, "epsilon_r": 4.1},
        {"type": "copper", "name": "In1.Cu", "thickness": 0.0152, "epsilon_r": 4.4},
        {"type": "core", "name": None, "thickness": 0.2, "epsilon_r": 4.5},
        {"type": "copper", "name": "In2.Cu", "thickness": 0.0152, "epsilon_r": 4.4},
        {"type": "prepreg", "name": None, "thickness": 0.8692, "epsilon_r": 4.4},
        {"type": "copper", "name": "In3.Cu", "thickness": 0.0152, "epsilon_r": 4.4},
        {"type": "core", "name": None, "thickness": 0.2, "epsilon_r": 4.5},
        {"type": "copper", "name": "In4.Cu", "thickness": 0.0152, "epsilon_r": 4.4},
        {"type": "prepreg", "name": None, "thickness": 0.1, "epsilon_r": 4.1},
        {"type": "copper", "name": "B.Cu", "thickness": 0.035, "epsilon_r": 4.4}]
PLANES = ["In1.Cu", "In4.Cu"]
SIG = ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"]
CONS = {"route": {"min_track_mm": 0.127, "min_clearance_mm": 0.127}, "fab": {"profile": "jlcpcb-standard"},
        "pairs": {"ETH_": {"impedance_diff": 100}, "USB_": {"impedance_diff": 90}}, "rf": {"target_z": 50, "nets": ["RF_A"]}}


def test_microstrip_hand_value():
    # IPC-2141: Z0 = 87/sqrt(er+1.41) ln(5.98 h / (0.8 w + t)), er 4.1 h 0.1 t 0.035 w 0.127 -> 54.7 ohm; Zdiff = 2 Z0 (1 - 0.48 e^(-0.96 s/h)) at s 0.179 -> 100
    z0 = 87 / math.sqrt(4.1 + 1.41) * math.log(5.98 * 0.1 / (0.8 * 0.127 + 0.035))
    zd = 2 * z0 * (1 - 0.48 * math.exp(-0.96 * 0.179 / 0.1))
    g = IM.geometry(ROWS, PLANES, "F.Cu")
    assert IM.z_diff(0.127, 0.179, g) == pytest.approx(zd, rel=0.03)
    assert zd == pytest.approx(100.0, abs=1.5)


def test_pair_geometry_preferred_floor_on_every_layer():
    for fam, z in (("ETH_", 100), ("USB_", 90)):
        tab = Z.family_table(CONS, ROWS, PLANES, SIG, fam, layers=6)
        assert set(tab) == set(SIG)
        for l, g in tab.items():
            assert g["status"] == "ok" and g["source"] == "preferred floor"
            assert g["w"] >= 0.127 - 1e-9 and g["gap"] >= 0.127 - 1e-9             # the owner's floor, not the fab minimum
            assert abs(IM.z_diff(g["w"], g["gap"], IM.geometry(ROWS, PLANES, l)) - z) < 0.6
        if fam == "ETH_":
            assert tab["F.Cu"]["gap"] > tab["In2.Cu"]["gap"]                        # outer microstrip needs the wider gap
    ok, best = Z.family_ok(Z.family_table(CONS, ROWS, PLANES, SIG, "ETH_"))
    assert ok and best in SIG


def test_the_old_constant_is_wrong_on_this_stackup():
    g = IM.geometry(ROWS, PLANES, "F.Cu")
    assert abs(IM.z_diff(0.173, 0.107, g) - 100) > 15                                # 0.173/0.107 belongs to another 4-layer stackup


def test_explicit_geometry_is_kept_and_graded():
    c = dict(CONS, pairs={"ETH_": {"impedance_diff": 100, "width_mm": 0.2, "gap_mm": 0.2}})
    g = Z.pair_geometry(c, ROWS, PLANES, "ETH_", "F.Cu")
    assert (g["w"], g["gap"], g["source"]) == (0.2, 0.2, "explicit [pairs]") and g["status"] == "off" and g["err_pct"] != 0
    c = dict(CONS, pairs={"ETH_": {"impedance_diff": 100, "width_mm": 0.15}})
    g = Z.pair_geometry(c, ROWS, PLANES, "ETH_", "F.Cu")
    assert g["w"] == 0.15 and "gap solved" in g["source"] and abs(g["err_pct"]) < 1


def test_fab_limit_only_when_allowed_and_tight():
    # a floor so wide (0.3 mm over a 0.1 mm dielectric: Z0 ~ 29 ohm, 58 ohm differential at best) that 100 ohm is out of reach
    thick = ROWS
    c = {"route": {"min_track_mm": 0.3, "min_clearance_mm": 0.3}, "fab": {"profile": "jlcpcb-standard"},
         "pairs": {"E_": {"impedance_diff": 100}}}
    g = Z.pair_geometry(c, thick, PLANES, "E_", "F.Cu")
    assert g["status"] == "unreachable" and g["source"] == "best achievable"
    ca = {"route": c["route"], "fab": c["fab"], "pairs": {"E_": {"impedance_diff": 100, "allow_fab_limit": True}}}
    assert Z.pair_geometry(ca, thick, PLANES, "E_", "F.Cu")["status"] == "unreachable"        # not tight: never below the preferred floor
    gt = Z.pair_geometry(ca, thick, PLANES, "E_", "F.Cu", tight=True)
    assert gt["status"] == "ok" and gt["source"] == "fab limit" and gt["gap"] < 0.3 and gt["w"] >= 0.09 - 1e-9 and abs(gt["err_pct"]) <= 10
    assert not Z.family_ok(Z.family_table(c, thick, PLANES, ["F.Cu"], "E_"))[0]


def test_single_ended_width_per_layer():
    # microstrip 50 ohm on F: hand IPC-2141, er 4.1 h 0.1 t 0.035: ln term = 50 sqrt(5.51)/87 = 1.349 -> 5.98h/(0.8w+t)=3.853 -> w = 0.15
    a = Z.single_width(CONS, ROWS, PLANES, "RF_A", "F.Cu")
    b = Z.single_width(CONS, ROWS, PLANES, "RF_A", "In2.Cu")
    assert a["w"] == pytest.approx(0.15, abs=0.01) and abs(a["err_pct"]) < 1
    assert b["w"] > a["w"] * 1.4 and abs(b["err_pct"]) < 1                            # the inner layer needs a much wider trace
    bad = Z.single_width(dict(CONS, nets={"RF_A": {"width_mm": 0.15}}), ROWS, PLANES, "RF_A", "In2.Cu")
    assert bad["status"] == "off" and bad["err_pct"] > 20 and bad["source"] == "explicit [nets]"   # the outer-layer 0.15 mm is ~66 ohm inside


def test_no_reference_falls_back_to_class_geometry():
    g = Z.pair_geometry(CONS, ROWS, [], "ETH_", "F.Cu", class_geom=(0.2, 0.25))
    assert (g["w"], g["gap"], g["status"]) == (0.2, 0.25, "no_reference")


def test_pairs_exposes_the_one_function_and_layer_geometry_uses_it():
    assert PA.geometry is Z.pair_geometry
    rule = {"w": 0.2, "gap": 0.25, "target": 100, "family": "ETH_"}
    geo = PA.layer_geometry(rule, SIG, PLANES, ["F.Cu"] + SIG[1:], ROWS, 0.127, cons=CONS)
    tab = Z.family_table(CONS, ROWS, PLANES, SIG, "ETH_")
    for l in SIG:
        assert geo[l][:2] == (tab[l]["w"], tab[l]["gap"])


def test_review_codes():
    facts = {"stackup": ROWS, "plane_layers": PLANES, "copper_layers": 6, "net_tracks": {
        "ETH_TX_P": {"segments": [("In2.Cu", 0.2, 30.0)]}, "ETH_TX_N": {"segments": [("F.Cu", 0.127, 30.0)]}}}
    allf = R.check_pair_geometry(facts, CONS)
    f = {x["code"]: x for x in allf}
    assert any(x["code"] == "PAIR_GEOMETRY" and x["level"] == "INFO" and "ETH_" in x["msg"] and "F 0.127/" in x["msg"] for x in allf)
    assert f["PAIR_IMPEDANCE_OFF"]["level"] == "FAIL" and "ETH_TX_P" in f["PAIR_IMPEDANCE_OFF"]["msg"]
    bad = dict(CONS, route={"min_track_mm": 0.3, "min_clearance_mm": 0.3})
    u = R.check_pair_geometry(facts, dict(bad, pairs={"ETH_": {"impedance_diff": 100, "layers": ["F.Cu"]}}))
    assert u[0]["code"] == "PAIR_IMPEDANCE_UNREACHABLE" and u[0]["level"] == "FAIL" and "best achievable" in u[0]["msg"]
