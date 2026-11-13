"""`pairs`: lay the differential pairs by construction, before general routing (constructive flow, after fan-out).

Per pair (netclass partner links; order: project net-class priority, pair kind, shortest span) and per SECTION (two
terminals = P pad + N pad on one part or side by side; a net with series parts or several drops is a tree of sections):
a coupled centre line is searched on a clearance-inflated grid (pairlay.astar: 45 degree moves, layer change only as a
matched via pair, never on plane layers, vias cost `via_cost_mm`), both legs are emitted as its +-(w + gap)/2 mitred
parallels with short uncoupled break-outs at the pads, the skew is closed with a bump on the short leg, every piece is
checked exactly against foreign copper (pairgeo.Items) and written LOCKED.  Width / gap / via come from the pair's
project net class (`diff_pair_width`, `diff_pair_gap`; constraints `[pairs.FAMILY] width_mm / gap_mm` override; never
thinner than the class).  A pair that cannot be laid keeps its fan-out copper and is reported with the reason and what
blocks it (PAIR_NOT_LAID) and stays with the router.  `--plan-only` writes nothing: it returns the centre-line
corridors (escreserve.py turns them into placement reservations).

    [pairlay]
    enabled = true  step_mm = 0.1  clearance_mm = (class, floor 0.127) + 0.003  via_cost_mm = 20  turn_cost_mm = 0.4
    max_nodes = 400000  pad_max_mm = 8.0  ring_mm = 1.0"""
import fnmatch
import json
import math
import os
import time
import uuid

import numpy as np

from . import pairgeo as G
from . import pairlay as PL

KIND_RANK = {"pcie": 0, "usb3": 0, "eth": 1, "usb2": 2, "other": 3, "audio": 4}
FLOOR = 0.127


def params(cons):
    e = dict((cons or {}).get("pairlay") or {})
    return {"enabled": e.get("enabled", True) is not False, "step": float(e.get("step_mm", 0.1)), "clearance": e.get("clearance_mm"),
            "via_cost": float(e.get("via_cost_mm", 20.0)), "turn_cost": float(e.get("turn_cost_mm", 0.4)),
            "nodes": int(e.get("max_nodes", 400000)), "pad_max": float(e.get("pad_max_mm", 16.0)), "ring": float(e.get("ring_mm", 1.0)),
            "margin": float(e.get("margin_mm", 0.03)), "inner_cost": float(e.get("inner_cost", 1.15)), "passes": int(e.get("passes", 3)),
            "nodes_per_mm": int(e.get("nodes_per_mm", 25000)), "via_exit": e.get("via_exit", True) is not False}


def project_classes(board):
    """(classes by name, patterns, assignments) of the board's .kicad_pro; empty when there is none."""
    try:
        ns = json.load(open(os.path.splitext(board)[0] + ".kicad_pro")).get("net_settings", {})
    except (OSError, ValueError, TypeError):
        return {}, [], {}
    return ({c.get("name"): c for c in ns.get("classes") or []}, ns.get("netclass_patterns") or [], ns.get("netclass_assignments") or {})


def class_of(net, classes, patterns, assign):
    """the net's project class merged over Default -> dict (name, priority, sizes); {} without a project."""
    d = dict(classes.get("Default") or {})
    names = list(assign.get(net) or []) + [p["netclass"] for p in patterns if fnmatch.fnmatchcase(net, p.get("pattern", ""))]
    for nm in names:
        if nm in classes:
            d.update({k: v for k, v in classes[nm].items() if v not in (None, "")})
            break
    return d


def rules_for(board, B, cons, R, p, P, proj=None):
    """track width, gap, clearance, via of the pair whose first net is `p`."""
    from . import constraints as C
    c = class_of(p, *(proj or project_classes(board)))
    fam = max((f for f in ((cons or {}).get("pairs") or {}) if p.startswith(f)), key=len, default=None)
    fv = ((cons or {}).get("pairs") or {}).get(fam) or {} if fam else {}
    zw, zg = C.PAIR_GEOM_BY_Z.get(int(fv["impedance_diff"]), (None, None)) if "impedance_diff" in fv and not c else (None, None)
    w = float(fv.get("width_mm") or c.get("diff_pair_width") or c.get("track_width") or zw or max(B["track"], 0.15))
    gap = float(fv.get("gap_mm") or c.get("diff_pair_gap") or zg or 0.2)
    w = max(w, float(c.get("diff_pair_width") or 0.0), R.get("min_track", 0.0))
    clr = float(P["clearance"]) if P["clearance"] else max(float(c.get("clearance") or 0.0), R.get("min_clearance", 0.0), FLOOR) + 0.003
    gap = max(gap, float(c.get("diff_pair_gap") or 0.0), R.get("min_clearance", 0.0))
    via = max(float(c.get("via_diameter") or R.get("via", 0.45)), R.get("min_via", 0.0))
    drill = max(float(c.get("via_drill") or R.get("drill", 0.2)), R.get("min_drill", 0.0))
    return {"w": w, "gap": gap, "clr": clr, "via": via, "drill": drill, "via_gap": max(float(c.get("diff_pair_via_gap") or 0.0), gap, clr),
            "target": fv.get("impedance_diff"), "class": c.get("name") or "-", "prio": int(c.get("priority", 1 << 31)) if c else 1 << 31}


def terminals(B, p, n, pad_max):
    """([{p, n, c, sep}], [unpaired pads]): P and N pads matched same part first, then nearest."""
    Pp = [q for q in B["pads"] if q["net"] == p and not q.get("np")]
    Np = [q for q in B["pads"] if q["net"] == n and not q.get("np")]
    cand = sorted((a["ref"] != b["ref"], math.hypot(a["x"] - b["x"], a["y"] - b["y"]), i, j) for i, a in enumerate(Pp) for j, b in enumerate(Np))
    ui, uj, out = set(), set(), []
    for _o, d, i, j in cand:
        if i in ui or j in uj or d > pad_max or d < 1e-6:
            continue
        ui.add(i)
        uj.add(j)
        a, b = Pp[i], Np[j]
        out.append({"p": a, "n": b, "c": ((a["x"] + b["x"]) / 2, (a["y"] + b["y"]) / 2), "sep": d,
                    "name": a["ref"] if a["ref"] == b["ref"] else f"{a['ref']}/{b['ref']}"})
    return out, [q for i, q in enumerate(Pp) if i not in ui] + [q for j, q in enumerate(Np) if j not in uj]


