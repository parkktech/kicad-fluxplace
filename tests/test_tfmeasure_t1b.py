"""TF T1b: one measure per remaining rule, fixtures that show the row not ok (or ok), rotation / mirror, HARD rows, facts."""
import json, math, os, sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
from fluxplace import tfmeasure as TM, routediag
from test_tracefinish import seg, rot

CU6 = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]


def pad(ref, num, x, y, net, w=0.5, h=0.5, tht=False, drill=0.0, layers=("F.Cu",), shape="rect", td=False):
    return {"ref": ref, "num": num, "x": x, "y": y, "w": w, "h": h, "ex": w / 2, "ey": h / 2, "min": min(w, h), "layers": set(layers),
            "net": net, "angle": 0.0, "shape": shape, "delta": (0, 0), "tht": tht, "np": False, "drill": drill, "rr": 0.25, "td": td, "rm_unused": False}


def via(net, x, y, d=0.46, drill=0.2, td=False, rm=False):
    return {"net": net, "x": x, "y": y, "d": d, "drill": drill, "layers": {"F.Cu", "B.Cu"}, "locked": False, "td": td, "rm_unused": rm}


def mdl(segs=(), pads=(), vias=(), zones=(), copper=("F.Cu", "B.Cu")):
    parts = {}
    for p in pads:
        parts.setdefault(p["ref"], {"pads": [], "n_pads": 0})["pads"].append(p)
    for v in parts.values():
        v["n_pads"] = len({p["num"] for p in v["pads"]})
    return {"segs": list(segs), "pads": list(pads), "parts": parts, "zones": list(zones), "vias": list(vias), "copper": list(copper), "edge": None, "file": None}


def XC(**kw):
    x = {"cls": {}, "ncl": {}, "pairs": {}, "pair_of": {}, "planes": ["In1.Cu", "In4.Cu"], "cu": CU6, "geom": {},
         "ref": {"F.Cu": "In1.Cu", "In2.Cu": "In1.Cu", "In3.Cu": "In4.Cu", "B.Cu": "In4.Cu"}, "h": {"F.Cu": 0.1195, "B.Cu": 0.1195},
         "f_ghz": 2.4, "rf": {}, "fab": {}, "roles": {}, "esd": set(), "cons": {}, "stack": []}
    x.update(kw)
    return x


def pairx(w=0.2, gap=0.2, target=0.1, fam="F_", cls="HSS", a="A", b="B", **kw):
    pe = {"fam": fam, "cls": cls, "target": target, "max": target, "imp": 90, "window": None, "w": w, "gap": gap, "max_vias": None}
    pe.update(kw)
    return {"pairs": {(a, b): pe}, "pair_of": {a: (a, b), b: (a, b)}, "cls": {a: cls, b: cls}}


def poly(pts, net="N", layer="F.Cu", w=0.2):
    return [seg(pts[i], pts[i + 1], net=net, layer=layer, w=w) for i in range(len(pts) - 1)]


def arc(R, cx=0.0, cy=0.0, net="N", w=0.2, layer="F.Cu"):
    return seg((cx + R, cy), (cx, cy + R), mid=(cx + R * math.cos(math.pi / 4), cy + R * math.sin(math.pi / 4)), net=net, w=w, layer=layer)


def offset_poly(pts, d):
    """left offset of a polyline by d with mitre joins (a coupled partner of a pair member)."""
    out = []
    n = len(pts)
    for i, p in enumerate(pts):
        dirs = []
        if i:
            dx, dy = p[0] - pts[i - 1][0], p[1] - pts[i - 1][1]
            L = math.hypot(dx, dy); dirs.append((dx / L, dy / L))
        if i < n - 1:
            dx, dy = pts[i + 1][0] - p[0], pts[i + 1][1] - p[1]
            L = math.hypot(dx, dy); dirs.append((dx / L, dy / L))
        if len(dirs) == 1:
            u = dirs[0]
            out.append((p[0] - u[1] * d, p[1] + u[0] * d))
        else:
            (ax, ay), (bx, by) = dirs
            nx, ny = -(ay + by), (ax + bx)
            L = math.hypot(nx, ny)
            nx, ny = nx / L, ny / L
            c = nx * (-ay) + ny * ax
            out.append((p[0] + nx * d / c, p[1] + ny * d / c))
    return out


def st(sites):
    return sorted(s["state"] for s in sites)


# ---- C rules -------------------------------------------------------------------------------------------------------------

def test_c3_chamfer_leg_scores():
    ch = [{"net": "N", "layer": "F.Cu", "x": 0, "y": 0, "w": 0.2, "leg": l} for l in (0.4, 0.3, 0.1)]
    assert st(TM.c3_sites(mdl(), ch)) == ["already", "fallback", "open"]
    assert TM.c3_sites(mdl()) == []
    assert TM.score_of(["already", "fallback"]) == (0.75, 2)


def test_c6_arc_radius_by_class():
    X = XC(cls={"R": "RF"})
    assert st(TM.c6_sites(mdl([arc(0.6, net="R"), arc(0.3, net="R")]), X)) == ["already", "open"]
    assert st(TM.c6_sites(mdl([arc(0.6, net="R")]), XC(cls={"R": "RF"}, f_ghz=5.8))) == ["open"]      # 5 w at >= 5 GHz
    assert TM.c6_sites(mdl([arc(0.6, net="G")]), XC(cls={"G": "GEN"})) == []


