"""Router rules, the post-batch audit, open-connection work lists and parallel per-net routing, against the FAKE
router of test_batchroute (no real py_router)."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
import test_batchroute as T
from fluxplace import batchroute as BR, routeaudit as RA, routeconn as RC, routediag as RD, routepar as RP
from fluxplace import routerules as RR, routestatus as RS

CONS = {"fab": {"min_track_mm": 0.127, "min_clearance_mm": 0.127, "min_via_dia_mm": 0.4, "min_drill_mm": 0.2}}


def rules(board, **kw):
    R = RR.rules(dict(CONS, route=dict(kw)), board, 0.15, 0.15)
    return R


def mk(tmp_path, n=8, **cfg):
    board, W, run, extra, spath = T.setup(tmp_path, n, **cfg)
    build = lambda src, dst, only, limited: [sys.executable, str(tmp_path / "fake.py"), src, "--output", dst] + \
        (["--max-ripup", "3"] if limited else []) + ["--nets", *only]
    return board, W, run, extra, spath, build


# ---------------------------------------------------------------- rules / flags
def test_rules_floors_flags_and_defaults(tmp_path):
    board = G.board(tmp_path, G.ic("U1", 20, 20, ["A", "B", "C", "D"], pitch=0.5) + G.two("R1", 40, 40, "A", "B"))
    R = RR.rules(CONS, board, 0.1, 0.1)
    assert R["min_track"] == 0.127 and R["track"] == 0.127 and R["clearance"] == 0.127     # requested < floor: raised
    assert R["via_in_pad"] is False and R["grid"] == 0.05 and R["mode"] == "serial" and 1 <= R["workers"] <= 16
    wide = G.board(tmp_path, G.two("R1", 40, 40, "A", "B") + G.two("R2", 60, 40, "A", "B"), name="w.kicad_pcb")
    assert RR.rules(CONS, wide, 0.2, 0.2)["grid"] == 0.1
    assert RR.rules({"route": {"grid_mm": 0.07, "via_in_pad": True}}, wide)["grid"] == 0.07
    krt = tmp_path / "krt"
    (krt / "py_router").mkdir(parents=True)
    (krt / "py_router" / "route.py").write_text('"--fab-overrides" "--same-net-pad-clearance" "--grid-step" "--fab-tier"')
    fl = RR.flags(R, str(krt), str(tmp_path / "W"))
    assert "--same-net-pad-clearance=0.2" in fl and fl[fl.index("--grid-step") + 1] == "0.05"
    txt = open(fl[fl.index("--fab-overrides") + 1]).read()
    assert "clearance = 0.127" in txt and "track_width = 0.127" in txt and "via_diameter = 0.4" in txt and "via_drill = 0.2" in txt
    R2 = RR.rules(dict(CONS, route={"via_in_pad": True}), board)
    assert "--same-net-pad-clearance=-1" in RR.flags(R2, str(krt), str(tmp_path / "W"))


def test_pipeline_route_cmd_carries_the_rules(tmp_path):
    from fluxplace import pipeline as PL
    import argparse
    krt = tmp_path / "krt"
    (krt / "py_router").mkdir(parents=True)
    (krt / "py_router" / "route.py").write_text('"--fab-overrides" "--same-net-pad-clearance" "--grid-step"')
    a = argparse.Namespace(router_py="py", router_dir=str(krt), layers=["F.Cu", "B.Cu"], track=0.1, clearance=0.1)
    board = G.board(tmp_path, G.two("R1", 40, 40, "A", "B"))
    R = RR.rules(CONS, board, a.track, a.clearance)
    cmd = PL._route_cmd(a, board, "o.kicad_pcb", ["A"], limited=True, R=R, rflags=RR.flags(R, str(krt), str(tmp_path)))
    j = " ".join(cmd)
    assert "--fab-overrides" in j and "--same-net-pad-clearance=0.2" in j and "--track-width 0.127" in j
    assert float(cmd[cmd.index("--clearance") + 1]) == round(0.127 + R["grid"], 6) and cmd[-2:] == ["--nets", "A"]


# ---------------------------------------------------------------- audit
def rt(path, n):
    op = RC.open_by_net(path, method="text")
    return {f"N{i}" for i in range(n) if f"N{i}" not in op}


def trk(y, net, w=0.15, x1=29.5):
    """pad 1 of RA -> up 1 mm -> across -> pad 1 of RB, clear of RA's second pad."""
    pts = [(9.5, y), (9.5, y + 1), (x1, y + 1), (29.5, y)]
    return "".join(f'(segment (start {a} {b}) (end {c} {d}) (width {w}) (layer "F.Cu") (net "{net}"))\n'
                   for (a, b), (c, d) in zip(pts, pts[1:]))


