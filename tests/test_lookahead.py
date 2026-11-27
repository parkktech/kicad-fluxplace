"""LOOKAHEAD stage: demand map, reservations, ghost/fitness/render helpers and the round loop (no pcbnew, no router)."""
from fluxplace import procs as _procs_mod
import json
import subprocess
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import lookahead as LA
from fluxplace import routediag as RD


def board(segs=(), vias=(), pads=(), zones=(), copper=("F.Cu", "B.Cu"), bbox=(0.0, 0.0, 10.0, 10.0)):
    return {"segs": list(segs), "vias": list(vias), "pads": list(pads), "zones": list(zones),
            "copper": list(copper), "bbox": bbox}


def seg(net, a, b, layer="F.Cu", mid=None):
    return {"net": net, "a": a, "b": b, "w": 0.2, "mid": mid, "layer": layer, "locked": False}


def pad(net, x, y):
    return {"net": net, "x": x, "y": y, "layers": {"F.Cu"}, "ref": "U1", "num": "1"}


@pytest.fixture
def load(monkeypatch):
    def install(B):
        monkeypatch.setattr(RD, "load", lambda path, **kw: B)
        return B
    return install


P0 = LA.params({"lookahead": {"enabled": True}})


def test_params_defaults_and_overrides():
    P = LA.params(None)
    assert P["enabled"] is False and P["cell_mm"] == 1.0 and P["min_nets"] == 2 and P["max_rounds"] == 3
    assert P["jobs"] == 4 and P["no_gain_rounds"] == 1 and P["verify_top"] == 2 and P["ghost_min"]["verify"] == 10.0
    assert P["variants"] == [(2, 3.0), (1, 3.0), (3, 2.0)]
    Q = LA.params({"lookahead": {"enabled": True, "cell_mm": "0.5", "variants": [[1, 2]]}})
    assert Q["enabled"] is True and Q["cell_mm"] == 0.5 and Q["variants"] == [(1, 2.0)]
    assert LA.params({"lookahead": {"enabled": "yes"}})["enabled"] is False


def test_demand_rasterises_non_critical_segment(load):
    load(board(segs=[seg("N1", (0.5, 0.5), (5.5, 0.5))]))
    D = LA.demand("x.kicad_pcb", set(), P0)
    row = sorted(k for k in D["cells"] if k[0] == "F.Cu" and k[2] == 0)
    assert [k[1] for k in row] == [0, 1, 2, 3, 4, 5]
    assert all(D["cells"][k]["nets"] == {"N1"} and not D["cells"][k]["crit"] for k in row)
    assert D["origin"] == (0.0, 0.0) and D["cell"] == 1.0


def test_critical_copper_goes_to_crit_not_nets(load):
    load(board(segs=[seg("P", (0.5, 2.5), (3.5, 2.5))]))
    D = LA.demand("x", {"P"}, P0)
    cl = D["cells"][("F.Cu", 1, 2)]
    assert cl["crit"] == {"P"} and cl["nets"] == set()


def test_plane_and_unnamed_nets_are_skipped(load):
    zone = {"net": "GND", "layers": {"F.Cu"}, "pts": [], "bbox": (0, 0, 1, 1)}
    load(board(segs=[seg("GND", (0.5, 0.5), (3.5, 0.5)), seg("", (0.5, 5.5), (3.5, 5.5))], zones=[zone]))
    D = LA.demand("x", set(), P0)
    assert D["cells"] == {} or all(not cl["nets"] for cl in D["cells"].values())


def test_via_marks_every_spanned_copper_layer(load):
    cu = ["F.Cu", "In1.Cu", "B.Cu"]
    via = {"net": "N2", "x": 4.5, "y": 4.5, "d": 0.6, "drill": 0.3, "layers": {"F.Cu", "B.Cu"}, "locked": False}
    load(board(vias=[via], copper=cu))
    D = LA.demand("x", set(), P0)
    assert {k[0] for k in D["cells"]} == set(cu)
    assert all(cl["nets"] == {"N2"} for cl in D["cells"].values())


def test_escape_flag_from_own_pad_distance(load):
    load(board(segs=[seg("N1", (0.5, 0.5), (0.5, 4.5))], pads=[pad("N1", 0.5, 0.5)]))
    D = LA.demand("x", set(), P0)
    near = D["cells"][("F.Cu", 0, 0)]
    far = D["cells"][("F.Cu", 0, 4)]
    assert near["escape"] is True and near["pad_dist"]["N1"] < 1.0
    assert far["escape"] is False and far["pad_dist"]["N1"] > 3.0


def test_reservations_drop_crit_cells_and_pad_clear_cells(load):
    # N1 runs along row 0 from x=0.5 to 9.5; a critical pad of P sits at (9.5, 0.5); a crit copper cell at (2,0)
    B = board(segs=[seg("N1", (0.5, 0.5), (9.5, 0.5))], pads=[pad("P", 9.5, 0.5)], bbox=(0.0, 0.0, 10.0, 10.0))
    load(B)
    D = LA.demand("x", {"P"}, P0)
    D["cells"][("F.Cu", 2, 0)]["crit"].add("P")
    out = LA.reservations(D, B, {"P"}, dict(P0, pair_clear_mm=0.6, max_area_frac=1.0, min_nets=1))
    assert out
    assert all(not (r["rect"][0] <= 2.5 <= r["rect"][2]) for r in out)    # crit cell (2,0) dropped
    assert all(not (r["rect"][0] <= 9.5 <= r["rect"][2]) for r in out)    # pad-clear cell (9,0) dropped
    assert any(r["rect"][0] <= 8.5 <= r["rect"][2] for r in out)          # cell 1.0 mm from the pad kept


def test_reservations_area_cap_keeps_highest_demand(load):
    B = board(segs=[seg("A", (0.5, 0.5), (3.5, 0.5)), seg("B", (0.5, 0.5), (3.5, 0.5)), seg("C", (0.5, 0.5), (1.5, 0.5))],
              bbox=(0.0, 0.0, 10.0, 10.0))
    load(B)
    D = LA.demand("x", set(), P0)
    out = LA.reservations(D, B, set(), dict(P0, max_area_frac=0.02))   # cap 2 mm2 = 2 cells
    kept = {(r["rect"][0], r["rect"][1]) for r in out}
    assert sum(r["rect"][2] - r["rect"][0] for r in out) <= 2.0 + 1e-9
    assert (0.0, 0.0) in kept                              # highest demand (3 nets) kept first


def test_reservations_merge_rows_into_rectangles(load):
    B = board(segs=[seg("A", (0.5, 0.5), (2.5, 0.5)), seg("A", (0.5, 1.5), (2.5, 1.5))], bbox=(0.0, 0.0, 10.0, 10.0))
    load(B)
    D = LA.demand("x", set(), dict(P0, min_nets=1))
    out = LA.reservations(D, B, set(), dict(P0, min_nets=1, max_area_frac=1.0))
    assert len(out) == 1
    r = out[0]
    assert r["layer"] == "F.Cu" and r["rect"] == (0.0, 0.0, 3.0, 2.0)
    assert r["nets"] == ["A"] and r["demand"] == 6


