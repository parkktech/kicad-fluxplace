"""PLACEGAPS phases 4-5: support-far (home, nudge, far-re-deal, swap-seat), support-starved (make-room), net-budget (pull) on synthetic boards
(real clusters / seater / preroute budgets on a generated board; no router, no KiCad)."""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
import pcbgen as g  # noqa: E402
import powerboard as PW  # noqa: E402
from fluxplace import clusters as CL, geom as GM, placegaps as PG, placetour as PT, routediag as RD  # noqa: E402

FIXED = set()
KINDS = ["support-far", "support-starved", "net-budget"]
OUT = (0, 0, 80, 60)
PINS6 = ["VCC", "GND", "IN", "OUT", "EN", "FB"]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FIXED.clear()
    monkeypatch.setattr(PT, "_movable", lambda B, cons, P: sorted(set(B["parts"]) - FIXED))


def mk(tmp_path, body, cons=None, outline=OUT):
    return g.board(tmp_path, body, outline=outline), cons or {}


def ctx_of(board, cons, B=None):
    return PG.Ctx(stage="floorplan", board=board, cons=cons, spec={}, ds_dir=None, B=B or RD.load(board), rows=[])


def applied(B, moves):
    B2 = copy.deepcopy(B)
    for r, (x, y, a) in moves.items():
        GM.place(B2["parts"][r], x, y, a)
    return B2


class ModelEval(PG.Evaluator):
    """Applies a candidate on the board model and measures every gap again (what FileEval does on files)."""
    def __init__(self, board, cons):
        self.board_, self.cons, self.B = board, cons, RD.load(board)

    def _ctx(self, B):
        return ctx_of(self.board_, self.cons, B)

    def detect(self, kinds):
        return PG.detect(self._ctx(self.B), KINDS)["gaps"]

    def state(self):
        return {"illegal": 0}

    def board(self):
        return self.B

    def trial(self, cand):
        B2 = applied(self.B, cand["moves"])
        return {"gaps": PG.detect(self._ctx(B2), KINDS)["gaps"], "state": {"illegal": 0}, "refusal": None, "B": B2}

    def commit(self, tr):
        self.B = tr["B"]


def far_of(c):
    return {f["ref"]: round(f["dist"], 1) for f in PG.far_supports(c)}


def run(board, cons, kinds=None):
    c = ctx_of(board, cons)
    ev = ModelEval(board, cons)
    res = PG.close(c, kinds or KINDS, ev)
    return c, ev, res


def lone(tmp_path, cy=50, extra="", cons=None):
    b = g.ic("U1", 30, 30, PINS6, pitch=1.0) + PW.cap("C1", 30, cy, "VCC", "GND", "100nF") + extra
    return mk(tmp_path, b, cons)


# ---------------------------------------------------------------- the one detector rule
def test_far_supports_is_the_rule_of_the_support_rows(tmp_path):
    from fluxplace import intent as IT
    board, cons = lone(tmp_path)
    B = RD.load(board)
    cl, _ = CL.build(B, cons, {})
    far = CL.far_supports(cl)
    rows = IT.support_rows(B, cons, {})
    assert [f["ref"] for f in far] == ["C1"] == sum((r["refs"] for r in rows), [])
    assert [f["ref"] for f in PG.far_supports(ctx_of(board, cons))] == ["C1"]
    assert CL.far_supports(cl, waive={"C1"}) == []


# ---------------------------------------------------------------- home
def test_home_seats_a_far_decap_within_its_limit(tmp_path):
    board, cons = lone(tmp_path)
    c, ev, res = run(board, cons, ["support-far"])
    assert res["accepted"] and res["accepted"][0]["rung"] == "home"
    assert far_of(ctx_of(board, cons, ev.B)) == {} and not [g_ for g_ in res["gaps_out"] if g_["kind"] == "support-far"]
    assert res["score_out"] < res["score_in"]


def test_a_fixed_support_is_not_moved_and_has_no_candidate(tmp_path):
    board, cons = lone(tmp_path)
    FIXED.add("C1")
    c = ctx_of(board, cons)
    g_ = PG.detect(c, ["support-far"])["gaps"][0]
    assert all(PG.RUNGS[r](g_, c) == [] for r in PG.DISPATCH["support-far"])


