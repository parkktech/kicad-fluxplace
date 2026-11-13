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
import os
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


def params(a, pr, board=None):
    """Fan-out geometry: the planner floors (or the command line), never below the board's own design rules (.kicad_pro)."""
    P = _params(a, pr)
    if board:
        from . import routerules as RR
        rr = RR._pro_rules(board)
        for k, ks in (("track", "min_track_width"), ("clear", "min_clearance"), ("via", "min_via_diameter"),
                      ("drill", "min_through_hole_diameter")):
            if rr.get(ks) and P[k] < float(rr[ks]):
                P[k] = float(rr[ks])
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
    subprocess.run(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-error", "--all-track-errors", "--refill-zones", "-o", out, board],
                   capture_output=True, text=True, timeout=timeout)
    d = json.load(open(out))
    c = Counter(v["type"] for v in d.get("violations", []))
    return c, len(d.get("unconnected_items", [])), time.time() - t0


def run(board, cons, spec, out, a, refs=None, log=print):
    from . import routediag as RD
    B = RD.load(board)
    pol = policy(B, cons, spec, refs)
    job = {"params": params(a, pol["pr"], board), "parts": pol["parts"], "nets": pol["nets"], "away": pol["away"],
           "signal_layers": pol["signal_layers"], "check_only": bool(a.check_only)}
    if getattr(a, "nets", None):
        job["only_nets"] = [n for n in a.nets.split(",") if n]
    if getattr(a, "add_zones", False):
        from . import escapeplan as EP
        job["pours"] = [z for z in EP.planned_pours(cons)]
    wd = tempfile.mkdtemp(prefix="fanout-")
    jp = os.path.join(wd, "job.json")
    json.dump(job, open(jp, "w"))
    t0 = time.time()
    tmp_out = out or os.path.join(wd, "out.kicad_pcb")
    r = subprocess.run(["/usr/bin/python3", HELPER, jp, board, tmp_out], capture_output=True, text=True)
    line = [l for l in r.stdout.splitlines() if l.startswith(MARK)]
    if not line:
        raise SystemExit("fanout: helper failed:\n" + (r.stderr or r.stdout)[-1500:])
    res = json.loads(line[-1][len(MARK):])
    res["runtime_s"] = round(time.time() - t0, 1)
    res["params"] = job["params"]
    return B, res


def summarise(B, res):
    per = {}
    for p in res["pins"]:
        e = per.setdefault(p["ref"], {"req": 0, "ok": 0, "blocked": []})
        e["req"] += 1
        if p["ok"]:
            e["ok"] += 1
        else:
            e["blocked"].append(p)
    for t in res.get("thermal", []):
        e = per.setdefault(t["ref"], {"req": 0, "ok": 0, "blocked": []})
        e["req"] += 1
        if t["vias"]:
            e["ok"] += 1
        else:
            e["blocked"].append({"ref": t["ref"], "pin": t["pin"], "net": t["net"], "blocker": "no via site inside the exposed pad", "dist": None})
    return per


