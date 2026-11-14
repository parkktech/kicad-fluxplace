"""Length matching: [match.*] config, exact bump geometry, review codes, and tune --match on a pcbnew-built board
(8 parallel nets of unequal length in free space, plus one net with no room)."""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import lengthmatch as LM
from fluxplace import tune as TU
from fluxplace import review as RV

NAMES = [f"DATA_{i}" for i in range(8)]
LENS = [40.0 + 3.0 * i for i in range(8)]          # mm, unequal: 40 .. 61
PITCH_Y = 2.0
SHORT = "STUB"                                      # 3 mm net that cannot be lengthened to 20 mm


# ----------------------------------------------------------------- config
def test_groups_parse_and_validate():
    g = LM.groups({"match": {"A": {"nets": ["DATA_\\d+"], "tolerance_mm": 0.2}}})[0]
    assert g["unit"] == "mm" and g["style"] == "accordion" and g["target"] is None and g["reference"] is None
    assert LM.groups({"match": {"B": {"nets": ["X"], "reference": "CLK", "tolerance_ps": 2.0}}})[0]["unit"] == "ps"
    for bad in ({"nets": ["X"]},                                              # no tolerance
                {"nets": ["X"], "tolerance_mm": 1, "tolerance_ps": 1},        # two tolerances
                {"nets": ["X"], "target_mm": 1, "reference": "Y", "tolerance_mm": 1},
                {"nets": ["X"], "target_mm": 1, "tolerance_ps": 1},           # unit mismatch
                {"nets": ["X"], "tolerance_mm": 1, "style": "trombone"},      # not implemented
                {"nets": ["X"], "tolerance_mm": 1, "style": "sawtooth"},
                {"nets": [], "tolerance_mm": 1}):
        with pytest.raises(ValueError):
            LM.groups({"match": {"BAD": bad}})


def test_members_fullmatch():
    g = LM.groups({"match": {"A": {"nets": ["DATA_\\d"], "tolerance_mm": 1}}})[0]
    assert LM.members(g, ["DATA_1", "DATA_12", "XDATA_1", "DATA_9"]) == ["DATA_1", "DATA_9"]


# ------------------------------------------------- exact bump geometry (hand-checked)
def _poly_len(pts):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))


def test_accordion_adds_exactly_2a_per_bump():
    # L = 30 mm, need 3.0 mm, amp_max 0.6 -> n = ceil(3.0/1.2) = 3 bumps, a = 3.0/6 = 0.5 mm; period 0.8 mm (bump 0.4)
    n, a = TU.bump_plan(30.0, 3.0, 0.6, 0.8, 0.6)
    assert n == 3 and a == pytest.approx(0.5)
    pts = TU._bump_polyline(30.0, n, a, 0.8)
    assert _poly_len(pts) - 30.0 == pytest.approx(3.0)       # each bump: two legs of 0.5 mm
    assert pts[0] == (0.0, 0.0) and pts[-1] == (30.0, 0.0)  # same end points: a drop-in replacement
    # no leg reverses: every consecutive triple turns by 90 degrees or less (no acute tip)
    for p0, p1, p2 in zip(pts, pts[1:], pts[2:]):
        v1 = (p1[0] - p0[0], p1[1] - p0[1]); v2 = (p2[0] - p1[0], p2[1] - p1[1])
        cosang = (v1[0] * v2[0] + v1[1] * v2[1]) / (math.hypot(*v1) * math.hypot(*v2))
        assert cosang >= -1e-9


def test_no_room_returns_none():
    assert TU.bump_plan(3.0, 20.0, 0.6, 0.8, 0.6) is None   # 3 mm segment cannot take 20 mm of bumps


# ------------------------------------------------- pcbnew-built boards
def _stackup():
    return [{"type": "copper", "name": "F.Cu", "thickness": 0.035},
            {"type": "dielectric", "name": "", "thickness": 0.2, "epsilon_r": 4.5},
            {"type": "copper", "name": "B.Cu", "thickness": 0.035}]


