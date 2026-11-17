"""Safe-write checks: board_rev + --expect-rev, one-line summaries / --brief, KiCad-open lock guard."""
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import boardlock, boardrev, hrcli  # noqa: E402

CLI = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cli.py")


# ---- rev
def test_rev_stable_and_changes(tmp_path):
    p = tmp_path / "a.kicad_pcb"
    p.write_text("(kicad_pcb)")
    r = boardrev.rev(str(p))
    assert r == boardrev.rev(str(p)) and len(r) == 12
    p.write_text("(kicad_pcb )")
    assert boardrev.rev(str(p)) != r


def test_rev_check(tmp_path):
    p = tmp_path / "a.kicad_pcb"
    p.write_text("x")
    r = boardrev.rev(str(p))
    assert boardrev.check(str(p), None) is None and boardrev.check(str(p), r) is None
    m = boardrev.check(str(p), "deadbeef")
    assert m == f"stale plan: board changed since rev deadbeef (now {r}); re-run suggest/probe"


# ---- summary
def test_summary_probe():
    v = {"kind": "clearance", "gap": 0.066, "need": 0.127, "with": "BT3_RF track", "at": [156.3, 130.6]}
    assert hrcli.summary("probe", {"ok": False, "violations": [v, v]}) == "probe failed: clearance 0.066 < 0.127 vs BT3_RF track at 156.3,130.6 (+1 more)"
    assert hrcli.summary("probe", {"ok": True, "net": "A"}) == "probe ok A: no violations"
    assert hrcli.summary("probe", {}).startswith("probe failed")


def test_summary_lay_and_shove():
    v = json.dumps([{"kind": "clearance", "gap": 0.066, "need": 0.127, "with": "T"}])
    assert hrcli.summary("lay", {"accepted": False, "reason": "probe failed: " + v}) == "lay rejected: probe failed: clearance 0.066 < 0.127 vs T"
    assert hrcli.summary("lay", {"accepted": False, "reason": "a | b"}) == "lay rejected: a"
    assert hrcli.summary("lay", {"accepted": True}) == "lay accepted"
    assert hrcli.summary("shove", {"accepted": False}) == "shove rejected: no reason given"
    assert hrcli.summary("commit", {"accepted": False, "reasons": ["x", "y"]}) == "commit rejected: x"
    assert hrcli.summary("commit", {"accepted": True, "out": "o.kicad_pcb"}) == "commit accepted -> o.kicad_pcb"


def test_summary_suggest_and_garbage():
    assert hrcli.summary("suggest", {"found": True, "length_mm": 3.2, "vias": 1}) == "suggest found: 3.2 mm, 1 via(s)"
    assert hrcli.summary("suggest", {"found": False, "reason": "sealed"}) == "suggest none: sealed"
    assert hrcli.summary("lay", {"accepted": False, "reason": 5, "reasons": 3}) == "lay rejected: 5"
    assert hrcli.summary("lay", {"accepted": False, "violations": None}).startswith("lay rejected")


def test_emit_summary_first_brief_and_rev(capsys):
    class A:
        json = True
        brief = False
    hrcli._emit(A, {"accepted": False, "reason": "r"}, "t", "lay", "abc")
    d = json.loads(capsys.readouterr().out)
    assert list(d)[0] == "summary" and d["board_rev"] == "abc"
    A.brief = True
    hrcli._emit(A, {"accepted": False, "reason": "r"}, "t", "lay", "abc")
    assert capsys.readouterr().out == "lay rejected: r\n"
    A.json = A.brief = False
    hrcli._emit(A, {"accepted": False}, "plain text", "lay", "abc")
    assert capsys.readouterr().out == "plain text\n"          # unchanged without the flags


# ---- lock
def test_lock_none_and_held(tmp_path):
    b = tmp_path / "x.kicad_pcb"
    b.write_text("")
    assert boardlock.held(str(b)) is None
    boardlock.guard(str(b))
    (tmp_path / "~x.kicad_pcb.lck").write_text(json.dumps({"hostname": "H", "username": "U"}))
    assert boardlock.held(str(b)) == {"lock": "~x.kicad_pcb.lck", "user": "U", "host": "H"}
    with pytest.raises(boardlock.BoardOpenError, match=r"board is open in KiCad \(lock ~x.kicad_pcb.lck, user U on host H\): close it or pass --force-open"):
        boardlock.guard(str(b))
    boardlock.guard(str(b), force=True)


def test_lock_unreadable_fails_closed(tmp_path):
    b = tmp_path / "x.kicad_pcb"
    b.write_text("")
    (tmp_path / "~x.kicad_pcb.lck").write_text("not json")
    with pytest.raises(boardlock.BoardOpenError):
        boardlock.guard(str(b))
    (tmp_path / "~x.kicad_pcb.lck").write_bytes(b"\xff\xfe\x00")
    assert boardlock.held(str(b))["user"] == "?"


def test_pcbnew_load_save_makes_no_lock(tmp_path):
    pcbnew = pytest.importorskip("pcbnew")
    p = str(tmp_path / "a.kicad_pcb")
    pcbnew.SaveBoard(p, pcbnew.BOARD())
    pcbnew.SaveBoard(str(tmp_path / "b.kicad_pcb"), pcbnew.LoadBoard(p))
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".lck")]


# ---- CLI wiring (refusals happen before any pcbnew/DRC work)
def _run(*args):
    return subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True)


def test_cli_lay_stale_rev_and_lock_refuse_before_writing(tmp_path):
    p = tmp_path / "a.kicad_pcb"
    p.write_text("(kicad_pcb)")
    out = tmp_path / "o.kicad_pcb"
    r = _run("lay", str(p), "--net", "A", "--path", "F.Cu: 0,0 1,1", "--out", str(out), "--expect-rev", "deadbeef")
    assert r.returncode == 2 and "stale plan: board changed since rev deadbeef" in r.stderr and not out.exists()
    (tmp_path / "~o.kicad_pcb.lck").write_text(json.dumps({"hostname": "H", "username": "U"}))
    r = _run("lay", str(p), "--net", "A", "--path", "F.Cu: 0,0 1,1", "--out", str(out))
    assert r.returncode == 2 and "board is open in KiCad" in r.stderr and not out.exists()
    r = _run("unlay", str(p), "--net", "A", "--ids", "ab", "--out", str(out))
    assert r.returncode == 2 and "--force-open" in r.stderr
    r = _run("session", "begin", str(p), "--work", str(out))
    assert r.returncode == 2 and "board is open" in r.stderr


def test_cli_probe_json_has_summary_first_rev_and_brief(tmp_path):
    pytest.importorskip("pcbnew")
    sys.path.insert(0, os.path.dirname(__file__))
    from test_handroute import gap_board
    p = gap_board(tmp_path)
    args = ["probe", p, "--net", "B", "--path", "F.Cu: 6.7,-0.3 13.3,-0.3", "--width", "0.25"]
    r = _run(*args, "--json")
    d = json.loads(r.stdout[r.stdout.index("{"):])
    assert r.returncode == 1 and list(d)[0] == "summary" and d["summary"].startswith("probe failed:") and d["board_rev"] == boardrev.rev(p)
    r = _run(*args, "--brief")
    assert r.returncode == 1 and r.stdout.count("\n") == 1 and r.stdout.startswith("probe failed:")
    assert "summary" not in _run(*args).stdout
