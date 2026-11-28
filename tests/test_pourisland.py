import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pcbnew = pytest.importorskip("pcbnew")
from fluxplace import pourisland as PI, handroute as H
from test_planetie import make, vias


def test_guarded_stitching_via_joins_open_pour_island(tmp_path):
    src = make(str(tmp_path / "in.kicad_pcb"))
    out = str(tmp_path / "out.kicad_pcb")
    before = H.open_nets(src)["open"]
    result = PI.repair(src, out, "GND")
    assert result["accepted"], result
    assert result["action"] == "via" and vias(out) == 1
    assert H.open_nets(out)["open"] < before and H.open_nets(out)["open"] == 0
    again = PI.repair(out, str(tmp_path / "again.kicad_pcb"), "GND")           # the rung calls once more: refused, never raised
    assert not again["accepted"] and not os.path.exists(str(tmp_path / "again.kicad_pcb"))


def test_net_without_pour_is_refused_without_output(tmp_path):
    src = make(str(tmp_path / "in.kicad_pcb"))
    out = str(tmp_path / "missing.kicad_pcb")
    result = PI.repair(src, out, "NO_SUCH_NET")
    assert not result["accepted"] and not os.path.exists(out)


def _blocked(path, locked=False, far_pad=False):
    """make()'s board plus a wide SIG track on B.Cu over every via site around the GND pad (SIG has a pad of its own: a net with
    no pad is not kept on load)."""
    make(path)
    b = pcbnew.LoadBoard(path)
    sig = pcbnew.NETINFO_ITEM(b, "SIG")
    b.Add(sig)
    for x in ((17, 5) if far_pad else (17,)):            # far_pad: SIG joins two pads, so ripping its track opens it
        fp = pcbnew.FOOTPRINT(b)
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(10)))
        pad = pcbnew.PAD(fp)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(1), pcbnew.FromMM(1)))
        pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(10)))
        pad.SetNet(sig)
        pad.SetNumber("1")
        fp.Add(pad)
        b.Add(fp)
    tr = pcbnew.PCB_TRACK(b)
    tr.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(5), pcbnew.FromMM(10)))
    tr.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(17), pcbnew.FromMM(10)))
    tr.SetWidth(pcbnew.FromMM(5))
    tr.SetLayer(b.GetLayerID("B.Cu"))
    tr.SetNet(sig)
    tr.SetLocked(locked)
    b.Add(tr)
    b.Save(path)
    return path


def _cons(tmp_path, rail=False):
    """Constraints present (no constraints = every net hard, nothing is ripped); rail: SIG is a [power] rail."""
    p = tmp_path / "c.toml"
    p.write_text("[route]\nrail_min_pads = 8\n" + ('[power."SIG"]\nmax_current_ma = 500\n' if rail else ""))
    return str(p)


def test_k15_sealed_island_rips_the_unlocked_blocking_track_and_lays_the_via(tmp_path):
    src = _blocked(str(tmp_path / "in.kicad_pcb"))
    out = str(tmp_path / "out.kicad_pcb")
    result = PI.repair(src, out, "GND", constraints=_cons(tmp_path))
    assert result["accepted"], result.get("reason")
    assert result["action"].startswith("rip+") and result["ripped"] == {"SIG": 1}
    assert vias(out) == 1 and H.open_nets(out)["open"] == 0


def test_k15_locked_blocking_track_is_never_ripped(tmp_path):
    src = _blocked(str(tmp_path / "in.kicad_pcb"), locked=True)
    out = str(tmp_path / "out.kicad_pcb")
    result = PI.repair(src, out, "GND", constraints=_cons(tmp_path))
    assert not result["accepted"] and not os.path.exists(out)


def test_a_ripped_rail_is_relaid_whole_at_its_rail_width(tmp_path):
    """W16 L300.2: the rail link over the only site is not redundant; the rip is kept when the same rung re-lays the rail off the site."""
    src = _blocked(str(tmp_path / "in.kicad_pcb"), far_pad=True)
    out = str(tmp_path / "out.kicad_pcb")
    result = PI.repair(src, out, "GND", constraints=_cons(tmp_path, rail=True))
    assert result["accepted"] and result["ripped"] == {"SIG": 1}, result.get("reason")
    assert H.open_nets(out)["open"] == 0                                     # GND tied AND the rail SIG whole again
    b = pcbnew.LoadBoard(out)
    widths = {t.GetWidth() for t in b.GetTracks() if t.GetNetname() == "SIG"}
    assert widths and min(widths) >= pcbnew.FromMM(0.5) and pcbnew.FromMM(5) not in widths


