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
    "turned_parts": [{"ref": "R7", "x": 10.0, "y": 5.0, "rot": 90, "saves_mm": 1.5, "swap_pose": [11.0, 5.5, 180]}],
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
    assert b["turned"] == [{"ref": "R7", "x": 10.0, "y": 5.0, "rot": 90, "saves": 1.5, "swap": (11.0, 5.5, 180)}]


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
            "turned": [{"ref": "R7", "x": 30.0, "y": 30.0, "rot": 90, "saves": 1.5, "swap": (30.0, 30.0, 270)}],
            "source": "route_review"}


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
            out.append({"tag": j["tag"], "routed": f"/routed/{cid}", "blame": {"k": keys[cid]}, "key": [0, 0, 0] + list(keys[cid]),
                        "png": None})
        return out

    monkeypatch.setattr(PTm, "judge", lambda a, b, outdir, P, cons=None, spec=None, log=None:
                        {"routed": "/routed/input", "blame": {"k": keys["input"]}, "source": "route_review", "secs": 0})
    monkeypatch.setattr(PTm, "class_key", lambda routed, blame, cons=None, spec=None: (0, 0, 0) + tuple(blame["k"]))
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


def _seg(a, b, net, locked=False):
    return {"a": a, "b": b, "net": net, "w": 0.2, "layer": "F.Cu", "mid": None, "locked": locked}


def _pad(ref, x, y, net):
    return {"ref": ref, "num": "1", "net": net, "x": x, "y": y, "ex": 0.1, "ey": 0.1}


def test_stub_detection_one_part_vs_two_part_piece():
    from fluxplace import placetour as PTm
    B = {"parts": {}, "vias": [], "bbox": (0, 0, 50, 50),
         "segs": [_seg((10, 10), (12, 10), "N1", locked=True), _seg((12, 10), (14, 10), "N1", locked=True),
                  _seg((30, 10), (32, 10), "N2"), _seg((32, 10), (34, 10), "N2")],
         "pads": [_pad("U1", 10, 10, "N1"), _pad("R2", 14, 10, "N1"), _pad("R3", 30, 10, "N2")]}
    pcs = sorted(PTm._pieces(B), key=lambda p: p["segs"])
    assert pcs[0]["refs"] == ["R2", "U1"] and PTm._stub_of(pcs[0]) is None      # joins two parts: a route, not a stub
    assert pcs[1]["refs"] == ["R3"] and PTm._stub_of(pcs[1]) == "R3"           # touches one part only: stub of R3


def test_stub_copper_does_not_block_its_part_but_route_does(monkeypatch):
    from fluxplace import placetour as PTm
    B = {"parts": {"U1": {"pos": (10, 10), "angle": 0, "pads": [{}], "locked": False},
                   "R2": {"pos": (14, 10), "angle": 0, "pads": [{}], "locked": False},
                   "R3": {"pos": (30, 10), "angle": 0, "pads": [{}], "locked": False}},
         "vias": [], "bbox": (0, 0, 50, 50), "fixed": (),
         "segs": [_seg((10, 10), (12, 10), "N1", locked=True), _seg((12, 10), (14, 10), "N1", locked=True),
                  _seg((30, 10), (32, 10), "N2", locked=True)],
         "pads": [_pad("U1", 10, 10, "N1"), _pad("R2", 14, 10, "N1"), _pad("R3", 30, 10, "N2"), _pad("R3", 32, 10, "N2")]}
    monkeypatch.setattr(PTm.RB, "_fixed_set", lambda B, cons: set())
    mov = PTm._movable(B, {}, PTm.params({"placetour": {"move_routed": True}}))
    assert "R2" not in mov and "U1" not in mov                                 # locked two-part route blocks both
    assert "R3" in mov                                                         # only its own locked stub: movable


