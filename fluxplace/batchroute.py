"""Batch routing with checkpoints: never time-box a whole-board router call.

The external router (KiCadRoutingTools ``py_router``) writes its output only when a whole
pass INCLUDING its retry / rip-up phase has finished, so a time-boxed kill of a whole-board
call loses everything.  This module routes the signal nets in batches, one router call per
batch, each time-boxed, and saves a checkpoint board after every batch that produced output:

    W/ck-001.kicad_pcb, W/ck-002.kicad_pcb, ...      (the last one is the "last good" board)

    [route] batch_nets = 35          nets per router call
    [route] batch_timeout_s = 240    wall clock per call (SIGINT, +4 s SIGTERM, +5 s SIGKILL)
    [route] batch_max_ripup = 3      --max-ripup for the batches
    [route] total_budget_s           optional cap on the whole run (default: none)
    [route] batch_env                env for the batches (default below), "{}" to keep the router's own

Order: fan-out (fine-pitch escape) nets, power nets, critical classes (rf, pair, switch node,
clock, bypass), the rest; inside each group ascending estimated length (local nets first).

A batch that times out or writes nothing is halved (down to single nets) and retried; the
earlier checkpoints are never touched.  A net that still fails goes to the leftovers list; after
the last batch ONE leftovers pass runs with the router's full retry phase enabled.  The result is
always the last good checkpoint (or the input board when nothing improved) plus the exact list of
requested nets that are still unrouted in it.
"""
import os
import shutil
import subprocess
import sys
import time
from collections import deque

from . import netclass as NC
from . import routediag as RD
from . import routestatus as RS

DEFAULT_BATCH_NETS = 35
DEFAULT_BATCH_TIMEOUT_S = 240

# The router's end-of-pass retry phase is what makes a pass long: net rescue (whole-net retries
# with rip-up), terminal geometry escalation (re-routes failed nets at shrinking width/via size)
# and dynamic A* iteration tranches.  The batches run with those limited; the leftovers pass
# runs with the router's defaults.  (KiCadRoutingTools py_router/env_knobs.py.)
BATCH_ENV = {"KICAD_NET_RESCUE": "0", "KICAD_TERMINAL_ESCALATION": "0",
             "KICAD_DYNAMIC_ITERATIONS_CLAMP": "200000"}
CRITICAL = ("rf", "pair", "switch_node", "clock", "bypass")


def settings(cons, **over):
    """[route] batch_* keys with defaults; `over` wins when not None."""
    r = dict((cons or {}).get("route") or {})
    s = {"batch_nets": int(r.get("batch_nets", DEFAULT_BATCH_NETS)),
         "batch_timeout_s": float(r.get("batch_timeout_s", DEFAULT_BATCH_TIMEOUT_S)),
         "max_ripup": int(r.get("batch_max_ripup", 3)),
         "total_budget_s": r.get("total_budget_s"),
         "env": dict(BATCH_ENV, **{k: str(v) for k, v in (r.get("batch_env") or {}).items()})
         if "batch_env" not in r or r["batch_env"] else {}}
    for k, v in over.items():
        if v is not None:
            s[k] = v
    s["batch_nets"] = max(1, int(s["batch_nets"]))
    return s


def router_env(limited, env=None):
    e = dict(os.environ)
    if limited:
        e.update(env if env is not None else BATCH_ENV)
    return e


def order_nets(B, nets, cls=None, first=(), power=()):
    """Routing order: fan-out, power, critical classes, rest; ascending estimated length inside a group."""
    cls = cls or {}
    first, power = set(first), set(power)
    pads = NC.net_pads(B)

    def key(n):
        info = cls.get(n) or {}
        g = 0 if n in first else 1 if (n in power or info.get("class") == "rail") else 2 if info.get("class") in CRITICAL else 3
        try:
            ln = NC.net_length(B, n, pads.get(n))
        except Exception:
            ln = 0.0
        return (g, round(ln, 3), n)
    return sorted(dict.fromkeys(nets), key=key)


def chunks(seq, size):
    return [list(seq[i:i + size]) for i in range(0, len(seq), size)]


def _sidecar(src, dst):
    a, b = os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(dst)[0] + ".kicad_pro"
    if os.path.exists(a) and not os.path.exists(b):
        shutil.copy(a, b)


