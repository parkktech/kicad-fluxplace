"""route stage, `[route] router = "krt-whole"`: the four-call KiCadRoutingTools chain, nothing else.

  1-2  route_planes.py  one call per plane (net, layer) of the board's zones
  r    [power] rails    spine routes of the [power] rails at their width (PHASE3B)
  p    pairgroups       Python step (PHASE3C): failed pairs re-laid per connector group; skipped when none is incomplete
  3    route_diff.py    the pair nets that are still open
  4    route.py         every net but the legs of pairs p could not lay, plane nets as --power-nets, --keep-input-copper

Each call reads the previous call's board and writes the next (the .kicad_pro travels along).  Exit 0 and 3 (some
feature delivered below the requested size, never below the floor) are accepted; anything else stops the chain."""
import os
import subprocess
import time

from . import routerules as RR, routeconn as RC, pairfb as PFB

CAP_S = 1200.0           # one router call, also bounded by the stage's total route budget
POWER_W = 0.3            # --power-nets width when the rules give none


def _pcb():
    import pcbnew
    return pcbnew


PLANE_FRAC = 0.9         # an inner layer is a plane when one zone's outline covers this share of the board outline


def plane_plan(board):
    """-> (signal layers, [(net, plane layer)] in stack order): the planes are the inner layers carrying a board-spanning zone."""
    b = _pcb().LoadBoard(board)
    cu = [b.GetLayerName(x) for x in b.GetEnabledLayers().CuStack()]
    bb = b.GetBoardEdgesBoundingBox()
    full = (bb.GetWidth() * bb.GetHeight()) or 1
    planes = []
    for z in b.Zones():
        if z.GetIsRuleArea() or not z.GetNetname() or z.Outline().Area() < PLANE_FRAC * full:
            continue
        for lid in z.GetLayerSet().CuStack():
            k = (z.GetNetname(), b.GetLayerName(lid))
            if k[1] not in (cu[0], cu[-1]) and k not in planes:
                planes.append(k)
    pl = {lay for _n, lay in planes}
    return [x for x in cu if x not in pl], sorted(planes, key=lambda k: cu.index(k[1]))


def pair_nets(cons, names, report):
    """nets of every pair: the pairs report's legs, else the [pairs.*] families' P/N tails."""
    from . import constraints as C
    nopair = C.not_pairs(cons)
    legs = [x for p in (report or {}).get("pairs") or [] for x in (p.get("pair") or [])]
    if legs:
        return sorted(set(legs) - nopair)
    fams = list(((cons or {}).get("pairs") or {}))
    return sorted(x for x in names if any(x.startswith(f) for f in fams) and x not in nopair)


def _missed_legs(row):
    legs = {row.get("p"), row.get("n")} - {None}
    if len(legs) == 2:
        return frozenset(legs)
    parts = str(row.get("pair", "")).split("/", 1)
    return frozenset(parts) if len(parts) == 2 and all(parts) else frozenset()


def _input_locked_connected(board, open_names, legsets):
    """Pairs with real, entirely locked copper on both legs and no open leg in the router input."""
    b = _pcb().LoadBoard(board)
    copper = {}
    for t in b.GetTracks():
        copper.setdefault(t.GetNetname(), []).append(t)
    done = set()
    for legs in legsets:
        if len(legs) != 2 or any(net in open_names for net in legs):
            continue
        items = [t for net in legs for t in copper.get(net, [])]
        if all(copper.get(net) for net in legs) and all(t.IsLocked() for t in items):
            done.add(legs)
    return done


def _partition_missed(missed, report, board, open_names):
    complete = {frozenset(p.get("pair") or []) for p in (report or {}).get("pairs", []) if p.get("status") == "complete"}
    legsets = {_missed_legs(row) for row in missed}
    try:
        locked = _input_locked_connected(board, open_names, legsets)
    except Exception:                                        # noqa: BLE001 - diagnostics must not lose a routed board
        locked = set()
    remaining, done = [], []
    for row in missed:
        legs = _missed_legs(row)
        reason = "pairs_stage_complete" if legs in complete else "input_connected_locked" if legs in locked else None
        if reason:
            done.append({"pair": row.get("pair"), "p": row.get("p"), "n": row.get("n"), "reason": reason})
        else:
            remaining.append(row)
    return remaining, done


def glob_escape(name):
    return name.replace("[", "[[]")


def cmd_planes(py, rdir, src, dst, net, layer, base):
    return [py, os.path.join(rdir, "py_router", "route_planes.py"), src, dst, "--nets", glob_escape(net), "--plane-layers", layer, *base]


