"""route stage, `[route] router = "krt-whole"`: the four-call KiCadRoutingTools chain, nothing else.

  1-2  route_planes.py  one call per plane (net, layer) of the board's zones
  3    route_diff.py    the pair nets that are still open
  4    route.py         every net, plane nets as --power-nets, --keep-input-copper

Each call reads the previous call's board and writes the next (the .kicad_pro travels along).  Exit 0 and 3 (some
feature delivered below the requested size, never below the floor) are accepted; anything else stops the chain."""
import os
import subprocess
import time

from . import routerules as RR, routeconn as RC, pairfb as PFB

CAP_S = 1200.0           # one router call
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
    legs = [x for p in (report or {}).get("pairs") or [] for x in (p.get("pair") or [])]
    if legs:
        return sorted(set(legs))
    fams = list(((cons or {}).get("pairs") or {}))
    return sorted(x for x in names if any(x.startswith(f) for f in fams))


def glob_escape(name):
    return name.replace("[", "[[]")


def cmd_planes(py, rdir, src, dst, net, layer, base):
    return [py, os.path.join(rdir, "py_router", "route_planes.py"), src, dst, "--nets", glob_escape(net), "--plane-layers", layer, *base]


def cmd_diff(py, rdir, src, dst, nets, sig, base):
    return [py, os.path.join(rdir, "py_router", "route_diff.py"), src, dst, "--nets", *nets, "--layers", *sig, *base, "--keep-input-copper"]


def cmd_route(py, rdir, src, dst, sig, base, power):
    pw = ["--power-nets", *[glob_escape(n) for n in power], "--power-nets-widths", *[f"{w:g}" for w in power.values()]] if power else []
    return [py, os.path.join(rdir, "py_router", "route.py"), src, dst, "--nets", "*", *pw, "--layers", *sig, *base, "--keep-input-copper"]


def base_flags(R, d):
    """the recipe's RULES: sizes from the rules, escalation off, strict sizes, the floor file."""
    fo = RR.write_overrides(os.path.join(d, "router-fab.txt"), R)
    return [*RR.size_args(R), "--escalation", "off", "--strict-sizes", *(["--fab-overrides", fo] if fo else [])]


def run_step(cmd, log, cap_s=CAP_S):
    """-> (rc, seconds); rc 124 on the cap, -N when killed by a signal.  Never retried."""
    t = time.time()
    try:
        with open(log, "w") as fh:
            rc = subprocess.run(cmd, cwd=os.path.dirname(os.path.dirname(cmd[1])), stdout=fh, stderr=subprocess.STDOUT, timeout=cap_s).returncode
    except subprocess.TimeoutExpired:
        rc = 124
    return rc, round(time.time() - t, 1)


def _carry(src, dst):
    import shutil
    s, t = os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(dst)[0] + ".kicad_pro"
    if os.path.exists(s) and not os.path.exists(t):
        shutil.copy(s, t)


def run(c, n, board, runner=run_step):
    """the route stage in krt-whole mode -> stage result dict.  `runner(cmd, log)` -> (rc, s): replaceable in tests."""
    import json
    a = c.a
    d = c.dir(n, "route")
    best = os.path.join(d, "board.kicad_pcb")
    shutil_copy(board, best)
    R = RR.rules(c.cons, board, a.track, a.clearance)
    base = base_flags(R, d)
    sig, planes = plane_plan(board)
    names = set(RC.open_by_net(board))                       # nets that are open
    rep = None
    try:
        pd = ((getattr(c, "state", None) or {}).get("stages") or {}).get("pairs", {}).get("dir")
        rep = json.load(open(os.path.join(c.work, pd, "board.pairs.json"))) if pd else None
    except (OSError, ValueError, TypeError):
        pass
    allnets = set(_all_nets(board))
    hand = PFB.handoff_nets(getattr(c, "state", None), getattr(c, "work", ""))
    pn = pair_nets(c.cons, allnets, rep)
    opn = {x for x in pn if x in names or x in hand}                 # a pair is incomplete when either leg is open
    diff = sorted(x for x in pn if any(_diffnamed(x, y) and (x in opn or y in opn) for y in pn))
    steps = [(f"plane-{net}", lambda s, o, net=net, lay=lay: cmd_planes(a.router_py, a.router_dir, s, o, net, lay, base)) for net, lay in planes]
    if diff:
        steps.append(("diff", lambda s, o: cmd_diff(a.router_py, a.router_dir, s, o, diff, sig, base)))
    power = {net: float(((c.cons or {}).get("power") or {}).get(net, {}).get("width_mm") or POWER_W)    # plane nets: taps, not rails
             for net in dict.fromkeys(x for x, _ in planes)}
    steps.append(("route", lambda s, o: cmd_route(a.router_py, a.router_dir, s, o, sig, base, power)))
    cur, rows = best, []
    for i, (name, build) in enumerate(steps, 1):
        dst = os.path.join(d, f"{i}-{name}.kicad_pcb".replace("*", "x"))
        _carry(cur, dst)
        log = dst[:-10] + ".log"
        rc, secs = runner(build(cur, dst), log)
        rows.append({"step": name, "rc": rc, "s": secs})
        if rc not in (0, 3) or not os.path.exists(dst):
            tail = open(log, errors="replace").read().splitlines()[-8:] if os.path.exists(log) else []
            return {"verdict": "fail", "board": cur, "line": f"krt-whole: step {name} rc={rc} after {secs}s", "next": tail, "steps": rows}
        cur = dst
    shutil_copy(cur, best)
    left = RC.open_by_net(best)
    line = f"krt-whole: {sum(left.values())} unconnected in {sum(r['s'] for r in rows):.0f}s, steps " + ", ".join(f"{r['step']}={r['rc']}" for r in rows)
    json.dump({"line": line, "steps": rows, "open": left, "diff_nets": diff, "planes": planes, "power": power},
              open(os.path.join(d, "result.json"), "w"), indent=1)
    if left:
        line += "; open: " + ", ".join(sorted(left))
    return {"verdict": "warn" if left else "pass", "open": left, "board": best, "line": line, "steps": rows}


def shutil_copy(src, dst):
    import shutil
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy(src, dst)
    _carry(src, dst)


def _diffnamed(p, n):
    return any(x.endswith(a) and y == x[:-len(a)] + b for x, y in ((p, n), (n, p)) for a, b in (("_P", "_N"), ("-P", "-N"), ("P", "N"), ("+", "-"), ("_DP", "_DM")))


def _all_nets(board):
    return [str(x) for x in _pcb().LoadBoard(board).GetNetsByName().keys() if str(x)]
