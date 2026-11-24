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
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = _procs.run([sys.executable, "-c", _FRESH, root, a, r], capture_output=True, text=True, timeout=timeout)
        if not os.path.isfile(r):
            raise RuntimeError(f"{module}.{func}: child exited {p.returncode} without a result: {(p.stderr or p.stdout)[-300:]}")
        out = json.load(open(r))
    if not out["ok"]:
        raise RuntimeError(out["error"])
    return out["result"]
