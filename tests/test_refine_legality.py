"""Legality verdict == KiCad DRC for courtyards on both sides and pad-to-pad shorts; atomic templates;
keep-out sides apply to the body side only."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import legality as LGL, routediag as RD, templates as TP, geom as GM, refine as RF
import pcbgen as g


def edge_launch(ref, x, y):
    """SMA edge launch: pads on F.Cu AND B.Cu, courtyard on both sides, footprint on F."""
    pads = ['(pad "1" smd rect (at -2 0 0) (size 1.3 3) (layers "F.Cu" "B.Cu") (net "RF"))',
            '(pad "2" smd rect (at 2 0 0) (size 1.3 3) (layers "F.Cu" "B.Cu") (net "GND"))']
    cy = "".join(f'(fp_rect (start -3.5 -4) (end 3.5 4) (stroke (width 0.05) (type solid)) (layer "{s}.CrtYd"))'
                 for s in "FB")
    return (f'(footprint "SMA" (layer "F.Cu") (at {x} {y} 0) (property "Reference" "{ref}") {cy} '
            + " ".join(pads) + ")\n")


def cap(ref, x, y, a, b, side="B", court=(-1, -0.6, 1, 0.6)):
    return g.fp(ref, x, y, [g.pad("1", -0.5, 0, a, 0.5, 0.5), g.pad("2", 0.5, 0, b, 0.5, 0.5)], side=side, court=court)


def test_cap_on_the_back_of_an_edge_connector_is_illegal(tmp_path):
    B = RD.load(g.board(tmp_path, edge_launch("J204", 50, 40) + cap("C221", 20, 20, "RF", "X")))
    assert not LGL.Legal(B, {}).issues()["overlap"]
    GM.place(B["parts"]["C221"], 51, 40, 0.0)            # onto the connector, other side
    r = LGL.Legal(B, {}).issues()
    assert r["overlap"] == ["C221/J204"]
    assert r["pad_short"], r                              # C221 pad 2 (net X) sits on J204 GND
    assert LGL.Legal(B, {}).issues() == LGL.Legal(B, {}).issues()


def test_pad_short_needs_a_shared_layer_and_different_nets(tmp_path):
    body = (cap("C1", 20, 20, "A", "B", side="F", court=None) + cap("C2", 20.5, 20, "C", "D", side="B", court=None)
            + cap("C3", 40, 20, "A", "B", side="F", court=None) + cap("C4", 40.5, 20, "B", "C", side="F", court=None))
    B = RD.load(g.board(tmp_path, body))
    r = LGL.Legal(B, {}).issues()["pad_short"]
    assert r == ["C3.1/C4.1", "C3.2/C4.2"], r       # C3.2/C4.1 touch but share net B; C1/C2 are on opposite layers


def test_refine_move_onto_a_connector_is_refused_and_audit_reverts(tmp_path):
    B = RD.load(g.board(tmp_path, edge_launch("J204", 50, 40) + cap("C221", 20, 20, "RF", "X")
                        + cap("C9", 70, 60, "X", "RF", side="F")))
    cons = {"placement": {"fixed": ["J204"]}}
    R = RF.Refiner(B, cons)
    R._apply({"C221": (51, 40, 0.0)})
    assert not R._legal(["C221"])
    # the final audit reverts it even if a bug let it through
    fixed = {"J204"}
    org = {r: (p["pos"][0], p["pos"][1], p["angle"], p["side"]) for r, p in B["parts"].items()}
    org["C221"] = (20, 20, 0.0, "B.Cu")
    rev, whole = LGL.self_audit(B, cons, fixed, set(), org)
    assert rev == ["C221"] and not whole and B["parts"]["C221"]["pos"] == (20, 20)
    assert not LGL.Legal(B, cons).issues()["pad_short"]


def _chain(tmp_path):
    b = g.fp("A", 10, 10, [g.pad("1", 0, 0, "n1")], locked=True, court=(-1, -1, 1, 1))
    b += g.fp("D", 90, 10, [g.pad("1", 0, 0, "n3")], locked=True, court=(-1, -1, 1, 1))
    b += g.two("B", 30, 12, "n1", "n2", court=(-1.5, -1, 1.5, 1)) + g.two("C", 60, 12, "n2", "n3", court=(-1.5, -1, 1.5, 1))
    cons = {"placement": {"template": {"line": {"kind": "chain", "refs": ["A", "B", "C", "D"]}}}}
    return RD.load(g.board(tmp_path, b)), cons


def test_template_without_a_legal_seat_is_rejected_atomically(tmp_path, monkeypatch):
    B, cons = _chain(tmp_path)
    T = TP.load(B, cons)
    before = {r: dict(pos=p["pos"], angle=p["angle"]) for r, p in B["parts"].items()}
    monkeypatch.setattr(TP, "seat", lambda *a, **k: None)
    mv, rep = TP.place(B, cons, None, T[0], fixed={"A", "D"})
    assert mv == {} and rep["rejected"].startswith("template rejected") and "no legal seat" in rep["rejected"]
    assert all(B["parts"][r]["pos"] == v["pos"] and B["parts"][r]["angle"] == v["angle"] for r, v in before.items())


def test_template_that_lengthens_a_net_is_rejected_with_numbers(tmp_path, monkeypatch):
    B, cons = _chain(tmp_path)
    T = TP.load(B, cons)
    monkeypatch.setattr(TP, "seat", lambda B_, legal, cost, ref, tx, ty, **k: (80.0, 70.0, 0.0))
    mv, rep = TP.place(B, cons, None, T[0], fixed={"A", "D"})
    assert mv == {} and "worst net" in rep["rejected"] and "mm" in rep["rejected"]
    assert B["parts"]["B"]["pos"] == (30, 12)


def test_template_that_helps_still_applies(tmp_path):
    B, cons = _chain(tmp_path)
    B2 = B
    P = B2["parts"]
    GM.place(P["B"], 30, 60, 0.0)                         # input is bad: B far off the line
    T = TP.load(B2, cons)
    mv, rep = TP.place(B2, cons, None, T[0], fixed={"A", "D"})
    assert not rep.get("rejected") and "B" in mv


def thr_header(ref, x, y):
    pads = ['(pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1) (layers "*.Cu") (net "N"))',
            '(pad "2" thru_hole circle (at 2.54 0) (size 1.7 1.7) (drill 1) (layers "*.Cu") (net "M"))']
    return (f'(footprint "HDR" (layer "F.Cu") (at {x} {y} 0) (property "Reference" "{ref}") '
            '(fp_rect (start -1.5 -1.5) (end 4 1.5) (stroke (width 0.05) (type solid)) (layer "F.CrtYd")) '
            + " ".join(pads) + ")\n")


def hole(ref, x, y):
    return (f'(footprint "MountingHole" (layer "F.Cu") (at {x} {y} 0) (property "Reference" "{ref}") '
            '(fp_circle (center 0 0) (end 2 0) (stroke (width 0.05) (type solid)) (layer "F.CrtYd")) '
            '(pad "" np_thru_hole circle (at 0 0) (size 3 3) (drill 3) (layers "*.Cu" "*.Mask")))\n')


def test_back_side_keepout_ignores_front_through_hole_bodies_and_mounting_holes(tmp_path):
    body = (thr_header("J7", 30, 30) + hole("MK1", 36, 30) + cap("C1", 32, 30, "A", "B", side="B")
            + cap("C2", 33, 33, "A", "B", side="F"))
    cons = {"placement": {"keepout": {"under": {"rect": [20, 20, 45, 40], "side": "B", "reason": "x"}}}}
    B = RD.load(g.board(tmp_path, body))
    r = LGL.Legal(B, cons).issues()["forbidden"]
    assert r == ["C1 in under"], r
    # a front keep-out flags the header body but still never the hole
    cons["placement"]["keepout"]["under"]["side"] = "F"
    r = LGL.Legal(B, cons).issues()["forbidden"]
    assert "J7 in under" in r and "C2 in under" in r and not any(x.startswith("MK1") for x in r)
