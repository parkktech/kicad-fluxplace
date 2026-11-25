"""PLACEGAPS phase 6: row-away (feasible, push-out, re-seat, impossible question), row-flight (push-out), dedupe (fakes: no router, no KiCad)."""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fluxplace import budget as BG, placegaps as PG, placetour as PT  # noqa: E402
from test_placegaps_core import ctx, part  # noqa: E402


def box(x, y, h=2.0):
    return part(x, y, court=(x - h, y - h, x + h, y + h))


def board(**xy):
    return {"parts": {r: box(*p) for r, p in xy.items()}, "bbox": (0, 0, 100, 100), "pads": [], "zones": [], "copper": ["F.Cu", "B.Cu"]}


def sep(name, a, b, mn):
    return {"name": name, "a": [a], "b": [b], "min_mm": mn, "hard": True}


def mk(B, cons, movable, **kw):
    return ctx(B=B, cons=cons, spec={}, rows=BG.separation_rows(B, cons), _cl=[], _intent=[], **kw)


@pytest.fixture
def mov(monkeypatch):
    holder = {"m": set()}
    monkeypatch.setattr(PT, "_movable", lambda B, cons, P: sorted(holder["m"] & set(B["parts"])))
    monkeypatch.setattr(PT, "_legal", lambda B, cons, tg, mv: {r: tuple(v) for r, v in tg.items()})
    return holder


class SimEval(PG.Evaluator):
    """Applies moves to a copy of the model and measures the separation rows again (what FileEval does on a written board)."""
    def __init__(self, B, cons):
        self.B, self.cons = B, cons

    def _ctx(self, B):
        return mk(B, self.cons, None)

    def detect(self, kinds):
        return PG.detect(self._ctx(self.B), kinds)["gaps"]

    def board(self):
        return self.B

    def trial(self, cand):
        B = copy.deepcopy(self.B)
        for r, (x, y, a) in cand["moves"].items():
            p = B["parts"][r]
            p["pos"], p["angle"], p["court"] = (x, y), a, (x - 2, y - 2, x + 2, y + 2)
        return {"gaps": PG.detect(self._ctx(B), ["row-away", "row-flight"])["gaps"], "state": {}, "refusal": None, "B": B}

    def commit(self, tr):
        self.B = tr["B"]


def test_row_to_kind_mapping():
    for r, k in (({"kind": "separation", "name": "a", "metric": "distance"}, "row-away"),
                 ({"kind": "keepout-flight", "name": "k", "metric": "crossing"}, "row-flight")):
        assert PG.row_kind(r) == k
    assert PG.row_kind({"kind": "away"}, True) == PG.row_kind({"kind": "region"}, True) == "row-away"
    assert PG.DISPATCH["row-away"] == ["feasible", "push-out", "re-seat"] and PG.DISPATCH["row-flight"] == ["push-out"]


def test_push_out_meets_the_separation(mov):
    mov["m"] = {"U2"}
    B, cons = board(U1=(20, 50), U2=(28, 50)), {"separation": [sep("gs", "U1", "U2", 10.0)]}
    c = mk(B, cons, None)
    g = PG.detect(c, ["row-away"])["gaps"][0]
    assert g["id"] == "row-away:gs|distance" and PG.RUNGS["feasible"](g, c) == [] and not c["away_impossible"]
    cands = PG.RUNGS["push-out"](g, c)
    assert len(cands) == 8 and all(list(x["moves"]) == ["U2"] for x in cands)
    assert cands[0]["moves"]["U2"][0] == pytest.approx(28 + 6.2) and cands[0]["moves"]["U2"][1] == pytest.approx(50)    # separating direction first: 10 - 4 + 0.2
    ev = SimEval(B, cons)
    res = PG.close(c, ["row-away"], ev, stage="t")
    assert res["accepted"] and not res["gaps_out"]
    assert BG.separation_rows(ev.B, cons)[0]["value"] >= 10.0


