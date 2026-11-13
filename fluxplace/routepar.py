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
from . import pairaudit as PA
from . import railroute as RL
from . import ripolicy as RIP
from . import routeaudit as RA
from . import routeconn as RC
from . import snapvias as SV
from . import boardtext as BT
from . import routedrops as DR
from . import routediag as RD
from . import routestatus as RS

from .boardtext import blocks, _num2, _NUM, key_of as _key   # noqa: E402,F401  (kept importable from here)


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


class Unit:
    """One router job: a net, a differential pair (both legs, one call) or one edge of a rail's tree."""

    def __init__(self, kind, nets, edge=None, net=None, width=None):
        self.kind, self.nets, self.edge, self.net, self.width = kind, list(nets), edge, net, width
        self.id = "\t".join(nets) if kind != "rail" else f"{net}@{edge['key'][0][0]}.{edge['key'][0][1]}-{edge['key'][1][0]}.{edge['key'][1][1]}"


def _plain_ok(Jb, net, cu):
    """a plain-copper net: no layer change beyond what its pads force (one via at most when no layer is common to all pads)."""
    pads = [p for p in Jb["pads"] if p["net"] == net and not p.get("np")]
    common = set(cu)
    for p in pads:
        common &= set(p["layers"] or cu)
    allowed = 0 if common else 1
    pr = PA.profile(Jb, net)
    return pr["vias"] <= allowed and len(pr["layers"]) <= 1 + allowed


def _pair_state(Jb, p, n, cons, open_set):
    """-> ('complete'|'mismatch'|'half'|'open', why) of a pair in the (job) board."""
    r = PA.audit(Jb, [(p, n)], cons, open_set)[(p, n)]
    return r["status"], r["why"]