# ---------------------------------------------------------------- far-re-deal
def two_ics(tmp_path, extra=""):
    """U1 and U2 on the VCC rail; U1 has its bypass C1 at the pin, U2 is bare and C2 sits far from both (a shared-rail part, any pin counts)."""
    b = g.ic("U1", 20, 30, PINS6, pitch=1.0) + g.ic("U2", 60, 30, PINS6, pitch=1.0)
    b += PW.cap("C1", 17.5, 28.5, "VCC", "GND", "100nF") + PW.cap("C2", 40, 52, "VCC", "GND", "100nF") + extra
    return mk(tmp_path, b)


def test_far_re_deal_serves_a_shared_rail_part_to_the_bare_pin(tmp_path):
    board, cons = two_ics(tmp_path)
    c = ctx_of(board, cons)
    assert "C2" in far_of(c)
    gp = next(x for x in PG.detect(c, ["support-far"])["gaps"] if x["refs"][0] == "C2")
    cands = PG.RUNGS["far-re-deal"](gp, c)
    assert cands and all(list(x["moves"]) == ["C2"] for x in cands)
    B2 = applied(c.B(), cands[0]["moves"])
    assert "C2" not in far_of(ctx_of(board, cons, B2))
    owner = next(x["ic"] for x in PG.far_supports(c) if x["ref"] == "C2")      # the guessed owner: `home` serves its pin, `far-re-deal` another IC's
    assert cands[0]["id"].startswith("far-re-deal-C2-") and f"-{owner}." not in cands[0]["id"]


def test_far_re_deal_has_nothing_for_a_part_tied_to_one_ic_by_its_net(tmp_path):
    board, cons = lone(tmp_path)
    c = ctx_of(board, cons)
    gp = PG.detect(c, ["support-far"])["gaps"][0]
    assert PG.RUNGS["far-re-deal"](gp, c) == [] and "one IC" in c["why_none"][("far-re-deal", gp["id"])]


# ---------------------------------------------------------------- swap-seat
def test_swap_seat_exchanges_poses_with_a_same_footprint_part_in_the_ring(tmp_path):
    pin_x = 28.0
    board, cons = lone(tmp_path, extra=g.two("R9", pin_x, 31.5, "S1", "S2"))
    c = ctx_of(board, cons)
    gp = PG.detect(c, ["support-far"])["gaps"][0]
    cands = PG.RUNGS["swap-seat"](gp, c)
    assert cands and all(set(x["moves"]) == {"C1", "R9"} for x in cands)
    mv = cands[0]["moves"]
    B = c.B()
    assert (mv["R9"][0], mv["R9"][1]) == tuple(B["parts"]["C1"]["pos"]) and (mv["C1"][0], mv["C1"][1]) == tuple(B["parts"]["R9"]["pos"])
    assert "C1" not in far_of(ctx_of(board, cons, applied(B, mv)))


def test_swap_seat_with_a_support_that_would_end_far_is_refused_by_the_score(tmp_path):
    """The partner is itself a graded support standing within its limit of ITS pin: it is no partner (held)."""
    b = g.ic("U1", 30, 30, PINS6, pitch=1.0) + PW.cap("C1", 30, 50, "VCC", "GND", "100nF") + PW.cap("C3", 26.5, 31.5, "IN", "GND", "100nF")
    board, cons = mk(tmp_path, b)
    c = ctx_of(board, cons)
    gp = next(x for x in PG.detect(c, ["support-far"])["gaps"] if x["refs"][0] == "C1")
    assert PG.RUNGS["swap-seat"](gp, c) == []


