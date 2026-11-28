"""pairs end-of-pass repair (pairs._repair): the failed pairs are laid first with the laid pairs that block them ripped and laid again
after; a trial is valid only when nothing is lost (W16 route-11: 4 of 21 pairs unlaid by lay order alone).  The named cause is
ripped first, and a ripped pair that comes back worse than its first lay (vias, skew class) is avoided or reported."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import pairs as PR

A, B, C = ("A_P", "A_N"), ("B_P", "B_N"), ("C_P", "C_N")


def res(pr, full, why="", blocked="", vias=0, skew=0.0):
    return {"e": {"pair": list(pr), "status": "laid" if full else "not-laid", "why": why, "blocked_by": blocked, "secs": 0,
                  "sections": [{"vias": vias}] if full else []},
            "pcs": {pr[0]: [], pr[1]: []}, "keep": [], "full": full, "nok": 1 if full else 0, "skew": skew}


def setup(monkeypatch, verdict, tiers=None, near=None, rounds=None):
    """verdict(pair, pairs on the state) -> True | via count (int, laid) | (vias, skew) | blocker text (not laid);
    the fake state is the set of applied pairs"""
    laid, logs = [], []

    def mk_ctx():
        return {"done": set(), "on": set(), "grid": object(), "limit": lambda net: 0.15}

    def lay(ctx, pr, info, rule, plan_only):
        ok = verdict(pr, frozenset(ctx["on"]))
        laid.append((pr, frozenset(ctx["on"]), ok))
        if ok is True or (isinstance(ok, int) and not isinstance(ok, bool)):
            return res(pr, True, vias=0 if ok is True else ok)
        if isinstance(ok, tuple):
            return res(pr, True, vias=ok[0], skew=ok[1])
        return res(pr, False, "no path", ok or "")
    monkeypatch.setattr(PR, "_lay_pair", lay)
    monkeypatch.setattr(PR, "_apply", lambda ctx, pr, c, plan_only: ctx["on"].add(pr) if c["nok"] else None)
    info = {p: ([], [], [(0, 1)], 1.0) for p in (A, B, C)}
    key = {p: ((tiers or {}).get(p, 0), 0, 1.0, p) for p in (A, B, C)}
    P = dict(PR.params({}), deadline=None)
    if rounds is not None:
        P["repair_rounds"] = rounds
    nr = near or (lambda c, pr: 99.0)
    run = lambda st: PR._repair(st, info, {p: {} for p in info}, key, P, mk_ctx, nr, False, logs.append)
    return run, laid, logs


def test_a_failed_pair_is_laid_on_the_final_state_without_ripping(monkeypatch):
    run, laid, _ = setup(monkeypatch, lambda pr, on: True)
    st = run({"order": [A, B], "res": {A: res(A, True), B: res(B, False, "corridor of A_P/A_N")}, "score": (1, 1, 0)})
    assert st["res"][B]["full"] and st["score"][0] == 2 and st.get("repaired")
    assert laid == [(B, frozenset({A}), True)], "one search, of the failed pair only, with the laid pair in place"


def test_the_named_blocker_is_ripped_and_laid_again_after_the_failed_pair(monkeypatch):
    # B cannot lay while A is there ("track A_P"); with B first both lay
    run, laid, logs = setup(monkeypatch, lambda pr, on: True if pr != B or A not in on else "track A_P")
    st = run({"order": [A, B, C], "res": {A: res(A, True), B: res(B, False, blocked="track A_P"), C: res(C, True)}, "score": (2, 2, 0)})
    assert all(st["res"][p]["full"] for p in (A, B, C)) and st["score"][0] == 3
    assert [x[0] for x in laid] == [B, A] and laid[0][1] == frozenset({C}) and laid[1][1] == frozenset({B, C})
    assert not st.get("repair_warn") and not any("WARN" in m for m in logs)


def test_the_named_cause_is_ripped_alone_before_the_pairs_that_are_only_near(monkeypatch):
    # C is laid beside B's terminals (near) but only A is named: the first trial rips A alone and C is never searched
    run, laid, _ = setup(monkeypatch, lambda pr, on: True if pr != B or A not in on else "track A_P", near=lambda c, pr: 0.5)
    st = run({"order": [A, C, B], "res": {A: res(A, True), C: res(C, True), B: res(B, False, blocked="track A_P")}, "score": (2, 2, 0)})
    assert st["score"][0] == 3 and [x[0] for x in laid] == [B, A] and laid[0][1] == frozenset({C})


def test_a_pair_laid_after_the_failure_is_no_cause_even_when_the_failure_names_it(monkeypatch):
    # B failed on A (laid before it, only near); its failure text names C's fan-out copper, and C was laid after B (W16 J11)
    run, laid, _ = setup(monkeypatch, lambda pr, on: True if pr != B or A not in on else "via C_P", near=lambda c, pr: 0.5)
    st = run({"order": [A, B, C], "res": {A: res(A, True), B: res(B, False, blocked="via C_P"), C: res(C, True)}, "score": (2, 2, 0)})
    assert st["score"][0] == 3 and [x[0] for x in laid] == [B, A] and laid[0][1] == frozenset({C}), laid


def test_a_rip_that_loses_the_blocker_is_not_kept(monkeypatch):
    def verdict(pr, on):
        return {A: B not in on or "track B_P", B: A not in on or "track A_P"}.get(pr, True)
    run, _, _ = setup(monkeypatch, verdict)
    a0 = res(A, True)
    st = run({"order": [A, B], "res": {A: a0, B: res(B, False, blocked="track A_P")}, "score": (1, 1, 0)})
    assert st["res"][A] is a0 and not st["res"][B]["full"] and st["score"] == (1, 1, 0) and not st.get("repaired")


def test_when_no_joint_trial_is_valid_each_failed_pair_is_tried_alone(monkeypatch):
    # B and C both failed on A; with B and C both laid first A has no way back, with only one of them first it has
    def verdict(pr, on):
        if pr == A:
            return not (B in on and C in on) or "track B_P"
        return A not in on or "track A_P"
    run, laid, _ = setup(monkeypatch, verdict)
    a0 = res(A, True)
    st = run({"order": [A, B, C], "res": {A: a0, B: res(B, False, blocked="track A_P"), C: res(C, False, blocked="track A_P")}, "score": (1, 1, 0)},)
    assert st["score"][0] == 2 and st["res"][A]["full"] and st["res"][A] is not a0
    assert sum(st["res"][p]["full"] for p in (B, C)) == 1


def test_a_higher_tier_pair_is_never_ripped_for_a_lower_one(monkeypatch):
    run, laid, _ = setup(monkeypatch, lambda pr, on: True if pr != B or A not in on else "track A_P", tiers={A: 0, B: 1})
    st = run({"order": [A, B], "res": {A: res(A, True), B: res(B, False, blocked="track A_P")}, "score": (1, 1, 0)})
    assert not st["res"][B]["full"] and [x[0] for x in laid] == [B]


def test_a_rip_set_that_brings_a_pair_back_with_more_vias_gives_way_to_one_that_does_not(monkeypatch):
    # ripping A alone: A comes back with 2 vias (C in its way).  Ripping A and C (C is near): both come back as they were.
    def verdict(pr, on):
        if pr == B:
            return True if A not in on else "track A_P"
        if pr == A:
            return 2 if (C in on and B in on) else True
        return True
    run, laid, logs = setup(monkeypatch, verdict, near=lambda c, pr: 0.5)
    st = run({"order": [A, C, B], "res": {A: res(A, True), C: res(C, True), B: res(B, False, blocked="track A_P")}, "score": (2, 2, 0)})
    assert st["score"][0] == 3 and PR._quality(st["res"][A], lambda n: 0.15) == (0, 0)
    assert [x[0] for x in laid] == [B, A, B, A, C], "the named cause alone first, then the wider set"
    assert not st.get("repair_warn") and not any("WARN" in m for m in logs)


def test_a_loss_no_rip_set_avoids_is_kept_and_reported_with_its_delta(monkeypatch):
    # A always comes back with 2 vias and its skew over the limit once B is laid: a complete pair beats an open one, WARN with the delta
    def verdict(pr, on):
        if pr == B:
            return True if A not in on else "track A_P"
        return (2, 0.4) if pr == A and B in on else True
    run, _, logs = setup(monkeypatch, verdict)
    st = run({"order": [A, B], "res": {A: res(A, True), B: res(B, False, blocked="track A_P")}, "score": (1, 1, 0)})
    assert st["score"][0] == 2
    assert st["repair_warn"] == [{"pair": list(A), "vias": [0, 2], "skew_over": [0, 1]}]
    w = [m for m in logs if "WARN" in m]
    assert len(w) == 1 and "A_P/A_N" in w[0] and "vias 0 -> 2" in w[0] and "skew now over its limit" in w[0]


def test_repair_rounds_zero_switches_it_off(monkeypatch):
    run, laid, _ = setup(monkeypatch, lambda pr, on: True, rounds=0)
    st = run({"order": [A, B], "res": {A: res(A, True), B: res(B, False)}, "score": (1, 1, 0)})
    assert not laid and not st["res"][B]["full"]


def test_a_ladder_trial_does_not_ask_the_escape_planner_again(tmp_path, monkeypatch):
    import planboards as PB
    from fluxplace import pairfb as FB
    two = lambda a, b: [PB.pad("1", 0, 0, a, 0.3, 0.8), PB.pad("2", 0.8, 0, b, 0.3, 0.8)]
    b = PB.write(tmp_path, PB.fp("U1", 40, 45, two("X_DP", "X_DM")) + PB.fp("U2", 40, 60, two("X_DP", "X_DM")))
    asked = []
    monkeypatch.setattr(PR, "_escape_repair", lambda best, *a, **k: asked.append(1) or best)
    PR.plan(b, {"pairlay": {"workers": 1}}, None)
    assert asked == [1]
    PR.plan(b, FB.variant({"pairlay": {"workers": 1}}, _trial=True), None)
    assert asked == [1], "the trial stamp (pairfb._trial) switches the planner off"
    seen = {}
    FB._trial({"pairs": [{"pair": ["X_DP", "X_DM"], "status": "open", "state": None}]}, str(tmp_path / "o.kicad_pcb"), {"pairlay": {}}, None,
              lambda board, cons, spec, out, **k: seen.update(cons["pairlay"]) or {"pairs": []}, "t", "b(i)", lambda m: None, src=b)
    assert seen.get("_trial") is True
