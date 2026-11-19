"""`fluxplace fanout BOARD --constraints C --out O [--parts REFS] [--check-only]`: REAL escape copper for every pin that
needs to leave its footprint, locked and tagged, so a placement is acceptable iff the whole fan-out exists.

Which pins (policy lives here; geometry and search in fanout_helper.py, run under /usr/bin/python3):
  signal  every signal pin of a fine-pitch part (escapeplan.is_fine: >= fine_pads pads or pitch <= fine_pitch_mm, >= 3 pads);
          pad-direct out of the courtyard, or dog-bone stub + via; pair legs share kind, layer, stub length and via count.
  plane   every pin of a net that has a plane or declared pour (GND, +5V planes, `[[plan.pour]]`): shortest legal stub + via,
          away from the IC pin it decouples; exposed GND pads of ICs (U*) get vias on a grid inside the pad (the only vias allowed in a pad).
  none    two-pad passives whose nets are both local, mounting hardware, through-hole plane pins (the barrel reaches the plane).
Assignment: candidates are generated against the exact pcbnew shapes of the board (pads, tracks, vias, holes, keep-outs,
outline), conflicts between candidates are capsule distances, one candidate per pin is chosen by a deterministic
constraint search (MRV + forward checking, node budget). FANOUT_OK = every required pin escaped AND (signal pins) a free
track lane >= --lane mm leaves its end point on a signal layer. Items are grouped `fanout:<ref>` and locked.
Self-check: kicad-cli pcb drc on input and output; the fan-out must add no error.
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fanout_helper.py")
MARK = "@@FANOUT@@"


def policy(B, cons, spec, refs=None):
    """(job without params, plane map) from the board text model: modes per part, per-net facts."""
    from . import escapeplan as EP, netclass as NC
    cls = NC.classify(B, cons, spec)
    pr = EP.plan_params(cons)
    pn = EP.plane_nets(B, cons, cls, pr)
    pours = {}
    for pz in EP.planned_pours(cons):
        pours.setdefault(pz["net"], []).append(pz)
    pads = NC.net_pads(B)
    nets = {}
    for n, ps in pads.items():
        info = cls.get(n, {})
        e = {"npads": len(ps), "controlled": info.get("class") in ("pair", "rf", "clock"), "pair": info.get("partner")}
        if NC.is_ground(n):
            e["ground"] = True
        if n in pn:
            e["plane"] = True
            pz = pours.get(n)
            if pz and not any(z["region"] == "board" for z in pz) and not (pn[n]["layers"] and not pn[n].get("planned")):
                e["region"] = pz[0]["region"] if len(pz) == 1 else None
        nets[n] = e
    parts = {}
    for ref, p in B["parts"].items():
        if refs and ref not in refs:
            continue
        if EP.is_mech(ref) or p["n_pads"] == 0:
            continue
        fine = EP.is_fine(p, pr) and p["n_pads"] >= 3
        parts[ref] = {"mode": "all" if fine else "plane", "pads": p["n_pads"]}
    # decoupling: via on the far side of a cap from the IC pin it serves
    away = {}
    ic_pads = {}
    for n, ps in pads.items():
        if n in pn:
            ic_pads[n] = [q for q in ps if B["parts"][q["ref"]]["n_pads"] >= 5]
    for n, ps in pads.items():
        if n not in pn or n not in ic_pads:
            continue
        for q in ps:
            if B["parts"][q["ref"]]["n_pads"] != 2:
                continue
            best = None
            for r in ic_pads[n]:
                d = ((r["x"] - q["x"]) ** 2 + (r["y"] - q["y"]) ** 2) ** 0.5
                if d < 4.0 and (best is None or d < best[0]):
                    best = (d, r)
            if best and best[0] > 1e-6:
                r = best[1]
                away[f"{q['ref']}.{q['num']}"] = [(r["x"] - q["x"]) / best[0], (r["y"] - q["y"]) / best[0]]
    return {"parts": parts, "nets": nets, "away": away,
            "signal_layers": EP.plan_layers(B, cons)[0], "pr": pr}


def owned_nets(B):
    """nets another stage already finished: every connection closed and all of their copper locked (pairs laid before the
    fan-out).  Their pins get no generic stub or via; their copper is an obstacle like any other."""
    from . import routeconn as RC
    opn = RC.text_open(B)
    cu = {}
    for s in B["segs"]:
        cu.setdefault(s["net"], []).append(s["locked"])
    npads = {}
    for p in B["pads"]:
        if p["net"] and not p.get("np"):
            npads[p["net"]] = npads.get(p["net"], 0) + 1
    return sorted(n for n, lk in cu.items() if n and all(lk) and npads.get(n, 0) >= 2 and not opn.get(n))


def params(a, pr, board=None, cons=None):
    """Fan-out sizes resolved exactly like the route stage: CLI/planner preferences raised to route/fab/project floors."""
    from . import routerules as RR
    P = _params(a, pr)
    R = RR.rules(cons, board, track=P["track"], clearance=P["clear"], via=P["via"], drill=P["drill"])
    P.update(track=R["track"], clear=RR.request_clearance(R), via=R["via"], drill=R["drill"])
    return P


def _params(a, pr):
    P = {"track": a.track or max(pr["track_mm"], 0.15 if pr["track_mm"] < 0.15 else pr["track_mm"]),
         "clear": a.clearance or max(pr["clearance_mm"], 0.15 if pr["clearance_mm"] < 0.15 else pr["clearance_mm"]),
         "via": a.via or pr["via_mm"], "drill": a.drill or pr["drill_mm"], "via_pad_gap": a.via_pad_gap, "reach": a.reach, "lane": a.lane,
         "max_dom": a.max_dom, "per_dir": 3, "nodes": a.nodes, "repairs": a.repairs, "via_cost": 0.6, "pair_len_tol": 0.2,
         "thermal_area": 2.0, "thermal_pitch": 1.0, "thermal_edge": 0.2, "edge": 0.0}
    return P


def drc_counts(board, workdir, timeout=900):
    out = os.path.join(workdir, os.path.basename(board) + ".drc.json")
    t0 = time.time()
    subprocess.run(["kicad-cli", "pcb", "drc", "--format", "json", "--all-track-errors", "--refill-zones", "-o", out, board],
                   capture_output=True, text=True, timeout=timeout)
    from . import drcfull as DF
    d = DF.complete(board, json.load(open(out)), fill=True)
    # errors, plus hole_to_hole: a board often keeps it a warning (it is a fab rule all the same - drills break into each other)
    c = Counter(v["type"] for v in d.get("violations", []) if v.get("severity") == "error" or v["type"] == "hole_to_hole")
    return c, len(d.get("unconnected_items", [])), time.time() - t0


def _drop_illegal_claims(board, job, claimed, wd, log):
    """reserved escapes whose via or stub lies in a keep-out / rule area (board or footprint) or inside the board-edge clearance
    are dropped (those pins escape through the normal, fully checked fan-out); the reason is logged."""
    jp = os.path.join(wd, "claims.json")
    json.dump({"params": job["params"], "signal_layers": job["signal_layers"], "parts": {}, "nets": {}, "claims": claimed}, open(jp, "w"))
    r = subprocess.run(["/usr/bin/python3", HELPER, jp, board, os.path.join(wd, "unused.kicad_pcb")], capture_output=True, text=True)
    line = [l for l in r.stdout.splitlines() if l.startswith(MARK)]
    if not line:
        log("fanout: reserved-escape legality check failed: " + (r.stderr or r.stdout)[-200:])
        return claimed
    bad = json.loads(line[-1][len(MARK):])["bad"]
    for k, why in sorted(bad.items(), key=lambda kv: int(kv[0])):
        c = claimed[int(k)]
        log(f"fanout: reserved escape {c['ref']}.{c['pin']} dropped ({why})")
    out = []
    for i, c in enumerate(claimed):
        if str(i) in bad:
            continue
        c = dict(c)
        lane = [list(q) for q in c["lane"]]
        # the stub must END at the via centre (KiCad: track_not_centered_on_via); the raster lane stops a cell short of it
        if lane and 0 < ((lane[-1][0] - c["site"][0]) ** 2 + (lane[-1][1] - c["site"][1]) ** 2) ** 0.5 < c["via"] / 2:
            lane[-1] = list(c["site"])
        elif lane and lane[-1] != list(c["site"]) and ((lane[-1][0] - c["site"][0]) ** 2 + (lane[-1][1] - c["site"][1]) ** 2) ** 0.5 >= c["via"] / 2:
            lane.append(list(c["site"]))
        c["lane"] = lane
        out.append(c)
    return out


def lay_breakout(board, cons, spec, wd, log=print):
    """The breakout templates of the dense parts (breakout.py), solved on the whole board and laid as locked lanes + vias BEFORE any
    other escape: -> (board path, report dict)."""
    from . import breakout as BO, routediag as RD
    B = RD.load(board)
    if not BO.conf(cons)["enabled"]:
        return board, None
    pr = BO.params(cons, B)
    res = BO.solve(B, cons, spec, pr=pr)
    for p in res["pins"].values():
        p["pts"] = [p["pad"]] + p["pts"]                  # the lane starts inside its pad
    if not res["pins"]:
        return board, res
    txt, ns, nv = BO.lay_text(open(board, errors="surrogateescape").read(), res["pins"], pr["drill"], pr["via"])
    dst = os.path.join(wd, "breakout.kicad_pcb")
    with open(dst, "w", errors="surrogateescape") as fh:
        fh.write(txt)
    for ext in (".kicad_pro", ".kicad_dru"):
        side = os.path.splitext(board)[0] + ext
        if os.path.exists(side):
            shutil.copy(side, os.path.splitext(dst)[0] + ext)
    res["laid"] = {"segments": ns, "vias": nv}
    log(f"fanout: breakout templates laid: {len(res['pins'])} pins of {len(res['dense'])} dense parts, {ns} lane segments, {nv} vias; {len(res['failed'])} pins without an escape")
    return dst, res


def run(board, cons, spec, out, a, refs=None, log=print, _drop=frozenset(), _depth=0):
    from . import routediag as RD
    bo_res = None
    if not a.check_only and not refs and not _depth and not getattr(a, "no_breakout", False):
        wd0 = tempfile.mkdtemp(prefix="breakout-")
        board, bo_res = lay_breakout(board, cons, spec, wd0, log)
    B = RD.load(board)
    pol = policy(B, cons, spec, refs)
    from . import routerules as RR
    job = {"params": params(a, pol["pr"], board, cons), "parts": pol["parts"], "nets": pol["nets"], "away": pol["away"],
           "signal_layers": pol["signal_layers"], "check_only": bool(a.check_only)}
    job["params"]["share_reach"] = float(((cons or {}).get("escape") or {}).get("share_reach_mm", 1.5))
    job["params"]["surface_exit"] = float(((cons or {}).get("escape") or {}).get("surface_exit_mm", 1.0))
    job["params"]["same_net_via"] = getattr(a, "same_net_via_spacing", None) or RR.same_net_via_spacing(cons)
    if getattr(a, "nets", None):
        job["only_nets"] = [n for n in a.nets.split(",") if n]
    job["skip_nets"] = owned_nets(B)
    if getattr(a, "add_zones", False):
        from . import escapeplan as EP
        job["pours"] = [z for z in EP.planned_pours(cons)]
    wd = tempfile.mkdtemp(prefix="fanout-")
    claimed = []
    if not a.check_only and not refs:                   # reserved escapes are claimed FIRST: locked stub + via copper in the input
        from . import escres as ER, routerules as RR
        try:
            claimed = ER.plan(B, cons, spec, snv=job["params"]["same_net_via"], h2h=float((RR._pro_rules(board) or {}).get("min_hole_to_hole") or 0.0))["reserved"] if ER.params(cons)["enabled"] else []
        except Exception as e:
            log(f"fanout: escape reservation skipped: {e}")
        if claimed:
            claimed = _drop_illegal_claims(board, job, claimed, wd, log)
        claimed = [c for c in claimed if (c["ref"], str(c["pin"])) not in _drop]
        if bo_res and bo_res["pins"]:                   # a net that already has breakout copper within reach needs no second reserved via beside it
            near = {}
            for r_ in bo_res["pins"].values():
                if r_.get("via"):
                    near.setdefault(r_["net"], []).append(r_["via"])
            claimed = [c for c in claimed if not any(math.hypot(c["site"][0] - v[0], c["site"][1] - v[1]) < 2.5 for v in near.get(c["net"], ()))]
        if claimed:
            src = os.path.join(wd, "claimed.kicad_pcb")
            txt, _n = ER.claim_text(open(board, errors="surrogateescape").read(), claimed)
            with open(src, "w", errors="surrogateescape") as fh:
                fh.write(txt)
            for ext in (".kicad_pro", ".kicad_dru"):
                side = os.path.splitext(board)[0] + ext
                if os.path.exists(side):
                    shutil.copy(side, os.path.splitext(src)[0] + ext)
            board_in = src
    board_in = locals().get("board_in", board)
    rsv = None
    if not a.check_only and not refs and not job.get("only_nets"):
        # pair-aware: the pairs stage owns the pads of every pair it can plan; its exits (corridor ends, via-pair sites) are reserved copper here
        try:
            from . import pairs as PRS
            rsv = PRS.reserve_for_fanout(board, board_in, cons, spec, wd)
        except Exception as e:                           # noqa: BLE001 - without a plan the fan-out is the generic one
            log(f"fanout: pair exits not reserved: {type(e).__name__}: {e}")
        if rsv:
            board_in = rsv[0]
            job["skip_nets"] = sorted(set(job["skip_nets"]) | set(rsv[2]))
    jp = os.path.join(wd, "job.json")
    json.dump(job, open(jp, "w"))
    t0 = time.time()
    tmp_out = out or os.path.join(wd, "out.kicad_pcb")
    r = subprocess.run(["/usr/bin/python3", HELPER, jp, board_in, tmp_out], capture_output=True, text=True)
    line = [l[l.index(MARK):] for l in r.stdout.splitlines() if MARK in l]      # pcbnew may print on the same line first
    if not line:
        raise SystemExit("fanout: helper failed:\n" + (r.stderr or r.stdout)[-1500:])
    res = json.loads(line[-1][len(MARK):])
    near = _claims_near_blocked(B, res, claimed) if (claimed and not a.check_only and not refs and _depth < 2) else set()
    if near:
        # a reserved escape of another pin (the escape reservation, claimed before the solve) takes the only exit of a blocked
        # pin: release the reservations within reach of the blocked pins and solve again (the owners solve like any pin)
        log("fanout: released reserved escape(s) " + ", ".join(sorted(f"{r}.{q}" for r, q in near)) + " next to blocked pins; re-solving")
        return run(board, cons, spec, out, a, refs, log, _drop=frozenset(_drop) | near, _depth=_depth + 1)
    if out and not a.check_only:                         # blocked pins: routed stub to another via site, shared via, pour, smaller via (fanalt.py)
        try:
            from . import fanalt as FALT
            res["params"] = job["params"]
            res["alternatives"] = FALT.rescue(tmp_out, res, cons, spec)
        except Exception as e:                           # noqa: BLE001 - the solver's result stands
            log(f"fanout: alternatives skipped: {type(e).__name__}: {e}")
    if rsv:
        if out:
            PRS.strip_reservations(tmp_out, rsv[1])
        res["pair_reservations"] = {"items": len(rsv[1]), "nets": len(rsv[2]), "secs": rsv[3]}
    res["runtime_s"] = round(time.time() - t0, 1)
    res["params"] = job["params"]
    res["escapes"] = claimed
    res["waive"], res["waive_fail"] = waivers(cons)
    if bo_res:
        res["breakout"] = {"pins": len(bo_res["pins"]), "dense": bo_res["dense"], "failed": bo_res["failed"], "laid": bo_res.get("laid")}
    return B, res


def _claims_near_blocked(B, res, claimed, reach=2.5):
    """keys (ref, pin) of reserved escapes within `reach` mm of a blocked pin of another pin (an exit the solver lacks)."""
    blocked = {(p["ref"], str(p["pin"])) for p in res["pins"] if not p["ok"]}
    if not blocked:
        return set()
    centres = {}
    for q in B["pads"]:
        k = (q["ref"], str(q["num"]))
        if k in blocked:
            centres.setdefault(k, []).append((q["x"], q["y"]))
    out = set()
    for c in claimed:
        k = (c["ref"], str(c["pin"]))
        sx, sy = c["site"]
        for bk, cs in centres.items():
            if bk == k:
                continue
            if any(math.hypot(sx - x, sy - y) <= reach for x, y in cs):
                out.add(k)
    return out


def waivers(cons):
    """-> (patterns, names). `[fanout] waive = ["REF.PIN: reason", ...]` (regex on REF.PIN). Every REF.PIN that `[floorplan]
    waive_fail` names is waived here too: the same pin must not pass one stage and fail the next. `names` = those REF.PINs
    (the caller prints the notice only for a pin that is actually waived)."""
    pats = [w.split(":", 1)[0].strip() for w in ((cons or {}).get("fanout") or {}).get("waive", []) or [] if w.split(":", 1)[0].strip()]
    names = sorted(set(re.findall(r"(?<![\w.])([A-Za-z][A-Za-z0-9_]*\.\d+)(?![\w.])", ((cons or {}).get("floorplan") or {}).get("waive_fail") or "")))
    pats += [re.escape(n) for n in names]
    return pats, names


def is_waived(ref, num, pats):
    key = f"{ref}.{num}"
    return any(re.fullmatch(p, key) for p in pats)


def strict_surface(cons):
    """`[fanout] surface_only = "fail"`: a SURFACE_ONLY pin is a blocked pin (FAIL) again. The default "warn" keeps it a WARN and
    the claim is then checked by route-ready (FANOUT_PIN_SURFACE_ONLY) and the route stage (FANOUT_PIN_UNROUTED)."""
    return str(((cons or {}).get("fanout") or {}).get("surface_only", "warn")).lower() == "fail"


def summarise(B, res, strict=False):
    """per part: a PIN (ref + pad number) is escaped when every one of its copper components is; the count is of pins.
    A blocked pin named by a waiver (res["waive"]) is `waived`, not `blocked`: it is WARN, not FAIL. SURFACE_ONLY pins are
    `surface` (WARN) unless `strict` (see strict_surface), when they are `blocked`."""
    per = {}
    keys = {}
    pats = res.get("waive") or []
    for p in res["pins"]:
        keys.setdefault((p["ref"], str(p["pin"])), []).append(p)
    for (ref, num), comps in sorted(keys.items()):
        e = per.setdefault(ref, {"req": 0, "ok": 0, "blocked": [], "waived": [], "surface": []})
        e["req"] += 1
        bad = [c for c in comps if not c["ok"]]
        if not bad:
            e["ok"] += 1
        elif is_waived(ref, num, pats):
            e["waived"].append(dict(bad[0], waived=True))
        elif not strict and all(c.get("class") == "SURFACE_ONLY" for c in bad):
            e["surface"].append(dict(bad[0], surface=True))       # no via site, but a free surface run leaves the pad
        else:
            e["blocked"].append(next((c for c in bad if c.get("class") != "SURFACE_ONLY"), bad[0]))
    for t in res.get("thermal", []):
        e = per.setdefault(t["ref"], {"req": 0, "ok": 0, "blocked": [], "waived": [], "surface": []})
        e["req"] += 1
        if t["vias"]:
            e["ok"] += 1
        else:
            e["blocked"].append({"ref": t["ref"], "pin": t["pin"], "net": t["net"], "blocker": "no via site inside the exposed pad", "dist": None})
    return per


def _record(out, cons, spec, B, per, req, ok, blocked, escapes=None, waived=(), surface=()):
    """write the derived facts back to the map next to the output board: planned pours, fan-out groups, which source decided what."""
    from . import escapeplan as EP, facts as FA
    try:
        FA.record(out, pours=EP.planned_pours(cons), sources=FA.resolve(out, B, cons, spec),
                  fanout={"groups": FA.fanout_groups(out), "required": req, "escaped": ok,
                          "parts": {r: [v["ok"], v["req"]] for r, v in sorted(per.items())},
                          "blocked": [f"{p['ref']}.{p['pin']}" for p in blocked][:60],
                          "waived": [f"{p['ref']}.{p['pin']} WAIVED - left for the router" for p in waived][:60],
                          "surface_only": [f"{p['ref']}.{p['pin']} SURFACE_ONLY - left for the router" for p in surface][:60]},
                  escapes=escapes or [])
    except OSError:
        pass


def cmd_fanout(a):
    from . import constraints as C
    spec = json.load(open(a.spec)) if a.spec else None
    cons = C.load(a.constraints)
    from . import questions as QS
    S, cons, spec = QS.guard(a, "fanout", a.board, cons, spec)
    refs = set(x.strip() for x in a.parts.split(",") if x.strip()) if a.parts else None
    out = None if a.check_only else (a.out or os.path.splitext(a.board)[0] + ".fanout.kicad_pcb")
    if out and os.path.abspath(out) == os.path.abspath(a.board):
        raise SystemExit("fanout: --out must differ from the input board")
    B, res = run(a.board, cons, spec, out, a, refs)
    per = summarise(B, res, strict_surface(cons))
    req = sum(v["req"] for v in per.values())
    ok = sum(v["ok"] for v in per.values())
    blocked = [p for v in per.values() for p in v["blocked"]]
    drc = None
    if out and not a.no_drc:
        from . import drcguard as DG
        # copper-rule errors only: connectivity (unconnected items) is the open-connection count's, one definition for reports
        keep = lambda v: v.get("severity") == "error" or v["type"] == "hole_to_hole"
        t0 = time.time()
        k0, (c0, u0) = DG.drc_keys(a.board, keep=keep, unconnected=False)
        k1, (c1, u1) = DG.drc_keys(out, keep=keep, unconnected=False)
        added = dict(Counter(k[0] for k in DG.new_keys(k0, k1)))
        drc = {"before": c0, "after": c1, "delta": added, "secs": round(time.time() - t0, 1)}
    waived = [p for v in per.values() for p in v["waived"]]
    surface = [p for v in per.values() for p in v["surface"]]
    for p in waived:
        if f"{p['ref']}.{p['pin']}" in res.get("waive_fail", []):
            print(f"fanout: [floorplan] waive_fail names {p['ref']}.{p['pin']}: applied to the fan-out stage (WAIVED, left for the router)")
    fail_drc = bool(drc and drc["delta"])
    verdict = ("FANOUT_FAIL" if blocked else "FANOUT_DRC" if fail_drc else "FANOUT_WARN" if (waived or surface) else "FANOUT_OK")
    nitems = sum(1 for p in res["pins"] if p["ok"]) + sum(1 for p in res["pins"] if p["ok"] and p.get("via"))
    print(f"fanout: {verdict} - {ok}/{req} required pins escaped on {len(per)} parts, {len(blocked)} blocked, {len(waived)} waived, {len(surface)} surface-only; "
          f"{sum(len(t['vias']) for t in res.get('thermal', []))} thermal vias; {res['runtime_s']} s "
          f"(domains {res['time']['domains']}, arcs {res['time']['arcs']}, solve {res['time']['solve']}, repairs {res['repairs']})")
    if res.get("breakout"):
        bo = res["breakout"]
        print(f"fanout: breakout templates: {bo['pins']} pins of {len(bo['dense'])} dense parts laid ({bo['laid']}), {len(bo['failed'])} without an escape")
        for f in bo["failed"][: (len(bo["failed"]) if a.full_output else 10)]:
            print(f"BREAKOUT_PIN_BLOCKED {f['msg']}")
    if res.get("alternatives") and res["alternatives"]["tried"]:
        al = res["alternatives"]
        print(f"fanout: alternatives for {al['tried']} blocked pin(s): routed via {al['via']}, shared via {al['share']}, pour {al['pour']}, "
              f"small via {al['small-via']}, left {al['left']}")
    if drc:
        print(f"fanout: DRC errors before {drc['before']} after {drc['after']}; added {drc['delta'] or 'none'} ({drc['secs']} s)")
    lim = len(per) if a.full_output else min(len(per), 12 if not a.top else a.top)
    worst = sorted(per.items(), key=lambda kv: (-(kv[1]["req"] - kv[1]["ok"]), kv[0]))
    for ref, v in worst[:lim]:
        if v["req"] - v["ok"] or a.full_output:
            print(f"  {ref}: {v['ok']}/{v['req']}")
    for p in blocked[: (len(blocked) if a.full_output else 30)]:
        d = "" if p.get("dist") is None else f" {p['dist']} mm"
        print(f"FANOUT_PIN_BLOCKED {p['ref']}.{p['pin']} {p['net']} {p.get('blocker', '?')}{d} [{p.get('class', '?')}, free run {p.get('free_mm', 0)} mm]")
    for p in waived:
        d = "" if p.get("dist") is None else f" {p['dist']} mm"
        print(f"FANOUT_PIN_WAIVED {p['ref']}.{p['pin']} {p['net']} {p.get('blocker', '?')}{d} - WAIVED - left for the router")
    for p in surface:
        print(f"FANOUT_PIN_SURFACE_ONLY {p['ref']}.{p['pin']} {p['net']} free run {p.get('free_mm')} mm - left for the router")
    rp = (out or a.board)
    rp = os.path.splitext(rp)[0] + ".fanout.json"
    json.dump({"verdict": verdict, "required": req, "escaped": ok, "per_part": {k: {"req": v["req"], "ok": v["ok"]} for k, v in per.items()},
               "blocked": blocked, "waived": waived, "surface": surface, "pins": res["pins"], "thermal": res["thermal"], "drc": drc, "time": res["time"],
               "runtime_s": res["runtime_s"], "params": res["params"]}, open(rp, "w"), indent=1)
    print(f"full report: {rp}")
    if out:
        S.mark(out)
        _record(out, cons, spec, B, per, req, ok, blocked, res.get("escapes"), waived, surface)
        print(f"wrote {out}")
    return 0 if verdict in ("FANOUT_OK", "FANOUT_WARN") else 1


def is_blocked_only(text):
    """True when a fanout run FAILed only on pins it could not escape (no DRC increase, no crash)."""
    lines = text.splitlines()
    return any(l.startswith("fanout: FANOUT_FAIL") for l in lines) and not any(
        l.startswith("fanout: FANOUT_DRC") or "Traceback" in l for l in lines)


def add_parser(sub):
    p = sub.add_parser("fanout", help="real, locked escape copper for every pin that must leave its footprint; "
                                      "FANOUT_OK only when every one of the required pins have an end point in open space")
    p.add_argument("board")
    p.add_argument("--constraints", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--out", default=None, help="output board (default <board>.fanout.kicad_pcb)")
    p.add_argument("--parts", default=None, help="comma-separated refs (default every part)")
    p.add_argument("--check-only", action="store_true", help="report only; write nothing")
    p.add_argument("--nets", default=None, help="comma-separated nets: escape only the pins of these nets")
    p.add_argument("--no-drc", action="store_true", help="skip the kicad-cli DRC self-check")
    p.add_argument("--no-breakout", dest="no_breakout", action="store_true", help="do not lay the breakout templates of the dense parts first")
    p.add_argument("--add-zones", dest="add_zones", action="store_true",
                   help="also write the declared pours (`[[plan.pour]]`) as zones (priority 0, clearance 0.2, solid connection)")
    p.add_argument("--track", type=float, default=None)
    p.add_argument("--clearance", type=float, default=None)
    p.add_argument("--via", type=float, default=None)
    p.add_argument("--drill", type=float, default=None)
    p.add_argument("--same-net-via-spacing", dest="same_net_via_spacing", choices=["freerouting", "hole"], default=None,
                   help="same-net via spacing: freerouting = via + clearance, hole = hole-to-hole only (default: [route] same_net_via_spacing, else by router)")
    p.add_argument("--via-pad-gap", dest="via_pad_gap", type=float, default=0.2, help="copper gap between a via and its own pad (default 0.2)")
    p.add_argument("--reach", type=float, default=2.2, help="largest stub length mm (default 2.2)")
    p.add_argument("--lane", type=float, default=1.0, help="free lane length mm leaving every end point (default 1.0)")
    p.add_argument("--max-dom", dest="max_dom", type=int, default=60)
    p.add_argument("--nodes", type=int, default=200000, help="search node budget per component")
    p.add_argument("--repairs", type=int, default=12, help="lane repair rounds")
    p.add_argument("--full", dest="full_output", action="store_true")
    p.add_argument("-q", "--quiet", dest="quiet", action="store_true")
    p.add_argument("--top", type=int, default=None)
    from . import questions as QS
    QS.add_args(p)
    p.set_defaults(fn=cmd_fanout)
