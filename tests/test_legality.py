import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import clusters as CL, routediag as RD, legality as LGL, geom as GM
import pcbgen as g

ROUND = ('(gr_line (start 10 0) (end 100 0) (layer "Edge.Cuts"))\n(gr_line (start 100 0) (end 100 80) (layer "Edge.Cuts"))\n'
         '(gr_line (start 100 80) (end 0 80) (layer "Edge.Cuts"))\n(gr_line (start 0 80) (end 0 10) (layer "Edge.Cuts"))\n'
         '(gr_arc (start 0 10) (mid 2.929 2.929) (end 10 0) (layer "Edge.Cuts"))\n')


def test_rounded_corner_is_outside(tmp_path):
    B = RD.load(g.board(tmp_path, g.two("C1", 20, 20, "A", "B"), edge=ROUND))
    assert GM.in_outline(B, (3, 1, 5, 3), 0.0) is False          # inside the bbox, outside the arc
    assert GM.in_outline(B, (8, 8, 10, 10), 0.0) is True
    assert GM.in_outline(B, (1, 12, 3, 14), 0.0) is True


def test_overlap_same_side_only_and_allow_list(tmp_path):
    body = g.two("C1", 20, 20, "A", "B", court=(-1, -1, 1, 1)) + g.two("C2", 20.5, 20, "A", "B", court=(-1, -1, 1, 1))
    body += g.two("C3", 20, 20, "A", "B", side="B", court=(-1, -1, 1, 1))
    B = RD.load(g.board(tmp_path, body))
    r = LGL.Legal(B, {}).issues()
    assert r["overlap"] == ["C1/C2"]
    r = LGL.Legal(B, {"placement": {"allow_overlap": [["C2", "C1"]]}}).issues()
    assert r["overlap"] == []


def test_named_keepout_side_and_no_tht(tmp_path):
    th = g.fp("J1", 40, 40, [g.pad("1", 0, 0, "A", typ="thru_hole")], court=(-1, -1, 1, 1))
    body = (g.two("C1", 20, 20, "A", "B", side="B", court=(-1, -1, 1, 1)) + g.two("C2", 20, 30, "A", "B", court=(-1, -1, 1, 1)) + th)
    cons = {"placement": {"keepout": {"mod": {"rect": [10, 10, 30, 35], "side": "B", "allow": []}},
                          "no_tht": {"nt": {"rect": [35, 35, 45, 45]}}}}
    B = RD.load(g.board(tmp_path, body))
    r = LGL.Legal(B, cons).issues()
    assert r["forbidden"] == ["C1 in mod"] and r["no_tht"] == ["J1 in nt"]
    assert any(f["code"] == "PLACEMENT_ILLEGAL" for f in LGL.findings(B, cons))


def test_edge_connector_may_overhang_by_its_mating_side(tmp_path):
    pads = [g.pad(str(i), 0, i * 1.0, "N%d" % i) for i in range(1, 4)]
    court = (-3, -1, 4, 5)            # sticks out on +x
    body = g.fp("J7", 98, 40, pads, name="Conn_Horizontal", court=court)
    B = RD.load(g.board(tmp_path, body))
    L = LGL.Legal(B, {"placement": {"connectors": {"J7": {"opening": "+x"}}}})
    assert "J7" in L.edge and L.issues()["outside"] == []
    L2 = LGL.Legal(B, {})
    L2.edge = {}
    assert L2.issues()["outside"] == ["J7"]


def _board_with_keepout(tmp_path):
    b = g.ic("U410", 40, 40, ["VDD", "A1", "D410", "GND", "A2", "A3"], pitch=0.5)
    for ref, x, y in (("C411", 70, 70), ("C412", 75, 10)):
        b += g.two(ref, x, y, "D410", "GND", side="B", court=(-1, -0.6, 1, 0.6))
    return g.board(tmp_path, b)


def test_reseat_respects_side_keepout_and_reports_what_it_leaves(tmp_path):
    path = _board_with_keepout(tmp_path)
    cons = {"placement": {"keepout": {"mod": {"rect": [30, 30, 50, 50], "side": "B"}}}}
    B = RD.load(path)
    cl, _ = CL.build(B, cons)
    L = LGL.Legal(B, cons)
    left = []
    mv = CL.reseat(B, cl, bounds=(0, 0, 100, 80), legal=L, strict=True, left=left)
    assert not mv and {r for r, _ in left} == {"C411", "C412"}       # every seat near the IC is in the keep-out
    # without the keep-out they seat beside the IC on their own side
    B = RD.load(path)
    cl, _ = CL.build(B, {})
    mv = CL.reseat(B, cl, bounds=(0, 0, 100, 80), legal=LGL.Legal(B, {}), strict=True)
    assert set(mv) == {"C411", "C412"} and LGL.count(LGL.Legal(B, {}).issues()) == 0


def test_reseat_stays_clear_of_a_through_hole_courtyard_and_the_outline_arc(tmp_path):
    b = g.ic("U410", 14, 14, ["VDD", "A1", "D410", "GND", "A2", "A3"], pitch=0.5)
    b += g.two("C411", 70, 70, "D410", "GND", side="B", court=(-1, -0.6, 1, 0.6))
    # a mounting hole (through-hole: its courtyard counts on both sides) right at the best seat
    b += g.fp("MH9", 21.5, 14, [g.pad("1", 0, 0, "", typ="np_thru_hole", w=2.0, h=2.0)], court=(-2, -2, 2, 2))
    B = RD.load(g.board(tmp_path, b, edge=ROUND))
    cl, _ = CL.build(B, {})
    L = LGL.Legal(B, {})
    CL.reseat(B, cl, bounds=(0, 0, 100, 80), legal=L)
    assert LGL.count(L.issues()) == 0
    assert B["parts"]["C411"]["pos"] != (70, 70)