def board_with(tmp_path, extra_body, name="x.kicad_pcb"):
    body = G.two("RA0", 10, 8, "N0", "P0") + G.two("RB0", 30, 8, "N0", "Q0") + \
        G.two("RA1", 10, 16, "N1", "P1") + G.two("RB1", 30, 16, "N1", "Q1")
    return G.board(tmp_path, body + extra_body, name=name, outline=(0, 0, 100, 100))


def test_audit_flags_via_in_pad_thin_track_and_clearance(tmp_path):
    base = board_with(tmp_path, "", "base.kicad_pcb")
    R = rules(base)
    seg = trk(8, "N0")
    assert RA.audit(base, board_with(tmp_path, seg, "ok.kicad_pcb"), R) == []
    via = '(via (at 29.6 8.1) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net "N0"))\n'     # inside RB0 pad 1 (same net)
    v = RA.audit(base, board_with(tmp_path, seg + via, "v.kicad_pcb"), R)
    assert [x["code"] for x in v] == ["VIA_IN_PAD"] and "RB0.1" in v[0]["msg"]
    foreign = '(via (at 10.5 16) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net "N0"))\n'    # inside a pad of ANOTHER net
    assert "VIA_IN_PAD" in {x["code"] for x in RA.audit(base, board_with(tmp_path, seg + foreign, "f.kicad_pcb"), R)}
    thin = trk(8, "N0", 0.1)
    assert {x["code"] for x in RA.audit(base, board_with(tmp_path, thin, "t.kicad_pcb"), R)} == {"TRACK_WIDTH"}
    near = seg + '(segment (start 15 9.2) (end 25 9.2) (width 0.15) (layer "F.Cu") (net "N1"))\n'       # gap 0.05 mm
    assert "CLEARANCE" in {x["code"] for x in RA.audit(base, board_with(tmp_path, near, "c.kicad_pcb"), R)}
    ok_gap = near.replace("9.2", "9.35")                                                          # gap 0.2 mm
    assert RA.audit(base, board_with(tmp_path, ok_gap, "g.kicad_pcb"), R) == []
    # via in pad allowed when [route] via_in_pad = true
    assert RA.audit(base, board_with(tmp_path, seg + via, "v2.kicad_pcb"), rules(base, via_in_pad=True)) == []


def test_audit_drc_new_types(tmp_path):
    cli = tmp_path / "kicad-cli"
    cli.write_text('#!/bin/sh\nout=""\nwhile [ $# -gt 0 ]; do [ "$1" = "--output" ] && out="$2"; shift; done\n'
                   'if grep -q SHORT "$(ls ' + str(tmp_path) + '/*.kicad_pcb | head -0)" 2>/dev/null; then :; fi\n'
                   'case "$LAST" in *) ;; esac\n'
                   'if grep -q DRCBAD "$BOARDARG"; then echo \'{"violations":[{"type":"clearance"},{"type":"shorting_items"}]}\' > "$out"; '
                   'else echo \'{"violations":[{"type":"clearance"}]}\' > "$out"; fi\n')
    # the board path is the last argument; wrap so BOARDARG is set
    cli.write_text('#!/bin/sh\nfor last; do :; done\nout=""\nwhile [ $# -gt 0 ]; do [ "$1" = "--output" ] && out="$2"; shift; done\n'
                   'if grep -q DRCBAD "$last"; then echo \'{"violations":[{"type":"clearance"},{"type":"shorting_items"}]}\' > "$out"; '
                   'else echo \'{"violations":[{"type":"clearance"}]}\' > "$out"; fi\n')
    cli.chmod(0o755)
    a = tmp_path / "a.kicad_pcb"; a.write_text("(kicad_pcb)\n")
    b = tmp_path / "b.kicad_pcb"; b.write_text("(kicad_pcb) DRCBAD\n")
    viol, base = RA.new_drc_types(str(a), str(b), str(cli))
    assert base == {"clearance": 1} and [v["code"] for v in viol] == ["DRC_NEW"] and "shorting_items" in viol[0]["msg"]
    assert RA.new_drc_types(str(a), str(a), str(cli))[0] == []


