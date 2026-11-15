"""Plane-net connections: a floating GND fragment (track, no via into the inner-plane zone) is tied by one via, the zones are
refilled and the board is saved; a net with no zone is reported with its reason (planetie.py)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbnew
from fluxplace import planetie as PT


def make(path):
    b = pcbnew.BOARD()
    b.SetCopperLayerCount(4)
    ed = b.GetLayerID("Edge.Cuts")
    for x0, y0, x1, y1 in ((0, 0, 40, 0), (40, 0, 40, 30), (40, 30, 0, 30), (0, 30, 0, 0)):
        s = pcbnew.PCB_SHAPE(b)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetLayer(ed)
        s.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x0), pcbnew.FromMM(y0)))
        s.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
        b.Add(s)
    gnd = pcbnew.NETINFO_ITEM(b, "GND")
    b.Add(gnd)
    fp = pcbnew.FOOTPRINT(b)
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(10)))
    pad = pcbnew.PAD(fp)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(1), pcbnew.FromMM(1)))
    pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(10)))
    pad.SetNet(gnd)
    pad.SetNumber("1")
    fp.Add(pad)
    b.Add(fp)
    tr = pcbnew.PCB_TRACK(b)
    tr.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(10)))
    tr.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(12), pcbnew.FromMM(10)))
    tr.SetWidth(pcbnew.FromMM(0.2))
    tr.SetLayer(b.GetLayerID("F.Cu"))
    tr.SetNet(gnd)
    b.Add(tr)
    z = pcbnew.ZONE(b)
    z.SetNet(gnd)
    z.SetLayer(b.GetLayerID("In1.Cu"))
    z.SetIsRuleArea(False)
    z.Outline().NewOutline()
    for x, y in ((1, 1), (39, 1), (39, 29), (1, 29)):
        z.Outline().Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    b.Add(z)
    b.Save(path)
    return path


def vias(path, net="GND"):
    b = pcbnew.LoadBoard(path)
    return sum(1 for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T and t.GetNetname() == net)


def judge(path):
    """test judge: the net is open while it has no via (the real count is batchroute's connectivity)."""
    return {"GND": 0 if vias(path) else 1}


def test_floating_plane_fragment_is_tied_by_a_via(tmp_path):
    src = make(str(tmp_path / "in.kicad_pcb"))
    assert judge(src) == {"GND": 1}
    out = str(tmp_path / "out.kicad_pcb")
    rec = PT.run(src, out, ["GND"], judge)
    assert rec["per_net"]["GND"]["vias"] >= 1 and rec["per_net"]["GND"]["open_after"] == 0
    assert vias(out) >= 1 and rec["placed"] >= 1


def test_plane_net_without_a_zone_reports_why(tmp_path):
    src = make(str(tmp_path / "in2.kicad_pcb"))
    rec = PT.run(src, str(tmp_path / "out2.kicad_pcb"), ["NOPE"], lambda p: {"NOPE": 2})
    assert rec["per_net"]["NOPE"]["reason"].startswith("no zone")      # open plane net with no pour: reported, not silent
    rec2 = PT.run(src, str(tmp_path / "out3.kicad_pcb"), ["GND"], lambda p: {"GND": 1})
    assert rec2["per_net"]["GND"]["layer"] == "In1.Cu"