def _bends_path(first, second):
    return poly([(0, 0), (5, 0), (5 + first * 0.7071, first * 0.7071), (5 + first * 0.7071, first * 0.7071 + second)], net="R")


def test_c7_same_way_neighbours():
    X = XC(cls={"R": "RF"})
    assert st(TM.c7_sites(mdl(_bends_path(0.3, 6)), X)) == ["open"]
    assert st(TM.c7_sites(mdl(_bends_path(1.5, 6)), X)) == ["already"]


def test_c8_pair_arcs_concentric():
    X = XC(**pairx(w=0.2, gap=0.2))
    good = [arc(1.0, net="A"), arc(1.4, net="B")]
    bad = [arc(1.0, net="A"), arc(1.4, cx=0.03, net="B")]
    assert st(TM.c8_sites(mdl(good), X)) == ["already", "already"]
    assert st(TM.c8_sites(mdl(bad), X)) == ["open", "open"]


@pytest.mark.parametrize("extra,state", [(0.0, "already"), (0.03, "open")])
def test_c9_pair_gap_through_the_bend(extra, state):
    pts = [(0, 0), (5, 0), (7, 2), (12, 2)]
    X = XC(**pairx(w=0.2, gap=0.2))
    segs = poly(pts, net="A") + poly(offset_poly(pts, 0.4 + extra), net="B")
    sites = [s for s in TM.c9_sites(mdl(segs), X) if s["net"] == "A"]
    assert len(sites) == 2 and {s["state"] for s in sites} == {state}


def test_c11_skew_vs_target():
    a = {("A", "B"): {"skew": 0.05, "target": 0.1}, ("C", "D"): {"skew": 0.3, "target": 0.1}, ("E", "F"): {"skew": None, "target": None}}
    out = TM.c11_sites(XC(), a)
    assert st(out) == ["already", "not_possible", "open"] and [s["reason"] for s in out if s["state"] == "not_possible"] == ["LENGTH"]


@pytest.mark.parametrize("L,state", [(1.0, "already"), (0.2, "open")])
def test_c12_bump_legs(L, state):
    pts = [(0, 0), (3, 0), (3, L), (5, L), (5, 0), (8, 0)]
    X = XC(**pairx(w=0.15), h={"F.Cu": 0.1195})
    sites = TM.c12_sites(mdl(poly(pts, net="A", w=0.15)), X)
    assert len(sites) == 1 and sites[0]["state"] == state


def test_c16_signed_bend_sum_per_pair():
    X = XC(**pairx(w=0.2, gap=0.2, target=0.1))
    same = [(0, 0), (5, 0), (7, 2), (7, 7)]                   # one-way 45 + 45 on both members
    segs = poly(same, net="A") + poly(offset_poly(same, 0.4), net="B")
    assert st(TM.c16_sites(mdl(segs), X)) == ["open"]
    zig = [(0, 0), (5, 0), (6, 1), (11, 1), (12, 0), (17, 0)]  # left then right: balanced
    segs = poly(zig, net="A") + poly(offset_poly(zig, 0.4), net="B")
    s = TM.c16_sites(mdl(segs), X)
    assert st(s) == ["already"] and abs(s[0]["sum_deg"]) < 1e-6


def test_c17_regular_pitch_corner_train_on_rf():
    X = XC(cls={"R": "RF"})
    train = poly([(0, 0), (3, 0), (5, 2), (7, 0), (9, 2), (15, 2)], net="R")
    irregular = poly([(0, 0), (3, 0), (4, 1), (5, 1), (9, 5), (20, 5), (21, 6)], net="R")
    assert st(TM.c17_sites(mdl(train), X)) == ["open"] and st(TM.c17_sites(mdl(irregular), X)) == ["already"]


def test_c18_pair_bend_spacing():
    X = XC(**pairx(w=0.15))
    near = poly([(0, 0), (5, 0), (5.2, 0.2), (5.2, 0.2 + 6)], net="A", w=0.15)       # 0.28 mm between the bends (needs 0.75)
    far = poly([(0, 0), (5, 0), (8, 3), (8, 9)], net="A", w=0.15)
    close_to_end = poly([(0, 0), (0.1, 0), (3, 2.9), (8, 2.9)], net="A", w=0.15)       # first bend 0.1 mm from the run end (needs 0.225)
    assert set(st(TM.c18_sites(mdl(near), X))) == {"open"} and st(TM.c18_sites(mdl(far), X)) == ["already", "already"]
    assert "open" in st(TM.c18_sites(mdl(close_to_end), X))


def test_c19_esd_segment_corner_rounded():
    X = XC(esd={"U1"}, roles={"J1": {"connector"}}, cls={"N": "GEN"})
    pads = [pad("U1", "1", 0, 0, "N"), pad("J1", "1", 10, 5, "N")]
    chamfer = poly([(0, 0), (5, 0), (7, 2), (10, 5)], net="N")
    assert set(st(TM.c19_sites(mdl(chamfer, pads), X))) == {"open"}
    other = mdl(chamfer, [pad("U1", "1", 0, 0, "N"), pad("J1", "1", 10, 5, "M")])
    assert TM.c19_sites(other, X) == []


def test_c20_rf_right_angle_mitre_percent():
    X = XC(cls={"R": "RF"}, h={"F.Cu": 0.12})
    s = TM.c20_sites(mdl(poly([(0, 0), (5, 0), (5, 5)], net="R", w=0.18)), X)
    assert st(s) == ["open"] and abs(s[0]["mitre_pct"] - (52 + 65 * math.exp(-1.35 * 0.18 / 0.12))) < 1e-9
    s = TM.c20_sites(mdl(poly([(0, 0), (5, 0), (5, 5)], net="R", w=0.03)), X)
    assert s[0]["reason"] == "OUT_OF_RANGE"