def test_min_nets_and_escape_thresholds(load):
    B = board(segs=[seg("A", (0.5, 0.5), (0.5, 0.5))], pads=[pad("A", 1.5, 0.5)], bbox=(0.0, 0.0, 10.0, 10.0))
    load(B)
    D = LA.demand("x", set(), P0)
    # one net on the cell, own pad 1.0 mm from the cell centre: reserved with min_nets=2 through the escape rule
    assert LA.reservations(D, B, set(), dict(P0, max_area_frac=1.0))
    # with escape_mm = 0.5 the pad is out of reach and min_nets=2 is not met: nothing reserved
    assert LA.reservations(D, B, set(), dict(P0, max_area_frac=1.0), escape_mm=0.5) == []


def test_critical_nets_uses_pair_list_and_rails(monkeypatch):
    from fluxplace import netclass as NC, pairaudit as PA, power as PWR
    monkeypatch.setattr(RD, "load", lambda path, **kw: {})
    monkeypatch.setattr(NC, "classify", lambda B, cons, spec=None: {"DP": {}, "DN": {}})
    monkeypatch.setattr(PA, "pair_list", lambda cls: [("DN", "DP")])
    monkeypatch.setattr(PWR, "route_widths", lambda cons, **kw: {"VBAT": 1.0})
    assert LA.critical_nets("x", {}) == {"DP", "DN", "VBAT"}


def test_apply_code_is_valid_python_and_sets_rule_area_flags():
    rects = [{"layer": "F.Cu", "rect": (1.0, 2.0, 3.0, 4.0)}, {"layer": "B.Cu", "rect": (0.0, 0.0, 5.0, 5.0)}]
    code = LA._apply_code("/in.kicad_pcb", "/out.kicad_pcb", rects)
    compile(code, "<apply>", "exec")
    for needle in ("SetIsRuleArea(True)", "SetDoNotAllowTracks", "SetDoNotAllowVias", "'F.Cu'", "/out.kicad_pcb",
                   "LOOKAHEAD-", "(1.0, 2.0, 3.0, 4.0)"):
        assert needle in code


def test_strip_code_targets_only_lookahead_zones():
    code = LA._strip_code("/in.kicad_pcb", "/out.kicad_pcb")
    compile(code, "<strip>", "exec")
    assert "startswith('LOOKAHEAD-')" in code and "b.Remove(z)" in code