def test_k15_a_rail_the_relay_cannot_close_keeps_its_copper(tmp_path, monkeypatch):
    src = _blocked(str(tmp_path / "in.kicad_pcb"), far_pad=True)
    out = str(tmp_path / "out.kicad_pcb")
    monkeypatch.setattr(H, "suggest", lambda *a, **k: {"found": False, "reason": "no grid path"})
    result = PI.repair(src, out, "GND", constraints=_cons(tmp_path, rail=True))
    assert not result["accepted"] and not os.path.exists(out)                # the rip would leave the rail SIG open: refused


def _sealed(path, wall=False):
    """make()'s board with an F.Cu GND pour island round the GND pad (no via in it), a LOCKED SIG track on B.Cu under every via
    site in and beside it, and a GND via 3.9 mm east that already ties into the In1 plane. wall: an unlocked SIG2 track on F.Cu
    between the island and that via (SIG2 joins two pads, so ripping it opens SIG2)."""
    make(path)
    b = pcbnew.LoadBoard(path)

    def pad(net, x, y, front=True):
        fp = pcbnew.FOOTPRINT(b)
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
        pd = pcbnew.PAD(fp)
        pd.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pd.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pd.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(1), pcbnew.FromMM(1)))
        pd.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
        if front:
            pd.SetLayerSet(pcbnew.PAD.SMDMask())
        pd.SetNet(net)
        pd.SetNumber("1")
        fp.Add(pd)
        b.Add(fp)

    def track(net, layer, x0, y0, x1, y1, w, locked=False):
        tr = pcbnew.PCB_TRACK(b)
        tr.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x0), pcbnew.FromMM(y0)))
        tr.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
        tr.SetWidth(pcbnew.FromMM(w))
        tr.SetLayer(b.GetLayerID(layer))
        tr.SetNet(net)
        tr.SetLocked(locked)
        b.Add(tr)
    gnd = b.FindNet("GND")
    for fp in b.GetFootprints():                         # make()'s GND pad is on every copper layer: a front SMD pad here
        for pd in fp.Pads():
            pd.SetLayerSet(pcbnew.PAD.SMDMask())
    sig = pcbnew.NETINFO_ITEM(b, "SIG")
    b.Add(sig)
    pad(sig, 5, 10, front=False)
    track(sig, "B.Cu", 5, 10, 10, 10, 5, locked=True)    # covers x 2.5 .. 12.5, y 7.5 .. 12.5 on B.Cu
    z = pcbnew.ZONE(b)
    z.SetNet(gnd)
    z.SetLayer(b.GetLayerID("F.Cu"))
    z.Outline().NewOutline()
    for x, y in ((8, 8), (12, 8), (12, 12), (8, 12)):
        z.Outline().Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    b.Add(z)
    v = pcbnew.PCB_VIA(b)
    v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(13.9), pcbnew.FromMM(10)))
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetWidth(pcbnew.FromMM(0.6))
    v.SetDrill(pcbnew.FromMM(0.3))
    v.SetNet(gnd)
    b.Add(v)
    if wall:
        sig2 = pcbnew.NETINFO_ITEM(b, "SIG2")
        b.Add(sig2)
        pad(sig2, 12.9, 3)
        pad(sig2, 12.9, 27)
        track(sig2, "F.Cu", 12.9, 3, 12.9, 27, 0.2)
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    b.Save(path)
    return path


def test_sealed_island_is_linked_on_its_own_layer_to_an_existing_tied_via(tmp_path):
    """W16 C112.2 / L300.2 (hand fix by fable-gnd): no via fits in or beside the island, a track to a via the net already has does."""
    src = _sealed(str(tmp_path / "in.kicad_pcb"))
    out = str(tmp_path / "out.kicad_pcb")
    result = PI.repair(src, out, "GND", constraints=_cons(tmp_path))
    assert result["accepted"] and result["action"] == "link", result
    assert vias(out) == 1 and H.open_nets(out)["open"] == 0                  # no new via: the existing one carries the island


def test_link_through_an_unlocked_track_rips_it_and_relays_its_net(tmp_path):
    """W16 C112.2: BT_INT_M wrapped the pad; the link is laid without it and BT_INT_M is re-laid round the link in the same repair."""
    src = _sealed(str(tmp_path / "in.kicad_pcb"), wall=True)
    out = str(tmp_path / "out.kicad_pcb")
    result = PI.repair(src, out, "GND", constraints=_cons(tmp_path))
    assert result["accepted"] and result["action"] == "rip+link" and result["ripped"] == {"SIG2": 1}, result
    assert H.open_nets(out)["open"] == 0                                     # GND tied, SIG2 whole again


