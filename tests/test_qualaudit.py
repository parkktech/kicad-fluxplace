from fluxplace import batchroute as BR
from fluxplace import qualaudit as QA


def test_rf_declared_width_wins_and_flags_off_width():
    cons = {"rf": {"width_mm": {"F.Cu": 0.2}, "min_segment_mm": 1.0, "neck_max_mm": 1.5}}
    ok = QA.rf_grade("RF_A", {"segments": [("F.Cu", 0.2, 10.0)], "vias": 0}, [], set(), cons)
    bad = QA.rf_grade("RF_A", {"segments": [("F.Cu", 0.3, 10.0)], "vias": 0}, [], set(), cons)
    assert ok == [] and bad[0]["code"] == "RF_WIDTH_OFF" and bad[0]["level"] == "FAIL"


def test_rf_via_limit_and_map_geometry():
    cons = {"rf": {"max_vias": 1}}
    out = QA.rf_grade("RF_A", {"segments": [("F.Cu", 0.15, 5.0)], "vias": 2}, [], set(), cons, {"rf_geometry": {"*": 0.15}})
    assert [f["code"] for f in out] == ["RF_VIA_COUNT"]


def test_phase_order():
    cls = {"P": {"class": "pair"}, "R": {"class": "rail"}, "S": {"class": "signal"}}
    ph = BR.phase_map({}, ["P", "R", "S", "E", "X"], cls, {"nets": {"X": {"straight": True}}}, escape_nets=["E"])
    assert ph == {"P": "controlled", "R": "rails", "S": "rest", "E": "power_pins", "X": "plain"}
    assert BR.order_summary(["X", "E", "R", "P", "S"], ph)[0]["phase"] == "plain"
