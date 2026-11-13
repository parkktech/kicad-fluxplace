"""`fluxplace modules BOARD --constraints C [--spec S]`: group every IC / connector / module with its support parts into a RIGID
MODULE that the floorplan places as one block.

Grouping (in this order):
  1. every part that is not a small passive (C R L FB Y X D LED F with <= 2 pads) is the ANCHOR of its own module;
     `[placement.group.NAME] refs = [...]` merges those anchors into one module (J10 + J11 = the CM5 pair);
  2. a small passive owned by exactly one IC (clusters.ownership: a pad on a net that carries pins of only that IC, or a pad on
     a rail next to one IC pin) joins that IC's module;
  3. the rest (rail-only caps, series links, ESD/TVS) join the module whose pins they share most SIGNAL nets with (ground and
     rails do not count), else the nearest module by the current placement;
  4. a fixed part (`[placement] fixed`, `[lock.at]`, locked) is its own module and fixed; a module with a fixed anchor is fixed
     as a block; mounting hardware is a fixed single-part module.
Internal arrangement: `clusters.reseat` seats far supports at their pins (decoupling at the pin, crystal adjacent), legal by
legality.Legal, before the geometry is cut (`--no-arrange` keeps the source placement).

Per module (the `modules` section of the map): anchor, members with pose relative to the anchor, fixed flag, side, area (mm2 of
member courtyards), bounding box, PORTS per side (the external nets, the number of pins and which side of the block they face:
N/E/S/W in the block frame at rotation 0), and its own FAN-OUT solved in isolation (fanout_helper `modules` mode: only the
module's parts exist; escape stubs and vias are kept as geometry relative to the anchor). A module placed at (X, Y) with rotation
th in {0, 90, 180, 270} puts a member at (X, Y) + R(th) * rel, angle + th (KiCad rotation, y down).
"""
import json
import math
import os
import re
import subprocess
import tempfile
import time
from collections import Counter, defaultdict

from . import geom as GM

SMALL = re.compile(r"^(C|R|L|FB|Y|X|XO|D|LED|F)\d")
MECH = re.compile(r"^(MH|MK|FID|TP|H)\d")
SIDES = ("N", "E", "S", "W")


def _small(ref, part):
    return bool(SMALL.match(ref)) and part["n_pads"] <= 2


def _centre(part):
    c = part["court"]
    return ((c[0] + c[2]) / 2, (c[1] + c[3]) / 2)


def fixed_set(B, cons):
    """Parts the floorplan holds: [placement] fixed, [placement] anchors, [lock.at], locked parts, mounting hardware and the
    refs of a `[placement.group.X]` that has a `centre` (the CM5 pair), minus `[floorplan] movable = [refs]`. Detected anchors
    (every connector / module) are NOT held unless listed: the owner's list decides."""
    from . import preroute as PR
    cons = cons or {}
    pl = cons.get("placement") or {}
    fx = set(PR.explicit_fixed(B, cons)) | set(pl.get("anchors", []) or [])
    fx |= {r for r in B["parts"] if MECH.match(r)}
    for g in (pl.get("group") or {}).values():
        if isinstance(g, dict) and g.get("centre") and g.get("refs"):
            fx |= set(g["refs"])
    return {r for r in fx if r in B["parts"]} - set((cons.get("floorplan") or {}).get("movable", []))


ATTACH_MM = 15.0                    # a support farther than this from its module's anchor is its own module


