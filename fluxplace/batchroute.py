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
from . import routeaudit as RA
from . import routeconn as RC
from . import routedrops as DR
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
         "conn_fill": bool(r.get("conn_fill", True)), "conn_method": str(r.get("conn_method", "auto")),
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


def has_fanout(board):
    """the board carries locked fan-out groups (fluxplace fanout): the escape-first group is unnecessary and the order is by class."""
    try:
        with open(board, errors="replace") as fh:
            return '(group "fanout:' in fh.read()
    except (OSError, TypeError):
        return False


CONTROLLED = {"pair": 0, "rf": 1, "clock": 2}


def order_nets(B, nets, cls=None, first=(), power=(), hint=None, fanout=False):
    """Routing order: fan-out, power, critical classes, rest; inside a group the plan's rank first (`hint`: {net: rank},
    planroute.route_hint: nets through PLAN_OVERFLOW channels = 0, pairs = 1), then ascending estimated length."""
    cls = cls or {}
    first, power = set(first), set(power)
    hint = hint or {}
    pads = NC.net_pads(B)

    def key(n):
        info = cls.get(n) or {}
        if fanout:               # copper to every pin exists: controlled pairs, RF, clocks first, then plan criticality, then length
            try:
                ln = NC.net_length(B, n, pads.get(n))
            except Exception:
                ln = 0.0
            c = CONTROLLED.get(info.get("class"))
            return (0 if c is not None else 1, c if c is not None else 0, hint.get(n, 9), round(ln, 3), n)
        g = 0 if n in first else 1 if (n in power or info.get("class") == "rail") else 2 if info.get("class") in CRITICAL else 3
        try:
            ln = NC.net_length(B, n, pads.get(n))
        except Exception:
            ln = 0.0
        return (g, hint.get(n, 9), round(ln, 3), n)
    return sorted(dict.fromkeys(nets), key=key)


def chunks(seq, size):
    return [list(seq[i:i + size]) for i in range(0, len(seq), size)]


def _sidecar(src, dst):
    a, b = os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(dst)[0] + ".kicad_pro"
    if os.path.exists(a) and not os.path.exists(b):
        shutil.copy(a, b)


def _read_open(path, conn):
    """{net: open connections} of the board at `path`; None when the file is not a usable board."""
    try:
        with open(path, errors="surrogateescape") as fh:
            if not fh.read().rstrip().endswith(")"):
                return None
        return dict(conn(path))
    except Exception:
        return None


