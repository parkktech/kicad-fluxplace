"""B6a: close execution protocol. Outcomes, pre-launch attempt record, one monotonic deadline, scheduling, crash suppression,
time accounting, and the hung-island regression. Fakes only: no kicad-cli, no router."""
import functools
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fluxplace import closeexec as CX
from fluxplace import gaps as GP
from fluxplace import procs as _procs_mod
from fluxplace import refusal as refusal_mod
from test_gaps import DRC, FREE, _fake_measure, _fp, guard, loop  # noqa: F401  (fixtures)

_close = functools.partial(GP.close, inproc=True)


@pytest.fixture(autouse=True)
def _no_prune(monkeypatch):
    monkeypatch.setitem(GP.DISPATCH, "open-free", [r for r in GP.DISPATCH["open-free"] if r != "prune"])
    monkeypatch.setattr(GP, "_blocker_items", lambda *a, **k: None)    # fake boards: no blocker data, a kind stays whole


def _write(mode):
    def fn(base, out, gaps, ctx):
        shutil.copy(base, out)
        if mode == "fix":
            open(out, "w").write("fixed")
        elif mode == "other":
            open(out, "w").write("changed")
        return {"status": "ok"}
    return fn


def _by_rung(work):
    return {r["rung"]: r for r in refusal_mod.load(refusal_mod.path(work))}


# ---- 1. outcomes ----

def test_every_attempt_has_one_outcome_and_only_a_candidate_has_a_verdict(monkeypatch, loop):
    def boom(*a):
        raise RuntimeError("kaboom")

    def fail(outcome, msg, **kw):
        def fn(base, out, gaps, ctx):
            raise CX.RungFailure(outcome, msg, **kw)
        return fn
    R = GP.RUNGS
    monkeypatch.setitem(R, "patch", _write("other"))                                       # candidate, guard refuses
    monkeypatch.setitem(R, "handrepair", lambda b, o, g, c: {"status": "no pair"})          # not applicable
    monkeypatch.setitem(R, "krt", _write("same"))                                           # board identical: no change
    monkeypatch.setitem(R, "relocate", fail("timeout", "rung timeout after 9 s"))
    monkeypatch.setitem(R, "krt_region", fail("child_error", "rung exit 1", code=1, stderr_tail="Traceback: boom"))
    monkeypatch.setitem(R, "krt_locked", lambda b, o, g, c: {"status": "no pair"})          # not applicable (rung added after B6a)
    monkeypatch.setitem(R, "region", fail("invalid_output", "rung wrote no result", code=0))
    monkeypatch.setitem(R, "patch_rip", lambda b, o, g, c: boom())
    monkeypatch.setitem(R, "ai", lambda b, o, g, c: {"status": "no packet"})
    res = _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    rows = _by_rung(loop["work"])
    assert {k: v["outcome"] for k, v in rows.items()} == {
        "patch": "candidate", "handrepair": "not_applicable", "krt": "no_change", "relocate": "timeout", "krt_locked": "not_applicable",
        "krt_region": "child_error", "region": "invalid_output", "patch_rip": "child_error", "ai": "not_applicable"}
    assert rows["patch"]["verdict"] == "guard_refused"
    assert all(v["verdict"] is None for k, v in rows.items() if k != "patch")             # never guard_refused without a candidate
    assert rows["krt_region"]["exit"]["code"] == 1 and "boom" in rows["krt_region"]["exit"]["stderr_tail"]
    assert "kaboom" in rows["patch_rip"]["exit"]["exception"]
    assert set(res["outcomes"]) <= set(CX.OUTCOMES) and sum(res["outcomes"].values()) == res["attempts"] - res["skipped"]
    assert res["verdicts"] == {"guard_refused": 1}


