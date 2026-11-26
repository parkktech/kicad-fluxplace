"""route stage, `[route] router = "krt-whole"`: the four-call KiCadRoutingTools chain, nothing else.

  1-2  route_planes.py  one call per plane (net, layer) of the board's zones
  r    [power] rails    spine routes of the [power] rails at their width (PHASE3B)
  p    pairgroups       Python step (PHASE3C): failed pairs re-laid per connector group; skipped when none is incomplete
  3    route_diff.py    the pair nets that are still open
  rf   route.py         the RF nets (the [rf] nets and the nets named as RF) before the rest: a plain net never takes their room
                        (PLAN-PLACE-GAPS phase 2: pairs and rails were already ahead of plain nets, RF nets were not)
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
    from . import budget as B
    legs = B.pair_legs(names, cons)
    fams = (cons or {}).get("pairs") or {}
    return sorted(x for x in names if x in legs and B.family_of(fams, x, legs) and x not in nopair)


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


def cmd_diff(py, rdir, src, dst, nets, sig, base, summary=None, width=None, gap=None):
    """route_diff over `nets` on `sig`. PHASE3G G3b: width / gap (mm) replace the signal width of `base` for this call."""
    if width is not None:
        base = _without_width(base)
        base = [*base, "--track-width", f"{width:g}", *(["--diff-pair-gap", f"{gap:g}"] if gap is not None else [])]
    cmd = [py, os.path.join(rdir, "py_router", "route_diff.py"), src, dst, "--nets", *nets, "--layers", *sig, *base, "--keep-input-copper"]
    return cmd + (["--json-out", summary] if summary else [])


def _without_width(base):
    out, skip = [], False
    for x in base:
        if skip:
            skip = False
            continue
        if x == "--track-width":
            skip = True
            continue
        out.append(x)
    return out


def diff_groups(cons, board, nets, sig):
    """PHASE3G G3b: [(nets, layers, width | None, gap | None)], one route_diff call per group. A controlled pair family
    is routed at its table width (zgeom.family_table on the board's stackup) on the layers whose width and gap equal the
    outer layer's, so inner layers no longer get the signal leg width. width None: the signal width (no family, or no
    stackup to grade against)."""
    from . import zgeom as ZG, pairs as PS, routediag as RD, review as RV
    rows, planes = [], []
    try:
        rows = RV.parse_stackup_text(open(board, errors="replace").read())
        planes = PS.pair_layers(RD.load(board), cons)[1]
    except Exception:                                    # noqa: BLE001 - no stackup: signal width everywhere (the old call)
        rows = []
    groups = {}
    for n in sorted(nets):
        fam = ZG.family_of(cons, n) if rows else None
        tab = ZG.family_table(cons, rows, planes, sig, fam, board=board) if fam else {}
        if not tab or sig[0] not in tab:
            key = (None, None, tuple(sig))
        else:
            o = tab[sig[0]]
            lay = tuple(l for l in sig if l in tab and tab[l]["w"] == o["w"] and tab[l]["gap"] == o["gap"])
            key = (round(o["w"], 4), round(o["gap"], 4), lay)
        groups.setdefault(key, []).append(n)
    return [(ns, list(k[2]), k[0], k[1]) for k, ns in groups.items()]


def cmd_route(py, rdir, src, dst, sig, base, power, summary=None, nets=("*",), extra=()):
    pw = ["--power-nets", *[glob_escape(n) for n in power], "--power-nets-widths", *[f"{w:g}" for w in power.values()]] if power else []
    cmd = [py, os.path.join(rdir, "py_router", "route.py"), src, dst, "--nets", *[glob_escape(n) if n != "*" else n for n in nets],
           *pw, "--layers", *sig, *base, *list(extra), "--keep-input-copper"]
    return cmd + (["--json-out", summary] if summary else [])


def base_flags(R, d):
    """the recipe's RULES: sizes from the rules, escalation off, strict sizes, the floor file."""
    fo = RR.write_overrides(os.path.join(d, "router-fab.txt"), R)
    return [*RR.size_args(R), "--escalation", "off", "--strict-sizes", *(["--fab-overrides", fo] if fo else [])]


NO_RIP_STEPS = ("rf", "route")      # steps that route over the copper of earlier steps: they must not rip it


def no_rip_env():
    """The router's environment with its pre-existing-net rip-up off: KRT registers unlocked pre-existing nets (<= 30 segments, <= 6 vias)
    as rip candidates, and --keep-input-copper only guards its cleanup, so a later step could rip an earlier step's rf / pair / rail copper."""
    return dict(os.environ, KICAD_RIP_PREEXISTING="0")


