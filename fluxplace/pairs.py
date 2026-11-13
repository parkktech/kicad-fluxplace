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
            "nodes": int(e.get("max_nodes", 400000)), "pad_max": float(e.get("pad_max_mm", 8.0)), "ring": float(e.get("ring_mm", 1.0)),
            "margin": float(e.get("margin_mm", 0.03))}


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
            "class": c.get("name") or "-", "prio": int(c.get("priority", 1 << 31)) if c else 1 << 31}


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


def _gates(grid, allowed, li_list, term, R, outward, lat=0.3):
    """{(li, j, i): [(d, pol, cost)]} gate cells around a terminal; outward=True for the start (leaving), False = arriving."""
    cx, cy = term["c"]
    ux, uy = G.unit((term["n"]["x"], term["n"]["y"]), (term["p"]["x"], term["p"]["y"]))        # N -> P
    j0, i0 = grid.cell(cx - R, cy - R)
    j1, i1 = grid.cell(cx + R, cy + R)
    j0, i0, j1, i1 = max(0, j0), max(0, i0), min(grid.ny - 1, j1), min(grid.nx - 1, i1)
    out = {}
    dirs = []
    for d, (vx, vy) in enumerate(PL.DVEC):
        cr = vx * uy - vy * ux                         # cross(travel, P - N): > 0 = P on the left
        if abs(cr) >= PL.SIN_MIN:
            dirs.append((d, vx, vy, 1 if cr > 0 else 0))
    for li in li_list:
        m = allowed[grid.sig[li]][j0:j1 + 1, i0:i1 + 1]
        for jj, ii in np.argwhere(m):
            j, i = int(jj) + j0, int(ii) + i0
            x, y = grid.xy(j, i)
            ox, oy = x - cx, y - cy
            r = math.hypot(ox, oy)
            if r > R:
                continue
            if not outward:
                ox, oy = -ox, -oy
            if abs(ox * ux + oy * uy) > term["sep"] / 2 + lat:          # a gate stays in front of the pad span
                continue
            lst = [(d, pol, 1.5 * r) for d, vx, vy, pol in dirs if vx * ox + vy * oy >= (0.5 * r if r > 0.2 else -0.05)]
            if lst:
                out[(li, j, i)] = lst
    return out