def cmd_diff(py, rdir, src, dst, nets, sig, base, summary=None):
    cmd = [py, os.path.join(rdir, "py_router", "route_diff.py"), src, dst, "--nets", *nets, "--layers", *sig, *base, "--keep-input-copper"]
    return cmd + (["--json-out", summary] if summary else [])


def cmd_route(py, rdir, src, dst, sig, base, power, summary=None, nets=("*",)):
    pw = ["--power-nets", *[glob_escape(n) for n in power], "--power-nets-widths", *[f"{w:g}" for w in power.values()]] if power else []
    cmd = [py, os.path.join(rdir, "py_router", "route.py"), src, dst, "--nets", *[glob_escape(n) if n != "*" else n for n in nets],
           *pw, "--layers", *sig, *base, "--keep-input-copper"]
    return cmd + (["--json-out", summary] if summary else [])


def base_flags(R, d):
    """the recipe's RULES: sizes from the rules, escalation off, strict sizes, the floor file."""
    fo = RR.write_overrides(os.path.join(d, "router-fab.txt"), R)
    return [*RR.size_args(R), "--escalation", "off", "--strict-sizes", *(["--fab-overrides", fo] if fo else [])]


def run_step(cmd, log, cap_s=CAP_S, status_path=None, step=None):
    """-> (rc, seconds); rc 124 on the cap, -N when killed by a signal.  Never retried."""
    from . import routestatus as RS
    t = time.time()
    try:
        r = RS.run_logged(cmd, cwd=os.path.dirname(os.path.dirname(cmd[1])), timeout=cap_s, out=cmd[3], log_path=log,
                          status_path=status_path, extra={"router": "krt-whole", "current_step": step}, keep_open=True)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        rc = 124
    return rc, round(time.time() - t, 1)


def _status(path, state, rows, left=None, rc=None, extra=None):
    import json
    from . import routestatus as RS
    try:
        with open(path) as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {"stage": "route", "router": "krt-whole"}
    st.update({"state": state, "steps": rows, "open": left or {}, "rc": rc, "updated": time.time()})
    if extra:
        st.update(extra)
    RS._atomic(path, st)


def _carry(src, dst):
    import shutil
    s, t = os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(dst)[0] + ".kicad_pro"
    if os.path.exists(s) and not os.path.exists(t):
        shutil.copy(s, t)


def _restore_project(source_board, output_board):
    """Discard router-written project settings; the approved input project is the only rule source."""
    import json
    import shutil
    src = os.path.splitext(source_board)[0] + ".kicad_pro"
    dst = os.path.splitext(output_board)[0] + ".kicad_pro"
    if not os.path.exists(src):
        return []
    changed = ["missing"]
    if os.path.exists(dst):
        if open(src, "rb").read() == open(dst, "rb").read():
            return []
        try:
            a = json.load(open(src))
            b = json.load(open(dst))
            changed = [k for k in ("board", "net_settings") if a.get(k) != b.get(k)] or ["project metadata"]
        except (OSError, ValueError, TypeError):
            changed = ["unreadable router project"]
    shutil.copy(src, dst)
    return changed


def _post(best, d, planes, R):
    """The same guarded cleanup the batch route gets: via-centre snap, plane ties, then a saved fresh fill."""
    from . import snapiter as SI, planetie as PT, opengate as OG
    snap = SI.run(best, best, log=lambda _m: None)
    ties = PT.run_guarded(best, best, [net for net, _lay in planes], RC.open_by_net, log=lambda _m: None, workdir=d,
                          plane_layers=[lay for _net, lay in planes],
                          rules={"track": R["track"], "clearance": RR.request_clearance(R), "via": R["via"], "drill": R["drill"]})
    OG.refill(best)
    return {"snap": snap, "ties": ties}


