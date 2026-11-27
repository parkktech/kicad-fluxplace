"""w83-placegate: a placement stage never hands on a collision-gate FAIL: a movable offender is legalised, otherwise the stage fails naming it."""
import os, sys
from types import SimpleNamespace
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
from fluxplace import pipeline as PL, routediag as RD, legality as LGL
from test_collisions import FP, P, KEEPOUT


def _ctx(tmp_path, cons=None):
    def d(n, stage):
        p = tmp_path / f"{n}-{stage}"
        p.mkdir(exist_ok=True)
        return str(p)
    return SimpleNamespace(cons=cons or {}, a=SimpleNamespace(waive=[]), dir=d)


def _board(tmp_path, extra=""):
    owner = FP("J1", 30, 30, [P("1", 3, 0, "A")], extra=KEEPOUT)
    r = FP("R1", 30.5, 30.5, [P("1", 0, 0, "B")])           # pad inside J1's rule area
    return G.board(tmp_path, owner + r + extra)


def test_movable_offender_is_legalised(tmp_path):
    b = _board(tmp_path)
    res = PL._placement_collision_gate(_ctx(tmp_path), 1, "floorplan", {"verdict": "pass", "board": b, "line": "floorplan: ok"})
    assert res["verdict"] == "pass" and res["board"] != b and "legalised R1" in res["line"]
    B = RD.load(res["board"])
    assert LGL.collision_gate(B)[0]
    x0, y0, x1, y1 = B["parts"]["R1"]["court"]                  # keeps [placement] edge_clearance_mm (0.3) from J1's rule area (29..31)
    assert max(29 - x1, x0 - 31, 29 - y1, y0 - 31) >= 0.3 - 1e-6


def test_unmovable_offender_fails_the_stage_naming_it(tmp_path):
    b = _board(tmp_path)
    cons = {"placement": {"fixed": ["R1"]}}
    res = PL._placement_collision_gate(_ctx(tmp_path, cons), 1, "placetour", {"verdict": "pass", "board": b, "line": "x"})
    assert res["verdict"] == "fail" and "1 FAIL" in res["line"] and any("R1" in l for l in res["next"])


def test_other_stages_and_clean_boards_untouched(tmp_path):
    b = _board(tmp_path)
    r0 = {"verdict": "pass", "board": b, "line": "x"}
    assert PL._placement_collision_gate(_ctx(tmp_path), 1, "fanout", r0) == r0