def lay_section(ctx, D, O, ta, tb, rule, nets, plan_only=False, ring=None):
    """one pair section between terminals ta and tb -> {ok, P, N, vias, layers, lenP, lenN, corridor | reason, blockers}."""
    grid, items, P = ctx["grid"], ctx["items"], ctx["P"]
    sig, step = grid.sig, grid.step
    w, gap, clr = rule["w"], rule["gap"], rule["clr"]
    dist = (w + gap) / 2
    env = 2 * w + gap
    lays = [[li for li, l in enumerate(sig) if l in t["p"]["layers"] and l in t["n"]["layers"]] for t in (ta, tb)]
    for t, ls in zip((ta, tb), lays):
        if not ls:
            return {"ok": False, "reason": f"{t['name']}: the two pads share no signal layer", "blockers": []}
    hvmin = (rule["via"] + rule["via_gap"]) / 2
    kv_s, kv_d = math.ceil(hvmin / step - 1e-9), math.ceil(hvmin / (step * math.sqrt(2)) - 1e-9)
    hv = [(kv_s if (di == 0 or dj == 0) else kv_d) * step * math.hypot(di, dj) for di, dj in (PL.DIRS[(d + 2) % 8] for d in range(8))]
    K = max(2, math.ceil(max(2 * dist * PL.T22, max(hv) - dist + dist * PL.T22) / step + 0.3))
    viaok = None
    banned = set()
    reason, blockers = "no path", []
    for attempt in range(5):
        margin = P["margin"] + 0.05 * (attempt // 2 if attempt < 4 else 2)
        thr = clr + env / 2 + margin
        allowed = {l: D[l] >= thr for l in sig}
        Rs = [t["sep"] / 2 + max(t["p"]["w"], t["p"]["h"], t["n"]["w"], t["n"]["h"]) / 2 + env / 2 + clr + (ring or P["ring"]) for t in (ta, tb)]
        ga = {k: v for k, v in _gates(grid, allowed, lays[0], ta, Rs[0], True, dist).items() if k not in banned}
        gb = {k: v for k, v in _gates(grid, allowed, lays[1], tb, Rs[1], False, dist).items() if k not in banned}
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
                                P["turn_cost"], P["via_cost"], P["nodes"]) if seeds else (None, {"reason": "no gate with a matching polarity"})
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
            return {"ok": True, "corridor": [{"layer": r["layer"], "pts": [[round(x, 3), round(y, 3)] for x, y in r["pts"]], "width": round(env + 2 * clr, 3)}
                                             for r in runs], "vias": len(vias), "layers": [r["layer"] for r in runs]}
        cp, p0, p1 = PL.legs(runs, vias, dist, hv, w, rule["via"], pol)
        cn, n0, n1 = PL.legs(runs, vias, dist, hv, w, rule["via"], -pol)
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
        skew = PL.piece_len(cp) - PL.piece_len(cn)
        fb = lambda pad, e, layer, net: PL.stub(grid.B, items, ctx["base_n"], ctx["ign"], pad, e, layer, w, net, clr, ctx["skip"])
        ba = PL.breakout(items, (ta["p"], ta["n"]), (p0, n0), t0, runs[0]["layer"], w, nets, clr, ctx["skip"], bias=skew, fallback=fb)
        if ba[0] is None:
            items.rollback(mark)
            reason, blockers = ba[1], [ba[2]["label"]] if ba[2] else []
            banned |= _ban(states[0], grid, 0.3)
            continue
        _claim(items, ba[0], nets[0])
        _claim(items, ba[1], nets[1])
        bb = PL.breakout(items, (tb["p"], tb["n"]), (p1, n1), (-t1[0], -t1[1]), runs[-1]["layer"], w, nets, clr, ctx["skip"], bias=skew + ba[2], fallback=fb)
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
    items, clr, w = ctx["items"], rule["clr"], rule["w"]
    sig = ctx["grid"].sig
    out = []
    mark = items.mark()
    for key, net in zip("pn", nets):
        a, b = ta[key], tb[key]
        done = False
        for l in [l for l in sig if l in a["layers"] and l in b["layers"]]:
            u = G.unit((a["x"], a["y"]), (b["x"], b["y"]))
            for pts in PL.doglegs((a["x"], a["y"]), (b["x"], b["y"]), (-u[0], -u[1]), exts=(0.0,)) + [[(a["x"], a["y"]), (b["x"], b["y"])]]:
                pcs = [("seg", l, x, y, w) for x, y in zip(pts, pts[1:])]
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
    return {"ok": True, "P": out[0], "N": out[1], "vias": 0, "layers": [out[0][0][1]], "lenP": PL.piece_len(out[0]), "lenN": PL.piece_len(out[1]),
            "direct": True, "uncompensated": 0.0}


