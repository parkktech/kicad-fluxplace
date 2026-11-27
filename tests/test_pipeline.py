"""Staged pipeline: state.json handoff, stage ranges, na stages, route stage with a fake router."""
import argparse, json, os, shutil, sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
from fluxplace import pipeline as PL
import cli

FAKE = '''
import sys, shutil
a = sys.argv
src, out = a[1], a[a.index("--output") + 1]
nets = a[a.index("--nets") + 1:]
nets = [n for n in nets if not n.startswith("--")]
txt = open(src).read().rstrip()
assert txt.endswith(")")
add = ""
for i, n in enumerate(nets):
    print("[%d/%d] Routing %s (id=%d)" % (i + 1, len(nets), n, i), flush=True)
    if n == "STUCK":
        print("  FAILED: Could not find route (1.0s)", flush=True)
        continue
    add += '(segment (start 1 %d) (end 5 %d) (width 0.15) (layer "F.Cu") (net "%s"))\\n' % (i, i, n)
    print("  SUCCESS: 1 segments", flush=True)
open(out, "w").write(txt[:-1] + add + ")\\n")
'''


def _board(tmp_path):
    body = (G.ic("U1", 20, 20, ["A", "B", "C", "D"], pitch=1.27) +
            G.ic("U2", 60, 40, ["A", "B", "C", "STUCK"], pitch=1.27) +
            G.two("R1", 40, 30, "D", "STUCK"))
    return G.board(tmp_path, body)


def _args(tmp_path, board, **kw):
    router = tmp_path / "krt"
    (router / "py_router").mkdir(parents=True, exist_ok=True)
    (router / "py_router" / "route.py").write_text(FAKE)
    d = dict(board=board, workdir=str(tmp_path / "W"), constraints=None, spec=None, from_stage=None,
             to_stage=None, status=False, waive=[], force=True, api=False, route_budget=60,
             route_timeout=30, stall_rounds=1, replace_on_stall=False,
             layers=["F.Cu", "B.Cu"], track=0.2, clearance=0.2, router_py=sys.executable,
             router_dir=str(router))
    d.update(kw)
    return argparse.Namespace(**d)


def test_state_stays_small_and_status_is_short(tmp_path):
    st = {"stages": {n: {"verdict": "pass", "line": "x" * 400, "dir": "d", "board": "/p" * 80} for n in PL.STAGES},
          "next": ["y" * 300] * 9, "board": "/b", "stage": "review", "verdict": "pass"}
    txt = PL.save_state(str(tmp_path), st)
    assert len(txt) <= PL.STATE_MAX
    assert len(PL.format_status(json.loads(txt)).splitlines()) <= 15


def test_range_and_handoff_through_state_json(tmp_path):
    b = _board(tmp_path)
    a = _args(tmp_path, b, to_stage="place")
    assert PL.run(a) == 0
    st = PL.load_state(a.workdir)
    assert st["stages"]["floorplan-check"]["verdict"] == "pass"
    assert st["stages"]["anchor-check"]["verdict"] == "pass" and st["stage"] == "place"
    assert os.path.exists(st["board"]) and "4-place" in st["board"]
    # a later stage starts from state.json alone (no BOARD argument)
    a2 = _args(tmp_path, None, from_stage="route", to_stage="route")
    assert PL.run(a2) == 3                       # a board with no records: the floors are a question, nothing routed
    assert PL.load_state(a.workdir)["stages"]["route"]["verdict"] == "questions"
    a2 = _args(tmp_path, None, from_stage="route", to_stage="route", answer=["rule-floors=standard"])
    assert PL.run(a2) == 1                       # STUCK stays unrouted -> fail, with numbers
    st = PL.load_state(a.workdir)
    r = st["stages"]["route"]
    assert r["verdict"] == "fail" and "STUCK" in " ".join(st["next"])   # unrouted net reported with numbers
    assert os.path.exists(os.path.join(a.workdir, "7-route", "board.kicad_pcb"))
    assert os.path.exists(os.path.join(a.workdir, "7-route", "routed.kicad_pcb.status.json"))
    assert len(open(PL.state_path(a.workdir)).read()) <= PL.STATE_MAX


def test_route_without_router_fails_loudly(tmp_path):
    b = _board(tmp_path)
    a = _args(tmp_path, b, to_stage="anchor-check")
    PL.run(a)
    a2 = _args(tmp_path, None, from_stage="route", to_stage="route", router_dir=str(tmp_path / "nope"))
    with pytest.raises(SystemExit) as e:
        PL.run(a2)
    assert str(tmp_path / "nope") in str(e.value)