def test_re_seat_proposes_clear_seats_of_the_module(mov):
    mov["m"] = {"U2"}
    B, cons = board(U1=(20, 50), U2=(28, 50)), {"separation": [sep("gs", "U1", "U2", 10.0)]}
    c = mk(B, cons, None)
    g = PG.detect(c, ["row-away"])["gaps"][0]
    seats = PG.RUNGS["re-seat"](g, c)
    assert seats and len(seats) <= 8
    for s in seats:
        x, y, _a = s["moves"]["U2"]
        assert max(0.0, 18 - (x + 2), (x - 2) - 22) ** 2 + max(0.0, 48 - (y + 2), (y - 2) - 52) ** 2 >= 10.0 ** 2 - 1e-6


def test_two_fixed_parts_closer_than_min_is_an_owner_question_and_no_move(mov):
    mov["m"] = set()
    B, cons = board(U1=(20, 50), U2=(28, 50)), {"separation": [sep("gs", "U1", "U2", 10.0)]}
    c = mk(B, cons, None)
    g = PG.detect(c, ["row-away"])["gaps"][0]
    assert PG.RUNGS["feasible"](g, c) == [] and PG.RUNGS["push-out"](g, c) == [] and PG.RUNGS["re-seat"](g, c) == []

    class S:
        asked = []

        def ask(self, qid, topic, found, options, default, settle, apply=None):
            self.asked.append((qid, found, sorted(options)))
            return default
    c["session"] = S()
    ev = SimEval(B, cons)
    res = PG.close(c, ["row-away"], ev, stage="t")
    assert not res["accepted"] and ev.B["parts"]["U2"]["pos"] == (28, 50) and len(res["gaps_out"]) == 1
    PG.away_questions(c, res)
    q = c["session"].asked
    assert len(q) == 1 and q[0][0] == "row:gs" and q[0][2] == ["grow", "lower", "unfix", "waive"]
    assert "limit 10 mm" in q[0][1] and "4 mm now" in q[0][1] and "U1, U2" in q[0][1]
    PG.away_questions(c, {"gaps_out": []})                                      # closed later: dropped, not asked again
    assert len(q) == 1 and not c["away_impossible"]


def test_no_place_on_the_board_is_impossible_with_best_reachable_and_refusing_part(mov):
    mov["m"] = {"U2"}
    B = board(U1=(20, 50), U2=(28, 50), K1=(41, 50))
    B["bbox"] = (10, 40, 50, 60)
    B["parts"]["K1"]["court"] = (33, 40, 50, 60)                               # a fixed part on the only side with room
    cons = {"separation": [sep("gs", "U1", "U2", 10.0)]}
    c = mk(B, cons, None)
    g = PG.detect(c, ["row-away"])["gaps"][0]
    PG.RUNGS["feasible"](g, c)
    rec = c["away_impossible"][g["id"]]
    assert rec["limit"] == 10.0 and rec["refusing"] == "K1" and 7.0 <= rec["best"] < 10.0
    assert PG.RUNGS["push-out"](g, c) == [] and PG.RUNGS["re-seat"](g, c) == []
    B["bbox"] = (10, 40, 34, 60)                                               # no fixed part to blame: the board itself
    c = mk(B, cons, None)
    PG.RUNGS["feasible"](g, c)
    assert "board" in c["away_impossible"][g["id"]]["refusing"]


def test_push_out_that_breaks_another_hard_row_is_refused(mov):
    mov["m"] = {"U2"}
    B = board(U1=(20, 50), U2=(28, 50), U3=(36, 50))
    cons = {"separation": [sep("gs", "U1", "U2", 10.0), sep("g3", "U2", "U3", 3.0)]}
    c = mk(B, cons, None)
    gs = PG.detect(c, ["row-away"])["gaps"]
    assert [g["id"] for g in gs] == ["row-away:gs|distance"]                    # U2-U3 holds (4 mm >= 3)
    res = PG.close(c, ["row-away"], SimEval(B, cons), stage="t")
    ref = [r for r in res["refused"] if r.get("candidate") == "push-0"]
    assert ref and ref[0]["check"].startswith("row:")


