"""No-poll routing: a small status file per router run, one-line readers, one blocking wait.

A long router run writes ``<out>.status.json`` every few seconds (nets total /
routed / failed, elapsed, eta, last checkpoint, pid) while its raw log goes to
``<out>.router.log``.  An agent never tails the log:

    fluxplace route-status BOARD_OR_STATUS            -> one line
    fluxplace route-wait   STATUS --timeout 900       -> blocks, one line + result path

The log parser understands the KiCadRoutingTools ``py_router`` format
(``[28/201 (2 failed) (55 ripped)] Routing NET (id=N)``, ``SUCCESS:``,
``FAILED:``, ``RETRY SUCCESS``, ``RETRY FAILED``, ``REROUTE SUCCESS``,
``REROUTE FAILED``, ``[REROUTE 329/357 (28 failed)] Re-routing ripped net X``)
by reusing routediag's net start/end regexes, plus a tolerant Freerouting 2.x
reader (pass / unrouted counters; not verified against a real log).
"""
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time

from . import routediag as RD

STATUS_EVERY = 3.0          # seconds between status writes (at most)
DEFAULT_STALL_S = 600.0

_PROG = re.compile(r"^\[(REROUTE\s+)?(\d+)/(\d+)(?:\s+\((\d+) failed\))?(?:\s+\((\d+) ripped\))?\]\s+"
                   r"(?:Routing|Re-routing ripped net)\s+(\S+)")
_FR_PASS = re.compile(r"pass\s*#?\s*(\d+)", re.I)
_FR_UNR = re.compile(r"(\d+)\s+(?:items?\s+)?(?:unrouted|left to route|remaining)", re.I)
_FR_DONE = re.compile(r"(auto-?rout\w+|routing)\s+(?:was\s+)?(?:completed|finished)", re.I)


class LogParser:
    """Incremental parser of a router log. Feed it lines; read ``snapshot()``."""

    def __init__(self):
        self.total = 0
        self.failed_p = 0           # failed count from the latest progress line
        self.ripped = 0
        self.done_idx = 0
        self.phase = "route"
        self.current = None
        self.net_status = {}        # net -> SUCCESS | FAILED (last attempt wins)
        self._cur = None
        self.fr_pass = None
        self.fr_unrouted = None
        self.fr_done = False
        self.progress_ts = None     # time of the last change in the counters

    def feed(self, line):
        before = (self.done_idx, self.failed_p, len(self.net_status), self.fr_pass, self.fr_unrouted)
        m = _PROG.match(line)
        if m:
            self.phase = "reroute" if m.group(1) else "route"
            self.done_idx, self.total = int(m.group(2)), int(m.group(3))
            self.failed_p = int(m.group(4) or 0)
            if m.group(5):
                self.ripped = int(m.group(5))
            self.current = self._cur = m.group(6)
        else:
            m = RD._START.search(line)
            if m and "Routing MST" not in line:
                self._cur = self.current = m.group(1)
            else:
                m = RD._END.match(line)
                if m and self._cur:
                    self.net_status[self._cur] = m.group(1)
                else:
                    self._freerouting(line)
        if (self.done_idx, self.failed_p, len(self.net_status), self.fr_pass, self.fr_unrouted) != before:
            self.progress_ts = time.time()

    def _freerouting(self, line):
        if self.done_idx or "pass" not in line.lower():
            return                  # KRT progress seen, or not a pass line: not Freerouting
        m = _FR_PASS.search(line)
        if m:
            self.fr_pass = int(m.group(1))
        m = _FR_UNR.search(line)
        if m:
            self.fr_unrouted = int(m.group(1))
        if _FR_DONE.search(line):
            self.fr_done = True

    def snapshot(self):
        ok = sum(1 for s in self.net_status.values() if s == "SUCCESS")
        bad = sum(1 for s in self.net_status.values() if s == "FAILED")
        d = {"nets_total": self.total or None, "nets_routed": ok,
             "nets_failed": max(bad, self.failed_p), "nets_ripped": self.ripped,
             "nets_done": self.done_idx, "phase": self.phase, "current": self.current}
        if self.fr_pass is not None or self.fr_unrouted is not None:
            d["freerouting"] = {"pass": self.fr_pass, "unrouted": self.fr_unrouted}
        return d


def parse_log_file(path):
    """One-shot parse of a finished (or growing) router log."""
    p = LogParser()
    with open(path, errors="replace") as fh:
        for line in fh:
            p.feed(line)
    return p


def _atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def status_path_for(out):
    return out + ".status.json"