def test_accepted_candidate_is_counted_with_connections_closed(monkeypatch, loop):
    monkeypatch.setitem(GP.RUNGS, "patch", _write("fix"))
    res = _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    assert res["verdicts"] == {"accepted": 1} and res["outcomes"] == {"candidate": 1}
    assert res["connections_closed"] == 1 and res["candidates_read"] == 1 and res["components_attempted"] == 1


# ---- 2. pre-launch record ----

def test_attempt_record_exists_before_the_child_runs_and_is_completed_after(monkeypatch, loop):
    seen = {}

    def rung(base, out, gaps, ctx):
        p = os.path.join(loop["work"], "close", "attempt-001.json")
        seen["rec"] = json.load(open(p))
        seen["base_sha"] = CX.sha_file(base)
        seen["now"] = CX.mono()
        seen["deadline"], seen["hard"] = ctx["deadline"], ctx["deadline_hard"]
        shutil.copy(base, out)
        open(out, "w").write("fixed")
        return {"status": "ok"}
    monkeypatch.setitem(GP.RUNGS, "patch", rung)
    _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    pre = seen["rec"]
    assert pre["state"] == "launched" and pre["rung"] == "patch" and pre["kind"] == "open-free"
    assert pre["input"]["board_sha"] == seen["base_sha"] and pre["input"]["rules_sha"]
    assert pre["targets"]["gap_ids"] == [g["id"] for g in GP.classify(DRC, {}) if g["id"] in pre["targets"]["gap_ids"]]
    for k in ("blockers", "params", "router", "deadline"):
        assert k in pre
    assert pre["deadline"]["child_mono"] < pre["deadline"]["hard_mono"] <= pre["deadline"]["stage_mono"] - pre["deadline"]["reserve_s"]
    assert seen["deadline"] < seen["hard"]
    post = json.load(open(os.path.join(loop["work"], "close", "attempt-001.json")))
    assert post["state"] == "done" and post["outcome"] == "candidate" and post["verdict"] == "accepted"
    assert post["phases"]["marks"] and post["last_completed"] and "artifact" in post and post["exit"] is None


def test_crashed_child_leaves_its_exit_and_last_phase_in_the_record(monkeypatch, loop):
    def rung(base, out, gaps, ctx):
        CX.mark(ctx["phase_file"], "router")
        raise CX.RungFailure("child_error", "rung crashed (signal 11)", sig=11, stderr_tail="segv")
    monkeypatch.setitem(GP.RUNGS, "patch", rung)
    _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    rec = json.load(open(os.path.join(loop["work"], "close", "attempt-001.json")))
    assert rec["outcome"] == "child_error" and rec["exit"]["signal"] == 11 and rec["exit"]["stderr_tail"] == "segv"
    assert rec["in_progress"] == "router"                               # died inside the router phase


# ---- 3. one monotonic deadline ----

def test_five_percent_rung_cannot_hold_more_than_its_allowance():
    now, stage = 1000.0, 1000.0 + 2400
    al = CX.allowance(stage, now, GP._rung_cap({}, "pourisland", 2400), CX.reserve_s({}, 2400))
    assert al["allow_s"] == pytest.approx(120)                          # 5 % of 40 min; the review's +120 s made it 240
    assert al["child"] + al["slack"] == pytest.approx(al["hard"]) and al["slack"] <= CX.KILL_SLACK_S


def test_last_rung_leaves_the_verification_reserve():
    now, stage = 0.0, 600.0
    al = CX.allowance(stage, now, 900, 90.0)
    assert al["hard"] == pytest.approx(510.0)                           # stage deadline minus the reserve
    assert CX.allowance(stage, 595.0, 900, 90.0) is None