def default_conn(S):
    return lambda path: RC.open_by_net(path, fill=S["conn_fill"], method=S["conn_method"])


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
                  leftover_timeout=None, status_path=None, extra=None, log=print, min_slice=5.0,
                  rules=None, conn=None, drc_cli="kicad-cli"):
    """Route the OPEN CONNECTIONS of `nets` onto copies of `board` in batches. `run` as from make_runner.

    The work list is real connectivity (routeconn: pads still not joined by copper/pour), so a net with
    some copper but unjoined pads is still routed.  `rules` (routerules.rules) switches on the post-batch
    audit: a batch whose new copper has a via in a pad, a track/via below the floors, clearance below the
    floor (or, with rules["audit_drc"], a new KiCad DRC error type) is DISCARDED, halved and retried; a
    single net that still violates is a leftover with reason RULE_VIOLATION.

    -> {board: last good board, checkpoints: [paths], leftovers: [nets still open], reasons,
        attempts, halvings, timeouts, violations, start_open, open (connections), open_nets,
        start_open_nets, elapsed}"""
    S = settings(cons, batch_nets=batch_nets, batch_timeout_s=batch_timeout, total_budget_s=total_budget)
    size, tmo = S["batch_nets"], float(S["batch_timeout_s"])
    conn = conn or default_conn(S)
    os.makedirs(workdir, exist_ok=True)
    t0 = time.time()
    budget = S["total_budget_s"]
    deadline = t0 + float(budget) if budget is not None else None
    B = B or RD.load(board)
    open0 = dict(conn(board))
    nets = [n for n in dict.fromkeys(nets)]
    hint = None
    pm = ((cons or {}).get("route") or {}).get("plan_map")
    if pm and os.path.exists(pm):
        from . import planroute as PL
        hint = PL.route_hint(pm)        # order only: the router takes no per-net layer or corridor hints
    fo = has_fanout(board)
    cls = cls or NC.classify(B, cons, spec)
    from . import railroute as RL
    rails = set(RL.rail_nets(B, cons, cls, [n for n in nets if n in open0])[0])    # a rail is never one batch job: parallel mode routes it as a tree
    order = order_nets(B, [n for n in nets if n in open0 and n not in rails], cls, () if fo else first, power, hint, fanout=fo)
    cur, cur_open = board, open0
    cur_B = B
    drc_base = [None]
    extra = extra if extra is not None else {}
    cks, reasons = [], {}
    tally = DR.Tally()
    stats = {"attempts": 0, "halvings": 0, "timeouts": 0, "violations": 0}
    queue = deque(chunks(order, size))
    total = lambda: sum(cur_open.values())
    extra.update({"mode": "batch", "batch": 0, "batches_total": len(queue), "batch_nets": 0, "halvings": 0,
                  "last_good": board, "last_good_open": total(), "last_good_open_nets": len(cur_open),
                  "start_open": total(), "leftovers": 0, "out": board, "rounds": [], "rule_violations": 0, "dropped": {}})

    def push_status(state="running"):
        extra.update(batches_total=extra["batch"] + len(queue), halvings=stats["halvings"],
                     leftovers=len(reasons), last_good=cur, last_good_open=total(),
                     last_good_open_nets=len(cur_open), out=cur, rule_violations=stats["violations"],
                     dropped=tally.totals())
        if cks:
            extra.update(checkpoint=cks[-1], checkpoint_n=len(cks))
        if status_path:
            _status_write(status_path, extra, state)

    def note_round(batch, before, after, result):
        extra["rounds"] = (extra["rounds"] + [{"round": extra["batch"], "nets": len(batch), "open_before": before,
                                               "open_after": after, "result": result}])[-30:]

    def attempt(batch, limited, timeout):
        """one router call -> (accepted, timed_out, why). Never touches `cur` unless it improves and audits clean."""
        nonlocal cur, cur_open, cur_B
        stats["attempts"] += 1
        extra["batch"] += 1
        extra["batch_nets"] = len(batch)
        before = total()
        tmp = os.path.join(workdir, f"_batch-{stats['attempts']:03d}.kicad_pcb")
        if os.path.exists(tmp):
            os.remove(tmp)
        push_status()
        res = run(cur, tmp, batch, timeout, limited)
        if res.timed_out:
            stats["timeouts"] += 1
        un = _read_open(tmp, conn) if os.path.exists(tmp) else None
        ripped = [n for n in (un or {}) if n not in batch and un[n] > cur_open.get(n, 0)] if un else []
        if un is None or sum(un.values()) > before or ripped:   # nothing, garbage, worse, or it ripped other nets open
            if os.path.exists(tmp):
                os.remove(tmp)
            why = "timeout" if res.timed_out else "no output"
            tally.add("rips-other-net" if ripped else "timeout" if res.timed_out else "no-path" if un is None else "other")
            note_round(batch, before, before, why)
            return False, res.timed_out, why
        if rules:
            viol = []
            try:
                viol = RA.audit(cur_B, tmp, rules) if rules.get("audit", True) else []
                if rules.get("audit_drc") and not viol:
                    viol, drc_base[0] = RA.new_drc_types(cur, tmp, drc_cli, drc_base[0])
            except Exception as e:                       # an audit that cannot run must not pass silently
                viol = [{"code": "AUDIT_ERROR", "net": "", "msg": str(e)[:120], "x": 0, "y": 0}]
            if viol:
                stats["violations"] += 1
                tally.add(DR.reason_of(viol))
                os.remove(tmp)
                log(f"    batch {extra['batch']}: RULE_VIOLATION, discarded: {RA.summarize(viol)}")
                note_round(batch, before, before, "RULE_VIOLATION " + RA.summarize(viol, 0))
                return False, res.timed_out, "RULE_VIOLATION"
        ck = os.path.join(workdir, f"ck-{len(cks) + 1:03d}.kicad_pcb")
        os.replace(tmp, ck)
        _sidecar(board, ck)
        cks.append(ck)
        cur, cur_open = ck, un
        cur_B = RD.load(ck)
        drc_base[0] = None
        log(f"    batch {extra['batch']}: {len(batch)} net(s), open connections {before} -> {total()} "
            f"({len(cur_open)} net(s) open) -> {os.path.basename(ck)}"
            + (" (timed out, partial output kept)" if res.timed_out else ""))
        note_round(batch, before, total(), "timeout, partial kept" if res.timed_out else "ok")
        return True, res.timed_out, "ok"

    while queue:
        batch = [n for n in queue.popleft() if n in cur_open]    # earlier batches may have connected some
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
        ok, timed, why = attempt(batch, True, timeout)
        if ok and not timed:
            for n in batch:
                if n in cur_open:
                    reasons[n] = "failed"
        elif ok:                                               # partial output: retry the rest smaller
            rest = [n for n in batch if n in cur_open]
            _requeue(queue, rest, stats, reasons, "timeout")
        else:
            _requeue(queue, batch, stats, reasons, why)
        push_status()

    # one leftovers pass, full retry phase on (nets that violated the rules are not retried)
    left = [n for n in order if n in cur_open and n in reasons and reasons[n] != "RULE_VIOLATION"]
    left += [n for n in order if n in cur_open and n not in reasons]
    left = list(dict.fromkeys(left))
    if left:
        remaining = (deadline - time.time()) if deadline else None
        lt = float(leftover_timeout or 2 * tmo)
        if remaining is None or remaining >= min_slice:
            log(f"    leftovers pass: {len(left)} net(s), router retry phase on")
            extra["batch_nets"] = len(left)
            ok, timed, why = attempt(left, False, lt if remaining is None else min(lt, remaining))
            for n in left:
                if n not in cur_open:
                    reasons.pop(n, None)
                elif ok and not timed:
                    reasons[n] = "failed"
                elif timed:
                    reasons[n] = "timeout"
                elif why == "RULE_VIOLATION":
                    reasons[n] = "RULE_VIOLATION"
        else:
            for n in left:
                reasons.setdefault(n, "budget")
    rl = sorted(n for n in rails if n in cur_open)
    out = finish(order + rl, cur, cur_open, cks, dict(reasons, **{n: 'rail: set [route] mode = "parallel" (tree routing)' for n in rl}), stats, open0, t0)
    out["dropped"] = tally.totals()
    push_status("done")
    write_leftovers(workdir, out)
    return out