def test_router_resolution_order(tmp_path, monkeypatch):
    from fluxplace import routerloc as RL
    def mk(n):
        d = tmp_path / n
        (d / "py_router").mkdir(parents=True)
        (d / "py_router" / "route.py").write_text("")
        (d / "VERSION").write_text("1.2.3\n")
        return str(d)
    flag, envd = mk("flag"), mk("env")
    r = RL.resolve(flag, sys.executable, env={RL.ENV_DIR: envd})
    assert r["dir"] == flag and r["source"] == "flag" and r["version"] == "1.2.3"
    assert RL.resolve(None, sys.executable, env={RL.ENV_DIR: envd})["dir"] == envd
    with pytest.raises(RuntimeError) as e:                 # an explicit bad choice does not fall through
        RL.resolve(str(tmp_path / "bad"), sys.executable, env={RL.ENV_DIR: envd})
    assert str(tmp_path / "bad") in str(e.value)
    home = tmp_path / "home"
    (home / "tools").mkdir(parents=True)
    for n in ("KiCadRoutingTools", "KiCadRoutingTools-latest"):
        (home / "tools" / n / "py_router").mkdir(parents=True)
        (home / "tools" / n / "py_router" / "route.py").write_text("")
    monkeypatch.setenv("HOME", str(home))
    assert RL.resolve(None, sys.executable, env={})["dir"].endswith("KiCadRoutingTools-latest")
    monkeypatch.setenv("HOME", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError) as e:
        RL.resolve(None, sys.executable, env={})
    assert "KiCadRoutingTools-latest" in str(e.value)


def test_route_rerun_on_existing_workdir_and_router_record(tmp_path):
    b = _board(tmp_path)
    a = _args(tmp_path, b, to_stage="anchor-check")
    PL.run(a)
    a2 = _args(tmp_path, None, from_stage="route", to_stage="route", answer=["rule-floors=standard"])
    PL.run(a2)
    rec = PL.load_state(a.workdir)["stages"]["route"]
    assert rec["router"]["dir"] == str(tmp_path / "krt") and rec["router"]["source"] == "flag"
    a3 = _args(tmp_path, None, from_stage="route", to_stage="route", answer=["rule-floors=standard"])
    PL.run(a3)                                              # board == the stage's own output: no SameFileError
    assert os.path.exists(os.path.join(a.workdir, "7-route", "board.kicad_pcb"))


def test_cli_status_and_missing_state(tmp_path, capsys):
    cli.main(["pipeline", "--workdir", str(tmp_path / "none"), "--status"])
    assert "no state.json" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["pipeline", "--workdir", str(tmp_path / "none"), "--from", "tune"])


def test_route_ready_gate_blocks_unless_forced(tmp_path):
    b = _board(tmp_path)
    cons = tmp_path / "c.toml"
    cons.write_text('[nets.A]\nclass = "signal"\nmax_len_mm = 1\n')
    a = _args(tmp_path, b, force=False, constraints=str(cons), to_stage="route-ready")
    rc = PL.run(a)
    st = PL.load_state(a.workdir)
    # either the synthetic board passes the gate or it is blocked; a block must stop the run
    if st["stages"]["route-ready"]["verdict"] == "blocked":
        assert rc == 1 and st["next"]


def test_constraints_intent_pairlay_and_wl_tables_load_together(tmp_path):
    from fluxplace import constraints as C, intent as IT, pairs as PR, wlrefine as WR
    p = tmp_path / "c.toml"
    p.write_text('[intent]\n[[intent.region]]\nname = "r"\nrect = [0, 0, 5, 5]\nkeep = "out"\nrefs = ["C1"]\n'
                 '[pairlay]\nreserve_exits = true\n[placement.wl]\nenabled = true\n')
    cons = C.load(str(p))
    assert IT.section(cons)["region"][0]["name"] == "r" and PR.params(cons)["reserve_exits"] is True and WR.enabled(cons)


def test_fanout_and_plan_route_stages_do_not_say_pass_on_their_own_fail(tmp_path, monkeypatch):
    b = _board(tmp_path)
    a = _args(tmp_path, b)
    c = PL.Ctx(a, a.workdir, b, {})
    outs = {"fanout": (0, "fanout: FANOUT_FAIL - 1/2 required pins escaped\nFANOUT_PIN_BLOCKED U1.1 N x\n"),
            "plan-route": (1, "PLAN_SCORE 9 FAIL\n")}
    monkeypatch.setattr(PL, "_run_cli", lambda argv, log: outs[argv[0]])
    r = PL.st_fanout(c, 4, b)
    assert r["verdict"] == "fail" and "FANOUT_PIN_BLOCKED" in " ".join(r["next"])
    r = PL.st_plan_route(c, 5, b)
    assert r["verdict"] == "warn" and r["line"].startswith("WARN")