def _record(out, cons, spec, B, per, req, ok, blocked):
    """write the derived facts back to the map next to the output board: planned pours, fan-out groups, which source decided what."""
    from . import escapeplan as EP, facts as FA
    try:
        FA.record(out, pours=EP.planned_pours(cons), sources=FA.resolve(out, B, cons, spec),
                  fanout={"groups": FA.fanout_groups(out), "required": req, "escaped": ok,
                          "parts": {r: [v["ok"], v["req"]] for r, v in sorted(per.items())},
                          "blocked": [f"{p['ref']}.{p['pin']}" for p in blocked][:60]})
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
    per = summarise(B, res)
    req = sum(v["req"] for v in per.values())
    ok = sum(v["ok"] for v in per.values())
    blocked = [p for v in per.values() for p in v["blocked"]]
    drc = None
    if out and not a.no_drc:
        wd = os.path.dirname(os.path.abspath(out))
        c0, u0, t0 = drc_counts(a.board, wd)
        c1, u1, t1 = drc_counts(out, wd)
        added = {k: c1.get(k, 0) - c0.get(k, 0) for k in set(c0) | set(c1) if c1.get(k, 0) != c0.get(k, 0)}
        drc = {"before": dict(c0), "after": dict(c1), "delta": added, "secs": round(t0 + t1, 1)}
    fail_drc = bool(drc and any(v > 0 for v in drc["delta"].values()))
    verdict = "FANOUT_OK" if not blocked and not fail_drc else ("FANOUT_DRC" if not blocked else "FANOUT_FAIL")
    nitems = sum(1 for p in res["pins"] if p["ok"]) + sum(1 for p in res["pins"] if p["ok"] and p.get("via"))
    print(f"fanout: {verdict} - {ok}/{req} required pins escaped on {len(per)} parts, {len(blocked)} blocked; "
          f"{sum(len(t['vias']) for t in res.get('thermal', []))} thermal vias; {res['runtime_s']} s "
          f"(domains {res['time']['domains']}, arcs {res['time']['arcs']}, solve {res['time']['solve']}, repairs {res['repairs']})")
    if drc:
        print(f"fanout: DRC errors before {sum(drc['before'].values())} after {sum(drc['after'].values())}; added {drc['delta'] or 'none'} ({drc['secs']} s)")
    lim = len(per) if a.full_output else min(len(per), 12 if not a.top else a.top)
    worst = sorted(per.items(), key=lambda kv: (-(kv[1]["req"] - kv[1]["ok"]), kv[0]))
    for ref, v in worst[:lim]:
        if v["req"] - v["ok"] or a.full_output:
            print(f"  {ref}: {v['ok']}/{v['req']}")
    for p in blocked[: (len(blocked) if a.full_output else 30)]:
        d = "" if p.get("dist") is None else f" {p['dist']} mm"
        print(f"FANOUT_PIN_BLOCKED {p['ref']}.{p['pin']} {p['net']} {p.get('blocker', '?')}{d}")
    rp = (out or a.board)
    rp = os.path.splitext(rp)[0] + ".fanout.json"
    json.dump({"verdict": verdict, "required": req, "escaped": ok, "per_part": {k: {"req": v["req"], "ok": v["ok"]} for k, v in per.items()},
               "blocked": blocked, "pins": res["pins"], "thermal": res["thermal"], "drc": drc, "time": res["time"],
               "runtime_s": res["runtime_s"], "params": res["params"]}, open(rp, "w"), indent=1)
    print(f"full report: {rp}")
    if out:
        S.mark(out)
        _record(out, cons, spec, B, per, req, ok, blocked)
        print(f"wrote {out}")
    return 0 if verdict == "FANOUT_OK" else 1


def add_parser(sub):
    p = sub.add_parser("fanout", help="real, locked escape copper for every pin that must leave its footprint; "
                                      "FANOUT_OK only when 100 % of the required pins have an end point in open space")
    p.add_argument("board")
    p.add_argument("--constraints", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--out", default=None, help="output board (default <board>.fanout.kicad_pcb)")
    p.add_argument("--parts", default=None, help="comma-separated refs (default every part)")
    p.add_argument("--check-only", action="store_true", help="report only; write nothing")
    p.add_argument("--nets", default=None, help="comma-separated nets: escape only the pins of these nets")
    p.add_argument("--no-drc", action="store_true", help="skip the kicad-cli DRC self-check")
    p.add_argument("--add-zones", dest="add_zones", action="store_true",
                   help="also write the declared pours (`[[plan.pour]]`) as zones (priority 0, clearance 0.2, solid connection)")
    p.add_argument("--track", type=float, default=None)
    p.add_argument("--clearance", type=float, default=None)
    p.add_argument("--via", type=float, default=None)
    p.add_argument("--drill", type=float, default=None)
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