def build_board(path, with_short=False):
    import pcbnew
    b = pcbnew.BOARD()
    nets = {}
    for n in NAMES + ([SHORT] if with_short else []):
        ni = pcbnew.NETINFO_ITEM(b, n)
        b.Add(ni)
        nets[n] = ni
    rows = list(zip(NAMES, LENS)) + ([(SHORT, 3.0)] if with_short else [])
    for i, (n, L) in enumerate(rows):
        y = 10.0 + i * PITCH_Y
        for x in (10.0, 10.0 + L):
            fp = pcbnew.FOOTPRINT(b)
            fp.SetReference(f"P{n}{int(x)}")
            fp.Reference().SetVisible(False)
            fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
            pad = pcbnew.PAD(fp)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.6), pcbnew.FromMM(0.6)))
            pad.SetNumber("1")
            pad.SetNet(nets[n])
            fp.Add(pad)
            b.Add(fp)
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(10.0), pcbnew.FromMM(y)))
        t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(10.0 + L), pcbnew.FromMM(y)))
        t.SetLayer(pcbnew.F_Cu)
        t.SetWidth(pcbnew.FromMM(0.2))
        t.SetNet(nets[n])
        b.Add(t)
    # outline so kicad-cli DRC sees a board
    s = pcbnew.PCB_SHAPE(b)
    s.SetShape(pcbnew.SHAPE_T_RECT)
    s.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(0), pcbnew.FromMM(0)))
    s.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(100), pcbnew.FromMM(60)))
    s.SetLayer(pcbnew.Edge_Cuts)
    b.Add(s)
    b.Save(str(path))
    return str(path)


def _lens(path, nets):
    import pcbnew
    return LM.lengths_from_board(pcbnew.LoadBoard(path), nets, _stackup(), [], False)


def test_tune_match_equalises_eight_nets_and_keeps_drc(tmp_path):
    pytest.importorskip("pcbnew")
    src = build_board(tmp_path / "in.kicad_pcb")
    g = LM.groups({"match": {"DATA": {"nets": ["DATA_\\d+"], "tolerance_mm": 0.2, "amplitude_mm": 0.6}}})[0]
    before = TU.drc_counts(src)
    lens0 = _lens(src, NAMES)
    tgt = max(v["mm"] for v in lens0.values())
    out = str(tmp_path / "out.kicad_pcb")
    rep = TU.tune_match(src, out, [g], lambda b, gg: LM.lengths_from_board(b, LM.members(gg, NAMES), _stackup(), [], False),
                        {"DATA": (tgt, 0.2)}, log=lambda m: None)
    assert all(r["status"] == "ok" for r in rep), rep
    lens1 = _lens(out, NAMES)
    for n in NAMES:
        assert abs(lens1[n]["mm"] - tgt) <= 0.2 + 1e-6, (n, lens1[n]["mm"], tgt)
    after = TU.drc_counts(out)
    assert after[0] <= before[0] and after[1] <= before[1]      # DRC delta: none rose
    assert after[1] == before[1]                                 # connectivity identical


def test_tune_match_no_room_reports_and_leaves_net_alone(tmp_path):
    pytest.importorskip("pcbnew")
    src = build_board(tmp_path / "in.kicad_pcb", with_short=True)
    g = LM.groups({"match": {"S": {"nets": [SHORT], "target_mm": 20.0, "tolerance_mm": 0.2}}})[0]
    out = str(tmp_path / "out.kicad_pcb")
    rep = TU.tune_match(src, out, [g], lambda b, gg: LM.lengths_from_board(b, [SHORT], _stackup(), [], False),
                        {"S": (20.0, 0.2)}, log=lambda m: None)
    assert rep[0]["status"] == "fail" and "no room" in rep[0]["note"]
    assert _lens(out, [SHORT])[SHORT]["mm"] == pytest.approx(3.0, abs=0.01)


# ------------------------------------------------- review codes
def _facts(path, nets):
    return {"board_path": path, "net_pads": {n: [] for n in nets}, "stackup": _stackup(), "plane_layers": []}