def test_supervisor_timeout_comes_from_the_shared_deadline(monkeypatch, tmp_path):
    seen = {}

    def run(cmd, **k):
        seen.update(k)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=k["timeout"], stderr=b"stuck in router")
    monkeypatch.setattr(_procs_mod, "run", run)
    ctx = {"cons": {}, "cons_path": None, "kicad_cli": "k", "work": str(tmp_path), "budget_s": 120.0, "ai_mode": "packet",
           "ai_dir": str(tmp_path), "log": lambda m: None, "deadline_hard": CX.mono() + 50, "deadline": CX.mono() + 45}
    with pytest.raises(CX.RungFailure, match="rung timeout") as e:
        GP._call_rung("patch", "in", str(tmp_path / "o.kicad_pcb"), [], ctx)
    assert 40 < seen["timeout"] <= 50                                   # not budget_s + 120
    assert float(seen["env"][CX.ENV_DEADLINE]) == ctx["deadline"]       # router descendants inherit it
    assert e.value.outcome == "timeout" and "stuck in router" in e.value.stderr_tail


def test_router_timeout_is_clamped_to_the_deadline():
    ctx = {"budget_s": 900.0, "deadline": CX.mono() + 20}
    assert GP._left(ctx, 900) <= 20
    assert GP._left({"budget_s": 70.0}, 900) == 70.0                    # no deadline: the nominal budget


# ---- 4. scheduling ----

def _unit(kind, key, rungs, prio):
    return {"key": key, "kind": kind, "prio": prio, "rungs": rungs}


def test_first_pass_gives_every_group_its_cheap_attempt_before_any_second_attempt():
    units = [_unit("open-free", "A", ["cheap", "dear"], (13, 4)), _unit("open-free", "B", ["cheap", "dear"], (13, 4)),
             _unit("open-pour", "C", ["cheap", "dear"], (12, 4))]
    done, order = set(), []
    while True:
        t = CX.next_task(units, done, {})
        if t is None:
            break
        done.add((t[0]["key"], t[1]))
        order.append((t[0]["key"], t[1]))
    assert order[:3] == [("C", "cheap"), ("A", "cheap"), ("B", "cheap")]
    assert {r for _, r in order[3:]} == {"dear"} and len(order) == 6


def test_opens_get_their_cheap_attempt_before_any_power_width_attempt():
    units = [_unit("power-width", "P", ["powerwiden"], (0, 1, 2)), _unit("open-free", "A", ["cheap", "dear"], (-1, 13, 4)),
             _unit("open-free", "B", ["cheap", "dear"], (-1, 13, 4)), _unit("open-pour", "C", ["cheap"], (-1, 12, 4))]
    done, order = set(), []
    while (t := CX.next_task(units, done, {})) is not None:
        done.add((t[0]["key"], t[1]))
        order.append(t[0]["key"])
    assert order.index("P") > max(order.index(k) for k in "ABC")      # every open group is tried before the power-width unit


def test_a_rung_that_spent_its_share_and_closed_nothing_goes_last_and_rate_orders_the_rest():
    units = [_unit("open-free", "A", ["r1", "r2", "r3"], (-1, 13, 4)), _unit("power-width", "P", ["r1", "r2"], (0, 1, 2))]
    done = {("A", "r1"), ("P", "r1")}
    stats = {("open-free", "r3"): (60.0, 3), ("open-free", "r2"): (600.0, 0), ("power-width", "r2"): (720.0, 0)}
    assert CX.next_task(units, done, stats) == (units[0], "r3")        # opens first, the measured rate within them
    done.add(("A", "r3"))
    stats2 = {("power-width", "r2"): (720.0, 0)}                       # powerwiden: 12 min, nothing closed: behind a rung not yet tried
    units2 = [_unit("power-width", "P", ["r1", "r2"], (-1, 1, 2)), _unit("open-free", "A", ["r1", "r2"], (-1, 13, 4))]
    assert CX.next_task(units2, {("P", "r1"), ("A", "r1")}, stats2) == (units2[1], "r2")


def _three_open_gaps(opens_fixed=()):
    unc = []
    for i in range(3):
        if i in opens_fixed:
            continue
        unc.append({"description": "Missing connection", "items": [
            {"description": f"Pad 1 [N{i}]", "pos": {"x": 20 * i, "y": 0}}, {"description": f"Pad 2 [N{i}]", "pos": {"x": 20 * i + 9, "y": 0}}]})
    return {"violations": [], "unconnected_items": unc}


