"""w109: a run paralleled by a same-net plane gets zone credit (power.plane_parallel_credit); a rail load of unknown current is a
kickoff finding (power.unknown_loads, readychecks.load_current_line)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fluxplace import power as PW  # noqa: E402


def _plane_board(path, via_b=True):
    import pcbnew as P
    b = P.BOARD()
    b.SetCopperLayerCount(2)
    net = P.NETINFO_ITEM(b, "VB")
    b.Add(net)

    def v(x, y):
        via = P.PCB_VIA(b)
        via.SetPosition(P.VECTOR2I(P.FromMM(x), P.FromMM(y)))
        via.SetWidth(P.FromMM(0.5))
        via.SetDrill(P.FromMM(0.25))
        via.SetNet(net)
        b.Add(via)
    v(2, 5)
    if via_b:
        v(14, 5)
    t = P.PCB_TRACK(b)
    t.SetStart(P.VECTOR2I(P.FromMM(2), P.FromMM(5)))
    t.SetEnd(P.VECTOR2I(P.FromMM(14), P.FromMM(5)))
    t.SetWidth(P.FromMM(0.15))
    t.SetLayer(P.B_Cu)
    t.SetNet(net)
    b.Add(t)
    z = P.ZONE(b)
    z.SetLayer(P.F_Cu)
    z.SetNetCode(net.GetNetCode())
    ch = P.SHAPE_LINE_CHAIN()
    for x, y in ((0, 0), (16, 0), (16, 10), (0, 10)):
        ch.Append(P.FromMM(x), P.FromMM(y))
    ch.SetClosed(True)
    z.Outline().AddOutline(ch)
    b.Add(z)
    P.ZONE_FILLER(b).Fill(b.Zones())
    P.SaveBoard(path, b)
    return path


MEM = [{"layer": "B.Cu", "x1": 2.0, "y1": 5.0, "x2": 14.0, "y2": 5.0, "width": 0.15, "need": 1.0}]


def test_run_tied_by_vias_to_a_same_net_plane_gets_the_plane_width(tmp_path):
    pytest.importorskip("pcbnew")
    ctx = PW._board_ctx(_plane_board(str(tmp_path / "a.kicad_pcb")), {"VB"})
    assert PW.plane_parallel_credit(ctx, "VB", MEM) >= 1.0


def test_run_with_one_end_not_tied_gets_no_plane_credit(tmp_path):
    pytest.importorskip("pcbnew")
    ctx = PW._board_ctx(_plane_board(str(tmp_path / "b.kicad_pcb"), via_b=False), {"VB"})
    assert PW.plane_parallel_credit(ctx, "VB", MEM) == 0.0


def test_unknown_load_is_named_by_the_kickoff_check(tmp_path):
    pytest.importorskip("pcbnew")
    from test_power_ladder import _br_board, CONS_BR
    from fluxplace import readychecks as KO
    p = _br_board(tmp_path, extra_pad=("U3", 14, 4, "VB"))
    assert PW.unknown_loads(p, CONS_BR, layers=2) == {"VB": ["U3"]}
    line = KO.load_current_line(p, CONS_BR)
    assert line["status"] == "WARN" and "U3" in line["detail"]
    ok = KO.load_current_line(_br_board(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path), CONS_BR)
    assert ok["status"] == "PASS"