# ---------------------------------------------------------------- nudge
def test_nudge_steps_a_neighbour_aside_then_seats(tmp_path, monkeypatch):
    """No seat within the limit while the movable resistor R9 stands where the capacitor would go; it steps aside and the part is seated."""
    board, cons = lone(tmp_path)
    c = ctx_of(board, cons)
    gp = PG.detect(c, ["support-far"])["gaps"][0]
    B = c.B()
    pin = next(q for q in B["parts"]["U1"]["pads"] if q["num"] == "1")
    real = PG._seat

    def seat(ctx, B_, s, ic, pad, pre=None):                 # the seat exists only once something stepped aside
        return real(ctx, B_, s, ic, pad, pre) if pre else ({}, "occupied by R9")
    monkeypatch.setattr(PG, "_seat", seat)
    blk = g.two("R9", pin["x"] - 2.0, pin["y"] - 2.0, "S1", "S2")
    board2, _ = lone(tmp_path / "b2" if (tmp_path / "b2").mkdir() is None else tmp_path, extra=blk)
    c2 = ctx_of(board2, cons)
    gp2 = PG.detect(c2, ["support-far"])["gaps"][0]
    assert PG.RUNGS["home"](gp2, c2) == []
    cands = PG.RUNGS["nudge"](gp2, c2)
    assert cands and "R9" in cands[0]["moves"] and "C1" in cands[0]["moves"]
    d = ((cands[0]["moves"]["R9"][0] - pin["x"] + 2.0) ** 2 + (cands[0]["moves"]["R9"][1] - pin["y"] + 2.0) ** 2) ** 0.5
    assert 0 < d <= PG.NUDGE_MAX_MM + 1e-6


def test_nudge_never_moves_a_fixed_neighbour(tmp_path, monkeypatch):
    board, cons = lone(tmp_path, extra=g.two("R9", 28, 28, "S1", "S2"))
    FIXED.add("R9")
    monkeypatch.setattr(PG, "_seat", lambda *a, **k: ({}, "occupied by R9"))
    c = ctx_of(board, cons)
    gp = PG.detect(c, ["support-far"])["gaps"][0]
    assert PG.RUNGS["nudge"](gp, c) == [] and "nudge" in str(c["why_none"])


# ---------------------------------------------------------------- walled in: the report rung
def test_a_walled_in_support_ends_as_a_leftover_with_the_refusing_check(tmp_path):
    """Every pose around the pin is a fixed part's courtyard: no rung proposes a move, the gap stays and says why."""
    walls = "".join(g.fp(f"R{i}", 30 + dx, 30 + dy, [g.pad("1", 0, 0, "S1", 3, 3)], court=(-9, -9, 9, 9)) for i, (dx, dy) in
                    enumerate([(-12, -8), (-12, 8), (12, -8), (12, 8), (0, -12), (0, 12)]))
    board, cons = lone(tmp_path, extra=walls)
    FIXED.update(f"R{i}" for i in range(6))
    c, ev, res = run(board, cons, ["support-far"])
    left = [x for x in res["gaps_out"] if x["id"] == "support-far:C1"]
    assert left and res["accepted"] == []
    assert any(x["gap"] == "support-far:C1" for x in res["refused"]) or res["stop"].startswith("no rung") or res["stop"].startswith("nothing")


# ---------------------------------------------------------------- make-room
def starved(tmp_path, movable=True):
    b = g.ic("U1", 30, 30, PINS6, pitch=1.0) + PW.cap("C1", 30, 50, "VCC", "GND", "100nF")
    b += g.fp("R5", 26.0, 28.5, [g.pad("1", 0, 0, "S1", 1.2, 1.2), g.pad("2", 1.5, 0, "S2", 1.2, 1.2)], court=(-1, -1, 3, 1))
    b += g.fp("R6", 26.0, 31.5, [g.pad("1", 0, 0, "S3", 1.2, 1.2), g.pad("2", 1.5, 0, "S4", 1.2, 1.2)], court=(-1, -1, 3, 1))
    return mk(tmp_path, b)


def test_make_room_steps_neighbours_out_and_seats_the_far_support(tmp_path):
    board, cons = starved(tmp_path)
    c = ctx_of(board, cons)
    gp = PG.gap("support-starved", "U1", refs=["U1", "C1"], mag=10.0)
    cands = PG.RUNGS["make-room"](gp, c)
    assert cands and len(cands) <= 4
    mv = cands[0]["moves"]
    assert "C1" in mv and set(mv) - {"C1"} <= {"R5", "R6"}
    assert far_of(ctx_of(board, cons, applied(c.B(), mv))) == {}


