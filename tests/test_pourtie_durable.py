"""Durable pour ties (W16 C112.2 / L300.2: GND pads that hung on the F.Cu pour only were counted as tied before routing, routing then
wrapped them and put a rail under every via site - a sealed pour island at the end of close).
On a board with a plane layer the pour on a pad's own routing layer is no tie: pour-tie lays the stub + via into the plane before any
signal copper, a pad with no site is recorded `fragile` and re-checked after routing, and the placement gate names it."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import pcbnew
import pcbgen as G
from fluxplace import pourtie as PT, preroute as PR, gapmap as GP, routediag as RD
from test_pourtie import _seg, _zone, _pad_fp

FULL = [(0, 0), (40, 0), (40, 30), (0, 30)]


def board(path, novia=False, plane=True, foreign=()):
    """4 layers: GND pour on F.Cu, GND plane on In1 (whole board), one SMD GND pad at (10, 10)."""
    b = pcbnew.BOARD()
    b.SetCopperLayerCount(4)
    for x0, y0, x1, y1 in ((0, 0, 40, 0), (40, 0, 40, 30), (40, 30, 0, 30), (0, 30, 0, 0)):
        _seg(b, x0, y0, x1, y1, "Edge.Cuts")
    for n in ("GND", "SIG"):
        b.Add(pcbnew.NETINFO_ITEM(b, n))
    _pad_fp(b, "C1", 10, 10, "GND")
    _pad_fp(b, "U4", 35, 5, "SIG")
    for i, (x, y) in enumerate(foreign):
        _pad_fp(b, f"R{i}", x, y, "SIG")
    _zone(b, "GND", "F.Cu", FULL)
    if plane:
        _zone(b, "GND", "In1.Cu", FULL)
    for f in b.GetFootprints():                               # the pads as a board file has them: on F.Cu by their layer set
        for q in f.Pads():
            ls = pcbnew.LSET()
            ls.AddLayer(pcbnew.F_Cu)
            q.SetLayerSet(ls)
    for z in b.Zones():
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)       # the pour covers the pad (no thermal spokes to lose on a 0.8 mm pad)
    if novia:
        z = pcbnew.ZONE(b)
        z.SetIsRuleArea(True)
        z.SetDoNotAllowVias(True)
        z.SetDoNotAllowTracks(False)
        z.SetDoNotAllowZoneFills(False)
        z.SetLayerSet(pcbnew.LSET.AllCuMask())
        z.Outline().NewOutline()
        for x, y in ((7, 7), (13, 7), (13, 13), (7, 13)):
            z.Outline().Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
        b.Add(z)
    b.Save(path)
    return path


def _ring(b, half, layer="F.Cu", net="SIG", c=(10, 10)):
    """a closed square of `net` track round `c`: what a router leaves when it wraps a pad."""
    pts = [(c[0] - half, c[1] - half), (c[0] + half, c[1] - half), (c[0] + half, c[1] + half), (c[0] - half, c[1] + half)]
    for a, e in zip(pts, pts[1:] + pts[:1]):
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
        t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(e[0]), pcbnew.FromMM(e[1])))
        t.SetWidth(pcbnew.FromMM(0.2)); t.SetLayer(b.GetLayerID(layer))
        b.Add(t)
        t.SetNet(b.FindNet(net))


def _pad(b, ref="C1"):
    return [p for f in b.GetFootprints() for p in f.Pads() if f.GetReference() == ref][0]


def test_pad_on_a_surface_pour_gets_its_via_into_the_plane_before_routing(tmp_path):
    src = board(str(tmp_path / "a.kicad_pcb"))
    rec = PT.run(src, str(tmp_path / "a.out.kicad_pcb"), str(tmp_path))
    assert rec["nets"]["GND"]["tied_by_via"] == 1 and not rec["fragile"]        # parent: "tied already by pour", no via
    b = pcbnew.LoadBoard(str(tmp_path / "a.out.kicad_pcb"))
    via = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T]
    stub = [t for t in b.GetTracks() if t.Type() != pcbnew.PCB_VIA_T]
    assert len(via) == 1 and len(stub) == 1 and stub[0].GetEnd() == via[0].GetPosition()
    assert not _pad(b).HitTest(via[0].GetPosition(), via[0].GetWidth(pcbnew.F_Cu) // 2)   # not a via in the pad
    # routing now wraps the pad: the F.Cu pour round it is an island, and the pad is still tied (its via is inside, in the plane)
    _ring(b, 2.0)
    PT.PL.refill(b)
    solid = PT.solid_lids(b)
    assert solid == {b.GetLayerID("In1.Cu")}
    assert PT.tie_status(b, "GND", _pad(b), PT.pour_map(b)["GND"], solid)[0] == "via"
    assert PT.repair_in_memory(b, rec)["lost"] == 0


def test_the_tie_site_keeps_off_foreign_pads_and_a_stub_never_crosses_one(tmp_path):
    """ring sites east of the pad are taken by a foreign pad: the via goes to a clear side, the stub clears the pad by the rule."""
    src = board(str(tmp_path / "b.kicad_pcb"), foreign=[(11.2, 10)])
    rec = PT.run(src, str(tmp_path / "b.out.kicad_pcb"), str(tmp_path))
    assert rec["nets"]["GND"]["tied_by_via"] == 1
    b = pcbnew.LoadBoard(str(tmp_path / "b.out.kicad_pcb"))
    r0 = _pad(b, "R0")
    for t in b.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            assert not r0.HitTest(t.GetPosition(), t.GetWidth(pcbnew.F_Cu) // 2 + pcbnew.FromMM(0.15))
        else:
            assert not r0.HitTest(t.GetEnd(), t.GetWidth() // 2 + pcbnew.FromMM(0.15)) and t.GetEnd().x < pcbnew.FromMM(10.6)


def test_pad_with_no_via_site_is_fragile_and_a_sealed_island_is_found_after_routing(tmp_path):
    src = board(str(tmp_path / "c.kicad_pcb"), novia=True)
    rec = PT.run(src, str(tmp_path / "c.out.kicad_pcb"), str(tmp_path))
    assert [f["ref"] for f in rec["fragile"]] == ["C1"] and "F.Cu pour only" in rec["fragile"][0]["reason"]
    assert not rec["untied"] and rec["nets"]["GND"]["fragile"] == 1               # still connected now: not the router's job
    b = pcbnew.LoadBoard(str(tmp_path / "c.out.kicad_pcb"))
    _ring(b, 2.0)
    rep = PT.repair_in_memory(b, rec)
    assert rep["lost"] == 1 and "sealed by routing" in rep["unrepaired"][0]["reason"]


def test_board_without_a_plane_keeps_the_pour_on_the_pads_layer_as_the_tie(tmp_path):
    src = board(str(tmp_path / "d.kicad_pcb"), plane=False)
    rec = PT.run(src, str(tmp_path / "d.out.kicad_pcb"), str(tmp_path))
    assert rec["nets"]["GND"] == {"pour_layers": ["F.Cu"], "pads": 1, "tied_before": 1, "tied_by_via": 0, "untied": 0, "fragile": 0}


def test_durable_ties_can_be_switched_off(tmp_path):
    src = board(str(tmp_path / "e.kicad_pcb"))
    rec = PT.run(src, str(tmp_path / "e.out.kicad_pcb"), str(tmp_path), cons={"pours": {"durable_ties": False}})
    assert rec["nets"]["GND"]["tied_by_via"] == 0 and rec["nets"]["GND"]["tied_before"] == 1


# ---- placement gate (text boards)
PLANE = ('(zone (net "GND") (layer "In1.Cu") (hatch edge 0.5) (polygon (pts (xy 0 0) (xy 100 0) (xy 100 80) (xy 0 80))))\n')


def _gate(tmp_path, body):
    B = RD.load(G.board(tmp_path, body + PLANE))
    return [f for f in PR.run(B, {})[0] if f["code"] == "POUR_TIE_BLOCKED"]


def _box(ref, x, y, net="X"):
    return G.fp(ref, x, y, [G.pad("1", 0, 0, net, 3.0, 3.0)])


def test_placement_gate_names_a_plane_pad_with_no_via_site(tmp_path):
    crowd = _box("R1", 52.3, 40) + _box("R2", 47.7, 40) + _box("R3", 50, 42.3) + _box("R4", 50, 37.7)
    f = _gate(tmp_path, G.fp("C1", 50, 40, [G.pad("2", 0, 0, "GND", 0.6, 0.6)]) + crowd)
    assert len(f) == 1 and f[0]["level"] == "WARN" and f[0]["pins"] == ["C1.2"] and {"R1", "R2", "R3", "R4"} <= set(f[0]["refs"])
    assert GP._X["POUR_TIE_BLOCKED"] == ("pin-escape", "floorplan")


def test_placement_gate_is_silent_with_room_for_one_via_and_for_a_thermal_pad(tmp_path):
    three = _box("R1", 52.3, 40) + _box("R2", 47.7, 40) + _box("R3", 50, 42.3)          # the north side is free
    assert _gate(tmp_path, G.fp("C1", 50, 40, [G.pad("2", 0, 0, "GND", 0.6, 0.6)]) + three) == []
    crowd = three + _box("R4", 50, 36.5)
    (tmp_path / "t").mkdir()
    assert _gate(tmp_path / "t", G.fp("U1", 50, 40, [G.pad("9", 0, 0, "GND", 2.0, 2.0)]) + crowd.replace("52.3", "53").replace("47.7", "47").replace("42.3", "43")) == []


def test_placement_gate_ignores_a_board_without_a_plane(tmp_path):
    crowd = _box("R1", 52.3, 40) + _box("R2", 47.7, 40) + _box("R3", 50, 42.3) + _box("R4", 50, 37.7)
    B = RD.load(G.board(tmp_path, G.fp("C1", 50, 40, [G.pad("2", 0, 0, "GND", 0.6, 0.6)]) + crowd))
    assert PR.pour_tie_blocked(B, {}) == []
