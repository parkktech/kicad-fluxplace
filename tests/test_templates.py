import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import templates as TP, routediag as RD, legality as LGL, routable as RB, netclass as NC, geom as GM
import pcbgen as g

CRT = (-4, -2.5, 4, 2.5)


def module(ref, x, y, net):
    return g.fp(ref, x, y, [g.pad("1", -3.5, 0, net), g.pad("2", 3.5, 0, "")], court=CRT)


def comb(ref, x, y, a, b, out):
    return g.fp(ref, x, y, [g.pad("1", -0.5, -0.5, a), g.pad("2", -0.5, 0.5, b), g.pad("3", 0.5, 0, out)], court=(-1, -1, 1, 1))


def tree_board(tmp_path):
    b = module("U1", 20, 10, "L1_RF") + module("U2", 75, 12, "L2_RF") + module("U3", 22, 70, "L3_RF") + module("U4", 80, 68, "L4_RF")
    b += comb("C1", 60, 40, "L1_RF", "L2_RF", "G1") + comb("C2", 30, 45, "L3_RF", "L4_RF", "G2") + comb("C3", 50, 20, "G1", "G2", "ROOT")
    b += g.fp("R9", 45, 60, [g.pad("1", -0.5, 0, "L1_RF"), g.pad("2", 0.5, 0, "L2_RF")], court=(-1, -0.6, 1, 0.6))
    b += g.fp("U0", 5, 40, [g.pad("1", 0, 0, "ROOT"), g.pad("2", 0, 1, "ANT")], court=(-1.5, -1.5, 1.5, 2.5))
    return g.board(tmp_path, b)


CONS = {"placement": {"fixed": ["U0"], "template": {"t": {
    "kind": "tree", "leaves": ["U1", "U2", "U3", "U4"], "combiners": ["C1", "C2", "C3"], "root": "U0"}}}}


def test_tree_template_brings_the_rf_tree_together_legally(tmp_path):
    B = RD.load(tree_board(tmp_path))
    cls = NC.classify(B, CONS)
    assert cls["L1_RF"]["class"] == "rf"
    T = TP.load(B, CONS)
    L = LGL.Legal(B, CONS, {"U0"})
    base = LGL.count(L.issues())
    mv, rep = TP.place(B, CONS, None, T[0], cls, {"U0"}, legal=L)
    assert "R9" in rep["passengers"] and not rep["unplaced"]
    assert LGL.count(LGL.Legal(B, CONS, {"U0"}).issues()) == base
    for n in ("L1_RF", "L2_RF", "L3_RF", "L4_RF"):
        assert rep["after"][n] < 0.4 * rep["before"][n] and rep["after"][n] < 16.0, (n, rep["after"])
    assert rep["weighted_after"] < 0.3 * rep["weighted_before"]
    assert "U0" not in mv                              # the root stays


def test_tree_parents_pair_leaves_then_combiners():
    p = TP._tree_parents(["a", "b", "c", "d"], ["x", "y", "z"], "R")
    assert p == {"a": "x", "b": "x", "c": "y", "d": "y", "x": "z", "y": "z", "z": "R"}


def test_chain_template_is_monotonic_between_fixed_ends(tmp_path):
    b = g.fp("A", 10, 10, [g.pad("1", 0, 0, "n1")], locked=True, court=(-1, -1, 1, 1))
    b += g.fp("D", 90, 10, [g.pad("1", 0, 0, "n3")], locked=True, court=(-1, -1, 1, 1))
    b += g.two("B", 60, 70, "n1", "n2", court=(-1.5, -1, 1.5, 1)) + g.two("C", 25, 60, "n2", "n3", court=(-1.5, -1, 1.5, 1))
    B = RD.load(g.board(tmp_path, b))
    cons = {"placement": {"template": {"line": {"kind": "chain", "refs": ["A", "B", "C", "D"]}}}}
    T = TP.load(B, cons)
    mv, rep = TP.place(B, cons, None, T[0], fixed={"A", "D"})
    assert rep["monotonic"] and rep["weighted_after"] < 0.4 * rep["weighted_before"]
    P = B["parts"]
    assert P["A"]["pos"][0] < P["B"]["pos"][0] < P["C"]["pos"][0] < P["D"]["pos"][0]
    assert LGL.count(LGL.Legal(B, cons, {"A", "D"}).issues()) == 0


def test_unknown_part_is_an_error(tmp_path):
    B = RD.load(tree_board(tmp_path))
    try:
        TP.load(B, {"placement": {"template": {"x": {"kind": "chain", "refs": ["U1", "NOPE"]}}}})
    except ValueError as e:
        assert "NOPE" in str(e)
    else:
        raise AssertionError


def test_placer_keeps_a_templated_group_rigid(tmp_path):
    p = tree_board(tmp_path)
    B = RD.load(p)
    pl = RB.Placer(B, CONS, None, log=lambda m: None)
    pl.run(rounds=2)
    grp = pl.groups["t"]
    assert "U0" not in pl.rigid and "C3" in grp and "R9" in grp
    # the group is fixed as a whole (it contains the fixed root): nothing in it moved after the template
    P = B["parts"]
    rep = pl.reports[0]
    assert all(rep["after"][n] < 16.0 for n in ("L1_RF", "L2_RF", "L3_RF", "L4_RF"))
    NCls = NC.classify(B, CONS)
    assert LGL.count(LGL.Legal(B, CONS, pl.fixed).issues()) == 0
