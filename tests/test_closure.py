import numpy as np

from fluxplace import pocket as PK


def _board(extra_segs=()):
    pad = {"ref": "R1", "num": "1", "x": 5.0, "y": 5.0, "w": 0.6, "h": 0.6, "ex": 0.3, "ey": 0.3, "layers": {"F.Cu"},
           "net": "A", "angle": 0.0, "shape": "rect", "np": False, "tht": False}
    return {"pads": [pad], "segs": list(extra_segs), "vias": [], "zones": [], "edge": [], "copper": ["F.Cu", "B.Cu"],
            "clearance": 0.127, "track": 0.15}


def _wall(x):
    return {"net": "B", "a": (x, 0.0), "b": (x, 10.0), "w": 0.2, "layer": "F.Cu", "locked": False}


def test_field_pocket_and_wall():
    B = _board([_wall(4.0), _wall(6.0)])
    F = PK.Field(B, (5.0, 5.0), 3.0, 0.05, own={"A"})
    seeds = F.shape_mask(B["pads"][0])
    d = PK.dist(F.free("F.Cu", 0.15), seeds, 0.05)
    reach = d < PK.INF / 2
    assert reach.any() and not reach[:, 0].any()           # walled left and right: the pocket never leaves the strip x in 4..6
    assert PK.area(reach, 0.05) < 2.0 * 6.0
    assert F.via_ok(0.45).any()


def test_nothing_foreign_means_open_pocket():
    B = _board()
    F = PK.Field(B, (5.0, 5.0), 3.0, 0.05, own={"A"})
    d = PK.dist(F.free("F.Cu", 0.15), F.shape_mask(B["pads"][0]), 0.05)
    assert (d[0, :] < PK.INF / 2).any()


def test_pad_sdf_sign():
    p = {"x": 0, "y": 0, "w": 1.0, "h": 1.0, "angle": 0, "shape": "rect"}
    assert PK.pad_sdf(p, np.array(0.0), np.array(0.0)) < 0 < PK.pad_sdf(p, np.array(2.0), np.array(0.0))
