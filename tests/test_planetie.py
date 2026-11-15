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


def test_via_at_points_refuses_a_hole_within_the_fab_spacing(tmp_path):
    """hole-to-hole, any net: a same-net via 0.3 mm away used to be accepted (same-net copper is not an obstacle), and
    kicad-cli then reported hole_to_hole for the pair. planes.hole_ok / via_at_points now refuse it."""
    import pcbnew
    from fluxplace import planes as PLN
    src = make(str(tmp_path / "h.kicad_pcb"))
    b = pcbnew.LoadBoard(src)
    v = pcbnew.PCB_VIA(b)
    v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(20), pcbnew.FromMM(15)))
    v.SetWidth(pcbnew.FromMM(0.6)); v.SetDrill(pcbnew.FromMM(0.3)); v.SetNetCode(b.FindNet("GND").GetNetCode())
    b.Add(v)
    assert PLN.hole_ok(b, pcbnew.FromMM(20.3), pcbnew.FromMM(15)) is False
    assert PLN.hole_ok(b, pcbnew.FromMM(21.5), pcbnew.FromMM(15)) is True
    assert PLN.via_at_points(b, "GND", [(20.3, 15.0)]) == 0
    assert PLN.via_at_points(b, "GND", [(21.5, 15.0)]) == 1


def test_signal_layer_pour_is_classified_and_census_counts_track_made_pads(tmp_path):
    """a pour on a layer that is not a plane is a signal-layer pour (owner "pour"); the census counts pads that a same-net
    track on a non-pour layer reaches (a track-made connection a via into the pour could replace)."""
    src = make(str(tmp_path / "s.kicad_pcb"))
    b = pcbnew.LoadBoard(src)
    tr = pcbnew.PCB_TRACK(b)
    tr.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(10)))
    tr.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(15), pcbnew.FromMM(10)))
    tr.SetWidth(pcbnew.FromMM(0.2)); tr.SetLayer(b.GetLayerID("F.Cu")); tr.SetNet(b.FindNet("GND"))
    b.Add(tr)
    c = PT.pour_census(b, "GND")
    assert c["pour_layers"] == ["In1.Cu"] and c["pads"] == 1 and c["track_made_pads"] == 0   # the test pad sits on B.Cu, the track on F.Cu
    rec = PT.run(src, str(tmp_path / "s2.kicad_pcb"), ["GND"], lambda p: {"GND": 1}, plane_layers=())
    assert rec["per_net"]["GND"]["signal_pour"] is True
    rec2 = PT.run(src, str(tmp_path / "s3.kicad_pcb"), ["GND"], lambda p: {"GND": 1}, plane_layers=("In1.Cu",))
    assert rec2["per_net"]["GND"]["signal_pour"] is False
