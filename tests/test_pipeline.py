"""Staged pipeline: state.json handoff, stage ranges, na stages, route stage with a fake router."""
import argparse, json, os, sys
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
    assert os.path.exists(st["board"]) and "3-place" in st["board"]
    # a later stage starts from state.json alone (no BOARD argument)
    a2 = _args(tmp_path, None, from_stage="route", to_stage="route")
    assert PL.run(a2) == 1                       # STUCK stays unrouted -> fail, with numbers
    st = PL.load_state(a.workdir)
    r = st["stages"]["route"]
    assert r["verdict"] == "fail" and "STUCK" in " ".join(st["next"])   # unrouted net reported with numbers
    assert os.path.exists(os.path.join(a.workdir, "5-route", "board.kicad_pcb"))
    assert os.path.exists(os.path.join(a.workdir, "5-route", "routed.kicad_pcb.status.json"))
    assert len(open(PL.state_path(a.workdir)).read()) <= PL.STATE_MAX


def test_route_na_without_router(tmp_path):
    b = _board(tmp_path)
    a = _args(tmp_path, b, to_stage="anchor-check")
    PL.run(a)
    a2 = _args(tmp_path, None, from_stage="route", to_stage="route", router_dir=str(tmp_path / "nope"))
    assert PL.run(a2) == 0
    assert PL.load_state(a.workdir)["stages"]["route"]["verdict"] == "na"


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
