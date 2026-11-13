"""Locate the external router (KiCadRoutingTools): explicit flag, env var, then the newest install.

Never falls back silently: when nothing works the error names every path tried."""
import os
import subprocess

ENV_DIR, ENV_PY = "FLUXPLACE_ROUTER_DIR", "FLUXPLACE_ROUTER_PY"
CANDIDATES = ("~/tools/KiCadRoutingTools-latest", "~/tools/KiCadRoutingTools")
PY_CANDIDATES = ("~/tools/router-venv/bin/python",)


def _ok(d):
    return bool(d) and os.path.exists(os.path.join(d, "py_router", "route.py"))


def version(d):
    """(version, commit) of a router install; '?' where unknown."""
    v = c = "?"
    try:
        v = open(os.path.join(d, "VERSION")).read().strip() or "?"
    except OSError:
        pass
    try:
        c = subprocess.run(["git", "-C", d, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                           timeout=10).stdout.strip() or "?"
    except (OSError, subprocess.SubprocessError):
        pass
    return v, c


def resolve(flag_dir=None, flag_py=None, env=None):
    """-> {"dir","py","source","version","commit","tried"}; raises RuntimeError naming the paths tried."""
    env = os.environ if env is None else env
    tried = []
    src = d = None
    for s, p in (("flag", flag_dir), ("env " + ENV_DIR, env.get(ENV_DIR))):
        if p:
            p = os.path.abspath(os.path.expanduser(p))
            tried.append(p)
            if _ok(p):
                src, d = s, p
            break                                   # an explicit choice is never second-guessed
    if not d and not (flag_dir or env.get(ENV_DIR)):
        for p in CANDIDATES:
            p = os.path.expanduser(p)
            tried.append(p)
            if _ok(p):
                src, d = "newest install", p
                break
    if not d:
        raise RuntimeError("router not found; tried " + ", ".join(tried) +
                           f" (need py_router/route.py). Pass --router-dir or set {ENV_DIR}")
    ptried = []
    py = None
    for p in (flag_py, env.get(ENV_PY), *PY_CANDIDATES):
        if p:
            p = os.path.abspath(os.path.expanduser(p))
            ptried.append(p)
            if os.path.exists(p):
                py = p
                break
            if flag_py or env.get(ENV_PY):
                break
    if not py:
        raise RuntimeError("router python not found; tried " + ", ".join(ptried) + f". Pass --router-py or set {ENV_PY}")
    v, c = version(d)
    return {"dir": d, "py": py, "source": src, "version": v, "commit": c, "tried": tried}
