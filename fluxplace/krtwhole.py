"""Route stage, mode `krt-whole`: whole-board KiCadRoutingTools passes instead of net-by-net batches.

Selected by `[route] router = "krt-whole"` or `--router krt-whole`; the `direct` flow selects it by default.
One whole-board router call per pass (no time-box on those calls; AGENTS.md):

  0 prep      copy the placed board; the constraints' netclasses (pair widths/gaps, rail widths) go into the copy's
              .kicad_pro, where the router reads them
  1 planes    route_planes.py per plane net onto its plane layer (ground first, then pour rails, in stack order)
  2 pairs     route_diff.py '*': the tool detects pairs by suffix; widths and gaps come from the netclasses
  3 rest      route.py '*', rails and RF nets at their rule width (--power-nets / --power-nets-widths)
  4 leftovers route.py on the open nets only, cheapest rung first; a rung is kept only if it opens fewer
              connections and adds no DRC key
  5 centering snap-vias (track ends to via centres); kept only if it adds no DRC key
  6 plane-tie planetie (floating plane clusters tied to their plane), same guard
  7 pour-tie  pourtie (pads of pour nets tied through the copper), same guard
  8 refill    zone refill and save; ownership table; counts from kicad-cli, never from the router's summary

Floors come from routerules.rules / routerules.flags (the --fab-overrides file is generated from the constraints,
escalation off, strict sizes). No literal floor or board-specific name appears here.
"""
import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter

from . import adaptive
from . import constraints as C
from . import drcguard as DG
from . import netclass as NC
from . import routediag as RD
from . import routerules as RR

NECK_MAX_MM = 2.0          # a rail or RF net may be thinner than its rule only in a pad-entry neck this long
KICAD_CLI = "kicad-cli"
PAIR_TAIL = re.compile(r"(_DP|_DM|_P|_N|P|N|\+|-)$")


# ----------------------------------------------------------------- board facts
def _pcb():
    import pcbnew
    return pcbnew


def copper_names(board):
    """copper layer names in stack order (F.Cu .. B.Cu)."""
    b = _pcb().LoadBoard(board)
    return [b.GetLayerName(x) for x in b.GetEnabledLayers().CuStack()]


def plane_plan(cons, planes, pads):
    """[(net, layer)] for the plane layers, in stack order: the ground net with the most pads on the first plane layer,
    then the `[power.*] pour = true` rails, largest current first, one per remaining layer."""
    grounds = sorted((n for n in pads if NC.is_ground(n)), key=lambda n: (-len(pads[n]), n))
    pours = sorted(((n, float(p.get("max_current_ma", 0) or 0)) for n, p in ((cons or {}).get("power") or {}).items()
                    if isinstance(p, dict) and p.get("pour") and n in pads), key=lambda x: -x[1])
    order = grounds[:1] + [n for n, _ in pours if n not in grounds[:1]]
    return list(zip(order, planes))


def rail_widths(cons, nets):
    """{net: width mm} for the non-pour power rails on the board: explicit width_mm, else the netclass rule."""
    out = {}
    for n, p in ((cons or {}).get("power") or {}).items():
        if n not in nets or not isinstance(p, dict) or p.get("pour"):
            continue
        if "width_mm" in p:
            out[n] = float(p["width_mm"])
        elif p.get("max_current_ma"):
            out[n] = C.rail_width_mm(float(p["max_current_ma"]))
    return out


def rf_width(board, cons, planes, layer):
    """Impedance-solved width for the RF target on `layer` (the outer signal layer RF copper starts on), clamped to the
    fab floor. None when the stackup does not describe the layer."""
    from . import review as R
    from . import stackup as SK
    stk = R.parse_stackup_text(open(board, errors="replace").read())
    geom = R.layer_geometry(stk, set(planes), layer)
    if not geom or (geom[0] is None and geom[2] is None):
        return None
    z = float(((cons or {}).get("rf") or {}).get("target_z", 50.0))
    w = SK._bisect(lambda x: R.z0_on_layer(x, geom)[0], z, 0.1, 1.0)
    return C.clamp_track_mm(round(w, 3), cons) if w else None


