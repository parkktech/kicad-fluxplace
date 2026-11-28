"""A track laid on an outer layer cuts that layer's pour: every island it makes is tied in the same guarded edit
(W16 RF_EXT: critroute refused "lay opened connection(s): GND 2->5"; with the old picker a lay passed by tying an OLD island
elsewhere and leaving the new one open)."""
import os
import sys

import pytest

pcbnew = pytest.importorskip("pcbnew")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fluxplace import critroute as CR          # noqa: E402
from fluxplace import handroute as H           # noqa: E402
from test_handroute import Brd, V, mm          # noqa: E402


def pour(B, net, layer, x0, y0, x1, y1):
    z = pcbnew.ZONE(B.b)
    z.SetLayer(layer)
    z.SetNet(B.net(net))
    o = z.Outline()
    o.NewOutline()
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        o.Append(mm(x), mm(y))
    z.SetLocalClearance(mm(0.2))
    z.SetMinThickness(mm(0.2))
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_NEVER)
    B.b.Add(z)


def cut_board(d):
    """GND pours on F.Cu and B.Cu tied by one via on the left.  An OLD untied F.Cu island (a GND pad inside a ring of another
    net, top right).  Net S: two pads at the top and bottom edge; the straight F.Cu track between them cuts the right half off."""
    B = Brd()
    B.edge(0, 0, 30, 20)
    pour(B, "GND", pcbnew.F_Cu, 0.4, 0.4, 29.6, 19.6)
    pour(B, "GND", pcbnew.B_Cu, 0.4, 0.4, 29.6, 19.6)
    v = pcbnew.PCB_VIA(B.b)
    v.SetPosition(V(5, 10))
    v.SetWidth(pcbnew.F_Cu, mm(0.6))
    v.SetDrill(mm(0.3))
    v.SetNet(B.net("GND"))
    B.b.Add(v)
    B.pad("G1", "GND", 7, 10, 1.0, 1.0)
    B.pad("G2", "GND", 25, 10, 1.0, 1.0)
    B.pad("G3", "GND", 27, 3, 0.8, 0.8)
    B.track("X", [(25.8, 1.8), (28.2, 1.8), (28.2, 4.2), (25.8, 4.2), (25.8, 1.8)])
    B.pad("S1", "S", 15, 1.0, 0.6, 1.6)
    B.pad("S2", "S", 15, 19.0, 0.6, 1.6)
    f = pcbnew.ZONE_FILLER(B.b)
    zs = pcbnew.ZONES()
    for z in B.b.Zones():
        zs.append(z)
    f.Fill(zs)
    return B.save(d)


def gnd_open(board):
    return H.summarize_drc(H.drc_raw(board, "kicad-cli"))["unc_by_net"].get("GND", 0)


def test_lay_ties_the_island_it_cut_not_an_old_one_elsewhere(tmp_path):
    p = cut_board(tmp_path)
    assert gnd_open(p) == 1                                         # the old ring island
    plain = H.lay(p, "S", "F.Cu: 15,1 15,19", out=str(tmp_path / "plain.kicad_pcb"), width=0.25)
    assert not plain["accepted"] and "lay opened connection(s): GND 1->2" in plain["reason"]
    out = str(tmp_path / "o.kicad_pcb")
    r = H.lay(p, "S", "F.Cu: 15,1 15,19", out=out, width=0.25, stitch=True)
    assert r["accepted"], r.get("reason")
    assert any(s["x"] > 15.5 and not (25.8 < s["x"] < 28.2 and 1.8 < s["y"] < 4.2) for s in r["stitched"]), r["stitched"]   # in the cut-off half
    assert gnd_open(out) == 0                                       # both islands tied: no open traded for another


def test_stitch_attempts_take_one_site_per_island(tmp_path, monkeypatch):
    from fluxplace import pourisland as PI, pourtie as PTF
    isl = [[(1.0, 1.0), (1.4, 1.0)], [(5.0, 5.0)], [(9.0, 9.0), (9.4, 9.0), (9.8, 9.0)]]
    monkeypatch.setattr(PI, "island_groups", lambda b, n, *a, **k: isl)
    monkeypatch.setattr(PTF, "fill_hit", lambda *a: True)
    monkeypatch.setattr(H, "_via_site_clear", lambda b, M, n, x, y, *vv: (x, y) != (9.0, 9.0))
    monkeypatch.setattr(H, "_split_sites", lambda b, n: [])

    class M:
        @staticmethod
        def net_rule(n):
            return {"via_dia": 0.6, "via_drill": 0.3}
    b = pcbnew.LoadBoard(cut_board(tmp_path))
    atts = [[(x, y) for _n, x, y, _vv in a] for a in H._stitch_attempts(b, M, ["GND"])]
    assert atts[0] == [(1.0, 1.0), (5.0, 5.0), (9.4, 9.0)]          # one via per island, an illegal site skipped
    assert atts[1] == [(1.4, 1.0), (9.8, 9.0)]                      # the next attempt: each island's next site


def test_critroute_lays_with_the_island_stitch(tmp_path, monkeypatch):
    p = cut_board(tmp_path)
    seen = {}

    def lay(cur, net, path, **kw):
        seen.update(kw)
        return {"accepted": False, "reason": "stop here"}
    monkeypatch.setattr(H, "lay", lay)
    monkeypatch.setattr(H, "suggest", lambda *a, **k: {"found": True, "path": "F.Cu: 15,1 15,19", "length_mm": 18.0})
    CR.apply(p, str(tmp_path / "o.kicad_pcb"), None, None, {}, None, crit=[{"net": "S", "class": "clock"}])
    assert seen.get("stitch") is True and seen.get("lock") is True