# ---------------------------------------------------------------- serial: audit discards, partly routed nets
def serial(tmp_path, n=8, batch=4, R=True, **cfg):
    board, W, run, extra, spath, build = mk(tmp_path, n, **cfg)
    rr = rules(board) if R else None
    r = BR.route_batches(board, W, [f"N{i}" for i in range(n)], run, batch_nets=batch, batch_timeout=3,
                         status_path=spath, extra=extra, log=lambda m: None, rules=rr, leftover_timeout=3)
    return board, W, r, spath


def test_batch_with_via_in_pad_is_discarded_halved_and_reported(tmp_path):
    board, W, r, spath = serial(tmp_path, via_in_pad=["N2"])
    assert r["leftovers"] == ["N2"] and r["reasons"] == {"N2": "RULE_VIOLATION"}
    assert T.routed(r["board"]) == {f"N{i}" for i in range(8)} - {"N2"}
    assert RA.audit(board, r["board"], rules(board)) == []                 # nothing violating ever reached a checkpoint
    assert r["halvings"] >= 2 and r["violations"] >= 3
    st = RS.load(spath)
    assert st["rule_violations"] == r["violations"] and "RULE_VIOLATION" in RS.one_line(st)
    assert any(x["result"].startswith("RULE_VIOLATION") for x in st["rounds"])


def test_thin_track_batch_is_discarded(tmp_path):
    board, W, r, spath = serial(tmp_path, thin=["N6"])
    assert r["reasons"] == {"N6": "RULE_VIOLATION"} and T.routed(r["board"]) == {f"N{i}" for i in range(8)} - {"N6"}


def test_without_rules_nothing_is_audited(tmp_path):
    board, W, r, spath = serial(tmp_path, R=False, thin=["N6"])
    assert r["leftovers"] == [] and r["violations"] == 0


def test_partly_routed_net_is_in_the_work_list(tmp_path):
    body = ""
    for i in range(3):
        body += G.two(f"RA{i}", 10, 8 + 8 * i, f"N{i}", f"P{i}") + G.two(f"RB{i}", 30, 8 + 8 * i, f"N{i}", f"Q{i}")
    body += G.two("RC1", 20, 16, "N1", "Z1")                      # N1 has a third pad
    body += '(segment (start 9.5 16) (end 9.5 17) (width 0.15) (layer "F.Cu") (net "N1"))\n'   # a stub: partly routed
    board = G.board(tmp_path, body, outline=(0, 0, 100, 100))
    assert RD.unrouted_nets(RD.load(board)) == ["N0", "N2"]       # the old view: N1 "has copper"
    assert RC.open_by_net(board, method="text") == {"N0": 1, "N1": 2, "N2": 1}
    (tmp_path / "fake.py").write_text(T.FAKE)
    cfg = {"log": str(tmp_path / "calls.jsonl")}
    (tmp_path / "cfg.json").write_text(json.dumps(cfg))
    os.environ["FAKE_CFG"] = str(tmp_path / "cfg.json")
    W = tmp_path / "W"; W.mkdir()
    extra = {}
    spath = str(W / "s.json")
    build = lambda src, dst, only, limited: [sys.executable, str(tmp_path / "fake.py"), src, "--output", dst, "--nets", *only]
    run = BR.make_runner(build, str(tmp_path), extra, spath, BR.settings({}))
    r = BR.route_batches(board, str(W), ["N0", "N1", "N2"], run, batch_nets=10, batch_timeout=3, status_path=spath,
                         extra=extra, log=lambda m: None)
    assert r["start_open"] == 4 and r["start_open_nets"] == 3
    assert ["N0", "N1", "N2"] in [c["nets"] for c in T.calls(tmp_path)]
    assert r["open"] == 1 and r["leftovers"] == ["N1"]            # the fake joins only pads 1; RC1 stays open: counted
    st = RS.load(spath)
    assert st["start_open"] == 4 and st["rounds"][0]["open_before"] == 4 and st["rounds"][0]["open_after"] == 1


# ---------------------------------------------------------------- parallel
def par(tmp_path, n=8, workers=4, **cfg):
    board, W, run, extra, spath, build = mk(tmp_path, n, **cfg)
    R = rules(board)
    r = RP.route_parallel(board, W, [f"N{i}" for i in range(n)], build, run, rules=R, workers=workers, job_timeout=3,
                          status_path=spath, extra=extra, log=lambda m: None, cwd=str(tmp_path), cons=None)
    return board, W, r, spath, R


