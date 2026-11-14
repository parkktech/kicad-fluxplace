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