def rf_nets(B, cons):
    """nets the netclass classifier calls RF."""
    return {n for n, v in NC.classify(B, cons, None).items() if v.get("class") == "rf"}


def pair_families(cons):
    """[(family prefix, skew limit mm)] from `[pairs.FAMILY]`, else `[si] default_skew_mm`."""
    si = float(((cons or {}).get("si") or {}).get("default_skew_mm", 1.0))
    return [(fam, float(v.get("skew_mm", si))) for fam, v in ((cons or {}).get("pairs") or {}).items()]


def measure(board, rules_by_net):
    """pcbnew read of the copper -> ({net: {len, min_w, thin_runs_mm}}, via count). A thin run is a track segment
    narrower than rules_by_net[net]."""
    b = _pcb().LoadBoard(board)
    seg = {}
    for t in b.GetTracks():
        if t.GetClass() != "PCB_TRACK":
            continue
        n, w, mm = t.GetNetname(), t.GetWidth() / 1e6, t.GetLength() / 1e6
        r = seg.setdefault(n, {"len": 0.0, "min_w": None, "thin_runs_mm": []})
        r["len"] += mm
        r["min_w"] = w if r["min_w"] is None else min(r["min_w"], w)
        if n in rules_by_net and w < rules_by_net[n] - 1e-6:
            r["thin_runs_mm"].append(round(mm, 3))
    vias = sum(1 for t in b.GetTracks() if t.GetClass() == "PCB_VIA")
    return seg, vias


def clamp_netclasses(pro_path, R):
    """Conform the copy's netclass values: raised to the floor (clearance, track, pair width and gap, via, drill) and
    via sizes capped at the requested via rule. inject_netclasses writes pair classes at 0.1 mm clearance (under the
    floor) and rail classes at 0.6/0.3 mm vias (not the rule); the router would request them as given.
    -> [(class, key, old, new)] for the record."""
    import json as _json
    d = _json.load(open(pro_path))
    floors = {"clearance": R["min_clearance"], "track_width": R["min_track"], "diff_pair_width": R["min_track"],
              "diff_pair_gap": R["min_clearance"], "via_diameter": R["min_via"], "via_drill": R["min_drill"]}
    ceil = {"via_diameter": R["via"], "via_drill": R["drill"]}     # the requested via rule caps the classes' via sizes
    changed = []
    for cl in (d.get("net_settings") or {}).get("classes", []):
        for key, floor in floors.items():
            if floor and key in cl and cl[key] is not None and float(cl[key]) < floor - 1e-9:
                changed.append((cl.get("name"), key, cl[key], floor))
                cl[key] = floor
        for key, top in ceil.items():
            if top and key in cl and cl[key] is not None and float(cl[key]) > top + 1e-9:
                changed.append((cl.get("name"), key, cl[key], top))
                cl[key] = top
    if changed:
        _json.dump(d, open(pro_path, "w"), indent=2)
    return changed


def copy_board(src, dst):
    """the board and its .kicad_pro (the router and kicad-cli read the rules there)."""
    shutil.copy(src, dst)
    side = os.path.splitext(src)[0] + ".kicad_pro"
    if os.path.exists(side):
        shutil.copy(side, os.path.splitext(dst)[0] + ".kicad_pro")


def drc(board):
    """kicad-cli DRC, severities all, zones refilled -> (keys Counter, by-type Counter, unconnected count).
    The board's .kicad_pro must sit beside it."""
    out = board + ".drc.json"
    subprocess.run([KICAD_CLI, "pcb", "drc", "--format", "json", "--severity-all", "--all-track-errors", "--refill-zones",
                    "-o", out, board], capture_output=True, text=True, timeout=900)
    d = json.load(open(out))
    os.unlink(out)
    keys, _counts = DG.keys_from_report(d)
    return keys, Counter(v["type"] for v in d.get("violations", [])), len(d.get("unconnected_items", []))