def tree(terms):
    """minimum spanning tree over the terminal centres -> [(ia, ib)] shortest first."""
    if len(terms) < 2:
        return []
    d = lambda a, b: math.hypot(terms[a]["c"][0] - terms[b]["c"][0], terms[a]["c"][1] - terms[b]["c"][1])
    inn, out = {0}, []
    while len(inn) < len(terms):
        _l, a, b = min((d(a, b), a, b) for a in inn for b in range(len(terms)) if b not in inn)
        inn.add(b)
        out.append((a, b))
    return sorted(out, key=lambda e: d(*e))


def _gates(grid, allowed, li_list, term, R, outward, lats):
    """{(li, j, i): [(d, pol, cost)]} gate cells around a terminal; outward=True for the start (leaving), False = arriving.
    A gate with travel direction v is ahead of both pads (behind them when arriving), sees them side by side (never along
    the pad axis) and sits between their two lines of travel (+- lat): the break-outs are short and cannot cross."""
    cx, cy = term["c"]
    px, py, qx, qy = term["p"]["x"], term["p"]["y"], term["n"]["x"], term["n"]["y"]
    sep = term["sep"]
    j0, i0 = grid.cell(cx - R, cy - R)
    j1, i1 = grid.cell(cx + R, cy + R)
    j0, i0, j1, i1 = max(0, j0), max(0, i0), min(grid.ny - 1, j1), min(grid.nx - 1, i1)
    sg = 1.0 if outward else -1.0
    out = {}
    for li in li_list:
        dirs = []
        for d, (vx, vy) in enumerate(PL.DVEC):
            cr = vx * (py - qy) - vy * (px - qx)           # cross(travel, P - N): > 0 = P on the left
            if abs(cr) >= PL.SIN_MIN * sep:
                dirs.append((d, vx, vy, 1 if cr > 0 else 0, abs(cr) / 2 + lats[li]))
        m = allowed[grid.sig[li]][j0:j1 + 1, i0:i1 + 1]
        for jj, ii in np.argwhere(m):
            j, i = int(jj) + j0, int(ii) + i0
            x, y = grid.xy(j, i)
            r = math.hypot(x - cx, y - cy)
            if r > R:
                continue
            lst = [(d, pol, 1.5 * r) for d, vx, vy, pol, half in dirs
                   if abs(-vy * (x - cx) + vx * (y - cy)) <= half
                   and sg * (vx * (x - px) + vy * (y - py)) >= -0.05 and sg * (vx * (x - qx) + vy * (y - qy)) >= -0.05]
            if lst:
                out[(li, j, i)] = lst
    return out