def _read_unrouted(path):
    """Nets without copper on the board at `path`; None when the file is not a usable board."""
    try:
        with open(path, errors="surrogateescape") as fh:
            if not fh.read().rstrip().endswith(")"):
                return None
        return set(RD.unrouted_nets(RD.load(path)))
    except Exception:
        return None


class Result:
    def __init__(self, timed_out=False, rc=0):
        self.timed_out, self.rc = timed_out, rc


def make_runner(build_cmd, cwd, extra, status_path, settings_=None, env_limited=None):
    """run(src, dst, only, timeout, limited) -> Result, through run_logged under ONE status file.
    build_cmd(src, dst, only, limited) -> argv."""
    st = settings_ or settings(None)

    def run(src, dst, only, timeout, limited=True):
        cmd = build_cmd(src, dst, only, limited)
        try:
            r = RS.run_logged(cmd, cwd=cwd, timeout=timeout, out=dst, stage="route",
                              env=router_env(limited, st["env"]), status_path=status_path,
                              extra=extra, keep_open=True)
            return Result(False, r.returncode)
        except subprocess.TimeoutExpired:
            return Result(True, None)
    return run


def route_batches(board, workdir, nets, run, *, cons=None, spec=None, B=None, cls=None, first=(),
                  power=(), batch_nets=None, batch_timeout=None, total_budget=None,
                  leftover_timeout=None, status_path=None, extra=None, log=print, min_slice=5.0):
    """Route `nets` onto copies of `board` in batches. `run` as from make_runner.

    -> {board: last good board, checkpoints: [paths], leftovers: [nets still unrouted], reasons,
        attempts, halvings, timeouts, start_unrouted, unrouted, elapsed}"""
    S = settings(cons, batch_nets=batch_nets, batch_timeout_s=batch_timeout, total_budget_s=total_budget)
    size, tmo = S["batch_nets"], float(S["batch_timeout_s"])
    os.makedirs(workdir, exist_ok=True)
    t0 = time.time()
    budget = S["total_budget_s"]
    deadline = t0 + float(budget) if budget is not None else None
    B = B or RD.load(board)
    todo0 = set(RD.unrouted_nets(B))
    nets = [n for n in dict.fromkeys(nets)]
    order = order_nets(B, nets, cls or NC.classify(B, cons, spec), first, power)
    cur, cur_un = board, todo0
    extra = extra if extra is not None else {}
    cks, reasons = [], {}
    stats = {"attempts": 0, "halvings": 0, "timeouts": 0}
    queue = deque(chunks(order, size))
    extra.update({"mode": "batch", "batch": 0, "batches_total": len(queue), "batch_nets": 0, "halvings": 0,
                  "last_good": board, "last_good_unrouted": len(cur_un), "leftovers": 0, "out": board})

    def push_status(state="running"):
        extra.update(batches_total=extra["batch"] + len(queue), halvings=stats["halvings"],
                     leftovers=len(reasons), last_good=cur, last_good_unrouted=len(cur_un), out=cur)
        if cks:
            extra.update(checkpoint=cks[-1], checkpoint_n=len(cks))
        if status_path:
            _status_write(status_path, extra, state)

    def attempt(batch, limited, timeout):
        """one router call -> (accepted, timed_out). Never touches `cur` unless it improves."""
        nonlocal cur, cur_un
        stats["attempts"] += 1
        extra["batch"] += 1
        extra["batch_nets"] = len(batch)
        tmp = os.path.join(workdir, f"_batch-{stats['attempts']:03d}.kicad_pcb")
        if os.path.exists(tmp):
            os.remove(tmp)
        push_status()
        res = run(cur, tmp, batch, timeout, limited)
        if res.timed_out:
            stats["timeouts"] += 1
        un = _read_unrouted(tmp) if os.path.exists(tmp) else None
        if un is None or len(un) > len(cur_un):          # nothing, garbage, or worse than we had
            if os.path.exists(tmp):
                os.remove(tmp)
            return False, res.timed_out
        ck = os.path.join(workdir, f"ck-{len(cks) + 1:03d}.kicad_pcb")
        os.replace(tmp, ck)
        _sidecar(board, ck)
        cks.append(ck)
        cur, cur_un = ck, un
        log(f"    batch {extra['batch']}: {len(batch)} net(s), {len(batch) - len([n for n in batch if n in un])} "
            f"routed, {len(un)} unrouted board-wide -> {os.path.basename(ck)}"
            + (" (timed out, partial output kept)" if res.timed_out else ""))
        return True, res.timed_out

    while queue:
        batch = [n for n in queue.popleft() if n in cur_un]      # earlier batches may have connected some
        if not batch:
            continue
        remaining = (deadline - time.time()) if deadline else None
        if remaining is not None and remaining < min_slice:
            for n in batch:
                reasons[n] = "budget"
            for b in queue:
                for n in b:
                    reasons[n] = "budget"
            queue.clear()
            break
        timeout = tmo if remaining is None else min(tmo, remaining)
        ok, timed = attempt(batch, True, timeout)
        if ok and not timed:
            for n in batch:
                if n in cur_un:
                    reasons[n] = "failed"
        elif ok:                                               # partial output: retry the rest smaller
            rest = [n for n in batch if n in cur_un]
            _requeue(queue, rest, stats, reasons, "timeout")
        else:
            _requeue(queue, batch, stats, reasons, "timeout" if timed else "no output")
        push_status()

    # one leftovers pass, full retry phase on
    left = [n for n in order if n in cur_un and n in reasons]
    left += [n for n in order if n in cur_un and n not in reasons]
    left = list(dict.fromkeys(left))
    if left:
        remaining = (deadline - time.time()) if deadline else None
        lt = float(leftover_timeout or 2 * tmo)
        if remaining is None or remaining >= min_slice:
            log(f"    leftovers pass: {len(left)} net(s), router retry phase on")
            extra["batch_nets"] = len(left)
            ok, timed = attempt(left, False, lt if remaining is None else min(lt, remaining))
            for n in left:
                if n not in cur_un:
                    reasons.pop(n, None)
                elif ok and not timed:
                    reasons[n] = "failed"
                elif timed:
                    reasons[n] = "timeout"
        else:
            for n in left:
                reasons.setdefault(n, "budget")
    final_left = [n for n in order if n in cur_un]
    for n in list(reasons):
        if n not in cur_un:
            del reasons[n]
    push_status("done")
    try:
        import json
        with open(os.path.join(workdir, "leftovers.json"), "w") as fh:
            json.dump({"board": cur, "checkpoints": cks, "leftovers": final_left,
                       "reasons": {n: reasons.get(n, "failed") for n in final_left}}, fh, indent=1)
    except OSError:
        pass
    return {"board": cur, "checkpoints": cks, "leftovers": final_left,
            "reasons": {n: reasons.get(n, "failed") for n in final_left}, "attempts": stats["attempts"],
            "halvings": stats["halvings"], "timeouts": stats["timeouts"], "start_unrouted": len(todo0),
            "unrouted": len(cur_un), "elapsed": round(time.time() - t0)}


