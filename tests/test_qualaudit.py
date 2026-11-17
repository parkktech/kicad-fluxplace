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


def _B(vias):
    pads = [{"net": n, "x": 0, "y": 0} for n in ("RF_A", "RF_B")]
    return {"pads": pads, "segs": [], "vias": [{"net": n} for n, k in vias.items() for _ in range(k)]}


def test_stated_via_limit_counts_not_done_unless_waived():
    B = _B({"RF_A": 4, "RF_B": 4})
    cons = {"rf": {"nets": ["RF_A", "RF_B"], "max_vias": 1}}
    nd = QA.not_done(B, {}, cons, set(), ctx=([], set(), None))
    assert {r["net"] for r in nd["rf"]} == {"RF_A", "RF_B"} and nd["rf"][0]["cause"] == "via_count"
    assert nd["nets"] == {"RF_A", "RF_B"}
    cons["review"] = {"waive": ["RF_VIA_COUNT:^RF_A:"]}
    nd = QA.not_done(B, {}, cons, set(), ctx=([], set(), None))
    assert [r["net"] for r in nd["rf"]] == ["RF_B"]


def test_default_via_limit_is_not_stated_and_not_counted():
    B = _B({"RF_A": 4})
    nd = QA.not_done(B, {}, {"rf": {"nets": ["RF_A"]}}, set(), ctx=([], set(), None))
    assert nd["rf"] == []
    rows = QA.rf_rows(B, {"rf": {"nets": ["RF_A"]}}, [], set())
    assert rows[0]["level"] == "WARN"          # wording unchanged


def test_rf_rows_nets_filter_returns_only_asked_net():
    B = _B({"RF_A": 4, "RF_B": 3})
    cons = {"rf": {"nets": ["RF_A", "RF_B"], "max_vias": 1}}
    assert {r["net"] for r in QA.rf_rows(B, cons, [], set())} == {"RF_A", "RF_B"}
    rows = QA.rf_rows(B, cons, [], set(), nets=["RF_B"])
    assert {r["net"] for r in rows} == {"RF_B"} and "3 vias" in rows[0]["msg"]