def lay_section(ctx, D, O, ta, tb, rule, nets, plan_only=False, ring=None):
    """one pair section between terminals ta and tb -> {ok, P, N, vias, layers, lenP, lenN, corridor | reason, blockers}."""
    grid, items, P = ctx["grid"], ctx["items"], ctx["P"]
    sig, step = grid.sig, grid.step
    clr, geo = rule["clr"], rule["geo"]
    wl = {l: geo[l][0] for l in sig}
    dl = {l: (geo[l][0] + geo[l][1]) / 2 for l in sig}
    envl = {l: 2 * geo[l][0] + geo[l][1] for l in sig}
    dist, env = max(dl.values()), max(envl.values())
    span = math.hypot(ta["c"][0] - tb["c"][0], ta["c"][1] - tb["c"][1])
    lays = [[li for li, l in enumerate(sig) if l in t["p"]["layers"] and l in t["n"]["layers"]] for t in (ta, tb)]
    for t, ls in zip((ta, tb), lays):
        if not ls:
            return {"ok": False, "reason": f"{t['name']}: the two pads share no signal layer", "blockers": []}
    hvmin = max((rule["via"] + rule["via_gap"]) / 2, dist)
    kv_s, kv_d = math.ceil(hvmin / step - 1e-9), math.ceil(hvmin / (step * math.sqrt(2)) - 1e-9)
    hv = [(kv_s if (di == 0 or dj == 0) else kv_d) * step * math.hypot(di, dj) for di, dj in (PL.DIRS[(d + 2) % 8] for d in range(8))]
    K = max(2, math.ceil(max(2 * dist * PL.T22, max(hv) - dist + dist * PL.T22) / step + 0.3))
    viaok = None
    banned = set()
    reason, blockers = "no path", []
    for attempt in range(5):
        margin = P["margin"] + 0.05 * (attempt // 2 if attempt < 4 else 2)
        thr = clr + env / 2 + margin
        allowed = {l: D[l] >= clr + envl[l] / 2 + margin for l in sig}
        Rs = [t["sep"] / 2 + max(t["p"]["w"], t["p"]["h"], t["n"]["w"], t["n"]["h"]) / 2 + env / 2 + clr + (ring or P["ring"]) for t in (ta, tb)]
        ga = {k: v for k, v in _gates(grid, allowed, lays[0], ta, Rs[0], True, [dl[l] for l in sig]).items() if k not in banned}
        gb = {k: v for k, v in _gates(grid, allowed, lays[1], tb, Rs[1], False, [dl[l] for l in sig]).items() if k not in banned}
        for t, g, R in ((ta, ga, Rs[0]), (tb, gb, Rs[1])):
            if not g:
                return {"ok": False, "reason": f"no room to leave {t['name']} coupled ({env:.2f} mm envelope + {clr:.3f} clearance)",
                        "blockers": [b for b in grid.blockers(D, O, [sig[li] for li in lays[0 if t is ta else 1]], t["c"][0], t["c"][1], R, thr)
                                     if t["p"]["ref"] + "." not in b and t["n"]["ref"] + "." not in b][:5] or
                        grid.blockers(D, O, [sig[li] for li in lays[0 if t is ta else 1]], t["c"][0], t["c"][1], R, thr)}
        nl = len(sig)
        seeds = [(c, li, j, i, d, pol) for (li, j, i), lst in ga.items() for d, pol, c in lst]
        goals = {}
        for (li, j, i), lst in gb.items():
            goals[(j * grid.nx + i) * nl + li] = {d * 2 + pol: c for d, pol, c in lst}
        if nl > 1 and viaok is None:
            viaok = np.ones((grid.ny, grid.nx), bool)
            for l in D:
                viaok &= D[l] >= clr + rule["via"] / 2
            vm = PL.via_masks(viaok, kv_s, kv_d)
        gj, gi = grid.cell(*tb["c"])
        H = {}
        free = [allowed[l] for l in sig]
        for pol in (0, 1):
            gl = [(li, j, i) for (li, j, i), lst in gb.items() if any(p == pol for _d, p, _c in lst)]
            if gl and any(p == pol for lst in ga.values() for _d, p, _c in lst):
                H[pol] = PL.coarse_dist(free, viaok, gl, 4, step, P["via_cost"])
        seeds = [s for s in seeds if s[5] in H]
        states, info = PL.astar(grid.nx, grid.ny, sig, allowed, vm if nl > 1 else None, seeds, goals, H, 4, K, step,
                                P["turn_cost"], P["via_cost"], P["nodes"] + int(P["nodes_per_mm"] * span),
                                [1.0 if l in (grid.B["copper"][0], grid.B["copper"][-1]) else P["inner_cost"] for l in sig]) if seeds else (None, {"reason": "no gate with a matching polarity"})
        if not states:
            reason = f"{info['reason']} for the coupled centre line ({env:.2f} mm envelope + {clr:.3f} clearance)"
            near = info.get("near")
            if not near and ga:                              # nothing reachable: the open area round the start, nearest the goal
                Hs = PL.coarse_dist(free, viaok, list(ga), 4, step, P["via_cost"])
                idx = np.argwhere(Hs < PL.PK.INF / 2)
                if len(idx):
                    k = int(np.argmin(np.hypot((idx[:, 2] * 4 + 2 - gi), (idx[:, 1] * 4 + 2 - gj))))
                    near = (int(idx[k, 0]), int(idx[k, 1]) * 4 + 2, int(idx[k, 2]) * 4 + 2)
            if near:
                li, j, i = near
                x, y = grid.xy(j, i)
                reason += f"; closest approach ({x:.1f}, {y:.1f}) {sig[li]}, {math.hypot(x - tb['c'][0], y - tb['c'][1]):.1f} mm short of {tb['name']}"
                blockers = grid.blockers(D, O, sig, x, y, env + clr + 0.6, thr)
            return {"ok": False, "reason": reason, "blockers": blockers}
        pol = info["pol"]
        runs, vias = PL.runs_of(states, sig, grid.xy)
        if plan_only:
            return {"ok": True, "corridor": [{"layer": r["layer"], "pts": [[round(x, 3), round(y, 3)] for x, y in r["pts"]], "width": round(envl[r["layer"]] + 2 * clr, 3)}
                                             for r in runs], "vias": len(vias), "layers": [r["layer"] for r in runs]}
        cp, p0, p1 = PL.legs(runs, vias, dl, hv, wl, rule["via"], pol)
        cn, n0, n1 = PL.legs(runs, vias, dl, hv, wl, rule["via"], -pol)
        g1, it1 = PL.check(items, cp, nets[0], clr, ctx["skip"])
        g2, it2 = PL.check(items, cn, nets[1], clr, ctx["skip"])
        if min(g1, g2) < clr - 1e-6:
            it = it1 if g1 <= g2 else it2
            reason, blockers = f"coupled path {min(g1, g2):.3f} mm from {it['label']} (exact check)", [it["label"]]
            continue
        mark = items.mark()
        _claim(items, cp, nets[0])
        _claim(items, cn, nets[1])
        t0, t1 = runs[0]["t0"], runs[-1]["t1"]
        skew = PL.piece_len(cp) - PL.piece_len(cn) + ta.get("bias", 0.0) + tb.get("bias", 0.0)
        fb = lambda pad, e, layer, net: PL.stub(grid.B, items, ctx["base_n"], ctx["ign"], pad, e, layer, wl[layer], net, clr, ctx["skip"])
        ba = PL.breakout(items, (ta["p"], ta["n"]), (p0, n0), t0, runs[0]["layer"], wl[runs[0]["layer"]], nets, clr, ctx["skip"], bias=skew, fallback=fb)
        if ba[0] is None:
            items.rollback(mark)
            reason, blockers = ba[1], [ba[2]["label"]] if ba[2] else []
            banned |= _ban(states[0], grid, 0.3)
            continue
        _claim(items, ba[0], nets[0])
        _claim(items, ba[1], nets[1])
        bb = PL.breakout(items, (tb["p"], tb["n"]), (p1, n1), (-t1[0], -t1[1]), runs[-1]["layer"], wl[runs[-1]["layer"]], nets, clr, ctx["skip"], bias=skew + ba[2], fallback=fb)
        if bb[0] is None:
            items.rollback(mark)
            reason, blockers = bb[1], [bb[2]["label"]] if bb[2] else []
            banned |= _ban(states[-1], grid, 0.3)
            continue
        _claim(items, bb[0], nets[0])
        _claim(items, bb[1], nets[1])
        skew += ba[2] + bb[2]
        left_mm = 0.0
        if abs(skew) > ctx["tol"](nets[0]):
            if skew > 0:
                cn, left_mm = PL.bump(items, cn, nets[1], skew, -pol, clr, ctx["skip"], 0, len(cn))
            else:
                cp, left_mm = PL.bump(items, cp, nets[0], -skew, pol, clr, ctx["skip"], 0, len(cp))
        Pp, Np = ba[0] + cp + bb[0], ba[1] + cn + bb[1]
        items.rollback(mark)
        if left_mm > ctx["limit"](nets[0]):                 # out of tolerance copper is not locked in: the router keeps the pair
            reason, blockers = f"skew {left_mm:.2f} mm over the {ctx['limit'](nets[0]):g} mm limit after compensation (break-outs of unequal length, no room for bumps)", []
            banned |= _ban(states[0], grid, 0.3) | _ban(states[-1], grid, 0.3)
            continue
        _claim(items, Pp, nets[0])
        _claim(items, Np, nets[1])
        return {"ok": True, "P": Pp, "N": Np, "vias": len(vias), "layers": [r["layer"] for r in runs],
                "lenP": PL.piece_len(Pp), "lenN": PL.piece_len(Np), "skew": round(abs(skew) if abs(skew) <= ctx["tol"](nets[0]) else left_mm, 3),
                "uncompensated": round(left_mm, 3)}
    return {"ok": False, "reason": reason, "blockers": blockers}


def _claim(items, pieces, net):
    for p in pieces:
        if p[0] == "seg":
            items.seg(p[2], p[3], p[4], p[1], net)
        else:
            items.via(p[1], p[2], p[3], net)


def _ban(state, grid, r):
    li, j, i, _d = state
    k = int(r / grid.step)
    return {(li, j + a, i + b) for a in range(-k, k + 1) for b in range(-k, k + 1)}


def direct_section(ctx, ta, tb, rule, nets):
    """terminals too close for a coupled run: each net pad to pad with one dog-leg, checked exactly."""
    items, clr = ctx["items"], rule["clr"]
    sig = ctx["grid"].sig
    out = []
    mark = items.mark()
    for key, net in zip("pn", nets):
        a, b = ta[key], tb[key]
        done = False
        for l in [l for l in sig if l in a["layers"] and l in b["layers"]]:
            u = G.unit((a["x"], a["y"]), (b["x"], b["y"]))
            for pts in PL.doglegs((a["x"], a["y"]), (b["x"], b["y"]), (-u[0], -u[1]), exts=(0.0,)) + [[(a["x"], a["y"]), (b["x"], b["y"])]]:
                pcs = [("seg", l, x, y, rule["geo"][l][0]) for x, y in zip(pts, pts[1:])]
                if PL.check(items, pcs, net, clr, ctx["skip"])[0] >= clr - 1e-6:
                    _claim(items, pcs, net)
                    out.append(pcs)
                    done = True
                    break
            if done:
                break
        if not done:
            items.rollback(mark)
            return None
    if abs(PL.piece_len(out[0]) - PL.piece_len(out[1])) > ctx["limit"](nets[0]):
        items.rollback(mark)
        return None
    return {"ok": True, "P": out[0], "N": out[1], "vias": 0, "layers": [out[0][0][1]], "lenP": PL.piece_len(out[0]), "lenN": PL.piece_len(out[1]),
            "direct": True, "uncompensated": 0.0}


def pair_layers(B, cons):
    """(signal layers, plane layers) for pairs.  `[preroute] signal_layers / plane_layers` decide when given; otherwise an
    INNER layer is a plane when one netted zone covers the whole board (polygon area >= 90 % of the outline box).  An inner
    layer with a partial fill is a signal layer that also carries a pour (the pour is refilled round the tracks)."""
    from . import preroute as PR
    pc = (cons or {}).get("preroute", {}) or {}
    if isinstance(pc.get("signal_layers"), list) or isinstance(pc.get("plane_layers"), list):
        return PR.layer_split(B, cons)
    cu = list(B["copper"])
    x0, y0, x1, y1 = B["bbox"]
    board = max((x1 - x0) * (y1 - y0), 1e-6)
    plane = set()
    for z in B["zones"]:
        p = z["pts"]
        if z["net"] and not z["keepout"] and len(p) >= 3:
            a = abs(sum(p[i][0] * p[(i + 1) % len(p)][1] - p[(i + 1) % len(p)][0] * p[i][1] for i in range(len(p)))) / 2
            if a >= 0.9 * board:
                plane |= {l for l in z["layers"] if l in cu[1:-1]}
    return [l for l in cu if l not in plane], [l for l in cu if l in plane]


def layer_geometry(rule, sig, planes, copper, stackup, floor):
    """{layer: (width, gap, source)}: the class geometry on the outer layers; on an inner signal layer, when the pair has an
    impedance target (`[pairs.FAMILY] impedance_diff`) and the stackup is known, the width impedance.py solves for the class
    gap there (never thinner than the class)."""
    from . import impedance as IM
    geo = {l: (rule["w"], rule["gap"], "class") for l in sig}
    if not rule.get("target") or not stackup:
        return geo
    for l in sig:
        if l in (copper[0], copper[-1]):
            continue
        try:
            g = IM.geometry(stackup, set(planes), l)
            o = IM.options(float(rule["target"]), True, g, floor=max(floor, 0.05), gap=rule["gap"])
        except Exception:                                    # noqa: BLE001 - no model for this layer: the class geometry stays
            o = []
        if o:
            geo[l] = (round(max(rule["w"], o[0]["width_mm"]), 3), rule["gap"], f"{o[0].get('model', 'stripline')} {rule['target']} ohm")
    return geo


def fan_exit(pad, old, copper):
    """the fan-out stub + via that leaves `pad` (old copper of its net) -> (via pad dict, [(kind, item)], stub mm) | None."""
    from . import routeconn as RC
    segs = [o for k, o in old if k == "seg" and o["layer"] in pad["layers"]]
    vias = [o for k, o in old if k == "via"]
    cur, chain, used, L = None, [], set(), 0.0
    for _ in range(5):
        for v in vias:
            if (cur is None and RC._in_pad(pad, v["x"], v["y"], 0.02)) or (cur is not None and math.hypot(v["x"] - cur[0], v["y"] - cur[1]) <= v["d"] / 2):
                d = v["d"]
                vp = {"ref": pad["ref"], "num": pad["num"], "x": v["x"], "y": v["y"], "w": d, "h": d, "ex": d / 2, "ey": d / 2, "min": d,
                      "layers": set(copper), "net": pad["net"], "angle": 0.0, "shape": "circle", "tht": True}
                return vp, chain + [("via", v)], L
        nxt = None
        for k, sg in enumerate(segs):
            if k in used:
                continue
            for a, b in ((sg["a"], sg["b"]), (sg["b"], sg["a"])):
                if (cur is None and RC._in_pad(pad, a[0], a[1], 0.02) and not RC._in_pad(pad, b[0], b[1], 0.0)) or \
                        (cur is not None and math.hypot(a[0] - cur[0], a[1] - cur[1]) <= 0.02):
                    nxt = (k, sg, b)
                    break
            if nxt:
                break
        if not nxt:
            return None
        used.add(nxt[0])
        chain.append(("seg", nxt[1]))
        L += math.hypot(nxt[1]["a"][0] - nxt[1]["b"][0], nxt[1]["a"][1] - nxt[1]["b"][1])
        cur = nxt[2]
    return None


def _old_pieces(keep):
    """{net: pieces} of kept fan-out items."""
    out = {}
    for kind, o in keep:
        out.setdefault(o["net"], []).append(("seg", o["layer"], o["a"], o["b"], o["w"]) if kind == "seg" else ("via", o["x"], o["y"], o["d"]))
    return out


def _section(ctx, D, O, ta, tb, rule, pr, plan_only):
    """one section, pads first; then through the fan-out via pair of either terminal (the pair leaves on another layer)."""
    grid, items, P = ctx["grid"], ctx["items"], ctx["P"]
    va, vb = ta.get("via"), tb.get("via")
    variants = [(ta, tb)] + ([(va, tb)] if va else []) + ([(ta, vb)] if vb else []) + ([(va, vb)] if va and vb else [])
    first = None
    d = math.hypot(ta["c"][0] - tb["c"][0], ta["c"][1] - tb["c"][1])
    for k, (a, b) in enumerate(variants):
        keep = a.get("keep", []) + b.get("keep", [])
        D2, O2, mark = D, O, items.mark()
        if keep and any(PL.check(items, pcs, net, rule["clr"], ctx["skip"])[0] < rule["clr"] - 1e-6 for net, pcs in _old_pieces(keep).items()):
            continue                                        # copper laid earlier already fouls this fan-out stub / via: no exit through it
        if keep:
            D2, O2 = {l: x.copy() for l, x in D.items()}, {l: x.copy() for l, x in O.items()}
            for net, pcs in _old_pieces(keep).items():
                grid.put(D2, O2, pcs, net)
                _claim(items, pcs, net)
        r = None
        if k == 0 and d < 1.5 and not plan_only:
            r = direct_section(ctx, a, b, rule, pr)
        if r is None:
            r = lay_section(ctx, D2, O2, a, b, rule, pr, plan_only)
            if not r["ok"] and k == 0 and "search budget" not in r["reason"]:          # a longer uncoupled break-out: gates further out
                r2 = lay_section(ctx, D2, O2, a, b, rule, pr, plan_only, ring=3 * P["ring"])
                r = r2 if r2["ok"] else r
        if not r["ok"] and k == 0 and d < 3.0 and not plan_only:
            r = direct_section(ctx, a, b, rule, pr) or r
        if r["ok"]:
            r["keep"] = keep
            r["exit"] = [t["name"] for t in (a, b) if t.get("keep")]
            return r
        items.rollback(mark)
        first = first or r
    return first


def _lay_pair(ctx, pr, info, rule, plan_only):
    """search one pair on the current state -> {e, pcs, keep, full, nok, skew}; the state is left as it was (see _apply)."""
    grid, items = ctx["grid"], ctx["items"]
    terms, lone, secs, span = info
    e = {"pair": list(pr), "class": rule["class"], "width": rule["w"], "gap": rule["gap"], "sections": [], "span": round(span, 2),
         "unpaired": [f"{q['ref']}.{q['num']}" for q in lone],
         "geometry": {l: {"width": g[0], "gap": g[1], "from": g[2]} for l, g in rule["geo"].items()}}
    out = {"e": e, "pcs": {pr[0]: [], pr[1]: []}, "keep": [], "full": False, "nok": 0, "skew": 0.0}
    if ctx["closed"](pr):
        e["status"], e["why"] = "kept", "already connected on the input board"
        return out
    if not secs:
        e["status"], e["why"] = "not-laid", "no two terminals (P and N pads further apart than [pairlay] pad_max_mm)" if len(terms) < 2 else "single terminal"
        return out
    if rule["gap"] + 1e-9 < ctx["min_clearance"]:
        e["status"], e["why"] = "not-laid", f"class gap {rule['gap']} below the clearance floor"
        return out
    D, O = grid.view()
    mark = items.mark()
    tp = time.time()
    for a, b in secs:
        ta, tb = terms[a], terms[b]
        r = _section(ctx, D, O, ta, tb, rule, pr, plan_only)
        s = {"from": ta["name"], "to": tb["name"], "ok": r["ok"], "mm": round(math.hypot(ta["c"][0] - tb["c"][0], ta["c"][1] - tb["c"][1]), 2)}
        if r["ok"] and plan_only:
            s.update(corridor=r["corridor"], vias=r["vias"], layers=r["layers"])
            out["nok"] += 1
        elif r["ok"]:
            out["nok"] += 1
            out["pcs"][pr[0]] += r["P"]
            out["pcs"][pr[1]] += r["N"]
            out["keep"] += [x for x in r.get("keep", []) if not any(x[1] is y[1] for y in out["keep"])]
            out["skew"] += r.get("skew") or 0.0
            s.update(vias=r["vias"], layers=r["layers"], lenP=round(r["lenP"], 3), lenN=round(r["lenN"], 3), direct=bool(r.get("direct")), skew=r.get("skew"),
                     uncompensated=r.get("uncompensated", 0.0), via_exit=r.get("exit", []))
            grid.put(D, O, r["P"], pr[0], "reserved")           # the next section of this pair sees this one
            grid.put(D, O, r["N"], pr[1], "reserved")
            for net, pcs in _old_pieces(r.get("keep", [])).items():
                grid.put(D, O, pcs, net, "reserved")
        else:
            s.update(reason=r["reason"], blockers=r.get("blockers", []))
        e["sections"].append(s)
    items.rollback(mark)
    e["secs"] = round(time.time() - tp, 1)
    out["full"] = full = out["nok"] == len(secs) and not lone
    e["status"] = ("planned" if plan_only else "laid") if full else ("partial" if out["nok"] else "not-laid")
    if not full:
        bad = [s for s in e["sections"] if not s["ok"]]
        e["why"] = "; ".join([f"{s['from']}>{s['to']}: {s['reason']}" for s in bad][:2] +
                             ([f"unpaired pad(s) {', '.join(e['unpaired'][:4])}"] if lone else []))
        e["blockers"] = sorted({b for s in bad for b in s.get("blockers", [])})[:8]
        e["blocked_by"] = " | ".join([s["reason"] for s in bad] + [b for s in bad for b in s.get("blockers", []) if not b.startswith("pad ")])
    return out


def _apply(ctx, pr, c, plan_only):
    """a pair's result becomes part of the state: its copper and kept fan-out are obstacles for every later pair."""
    grid, items = ctx["grid"], ctx["items"]
    if plan_only or not c["nok"]:
        return
    for net, pcs in list(c["pcs"].items()) + list(_old_pieces(c["keep"]).items()):
        _claim(items, pcs, net)
        grid.claim(pcs, net)
    for net in pr:
        if net in grid.old:
            grid.old[net] = [x for x in grid.old[net] if not any(x[1] is y[1] for y in c["keep"])]
    if c["full"]:
        grid.drop_old(pr)
        items.retire(nets=set(pr))


def plan(board, cons=None, spec=None, plan_only=False, only=None, B=None, log=lambda m: None):
    """-> {pairs: [...], laid: {net: pieces}, strip: [nets whose old copper goes], keep, kill, B, secs, order}.
    Negotiated order: a pair that fails on copper laid by this stage is moved ahead of the pair that laid it and the
    order is laid again (`[pairlay] passes`); the best result (pairs complete, then total skew) is kept."""
    import re
    from . import routediag as RD, netclass as NC, pairaudit as PA, routeconn as RC, routerules as RR
    from . import constraints as C
    t0 = time.time()
    cons = cons or {}
    P = params(cons)
    B = B or RD.load(board)
    if board:                                              # holes without a pad number are obstacles too
        try:
            B = dict(B, pads=B["pads"] + G.holes(board, B["copper"]))
        except (OSError, ValueError, IndexError):
            pass
    cls = NC.classify(B, cons, spec)
    pl = [pr for pr in PA.pair_list(cls) if not only or pr[0] in only or pr[1] in only]
    R = RR.rules(cons, board, B=B)
    proj = project_classes(board)
    sig, planes = pair_layers(B, cons)
    stackup = []
    if board and os.path.isfile(board):
        try:
            from . import review as RV
            stackup = RV.parse_stackup_text(open(board, errors="replace").read())
        except Exception:                                    # noqa: BLE001 - no stackup: class geometry on every layer
            stackup = []
    rules = {pr: rules_for(board, B, cons, R, pr[0], P, proj) for pr in pl}
    for r in rules.values():
        r["geo"] = layer_geometry(r, sig, planes, B["copper"], stackup, R.get("min_track", 0.0))
    open0 = RC.text_open(B)
    res = {"pairs": [], "laid": {}, "strip": [], "kill": [], "keep": [], "B": B, "signal_layers": sig, "plane_layers": planes, "passes": 0}
    if not pl:
        res["secs"] = round(time.time() - t0, 1)
        return res
    clr = max(r["clr"] for r in rules.values())
    edge = max(float((RR._pro_rules(board) if board else {}).get("min_copper_edge_clearance") or 0.0), 0.3)
    pair_nets = {n for pr in pl for n in pr}
    old = {}
    for kind, src in (("seg", B["segs"]), ("via", B["vias"])):
        for o in src:
            if o["net"] in pair_nets:
                old.setdefault(o["net"], []).append((kind, o))
    info, key = {}, {}
    for pr in pl:
        terms, lone = terminals(B, pr[0], pr[1], P["pad_max"])
        if P["via_exit"] and not plan_only:
            for t in terms:
                fp, fn = fan_exit(t["p"], old.get(pr[0], []), B["copper"]), fan_exit(t["n"], old.get(pr[1], []), B["copper"])
                sep = math.hypot(fp[0]["x"] - fn[0]["x"], fp[0]["y"] - fn[0]["y"]) if fp and fn else 0.0
                if sep > 1e-3:
                    t["via"] = {"p": fp[0], "n": fn[0], "c": ((fp[0]["x"] + fn[0]["x"]) / 2, (fp[0]["y"] + fn[0]["y"]) / 2), "sep": sep,
                                "name": t["name"] + "@via", "keep": fp[1] + fn[1], "bias": fp[2] - fn[2]}
        secs = tree(terms)
        span = sum(math.hypot(terms[a]["c"][0] - terms[b]["c"][0], terms[a]["c"][1] - terms[b]["c"][1]) for a, b in secs)
        info[pr] = (terms, lone, secs, span)
        key[pr] = (rules[pr]["prio"], KIND_RANK.get(cls[pr[0]].get("kind"), 3), span, pr)
    cache = {}

    def one_pass(order):
        grid = PL.Grid(B, sig, clr, edge, P["step"], pair_nets)
        items = G.Items.of_board(B, old_nets=pair_nets, edge_extra=max(0.0, edge - clr))
        ctx = {"grid": grid, "items": items, "P": P, "base_n": items.mark(), "skip": pair_nets, "min_clearance": R.get("min_clearance", 0.0),
               "closed": lambda pr: not open0.get(pr[0]) and not open0.get(pr[1]),
               "ign": {("seg", k) for k, s in enumerate(B["segs"]) if s["net"] in pair_nets} | {("via", k) for k, v in enumerate(B["vias"]) if v["net"] in pair_nets},
               "limit": lambda net: C.skew_limit_mm(cons, net),
               "tol": lambda net: min(0.25 * C.skew_limit_mm(cons, net), 0.25)}
        st = {"res": {}, "grid": grid, "order": list(order)}
        prefix = ()
        for pr in order:
            c = cache.get((pr, prefix))
            if c is None:
                c = cache[(pr, prefix)] = _lay_pair(ctx, pr, info[pr], rules[pr], plan_only)
                log(f"pairs: {pr[0]}/{pr[1]} {c['e']['status']} ({c['e'].get('secs', 0)} s)")
            _apply(ctx, pr, c, plan_only)
            st["res"][pr] = c
            prefix += (pr,)
        st["score"] = (sum(1 for c in st["res"].values() if c["full"] or c["e"]["status"] == "kept"), sum(c["nok"] for c in st["res"].values()),
                       -round(sum(c["skew"] for c in st["res"].values()), 3))
        return st

    def moves(st):
        """[(failed pair, the pair whose laid copper blocks it)] with the blocker earlier in the order."""
        out, pos = [], {pr: k for k, pr in enumerate(st["order"])}
        for f in st["order"]:
            c = st["res"][f]
            txt = c["e"].get("blocked_by", "")
            if c["full"] or not txt:
                continue
            hit = [g for g in st["order"] if g != f and pos[g] < pos[f] and st["res"][g]["nok"]
                   and any(re.search(r"(^|[ \[(])" + re.escape(n) + r"($|[ \])])", txt) for n in g)]
            if hit:
                out.append((f, min(hit, key=lambda g: pos[g])))
        return out

    best = one_pass(sorted(pl, key=lambda pr: key[pr]))
    tried = {tuple(best["order"])}
    for _ in range(max(0, P["passes"])):
        mv = moves(best)
        cands = []
        if mv:
            o = list(best["order"])
            for f, g in mv:                                   # every blocked pair ahead of its blocker, in one go
                if o.index(g) < o.index(f):
                    o.remove(f)
                    o.insert(o.index(g), f)
            cands.append(o)
            for f, g in mv:                                   # ... or one at a time
                o = list(best["order"])
                o.remove(f)
                o.insert(o.index(g), f)
                cands.append(o)
        cand = next((o for o in cands if tuple(o) not in tried), None)
        if cand is None:
            break
        tried.add(tuple(cand))
        st = one_pass(cand)
        res["passes"] += 1
        log(f"pairs: pass {res['passes']} score {st['score']} (best {best['score']})")
        if st["score"] > best["score"]:                       # never rip a pair to gain nothing
            best = st
    for pr in best["order"]:
        c = best["res"][pr]
        res["pairs"].append(c["e"])
        if c["nok"] and not plan_only:
            for net, pcs in c["pcs"].items():
                res["laid"][net] = res["laid"].get(net, []) + pcs
            res["keep"] += c["keep"]
            if c["full"]:
                res["strip"] += list(pr)
    # fan-out copper of the pairs that keep theirs: only what the new copper does not foul stays (the rest would be a DRC error)
    if res["laid"] and not plan_only:
        new = G.Items(B["copper"])
        for net, pcs in res["laid"].items():
            _claim(new, pcs, net)
        for net, lst in best["grid"].old.items():
            for kind, o in lst:
                pc = ("seg", o["layer"], o["a"], o["b"], o["w"]) if kind == "seg" else ("via", o["x"], o["y"], o["d"])
                if PL.check(new, [pc], net, clr, ())[0] < clr - 1e-6:
                    res["kill"].append((kind, o))
    res["order"] = ["/".join(pr) for pr in best["order"]]
    res["secs"] = round(time.time() - t0, 1)
    return res


def _fmt(v):
    return f"{v:.4f}".rstrip("0").rstrip(".")


def write(board, out, res, drill_of):
    """board text with the laid copper LOCKED and the replaced fan-out copper removed -> (added, removed)."""
    from . import boardtext as BT
    text = open(board, errors="replace").read()
    strip = set(res["strip"])
    kill_s = {(tuple(sorted(((round(o["a"][0], 4), round(o["a"][1], 4)), (round(o["b"][0], 4), round(o["b"][1], 4))))), o["layer"], o["net"])
              for k, o in res["kill"] if k == "seg"}
    kill_v = {(round(o["x"], 4), round(o["y"], 4), o["net"]) for k, o in res["kill"] if k == "via"}
    keep_s = {(tuple(sorted(((round(o["a"][0], 4), round(o["a"][1], 4)), (round(o["b"][0], 4), round(o["b"][1], 4))))), o["layer"], o["net"])
              for k, o in res.get("keep", []) if k == "seg"}                       # fan-out stub + via a laid pair leaves through
    keep_v = {(round(o["x"], 4), round(o["y"], 4), o["net"]) for k, o in res.get("keep", []) if k == "via"}
    keys, gone = [], []
    for _i, _j, key, blk in BT.scan(text):
        net = BT.net_name(blk)
        if net and ((key[0] == "s" and (tuple(sorted((key[1], key[2]))), key[4], net) in keep_s) or (key[0] == "v" and (key[1], key[2], net) in keep_v)):
            continue
        hit = net in strip
        if not hit and net and key[0] == "s":
            hit = (tuple(sorted((key[1], key[2]))), key[4], net) in kill_s
        elif not hit and net:
            hit = (key[1], key[2], net) in kill_v
        if hit:
            keys.append(key)
            import re
            m = re.search(r'\(uuid "([^"]+)"\)', blk)
            if m:
                gone.append(m.group(1))
    if keys:
        text = BT.remove_blocks(text, keys)
        for u in gone:                                       # group membership of removed fan-out items
            text = text.replace(f' "{u}"', "")
    blks = []
    u = lambda: str(uuid.uuid4())
    cu = res["B"]["copper"]
    for net, pieces in res["laid"].items():
        for p in pieces:
            if p[0] == "seg":
                if math.hypot(p[3][0] - p[2][0], p[3][1] - p[2][1]) < 1e-4:
                    continue
                blks.append(f'\t(segment\n\t\t(start {_fmt(p[2][0])} {_fmt(p[2][1])})\n\t\t(end {_fmt(p[3][0])} {_fmt(p[3][1])})\n\t\t(width {_fmt(p[4])})\n'
                            f'\t\t(locked yes)\n\t\t(layer "{p[1]}")\n\t\t(net "{net}")\n\t\t(uuid "{u()}")\n\t)')
            else:
                blks.append(f'\t(via\n\t\t(at {_fmt(p[1])} {_fmt(p[2])})\n\t\t(size {_fmt(p[3])})\n\t\t(drill {_fmt(drill_of(net))})\n'
                            f'\t\t(layers "{cu[0]}" "{cu[-1]}")\n\t\t(locked yes)\n\t\t(net "{net}")\n\t\t(uuid "{u()}")\n\t)')
    text = BT.append_blocks(text, blks)
    with open(out, "w") as fh:
        fh.write(text)
    return len(blks), len(keys)


def run(board, cons=None, spec=None, out=None, plan_only=False, only=None, log=lambda m: None):
    """plan (and with `out`, write) -> report dict: complete / total, per pair length, skew, vias, layers, reasons."""
    from . import routediag as RD, pairaudit as PA, routeconn as RC, constraints as C, routerules as RR
    cons = cons or {}
    res = plan(board, cons, spec, plan_only=plan_only or not out, only=only, log=log) if (plan_only or not out) else \
        plan(board, cons, spec, only=only, log=log)
    rep = {"board": board, "out": out, "plan_only": bool(plan_only or not out), "secs": res["secs"], "signal_layers": res["signal_layers"],
           "passes": res.get("passes", 0), "order": res.get("order", []),
           "plane_layers": res["plane_layers"], "pairs": []}
    B2 = res["B"]
    if out and not plan_only:
        proj = project_classes(board)
        R = RR.rules(cons, board, B=res["B"])
        P = params(cons)
        added, removed = write(board, out, res, lambda net: rules_for(board, res["B"], cons, R, net, P, proj)["drill"])
        rep.update(added=added, removed=removed)
        for ext in (".kicad_pro", ".kicad_prl"):
            src = os.path.splitext(board)[0] + ext
            if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(os.path.splitext(out)[0] + ext):
                import shutil
                shutil.copy(src, os.path.splitext(out)[0] + ext)
        B2 = RD.load(out)
    opn = RC.text_open(B2)
    aud = PA.audit(B2, [tuple(e["pair"]) for e in res["pairs"]], cons, {n for n, k in opn.items() if k})
    bad = [e for e in res["pairs"] if e["status"] in ("laid", "partial") and aud[tuple(e["pair"])]["status"] == "mismatch"] if out and not plan_only else []
    if bad:                                                 # the audit on the written board is the judge: out-of-tolerance copper is taken back
        for e in bad:
            for n in e["pair"]:
                res["laid"].pop(n, None)
                if n in res["strip"]:
                    res["strip"].remove(n)
            e["status"], e["why"] = "not-laid", f"taken back: pair audit says {aud[tuple(e['pair'])]['why']}"
        res["kill"] = [(k, o) for k, o in res["kill"] if o["net"] not in {n for e in bad for n in e["pair"]}]
        added, removed = write(board, out, res, lambda net: rules_for(board, res["B"], cons, R, net, P, proj)["drill"])
        rep.update(added=added, removed=removed, taken_back=["/".join(e["pair"]) for e in bad])
        B2 = RD.load(out)
        opn = RC.text_open(B2)
        aud = PA.audit(B2, [tuple(e["pair"]) for e in res["pairs"]], cons, {n for n, k in opn.items() if k})
    for e in res["pairs"]:
        pr = tuple(e["pair"])
        a = aud[pr]
        row = {"pair": e["pair"], "class": e["class"], "width": e["width"], "gap": e["gap"], "span": e["span"], "plan": e["status"],
               "why": e.get("why", ""), "blockers": e.get("blockers", []), "sections": e["sections"], "secs": e.get("secs", 0),
               "limit": C.skew_limit_mm(cons, pr[0]), "limit_from": C.skew_limit_source(cons, pr[0])[1], "geometry": e.get("geometry", {}),
               "via_exit": sorted({x for s in e["sections"] for x in s.get("via_exit", [])}), "open": [opn.get(pr[0], 0), opn.get(pr[1], 0)]}
        if rep["plan_only"]:
            row["status"] = "planned" if e["status"] in ("planned", "kept") else "not-planned"
            row["vias"] = sum(s.get("vias", 0) for s in e["sections"])
            row["layers"] = sorted({l for s in e["sections"] for l in s.get("layers", [])})
        else:
            row["status"] = a["status"]
            if a["status"] in ("complete", "mismatch"):
                row.update(length=[a["p"]["length"], a["n"]["length"]], skew=a["skew"], vias=a["p"]["vias"], layers=a["p"]["layers"])
                if a["status"] == "mismatch":
                    row["why"] = (row["why"] + "; " if row["why"] else "") + a["why"]
                    row["audit"] = a["why"]
        rep["pairs"].append(row)
    good = ("planned",) if rep["plan_only"] else ("complete",)
    rep["complete"] = sum(1 for r in rep["pairs"] if r["status"] in good)
    rep["total"] = len(rep["pairs"])
    rep["verdict"] = "PAIRS_OK" if rep["complete"] == rep["total"] else "PAIRS_WARN"
    return rep


def format_text(rep, top=None):
    out = [f"pairs: {rep['verdict']} - {rep['complete']}/{rep['total']} {'planned' if rep['plan_only'] else 'complete'}; "
           f"signal layers {' '.join(l[:-3] for l in rep['signal_layers'])}; {rep['secs']} s, {rep.get('passes', 0)} re-order pass(es)"
           + (f"; {rep.get('added', 0)} locked items written, {rep.get('removed', 0)} fan-out items replaced" if not rep["plan_only"] else "")]
    rows = sorted(rep["pairs"], key=lambda r: (r["status"] in ("complete", "planned"), r["pair"]))
    for r in rows[: top or len(rows)]:
        nm = "/".join(r["pair"])
        if r["status"] == "complete" and "length" in r:
            inner = sorted({f"{l[:-3]} {g['width']:g}/{g['gap']:g} ({g['from']})" for l, g in r.get("geometry", {}).items()
                            if l in r["layers"] and g["from"] != "class"})
            out.append(f"  {nm}: complete {r['length'][0]:.2f} / {r['length'][1]:.2f} mm, skew {r['skew']:.3f} (limit {r['limit']:g} {r.get('limit_from', '')}), "
                       f"{r['vias']} via(s), {'>'.join(l[:-3] for l in r['layers']) or '-'}, {r['width']:g}/{r['gap']:g} {r['class']}"
                       + (f", {'; '.join(inner)}" if inner else "") + (f", exit via {' '.join(r['via_exit'])}" if r.get("via_exit") else "")
                       + f", {r.get('secs', 0)} s" + (" [kept]" if r["plan"] == "kept" else ""))
        elif r["status"] == "planned":
            out.append(f"  {nm}: planned, {r.get('vias', 0)} via pair(s), {' '.join(l[:-3] for l in r.get('layers', [])) or '-'}")
        else:
            out.append(f"PAIR_NOT_LAID {nm} [{r['status']}/{r['plan']}, {r.get('secs', 0)} s] {r['why'] or 'open connections remain'}"
                       + (f"; blocked by {', '.join(r['blockers'][:5])}" if r.get("blockers") else ""))
    return "\n".join(out)


def cmd_pairs(a):
    from . import constraints as C
    cons = C.load(a.constraints)
    spec = json.load(open(a.spec)) if a.spec else None
    out = None if a.plan_only else (a.out or os.path.splitext(a.board)[0] + ".pairs.kicad_pcb")
    if out and os.path.abspath(out) == os.path.abspath(a.board):
        raise SystemExit("pairs: --out must differ from the input board")
    only = set(x.strip() for x in a.nets.split(",") if x.strip()) if a.nets else None
    import sys
    rep = run(a.board, cons, spec, out, plan_only=a.plan_only, only=only,
              log=(lambda m: print(m, file=sys.stderr, flush=True)) if getattr(a, "verbose", False) else (lambda m: None))
    rp = os.path.splitext(out or a.board)[0] + (".pairs.plan.json" if a.plan_only else ".pairs.json")
    json.dump(rep, open(rp, "w"), indent=1)
    q = getattr(a, "quiet", False)
    txt = format_text(rep, None if (getattr(a, "full_output", False) or getattr(a, "full", False)) else (a.top or 12))
    print(txt.splitlines()[0] if q else txt)
    print(f"full report: {rp}")
    if out:
        try:
            from . import questions as QS
            QS.inherit(a.board, out)
        except Exception:                                   # noqa: BLE001 - the answers file is optional
            pass
        print(f"wrote {out}")
    return 0 if rep["verdict"] == "PAIRS_OK" else 1


def stage(c, n, board):
    """pipeline stage (constructive flow, after fan-out): never blocks; incomplete pairs stay with the router (WARN)."""
    if not params(c.cons)["enabled"]:
        return {"verdict": "skipped", "line": "[pairlay] enabled = false", "board": board}
    d = c.dir(n, "pairs")
    out = os.path.join(d, "board.kicad_pcb")
    try:
        rep = run(board, c.cons, c.spec, out)
    except Exception as e:                                  # noqa: BLE001 - the router still has every pair
        return {"verdict": "pass", "line": f"WARN pairs not laid ({type(e).__name__}: {str(e)[:80]})", "board": board}
    json.dump(rep, open(os.path.join(d, "board.pairs.json"), "w"), indent=1)
    with open(os.path.join(d, "stage.log"), "a") as fh:
        fh.write(format_text(rep) + "\n")
    fm = os.path.splitext(board)[0] + ".fluxmap.json"
    if os.path.exists(fm):
        import shutil
        shutil.copy(fm, os.path.join(d, "board.fluxmap.json"))
    line = format_text(rep).splitlines()[0]
    res = {"verdict": "pass", "line": ("WARN " if rep["verdict"] != "PAIRS_OK" else "") + line, "board": out}
    if rep["verdict"] != "PAIRS_OK":
        res["next"] = [l for l in format_text(rep).splitlines() if l.startswith("PAIR_NOT_LAID")][:5]
    return res


def add_parser(sub):
    p = sub.add_parser("pairs", help="lay the differential pairs by construction (coupled, locked) before routing; PAIRS report")
    p.add_argument("board")
    p.add_argument("--constraints", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--out", default=None, help="output board (default <board>.pairs.kicad_pcb)")
    p.add_argument("--plan-only", dest="plan_only", action="store_true", help="centre-line corridors only; writes no copper")
    p.add_argument("--nets", default=None, help="comma-separated nets: only the pairs of these nets")
    p.add_argument("-v", "--verbose", action="store_true", help="one line per pair and re-order pass on stderr")
    p.add_argument("--full", dest="full_output", action="store_true")
    p.add_argument("-q", "--quiet", dest="quiet", action="store_true")
    p.add_argument("--top", type=int, default=None)
    p.set_defaults(fn=cmd_pairs)