def new_keys(board, base):
    """DRC keys on `board` that `base` (a Counter) does not already hold."""
    keys = drc(board)[0]
    return Counter(keys) - Counter(base)


# ----------------------------------------------------------------- router calls
def sizes(R):
    """the requested sizes, the floor itself as clearance: routerules.size_args adds a grid step of margin (the router lands
    up to a grid step closer than asked), which with escalation off leaves the whole-board passes many nets open."""
    return ["--track-width", f"{R['track']:g}", "--clearance", f"{R['clearance']:g}",
            "--via-size", f"{R['via']:g}", "--via-drill", f"{R['drill']:g}"]


def router_cmd(a, R, d, script, src, dst, extra=()):
    """One whole-board call: positional in/out, the project rules, the signal layers (not for route_planes), `extra`."""
    cmd = [a.router_py, os.path.join(a.router_dir, "py_router", script), src, dst]
    if script in ("route.py", "route_diff.py"):      # route_planes.py has no such flag (krt_flags scans the whole package)
        cmd += ["--keep-input-copper"]
    if script != "route_planes.py":
        cmd += ["--layers", *a.layers]
    cmd += [*sizes(R), *adaptive.krt_extra(a.router_dir, script), *RR.flags(R, a.router_dir, d, script=script)]
    return cmd + list(extra)


def run_router(cmd, cwd, log):
    """-> (rc, seconds). Output to `log`; no time-box (whole-board passes run to completion)."""
    t = time.time()
    with open(log, "w") as fh:
        rc = subprocess.run(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT).returncode
    return rc, round(time.time() - t, 1)


def leftover_ladder(a):
    """(label, extra args) rungs, cheapest first; only the flags this checkout accepts."""
    have = adaptive.krt_flags(a.router_dir, "route.py")
    rungs = [("ripup10", ["--max-ripup", "10"])]
    swap = [f for f in ("--mps-layer-swap", "--can-swap-to-top-layer") if f in have]
    if swap:
        rungs.append(("ripup10+swap", ["--max-ripup", "10", *swap]))
    return rungs


def route_extra(a):
    """route.py flags of the whole-board pass: no power-tap necks (rails keep their rule width) where the checkout has it."""
    have = adaptive.krt_flags(a.router_dir, "route.py")
    return ["--no-power-tap-neckdown"] if "--no-power-tap-neckdown" in have else []