def _guess_out(cmd):
    cmd = [str(c) for c in cmd]
    if "--output" in cmd and cmd.index("--output") + 1 < len(cmd):
        return cmd[cmd.index("--output") + 1]
    boards = [c for c in cmd if c.endswith(".kicad_pcb")]
    return boards[-1] if boards else None


def run_logged(cmd, cwd=None, timeout=None, out=None, stage="route", env=None,
               log_path=None, every=STATUS_EVERY, stall_s=DEFAULT_STALL_S,
               status_path=None, extra=None, keep_open=False):
    """Drop-in for ``subprocess.run(cmd, capture_output=True, text=True, timeout=...)``
    that streams the router's output to a log file and keeps a status file current.

    Batch routing (batchroute.py) runs many of these under ONE status file: `status_path`
    names it, `extra` (a dict the caller keeps mutating) is merged over every write, and
    `keep_open` leaves the state "running" (owned by the caller's pid) when the run ends,
    so route-wait does not report "done" after the first batch.

    Returns CompletedProcess(stdout=<log text>); raises TimeoutExpired like run().
    """
    out = out or _guess_out(cmd) or os.path.join(cwd or ".", "router")
    out = os.fspath(out)
    spath = status_path or status_path_for(out)
    lpath = log_path or out + ".router.log"
    parser = LogParser()
    t0 = time.time()
    last_w = 0.0
    state = {"stage": stage, "state": "running", "pid": None, "out": out, "log": lpath,
             "started": t0, "cmd": " ".join(map(str, cmd))[:300]}
    ckpt = {"mtime": None, "n": 0, "path": None}
    off = [0]

    def drain(fh):
        fh.seek(off[0])
        data = fh.read()
        cut = data.rfind(b"\n")          # whole lines only; keep the tail for next time
        if cut < 0:
            return
        off[0] += cut + 1
        for ln in data[: cut + 1].decode(errors="replace").splitlines():
            parser.feed(ln)

    def write(final=None, rc=None):
        nonlocal last_w
        now = time.time()
        snap = parser.snapshot()
        el = now - t0
        eta = None
        done, tot = snap["nets_done"], snap["nets_total"]
        if tot and done and done < tot and parser.phase == "route":
            eta = round(el / done * (tot - done))
        if os.path.exists(out):
            mt = os.path.getmtime(out)
            if mt != ckpt["mtime"]:
                ckpt.update(mtime=mt, n=ckpt["n"] + 1, path=out)
        state.update(snap)
        state.update({"elapsed_s": round(el), "eta_s": eta, "updated": now,
                      "last_progress_ts": parser.progress_ts or t0,
                      "stalled_s": round(now - (parser.progress_ts or t0)),
                      "stall_limit_s": stall_s,
                      "checkpoint": ckpt["path"], "checkpoint_n": ckpt["n"]})
        if extra:
            state.update(extra)
        if final:
            if keep_open:
                state["pid"] = os.getpid()
                final, rc = "running", None
            state["state"] = final
            state["rc"] = rc
        _atomic(spath, state)
        last_w = now

    os.makedirs(os.path.dirname(os.path.abspath(lpath)), exist_ok=True)
    with open(lpath, "w") as lw:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=lw, stderr=subprocess.STDOUT,
                                start_new_session=True)
    state["pid"] = proc.pid
    write()
    try:
        with open(lpath, "rb") as rd:
            while True:
                try:
                    rc = proc.wait(timeout=min(1.0, every))
                    break
                except subprocess.TimeoutExpired:
                    pass
                if timeout and time.time() - t0 > timeout:
                    _kill(proc)
                    drain(rd)
                    write("timeout", None)
                    raise subprocess.TimeoutExpired(cmd, timeout)
                if time.time() - last_w >= every:
                    drain(rd)
                    write()
            drain(rd)
    except BaseException:
        if proc.poll() is None:
            _kill(proc)
        raise
    write("done" if rc == 0 else "failed", rc)
    text = open(lpath, errors="replace").read()
    return subprocess.CompletedProcess(cmd, rc, stdout=text, stderr="")


KILL_WAIT = {"INT": 4.0, "TERM": 5.0}     # seconds to wait after each signal


def _kill(proc):
    """SIGINT (a router that handles Ctrl-C may flush what it has), then SIGTERM, then SIGKILL."""
    try:
        for sig, key in ((signal.SIGINT, "INT"), (signal.SIGTERM, "TERM")):
            os.killpg(proc.pid, sig)
            try:
                proc.wait(timeout=KILL_WAIT[key])
                return
            except subprocess.TimeoutExpired:
                pass
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except (ProcessLookupError, PermissionError):
        pass