def route_parallel(board, workdir, nets, build_cmd, serial_run, *, cons=None, spec=None, B=None, cls=None,
                   first=(), power=(), rules=None, workers=None, job_timeout=None, round_jobs=None,
                   total_budget=None, status_path=None, extra=None, log=print, conn=None,
                   drc_cli="kicad-cli", cwd=None, min_slice=5.0, keep_jobs=False, post=None):
    """Parallel per-net routing with merge rounds (see module doc).  `build_cmd(src, dst, only, limited)` as for
    batchroute.make_runner; `serial_run` = batchroute runner for the optional serial pass (`allow_rip = "any"`).
    Units: a net, a differential pair (both legs as one job), one edge of a rail's tree (railroute), plain-copper nets first."""
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
    cls = cls or NC.classify(B, cons, spec)
    fo = BR.has_fanout(board)
    want = [n for n in dict.fromkeys(nets) if n in open0]
    plain = RIP.plain_nets(cons)
    rails, poured = RL.rail_nets(B, cons, cls, want, plain)
    railset = set(rails)
    first_eff = () if fo else first
    ph = BR.phase_map(B, want, cls, cons, first_eff, power, BR.escape_nets_of(board, cons), rails=railset)
    full = BR.order_nets(B, want, cls, first_eff, power, fanout=fo, phases=ph)     # escape, plain, power pins, rails, controlled, rest
    order = [n for n in full if n not in railset]
    rank = {n: i for i, n in enumerate(full)}                        # rails rank by their phase too, not last
    order_info = BR.order_summary(full, ph)
    pair_of = {}
    if r.get("pair_audit", True):
        for p_, n_ in PA.pair_list(cls):
            pair_of[p_], pair_of[n_] = n_, p_
    controlled = RIP.controlled_nets(cls, cons, names=[p["net"] for p in B["pads"] if p["net"]])
    pol = RIP.policy(cons)
    rip_trials = int(r.get("rip_trials_per_round", 3))
    pair_mismatch = str(r.get("pair_mismatch", "keep")).lower()      # "keep": merged, reported as mismatch; "drop": not merged
    cur, cur_open, cur_B, cks = board, open0, B, []
    total = lambda: sum(cur_open.values())
    dead = {}                             # unit id -> reason
    dead_edges = {}                       # rail net -> {edge key}
    rail_done = {}                        # rail net -> merged edges
    pair_events = {}                      # (p, n) -> "half-removed" | "failed"
    pair_causes = {}                      # why pair results were not merged: half / layers / vias / skew -> count
    stats = {"attempts": 0, "halvings": 0, "timeouts": 0, "violations": 0}
    extra = extra if extra is not None else {}
    gap = (rules or {}).get("min_clearance") or (rules or {}).get("clearance") or 0.1
    tally = DR.Tally()
    env = BR.router_env(True, S["env"])
    extra.update({"mode": "parallel", "round": 0, "workers": workers, "jobs_running": 0, "last_good": board,
                  "last_good_open": total(), "last_good_open_nets": len(cur_open), "start_open": total(),
                  "leftovers": 0, "out": board, "rounds": [], "rule_violations": 0, "dropped": {}, "batch": 0,
                  "batches_total": 0, "batch_nets": 1, "order": order_info})

    def rails_status():
        left = {n: cur_open.get(n, 0) for n in rails}
        return {"total": len(rails), "complete": sum(1 for n in rails if not left[n]),
                "open_connections": sum(left.values()), "edges_merged": sum(rail_done.values()),
                "edges_failed": sum(len(v) for v in dead_edges.values()), "poured": poured,
                "open": {n: k for n, k in left.items() if k}}

    def pairs_status():
        res = PA.audit(cur_B, PA.pair_list(cls), cons, cur_open) if pair_of else {}
        out = {"complete": [], "mismatch": [], "half-removed": [], "failed": []}
        for (p_, n_), e in res.items():
            k = f"{p_}/{n_}"
            if e["status"] == "complete":
                out["complete"].append(k)
            elif e["status"] == "mismatch":
                out["mismatch"].append(k)
            elif e["status"] == "open" and (p_, n_) in pair_events:
                out[pair_events[(p_, n_)]].append(k)
            elif e["status"] == "open":
                out["failed"].append(k)
            else:
                out["half-removed"].append(k)
        return out

    qa = {"ctx": None, "path": None}

    def quality():
        """pair / RF quality of the checkpoint (qualaudit): out-of-tolerance nets are NOT DONE, not routed."""
        if qa["path"] != cur:
            try:
                from . import qualaudit as QA
                qa["ctx"] = qa["ctx"] or QA._ctx(board, cur_B, cons)
                extra.update(QA.status_fields(cur_B, cls, cons, cur_open, open0, qa["ctx"]))
            except Exception as e:                       # never let the audit stop routing; say so
                extra["not_done"] = {"error": str(e)[:100]}
            qa["path"] = cur
        return extra.get("not_done")

    def push(state="running"):
        quality()
        extra.update(last_good=cur, last_good_open=total(), last_good_open_nets=len(cur_open), out=cur,
                     leftovers=len(dead), rule_violations=stats["violations"], dropped=tally.totals())
        if rails:
            extra["rails"] = rails_status()
        if pair_causes:
            extra["pair_causes"] = dict(pair_causes)
        if cks:
            extra.update(checkpoint=cks[-1], checkpoint_n=len(cks))
        if status_path:
            BR._status_write(status_path, extra, state)

    def accept_board(path):
        """`path` becomes the new checkpoint."""
        nonlocal cur, cur_open, cur_B
        cks.append(path)
        if post:                                                  # e.g. join small gaps: edits the checkpoint in place
            post(path)
        cur, cur_B = path, RD.load(path)
        cur_open = dict(conn(path))

    def pair_pass(tag):
        """a half-routed pair is removed (both legs' unlocked copper) before anything else is routed."""
        nonlocal cur, cur_open, cur_B
        if not pair_of:
            return
        res = PA.audit(cur_B, PA.pair_list(cls), cons, cur_open)
        half = [k for k, e in res.items() if e["status"] == "half"]
        if not half:
            return
        txt = open(cur, errors="surrogateescape").read()
        for p_, n_ in half:
            txt, k = PA.strip_legs(txt, [p_, n_])
            pair_events[(p_, n_)] = "half-removed"
            dead.pop("\t".join((p_, n_)), None)
            log(f"    pair {p_}/{n_}: half-routed, {k} track/via(s) removed ({tag})")
        ck = os.path.join(workdir, f"ck-{len(cks) + 1:03d}.kicad_pcb")
        with open(ck, "w", errors="surrogateescape") as fh:
            fh.write(txt)
        BR._sidecar(board, ck)
        accept_board(ck)

    pair_pass("input")
    size = rj
    rnd = 0
    while True:
        remaining = (deadline - time.time()) if deadline else None
        work = [n for n in order if n in cur_open]
        units, used = [], set()
        for n in work:
            if n in used:
                continue
            q = pair_of.get(n)
            u = Unit("pair", [n, q]) if q and q in cur_open and q in rank else Unit("net", [n])
            used.update(u.nets)
            if u.id not in dead:
                units.append(u)
        runits, rmem = [], {}
        if rails:
            open_rails = [n for n in rails if n in cur_open]
            rp, _left, rmem = RL.plan(cur_B, cons, open_rails, dead_edges)
            for n in open_rails:
                for e in rp.get(n, []):
                    runits.append(Unit("rail", [n], edge=e, net=n, width=RL.width_mm(cons, n)))
                if n not in rp and f"rail:{n}" not in dead:
                    dead[f"rail:{n}"] = "no-path"
                    tally.add("no-path")
        if not units and not runits or (remaining is not None and remaining < min_slice):
            for u in units + runits:
                dead.setdefault(u.id, "budget")
            break
        rnd += 1
        tally.new_round()
        cap = max(1, size // 2) if units else size                  # rail edges take at most half the slots while other nets wait
        jobs, nr = [], 0
        for u in sorted(units + runits, key=lambda u: min(rank.get(n, 10 ** 6) for n in u.nets)):   # the routing order, rails included
            if len(jobs) >= size:
                break
            if u.kind == "rail":
                if nr >= cap:
                    continue
                nr += 1
            jobs.append(u)
        before = total()
        dstate0 = (len(dead), sum(len(v) for v in dead_edges.values()))
        extra.update(round=rnd, jobs_running=len(jobs), batch=rnd, batch_nets=len(jobs))
        push()
        base_text = open(cur, errors="surrogateescape").read()
        base_blocks = blocks(base_text)
        jd = os.path.join(workdir, f"jobs-r{rnd:02d}")
        os.makedirs(jd, exist_ok=True)

        def one(item):
            k, u = item
            src = os.path.join(jd, f"{k:02d}-in.kicad_pcb")
            dst = os.path.join(jd, f"{k:02d}-out.kicad_pcb")
            if u.kind == "rail":
                with open(src, "w", errors="surrogateescape") as fh:
                    fh.write(RL.job_text(base_text, cur_B, u.net, u.edge, rmem))
            else:
                shutil.copy(cur, src)
            BR._sidecar(cur, src)
            stats["attempts"] += 1
            try:
                cmd = build_cmd(src, dst, u.nets, True, pair=True) if u.kind == "pair" else build_cmd(src, dst, u.nets, True)
            except TypeError:                                  # a builder without a pair mode: the legs go to the same router
                cmd = build_cmd(src, dst, u.nets, True)
            if u.width:
                cmd = list(cmd) + ["--power-nets", u.net, "--power-nets-widths", f"{u.width:g}"]
            st = run_job(cmd, cwd, dst, jt if remaining is None else min(jt, remaining), env)
            return u, dst, st

        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(one, list(enumerate(jobs))))
        rec = {"round": rnd, "jobs": len(jobs), "merged": 0, "dropped": 0, "ripped": 0, "timeouts": 0,
               "violations": 0, "failed": 0, "open_before": before, "rail_jobs": nr}

        def drop(u, reason, why=None):
            """a result is not merged: counted by reason; a rail edge is marked failed, any other unit dead."""
            tally.add(reason)
            if u.kind == "rail":
                dead_edges.setdefault(u.net, set()).add(u.edge["key"])
            else:
                dead[u.id] = why or reason
            if u.kind == "pair":
                pair_events[tuple(u.nets)] = pair_events.get(tuple(u.nets), "failed")

        cands, rip_cands = [], []
        for u, dst, st in results:
            if st == "timeout":
                stats["timeouts"] += 1
                rec["timeouts"] += 1
                drop(u, "timeout")
                continue
            txt = open(dst, errors="surrogateescape").read() if os.path.exists(dst) else ""
            if not txt.rstrip().endswith(")"):
                rec["failed"] += 1
                drop(u, "no-path", "no output")
                continue
            try:
                Jb = RD.load(dst)
            except Exception:
                rec["failed"] += 1
                drop(u, "other", "no output")
                continue
            Jb["pads"] = cur_B["pads"]               # a rail job's pads were taken off the net: audit against the true ones
            if u.kind == "rail":                     # ... and so was the copper of its other clusters: it is the rail's again
                for x in Jb["segs"] + Jb["vias"]:
                    if not x["net"]:
                        x["net"] = u.net
            new = RA.new_copper(cur_B, Jb)
            rem = RA.removed_copper(cur_B, Jb)
            aff = RIP.affected(rem, set(u.nets))
            if aff:
                ok, why = RIP.prefilter(pol, aff, controlled, list(rem[0]) + list(rem[1]))
                if not ok:
                    rec["ripped"] += 1
                    log(f"    job {u.id}: rips {', '.join(sorted(aff)[:3])}: {why}; dropped")
                    drop(u, "rips-other-net")
                    continue
            if not new[0] and not new[1]:
                rec["failed"] += 1
                drop(u, "no-path", "failed")
                continue
            viol = RA.audit(cur_B, Jb, rules, new_only=new) if rules and rules.get("audit", True) else []
            if viol:
                stats["violations"] += 1
                rec["violations"] += 1
                log(f"    parallel job {u.id}: RULE_VIOLATION, discarded: {RA.summarize(viol)}")
                if aff:
                    viol = [v for v in viol if v["code"] != "FANOUT_RIPPED"] or viol
                drop(u, DR.reason_of(viol), "RULE_VIOLATION")
                continue
            if u.kind == "pair":                                  # a half-routed pair is never merged
                jo = dict(RC.text_open(dict(Jb, pads=[x for x in Jb["pads"] if x["net"] in u.nets])))
                stt, why = _pair_state(Jb, u.nets[0], u.nets[1], cons, jo)
                got = {x["net"] for x in list(new[0]) + list(new[1])}
                if stt == "half" or (stt == "open" and not set(u.nets) <= got) or (stt == "mismatch" and pair_mismatch == "drop"):
                    log(f"    pair {u.id}: {stt} ({why}); result removed")
                    for c_ in ([w.split()[0] for w in why.split("; ")] if stt == "mismatch" and why else [stt]):
                        pair_causes[c_] = pair_causes.get(c_, 0) + 1
                    pair_events[tuple(u.nets)] = "half-removed" if stt in ("half", "open") else "failed"
                    drop(u, "no-path" if stt in ("half", "open") else "other", f"pair {stt}")
                    continue
                if stt == "mismatch":                             # merged, but not counted as routed: status `mismatch`
                    for c_ in [w.split()[0] for w in why.split("; ")]:
                        pair_causes[c_] = pair_causes.get(c_, 0) + 1
            if u.nets[0] in plain and u.kind == "net" and not _plain_ok(Jb, u.nets[0], Jb["copper"]):
                log(f"    plain-copper net {u.id}: the result changes layer; discarded")
                drop(u, "other", "plain copper violated")
                continue
            jb = blocks(txt)
            nb = {k: b for k, b in jb.items() if k not in base_blocks and (u.kind != "rail" or BT.net_name(b))}
            rk = [k for k in base_blocks if k not in jb] if aff else []
            c = (u, new, nb, rk, aff, rem)
            (rip_cands if aff else cands).append(c)
        cands.sort(key=lambda c: min(rank.get(n, 10 ** 6) for n in c[0].nets))
        accepted = []
        for c in cands:
            if any(conflicts(c[1], a[1], gap, cur_B["copper"]) for a in accepted):
                rec["dropped"] += 1              # stays in the work list for the next round
            else:
                accepted.append(c)
        ok = shrunk = False

        def merged_ck(txt0, add, remove=()):
            txt1 = BT.remove_blocks(txt0, remove) if remove else txt0
            txt1 = BT.append_blocks(txt1, add)
            txt1, nsnap, _nv = SV.snap_text(txt1)                    # track ends inside a via go to its centre
            ck = os.path.join(workdir, f"ck-{len(cks) + 1:03d}.kicad_pcb")
            with open(ck, "w", errors="surrogateescape") as fh:
                fh.write(txt1)
            BR._sidecar(board, ck)
            return ck

        def check(ck):
            """-> (violations, open dict)."""
            viol = []
            try:
                viol = RA.audit(cur_B, ck, rules) if rules and rules.get("audit", True) else []
                if rules and rules.get("audit_drc") and not viol:
                    viol, _ = RA.new_drc_types(cur, ck, drc_cli)
            except Exception as e:
                viol = [{"code": "AUDIT_ERROR", "msg": str(e)[:120]}]
            return viol, (dict(conn(ck)) if not viol else cur_open)

        if accepted:
            merged = {}
            for c in accepted:
                merged.update(snap_vias(c[2]))
            ck = merged_ck(base_text, merged.values())
            viol, after = check(ck)
            if viol:
                stats["violations"] += 1
                rec["violations"] += 1
                log(f"    parallel round {rnd}: merged board RULE_VIOLATION, discarded: {RA.summarize(viol)}")
                if len(accepted) == 1:
                    drop(accepted[0][0], DR.reason_of(viol), "RULE_VIOLATION")
                else:
                    tally.add(DR.reason_of(viol), len(accepted))
                size, shrunk = max(1, len(accepted) // 2), True
                stats["halvings"] += 1
            elif sum(after.values()) <= before:
                accept_board(ck)
                rec["merged"] = len(accepted)
                for c in accepted:
                    if c[0].kind == "rail":
                        rail_done[c[0].net] = rail_done.get(c[0].net, 0) + 1
                ok = sum(after.values()) < before
            else:
                if len(accepted) == 1:
                    drop(accepted[0][0], "other", "failed")
                else:
                    tally.add("other", len(accepted))
                size, shrunk = max(1, len(accepted) // 2), True
                stats["halvings"] += 1
            if not (cks and cks[-1] == ck) and os.path.exists(ck):
                os.remove(ck)
        # results that removed other nets' copper: each adopted only when the open count drops and the affected nets are routed again
        for c in sorted(rip_cands, key=lambda c: min(rank.get(n, 10 ** 6) for n in c[0].nets))[:max(0, rip_trials)]:
            u, new, nb, rk, aff, rem = c
            if any(conflicts(new, a[1], gap, cur_B["copper"]) for a in accepted):
                rec["dropped"] += 1
                continue
            cur_text = open(cur, errors="surrogateescape").read()
            ck = merged_ck(cur_text, nb.values(), rk)
            viol, after = check(ck)
            tot_after = sum(after.values())
            if not viol and tot_after < total() and all(after.get(n, 0) <= cur_open.get(n, 0) for n in aff) \
                    and after.get(u.nets[0], 0) < cur_open.get(u.nets[0], 0):
                before_r = total()
                accept_board(ck)
                accepted.append(c)
                rec["merged"] += 1
                ok = True
                log(f"    job {u.id}: ripped {len(aff)} net(s), all routed again; open {before_r} -> {total()}; adopted")
            else:
                if os.path.exists(ck):
                    os.remove(ck)
                drop(u, "rips-other-net")
                rec["ripped"] += 1
                log(f"    job {u.id}: rips {', '.join(sorted(aff)[:3])}; open count not lower or a net left open; dropped")
        for c in rip_cands[max(0, rip_trials):]:
            rec["dropped"] += 1                  # next round
        rec["open_after"] = total()
        rec["dropped_by"] = tally.this_round()
        extra["rounds"] = (extra["rounds"] + [rec])[-30:]
        extra["jobs_running"] = 0
        log(f"    parallel round {rnd}: {len(jobs)} job(s) ({nr} rail edge(s)), {rec['merged']} merged, {rec['dropped']} dropped "
            f"(conflict), {rec['ripped']} ripped, {rec['timeouts']} timeout, {rec['violations']} RULE_VIOLATION; "
            f"open {before} -> {total()}")
        push()
        if not keep_jobs:
            shutil.rmtree(jd, ignore_errors=True)
        if ok and not shrunk:
            size = min(rj, size * 2)                 # a round that improved the board earns its jobs back
        if not (ok or shrunk or (len(dead), sum(len(v) for v in dead_edges.values())) != dstate0):
            break

    pair_pass("final")
    reasons = {}
    for k, v in dead.items():
        for n in k.split("\t") if not k.startswith("rail:") else [k[5:]]:
            reasons[n] = v
    for n in rails:
        if n in cur_open:
            reasons.setdefault(n, "rail: %d connection(s) open" % cur_open[n])
    for n in order:
        if n in cur_open and n not in reasons:
            reasons[n] = "failed"
    out = BR.finish(order + rails, cur, cur_open, cks, reasons, stats, open0, t0)
    out["rounds"] = extra["rounds"]
    out["dropped"] = tally.totals()
    out["rails"] = rails_status() if rails else None
    out["pairs"] = pairs_status() if pair_of else None
    if out["pairs"]:
        extra["pairs"] = {k: v for k, v in out["pairs"].items()}
        extra["pair_causes"] = dict(pair_causes)
        out["pair_causes"] = dict(pair_causes)
    push("done")
    BR.write_leftovers(workdir, out)
    return out
