"""B10: the total map review FAIL code -> gap kind -> owner stage; the placement gate lists the review's placement findings;
a crystal 20 mm from its pins fails the placement gate (microboard)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))      # tests/ is a package: pcbgen is not importable alone otherwise
from fluxplace import gapmap as GM, placegaps as PG, routediag as RD  # noqa: E402
import pcbgen as g  # noqa: E402


def test_every_review_fail_code_has_an_owner_stage_or_the_data_owner_tag():
    codes = GM.fail_codes()
    assert len(codes) > 100                                    # the scan found the review's FAILs, not an empty set
    assert GM.missing(codes) == [], "FAIL codes with no row in gapmap._X (add a gap kind + owner stage, or data/owner)"
    for c in codes:
        assert GM.owner(c) in GM.STAGES | {GM.OWNER_DATA}, c


def test_the_scan_sees_the_known_placement_and_routing_fails():
    codes = GM.fail_codes()
    for c in ("SUPPORT_PART_FAR", "NET_OVER_BUDGET", "PIN_ESCAPE_BLOCKED", "UNROUTED", "POWER_WIDTH", "SILK_OVER_PAD", "PAIR_SKEW"):
        assert c in codes, c


def test_a_new_fail_without_a_row_is_reported_missing():
    assert GM.missing(["SUPPORT_PART_FAR", "BRAND_NEW_FAIL"]) == ["BRAND_NEW_FAIL"]
    assert GM.owner("SILK_BRAND_NEW") == "silk"                # a family prefix covers a new member


def test_mapped_gap_kinds_exist_in_a_dispatcher():
    known = GM.kinds_known()
    for c in GM.fail_codes():
        k = GM.kind(c)
        assert k is None or k in known, (c, k)


def test_net_budget_is_a_placegaps_kind_with_a_detector():
    assert "net-budget" in PG.ORDER and "net-budget" in PG.DETECT and "net-budget" in PG.DISPATCH
    assert "net-budget" in PG.SCOPE["floorplan"] and "net-budget" in PG.SCOPE["placetour"]


def test_uncovered_lists_a_review_finding_the_gate_did_not_list():
    fs = [{"level": "FAIL", "code": "SUPPORT_PART_FAR", "refs": ["C1", "U1"]},
          {"level": "FAIL", "code": "NET_OVER_BUDGET", "refs": ["U1", "U2"]},
          {"level": "FAIL", "code": "SILK_OVER_PAD", "refs": ["C1"]},          # not a placement kind: not the gate's
          {"level": "WARN", "code": "SUPPORT_PART_FAR", "refs": ["C9", "U1"]}]
    gaps = [PG.gap("support-far", "C1", refs=("C1", "U1"))]
    assert [f["code"] for f in GM.uncovered(fs, gaps)] == ["NET_OVER_BUDGET"]


def _crystal_board(tmp_path, dist):
    xt = g.fp("Y2", 10 + dist, 10, [g.pad("1", 0, 0, "XIN"), g.pad("2", 1, 0, "XOUT")])
    u = g.ic("U1", 10, 10, ["XIN", "XOUT", "GND", "a", "b", "c"])
    return g.board(tmp_path, u + xt)


def _gate(board, kinds=None):
    ctx = PG.Ctx(stage="floorplan", board=board, cons={}, spec={}, ds_dir=None)
    return PG.detect(ctx, kinds or PG.SCOPE["floorplan"])


def test_microboard_crystal_20mm_from_its_pins_fails_the_placement_gate(tmp_path):
    far = _gate(_crystal_board(tmp_path, 20.0), ["net-budget"])
    assert {gp["id"] for gp in far["gaps"]} == {"net-budget:XIN", "net-budget:XOUT"}
    assert all(gp["mag"] > 5.0 for gp in far["gaps"])            # 20 mm against the 8 mm crystal budget
    near = _gate(_crystal_board(tmp_path, 4.0), ["net-budget"])
    assert near["gaps"] == []


@pytest.mark.xfail("net-bound" not in PG.ORDER, strict=False,
                   reason="A8 (w62-placebounds) not merged: the octilinear lower-bound gap kind net-bound does not exist yet")
def test_microboard_crystal_20mm_is_also_a_net_bound_gap_when_a8_is_merged(tmp_path):
    res = _gate(_crystal_board(tmp_path, 20.0), ["net-bound"])
    assert {gp["kind"] for gp in res["gaps"]} == {"net-bound"} and res["gaps"]


REVIEW = "/var/kicad/utv-comms-bridge/W16/view/review-preview-w40.json"
BOARD = "/var/kicad/utv-comms-bridge/W16/CURRENT.kicad_pcb"


@pytest.mark.skipif(not (os.path.exists(REVIEW) and os.path.exists(BOARD)), reason="W16 board not on this machine")
def test_w16_placement_gate_lists_the_review_placement_findings():
    """Real board (slow, ~10 s): every placement FAIL of the review preview is a gap of the gate."""
    fs = [f for f in json.load(open(REVIEW)) if f["level"] == "FAIL" and GM.kind(f["code"]) in ("support-far", "net-budget")]
    assert len(fs) == 19
    from fluxplace import constraints as C
    W = "/var/kicad/utv-comms-bridge/W16/inputs/"
    ctx = PG.Ctx(stage="floorplan", board=BOARD, cons=C.load(W + "constraints.toml"), spec=json.load(open(W + "spec.json")), ds_dir=None)
    res = PG.detect(ctx, ["support-far", "net-budget"])
    assert res["errors"] == {}
    assert GM.uncovered(fs, res["gaps"]) == []


def test_silk_fail_codes_map_to_silk_gap_kinds_owned_by_the_silk_stage():
    """B8: the silk stage runs in the flow; every SILK_ FAIL code has a gap kind and the silk stage owns it."""
    for code, kind in (("SILK_OVER_PAD", "silk-fit"), ("SILK_OFF_BOARD", "silk-fit"), ("SILK_OVER_HOLE", "silk-fit"),
                       ("SILK_TITLE_NO_ROOM", "silk-title"), ("SILK_VERSION_STALE", "silk-title")):
        assert GM.kind(code) == kind and GM.owner(code) == "silk", code
    assert {"silk-fit", "silk-title"} <= GM.kinds_known()
    assert all(GM.kind(c) for c in GM.fail_codes() if c.startswith("SILK_"))