# ----------------------------------------------------------------- the stage
def run(c, n, board, conn):
    """`route` stage in krt-whole mode -> stage result (verdict, line, board, next). `conn` is batchroute's
    connectivity count (net -> open connections)."""
    from . import planetie as PT, pourtie as PTI, snapvias as SV, routeowners as RO
    a, cons = c.a, c.cons or {}
    d = c.dir(n, "route")
    work = os.path.join(d, "steps")
    os.makedirs(work, exist_ok=True)
    R = RR.rules(cons, board, a.track, a.clearance)
    steps, t0 = [], time.time()

    # 0 prep
    cur = os.path.join(work, "00-prep.kicad_pcb")
    copy_board(board, cur)
    pro_cur = os.path.splitext(cur)[0] + ".kicad_pro"
    C.inject_netclasses(pro_cur, cons, log=lambda m: None, board=cur)
    clamped = clamp_netclasses(pro_cur, R)
    base_keys, base_types, base_unc = drc(board)           # the placed input is the reference for "no new type"
    B = RD.load(cur)
    pads = NC.net_pads(B)
    nets = set(pads)
    planes = [l for l in copper_names(cur) if l not in a.layers]
    plan = plane_plan(cons, planes, pads)
    rails = rail_widths(cons, nets)
    rf = rf_nets(B, cons) & nets
    rfw = rf_width(cur, cons, planes, a.layers[0]) if rf else None
    rules_by_net = dict(rails)
    rules_by_net.update({n_: rfw for n_ in rf if rfw})
    power = {}
    power.update(rails)
    for net in rf:
        if rfw:
            power[net] = max(power.get(net, 0.0), rfw)
    for net, _layer in plan:                                # plane nets routed as traces keep the track rule
        power.setdefault(net, R["track"])
    pw_args = ["--power-nets", *power, "--power-nets-widths", *[f"{w:g}" for w in power.values()]] if power else []
    plane_set = {n_ for n_, _ in plan}

    def signal_open(path):                        # the router's passes are judged on signal nets (planes: planetie)
        return sum(v for k, v in conn(path).items() if k not in plane_set)

    open0 = sum(conn(cur).values())
    cur_open = signal_open(cur)
    state_path = os.path.join(work, "passes.json")
    done = []
    if getattr(a, "krt_whole_resume", False) and os.path.exists(state_path):
        st = json.load(open(state_path))
        done, cur, cur_open, steps = st["done"], st["cur"], st["cur_open"], st["steps"]

    def checkpoint(name):
        done.append(name)
        with open(state_path, "w") as fh:
            json.dump({"done": done, "cur": cur, "cur_open": cur_open, "steps": steps}, fh)

    def rec(name, rc, secs, ok, why=""):
        steps.append({"step": name, "rc": rc, "s": secs, "accepted": bool(ok), "why": why})

    # 1 planes
    for net, layer in plan:
        tag = layer.replace(".", "_")
        if f"planes {net}" in done:
            continue
        dst = os.path.join(work, f"01-plane-{tag}.kicad_pcb")
        rc, secs = run_router(router_cmd(a, R, d, "route_planes.py", cur, dst, ["--nets", net, "--plane-layers", layer]),
                              a.router_dir, os.path.join(work, f"01-plane-{tag}.log"))
        ok = rc == 0 and os.path.exists(dst)
        rec(f"planes {net}->{layer}", rc, secs, ok)
        if ok:
            cur = dst
            checkpoint(f"planes {net}")

    # 2 pairs
    dst = os.path.join(work, "02-pairs.kicad_pcb")
    if "pairs" not in done:
        rc, secs = run_router(router_cmd(a, R, d, "route_diff.py", cur, dst, ["--nets", "*"]), a.router_dir,
                              os.path.join(work, "02-pairs.log"))
        if rc == 0 and os.path.exists(dst) and signal_open(dst) <= cur_open:
            cur, cur_open = dst, signal_open(dst)
            rec("pairs route_diff *", rc, secs, True)
            checkpoint("pairs")
        else:
            rec("pairs route_diff *", rc, secs, False, "router error or more open")

    # 3 rest
    dst = os.path.join(work, "03-rest.kicad_pcb")
    # the plane nets are left out here: routed as traces they cost the router an hour (GND: ~416 pad-to-pad edges); the
    # plane-tie and pour-tie steps below connect them through their pours (README #562 chain, planetie.py)
    skip = [f"!{n_}" for n_, _layer in plan]
    if "rest" not in done:
        rc, secs = run_router(router_cmd(a, R, d, "route.py", cur, dst, ["--nets", "*", *skip, *pw_args, *route_extra(a)]),
                              a.router_dir, os.path.join(work, "03-rest.log"))
        if rc == 0 and os.path.exists(dst) and signal_open(dst) <= cur_open:
            cur, cur_open = dst, signal_open(dst)
            rec("rest route.py *", rc, secs, True)
            checkpoint("rest")
        else:
            rec("rest route.py *", rc, secs, False, "router error or more open")

    # 4 leftovers: the open nets only; a rung is kept when it opens fewer and adds no DRC key
    keys_cur = drc(cur)[0]
    for i, (label, extra) in enumerate(leftover_ladder(a), 1):
        if f"leftovers {label}" in done:
            continue
        open_nets = sorted(k for k, v in conn(cur).items() if v and k not in plane_set)
        if not open_nets:
            break
        dst = os.path.join(work, f"04-leftover-{i}.kicad_pcb")
        rc, secs = run_router(router_cmd(a, R, d, "route.py", cur, dst, ["--nets", *open_nets, *skip, *extra, *route_extra(a)]),
                              a.router_dir, os.path.join(work, f"04-leftover-{i}.log"))
        if rc != 0 or not os.path.exists(dst):
            rec(f"leftovers {label}", rc, secs, False, "router error")
            continue
        # the router's new copper carries track ends off their via centres, which the centring step fixes after the
        # whole run: the candidate is snapped first, and the DRC guard judges the snapped board
        cand = os.path.join(work, f"04-leftover-{i}.snap.kicad_pcb")
        SV.snap_file(dst, cand)
        o = signal_open(cand)
        added = Counter(drc(cand)[0]) - Counter(keys_cur)
        ok = o < cur_open and not added
        rec(f"leftovers {label}", rc, secs, ok, "" if ok else (f"open {o} of {cur_open}" if o >= cur_open else f"{sum(added.values())} new DRC key(s)"))
        if ok:
            cur, cur_open, keys_cur = cand, o, Counter(drc(cand)[0])
            checkpoint(f"leftovers {label}")

    # 5 centering: snap-vias, kept only when it adds no DRC key
    dst = os.path.join(work, "05-centering.kicad_pcb")
    nsn, nv = SV.snap_file(cur, dst)
    added = new_keys(dst, keys_cur) if nsn else Counter()
    ok = bool(nsn) and not added
    rec(f"centering ({nsn} end(s) moved, {nv} via(s))", 0, 0.0, ok, "" if ok or not nsn else "new DRC key(s)")
    if ok:
        cur = dst
        keys_cur = Counter(drc(dst)[0])

    # 6 plane-tie, 7 pour-tie: each through the same guard
    tie_nets = [n_ for n_, _ in plan]
    for name, fn in (("plane-tie", lambda s, t: PT.run(s, t, tie_nets, conn, log=None, workdir=work, plane_layers=tuple(planes))),
                     ("pour-tie", lambda s, t: PTI.run(s, t, work))):
        dst = os.path.join(work, f"06-{name}.kicad_pcb")
        try:
            fn(cur, dst)
        except Exception as e:                  # noqa: BLE001 - recorded; the board stays as it was
            rec(name, 1, 0.0, False, f"error {str(e)[:80]}")
            continue
        o = sum(conn(dst).values())                      # ties judge all nets: they are what closes the plane pads
        added = new_keys(dst, keys_cur)
        ok = os.path.exists(dst) and not added and o <= sum(conn(cur).values())
        rec(name, 0, 0.0, ok, "" if ok else "new DRC key(s) or more open")
        if ok:
            cur, cur_open, keys_cur = dst, signal_open(dst), Counter(drc(dst)[0])

    # 8 refill, save, count
    final = os.path.join(d, "board.kicad_pcb")
    refill_save(cur, final)
    keys_f, types_f, unc_f = drc(final)
    conn_f = conn(final)
    open_f = sum(conn_f.values())
    open_sig = signal_open(final)
    seg, vias = measure(final, rules_by_net)
    pairs = pair_report(final, conn_f, cons, seg)
    rails_rep = rail_report(seg, rails, rf, rfw)
    new_types = sorted(t for t in types_f if t not in base_types)
    record = {"mode": "krt-whole", "steps": steps, "netclass_clamped": clamped, "open_in": open0, "open_out": open_f,
              "open_signal_out": open_sig, "unconnected_drc": unc_f,
              "unconnected_in": base_unc, "drc_in": dict(base_types), "drc_out": dict(types_f), "new_drc_types": new_types,
              "new_drc_keys": sum((Counter(keys_f) - Counter(base_keys)).values()), "vias": vias, "pairs": pairs,
              "rails": rails_rep, "planes": plan, "rf_nets": sorted(rf), "rf_width_mm": rfw,
              "wall_s": round(time.time() - t0, 1)}
    with open(os.path.join(d, "krtwhole.json"), "w") as fh:
        json.dump(record, fh, indent=1, default=list)
    own = owners(d, final, conn_f, cons)
    bad = [r for r in rails_rep if r["fail"]]
    verdict = "fail" if bad else ("warn" if (open_f or new_types or pairs["uncomplete"] or pairs["over_limit"]) else "pass")
    if os.path.exists(state_path):
        os.unlink(state_path)                    # finished: a later --krt-whole-resume starts a fresh run
    line = (f"krt-whole: {open_f} open ({open_sig} signal; in {open0}), DRC new types {len(new_types)}, pairs {pairs['complete']}/{pairs['total']} "
            f"complete, {pairs['over_limit']} over skew limit, rails {len(rails_rep) - len(bad)}/{len(rails_rep)} ok, "
            f"vias {vias}, {record['wall_s']:.0f}s")
    nxt = [f"open {r['net']}: {r['open']}" for r in own["rows"][:6]] + [os.path.join(d, "krtwhole.json")]
    return {"verdict": verdict, "line": line, "board": final, "next": nxt}


