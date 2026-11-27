"""Pour-first ties (pourtie.py) on a synthetic 4-layer board: a rail pour on an inner signal layer.
A pad inside the pour outline is tied by a via before routing (verified on the filled zone), a net whose pads are all tied is
fully connected (so the router's job skips it), a pad outside the outline is listed as untied, and a later track on the
pour layer that cuts a tie is detected by repair() and re-tied."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbnew
from fluxplace import pourtie as PT, routeconn as RC


def _seg(b, x0, y0, x1, y1, layer, net=None):
    s = pcbnew.PCB_SHAPE(b)
    s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetLayer(b.GetLayerID("Edge.Cuts"))
    s.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x0), pcbnew.FromMM(y0)))
    s.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
    b.Add(s)


def _zone(b, net, layer, pts):
    z = pcbnew.ZONE(b)
    z.SetNet(b.FindNet(net))
    z.SetLayer(b.GetLayerID(layer))
    z.SetIsRuleArea(False)
    z.Outline().NewOutline()
    for x, y in pts:
        z.Outline().Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    b.Add(z)


def _pad_fp(b, ref, x, y, net):
    fp = pcbnew.FOOTPRINT(b)
    fp.SetReference(ref)
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    pad = pcbnew.PAD(fp)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.8), pcbnew.FromMM(0.8)))
    pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    pad.SetLayer(b.GetLayerID("F.Cu"))
    pad.SetNet(b.FindNet(net))
    pad.SetNumber("1")
    fp.Add(pad)
    b.Add(fp)


def board(path):
    b = pcbnew.BOARD()
    b.SetCopperLayerCount(4)
    for x0, y0, x1, y1 in ((0, 0, 40, 0), (40, 0, 40, 30), (40, 30, 0, 30), (0, 30, 0, 0)):
        _seg(b, x0, y0, x1, y1, "Edge.Cuts")
    for n in ("RAIL", "VT", "SIG"):
        b.Add(pcbnew.NETINFO_ITEM(b, n))
    _pad_fp(b, "U1", 10, 10, "RAIL")        # inside the pour outline
    _pad_fp(b, "U2", 30, 10, "RAIL")        # outside it (x > 20)
    _pad_fp(b, "U3", 25, 25, "VT")          # VT: its own pad inside a pour of VT (below)
    _pad_fp(b, "U4", 35, 5, "SIG")          # SIG: a signal net with one pad (an unused net is dropped on save)
    _zone(b, "RAIL", "In2.Cu", [(1, 1), (20, 1), (20, 29), (1, 29)])
    _zone(b, "VT", "In1.Cu", [(21, 21), (29, 21), (29, 29), (21, 29)])
    b.Save(path)
    return path


def test_pad_inside_pour_is_tied_by_via_and_outside_pad_is_listed(tmp_path):
    src = board(str(tmp_path / "in.kicad_pcb"))
    rec = PT.run(src, str(tmp_path / "out.kicad_pcb"), str(tmp_path))
    assert any(t["net"] == "RAIL" and t["pad"] == "1" and t["via"] for t in rec["tied"])     # tied through a via into In2
    assert any(u["net"] == "RAIL" and "outside" in u["reason"] for u in rec["untied"])     # no via can reach it: listed
    assert rec["nets"]["RAIL"]["pour_layers"] == ["In2.Cu"]


def test_tie_vias_use_route_rule_sizes(tmp_path):
    src = board(str(tmp_path / "rules.kicad_pcb"))
    out = str(tmp_path / "rules-out.kicad_pcb")
    rec = PT.run(src, out, str(tmp_path), cons={"route": {"min_via_mm": 0.73, "min_drill_mm": 0.33}})
    assert rec["rules"]["via"] == 0.73 and rec["rules"]["drill"] == 0.33
    vias = [t for t in pcbnew.LoadBoard(out).GetTracks() if t.Type() == pcbnew.PCB_VIA_T]
    assert vias and all(t.GetWidth() == pcbnew.FromMM(0.73) and t.GetDrill() == pcbnew.FromMM(0.33) for t in vias)


def test_fully_tied_net_has_no_open_connection_for_the_router(tmp_path):
    """the router's job is the open connections (routeconn): VT's only pad is in its own pour, so VT is not in the job."""
    src = board(str(tmp_path / "in2.kicad_pcb"))
    PT.run(src, str(tmp_path / "out2.kicad_pcb"), str(tmp_path))
    RC.PY = "/usr/bin/python3"                   # other tests point RC.PY at a missing interpreter (module global)
    assert RC.open_by_net(str(tmp_path / "out2.kicad_pcb"), method="pcbnew", fill=True).get("VT", 0) == 0


def test_track_on_pour_layer_cuts_a_tie_and_repair_retries_it(tmp_path):
    """the tie is made, then a later signal track on the pour layer (In2) runs across the via and cuts the pour around it;
    repair_in_memory detects the lost tie and re-ties it at a legal site (or lists it)."""
    src = board(str(tmp_path / "in3.kicad_pcb"))
    b = pcbnew.LoadBoard(src)
    rec = PT.run(src, str(tmp_path / "tied.kicad_pcb"), str(tmp_path))
    b = pcbnew.LoadBoard(str(tmp_path / "tied.kicad_pcb"))
    tr = pcbnew.PCB_TRACK(b)
    tr.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(5), pcbnew.FromMM(10)))
    tr.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(15), pcbnew.FromMM(10)))
    tr.SetWidth(pcbnew.FromMM(0.2)); tr.SetLayer(b.GetLayerID("In2.Cu"))
    b.Add(tr)
    tr.SetNet(b.FindNet("SIG"))
    rep = PT.repair_in_memory(b, rec)
    assert rep["lost"] >= 1                                              # detected
    assert rep["repaired"] + len(rep["unrepaired"]) == rep["lost"]      # every lost tie is repaired or listed with a reason


def test_5a_rail_without_a_routed_spine_gets_a_spine_zone_at_least_the_ipc_width(tmp_path, monkeypatch):
    """R036: a 5 A rail whose IPC width does not fit a track gets a pour along a pad spanning tree even when no router laid a spine."""
    from test_power import _stub_dg, CLEAN
    from fluxplace import power as PW, gaps as GP
    monkeypatch.setattr(GP, "_zone_nets", lambda p: set())
    _stub_dg(monkeypatch, CLEAN, CLEAN)
    b = pcbnew.BOARD()
    b.SetCopperLayerCount(4)
    for x0, y0, x1, y1 in ((0, 0, 40, 0), (40, 0, 40, 30), (40, 30, 0, 30), (0, 30, 0, 0)):
        _seg(b, x0, y0, x1, y1, "Edge.Cuts")
    b.Add(pcbnew.NETINFO_ITEM(b, "VBATT"))
    for ref, x, y in (("U1", 6, 15), ("U2", 20, 15), ("U3", 34, 15)):
        _pad_fp(b, ref, x, y, "VBATT")
    for pad in b.GetPads():                                      # SMD pads on F.Cu only (the fixture's SetLayer leaves the set on B.Cu)
        ls = pcbnew.LSET()
        ls.AddLayer(pcbnew.F_Cu)
        pad.SetLayerSet(ls)
    src = str(tmp_path / "rail.kicad_pcb")
    b.Save(src)
    cons = {"power": {"VBATT": {"max_current_ma": 5000, "max_v": 14.5}}}
    r = PW.plan(cons, layers=4)["VBATT"]
    assert r["pour"] and r["track_mm"] > 1.0                      # IPC width for 5 A is far over a signal track
    out = str(tmp_path / "rail-out.kicad_pcb")
    rec = PW.pour_rails(src, cons, out, layers=4)
    assert rec["added"] == ["VBATT/F.Cu"]
    z = [z for z in pcbnew.LoadBoard(out).Zones() if z.GetNetname() == "VBATT"]
    assert len(z) == 1
    bb = z[0].GetBoundingBox()
    assert pcbnew.ToMM(bb.GetHeight()) >= r["width_outer"] and pcbnew.ToMM(bb.GetWidth()) >= 28     # spans all three pads
