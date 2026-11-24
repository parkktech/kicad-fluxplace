"""F1 (placer step 2): fixed parts carry a reason class; a non-mechanical fixed support part over its max is OPEN."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import ready as RDY, fixedreason as FR


def _row(ref, dist, mx=5.0, sure=True, role="bypass", serves="U1.7"):
    return {"ref": ref, "role": role, "serves": serves, "dist_mm": dist, "sure": sure, "limit": {"max": mx}}


def _cons(**fixed_reason):
    return {"placement": {"fixed": list(fixed_reason), "fixed_reason": fixed_reason}}


def test_fixed_cap_30mm_from_pin_is_open_with_numbers():
    cons = _cons(C9={"reason": "owner", "decision": "D71dh"})
    line = RDY.fixed_line({"C9"}, cons, {"C9": [_row("C9", 30.0)]})
    assert line["status"] == RDY.OPEN
    codes = "\n".join(line["blockers"])
    assert "FIXED_OVER_BUDGET" in codes and "C9" in codes
    assert "30 mm" in codes and "max 5 mm" in codes


def test_mechanical_connector_passes():
    cons = _cons(J12={"reason": "mechanical", "note": "outline"})
    line = RDY.fixed_line({"J12"}, cons, {})
    assert line["status"] == RDY.PASS
    assert line["blockers"] == [] and "open_items" not in line


def test_mechanical_is_no_exemption_for_a_support_part():
    # the loophole: a capacitor declared mechanical is still graded, and the class itself is refused
    f = FR.findings({"C9"}, _cons(C9={"reason": "mechanical"}), {"C9": [_row("C9", 30.0)]})
    assert [x["code"] for x in f] == ["FIXED_OVER_BUDGET", "FIXED_REASON_MISMATCH"]
    assert "30 mm" in f[0]["msg"] and "max 5 mm" in f[0]["msg"] and "over by 25 mm" in f[0]["msg"]
    for reason in ("enclosure", "module-mount"):
        f = FR.findings({"C9"}, _cons(C9={"reason": reason}), {"C9": [_row("C9", 30.0)]})
        assert [x["code"] for x in f] == ["FIXED_OVER_BUDGET"]


def test_enclosure_connector_on_the_edge_passes():
    cons = _cons(J12={"reason": "enclosure", "note": "D71dh"})
    assert FR.findings({"J12"}, cons, {}) == []


def test_fixed_part_without_reason_is_open_and_named():
    f = FR.findings({"J4", "J3"}, _cons(J3={"reason": "enclosure"}), {})
    assert [(x["code"], x["ref"]) for x in f] == [("FIXED_NO_REASON", "J4")]
    assert "J4" in f[0]["msg"] and f[0]["level"] == "OPEN"


def test_unknown_reason_class_is_open():
    f = FR.findings({"J4"}, _cons(J4={"reason": "thermal"}), {})
    assert f[0]["code"] == "FIXED_NO_REASON" and "thermal" in f[0]["msg"]


def test_owner_without_decision_id_is_open():
    f = FR.findings({"D600"}, _cons(D600={"reason": "owner"}), {})
    assert [x["code"] for x in f] == ["FIXED_OWNER_NO_DECISION"]
    assert "D600" in f[0]["msg"]


def test_owner_with_decision_id_passes():
    assert FR.findings({"J205"}, _cons(J205={"reason": "owner", "decision": "D71dh"}), {}) == []


def test_unsure_distance_is_not_graded():
    cons = _cons(C9={"reason": "owner", "decision": "D71dh"})
    f = FR.findings({"C9"}, cons, {"C9": [_row("C9", 30.0, sure=False)]})
    assert [x["code"] for x in f] == ["FIXED_OWNER_GUESSED"]        # not graded, and not silent either


def test_within_max_passes():
    cons = _cons(C9={"reason": "owner", "decision": "D71dh"})
    assert FR.findings({"C9"}, cons, {"C9": [_row("C9", 4.0)]}) == []


def test_unclassified_fixed_cap_over_max_is_still_graded():
    f = FR.findings({"C30"}, {}, {"C30": [_row("C30", 12.0)]})
    assert [x["code"] for x in f] == ["FIXED_OVER_BUDGET", "FIXED_NO_REASON"]          # the fault with numbers comes first
    assert "(no reason)" in f[0]["msg"] and "None" not in f[0]["msg"]


def test_worst_row_is_reported_and_a_guessed_pin_is_named():
    cons = _cons(C9={"reason": "owner", "decision": "D1"}, C8={"reason": "owner", "decision": "D1"})
    f = FR.findings({"C9", "C8"}, cons, {"C9": [_row("C9", 6.0), _row("C9", 40.0, serves="U2.3")],
                                         "C8": [_row("C8", 45.8, sure=False)]})
    assert [(x["code"], x["ref"]) for x in f] == [("FIXED_OVER_BUDGET", "C9"), ("FIXED_OWNER_GUESSED", "C8")]
    assert "U2.3" in f[0]["msg"] and "40 mm" in f[0]["msg"]
    assert "45.8 mm" in f[1]["msg"]


def test_detected_anchor_is_undeclared_unless_declared_or_movable():
    cons = _cons(J3={"reason": "enclosure"})
    f = FR.findings({"J3"}, cons, {}, detected={"J5", "J400"})
    assert [(x["code"], x["ref"]) for x in f] == [("FIXED_UNDECLARED", "J400"), ("FIXED_UNDECLARED", "J5")]
    cons["floorplan"] = {"movable": ["J5"]}
    cons["placement"]["fixed_reason"]["J400"] = {"reason": "enclosure"}
    assert FR.findings({"J3"}, cons, {}, detected={"J5", "J400"}) == []


def test_unconnected_part_is_mechanical_without_a_declaration():
    assert FR.findings({"MH1", "J3"}, _cons(J3={"reason": "enclosure"}), {}, unconnected={"MH1"}) == []


def test_line_lists_every_fault_and_groups_the_reason_only_findings():
    fixed = {f"C{i}" for i in range(1, 9)} | {f"J{i}" for i in range(1, 31)}
    rows = {f"C{i}": [_row(f"C{i}", 20.0 + i)] for i in range(1, 9)}
    detail = {}
    line = RDY.fixed_line(fixed, {}, rows, detail=detail)
    over = [b for b in line["blockers"] if b.startswith("FIXED_OVER_BUDGET")]
    assert len(over) == 8                                           # none hidden behind a row cap
    assert line["blockers"][-1].startswith("FIXED_NO_REASON 38 part(s): C1, C2")
    assert len(line["blockers"]) == 9 and line["open_items"] == 46 == len(detail["fixed_reasons"])
    assert line["status"] == RDY.OPEN and "38 of 38" in line["detail"]


def _cls(*pairs, **kw):
    out = {}
    for a, b in pairs:
        out[a] = {"class": "pair", "partner": b, "override": False}
        out[b] = {"class": "pair", "partner": a, "override": False}
    out.update(kw)
    return out


def test_name_pair_without_a_family_is_open_and_named():
    cls = _cls(("USB_D_N", "USB_D_P"), ("MIC_N", "MIC_P"), ("BT1_DM", "BT1_DP"), X={"class": "signal", "partner": None})
    cons = {"pairs": {"USB_": {"impedance_diff": 90}}}
    assert FR.unclassified_pairs(cls, cons) == ["BT1_DM/BT1_DP", "MIC_N/MIC_P"]
    line = RDY.pair_class_line(cons, cls)
    assert line["status"] == RDY.OPEN and line["open_items"] == 2
    assert line["blockers"][0].startswith("PAIR_UNCLASSIFIED 2 pair(s): BT1_DM/BT1_DP, MIC_N/MIC_P")
    cons["channel"] = {"MIC_": {"length_mm_max": 50}}
    cls["BT1_DM"]["override"] = True                                # a per-net budget is a class too
    line = RDY.pair_class_line(cons, cls)
    assert line["status"] == RDY.PASS and "3 name pair(s)" in line["detail"] and "open_items" not in line