def test_close_groups_opens_by_shared_blocker_and_runs_each_groups_cheap_rung_first(monkeypatch, loop):
    blockers = {"N0": ["X"], "N1": ["X"], "N2": ["Y"]}                  # N0 and N1 share a blocker, N2 has its own
    monkeypatch.setattr(GP, "_blocker_items", lambda board, gaps, skip=(), r=3.0: {b for g in gaps for n in g["nets"] for b in blockers[n]})

    def measure(board, kicad_cli="kicad-cli"):
        d = _three_open_gaps()
        return {"drc": d, "opens": 3, "shorts": 0, "errors": 0, "err_keys": Counter()}
    monkeypatch.setattr(GP, "measure", measure)
    calls = []

    def spy(name):
        def fn(base, out, gaps, ctx):
            calls.append((name, tuple(sorted(n for g in gaps for n in g["nets"]))))
            shutil.copy(base, out)
            return {"status": "no change"}
        return fn
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, spy(r))
    _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    first = GP.DISPATCH["open-free"][0]
    assert calls[:2] == [(first, ("N0", "N1")), (first, ("N2",))]       # both groups tried before a second rung
    assert all(c[0] != first for c in calls[2:])


# ---- crash suppression ----

def test_identical_retry_of_a_crashed_attempt_is_suppressed(monkeypatch, loop):
    n = []

    def crash(base, out, gaps, ctx):
        n.append(1)
        raise CX.RungFailure("child_error", "rung exit 1", code=1)
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, _write("same"))
    monkeypatch.setitem(GP.RUNGS, "krt", crash)
    _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    assert len(n) == 1
    res = _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)    # same input, same rung, same params
    assert len(n) == 1 and res["skipped"] >= 1
    rows = [json.loads(x) for x in open(loop["attempts"])]
    assert any(r["method"] == "krt" and r["result"] == "skipped" and r["why"].startswith("crash memo") for r in rows)
    open(loop["board"], "w").write("original, edited")                  # the input changed: it may run again
    _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    assert len(n) == 2


# ---- 5. time accounting ----

def test_clock_intervals_do_not_overlap_and_add_up():
    c = CX.Clock(t0=100.0)
    c.switch("measure", now=100.0)
    c.switch("diagnose", now=105.0)
    c.switch("child_start", now=110.0)
    c.split(CX.child_parts([{"p": "child_start", "m": 114.0}, {"p": "router", "m": 120.0}, {"p": "router_done", "m": 150.0}], 110.0, 160.0),
            then="refill_drc", now=160.0)
    c.switch("idle", now=170.0)
    assert c.acc["child_start"] == 4 and c.acc["router"] == 30 and c.acc["rung"] == 16 and c.acc["refill_drc"] == 10
    assert c.total(now=175.0) == pytest.approx(75.0) and sum(c.acc.values()) == pytest.approx(75.0)
    assert "sum 100.0%" in c.line(now=175.0)


def test_child_that_never_started_and_one_killed_inside_the_router():
    assert CX.child_parts([], 10.0, 40.0) == {"child_start": 30.0}
    p = CX.child_parts([{"p": "child_start", "m": 12.0}, {"p": "router", "m": 20.0}], 10.0, 40.0)
    assert p == {"child_start": 2.0, "router": 20.0, "rung": 8.0}       # an open router phase runs to the kill


def test_close_ends_with_a_time_line_that_sums_to_the_wall_time(monkeypatch, loop):
    monkeypatch.setitem(GP.RUNGS, "patch", _write("fix"))
    out = []
    res = _close(loop["board"], loop["work"], attempts=loop["attempts"], log=out.append)
    t = res["time_accounting"]
    assert sum(v for k, v in t.items() if k != "total") == pytest.approx(t["total"], abs=0.1)
    assert abs(t["total"] - res["minutes"] * 60) < 1.0
    assert set(t) == set(CX.CATS) | {"total"}
    assert any(l.startswith("close time ") and "sum 100.0%" in l for l in out)
    assert any("disconnected components with an applicable attempt 1" in l and "connections closed 1" in l for l in out)