# ---- P rules -------------------------------------------------------------------------------------------------------------

def _entry(y_off=0.0, w=0.15):
    return mdl([seg((0.0, y_off), (3.0, y_off), net="N", w=w)], [pad("U1", "1", 0, 0, "N", 0.5, 0.4), pad("U1", "2", 3, 0, "N")])


def test_p1_pad_exit_on_axis():
    X = XC(cls={"N": "GEN"})
    assert "already" in st(TM.p1_sites(_entry(), X))
    assert st([s for s in TM.p1_sites(_entry(0.1), X) if s["x"] == 0]) == ["open"]


def test_p2_bend_near_pad_edge():
    X = XC(cls={"N": "GEN"})
    pads = [pad("U1", "1", 0, 0, "N", 0.5, 0.4)]
    near = mdl(poly([(0, 0), (0.3, 0), (3, 2.7)], net="N", w=0.15), pads)          # bend 0.05 mm outside the pad edge (needs 0.15)
    far = mdl(poly([(0, 0), (0.6, 0), (3.3, 2.7)], net="N", w=0.15), pads)
    assert st(TM.p2_sites(near, X)) == ["open"] and st(TM.p2_sites(far, X)) == ["already"]


def test_p3_trace_into_pad_corner():
    X = XC(cls={"N": "GEN"})
    pads = [pad("U1", "1", 0, 0, "N", 0.5, 0.4)]
    corner = mdl([seg((0.0, 0.17), (3.0, 0.17), net="N", w=0.15)], pads)           # trace edge flush past the pad side (0.2)
    centre = mdl([seg((0.0, 0.0), (3.0, 0.0), net="N", w=0.15)], pads)
    assert st(TM.p3_sites(corner, X)) == ["open"] and st(TM.p3_sites(centre, X)) == ["already"]


@pytest.mark.parametrize("dp,dn,state", [(1.0, 1.0, "already"), (1.0, 1.3, "open")])
def test_p4_pair_members_enter_symmetrically(dp, dn, state):
    X = XC(**pairx(w=0.15))
    pads = [pad("U1", "1", 0, 0, "A"), pad("U1", "2", 0, 1.0, "B")]
    segs = poly([(0, 0), (dp, 0), (dp + 3, 3)], net="A", w=0.15) + poly([(0, 1.0), (dn, 1.0), (dn + 2, 3)], net="B", w=0.15)
    s = TM.p4_sites(mdl(segs, pads), X)
    assert len(s) == 1 and s[0]["state"] == state


@pytest.mark.parametrize("pitch,state", [(2.0, "open"), (0.5, "already")])
def test_p5_neck_down_only_at_fine_pitch(pitch, state):
    X = XC(cls={"N": "GEN"})
    pads = [pad("U1", "1", 0, 0, "N", 0.3, 0.3), pad("U1", "2", pitch, 0, "M", 0.3, 0.3)]
    segs = [seg((0, 0), (0.0, 1.0), net="N", w=0.127), seg((0.0, 1.0), (0.0, 5.0), net="N", w=0.3)]
    s = TM.p5_sites(mdl(segs, pads), X)
    assert len(s) == 1 and s[0]["state"] == state


def test_p6_width_step_distance_from_bend():
    X = XC()
    near = [seg((0, 0), (5, 0), w=0.3), seg((5, 0), (5.1, 0.0), w=0.15), seg((5.1, 0), (8, 3), w=0.15)]
    far = [seg((0, 0), (5, 0), w=0.3), seg((5, 0), (15, 0), w=0.15), seg((15, 0), (18, 3), w=0.15)]
    assert TM.width_changes(mdl(near)) and st(TM.p6_sites(mdl(far), X)) == ["already"]
    s = TM.p6_sites(mdl([seg((0, 0), (5, 0), w=0.3), seg((5, 0), (5.2, 0.2), w=0.15)]), X)
    assert st(s) == ["not_possible"] and s[0]["reason"] == "NEEDS_ROUTE" and s[0]["fix"]


def test_p7_rf_pad_step():
    X = XC(cls={"R": "RF"})
    big = mdl([seg((0, 0), (3, 0), net="R", w=0.18)], [pad("U1", "1", 0, 0, "R", 0.5, 0.5)])
    match = mdl([seg((0, 0), (3, 0), net="R", w=0.18)], [pad("U1", "1", 0, 0, "R", 0.5, 0.19)])
    assert st(TM.p7_sites(big, X)) == ["not_possible"] and st(TM.p7_sites(match, X)) == ["already"]


def _via_track(w, td=False, net="N"):
    return mdl([seg((0, 0), (3, 0), net=net, w=w)], vias=[via(net, 0, 0, td=td)])


def test_p8_teardrop_on_via():
    X = XC(cls={"N": "GEN"})
    assert st(TM.p8_sites(_via_track(0.15), X)) == ["open"]
    assert st(TM.p8_sites(_via_track(0.15, td=True), X)) == ["already"]
    assert TM.p8_sites(_via_track(0.6), X) == []                                 # track >= 0.508: not eligible
    zone = {"net": "N", "layers": {"F.Cu"}, "pts": [(-5, -5), (5, -5), (5, 5), (-5, 5)], "keepout": False}
    z = _via_track(0.15)
    z["zones"] = [zone]
    assert st(TM.p8_sites(z, X)) == ["waived"]
    assert TM.p8_sites(_via_track(0.15), XC(cls={"N": "RF"})) == []              # RF: P9 says none


