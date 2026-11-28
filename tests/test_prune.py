"""launder.dead_copper: padless islands and free-end stubs, from KiCad's connectivity."""
import pytest

pcbnew = pytest.importorskip("pcbnew")

from fluxplace import launder as LA


def _board():
    b = pcbnew.BOARD()
    nets = {}
    for name in ("A", "B"):
        n = pcbnew.NETINFO_ITEM(b, name)
        b.Add(n)
        nets[name] = n
    fp = pcbnew.FOOTPRINT(b)
    fp.SetReference("R1")
    b.Add(fp)
    for num, x, net in (("1", 0, "A"), ("2", 10, "A"), ("3", 0, "B"), ("4", 10, "B")):
        p = pcbnew.PAD(fp)
        p.SetNumber(num)
        p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        p.SetLayerSet(pcbnew.PAD.SMDMask())
        p.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_RECT)
        p.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(pcbnew.FromMM(1), pcbnew.FromMM(1)))
        p.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(0 if net == "A" else 20)))
        p.SetNet(nets[net])
        fp.Add(p)
    return b, nets


def _trk(b, net, x0, y0, x1, y1, locked=False):
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x0), pcbnew.FromMM(y0)))
    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
    t.SetWidth(pcbnew.FromMM(0.2))
    t.SetLayer(pcbnew.F_Cu)
    t.SetNet(net)
    t.SetLocked(locked)
    b.Add(t)
    return t


def _via(b, net, x, y):
    v = pcbnew.PCB_VIA(b)
    v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    v.SetWidth(pcbnew.F_Cu, pcbnew.FromMM(0.5))
    v.SetDrill(pcbnew.FromMM(0.2))
    v.SetNet(net)
    b.Add(v)
    return v


def _xy(items):
    return sorted((round(pcbnew.ToMM(t.GetStart().x), 2), round(pcbnew.ToMM(t.GetStart().y), 2)) for t in items)


def test_island_and_stub_are_dead_live_path_kept():
    b, nets = _board()
    _trk(b, nets["A"], 0, 0, 5, 0)                    # live: pad 1 -> junction
    _trk(b, nets["A"], 5, 0, 10, 0)                   # live: junction -> pad 2
    _trk(b, nets["A"], 5, 0, 5, 3)                    # stub off the junction
    _trk(b, nets["A"], 5, 3, 6, 4)                    # stub tail
    _via(b, nets["A"], 3, 8)                          # island: a via touching nothing
    _trk(b, nets["B"], 0, 20, 4, 20)                  # stub from pad 3 that never reaches pad 4 (an open net keeps
    b.BuildConnectivity()                             # its pads; the free-end copper is still dead)
    dead = LA.dead_copper(b)
    assert _xy(dead) == [(0.0, 20.0), (3.0, 8.0), (5.0, 0.0), (5.0, 3.0)]


def test_locked_and_protected_copper_is_kept():
    b, nets = _board()
    _trk(b, nets["A"], 0, 0, 4, 0, locked=True)       # locked stub
    _trk(b, nets["B"], 0, 20, 4, 20)                  # stub on a protected net
    b.BuildConnectivity()
    assert LA.dead_copper(b, protect={"B"}) == []
    assert _xy(LA.dead_copper(b)) == [(0.0, 20.0)]


def test_end_pieces_shorter_than_the_track_is_wide_are_stubs_too():
    """W16 BT_INT_M: a ripped run left 0.025 / 0.035 mm end pieces; a neighbour as wide as they are long covers both their ends,
    so they counted as attached at both ends and the whole free branch behind them stayed."""
    b, nets = _board()
    _trk(b, nets["A"], 0, 0, 5, 0)
    _trk(b, nets["A"], 5, 0, 10, 0)
    _trk(b, nets["A"], 5, 0, 5, 3)                    # branch off the junction ...
    _trk(b, nets["A"], 5, 3, 5, 3.05)                 # ... ending in two pieces far shorter than the 0.2 mm width
    _trk(b, nets["A"], 5, 3.05, 5.03, 3.08)
    b.BuildConnectivity()
    assert _xy(LA.dead_copper(b)) == [(5.0, 0.0), (5.0, 3.0), (5.0, 3.05)]


def test_via_whose_tracks_are_all_on_one_layer_and_touch_each_other_is_dead():
    """W16 BT_INT_M: after its far side was re-laid elsewhere the old via kept two In2 neighbours (a short end piece and the track
    behind it both sit on the via's pad), so it never counted as a free end and the old run stayed."""
    b, nets = _board()
    _trk(b, nets["A"], 0, 0, 5, 0)
    _trk(b, nets["A"], 5, 0, 10, 0)
    _trk(b, nets["A"], 5, 0, 5, 2.9)                  # old run to a via that changes layer for nothing now
    _trk(b, nets["A"], 5, 2.9, 5, 3)
    _via(b, nets["A"], 5, 3)
    b.BuildConnectivity()
    assert _xy(LA.dead_copper(b)) == [(5.0, 0.0), (5.0, 2.9), (5.0, 3.0)]