# ------------------------------------------------------------------ readers
def resolve(path):
    """Board, router output or status path -> the status file (newest match)."""
    if path.endswith(".status.json") and os.path.exists(path):
        return path
    if os.path.exists(path + ".status.json"):
        return path + ".status.json"
    stem = os.path.splitext(path)[0]
    cands = [c for c in glob.glob(stem + "*.status.json") if os.path.exists(c)]
    return max(cands, key=os.path.getmtime) if cands else None


def load(spath):
    try:
        with open(spath) as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return None
    if st.get("state") == "running" and st.get("pid"):
        try:
            os.kill(st["pid"], 0)
        except ProcessLookupError:
            st["state"] = "dead"            # writer gone without a final write
        except PermissionError:
            pass
    return st


def one_line(st):
    n = st.get("nets_total")
    done = st.get("nets_routed", 0)
    bits = [f"route {st['state']}"]
    if st.get("mode") == "parallel":
        bits.append(f"parallel round {st.get('round', 0)} ({st.get('jobs_running', 0)} jobs, "
                    f"{st.get('workers', '?')} workers)")
    elif st.get("mode") == "batch":
        bits.append(f"batch {st.get('batch', 0)}/{st.get('batches_total', '?')} ({st.get('batch_nets', 0)} nets"
                    f"{', halved' if st.get('halvings') else ''})")
    if n:
        bits.append(f"{'reroute ' if st.get('phase') == 'reroute' else ''}{st.get('nets_done', 0)}/{n} nets ({st.get('nets_failed', 0)} failed"
                    + (f", {st['nets_ripped']} ripped" if st.get("nets_ripped") else "") + ")")
    elif st.get("freerouting"):
        f = st["freerouting"]
        bits.append(f"freerouting pass {f.get('pass')} unrouted {f.get('unrouted')}")
    bits.append(f"{st.get('elapsed_s', 0)}s")
    if st.get("eta_s") is not None and st["state"] == "running":
        bits.append(f"eta {st['eta_s']}s")
    if st["state"] == "running" and st.get("stalled_s", 0) >= 60:
        bits.append(f"no progress {st['stalled_s']}s")
    if st.get("current") and st["state"] == "running":
        bits.append(f"on {st['current']}")
    if st.get("mode") in ("batch", "parallel"):
        bits.append(f"last good {os.path.basename(st.get('last_good') or '-')} "
                    f"({st.get('last_good_open', '?')} open connections in {st.get('last_good_open_nets', '?')} nets, "
                    f"{st.get('leftovers', 0)} leftover"
                    + (f", {st['rule_violations']} RULE_VIOLATION" if st.get("rule_violations") else "") + ")")
        rd = (st.get("rounds") or [None])[-1]
        if rd:
            bits.append(f"last round open {rd.get('open_before')} -> {rd.get('open_after')}")
    elif st.get("checkpoint"):
        bits.append(f"ckpt {st['checkpoint']}")
    if st.get("rc") not in (None, 0):
        bits.append(f"rc={st['rc']}")
    bits.append(f"pid {st.get('pid')}")
    return "  ".join(bits)


def wait(spath, timeout, until="done", stall_s=None, poll=2.0):
    """Block until ``until`` ('done' | 'stall' | 'checkpoint') or the timeout.
    -> (reason, status) where reason is done|failed|stall|checkpoint|timeout|missing."""
    t0 = time.time()
    st = load(spath)
    n0 = (st or {}).get("checkpoint_n", 0)
    while True:
        st = load(spath)
        if st is None:
            if time.time() - t0 > min(timeout, 30):
                return "missing", None
        else:
            s = st["state"]
            if s in ("done",):
                return "done", st
            if s in ("failed", "timeout", "dead"):
                return "failed", st
            lim = stall_s if stall_s is not None else st.get("stall_limit_s", DEFAULT_STALL_S)
            if until == "stall" and st.get("stalled_s", 0) >= lim:
                return "stall", st
            if until == "checkpoint" and st.get("checkpoint_n", 0) > n0:
                return "checkpoint", st
        if time.time() - t0 >= timeout:
            return "timeout", st
        time.sleep(min(poll, max(0.1, timeout - (time.time() - t0))))


def diagnose_result(st, cons=None, spec=None):
    """On finish: write <out>.diagnose.{txt,json} (read-only) and return (path, n_failed)."""
    out = st.get("out")
    if not out or not os.path.exists(out):
        return None, None
    B = RD.load(out)
    failed = RD.unrouted_nets(B)
    res = RD.diagnose(B, failed, cons or None, spec)
    base = out + ".diagnose"
    with open(base + ".json", "w") as fh:
        json.dump(res, fh, indent=1)
    with open(base + ".txt", "w") as fh:
        fh.write(RD.format_text(res))
    return base + ".txt", len(failed)
