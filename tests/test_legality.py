import math, os, sys
import pytest
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


def test_gate_fails_placement_illegal_and_allow_overlap_clears_it(tmp_path):
    from fluxplace import preroute as PR
    body = g.two("C1", 20, 20, "A", "B", court=(-2, -1, 2, 1)) + g.two("C2", 22, 20, "A", "B", court=(-2, -1, 2, 1))
    B = RD.load(g.board(tmp_path, body))
    f, card = PR.run(B, {})
    bad = [x for x in f if x["code"] == "PLACEMENT_ILLEGAL"]
    assert bad and bad[0]["level"] == "FAIL" and card["counts"]["PLACEMENT_ILLEGAL"] == 1 and card["fail"] >= 1
    f2, card2 = PR.run(B, {"placement": {"allow_overlap": [["C1", "C2"]]}})
    assert card2["counts"]["PLACEMENT_ILLEGAL"] == 0 and card2["score"] < card["score"]


# ---- placer step 5b S1: a side-aware seat
KO = {"placement": {"keepout": {"mod": {"rect": [30, 30, 50, 50], "side": "B"}}}}


def _reseat(path, cons, **kw):
    B = RD.load(path)
    cl, _ = CL.build(B, cons)
    left, refused = [], {}
    mv = CL.reseat(B, cl, bounds=(0, 0, 100, 80), legal=LGL.Legal(B, cons), strict=True, left=left, refused=refused, **kw)
    return B, mv, left, refused


def test_a_part_with_no_seat_on_its_side_is_seated_on_the_other_side_within_its_limit(tmp_path):
    path = _board_with_keepout(tmp_path)
    B, mv, left, refused = _reseat(path, KO)                          # no flips asked: left, and the refusing check is named
    assert not mv and refused["C411"].startswith("side closed: mod")
    flips = set()
    B, mv, left, refused = _reseat(path, KO, flips=flips)
    assert set(mv) == {"C411", "C412"} and flips == {"C411", "C412"} and left == []
    assert {B["parts"][r]["side"] for r in mv} == {"F.Cu"} and LGL.count(LGL.Legal(B, KO).issues()) == 0
    for r in mv:
        pin = next(p for p in B["parts"]["U410"]["pads"] if p["net"] == "D410")
        via = next(p for p in B["parts"][r]["pads"] if p["net"] == "D410")
        assert math.hypot(via["x"] - pin["x"], via["y"] - pin["y"]) <= 5.0


def test_a_through_hole_part_is_never_flipped_and_no_keepout_is_todays_result(tmp_path):
    b = g.ic("U410", 40, 40, ["VDD", "A1", "D410", "GND", "A2", "A3"], pitch=0.5)
    b += g.fp("C411", 70, 70, [g.pad("1", -1, 0, "D410", typ="thru_hole", w=1.0, h=1.0), g.pad("2", 1, 0, "GND", typ="thru_hole", w=1.0, h=1.0)],
              side="B", court=(-1.5, -0.8, 1.5, 0.8))
    path = g.board(tmp_path, b)
    flips = set()
    B, mv, left, _ = _reseat(path, KO, flips=flips)
    assert B["parts"]["C411"]["tht"] and not mv and flips == set() and [r for r, _ in left] == ["C411"]
    p2 = _board_with_keepout(tmp_path)
    B0, mv0, _l, _r = _reseat(p2, {})
    f2 = set()
    B1, mv1, _l, _r = _reseat(p2, {}, flips=f2)
    assert mv0 == mv1 and f2 == set()                                   # a part that seats on its own side is not flipped


def test_a_flipped_seat_is_written_to_the_board_as_the_model_has_it(tmp_path):
    from fluxplace import boardwrite as BW, flip as FL, modules as MD
    if not FL.available():
        pytest.skip("pcbnew not available")
    from test_flip import KICAD_HEAD                      # pcbnew wants the real layer table
    path = _board_with_keepout(tmp_path)
    txt = open(path).read().replace(g.HEAD, KICAD_HEAD)
    open(path, "w").write(txt)
    B = RD.load(path)
    M = MD.build(B, KO, None, arrange=True)
    assert M["flipped"] == ["C411", "C412"] and {B["parts"][r]["side"] for r in M["flipped"]} == {"F.Cu"}
    dst, dst2 = str(tmp_path / "f.kicad_pcb"), str(tmp_path / "w.kicad_pcb")
    FL.flip_file(path, dst, M["flipped"])
    BW.move_file(dst, dst2, {r: (*B["parts"][r]["pos"], B["parts"][r]["angle"]) for r in M["flipped"]}, post=False)
    W = RD.load(dst2)
    for r in M["flipped"]:
        assert W["parts"][r]["side"] == "F.Cu"
        for q, w in zip(B["parts"][r]["pads"], W["parts"][r]["pads"]):
            assert (q["x"], q["y"]) == pytest.approx((w["x"], w["y"]), abs=1e-3)