def test_apply_and_strip_run_fresh_process_and_copy_sidecar(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(LA, "_run", lambda code, timeout=900: calls.append(code) or "OK 1")
    src = tmp_path / "in.kicad_pcb"
    src.write_text("(kicad_pcb)")
    (tmp_path / "in.kicad_pro").write_text("{}")
    dst = str(tmp_path / "out.kicad_pcb")
    LA.apply(str(src), dst, [{"layer": "F.Cu", "rect": (0, 0, 1, 1)}])
    LA.strip(str(src), str(tmp_path / "s.kicad_pcb"))
    assert "SetIsRuleArea" in calls[0] and "startswith" in calls[1]
    assert (tmp_path / "out.kicad_pro").exists() and (tmp_path / "s.kicad_pro").exists()


def test_run_raises_when_pcbnew_step_does_not_print_ok(monkeypatch):
    import subprocess
    class R:
        returncode, stdout, stderr = 1, "", "boom"
    monkeypatch.setattr(_procs_mod, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="pcbnew step failed"):
        LA._run("x")


def test_ghost_command_flags_and_result(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace
    seen = {}
    src = tmp_path / "g.kicad_pcb"
    src.write_text("(kicad_pcb)")

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        rd = tmp_path / "out" / "run" / "3-route"
        rd.mkdir(parents=True)
        (rd / "board.kicad_pcb").write_text("(kicad_pcb)")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(_procs_mod, "run", fake_run)
    a = SimpleNamespace(constraints="/c.toml", spec="/s.json", router_dir="/r", router_py="/rp")
    got = LA.ghost(a, str(src), str(tmp_path / "out"), 40, lean=False)
    cmd = seen["cmd"]
    assert cmd[0] == LA.PY and cmd[1].endswith("cli.py") and "pipeline" in cmd
    for flag in ("--flow", "--from", "--to", "--router", "--router-dir", "--router-py", "--constraints", "--spec",
                 "--assume-defaults", "--allow-incomplete", "--allow-undocumented", "--no-api"):
        assert flag in cmd
    assert cmd[cmd.index("--route-budget") + 1] == "2400" and seen["kw"]["timeout"] == 2400 + 120
    assert got == str(tmp_path / "out" / "run" / "3-route" / "board.kicad_pcb")


def test_ghost_partial_board_after_hard_cap_is_kept(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace
    src = tmp_path / "g.kicad_pcb"
    src.write_text("x")
    out = tmp_path / "o"

    def cap(cmd, **kw):
        rd = out / "run" / "2-route"
        rd.mkdir(parents=True)
        (rd / "board.kicad_pcb").write_text("(kicad_pcb partial)")
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(_procs_mod, "run", cap)
    got = LA.ghost(SimpleNamespace(router_dir="/r", router_py="/rp"), str(src), str(out), 1, lean=False)
    assert got and got.endswith("board.kicad_pcb")


def test_ghost_returns_none_on_timeout(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace
    src = tmp_path / "g.kicad_pcb"
    src.write_text("x")

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(_procs_mod, "run", boom)
    assert LA.ghost(SimpleNamespace(router_dir="/r", router_py="/rp"), str(src), str(tmp_path / "o"), 1, lean=False) is None


def _fit_fixture(monkeypatch, opens_by_net, unc, pair_status, B=None):
    from fluxplace import gaps as GP, netclass as NC, pairaudit as PA
    if B is None:
        locked = dict(seg("DP", (0, 0), (5, 0)), locked=True)
        B = board(segs=[locked])
    monkeypatch.setattr(RD, "load", lambda path, **kw: B)
    monkeypatch.setattr(NC, "classify", lambda B, cons, spec=None: {"DP": {}, "DN": {}})
    monkeypatch.setattr(PA, "pair_list", lambda cls: [("DN", "DP")])
    monkeypatch.setattr(PA, "audit", lambda B, pairs, cons, opn: {("DN", "DP"): {"status": pair_status}})
    monkeypatch.setattr(GP, "measure", lambda board, **kw: {"opens": len(unc), "shorts": 0, "errors": 0,
                                                             "opens_by_net": opens_by_net, "drc": {"unconnected_items": unc}})


def test_fitness_key_and_sealed_blockers(monkeypatch):
    unc = [{"description": "Pad 1 [N9] of U1 is unconnected", "pos": {"x": 2.0, "y": 0.2}}]
    _fit_fixture(monkeypatch, {"N9": 1}, unc, "complete")
    f = LA.fitness("x", {"DP", "DN"}, {}, escape_mm=0.5)
    assert f["pairs_complete"] == 1 and f["opens"] == 1 and f["sealed"] == 1
    assert f["blockers"] == {"DN|DP": {"count": 1, "nets": ["N9"]}}
    assert f["key"] == (-1, 0, 1, 1, 0, 0.0)


def test_fitness_orders_candidates_lexicographically(monkeypatch):
    _fit_fixture(monkeypatch, {}, [], "complete")
    good = LA.fitness("a", {"DP", "DN"}, {})["key"]
    _fit_fixture(monkeypatch, {"N9": 1}, [{"description": "Pad [N9]", "pos": {"x": 90, "y": 90}}], "half")
    bad = LA.fitness("b", {"DP", "DN"}, {})["key"]
    assert good < bad
    assert sorted([bad, good])[0] == good


def test_render_writes_png_fixture(load, tmp_path):
    B = board(segs=[seg("P", (1, 1), (8, 1)), seg("N1", (1, 4), (8, 4), layer="B.Cu")],
              vias=[{"net": "P", "x": 8, "y": 1, "d": 0.6, "drill": 0.3, "layers": {"F.Cu", "B.Cu"}, "locked": True}],
              bbox=(0.0, 0.0, 10.0, 10.0))
    B["edge"] = [((0, 0), (10, 0)), ((10, 0), (10, 10)), ((10, 10), (0, 10)), ((0, 10), (0, 0))]
    load(B)
    rects = [{"layer": "F.Cu", "rect": (2.0, 5.0, 6.0, 7.0), "nets": ["N1"], "demand": 3}]
    fit = {"id": "r1-v0", "key": (-1, 0, 2, 0, 0, 0.0), "critical": ["P"],
           "unconnected": [{"net": "N2", "pos": (3.0, 8.0)}]}
    out = str(tmp_path / "c.png")
    assert LA.render("x.kicad_pcb", rects, fit, out) == out
    assert open(out, "rb").read(8) == b"\x89PNG\r\n\x1a\n"


# ---- L5 tournament: every external call monkeypatched (ghost, fitness, render, demand, reservations, workers) ----

def _tour_setup(monkeypatch, tmp_path, plan, sync=False):
    """plan(round, job) -> fit dict (key, opens, pairs_complete, pairs_total, blockers) for each candidate result."""
    from types import SimpleNamespace
    fixture = tmp_path / "ghost0.kicad_pcb"
    fixture.write_text("(kicad_pcb)")
    monkeypatch.setattr(LA, "critical_nets", lambda board, cons, spec=None: {"A", "B"})
    monkeypatch.setattr(LA, "critical_pairs", lambda board, cons, spec=None: [("A", "B")])
    monkeypatch.setattr(LA, "corridors", lambda B, pairs, P: ({}, {}))
    monkeypatch.setattr(LA, "adopt_pairs", lambda gb, pairs, cons: [("A", "B")])
    monkeypatch.setattr(LA, "ghost", lambda a, board, outdir, budget, router="krt-whole", passes=None, lean=True, threads=8: str(fixture))
    monkeypatch.setattr(LA, "global_demand", lambda board, cons, spec, P, crit: {"cells": {}, "origin": (0.0, 0.0), "cell": 1.0})
    monkeypatch.setattr(LA, "fitness", lambda board, crit, cons, reserved_area=0.0, escape_mm=3.0:
                        {"key": (0, 0, 5, 0, 0, 0.0), "opens": 5, "pairs_complete": 0, "pairs_total": 1, "blockers": {}})
    monkeypatch.setattr(LA, "render", lambda board, rects, fit, path: (open(path, "w").write("x"), path)[1])
    monkeypatch.setattr(LA, "demand", lambda board, crit, P: {"cells": {}, "origin": (0.0, 0.0), "cell": 1.0})
    monkeypatch.setattr(LA.RD, "load", lambda path, **kw: board())
    monkeypatch.setattr(LA, "reservations", lambda D, B, crit, P, min_nets=None, escape_mm=None:
                        [{"layer": "F.Cu", "rect": (0.0, 0.0, 1.0, 1.0), "nets": ["N"], "demand": 1}])
    jobs_seen = []
    rounds = {"n": 0}

    def fake_run_jobs(jobs, P, timeout):
        if all(j["kind"] == "verify" for j in jobs):
            rounds["verify"] = rounds.get("verify", 0) + 1
        else:
            rounds["n"] += 1
        res = []
        for job in jobs:
            jobs_seen.append(job)
            locked = tmp_path / f"{job['tag']}-locked.kicad_pcb"
            locked.write_text("(kicad_pcb)")
            base = dict(job, tag=job["tag"][:-2]) if job["kind"] == "verify" else job
            fit = dict(plan(rounds["n"], base), id=job["tag"])
            res.append({"tag": job["tag"], "kind": job["kind"], "variant": job["variant"], "locked": str(locked),
                        "png": str(tmp_path / f"{job['tag']}.png"), "fit": fit})
        return res
    monkeypatch.setattr(LA, "_run_jobs", fake_run_jobs)

    def sync_ghosts(a, board, d, P, routers):               # every ghost has landed before round 0 polls: no thread race
        import concurrent.futures as cf
        futs = {}
        for r in routers:
            futs[r] = cf.Future()
            futs[r].set_result(LA._one_ghost(a, board, d, P, r))
        return cf.ThreadPoolExecutor(max_workers=1), futs
    if sync:
        monkeypatch.setattr(LA, "_launch_ghosts", sync_ghosts)
    d = tmp_path / "stage"
    c = SimpleNamespace(cons={"lookahead": {"enabled": True, "max_rounds": 4, "no_gain_rounds": 2}}, spec=None,
                        a=SimpleNamespace(constraints=None, spec=None, router_dir="/r", router_py="/rp"),
                        dir=lambda n, name: str(_mkd(d / f"{n}-{name}")))
    return c, jobs_seen, rounds


def _mkd(p):
    os.makedirs(p, exist_ok=True)
    return p


def test_stage_skipped_when_disabled(tmp_path):
    from types import SimpleNamespace
    c = SimpleNamespace(cons={}, spec=None, a=None, dir=lambda n, name: str(_mkd(tmp_path / f"{n}-{name}")))
    assert LA.stage(c, 7, "in.kicad_pcb") == {"verdict": "skipped", "line": "[lookahead] enabled = false", "board": "in.kicad_pcb"}


def test_tournament_winner_stops_when_clean(monkeypatch, tmp_path):
    def plan(r, job):
        if job["tag"] == "r1-1":
            return {"key": (-1, 0, 0, 0, 0, 0.0), "opens": 0, "pairs_complete": 1, "pairs_total": 1, "blockers": {}}
        return {"key": (-1, 0, 2, 0, 0, 0.0), "opens": 2, "pairs_complete": 1, "pairs_total": 1, "blockers": {}}
    c, jobs, rounds = _tour_setup(monkeypatch, tmp_path, plan)
    out = LA.stage(c, 7, "in.kicad_pcb")
    assert out["verdict"] == "pass" and rounds["n"] == 1
    assert "winner r1-1" in out["line"] and "pairs 1/1" in out["line"] and "ghost opens 0 (round-0 ghost opens 5)" in out["line"]
    table = open(os.path.join(c.dir(7, "lookahead"), "TOURNAMENT.md")).read()
    for tag in ("ghost-krt-whole", "ghost-freerouting", "r1-0", "r1-1", "winner"):
        assert tag in table
    assert os.path.exists(out["board"]) and os.path.exists(os.path.join(c.dir(7, "lookahead"), "progress.json"))


def test_tournament_no_gain_stops_after_two_rounds(monkeypatch, tmp_path):
    def plan(r, job):
        if r == 1:
            return {"key": (-1, 0, 1, 0, 0, 0.0), "opens": 1, "pairs_complete": 1, "pairs_total": 2,
                    "blockers": {"A|B": {"count": 3, "nets": ["X"]}, "C|D": {"count": 1, "nets": ["Y"]}}}
        return {"key": (-1, 0, 4, 0, 0, 0.0), "opens": 4, "pairs_complete": 1, "pairs_total": 2,
                "blockers": {"A|B": {"count": 3, "nets": ["X"]}}}
    c, jobs, rounds = _tour_setup(monkeypatch, tmp_path, plan)
    out = LA.stage(c, 7, "in.kicad_pcb")
    assert out["verdict"] == "pass" and rounds["n"] == 3          # round 2 and 3 never beat round 1: no_gain_rounds = 2
    assert "winner r1-0" in out["line"]


def test_blockers_drive_repair_candidates_in_round_two(monkeypatch, tmp_path):
    def plan(r, job):
        if r == 1:
            return {"key": (-1, 0, 1, 0, 0, 0.0), "opens": 1, "pairs_complete": 1, "pairs_total": 2,
                    "blockers": {"A|B": {"count": 3, "nets": ["X"]}, "C|D": {"count": 1, "nets": ["Y"]},
                                 "E|F": {"count": 9, "nets": ["Z"]}}}
        return {"key": (0, 0, 9, 0, 0, 0.0), "opens": 9, "pairs_complete": 0, "pairs_total": 2, "blockers": {}}
    c, jobs, rounds = _tour_setup(monkeypatch, tmp_path, plan, sync=True)
    LA.stage(c, 7, "in.kicad_pcb")
    r2 = [j for j in jobs if j["tag"].startswith("r2-")]
    repairs = [j for j in r2 if j["kind"] == "repair"]
    assert [tuple(j["pair"]) for j in repairs] == [("E", "F"), ("A", "B")]     # top 2 blockers by count
    assert all(tuple(j["variant"]) == (1, 4.0) for j in repairs)               # min_nets 1, escape_mm + 1
    fresh = [j for j in r2 if j["kind"] == "soft"]
    assert len(fresh) == 1 and tuple(fresh[0]["variant"]) == (1, 3.0)         # round-1 winner's variant, min_nets - 1
    assert all(j["board"] == "in.kicad_pcb" or j["board"].endswith("-locked.kicad_pcb") for j in r2)


def test_lay_fresh_lays_again_only_when_incomplete(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(LA, "apply", lambda bi, bo, rects: calls.append(("apply", bo)) or bo)
    monkeypatch.setattr(LA, "strip", lambda bi, bo: calls.append(("strip", bo)) or bo)
    states = iter([("laid1", 1, 2), ("laid2", 2, 2)])
    monkeypatch.setattr(LA, "_pairs_stage", lambda sub, n, board: calls.append(("pairs", n, board)) or next(states))
    sub = types.SimpleNamespace()
    out = LA._lay_fresh(sub, str(tmp_path), "in", [])
    assert [c[0] for c in calls] == ["apply", "pairs", "strip", "pairs", "strip"]
    assert calls[3][2].endswith("stripped.kicad_pcb") and out.endswith("locked.kicad_pcb")


def test_tournament_stage_registered_before_pairs():
    from fluxplace import pipeline as PL
    i = PL.CONSTRUCTIVE.index("lookahead")
    assert PL.CONSTRUCTIVE[i + 1] == "pairs" and "lookahead" in PL.FUNCS


# ---- L7: soft guide (pairs ctx["guide"]), corridors, round-1 soft/hard mix ----

def test_guide_cost_identity_and_merge():
    from fluxplace import pairs as PR
    cost = [[1, 2], [3, 4]]
    assert PR._guide_cost({}, None, ["F.Cu"], ("A", "B"), cost) is cost        # no guide: unchanged
    g = lambda pr, grid, sig: [[10, 250], [0, 0]]
    merged = PR._guide_cost({"guide": g}, None, ["F.Cu", "B.Cu"], ("A", "B"), cost)
    assert [list(b) for b in merged] == [[10, 250], [3, 4]]
    assert [list(b) for b in PR._guide_cost({"guide": g}, None, ["F.Cu"], ("A", "B"), None)] == [[10, 250], [0, 0]]


class _Grid:
    def __init__(self, nx, ny, step=1.0):
        from types import SimpleNamespace
        self.nx, self.ny, self.step = nx, ny, step
        self.F = SimpleNamespace(nx=nx, ny=ny, step=step, x0=0.0, y0=0.0)

    def xy(self, j, i):
        return (i + 0.5) * self.step, (j + 0.5) * self.step


def test_make_guide_reserved_and_off_corridor_costs():
    G = {"origin": (0.0, 0.0), "cell": 1.0, "res": [{"layer": "F.Cu", "rect": (0.0, 0.0, 2.0, 1.0)}],
         "corr": {"A|B": [("F.Cu", 3, 0)]}, "res_cost": 200.0, "off_cost": 20.0}
    guide = LA.make_guide(G)
    out = [list(b) for b in guide(("B", "A"), _Grid(4, 1), ["F.Cu"])]
    # cells 0,1 reserved and off the corridor (200+20); cell 2 off the corridor (20); cell 3 is the corridor (0)
    assert out == [[220, 220, 20, 0]]
    # a pair with no corridor pays only the reservation
    assert [list(b) for b in guide(("X", "Y"), _Grid(4, 1), ["F.Cu"])] == [[200, 200, 0, 0]]


def test_corridors_cover_both_legs_with_halo_and_skip_open_pairs(monkeypatch):
    from fluxplace import routeconn as RC
    B = board(segs=[seg("A", (0.5, 0.5), (3.5, 0.5)), seg("B", (0.5, 2.5), (3.5, 2.5)), seg("C", (0.5, 6.5), (1.5, 6.5))],
              bbox=(0.0, 0.0, 10.0, 10.0))
    monkeypatch.setattr(RC, "text_open", lambda B, **kw: {})
    cor, why = LA.corridors(B, [("A", "B")], dict(P0, corridor_mm=0.0))
    assert ("F.Cu", 0, 0) in cor["A|B"] and ("F.Cu", 3, 0) in cor["A|B"] and ("F.Cu", 0, 2) in cor["A|B"]
    assert why == {}
    monkeypatch.setattr(RC, "text_open", lambda B, **kw: {"B": 1})
    cor2, why2 = LA.corridors(B, [("A", "B")], dict(P0, corridor_mm=0.0))
    assert cor2 == {} and "A|B" in why2


def test_round_one_is_soft_variants_plus_one_hard():
    P = LA.params({"lookahead": {"enabled": True}})
    specs = LA._specs(1, None, None, P, ["krt-whole"], [])
    assert [k for k, _v, _p, _s in specs] == ["soft", "soft", "soft", "hard"]
    assert [v for k, v, _p, _s in specs if k == "hard"] == [(2, 3.0)]
    two = LA._specs(1, None, None, P, ["krt-whole", "freerouting"], ["consensus", "union"])
    assert [s for _k, _v, _p, s in two] == ["krt-whole"] * 4 + ["freerouting"] * 3 + ["consensus", "union"]
    Ph = LA.params({"lookahead": {"enabled": True, "hard": True}})
    assert {k for k, _v, _p, _s in LA._specs(1, None, None, Ph, ["krt-whole"], [])} == {"hard"}


# ---- L7b: several ghosts (KRT and Freerouting as ideas), consensus and union demand ----

def test_merge_demand_consensus_needs_same_net_and_union_keeps_either():
    P = dict(P0, escape_mm=3.0)
    def D(cells):
        return {"cells": cells, "origin": (0.0, 0.0), "cell": 1.0}
    cl = lambda nets, pd=None: {"nets": set(nets), "crit": set(), "pad_dist": pd or {}, "escape": False}
    A = D({("F.Cu", 0, 0): cl({"N1"}), ("F.Cu", 1, 0): cl({"N1"}), ("F.Cu", 2, 0): cl({"N2"})})
    Bd = D({("F.Cu", 0, 0): cl({"N1", "N9"}, {"N1": 0.5}), ("F.Cu", 1, 0): cl({"N7"}), ("F.Cu", 5, 0): cl({"N3"})})
    cons = LA.merge_demand([A, Bd], "consensus", P)
    assert set(cons["cells"]) == {("F.Cu", 0, 0)} and cons["cells"][("F.Cu", 0, 0)]["nets"] == {"N1"}
    assert cons["cells"][("F.Cu", 0, 0)]["pad_dist"] == {"N1": 0.5}
    uni = LA.merge_demand([A, Bd], "union", P)
    assert set(uni["cells"]) == {("F.Cu", 0, 0), ("F.Cu", 1, 0), ("F.Cu", 2, 0), ("F.Cu", 5, 0)}
    assert uni["cells"][("F.Cu", 0, 0)]["nets"] == {"N1", "N9"}


def test_ghost_router_flags(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace
    seen = {}
    src = tmp_path / "g.kicad_pcb"
    src.write_text("x")
    monkeypatch.setattr(_procs_mod, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd) and subprocess.CompletedProcess(cmd, 0, "", ""))
    a = SimpleNamespace(router_dir="/r", router_py="/rp", freerouting_jar="/fr.jar", java="/jdk/bin/java")
    LA.ghost(a, str(src), str(tmp_path / "o1"), 4, router="freerouting", lean=False)
    cmd = seen["cmd"]
    assert cmd[cmd.index("--router") + 1] == "freerouting" and cmd[cmd.index("--freerouting-jar") + 1] == "/fr.jar"
    assert "--router-dir" not in cmd and "--router-py" not in cmd
    seen.clear()
    LA.ghost(a, str(src), str(tmp_path / "o2"), 4, lean=False)
    assert seen["cmd"][seen["cmd"].index("--router") + 1] == "krt-whole" and "--router-dir" in seen["cmd"]


def test_stage_runs_each_ghost_and_drops_a_failed_one(monkeypatch, tmp_path):
    def plan(r, job):
        return {"key": (-1, 0, 0, 0, 0, 0.0), "opens": 0, "pairs_complete": 1, "pairs_total": 1, "blockers": {}}
    c, jobs, rounds = _tour_setup(monkeypatch, tmp_path, plan)
    real = LA.ghost
    monkeypatch.setattr(LA, "ghost", lambda a, board, outdir, budget, router="krt-whole", passes=None, lean=True, threads=8:
                        None if router == "freerouting" else real(a, board, outdir, budget, router))
    out = LA.stage(c, 7, "in.kicad_pcb")
    assert out["verdict"] == "pass"
    table = open(os.path.join(c.dir(7, "lookahead"), "TOURNAMENT.md")).read()
    assert "fail: no routed board" in table and "ghost-krt-whole" in table
    assert not any(j["src"] == "freerouting" for j in jobs)


# ---- L8: tiers (tier-1 text key, KRT verify of the top 2 with opens as tie-break), fast ghosts, global ghost ----

def test_tier_two_opens_break_ties_between_equal_tier_one_keys(monkeypatch, tmp_path):
    def plan(r, job):
        opens = 0 if job["tag"] == "r1-1" else 3
        return {"key": (-1, 0, 0, 0, 0, 0.0), "opens": opens, "pairs_complete": 1, "pairs_total": 1, "blockers": {}}
    c, jobs, rounds = _tour_setup(monkeypatch, tmp_path, plan)
    out = LA.stage(c, 7, "in.kicad_pcb")
    assert "winner r1-1" in out["line"] and "ghost opens 0" in out["line"]


def test_only_the_top_two_candidates_are_verified(monkeypatch, tmp_path):
    def plan(r, job):
        return {"key": (-1, 0, 1, 0, 0, 0.0), "opens": 1, "pairs_complete": 1, "pairs_total": 1, "blockers": {}}
    c, jobs, rounds = _tour_setup(monkeypatch, tmp_path, plan)
    LA.stage(c, 7, "in.kicad_pcb")
    verify_jobs = [j for j in jobs if j["kind"] == "verify"]
    assert rounds["verify"] == rounds["n"] and len(verify_jobs) <= 2 * rounds["verify"]   # at most top 2 per round
    table = open(os.path.join(c.dir(7, "lookahead"), "TOURNAMENT.md")).read()
    assert "tier1 only" in table and "(opens 1)" in table


def test_freerouting_ghost_gets_passes_override_when_constraints_lack_the_table(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace
    cons = tmp_path / "c.toml"
    cons.write_text("[power]\n")
    src = tmp_path / "g.kicad_pcb"
    src.write_text("x")
    seen = {}
    monkeypatch.setattr(_procs_mod, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd) and subprocess.CompletedProcess(cmd, 0, "", ""))
    a = SimpleNamespace(router_dir="/r", router_py="/rp", constraints=str(cons), spec=None, freerouting_jar="/fr.jar", java=None)
    LA.ghost(a, str(src), str(tmp_path / "o"), 8, router="freerouting", passes=4, lean=False)
    cc = seen["cmd"][seen["cmd"].index("--constraints") + 1]
    assert cc.endswith("constraints.ghost.toml") and "[route.freerouting]\npasses = 4" in open(cc).read()


def test_global_demand_maps_route_nodes_to_cells(monkeypatch):
    from fluxplace import planroute as PRR
    class GR:
        N, layers = 4, ["F.Cu", "B.Cu"]
        routes = {"N1": {"conns": [{"path": [0, 1, 4 + 2]}]}, "P": {"conns": [{"path": [0]}]}}
        def xy_of(self, c):
            return (0.5 + c, 0.5)
    monkeypatch.setattr(PRR, "run", lambda B, cons, spec, ignore_routing=False: {"gr": GR()})
    monkeypatch.setattr(LA.RD, "load", lambda path, **kw: board(bbox=(0.0, 0.0, 4.0, 4.0)))
    D = LA.global_demand("x", {}, None, dict(P0), {"P"})
    assert D["cells"][("F.Cu", 0, 0)]["nets"] == {"N1"} and D["cells"][("F.Cu", 1, 0)]["nets"] == {"N1"}
    assert D["cells"][("B.Cu", 2, 0)]["nets"] == {"N1"}
    assert all("P" not in cl["nets"] for cl in D["cells"].values())


def test_pcbnew_scripts_flush_their_ok_line():
    for code in (LA._apply_code("/i", "/o", [{"layer": "F.Cu", "rect": (0, 0, 1, 1)}]), LA._strip_code("/i", "/o")):
        assert "print('OK', " in code and "flush=True" in code


def test_restrict_demand_keeps_only_the_sealed_nets():
    cl = lambda nets: {"nets": set(nets), "crit": set(), "escape": False, "pad_dist": {n: 1.0 for n in nets}}
    D = {"cells": {("F.Cu", 0, 0): cl({"S1", "X"}), ("F.Cu", 1, 0): cl({"X"})}, "origin": (0.0, 0.0), "cell": 1.0}
    R = LA.restrict_demand(D, {"S1"})
    assert list(R["cells"]) == [("F.Cu", 0, 0)] and R["cells"][("F.Cu", 0, 0)]["nets"] == {"S1"}


def test_repair_rects_come_only_from_sealed_net_cells(monkeypatch, tmp_path):
    real_reservations = LA.reservations

    def plan(r, job):
        if r == 1:
            return {"key": (-1, 0, 1, 0, 0, 0.0), "opens": 1, "pairs_complete": 1, "pairs_total": 2,
                    "blockers": {"A|B": {"count": 3, "nets": ["S1"]}}}
        return {"key": (0, 0, 9, 0, 0, 0.0), "opens": 9, "pairs_complete": 0, "pairs_total": 2, "blockers": {}}
    c, jobs, rounds = _tour_setup(monkeypatch, tmp_path, plan)
    monkeypatch.setattr(LA, "reservations", real_reservations)
    cl = lambda nets: {"nets": set(nets), "crit": set(), "escape": False, "pad_dist": {}}
    monkeypatch.setattr(LA, "demand", lambda board, crit, P: {"cells": {("F.Cu", 0, 0): cl({"S1", "X"}),
                                                                       ("F.Cu", 4, 4): cl({"X"})},
                                                                "origin": (0.0, 0.0), "cell": 1.0})
    LA.stage(c, 7, "in.kicad_pcb")
    repairs = [j for j in jobs if j["kind"] == "repair"]
    assert repairs and all(set(n for r in j["rects"] for n in r["nets"]) <= {"S1"} for j in repairs)


def test_no_winner_is_logged_as_the_stop_reason(monkeypatch, tmp_path):
    def plan(r, job):
        return {"key": (-1, 0, 0, 0, 0, 0.0), "opens": 0, "pairs_complete": 1, "pairs_total": 1, "blockers": {}}
    c, jobs, rounds = _tour_setup(monkeypatch, tmp_path, plan)
    monkeypatch.setattr(LA, "_run_jobs", lambda jobs, P, timeout: [{"tag": j["tag"], "error": "boom"} for j in jobs])
    out = LA.stage(c, 7, "in.kicad_pcb")
    assert out["verdict"] == "fail"
    prog = json.load(open(os.path.join(c.dir(7, "lookahead"), "progress.json")))
    assert "nothing to keep" in prog["reason"]


def test_lay_repair_raises_when_pairs_wrote_no_board(monkeypatch, tmp_path):
    from fluxplace import pairs as PR
    board_in = tmp_path / "in.kicad_pcb"
    board_in.write_text("(kicad_pcb)")
    monkeypatch.setattr(LA, "apply", lambda bi, bo, rects: bi)
    monkeypatch.setattr(PR, "run", lambda *a, **k: {})
    with pytest.raises(RuntimeError, match="wrote no board"):
        LA._lay_repair(types.SimpleNamespace(), str(tmp_path), str(board_in), [], ("A", "B"), {}, None)


# ---- L7 adopt-first: a complete ghost pair is copied onto the candidate as locked copper ----

def test_adopt_pairs_keeps_only_complete_pairs(monkeypatch):
    from fluxplace import pairaudit as PA, routeconn as RC, routediag as RD
    monkeypatch.setattr(RD, "load", lambda path, **kw: board())
    monkeypatch.setattr(RC, "text_open", lambda B, **kw: {})
    monkeypatch.setattr(PA, "audit", lambda B, pairs, cons, opn: {("A", "B"): {"status": "complete"},
                                                                  ("C", "D"): {"status": "mismatch"}})
    assert LA.adopt_pairs("g.kicad_pcb", [("A", "B"), ("C", "D")], {}) == [("A", "B")]


def test_adopt_script_copies_locked_items_by_net_name():
    code = LA._adopt_code("/c.kicad_pcb", "/g.kicad_pcb", "/o.kicad_pcb", ["DP", "DN"])
    compile(code, "<adopt>", "exec")
    for needle in ("SetLocked(True)", "FindNet(nm)", "PCB_VIA", "PCB_ARC", "['DN', 'DP']", "flush=True", "/o.kicad_pcb"):
        assert needle in code


def test_only_soft_candidates_carry_the_adopt_list(monkeypatch, tmp_path):
    def plan(r, job):
        return {"key": (-1, 0, 0, 0, 0, 0.0), "opens": 0, "pairs_complete": 1, "pairs_total": 1, "blockers": {}}
    c, jobs, rounds = _tour_setup(monkeypatch, tmp_path, plan)
    LA.stage(c, 7, "in.kicad_pcb")
    soft = [j for j in jobs if j["kind"] == "soft" and j["tag"].startswith("r1-")]
    hard = [j for j in jobs if j["kind"] == "hard"]
    assert soft and all(j["adopt"] and j["adopt"][0]["pairs"] == [["A", "B"]] for j in soft)
    assert hard and all(j["adopt"] is None for j in hard)


# ---- L9 lean ghosts: one KRT route.py call on the signal nets; Freerouting DSN -> java -> SES ----

def _lean_fixture(monkeypatch, tmp_path, pads):
    from types import SimpleNamespace
    from fluxplace import constraints as C, krtchain as KC, routediag as RD, routerules as RR, routeconn as RC
    src = tmp_path / "in.kicad_pcb"
    src.write_text("(kicad_pcb)")
    monkeypatch.setattr(RD, "load", lambda path, **kw: board() | {"pads": pads})
    monkeypatch.setattr(RC, "text_open", lambda B, **kw: {})
    monkeypatch.setattr(RR, "rules_for", lambda cons, path, a, **kw: {"r": 1})
    monkeypatch.setattr(KC, "base_flags", lambda R, d: ["--base"])
    monkeypatch.setattr(KC, "plane_plan", lambda path: (["F.Cu", "B.Cu"], [("GND", "In1.Cu")]))
    a = SimpleNamespace(router_dir="/krt", router_py="/py", constraints=None, spec=None)
    return a, str(src)


def _pad(net, i):
    return {"net": net, "x": float(i), "y": 0.0, "layers": {"F.Cu"}, "ref": "U1", "num": str(i)}


def test_lean_krt_is_one_route_call_on_non_plane_nets(monkeypatch, tmp_path):
    from fluxplace import routestatus as RS
    pads = [_pad("A", 1), _pad("A", 2), _pad("B", 3), _pad("GND", 4), _pad("GND", 5)]
    a, src = _lean_fixture(monkeypatch, tmp_path, pads)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        open(kw["out"], "w").write("(kicad_pcb routed)")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(RS, "run_logged", fake_run)
    out = LA.ghost(a, src, str(tmp_path / "g"), 3, lean=True)
    cmd = seen["cmd"]
    assert cmd[1].endswith("py_router/route.py") or "route.py" in cmd[1]
    assert cmd[cmd.index("--nets") + 1:cmd.index("--layers")] == ["A"]          # plane net GND and the 1-pad net B left out
    assert cmd[cmd.index("--layers") + 1: cmd.index("--layers") + 3] == ["F.Cu", "B.Cu"]
    assert "--power-nets" not in cmd and "--keep-input-copper" in cmd and "--base" in cmd
    assert cmd[cmd.index("--quick") + 1] == "2"                       # ghost mode: judge, not ship
    assert out.endswith("krt-lean.kicad_pcb") and os.path.exists(os.path.join(str(tmp_path / "g"), "ghost.stats.json"))


def test_lean_krt_falls_back_to_the_named_checkpoint_at_the_cap(monkeypatch, tmp_path):
    from fluxplace import routestatus as RS
    pads = [_pad("A", 1), _pad("A", 2)]
    a, src = _lean_fixture(monkeypatch, tmp_path, pads)
    outdir = tmp_path / "g"
    ck = outdir / "checkpoint-3.kicad_pcb"
    outdir.mkdir()
    ck.write_text("(kicad_pcb ckpt)")

    def capped(cmd, **kw):
        (outdir / "krt.status.json").write_text(json.dumps({"checkpoint": str(ck)}))
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(RS, "run_logged", capped)
    assert LA.ghost(a, src, str(outdir), 3, lean=True) == str(ck)
    # no checkpoint named: None
    (outdir / "krt.status.json").write_text(json.dumps({"checkpoint": None}))
    monkeypatch.setattr(RS, "run_logged", lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 1)))
    assert LA.ghost(a, src, str(tmp_path / "g2"), 3, lean=True) is None


def test_lean_freerouting_export_declares_planes_and_runs_passes(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from fluxplace import routefr as RF
    pads = [_pad("A", 1), _pad("A", 2)]
    a, src = _lean_fixture(monkeypatch, tmp_path, pads)
    a.freerouting_jar, a.java = "/fr.jar", "java"
    seen = {}

    def helper(cmd, args, workdir, timeout=1800):
        if cmd == "export":
            open(args["dsn"], "w").write("(pcb)")
            return {"ok": True}
        seen["import"] = args
        open(args["out"], "w").write("(kicad_pcb fr)")
        return {}
    monkeypatch.setattr(RF, "_helper", helper)
    monkeypatch.setattr(RF, "locate", lambda jar, java, env=None: {"jar": jar, "java": java})
    monkeypatch.setattr(RF, "jar_version", lambda jar: None)
    def fake_fix(text, planes=(), **kw):
        seen["planes"] = list(planes)
        return text, {}

    def fake_jvm(cmd, log, t, g, ses, **kw):
        seen["cmd"] = cmd
        return {"rc": 0, "killed": False}
    monkeypatch.setattr(RF, "fix_dsn", fake_fix)
    monkeypatch.setattr(RF, "ses_ok", lambda path: True)
    monkeypatch.setattr(RF, "run_jvm", fake_jvm)
    out = LA.ghost(a, src, str(tmp_path / "f"), 4, router="freerouting", passes=4, lean=True, threads=2)
    cmd = seen["cmd"]
    assert "--router.autorouter.max_passes=4" in cmd and "-de" in cmd and "-do" in cmd
    assert "--router.max_threads=2" in cmd
    assert seen["planes"] == ["GND"]                              # plane nets declared power: not routed
    assert out and out.endswith("fr-lean.kicad_pcb") and seen["import"]["skip_nets"] == []


# ---- L10 streaming ghosts / L11 live snapshots: the router ghosts land at different times ----

def _stream(monkeypatch, tmp_path, keys, plan, gate_fr=True, release=True):
    """_tour_setup plus: KRT ghost lands at once; Freerouting blocks on `gate` until round 1 has run; `keys` = fitness
    key per router. -> (c, jobs, rounds, gate, slow_done, log)"""
    import threading
    import time
    c, jobs, rounds = _tour_setup(monkeypatch, tmp_path, plan)
    gate, slow_done = threading.Event(), threading.Event()
    log = {"pre_release_landed": None}
    fixtures = {r: tmp_path / f"fix-{r}.kicad_pcb" for r in ("krt-whole", "freerouting")}
    for f in fixtures.values():
        f.write_text("(kicad_pcb)")

    def gated_ghost(a, board, outdir, budget, router="krt-whole", passes=None, lean=True, threads=8):
        if router == "freerouting" and gate_fr:
            gate.wait()
            slow_done.set()
        return str(fixtures[router])
    monkeypatch.setattr(LA, "ghost", gated_ghost)
    monkeypatch.setattr(LA, "fitness", lambda board, crit, cons, reserved_area=0.0, escape_mm=3.0: {
        "key": keys["freerouting" if "freerouting" in board else "krt-whole"], "opens": keys["opens"][
            "freerouting" if "freerouting" in board else "krt-whole"], "pairs_complete": 1, "pairs_total": 1,
        "blockers": {}})
    orig = LA._run_jobs

    def run_and_release(jobs_, P, timeout):
        first_round = not any(j["kind"] == "verify" for j in jobs_)
        if first_round and log["pre_release_landed"] is None:
            log["pre_release_landed"] = slow_done.is_set()          # round 1 must not wait for the slow ghost
            if not release:
                return orig(jobs_, P, timeout)
            gate.set()
            slow_done.wait(10)
            time.sleep(0.2)                                         # let the stage's future mark itself done
        return orig(jobs_, P, timeout)
    monkeypatch.setattr(LA, "_run_jobs", run_and_release)
    return c, jobs, rounds, gate, log


def test_late_better_ghost_queues_its_candidates_for_the_next_round(monkeypatch, tmp_path):
    keys = {"krt-whole": (-1, 0, 3, 0, 0, 0.0), "freerouting": (-1, 0, 0, 0, 0, 0.0),
            "opens": {"krt-whole": 3, "freerouting": 0}}
    plan = lambda r, job: {"key": (-1, 0, 1, 0, 0, 0.0), "opens": 1, "pairs_complete": 1, "pairs_total": 1, "blockers": {}}
    c, jobs, rounds, gate, log = _stream(monkeypatch, tmp_path, keys, plan)
    try:
        LA.stage(c, 7, "in.kicad_pcb")
    finally:
        gate.set()
    assert log["pre_release_landed"] is False                     # round 1 started before the slow ghost landed
    assert not any(j["src"] == "freerouting" for j in jobs if j["tag"].startswith("r1-"))
    fr_later = [j for j in jobs if j["src"] == "freerouting" and j["kind"] != "verify" and not j["tag"].startswith("r1-")]
    assert fr_later and all(j["kind"] == "soft" for j in fr_later)
    assert any(j["src"] == "consensus+freerouting" for j in jobs) and any(j["src"] == "union+freerouting" for j in jobs)
    table = open(os.path.join(c.dir(7, "lookahead"), "TOURNAMENT.md")).read()
    assert "late ghost freerouting: pairs 1 vs 1, opens 0 vs 3" in table and "-> better" in table
    prog = json.load(open(os.path.join(c.dir(7, "lookahead"), "progress.json")))
    assert prog["timing"]["ghosts"]["freerouting"]["used"] == "late-better"
    assert prog["timing"]["ghosts"]["krt-whole"]["used"] == "first"


def test_late_worse_ghost_adds_only_the_consensus_candidate(monkeypatch, tmp_path):
    keys = {"krt-whole": (-1, 0, 0, 0, 0, 0.0), "freerouting": (-1, 0, 4, 0, 0, 0.0),
            "opens": {"krt-whole": 0, "freerouting": 4}}
    plan = lambda r, job: {"key": (-1, 0, 1, 0, 0, 0.0), "opens": 1, "pairs_complete": 1, "pairs_total": 1, "blockers": {}}
    c, jobs, rounds, gate, log = _stream(monkeypatch, tmp_path, keys, plan)
    try:
        LA.stage(c, 7, "in.kicad_pcb")
    finally:
        gate.set()
    later = [j for j in jobs if j["kind"] != "verify" and not j["tag"].startswith("r1-")
             and j["src"] in ("freerouting", "consensus+freerouting", "union+freerouting")]
    assert [j["src"] for j in later] == ["consensus+freerouting"]           # one consensus candidate, nothing else
    table = open(os.path.join(c.dir(7, "lookahead"), "TOURNAMENT.md")).read()
    assert "-> worse" in table
    prog = json.load(open(os.path.join(c.dir(7, "lookahead"), "progress.json")))
    assert prog["timing"]["ghosts"]["freerouting"]["used"] == "late-ignored"


def test_a_ghost_still_running_at_stage_end_is_killed_and_logged(monkeypatch, tmp_path):
    keys = {"krt-whole": (-1, 0, 0, 0, 0, 0.0), "freerouting": (-1, 0, 0, 0, 0, 0.0),
            "opens": {"krt-whole": 0, "freerouting": 0}}
    plan = lambda r, job: {"key": (-1, 0, 0, 0, 0, 0.0), "opens": 0, "pairs_complete": 1, "pairs_total": 1, "blockers": {}}
    c, jobs, rounds, gate, log = _stream(monkeypatch, tmp_path, keys, plan, release=False)
    out = LA.stage(c, 7, "in.kicad_pcb")
    assert out["verdict"] == "pass" and rounds["n"] == 1        # clean winner after round 1: the slow ghost never landed
    prog = json.load(open(os.path.join(c.dir(7, "lookahead"), "progress.json")))
    assert prog["timing"]["ghosts"]["freerouting"]["used"] == "timeout"
    table = open(os.path.join(c.dir(7, "lookahead"), "TOURNAMENT.md")).read()
    assert "timeout (stage done)" in table
    gate.set()


def test_live_snapshots_per_ghost_landing_and_candidate_result(monkeypatch, tmp_path):
    from fluxplace import snapshot as SN
    reasons = []
    monkeypatch.setattr(SN, "active_take", lambda board, reason, force=False, opens=None: reasons.append(reason))
    keys = {"krt-whole": (-1, 0, 0, 0, 0, 0.0), "freerouting": (-1, 0, 0, 0, 0, 0.0),
            "opens": {"krt-whole": 0, "freerouting": 0}}
    plan = lambda r, job: {"key": (-1, 0, 0, 0, 0, 0.0), "opens": 0, "pairs_complete": 1, "pairs_total": 1, "blockers": {}}
    c, jobs, rounds, gate, log = _stream(monkeypatch, tmp_path, keys, plan, gate_fr=False)
    LA.stage(c, 7, "in.kicad_pcb")
    assert "ghost krt-whole" in reasons and "ghost freerouting" in reasons
    assert "cand r1-0 locked" in reasons


def test_canonical_copy_sorts_footprints(tmp_path):
    def fp(r):
        return f'\t(footprint "X:{r}"\n\t\t(property "Reference" "{r}"\n\t\t\t(at 0 0)\n\t\t)\n\t)\n'
    head, mid, tail = '(kicad_pcb\n\t(version 1)\n', '\t(gr_line (start 0 0))\n', '\t(segment (start 0 0))\n)\n'
    src = tmp_path / "a.kicad_pcb"
    src.write_text(head + fp("R10") + fp("C2") + mid + fp("R2") + fp("C10") + tail)
    for rev, want in ((False, ["C2", "C10", "R2", "R10"]), (True, ["R10", "R2", "C10", "C2"])):
        dst = tmp_path / f"b{rev}.kicad_pcb"
        LA._canonical_copy(str(src), str(dst), reverse=rev)
        t = dst.read_text()
        assert [l.split('"')[3] for l in t.splitlines() if "Reference" in l] == want
        assert t.count("\t(footprint ") == 4
        assert t.startswith(head + "".join(fp(r) for r in want)) and t.endswith(mid + tail)


def test_pick_first():
    assert LA._pick_first({"fwd": {"a"}, "rev": {"a", "b"}}) == ("fwd", ["b"])
    assert LA._pick_first({"fwd": {"a", "b"}, "rev": {"a"}}) == ("rev", ["b"])
    assert LA._pick_first({"fwd": {"a"}, "rev": {"b"}}) == ("fwd", ["a", "b"])
    assert LA._pick_first({"fwd": None, "rev": {"a"}}) == ("rev", [])
    assert LA._pick_first({"fwd": {"a"}, "rev": None}) == ("fwd", [])
    assert LA._pick_first({"fwd": None, "rev": None}) == (None, [])
