"""repair.bridge — the multi-layer maze router for one unconnected pad.
Needs pcbnew (skipped elsewhere). Builds a 2-layer board with two pads of net
SIG on F.Cu separated by a foreign F.Cu wall: the only route is a via to
B.Cu and back, and it must not touch the wall."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pcbnew = pytest.importorskip("pcbnew")
from fluxplace import repair as RP  # noqa: E402


def _pad(board, ref, at, net):
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetPosition(pcbnew.VECTOR2I(int(at[0] * 1e6), int(at[1] * 1e6)))
    p = pcbnew.PAD(fp)
    p.SetNumber("1")
    p.SetShape(pcbnew.PAD_SHAPE_RECT)
    p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    p.SetSize(pcbnew.VECTOR2I(int(0.6e6), int(0.6e6)))
    ls = pcbnew.LSET()
    ls.AddLayer(pcbnew.F_Cu)
    p.SetLayerSet(ls)  # F.Cu only
    p.SetPosition(fp.GetPosition())
    p.SetNet(net)
    fp.Add(p)
    board.Add(fp)
    return fp


def test_bridge_crosses_a_wall_by_via():
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    edge = pcbnew.PCB_SHAPE(board)
    edge.SetShape(pcbnew.SHAPE_T_RECT)
    edge.SetStart(pcbnew.VECTOR2I(0, 0))
    edge.SetEnd(pcbnew.VECTOR2I(int(20e6), int(20e6)))
    edge.SetLayer(pcbnew.Edge_Cuts)
    board.Add(edge)
    sig = pcbnew.NETINFO_ITEM(board, "SIG")
    wall = pcbnew.NETINFO_ITEM(board, "WALL")
    board.Add(sig)
    board.Add(wall)
    _pad(board, "A", (5, 10), sig)
    _pad(board, "B", (15, 10), sig)
    # a foreign F.Cu wall across the whole board between them
    w = pcbnew.PCB_TRACK(board)
    w.SetStart(pcbnew.VECTOR2I(int(10e6), int(0.5e6)))
    w.SetEnd(pcbnew.VECTOR2I(int(10e6), int(19.5e6)))
    w.SetLayer(pcbnew.F_Cu)
    w.SetWidth(int(0.3e6))
    w.SetNet(wall)
    board.Add(w)
    added = RP.bridge(board, "A", "1", layers=["F.Cu", "B.Cu"], cell=0.25, log=lambda m: None)
    assert added, "no route found"
    vias = [t for t in added if t.GetClass() == "PCB_VIA"]
    assert len(vias) == 2, "expected down-and-up through the wall"
    layers = {t.GetLayer() for t in added if t.GetClass() == "PCB_TRACK"}
    assert pcbnew.B_Cu in layers
    # nothing added on F.Cu may come within clearance of the wall
    for t in added:
        if t.GetClass() == "PCB_TRACK" and t.GetLayer() == pcbnew.F_Cu:
            assert not t.GetEffectiveShape(pcbnew.F_Cu).Collide(
                w.GetEffectiveShape(pcbnew.F_Cu), int(0.13e6))
    # the route starts on pad A and ends on pad B
    pts = [t.GetStart() for t in added if t.GetClass() == "PCB_TRACK"] + \
          [t.GetEnd() for t in added if t.GetClass() == "PCB_TRACK"]
    assert pcbnew.VECTOR2I(int(5e6), int(10e6)) in pts
    assert pcbnew.VECTOR2I(int(15e6), int(10e6)) in pts


def test_bridge_reports_no_path():
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    edge = pcbnew.PCB_SHAPE(board)
    edge.SetShape(pcbnew.SHAPE_T_RECT)
    edge.SetStart(pcbnew.VECTOR2I(0, 0))
    edge.SetEnd(pcbnew.VECTOR2I(int(20e6), int(20e6)))
    edge.SetLayer(pcbnew.Edge_Cuts)
    board.Add(edge)
    sig = pcbnew.NETINFO_ITEM(board, "SIG")
    wall = pcbnew.NETINFO_ITEM(board, "WALL")
    board.Add(sig)
    board.Add(wall)
    _pad(board, "A", (5, 10), sig)
    _pad(board, "B", (15, 10), sig)
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):   # walled on both layers
        w = pcbnew.PCB_TRACK(board)
        w.SetStart(pcbnew.VECTOR2I(int(10e6), int(0)))
        w.SetEnd(pcbnew.VECTOR2I(int(10e6), int(20e6)))
        w.SetLayer(layer)
        w.SetWidth(int(0.3e6))
        w.SetNet(wall)
        board.Add(w)
    msgs = []
    assert RP.bridge(board, "A", "1", layers=["F.Cu", "B.Cu"], cell=0.25, log=msgs.append) == []
    assert msgs and "no path" in msgs[-1]