def run_step(cmd, log, cap_s=CAP_S, status_path=None, step=None, env=None):
    """-> (rc, seconds); rc 124 on the cap, -N when killed by a signal.  Never retried."""
    from . import routestatus as RS
    t = time.time()
    try:
        r = RS.run_logged(cmd, cwd=os.path.dirname(os.path.dirname(cmd[1])), timeout=cap_s, out=cmd[3], log_path=log, env=env,
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


def _post(best, d, planes, R, protect=()):
    """The same guarded cleanup the batch route gets: via-centre snap, plane ties, then a saved fresh fill.
    Ends the snap leaves (clearance-refused) go to the via-to-end move, then the joiner (the close-stage fallbacks); each is DRC-guarded,
    lock-respecting and skips the `protect` nets (the manifest's nets), the last fallback (centre) included."""
    from . import snapiter as SI, planetie as PT, opengate as OG
    snap = SI.run(best, best, log=lambda _m: None)
    if snap.get("centred_left") != 0:
        for key, fn in (("via", SI.via_run), ("joiner", SI.joiner_run), ("centre", SI.centre_run)):
            try:
                snap[key] = fn(best, best, log=lambda _m: None, protect=list(protect))      # W46: the centre move too (it moved locked copper of 25 protected nets: PROTECTED_CHANGED at the stage commit)
            except Exception as e:                           # noqa: BLE001 - a fallback failure keeps the snapped board
                snap[key] = {"error": str(e)[:160]}
                break
            if snap[key].get("centred_left") == 0:
                break
        left = [r.get("centred_left") for r in (snap, snap.get("via") or {}, snap.get("joiner") or {}, snap.get("centre") or {}) if r.get("centred_left") is not None]
        snap["centred_left"] = left[-1] if left else snap.get("centred_left")
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


def route_net_set(allnets, failed_legs, hs_block, still_open):
    """PHASE3I F1: the nets the whole-board router may take. `("*",)` only when nothing is excluded; else every net but the
    pairgroups failures and the gated pair legs still open (a pair the pairs steps completed is no longer excluded)."""
    excluded = set(failed_legs) | (set(hs_block) & set(still_open))
    return ("*",) if not excluded else sorted(set(allnets) - excluded)


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
    xtra = list(getattr(c, "krt_extra", None) or [])         # D71ct: variant flags, route.py calls only (not route_diff/planes)
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
    hs_block = PFB.gated_open_legs(c.cons, pn, names, hand)          # PHASE3I F1: gated pair legs are the pair builder's, not KRT's
    diff = [x for x in diff if x not in hs_block]
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
    ladder_rec = {}                                          # W34: net -> [{width, layers, connected}] per rung, for the result
    if pour_rail_nets:
        steps.append(("rails-pour", lambda s0, o0: lambda s, o: _rails_ladder(a, c, base, rails, pour_rail_nets, sig, s, o, xtra, ladder_rec, runner, spath)))
    if other_rail_nets:
        steps.append(("rails", lambda s, o: cmd_route(a.router_py, a.router_dir, s, o, list(sig), base, rails, o[:-10] + ".json", nets=other_rail_nets, extra=xtra)))
    from . import railbranch as RB                           # W46: rail branches at each connection's D71da width; the plain step skips rail nets
    try:
        branch_nets = RB.branch_nets(c.cons, {x for x, _l in planes})
    except Exception:                                        # noqa: BLE001 - no [power] facts: no branch step
        branch_nets = []
    branch_rec = {}
    if branch_nets:
        def _branch(s, o):
            mk = lambda src, out, lay, widths, ns: cmd_route(a.router_py, a.router_dir, src, out, lay, base, widths, out[:-10] + ".json", nets=ns, extra=xtra)
            rn = lambda cmd, log, cap_s: runner(cmd, log, cap_s=min(CAP_S, cap_s), status_path=spath, step="rails-branch", env=no_rip_env())
            try:
                return RB.route(s, o, c.cons, branch_nets, R_TRACK(base, a), nl, sig, mk, rn, lambda: budget - (time.time() - t0), branch_rec)
            except Exception as e:                           # noqa: BLE001 - a failed branch step leaves the board as it was; reported
                branch_rec["error"] = f"{type(e).__name__}: {str(e)[:160]}"
                shutil_copy(s, o)
                return 0
        steps.append(("rails-branch", lambda s, o: _branch))
    failed_legs = set()                                      # C.2: legs of pairs pairgroups could not lay are never plain nets
    def _pairgroups(s, o):
        return _run_pairgroups(c, d, s, o, failed_legs, min(CAP_S, max(0.0, budget - (time.time() - t0))), pair_legs=pn)
    steps.append(("pairgroups", lambda s, o: _pairgroups))   # a callable step: run() calls it, not the router runner
    hs_rec = {"excluded": []}
    def _route_nets(src):
        still = set(RC.open_by_net(src))                     # re-read after pairgroups: a pair it completed is back in
        hs_rec["excluded"] = sorted(hs_block & still)
        got = route_net_set(allnets, failed_legs | set(rf_nets), hs_block, still)
        if not branch_nets:
            return got
        return sorted((set(allnets) if got == ("*",) else set(got)) - set(branch_nets))     # W46: rail nets belong to the rail steps
    if diff:
        for gi, (gnets, glay, gw, ggap) in enumerate(diff_groups(c.cons, board, diff, sig)):
            steps.append(("diff" if gi == 0 else f"diff{gi + 1}",
                          lambda s, o, gnets=gnets, glay=glay, gw=gw, ggap=ggap: cmd_diff(
                              a.router_py, a.router_dir, s, o, gnets, glay, base, o[:-10] + ".json", width=gw, gap=ggap)))
    from . import review as RV
    rfc = set(((c.cons or {}).get("rf") or {}).get("nets") or [])
    try:                                                     # the same class rule as the placement gaps (name, pin names, mate)
        from . import placegaps as PG
        of = PG._net_class(PG.Ctx(stage="route", board=board, cons=c.cons or {}, spec=getattr(c, "spec", None)))
        is_rf_net = lambda n: n in rfc or of(n)[0] == "rf"
    except Exception:                                        # noqa: BLE001 - no board facts: the name rule
        is_rf_net = lambda n: n in rfc or RV.is_rf(n)
    rf_nets = sorted(n for n in allnets if is_rf_net(n) and n not in pn and n not in rail_nets and n not in {x for x, _l in planes})
    rf_rec = {"nets": rf_nets, "layers": [], "widths": {}, "unknown_width": [], "open": [], "routed": []}
    if rf_nets:                                              # RF before plain: one router call on the RF nets, the route step never takes them
        from . import rfroute as RFR
        outer = RFR.outer_layers(sig) or list(sig)           # outer layers over the GND planes first; an unroutable RF net stays open
        rfw = RFR.widths(board, c.cons, rf_nets, outer)
        rf_rec.update(layers=outer, widths=rfw, unknown_width=sorted(n for n in rf_nets if not rfw[n]["w"]))
        rf_power = {n: rfw[n]["w"] for n in rf_nets if rfw[n]["w"]}
        steps.append(("rf", lambda s, o: cmd_route(a.router_py, a.router_dir, s, o, outer, base, rf_power, o[:-10] + ".json", nets=rf_nets, extra=xtra)))
    power = {net: float(((c.cons or {}).get("power") or {}).get(net, {}).get("width_mm") or POWER_W)    # plane nets: taps, not rails
             for net in dict.fromkeys(x for x, _ in planes)}
    power = {**rails, **power}                               # plane entries win for plane nets
    steps.append(("route", lambda s, o: cmd_route(a.router_py, a.router_dir, s, o, sig, base, power, o[:-10] + ".json", nets=_route_nets(s), extra=xtra)))
    if branch_nets:                                          # W46: the plain step's tracks split rail zone fills (KRT does not repair zone nets outside its --nets):
        steps.append(("rails-branch2", lambda s, o: _branch))        # the same class-by-class pass reconnects what it cut, at the same D71da widths
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
                rc, secs = runner(cmd, log, cap_s=cap, status_path=spath, step=name, **({"env": no_rip_env()} if name in NO_RIP_STEPS else {}))
        rows.append({"step": name, "rc": rc, "s": secs, "cap_s": round(cap, 1), "board": dst,
                     "log": log, "summary": dst[:-10] + ".json" if name in ("route", "rails", "rails-pour", "rails-branch", "rails-branch2", "pairgroups", "rf") or name.startswith("diff") else None})
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
            from . import boardwrite as BW
            post = _post(best, d, planes, R, protect=sorted((BW.load_manifest(board) or {}).get("nets") or {}))
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
    wid = {"widened_mm": 0.0, "necks": [], "unreached": {}, "dropped": None}   # W34: narrow rail copper widened / necked per D71da
    try:
        wo = os.path.join(d, "widened.kicad_pcb")
        wid = PWR.widen_guarded(best, c.cons, wo, nets=set(rail_nets) | set(PWR.plan(c.cons, layers=nl)))
        if os.path.exists(wo):
            shutil_copy(wo, best)
    except Exception as e:                                   # noqa: BLE001 - the routed board stands without the widening
        wid["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    mask = {"blocks": 0, "inferred": 0, "skipped": 0, "written": False}   # PHASE3F A7: mask openings re-cut after the copper
    try:
        from . import thermal as TH
        mk = os.path.join(d, "maskcut.kicad_pcb")
        mr = TH.recut(best, c.cons, mk)
        if os.path.exists(mk):
            shutil_copy(mk, best)
            mask.update(blocks=mr["blocks"], inferred=mr["inferred"], skipped=len(mr["skipped"]), written=True)
    except Exception as e:                                   # noqa: BLE001 - the routed board stands without the re-cut
        mask["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    rails_rec = {"routed": rail_nets, "widths": {n: rails[n] for n in rail_nets}, "error": rails_error,
                 "pour": pour, "widen": wid, "ladder": ladder_rec, "rail_refused": _rail_refused(rail_nets, rows), "rail_inner_mm": _rail_inner(best, rail_nets), "branch": branch_rec}
    left = RC.open_by_net(best)
    if rf_nets:
        from . import rfroute as RFR
        rf_rec["routed"], rf_rec["open"] = RFR.split(rf_nets, left)
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
        drows = [r for r in rows if r["step"].startswith("diff")]     # PHASE3G G3b: one diff step per geometry group
        if drows:
            Bm = RD2.load(best)
            plist = PA.pair_list(NC2.classify(Bm, c.cons, getattr(c, "spec", None)))
            per = [PRS.crosscheck_external(plist, summary=r.get("summary"), log=r.get("log")) for r in drows]
            seen_by = [{str(m.get("pair")) for m in ms} for ms in per]
            # a pair is missed only when EVERY diff step missed it (each step sees only its own group's nets)
            missed = [m for m in per[0] if all(str(m.get("pair")) in sb for sb in seen_by)]
            missed, already_done = _partition_missed(missed, rep, board, names)
    except Exception as e:                                  # noqa: BLE001 - a report, never a reason to lose the board
        missed = [{"pair": "?", "reason": "crosscheck_error", "detail": str(e)[:120]}]
    if already_done:
        line += f"; pairs already done {len(already_done)} pair(s), not missed"
    if missed:
        line += f"; pair router missed {len(missed)} pair(s): " + ", ".join(str(m.get("pair")) for m in missed[:6]) + " (use `pairs --only`)"
    if mask["blocks"] or mask["inferred"]:
        line += f"; mask re-cut {mask['blocks'] + mask['inferred']}"
    if pour["added"]:
        line += f"; poured {len(pour['added'])} rail zone(s): {', '.join(pour['added'])}"
    elif pour["dropped"]:
        line += f"; rail pour dropped ({pour['dropped_reason']})"
    if wid.get("widened_mm") or wid.get("necks"):
        line += f"; rails widened {wid.get('widened_mm', 0):g} mm, {len(wid.get('necks') or [])} neck-down(s) proven (D71da)"
    if wid.get("unreached"):
        line += "; rail width NOT reached (placement finding): " + ", ".join(f"{n} {len(v)} run(s) near {','.join(sorted({r for x in v for r in x['refs']})[:4])}" for n, v in sorted(wid["unreached"].items()))
    if branch_rec:
        br, bn, bo = RB.summary({k: v for k, v in branch_rec.items() if isinstance(v, dict)})
        line += f"; rail branches routed {br} ({bn} neck-down(s) proven), {bo} left open at their width" + (f" ({branch_rec['error']})" if branch_rec.get("error") else "")
    if rails_rec["rail_refused"]:
        line += "; rail_refused: " + ", ".join(rails_rec["rail_refused"])
    if quality["not_done"]["count"]:
        line += f"; NOT DONE {quality['not_done']['count']} pair/RF net(s) out of tolerance (routed {quality['nets_routed']}/{quality['nets_open0']})"
    json.dump({"line": line, "sizes": sizes, "budget_s": budget, "steps": rows, "open": left, "diff_nets": diff, "planes": planes, "power": power, "post": post, "post_error": post_error, "quality": quality, "pairs_missed": missed, "pairs_already_done": already_done, "router_project_discarded": discarded, "rails": rails_rec, "rf": rf_rec, "mask": mask},
              open(os.path.join(d, "result.json"), "w"), indent=1)
    if rf_rec["open"]:
        line += (f"; RF FINDING: {len(rf_rec['open'])} RF net(s) not routable on the outer layers at their impedance width, left OPEN "
                 f"(never routed at default width): " + ", ".join(rf_rec["open"]))
    if rf_rec["unknown_width"]:
        line += "; RF width unknown (no stackup / reference plane): " + ", ".join(rf_rec["unknown_width"])
    if hs_rec["excluded"]:
        line += f"; {len(hs_rec['excluded'])} gated pair leg(s) left to the pairs gate: " + ", ".join(hs_rec["excluded"][:6])
    if left:
        line += "; open: " + ", ".join(sorted(left))
    unfinished = bool(left) or bool(quality["not_done"]["count"]) or bool(post_error)
    _status(spath, "warn" if unfinished else "done", rows, left, 0, extra=dict(quality, pairs_missed=missed, pairs_already_done=already_done, **({"post_error": post_error} if post_error else {})))
    return {"verdict": "warn" if unfinished else "pass", "open": left, "board": best, "line": line, "steps": rows, "post": post, "post_error": post_error, "quality": quality, "rails": rails_rec, "rf": rf_rec, "hs_excluded": hs_rec["excluded"]}


def _rails_ladder(a, c, base, rails, nets, sig, src, dst, xtra, rec, runner, spath):
    """W34 (W14 measured ladder, ONE-SHOT-LEDGER 2026-11-19 "rails first"): the pour rails, before everything else, one router
    call per rung: widest first (the rail's IPC outer width, then `[route] power_ladder_mm` 1.4 / 1.0 / 0.6), outer layers above
    1.0 mm and every signal layer at or below it (power.ladder_layers).  A rung routes only the rails the previous rung left
    open, on the board it wrote (a rail keeps its wide copper and finishes narrower, as W14 did).  A single call for a rail with
    one rung.  -> rc; rung 0 writes dst's summary json, later rungs `<dst>.r<k>.json`, `rec` gets {net: [{width, layers, open}]}."""
    from . import power as PWR
    cur, todo, k = src, list(nets), 0
    ladders = {n: PWR.ladder_widths(c.cons, n, rails[n], R_TRACK(base, a)) for n in nets}
    base_json = dst[:-10] + ".json"
    while todo:
        widths = {n: ladders[n][k] for n in todo if k < len(ladders[n])}
        if not widths:
            break
        lows = {n: w for n, w in widths.items() if w <= PWR.LADDER_INNER_MAX_MM + 1e-9 and k > 0}
        groups = [(g, PWR.ladder_layers(c.cons, sig, min(g.values()), fallback=k > 0)) for g in
                  ({n: w for n, w in widths.items() if n not in lows}, lows) if g]
        for gi, (g, lay) in enumerate(groups):
            out_k = f"{dst[:-10]}.r{k}{gi}.kicad_pcb"
            summ = base_json if (k == 0 and gi == 0) else f"{dst[:-10]}.r{k}{gi}.json"
            cmd = cmd_route(a.router_py, a.router_dir, cur, out_k, lay, base, {**rails, **g}, summ, nets=list(g), extra=xtra)
            rc, _s = runner(cmd, dst[:-10] + (".log" if (k == 0 and gi == 0) else f".r{k}{gi}.log"), cap_s=CAP_S, status_path=spath, step="rails-pour")
            if rc not in (0, 3) or not os.path.exists(out_k):
                if k == 0 and gi == 0:
                    return rc
                continue
            _restore_project(src, out_k)
            cur = out_k
            for n, w in g.items():
                rec.setdefault(n, []).append({"width": w, "layers": lay})
        still = RC.open_by_net(cur, fill=False)
        for n in todo:
            if rec.get(n):
                rec[n][-1]["open"] = int(still.get(n, 0))
        todo = [n for n in todo if still.get(n)]
        k += 1
    shutil_copy(cur, dst)
    return 0


def R_TRACK(base, a):
    """the signal track width the rails never go below: the --track-width of the recipe flags."""
    try:
        return float(base[base.index("--track-width") + 1])
    except (ValueError, IndexError):
        return float(getattr(a, "track", 0.15) or 0.15)


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
        if (r["step"] in ("rails", "rails-pour", "route") or r["step"].startswith("diff")) and r.get("summary") and os.path.exists(r["summary"]):
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