def _run_pairgroups(c, d, src, dst, failed_legs, budget_s, pair_legs=()):
    """PHASE3C C.1 `pairgroups`: regroup the incomplete pairs (pairaudit) per connector group -> dst. No incomplete pair:
    dst is a copy of src. Legs of the pairs still failed go to `failed_legs` (the route step leaves them to the pair stage).
    PHASE3E 4: budget = PER_PAIR_S x incomplete pairs, capped by budget_s (the stage budget left). Any exception puts
    every leg of every incomplete pair (all `pair_legs` until the audit ran) in failed_legs: a crashed step never
    plain-routes a pair. A failure copies src and records the error: the routed board stands. Always returns rc 0."""
    import json
    from . import pairsonly as PO, pairaudit as PA, netclass as NC, routediag as RD, constraints as C
    spec = getattr(c, "spec", None)
    lines = []
    legs = set(pair_legs)
    try:
        B = RD.load(src)
        pairs = PA.pair_list(NC.classify(B, c.cons, spec))
        nopair = C.not_pairs(c.cons)
        opn = {x for x, k in RC.text_open(B).items() if k}
        aud = PA.audit(B, pairs, c.cons, opn) if pairs else {}
        todo = [p for p in pairs if aud[p]["status"] != "complete" and not (set(p) & nopair)]
        legs = {x for p in todo for x in p}
        if todo:
            budget = min(PO.PER_PAIR_S * len(todo), float(budget_s))
            if budget <= 0:
                raise RuntimeError("no stage budget left for pairgroups")
            rep = PO.regroup(src, dst, c.cons, spec, todo, log=lines.append, budget_s=budget)
            failed_legs.update(x for p in rep["failed"] for x in p)
        else:
            shutil_copy(src, dst)
            rep = {"skipped": "no incomplete pair"}
    except Exception as e:                                   # noqa: BLE001 - a worse route, never a lost board
        failed_legs.update(legs)
        shutil_copy(src, dst)
        rep = {"error": f"{type(e).__name__}: {str(e)[:160]}", "failed_legs": sorted(legs)}
        lines.append(rep["error"])
    open(os.path.splitext(dst)[0] + ".json", "w").write(json.dumps(rep, indent=1, default=list))
    open(os.path.splitext(dst)[0] + ".log", "w").write("\n".join(lines) + "\n")
    return 0


