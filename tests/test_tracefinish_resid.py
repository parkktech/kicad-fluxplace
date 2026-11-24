"""T1b residuals: joint matching grid, degenerate segments, pair class key (W15 baseline vs Fable's expected counts)."""
import math, os, sys
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import tfmeasure as TM
from test_tracefinish import seg, model, wedge


def test_joints_one_micrometre_apart_still_join():
    # the old 1e-3 grid split a joint that straddles a rounding boundary and merged joints 1 um apart
    a = seg((0, 0), (5.0004999, 0))
    b = seg((5.0005001, 0), (9, 4))
    assert len(TM.corners(model([a, b]))) == 1
    far = [seg((0, 0), (5.0, 0)), seg((5.0006, 0), (9, 4))]       # 0.6 um apart: different grid cell, no joint at 1e-4
    assert TM.corners(model(far)) == []


def test_duplicate_and_zero_segments_make_no_backtrack_corner():
    dup = [seg((0, 0), (5, 0)), seg((5, 0), (0, 0))]               # same copper drawn twice, reversed
    assert TM.corners(model(dup)) == [] and TM.junctions(model(dup)) == []
    zero = [seg((0, 0), (5, 0)), seg((5, 0), (5, 0)), seg((5, 0), (8, 3))]
    cs = TM.corners(model(zero))
    assert len(cs) == 1 and cs[0]["kind"] == "45"


def test_duplicate_does_not_turn_a_corner_into_a_junction():
    segs = wedge(135) + [dict(wedge(135)[0])]                      # the first leg twice
    assert len(TM.corners(model(segs))) == 1 and TM.junctions(model(segs)) == []


def test_degenerate_segments_are_their_own_hygiene_measure():
    segs = [seg((0, 0), (5, 0)), seg((5, 0), (0, 0)), seg((7, 0), (7, 0))]
    h = TM.degenerate(model(segs))
    assert h == {"zero": 1, "duplicate": 1}
    assert sorted(s["state"] for s in TM.m9_sites(model(segs))).count("open") == 2


def test_pair_class_uses_skew_max_mm_key_of_the_constraints():
    from test_tracefinish import _two_nets
    cons = {"pairs": {"USB3_": {"impedance_diff": 90, "skew_max_mm": 0.127}, "ETH_": {"impedance_diff": 100, "skew_max_mm": 0.5}}}
    import tempfile, pathlib
    B = _two_nets(pathlib.Path(tempfile.mkdtemp()), [("USB3_DP", "USB3_DN"), ("ETH_TXP", "ETH_TXN")])
    got = TM.classes(B, cons, None)
    assert got["USB3_DP"] == "HSF" and got["ETH_TXP"] == "HSS"
