"""Freeze the code a pipeline run uses.  A run lasts hours; subprocess rungs (`-m fluxplace.gaps --rung-job`, stage interpreters, lazy
imports) read the checkout's CURRENT files, so a pull / merge / branch switch mid-run mixed versions.  `freeze(work)` copies cli.py and
the package to <work>/.code/ once and points every later import and child at that copy (`root()`).  Resume in the same workdir reuses
the copy; to take new code, delete <work>/.code.  The copy is removed with the workdir."""
import os
import shutil
import subprocess
import sys

ENV = "FLUXPLACE_CODE"
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COPY = ("cli.py", "metadata.json", "pyproject.toml", "fluxplace", "plugins")


def root():
    """The code root children must run from: the frozen copy when a run froze one, else this checkout."""
    r = os.environ.get(ENV)
    return r if r and os.path.isdir(r) else _CHECKOUT


def _git(*a):
    try:
        p = subprocess.run(["git", "-C", _CHECKOUT, *a], capture_output=True, text=True, timeout=30)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def freeze(work):
    """Freeze (or reuse) <work>/.code and activate it in this process and its children -> {"sha","dirty","dir"}.
    Already frozen (nested run, or this process runs from a copy): activate nothing new."""
    if os.environ.get(ENV) and os.path.isdir(os.environ[ENV]):
        return _info(os.environ[ENV])
    code = os.path.join(os.path.abspath(work), ".code")
    done = os.path.join(code, "DONE")
    if not os.path.isfile(done):
        shutil.rmtree(code, ignore_errors=True)
        sha = _git("rev-parse", "HEAD") or "nogit"
        dirty = bool(_git("status", "--porcelain", "--", "cli.py", "fluxplace", "plugins"))
        tmp = code + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        ign = shutil.ignore_patterns("__pycache__", "*.pyc")
        for n in _COPY:
            s = os.path.join(_CHECKOUT, n)
            if os.path.isdir(s):
                shutil.copytree(s, os.path.join(tmp, n), ignore=ign)
            elif os.path.isfile(s):
                shutil.copy2(s, os.path.join(tmp, n))
        with open(os.path.join(tmp, "DONE"), "w") as fh:
            fh.write(f"{sha}{' dirty' if dirty else ''}\n")
        os.replace(tmp, code)
    _activate(code)
    return _info(code)


def _info(code):
    try:
        with open(os.path.join(code, "DONE")) as fh:
            s = fh.read().split()
    except OSError:
        s = ["unknown"]
    return {"sha": s[0], "dirty": "dirty" in s[1:], "dir": code}


_PKG_PATH = []


def _activate(code):
    os.environ[ENV] = code
    os.environ["PYTHONPATH"] = code + os.pathsep + os.environ.get("PYTHONPATH", "")
    if code not in sys.path:
        sys.path.insert(0, code)
    pkg = sys.modules.get("fluxplace")
    if pkg is not None:
        _PKG_PATH[:] = _PKG_PATH or list(pkg.__path__)    # lazy `from fluxplace import x` now reads the copy
        pkg.__path__[:] = [os.path.join(code, "fluxplace")]


def release(saved_pythonpath, saved_path):
    """Undo _activate in this process (the copy stays on disk)."""
    os.environ.pop(ENV, None)
    if saved_pythonpath is None:
        os.environ.pop("PYTHONPATH", None)
    else:
        os.environ["PYTHONPATH"] = saved_pythonpath
    pkg = sys.modules.get("fluxplace")
    if pkg is not None:
        pkg.__path__[:] = _PKG_PATH or pkg.__path__
        _PKG_PATH.clear()
    sys.path[:] = saved_path
