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
    return {"pair": pr, "ti": ti, "var": {"name": name}, "vkey": (name, x, 0.0), "layer": "F.Cu", "dir": (0, 1), "foot": [seg], "own": {},
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


def test_far_end_conflict_planner_changes_the_first_choice(monkeypatch):
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
    res = PE.plan(lambda: {"budget": {}}, ghost, info, rules, key, [p0, p1], {}, {}, lambda net: 1.0, order=[p0, p1])
    nh = res["report"]["neighbourhoods"][0]
    assert nh["verdict"] == "chosen", nh
    assert res["report"]["memory"]["forbidden"] >= 1                 # the first shortlisted combination failed and is forbidden
    names = {v["name"] for v in res["choice"].values()}
    assert "left" not in names and res["report"]["expansions"] > 0 and res["reserve"]


def test_healthy_neighbourhood_is_left_alone(monkeypatch):
    p0, p1, info, rules, key = fake_world(monkeypatch)
    ok = lambda prs, esc, rsv: {q: {"full": True, "skew": 0.0, "nok": 1, "e": {"status": "laid"}} for q in prs}
    res = PE.plan(lambda: {"budget": {}}, ok, info, rules, key, [p0, p1], {}, {}, lambda net: 1.0, order=[p0, p1])
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