def refill_save(src, dst):
    """zones refilled, saved to dst; the project file follows the board."""
    from . import planes as PL
    b = _pcb().LoadBoard(src)
    PL.refill(b)
    b.Save(dst)
    shutil.copy(os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(dst)[0] + ".kicad_pro")


def pair_report(board, conn_f, cons, seg):
    """Pairs by suffix (the names the tool sees): complete = no open pad on either leg; intra-pair copper skew against the
    family limit. Family nets with a P/N-style tail that no complete pair covers are listed as uncoupled."""
    b = _pcb().LoadBoard(board)
    names = [b.FindNet(i).GetNetname() for i in range(1, b.GetNetCount())]
    groups = {}
    for nm in names:
        for suf, tag in (("_DP", "P"), ("_DM", "N"), ("_P", "P"), ("_N", "N")):
            if nm.endswith(suf):
                groups.setdefault(nm[:-len(suf)], {})[tag] = nm
                break
    fams = sorted(pair_families(cons), key=lambda x: -len(x[0]))
    rows, paired = [], set()
    for base, v in sorted(groups.items()):
        limit = next((lim for fam, lim in fams if base.startswith(fam)), None)
        if set(v) != {"P", "N"} or limit is None:
            continue
        p, q = v["P"], v["N"]
        skew = abs(seg.get(p, {}).get("len", 0.0) - seg.get(q, {}).get("len", 0.0))
        done = not conn_f.get(p) and not conn_f.get(q)
        rows.append({"pair": base, "complete": done, "skew_mm": round(skew, 2), "limit_mm": limit, "over": skew > limit})
        paired |= {p, q}
    uncoupled = sorted(n_ for n_ in names if PAIR_TAIL.search(n_) and n_ not in paired
                       and any(n_.startswith(f) for f, _ in fams))
    return {"total": len(rows), "complete": sum(r["complete"] for r in rows),
            "uncomplete": [r["pair"] for r in rows if not r["complete"]],
            "over_limit": sum(1 for r in rows if r["over"]), "rows": rows, "uncoupled": uncoupled}


def rail_report(seg, rails, rf, rfw):
    """per rail and RF net: the rule width against the copper; a thin run longer than NECK_MAX_MM fails."""
    rules = dict(rails)
    if rfw:
        rules.update({n_: rfw for n_ in rf})
    out = []
    for n_, rule in sorted(rules.items()):
        r = seg.get(n_)
        if not r or r["min_w"] is None:
            continue
        longest = max(r["thin_runs_mm"], default=0.0)
        out.append({"net": n_, "rule_mm": round(rule, 3), "min_mm": round(r["min_w"], 3),
                    "longest_thin_mm": longest, "fail": longest > NECK_MAX_MM})
    return out


def owners(d, board, conn_f, cons):
    """route_owners.{json,txt}: every open net is owned by `krt-whole` (the whole-board passes were handed it)."""
    from . import routeowners as RO
    cls = NC.classify(RD.load(board), cons, None)
    open_ = {n_: k for n_, k in conn_f.items() if k}
    rows, unowned = RO.table(open_, cls, {n_: "krt-whole" for n_ in open_}, None)
    RO.write(d, rows, unowned)
    return {"rows": rows, "unowned": unowned}