def test_parallel_merges_single_net_jobs_into_checkpoints(tmp_path):
    board, W, r, spath, R = par(tmp_path)
    assert r["leftovers"] == [] and T.routed(r["board"]) == {f"N{i}" for i in range(8)}
    assert r["start_open"] == 8 and r["open"] == 0 and len(r["checkpoints"]) == 2          # 2 rounds of 4 jobs
    assert [c["nets"] for c in T.calls(tmp_path)].count(["N0"]) == 1 and all(len(c["nets"]) == 1 for c in T.calls(tmp_path))
    assert RA.audit(board, r["board"], R) == []
    st = RS.load(spath)
    assert st["mode"] == "parallel" and st["state"] == "done"
    assert [(x["open_before"], x["open_after"]) for x in st["rounds"]] == [(8, 4), (4, 0)]
    assert "parallel round" in RS.one_line(st) and "last round open 4 -> 0" in RS.one_line(st)


def test_parallel_conflicting_pair_keeps_first_and_requeues_the_other(tmp_path):
    board, W, r, spath, R = par(tmp_path, n=2, workers=2, conflict_row={"N1": 9.1})    # N1's track runs beside N0's
    st = RS.load(spath)
    assert st["rounds"][0]["merged"] == 1 and st["rounds"][0]["dropped"] == 1
    assert r["leftovers"] == [] and rt(r["board"], 2) == {"N0", "N1"} and len(r["checkpoints"]) == 2
    assert [c["nets"] for c in T.calls(tmp_path)] == [["N0"], ["N1"], ["N1"]] or sorted(c["nets"][0] for c in T.calls(tmp_path)) == ["N0", "N1", "N1"]
    assert RA.audit(board, r["board"], R) == []


def test_parallel_job_that_rips_goes_to_the_final_serial_pass(tmp_path):
    # N1's job deletes N0's copper whenever it runs on a board where N0 is routed (round 2, serial pass)
    board, W, r, spath, R = par(tmp_path, n=2, workers=1, rip={"N1": ["N0"]})
    ns = [c["nets"] for c in T.calls(tmp_path)]
    assert ns[0] == ["N0"] and ns[1] == ["N1"]                   # round 1: N0 merged; round 2: N1 job ripped N0
    st = RS.load(spath)
    assert st["rounds"][1]["ripped"] == 1 and st["rounds"][1]["merged"] == 0
    assert ns[2:] and all(c == ["N1"] for c in ns[2:])           # serial pass tried N1 (the fake rips there too)
    assert rt(r["board"], 2) >= {"N0"} and "N1" in r["leftovers"]


def test_parallel_hanging_job_is_a_timeout_leftover_and_the_rest_merge(tmp_path, monkeypatch):
    monkeypatch.setitem(RS.KILL_WAIT, "INT", 1.0)
    board, W, r, spath, R = par(tmp_path, n=4, workers=4, hang_always=["N2"])
    assert r["leftovers"] == ["N2"] and r["reasons"] == {"N2": "timeout"} and r["timeouts"] == 1
    assert rt(r["board"], 4) == {"N0", "N1", "N3"}


def test_parallel_rule_violating_job_is_not_merged(tmp_path):
    board, W, r, spath, R = par(tmp_path, n=4, workers=4, via_in_pad=["N1"], thin=["N3"])
    assert r["reasons"] == {"N1": "RULE_VIOLATION", "N3": "RULE_VIOLATION"} and r["violations"] == 2
    assert RA.audit(board, r["board"], R) == [] and rt(r["board"], 4) == {"N0", "N2"}


def test_parallel_snaps_vias_to_track_ends_and_blocks_helper(tmp_path):
    board, W, r, spath, R = par(tmp_path, n=2, workers=2, snap=["N0", "N1"])
    vias = RD.load(r["board"])["vias"]
    assert sorted((v["net"], v["x"]) for v in vias) == [("N0", 20.0), ("N1", 20.0)]
    txt = open(r["board"]).read()
    assert len(RP.blocks(txt)) == 10 and '(net "N0")' in txt