def test_silk_fail_findings_do_not_stop_the_flow_before_review(tmp_path, monkeypatch):
    b = _board(tmp_path)
    a = _args(tmp_path, b)
    c = PL.Ctx(a, a.workdir, b, {})
    out = os.path.join(c.dir(9, "silk"), "board.kicad_pcb")

    def fake(argv, log):
        shutil.copy(b, out)
        return 1, "silk: 8 FAIL, 7 WARN, 23 INFO (after fix)\n"
    monkeypatch.setattr(PL, "_run_cli", fake)
    r = PL.st_silk(c, 9, b)
    assert r["verdict"] == "warn" and r["board"] == out and "review" in r["next"][0]


def test_silk_stage_with_clean_check_is_a_warn_while_drc_silk_items_remain(tmp_path, monkeypatch):
    b = _board(tmp_path)
    a = _args(tmp_path, b)
    c = PL.Ctx(a, a.workdir, b, {})
    out = os.path.join(c.dir(9, "silk"), "board.kicad_pcb")

    def fake(argv, log):
        shutil.copy(b, out)
        return 0, "silk: ok (exit 0) - silk: 0 FAIL, 0 WARN, 0 INFO (after fix)\n"
    monkeypatch.setattr(PL, "_run_cli", fake)
    monkeypatch.setattr(PL, "_silk_drc_left", lambda board: {"silk_over_copper": 3})
    r = PL.st_silk(c, 9, b)
    assert r["verdict"] == "warn" and r["silk_left"]["drc"] == {"silk_over_copper": 3} and "silk_over_copper" in r["line"]
    monkeypatch.setattr(PL, "_silk_drc_left", lambda board: {})
    assert PL.st_silk(c, 9, b)["verdict"] == "pass"


def test_in_fresh_process_runs_elsewhere_and_keeps_the_traceback():
    assert PL.in_fresh_process("os", "getpid") != os.getpid()
    assert PL.in_fresh_process("os.path", "join", "a", "b") == os.path.join("a", "b")
    with pytest.raises(RuntimeError) as e:
        PL.in_fresh_process("os", "listdir", "/nonexistent-dir-xyz")
    assert "FileNotFoundError" in str(e.value) and "Traceback" in str(e.value)


def test_close_stage_runs_in_a_fresh_process_and_a_crash_leaves_its_traceback(tmp_path, monkeypatch):
    b = _board(tmp_path)
    a = _args(tmp_path, b)
    c = PL.Ctx(a, a.workdir, b, {})
    seen = {}

    def fake(module, func, *args, **kw):
        seen["call"] = (module, func)
        raise RuntimeError("Traceback (most recent call last):\n  ...\nAttributeError: 'SwigPyObject' object has no attribute 'GetTracks'")
    monkeypatch.setattr(PL, "in_fresh_process", fake)
    r = PL.st_close(c, 8, b)
    assert seen["call"] == ("fluxplace.gaps", "close") and r["verdict"] == "warn" and "GetTracks" in r["line"]
    assert "Traceback" in open(os.path.join(c.dir(8, "close"), "crash.log")).read()


def test_close_follows_route_in_both_flows():
    for flow in (PL.STAGES, PL.CONSTRUCTIVE):
        assert flow[flow.index("route") + 1] == "close"
    assert PL.FUNCS["close"] is PL.st_close


def test_tune_locks_pair_copper_only(monkeypatch):
    from fluxplace import boardtext as BT
    seg = lambda net, y: f'\t(segment\n\t\t(start 1 {y})\n\t\t(end 2 {y})\n\t\t(width 0.2)\n\t\t(layer "F.Cu")\n\t\t(net "{net}")\n\t\t(uuid "u{y}")\n\t)\n'
    text = "(kicad_pcb\n" + seg("D_P", 1) + seg("SIG", 2) + '\t(via\n\t\t(at 3 3)\n\t\t(size 0.6)\n\t\t(drill 0.3)\n\t\t(net "D_N")\n\t)\n)\n'
    out, n = BT.lock_nets(text, {"D_P", "D_N"})
    assert n == 2 and out.count("(locked yes)") == 2
    assert [BT.is_locked(b) for _i, _j, _k, b in BT.scan(out)] == [True, False, True]
    assert BT.lock_nets(out, {"D_P", "D_N"})[1] == 0           # idempotent