def plan(board, cons=None, spec=None, plan_only=False, only=None, B=None, log=lambda m: None):
    """-> {pairs: [...], laid: {net: pieces}, strip: [nets whose old copper goes], B, secs}."""
    from . import routediag as RD, netclass as NC, pairaudit as PA, routeconn as RC, routerules as RR, escapeplan as EP
    from . import constraints as C
    t0 = time.time()
    cons = cons or {}
    P = params(cons)
    B = B or RD.load(board)
    cls = NC.classify(B, cons, spec)
    pl = [pr for pr in PA.pair_list(cls) if not only or pr[0] in only or pr[1] in only]
    R = RR.rules(cons, board, B=B)
    proj = project_classes(board)
    sig, planes = EP.plan_layers(B, cons)
    rules = {pr: rules_for(board, B, cons, R, pr[0], P, proj) for pr in pl}
    open0 = RC.text_open(B)
    res = {"pairs": [], "laid": {}, "strip": [], "kill": [], "B": B, "signal_layers": sig, "plane_layers": planes}
    if not pl:
        res["secs"] = round(time.time() - t0, 1)
        return res
    clr = max(r["clr"] for r in rules.values())
    edge = max(float((RR._pro_rules(board) if board else {}).get("min_copper_edge_clearance") or 0.0), 0.3)
    pair_nets = {n for pr in pl for n in pr}
    grid = PL.Grid(B, sig, clr, edge, P["step"], pair_nets)
    items = G.Items.of_board(B, old_nets=pair_nets, edge_extra=max(0.0, edge - clr))
    ctx = {"grid": grid, "items": items, "P": P, "base_n": items.mark(), "skip": pair_nets,
           "ign": {("seg", k) for k, s in enumerate(B["segs"]) if s["net"] in pair_nets} | {("via", k) for k, v in enumerate(B["vias"]) if v["net"] in pair_nets},
           "tol": lambda net: min(0.25 * C.skew_limit_mm(cons, net), 0.25)}
    span = {}
    todo = []
    for pr in pl:
        terms, lone = terminals(B, pr[0], pr[1], P["pad_max"])
        secs = tree(terms)
        span[pr] = sum(math.hypot(terms[a]["c"][0] - terms[b]["c"][0], terms[a]["c"][1] - terms[b]["c"][1]) for a, b in secs)
        todo.append((rules[pr]["prio"], KIND_RANK.get(cls[pr[0]].get("kind"), 3), span[pr], pr, terms, lone, secs))
    for prio, _k, _s, pr, terms, lone, secs in sorted(todo, key=lambda t: t[:4]):
        rule = rules[pr]
        e = {"pair": list(pr), "class": rule["class"], "width": rule["w"], "gap": rule["gap"], "sections": [], "span": round(span[pr], 2),
             "unpaired": [f"{q['ref']}.{q['num']}" for q in lone]}
        res["pairs"].append(e)
        if not open0.get(pr[0]) and not open0.get(pr[1]):
            e["status"], e["why"] = "kept", "already connected on the input board"
            continue
        if not secs:
            e["status"], e["why"] = "not-laid", "no two terminals (P and N pads never side by side)" if not terms or len(terms) < 2 else "single terminal"
            continue
        if rule["gap"] + 1e-9 < R.get("min_clearance", 0.0):
            e["status"], e["why"] = "not-laid", f"class gap {rule['gap']} below the clearance floor"
            continue
        D, O = grid.view()
        pcs = {pr[0]: [], pr[1]: []}
        nok = 0
        tp = time.time()
        for a, b in secs:
            ta, tb = terms[a], terms[b]
            d = math.hypot(ta["c"][0] - tb["c"][0], ta["c"][1] - tb["c"][1])
            r = None
            if d < 1.5 and not plan_only:
                r = direct_section(ctx, ta, tb, rule, pr)
            if r is None:
                r = lay_section(ctx, D, O, ta, tb, rule, pr, plan_only)
                if not r["ok"] and "search budget" not in r["reason"]:          # a longer uncoupled break-out: gates further out
                    r2 = lay_section(ctx, D, O, ta, tb, rule, pr, plan_only, ring=3 * P["ring"])
                    r = r2 if r2["ok"] else r
            if not r["ok"] and d < 3.0 and not plan_only:
                r = direct_section(ctx, ta, tb, rule, pr) or r
            s = {"from": ta["name"], "to": tb["name"], "ok": r["ok"], "mm": round(d, 2)}
            if r["ok"] and plan_only:
                s.update(corridor=r["corridor"], vias=r["vias"], layers=r["layers"])
                nok += 1
            elif r["ok"]:
                nok += 1
                pcs[pr[0]] += r["P"]
                pcs[pr[1]] += r["N"]
                s.update(vias=r["vias"], layers=r["layers"], lenP=round(r["lenP"], 3), lenN=round(r["lenN"], 3), direct=bool(r.get("direct")), skew=r.get("skew"),
                         uncompensated=r.get("uncompensated", 0.0))
                grid.claim(r["P"], pr[0])
                grid.claim(r["N"], pr[1])
                D, O = grid.view()                         # the next section of this pair sees this one
            else:
                s.update(reason=r["reason"], blockers=r.get("blockers", []))
            e["sections"].append(s)
        e["secs"] = round(time.time() - tp, 1)
        full = nok == len(secs) and not lone
        e["status"] = ("planned" if plan_only else "laid") if full else ("partial" if nok else "not-laid")
        if not full:
            bad = [s for s in e["sections"] if not s["ok"]]
            e["why"] = "; ".join([f"{s['from']}>{s['to']}: {s['reason']}" for s in bad][:2] +
                                 ([f"unpaired pad(s) {', '.join(e['unpaired'][:4])}"] if lone else []))
            e["blockers"] = sorted({b for s in bad for b in s.get("blockers", [])})[:8]
        if nok and not plan_only:
            res["laid"].update({k: res["laid"].get(k, []) + v for k, v in pcs.items()})
            if full:
                res["strip"] += list(pr)
                grid.drop_old(pr)
                items.retire(nets=set(pr))
        log(f"pairs: {pr[0]}/{pr[1]} {e['status']} ({e.get('secs', 0)} s)")
    # fan-out copper of the pairs that keep theirs: only what the new copper does not foul stays (the rest would be a DRC error)
    lim = min(clr, B["clearance"]) - 0.005
    for net, lst in grid.old.items():
        for kind, o in lst:
            pc = ("seg", o["layer"], o["a"], o["b"], o["w"]) if kind == "seg" else ("via", o["x"], o["y"], o["d"])
            if not plan_only and res["laid"] and PL.check(items, [pc], net, lim, pair_nets)[0] < lim:
                res["kill"].append((kind, o))
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
    keys, gone = [], []
    for _i, _j, key, blk in BT.scan(text):
        net = BT.net_name(blk)
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
    for e in res["pairs"]:
        pr = tuple(e["pair"])
        a = aud[pr]
        row = {"pair": e["pair"], "class": e["class"], "width": e["width"], "gap": e["gap"], "span": e["span"], "plan": e["status"],
               "why": e.get("why", ""), "blockers": e.get("blockers", []), "sections": e["sections"], "secs": e.get("secs", 0),
               "limit": C.skew_limit_mm(cons, pr[0]), "open": [opn.get(pr[0], 0), opn.get(pr[1], 0)]}
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
           f"signal layers {' '.join(l[:-3] for l in rep['signal_layers'])}; {rep['secs']} s"
           + (f"; {rep.get('added', 0)} locked items written, {rep.get('removed', 0)} fan-out items replaced" if not rep["plan_only"] else "")]
    rows = sorted(rep["pairs"], key=lambda r: (r["status"] in ("complete", "planned"), r["pair"]))
    for r in rows[: top or len(rows)]:
        nm = "/".join(r["pair"])
        if r["status"] == "complete" and "length" in r:
            out.append(f"  {nm}: complete {r['length'][0]:.2f} / {r['length'][1]:.2f} mm, skew {r['skew']:.3f} (limit {r['limit']:g}), "
                       f"{r['vias']} via(s), {'>'.join(l[:-3] for l in r['layers']) or '-'}" + (" [kept]" if r["plan"] == "kept" else ""))
        elif r["status"] == "planned":
            out.append(f"  {nm}: planned, {r.get('vias', 0)} via pair(s), {' '.join(l[:-3] for l in r.get('layers', [])) or '-'}")
        else:
            out.append(f"PAIR_NOT_LAID {nm} [{r['status']}/{r['plan']}] {r['why'] or 'open connections remain'}"
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
    rep = run(a.board, cons, spec, out, plan_only=a.plan_only, only=only)
    rp = os.path.splitext(out or a.board)[0] + (".pairs.plan.json" if a.plan_only else ".pairs.json")
    json.dump(rep, open(rp, "w"), indent=1)
    q = getattr(a, "quiet", False)
    txt = format_text(rep, None if getattr(a, "full_output", False) else (a.top or 12))
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
    p.add_argument("--full", dest="full_output", action="store_true")
    p.add_argument("-q", "--quiet", dest="quiet", action="store_true")
    p.add_argument("--top", type=int, default=None)
    p.set_defaults(fn=cmd_pairs)
