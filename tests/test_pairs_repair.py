"""pairs end-of-pass repair (pairs._repair): a failed pair is tried again on the final state, then with its named blockers ripped
and laid again after it; a round is kept only when nothing is lost (W16 route-11: 4 of 21 pairs unlaid by lay order alone)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import pairs as PR

A, B, C = ("A_P", "A_N"), ("B_P", "B_N"), ("C_P", "C_N")


def res(pr, full, why="", blocked=""):
    return {"e": {"pair": list(pr), "status": "laid" if full else "not-laid", "why": why, "blocked_by": blocked, "secs": 0},
            "pcs": {pr[0]: [], pr[1]: []}, "keep": [], "full": full, "nok": 1 if full else 0, "skew": 0.0}


def setup(monkeypatch, verdict, tiers=None):
    """verdict(pair, pairs on the state) -> full?; the fake state is the set of applied pairs"""
    laid = []

    def mk_ctx():
        return {"done": set(), "on": set(), "grid": object()}

    def lay(ctx, pr, info, rule, plan_only):
        ok = verdict(pr, frozenset(ctx["on"]))
        laid.append((pr, frozenset(ctx["on"]), ok))
        return res(pr, True) if ok is True else res(pr, False, "no path", ok or "")
    monkeypatch.setattr(PR, "_lay_pair", lay)
    monkeypatch.setattr(PR, "_apply", lambda ctx, pr, c, plan_only: ctx["on"].add(pr) if c["nok"] else None)
    info = {p: ([], [], [(0, 1)], 1.0) for p in (A, B, C)}
    key = {p: ((tiers or {}).get(p, 0), 0, 1.0, p) for p in (A, B, C)}
    P = dict(PR.params({}), deadline=None)
    run = lambda st: PR._repair(st, info, {p: {} for p in info}, key, P, mk_ctx, lambda c, pr: 99.0, False, lambda m: None)
    return run, laid


def test_a_failed_pair_is_laid_on_the_final_state_without_ripping(monkeypatch):
    run, laid = setup(monkeypatch, lambda pr, on: True)
    st = run({"order": [A, B], "res": {A: res(A, True), B: res(B, False, "corridor of A_P/A_N")}, "score": (1, 1, 0)})
    assert st["res"][B]["full"] and st["score"][0] == 2 and st.get("repaired")
    assert laid == [(B, frozenset({A}), True)], "one search, of the failed pair only, with the laid pair in place"


def test_the_named_blocker_is_ripped_and_laid_again_after_the_failed_pair(monkeypatch):
    # B cannot lay while A is there ("track A_P"); with B first both lay
    run, laid = setup(monkeypatch, lambda pr, on: True if pr != B or A not in on else "track A_P")
    st = run({"order": [A, B, C], "res": {A: res(A, True), B: res(B, False, blocked="track A_P"), C: res(C, True)}, "score": (2, 2, 0)})
    assert all(st["res"][p]["full"] for p in (A, B, C)) and st["score"][0] == 3
    assert [x[0] for x in laid] == [B, A] and laid[0][1] == frozenset({C}) and laid[1][1] == frozenset({B, C})


def test_when_the_joint_trial_loses_a_ripped_pair_each_failed_pair_is_tried_alone(monkeypatch):
    # B and C both failed on A; with B and C both laid first A has no way back, with only C first it has
    def verdict(pr, on):
        if pr == A:
            return not (B in on and C in on) or "track B_P"
        return A not in on or "track A_P"
    run, laid = setup(monkeypatch, verdict)
    a0 = res(A, True)
    st = run({"order": [A, B, C], "res": {A: a0, B: res(B, False, blocked="track A_P"), C: res(C, False, blocked="track A_P")}, "score": (1, 1, 0)})
    assert st["score"][0] == 2 and st["res"][A]["full"] and st["res"][A] is not a0
    assert sum(st["res"][p]["full"] for p in (B, C)) == 1


def test_a_rip_that_loses_the_blocker_is_not_kept(monkeypatch):
    def verdict(pr, on):
        return {A: B not in on or "track B_P", B: A not in on or "track A_P"}.get(pr, True)
    run, _ = setup(monkeypatch, verdict)
    a0 = res(A, True)
    st = run({"order": [A, B], "res": {A: a0, B: res(B, False, blocked="track A_P")}, "score": (1, 1, 0)})
    assert st["res"][A] is a0 and not st["res"][B]["full"] and st["score"] == (1, 1, 0) and not st.get("repaired")


def test_a_higher_tier_pair_is_never_ripped_for_a_lower_one(monkeypatch):
    run, laid = setup(monkeypatch, lambda pr, on: True if pr != B or A not in on else "track A_P", tiers={A: 0, B: 1})
    st = run({"order": [A, B], "res": {A: res(A, True), B: res(B, False, blocked="track A_P")}, "score": (1, 1, 0)})
    assert not st["res"][B]["full"] and [x[0] for x in laid] == [B]


def test_repair_rounds_zero_switches_it_off(monkeypatch):
    run, laid = setup(monkeypatch, lambda pr, on: True)
    monkeypatch.setattr(PR, "params", lambda cons, _p=PR.params: dict(_p(cons), repair_rounds=0))
    run2, laid2 = setup(monkeypatch, lambda pr, on: True)
    st = run2({"order": [A, B], "res": {A: res(A, True), B: res(B, False)}, "score": (1, 1, 0)})
    assert not laid2 and not st["res"][B]["full"]
