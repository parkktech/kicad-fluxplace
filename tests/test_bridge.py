"""repair.bridge — the multi-layer maze router for one unconnected pad.
Needs pcbnew (skipped elsewhere). Builds a 2-layer board with two pads of net
SIG on F.Cu separated by a foreign F.Cu wall: the only route is a via to
B.Cu and back, and it must not touch the wall."""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pcbnew = pytest.importorskip("pcbnew")
from fluxplace import repair as RP  # noqa: E402
from fluxplace import patch as PATCH  # noqa: E402


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


def test_apply_path_snaps_track_end_onto_via_centre():
    """patch.apply_path — the utv-comms V1.5 repair --patch bug (D70ag): a
    dijkstra path terminates on WHATEVER grid cell of an existing via's
    island it first reaches, not the via's true drill centre. Drawing the
    joining track to that cell centre leaves a track end merely inside the
    via's copper, not centred on it, which KiCad's own DRC guard then
    catches as track_not_centered_on_via and the patch reverts itself.
    apply_path must snap onto the via's exact position instead."""
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    sig = pcbnew.NETINFO_ITEM(board, "SIG")
    board.Add(sig)
    # an existing via NOT aligned to any 0.25 mm grid cell centre — exactly
    # the situation a real board's copper is in
    via_x, via_y = 10.13, 10.07
    v = pcbnew.PCB_VIA(board)
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetPosition(pcbnew.VECTOR2I(int(via_x * 1e6), int(via_y * 1e6)))
    v.SetWidth(int(0.6e6))         # radius 0.3 mm
    v.SetDrill(int(0.3e6))
    v.SetNet(sig)
    board.Add(v)

    class _FakeGrid:
        """Just enough of patch.Grid for apply_path: .layers and .mm()."""
        cell = 0.25
        x0 = 0.0
        y0 = 0.0
        layers = [pcbnew.F_Cu, pcbnew.B_Cu]

        def mm(self, cx, cy):
            return (self.x0 + (cx + 0.5) * self.cell,
                    self.y0 + (cy + 0.5) * self.cell)

    grid = _FakeGrid()
    # the cell dijkstra would have reached first: inside the via's 0.3 mm
    # disc but ~0.2 mm off its true centre (cell centre (10.125, 9.875))
    cx, cy = 40, 39
    cell_x, cell_y = grid.mm(cx, cy)
    assert math.hypot(cell_x - via_x, cell_y - via_y) < 0.3      # inside the via
    assert (cell_x, cell_y) != (via_x, via_y)                    # but off-centre

    path = [(0, cx - 4, cy), (0, cx, cy)]      # one F.Cu run into the via
    added = PATCH.apply_path(board, grid, path, "SIG", width_mm=0.15,
                             via_mm=0.6, drill_mm=0.3)
    trk = added[-1]
    assert trk.GetClass() == "PCB_TRACK"
    end = trk.GetEnd()
    assert (end.x, end.y) == (v.GetPosition().x, v.GetPosition().y), (
        "track end must be snapped onto the existing via's exact centre, "
        "not the grid cell that merely overlaps its copper")
