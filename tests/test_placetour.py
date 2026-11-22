"""PLACETOUR stage: judge + blame (P1), move generator (P2), tier-1 gates (P3), tournament stage (P4), report (P5). No pcbnew, no router."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import placetour as PT  # noqa: E402

REVIEW = {
    "open_nets": [{"net": "GND", "poured": True, "disconnected_pad_parts": ["C1"], "parts": ["C1"]},
                  {"net": "SIG1", "poured": False, "disconnected_pad_parts": ["U1", "R2"], "parts": ["U1", "R2"]},
                  {"net": "SIG2", "poured": False, "disconnected_pad_parts": [], "parts": ["R3"]}],
    "most_vias": [{"net": "SIG1", "pads": 3, "vias": 4, "detour": 1.2, "parts": ["U1", "R2"]}],
    "longest_detours": [{"net": "SIG1", "pads": 3, "vias": 1, "detour": 2.0, "parts": ["R2", "R9"]},
                        {"net": "SIG3", "pads": 2, "vias": 0, "detour": 1.6, "parts": ["R4"]}],
    "congested_areas": [{"box": [1.0, 2.0, 3.0, 4.0], "cells": 6, "peak": 0.8, "mean": 0.6, "vias": 2,
                         "parts": ["U1", "C5"], "nets": ["SIG1"]}],
    "turned_parts": [{"ref": "R7", "x": 10.0, "y": 5.0, "rot": 90, "saves_mm": 1.5}],
    "counts": {},
}


def test_blame_parses_review_fixture():
    b = PT.blame_of(REVIEW)
    assert b["source"] == "route_review"
    assert b["open"] == [{"net": "SIG1", "refs": ["R2", "U1"]}, {"net": "SIG2", "refs": ["R3"]}]    # poured GND left out
    nets = {c["net"]: c for c in b["costly"]}
    assert nets["SIG1"]["vias"] == 4 and nets["SIG1"]["detour"] == 2.0 and nets["SIG1"]["refs"] == ["R2", "R9", "U1"]
    assert [c["net"] for c in b["costly"]] == ["SIG1", "SIG3"]                                      # sorted by detour
    assert b["hot"] == [{"cells": 6, "occ": 0.8, "refs": ["C5", "U1"], "nets": ["SIG1"], "bbox": [1.0, 2.0, 3.0, 4.0], "vias": 2}]
    assert b["turned"] == [{"ref": "R7", "x": 10.0, "y": 5.0, "rot": 90, "saves": 1.5}]


def test_judge_key_orders_by_opens_then_hot_then_vias(monkeypatch):
    vias = {"a": 5, "b": 2, "c": 2}
    monkeypatch.setattr(PT.RD, "load", lambda p: {"vias": [0] * vias[p]})
    base = {"open": [{"net": "N", "refs": []}], "costly": [{"detour": 1.0}], "hot": [{"occ": 0.5}], "turned": []}
    more_open = dict(base, open=base["open"] * 2)
    more_hot = dict(base, hot=[{"occ": 0.5}, {"occ": 0.6}])
    assert PT.judge_key("a", base) < PT.judge_key("a", more_open)                                   # opens dominate
    assert PT.judge_key("b", base) < PT.judge_key("b", more_hot)                                    # then hot areas
    assert PT.judge_key("b", base) < PT.judge_key("a", base)                                        # then vias
    assert PT.judge_key(None, base) > PT.judge_key("a", more_open)                                  # no routed copy: worst


def test_judge_falls_back_to_predictor_without_routed_copy(monkeypatch, tmp_path):
    class A:
        router_dir, router_py = "/nowhere", "/usr/bin/python3"
    monkeypatch.setattr(PT.LA, "lean_krt", lambda a, board, outdir, budget: None)
    monkeypatch.setattr(PT, "_predictor_blame", lambda board, cons, spec: {"open": [], "costly": [], "hot": [], "turned": [],
                                                                         "source": "planroute"})
    j = PT.judge(A(), "in.kicad_pcb", str(tmp_path / "unused"), PT.params({}))
    assert j["routed"] is None and j["source"] == "planroute"


def test_judge_uses_route_review_blame_when_routed(monkeypatch, tmp_path):
    class A:
        router_dir, router_py = "/nowhere", "/usr/bin/python3"
    monkeypatch.setattr(PT.LA, "lean_krt", lambda a, board, outdir, budget: "/x/routed.kicad_pcb")
    monkeypatch.setattr(PT, "_review", lambda a, routed, rev: PT.blame_of(REVIEW))
    j = PT.judge(A(), "in.kicad_pcb", str(tmp_path / "r1"), PT.params({"placetour": {"judge_min": 2}}))
    assert j["routed"] == "/x/routed.kicad_pcb" and j["source"] == "route_review"


def test_params_defaults_and_enable():
    P = PT.params({})
    assert P["enabled"] is False and P["max_rounds"] == 10 and P["judge_top"] == 4 and P["move_routed"] is True
    assert PT.params({"placetour": {"enabled": True, "max_rounds": "3"}})["max_rounds"] == 3


def _board():
    """synthetic board: U1 (16 pads) and R2 (2 pads) in a hot area, F1 fixed, R7 turned."""
    def part(x, y, n, side="F"):
        return {"pos": (x, y), "angle": 0.0, "pads": [{"num": str(i)} for i in range(n)], "side": side, "tht": False,
                "court": (x - 1, y - 1, x + 1, y + 1), "locked": False}
    parts = {"U1": part(10, 10, 16), "R2": part(12, 10, 2), "F1": part(20, 20, 2), "R7": part(30, 30, 2)}
    pads = [{"ref": "U1", "num": "1", "net": "SIG1", "x": 9, "y": 10, "ex": 0.2, "ey": 0.2},
            {"ref": "R2", "num": "1", "net": "SIG1", "x": 12, "y": 10, "ex": 0.2, "ey": 0.2},
            {"ref": "R7", "num": "1", "net": "SIG2", "x": 30, "y": 30, "ex": 0.2, "ey": 0.2},
            {"ref": "F1", "num": "1", "net": "SIG2", "x": 20, "y": 20, "ex": 0.2, "ey": 0.2}]
    return {"parts": parts, "segs": [], "vias": [], "pads": pads, "bbox": (0.0, 0.0, 50.0, 50.0)}


def _blame():
    return {"open": [{"net": "SIG1", "refs": ["R2", "U1"]}], "costly": [{"net": "SIG1", "vias": 0, "detour": 2.0, "refs": ["U1", "R2"]}],
            "hot": [{"cells": 4, "occ": 0.9, "refs": ["U1", "R2", "F1"], "nets": ["SIG1"], "bbox": [8.0, 8.0, 14.0, 12.0], "vias": 0}],
            "turned": [{"ref": "R7", "x": 30.0, "y": 30.0, "rot": 90, "saves": 1.5}], "source": "route_review"}


def _identity_legalise(calls):
    def fake(B, targets, fixed=(), cons=None, strict=True, **k):
        calls.append(set(fixed))
        return dict(targets), {}
    return fake


def test_candidates_never_move_fixed_or_group_parts(monkeypatch):
    B = _board()
    calls = []
    monkeypatch.setattr(PT.RB, "_fixed_set", lambda B, cons: {"F1"})
    monkeypatch.setattr(PT.LG, "legalise", _identity_legalise(calls))
    monkeypatch.setattr(PT.PP, "plan_place", lambda Bc, cons, spec, **k: ({"F1": (1, 1, 0), "U1": (11.0, 10.0, 0)}, {"flips": []}))
    cands = PT.candidates(B, _blame(), {"placement": {"group": {"g": {"refs": ["R7"]}}}}, PT.params({"placetour": {"cands": 50}}))
    assert cands
    for c in cands:
        assert "F1" not in c["moves"] and "R7" not in c["moves"]                  # fixed and group member never move
    assert calls and all("F1" in fx for fx in calls)                             # legaliser: fixed parts are obstacles


def test_candidates_dedupe_and_round_robin(monkeypatch):
    B = _board()
    monkeypatch.setattr(PT.RB, "_fixed_set", lambda B, cons: set())
    monkeypatch.setattr(PT.LG, "legalise", _identity_legalise([]))
    monkeypatch.setattr(PT.PP, "plan_place", lambda Bc, cons, spec, **k: ({}, {"flips": []}))
    cands = PT.candidates(B, _blame(), {}, PT.params({"placetour": {"cands": 12}}))
    keys = [PT._key(c["moves"]) for c in cands]
    assert len(keys) == len(set(keys))                                           # identical move sets are dropped
    turn = [c for c in cands if c["kind"] == "turn"]
    assert len(turn) == 1 and turn[0]["moves"] == {"R7": (30.0, 30.0, 270)}      # one turned part: +180 deg, once
    assert cands[0]["kind"] == "turn"
    small = PT.candidates(B, _blame(), {}, PT.params({"placetour": {"cands": 2}}))
    assert len(small) == 2 and small[0]["kind"] == "turn"                       # round-robin starts with the cheapest kind


def test_nudge_moves_hot_parts_away_from_centroid(monkeypatch):
    B = _board()
    monkeypatch.setattr(PT.RB, "_fixed_set", lambda B, cons: {"F1"})
    monkeypatch.setattr(PT.LG, "legalise", _identity_legalise([]))
    monkeypatch.setattr(PT.PP, "plan_place", lambda Bc, cons, spec, **k: ({}, {"flips": []}))
    b = _blame()
    b["turned"] = []
    cands = PT.candidates(B, b, {}, PT.params({"placetour": {"cands": 50, "nudge_mm": [1.0]}}))
    nudge = [c for c in cands if c["kind"] == "nudge"]
    assert nudge, "a hot area with movable parts gives a nudge"
    x, y, _a = nudge[0]["moves"]["R2"]
    assert x > 12.0 and "F1" not in nudge[0]["moves"]                            # pushed away from centroid (10,10)


def test_worse_gates_counts_and_new_members():
    base = {"collision": 1, "intent": frozenset({"pos:a"}), "connector": 0, "side": 0, "height": 0,
            "keepout": frozenset(), "outline": frozenset()}
    same = dict(base)
    assert PT.worse_gates(same, base) == []
    assert PT.worse_gates(dict(base, collision=2), base) == ["collision"]
    assert PT.worse_gates(dict(base, intent=frozenset({"pos:a", "pos:b"})), base) == ["intent"]
    assert PT.worse_gates(dict(base, intent=frozenset()), base) == []              # a fixed row is not worse
    assert PT.worse_gates(dict(base, outline=frozenset({"R2"})), base) == ["outline"]


def test_tier1_drops_gate_worsening_and_ranks_rest(monkeypatch, tmp_path):
    base = str(tmp_path / "base.kicad_pcb")
    open(base, "w").write("x")
    gates = {"good1": {"collision": 0}, "bad": {"collision": 3}, "good2": {"collision": 0}}

    def fake_apply(board, moves, out, move_routed):
        open(out, "w").write("x")
        return True, [], {"segs": [], "vias": []}

    def fake_state(B, cons, spec):
        return {"collision": gates[B["name"]]["collision"]}

    keys = {"good1": (2,), "good2": (1,)}
    monkeypatch.setattr(PT, "_apply", fake_apply)
    monkeypatch.setattr(PT.RD, "load", lambda p: {"name": os.path.basename(p).split(".")[0]})
    monkeypatch.setattr(PT, "gate_state", lambda B, cons, spec: fake_state(B, cons, spec))
    monkeypatch.setattr(PT.PL, "run", lambda B, cons, spec, ignore_routing=False: {"name": B["name"]})
    monkeypatch.setattr(PT.PL, "rank_key", lambda R: keys[R["name"]])
    cands = [{"id": n, "kind": "nudge", "moves": {}} for n in ("good1", "bad", "good2")]
    surv, dropped = PT.tier1(base, cands, {}, None, PT.params({}), str(tmp_path / "t1"), {"collision": 0})
    assert [s[1]["id"] for s in surv] == ["good2", "good1"]                       # ranked by predictor key, best first
    assert [d[0]["id"] for d in dropped] == ["bad"] and dropped[0][1] == "worse: collision"


def _stage_ctx(tmp_path, cons_extra=None):
    from types import SimpleNamespace
    cons = {"placetour": dict({"enabled": True, "max_rounds": 3, "no_gain_rounds": 1, "judge_top": 2}, **(cons_extra or {}))}
    a = SimpleNamespace(constraints=None, spec=None, router_dir="/r", router_py="/py", freerouting_jar=None, java=None)
    work = tmp_path / "w"
    work.mkdir()
    def stage_dir(n, name):
        d = work / f"{n}-{name}"
        d.mkdir(exist_ok=True)
        return str(d)
    return SimpleNamespace(cons=cons, spec=None, a=a, dir=stage_dir)


def _script_stage(monkeypatch, tmp_path, rounds, keys):
    """stage with scripted candidates: rounds = [[ids surviving tier 1 in order]], keys = {id: judge key}."""
    from fluxplace import placetour as PTm
    board = str(tmp_path / "in.kicad_pcb")
    open(board, "w").write("input")
    state = {"r": -1}

    def fake_tier1(base, cands, cons, spec, P, outdir, base_state):
        state["r"] += 1
        surv = []
        os.makedirs(outdir, exist_ok=True)
        for i, cid in enumerate(rounds[state["r"]]):
            p = os.path.join(outdir, f"{cid}.kicad_pcb")
            open(p, "w").write(f"moved-{cid}")
            surv.append((tuple(keys[cid]), {"id": cid, "kind": "nudge", "moves": {"U1": (1.0, 1.0, 0)}}, p,
                         {"segs": [], "vias": []}))
        return surv, []

    def fake_jobs(jobs, P, timeout, entry="lookahead:_worker"):
        out = []
        for j in jobs:
            cid = j["tag"].split("-", 1)[1]
            out.append({"tag": j["tag"], "routed": f"/routed/{cid}", "blame": {"k": keys[cid]}, "key": list(keys[cid]),
                        "png": None})
        return out

    monkeypatch.setattr(PTm, "judge", lambda a, b, outdir, P, cons=None, spec=None, log=None:
                        {"routed": "/routed/input", "blame": {"k": keys["input"]}, "source": "route_review", "secs": 0})
    monkeypatch.setattr(PTm, "judge_key", lambda routed, blame, cons=None: tuple(blame["k"]))
    monkeypatch.setattr(PTm, "candidates", lambda B, blame, cons, P, seed=0, spec=None: [{"id": "x", "kind": "nudge", "moves": {}}])
    monkeypatch.setattr(PTm, "tier1", fake_tier1)
    monkeypatch.setattr(PTm, "gate_state", lambda B, cons, spec: {"collision": 0})
    monkeypatch.setattr(PTm.RD, "load", lambda p: _board())
    monkeypatch.setattr(PTm.LA, "_run_jobs", fake_jobs)
    monkeypatch.setattr(PTm.LA, "critical_nets", lambda b, cons, spec=None: [])
    monkeypatch.setattr(PTm.LA, "render", lambda board_, rects, fit, path: path)
    monkeypatch.setattr(PTm.SN, "active_take", lambda *a, **k: None)
    return board


def test_stage_skipped_when_disabled(tmp_path):
    from fluxplace import placetour as PTm
    from types import SimpleNamespace
    c = SimpleNamespace(cons={}, spec=None, a=None, dir=lambda n, name: str(tmp_path))
    r = PTm.stage(c, 3, "in.kicad_pcb")
    assert r["verdict"] == "skipped" and r["board"] == "in.kicad_pcb"


def test_stage_keeps_winner_moved_board_and_stops_on_no_gain(monkeypatch, tmp_path):
    from fluxplace import placetour as PTm
    keys = {"input": (5, 1, 0.5, 0, 1.0), "A": (3, 0, 0.0, 0, 0.5), "B": (4, 0, 0.0, 0, 0.5), "C": (3, 0, 0.0, 0, 0.5),
            "D": (3, 0, 0.0, 0, 0.5)}
    board = _script_stage(monkeypatch, tmp_path, [["A", "B"], ["C", "D"]], keys)
    c = _stage_ctx(tmp_path)
    r = PTm.stage(c, 3, board)
    assert r["verdict"] == "pass"
    out = r["board"]
    assert open(out).read() == "moved-A"                                       # the winner's moved board, not the judge copy
    d = c.dir(3, "placetour")
    prog = __import__("json").load(open(os.path.join(d, "progress.json")))
    assert prog["state"] == "done" and prog["rounds"] == 2 and prog["reason"] == "no gain for 1 round(s)"
    md = open(os.path.join(d, "placetour.md")).read()
    assert "| 1 | nudge | U1 | (10, 10, 0.0) | (1.0, 1.0, 0) | 0 |" in md
    table = open(os.path.join(d, "TOURNAMENT.md")).read()
    assert "| r1-A |" in table and "winner" in table


def test_stage_stops_at_max_rounds(monkeypatch, tmp_path):
    from fluxplace import placetour as PTm
    keys = {"input": (9, 0, 0.0, 0, 0.0), "A": (8, 0, 0.0, 0, 0.0), "B": (7, 0, 0.0, 0, 0.0), "C": (6, 0, 0.0, 0, 0.0),
            "D": (5, 0, 0.0, 0, 0.0)}
    board = _script_stage(monkeypatch, tmp_path, [["A"], ["B"], ["C"]], keys)
    c = _stage_ctx(tmp_path, {"no_gain_rounds": 9})
    r = PTm.stage(c, 3, board)
    prog = __import__("json").load(open(os.path.join(c.dir(3, "placetour"), "progress.json")))
    assert prog["rounds"] == 3 and prog["reason"] == "max_rounds"
    assert open(r["board"]).read() == "moved-C"


def test_report_hint_lists_removed_copper_refs_and_group_members(monkeypatch, tmp_path):
    from fluxplace import placetour as PTm
    keys = {"input": (5, 1, 0.5, 0, 1.0), "A": (3, 0, 0.0, 0, 0.5)}
    board = _script_stage(monkeypatch, tmp_path, [["A"], []], keys)
    monkeypatch.setattr(PTm.RL, "attached_copper", lambda B: {"segs": {0: [(0, "U1", "1")]}, "vias": {}})
    orig_tier1 = PTm.tier1

    def tier1_with_removed_track(*a, **k):                                      # the candidate's move drops track 0
        surv, dropped = orig_tier1(*a, **k)
        return [(s[0], s[1], s[2], {"segs": [0], "vias": []}) for s in surv], dropped
    monkeypatch.setattr(PTm, "tier1", tier1_with_removed_track)
    c = _stage_ctx(tmp_path, {"no_gain_rounds": 1})
    c.cons["placement"] = {"group": {"g": {"refs": ["R7", "R8"]}}}
    r = PTm.stage(c, 3, board)
    assert "fanout --parts U1" in " ".join(r["next"])                             # moved part lost its copper: fan out again
    md = open(os.path.join(c.dir(3, "placetour"), "placetour.md")).read()
    assert "R7, R8" in md and "U1" in md
