"""critroute lost clocks to "grid path failed exact probe": the grid path's ends were moved onto a via centre and left a stub inside
the via (track_not_centered_on_via) or a long segment off 45 degrees (angle_not_45_90). W16 route-10 resume-5: MCLK, HUB_XTAL2, SIM_CLK."""
import math
import os, sys
from types import SimpleNamespace as NS
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import handroute as HR, hrgeom as G  # noqa: E402

CU = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]
VD = 0.6


def _m(*own):
    return NS(cu=CU, obs=[NS(kind="via", net="N", g=("c", c, None, 0.3)) for c in own])


def _bad(path, own):
    """The probe's two geometry rules on a path string: [] when clean."""
    prims = G.parse_path(path, CU)
    vias = list(own) + [p[1] for p in prims if p[0] == "via"]
    bad = []
    for p in prims:
        if p[0] != "seg":
            continue
        a, b = p[2], p[3]
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 45
        if math.hypot(b[0] - a[0], b[1] - a[1]) >= 0.1 and min(ang, 45 - ang) > 0.01:
            bad.append(("angle", a, b))
        bad += [("off-centre", pt, c) for pt in (a, b) for c in vias if 0.0005 < math.hypot(pt[0] - c[0], pt[1] - c[1]) < 0.3]
    return bad


def _ends(path):
    prims = G.parse_path(path, CU)
    return prims[0][2] if prims[0][0] == "seg" else prims[0][1], prims[-1][3] if prims[-1][0] == "seg" else prims[-1][1]


CASES = [   # (name, path as the old variants gave it, own-net vias on the board)
    ("MCLK: start snapped onto an own via, 0.28 mm first piece at 175 deg",
     "B.Cu: 183.7762,49.4000 183.4967,49.4250 182.9467,49.9750", [(183.7762, 49.4)]),
    ("HUB_XTAL2: start snapped onto an own via bent the 5.5 mm diagonal to 46.17 deg",
     "In3.Cu: 192.1650,87.0409 195.9704,91.0044 195.9704,91.4044; via 195.9704,91.4044", [(192.165, 87.0409)]),
    ("SIM_CLK: 0.06 mm sideways between the path's via and an own via, knee 0.083 mm from the centre",
     "B.Cu: 151.2963,54.9386 152.3463,53.8886; via 152.3463,53.8886; In3.Cu: 152.3463,53.8886 152.3463,45.9974 152.4051,45.9386",
     [(152.4051, 45.9386)]),
    ("SIM_CLK: 0.07 mm F.Cu stub wholly inside the path's own via",
     "F.Cu: 131.9824,55.6886 132.2524,55.6886 132.2524,55.9200 132.3024,55.9700; via 132.3024,55.97; In3.Cu: 132.3024,55.9700 147.2024,55.9700", []),
]


def test_run_ends_on_a_via_are_redrawn_clean():
    for name, path, own in CASES:
        assert _bad(path, own), name                                   # the old variant breaks a probe rule
        new = HR._via_ends(_m(*own), "N", path, VD)
        assert not _bad(new, own), (name, new, _bad(new, own))
        assert all(math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-3 for a, b in zip(_ends(path), _ends(new))), name   # same two ends
        assert [p[1] for p in G.parse_path(new, CU) if p[0] == "via"] == [p[1] for p in G.parse_path(path, CU) if p[0] == "via"], name


def test_a_last_run_that_never_leaves_its_via_is_dropped():
    path = "F.Cu: 2.0000,0.0000 9.3000,0.0000; via 9.3,0.0; B.Cu: 9.3000,0.0000 15.9000,0.0000; via 15.9,0.0; F.Cu: 15.9000,0.0000 16.1000,0.0000"
    new = HR._via_ends(_m(), "N", path, VD)
    assert not _bad(new, []) and G.parse_path(new, CU)[-1] == ("via", (15.9, 0.0))
    m = _m()                                             # with an own-net stub ahead the run is carried on to its first end outside the via
    m.obs.append(NS(kind="track", net="N", g=("s", (16.2, 0.04), (16.7, 0.04))))
    new = HR._via_ends(m, "N", path, VD)
    last = G.parse_path(new, CU)[-1]
    assert not _bad(new, []) and last[0] == "seg" and last[3] == (16.7, 0.04)


def test_a_clean_path_and_a_path_without_vias_are_returned_unchanged():
    m = _m()
    for path in ("F.Cu: 10.0000,10.0000 12.0000,12.0000 15.0000,12.0000",
                 "F.Cu: 10.0000,10.0000 12.0000,12.0000; via 12.0,12.0; B.Cu: 12.0000,12.0000 12.0000,20.0000"):
        assert G.parse_path(HR._via_ends(m, "N", path, VD), CU) == G.parse_path(path, CU)


def test_suggest_probes_the_redrawn_variant_after_the_plain_ones():
    import inspect
    src = inspect.getsource(HR.suggest)
    assert src.index("_via_ends(M, net, v, vd)") > src.index("_snap_ends(M, net")      # plain variants keep their order: no change where they pass


def test_a_capped_drc_type_does_not_refuse_a_lay_for_another_net():
    """kicad-cli lists 199 violations per type. With 200+ dangling fan-out vias, a lay that ends on one of them moves entry 200 into
    the list: "new DRC violation: via_dangling Via [HID_A]" refused MCLK, SIM_CLK and BT_SW_RFC (W16 route-10 resume-5)."""
    from collections import Counter
    dang = lambda n: {"type": "via_dangling", "items": [{"description": f"Via [{n}] on F.Cu - B.Cu"}]}
    base = {"violations": [dang(f"N{i}") for i in range(198)] + [dang("MCLK")]}
    cand = {"violations": [dang(f"N{i}") for i in range(198)] + [dang("HID_A")]}
    assert HR.capped_types(base, cand) == {"via_dangling"} and HR.capped_types({"violations": base["violations"][:198]}) == set()
    foreign = ("via_dangling", ("Via [HID_A] on F.Cu - B.Cu",))
    own = ("via_dangling", ("Via [MCLK] on F.Cu - B.Cu",))
    clr = ("clearance", ("Track [HID_A] on F.Cu, length # mm",))
    new = Counter({foreign: 1, own: 1, clr: 1})
    assert HR._edit_violation_keys(new, "MCLK", capped={"via_dangling"}) == {own: 1, clr: 1}      # own net and other types still count
    assert HR._edit_violation_keys(new, "MCLK") == {foreign: 1, own: 1, clr: 1}                    # below the cap the list is complete