def test_carry_copper_translate_and_quarter_turn_exact():
    from fluxplace import boardwrite as BW
    t = ('(kicad_pcb (segment (start 1 2) (end 3 2) (width 0.2) (layer "F.Cu") (net 1) (locked yes)) '
         '(via (at 5 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1)))')
    out = BW.carry_copper(t, {"U1": ((0, 0, 0), (10, 0, 0))}, [{"ref": "U1", "segs": [0], "vias": [0]}])
    assert "(start 11 2)" in out and "(end 13 2)" in out and "(at 15 5)" in out and "(locked yes)" in out
    out = BW.carry_copper(t, {"U1": ((0, 0, 0), (10, 0, 90))}, [{"ref": "U1", "segs": [0], "vias": [0]}])
    assert "(start 12 -1)" in out and "(end 12 -3)" in out and "(at 15 -5)" in out     # KiCad: +90 turns CCW on screen


def test_box_parts_fills_empty_hot_area_from_pads():
    from fluxplace import placetour as PTm
    B = {"pads": [_pad("C5", 3.0, 3.0, "N"), _pad("U9", 20, 20, "N"), _pad("R1", 4.5, 3.0, "N")]}
    assert PTm._box_parts(B, [1.0, 2.0, 3.0, 4.0], grow=2.0) == ["C5", "R1"]    # R1 is 1.5 mm outside the box: within 2 mm
    assert PTm._box_parts(B, [1.0, 2.0, 3.0, 4.0], grow=0.5) == ["C5"]


def _clr_board(foreign_track_y=10.0, via_y=None, pad=None):
    B = {"parts": {}, "bbox": (0, 0, 50, 50), "clearance": 0.14, "pads": [], "segs": [], "vias": []}
    B["segs"].append({"a": (0.0, foreign_track_y), "b": (20.0, foreign_track_y), "w": 0.2, "layer": "F.Cu", "net": "N2",
                      "mid": None, "locked": False})
    B["vias"].append({"x": 10.0, "y": via_y if via_y is not None else 10.0 + 0.45, "d": 0.6, "net": "N1",
                      "layers": {"F.Cu", "B.Cu"}, "locked": False, "drill": 0.3})
    if pad is not None:
        B["pads"].append(pad)
    return B


def test_carried_via_too_close_to_foreign_track_is_dropped():
    from fluxplace import placetour as PTm
    B = _clr_board(via_y=10.0 + 0.45)                        # copper gap 0.45 - 0.3 - 0.1 = 0.05 mm < 0.14 mm
    assert PTm.carried_clearance(B, {"segs": {}, "vias": {0: "U1"}}) == "U1"


def test_carried_via_one_mm_away_passes():
    from fluxplace import placetour as PTm
    B = _clr_board(via_y=11.0)                               # gap 1.0 - 0.4 = 0.6 mm
    assert PTm.carried_clearance(B, {"segs": {}, "vias": {0: "U1"}}) is None


def test_carried_track_near_foreign_pad_and_same_net_ignored():
    from fluxplace import placetour as PTm
    pad = {"ref": "R9", "num": "1", "net": "N2", "x": 5.0, "y": 11.0, "ex": 0.5, "ey": 0.25, "w": 1.0, "h": 0.5,
           "shape": "rect", "angle": 0, "layers": {"F.Cu"}}
    B = _clr_board(pad=pad)
    B["segs"].append({"a": (4.0, 11.2), "b": (6.0, 11.2), "w": 0.2, "layer": "F.Cu", "net": "N1", "mid": None, "locked": False})
    # carried track N1 at y=11.2 with half width 0.1 against the pad's top edge at y=11.25: overlaps -> violation
    assert PTm.carried_clearance(B, {"segs": {1: "R1"}, "vias": {}}) == "R1"
    pad2 = dict(pad, net="N1")                                 # same net as the carried track: no clearance check
    B["pads"] = [pad2]
    assert PTm.carried_clearance(B, {"segs": {1: "R1"}, "vias": {}}) is None


