"""Route stage, mode `krt-whole`: whole-board KiCadRoutingTools passes instead of net-by-net batches.

Selected by `[route] router = "krt-whole"` or `--router krt-whole`; the `direct` flow selects it by default.
One whole-board router call per pass (no time-box on those calls; AGENTS.md):

  0 prep      copy the board with its .kicad_pro; netclasses from the constraints are injected and conformed to the floors
  1 planes    route_planes.py per zone (net, layer) of the board's plane layers
  2 pairs     route_diff.py only for the pairs that still have an open leg (the pairs stage's complete, locked pairs are kept)
  3 protect   route.py on the nets the constraints protect from thinning (rails, RF, clocks, controlled nets) at their rule
              width, escalation off
  4 rest      route.py on everything else at the preferred sizes, stepping down to the generated floor file only where needed
              (--escalation fab + --fab-overrides); protected nets still open are excluded so they are never thinned
  5 finish    batchroute.route_batches (hybrid finish, cheapest first, rip-up of unlocked copper) on what is still open,
              capped at `finish_share` of the stage budget
  6 centring  every track end moved onto its via centre; a move is kept only if no new DRC violation appears (iterative)
  7 ties      planetie, pourtie, refill; ownership table; counts from kicad-cli, never from the router's summary

Every router call keeps the copper already on the board (--keep-input-copper). Exit code 3 (--strict-sizes: something delivered
below a requested size) is accepted when every delivered feature is at or above the floor (floor_audit) and no protected net was
thinned; anything below a floor fails the step and the input is kept. Floors come from routerules.rules; no literal floor or
board-specific name appears here.
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


def rail_widths(cons, nets, pour=False):
    """{net: width mm} for the power rails on the board: explicit width_mm, else the netclass rule. Pour rails only with `pour`."""
    out = {}
    for n, p in ((cons or {}).get("power") or {}).items():
        if n not in nets or not isinstance(p, dict) or (p.get("pour") and not pour):
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
    rules = d.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})
    for key, floor in (("min_clearance", R["min_clearance"]), ("min_track_width", R["min_track"]),
                       ("min_via_diameter", R["min_via"]), ("min_through_hole_diameter", R["min_drill"])):
        if floor and float(rules.get(key) or 0) < floor - 1e-9:      # kicad-cli grades at the floor, not at a looser board rule
            changed.append(("rules", key, rules.get(key), floor))
            rules[key] = floor
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
PAIR_SUFFIX = (("_DP", "_DM"), ("_P", "_N"), ("-P", "-N"), ("P", "N"), ("+", "-"))     # (positive, negative) tails
ROUTE_DIFF_NAMES = (("_P", "_N"), ("-P", "-N"), ("P", "N"), ("+", "-"), ("_DP", "_DM"))  # what route_diff.py pairs by itself
CAP_S = 10800.0            # wall-clock cap on one router call (the whole-board calls are otherwise not time-boxed)


def sizes(R):
    """the preferred sizes (the stage's --track/--clearance, floored; via and drill the rule's)."""
    return ["--track-width", f"{R['track']:g}", "--clearance", f"{R['clearance']:g}",
            "--via-size", f"{R['via']:g}", "--via-drill", f"{R['drill']:g}"]


def glob_escape(name):
    """a net name as a router net pattern: `[` would read as a character class."""
    return name.replace("[", "[[]")


def planes_of(board, signal_layers):
    """[(net, layer)] for every zone on a non-signal copper layer of the board, in stack order, one entry per (net, layer)."""
    b = _pcb().LoadBoard(board)
    cu = [b.GetLayerName(x) for x in b.GetEnabledLayers().CuStack()]
    seen, out = set(), []
    for z in b.Zones():
        if z.GetIsRuleArea() or not z.GetNetname():
            continue
        for lid in z.GetLayerSet().CuStack():
            lay = b.GetLayerName(lid)
            if lay not in signal_layers and (z.GetNetname(), lay) not in seen:
                seen.add((z.GetNetname(), lay))
                out.append((z.GetNetname(), lay))
    return sorted(out, key=lambda x: cu.index(x[1]) if x[1] in cu else 99)


def protected_nets(cls, cons):
    """nets the constraints keep from thinning: pairs, RF, clocks, `[nets.X]` impedance nets, `[pairs.*]` families,
    controlled names (globalroute.is_controlled) and rails."""
    from . import globalroute as GR
    return {n for n, info in cls.items() if GR.is_fixed_width(n, info, cons)}


def protected_widths(prot, rails, rf, rfw, R):
    """{net: width mm} for the protected nets that go to route.py: the rule width (rail: current; RF: impedance) else the
    preferred track."""
    out = {}
    for n in sorted(prot):
        if n in rails:
            out[n] = rails[n]
        elif n in rf and rfw:
            out[n] = rfw
        else:
            out[n] = R["track"]
    return out


def tail_pairs(names, fams):
    """{base: {"P": net, "N": net}} for the nets of a `[pairs.*]` family whose names differ in a P/N-style tail."""
    groups = {}
    for nm in names:
        if not any(nm.startswith(f) for f, _ in fams):
            continue
        for pos, neg in PAIR_SUFFIX:
            for tag, suf in (("P", pos), ("N", neg)):
                if nm.endswith(suf) and len(nm) > len(suf):
                    groups.setdefault(nm[:-len(suf)] + "|" + pos, {})[tag] = nm
                    break
            else:
                continue
            break
    return {k: v for k, v in groups.items() if set(v) == {"P", "N"}}


def routed_by_diff(p, n):
    return any(x.endswith(a) and y == x[:-len(a)] + b for a, b in ROUTE_DIFF_NAMES for x, y in ((p, n), (n, p)))


def open_pairs(names, conn_map, cons, handoff=()):
    """-> (diff, plain): nets of the pair legs that still have an open connection (or that the pairs stage handed over, state
    c), split into those route_diff.py can pair by name and those it cannot (those go to route.py as protected nets).
    A pair whose legs are both connected is left alone: its copper is the pairs stage's, locked."""
    fams = pair_families(cons)
    diff, plain = [], []
    for g in tail_pairs(names, fams).values():
        p, n = g["P"], g["N"]
        if not (conn_map.get(p) or conn_map.get(n) or p in handoff or n in handoff):
            continue
        (diff if routed_by_diff(p, n) else plain).extend([p, n])
    return sorted(set(diff)), sorted(set(plain))