def test_critroute_order_and_packets_exit_5(tmp_path, monkeypatch):
    assert PL.CONSTRUCTIVE[PL.CONSTRUCTIVE.index("pairs") + 1] == "tune"
    assert PL.CONSTRUCTIVE[PL.CONSTRUCTIVE.index("tune") + 1] == "critroute"
    assert PL.CONSTRUCTIVE.count("tune") == 1 and "tune" not in PL.CONSTRUCTIVE[PL.CONSTRUCTIVE.index("route"):]
    assert PL.CONSTRUCTIVE[PL.CONSTRUCTIVE.index("critroute") + 1] == "plan-route"
    assert PL.STAGES[PL.STAGES.index("critroute") + 1] == "route"
    assert "packets" in PL.VERDICTS
    monkeypatch.setitem(PL.FUNCS, "critroute", lambda c, n, board: {"verdict": "packets", "line": "p", "board": board})
    b = _board(tmp_path)
    a = _args(tmp_path, b, from_stage="critroute", to_stage="critroute")
    assert PL.run(a) == 5
    assert PL.load_state(a.workdir)["stages"]["critroute"]["verdict"] == "packets"


def test_stage_board_has_stable_relative_link(tmp_path, monkeypatch):
    def fake(c, n, board):
        d = c.dir(n, "critroute")
        dst = os.path.join(d, "board.kicad_pcb")
        shutil.copy(board, dst)
        open(os.path.join(d, "board.kicad_pro"), "w").write("{}")
        return {"verdict": "pass", "line": "fake", "board": dst}
    monkeypatch.setitem(PL.FUNCS, "critroute", fake)
    a = _args(tmp_path, _board(tmp_path), from_stage="critroute", to_stage="critroute")
    assert PL.run(a) == 0
    link = os.path.join(a.workdir, "critroute.kicad_pcb")
    assert os.path.islink(link) and not os.path.isabs(os.readlink(link))
    assert os.path.realpath(link) == os.path.realpath(os.path.join(a.workdir, "6-critroute", "board.kicad_pcb"))
    assert os.path.islink(os.path.join(a.workdir, "critroute.kicad_pro"))
    assert PL.run(a) == 0                                # re-run replaces the link, no error
    assert os.path.islink(link)


def test_mask_warning_line_and_helper(tmp_path, monkeypatch):
    assert PL.mask_warning(0) == [] and PL.mask_warning(2) == [PL.MASK_WARN]
    assert PL.MASK_WARN.startswith("WARN mask opening present before routing")
    assert PL.mask_openings(str(tmp_path / "missing.kicad_pcb")) == 0
    pcbnew = pytest.importorskip("pcbnew")
    b = pcbnew.BOARD()
    s = pcbnew.PCB_SHAPE(b)
    s.SetShape(pcbnew.SHAPE_T_POLY)
    s.SetLayer(pcbnew.B_Mask)
    ps = pcbnew.SHAPE_POLY_SET()
    ps.NewOutline()
    for x, y in [(0, 0), (3000000, 0), (3000000, 3000000), (0, 3000000)]:
        ps.Append(x, y)
    s.SetPolyShape(ps)
    b.Add(s)
    b.Save(str(tmp_path / "m.kicad_pcb"))
    assert PL.mask_openings(str(tmp_path / "m.kicad_pcb")) == 1
    monkeypatch.setattr(PL, "mask_openings", lambda b: 1)
    monkeypatch.setattr(PL, "_st_route", lambda c, n, board: {"verdict": "pass", "line": "x", "next": ["kept"]})
    res = PL.st_route(None, 7, "board.kicad_pcb")
    assert res["next"] == ["kept", PL.MASK_WARN] and res["verdict"] == "pass"
    monkeypatch.setattr(PL, "mask_openings", lambda b: 0)
    assert PL.st_route(None, 7, "board.kicad_pcb")["next"] == ["kept"]


# ---- PHASE3M review fixes: gate verdicts tied to their input (H1), crashed gate record (M2), --from without state (M3) ----
def _fake_gate(monkeypatch, stop, gd, best):
    from fluxplace import gaps as GP
    def fake_close(board, d, *a, **k):
        (gd / "stop.json").write_text(json.dumps({"stop": stop}))
        return {"stop": stop, "best": str(best), "start": {"opens": 1}, "end": {"opens": 1}, "packets": []}
    monkeypatch.setattr(GP, "close", fake_close)