def run(c, n, board, runner=run_step):
    """the route stage in krt-whole mode -> stage result dict.  `runner(cmd, log)` -> (rc, s): replaceable in tests."""
    import json
    a = c.a
    d = c.dir(n, "route")
    best = os.path.join(d, "board.kicad_pcb")
    spath = os.path.join(d, "routed.kicad_pcb.status.json")
    shutil_copy(board, best)
    R = RR.rules_for(c.cons, board, a)
    base = base_flags(R, d)
    sig, planes = plane_plan(board)
    names = set(RC.open_by_net(board))                       # nets that are open
    rep = None
    pd = ((getattr(c, "state", None) or {}).get("stages") or {}).get("pairs", {}).get("dir")
    reports = ([os.path.join(c.work, pd, "board.pairs.json")] if pd else []) + [os.path.splitext(board)[0] + ".pairs.json"]
    for report_path in reports:
        try:
            rep = json.load(open(report_path))
            break
        except (OSError, ValueError, TypeError):
            pass
    allnets = set(_all_nets(board))
    hand = PFB.handoff_nets(getattr(c, "state", None), getattr(c, "work", ""))
    pn = pair_nets(c.cons, allnets, rep)
    opn = {x for x in pn if x in names or x in hand}                 # a pair is incomplete when either leg is open
    diff = sorted(x for x in pn if any(_diffnamed(x, y) and (x in opn or y in opn) for y in pn))
    from . import power as PWR      # D71cj: [power] rails at their width; a net with a zone on the board is not a rail
    from . import gaps as GP
    nl = len(sig) + len({lay for _n, lay in planes})
    rails_error = None
    try:
        rails = PWR.route_widths(c.cons, layers=nl, track_mm=R["track"], exclude=GP._zone_nets(board))
    except Exception as e:                                   # noqa: BLE001 - no rail widths is a worse route, not a failed stage
        rails = {}
        rails_error = str(e)[:120]
    rail_nets = sorted(rails)
    pour_rail_nets = [n for n in rail_nets if PWR.is_pour(c.cons, n)]     # PHASE3E 5: only pour rails are outer-only
    other_rail_nets = [n for n in rail_nets if n not in pour_rail_nets]
    steps = [(f"plane-{net}", lambda s, o, net=net, lay=lay: cmd_planes(a.router_py, a.router_dir, s, o, net, lay, base)) for net, lay in planes]
    if pour_rail_nets:
        steps.append(("rails-pour", lambda s, o: cmd_route(a.router_py, a.router_dir, s, o, PWR.rail_layers(c.cons, sig), base, rails, o[:-10] + ".json", nets=pour_rail_nets)))
    if other_rail_nets:
        steps.append(("rails", lambda s, o: cmd_route(a.router_py, a.router_dir, s, o, list(sig), base, rails, o[:-10] + ".json", nets=other_rail_nets)))
    failed_legs = set()                                      # C.2: legs of pairs pairgroups could not lay are never plain nets
    def _pairgroups(s, o):
        return _run_pairgroups(c, d, s, o, failed_legs, min(CAP_S, max(0.0, budget - (time.time() - t0))), pair_legs=pn)
    steps.append(("pairgroups", lambda s, o: _pairgroups))   # a callable step: run() calls it, not the router runner
    def _route_nets():
        return sorted(allnets - failed_legs) if failed_legs else ("*",)
    if diff:
        steps.append(("diff", lambda s, o: cmd_diff(a.router_py, a.router_dir, s, o, diff, sig, base, o[:-10] + ".json")))
    power = {net: float(((c.cons or {}).get("power") or {}).get(net, {}).get("width_mm") or POWER_W)    # plane nets: taps, not rails
             for net in dict.fromkeys(x for x, _ in planes)}
    power = {**rails, **power}                               # plane entries win for plane nets
    steps.append(("route", lambda s, o: cmd_route(a.router_py, a.router_dir, s, o, sig, base, power, o[:-10] + ".json", nets=_route_nets())))
    sizes = {"track": R["track"], "clearance": RR.request_clearance(R), "via": R["via"], "drill": R["drill"]}
    budget = float(getattr(a, "route_budget", CAP_S) or CAP_S)
    t0 = time.time()
    cur, rows, discarded = best, [], []
    for i, (name, build) in enumerate(steps, 1):
        dst = os.path.join(d, f"{i}-{name}.kicad_pcb".replace("*", "x"))
        _carry(cur, dst)
        if os.path.exists(dst):                              # a previous run's board is never evidence for this one
            os.remove(dst)
        log = dst[:-10] + ".log"
        left_s = budget - (time.time() - t0)
        cap = min(CAP_S, max(0.0, left_s))
        if cap <= 0:
            rc, secs = 124, 0.0
        else:
            cmd = build(cur, dst)
            if callable(cmd):                                # a Python step: fn(src, dst) -> rc
                t1 = time.time()
                rc, secs = cmd(cur, dst), round(time.time() - t1, 1)
            else:
                rc, secs = runner(cmd, log, cap_s=cap, status_path=spath, step=name)
        rows.append({"step": name, "rc": rc, "s": secs, "cap_s": round(cap, 1), "board": dst,
                     "log": log, "summary": dst[:-10] + ".json" if name in ("diff", "route", "rails", "rails-pour", "pairgroups") else None})
        if rc not in (0, 3) or not os.path.exists(dst):
            tail = open(log, errors="replace").read().splitlines()[-8:] if os.path.exists(log) else []
            left = RC.open_by_net(cur)
            _status(spath, "failed", rows, left, rc)
            return {"verdict": "fail", "board": cur, "line": f"krt-whole: step {name} rc={rc} after {secs}s (total budget {budget:g}s)",
                    "next": tail, "steps": rows}
        changed = _restore_project(board, dst)
        if changed:
            rows[-1]["router_project_discarded"] = changed
            discarded.append({"step": name, "sections": changed})
        cur = dst
    shutil_copy(cur, best)
    post, post_error = None, None
    if ((c.cons or {}).get("route") or {}).get("whole_post", True):
        keep = best + ".prepost"
        shutil_copy(best, keep)
        try:
            post = _post(best, d, planes, R)
        except Exception as e:                               # snap / plane ties failed: the routed board stands, flagged
            shutil_copy(keep, best)
            post_error = {"type": type(e).__name__, "message": str(e)[:300]}
        finally:
            if os.path.exists(keep):
                os.remove(keep)
    pour = {"added": [], "dropped": False, "skipped": {}}     # D71cj D: rail corridor pours, guarded; before the report below
    try:
        pour = PWR.pour_rails(best, c.cons, os.path.join(d, "poured.kicad_pcb"), layers=nl)
        if pour["added"]:
            shutil_copy(os.path.join(d, "poured.kicad_pcb"), best)
    except Exception as e:                                   # noqa: BLE001 - the routed board stands without the pour
        pour["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    rails_rec = {"routed": rail_nets, "widths": {n: rails[n] for n in rail_nets}, "error": rails_error,
                 "pour": pour, "rail_refused": _rail_refused(rail_nets, rows), "rail_inner_mm": _rail_inner(best, rail_nets)}
    left = RC.open_by_net(best)
    try:
        from . import qualaudit as QA, routediag as RD, netclass as NC
        Bq = RD.load(best)
        quality = QA.status_fields(Bq, NC.classify(Bq, c.cons, getattr(c, "spec", None)), c.cons, set(left), set(names), board=best)
    except Exception as e:
        quality = {"not_done": {"count": 1, "pairs": [], "rf": [], "audit_error": str(e)[:120]},
                   "nets_routed": 0, "nets_open0": len(names)}
    line = f"krt-whole: sizes " + "/".join(f"{v:g}" for v in sizes.values()) + f" (track/clr/via/drill); {sum(left.values())} unconnected in {sum(r['s'] for r in rows):.0f}s, budget {budget:g}s, steps " + ", ".join(f"{r['step']}={r['rc']}" for r in rows)
    if discarded:
        line += "; router project rules discarded: " + ", ".join(f"{d['step']} ({'/'.join(d['sections'])})" for d in discarded)
    if post:
        line += f"; snap {post['snap']['moved']} end(s), {post['snap']['centred_left']} off-centre left; guarded plane ties {post['ties']['placed']}"
    if post_error:
        line += f"; POST ERROR {post_error['type']}: {post_error['message']} (routed board kept, cleanup skipped)"
    missed, already_done = [], []
    try:                                                    # pairs the external pair router did not see or did not route coupled
        from . import pairs as PRS, pairaudit as PA, routediag as RD2, netclass as NC2
        drow = next((r for r in rows if r["step"] == "diff"), None)
        if drow:
            Bm = RD2.load(best)
            missed = PRS.crosscheck_external(PA.pair_list(NC2.classify(Bm, c.cons, getattr(c, "spec", None))),
                                             summary=drow.get("summary"), log=drow.get("log"))
            missed, already_done = _partition_missed(missed, rep, board, names)
    except Exception as e:                                  # noqa: BLE001 - a report, never a reason to lose the board
        missed = [{"pair": "?", "reason": "crosscheck_error", "detail": str(e)[:120]}]
    if already_done:
        line += f"; pairs already done {len(already_done)} pair(s), not missed"
    if missed:
        line += f"; pair router missed {len(missed)} pair(s): " + ", ".join(str(m.get("pair")) for m in missed[:6]) + " (use `pairs --only`)"
    if pour["added"]:
        line += f"; poured {len(pour['added'])} rail zone(s): {', '.join(pour['added'])}"
    elif pour["dropped"]:
        line += f"; rail pour dropped ({pour['dropped_reason']})"
    if rails_rec["rail_refused"]:
        line += "; rail_refused: " + ", ".join(rails_rec["rail_refused"])
    if quality["not_done"]["count"]:
        line += f"; NOT DONE {quality['not_done']['count']} pair/RF net(s) out of tolerance (routed {quality['nets_routed']}/{quality['nets_open0']})"
    json.dump({"line": line, "sizes": sizes, "budget_s": budget, "steps": rows, "open": left, "diff_nets": diff, "planes": planes, "power": power, "post": post, "post_error": post_error, "quality": quality, "pairs_missed": missed, "pairs_already_done": already_done, "router_project_discarded": discarded, "rails": rails_rec},
              open(os.path.join(d, "result.json"), "w"), indent=1)
    if left:
        line += "; open: " + ", ".join(sorted(left))
    unfinished = bool(left) or bool(quality["not_done"]["count"]) or bool(post_error)
    _status(spath, "warn" if unfinished else "done", rows, left, 0, extra=dict(quality, pairs_missed=missed, pairs_already_done=already_done, **({"post_error": post_error} if post_error else {})))
    return {"verdict": "warn" if unfinished else "pass", "open": left, "board": best, "line": line, "steps": rows, "post": post, "post_error": post_error, "quality": quality, "rails": rails_rec}


