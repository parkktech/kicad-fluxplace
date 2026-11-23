"""channel_noise on hand-built board dicts (the routediag.load shape): crosstalk, spacing, broadside, plane gap,
return vias, noisy-net distance. No KiCad needed."""
import pytest

from fluxplace import channel_noise as CN
from tests.test_channel import ROWS, PLANES

W = 0.15


def seg(net, a, b, layer="F.Cu", w=W):
    return {"net": net, "a": a, "b": b, "w": w, "layer": layer}


def board(segs=(), vias=(), pads=(), zones=()):
    return {"segs": list(segs), "vias": list(vias), "pads": list(pads), "zones": list(zones)}


def run(B, b, legs=("V_P", "V_N"), cls=None):
    return {m["name"]: m for m in CN.analyse(B, ROWS, PLANES, list(legs), b, {}, None, cls or {})}


def test_parallel_aggressor_gap_spacing_and_xtalk_trend():
    far = board([seg("V_P", (0, 0), (20, 0)), seg("A", (0, 1.0), (20, 1.0))])
    near = board([seg("V_P", (0, 0), (20, 0)), seg("A", (0, 0.3), (20, 0.3))])
    b = {"xtalk_db_max": -25.0, "freq_ghz": 4.0}
    f, n = run(far, b), run(near, b)
    assert n["spacing"]["value"] == pytest.approx((0.3 - W) / W)             # edge gap in track widths
    assert f["spacing"]["value"] == pytest.approx((1.0 - W) / W)
    assert n["xtalk"]["value"] > f["xtalk"]["value"] and n["xtalk"]["value"] < 0
    assert n["spacing"]["limit"] == 2.0 and n["spacing"]["target"] == 3.0 and n["spacing"]["minimum"]


def test_crossing_and_distant_runs_are_not_aggressors():
    B = board([seg("V_P", (0, 0), (20, 0)), seg("A", (10, -5), (10, 5)), seg("GND", (0, 0.3), (20, 0.3))])
    m = run(B, {"xtalk_db_max": -25.0})
    assert m["xtalk"]["value"] is None and m["spacing"]["value"] is None


def test_broadside_overlap_on_thin_dielectric_layer_only():
    B = board([seg("V_P", (0, 0), (20, 0), "In2.Cu"), seg("A", (5, 0), (15, 0), "In3.Cu"), seg("X", (0, -5), (0, 5), "In3.Cu")])
    assert run(B, {"broadside_mm_max": 3.0})["broadside"]["value"] == pytest.approx(10.0)
    assert CN.coupled_layers(ROWS, PLANES, "In2.Cu") == {"In3.Cu": pytest.approx(0.175)}
    assert CN.coupled_layers(ROWS, PLANES, "F.Cu") == {}                        # neighbour is a plane
    off = board([seg("V_P", (0, 0), (20, 0), "In2.Cu"), seg("A", (5, 0.5), (15, 0.5), "In3.Cu")])   # beside, not above
    assert run(off, {"broadside_mm_max": 3.0})["broadside"]["value"] == 0.0


def test_plane_gap_counts_void_under_the_path():
    zone = {"net": "GND", "layers": {"In1.Cu"}, "keepout": False, "pts": [],
            "fills": [("In1.Cu", [(-5, -5), (5, -5), (5, 5), (-5, 5)]), ("In1.Cu", [(7, -5), (25, -5), (25, 5), (7, 5)])]}
    B = board([seg("V_P", (-3, 0), (23, 0))], zones=[zone])                      # void between x=5 and x=7
    m = run(B, {"plane_gap_mm_max": 0.5})["plane"]
    assert m["value"] == pytest.approx(2.0, abs=0.6) and "1 void run" in m["detail"]
    whole = dict(zone, fills=[("In1.Cu", [(-5, -5), (25, -5), (25, 5), (-5, 5)])])
    assert run(board([seg("V_P", (-3, 0), (23, 0))], zones=[whole]), {"plane_gap_mm_max": 0.5})["plane"]["value"] == 0.0


def test_return_via_distance():
    B = board(vias=[{"net": "V_P", "x": 0, "y": 0, "d": 0.46}, {"net": "GND", "x": 3, "y": 4, "d": 0.46}, {"net": "OTHER", "x": 0.5, "y": 0, "d": 0.46}])
    assert run(B, {"return_via_mm": 5.0})["return_via"]["value"] == pytest.approx(5.0)


def test_noisy_net_distance_edge_to_edge_and_classes():
    B = board([seg("V_P", (0, 0), (20, 0)), seg("SW", (0, 2.0), (20, 2.0), w=0.5), seg("SIG", (0, 0.4), (20, 0.4))])
    cls = {"SW": {"class": "switch_node"}, "SIG": {"class": "signal"}}
    m = run(B, {"noisy_mm": 1.0, "noisy_mm_target": 3.0}, cls=cls)["noisy"]
    assert m["value"] == pytest.approx(2.0 - 0.25 - W / 2) and m["minimum"] and "SW" in m["detail"]
    assert run(B, {"noisy_mm": 1.0, "noisy_classes": ["signal"]}, cls=cls)["noisy"]["value"] == pytest.approx(0.4 - W)