def test_stage_reports_honoured_rows_on_the_written_board(monkeypatch, tmp_path):
    import json
    from fluxplace import placetour as PTm
    keys = {"input": (5, 1, 0.5, 0, 1.0), "A": (3, 0, 0.0, 0, 0.5)}
    board = _script_stage(monkeypatch, tmp_path, [["A"], []], keys)
    c = _stage_ctx(tmp_path)
    from fluxplace import ready as RDY, budget as BG
    far = BG.row("separation", "far", "distance", "mm", 3.0, None, 900.0, "ds p.1", hard=True, minimum=True)
    far_ok = BG.row("separation", "far", "distance", "mm", 950.0, None, 900.0, "ds p.1", hard=True, minimum=True)
    # the synthetic board has no copper model: wiring only. Stub by path: the input board measures 950, the written one 3.
    monkeypatch.setattr(RDY, "measure_rows", lambda spec, cons, B, path, **k: ([far_ok] if path == board else [far], []))
    ev = tmp_path / "ev" / "events.jsonl"                 # the live-view work folder: the PLACE_OK gate is opened, not waited on
    monkeypatch.setenv("FLUXPLACE_EVENTS", str(ev))
    c.cons["live"] = {"gate": {"PLACE_OK": {"wait_min": 0}}}
    from fluxplace import liveview as LV

    class _NoOwner:                          # the synthetic board has no copper model, so no Feedback: an owner with no rows
        dir = str(ev.parent / "feedback")
        open_gates, answers, stage = {}, {}, "placetour"
        def on(self): return True
        def boundary(self, **k): pass
        def finish(self, B): return []
    monkeypatch.setattr(LV, "stage_start", lambda board, cons, spec, stage, wd, apply=True: (board, cons, _NoOwner()))
    r = PTm.stage(c, 3, board)
    assert any(l.startswith("rows on the output: 0 honoured, 1 hard not honoured (far)") for l in r["next"])
    d = c.dir(3, "placetour")
    bj = json.load(open(os.path.join(d, "board.budgets.json")))
    assert bj["honoured"][0]["value_in"] == 950.0 and bj["honoured"][0]["value_out"] == 3.0
    lines = [json.loads(l) for l in open(ev)]
    pg = [l for l in lines if l["kind"] == "placement_progress" and l.get("phase") == "gate" and l.get("cand") == "result"]
    assert [(x["id"], x["hard"], x["ok"]) for x in pg[0]["criteria"] if x["id"] == "far"] == [("far", True, False)]
    gate = next(l for l in lines if l["kind"] == "gate_open")
    assert [(x["id"], x["hard"], x["ok"]) for x in gate["candidates"][0]["criteria"] if x["id"] == "far"] == [("far", True, False)]


# ---- judge guard, minor gains, budgets
def _k(opens, hot=1, occ=0.5, vias=600, det=80.0, cls=(0, 0, 0)):
    return tuple(cls) + (opens, hot, occ, vias, det)


def test_beats_guard_cases():
    P = PT.params({})
    ref = _k(44, hot=1, occ=0.51, vias=609)
    assert PT._beats(_k(43, hot=1, occ=0.51, vias=610), ref, P)                       # plain gain, guard holds
    assert not PT._beats(ref, ref, P) and not PT._beats(_k(45), ref, P)               # not smaller
    assert PT.guard_why(_k(42, hot=3, occ=1.56, vias=632), ref, P) == "guard: hot 1->3"
    assert not PT._beats(_k(42, hot=3, occ=1.56, vias=632), ref, P)
    assert PT.guard_why(_k(42, hot=1, occ=0.51, vias=632), ref, P) == "guard: vias +23"
    assert PT._beats(_k(42, hot=1, occ=0.51, vias=627), ref, P)                       # +18 <= 3 % of 609
    assert PT._beats(_k(40, hot=3, vias=700), ref, P)                                 # gain 4 > guard_opens: guard off
    assert PT._beats(_k(44, hot=1, occ=0.51, vias=609, det=79.0), ref, P)            # no open gain: plain order, no guard
    assert PT._beats(_k(44, hot=2, occ=0.6, vias=609), _k(44, hot=3, occ=1.5, vias=609), P)
    assert PT._beats(_k(43, cls=(0, 0, 0)), _k(44, cls=(1, 0, 0)), P)                 # class opens differ: guard off
    assert PT._beats(_k(50, hot=9, vias=900, cls=(0, 0, 0)), _k(40, cls=(1, 0, 0)), P)
    assert PT.guard_why(_k(43, hot=2), _k(44, hot=1), PT.params({"placetour": {"guard_opens": 0}})) == ""    # guard_opens 0: off
    assert not PT._beats(_k(43, vias=640), ref, PT.params({"placetour": {"guard_vias_frac": 0.01}}))


