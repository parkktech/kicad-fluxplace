"""Child processes that never outlive their caller.

Every child starts in its own session (a process group of its own), so a timeout, Ctrl-C, an exception, SIGTERM/SIGHUP
on the parent, or normal interpreter exit signals the WHOLE group (router, kicad-cli, rung jobs and their grandchildren),
SIGTERM first, SIGKILL after GRACE_S.

  run(cmd, timeout=..., **kw)  drop-in for subprocess.run (same CompletedProcess / TimeoutExpired / CalledProcessError)
  popen(cmd, **kw)             subprocess.Popen in a new session, registered for reap_all()
  reap_all()                   kill every registered group
  install()                    once at CLI entry: atexit reap_all + SIGTERM/SIGHUP -> reap, exit 128+sig
"""
import atexit
import os
import signal
import subprocess
import threading
import time

GRACE_S = 5.0
_LIVE = {}                      # pgid -> Popen
_LOCK = threading.Lock()
_installed = False


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_group(pgid, grace=None):
    """SIGTERM the group, wait up to `grace` s for it to empty, then SIGKILL. True if anything was signalled."""
    grace = GRACE_S if grace is None else grace
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    end = time.time() + grace
    while time.time() < end and _group_alive(pgid):
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return True


def _reap_proc(p, grace=None):
    kill_group(p.pid, grace)
    try:
        p.wait(timeout=2)
    except Exception:           # noqa: BLE001
        pass
    with _LOCK:
        _LIVE.pop(p.pid, None)


def popen(cmd, **kw):
    kw["start_new_session"] = True
    p = subprocess.Popen(cmd, **kw)
    with _LOCK:
        _LIVE[p.pid] = p
    return p


def run(cmd, *, input=None, capture_output=False, timeout=None, check=False, **kw):
    """subprocess.run in its own process group; the whole group is reaped on timeout / interrupt / error."""
    if capture_output:
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.PIPE
    if input is not None:
        kw["stdin"] = subprocess.PIPE
    p = popen(cmd, **kw)
    try:
        try:
            out, err = p.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            _reap_proc(p)
            try:
                p.communicate(timeout=2)
            except Exception:       # noqa: BLE001
                pass
            raise subprocess.TimeoutExpired(p.args, timeout, output=e.output, stderr=e.stderr) from None
        except BaseException:
            _reap_proc(p)
            raise
        rc = p.poll()
        if check and rc:
            raise subprocess.CalledProcessError(rc, p.args, output=out, stderr=err)
        return subprocess.CompletedProcess(p.args, rc, out, err)
    finally:
        with _LOCK:
            _LIVE.pop(p.pid, None)


def reap_all(grace=None):
    """Kill every registered group (also groups whose leader already exited but left grandchildren)."""
    with _LOCK:
        procs = list(_LIVE.values())
        _LIVE.clear()
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    end = time.time() + (GRACE_S if grace is None else grace)
    while time.time() < end and any(_group_alive(p.pid) for p in procs):
        time.sleep(0.05)
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.wait(timeout=0.5)
        except Exception:           # noqa: BLE001
            pass


def _on_signal(signum, _frame):
    reap_all()
    raise SystemExit(128 + signum)


def install():
    """Idempotent. atexit reap_all; SIGTERM/SIGHUP reap then exit 128+sig."""
    global _installed
    if _installed:
        return
    _installed = True
    atexit.register(reap_all)
    if threading.current_thread() is threading.main_thread():
        for s in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(s, _on_signal)