def build(B, cons=None, spec=None, arrange=True, log=None):
    """-> {"modules": [module dicts], "of": {ref: module name}} from the board model B (mutated only when `arrange`)."""
    from . import clusters as CL, legality as LGL, netclass as NC, routable as RB
    cons = cons or {}
    parts = B["parts"]
    fixed = fixed_set(B, cons)
    cls = NC.classify(B, cons, spec)
    cl, links = CL.build(B, cons, spec)
    if arrange:
        x0, y0, x1, y1 = B["bbox"]
        L = LGL.Legal(B, cons, fixed)
        CL.reseat(B, cl, everything=False, bounds=(x0 + 0.2, y0 + 0.2, x1 - 0.2, y1 - 0.2), legal=L, strict=True, log=log)
    owner = {s["ref"]: c["ic"] for c in cl for s in c["supports"] if s["sure"]}
    groups = {}
    pg = (cons.get("placement") or {}).get("group") or {}
    for name, g in sorted(pg.items()):
        if isinstance(g, dict) and g.get("refs"):
            for r in g["refs"]:
                if r in parts:
                    groups[r] = name
    anchors = [r for r in sorted(parts) if not _small(r, parts[r])]
    mods = {}
    of = {}
    for r in anchors:
        key = groups.get(r) or r
        m = mods.setdefault(key, {"name": key, "anchor": None, "refs": [], "group": bool(groups.get(r))})
        m["refs"].append(r)
        of[r] = key
    for k, m in mods.items():
        m["anchor"] = sorted(m["refs"], key=lambda r: (-parts[r]["n_pads"], r))[0]
    pads = NC.net_pads(B)
    rail = {n for n, i in cls.items() if i["class"] == "rail"} | {n for n in pads if NC.is_ground(n)}
    # pass 2: owned supports
    left = []
    for r in sorted(parts):
        if r in of or MECH.match(r):
            continue
        ic = owner.get(r)
        if ic and ic in of:
            of[r] = of[ic]
            mods[of[ic]]["refs"].append(r)
        else:
            left.append(r)
    # pass 3: connectivity, then nearest
    mod_c = {k: _centre(parts[m["anchor"]]) for k, m in mods.items()}
    for r in left:
        score = Counter()
        for q in parts[r]["pads"]:
            n = q["net"]
            if not n or n in rail:
                continue
            for o in pads.get(n, []):
                if o["ref"] in of and o["ref"] != r:
                    score[of[o["ref"]]] += 1
        if score:
            best = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        else:
            cx, cy = _centre(parts[r])
            best = min(sorted(mod_c), key=lambda k: math.hypot(mod_c[k][0] - cx, mod_c[k][1] - cy))
        of[r] = best
        mods[best]["refs"].append(r)
    # far members (a rail cap owned by an IC across the board) would make the block huge: they stand alone
    for key in sorted(mods):
        m = mods[key]
        ax, ay = _centre(parts[m["anchor"]])
        for r in list(m["refs"]):
            if r == m["anchor"] or (m["group"] and r in groups):
                continue
            cx, cy = _centre(parts[r])
            if math.hypot(cx - ax, cy - ay) > ATTACH_MM or (r in fixed and m["anchor"] not in fixed):
                m["refs"].remove(r)
                of.pop(r)
    # mechanical hardware: fixed single-part modules
    for r in sorted(parts):
        if r not in of:
            mods[r] = {"name": r, "anchor": r, "refs": [r], "group": False}
            of[r] = r
    out = []
    for key in sorted(mods):
        m = mods[key]
        a = parts[m["anchor"]]
        ax, ay = a["pos"]
        mem = {}
        for r in sorted(m["refs"]):
            p = parts[r]
            mem[r] = {"dx": round(p["pos"][0] - ax, 4), "dy": round(p["pos"][1] - ay, 4), "ang": round(p["angle"], 4), "side": p["side"]}
        mod = {"name": key, "anchor": m["anchor"], "members": mem, "fixed": bool(m["anchor"] in fixed or MECH.match(m["anchor"])),
               "pos": [round(ax, 4), round(ay, 4)], "angle": round(a["angle"], 4), "side": a["side"],
               "fixed_members": sorted(r for r in m["refs"] if r in fixed and r != m["anchor"])}
        _geometry(B, mod)
        out.append(mod)
    ports(B, out, of, cls)
    return {"modules": out, "of": of}


def pose(mod, X, Y, th):
    """{ref: (x, y, angle)} of the members with the anchor at (X, Y) and the block turned th degrees."""
    out = {}
    for r, m in mod["members"].items():
        rx, ry = GM.rot(m["dx"], m["dy"], th)
        out[r] = (X + rx, Y + ry, (m["ang"] + th + 180.0) % 360.0 - 180.0)
    return out