def test_p9_rf_via_has_no_teardrop():
    X = XC(cls={"R": "RF"})
    assert st(TM.p9_sites(mdl(vias=[via("R", 0, 0, td=True), via("R", 5, 0)]), X)) == ["already", "open"]


def test_p10_pair_vias_by_class():
    h = XC(**pairx(cls="HSF"))
    assert st(TM.p10_sites(mdl(vias=[via("A", 0, 0, td=True), via("B", 1, 0)]), h)) == ["already", "open"]
    s = XC(**pairx(cls="HSS"))
    assert st(TM.p10_sites(mdl(vias=[via("A", 0, 0, td=True), via("B", 1, 0, td=True)]), s)) == ["already", "already"]
    assert st(TM.p10_sites(mdl(vias=[via("A", 0, 0, td=True), via("B", 1, 0)]), s)) == ["open", "open"]


def test_p12_bend_near_via_and_entry_offset():
    X = XC(cls={"N": "GEN"})
    near = mdl(poly([(0, 0), (0.3, 0), (3, 2.7)], net="N", w=0.15), vias=[via("N", 0, 0)])          # 0.07 mm from the via edge
    far = mdl(poly([(0, 0), (0.6, 0), (3.3, 2.7)], net="N", w=0.15), vias=[via("N", 0, 0)])
    assert st(TM.p12_sites(near, X)) == ["open"] and st(TM.p12_sites(far, X)) == ["already"]
    off = mdl([seg((0.1, 0), (3, 0), net="N", w=0.15)], vias=[via("N", 0, 0)])
    assert "open" in st(TM.p12_sites(off, X))


@pytest.mark.parametrize("n_end,state", [(4.0, "already"), (8.0, "not_possible")])
def test_p13_pair_breakout_length(n_end, state):
    X = XC(**pairx(w=0.15, gap=0.25))
    pads = [pad("U1", "1", 0, 0, "A"), pad("U1", "2", 0, 2.0, "B"), pad("J1", "1", 20, 0, "A"), pad("J1", "2", 20, 0.4, "B")]
    A = poly([(0, 0), (20, 0)], net="A", w=0.15)
    Bs = poly([(0, 2.0), (n_end, 2.0), (n_end + 1.6, 0.4), (20, 0.4)], net="B", w=0.15)
    s = [x for x in TM.p13_sites(mdl(A + Bs, pads), X) if abs(x["x"]) < 1]
    assert len(s) == 1 and s[0]["state"] == state


def test_p14_two_pin_part_entries():
    X = XC()
    pads = [pad("R1", "1", -0.8, 0, "N", 0.8, 0.9), pad("R1", "2", 0.8, 0, "M", 0.8, 0.9)]
    ok = mdl([seg((-0.8, 0), (-4, 0), net="N", w=0.2), seg((0.8, 0), (4, 0), net="M", w=0.2)], pads)
    bad = mdl([seg((-0.8, 0), (-4, 0), net="N", w=0.2), seg((0.8, 0), (4, 0), net="M", w=0.95)], pads)
    assert st(TM.p14_sites(ok, X)) == ["already"] and st(TM.p14_sites(bad, X)) == ["not_possible"]
    big = [pad("U9", "1", -3, 0, "N", 1, 1), pad("U9", "2", 3, 0, "M", 1, 1)]
    assert TM.p14_sites(mdl([seg((-3, 0), (-5, 0), net="N", w=0.2)], big), X) == []      # not 0603 or smaller


def test_p15_unused_via_pads_on_pair_vias():
    X = XC(**pairx())
    B = mdl(vias=[via("A", 0, 0), via("B", 1, 0, rm=True)], copper=CU6)
    assert st(TM.p15_sites(B, X)) == ["already", "open"]


# ---- M rules -------------------------------------------------------------------------------------------------------------

def test_m2_m4_zone_findings_from_drc():
    v = [{"type": "copper_sliver", "items": [{"description": "Zone", "pos": {"x": 1.0, "y": 2.0}}]}, {"type": "isolated_copper", "items": []}]
    s = TM.drc_sites(v, "copper_sliver")
    assert st(s) == ["not_possible"] and s[0]["reason"] == "NEEDS_ROUTE" and (s[0]["x"], s[0]["y"]) == (1.0, 2.0)
    assert len(TM.drc_sites(v, "isolated_copper")) == 1 and TM.drc_sites([], "copper_sliver") == []


def test_m11_mask_web():
    X = XC()
    B = mdl(pads=[pad("U1", "1", 0, 0, "A", 0.5, 0.5), pad("U1", "2", 0.6, 0, "B", 0.5, 0.5), pad("U1", "3", 2.0, 0, "C", 0.5, 0.5)])
    s = TM.m11_sites(B, dict(X, mask_exp=0.0))
    assert st(s) == ["not_possible"] and s[0]["web"] == pytest.approx(0.1)
    assert st(TM.m11_sites(B, dict(X, mask_exp=0.05))) == ["not_possible"] and TM.m11_sites(B, dict(X, mask_exp=0.05))[0]["web"] == pytest.approx(0.0)
    wide = mdl(pads=[pad("U1", "1", 0, 0, "A", 0.5, 0.5), pad("U1", "2", 0.9, 0, "B", 0.5, 0.5)])
    assert st(TM.m11_sites(wide, XC(mask_exp=0.0))) == ["already"]