def test_one_gap_per_named_separation(mov):
    sep_row = {"kind": "separation", "name": "gs", "metric": "distance", "hard": True, "verdict": "fail", "value": 4.0, "max": 10.0, "refs": ["U1", "U2"]}
    irow = {"kind": "away", "name": "sep:gs", "hard": True, "met": False, "value": 4.0, "limit": 10.0, "refs": ["U1", "U2"], "detail": ""}
    c = ctx(rows=[sep_row], _intent=[irow], _cl=[], _far=[])
    gs = PG.detect(c, ["row-away"])["gaps"]
    assert [g["id"] for g in gs] == ["row-away:gs|distance"]                  # both name the offender: the budget row stands
    c = ctx(rows=[dict(sep_row, refs=[])], _intent=[irow], _cl=[], _far=[])
    assert [g["id"] for g in PG.detect(c, ["row-away"])["gaps"]] == ["row-away:sep:gs"]       # the one that names the offender stands
    other = dict(irow, name="away1")                                           # an [[intent.away]] of its own is not a duplicate
    c = ctx(rows=[sep_row], _intent=[irow, other], _cl=[], _far=[])
    assert sorted(g["id"] for g in PG.detect(c, ["row-away"])["gaps"]) == ["row-away:away1", "row-away:gs|distance"]


def flight_board():
    B = board(A=(20, 50), C=(80, 50))
    for r in ("A", "C"):
        B["parts"][r]["pads"] = [{"ref": r, "num": "1", "x": B["parts"][r]["pos"][0], "y": 50.0, "net": "N1"}]
        B["pads"] += B["parts"][r]["pads"]
    return B


FROW = {"kind": "keepout-flight", "name": "N1/N2 x ko", "metric": "crossing", "hard": True, "verdict": "fail", "value": 1, "max": 0, "refs": ["A", "C"],
        "nets": ["N1", "N2"]}
FCONS = {"placement": {"keepout": {"ko": {"layers": "all", "rect": [45, 45, 55, 55]}}}}


def test_flight_push_out_moves_the_movable_end_clear(mov):
    mov["m"] = {"C"}
    B = flight_board()
    c = ctx(B=B, cons=FCONS, spec={}, rows=[FROW], _cl=[], _intent=[])
    g = PG.detect(c, ["row-flight"])["gaps"][0]
    assert g["id"] == "row-flight:N1/N2 x ko|crossing"
    cands = PG.RUNGS["push-out"](g, c)
    assert cands and all(list(x["moves"]) == ["C"] for x in cands)
    from fluxplace import geom as GM
    for x in cands:
        cx, cy, _a = x["moves"]["C"]
        assert not GM.seg_hits_rect((20, 50), (cx, cy), (45, 45, 55, 55))


def test_flight_with_both_ends_fixed_is_reported_not_moved(mov):
    mov["m"] = set()
    c = ctx(B=flight_board(), cons=FCONS, spec={}, rows=[FROW], _cl=[], _intent=[])
    g = PG.detect(c, ["row-flight"])["gaps"][0]
    assert PG.RUNGS["push-out"](g, c) == [] and "route stage" in c["row_reports"][g["id"]]["why"]
    PG.away_questions(c, {"gaps_out": [g]})
    assert g["id"] in c["row_reports"]


def test_region_row_without_a_rung_is_reported_not_silent():
    c = PG.Ctx(stage="floorplan", cons={}, spec={}, rows=[])
    c["B"] = {"parts": {}}
    c["_intent"] = []
    g = PG.gap("row-away", "keep-in-x", "plain", 0.5, ("U1",), row="intent:region:keep-in-x")
    assert PG.RUNGS["feasible"](g, c) == []
    assert "region row" in c["row_reports"][g["id"]]["why"]
