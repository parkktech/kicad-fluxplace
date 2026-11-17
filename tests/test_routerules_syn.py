"""Route-rules proofs on synthetic boards: same-part fine-pitch pads, the router keeping the floor, the collision gate."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import routefr as FR  # noqa: E402
from fluxplace import legality as LGL  # noqa: E402
from fluxplace import routediag as RD  # noqa: E402

try:
    import pcbnew as P
    HAVE = True
except ImportError:
    HAVE = False
need_pcbnew = pytest.mark.skipif(not HAVE, reason="pcbnew not available")
FLOOR = 0.127
PRO = {"meta": {"version": 1}, "net_settings": {"classes": [{"name": "Default", "clearance": FLOOR, "track_width": 0.15,
       "via_diameter": 0.45, "via_drill": 0.2}], "netclass_patterns": []}}


def _board(path, pads=(), tracks=(), vias=()):
    """pads: (ref, x, y, net); tracks: (x1, y1, x2, y2, net); vias: (x, y, net).  Square outline 20 x 12 mm."""
    b = P.BOARD()
    for n in sorted({p[3] for p in pads} | {t[4] for t in tracks} | {v[2] for v in vias}):
        b.Add(P.NETINFO_ITEM(b, n))
    e = [(0, 0), (20, 0), (20, 12), (0, 12)]
    for i in range(4):
        s = P.PCB_SHAPE(b); s.SetShape(P.SHAPE_T_SEGMENT); s.SetLayer(P.Edge_Cuts); s.SetWidth(P.FromMM(0.1))
        s.SetStart(P.VECTOR2I(P.FromMM(e[i][0]), P.FromMM(e[i][1]))); s.SetEnd(P.VECTOR2I(P.FromMM(e[(i + 1) % 4][0]), P.FromMM(e[(i + 1) % 4][1])))
        b.Add(s)
    fps = {}
    for n, (ref, x, y, net) in enumerate(pads, 1):                # one footprint per reference, one pad per entry
        if ref not in fps:
            fps[ref] = P.FOOTPRINT(b); fps[ref].SetReference(ref); fps[ref].SetPosition(P.VECTOR2I(P.FromMM(x), P.FromMM(y)))
        f = fps[ref]
        p = P.PAD(f); p.SetNumber(str(n)); p.SetAttribute(P.PAD_ATTRIB_SMD); p.SetShape(P.PAD_SHAPE_RECT)
        p.SetSize(P.VECTOR2I(P.FromMM(0.25), P.FromMM(0.25))); p.SetPosition(P.VECTOR2I(P.FromMM(x), P.FromMM(y)))
        ls = P.LSET(); ls.AddLayer(P.F_Cu); p.SetLayerSet(ls); p.SetNet(b.FindNet(net)); f.Add(p)
    for f in fps.values():
        b.Add(f)
    for x1, y1, x2, y2, net in tracks:
        t = P.PCB_TRACK(b); t.SetStart(P.VECTOR2I(P.FromMM(x1), P.FromMM(y1))); t.SetEnd(P.VECTOR2I(P.FromMM(x2), P.FromMM(y2)))
        t.SetWidth(P.FromMM(0.15)); t.SetLayer(P.F_Cu); t.SetNet(b.FindNet(net)); b.Add(t)
    for x, y, net in vias:
        v = P.PCB_VIA(b); v.SetPosition(P.VECTOR2I(P.FromMM(x), P.FromMM(y))); v.SetWidth(P.FromMM(0.45)); v.SetDrill(P.FromMM(0.2))
        v.SetNet(b.FindNet(net)); v.SetLayerPair(P.F_Cu, P.B_Cu); b.Add(v)
    P.SaveBoard(path, b)
    json.dump(PRO, open(path[:-len(".kicad_pcb")] + ".kicad_pro", "w"))
    return path


@need_pcbnew
def test_open_board_project_via_floor_uses_shared_resolver():
    from fluxplace import routerules as RR
    b = P.BOARD()
    ds = b.GetDesignSettings()
    ds.m_ViasMinSize = P.FromMM(0.61)
    ds.m_MinThroughDrill = P.FromMM(0.31)
    r = RR.rules({"route": {"min_via_mm": 0.62, "min_drill_mm": 0.32}}, b)
    assert (r["via"], r["drill"]) == (0.62, 0.32)


@need_pcbnew
def test_same_part_fine_pitch_pads_are_no_load_violation(tmp_path):
    """(i) two pads of ONE part at a 0.10 mm gap (closer than the floor): the smd_smd rule stays as KiCad wrote it, so
    Freerouting's load check reports nothing for them."""
    b = _board(str(tmp_path / "fp.kicad_pcb"), pads=[("U1", 5, 6, "NA"), ("U1", 5.35, 6, "NB")])
    loc = FR.locate(None, None)
    S = FR.settings(None, threads=1)
    r = FR.load_violations(b, str(tmp_path / "w"), loc, S, planes=(), floor_track=FLOOR, floor_clearance=FLOOR, tmo_s=90)
    assert "error" not in r and r["pin_to_pin"] == 0 and r["fixed_trace_via"] == 0, r


@need_pcbnew
def test_pads_of_different_parts_closer_than_floor_are_a_collision(tmp_path):
    """(iii) two pads of DIFFERENT parts at a 0.10 mm gap: the DSN (smd_smd) does not catch them; fluxplace's collision gate
    must, against the floor."""
    b = _board(str(tmp_path / "cp.kicad_pcb"), pads=[("U1", 5, 6, "NA"), ("U2", 5.35, 6, "NB")])
    B = RD.load(b)
    f = LGL.collisions(B, {"route": {"min_clearance_mm": FLOOR}})
    assert any(x["code"] == "COLLISION" and {"U1", "U2"} <= set(x.get("refs", [])) for x in f), f
    far = _board(str(tmp_path / "far.kicad_pcb"), pads=[("U1", 5, 6, "NA"), ("U2", 5.6, 6, "NB")])      # 0.35 mm gap
    far_f = LGL.collisions(RD.load(far), {"route": {"min_clearance_mm": FLOOR}})
    assert not any("inside the clearance" in x["msg"] for x in far_f), far_f          # courtyards are not what this checks


@need_pcbnew
def test_router_keeps_the_floor_from_pads_tracks_and_vias(tmp_path):
    """(ii) net A must cross a wall of net B pads whose gaps are narrower than track + 2 x floor: the router must not
    use them.  Asserted on the routed board: no sub-floor copper between different nets, any layer."""
    pads = [("A1", 2, 6, "NA"), ("A2", 18, 6, "NA")]
    pads += [(f"W{i}", 10, 0.5 + 0.35 * i, "NB") for i in range(1, 34)]        # wall at x=10, centres 0.35 mm apart
    b = _board(str(tmp_path / "wall.kicad_pcb"), pads=pads)
    loc = FR.locate(None, None)
    S = FR.settings(None, threads=2)
    out = str(tmp_path / "out.kicad_pcb")
    rec = FR.route_board(b, out, str(tmp_path / "fr"), loc=loc, planes=(), floor_track=FLOOR, floor_clearance=FLOOR,
                         budget_s=120, S=S, cons=None)
    assert rec.get("ok"), rec.get("error")
    cls = FR.net_classes(None, {"track": 0.15, "min_track": FLOOR, "min_clearance": FLOOR, "clearance": FLOOR}, 0.0)
    bad = FR.subfloor_copper(out, str(tmp_path / "chk"), cls, FLOOR)
    assert not bad, bad[:5]