def test_k15_a_rip_that_alone_lets_the_pour_reach_the_island_is_the_repair(tmp_path, monkeypatch):
    """W16 C112.2: ripping BT_INT_M let the F.Cu GND fill reach the pad, so the lay on the stripped copy saw nothing left to close."""
    src = _blocked(str(tmp_path / "in.kicad_pcb"))
    out = str(tmp_path / "out.kicad_pcb")
    monkeypatch.setattr(H, "lay", lambda *a, **k: {"accepted": False, "reason": "target not closed: open connections of GND 0 -> 0"})
    monkeypatch.setattr(PI, "_relay", lambda *a, **k: {})                    # the re-count of the stripped board: nothing open
    result = PI.repair(src, out, "GND", constraints=_cons(tmp_path))
    assert result["accepted"] and result["action"] == "rip" and result["ripped"] == {"SIG": 1}
    assert os.path.exists(out) and "SIG" in open(out).read() and vias(out) == 0


def test_a_track_tied_to_the_fill_by_its_body_only_is_one_island_as_in_drc(tmp_path):
    """W16 U418.3: a stub ended just outside the fill edge with its body in the fill. DRC calls that connected; the copper model
    looked at centre-line ends only, called the island untied and the rung spent its tries on it."""
    src = _sealed(str(tmp_path / "in.kicad_pcb"))
    b = pcbnew.LoadBoard(src)
    tr = pcbnew.PCB_TRACK(b)
    tr.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(12.06), pcbnew.FromMM(11)))    # fill edge at x = 12: the end is 0.06 mm outside it
    tr.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(13.9), pcbnew.FromMM(11)))
    tr.SetWidth(pcbnew.FromMM(0.2))
    tr.SetLayer(b.GetLayerID("F.Cu"))
    tr.SetNet(b.FindNet("GND"))
    b.Add(tr)
    tr2 = pcbnew.PCB_TRACK(b)
    tr2.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(13.9), pcbnew.FromMM(11)))
    tr2.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(13.9), pcbnew.FromMM(10)))
    tr2.SetWidth(pcbnew.FromMM(0.2))
    tr2.SetLayer(b.GetLayerID("F.Cu"))
    tr2.SetNet(b.FindNet("GND"))
    b.Add(tr2)
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    b.Save(src)
    assert not [r for r in H.open_nets(src)["connections"] if r["net"] == "GND"]
    M = H._model(src)
    isl, omap = M.islands("GND")
    pad = next(o for o in M.obs if o.kind == "pad" and o.net == "GND")
    via = next(o for o in M.obs if o.kind == "via" and o.net == "GND")
    assert omap[pad.id] == omap[via.id]
    assert list(PI._untied(M.b, "GND")) and not list(PI._untied(M.b, "GND", M))   # no via in the outline, yet tied


def test_a_pad_tied_only_by_its_surface_pour_gets_a_track_to_an_existing_via(tmp_path):
    """W16 fragile pads (pourtie state `pour`): no via site of their own, the pour joins them to a via; link_pads adds the track."""
    src = _sealed(str(tmp_path / "in.kicad_pcb"))
    b = pcbnew.LoadBoard(src)
    for z in b.Zones():                                  # the F.Cu pour now reaches the via: the pad is tied, by the pour alone
        if z.GetLayerName() == "F.Cu":
            z.Outline().RemoveAllContours()
            z.Outline().NewOutline()
            for x, y in ((8, 8), (15, 8), (15, 12), (8, 12)):
                z.Outline().Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    b.Save(src)
    assert not [r for r in H.open_nets(src)["connections"] if r["net"] == "GND"]
    out = str(tmp_path / "out.kicad_pcb")
    rows = PI.link_pads(src, out, [{"net": "GND", "ref": "", "pad": "1", "at": [10.0, 10.0]}], _cons(tmp_path))
    assert rows[0]["state"] == "link" and os.path.exists(out), rows
    M = H._model(out)
    isl, omap = M.islands("GND", fills=False)            # pad and via are one island with the pours taken away
    pad = next(o for o in M.obs if o.kind == "pad" and o.net == "GND")
    via = next(o for o in M.obs if o.kind == "via" and o.net == "GND")
    assert omap[pad.id] == omap[via.id] and H.open_nets(out)["open"] == H.open_nets(src)["open"]