def test_minor_gain_counts_as_stale_but_advances(monkeypatch, tmp_path):
    from fluxplace import placetour as PTm
    keys = {"input": (5, 1, 0.5, 600, 80.0), "A": (4, 1, 0.5, 600, 80.0), "B": (4, 1, 0.5, 598, 79.0), "C": (4, 1, 0.5, 590, 70.0)}
    board = _script_stage(monkeypatch, tmp_path, [["A"], ["B"], ["C"]], keys)
    c = _stage_ctx(tmp_path, {"max_rounds": 5, "no_gain_rounds": 2})
    r = PTm.stage(c, 3, board)
    d = c.dir(3, "placetour")
    prog = __import__("json").load(open(os.path.join(d, "progress.json")))
    assert prog["rounds"] == 3 and prog["reason"] == "no gain for 2 round(s)"      # A real, B and C decimal-only: stale 1, 2
    assert open(r["board"]).read() == "moved-C"                                       # the minor winners still advanced the board
    table = open(os.path.join(d, "TOURNAMENT.md")).read()
    assert "| r1-A |" in table and "winner" in table and "minor" in table


def test_stage_guard_refusal_in_verdict(monkeypatch, tmp_path):
    from fluxplace import placetour as PTm
    keys = {"input": (5, 1, 0.5, 600, 80.0), "A": (3, 3, 1.5, 640, 78.0), "B": (5, 1, 0.5, 600, 79.0)}
    board = _script_stage(monkeypatch, tmp_path, [["A", "B"]], keys)
    c = _stage_ctx(tmp_path, {"max_rounds": 1})
    r = PTm.stage(c, 3, board)
    table = open(os.path.join(c.dir(3, "placetour"), "TOURNAMENT.md")).read()
    assert "guard: hot 1->3" in table and open(r["board"]).read() == "moved-B"


def test_stage_budget_skips_a_round_that_does_not_fit(monkeypatch, tmp_path):
    from fluxplace import placetour as PTm
    keys = {"input": (9, 0, 0.0, 0, 0.0), "A": (8, 0, 0.0, 0, 0.0), "B": (7, 0, 0.0, 0, 0.0)}
    board = _script_stage(monkeypatch, tmp_path, [["A"], ["B"]], keys)
    now, fake = {"t": 1000.0}, PTm.LA._run_jobs

    def slow_jobs(*a, **k):
        now["t"] += 50.0                                                              # one judge = 50 s of a 1-minute budget
        return fake(*a, **k)
    monkeypatch.setattr(PTm.LA, "_run_jobs", slow_jobs)
    monkeypatch.setattr(PTm.time, "time", lambda: now["t"])
    c = _stage_ctx(tmp_path, {"max_rounds": 5, "no_gain_rounds": 9, "budget_min": 1.0})
    PTm.stage(c, 3, board)
    prog = __import__("json").load(open(os.path.join(c.dir(3, "placetour"), "progress.json")))
    assert prog["rounds"] == 1 and "budget_min" in prog["reason"]


def test_params_budget_default():
    assert PT.params({})["budget_min"] == 45.0 and PT.params({"placetour": {"budget_min": "10"}})["budget_min"] == 10.0
