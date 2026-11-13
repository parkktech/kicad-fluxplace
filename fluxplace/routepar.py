"""Parallel per-net routing: `[route] mode = "parallel"`.

A serial batch of 3-15 nets pays the router's own startup + pour refill (~70 s) per call and times out.
What works is many SINGLE-net router jobs at once, each on its own copy of the same checkpoint, then a
merge:

  round:  open connections (routeconn, zones filled) -> work list in routing order -> up to `workers`
          single-net jobs run in parallel (each on a copy of the checkpoint, time-boxed)
  per job: new copper = tracks/vias the job output has and the checkpoint lacks.  A job that REMOVED copper
          of another net ripped something: not mergeable, queued for the final serial pass.  A job whose new
          copper violates the rules (routeaudit: via in pad, width/clearance floors) is RULE_VIOLATION.
  merge:  jobs in routing order; the first of any two jobs whose new copper of different nets comes within
          clearance of each other wins, the later one is dropped and re-queued for the next round.  The
          accepted new tracks/vias are imported into the checkpoint as text (vias snapped to the end of the
          same net's new track), the merged board is audited again and its open connections measured; it
          becomes the next checkpoint only if it audits clean and has no more open connections.
  repeat until a round merges nothing / reduces no open connection; then ONE serial pass (batchroute) over the
  nets whose jobs ripped copper.

    [route] mode = "parallel"
    [route] workers = 8            # default CPU count / 2, max 16
    [route] job_timeout_s = 240    # per single-net job (default batch_timeout_s)
    [route] round_jobs = 8         # jobs per round (default = workers)
"""
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from . import batchroute as BR
from . import netclass as NC
from . import routeaudit as RA
from . import routedrops as DR
from . import routediag as RD
from . import routestatus as RS

_START = re.compile(r"^[ \t]?\((segment|arc|via)[ \t\n]", re.M)
_NUM = r"(-?\d+(?:\.\d+)?)"


def blocks(text):
    """{raw key: block text} for the top-level (segment|arc|via ...) forms of a board file."""
    out = {}
    for m in _START.finditer(text):
        i = m.start() + (m.group(0).index("("))
        depth, j, instr = 0, i, False
        while j < len(text):
            c = text[j]
            if instr:
                if c == "\\":
                    j += 1
                elif c == '"':
                    instr = False
            elif c == '"':
                instr = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        blk = text[i:j + 1]
        out[_key(m.group(1), blk)] = blk
    return out


def _num2(blk, tag):
    m = re.search(r"\(%s %s %s\)" % (tag, _NUM, _NUM), blk)
    return (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)


def _key(kind, blk):
    net = re.search(r'\(net ("[^"]*"|\d+)\)', blk)
    net = net.group(1) if net else ""
    if kind == "via":
        x, y = _num2(blk, "at")
        sz = re.search(r"\(size %s\)" % _NUM, blk)
        return ("v", round(x, 4), round(y, 4), sz.group(1) if sz else "", net)
    a, b = sorted((tuple(round(v, 4) for v in _num2(blk, "start")), tuple(round(v, 4) for v in _num2(blk, "end"))))
    w = re.search(r"\(width %s\)" % _NUM, blk)
    ly = re.search(r'\(layer "([^"]*)"\)', blk)
    return ("s", a, b, w.group(1) if w else "", ly.group(1) if ly else "", net)


def snap_vias(new):
    """new = {key: block}; each new via moves onto the end of a new track of the same net within 0.02 mm."""
    ends = {}
    for k, b in new.items():
        if k[0] == "s":
            ends.setdefault(k[-1], []).extend([k[1], k[2]])
    out = {}
    for k, b in new.items():
        if k[0] == "v":
            best = min(ends.get(k[-1], []), key=lambda e: (e[0] - k[1]) ** 2 + (e[1] - k[2]) ** 2, default=None)
            if best and 0 < (best[0] - k[1]) ** 2 + (best[1] - k[2]) ** 2 <= 0.02 ** 2:
                b = re.sub(r"\(at %s %s\)" % (_NUM, _NUM), "(at %g %g)" % best, b, count=1)
        out[k] = b
    return out