def router_cmd(a, R, d, script, src, dst, extra=(), esc="off"):
    """One whole-board call: positional in/out, the project rules, the signal layers (not for route_planes), `extra`.
    esc: the escalation policy; "fab" steps below the preferred sizes down to the generated floor file and no further."""
    from . import batchroute as BR
    have = adaptive.krt_flags(a.router_dir, script)
    cmd = [a.router_py, os.path.join(a.router_dir, "py_router", script), src, dst]
    if script in ("route.py", "route_diff.py"):      # route_planes.py has no such flag (krt_flags scans the whole package)
        cmd += ["--keep-input-copper"]
        if BR.has_fanout(src) and "--no-stub-layer-swap" in have:
            cmd += ["--no-stub-layer-swap"]          # the router may not re-layer the locked fan-out stubs
    if script != "route_planes.py":
        cmd += ["--layers", *a.layers]
    cmd += [*sizes(R), *adaptive.krt_extra(a.router_dir, script), *RR.flags(dict(R, escalation=esc), a.router_dir, d, script=script)]
    return cmd + list(extra)


def run_router(cmd, cwd, log, cap_s=CAP_S):
    """-> (rc, seconds). Output to `log`; capped by wall clock only (rc 124 on the cap)."""
    t = time.time()
    with open(log, "w") as fh:
        try:
            rc = subprocess.run(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, timeout=cap_s).returncode
        except subprocess.TimeoutExpired:
            rc = 124
    return rc, round(time.time() - t, 1)


def route_extra(a):
    """route.py flags of the whole-board pass: no power-tap necks (rails keep their rule width) where the checkout has it."""
    have = adaptive.krt_flags(a.router_dir, "route.py")
    return ["--no-power-tap-neckdown"] if "--no-power-tap-neckdown" in have else []


def power_args(widths):
    return ["--power-nets", *[glob_escape(n) for n in widths], "--power-nets-widths", *[f"{w:g}" for w in widths.values()]] if widths else []