# ---- 6. regression: a hung island job and a cheap routable open ----

HANG = ("import subprocess, sys, time\n"
        "p = subprocess.Popen(['sleep', '600'])\n"
        "open(sys.argv[1], 'w').write(str(p.pid))\n"
        "time.sleep(600)\n")


def _alive(pid):
    try:
        return open(f"/proc/{pid}/stat").read().split(") ")[1][0] != "Z"
    except OSError:
        return False


def test_hung_island_job_is_reaped_and_the_cheap_open_is_committed_within_the_stage_budget(monkeypatch, tmp_path, guard):
    zone_open = DRC["unconnected_items"][0]

    def measure(board, kicad_cli="kicad-cli"):
        txt = open(board).read()
        unc = ([] if "pour_fixed" in txt else [zone_open]) + ([] if "free_fixed" in txt else [FREE])
        return {"drc": {"violations": [], "unconnected_items": unc}, "opens": len(unc), "shorts": 0, "errors": 0, "err_keys": Counter()}
    monkeypatch.setattr(GP, "measure", measure)
    monkeypatch.setattr(GP.versions, "copper_fingerprint", _fp)
    monkeypatch.setattr(GP, "_blockers_of", lambda *a, **k: None)
    monkeypatch.setenv(CX.ENV_DEADLINE, "0")                            # the in-process child entry sets it; the fixture restores it
    pidfile = tmp_path / "grandchild.pid"
    real = _procs_mod.run

    def run(cmd, **k):
        job = json.load(open(cmd[-1]))
        if job["name"] == "pourisland":                                 # the hung island fill: a child with its own grandchild
            return real([sys.executable, "-c", HANG, str(pidfile)], **k)
        assert GP._main(["--rung-job", cmd[-1]]) == 0
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(_procs_mod, "run", run)
    patch = lambda base, out, gaps, ctx: (open(out, "w").write(open(base).read() + " free_fixed"), {"status": "ok"})[1]       # noqa: E731
    for r in set(GP.DISPATCH["open-pour"] + GP.DISPATCH["open-free"]):
        monkeypatch.setitem(GP.RUNGS, r, lambda b, o, g, c: {"status": "no change"})
    monkeypatch.setitem(GP.RUNGS, "patch", patch)
    board = tmp_path / "in.kicad_pcb"
    board.write_text("original")
    cons = {"flow": {"rung_frac": {"pourisland": 0.2}, "verify_reserve_s": 3}}
    t = time.monotonic()
    res = GP.close(str(board), str(tmp_path / "work"), cons=cons, budget_min=0.5, attempts=str(tmp_path / "a.jsonl"), log=lambda m: None)
    wall = time.monotonic() - t
    assert wall < 30.0                                                  # inside the stage budget (30 s)
    rows = _by_rung(str(tmp_path / "work"))
    assert rows["pourisland"]["outcome"] == "timeout" and rows["pourisland"]["verdict"] is None
    time.sleep(0.3)
    assert pidfile.exists() and not _alive(int(pidfile.read_text()))    # the hung job's whole group is reaped
    lines = [json.loads(x) for x in open(tmp_path / "a.jsonl")]
    order = [l["method"] for l in lines if l["result"] != "skipped"]
    assert order.index("patch") < order.index("pourisland")             # the cheap open had its turn first
    assert any(l["method"] == "patch" and l["result"] in ("closed", "partial") for l in lines)
    assert open(res["best"]).read().endswith("free_fixed") and res["end"]["opens"] == 1       # committed; only the island stays open
    assert res["time_accounting"]["child_start"] >= 5.0                 # the hung child's wall is accounted, to the kill
