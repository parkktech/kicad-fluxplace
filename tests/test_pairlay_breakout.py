"""PAIRS-S2: a fine-pitch pad's break-out leaves along the pad axis; PAIRS-S1: a pair's own copper is no obstacle to itself."""
from fluxplace import pairlay as PL, pairgeo as G


def _pad(num, net, x, y):
    return {"ref": "J", "num": num, "net": net, "x": x, "y": y, "w": 0.2, "h": 0.7, "angle": 180.0, "layers": {"F.Cu"}, "shape": "roundrect"}


def test_pad_axis_points_along_the_long_side_towards_the_target():
    p = _pad("1", "A", 0.0, 0.0)
    ax = PL.pad_axis(p, (0.0, 1.0))
    assert abs(ax[0]) < 1e-9 and ax[1] > 0.99
    assert PL.pad_axis(p, (0.0, -1.0))[1] < -0.99
    assert PL.pad_axis(dict(p, w=0.5, h=0.5), (0, 1)) is None


def test_axis_candidates_exist_and_the_row_neighbour_is_cleared():
    """0.4 mm pitch, 0.2 mm pads: the old candidates ran along the pad row into the GND pad; an axis-first one clears it."""
    q, e, t = (0.0, 0.0), (-0.95, 0.9), (-0.7071, 0.7071)
    items = G.Items(["F.Cu"])
    items.pad(dict(_pad("2", "GND", -0.4, 0.0), net="GND"))
    plain = PL.doglegs(q, e, t)
    axis = PL.doglegs(q, e, t, axis=PL.pad_axis(_pad("1", "A", 0.0, 0.0), e))

    def best(c):
        return max(PL.check(items, [("seg", "F.Cu", a, b, 0.15) for a, b in zip(x, x[1:])], "A", 0.127, ())[0] for x in c)
    assert best(plain) < 0.127 - 1e-6
    assert best(axis) >= 0.127 - 1e-6
    assert all(x[1][0] == 0.0 or x[0] == q for x in axis)


def test_own_net_copper_is_not_an_obstacle():
    items = G.Items(["F.Cu"])
    items.seg((0.0, 0.0), (1.0, 0.0), 0.15, "F.Cu", "A")
    items.seg((0.0, 0.0), (1.0, 0.0), 0.15, "F.Cu", "B")
    assert PL.check(items, [("seg", "F.Cu", (0.5, -1.0), (0.5, 1.0), 0.15)], "A", 0.127, ())[0] < 0           # B crosses
    items2 = G.Items(["F.Cu"])
    items2.seg((0.0, 0.0), (1.0, 0.0), 0.15, "F.Cu", "A")
    assert PL.check(items2, [("seg", "F.Cu", (0.5, -1.0), (0.5, 1.0), 0.15)], "A", 0.127, ())[0] >= 1e8    # own copper
