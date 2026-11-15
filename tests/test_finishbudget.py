"""Finish step: guaranteed budget share, cheapest-first order, measured per-net caps, untried vs tried-and-failed, resume."""
import json
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import test_batchroute as TB
from fluxplace import batchroute as BR, pipeline as PL, routediag as RD


def test_router_cap_leaves_the_finish_share():
    assert BR.router_cap(10800, 1080, 0.4, 0) == 6480                 # 60 % of the budget at most
    assert BR.router_cap(10800, 1080, 0.4, 6000) == 480
    assert BR.router_cap(10800, 5000, 0.1, 0) == 5800                 # the ladder's own minimum still wins when it is bigger
    assert BR.settings({})["finish_share"] == 0.4
    assert BR.settings({"route": {"finish_share": 0.25}})["finish_share"] == 0.25
    assert BR.settings({"route": {"finish_share": 5}})["finish_share"] == 0.9


def test_pace_scales_the_cap_from_measured_time_per_closed_net():
    p = BR.Pace(tmo=240, size=35)
    assert p.size_now() == 8 and p.timeout(8) == 240.0 and p.timeout(2) == 160.0   # nothing measured: small batches, 4 x 20 s a net, capped by the batch timeout
    p.note(30.0, 10)                                                 # 3 s per closed net
    assert abs(p.avg() - 3.0) < 1e-9
    assert p.timeout(5) == 60.0 and p.timeout(1) == 20.0             # 4 x 3 s x k, never under the floor
    assert p.size_now() == 35 and BR.Pace(tmo=240, size=35).size_now() == 8
    p.note(5000.0, 0)                                                # time spent on attempts that closed nothing does not inflate it
    assert abs(p.avg() - 3.0) < 1e-9


def test_order_finish_is_cheapest_first_and_failed_nets_last(tmp_path):
    board, W, run, extra, spath = TB.setup(tmp_path, 4)
    B = RD.load(board)
    open0 = {f"N{i}": 1 for i in range(4)}
    assert BR.order_finish(B, list(open0), open0) == ["N0", "N1", "N2", "N3"]
    assert BR.order_finish(B, list(open0), open0, retry_last={"N0"})[-1] == "N0"
    assert BR.order_finish(B, list(open0), {**open0, "N1": 3})[-1] == "N1"          # more open connections = dearer


def slow(run, secs):
    def r(src, dst, only, timeout, limited=True):
        time.sleep(secs)
        return run(src, dst, only, timeout, limited)
    return r


def test_budget_end_says_untried_versus_tried_and_failed(tmp_path):
    board, W, run, extra, spath = TB.setup(tmp_path, 8, fail=["N0", "N1"])
    r = BR.route_batches(board, W, [f"N{i}" for i in range(8)], slow(run, 1.0), batch_nets=2, batch_timeout=5, total_budget=2.5, min_slice=0.2,
                         status_path=spath, extra=extra, log=lambda m: None, hybrid=True)
    assert set(r["untried"]) | set(r["tried_failed"]) == set(r["leftovers"])
    assert not set(r["untried"]) & set(r["tried_failed"])
    assert r["untried"], "the budget ended with nets untried"
    assert set(r["tried_failed"]) >= {"N0", "N1"}
    lj = json.load(open(os.path.join(W, "leftovers.json")))
    assert lj["untried"] == r["untried"] and lj["tried_failed"] == r["tried_failed"]
    assert all(r["reasons"][n] == "budget" for n in r["untried"])
    assert "untried" in BR.format_result(r)


def test_hybrid_closes_the_cheapest_nets_in_the_first_calls(tmp_path):
    board, W, run, extra, spath = TB.setup(tmp_path, 8)
    r = BR.route_batches(board, W, [f"N{i}" for i in range(8)], run, batch_nets=4, batch_timeout=5, status_path=spath, extra=extra,
                         log=lambda m: None, hybrid=True, retry_last=["N0"])
    first = TB.calls(tmp_path)[0]["nets"]
    assert "N0" not in first and first[0] == "N1"                     # the retried net waits
    assert not r["leftovers"] and r["untried"] == [] and r["tried_failed"] == []


def args(**kw):
    return types.SimpleNamespace(**kw)


def test_from_route_resumes_the_finish_step_without_the_router(tmp_path):
    d = tmp_path / "7-route"
    d.mkdir()
    board = tmp_path / "in.kicad_pcb"
    board.write_text("(kicad_pcb)\n")
    best = str(d / "board.kicad_pcb")
    (d / "resume.kicad_pcb").write_text("(kicad_pcb (routed))\n")
    json.dump({"input": PL._sha(str(board)), "board": str(d / "resume.kicad_pcb"), "untried": ["A"], "tried_failed": ["B"]}, open(d / "finish_resume.json", "w"))
    open(best, "w").write("(kicad_pcb)\n")
    assert PL._resume_finish(args(from_stage="pairs"), str(d), str(board), best) is None             # not asked for
    assert PL._resume_finish(args(from_stage="route", no_resume=True), str(d), str(board), best) is None
    rec = PL._resume_finish(args(from_stage="route"), str(d), str(board), best)
    assert rec["tried_failed"] == ["B"] and open(best).read() == "(kicad_pcb (routed))\n"
    board.write_text("(kicad_pcb (changed))\n")                                                      # another input: never resume
    assert PL._resume_finish(args(from_stage="route"), str(d), str(board), best) is None
    json.dump({"input": PL._sha(str(board)), "board": str(d / "resume.kicad_pcb"), "untried": [], "tried_failed": []}, open(d / "finish_resume.json", "w"))
    assert PL._resume_finish(args(from_stage="route"), str(d), str(board), best) is None             # nothing left to do
