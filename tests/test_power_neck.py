"""D71da: a narrow run of a power net is exempt from POWER_WIDTH only when proven (length, heat, [[dcdrop]])."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import power as PW  # noqa: E402

STACK = [{"type": "copper", "thickness": 0.035}, {"type": "copper", "thickness": 0.035}]


def seg(x1, y1, x2, y2, w=0.127, layer="F.Cu", net="P"):
    return {"net": net, "layer": layer, "width": w, "kind": "track", "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "x": (x1 + x2) / 2, "y": (y1 + y2) / 2, "len": ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** .5}


def cons(a, dcdrop=True):
    c = {"power": {"P": {"current_a": a, "width_mm": 1.0}}}
    if dcdrop:
        c["dcdrop"] = [{"net": "P", "current_a": a, "drop_mv_max": 150, "source": "t"}]
    return c


def run(segs, a=1.0, dc=None, dcdrop=True):
    f = {"copper_layers": 2, "stackup": STACK, "track_geom": segs}
    return PW.check(f, cons(a, dcdrop), dc)


OK = {"P": {"ok": True, "mv": 20.0, "max": 150.0, "why": ""}}


def test_runs_group_connected_segments_per_layer():
    segs = [dict(seg(0, 0, 1, 0), need=1), dict(seg(1, 0.01, 2, 0), need=1), dict(seg(5, 0, 6, 0), need=1),
            dict(seg(1, 0, 2, 0, layer="B.Cu"), need=1)]
    runs = PW.narrow_runs(segs)
    assert sorted((r["layer"], round(r["len"], 2)) for r in runs) == [("B.Cu", 1.0), ("F.Cu", 1.0), ("F.Cu", 2.0)]


def test_short_run_passes_heat_and_is_exempt():
    # 1 mm x 0.127 mm, 1 A, 1 oz: dT = 1*1.72e-8*1e-6/(8*390*(4.445e-9)^2)
    assert abs(PW.neck_heat_c(1.0, 1.0, 0.127, 1.0) - 0.279) < 0.01
    out = run([seg(0, 0, 1, 0)], dc=OK)
    assert [f["code"] for f in out] == ["POWER_NECKDOWN_OK"] and out[0]["level"] == "INFO"


def test_long_run_fails_length():
    out = run([seg(0, 0, 1.5, 0), seg(1.5, 0, 3, 0)], dc=OK)      # two segments, one 3 mm run
    assert out[0]["code"] == "POWER_WIDTH" and "length 3.00 mm > 2" in out[0]["msg"]


def test_high_current_fails_heat():
    dt = PW.neck_heat_c(10.0, 1.0, 0.127, 1.0)
    assert 27 < dt < 29                                   # 100 x 0.279
    out = run([seg(0, 0, 1, 0)], a=10.0, dc=OK)
    assert out[0]["code"] == "POWER_WIDTH" and f"dT {dt:.1f} C > 10" in out[0]["msg"]


def test_no_dcdrop_row_is_not_exempt():
    out = run([seg(0, 0, 1, 0)], dc={}, dcdrop=False)
    assert out[0]["code"] == "POWER_WIDTH" and "no [[dcdrop]] for P" in out[0]["msg"]


def test_failing_dcdrop_is_not_exempt_and_split_runs_report_separately():
    bad = {"P": {"ok": False, "mv": 160.0, "max": 150.0, "why": ""}}
    out = run([seg(0, 0, 1, 0), seg(5, 0, 6, 0)], dc=bad)
    assert [f["code"] for f in out] == ["POWER_WIDTH"] and "net: drop 160 > 150 mV" in out[0]["msg"] and "2 narrow run(s)" in out[0]["msg"]


def test_mixed_runs_split_ok_and_fail():
    out = run([seg(0, 0, 1, 0), seg(5, 0, 8, 0)], dc=OK)
    assert [f["code"] for f in out] == ["POWER_NECKDOWN_OK", "POWER_WIDTH"]
    assert "1 neck-down run(s), 1.00 mm" in out[0]["msg"]