# ---- S rules -------------------------------------------------------------------------------------------------------------

def test_s1_s2_dangling_ends_and_t():
    rf = XC(cls={"R": "RF", "G": "GEN"})
    pads = [pad("U1", "1", 0, 0, "R"), pad("U1", "2", 10, 0, "R"), pad("U2", "1", 0, 5, "G"), pad("U2", "2", 10, 5, "G")]
    segs = poly([(0, 0), (10, 0)], net="R") + [seg((5, 0), (5, 1.0), net="R")] + poly([(0, 5), (10, 5)], net="G") + [seg((6, 5), (6, 5.1), net="G")]
    B = mdl(segs, pads)
    assert [s["state"] for s in TM.s1_sites(B, rf)] == ["open", "open"]           # dangling 1 mm stub and the T it hangs on
    assert [s["state"] for s in TM.s2_sites(B, rf)] == ["open"]                    # 0.1 mm stub on a general net, on a T of its own
    clean = mdl(poly([(0, 0), (10, 0)], net="R"), pads)
    assert TM.s1_sites(clean, rf) == [] and TM.s2_sites(clean, rf) == []


def _fillboard(tmp_path, segs_txt, extra=""):
    zone = ('(zone (net 0) (net_name "") (layer "In1.Cu") (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20))) '
            '(filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20))))\n')
    return G.board(tmp_path, zone + segs_txt + extra)


def _segtxt(a, b, net, w=0.2, layer="F.Cu"):
    return f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) (layer "{layer}") (net "{net}"))\n'


@pytest.mark.parametrize("x0,state", [(10.0, "already"), (0.3, "not_possible")])
def test_s3_rf_copper_keeps_off_plane_edge(tmp_path, x0, state):
    B = routediag.load(_fillboard(tmp_path, _segtxt((x0, 5), (x0, 12), "R")))
    X = XC(cls={"R": "RF"}, h={"F.Cu": 0.12})
    s = TM.s3_sites(B, X)
    assert len(s) == 1 and s[0]["state"] == state and (state == "already" or s[0]["reason"] == "NEEDS_ROUTE")


@pytest.mark.parametrize("y0,state", [(None, None), (5.3, "not_possible"), (7.0, "already")])
def test_s13_pair_to_pour_distance(tmp_path, y0, state):
    pour = (f'(zone (net 1) (net_name "GND") (layer "F.Cu") (polygon (pts (xy 5 {y0}) (xy 15 {y0}) (xy 15 {y0 + 3}) (xy 5 {y0 + 3}))) '
            f'(filled_polygon (layer "F.Cu") (pts (xy 5 {y0}) (xy 15 {y0}) (xy 15 {y0 + 3}) (xy 5 {y0 + 3}))))\n') if y0 else ""
    B = routediag.load(_fillboard(tmp_path, _segtxt((6, 5), (14, 5), "A") + pour))
    X = XC(**pairx())
    s = TM.s13_sites(B, X)
    assert (s == []) if state is None else (len(s) == 1 and s[0]["state"] == state)       # no pour on the layer: no eligible site


def _gnd_zone():
    return {"net": "GND", "layers": {"In1.Cu"}, "pts": [(-9, -9), (9, -9), (9, 9), (-9, 9)], "keepout": False}


def test_s4_plane_change_via_needs_ground_via():
    X = XC(**pairx())
    ends = [seg((-3, 0), (0, 0), net="A", layer="F.Cu"), seg((0, 0), (3, 0), net="A", layer="B.Cu")]
    B = mdl(ends, vias=[via("A", 0, 0)], zones=[_gnd_zone()])
    s = TM.s4_sites(B, X)
    assert st(s) == ["not_possible"] and s[0]["reason"] == "NEEDS_ROUTE" and s[0]["fix"]
    B = mdl(ends, vias=[via("A", 0, 0), via("GND", 1.0, 0)], zones=[_gnd_zone()])
    assert st(TM.s4_sites(B, X)) == ["already"]
    same = mdl([seg((-3, 0), (0, 0), net="A", layer="F.Cu")], vias=[via("A", 0, 0)], zones=[_gnd_zone()])
    assert TM.s4_sites(same, X) == []                                          # F.Cu only: no plane change


def test_s5_rf_vias_per_net():
    one = mdl(vias=[via("R", 0, 0)])
    two = mdl(vias=[via("R", 0, 0), via("R", 5, 0)])
    X = XC(cls={"R": "RF"}, rf={"max_vias": 1})
    assert st(TM.s5_sites(one, X)) == ["already"] and st(TM.s5_sites(two, X)) == ["not_possible", "not_possible"]


def test_s6_pair_via_counts():
    X = XC(**pairx(max_vias=2))
    assert st(TM.s6_sites(mdl(vias=[via("A", 0, 0), via("B", 1, 0)]), X)) == ["already"]
    assert st(TM.s6_sites(mdl(vias=[via("A", 0, 0), via("A", 3, 0), via("B", 1, 0)]), X)) == ["not_possible"]
    assert st(TM.s6_sites(mdl(vias=[via("A", i, 0) for i in range(3)] + [via("B", i, 1) for i in range(3)]), X)) == ["not_possible"]


