"""PHASE3I B6: mask openings are cut after copper. thermal `post` before routing defers; recut (finish) carves an
existing opening round copper routed inside it, so no B.Mask polygon covers a B.Cu track afterwards."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pcbnew = pytest.importorskip("pcbnew")
from fluxplace import thermal as TH, gaps as G
from test_planetie import make

CONS = {"thermal": {"cm5": {"net": "GND", "side": "B", "mask_open": True,
                            "rect": [1.0, 1.0, 20.0, 20.0]}}}


def test_pre_route_post_writes_no_mask_opening(tmp_path):
    src = make(str(tmp_path / "in.kicad_pcb"))
    out = str(tmp_path / "post.kicad_pcb")
    log = TH.run(src, CONS, out, stage="post")
    assert any("deferred to the finish stage" in line for line in log)
    b = pcbnew.LoadBoard(out)
    assert not [d for d in b.GetDrawings() if d.GetLayer() == pcbnew.B_Mask and d.GetClass() == "PCB_SHAPE"]


def test_gap_order_puts_mask_after_copper_kinds():
    assert G.ORDER.index("mask-opening") > G.ORDER.index("open-free")


def test_finish_carve_cuts_the_opening_round_copper_routed_inside_it(tmp_path):
    """An opening (owner GND: most copper inside) with a foreign SIG track routed through it: the finish carve (the
    shared step of recut) subtracts the track, so the opening no longer covers it. In-memory: a foreign net written
    to file and reloaded loses its name in this KiCad build, so the saved-file path is not used here."""
    b = pcbnew.LoadBoard(make(str(tmp_path / "in.kicad_pcb")))
    FM = pcbnew.FromMM

    def track(net, y):
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(pcbnew.VECTOR2I(FM(3), FM(y)))
        t.SetEnd(pcbnew.VECTOR2I(FM(18), FM(y)))
        t.SetWidth(FM(0.25))
        t.SetLayer(pcbnew.B_Cu)
        t.SetNet(net)
        b.Add(t)
    sig = pcbnew.NETINFO_ITEM(b, "SIG")
    b.Add(sig)
    track(b.FindNet("SIG"), 10)
    track(b.FindNet("GND"), 5)
    track(b.FindNet("GND"), 15)
    area = pcbnew.SHAPE_POLY_SET()
    area.NewOutline()
    for x, y in [(1, 1), (20, 1), (20, 20), (1, 20)]:
        area.Append(FM(x), FM(y))
    n = TH._carve(pcbnew, b, area, pcbnew.B_Cu, "GND", (0.5, 0.5, 20.5, 20.5), TH.GROW_MM)
    assert n["track"] == 1
    assert not area.Collide(pcbnew.VECTOR2I(FM(10), FM(10)), 0)