def _requeue(queue, nets, stats, reasons, why):
    """Halve a batch that produced nothing usable; a single net goes to the leftovers."""
    if len(nets) <= 1:
        for n in nets:
            reasons[n] = why
        return
    stats["halvings"] += 1
    h = (len(nets) + 1) // 2
    queue.appendleft(nets[h:])
    queue.appendleft(nets[:h])


def _status_write(path, extra, state):
    """Between router calls: merge `extra` into the status file (driver pid owns it)."""
    import json
    try:
        with open(path) as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {"stage": "route", "started": time.time()}
    st.update(extra)
    st.update({"state": state, "pid": os.getpid(), "rc": None, "updated": time.time()})
    RS._atomic(path, st)


def format_result(r):
    return (f"batch routing: {r['start_unrouted']} -> {r['unrouted']} unrouted in {r['elapsed']}s, "
            f"{r['attempts']} router call(s), {len(r['checkpoints'])} checkpoint(s)"
            + (f", {r['halvings']} halving(s)" if r["halvings"] else "")
            + (f", {r['timeouts']} timeout(s)" if r["timeouts"] else "")
            + f"; last good {os.path.basename(r['board'])}"
            + (f"; leftovers ({len(r['leftovers'])}): {', '.join(r['leftovers'][:12])}"
               f"{' ...' if len(r['leftovers']) > 12 else ''}" if r["leftovers"] else ""))