def test_s8_rf_via_fence_pitch():
    g = {"hu": None, "eu": None, "hd": 0.1195, "ed": 4.45, "t": 0.035, "model": "microstrip"}
    X = XC(cls={"R": "RF"}, f_ghz=2.48, geom={"F.Cu": g}, planes=["In1.Cu"])
    assert TM.lambda_sub(X) / 20 == pytest.approx(2.87, abs=0.01)
    seg_ = [seg((0, 0), (20, 0), net="R")]
    tight = mdl(seg_, vias=[via("GND", x, 1.0) for x in (2, 4, 6)], zones=[_gnd_zone()])
    wide = mdl(seg_, vias=[via("GND", x, 1.0) for x in (2, 10)], zones=[_gnd_zone()])
    assert set(st(TM.s8_sites(tight, X))) == {"already"}
    s = TM.s8_sites(wide, X)
    assert set(st(s)) == {"not_possible"} and s[0]["bad"] is True


def test_s9_spacing_to_parallel_copper():
    X = XC(**pairx(w=0.15))
    near = mdl(poly([(0, 0), (10, 0)], net="A", w=0.15) + poly([(0, 0.4), (10, 0.4)], net="C", w=0.15))
    far = mdl(poly([(0, 0), (10, 0)], net="A", w=0.15) + poly([(0, 2.0), (10, 2.0)], net="C", w=0.15))
    cross = mdl(poly([(0, 0), (10, 0)], net="A", w=0.15) + poly([(5, -0.3), (5, 4)], net="C", w=0.15))
    assert st(TM.s9_sites(near, X)) == ["not_possible"] and TM.s9_sites(far, X) == []
    assert TM.s9_sites(cross, X) == []                                          # not parallel


def test_s10_stub_rows(monkeypatch):
    from fluxplace import pairaudit as PA
    monkeypatch.setattr(PA, "via_stubs", lambda B, nets, min_mm=0.05: [
        {"net": "A", "x": 0, "y": 0, "used": ["F.Cu", "In2.Cu"], "stub_mm": 0.83, "dead": False},
        {"net": "A", "x": 1, "y": 0, "used": ["F.Cu", "B.Cu"], "stub_mm": 0.0, "dead": False}])
    s = TM.s10_sites(mdl(), XC(**pairx()))
    assert st(s) == ["already", "not_possible"] and [x["stub"] for x in s if x["state"] == "not_possible"] == [0.83]


def test_s11_pair_on_inner_layer():
    X = XC(**pairx())
    assert st(TM.s11_sites(mdl(poly([(0, 0), (5, 0)], net="A", layer="In2.Cu")), X)) == ["not_possible"]
    assert st(TM.s11_sites(mdl(poly([(0, 0), (5, 0)], net="A", layer="F.Cu")), X)) == ["already"]


def test_s12_in2_over_in3_overlap_listed():
    X = XC(**pairx())
    over = mdl(poly([(0, 0), (5, 0)], net="A", layer="In2.Cu") + poly([(0, 0.05), (5, 0.05)], net="C", layer="In3.Cu"))
    s = TM.s12_sites(over, X)
    assert any(x["state"] == "not_possible" and x["overlap_mm"] == pytest.approx(5.0) for x in s)
    apart = mdl(poly([(0, 0), (5, 0)], net="A", layer="In2.Cu") + poly([(0, 3), (5, 3)], net="C", layer="In3.Cu"))
    assert set(st(TM.s12_sites(apart, X))) == {"already"}


# ---- rotation and mirror (A6) -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("k,mirror", [(0, False), (1, False), (2, False), (3, False), (0, True), (1, True)])
def test_measure_counts_invariant_under_rotation_and_mirror(k, mirror):
    segs = (poly([(0, 0), (3, 0), (4, 1), (7, 1), (8, 2), (11, 2), (12, 3)], net="R") + poly([(20, 0), (25, 0), (25, 5)], net="R")
            + poly([(0, 20), (3, 20), (3, 21), (5, 21), (5, 20), (8, 20)], net="A", w=0.15) + poly([(0, 30), (5, 30), (5.2, 30.2), (5.2, 36)], net="B", w=0.15)
            + poly([(30, 0), (40, 0)], net="A", w=0.15) + poly([(30, 0.4), (40, 0.4)], net="C", w=0.15))
    X = XC(cls={"R": "RF", "A": "HSS", "B": "HSS"}, pairs=pairx(w=0.15)["pairs"], pair_of=pairx(w=0.15)["pair_of"], h={"F.Cu": 0.12})
    for fn in (TM.c7_sites, TM.c12_sites, TM.c17_sites, TM.c18_sites, TM.c20_sites, TM.s9_sites):
        a, b = fn(mdl(segs), X), fn(mdl(rot(segs, k, mirror)), X)
        assert st(a) == st(b), fn.__name__


# ---- HARD rows ---------------------------------------------------------------------------------------------------------------

def test_h3_annular_ring():
    X = XC(fab={"min_annular_mm": 0.13, "hard": {"pth_annular_min": 0.15}})
    B = mdl(vias=[via("N", 0, 0, d=0.46, drill=0.2), via("N", 1, 0, d=0.4, drill=0.3)], pads=[pad("J1", "1", 5, 5, "N", 1.0, 1.0, tht=True, drill=0.8)])
    r = TM.h3(B, X)
    assert r["base"] == 2 and r["limit"] == {"via": 0.13, "pth": 0.15}              # the 0.4 / 0.3 via and the PTH ring 0.1