def test_h1_packets_gate_is_reused_only_for_the_input_it_was_written_for(tmp_path, monkeypatch):
    from types import SimpleNamespace
    a, b = tmp_path / "a.kicad_pcb", tmp_path / "b.kicad_pcb"
    a.write_text("A"); b.write_text("B")
    gd = tmp_path / "gate"; gd.mkdir()
    best = gd / "best.kicad_pcb"; best.write_text("best")
    _fake_gate(monkeypatch, "packets", gd, best)
    c = SimpleNamespace(dir=lambda n, name: str(gd), cons_path=None, cons={"ai": {"answer": "required"}}, board_in=str(a))
    g = PL._gate_check(c, 1, "pairs", str(best), ["pair-open"], src=str(a))
    assert g["verdict"] == "packets"
    assert PL._gate_reusable(str(gd), str(a))                 # the stop.json carries the input's fingerprint
    assert not PL._gate_reusable(str(gd), str(b))             # another input: not reused


def test_m2_crashed_gate_still_returns_a_gate_record(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fluxplace import gaps as GP
    a = tmp_path / "a.kicad_pcb"; a.write_text("A")
    gd = tmp_path / "gate"; gd.mkdir()
    def boom(*a_, **k):
        raise RuntimeError("probe died")
    monkeypatch.setattr(GP, "close", boom)
    c = SimpleNamespace(dir=lambda n, name: str(gd), cons_path=None, cons={}, board_in=str(a))
    g = PL._gate_check(c, 1, "pairs", str(a), ["pair-open"])
    assert g["verdict"] == "fail" and g["gate"]["stop"] == "crashed" and g["gate"]["kinds"] == ["pair-open"]


def test_m3_from_stage_without_state_checks_the_missing_gate_on_the_input(tmp_path, monkeypatch):
    from fluxplace import gaps as GP
    board = _board(tmp_path)
    monkeypatch.setattr(GP, "GATE_DEFAULTS", {"place": ["pair-open"]})
    _fake_gate(monkeypatch, "clean", tmp_path, tmp_path / "x.kicad_pcb")
    monkeypatch.setattr(GP, "gate_measure", lambda *a, **k: {"stop": "clean", "left": [], "start": {"opens": 0}, "best": str(board)})
    monkeypatch.setattr(GP, "close", lambda *a, **k: (_ for _ in ()).throw(AssertionError("repair loop ran on --from")))
    seen = []
    def stage(c, n, b):
        seen.append(n)
        return {"verdict": "pass", "board": b, "line": "stub"}
    monkeypatch.setattr(PL, "FUNCS", {name: stage for name in PL.STAGES})
    a = _args(tmp_path, board, from_stage="route-ready", to_stage="route-ready")
    rc = PL.run(a)
    assert rc == 0 and seen                                   # ran the stage instead of exiting 5 for the missing gate


def test_a5_from_on_an_unclean_gate_exits_5_with_gaps_and_no_rung_attempt(tmp_path, monkeypatch, capsys):
    from fluxplace import gaps as GP
    board = _board(tmp_path)
    monkeypatch.setattr(GP, "GATE_DEFAULTS", {"place": ["short"]})
    monkeypatch.setattr(GP, "close", lambda *a, **k: (_ for _ in ()).throw(AssertionError("repair loop ran")))
    left = [{"id": "g1", "kind": "short", "class": "hs-pair", "nets": ["A_P"], "at": [1.0, 2.0]}]
    monkeypatch.setattr(GP, "gate_measure", lambda *a, **k: {"stop": "open", "left": left, "start": {"opens": 1}, "best": str(board)})
    seen = []
    monkeypatch.setattr(PL, "FUNCS", {name: (lambda c, n, b: seen.append(n) or {"verdict": "pass", "board": b}) for name in PL.STAGES})
    rc = PL.run(_args(tmp_path, board, from_stage="route-ready", to_stage="route-ready"))
    out = capsys.readouterr().out
    assert rc == 5 and not seen and "gap g1 short" in out


def test_a5_from_critroute_without_state_starts_the_stage(tmp_path, monkeypatch, capsys):
    from fluxplace import gaps as GP
    board = _board(tmp_path)
    # pair-skew is owned by `tune`, which runs before critroute: --from critroute skips it, so the finding must not stop the flow
    left = [{"id": "p1", "kind": "pair-skew", "class": "hs-pair", "nets": ["MIC_P", "MIC_N"], "at": [1.0, 2.0]}]
    monkeypatch.setattr(GP, "gate_measure", lambda *a, **k: {"stop": "open", "left": left, "start": {"opens": 0}, "best": str(board)})
    monkeypatch.setattr(GP, "close", lambda *a, **k: (_ for _ in ()).throw(AssertionError("repair loop ran on --from")))
    seen = []
    monkeypatch.setattr(PL, "FUNCS", {name: (lambda name: lambda c, n, b: seen.append(name) or {"verdict": "pass", "board": b})(name)
                                      for name in PL.STAGES})
    rc = PL.run(_args(tmp_path, board, from_stage="critroute", to_stage="critroute", flow="constructive"))
    out = capsys.readouterr().out
    assert rc == 0 and seen == ["critroute"], out
    assert "deferred" in out


def test_a5_tune_stage_passes_when_pairs_are_still_over_skew(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fluxplace import tune as TU
    b = tmp_path / "in.kicad_pcb"; b.write_text("(kicad_pcb)\n")
    c = SimpleNamespace(dir=lambda n, name: str(tmp_path), cons_path=None)
    monkeypatch.setattr(PL, "_run_cli", lambda argv, log: (0, "  MIC_P/MIC_N: P 5 / N 6 mm, skew +1.0   <-- still over\ntune: 0 pair(s) got worse"))
    monkeypatch.setattr(TU, "lock_tuned", lambda *a, **k: None)
    r = PL.st_tune(c, 1, str(b))
    assert r["verdict"] == "pass" and "still over" in r["line"]
    monkeypatch.setattr(PL, "_run_cli", lambda argv, log: (1, "tune: 1 pair(s) got worse"))
    assert PL.st_tune(c, 1, str(b))["verdict"] == "fail"

def test_close_stage_runs_the_post_route_snap_first_and_closes_the_cleaned_board(tmp_path, monkeypatch):
    b = _board(tmp_path)
    a = _args(tmp_path, b)
    c = PL.Ctx(a, a.workdir, b, {})
    calls = []

    def fake(module, func, *args, **kw):
        calls.append((module, func, args[0]))
        if func == "clean":
            return {"snap": {"moved": 5}, "unconnected": [3, 3], "kept": True}
        return {"start": {"opens": 3, "errors": 9}, "end": {"opens": 2, "errors": 4}, "attempts": 1, "stop": "clean", "best": "BEST"}
    monkeypatch.setattr(PL, "in_fresh_process", fake)
    from fluxplace import boardwrite as BW
    monkeypatch.setattr(BW, "commit", lambda *a, **k: {"written": True, "why": ""})     # the manifest carry is tested in test_commit_point_real
    r = PL.st_close(c, 8, b)
    assert [x[1] for x in calls] == ["clean", "close"] and calls[1][2].endswith("postroute.kicad_pcb")
    assert r["verdict"] == "pass" and "snapped 5" in r["line"] and r["board"] == "BEST"

def test_trace_finish_sits_directly_before_silk():
    # TF T1a item 1: classic flow after tune, constructive flow after close; one flow, no flag.
    assert PL.STAGES[PL.STAGES.index("silk") - 1] == "trace-finish"
    assert PL.CONSTRUCTIVE[PL.CONSTRUCTIVE.index("silk") - 1] == "trace-finish"
    assert PL.STAGES[PL.STAGES.index("trace-finish") - 1] == "tune"
    assert PL.CONSTRUCTIVE[PL.CONSTRUCTIVE.index("trace-finish") - 1] == "close"
    assert PL.ALL_STAGES.count("trace-finish") == 1
    assert "trace-finish" in PL.FUNCS


def test_route_widths_failure_fails_the_stage_not_a_default_width_route(tmp_path, monkeypatch):
    from fluxplace import power as PWR
    b = _board(tmp_path)
    PL.run(_args(tmp_path, b, to_stage="anchor-check"))
    monkeypatch.setenv("FLUXPLACE_STAGE_INPROC", "1")       # B4: the patch must reach the stage, so run it in-process
    monkeypatch.setattr(PWR, "route_widths", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no width table")))
    a2 = _args(tmp_path, None, from_stage="route", to_stage="route", answer=["rule-floors=standard"])
    assert PL.run(a2) == 1
    r = PL.load_state(a2.workdir)["stages"]["route"]
    assert r["verdict"] == "fail" and "rail width table failed" in r["line"]


def test_from_silk_runs_when_the_route_gate_has_a_power_width_gap(tmp_path, monkeypatch, capsys):
    from fluxplace import gaps as GP
    board = _board(tmp_path)
    left = [{"id": "w1", "kind": "power-width", "class": "power", "nets": ["+5V"], "at": [1.0, 2.0]}]
    monkeypatch.setattr(GP, "gate_measure", lambda *a, **k: {"stop": "open", "left": left, "start": {"opens": 0}, "best": str(board)})
    monkeypatch.setattr(GP, "close", lambda *a, **k: (_ for _ in ()).throw(AssertionError("repair loop ran on --from")))
    seen = []
    monkeypatch.setattr(PL, "FUNCS", {name: (lambda name: lambda c, n, b: seen.append(name) or {"verdict": "pass", "board": b})(name)
                                      for name in PL.STAGES})
    rc = PL.run(_args(tmp_path, board, from_stage="silk", to_stage="silk", flow="constructive"))
    out = capsys.readouterr().out
    assert rc == 0 and seen == ["silk"] and "deferred" in out, out


def test_relative_datasheets_dir_falls_back_to_the_project_dir_and_names_both(tmp_path):
    from fluxplace import constraints as C
    proj, cdir = tmp_path / "proj", tmp_path / "elsewhere"
    (proj / "docs" / "ds").mkdir(parents=True); cdir.mkdir()
    (proj / "b.kicad_pro").write_text("{}")
    board = proj / "b.kicad_pcb"; board.write_text("x")
    cp = cdir / "c.toml"; cp.write_text('[docs]\ndatasheets = "docs/ds"\n')
    assert C.load(str(cp), str(board))["docs"]["datasheets"] == str(proj / "docs" / "ds")
    with pytest.raises(SystemExit) as e:
        C.load(str(cp))
    assert str(cdir / "docs/ds") in str(e.value)
    (proj / "docs" / "ds").rmdir()
    with pytest.raises(SystemExit) as e:
        C.load(str(cp), str(board))
    assert str(cdir / "docs/ds") in str(e.value) and str(proj / "docs/ds") in str(e.value)


def _copied_constraints(tmp_path):
    proj, bd = tmp_path / "proj", tmp_path / "build"
    (proj / "docs" / "datasheets").mkdir(parents=True); bd.mkdir()
    (proj / "p.kicad_pro").write_text("{}")
    board = proj / "p.kicad_pcb"; board.write_text("(kicad_pcb)\n")
    cp = bd / "constraints.toml"; cp.write_text('[docs]\ndatasheets = "docs/datasheets"\n')
    return board, cp, proj / "docs" / "datasheets"


def test_every_cli_command_resolves_datasheets_via_the_board_project_dir(tmp_path, monkeypatch):
    from fluxplace import constraints as C
    board, cp, ds = _copied_constraints(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(C, "PROJECT_REF", None)
    with pytest.raises(SystemExit):
        C.load(str(cp))                                   # no project known: refused
    seen = {}
    monkeypatch.setattr(cli, "cmd_silk", lambda a: seen.update(ds=C.load(a.constraints)["docs"]["datasheets"]))
    cli._main(["silk", str(board), "--constraints", str(cp)])
    assert seen["ds"] == str(ds)


def test_pipeline_intake_with_copied_constraints_finds_the_project_datasheets(tmp_path, monkeypatch):
    from fluxplace import constraints as C
    board, cp, ds = _copied_constraints(tmp_path)
    monkeypatch.setattr(C, "PROJECT_REF", None)
    c = PL.Ctx(_args(tmp_path, str(board), constraints=str(cp)), str(tmp_path / "W"), str(board), None)
    assert c.cons["docs"]["datasheets"] == str(ds)


def test_ctx_makes_the_constraints_and_spec_paths_absolute(tmp_path, monkeypatch):
    """w84: close's rungs run as children with cwd = the work dir; a relative --constraints path made all 46 of them fail (FileNotFoundError)."""
    b = tmp_path / "b.kicad_pcb"
    b.write_text("x")
    (tmp_path / "c.toml").write_text("")
    (tmp_path / "s.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    c = PL.Ctx(_args(tmp_path, str(b), constraints="c.toml", spec="s.json"), str(tmp_path / "W"), str(b), None)
    assert c.cons_path == str(tmp_path / "c.toml") and c.spec_path == str(tmp_path / "s.json")


# ---- k13: stop on fail, [go.<stage>] thresholds, resume
def _stub_flow(monkeypatch, tmp_path, results):
    """FUNCS stubs returning results[name] (default pass); returns the list of stages that ran."""
    seen = []
    def mk(name):
        def f(c, n, b):
            seen.append(name)
            return dict({"verdict": "pass", "board": b, "line": "stub"}, **results.get(name, {}))
        return f
    monkeypatch.setattr(PL, "FUNCS", {name: mk(name) for name in PL.STAGES})
    from fluxplace import gaps as GP
    monkeypatch.setattr(GP, "GATE_DEFAULTS", {})
    return seen


def _cons(tmp_path, text):
    p = tmp_path / "c.toml"; p.write_text(text)
    return str(p)


def test_k13_close_stalled_stops_before_trace_finish(tmp_path, monkeypatch, capsys):
    seen = _stub_flow(monkeypatch, tmp_path, {"close": {"verdict": "warn", "unclosed": "stalled", "line": "close: opens 12->12, stop stalled"}})
    rc = PL.run(_args(tmp_path, _board(tmp_path), flow="classic"))
    out = capsys.readouterr().out
    assert rc == 3 and "trace-finish" not in seen and seen[-1] == "close"
    assert "pipeline: stop at close:" in out
    s = json.load(open(tmp_path / "W" / "STOP.json"))
    assert s["stage"] == "close" and s["checks"][0]["measured"] == "stalled" and s["input_sha1"] and s["resume"]


def test_k13_go_route_opens_max_miss_stops(tmp_path, monkeypatch):
    from fluxplace import stopgate as SGm
    seen = _stub_flow(monkeypatch, tmp_path, {})
    monkeypatch.setattr(SGm, "_opens", lambda b, w: 12)
    cons = _cons(tmp_path, "[go.route]\nopens_max = 8\n")
    rc = PL.run(_args(tmp_path, _board(tmp_path), constraints=cons))
    assert rc == 3 and seen[-1] == "route"
    s = json.load(open(tmp_path / "W" / "STOP.json"))
    assert s["checks"] == [{"check": "opens", "measured": 12, "limit": "<= 8"}]


def test_k13_on_fail_continue_keeps_old_behaviour(tmp_path, monkeypatch):
    seen = _stub_flow(monkeypatch, tmp_path, {"close": {"verdict": "warn", "unclosed": "stalled"}})
    cons = _cons(tmp_path, '[flow]\non_fail = "continue"\n')
    rc = PL.run(_args(tmp_path, _board(tmp_path), constraints=cons))
    assert rc == 0 and seen[-1] == "review"
    assert not (tmp_path / "W" / "STOP.json").exists()


def test_k13_rerun_resumes_at_the_stopped_stage(tmp_path, monkeypatch, capsys):
    from fluxplace import stopgate as SGm
    seen = _stub_flow(monkeypatch, tmp_path, {})
    opens = [12]
    monkeypatch.setattr(SGm, "_opens", lambda b, w: opens[0])
    cons = _cons(tmp_path, "[go.route]\nopens_max = 8\n")
    b = _board(tmp_path)
    assert PL.run(_args(tmp_path, b, constraints=cons)) == 3
    seen.clear(); opens[0] = 0
    rc = PL.run(_args(tmp_path, b, constraints=cons))
    out = capsys.readouterr().out
    assert rc == 0 and seen[0] == "route" and "ready" not in seen and "critroute" not in seen
    assert "pipeline: resume at route (stages 1..6 passed" in out
    assert not (tmp_path / "W" / "STOP.json").exists()


def test_k13_deferral_to_a_later_owner_still_continues(tmp_path, monkeypatch):
    from fluxplace import gaps as GP
    seen = _stub_flow(monkeypatch, tmp_path, {})
    row = {"id": "p1", "kind": "pair-skew", "class": "hs-pair", "nets": ["A", "B"], "at": [1.0, 2.0]}
    monkeypatch.setattr(GP, "GATE_DEFAULTS", {"critroute": ["pair-skew"]})
    monkeypatch.setattr(GP, "GATE_DEFER", dict(GP.GATE_DEFER, **{"pair-skew": ("tune", "close")}))
    monkeypatch.setattr(GP, "close", lambda *a, **k: {"stop": "open", "left": [row], "start": {"opens": 0}, "end": {"opens": 0}, "best": str(_board(tmp_path))})
    rc = PL.run(_args(tmp_path, _board(tmp_path), to_stage="critroute"))
    assert rc == 0 and seen[-1] == "critroute"
    assert json.load(open(tmp_path / "W" / "state.json"))["stages"]["critroute"]["gate"]["stop"] == "deferred"