def test_review_codes_positive_and_negative(tmp_path, monkeypatch):
    pytest.importorskip("pcbnew")
    from fluxplace import routeconn as RC
    src = build_board(tmp_path / "in.kicad_pcb")
    monkeypatch.setattr(RC, "open_by_net", lambda p, **k: {})
    cons = {"match": {"DATA": {"nets": ["DATA_\\d+"], "tolerance_mm": 0.2}}}
    fails = [f for f in RV.check_match_groups(_facts(src, NAMES), cons) if f["code"] == "MATCH_GROUP_SKEW"]
    assert len(fails) == 1 and fails[0]["level"] == "FAIL"
    assert "DATA_0 40.00 mm" in fails[0]["msg"] and "DATA_7 61.00 mm" in fails[0]["msg"]
    # negative: a group whose members are equal (2 nets of the same length) passes
    ok_cons = {"match": {"EQ": {"nets": ["DATA_0"], "target_mm": 40.0, "tolerance_mm": 0.2}}}
    assert not RV.check_match_groups(_facts(src, NAMES), ok_cons)
    # MATCH_NO_NETS
    none = RV.check_match_groups(_facts(src, NAMES), {"match": {"X": {"nets": ["NOPE"], "tolerance_mm": 1}}})
    assert [f["code"] for f in none] == ["MATCH_NO_NETS"] and none[0]["level"] == "WARN"
    # MATCH_UNROUTED: the group is not evaluated
    monkeypatch.setattr(RC, "open_by_net", lambda p, **k: {"DATA_3": 2})
    un = RV.check_match_groups(_facts(src, NAMES), cons)
    assert [f["code"] for f in un] == ["MATCH_UNROUTED"] and un[0]["level"] == "WARN"


# ------------------------------------------------- pad-to-pad basis, ps branch, default spacing
def _stack_planes():
    from test_timing import STACK, PLANES
    return STACK, PLANES


def test_pad_to_pad_excludes_stub_and_total_is_marked(tmp_path):
    pytest.importorskip("pcbnew")
    import pcbnew
    src = build_board(tmp_path / "in.kicad_pcb")
    b = pcbnew.LoadBoard(src)
    # a 3 mm stub hanging off the middle of DATA_0's 40 mm row (a T-junction, not on the pad-to-pad path)
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(20.0), pcbnew.FromMM(10.0)))
    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(20.0), pcbnew.FromMM(13.0)))
    t.SetLayer(pcbnew.F_Cu)
    t.SetWidth(pcbnew.FromMM(0.2))
    t.SetNet(b.FindNet("DATA_0"))
    b.Add(t)
    stub = str(tmp_path / "stub.kicad_pcb")
    b.Save(stub)
    l = _lens(stub, ["DATA_0"])["DATA_0"]
    assert l["basis"] == "pad-to-pad"
    assert l["mm"] == pytest.approx(40.0, abs=0.01)           # total copper would be 43.0
    # the same net, total copper: the report says so
    m = _lens(build_board(tmp_path / "plain.kicad_pcb"), ["DATA_7"])["DATA_7"]
    assert m["basis"] == "pad-to-pad" and m["mm"] == pytest.approx(61.0, abs=0.01)


def test_multidrop_net_is_total_and_review_says_total(tmp_path, monkeypatch):
    pytest.importorskip("pcbnew")
    import test_timing as TT
    from fluxplace import routeconn as RC
    import pcbnew
    path = TT.build(tmp_path / "md.kicad_pcb", {"CK": [("U1", 10, 10), ("D1", 30, 10), ("D2", 50, 10)]})
    b = pcbnew.LoadBoard(path)
    l = LM.lengths_from_board(b, ["CK"], TT.STACK, TT.PLANES)["CK"]
    assert l["basis"] == "total" and l["mm"] == pytest.approx(40.0, abs=0.01)
    monkeypatch.setattr(RC, "open_by_net", lambda p, **k: {})
    cons = {"match": {"CK": {"nets": ["CK"], "target_mm": 30.0, "tolerance_mm": 0.5}}}
    f = RV.check_match_groups({"board_path": path, "net_pads": {"CK": []}, "stackup": TT.STACK,
                               "plane_layers": TT.PLANES}, cons)
    assert len(f) == 1 and f[0]["code"] == "MATCH_GROUP_SKEW" and "CK 40.00 mm total" in f[0]["msg"]


def test_tune_match_ps_target_branch(tmp_path):
    pytest.importorskip("pcbnew")
    from fluxplace import impedance as IM
    STACK, PLANES = _stack_planes()
    src = build_board(tmp_path / "in.kicad_pcb")
    names = ["DATA_5", "DATA_6", "DATA_7"]                  # 55, 58, 61 mm
    k = IM.route_delay_ps_per_mm(STACK, set(PLANES), {"F.Cu": 1.0})
    f = lambda b, gg: LM.lengths_from_board(b, LM.members(gg, names), STACK, PLANES, False)
    g = LM.groups({"match": {"P": {"nets": ["DATA_[567]"], "tolerance_ps": 2.0}}})[0]
    tgt = max(v["ps"] for v in f(LM_board(src), g).values())
    assert tgt == pytest.approx(61.0 * k, rel=1e-6)           # hand value: longest member x k ps/mm
    out = str(tmp_path / "out.kicad_pcb")
    rep = TU.tune_match(src, out, [g], f, {"P": (tgt, 2.0)}, log=lambda m: None)
    assert [r["status"] for r in rep] == ["ok", "ok", "ok"], rep
    assert all(r["unit"] == "ps" for r in rep)
    after = f(LM_board(out), g)
    for n in names:
        assert abs(after[n]["ps"] - tgt) <= 2.0 + 1e-6, (n, after[n]["ps"], tgt)


