"""B4: every pipeline stage runs in a fresh interpreter (freshproc.run_stage), with a [flow] timeout that reaps the whole process group."""
import argparse
import os
import subprocess
import sys
import textwrap
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fluxplace import freshproc, pipeline as PL  # noqa: E402

MK = textwrap.dedent('''
    import sys, os
    import pcbnew
    b = pcbnew.CreateEmptyBoard()
    s = pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_POLY); s.SetLayer(pcbnew.B_Mask)
    s.SetPolyPoints([pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)) for x, y in ((10, 10), (30, 10), (30, 30), (10, 30))])
    b.Add(s)
    pcbnew.SaveBoard(os.path.join(sys.argv[1], "b.kicad_pcb"), b)
''')
STAGES = textwrap.dedent('''
    import os, subprocess, sys, time

    def _kind(p):
        import pcbnew
        return type(pcbnew.LoadBoard(p)).__name__

    def removes(c, n, board):
        """Remove() of a discarded item: LoadBoard in THIS process is a SwigPyObject afterwards (the swig_remove_then_load pattern)."""
        import pcbnew

        def strip():
            b = pcbnew.LoadBoard(board)
            for d in [d for d in b.GetDrawings() if d.GetShape() == pcbnew.SHAPE_T_POLY]:
                b.Remove(d)
            pcbnew.SaveBoard(board + ".out", b)
        strip()
        return {"verdict": "pass", "line": "removed", "board": board, "poisoned": _kind(board), "pid": os.getpid()}
    removes._isolate = True

    def looks(c, n, board):
        return {"verdict": "pass", "line": "looked", "board": board, "kind": _kind(board), "pid": os.getpid()}
    looks._isolate = True

    def hangs(c, n, board):
        p = subprocess.Popen(["sleep", "300"])                 # a grandchild in the stage's group
        open(os.path.join(c.work, "gc.pid"), "w").write(f"{os.getpid()} {p.pid}")
        time.sleep(300)
    hangs._isolate = True

    def boom(c, n, board):
        raise ValueError("stage bug")
    boom._isolate = True
''')


def _ctx(tmp_path, cons=""):
    cp = tmp_path / "c.toml"
    cp.write_text(cons)
    a = argparse.Namespace(constraints=str(cp), spec=None, answer=[], assume_defaults=False)
    return PL.Ctx(a, str(tmp_path), str(tmp_path / "b.kicad_pcb"), {})


def _setup(tmp_path, monkeypatch, cons=""):
    (tmp_path / "stagemod.py").write_text(STAGES)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.syspath_prepend(str(tmp_path))
    subprocess.run(["/usr/bin/python3", "-c", MK, str(tmp_path)], check=True, timeout=120)
    import stagemod
    return _ctx(tmp_path, cons), {k: getattr(stagemod, k) for k in ("removes", "looks", "hangs", "boom")}


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return not open(f"/proc/{pid}/stat").read().split(")")[1].split()[0] == "Z"


def test_swig_remove_then_load(tmp_path, monkeypatch):
    c, f = _setup(tmp_path, monkeypatch)
    b = str(tmp_path / "b.kicad_pcb")
    r1 = freshproc.run_stage("removes", c, 1, b, f)
    r2 = freshproc.run_stage("looks", c, 2, b, f)
    assert r1["line"] == "removed" and r1["poisoned"] != "BOARD", r1     # the Remove() decayed pcbnew inside the stage's own process
    assert r2["kind"] == "BOARD" and r2["pid"] != r1["pid"] != os.getpid()
    import pcbnew
    assert type(pcbnew.LoadBoard(b)).__name__ == "BOARD"                # the pipeline process is clean
    assert PL._pcbnew_state_guard("removes", dict(r1))["verdict"] == "pass"


def test_stage_exception_reraises(tmp_path, monkeypatch):
    import pytest
    c, f = _setup(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="stage bug"):
        freshproc.run_stage("boom", c, 1, str(tmp_path / "b.kicad_pcb"), f)


def test_timeout_fails_and_leaves_no_orphan(tmp_path, monkeypatch):
    c, f = _setup(tmp_path, monkeypatch, "[flow]\nstage_timeout_min = 0.05\n")
    t = time.time()
    res = freshproc.run_stage("hangs", c, 1, str(tmp_path / "b.kicad_pcb"), f)
    assert res["verdict"] == "fail" and "stage timeout" in res["line"] and time.time() - t < 30
    pids = [int(x) for x in (tmp_path / "gc.pid").read_text().split()]
    time.sleep(0.3)
    assert not any(_alive(p) for p in pids), pids


def test_per_stage_timeout_overrides():
    assert freshproc.stage_timeout_s({}, "route") is None
    cons = {"flow": {"stage_timeout_min": 10, "stage_timeouts": {"route": 120, "silk": 0}}}
    assert freshproc.stage_timeout_s(cons, "fanout") == 600
    assert freshproc.stage_timeout_s(cons, "route") == 7200
    assert freshproc.stage_timeout_s(cons, "silk") is None


def test_doubles_stay_in_process():
    def fake(c, n, b):
        return {"verdict": "pass", "pid": os.getpid()}
    assert freshproc.run_stage("x", None, 1, "b", {"x": fake})["pid"] == os.getpid()
    assert all(freshproc.isolatable(k, PL.FUNCS) for k in PL.FUNCS)
