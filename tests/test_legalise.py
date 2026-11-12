import os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import legalise as LG, routediag as RD, geom as GM
import pcbgen as g


def cap(ref, x, y, **kw):
    return g.two(ref, x, y, "A" + ref, "B" + ref, court=(-1.0, -0.5, 1.0, 0.5), **kw)


def load(tmp_path, body, **kw):
    return RD.load(g.board(tmp_path, body, **kw))


def test_pushes_apart_keeps_order_and_stays_inside(tmp_path):
    B = load(tmp_path, cap("C1", 10, 10) + cap("C2", 10.2, 10) + cap("C3", 10.4, 10))
    res, rep = LG.legalise(B, {"C1": (10, 10, 0), "C2": (10.2, 10, 0), "C3": (10.4, 10, 0)})
    xs = [res[r][0] for r in ("C1", "C2", "C3")]
    assert xs == sorted(xs) and rep["overlaps"] == [] and rep["outside"] == []
    P = {r: GM.moved(B["parts"][r], *res[r])[1] for r in res}
    assert not GM.overlap(P["C1"], P["C2"], -0.001) and not GM.overlap(P["C2"], P["C3"], -0.001)


def test_fixed_and_locked_never_move(tmp_path):
    B = load(tmp_path, cap("C1", 10, 10, locked=True) + cap("C2", 10.5, 10) + cap("C3", 30, 30))
    res, _ = LG.legalise(B, {"C1": (50, 50, 0), "C2": (10.5, 10, 0)}, fixed=["C3"])
    assert "C1" not in res and "C3" not in res
    assert not GM.overlap(GM.moved(B["parts"]["C2"], *res["C2"])[1], B["parts"]["C1"]["court"], -0.001)


def test_never_outside_outline_fails_loudly(tmp_path):
    B = load(tmp_path, cap("C1", 10, 10), outline=(0, 0, 100, 80))
    res, _ = LG.legalise(B, {"C1": (500, 500, 0)})           # asked for far outside: clamped in
    assert LG.check_outline(B, res) == []
    assert LG.check_outline(B, {"C1": (500, 500, 0)}) == ["C1"]
    big = g.fp("J9", 5, 5, [g.pad("1", -60, 0, "X"), g.pad("2", 60, 0, "Y")], court=(-61, -2, 61, 2))
    B2 = load(tmp_path, big, name="big.kicad_pcb", outline=(0, 0, 100, 80))
    with pytest.raises(LG.PlacementError):
        LG.legalise(B2, {"J9": (5, 5, 0)})


def test_fine_pitch_escape_allowance_and_connector_row(tmp_path):
    ic = g.ic("U1", 30, 30, list("ABCDEFGHIJ"), pitch=0.4)
    B = load(tmp_path, ic + cap("C1", 30, 30))
    p = B["parts"]["U1"]
    pads, court = GM.moved(p, 0, 0, 0)
    a = LG.allowances(p, pads, court)
    assert a[0] >= 0.6 and a[2] >= 0.6 and a[1] == 0 == a[3]     # pins on the left/right only
    # a cap right next to the IC is pushed at least the allowance away
    res, _ = LG.legalise(B, {"C1": (30 + 2.2 + 1.2, 30, 0)})
    rect = GM.moved(B["parts"]["C1"], *res["C1"])[1]
    ic_court = p["court"]
    gap = rect[0] - ic_court[2]
    assert gap >= 0.6 - 1e-6
    conn = g.fp("J1", 60, 30, [g.pad(str(i), 0, i * 0.5, "N%d" % i, 0.8, 0.3) for i in range(50)], court=(-1, -1, 1, 26))
    B2 = load(tmp_path, conn, name="c.kicad_pcb")
    pads, court = GM.moved(B2["parts"]["J1"], 0, 0, 0)
    assert max(LG.allowances(B2["parts"]["J1"], pads, court)) == 1.5


def test_keepout_rect_blocks_tht_and_rule_area_blocks_smd(tmp_path):
    zone = ('(zone (net 0) (net_name "") (layers "F.Cu") (name "ANT9_CLEARANCE") '
            '(keepout (tracks not_allowed) (vias allowed) (pads allowed)) '
            '(polygon (pts (xy 40 40) (xy 60 40) (xy 60 60) (xy 40 60))))\n')
    tht = g.fp("J2", 20, 20, [g.pad("1", 0, 0, "X", typ="thru_hole")], court=(-1, -1, 1, 1))
    B = load(tmp_path, zone + cap("C1", 50, 50) + tht)
    cons = {"keepout": [{"name": "shadow", "rect": [0, 0, 30, 30], "side": "both"}]}
    res, _ = LG.legalise(B, {"C1": (50, 50, 0), "J2": (20, 20, 0)}, cons=cons)
    c1 = GM.moved(B["parts"]["C1"], *res["C1"])[1]
    assert not GM.overlap(c1, (40, 40, 60, 60))                   # out of the rule area
    j2 = GM.moved(B["parts"]["J2"], *res["J2"])[1]
    assert not GM.overlap(j2, (0, 0, 30, 30))                      # THT out of the shadow


def test_two_sides_do_not_collide(tmp_path):
    b = cap("C1", 10, 10) + cap("C2", 10, 10, side="B")
    B = load(tmp_path, b)
    res, rep = LG.legalise(B, {"C1": (10, 10, 0), "C2": (10, 10, 0)})
    assert res["C1"][:2] == (10, 10) and res["C2"][:2] == (10, 10)


def test_hot_corridor_moves_passive_out(tmp_path):
    B = load(tmp_path, cap("C1", 20, 20))
    res, _ = LG.legalise(B, {"C1": (20, 20, 0)}, hot=[(19, 19, 21, 21)])
    assert not GM.overlap(GM.moved(B["parts"]["C1"], *res["C1"])[1], (19, 19, 21, 21))
