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
    line = RDY.fixed_line({"J12"}, cons, {"J12": [_row("J12", 40.0)]})
    assert line["status"] == RDY.PASS
    assert line["blockers"] == []


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
    assert FR.findings({"C9"}, cons, {"C9": [_row("C9", 30.0, sure=False)]}) == []


def test_within_max_passes():
    cons = _cons(C9={"reason": "owner", "decision": "D71dh"})
    assert FR.findings({"C9"}, cons, {"C9": [_row("C9", 4.0)]}) == []


def test_unclassified_fixed_cap_over_max_is_still_graded():
    f = FR.findings({"C30"}, {}, {"C30": [_row("C30", 12.0)]})
    assert [x["code"] for x in f] == ["FIXED_NO_REASON", "FIXED_OVER_BUDGET"]
