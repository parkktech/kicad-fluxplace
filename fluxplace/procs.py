"""Child processes that never outlive their caller.

Every child starts in its own session (a process group of its own), so a timeout, Ctrl-C, an exception, SIGTERM/SIGHUP
on the parent, or normal interpreter exit signals the WHOLE group (router, kicad-cli, rung jobs and their grandchildren),
SIGTERM first, SIGKILL after GRACE_S.

  run(cmd, timeout=..., **kw)  drop-in for subprocess.run (same CompletedProcess / TimeoutExpired / CalledProcessError)
  popen(cmd, **kw)             subprocess.Popen in a new session, registered for reap_all()
  forget(p)                    a popen() child the caller has finished with
  reap_all()                   kill every registered group
  install()                    atexit reap_all + SIGTERM/SIGHUP -> reap, exit 128+sig; SIGINT -> reap, KeyboardInterrupt

A group kill does not cross sessions, so a fluxplace child (rung job, candidate worker, nested cli) must reap its OWN
children when its parent signals it: popen() installs the handlers in every process that uses it (install() is implied),
and a signalled process uses SIGNAL_GRACE_S < GRACE_S so it is done before its parent's SIGKILL.
A disposition somebody else set is kept (nohup's ignored SIGHUP, tune.Budget's handlers).
"""
import atexit
import os
import signal
import subprocess
import sys
import threading
import time

GRACE_S = 5.0
SIGNAL_GRACE_S = 2.0            # a signalled process reaps its sessions before the sender's GRACE_S runs out
_LIVE = {}                      # pgid -> Popen
_LOCK = threading.RLock()       # re-entrant: the signal handler runs in the main thread, maybe inside a locked block
_atexit = False
_signals = False
_closing = False                # SIGTERM/SIGHUP seen: no new children
_handling = False


class Closing(RuntimeError):
    """popen() after SIGTERM/SIGHUP: the process is going down."""


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _recycled(p):
    """The leader was reaped and its pid exists again: another process now (the pid of a group that still has members
    cannot be reused, so the group is gone and the number names a stranger)."""
    return p.poll() is not None and os.path.exists(f"/proc/{p.pid}")


def kill_group(pgid, grace=None, proc=None):
    """SIGTERM the group, wait up to `grace` s for it to empty, then SIGKILL. True if anything was signalled.
    `proc` (the leader's Popen) is polled while waiting: an unreaped leader is a zombie that keeps the group "alive"."""
    grace = GRACE_S if grace is None else grace
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    end = time.time() + grace
    while time.time() < end:
        if proc is not None:
            proc.poll()
        if not _group_alive(pgid):
            return True
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return True


def _reap_proc(p, grace=None):
    if not _recycled(p):
        kill_group(p.pid, grace, p)
    try:
        p.wait(timeout=2)
    except Exception:           # noqa: BLE001
        pass
    forget(p)


def forget(p):
    with _LOCK:
        if _LIVE.get(p.pid) is p:
            del _LIVE[p.pid]


def _prune():
    """Drop finished children whose group is empty (a popen() child nobody forgot; its pid may be reused later)."""
    with _LOCK:
        for pid, p in list(_LIVE.items()):
            if p.poll() is not None and not _group_alive(pid):
                del _LIVE[pid]


def popen(cmd, **kw):
    if _closing:
        raise Closing("fluxplace is shutting down: no new child process")
    install()
    _prune()
    kw["start_new_session"] = True
    p = subprocess.Popen(cmd, **kw)
    with _LOCK:
        _LIVE[p.pid] = p
    return p


def run(cmd, *, input=None, capture_output=False, timeout=None, check=False, **kw):
    """subprocess.run in its own process group; the whole group is reaped on timeout / interrupt / error."""
    if capture_output:
        if kw.get("stdout") is not None or kw.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output.")
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.PIPE
    if input is not None:
        if kw.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used.")
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
        forget(p)


def reap_all(grace=None):
    """Kill every registered group (also groups whose leader already exited but left grandchildren)."""
    with _LOCK:
        procs = list(_LIVE.values())
    live = [p for p in procs if not _recycled(p)]
    for p in live:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    end = time.time() + (GRACE_S if grace is None else grace)
    while time.time() < end:
        for p in live:
            p.poll()                # reap the leader: a zombie keeps its group "alive"
        if not any(_group_alive(p.pid) for p in live):
            break
        time.sleep(0.05)
    for p in live:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.wait(timeout=0.5)
        except Exception:           # noqa: BLE001
            pass
    for p in procs:                 # only now: a second handler run in between still sees them
        forget(p)


def _on_signal(signum, _frame):
    global _closing, _handling
    if _handling:                   # a second signal must not cut the first reap short (its SIGKILL pass)
        return
    _handling = True
    try:
        if signum != signal.SIGINT:
            _closing = True
        mp = sys.modules.get("multiprocessing")
        if mp is not None:          # pool workers are not procs children; each reaps its own on SIGTERM
            try:
                for c in mp.active_children():
                    c.terminate()
            except Exception:       # noqa: BLE001
                pass
        reap_all(SIGNAL_GRACE_S)
    finally:
        _handling = False
    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    raise SystemExit(128 + signum)


def _after_fork():
    """A forked worker owns none of its parent's children (its inherited handler must not kill them)."""
    global _LOCK, _closing, _handling
    _LIVE.clear()
    _LOCK = threading.RLock()
    _closing = _handling = False


os.register_at_fork(after_in_child=_after_fork)


def install():
    """Idempotent. atexit reap_all; SIGTERM/SIGHUP reap then exit 128+sig; SIGINT reap then KeyboardInterrupt.
    Only default dispositions are replaced (main thread only; a later call from the main thread completes it)."""
    global _atexit, _signals
    if not _atexit:
        _atexit = True
        atexit.register(reap_all)
    if _signals or threading.current_thread() is not threading.main_thread():
        return
    _signals = True
    for s, default in ((signal.SIGTERM, signal.SIG_DFL), (signal.SIGHUP, signal.SIG_DFL),
                       (signal.SIGINT, signal.default_int_handler)):
        if signal.getsignal(s) is default:
            signal.signal(s, _on_signal)