def LM_board(path):
    import pcbnew
    return pcbnew.LoadBoard(path)


def test_default_spacing_is_three_times_max_width_or_clearance(tmp_path):
    pytest.importorskip("pcbnew")
    src = build_board(tmp_path / "in.kicad_pcb")
    g = LM.groups({"match": {"S": {"nets": ["DATA_0"], "tolerance_mm": 0.5}}})[0]
    f = lambda b, gg: LM.lengths_from_board(b, ["DATA_0"], _stackup(), [], False)
    out = str(tmp_path / "out.kicad_pcb")
    # width 0.2 mm, clearance 0.5 mm -> spacing 3 x 0.5 = 1.5 mm; target 42 mm (2 mm of bumps)
    rep = TU.tune_match(src, out, [g], f, {"S": (42.0, 0.5)}, clearance_of=lambda n: 0.5, log=lambda m: None)
    assert rep[0]["spacing_mm"] == pytest.approx(1.5) and rep[0]["status"] == "ok"
    # no clearance above the width: spacing 3 x 0.2 = 0.6 mm
    rep2 = TU.tune_match(src, str(tmp_path / "o2.kicad_pcb"), [g], f, {"S": (42.0, 0.5)},
                         clearance_of=lambda n: 0.1, log=lambda m: None)
    assert rep2[0]["spacing_mm"] == pytest.approx(0.6)


def _fcu_set():
    import pcbnew
    s = pcbnew.LSET()
    s.AddLayer(pcbnew.F_Cu)
    return s


def _arc_board(path):
    """Two pads on net SIG joined by a 10 mm straight then a 5 mm quarter-ish arc (true arc length)."""
    import pcbnew
    b = pcbnew.BOARD()
    b.SetCopperLayerCount(2)
    net = pcbnew.NETINFO_ITEM(b, "SIG")
    b.Add(net)
    r = 5.0 / (math.pi / 2)                       # radius for a 5 mm quarter arc
    pads = [(0.0, 0.0), (10.0 + r, r)]
    for i, (x, y) in enumerate(pads):
        fp = pcbnew.FOOTPRINT(b)
        fp.SetReference(f"P{i}")
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
        pad = pcbnew.PAD(fp)
        pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.8), pcbnew.FromMM(0.8)))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(_fcu_set())
        pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
        pad.SetNumber("1")
        pad.SetNet(net)
        fp.Add(pad)
        b.Add(fp)
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(0.0), pcbnew.FromMM(0.0)))
    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(10.0), pcbnew.FromMM(0.0)))
    t.SetWidth(pcbnew.FromMM(0.2)); t.SetLayer(pcbnew.F_Cu); t.SetNet(net)
    b.Add(t)
    a = pcbnew.PCB_ARC(b)
    c = (10.0, r)                                  # arc centre; start at -90 deg, end at 0 deg
    pt = lambda ang: pcbnew.VECTOR2I(pcbnew.FromMM(c[0] + r * math.cos(math.radians(ang))),
                                     pcbnew.FromMM(c[1] + r * math.sin(math.radians(ang))))
    a.SetStart(pt(-90)); a.SetMid(pt(-45)); a.SetEnd(pt(0))
    a.SetWidth(pcbnew.FromMM(0.2)); a.SetLayer(pcbnew.F_Cu); a.SetNet(net)
    b.Add(a)
    b.Save(path)
    return path


def test_arc_track_counts_its_true_length_and_pad_to_pad_path_goes_through_it(tmp_path):
    pytest.importorskip("pcbnew")
    p = _arc_board(str(tmp_path / "arc.kicad_pcb"))
    l = _lens(p, ["SIG"])["SIG"]
    assert l["basis"] == "pad-to-pad", l          # the arc joins the two pads in the connectivity graph
    assert l["mm"] == pytest.approx(15.0, abs=0.01), l   # 10 mm straight + 5 mm arc
