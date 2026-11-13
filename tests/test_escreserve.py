"""Escape reservations in the legaliser: a part crowding a fine-pitch / multi-row part is pushed out of the reserved escape area."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import pcbgen as G
from fluxplace import escreserve as ES
from fluxplace import geom as GM
from fluxplace import legalise as LG
from fluxplace import routediag as RD

CAP = dict(court=(-0.9, -0.45, 0.9, 0.45))


def conn(ref, x, y, n=12, pitch=0.5, rows=1, **kw):
    """pads on the bottom edge of the body (rows of `n`), signals S<i>."""
    pads = [G.pad(f"{r}{i + 1}", (i - (n - 1) / 2) * pitch, 1.3 - 0.9 * r, f"S{ref}{r}{i}", 0.25, 0.7) for r in range(rows) for i in range(n)]
    return G.fp(ref, x, y, pads, court=(-n * pitch / 2 - 0.5, -1.5, n * pitch / 2 + 0.5, 1.7), **kw)


def load(tmp, body):
    return RD.load(G.board(tmp, body))


def test_part_crowding_a_fine_pitch_connector_leaves_the_first_escape_row(tmp_path):
    B = load(tmp_path, conn("J1", 50, 40, locked=True) + G.two("C1", 50, 42.3, "A", "B", **CAP))
    rsv = ES.for_legalise(B, {}, None, movable={"C1"})
    hard = [r for r in rsv["rects"] if r["hard"] and r["owner"] == "J1"]
    assert len(hard) == 1 and hard[0]["rect"][1] == B["parts"]["J1"]["court"][3]            # the band starts at the pad side of the body
    assert ES.crowding(B, rsv) == [("C1", "HARD first escape row of J1")]
    tg = {"C1": (50, 42.3, 0.0)}
    plain, _ = LG.legalise(B, tg, fixed={"J1"})
    assert plain["C1"][:2] == (50, 42.3)                           # legal without the reservation: nothing moved it
    res, rep = LG.legalise(B, tg, fixed={"J1"}, reserve=rsv)
    court = GM.moved(B["parts"]["C1"], *res["C1"])[1]
    assert not GM.overlap(court, hard[0]["rect"])
    assert court[1] >= hard[0]["rect"][3] - 1e-6                   # pushed straight out, below the band
    assert not rep["in_keepout"] and rep["reserved"]["hard"] == 1


def test_soft_rows_of_a_multi_row_part_cost_a_move_but_do_not_fail(tmp_path):
    B = load(tmp_path, conn("J1", 50, 40, n=10, pitch=1.0, rows=2, locked=True) + G.two("C1", 50, 43.4, "A", "B", **CAP))
    rsv = ES.for_legalise(B, {}, None, movable={"C1"})
    soft = [r for r in rsv["rects"] if not r["hard"] and r["owner"] == "J1" and GM.overlap(B["parts"]["C1"]["court"], r["rect"])]
    assert len(soft) == 1 and not any(r["hard"] and GM.overlap(B["parts"]["C1"]["court"], r["rect"]) for r in rsv["rects"])
    res, rep = LG.legalise(B, {"C1": (50, 43.4, 0.0)}, fixed={"J1"}, reserve=rsv)
    assert rep["reserved"]["moved"] == ["C1"]
    assert not GM.overlap(GM.moved(B["parts"]["C1"], *res["C1"])[1], soft[0]["rect"])
    # no seat within the cost: the part stays, the legaliser does not fail
    far = dict(rsv, rects=[dict(r, cost=0.3) for r in rsv["rects"]])
    res2, rep2 = LG.legalise(B, {"C1": (50, 43.4, 0.0)}, fixed={"J1"}, reserve=far)
    assert rep2["reserved"]["left"] == ["C1"] and res2["C1"][:2] == (50, 43.4)


def test_movable_fine_pitch_part_carries_its_escape_row(tmp_path):
    B = load(tmp_path, conn("J1", 50, 40) + G.two("C1", 50, 42.3, "A", "B", locked=True, **CAP))
    rsv = ES.for_legalise(B, {}, None, movable={"J1"})
    assert rsv["inflate"]["J1"][3] > 0.8 and not [r for r in rsv["rects"] if r["owner"] == "J1"]
    res, _ = LG.legalise(B, {"J1": (50, 40, 0.0)}, fixed={"C1"}, reserve=rsv)
    court = GM.moved(B["parts"]["J1"], *res["J1"])[1]
    assert B["parts"]["C1"]["court"][1] - court[3] >= rsv["inflate"]["J1"][3] - 0.05       # the connector moved away from the cap


def test_pair_corridor_becomes_a_soft_reservation(tmp_path):
    cor = [{"layer": "F.Cu", "pts": [[40, 50], [50, 50], [55, 55]], "width": 1.0}, {"layer": "In1.Cu", "pts": [[0, 0], [5, 0]], "width": 1.0}]
    B = load(tmp_path, conn("J1", 20, 20, locked=True) + G.two("C1", 45, 50.2, "A", "B", **CAP))
    rsv = ES.for_legalise(B, {}, None, movable={"C1"}, corridors=cor, pins=False)
    pc = [r for r in rsv["rects"] if r["why"] == "pair corridor"]
    assert pc and all(r["sides"] == {"F"} and not r["hard"] for r in pc) and len(pc) > 2       # the diagonal is cut into steps; inner layers reserve nothing
    res, rep = LG.legalise(B, {"C1": (45, 50.2, 0.0)}, fixed={"J1"}, reserve=rsv)
    assert rep["reserved"]["moved"] == ["C1"]
    assert not any(GM.overlap(GM.moved(B["parts"]["C1"], *res["C1"])[1], r["rect"]) for r in pc)


def test_reserve_off_in_constraints(tmp_path):
    B = load(tmp_path, conn("J1", 50, 40, locked=True) + G.two("C1", 50, 42.3, "A", "B", **CAP))
    assert ES.for_legalise(B, {"escape": {"reserve": False}}, None, movable={"C1"}) == {"rects": [], "inflate": {}}


def test_intent_region_and_escape_band_apply_in_the_same_legalise_call(tmp_path):
    """e2e-intent's regions/held parts and e2e-pairs' `reserve=` are both live in one pass: a part is kept out of both."""
    B = load(tmp_path, conn("J1", 50, 40, locked=True) + G.two("C1", 50, 42.3, "A", "B", **CAP))
    rsv = ES.for_legalise(B, {}, None, movable={"C1"})
    band = [r for r in rsv["rects"] if r["hard"] and r["owner"] == "J1"][0]["rect"]
    reg = [44, band[3], 56, band[3] + 3.0]                      # a keep-out region right below the reserved band
    cons = {"intent": {"region": [{"name": "noc", "rect": reg, "keep": "out", "refs": ["C1"]}]}}
    res, rep = LG.legalise(B, {"C1": (50, 42.3, 0.0)}, fixed={"J1"}, cons=cons, reserve=rsv)
    court = GM.moved(B["parts"]["C1"], *res["C1"])[1]
    assert not GM.overlap(court, band) and not GM.overlap(court, tuple(reg)), (res["C1"], band, reg)
    assert rep["in_keepout"] == []
    only_reserve, _ = LG.legalise(B, {"C1": (50, 42.3, 0.0)}, fixed={"J1"}, reserve=rsv)
    assert GM.overlap(GM.moved(B["parts"]["C1"], *only_reserve["C1"])[1], tuple(reg))      # the region is what moved it further