def _bbox(segs, vias, pad):
    xs, ys = [], []
    for s in segs:
        xs += [s["a"][0], s["b"][0]]
        ys += [s["a"][1], s["b"][1]]
    for v in vias:
        xs.append(v["x"])
        ys.append(v["y"])
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad) if xs else None


def conflicts(ca, cb, gap, cu):
    """True when new copper `ca` and `cb` ((segs, vias) of two jobs) come within `gap` mm of each other."""
    ba, bb = _bbox(*ca, gap + 1.0), _bbox(*cb, gap + 1.0)
    if not ba or not bb or ba[2] < bb[0] or bb[2] < ba[0] or ba[3] < bb[1] or bb[3] < ba[1]:
        return False
    la = [("s", s) for s in ca[0]] + [("v", v) for v in ca[1]]
    lb = [("s", s) for s in cb[0]] + [("v", v) for v in cb[1]]
    for ka, a in la:
        for kb, b in lb:
            if a["net"] == b["net"]:
                continue
            g = RA._gap(ka, a, kb, b, cu)
            if g is not None and g < gap - RA.EPS:
                return True
    return False


def run_job(cmd, cwd, out, timeout, env):
    """-> 'ok' | 'timeout' | 'failed'.  Own status and log files: safe in threads."""
    try:
        r = RS.run_logged(cmd, cwd=cwd, timeout=timeout, out=out, stage="route-job", env=env,
                          status_path=out + ".status.json", log_path=out + ".router.log")
        return "ok" if r.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        return "timeout"


