"""B11: pairs fallback ladder memo (same input, constraints and blocker set is not searched twice) and the stage time budget."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import pairfb as FB, pairs as PR


def _rep():
    return {"pairs": [{"pair": ["A_P", "A_N"], "state": None, "tried": [], "blockers": ["track X F.Cu"]},
                      {"pair": ["B_P", "B_N"], "state": None, "tried": [], "blockers": []}]}


def _run_fn(calls):
    def run(board, cons, spec, out, only=None, fallback=True):
        calls.append((sorted(only), (cons.get("pairlay") or {}).get("_deadline")))
        open(out, "w").write("trial")
        return {"pairs": []}
    return run


def test_same_attempt_is_not_searched_twice(tmp_path):
    out = str(tmp_path / "out.kicad_pcb")
    open(out, "w").write("board")
    rep, calls = _rep(), []
    cons = FB.variant({}, via_cost_mm=2.0)
    assert FB._trial(rep, out, cons, None, _run_fn(calls), "i1", "b(i)", lambda m: None) == 0
    assert FB._trial(rep, out, cons, None, _run_fn(calls), "i1b", "b(i)", lambda m: None) == 0       # identical: memo hit
    assert len(calls) == 1 and rep["memo_skips"] == ["i1b"] and rep["pairs"][0]["tried"][-1] == "b(i):memo"
    FB._trial(rep, out, FB.variant({}, via_cost_mm=3.0), None, _run_fn(calls), "i2", "b(i)", lambda m: None)   # other constraints: searched
    open(out, "w").write("board with other copper")
    FB._trial(rep, out, cons, None, _run_fn(calls), "i3", "b(i)", lambda m: None)                 # other input geometry: searched
    assert len(calls) == 3


def test_changed_blocker_set_is_a_new_attempt(tmp_path):
    out = str(tmp_path / "out.kicad_pcb")
    open(out, "w").write("board")
    rep, calls = _rep(), []
    FB._trial(rep, out, {}, None, _run_fn(calls), "t1", "b(i)", lambda m: None)
    rep["pairs"][0]["blockers"] = ["track Y B.Cu"]
    FB._trial(rep, out, {}, None, _run_fn(calls), "t2", "b(i)", lambda m: None)
    assert [c[0] for c in calls] == [["A_N", "A_P", "B_N", "B_P"], ["A_N", "A_P"]]                   # only the pair whose blockers changed


def test_no_trial_starts_after_the_stage_budget(tmp_path):
    out = str(tmp_path / "out.kicad_pcb")
    open(out, "w").write("board")
    rep, calls, msgs = _rep(), [], []
    cons = FB.variant({}, _deadline=time.time() - 1)
    assert FB._trial(rep, out, cons, None, _run_fn(calls), "t", "b(i)", msgs.append) == 0
    assert not calls and rep["pairs"][0]["tried"] == ["b(i):budget"] and "time budget" in msgs[0]


def test_trial_gets_an_equal_share_of_what_is_left():
    rep = {"fallback_budget": {"slots": 4}}
    now = 1000.0
    d1 = FB.trial_deadline(rep, {"deadline": now + 400}, now)
    assert d1 == now + 100                                                                         # 400 s / 4 steps
    d2 = FB.trial_deadline(rep, {"deadline": now + 400}, now + 20)                                 # an early finisher leaves more for the rest
    assert d2 == now + 20 + 380 / 3
    assert FB.trial_deadline(rep, {}, now) is None


def test_params_budget_and_deadline_stamp():
    assert PR.params({})["budget_s"] == 1200 and PR.params({"pairlay": {"budget_min": 5}})["budget_s"] == 300
    assert PR.params(FB.variant({}, _deadline=5.0))["deadline"] == 5.0
