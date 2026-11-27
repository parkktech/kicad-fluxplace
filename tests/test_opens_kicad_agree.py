"""w113: text opens / pairaudit paths agree with KiCad's connectivity (a track joins a pad or via by body overlap, not only by end point)."""
from fluxplace import routeconn as RC, pairaudit as PA

CU = ["F.Cu", "B.Cu"]


def pad(x, y, w, h, net="N", layer="F.Cu", angle=0.0, num="1"):
    return {"ref": "U", "num": num, "x": x, "y": y, "w": w, "h": h, "layers": {layer}, "net": net, "angle": angle,
            "shape": "rect", "np": False}


def seg(a, b, net="N", layer="F.Cu", w=0.15):
    return {"a": a, "b": b, "layer": layer, "w": w, "net": net}


def via(x, y, net="N", d=0.45):
    return {"x": x, "y": y, "d": d, "net": net, "layers": set(CU)}


def board(pads, segs=(), vias=()):
    return {"pads": list(pads), "segs": list(segs), "vias": list(vias), "zones": [], "copper": CU}


def opens(B):
    return sum(RC.text_open(B).values())


def test_track_end_just_outside_pad_within_half_width():
    # end 0.045 outside the pad edge: the 0.15 track still overlaps it (KiCad: connected)
    B = board([pad(0, 0, 1, 1), pad(5, 0, 1, 1, num="2")], [seg((0.5 + 0.045, 0), (4.5, 0))])
    assert opens(B) == 0 and PA.profile(B, "N")["path"]


def test_track_end_beyond_half_width_is_open():
    B = board([pad(0, 0, 1, 1), pad(5, 0, 1, 1, num="2")], [seg((0.5 + 0.2, 0), (4.5, 0))])
    assert opens(B) == 1 and not PA.profile(B, "N")["path"]


def test_track_body_crossing_pad_joins():
    B = board([pad(0, 0, 1, 1), pad(5, 0, 1, 1, num="2")], [seg((-3, 0.0), (3, 0.0)), seg((3, 0), (5, 0))])
    assert opens(B) == 0 and PA.profile(B, "N")["path"]


def test_rotated_pad():
    B = board([pad(0, 0, 0.5, 2.0, angle=90.0), pad(5, 0, 1, 1, num="2")], [seg((1.0 + 0.05, 0), (5, 0))])
    assert opens(B) == 0


def test_seg_ends_gap_inside_half_width_pairaudit():
    # two collinear ends 0.04 apart, offset sideways: KiCad joins them (end within the other track's width)
    B = board([pad(0, 0, 1, 1), pad(5, 0, 1, 1, num="2")],
              [seg((0, 0), (2, 0)), seg((2.0, 0.04), (5, 0.04))])
    assert PA.profile(B, "N")["path"]


def test_via_on_track_body_pairaudit():
    # a track passes through a via centre (no end there); the other layer leaves from the via
    B = board([pad(0, 0, 1, 1), pad(5, 0, 1, 1, num="2", layer="B.Cu")],
              [seg((0, 0), (3, 0)), seg((1.5, 0), (5, 0), layer="B.Cu")], [via(1.5, 0)])
    assert PA.profile(B, "N")["path"]
    B2 = board([pad(0, 0, 1, 1), pad(5, 0, 1, 1, num="2", layer="B.Cu")],
               [seg((0, 0), (3, 0)), seg((1.5, 0), (5, 0), layer="B.Cu")])
    assert not PA.profile(B2, "N")["path"]