def route_parallel(board, workdir, nets, build_cmd, serial_run, *, cons=None, spec=None, B=None, cls=None,
                   first=(), power=(), rules=None, workers=None, job_timeout=None, round_jobs=None,
                   total_budget=None, status_path=None, extra=None, log=print, conn=None,
                   drc_cli="kicad-cli", cwd=None, min_slice=5.0, keep_jobs=False):
    """Parallel per-net routing with merge rounds (see module doc).  `build_cmd(src, dst, only, limited)` as for
    batchroute.make_runner; `serial_run` = batchroute runner for the final serial pass."""
    S = BR.settings(cons, total_budget_s=total_budget)
    r = dict((cons or {}).get("route") or {})
    workers = max(1, int(workers or (rules or {}).get("workers") or max(1, (os.cpu_count() or 2) // 2)))
    jt = float(job_timeout or r.get("job_timeout_s") or S["batch_timeout_s"])
    rj = max(1, int(round_jobs or r.get("round_jobs") or workers))
    conn = conn or BR.default_conn(S)
    os.makedirs(workdir, exist_ok=True)
    t0 = time.time()
    deadline = t0 + float(S["total_budget_s"]) if S["total_budget_s"] is not None else None
    B = B or RD.load(board)
    open0 = dict(conn(board))
    order = BR.order_nets(B, [n for n in dict.fromkeys(nets) if n in open0], cls or NC.classify(B, cons, spec),
                          () if BR.has_fanout(board) else first, power, fanout=BR.has_fanout(board))
    rank = {n: i for i, n in enumerate(order)}
    cur, cur_open, cur_B, cks = board, open0, B, []
    total = lambda: sum(cur_open.values())
    dead, ripped = {}, []
    stats = {"attempts": 0, "halvings": 0, "timeouts": 0, "violations": 0}
    extra = extra if extra is not None else {}
    gap = (rules or {}).get("min_clearance") or (rules or {}).get("clearance") or 0.1
    tally = DR.Tally()
    env = BR.router_env(True, S["env"])
    extra.update({"mode": "parallel", "round": 0, "workers": workers, "jobs_running": 0, "last_good": board,
                  "last_good_open": total(), "last_good_open_nets": len(cur_open), "start_open": total(),
                  "leftovers": 0, "out": board, "rounds": [], "rule_violations": 0, "dropped": {}, "batch": 0,
                  "batches_total": 0, "batch_nets": 1})

    def push(state="running"):
        extra.update(last_good=cur, last_good_open=total(), last_good_open_nets=len(cur_open), out=cur,
                     leftovers=len(dead) + len(ripped), rule_violations=stats["violations"], dropped=tally.totals())
        if cks:
            extra.update(checkpoint=cks[-1], checkpoint_n=len(cks))
        if status_path:
            BR._status_write(status_path, extra, state)

    size = rj
    rnd = 0
    while True:
        work = [n for n in order if n in cur_open and n not in dead and n not in ripped]
        remaining = (deadline - time.time()) if deadline else None
        if not work or (remaining is not None and remaining < min_slice):
            for n in work:
                dead[n] = "budget"
            break
        rnd += 1
        tally.new_round()
        jobs = work[:size]
        before = total()
        extra.update(round=rnd, jobs_running=len(jobs), batch=rnd, batch_nets=len(jobs))
        push()
        base_text = open(cur, errors="surrogateescape").read()
        base_blocks = blocks(base_text)
        jd = os.path.join(workdir, f"jobs-r{rnd:02d}")
        os.makedirs(jd, exist_ok=True)

        def one(item):
            k, net = item
            src = os.path.join(jd, f"{k:02d}-in.kicad_pcb")
            dst = os.path.join(jd, f"{k:02d}-out.kicad_pcb")
            shutil.copy(cur, src)
            BR._sidecar(cur, src)
            stats["attempts"] += 1
            st = run_job(build_cmd(src, dst, [net], True), cwd, dst, jt if remaining is None else min(jt, remaining),
                         env)
            return net, dst, st

        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(one, list(enumerate(jobs))))
        rec = {"round": rnd, "jobs": len(jobs), "merged": 0, "dropped": 0, "ripped": 0, "timeouts": 0,
               "violations": 0, "failed": 0, "open_before": before}
        cands = []
        for net, dst, st in results:
            if st == "timeout":
                stats["timeouts"] += 1
                rec["timeouts"] += 1
                dead[net] = "timeout"
                tally.add("timeout")
                continue
            txt = open(dst, errors="surrogateescape").read() if os.path.exists(dst) else ""
            if not txt.rstrip().endswith(")"):
                dead[net] = "no output"
                rec["failed"] += 1
                tally.add("no-path")
                continue
            try:
                Jb = RD.load(dst)
            except Exception:
                dead[net] = "no output"
                rec["failed"] += 1
                tally.add("other")
                continue
            new = RA.new_copper(cur_B, Jb)
            rem = RA.removed_copper(cur_B, Jb)
            if any(x["net"] != net for x in rem[0] + rem[1]):
                ripped.append(net)
                rec["ripped"] += 1
                tally.add("rips-other-net")
                continue
            if not new[0] and not new[1]:
                dead[net] = "failed"
                rec["failed"] += 1
                tally.add("no-path")
                continue
            viol = RA.audit(cur_B, Jb, rules, new_only=new) if rules and rules.get("audit", True) else []
            if viol:
                stats["violations"] += 1
                rec["violations"] += 1
                dead[net] = "RULE_VIOLATION"
                tally.add(DR.reason_of(viol))
                log(f"    parallel job {net}: RULE_VIOLATION, discarded: {RA.summarize(viol)}")
                continue
            nb = {k: b for k, b in blocks(txt).items() if k not in base_blocks}
            cands.append((net, new, nb))
        cands.sort(key=lambda c: rank[c[0]])
        accepted = []
        for c in cands:
            if any(conflicts(c[1], a[1], gap, cur_B["copper"]) for a in accepted):
                rec["dropped"] += 1              # stays in the work list for the next round
            else:
                accepted.append(c)
        ok = shrunk = False
        if accepted:
            merged = {}
            for _n, _new, nb in accepted:
                merged.update(snap_vias(nb))
            ck = os.path.join(workdir, f"ck-{len(cks) + 1:03d}.kicad_pcb")
            with open(ck, "w", errors="surrogateescape") as fh:
                fh.write(base_text.rstrip()[:-1].rstrip("\n") + "\n" + "\n".join(merged.values()) + "\n)\n")
            BR._sidecar(board, ck)
            viol = []
            try:
                viol = RA.audit(cur_B, ck, rules) if rules and rules.get("audit", True) else []
                if rules and rules.get("audit_drc") and not viol:
                    viol, _ = RA.new_drc_types(cur, ck, drc_cli)
            except Exception as e:
                viol = [{"code": "AUDIT_ERROR", "msg": str(e)[:120]}]
            after = dict(conn(ck)) if not viol else cur_open
            if viol:
                stats["violations"] += 1
                rec["violations"] += 1
                log(f"    parallel round {rnd}: merged board RULE_VIOLATION, discarded: {RA.summarize(viol)}")
                if len(accepted) == 1:
                    dead[accepted[0][0]] = "RULE_VIOLATION"
                size, shrunk = max(1, len(accepted) // 2), True
                stats["halvings"] += 1
            elif sum(after.values()) <= before:
                cks.append(ck)
                cur, cur_open, cur_B = ck, after, RD.load(ck)
                rec["merged"] = len(accepted)
                ok = sum(after.values()) < before
            else:
                if len(accepted) == 1:
                    dead[accepted[0][0]] = "failed"
                size, shrunk = max(1, len(accepted) // 2), True
                stats["halvings"] += 1
            if not (cks and cks[-1] == ck) and os.path.exists(ck):
                os.remove(ck)
        rec["open_after"] = total()
        rec["dropped_by"] = tally.this_round()
        extra["rounds"] = (extra["rounds"] + [rec])[-30:]
        extra["jobs_running"] = 0
        log(f"    parallel round {rnd}: {len(jobs)} job(s), {rec['merged']} merged, {rec['dropped']} dropped "
            f"(conflict), {rec['ripped']} ripped, {rec['timeouts']} timeout, {rec['violations']} RULE_VIOLATION; "
            f"open {before} -> {total()}")
        push()
        if not keep_jobs:
            shutil.rmtree(jd, ignore_errors=True)
        if not ok and not shrunk:
            break

    # one serial pass over the nets whose jobs ripped copper
    serial_left = [n for n in ripped if n in cur_open]
    reasons = dict(dead)
    if serial_left:
        remaining = (deadline - time.time()) if deadline else None
        if remaining is None or remaining >= min_slice:
            log(f"    serial pass: {len(serial_left)} net(s) whose parallel job ripped other copper")
            sr = BR.route_batches(cur, os.path.join(workdir, "serial"), serial_left, serial_run, cons=cons, spec=spec,
                                  B=cur_B, cls=cls, rules=rules, conn=conn, total_budget=remaining,
                                  status_path=None, log=log, drc_cli=drc_cli)
            cks += sr["checkpoints"]
            if sr["checkpoints"]:
                cur = sr["board"]
                cur_open = dict(conn(cur))
                cur_B = RD.load(cur)
            for n in serial_left:
                if n in sr["reasons"]:
                    reasons[n] = sr["reasons"][n]
            for k in ("attempts", "halvings", "timeouts", "violations"):
                stats[k] += sr[k]
        else:
            for n in serial_left:
                reasons[n] = "budget"
    for n in order:
        if n in cur_open and n not in reasons:
            reasons[n] = "failed"
    out = BR.finish(order, cur, cur_open, cks, reasons, stats, open0, t0)
    out["rounds"] = extra["rounds"]
    out["dropped"] = tally.totals()
    push("done")
    BR.write_leftovers(workdir, out)
    return out
