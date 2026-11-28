"""k15: power-width graded against the sourced hard limit (realistic limits); dcdrop copper graph joins a T-junction."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def test_hard_rise_is_the_sourced_rti_minus_the_ambient_maximum(monkeypatch):
    from fluxplace import power as PW, limits as LM
    e = {"hard": 130.0, "sources": [{"url": "https://x", "quote": "RTI 130"}]}
    monkeypatch.setattr(LM, "get", lambda rule, iface: e)
    assert PW.hard_rise_c({"env": {"temp_max_c": 85}})[0] == 45.0
    assert PW.hard_rise_c({}) is None                                    # no ambient maximum: nothing is relaxed
    monkeypatch.setattr(LM, "get", lambda rule, iface: {"hard": 130.0, "sources": []})
    assert PW.hard_rise_c({"env": {"temp_max_c": 85}}) is None           # unsourced: nothing is relaxed


def test_continuous_fraction_of_a_rail():
    from fluxplace import power as PW
    assert PW._cont_frac({"power": {"V": {"max_current_ma": 3000, "continuous_ma": 1512}}}, "V") == 1512 / 3000
    assert PW._cont_frac({"power": {"V": {"max_current_ma": 3000}}}, "V") == 1.0


def test_hard_pass_judges_only_the_thin_members_as_the_neck():
    from fluxplace import power as PW
    r = {"oz_outer": 1.0, "oz_inner": 1.0}
    segs = [{"layer": "F.Cu", "x1": 0, "y1": 0, "x2": 10, "y2": 0, "width": 1.4},
            {"layer": "F.Cu", "x1": 10, "y1": 0, "x2": 10.5, "y2": 0, "width": 0.15}]
    run = {"members": [0, 1], "layers": ["F.Cu"], "len": 10.5, "min_w": 0.15}
    assert PW._hard_pass(run, segs, r, {"F.Cu", "B.Cu"}, None, 1.0, 45.0)        # 0.5 mm neck at 1 A: a few C
    long_thin = dict(run, members=[1], len=10.5)
    segs2 = [segs[0], dict(segs[1], x2=20.0)]
    assert not PW._hard_pass(long_thin, segs2, r, {"F.Cu", "B.Cu"}, None, 2.0, 45.0)   # 10 mm at 0.15 mm, 2 A: 80 C, over 45 C


def test_channel_graph_joins_a_track_end_on_another_tracks_side(tmp_path):
    import pytest
    pcbnew = pytest.importorskip("pcbnew")
    from fluxplace import channel as CH
    b = pcbnew.BOARD()
    net = pcbnew.NETINFO_ITEM(b, "V")
    b.Add(net)
    lay = pcbnew.F_Cu
    for (x1, y1, x2, y2) in ((0, 0, 10, 0), (5, 5, 5, 0)):           # the second track ends on the middle of the first
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
        t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x2), pcbnew.FromMM(y2)))
        t.SetWidth(pcbnew.FromMM(0.3))
        t.SetLayer(lay)
        t.SetNet(net)
        b.Add(t)
    rows = [{"type": "copper", "name": n} for n in ("F.Cu", "B.Cu")]
    adj, _pads, segs, _v = CH._graph(b, "V", rows)
    nodes = {sid: [n for n, e in adj.items() if any(x[2] and x[2].get("kind") == "seg" and x[2]["id"] == sid for x in e)] for sid in (0, 1)}
    dist, _prev = CH._dijkstra(adj, nodes[0][0])
    assert all(n in dist for n in nodes[1])                                # the stub's copper reaches both ends of the track it tees into
