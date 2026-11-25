"""PLACEGAPS phase 2: ghost-open rungs (unblock, seal, endpoint) and JudgeEval inside `close` (fakes: no router, no KiCad)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fluxplace import placegaps as PG, placetour as PT  # noqa: E402
from test_placegaps_core import ctx, part  # noqa: E402


def line_board():
    """U1 at x=10 and J1 at x=60 joined by net N1 on one line (y=10); X1 / X2 sit on that line, X3 does not."""
    B = {"parts": {"U1": part(10, 10, court=(8, 8, 12, 12)), "J1": part(60, 10, court=(56, 6, 64, 14)),
                   "X1": part(30, 10, court=(29, 9, 31, 11)), "X2": part(40, 10, court=(38.5, 8.5, 41.5, 11.5)),
                   "X3": part(30, 40)},
         "bbox": (0, 0, 100, 100),
         "pads": [{"ref": "U1", "num": "1", "x": 10.0, "y": 10.0, "net": "N1"}, {"ref": "J1", "num": "1", "x": 60.0, "y": 10.0, "net": "N1"}]}
    for p in B["parts"].values():
        p["pads"] = [0, 0]
    return B


def gap(**kw):
    g = PG.gap("ghost-open", "N1", kw.pop("cls", "rf"), 2, refs=["J1", "U1"], nets=["N1"])
    g.update(dict(dict(cause="blocked", causes=["LONG_HAUL"]), **kw))
    return g


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(PG, "_rules", lambda c, g: (0.15, 0.15, 0.5))
    monkeypatch.setattr(PT, "_movable", lambda B, cons, P: sorted(B["parts"]))
    monkeypatch.setattr(PT, "_legal", lambda B, cons, tg, mov: {r: tuple(v) for r, v in tg.items()})    # the legaliser accepts the targets


def test_clear_shift_is_the_minimum_translation_plus_clearance():
    rects = [(0, 9.5, 100, 10.5)]
    court = (29, 9, 31, 11)
    assert PG.clear_shift(court, rects, (0, 1)) == pytest.approx(1.5 + PG.CLEAR_MM, abs=0.11)      # up out of a 1 mm wide strip
    assert PG.clear_shift(court, rects, (1, 0)) is None or PG.clear_shift(court, rects, (1, 0)) > 50  # along the strip: never clears
    assert PG.clear_shift(court, [(100, 100, 101, 101)], (0, 1)) == PG.CLEAR_MM                      # not in the way: just the clearance


def test_nearest_normal_is_perpendicular_to_the_nearest_flight_line():
    n = PG._nearest_normal([((0, 0), (10, 0)), ((0, 50), (10, 50))], (5, 3))
    assert n == [(0.0, 1.0), (-0.0, -1.0)] or [tuple(round(c, 6) + 0 for c in v) for v in n] == [(0.0, 1.0), (0.0, -1.0)]


def test_unblock_moves_the_smallest_blockers_out_of_the_corridor_both_normals(env):
    c = ctx(B=line_board(), cons={}, spec={})
    cands = PG._r_unblock(gap(), c)
    ids = [x["id"] for x in cands]
    assert ids == ["unblock-n1", "unblock-n2", "unblock-n1-other"]                                 # X1 (4 mm2) then X2 (9 mm2)
    n1, n2, other = cands
    assert sorted(n1["moves"]) == ["X1"] and sorted(n2["moves"]) == ["X1", "X2"]
    x, y, a = n1["moves"]["X1"]
    assert x == pytest.approx(30.0) and abs(y - 10.0) >= 1.0                                       # moved off the line, not along it
    assert other["moves"]["X1"][1] != n1["moves"]["X1"][1] and (other["moves"]["X1"][1] - 10) * (n1["moves"]["X1"][1] - 10) < 0   # the other side
    assert "X3" not in {r for x_ in cands for r in x_["moves"]} and all(x_["rung"] == "unblock" for x_ in cands)


def test_unblock_only_for_blocked_or_unconfirmed(env):
    c = ctx(B=line_board(), cons={}, spec={})
    assert PG._r_unblock(gap(cause="order"), c) == [] and PG._r_unblock(gap(cause="not-confirmed"), c) == []
    assert PG._r_unblock(gap(cause="unconfirmed"), c)


def test_endpoint_smaller_end_steps_toward_the_other_by_fractions_of_the_excess(env, monkeypatch):
    monkeypatch.setattr(PG, "endpoint_budget", lambda g: 20.0)
    B = line_board()
    c = ctx(B=B, cons={}, spec={})
    cands = PG._r_endpoint(gap(), c)
    assert [x["id"] for x in cands] == ["endpoint-U1-25", "endpoint-U1-50", "endpoint-U1-100"]       # U1 (16 mm2) is smaller than J1 (64 mm2)
    xs = [x["moves"]["U1"][0] for x in cands]
    assert xs == pytest.approx([10 + 0.25 * 30, 10 + 0.5 * 30, 10 + 30])                              # span 50 - budget 20 = 30 mm excess
    monkeypatch.setattr(PG, "endpoint_budget", lambda g: None)                                       # no budget: fractions of the span
    assert [x["moves"]["U1"][0] for x in PG._r_endpoint(gap(), c)] == pytest.approx([10 + 12.5, 10 + 25, 10 + 50])
    assert PG._r_endpoint(gap(cause="order"), c) == []


def test_endpoint_skips_a_gap_with_one_part_or_no_movable_end(env, monkeypatch):
    B = line_board()
    B["pads"] = B["pads"][:1]
    assert PG._r_endpoint(gap(), ctx(B=B, cons={}, spec={})) == []
    monkeypatch.setattr(PT, "_movable", lambda B, cons, P: ["X1"])
    assert PG._r_endpoint(gap(), ctx(B=line_board(), cons={}, spec={})) == []


def test_seal_needs_a_sealed_pad_and_proposes_the_parts_breakout_moved(tmp_path, monkeypatch):
    from fluxplace import breakout as BO, routediag as RDG
    B = line_board()
    c = ctx(B=B, cons={}, spec={}, board=str(tmp_path / "b.kicad_pcb"))
    open(c["board"], "w").write("x")
    assert PG._r_seal(gap(), c) == []                                                                # LONG_HAUL only: nothing sealed
    moved = line_board()
    moved["parts"]["X1"]["pos"] = (30, 14)
    monkeypatch.setattr(BO, "repair", lambda path, cons, spec, **k: {"moved": {"X1": 1}, "before": 3, "after": 1})
    monkeypatch.setattr(RDG, "load", lambda p: moved)
    cands = PG._r_seal(gap(causes=["SEALED_PAD"]), c)
    assert len(cands) == 1 and list(cands[0]["moves"]) == ["X1"] and "3->1" in cands[0]["notes"]
    assert os.listdir(tmp_path) == ["b.kicad_pcb"]                                                   # the scratch copy is gone
    monkeypatch.setattr(BO, "repair", lambda path, cons, spec, **k: {"moved": {}, "before": 3, "after": 3})
    monkeypatch.setattr(RDG, "load", lambda p: line_board())
    assert PG._r_seal(gap(causes=["SEALED_PAD"]), c) == []


# ------------------------------------------------------------------ JudgeEval inside close
def judge_world(monkeypatch, tmp_path, results, drc=None):
    """A JudgeEval whose tier-1, judge and gap measurement are scripted: `results` = {cand id: (key, open nets)}."""
    from fluxplace import legalgate as LG
    base = str(tmp_path / "base.kicad_pcb")
    open(base, "w").write("base")
    monkeypatch.setattr(PT, "gate_state", lambda B, cons, spec: {"collision": 0})
    monkeypatch.setattr(PT, "params", lambda cons: dict(PT.DEFAULTS, jobs=1))

    def tier1(base_path, cands, cons, spec, P, outdir, st):
        os.makedirs(outdir, exist_ok=True)
        surv, dropped = [], []
        for cd in cands:
            if cd["id"] == "bad-gate":
                dropped.append((cd, "worse: collision"))
                continue
            p = os.path.join(outdir, cd["id"] + ".kicad_pcb")
            open(p, "w").write(cd["id"])
            surv.append(((0,), cd, p, {"segs": [], "vias": []}))
        return surv, dropped
    monkeypatch.setattr(PT, "tier1", tier1)

    def jobs(js):
        out = []
        for j in js:
            cid = j["tag"].split("-", 1)[1]
            key, opens = results[cid]
            out.append({"key": list(key), "blame": {"open": [{"net": n, "refs": ["U1"]} for n in opens], "costly": [], "hot": [], "turned": []},
                        "routed": j["board"] + ".routed", "error": None})
        return out

    def measure(self, board, blame):
        gs = [PG.gap("ghost-open", n["net"], "hs-pair" if n["net"].startswith("HS") else "plain", 1, nets=[n["net"]]) for n in blame["open"]]
        for g in gs:
            g.update(cause="blocked", causes=[], blockers=[], blockers_movable=[], corridor_mm=1.0, width_mm=0.5)
        return gs, None
    monkeypatch.setattr(PG.JudgeEval, "_measure", measure)
    monkeypatch.setattr(PG, "diagnose", lambda *a, **k: None)
    monkeypatch.setattr(LG, "_drc", lambda *a, **k: [])
    monkeypatch.setattr(LG, "drc_new", drc or (lambda path, orig, **k: {"n": {}, "items": [], "pairs": []}))
    from fluxplace import routediag as RDG
    monkeypatch.setattr(RDG, "load", lambda p: {"parts": {"U1": part(1, 1)}, "bbox": (0, 0, 9, 9), "pads": []})
    c = PG.Ctx(stage="placetour", board=base, cons={"flow": {"no_gain_rounds": 1}}, spec={}, B={"parts": {"U1": part(1, 1)}, "bbox": (0, 0, 9, 9)})
    base_key = (1, 0, 0, 3, 0, 0.0, 10, 5.0)
    ev = PG.JudgeEval(c, {}, base, {"open": [{"net": "HS_N", "refs": ["U1"]}, {"net": "S1", "refs": []}, {"net": "S2", "refs": []}], "costly": [], "hot": [], "turned": []},
                      base_key, str(tmp_path / "w"), dict(PT.DEFAULTS, jobs=1), jobs_fn=jobs)
    return c, ev


def one_cand(cid):
    return {"id": cid, "rung": "t", "moves": {"U1": (1.0, 1.0, 0)}, "claims": [], "notes": ""}


def test_close_with_judge_eval_accepts_the_class_gain_and_refuses_the_rest(monkeypatch, tmp_path):
    results = {"good": ((0, 0, 0, 2, 0, 0.0, 11, 5.0), ["S1", "S2"]),                       # hs open routed, a plain open gone too
               "trade": ((1, 0, 0, 0, 0, 0.0, 10, 5.0), ["HS_N"]),                           # plain opens routed, hs still open: lower score, but not the best
               "noop": ((1, 0, 0, 3, 0, 0.0, 10, 5.0), ["HS_N", "S1", "S2"]),                # nothing changed: no gain
               "newhs": ((2, 0, 0, 1, 0, 0.0, 10, 5.0), ["HS_N", "HS_M"]),                  # one more hs net open: never-worse term refuses
               "plainonly": ((0, 0, 0, 3, 0, 0.0, 10, 5.0), ["S1", "S2", "S3"])}            # hs routed, a new plain open: lower on the hs term -> accepted too, but worse than `good`
    c, ev = judge_world(monkeypatch, tmp_path, results)
    monkeypatch.setitem(PG.DISPATCH, "ghost-open", ["t"])
    monkeypatch.setitem(PG.RUNGS, "t", lambda g, ctx: [one_cand(x) for x in ("good", "trade", "newhs", "bad-gate", "plainonly", "noop")] if g["id"] == "ghost-open:HS_N" else [])
    out = PG.close(c, ["ghost-open"], ev)
    assert [a["candidate"] for a in out["accepted"]] == ["good"]                                # the best acceptable candidate, not the first
    refused = {r["candidate"]: r["check"] for r in out["refused"] if r.get("candidate")}
    assert refused["newhs"] == "judge:hs-pair open" and refused["bad-gate"].startswith("legal") and refused["noop"] == "score:no-gain" and "trade" not in refused
    assert ev.board_path.endswith("good.kicad_pcb") and ev.key == (0, 0, 0, 2, 0, 0.0, 11, 5.0)  # the base moved only for the winner
    assert [g["id"] for g in out["gaps_out"]] == ["ghost-open:S1", "ghost-open:S2"] and out["score_out"] < out["score_in"]
    assert c["board"] == ev.board_path


def test_a_new_drc_item_refuses_an_improving_candidate(monkeypatch, tmp_path):
    results = {"good": ((0, 0, 0, 2, 0, 0.0, 11, 5.0), ["S1", "S2"])}
    drc = lambda path, orig, **k: {"n": {"clearance": 1}, "items": [{"type": "clearance"}], "pairs": [["C1", "C2"]]}
    c, ev = judge_world(monkeypatch, tmp_path, results, drc=drc)
    monkeypatch.setitem(PG.DISPATCH, "ghost-open", ["t"])
    monkeypatch.setitem(PG.RUNGS, "t", lambda g, ctx: [one_cand("good")] if g["id"] == "ghost-open:HS_N" else [])
    out = PG.close(c, ["ghost-open"], ev)
    assert out["accepted"] == [] and out["refused"][0]["check"] == "drc:clearance" and "C1/C2" in out["refused"][0]["why"]
    assert ev.board_path.endswith("base.kicad_pcb")                                             # unchanged
    assert open(ev.board_path).read() == "base"


def test_drc_that_does_not_run_fails_closed(monkeypatch, tmp_path):
    results = {"good": ((0, 0, 0, 2, 0, 0.0, 11, 5.0), ["S1", "S2"])}
    c, ev = judge_world(monkeypatch, tmp_path, results, drc=lambda *a, **k: None)
    monkeypatch.setitem(PG.DISPATCH, "ghost-open", ["t"])
    monkeypatch.setitem(PG.RUNGS, "t", lambda g, ctx: [one_cand("good")] if g["id"] == "ghost-open:HS_N" else [])
    out = PG.close(c, ["ghost-open"], ev)
    assert out["accepted"] == [] and out["refused"][0]["check"] == "drc:not-run"


def test_a_judge_with_no_routed_copy_is_refused(monkeypatch, tmp_path):
    c, ev = judge_world(monkeypatch, tmp_path, {"good": ((0,) * 8, [])})
    ev.jobs_fn = lambda js: [{"key": None, "blame": None, "routed": None, "error": "boom"} for _ in js]
    monkeypatch.setitem(PG.DISPATCH, "ghost-open", ["t"])
    monkeypatch.setitem(PG.RUNGS, "t", lambda g, ctx: [one_cand("good")] if g["id"] == "ghost-open:HS_N" else [])
    out = PG.close(c, ["ghost-open"], ev)
    assert out["accepted"] == [] and out["refused"][0]["check"].startswith("judge:")


def test_close_ghost_never_raises_and_keeps_best(monkeypatch, tmp_path):
    best = {"board": "b", "blame": {}, "key": (1, 0, 0, 1, 0, 0.0, 1, 1.0), "routed": "r"}
    monkeypatch.setattr(PG, "JudgeEval", lambda *a, **k: 1 / 0)
    from types import SimpleNamespace
    got, res = PT._close_ghost(SimpleNamespace(), {}, {}, best, str(tmp_path), {}, {}, "b", str(tmp_path / "p.json"))
    assert got is best and res is None


def test_candidates_carry_the_project_file_and_the_drc_sees_it(monkeypatch, tmp_path):
    seen = {}
    from fluxplace import legalgate as LG
    results = {"good": ((0, 0, 0, 2, 0, 0.0, 11, 5.0), ["S1", "S2"])}
    c, ev = judge_world(monkeypatch, tmp_path, results)
    open(tmp_path / "base.kicad_pro", "w").write("{rules}")
    monkeypatch.setattr(LG, "drc_new", lambda path, orig, **k: seen.update(pro=os.path.exists(os.path.splitext(path)[0] + ".kicad_pro")) or {"n": {}, "items": [], "pairs": []})
    monkeypatch.setitem(PG.DISPATCH, "ghost-open", ["t"])
    monkeypatch.setitem(PG.RUNGS, "t", lambda g, ctx: [one_cand("good")] if g["id"] == "ghost-open:HS_N" else [])
    out = PG.close(c, ["ghost-open"], ev)
    assert out["accepted"] and seen["pro"] is True


def test_close_stops_when_every_rung_already_ran_on_these_poses(monkeypatch, tmp_path):
    c, ev = judge_world(monkeypatch, tmp_path, {"noop": ((1, 0, 0, 3, 0, 0.0, 10, 5.0), ["HS_N", "S1", "S2"])})
    monkeypatch.setitem(PG.DISPATCH, "ghost-open", ["t"])
    monkeypatch.setitem(PG.RUNGS, "t", lambda g, ctx: [one_cand("noop")] if g["id"] == "ghost-open:HS_N" else [])
    c["cons"]["flow"] = {"no_gain_rounds": 9}
    out = PG.close(c, ["ghost-open"], ev)
    assert out["stop"].startswith("nothing new") and out["rounds"] == 2 and out["memo_skips"] >= 1
