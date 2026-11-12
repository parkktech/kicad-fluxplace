import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from fluxplace import flip as FL, routediag as RD, geom as GM, refine as RF
import pcbgen as g

pcbnew_ok = FL.available()
KICAD_HEAD = ('(kicad_pcb (version 20260206) (generator "pcbnew") (generator_version "10.0")\n(general (thickness 1.6))\n(paper "A4")\n'
              '(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (9 "F.Adhes" user "F.Adhesive") (11 "B.Adhes" user "B.Adhesive") '
              '(13 "F.Paste" user) (15 "B.Paste" user) (5 "F.SilkS" user "F.Silkscreen") (7 "B.SilkS" user "B.Silkscreen") '
              '(1 "F.Mask" user) (3 "B.Mask" user) (25 "Edge.Cuts" user) (31 "F.CrtYd" user "F.Courtyard") (29 "B.CrtYd" user "B.Courtyard") '
              '(35 "F.Fab" user) (33 "B.Fab" user))\n')


def test_model_flip_is_an_involution_and_mirrors_pads(tmp_path):
    B = RD.load(g.board(tmp_path, g.fp("U1", 20, 20, [g.pad("1", -1.5, 0.5, "A"), g.pad("2", 1.0, -0.5, "B")], angle=90.0,
                                       court=(-2, -1, 2, 1))))
    p = B["parts"]["U1"]
    x0 = [(q["x"], q["y"]) for q in p["pads"]]
    c0, a0 = p["court"], p["angle"]
    GM.flip(p)
    assert p["side"] == "B.Cu" and p["angle"] == 90.0 and all(q["layers"] == {"B.Cu"} for q in p["pads"])
    assert [round(q["x"], 4) for q in p["pads"]] == [round(40 - x, 4) for x, _ in x0]
    GM.flip(p)
    assert p["side"] == "F.Cu" and [(round(q["x"], 4), round(q["y"], 4)) for q in p["pads"]] == [(round(x, 4), round(y, 4)) for x, y in x0]
    assert p["court"] == pytest.approx(c0) and p["angle"] == a0


def test_through_hole_cannot_flip(tmp_path):
    B = RD.load(g.board(tmp_path, g.fp("J1", 20, 20, [g.pad("1", 0, 0, "A", typ="thru_hole")])))
    with pytest.raises(ValueError):
        GM.flip(B["parts"]["J1"])


@pytest.mark.skipif(not pcbnew_ok, reason="no pcbnew under /usr/bin/python3")
def test_flip_through_pcbnew_matches_the_model(tmp_path):
    body = (g.fp("U1", 20, 20, [g.pad("1", -1.5, 0.5, "A"), g.pad("2", 1.0, -0.5, "B")], angle=90.0, name="Pkg", court=(-2, -1, 2, 1))
            + g.two("R1", 40, 30, "A", "B", name="R0603", court=(-1, -0.5, 1, 0.5)) + g.two("R2", 60, 30, "A", "B", name="R0603", court=(-1, -0.5, 1, 0.5)))
    src = g.board(tmp_path, body)
    txt = open(src).read().replace(g.HEAD, KICAD_HEAD)           # pcbnew wants the real layer table
    open(src, "w").write(txt)
    B = RD.load(src)
    dst = str(tmp_path / "o.kicad_pcb")
    facts = FL.flip_file(src, dst, ["U1", "R1"])
    assert FL.verify(B, dst, ["U1", "R1"], facts) == []
    B2 = RD.load(dst)
    assert B2["parts"]["U1"]["side"] == "B.Cu" and B2["parts"]["R1"]["side"] == "B.Cu" and B2["parts"]["R2"]["side"] == "F.Cu"
    assert B2["parts"]["R2"]["pos"] == B["parts"]["R2"]["pos"]


def test_refine_flip_needs_a_flip_cost(tmp_path):
    b = g.ic("U1", 20, 20, ["N1", "a", "b", "c", "d", "e"]) + g.two("C1", 24, 20, "N1", "GND") + g.two("C2", 30, 20, "N1", "GND")
    B = RD.load(g.board(tmp_path, b))
    assert RF.Refiner(B, {}, None).flip_cost is None
    B = RD.load(g.board(tmp_path, b))
    assert RF.Refiner(B, {"placement": {"side": {"flip_cost": 12}}}, None).flip_cost == 12.0