def test_h6_not_measured_until_the_board_rule_is_set(tmp_path):
    body = G.two("R1", 10, 10, "A", "B")
    path = G.board(tmp_path, body)
    B = routediag.load(path)
    X = XC()
    assert "gap 16" in TM.h6(B, X, path)["not_measured"]
    (tmp_path / "b.kicad_pro").write_text(json.dumps({"board": {"design_settings": {"rules": {"solder_mask_min_width": 0.1016}}}}))
    r = TM.h6(B, X, path)
    assert "not_measured" not in r and r["base"] == 0 and r["limit"] == pytest.approx(0.1016)


def test_h7_impedance_window_rows():
    from fluxplace import impedance as IM
    g = {"hu": None, "eu": None, "hd": 0.1195, "ed": 4.45, "t": 0.035, "model": "microstrip", "layer": "F.Cu"}
    z = IM.z_diff(0.15, 0.25, g)
    B = mdl(poly([(0, 0), (10, 0)], net="A", w=0.15) + poly([(0, 0.4), (10, 0.4)], net="B", w=0.15))
    ok = XC(**pairx(w=0.15, gap=0.25, imp=z), geom={"F.Cu": g})
    r = TM.h7(B, ok)
    assert r["base"] == 0 and r["rows"][0]["z"] == pytest.approx(z)
    bad = XC(**pairx(w=0.15, gap=0.25, imp=z * 1.3), geom={"F.Cu": g})
    assert TM.h7(B, bad)["base"] == 1
    narrow = XC(**pairx(w=0.15, gap=0.25, imp=z, window=[z * 1.05, z * 1.2]), geom={"F.Cu": g})        # a datasheet window excluding z
    assert TM.h7(B, narrow)["base"] == 1


def test_h8_h9_from_audit():
    au = {("A", "B"): {"causes": ["skew"], "status": "mismatch", "stubs": []}, ("C", "D"): {"causes": [], "status": "complete", "stubs": [{"mm": 4.0}]},
          ("E", "F"): {"status": "unknown", "causes": []}}
    assert TM.h8(au)["base"] == 1 and TM.h8(au)["unmeasured"] == [["E", "F"]]
    cons = {"pairs": {"C": {"stub_max_mm": 3.5}}}
    r = TM.h9(au, cons)
    assert r["graded"] == 1 and r["base"] == 1 and r["over"][0]["stub_mm"] == 4.0


def test_facts_report_the_numbers_the_acceptance_names():
    X = XC(cls={"R": "RF"})
    B = mdl(poly([(0, 0), (1, 0), (1.2, 0.2)], net="R", w=0.18) + [seg((9, 9), (9, 9), net="R")], pads=[pad("J1", "1", 0, 0, "R", tht=True, drill=0.5)],
            vias=[via("GND", 0, 0)])
    f = TM.facts(B, X)
    assert f["vias"] == 1 and f["vias_ground"] == 1 and f["pads_pth"] == 1 and f["teardrops"] == 0
    assert f["short_leg_45_under_3w"]["RF"] == [1, 1] and f["degenerate"] == {"zero": 1, "duplicate": 0}



@pytest.mark.parametrize("k,mirror", [(k, m) for k in range(4) for m in (False, True)])
def test_pad_exit_vs_buried_segment(k, mirror):
    segs = rot([seg((0.0, 0.0), (0.1, 0.0), net="N", w=0.2), seg((0.0, 0.0), (3.0, 0.0), net="N", w=0.2)], k, mirror)
    m = mdl(segs=segs, pads=[pad("U1", "1", 0, 0, "N", 0.5, 0.5)])
    assert len(TM.pad_exits(m)) == 1
    assert len(TM.p1_sites(m, XC(cls={"N": "GEN"}))) == 1
    buried = [s for s in TM.m9_sites(m) if s.get("kind") == "buried"]
    assert len(buried) == 1 and buried[0]["state"] == "open"


def test_h3_ring_boundary():
    X = XC(fab={"min_annular_mm": 0.13, "hard": {"pth_annular_min": 0.15}})
    assert TM.h3(mdl(vias=[via("N", 0, 0, d=0.45, drill=0.2)]), X)["base"] == 1     # ring 0.125
    assert TM.h3(mdl(vias=[via("N", 0, 0, d=0.46, drill=0.2)]), X)["base"] == 0     # ring 0.13

def test_h7_half_ohm_inside_and_outside_window():
    from fluxplace import impedance as IM
    g = {"hu": None, "eu": None, "hd": 0.1195, "ed": 4.45, "t": 0.035, "model": "microstrip", "layer": "F.Cu"}
    z = IM.z_diff(0.15, 0.25, g)
    B = mdl(poly([(0, 0), (10, 0)], net="A", w=0.15) + poly([(0, 0.4), (10, 0.4)], net="B", w=0.15))
    assert TM.h7(B, XC(**pairx(w=0.15, gap=0.25, imp=z), geom={"F.Cu": g}))["base"] == 0
    assert TM.h7(B, XC(**pairx(w=0.15, gap=0.25, imp=(z - 0.5) / 1.1), geom={"F.Cu": g}))["base"] == 1   # measured 0.5 ohm above hi
    assert TM.h7(B, XC(**pairx(w=0.15, gap=0.25, imp=(z + 0.5) / 0.9), geom={"F.Cu": g}))["base"] == 1   # measured 0.5 ohm below lo