def finish(order, cur, cur_open, cks, reasons, stats, open0, t0):
    """The result dict shared by the serial and the parallel driver."""
    final_left = [n for n in order if n in cur_open]
    reasons = {n: reasons.get(n, "failed") for n in final_left}
    return {"board": cur, "checkpoints": cks, "leftovers": final_left, "reasons": reasons,
            "attempts": stats["attempts"], "halvings": stats["halvings"], "timeouts": stats["timeouts"],
            "violations": stats["violations"], "start_open": sum(open0.values()), "start_open_nets": len(open0),
            "open": sum(cur_open.values()), "open_nets": len(cur_open), "elapsed": round(time.time() - t0)}


def write_leftovers(workdir, r):
    try:
        import json
        with open(os.path.join(workdir, "leftovers.json"), "w") as fh:
            json.dump({"board": r["board"], "checkpoints": r["checkpoints"], "leftovers": r["leftovers"],
                       "reasons": r["reasons"], "open_connections": r["open"], "open_nets": r["open_nets"]},
                      fh, indent=1)
    except OSError:
        pass


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
    return (f"batch routing: {r['start_open']} -> {r['open']} open connection(s) ({r['start_open_nets']} -> "
            f"{r['open_nets']} nets) in {r['elapsed']}s, "
            f"{r['attempts']} router call(s), {len(r['checkpoints'])} checkpoint(s)"
            + (f", {r['halvings']} halving(s)" if r["halvings"] else "")
            + (f", {r['timeouts']} timeout(s)" if r["timeouts"] else "")
            + (f", {r['violations']} RULE_VIOLATION batch(es) discarded" if r.get("violations") else "")
            + f"; last good {os.path.basename(r['board'])}"
            + (f"; leftovers ({len(r['leftovers'])}): {', '.join(r['leftovers'][:12])}"
               f"{' ...' if len(r['leftovers']) > 12 else ''}" if r["leftovers"] else ""))
