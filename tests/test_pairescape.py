"""B5 pair escape: bundle selection, far-end re-selection with probe memory, twin receptacle tap vias in the audit, no-op safety."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import pairaudit as PA
from fluxplace import pairescape as PE
from fluxplace import pairs as PR
from fluxplace import routediag as RD


def bundle(pr, ti, name, x, cost):
    """a bundle whose footprint is one vertical track at x (width 0.15) on F.Cu"""
    seg = ("seg", "F.Cu", (x, 0.0), (x, 3.0), 0.15)
    return {"pair": pr, "ti": ti, "var": {"name": name, "c": (x, 0.0)}, "vkey": (name, x, 0.0), "layer": "F.Cu", "dir": (0, 1), "foot": [seg], "own": {},
            "cost": cost, "stub": 0.0, "vias": 0, "margin": None, "sig": (name, "F.Cu", (0, 1))}


def two_vars():
    p0, p1 = ("A_N", "A_P"), ("B_N", "B_P")
    # the cheapest bundle of each pair is the same track: choosing both locally collides (pair_escape_conflict)
    d0 = [bundle(p0, 0, "mid", 1.0, 1.0), bundle(p0, 0, "left", 0.0, 2.0)]
    d1 = [bundle(p1, 0, "mid", 1.05, 1.0), bundle(p1, 0, "right", 2.0, 2.5)]
    return {(p0, 0): d0, (p1, 0): d1}


def test_select_joint_choice_avoids_the_local_conflict():
    doms = two_vars()
    local = [d[0] for d in doms.values()]
    assert PE.foot_conflict(local[0]["foot"], local[1]["foot"], 0.127)
    sols, used = PE.select(doms, 0.127, 10000, 5)
    assert sols and used <= 10000
    best = sols[0]
    a, b = list(best.values())
    assert not PE.foot_conflict(a["foot"], b["foot"], 0.127)
    assert math.isclose(sum(x["cost"] for x in best.values()), 3.0)       # (mid, right) = 1.0 + 2.5 is worse than (left, mid) = 2.0 + 1.0
    assert {x["vkey"][0] for x in best.values()} == {"left", "mid"}


def test_select_expansion_limit_is_a_parameter_and_prunes_early():
    doms = two_vars()
    sols, used = PE.select(doms, 0.127, 1, 5)
    assert used <= 1 and not sols
    sols, used = PE.select(doms, 0.127, 10000, 5)
    assert used < 20 and sols


def test_select_probe_memory_forbids_a_combination():
    doms = two_vars()
    sols, _ = PE.select(doms, 0.127, 10000, 5)
    first = frozenset(b["sig"] for b in sols[0].values())
    again, _ = PE.select(doms, 0.127, 10000, 5, forbidden=frozenset({first}))
    assert again and frozenset(b["sig"] for b in again[0].values()) != first


def fake_world(monkeypatch):
    """two pairs sharing one part; ghost lays the pair unless the first local choice (mid, mid-ish) is used"""
    p0, p1 = ("A_N", "A_P"), ("B_N", "B_P")
    term = lambda pr, x: {"name": "U1", "c": (x, 0.0), "p": {"ref": "U1", "x": x, "y": 0, "w": 0.3, "h": 0.5, "layers": {"F.Cu"}, "net": pr[1]}}
    info = {p0: ([term(p0, 0.0)], [], [], 0.0), p1: ([term(p1, 0.5)], [], [], 0.0)}
    rules = {p0: {"clr": 0.127}, p1: {"clr": 0.127}}
    key = {p0: (0, 0, 1.0), p1: (0, 0, 2.0)}
    doms = two_vars()
    monkeypatch.setattr(PE, "bundles", lambda ctx, pr, ti, rule, others, p: list(doms[(pr, ti)]))
    monkeypatch.setattr(PE, "part_box", lambda B, c: (0, 0, 1, 1))
    return p0, p1, info, rules, key


def test_far_end_conflict_planner_changes_the_first_choice(monkeypatch, tmp_path):
    p0, p1, info, rules, key = fake_world(monkeypatch)
    calls = []

    def ghost(prs, esc, rsv):
        calls.append(sorted(v["name"] for v in esc.values()))
        bad = {"left"}                                     # the far end cannot take the exit that fits locally: pair A fails there
        out = {}
        for q in prs:
            names = {v["name"] for k, v in esc.items() if k[0] == tuple(q)}
            ok = not (names & bad) and (esc != {} or False is True) if False else bool(esc) and not (names & bad)
            out[q] = {"full": ok, "skew": 0.0, "nok": int(ok), "e": {"status": "laid" if ok else "not-laid", "why": "" if ok else "no path"}}
        return out
    res = PE.plan(lambda: {"budget": {}}, ghost, info, rules, key, [p0, p1], {"pairlay": {"escape_cache_dir": str(tmp_path)}}, {}, lambda net: 1.0, order=[p0, p1])
    nh = res["report"]["neighbourhoods"][0]
    assert nh["verdict"] == "chosen", nh
    assert res["report"]["memory"]["forbidden"] >= 1                 # the first shortlisted combination failed and is forbidden
    names = {v["name"] for v in res["choice"].values()}
    assert "left" not in names and res["report"]["expansions"] > 0 and res["reserve"]


def test_healthy_neighbourhood_is_left_alone(monkeypatch, tmp_path):
    p0, p1, info, rules, key = fake_world(monkeypatch)
    ok = lambda prs, esc, rsv: {q: {"full": True, "skew": 0.0, "nok": 1, "e": {"status": "laid"}} for q in prs}
    res = PE.plan(lambda: {"budget": {}}, ok, info, rules, key, [p0, p1], {"pairlay": {"escape_cache_dir": str(tmp_path)}}, {}, lambda net: 1.0, order=[p0, p1])
    assert res["choice"] == {} and res["report"]["neighbourhoods"][0]["verdict"] == "baseline already laid"


def test_escape_can_be_switched_off():
    assert PE.params({"pairlay": {"escape": False}})["on"] is False
    assert PE.params({})["expansions"] == 10000 and PE.params({"pairlay": {"escape_expansions": 50}})["expansions"] == 50


def test_verification_set_is_geometric():
    p0, p1, p2 = ("A_N", "A_P"), ("B_N", "B_P"), ("C_N", "C_P")
    mk = lambda a, b: ([{"c": a}, {"c": b}], [], [(0, 1)], 1.0)
    info = {p0: mk((0, 0), (10, 0)), p1: mk((2, 3), (8, 3)), p2: mk((2, 40), (8, 40))}
    key = {p0: (1, 1, 1), p1: (0, 0, 1), p2: (0, 0, 1)}
    v = PE.verification_set(info, {}, key, [p0, p1, p2], [p0], [(4, -1, 6, 1)], 5.0)
    assert set(v) == {p0, p1}                                        # p1 crosses the neighbourhood, p2 does not


def twin_board(extra):
    row = [PB.pad(str(i + 1), 0.5 * i, 0, net, 0.3, 0.8) for i, net in enumerate(("DM", "DP", "DM", "DP"))]
    far = PB.fp("U8", 40.5, 45, [PB.pad("1", 0, 0, "DP", 0.3, 0.8), PB.pad("2", 0.8, 0, "DM", 0.3, 0.8)])
    return PB.fp("J5", 40, 60, row) + far + extra


def seg(a, b, net, layer="F.Cu"):
    return f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.15) (layer "{layer}") (net "{net}"))\n'


def via(x, y, net):
    return f'(via (at {x} {y}) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net "{net}"))\n'


def test_twin_tap_vias_are_spurs_not_part_of_the_pair_path(tmp_path):
    # DP: pad 2 leaves north to U8.1; pad 4 reaches pad 2 through tap vias and a B.Cu jumper; DM: pad 3 leaves, pad 1 is a plain jumper
    cu = seg((40.5, 60), (40.5, 45), "DP") + seg((41.5, 60), (41.5, 58.5), "DP") + via(41.5, 58.5, "DP") + via(40.5, 58.5, "DP") \
        + seg((41.5, 58.5), (40.5, 58.5), "DP", "B.Cu")
    B = RD.load(PB.write(tmp_path, twin_board(cu)))
    p = PA.profile(B, "DP")
    assert p["path"] and p["layers"] == ["F.Cu"] and p["vias"] == 0
    grp = PA.twin_group([q for q in B["pads"] if q["net"] == "DP" and q["ref"] == "J5"], 0)
    assert len(grp) == 2                                             # matched by footprint pattern (same net, same footprint, near)


def usbc_body():
    row = [PB.pad(str(i + 1), 0.5 * i, 0, net, 0.3, 0.8) for i, net in enumerate(("USB_DM", "USB_DP", "USB_DM", "USB_DP"))]
    duo = [PB.pad("1", 0, 0, "USB_DP", 0.3, 0.8), PB.pad("2", 0.8, 0, "USB_DM", 0.3, 0.8)]
    return PB.fp("J5", 40, 60, row) + PB.fp("U8", 40.5, 45, duo)


def test_twin_template_picks_the_polarity_right_middle_pair(tmp_path):
    B = RD.load(PB.write(tmp_path, usbc_body()))
    terms, lone = PR.terminals(B, "USB_DP", "USB_DM", 16.0, merge_twins=True)
    j5 = next(t for t in terms if t["p"]["ref"] == "J5" or t["name"] == "J5")
    tpl = j5["twin"]["template"]
    assert tpl["polarity_ok"] and tpl["primary"] == ("2", "3") and set(tpl["twins"]) == {"1", "4"}
    # an invalid row (nets not alternating) gives no template
    row = [q for q in B["pads"] if q["ref"] == "J5"]
    assert PR.twin_template(row[:3], "USB_DP", []) is None


def test_twin_receptacle_lays_with_tap_vias_and_passes_the_audit(tmp_path):
    b = PB.write(tmp_path, usbc_body())
    out = str(tmp_path / "o.kicad_pcb")
    rep = PR.run(b, {"pairlay": {"workers": 1}}, None, out, fallback=False)
    row = rep["pairs"][0]
    assert row["open"] == [0, 0], row
    B = RD.load(out)
    assert sum(1 for v in B["vias"] if v["net"] in ("USB_DM", "USB_DP")) >= 2          # the tap vias of the twin joins
    a = PA.audit(B, [("USB_DP", "USB_DM")], {}, set())[("USB_DP", "USB_DM")]
    assert set(a["causes"]) <= {"skew"} and a["status"] in ("complete", "mismatch"), a          # skew of the jumper is the tuner's, never layers or vias


def test_plan_is_cached_by_placement_and_constraints(monkeypatch, tmp_path):
    p0, p1, info, rules, key = fake_world(monkeypatch)
    cons = {"pairlay": {"escape_cache_dir": str(tmp_path)}}
    n = []

    def ghost(prs, esc, rsv):
        n.append(1)
        ok = bool(esc) and not any(v["name"] == "left" for v in esc.values())
        return {q: {"full": ok, "skew": 0.0, "nok": int(ok), "e": {"status": "laid" if ok else "not-laid", "why": ""}} for q in prs}
    monkeypatch.setattr(PE, "_variants_of", lambda ctx, t, rule, pr, nmax: [d["var"] for d in two_vars()[(pr, 0)]])
    B = {"pads": [{"ref": "U1", "num": "1", "net": "A_N", "x": 0, "y": 0, "w": 1, "h": 1, "layers": ["F.Cu"]}]}
    first = PE.plan(lambda: {"budget": {}}, ghost, info, rules, key, [p0, p1], cons, B, lambda net: 1.0, order=[p0, p1])
    calls = len(n)
    again = PE.plan(lambda: {"budget": {}}, ghost, info, rules, key, [p0, p1], cons, B, lambda net: 1.0, order=[p0, p1])
    assert len(n) == calls and again["report"]["cache"] == "hit"                 # the second plan lays nothing
    assert {k: v["name"] for k, v in again["choice"].items()} == {k: v["name"] for k, v in first["choice"].items()} and set(again["reserve"]) == set(first["reserve"])
    B2 = {"pads": [dict(B["pads"][0], x=5)]}                                    # a moved pad is a different placement: planned again
    PE.plan(lambda: {"budget": {}}, ghost, info, rules, key, [p0, p1], cons, B2, lambda net: 1.0, order=[p0, p1])
    assert len(n) > calls


def test_shortlist_has_no_heading_duplicates():
    p0, p1 = ("A_N", "A_P"), ("B_N", "B_P")
    d0 = [bundle(p0, 0, "mid", 1.0, 1.0)] + [dict(bundle(p0, 0, "mid", 1.0, 1.0 + 0.01 * k), dir=(0, k), sig=("mid", "F.Cu", (0, k))) for k in range(1, 5)] + [bundle(p0, 0, "left", 0.0, 3.0)]
    d1 = [bundle(p1, 0, "far", 5.0, 1.0)]
    sols, _ = PE.select({(p0, 0): d0, (p1, 0): d1}, 0.127, 10000, 5)
    assert len(sols) == 2                                            # (mid, far) and (left, far): the four heading variants of "mid" are one plan


def test_observed_victims_stay_in_the_verification_set_and_params_carry_the_watch():
    p0, p1, p2 = ("A_N", "A_P"), ("B_N", "B_P"), ("C_N", "C_P")
    mk = lambda a, b: ([{"c": a}, {"c": b}], [], [(0, 1)], 1.0)
    info = {p0: mk((0, 0), (10, 0)), p1: mk((2, 40), (8, 40)), p2: mk((2, 80), (8, 80))}
    key = {p0: (1, 1, 1), p1: (0, 0, 1), p2: (0, 0, 1)}
    v = PE.verification_set(info, {}, key, [p0, p1, p2], [p0], [(4, -1, 6, 1)], 5.0, watch={p2})
    assert set(v) == {p0, p2}                                        # p2's corridor misses the part: in by the watch only
    assert PE.params({"pairlay": {"escape_watch": ["PCIE_RX", "USB3"]}})["watch"] == ["PCIE_RX", "USB3"]


def test_failed_probe_records_victims_and_crosses_orders(monkeypatch, tmp_path):
    p0, p1, info, rules, key = fake_world(monkeypatch)
    v = ("V_N", "V_P")
    info[v] = ([{"name": "X", "c": (0.0, 9.0), "p": {"ref": "X"}}, {"name": "Y", "c": (9.0, 9.0), "p": {"ref": "Y"}}], [], [(0, 1)], 9.0)
    rules[v] = {"clr": 0.127}
    key[v] = (0, 0, 3.0)
    calls = []

    def ghost(prs, esc, rsv):
        calls.append((tuple(prs), bool(esc)))
        names = {x["name"] for x in esc.values()}
        out = {}
        for q in prs:
            ok = (bool(esc) and "left" not in names and q != p0) or (q == v and not esc) or (bool(esc) and q in (p0, p1) and "left" not in names) or (q == v and "left" not in names)
            out[q] = {"full": ok, "skew": 0.0, "nok": int(ok), "e": {"status": "laid" if ok else "not-laid", "why": ""}}
        return out
    cons = {"pairlay": {"escape_cache_dir": str(tmp_path), "escape_watch": ["V_"], "escape_cross": True}}
    res = PE.plan(lambda: {"budget": {}}, ghost, info, rules, key, [p0, p1, v], cons, {}, lambda net: 1.0, order=[p0, p1, v])
    rep = res["report"]
    assert "V_N/V_P" in rep["watch"] and any(c["order"] == "targets-reversed" for c in rep["cross"])
    assert {r["candidate"] for r in rep["cross"]} <= {0, 1} and all(set(r["scores"]) >= {"A_N/A_P", "B_N/B_P", "V_N/V_P"} for r in rep["cross"])


def test_bundles_shorten_the_reach_before_a_terminal_is_blocked(monkeypatch):
    """W16 J10/U12: a dense break-out has no clear 3 mm leg; a shorter leg is a legal bundle (was: 'a terminal has no legal bundle')"""
    seen = []

    def at(ctx, pr, ti, rule, others, p):
        seen.append(p["reach"])
        return [{"cost": 1.0}] if p["reach"] <= 1.5 else []
    monkeypatch.setattr(PE, "_bundles_at", at)
    assert PE.bundles({}, ("A_N", "A_P"), 0, {}, [], {"reach": 3.0}) == [{"cost": 1.0}]
    assert seen == [3.0, 1.5]
    monkeypatch.setattr(PE, "_bundles_at", lambda *a: [])
    assert PE.bundles({}, ("A_N", "A_P"), 0, {}, [], {"reach": 3.0}) == []


def test_a_blocked_terminal_does_not_abandon_the_neighbourhood(monkeypatch, tmp_path):
    """one terminal with no bundle keeps its generic exit; the other terminal is still planned jointly"""
    p0, p1, info, rules, key = fake_world(monkeypatch)
    doms = two_vars()
    monkeypatch.setattr(PE, "bundles", lambda ctx, pr, ti, rule, others, p: [] if pr == p1 else list(doms[(pr, ti)]))
    calls = []

    def ghost(prs, esc, rsv):
        calls.append(dict(esc))
        ok = bool(esc)
        return {q: {"full": ok, "skew": 0.0, "nok": int(ok), "e": {"status": "laid" if ok else "not-laid", "why": ""}} for q in prs}
    res = PE.plan(lambda: {"budget": {}}, ghost, info, rules, key, [p0, p1], {"pairlay": {"escape_cache_dir": str(tmp_path)}}, {}, lambda net: 1.0, order=[p0, p1])
    nh = res["report"]["neighbourhoods"][0]
    assert nh["verdict"] == "chosen", nh
    assert {k[0] for k in res["choice"]} == {p0} and any("generic exit" in w for w in nh["why"])


def _repair_world(monkeypatch, new_score):
    q0, q1 = ("A_N", "A_P"), ("B_N", "B_P")
    ok = {"full": True, "e": {"status": "laid"}}
    bad = {"full": False, "e": {"status": "partial"}}
    best = {"order": [q0, q1], "res": {q0: ok, q1: bad}, "score": (1, 0, 0.0)}
    info = {q0: ([], [], [(0, 1)], 1.0), q1: ([], [], [(0, 1)], 1.0)}
    neg, calls = {"escape": {}, "escape_reserve": {}}, []

    def fake_plan(*a, **kw):
        calls.append(kw.get("focus"))
        return {"choice": {("x", 0): {}}, "reserve": {}, "report": {"neighbourhoods": []}}
    monkeypatch.setattr(PE, "plan", fake_plan)
    monkeypatch.setattr(PR, "guard_limit", lambda cons, net: 1.0)
    run = lambda: PR._escape_repair(best, [q0, q1], [q0, q1], info, {}, {}, {}, {}, None, lambda order: {"order": order, "score": new_score}, {}, neg, None, None, lambda m: None)
    return best, neg, calls, run, q1


def test_escape_repair_plans_only_for_a_failed_pair_and_keeps_only_a_gain(monkeypatch):
    best, neg, calls, run, q1 = _repair_world(monkeypatch, (2, 0, 0.0))
    st = run()
    assert calls == [{q1}] and st["score"] == (2, 0, 0.0) and neg["escape"]
    best, neg, calls, run, q1 = _repair_world(monkeypatch, (1, 0, 0.0))
    assert run() is best and not neg["escape"] and not neg["escape_reserve"]          # no gain: the choice is dropped


def test_escape_repair_is_free_when_every_pair_laid(monkeypatch):
    best, neg, calls, run, q1 = _repair_world(monkeypatch, (2, 0, 0.0))
    best["res"][q1] = {"full": True, "e": {"status": "laid"}}
    assert run() is best and calls == []


def _pure_ghost(prs, esc, rsv):
    ok = bool(esc) and not any(v["name"] == "left" for v in esc.values())
    return {q: {"full": ok, "skew": 0.0, "nok": int(ok), "e": {"status": "laid" if ok else "not-laid", "why": ""}} for q in prs}


def test_parallel_probes_give_the_serial_plan_and_emit_progress(monkeypatch, tmp_path):
    """fanout speed (w103): probes spread over workers are judged in shortlist order, so the plan equals the one-worker plan; every
    probe and neighbourhood emits an iteration event."""
    from fluxplace import events as EV
    ev = []
    monkeypatch.setattr(EV, "iteration", lambda loop, i, n=None, metrics=None, note="": ev.append((loop, i)))
    p0, p1, info, rules, key = fake_world(monkeypatch)
    monkeypatch.setattr(PE, "_variants_of", lambda ctx, t, rule, pr, nmax: [d["var"] for d in two_vars()[(pr, 0)]])
    out = {}
    for w in (1, 4):
        cons = {"pairlay": {"escape_cache_dir": str(tmp_path / f"w{w}"), "escape_workers": w}}
        r = PE.plan(lambda: {"budget": {}}, _pure_ghost, info, rules, key, [p0, p1], cons, {}, lambda net: 1.0, order=[p0, p1])
        r["report"].pop("secs", None)
        out[w] = ({k: v["name"] for k, v in r["choice"].items()}, r["reserve"], r["report"])
    assert out[1] == out[4]
    assert any(l == "escape" for l, _i in ev) and any(l == "escape-hood" for l, _i in ev)


def test_pmap_keeps_input_order_across_workers():
    assert PE.pmap(lambda a, b: a * 10 + b, [(i, 1) for i in range(7)], 3) == [i * 10 + 1 for i in range(7)]
    assert PE.pmap(lambda a: a, [(1,)], 4) == [1]


def _coarse_dist_reference(free, viaok, goals, f, step, via_cost):
    """the original Jacobi relaxation, one array op per layer and direction (kept as the oracle for the fused one)"""
    import numpy as np
    from fluxplace import pocket as PK
    nl = len(free)
    cj, ci = free[0].shape[0] // f, free[0].shape[1] // f
    C = np.stack([a[:cj * f, :ci * f].reshape(cj, f, ci, f).any(axis=(1, 3)) for a in free])
    Vc = viaok[:cj * f, :ci * f].reshape(cj, f, ci, f).any(axis=(1, 3)) if viaok is not None and nl > 1 else None
    d = np.full(C.shape, PK.INF, np.float32)
    for li, j, i in goals:
        if j // f < cj and i // f < ci:
            d[li, j // f, i // f] = 0.0
    w1, w2 = np.float32(step * f), np.float32(step * f * math.sqrt(2))
    dirs = [(0, 1, w1), (0, -1, w1), (1, 0, w1), (-1, 0, w1), (1, 1, w2), (1, -1, w2), (-1, 1, w2), (-1, -1, w2)]
    for _ in range(4 * max(cj, ci)):
        nd = d.copy()
        for l in range(nl):
            for dy, dx, w in dirs:
                np.minimum(nd[l], PK._shift(d[l], dy, dx, PK.INF) + w, out=nd[l])
        if Vc is not None:
            m = d.min(axis=0) + np.float32(via_cost)
            for l in range(nl):
                np.minimum(nd[l], np.where(Vc, m, PK.INF), out=nd[l])
        nd[~C] = PK.INF
        for li, j, i in goals:
            if j // f < cj and i // f < ci:
                nd[li, j // f, i // f] = 0.0
        if np.array_equal(nd, d):
            break
        d = nd
    return d


def test_coarse_dist_is_bit_identical_to_the_reference():
    import numpy as np
    from fluxplace import pairlay as PL
    rng = np.random.default_rng(7)
    for nl, vi in ((1, False), (3, True), (3, False)):
        free = [rng.random((83, 61)) > 0.3 for _ in range(nl)]
        viaok = rng.random((83, 61)) > 0.4 if vi else None
        goals = [(0, 5, 7), (nl - 1, 70, 50), (0, 999, 3)]
        a = PL.coarse_dist(free, viaok, goals, 4, 0.1, 20.0)
        b = _coarse_dist_reference(free, viaok, goals, 4, 0.1, 20.0)
        assert a.dtype == b.dtype and np.array_equal(a, b)


def test_pmap_falls_back_to_serial_in_a_thread_and_on_a_stuck_pool():
    import os
    import threading
    import time
    pid = os.getpid()
    f = lambda a: (a, os.getpid() == pid)
    got = []
    t = threading.Thread(target=lambda: got.append(PE.pmap(f, [(1,), (2,), (3,)], 3)))
    t.start()
    t.join()
    assert got[0] == [(1, True), (2, True), (3, True)]                          # not the main thread: no fork
    stuck = lambda a: (time.sleep(30) if os.getpid() != pid else None, a)[1]
    assert PE.pmap(stuck, [(1,), (2,)], 2, timeout=0.5) == [1, 2]                # a stuck worker: serial answer


def test_pmap_deadline_returns_none_for_unfinished_calls_and_never_reruns():
    """route-10 pairs 2026-11-27: an escape probe batch ran 40+ min past the 900 s escape budget with no event."""
    import time
    from fluxplace import pairescape as PE
    t = time.time()
    got = PE.pmap(lambda s: (time.sleep(s), s)[1], [(0.01,), (5,), (5,)], 3, deadline=time.time() + 1.0)
    assert got[0] == 0.01 and got[1] is None and got[2] is None and time.time() - t < 3


def test_escape_cross_diagnostic_is_off_by_default():
    from fluxplace import pairescape as PE
    assert PE.params({})["cross"] is False and PE.params({"pairlay": {"escape_cross": True}})["cross"] is True