def test_pipeline_from_stage_creates_the_state(tmp_path):
    from fluxplace import pipeline as PL
    import argparse
    board = G.board(tmp_path, G.two("R1", 40, 40, "A", "B"))
    a = argparse.Namespace(board=board, workdir=str(tmp_path / "W"), constraints=None, spec=None, status=False,
                           from_stage="review", to_stage="review", waive=None, api=False, force=False)
    # review will fail on the bare board, but the state file must exist and name the board
    try:
        PL.run(a)
    except SystemExit:
        pass
    st = json.load(open(tmp_path / "W" / "state.json"))
    assert st["board0"] == board and "review" in st["stages"]


def test_pcbnew_helper_counts_open_connections_on_a_real_board(monkeypatch):
    import pytest, shutil
    if not shutil.which("/usr/bin/python3"):
        pytest.skip("no KiCad python")
    monkeypatch.setattr(RC, "PY", "/usr/bin/python3")
    sample = os.path.join(os.path.dirname(__file__), "..", "fluxplace", "sample", "sample.kicad_pcb")
    try:
        m = RC.measure(sample, fill=True, method="pcbnew")
    except Exception:
        pytest.skip("pcbnew not importable")
    assert m["method"] == "pcbnew" and not m["approx"] and m["total"] == 20      # KiCad's own unconnected count
    t = RC.measure(sample, method="text")
    assert t["approx"] and t["total"] > 0


# ---------------------------------------------------------------- item 1: clearance request margin, dropped-by-reason
def test_clearance_margin_requested_floor_kept(tmp_path):
    board = G.board(tmp_path, G.two("R1", 40, 40, "A", "B"))
    R = RR.rules(CONS, board, 0.15, 0.2)
    assert R["clearance_margin"] == R["grid"] and R["min_clearance"] == 0.127
    assert abs(RR.request_clearance(R) - (0.2 + R["grid"])) < 1e-9                  # default clearance + one grid step
    assert RR.size_args(R)[3] == f"{0.2 + R['grid']:g}"
    R2 = RR.rules(dict(CONS, route={"clearance_margin_mm": 0.03}), board, 0.15, 0.2)
    assert abs(RR.request_clearance(R2) - 0.23) < 1e-9 and R2["min_clearance"] == 0.127   # the audit floor is unchanged
    assert RR.rules({}, board, 0.15, 0.2)["clearance_margin"] == 0.0                # no floor, no margin
    krt = tmp_path / "krt"
    (krt / "py_router").mkdir(parents=True)
    (krt / "py_router" / "route.py").write_text('"--escalation" "--strict-sizes"')
    fl = RR.flags(R, str(krt), str(tmp_path))
    assert fl[fl.index("--escalation") + 1] == "off" and "--strict-sizes" in fl
    assert "--strict-sizes" not in RR.flags(RR.rules(dict(CONS, route={"strict_sizes": False}), board), str(krt), str(tmp_path))


def test_dropped_by_reason_in_status(tmp_path):
    from fluxplace import routedrops as DR
    assert DR.reason_of([{"code": "VIA_IN_PAD"}]) == "via-in-pad"
    assert DR.reason_of([{"code": "CLEARANCE"}, {"code": "CLEARANCE"}, {"code": "TRACK_WIDTH"}]) == "under-floor"
    assert DR.reason_of([{"code": "OFF_BOARD"}]) == "off-board" and DR.reason_of([{"code": "FANOUT_RIPPED"}]) == "rips-other-net"
    t = DR.Tally()
    t.add("under-floor"); t.add("under-floor"); t.new_round(); t.add("timeout")
    assert t.totals() == {"under-floor": 2, "timeout": 1} and t.this_round() == {"timeout": 1}
    board, W, run, extra, spath, build = mk(tmp_path, 6, thin=["N1", "N4"])
    r = RP.route_parallel(board, W, [f"N{i}" for i in range(6)], build, run, B=None, rules=rules(board), workers=3,
                          job_timeout=10, status_path=spath, extra=extra, log=lambda m: None, cwd=str(tmp_path))
    assert r["dropped"].get("under-floor") == 2
    st = RS.load(spath)
    assert st["dropped"]["under-floor"] == 2 and "dropped under-floor 2" in RS.one_line(st)
    assert any(x.get("dropped_by") for x in st["rounds"])


def test_audit_off_board(tmp_path):
    base = board_with(tmp_path, "", "base.kicad_pcb")
    out = '(segment (start 95 50) (end 105 50) (width 0.15) (layer "F.Cu") (net "N0"))\n'
    v = RA.audit(base, board_with(tmp_path, out, "o.kicad_pcb"), rules(base))
    assert "OFF_BOARD" in {x["code"] for x in v}