def _rail_refused(rail_nets, rows):
    """Rail nets KRT refused a neck-down for (--escalation off): the refusal records in the rail-carrying steps' summaries."""
    import json
    hit = set()

    def walk(x, owner=None):
        if isinstance(x, dict):
            if "pad_entry_refused" in x:
                hit.add(str(x.get("net") or owner))
            for k, v in x.items():
                walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, owner)
    for r in rows:
        if r["step"] in ("rails", "rails-pour", "route", "diff") and r.get("summary") and os.path.exists(r["summary"]):
            try:
                walk(json.load(open(r["summary"])))
            except (OSError, ValueError):
                pass
    return sorted(n for n in hit if n in set(rail_nets))


def _rail_inner(board, rail_nets):
    from . import power as PWR
    try:
        return PWR.rail_inner_mm(board, rail_nets)
    except Exception as e:                                   # noqa: BLE001
        return {"error": str(e)[:120]}


def shutil_copy(src, dst):
    import shutil
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy(src, dst)
    _carry(src, dst)


def _diffnamed(p, n):
    return any(x.endswith(a) and y == x[:-len(a)] + b for x, y in ((p, n), (n, p)) for a, b in (("_P", "_N"), ("-P", "-N"), ("P", "N"), ("+", "-"), ("_DP", "_DM")))


def _all_nets(board):
    return [str(x) for x in _pcb().LoadBoard(board).GetNetsByName().keys() if str(x)]