def floor_audit(board, R):
    """features below a floor on `board` -> [text]; empty = every track, via and drill is at or above its floor.
    (Clearance is graded by kicad-cli with the floors written into the .kicad_pro.)"""
    b = _pcb().LoadBoard(board)
    bad = Counter()
    for t in b.GetTracks():
        if t.GetClass() == "PCB_VIA":
            if R["min_via"] and t.GetWidth() / 1e6 < R["min_via"] - 1e-6:
                bad["via diameter"] += 1
            if R["min_drill"] and t.GetDrillValue() / 1e6 < R["min_drill"] - 1e-6:
                bad["via drill"] += 1
        elif R["min_track"] and t.GetWidth() / 1e6 < R["min_track"] - 1e-6:
            bad["track width"] += 1
    return [f"{k} below floor x{v}" for k, v in sorted(bad.items())]


def accept_rc(rc):
    """0 = delivered as requested; 3 = `--strict-sizes`: something delivered below a requested size (the floor audit decides)."""
    return rc in (0, 3)


# ----------------------------------------------------------------- the stage
def run(c, n, board, conn):
    """`route` stage in krt-whole mode -> stage result (verdict, line, board, next). `conn` is batchroute's
    connectivity count (net -> open connections)."""
    from . import planetie as PT, pourtie as PTI, snapvias as SV
    from . import batchroute as BR, pairfb as PFB
    a, cons = c.a, c.cons or {}
    d = c.dir(n, "route")
    work = os.path.join(d, "steps")
    os.makedirs(work, exist_ok=True)
    R = RR.rules(cons, board, a.track, a.clearance)
    S = BR.settings(cons)
    cap_s = float(((cons.get("route") or {}).get("krt_whole_call_cap_s", CAP_S)))
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
    cls = NC.classify(B, cons, None)
    plan = planes_of(cur, set(a.layers))
    plane_layers = sorted({l for _n, l in plan})
    rails = rail_widths(cons, nets, pour=True)
    rf = rf_nets(B, cons) & nets
    rfw = rf_width(cur, cons, [l for l in copper_names(cur) if l not in a.layers], a.layers[0]) if rf else None
    rules_by_net = dict(rails)
    rules_by_net.update({n_: rfw for n_ in rf if rfw})
    prot = protected_nets(cls, cons) & nets
    plane_set = {n_ for n_, _ in plan}
    handoff = PFB.handoff_nets(getattr(c, "state", None), getattr(c, "work", ""))

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

    def judge(name, rc, secs, dst, check_open=True):
        """accept a router output: rc 0/3, no feature below a floor, no protected net thinned, no more signal connections open."""
        if not accept_rc(rc) or not os.path.exists(dst):
            rec(name, rc, secs, False, "router error" if rc != 124 else "wall-clock cap")
            return False
        why = floor_audit(dst, R)
        if not why:
            seg, _v = measure(dst, rules_by_net)
            thin = [r["net"] for r in rail_report(seg, rails, rf, rfw) if r["fail"]]
            why = [f"protected net thinned: {', '.join(thin[:4])}"] if thin else []
        if not why and check_open and signal_open(dst) > cur_open:
            why = [f"open {signal_open(dst)} of {cur_open}"]
        rec(name, rc, secs, not why, "; ".join(why) + ("; stepped below requested size (exit 3), at or above the floor" if rc == 3 and not why else ""))
        return not why

    # 1 planes: one call per zone (net, layer) of the board
    for net, layer in plan:
        tag = layer.replace(".", "_")
        if f"planes {net}" in done:
            continue
        dst = os.path.join(work, f"01-plane-{tag}.kicad_pcb")
        rc, secs = run_router(router_cmd(a, R, d, "route_planes.py", cur, dst, ["--nets", net, "--plane-layers", layer]),
                              a.router_dir, os.path.join(work, f"01-plane-{tag}.log"), cap_s)
        if judge(f"planes {net}->{layer}", rc, secs, dst, check_open=False):
            cur = dst
            checkpoint(f"planes {net}")

    # 2 pairs: only the pairs with an open leg; complete pairs are the pairs stage's locked copper
    cur_open = signal_open(cur)
    diff, plain = open_pairs(sorted(nets), conn(cur), cons, handoff)
    dst = os.path.join(work, "02-pairs.kicad_pcb")
    if diff and "pairs" not in done:
        rc, secs = run_router(router_cmd(a, R, d, "route_diff.py", cur, dst, ["--nets", *[glob_escape(x) for x in diff]]),
                              a.router_dir, os.path.join(work, "02-pairs.log"), cap_s)
        if judge(f"pairs route_diff ({len(diff) // 2} pair(s))", rc, secs, dst):
            cur, cur_open = dst, signal_open(dst)
            checkpoint("pairs")
    elif not diff:
        rec("pairs route_diff", 0, 0.0, True, "no pair with an open leg")

    # 3 protected nets: exact sizes (escalation off), rule widths; plane nets ride here (their pads are already plane-tied)
    open_now = lambda p_: {k for k, v in conn(p_).items() if v}
    pw = protected_widths(prot, rails, rf, rfw, R)
    todo = sorted(k for k in open_now(cur) if k in prot and k not in diff)
    dst = os.path.join(work, "03-protected.kicad_pcb")
    if todo and "protected" not in done:
        rc, secs = run_router(router_cmd(a, R, d, "route.py", cur, dst, ["--nets", *[glob_escape(x) for x in todo],
                                                                         *power_args({k: pw[k] for k in todo if k in pw}), *route_extra(a)], esc="off"),
                              a.router_dir, os.path.join(work, "03-protected.log"), cap_s)
        if judge(f"protected route.py ({len(todo)} net(s))", rc, secs, dst):
            cur, cur_open = dst, signal_open(dst)
            checkpoint("protected")

    # 4 the rest: preferred sizes, stepping down to the floor file only where needed; protected nets still open stay out
    held = sorted(k for k in open_now(cur) if k in prot)
    todo = sorted(k for k in open_now(cur) if k not in prot)
    dst = os.path.join(work, "04-rest.kicad_pcb")
    if todo and "rest" not in done:
        rc, secs = run_router(router_cmd(a, R, d, "route.py", cur, dst, ["--nets", "*", *[f"!{glob_escape(x)}" for x in held],
                                                                         *power_args({k: pw[k] for k in pw if k in prot}), *route_extra(a)], esc="fab"),
                              a.router_dir, os.path.join(work, "04-rest.log"), cap_s)
        if judge(f"rest route.py * ({len(todo)} net(s) open at start)", rc, secs, dst):
            cur, cur_open = dst, signal_open(dst)
            checkpoint("rest")

    # 5 finish: the existing hybrid finish step on what is open, cheapest first, rip-up of unlocked copper, capped
    keys_cur = drc(cur)[0]
    left = sorted(k for k in open_now(cur) if k not in plane_set)
    if left and "finish" not in done:
        budget = float(S["finish_share"]) * float(getattr(a, "route_budget", 1800))
        for label, group, esc in (("protected", [k for k in left if k in prot], "off"), ("rest", [k for k in left if k not in prot], "fab")):
            if not group or budget <= 0:
                continue
            fd = os.path.join(work, f"05-finish-{label}")

            def build(src, out, only, limited, esc=esc):
                ex = ["--nets", *[glob_escape(x) for x in only], *power_args({k: pw[k] for k in only if k in pw}), *route_extra(a)]
                if limited:
                    ex += ["--max-ripup", str(S["max_ripup"])]
                return router_cmd(a, R, d, "route.py", src, out, ex, esc=esc)
            t_f = time.time()
            res = BR.route_batches(cur, fd, group, BR.make_runner(build, a.router_dir, {}, os.path.join(fd, "status.json"), S), cons=cons,
                                   B=RD.load(cur), cls=cls, total_budget=budget, rules=R, conn=conn, hybrid=True, log=lambda m: None)
            budget -= time.time() - t_f
            cand = res["board"]
            ok = cand != cur and not floor_audit(cand, R) and signal_open(cand) < cur_open
            rec(f"finish {label} ({len(group)} net(s))", 0, round(time.time() - t_f, 1), ok,
                f"open {res['start_open']} -> {res['open']}; {res['attempts']} call(s)")
            if ok:
                cur, cur_open = cand, signal_open(cand)
        checkpoint("finish")

    # 6 centring: guarded, iterative
    keys_cur = drc(cur)[0]
    dst = os.path.join(work, "06-centred.kicad_pcb")
    t_c = time.time()
    cen = SV.snap_guarded(cur, dst, lambda p_: drc_report(p_))
    rec(f"centring ({cen['moved']} end(s) moved, {cen['before']} -> {cen['after']} off-centre)", 0, round(time.time() - t_c, 1),
        cen["accepted"], cen["why"])
    if cen["accepted"]:
        cur = dst
        keys_cur = Counter(drc(dst)[0])

    # 7 plane-tie, pour-tie: each through the same guard
    tie_nets = sorted(plane_set)
    for name, fn in (("plane-tie", lambda s_, t_: PT.run(s_, t_, tie_nets, conn, log=None, workdir=work, plane_layers=tuple(plane_layers))),
                     ("pour-tie", lambda s_, t_: PTI.run(s_, t_, work, cons=cons))):
        dst = os.path.join(work, f"07-{name}.kicad_pcb")
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
    clamp_netclasses(os.path.splitext(final)[0] + ".kicad_pro", R)     # the measuring project holds the floors, whatever a router wrote
    keys_f, types_f, unc_f = drc(final)
    conn_f = conn(final)
    open_f = sum(conn_f.values())
    open_sig = signal_open(final)
    seg, vias = measure(final, rules_by_net)
    pairs = pair_report(final, conn_f, cons, seg)
    rails_rep = rail_report(seg, rails, rf, rfw)
    stepped = stepped_features(seg, R)
    new_types = sorted(t for t in types_f if t not in base_types)
    record = {"mode": "krt-whole", "steps": steps, "netclass_clamped": clamped, "open_in": open0, "open_out": open_f,
              "open_signal_out": open_sig, "unconnected_drc": unc_f,
              "unconnected_in": base_unc, "drc_in": dict(base_types), "drc_out": dict(types_f), "new_drc_types": new_types,
              "new_drc_keys": sum((Counter(keys_f) - Counter(base_keys)).values()), "vias": vias, "pairs": pairs,
              "rails": rails_rep, "planes": plan, "rf_nets": sorted(rf), "rf_width_mm": rfw, "protected": sorted(prot),
              "pairs_to_router": diff, "stepped_down": stepped, "floor_audit": floor_audit(final, R),
              "wall_s": round(time.time() - t0, 1)}
    with open(os.path.join(d, "krtwhole.json"), "w") as fh:
        json.dump(record, fh, indent=1, default=list)
    own = owners(d, final, conn_f, cons)
    bad = [r for r in rails_rep if r["fail"]]
    verdict = "fail" if (bad or record["floor_audit"]) else ("warn" if (open_f or new_types or pairs["uncomplete"] or pairs["over_limit"]) else "pass")
    if os.path.exists(state_path):
        os.unlink(state_path)                    # finished: a later --krt-whole-resume starts a fresh run
    line = (f"krt-whole: {open_f} open ({open_sig} signal; in {open0}), DRC new types {len(new_types)}, pairs {pairs['complete']}/{pairs['total']} "
            f"complete, {pairs['over_limit']} over skew limit, rails {len(rails_rep) - len(bad)}/{len(rails_rep)} ok, "
            f"vias {vias}, {record['wall_s']:.0f}s")
    nxt = [f"open {r['net']}: {r['open']}" for r in own["rows"][:6]] + [os.path.join(d, "krtwhole.json")]
    return {"verdict": verdict, "line": line, "board": final, "next": nxt}


def stepped_features(seg, R):
    """nets whose copper is narrower than the preferred track: {net: min width} (the features the step-down delivered)."""
    return {n_: round(r["min_w"], 3) for n_, r in sorted(seg.items()) if r["min_w"] is not None and r["min_w"] < R["track"] - 1e-6}


def drc_report(board):
    """kicad-cli DRC json dict of `board` (all severities, zones refilled)."""
    out = board + ".drc.json"
    subprocess.run([KICAD_CLI, "pcb", "drc", "--format", "json", "--severity-all", "--all-track-errors", "--refill-zones",
                    "-o", out, board], capture_output=True, text=True, timeout=900)
    d = json.load(open(out))
    os.unlink(out)
    return d


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