def _geometry(B, mod):
    parts = B["parts"]
    ax, ay = mod["pos"]
    rects, area = [], 0.0
    for r in mod["members"]:
        c = parts[r]["court"]
        rects.append(c)
        area += max(0.0, (c[2] - c[0]) * (c[3] - c[1]))
    mod["bbox"] = [round(min(c[0] for c in rects) - ax, 3), round(min(c[1] for c in rects) - ay, 3),
                   round(max(c[2] for c in rects) - ax, 3), round(max(c[3] for c in rects) - ay, 3)]
    mod["area"] = round(area, 2)
    mod["bbox_area"] = round((mod["bbox"][2] - mod["bbox"][0]) * (mod["bbox"][3] - mod["bbox"][1]), 2)


def ports(B, mods, of, cls):
    """per module: {net: {"N": n, "E": n, "S": n, "W": n}} for the nets that leave the block (pins of members vs pins elsewhere)."""
    from . import netclass as NC
    parts = B["parts"]
    pads = NC.net_pads(B)
    for mod in mods:
        mem = set(mod["members"])
        ax, ay = mod["pos"]
        x0, y0, x1, y1 = mod["bbox"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        pt = {}
        for n, ps in pads.items():
            inside = [q for q in ps if q["ref"] in mem]
            if not inside or len(inside) == len(ps):
                continue
            c = cls.get(n, {})
            e = {"N": 0, "E": 0, "S": 0, "W": 0}
            for q in inside:
                rx, ry = q["x"] - ax, q["y"] - ay
                d = {"N": ry - y0, "S": y1 - ry, "W": rx - x0, "E": x1 - rx}
                e[min(sorted(d), key=lambda k: d[k])] += 1
            pt[n] = {"c": c.get("class", "signal"), "pins": len(inside), "sides": {k: v for k, v in e.items() if v}}
        mod["ports"] = pt
        tot = {k: sum(v["sides"].get(k, 0) for v in pt.values()) for k in SIDES}
        mod["ports_per_side"] = tot


def fanouts(board, cons, spec, mods, a):
    """Solve each module's fan-out in isolation; stores `fanout` on every module. -> seconds."""
    from . import fanout as FO, routediag as RD
    B = RD.load(board)
    pol = FO.policy(B, cons, spec)
    job = {"params": FO.params(a, pol["pr"]), "parts": pol["parts"], "nets": pol["nets"], "away": pol["away"],
           "signal_layers": pol["signal_layers"], "check_only": True,
           "modules": {m["name"]: {"refs": sorted(m["members"]), "origin": m["pos"]} for m in mods
                       if any(pol["parts"].get(r, {}).get("mode") not in (None, "skip") for r in m["members"])}}
    wd = tempfile.mkdtemp(prefix="modfan-")
    jp = os.path.join(wd, "job.json")
    json.dump(job, open(jp, "w"))
    t0 = time.time()
    r = subprocess.run(["/usr/bin/python3", FO.HELPER, jp, board, os.path.join(wd, "unused.kicad_pcb")], capture_output=True, text=True)
    line = [l for l in r.stdout.splitlines() if l.startswith(FO.MARK)]
    if not line:
        raise SystemExit("modules: fan-out helper failed:\n" + (r.stderr or r.stdout)[-1500:])
    res = json.loads(line[-1][len(FO.MARK):])
    for m in mods:
        f = res.get(m["name"])
        if f is None:
            m["fanout"] = {"req": 0, "ok": 0, "blocked": []}
            continue
        req = len(f["pins"]) + len(f["thermal"])
        ok = sum(1 for p in f["pins"] if p["ok"]) + len(f["thermal"])
        m["fanout"] = {"req": req, "ok": ok, "pins": f["pins"], "thermal": f["thermal"],
                       "blocked": [f"{p['ref']}.{p['pin']}" for p in f["pins"] if not p["ok"]]}
    return time.time() - t0


def run(board, cons, spec, arrange=True, fan=True, a=None, log=None):
    from . import routediag as RD
    B = RD.load(board)
    M = build(B, cons, spec, arrange=arrange, log=log)
    secs = 0.0
    if fan:
        secs = fanouts(board, cons, spec, M["modules"], a)
    M["fan_secs"] = secs
    M["board"] = B
    return M


def store(path, M):
    """write the `modules` section into the map file at `path` (created from the board when missing)."""
    from . import fluxmap as FM
    m = FM.load(path) if os.path.exists(path) else {"fluxmap": 1}
    m["modules"] = M["modules"]
    FM.save(m, path)


def cmd_modules(a):
    from . import constraints as C
    from . import fanout as FO
    spec = json.load(open(a.spec)) if a.spec else None
    cons = C.load(a.constraints)
    from . import questions as QS
    S, cons, spec = QS.guard(a, "modules", a.board, cons, spec)
    if not hasattr(a, "reach"):
        a.reach = 2.2
    t0 = time.time()
    M = run(a.board, cons, spec, arrange=not a.no_arrange, fan=not a.no_fanout, a=_fanargs(a))
    mods = M["modules"]
    path = a.map or os.path.splitext(a.board)[0] + ".fluxmap.json"
    store(path, M)
    S.mark(path)
    nm = len(mods)
    nfix = sum(1 for m in mods if m["fixed"])
    multi = sorted((m for m in mods if len(m["members"]) > 1), key=lambda m: -m["area"])
    tot = {k: sum(m["ports_per_side"][k] for m in mods) for k in SIDES}
    blocked = [(m["name"], m["fanout"]["blocked"]) for m in mods if m.get("fanout") and m["fanout"]["blocked"]]
    req = sum(m["fanout"]["req"] for m in mods if m.get("fanout"))
    ok = sum(m["fanout"]["ok"] for m in mods if m.get("fanout"))
    print(f"modules: {nm} modules ({nfix} fixed, {len(multi)} with support parts, {sum(len(m['members']) for m in mods)} parts), "
          f"fan-out in isolation {ok}/{req} pins, {len(blocked)} modules with blocked pins; ports N{tot['N']} E{tot['E']} S{tot['S']} W{tot['W']}; "
          f"{time.time() - t0:.1f} s; wrote section 'modules' into {path}")
    top = len(multi) if a.full_output else min(len(multi), a.top or 12)
    print(f"largest {top} modules (area mm2 of courtyards, parts, ports N/E/S/W, fan-out ok/req):")
    for m in multi[:top]:
        ps = m["ports_per_side"]
        fo = m.get("fanout") or {}
        print(f"  {m['name']:8s} {m['area']:7.1f} mm2  {len(m['members']):3d} parts  ports {ps['N']}/{ps['E']}/{ps['S']}/{ps['W']}"
              f"  fan-out {fo.get('ok', 0)}/{fo.get('req', 0)}" + ("  fixed" if m["fixed"] else ""))
    for n, b in blocked[: (len(blocked) if a.full_output else 10)]:
        print(f"  blocked in isolation: {n}: {', '.join(b)}")
    return 0


def _fanargs(a):
    import argparse
    return argparse.Namespace(track=None, clearance=None, via=None, drill=None, via_pad_gap=0.2, reach=2.2, lane=1.0, max_dom=60,
                              nodes=200000, repairs=12, check_only=True)


def add_parser(sub):
    p = sub.add_parser("modules", help="group ICs/connectors with their support parts into rigid modules: area, ports per side, "
                                       "isolated fan-out; stored in the map's `modules` section")
    p.add_argument("board")
    p.add_argument("--constraints", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--map", default=None, help="map file to update (default <board>.fluxmap.json)")
    p.add_argument("--no-arrange", action="store_true", help="keep the source placement (no clusters.reseat)")
    p.add_argument("--no-fanout", action="store_true", help="skip the per-module fan-out")
    p.add_argument("--full", dest="full_output", action="store_true")
    p.add_argument("-q", "--quiet", dest="quiet", action="store_true")
    p.add_argument("--top", type=int, default=None)
    from . import questions as QS
    QS.add_args(p)
    p.set_defaults(fn=cmd_modules)