def test_s4_pair_ground_via_limit_and_rf_two_within_1mm():
    X = XC(**pairx())
    ends = [seg((-3, 0), (0, 0), net="A", layer="F.Cu"), seg((0, 0), (3, 0), net="A", layer="B.Cu")]
    assert st(TM.s4_sites(mdl(ends, vias=[via("A", 0, 0), via("GND", 1.2, 0)], zones=[_gnd_zone()]), X)) == ["already"]
    assert st(TM.s4_sites(mdl(ends, vias=[via("A", 0, 0), via("GND", 1.4, 0)], zones=[_gnd_zone()]), X)) == ["not_possible"]
    XR = XC(**pairx())
    XR["cls"] = {"A": "RF"}
    rf_one = mdl(ends, vias=[via("A", 0, 0), via("GND", 0.9, 0)], zones=[_gnd_zone()])
    assert st(TM.s4_sites(rf_one, XR)) == ["not_possible"]
    rf_two = mdl(ends, vias=[via("A", 0, 0), via("GND", 0.9, 0), via("GND", 0.0, 0.9)], zones=[_gnd_zone()])
    assert st(TM.s4_sites(rf_two, XR)) == ["already"]



# ---- M4: H7 rows detail, gap by length, break-out / neck exemption --------------------------------------------------

def _h7_pair_board(len_b, gap_b):
    """30 mm in-window pair run on F.Cu (gap 0.25) and a len_b mm section on B.Cu at centre distance gap_b + 0.15."""
    pts = [(0, 0), (30, 0)]
    f = poly(pts, net="A", w=0.15) + poly([(0, 0.4), (30, 0.4)], net="B", w=0.15)
    b = (poly([(0, 5), (len_b, 5)], net="A", layer="B.Cu", w=0.15) +
         poly([(0, 5 + gap_b + 0.15), (len_b, 5 + gap_b + 0.15)], net="B", layer="B.Cu", w=0.15))
    return mdl(f + b)


def _h7_x(**kw):
    from fluxplace import impedance as IM
    g = {"hu": None, "eu": None, "hd": 0.1195, "ed": 4.45, "t": 0.035, "model": "microstrip", "layer": "F.Cu"}
    gb = dict(g, layer="B.Cu")
    z = IM.z_diff(0.15, 0.25, g)
    X = XC(**pairx(w=0.15, gap=None, imp=z, window=[z * 0.95, z * 1.02]), geom={"F.Cu": g, "B.Cu": gb}, **kw)
    X["cu"] = ["F.Cu", "B.Cu"]
    X["cons"] = {}
    return X


@pytest.mark.parametrize("mm,base,exempt", [(2.0, 0, 1), (10.0, 1, 0)])
def test_h7_pair_breakout_section_exempt_when_short(mm, base, exempt):
    r = TM.h7(_h7_pair_board(mm, 0.4), _h7_x())
    assert r["base"] == base and sum(1 for x in r["rows"] if x.get("exempt") == "breakout") == exempt
    row = next(x for x in r["rows"] if x["layer"] == "B.Cu")
    assert {"net", "layer", "z", "lo", "hi", "w", "gap", "len_mm", "ok"} <= set(row) and abs(row["len_mm"] - mm) < 1e-6


@pytest.mark.parametrize("mm,base,exempt", [(1.4, 0, 1), (3.0, 1, 0)])
def test_h7_rf_neck_exempt_when_short(mm, base, exempt):
    from fluxplace import impedance as IM
    g = {"hu": None, "eu": None, "hd": 0.1195, "ed": 4.45, "t": 0.035, "model": "microstrip", "layer": "F.Cu"}
    z = IM.z_single(0.15, g)                                   # target z; the thin neck is off
    X = XC(geom={"F.Cu": g}, rf={"target_z": z, "tolerance_pct": 10, "neck_max_mm": 1.5}, cls={"R": "RF"})
    r = TM.h7(mdl(poly([(0, 0), (mm, 0)], net="R", w=0.05)), X)
    assert r["base"] == base and sum(1 for x in r["rows"] if x.get("exempt") == "neck") == exempt


def test_gap_of_most_frequent_gap_by_length_not_count():
    # 1 long run at centre distance 0.4 (gap 0.25), 5 short pieces at 0.5 (gap 0.35): by count 0.5 wins, by length 0.4
    long_ = poly([(0, 0), (20, 0)], net="A", w=0.15) + poly([(0, 0.4), (20, 0.4)], net="B", w=0.15)
    short = []
    for i in range(5):
        x0 = 30 + i * 3
        short += poly([(x0, 0), (x0 + 0.8, 0)], net="A", w=0.15) + poly([(x0, 0.5), (x0 + 0.8, 0.5)], net="B", w=0.15)
    X = XC(**pairx(w=0.15, gap=None))
    assert TM._gap_of(mdl(long_ + short), X, ("A", "B"), "F.Cu") == pytest.approx(0.25, abs=0.011)


def test_hard_row_keeps_detail():
    from fluxplace import tracefinish as TF
    r = TF._hard_row("H7", {"base": 0, "rows": [{"net": "A"}], "extra": 1}, "DATASHEET")
    assert r["rows"] == [{"net": "A"}] and "extra" not in r
    r8 = TF._hard_row("H8", {"base": 1, "pairs": [["A", "B"]], "unmeasured": []}, "STD")
    assert r8["pairs"] == [["A", "B"]] and r8["unmeasured"] == []
    r9 = TF._hard_row("H9", {"base": 0, "graded": 2, "over": []}, "STD")
    assert r9["graded"] == 2 and r9["over"] == []
    r3 = TF._hard_row("H3", {"base": 1, "limit": {"via": 0.13}, "sites": [{"x": 1}]}, "FAB")
    assert r3["sites"] == [{"x": 1}]