def test_make_room_does_not_move_a_fixed_neighbour(tmp_path):
    board, cons = starved(tmp_path)
    FIXED.update({"R5", "R6"})
    c = ctx_of(board, cons)
    cands = PG.RUNGS["make-room"](PG.gap("support-starved", "U1", refs=["U1", "C1"], mag=10.0), c)
    assert all(not ({"R5", "R6"} & set(x["moves"])) for x in cands)


# ---------------------------------------------------------------- net-budget
BUDGET = {"placement": {"budget": {"XIN": {"mm": 12.0, "reason": "crystal"}}}}


def xtal(tmp_path, ya=(30, 52), cb=(42, 52)):
    b = g.ic("U1", 30, 30, ["XIN", "GND", "IN", "OUT", "EN", "FB"], pitch=1.0)
    b += g.two("Y1", ya[0], ya[1], "XIN", "GND") + PW.cap("C5", cb[0], cb[1], "XIN", "GND", "12pF")
    return mk(tmp_path, b, copy.deepcopy(BUDGET))


def test_net_budget_gap_is_a_crystal_net_over_its_budget(tmp_path):
    board, cons = xtal(tmp_path)
    c = ctx_of(board, cons)
    gs = PG.detect(c, ["net-budget"])["gaps"]
    xs = [x for x in gs if x["id"] == "net-budget:XIN"]
    assert xs and xs[0]["mag"] > 0 and set(xs[0]["refs"]) >= {"U1", "Y1", "C5"}


def test_pull_brings_the_crystal_parts_to_the_ic_and_the_net_gets_within_budget(tmp_path):
    board, cons = xtal(tmp_path)
    c, ev, res = run(board, cons, ["net-budget"])
    assert any(a["rung"] == "pull" for a in res["accepted"])
    left = [x for x in PG.detect(ctx_of(board, cons, ev.B), ["net-budget"])["gaps"] if x["id"] == "net-budget:XIN"]
    assert not left and res["score_out"] < res["score_in"]


def test_pull_leaves_fixed_parts_alone(tmp_path):
    board, cons = xtal(tmp_path)
    FIXED.update({"Y1", "C5"})
    c = ctx_of(board, cons)
    gp = next(x for x in PG.detect(c, ["net-budget"])["gaps"] if x["id"] == "net-budget:XIN")
    assert PG.RUNGS["pull"](gp, c) == [] and ("pull", gp["id"]) in c["why_none"]


def test_the_budget_terms_are_scored_and_a_pull_that_lengthens_another_net_is_never_better(tmp_path):
    board, cons = xtal(tmp_path)
    c = ctx_of(board, cons)
    gaps = PG.detect(c, KINDS)["gaps"]
    t = PG.score_terms(gaps)
    assert t["budget_over"] == sum(1 for x in gaps if x["kind"] == "net-budget") >= 1 and t["budget_mm"] > 0
    assert PG.TERMS.index("budget_over") > PG.TERMS.index("far_mm")


# ---------------------------------------------------------------- determinism, guards
def test_two_runs_end_on_equal_poses(tmp_path):
    board, cons = two_ics(tmp_path)
    ends = []
    for _ in range(2):
        c, ev, res = run(board, cons)
        ends.append(sorted((r, round(p["pos"][0], 3), round(p["pos"][1], 3), p["angle"]) for r, p in ev.B["parts"].items()))
    assert ends[0] == ends[1]


def test_a_repair_that_breaks_a_hard_row_is_refused_whatever_it_gained():
    base = {"gaps": [PG.gap("support-far", "C1", "decap", 10.0, ("C1", "U1"))], "state": {}}
    row = PG.gap("row-cin-side", "U1|cin", "plain", 1.0, ("U1",), row="support:U1 cin")
    cand = {"gaps": [row], "state": {}}
    ok, check, _why = PG._accept(base, cand, "support-far:C1")
    assert not ok and check.startswith("row:")
