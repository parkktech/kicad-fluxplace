"""Run a function in a fresh interpreter.

pcbnew's SWIG bindings decay in a process that has called board.Remove() (LoadBoard then returns a raw SwigPyObject with no GetTracks;
reproduced: snapiter.run, then LoadBoard).  Code that removes items runs here, so the pipeline process stays clean for the stages after it."""
import json
import os
import sys

_FRESH = (
    "import json, sys, importlib, traceback\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "a = json.load(open(sys.argv[2]))\n"
    "try:\n"
    "    r = getattr(importlib.import_module(a['module']), a['func'])(*a.get('args', []), **a.get('kwargs', {}))\n"
    "    json.dump({'ok': True, 'result': r}, open(sys.argv[3], 'w'), default=str)\n"
    "except BaseException:\n"
    "    json.dump({'ok': False, 'error': traceback.format_exc()}, open(sys.argv[3], 'w'))\n")


def in_fresh_process(module, func, *args, timeout=None, **kwargs):
    """Call module.func(*args, **kwargs) in a new interpreter -> its (JSON-able) result; raises RuntimeError with the child's traceback.
    pcbnew's SWIG bindings decay in a process that has called board.Remove() (LoadBoard then returns a raw SwigPyObject with no
    GetTracks)."""
    import subprocess
    from fluxplace import procs as _procs
    import tempfile
    with tempfile.TemporaryDirectory(prefix="fxfresh-") as td:
        a, r = os.path.join(td, "a.json"), os.path.join(td, "r.json")
        json.dump({"module": module, "func": func, "args": list(args), "kwargs": kwargs}, open(a, "w"), default=str)
        from fluxplace import codefreeze
        root = codefreeze.root()
        p = _procs.run([sys.executable, "-c", _FRESH, root, a, r], capture_output=True, text=True, timeout=timeout)
        if not os.path.isfile(r):
            raise RuntimeError(f"{module}.{func}: child exited {p.returncode} without a result: {(p.stderr or p.stdout)[-300:]}")
        out = json.load(open(r))
    if not out["ok"]:
        raise RuntimeError(out["error"])
    return out["result"]


# ---------------------------------------------------------------- B4: one pipeline stage in its own interpreter
_STAGE = ("import sys\nsys.path.insert(0, sys.argv[1])\nfrom fluxplace import freshproc\nsys.exit(freshproc._stage_job(sys.argv[2]))\n")
DEFAULT_STAGE_TIMEOUT_MIN = 0          # [flow] stage_timeout_min / stage_timeouts.<stage>; 0 = no limit


def stage_timeout_s(cons, name):
    """[flow] stage_timeouts.<stage> (minutes) over [flow] stage_timeout_min; None = unlimited."""
    fl = (cons or {}).get("flow") or {}
    m = (fl.get("stage_timeouts") or {}).get(name, fl.get("stage_timeout_min", DEFAULT_STAGE_TIMEOUT_MIN))
    return float(m) * 60 if m and float(m) > 0 else None


def _importable(f):
    """(module, qualname) when `f` is a module-level function a fresh interpreter can import by name: the pipeline's own stages
    (module fluxplace.*), or a function marked `_isolate = True` (tests).  A test double or a closure is run in-process."""
    import importlib
    m, q = getattr(f, "__module__", None), getattr(f, "__qualname__", "")
    if not m or "<" in q or "." in q or not (m.startswith("fluxplace.") or getattr(f, "_isolate", False)):
        return None
    try:
        return (m, q) if getattr(importlib.import_module(m), q, None) is f else None
    except Exception:                                          # noqa: BLE001
        return None


def isolatable(name, funcs):
    return name in funcs and not os.environ.get("FLUXPLACE_STAGE_INPROC") and _importable(funcs[name]) is not None


def run_stage(name, c, n, board, funcs):
    """FUNCS[name](c, n, board) in a fresh interpreter -> the same result dict.  The context is rebuilt in the child from the run's
    args, work dir, input board and state (all JSON); stdout/stderr stream through.  A stage that exceeds its [flow] timeout, or
    dies natively, is a `fail` verdict (its whole process group, rung jobs and routers included, is killed); a Python exception in the
    stage re-raises here as it did in-process."""
    if not isolatable(name, funcs):
        return funcs[name](c, n, board)
    import subprocess
    import tempfile
    from . import procs as _procs, snapshot as SN
    tmo = stage_timeout_s(c.cons, name)
    sn = SN._ACTIVE
    with tempfile.TemporaryDirectory(prefix="fxstage-") as td:
        jp, rp = os.path.join(td, "job.json"), os.path.join(td, "res.json")
        json.dump({"name": name, "n": n, "board": board, "work": c.work, "board_in": c.board_in, "state": c.state,
                   "args": vars(c.a), "fn": _importable(funcs[name]), "snap_seq": sn.seq if sn is not None else None, "result": rp}, open(jp, "w"), default=str)
        from fluxplace import codefreeze
        root = codefreeze.root()
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            p = _procs.run([sys.executable, "-c", _STAGE, root, jp], timeout=tmo)
        except subprocess.TimeoutExpired:
            return {"verdict": "fail", "board": board, "line": f"{name}: killed at the stage timeout ({tmo / 60:g} min, [flow] stage_timeout_min); "
                    f"process group reaped, board unchanged"}
        if not os.path.isfile(rp):
            return {"verdict": "fail", "board": board, "line": f"{name}: stage process died without a result (exit {p.returncode}"
                    + (f", signal {-p.returncode}" if p.returncode < 0 else "") + ")"}
        out = json.load(open(rp))
    if sn is not None and out.get("snap_seq") is not None:
        sn.seq = max(sn.seq, out["snap_seq"])
    if not out["ok"]:
        raise RuntimeError(f"stage {name} (fresh process): {out['error']}")
    return out["result"]


def _stage_job(job_path):
    """Child entry of run_stage."""
    import argparse
    import traceback
    j = json.load(open(job_path))
    sn = None
    try:
        from . import pipeline as P, snapshot as SN
        if j.get("snap_seq") is not None:
            sn = SN.Snapper(j["work"])
            sn.seq = j["snap_seq"]
            SN.set_active(sn)
        c = P.Ctx(argparse.Namespace(**j["args"]), j["work"], j["board_in"], j["state"])
        import importlib
        out = {"ok": True, "result": getattr(importlib.import_module(j["fn"][0]), j["fn"][1])(c, j["n"], j["board"])}
    except BaseException:                                      # noqa: BLE001 - the parent re-raises with this traceback
        out = {"ok": False, "error": traceback.format_exc()}
    out["snap_seq"] = sn.seq if sn is not None else None
    json.dump(out, open(j["result"], "w"), default=str)
    return 0
