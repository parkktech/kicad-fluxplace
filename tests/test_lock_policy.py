"""PHASE3I B5 / F6: lock classes. hard = gated pair legs, RF and controlled nets, soft = low-speed pair legs and audio, free = the rest."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import lockpolicy as LP

NETS = {"USB_DP", "USB_DM", "MIC_P", "MIC_N", "LED_1", "RF_OUT", "VBAT"}
CONS = {"rf": {"nets": ["RF_OUT"]}}


def test_levels_by_class():
    assert LP.level("USB_DP", CONS, NETS) == LP.HARD          # high-speed family pair leg
    assert LP.level("USB_DM", CONS, NETS) == LP.HARD          # partner found by naming
    assert LP.level("RF_OUT", CONS, NETS) == LP.HARD          # RF net
    assert LP.level("MIC_P", CONS, NETS) == LP.SOFT           # low-speed pair leg
    assert LP.level("LED_1", CONS, NETS) == LP.FREE
    assert LP.level("VBAT", CONS, NETS) == LP.FREE


def test_audio_single_net_is_soft_and_unpaired_pair_leg_is_free():
    CONS0 = {"route": {}}                                       # non-empty constraints: empty ones fail closed (F6)
    assert LP.level("MIC_AUDIO_OUT", CONS0, {"MIC_AUDIO_OUT"}) == LP.SOFT
    assert LP.level("USB_DP", CONS0, {"USB_DP"}) == LP.FREE      # no partner on the board: not a pair leg
    assert LP.level("MIC_AUDIO_OUT", {}, {"MIC_AUDIO_OUT"}) == LP.HARD   # no constraints: hard (fail closed)


def test_hard_locked_filters_to_hard():
    assert LP.hard_locked(["USB_DP", "MIC_P", "LED_1", "RF_OUT"], CONS, NETS) == ["RF_OUT", "USB_DP"]


def test_f6_hard_set_is_complete():
    assert LP.level("RF_GNSS", {"route": {}}, {"RF_GNSS"}) == LP.HARD            # is_rf by name, no [rf] table
    assert LP.level("CTRL1", {"route": {"controlled": ["CTRL1"]}}, {"CTRL1", "X"}) == LP.HARD   # [route] controlled
    assert LP.level("XHS_P", {"pairs": {"XHS_": {}}}, {"XHS_P", "XHS_N"}) == LP.HARD           # [pairs.X] family leg
    assert LP.level("XHS_N", {"pairs": {"XHS_": {}}}, {"XHS_P", "XHS_N"}) == LP.HARD
    assert LP.level("USB_DP", {"pairs": {"USB_": {}}, "not_pairs": ["USB_DP", "USB_DM"]},
                    {"USB_DP", "USB_DM"}) == LP.FREE                                     # not_pairs: plain signal
    assert LP.level("LED_N", {"route": {}}, {"LED_P", "LED_N"}) == LP.SOFT               # low-speed pair leg


# ---- PHASE3I F6: soft-locked copper is ripped only in a session and committed only with its re-lay (real boards) ----
import pytest  # noqa: E402

pytest.importorskip("pcbnew")
sys.path.insert(0, os.path.dirname(__file__))
from test_handroute import Brd, V, mm  # noqa: E402
from fluxplace import handroute as H  # noqa: E402

pcbnew = pytest.importorskip("pcbnew")

LED_LAY = "F.Cu: 4,-1 16,-1"
WIN = (8, -2, 2, 2)                                        # covers the LED_P copper at x = 8..10 only


def _led_board(d, cons_path):
    os.makedirs(d, exist_ok=True)
    B = Brd()
    B.edge(-2, -8, 22, 8)
    B.pad("L1", "LED_P", 4, -1)
    B.pad("L2", "LED_P", 16, -1)
    B.pad("M1", "LED_N", 4, 1)
    B.pad("M2", "LED_N", 16, 1)
    B.track("LED_P", [(4, -1), (16, -1)], locked=True)
    B.track("LED_N", [(4, 1), (16, 1)], locked=True)
    p = B.save(d)
    with open(cons_path, "w") as fh:
        fh.write("[route]\n")                              # constraints: LED pair legs are low-speed (soft)
    return p


def test_soft_rip_outside_a_session_is_refused(tmp_path):
    cons = str(tmp_path / "cons.toml")
    p = _led_board(str(tmp_path / "b"), cons)
    r = H.shove(p, "LED_P", out=str(tmp_path / "o.kicad_pcb"), window_box=WIN, constraints=cons)
    assert not r["accepted"] and "only in a session" in r["reason"]
    assert not os.path.exists(str(tmp_path / "o.kicad_pcb"))


def test_soft_rip_without_relay_is_refused_at_commit(tmp_path):
    cons = str(tmp_path / "cons.toml")
    p = _led_board(str(tmp_path / "b"), cons)
    work = str(tmp_path / "w.kicad_pcb")
    H.session_begin(p, work)
    assert H.shove(work, "LED_P", window_box=WIN, constraints=cons)["accepted"]
    c = H.session_commit(work, str(tmp_path / "out.kicad_pcb"), mode="edit")
    assert not c["accepted"] and any("ripped and not re-laid" in x for x in c["reasons"])


def test_soft_rip_relaid_and_locked_commits(tmp_path):
    cons = str(tmp_path / "cons.toml")
    p = _led_board(str(tmp_path / "b"), cons)
    work = str(tmp_path / "w.kicad_pcb")
    H.session_begin(p, work)
    assert H.shove(work, "LED_P", window_box=WIN, constraints=cons)["accepted"]
    assert H.lay(work, "LED_P", LED_LAY, width=0.25, lock=True, constraints=cons)["accepted"]
    c = H.session_commit(work, str(tmp_path / "out.kicad_pcb"), mode="edit")
    assert c["accepted"], c["reasons"]
    assert os.path.exists(str(tmp_path / "out.kicad_pcb"))


def _fine_ic_board(d):
    """U1: six pads at 0.5 mm pitch (three per side, x = 18 and 22), free lanes; R8/R9 carry net N2 (the lay's ends)."""
    os.makedirs(d, exist_ok=True)
    B = Brd()
    B.edge(0, 5, 40, 35)
    fp = pcbnew.FOOTPRINT(B.b)
    fp.SetReference("U1")
    fp.SetPosition(V(20, 20))
    for i, net in enumerate(["P1", "P2", "P3", "P4", "P5", "P6"]):
        side, k = (-2, i) if i < 3 else (2, i - 3)
        pd = pcbnew.PAD(fp)
        pd.SetNumber(str(i + 1))
        pd.SetShape(pcbnew.PAD_SHAPE_RECT)
        pd.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        ls = pcbnew.LSET()
        ls.AddLayer(pcbnew.F_Cu)
        pd.SetLayerSet(ls)
        pd.SetSize(pcbnew.VECTOR2I(mm(0.8), mm(0.3)))
        pd.SetPosition(V(20 + side, 20 + (k - 1) * 0.5))
        pd.SetNet(B.net(net))
        fp.Add(pd)
    B.b.Add(fp)
    B.pad("R8", "N2", 17.2, 14, 0.5, 0.5)
    B.pad("R9", "N2", 17.2, 25, 0.5, 0.5)
    return B.save(d)


def test_locked_lay_across_the_only_escape_of_a_neighbour_pad_is_refused(tmp_path):
    """a fine-pitch IC pad whose outward lane is free; a locked lay crossing that lane blocks it: refused."""
    from fluxplace import review as RV
    cons = str(tmp_path / "cons.toml")
    with open(cons, "w") as fh:
        fh.write("[route]\n")
    p = _fine_ic_board(str(tmp_path / "b"))
    assert not any(x.startswith("U1.") for x in RV.pin_escape_blocked(p))      # the lane is free before the lay
    out = str(tmp_path / "lk.kicad_pcb")
    r = H.lay(p, "N2", "F.Cu: 17.2,14 17.2,25", out=out, width=0.2, lock=True, constraints=cons)
    assert not r["accepted"] and "lock blocks the last escape of" in r["reason"] and "U1." in r["reason"]
    assert not os.path.exists(out)
