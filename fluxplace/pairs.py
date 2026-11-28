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
import re
import sys
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
            "margin": float(e.get("margin_mm", 0.03)), "inner_cost": float(e.get("inner_cost", 1.15)), "passes": int(e.get("passes", 0)), "repair_rounds": int(e.get("repair_rounds", 3)), "rip_max": int(e.get("repair_rip_max", 4)), "exit_reserve_mm": float(e.get("exit_reserve_mm", 4.0)), "near_mm": float(e.get("blocker_near_mm", 2.0)),
            "inner_cost_gated": float(e.get("inner_cost", 3.0)),        # PHASE3G G3d: gated pairs stay outer unless blocked
            "nodes_per_mm": int(e.get("nodes_per_mm", 25000)), "via_exit": e.get("via_exit", True) is not False,
            "dogbone": e.get("dogbone", True) is not False, "reserve_exits": e.get("reserve_exits") if isinstance(e.get("reserve_exits"), bool) else None,
            "reserve_mm": float(e.get("reserve_mm", 3.0)), "negotiate": e.get("negotiate", True) is not False,
            "neg_passes": int(e.get("negotiate_passes", 8)), "workers": int(e.get("workers", min(8, os.cpu_count() or 1))),
            "skew_frac": float(e.get("skew_frac", 0.85)), "max_uncoupled": float(e.get("max_uncoupled_mm", 0.0)),
            "keep_partial": e.get("keep_partial", False) is True, "fallback": e.get("fallback", True) is not False,
            "rip_stubs": e.get("rip_fanout_stubs", True) is not False, "fb_trials": int(e.get("fallback_trials", 12)),
            "tight": e.get("tight_geometry", False) is True,
            "budget_s": float(e.get("budget_min", 20.0)) * 60,       # B11: wall budget of plan + fallback ladder ([pairlay] budget_min, 0 = off)
            "trial": e.get("_trial") is True,                        # a fallback-ladder trial (pairfb._trial stamps it)
            "deadline": e.get("_deadline")}                         # B11: epoch run() stamps into the cons it hands to plan and to every trial


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


from .zgeom import pair_geometry, pair_geometry as geometry, family_table, family_of, single_width, resolve_stack   # noqa: E402,F401  (THE geometry: pairs.geometry(cons, rows, planes, family, layer))


def rules_for(board, B, cons, R, p, P, proj=None):
    """track width, gap, clearance, via of the pair whose first net is `p`."""
    from . import constraints as C
    c = class_of(p, *(proj or project_classes(board)))
    fam = max((f for f in ((cons or {}).get("pairs") or {}) if p.startswith(f)), key=len, default=None)
    fv = ((cons or {}).get("pairs") or {}).get(fam) or {} if fam else {}
    zw, zg = C.PAIR_GEOM_BY_Z.get(int(fv["impedance_diff"]), (None, None)) if "impedance_diff" in fv and not c else (None, None)
    w = float(fv.get("width_mm") or c.get("diff_pair_width") or c.get("track_width") or zw or max(B["track"], 0.15))
    gap = float(fv.get("gap_mm") or c.get("diff_pair_gap") or zg or 0.2)
    og = None
    if fam and "impedance_diff" in fv and not (fv.get("width_mm") and fv.get("gap_mm")):
        from . import zgeom as ZG                       # impedance family: width/gap from the ACTUAL stackup, first copper layer
        og = ZG.outer_geometry(cons, fam, board=board, B=B)
        if og:
            w, gap = og
    w = max(w, 0.0 if og else float(c.get("diff_pair_width") or 0.0), R.get("min_track", 0.0) if not og else 0.0)
    # the clearance the router is given (floor + margin, routerules.request_clearance): the pair's copper must clear it
    from . import routerules as RR
    req = RR.request_clearance(R) if R.get("min_clearance") else 0.0
    clr = float(P["clearance"]) if P["clearance"] else max(float(c.get("clearance") or 0.0), req, R.get("min_clearance", 0.0), FLOOR) + 0.003
    # intra-pair gap: judged against the manufacturer's HARD limit, not the owner's preferred general floor
    # (an impedance-derived gap below the preferred floor but inside the fab limit is legal: fabcaps.judge)
    gap = max(gap, 0.0 if og else float(c.get("diff_pair_gap") or 0.0), R.get("fab_min_clearance") or R.get("min_clearance", 0.0))
    via = max(float(c.get("via_diameter") or R.get("via", 0.45)), R.get("min_via", 0.0))
    drill = max(float(c.get("via_drill") or R.get("drill", 0.2)), R.get("min_drill", 0.0))
    return {"w": w, "gap": gap, "clr": clr, "via": via, "drill": drill, "via_gap": max(float(c.get("diff_pair_via_gap") or 0.0), gap, clr),
            "target": fv.get("impedance_diff"), "family": fam, "class": c.get("name") or "-", "prio": int(c.get("priority", 1 << 31)) if c else 1 << 31}


def terminals(B, p, n, pad_max, merge_twins=False):
    """([{p, n, c, sep}], [unpaired pads]): P and N pads matched same part first, then nearest.  `merge_twins`: the two terminals of
    one part whose pads interleave (a USB-C receptacle: two pads per net, DM, DP, DM, DP) become ONE terminal (the closer pad pair)
    with `twin` = the other pad of each net, joined by `twin_join` before the pair leaves."""
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
    lone = [q for i, q in enumerate(Pp) if i not in ui] + [q for j, q in enumerate(Np) if j not in uj]
    if merge_twins:
        out = _merge_twins(out, Pp, Np)
    return out, lone


def twin_template(row, pnet, far_terms):
    """polarity-checked template of an interleaved double-pad row (USB-C receptacle: DM DP DM DP): `row` = its four pads sorted along the
    row, `pnet` = the pair's P net, `far_terms` = the other terminals of the pair.  Every adjacent pad pair with one pad per net is a
    candidate primary pair; its two other pads are the twins, joined to it by twin_join (dog-bone tap vias + a jumper on another layer).
    A candidate is polarity-right when its P->N side seen along the heading to the far terminals equals the far terminals' own (else the
    legs cross and the pair would need a layer-swapping via pair).  Validated against the footprint pin identities (one footprint, four
    distinct pin numbers, two pads per net, nets alternating) and the netlist (the nets are the pair's).  Polarity-right candidates
    first, the one nearest the far terminals among them; none polarity-right: the nearest end pair (the old choice), `polarity_ok` False.
    -> {p, n, tp, tn, info} | None when the row is no valid twin row.  Pure geometry and pin identity: no refs, no net names."""
    if len(row) != 4 or len({q["ref"] for q in row}) != 1 or len({q["num"] for q in row}) != 4:
        return None
    nets = [q["net"] for q in row]
    if len(set(nets)) != 2 or any(nets[k] == nets[k + 1] for k in range(3)) or nets[0] != nets[2] or nets[1] != nets[3] or pnet not in nets:
        return None
    cx, cy = sum(q["x"] for q in row) / 4, sum(q["y"] for q in row) / 4
    if far_terms:
        fx, fy = sum(t["c"][0] for t in far_terms) / len(far_terms), sum(t["c"][1] for t in far_terms) / len(far_terms)
        head = G.unit((cx, cy), (fx, fy))
        far_cross = sum(head[0] * (t["n"]["y"] - t["p"]["y"]) - head[1] * (t["n"]["x"] - t["p"]["x"]) for t in far_terms)
    else:
        fx = fy = head = None
        far_cross = 0.0
    cands = []
    for i in range(3):
        a, b = row[i], row[i + 1]
        p, n = (a, b) if a["net"] == pnet else (b, a)
        rest = [q for k, q in enumerate(row) if k not in (i, i + 1)]
        tp_ = next(q for q in rest if q["net"] == p["net"])
        tn_ = next(q for q in rest if q is not tp_)
        cross = 0.0 if head is None else head[0] * (n["y"] - p["y"]) - head[1] * (n["x"] - p["x"])
        ok = head is None or cross * far_cross > 0
        d = 0.0 if fx is None else math.hypot((p["x"] + n["x"]) / 2 - fx, (p["y"] + n["y"]) / 2 - fy)
        cands.append((not ok, d, i, {"p": p, "n": n, "tp": tp_, "tn": tn_, "polarity_ok": bool(ok), "via_only": i == 1, "primary": (p["num"], n["num"]), "twins": (tp_["num"], tn_["num"])}))
    cands.sort(key=lambda c: c[:3])
    best = cands[0][3]
    return dict(best, info={k: best[k] for k in ("polarity_ok", "via_only", "primary", "twins")})


def _merge_twins(terms, Pp, Np):
    by = {}
    for t in terms:
        if t["p"]["ref"] == t["n"]["ref"]:
            by.setdefault(t["p"]["ref"], []).append(t)
    for ref, ts in by.items():
        if len(ts) != 2 or sum(1 for q in Pp if q["ref"] == ref) != 2 or sum(1 for q in Np if q["ref"] == ref) != 2:
            continue
        pads = [q for t in ts for q in (t["p"], t["n"])]
        a0, b0 = max(((a, b) for a in pads for b in pads), key=lambda ab: math.hypot(ab[0]["x"] - ab[1]["x"], ab[0]["y"] - ab[1]["y"]))
        ux, uy = G.unit((a0["x"], a0["y"]), (b0["x"], b0["y"]))
        proj = lambda q: (q["x"] - a0["x"]) * ux + (q["y"] - a0["y"]) * uy
        if max(abs(-(q["x"] - a0["x"]) * uy + (q["y"] - a0["y"]) * ux) for q in pads) > 0.1:        # one straight pad row (a receptacle), not a chip's two columns
            continue
        row = sorted([(proj(q), "p") for q in (ts[0]["p"], ts[1]["p"])] + [(proj(q), "n") for q in (ts[0]["n"], ts[1]["n"])])
        if [k for _x, k in row] in (list("pnpn"), list("npnp")):       # P N P N along the row: interleaved (nested P N N P is not)
            # the terminal is the OUTER pad pair on the end of the row nearer the other terminals: its twins then sit on one side of
            # it and the pair leaves past the row end, not under the jumpers
            far = [t["c"] for t in terms if t not in ts]
            by_pad = {id(q): x for x, q in ((proj(q), q) for q in pads)}
            sel = sorted(pads, key=lambda q: by_pad[id(q)])
            tpl = twin_template(sel, ts[0]["p"]["net"], [t for t in terms if t not in ts])
            if tpl is None:
                continue
            pp, pn, tp_, tn_ = tpl["p"], tpl["n"], tpl["tp"], tpl["tn"]
            terms = [dict(ts[0], p=pp, n=pn, c=((pp["x"] + pn["x"]) / 2, (pp["y"] + pn["y"]) / 2), sep=math.hypot(pp["x"] - pn["x"], pp["y"] - pn["y"]),
                          twin={"p": tp_, "n": tn_, "template": tpl["info"]}) if t is ts[0] else t for t in terms if t is not ts[1]]
    return terms


def coupled_preferred(B, cons, p, n, pad_max=None):
    """Why the pair p/n is "coupled-preferred" (None = a real pair for pairlay): its P and N pads do not form two or more
    terminals within pad_max_mm, or pads of either leg stay unpaired (the pair runs through a passive, a divider or a transformer
    winding). pairlay cannot lay it, the gate does not count it as an open pair; the router lays the two legs as single nets,
    side by side where it can (it has no coupling hint). A pair of a controlled-impedance or high-speed family is never one."""
    from . import review as RV
    fam = max((f for f in ((cons or {}).get("pairs") or {}) if p.startswith(f)), key=len, default=None)
    if fam and "impedance_diff" in (((cons or {}).get("pairs") or {}).get(fam) or {}):
        return None
    if re.sub(r"\d+$", "", p.upper().split("_")[0]) in RV._HS_FAMILIES:
        return None
    terms, lone = terminals(B, p, n, pad_max if pad_max is not None else params(cons)["pad_max"])
    if len(terms) < 2:
        return "no two terminals (P and N pads further apart than [pairlay] pad_max_mm)"
    if lone:
        return "unpaired pad(s) " + ", ".join(f"{q['ref']}.{q['num']}" for q in lone[:4])
    return None


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


def _split(items, t, lays, nets, clr, skip):
    """a SPLIT terminal: foreign copper sits between its P pad and its N pad (the ground pin of a flow-through protection array, that
    pin's fan-out stub and via).  Pure geometry on the pad layers; the answer is kept on the terminal."""
    if "_split" not in t:
        a, b = (t["p"]["x"], t["p"]["y"]), (t["n"]["x"], t["n"]["y"])
        r = clr + 0.6
        t["_split"] = any(it["k"] != "poly" and it["net"] not in nets and not (it["old"] and (it["net"] in skip or it.get("dead")))
                          and any(l in it["layers"] for l in lays) and items._dist(it, a, b) < clr
                          for it in items._near(min(a[0], b[0]) - r, min(a[1], b[1]) - r, max(a[0], b[0]) + r, max(a[1], b[1]) + r))
    return t["_split"]


def _sight(ctx, t, gates, dl, wl, nets, clr, R, strict=True):
    """gates of a split terminal: only those from which each leg sees its own pad in a straight clear line, so the search arrives
    where the pair can part round the foreign copper between the pads (it used to arrive at the nearest gate, whose break-out had to
    cross that copper and was refused, five times: W16 USB_DM_J / USB_DP_J at a SOT-23-6 array with the ground pin's via between the
    pads).  None in sight, or not `strict` (the last attempts): every gate, the blind ones at the cost of a walk round the terminal."""
    grid, items = ctx["grid"], ctx["items"]
    if not _split(items, t, [l for l in grid.sig if l in t["p"]["layers"] and l in t["n"]["layers"]], nets, clr, ctx["skip"]):
        return gates
    memo = t.setdefault("_sight", {})
    if memo.get("mark") != items.mark():
        memo.clear()
        memo["mark"] = items.mark()
    out = {}
    for (li, j, i), lst in gates.items():
        x, y = grid.xy(j, i)
        l = grid.sig[li]
        new = []
        for d, pol, c in lst:
            ok = memo.get((li, j, i, d))
            if ok is None:
                n = G.left(PL.DVEC[d])
                sg = dl[l] if pol else -dl[l]                # pol 1: P on the left of travel
                ok = memo[(li, j, i, d)] = all(
                    items.gap_seg((x + k * sg * n[0], y + k * sg * n[1]), (pad["x"], pad["y"]), wl[l], l, net, clr, ctx["skip"])[0] >= clr - 1e-6
                    for pad, net, k in ((t["p"], nets[0], 1), (t["n"], nets[1], -1)))
            new.append((d, pol, c if ok else c + 3 * R, ok))
        out[(li, j, i)] = new
    seen = {k: [g[:3] for g in v if g[3]] for k, v in out.items()} if strict else {}
    seen = {k: v for k, v in seen.items() if v}
    return seen or {k: [g[:3] for g in v] for k, v in out.items()}


def layer_cost(copper, sig, P, gated):
    """A* length factor per signal layer: outer layers 1.0; inner layers P["inner_cost"], or P["inner_cost_gated"] for a
    gated pair (review.pair_gated: a route stays on the outer layers unless blocked; a cost, not a ban)."""
    inner = P["inner_cost_gated"] if gated else P["inner_cost"]
    return [1.0 if l in (copper[0], copper[-1]) else inner for l in sig]


def via_half_pitch(via, via_gap, dist, step):
    """PHASE3G G1b a: the via-pair grid -> (kv_s, kv_d, hv): kv in grid steps (straight / diagonal) and hv = half the via-pair
    pitch per direction index. The one definition the pairs stage (lay_section) and the centre line (pairsonly.lay_centreline)
    both use. dist = the pair's half pitch on the layer."""
    hvmin = max((via + via_gap) / 2, dist)
    kv_s, kv_d = math.ceil(hvmin / step - 1e-9), math.ceil(hvmin / (step * math.sqrt(2)) - 1e-9)
    return kv_s, kv_d, [(kv_s if (di == 0 or dj == 0) else kv_d) * step * math.hypot(di, dj) for di, dj in (PL.DIRS[(d + 2) % 8] for d in range(8))]


SOFT_RESERVE_COST = 6           # x cunit (0.1 mm): entering a cell of a peer's / higher tier's reserved corridor costs 0.6 mm of path


def _soft_cost(ctx, grid, sig, pr, cost):
    """corridors reserved for pairs not yet emitted that are NOT hard for `pr` (peers and higher tiers, see _lay_pair): a per-layer
    extra cost of entering their cells, max-merged with `cost`.  Nothing soft -> `cost` unchanged."""
    tier = ctx.get("tier")
    soft = [fp for q, fp in ctx.get("reserve", {}).items() if tier is not None and q != pr and q not in ctx["done"] and not tier(q) < tier(pr)]
    if not soft:
        return cost
    import numpy as np
    D0 = {l: np.full(grid.F.dist[l].shape, 1e9, dtype=np.float64) for l in sig}
    O0 = {l: np.full(grid.F.owner[l].shape, -1, dtype=grid.F.owner[l].dtype) for l in sig}
    for fp in soft:
        grid.put(D0, O0, fp, "", "soft")
    arrs = [np.where(D0[l] < 0.02, SOFT_RESERVE_COST, 0).astype(np.uint8).ravel() for l in sig]
    if not cost:
        return [a.tobytes() for a in arrs]
    return [np.maximum(np.frombuffer(bytes(x), dtype=np.uint8), a).tobytes() for x, a in zip(cost, arrs)]


def _guide_cost(ctx, grid, sig, nets, cost):
    """pairs guide (lookahead, L7): per-layer extra cost of entering each cell, max-merged with the negotiation cost and
    capped at 255. No guide in ctx -> `cost` returned unchanged (the search is byte-identical to before)."""
    cost = _soft_cost(ctx, grid, sig, nets, cost)
    g = ctx.get("guide")
    if not g:
        return cost
    arrs = g(tuple(nets), grid, sig)
    if not arrs:
        return cost
    if not cost:
        return arrs
    import numpy as np
    return [np.maximum(np.frombuffer(bytes(x), dtype=np.uint8), np.frombuffer(bytes(y), dtype=np.uint8)).tobytes()
            for x, y in zip(arrs, cost)]


NODE_RATE = 30000          # A* pops per second measured on W16 (6M nodes ~ 200 s)
NODE_CAP_S = 300           # w82: one section's search never plans for more than this many seconds
ATTEMPTS = 4               # searches one pair makes inside its share (gate retries); a retry at budget_x=3 reuses the same share
NODE_MIN_S = 30            # w82: seconds of search a section keeps even when the stage deadline is near
STATES_PER_CELL = 8        # direction x polarity states per cell and layer the search can visit


def node_limit(P, span, grid, ta, tb, budget_x=1, left=1):
    """A* node limit of one section: the old span formula, or the corridor's own size when that is larger -- the cells of the box
    round the two terminals (padded by the pair's break-out reach) x layers x states per cell -- capped at NODE_CAP_S of search.
    The 6M-node W16 ETH_P1 search got 4 mm further than the 1.4M one; a long congested corridor needs the room."""
    base = P["nodes"] + P["nodes_per_mm"] * span
    pad = max(8.0, 2 * P["ring"])
    w = abs(ta["c"][0] - tb["c"][0]) + 2 * pad
    h = abs(ta["c"][1] - tb["c"][1]) + 2 * pad
    area = w * h / (grid.step * grid.step) * len(grid.sig) * STATES_PER_CELL
    cap_s = NODE_CAP_S
    if P.get("deadline"):                               # [pairlay] budget_min: one pair's extra room is a fair share of what is left (W16 route-8: ETH_P3
        cap_s = min(cap_s, max(NODE_MIN_S, (P["deadline"] - time.time()) / max(1, left)))      # took 335 s and 7 pairs were never searched)
    if P.get("deadline"):                               # the share also bounds the base: a section tries up to 5 searches, x3 on a budget retry
        return int(min(budget_x * max(base, area), budget_x * max(P["nodes"], NODE_RATE * cap_s / ATTEMPTS)))
    return int(min(budget_x * max(base, area), max(budget_x * base, NODE_RATE * cap_s)))


def lay_section(ctx, D, O, ta, tb, rule, nets, plan_only=False, ring=None, preset=None, search_only=False, cost=None, budget_x=1):
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
    kv_s, kv_d, hv = via_half_pitch(rule["via"], rule["via_gap"], dist, step)
    K = max(2, math.ceil(max(2 * dist * PL.T22, max(hv) - dist + dist * PL.T22) / step + 0.3))
    viaok = None
    shut = False
    banned = set()
    reason, blockers = "no path", []
    for attempt in range(5):
        margin = P["margin"] + 0.05 * (attempt // 2 if attempt < 4 else 2)
        thr = clr + env / 2 + margin
        allowed = {l: D[l] >= clr + envl[l] / 2 + margin for l in sig}
        Rs = [t["sep"] / 2 + max(t["p"]["w"], t["p"]["h"], t["n"]["w"], t["n"]["h"]) / 2 + env / 2 + clr + (ring or P["ring"]) for t in (ta, tb)]
        ga = {k: v for k, v in _gates(grid, allowed, lays[0], ta, Rs[0], True, [dl[l] for l in sig]).items() if k not in banned}
        gb = {k: v for k, v in _gates(grid, allowed, lays[1], tb, Rs[1], False, [dl[l] for l in sig]).items() if k not in banned}
        ga, gb = _sight(ctx, ta, ga, dl, wl, nets, clr, Rs[0], attempt < 3), _sight(ctx, tb, gb, dl, wl, nets, clr, Rs[1], attempt < 3)
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
        for pol in ((0, 1) if not (preset is not None and attempt == 0) else ()):
            gl = [(li, j, i) for (li, j, i), lst in gb.items() if any(p == pol for _d, p, _c in lst)]
            if gl and any(p == pol for lst in ga.values() for _d, p, _c in lst):
                H[pol] = PL.coarse_dist(free, viaok, gl, 4, step, P["via_cost"])
        seeds = [s for s in seeds if s[5] in H]
        if preset is not None and attempt == 0:                # the path the negotiation settled on
            states, info = preset[0], {"pol": preset[1]}
        else:
            states, info = PL.astar(grid.nx, grid.ny, sig, allowed, vm if nl > 1 else None, seeds, goals, H, 4, K, step,
                                    P["turn_cost"], P["via_cost"], node_limit(P, span, grid, ta, tb, budget_x, ctx["left"]() if ctx.get("left") else 1),
                                    layer_cost(grid.B["copper"], sig, P, ctx["gated"](nets)),
                                    ccost=_guide_cost(ctx, grid, sig, nets, cost)) if seeds else (None, {"reason": "no gate with a matching polarity"})
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
                blockers = grid.blockers(D, O, [sig[li]], x, y, env + clr + 0.6, thr)        # the closest-approach layer's own obstacles (w82)
                blockers += [b + " [via site]" for b in grid.blockers(D, O, [l for l in sig if l != sig[li]], x, y, env + clr + 0.6, thr)
                             if b not in blockers][:2]                                      # other layers: only as the via sites they shut
            return {"ok": False, "reason": reason, "blockers": blockers}
        pol = info["pol"]
        runs, vias = PL.runs_of(states, sig, grid.xy)
        if _self_cross(runs, dl, wl, clr):                  # a turn-around loop: the legs of the pair would cross each other
            reason, blockers = "the coupled centre line can only turn round by crossing itself here", []
            banned |= _ban(states[0], grid, 0.3) | _ban(states[-1], grid, 0.3)
            continue
        if search_only:
            fp = [("seg", r["layer"], a, b, envl[r["layer"]]) for r in runs for a, b in zip(r["pts"], r["pts"][1:])]
            fp += [("via", v["at"][0], v["at"][1], 2 * hv[v["d"]] + rule["via"]) for v in vias]
            return {"ok": True, "states": states, "pol": pol, "fp": fp, "gates": len(ga) + len(gb), "vias": len(vias),
                    "layers": [r["layer"] for r in runs], "cost": info.get("cost", 0.0)}
        if plan_only:
            cp, p0, p1 = PL.legs(runs, vias, dl, hv, wl, rule["via"], pol)          # the break-outs the lay will make must exist
            cn, n0, n1 = PL.legs(runs, vias, dl, hv, wl, rule["via"], -pol)
            mark = items.mark()
            _claim(items, cp, nets[0])
            _claim(items, cn, nets[1])
            fb = lambda pad, e, layer, net: PL.stub(grid.B, items, ctx["base_n"], ctx["ign"], pad, e, layer, wl[layer], net, clr, ctx["skip"])
            ba = PL.breakout(items, (ta["p"], ta["n"]), (p0, n0), runs[0]["t0"], runs[0]["layer"], wl[runs[0]["layer"]], nets, clr, ctx["skip"], fallback=fb, gap=geo[runs[0]["layer"]][1])
            if ba[0] is not None:
                _claim(items, ba[0], nets[0])
                _claim(items, ba[1], nets[1])
                bb = PL.breakout(items, (tb["p"], tb["n"]), (p1, n1), (-runs[-1]["t1"][0], -runs[-1]["t1"][1]), runs[-1]["layer"], wl[runs[-1]["layer"]], nets, clr, ctx["skip"], fallback=fb, gap=geo[runs[-1]["layer"]][1])
            items.rollback(mark)
            if ba[0] is None or bb[0] is None:
                bad, side = (ba, states[0]) if ba[0] is None else (bb, states[-1])
                reason, blockers = bad[1], [bad[2]["label"]] if bad[2] else []
                banned |= _ban(side, grid, 0.3)
                continue
            keepfree = []                                   # what the fan-out must leave free: both ends of the corridor and the via pairs near them
            for r, t, rev in ((runs[0], ta, False), (runs[-1], tb, True)):
                pts = r["pts"][::-1] if rev else list(r["pts"])
                keepfree.append(("seg", r["layer"], t["c"], pts[0], envl[r["layer"]]))
                left_mm = P["reserve_mm"]
                for a_, b_ in zip(pts, pts[1:]):
                    L = math.hypot(b_[0] - a_[0], b_[1] - a_[1])
                    if L > left_mm:
                        b_ = (a_[0] + (b_[0] - a_[0]) * left_mm / L, a_[1] + (b_[1] - a_[1]) * left_mm / L)
                    keepfree.append(("seg", r["layer"], a_, b_, envl[r["layer"]]))
                    left_mm -= L
                    if left_mm <= 0:
                        break
            for v in vias:
                if min(math.hypot(v["at"][0] - t["c"][0], v["at"][1] - t["c"][1]) for t in (ta, tb)) <= P["reserve_mm"] + max(Rs):
                    n = G.left(v["t"])
                    keepfree += [("via", v["at"][0] + n[0] * sg * hv[v["d"]], v["at"][1] + n[1] * sg * hv[v["d"]], rule["via"]) for sg in (1, -1)]
            return {"ok": True, "keepfree": keepfree, "corridor": [{"layer": r["layer"], "pts": [[round(x, 3), round(y, 3)] for x, y in r["pts"]], "width": round(envl[r["layer"]] + 2 * clr, 3)}
                                             for r in runs], "vias": len(vias), "layers": [r["layer"] for r in runs]}
        cp, p0, p1 = PL.legs(runs, vias, dl, hv, wl, rule["via"], pol)
        cn, n0, n1 = PL.legs(runs, vias, dl, hv, wl, rule["via"], -pol)
        g1, it1 = PL.check(items, cp, nets[0], clr, ctx["skip"])
        g2, it2 = PL.check(items, cn, nets[1], clr, ctx["skip"])
        if min(g1, g2) < clr - 1e-6:
            it = it1 if g1 <= g2 else it2
            reason, blockers = f"coupled path {min(g1, g2):.3f} mm from {it['label']} (exact check)", [it["label"]]
            # the refusal goes back into the search: the centre-line cells that put a leg there are shut in this section's view, so the
            # next attempt takes another line (it used to find the same one five times: W16 USB_DM_J / USB_DP_J past its own tap via)
            if not shut:
                D, O, shut = {l: x.copy() for l, x in D.items()}, {l: x.copy() for l, x in O.items()}, True
            for leg, net in ((cp, nets[0]), (cn, nets[1])):
                for l, x, y, bad in PL.fouls(items, leg, net, clr, ctx["skip"], step / 2):
                    grid.shut(D, O, l, x, y, (dl[l] if l else max(hv)) + step, f"{bad['label']} (exact check)")
            viaok = None
            continue
        mark = items.mark()
        _claim(items, cp, nets[0])
        _claim(items, cn, nets[1])
        t0, t1 = runs[0]["t0"], runs[-1]["t1"]
        skew = PL.piece_len(cp) - PL.piece_len(cn) + ta.get("bias", 0.0) + tb.get("bias", 0.0)
        fb = lambda pad, e, layer, net: PL.stub(grid.B, items, ctx["base_n"], ctx["ign"], pad, e, layer, wl[layer], net, clr, ctx["skip"])
        ba = PL.breakout(items, (ta["p"], ta["n"]), (p0, n0), t0, runs[0]["layer"], wl[runs[0]["layer"]], nets, clr, ctx["skip"], bias=skew, fallback=fb, gap=geo[runs[0]["layer"]][1])
        if ba[0] is None:
            items.rollback(mark)
            reason, blockers = ba[1], [ba[2]["label"]] if ba[2] else []
            banned |= _ban(states[0], grid, 0.3)
            continue
        _claim(items, ba[0], nets[0])
        _claim(items, ba[1], nets[1])
        bb = PL.breakout(items, (tb["p"], tb["n"]), (p1, n1), (-t1[0], -t1[1]), runs[-1]["layer"], wl[runs[-1]["layer"]], nets, clr, ctx["skip"], bias=skew + ba[2], fallback=fb, gap=geo[runs[-1]["layer"]][1])
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
            gapl = {l: geo[l][1] for l in sig}
            if skew > 0:
                cn, left_mm = PL.bump(items, cn, nets[1], skew, -pol, clr, ctx["skip"], 0, len(cn), hmax=max(0.6, min(2.0, abs(skew) / 6)), mate=nets[0], gap=gapl)
            else:
                cp, left_mm = PL.bump(items, cp, nets[0], -skew, pol, clr, ctx["skip"], 0, len(cp), hmax=max(0.6, min(2.0, abs(skew) / 6)), mate=nets[1], gap=gapl)
        Pp, Np = ba[0] + cp + bb[0], ba[1] + cn + bb[1]
        items.rollback(mark)
        if left_mm > P["skew_frac"] * ctx["limit"](nets[0]):          # out of tolerance (or marginal) copper is not locked in: the router keeps the pair
            reason, blockers = f"skew {left_mm:.2f} mm over the {ctx['limit'](nets[0]):g} mm limit after compensation (break-outs of unequal length, no room for bumps)", []
            banned |= _ban(states[0], grid, 0.3) | _ban(states[-1], grid, 0.3)
            continue
        _claim(items, Pp, nets[0])
        _claim(items, Np, nets[1])
        return {"ok": True, "P": Pp, "N": Np, "vias": len(vias), "layers": [r["layer"] for r in runs],
                "lenP": PL.piece_len(Pp), "lenN": PL.piece_len(Np), "skew": round(abs(skew) if abs(skew) <= ctx["tol"](nets[0]) else left_mm, 3),
                "uncompensated": round(left_mm, 3)}
    return {"ok": False, "reason": reason, "blockers": blockers}


def _self_cross(runs, dl, wl, clr):
    """True when two parts of one centre line on a layer come closer than the pair needs to pass itself."""
    by = {}
    k = 0
    for r in runs:
        for a, b in zip(r["pts"], r["pts"][1:]):
            by.setdefault(r["layer"], []).append((k, a, b))
            k += 1
        k += 3                                              # a via between two runs: not neighbours
    for l, segs in by.items():
        need = 2 * dl[l] + wl[l] + clr
        for i, (ka, a, b) in enumerate(segs):
            for kb, c, d in segs[i + 1:]:
                if kb - ka >= 3 and G.seg_seg(a, b, c, d) < need:
                    return True
    return False


def _pt_seg(x, y, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0.0 if L < 1e-12 else max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / L))
    return math.hypot(x - a[0] - t * dx, y - a[1] - t * dy)


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
    """(signal layers, plane layers) for pairs: preroute.layer_split (the same split the congestion model uses)."""
    from . import preroute as PR
    return PR.layer_split(B, cons)


def layer_geometry(rule, sig, planes, copper, stackup, floor, cons=None, tight=False, board=None):
    """{layer: (width, gap, source)} per signal layer. An impedance family (`rule["family"]`, `[pairs.F] impedance_diff`) with a
    known stackup gets zgeom.pair_geometry on EVERY layer (the width AND gap that give the target there; the preferred floor, the
    fab limit when `tight` and `allow_fab_limit`); everything else keeps the class geometry."""
    geo = {l: (rule["w"], rule["gap"], "class") for l in sig}
    fam = rule.get("family")
    if not rule.get("target") or not stackup:
        return geo
    if cons is None or not fam:                          # no constraints given: the rule's own target and the floor argument
        fam = fam or "_"
        cons = {"pairs": {fam: {"impedance_diff": rule["target"]}}, "route": {"min_track_mm": floor, "min_clearance_mm": floor}}
    for l in sig:
        g = pair_geometry(cons, stackup, planes, fam, l, tight=tight, board=board, class_geom=(rule["w"], rule["gap"]))
        if g["w"] and g["status"] in ("ok", "off", "unreachable"):
            geo[l] = (g["w"], g["gap"], f"{g['source']} {g['target']} ohm ({g['err_pct']:+.1f}%{', ' + g['status'] if g['status'] != 'ok' else ''})")
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


def _vpad(pad, x, y, d, copper):
    return {"ref": pad["ref"], "num": pad["num"], "x": x, "y": y, "w": d, "h": d, "ex": d / 2, "ey": d / 2, "min": d,
            "layers": set(copper), "net": pad["net"], "angle": 0.0, "shape": "circle", "tht": True}


def _dog_leg(ctx, rule, pad, v, net, with_via=True):
    """a short stub (dog-leg) from `pad` to a via at `v` (`with_via` False: to a bare point) -> (pieces, length) | None when the
    copper laid so far fouls it."""
    items, clr, via, cu = ctx["items"], rule["clr"], rule["via"], ctx["grid"].B["copper"]
    lay = next((l for l in cu if l in pad["layers"]), None)
    w = rule["geo"].get(lay, (rule["w"],))[0]
    if lay is None or (with_via and items.gap_via(v[0], v[1], via, net, clr, ctx["skip"])[0] < clr - 1e-6):
        return None
    for pts in PL.doglegs((pad["x"], pad["y"]), v, G.unit((pad["x"], pad["y"]), v), exts=(0.0,)):
        pcs = [("seg", lay, a, b, w) for a, b in zip(pts, pts[1:])]
        if PL.check(items, pcs, net, clr, ctx["skip"])[0] >= clr - 1e-6:
            return pcs + ([("via", v[0], v[1], via)] if with_via else []), G.length(pts)
    return None


def twin_join(ctx, t, rule, pr):
    """the two pads of one net in an interleaved double-pad row (USB-C receptacle: DM, DP, DM, DP at 0.5 mm) cannot meet on the pad
    layer (the other net's pad sits between them): each leg gets a dog-bone via beside either pad and a jumper between the two vias on
    a signal layer.  -> {net: pieces} (+ t["join_lead"] = {net: the primary pad's own stub}) | None when no length / side / layer is clear of the copper laid so far (checked leg by leg)."""
    items, clr, via, sig = ctx["items"], rule["clr"], rule["via"], ctx["grid"].sig
    tw = t["twin"]
    others = [x["c"] for x in ctx.get("info", {}).get(pr, ([],))[0] if x is not t]
    ex, ey = G.unit(t["c"], (sum(c[0] for c in others) / len(others), sum(c[1] for c in others) / len(others))) if others else (0.0, 0.0)
    pads4 = [t["p"], t["n"], tw["p"], tw["n"]]
    far = max(((a, b) for a in pads4 for b in pads4), key=lambda ab: math.hypot(ab[0]["x"] - ab[1]["x"], ab[0]["y"] - ab[1]["y"]))
    ax = G.unit((far[0]["x"], far[0]["y"]), (far[1]["x"], far[1]["y"]))
    pj = lambda q: (q["x"] - far[0]["x"]) * ax[0] + (q["y"] - far[0]["y"]) * ax[1]
    outer = max((t["p"], t["n"]), key=lambda q: min(abs(pj(q) - pj(r)) for r in (tw["p"], tw["n"])))      # the primary pad farther from the twins
    # the net of the outer primary pad has its jumper over the other primary pad: it goes AWAY from the other terminals (the pair
    # leaves that way), the other net's jumper branches off its own stub on the exit side
    sidepref = {outer["net"]: -1, (t["n"] if outer is t["p"] else t["p"])["net"]: 1}
    mark = items.mark()
    out, lead = {}, {}
    try:
        for net, a, b in ((pr[0], t["p"], tw["p"]), (pr[1], t["n"], tw["n"])):
            ux, uy = G.unit((a["x"], a["y"]), (b["x"], b["y"]))
            nrm = (-uy, ux)
            sides = (1, -1) if sidepref.get(net, 0) * (nrm[0] * ex + nrm[1] * ey) >= 0 else (-1, 1)
            done = None
            play = next((l for l in sig if l in a["layers"]), None)
            # first a jumper on the pad layer round the end of the other net's pad (no via), then dog-bone vias and a jumper on another layer
            # an interior primary pair (the middle two pads) has the other net's primary pad between every jumper's ends: a jumper on the pad
            # layer would cross that net's exit, so the joins are dog-bone tap vias + a jumper on another layer
            for with_via in ((True,) if (tw.get("template") or {}).get("via_only") else (False, True)):
                for L in (0.8, 1.1, 1.5, 2.0, 2.6):
                    for side in sides:
                        for La, Lb in ((L, L), (L, L + 0.4), (L + 0.4, L)):
                            va = (a["x"] + nrm[0] * side * La, a["y"] + nrm[1] * side * La)
                            vb = (b["x"] + nrm[0] * side * Lb, b["y"] + nrm[1] * side * Lb)
                            mk = items.mark()
                            la = _dog_leg(ctx, rule, a, va, net, with_via)
                            if la:
                                _claim(items, la[0], net)
                            lb = _dog_leg(ctx, rule, b, vb, net, with_via) if la else None
                            if lb:
                                _claim(items, lb[0], net)
                                for lay in ([play] if not with_via else [l for l in sig if l != play] + [play]):
                                    jp = [("seg", lay, va, vb, rule["geo"].get(lay, (rule["w"],))[0])]
                                    if PL.check(items, jp, net, clr, ctx["skip"])[0] >= clr - 1e-6:
                                        done = (la[0] + lb[0] + jp, la[0])
                                        _claim(items, jp, net)           # the other net's stubs and jumper see this one
                                        break
                            if done:
                                break
                            items.rollback(mk)
                        if done:
                            break
                    if done:
                        break
                if done:
                    break
            if not done:
                return None
            out[net] = done[0]
            lead[net] = done[1]
    finally:
        items.rollback(mark)
    for net, side in sidepref.items():                       # the exit-side net: its jumper (last piece) branches off the stub the pair leaves through
        if side > 0 and net in out:
            lead[net] = lead[net] + [out[net][-1]]
    t["join_lead"] = lead                                    # the primary pads' own stubs (and that jumper): the pair leaves through them
    return out


def dogbone(ctx, t, rule, pr, limit=2):
    """new via-pair exits of a terminal: a short stub from each pad to its own via, the two vias side by side
    (a dog-bone per leg), so the pair can leave on any signal layer.  A terminal whose pads share no signal layer gets
    one via, on the odd pad.  -> [terminal dicts with `new` = {net: pieces}] (geometry against the board; re-checked at use)."""
    if "dog" in t:
        return t["dog"]
    items, clr, via, cu = ctx["items"], rule["clr"], rule["via"], ctx["grid"].B["copper"]
    sig = ctx["grid"].sig
    P_, N_ = t["p"], t["n"]
    shared = [l for l in sig if l in P_["layers"] and l in N_["layers"]]
    out = []
    ux, uy = G.unit((N_["x"], N_["y"]), (P_["x"], P_["y"]))
    nx_, ny_ = -uy, ux
    need = via + max(rule["via_gap"], clr)

    def leg(pad, v, net):
        return _dog_leg(ctx, rule, pad, v, net)
    if shared and all(len(q["layers"]) > 1 for q in (P_, N_)):
        t["dog"] = out                                      # through-hole pads are on every layer already
        return out
    for L in (0.8, 1.1, 1.5, 2.0, 2.6):
        for side in (1, -1):
            for ax in ((nx_, ny_), (ux, uy)):
                vp = (P_["x"] + ax[0] * side * L, P_["y"] + ax[1] * side * L)
                vn = (N_["x"] + ax[0] * side * L, N_["y"] + ax[1] * side * L)
                if ax == (ux, uy):                          # along the pad axis: each leg away from its partner
                    vp = (P_["x"] + ux * L, P_["y"] + uy * L)
                    vn = (N_["x"] - ux * L, N_["y"] - uy * L)
                    if side < 0:
                        continue
                sep = math.hypot(vp[0] - vn[0], vp[1] - vn[1])
                if sep < need:
                    k = (need - sep) / 2 + 0.01
                    vp, vn = (vp[0] + ux * k, vp[1] + uy * k), (vn[0] - ux * k, vn[1] - uy * k)
                if shared:
                    mark = items.mark()
                    a = leg(P_, vp, pr[0])
                    if a:
                        _claim(items, a[0], pr[0])
                    b = leg(N_, vn, pr[1]) if a else None
                    items.rollback(mark)
                    if a and b:
                        out.append({"p": _vpad(P_, vp[0], vp[1], via, cu), "n": _vpad(N_, vn[0], vn[1], via, cu),
                                    "c": ((vp[0] + vn[0]) / 2, (vp[1] + vn[1]) / 2), "sep": math.hypot(vp[0] - vn[0], vp[1] - vn[1]),
                                    "name": t["name"] + "@dogbone", "new": {pr[0]: a[0], pr[1]: b[0]}, "bias": a[1] - b[1], "of": id(t)})
                else:                                       # pads on different layers: a via on one pad brings both to one layer
                    for key, pad, v, net, sgn in (("n", N_, vn, pr[1], -1.0), ("p", P_, vp, pr[0], 1.0)):
                        a = leg(pad, v, net)
                        if a:
                            vt = dict(t, name=t["name"] + "@via-on-" + pad["ref"] + "." + pad["num"], new={net: a[0]}, bias=sgn * a[1], of=id(t))
                            vt[key] = _vpad(pad, v[0], v[1], via, cu)
                            vt["c"] = ((vt["p"]["x"] + vt["n"]["x"]) / 2, (vt["p"]["y"] + vt["n"]["y"]) / 2)
                            vt["sep"] = math.hypot(vt["p"]["x"] - vt["n"]["x"], vt["p"]["y"] - vt["n"]["y"])
                            vt.pop("via", None)
                            vt.pop("dog", None)
                            out.append(vt)
                            break
                if len(out) >= limit:
                    t["dog"] = out
                    return out
    t["dog"] = out
    return out


def _ekey(pr, t):
    return (tuple(pr), t["name"], round(t["c"][0], 3), round(t["c"][1], 3))


def _variants(ctx, ta, tb, rule, pr):
    """terminal pairs to try, in order: the pads; the fan-out via pair of either end; a new dog-bone via pair of either end."""
    P = ctx["P"]

    def alts(t):
        return ([t["via"]] if t.get("via") else []) + (dogbone(ctx, t, rule, pr) if P["dogbone"] else [])
    A, B_ = alts(ta), alts(tb)
    out = [(ta, tb)] + [(a, tb) for a in A] + [(ta, b) for b in B_] + [(a, b) for a in A[:2] for b in B_[:2]]
    # B5: the exit the joint escape planner chose for a terminal (pairescape.py) comes first; the plain order stays as the fallback
    esc = ctx.get("escape") or {}
    ea, eb = esc.get(_ekey(pr, ta)), esc.get(_ekey(pr, tb))
    if ea is not None or eb is not None:
        out = [(ea or ta, eb or tb)] + out
    return out[:9]


def _setup(ctx, D, O, a, b, rule):
    """the extra copper a variant brings (kept fan-out stub + via, new dog-bone) as obstacles of the search
    -> (D, O, keep, new) | None when copper laid earlier fouls it."""
    grid, items = ctx["grid"], ctx["items"]
    keep = a.get("keep", []) + b.get("keep", [])
    new = {}
    for t in (a, b):
        for net, pcs in (t.get("new") or {}).items():
            new[net] = new.get(net, []) + pcs
    extra = dict(_old_pieces(keep))
    for net, pcs in new.items():
        extra[net] = extra.get(net, []) + pcs
    if not extra:
        return D, O, keep, new
    if any(PL.check(items, pcs, net, rule["clr"], ctx["skip"])[0] < rule["clr"] - 1e-6 for net, pcs in extra.items()):
        return None
    nets = list(extra)
    if len(nets) == 2:                                      # the two legs' exits against each other
        I2 = G.Items(grid.B["copper"])
        _claim(I2, extra[nets[0]], nets[0])
        if PL.check(I2, extra[nets[1]], nets[1], rule["clr"], ())[0] < rule["clr"] - 1e-6:
            return None
    D2, O2 = {l: x.copy() for l, x in D.items()}, {l: x.copy() for l, x in O.items()}
    for net, pcs in extra.items():
        grid.put(D2, O2, pcs, net)
        _claim(items, pcs, net)
    return D2, O2, keep, new


def _alone(ctx, key, cost, bx):
    """negotiation worker: the centre line of one section against the current state, first variant that has one."""
    pr, ia, ib = key
    terms = ctx["info"][pr][0]
    rule, items = ctx["rules"][pr], ctx["items"]
    D, O = ctx["grid"].view()
    first = None
    for k, (a, b) in enumerate(_variants(ctx, terms[ia], terms[ib], rule, pr)):
        mark = items.mark()
        st = _setup(ctx, D, O, a, b, rule)
        if st is None:
            continue
        r = lay_section(ctx, st[0], st[1], a, b, rule, pr, search_only=True, cost=cost, budget_x=bx)
        items.rollback(mark)
        if r["ok"]:
            r["variant"] = (a["name"], round(a["c"][0], 3), round(a["c"][1], 3), b["name"], round(b["c"][0], 3), round(b["c"][1], 3))
            for pcs in list((a.get("new") or {}).values()) + list((b.get("new") or {}).values()):
                r["fp"] += [p for p in pcs if p[0] == "via"]
            return r
        first = first or r
    return first or {"ok": False, "reason": "no terminal variant"}


def _section(ctx, D, O, ta, tb, rule, pr, plan_only, preset=None):
    """one section: the pads first, then through a via pair at either terminal (fan-out copper or a new dog-bone).
    `preset` = (variant, states, polarity) from the negotiation."""
    items, P = ctx["items"], ctx["P"]
    first = None
    d = math.hypot(ta["c"][0] - tb["c"][0], ta["c"][1] - tb["c"][1])
    variants = _variants(ctx, ta, tb, rule, pr)
    order = list(range(len(variants)))
    sig_ = [(a["name"], round(a["c"][0], 3), round(a["c"][1], 3), b["name"], round(b["c"][0], 3), round(b["c"][1], 3)) for a, b in variants]
    pk = sig_.index(preset[0]) if preset and preset[0] in sig_ else None      # the negotiated variant first (matched by its terminals, not its index)
    if pk is not None:
        order.remove(pk)
        order.insert(0, pk)
    for k in order:
        a, b = variants[k]
        mark = items.mark()
        st = _setup(ctx, D, O, a, b, rule)
        if st is None:
            continue
        D2, O2, keep, new = st
        r = None
        if k == 0 and d < 1.5 and not plan_only:
            r = direct_section(ctx, a, b, rule, pr)
        if r is None:
            r = lay_section(ctx, D2, O2, a, b, rule, pr, plan_only, preset=preset[1:] if pk == k else None)
            if not r["ok"] and k == 0 and "search budget" not in r["reason"]:          # a longer uncoupled break-out: gates further out
                r2 = lay_section(ctx, D2, O2, a, b, rule, pr, plan_only, ring=max(3 * P["ring"], P["max_uncoupled"]))
                r = r2 if r2["ok"] else r
        if not r["ok"] and "search budget" in r["reason"] and not plan_only:        # the negotiation retries at 4x; the lay must too (bounded: x3)
            r3 = lay_section(ctx, D2, O2, a, b, rule, pr, plan_only, budget_x=3)
            r = r3 if r3["ok"] else r
        if not r["ok"] and k == 0 and d < 3.0 and not plan_only:
            r = direct_section(ctx, a, b, rule, pr) or r
        if r["ok"]:
            r["keep"], r["new"] = keep, new
            r["newparts"] = [(id(t), t["new"]) for t in (a, b) if t.get("new")]       # per exit variant: two sections may use two exits of one terminal
            r["exit"] = [t["name"] for t in (a, b) if t.get("keep") or t.get("new")]
            return r
        items.rollback(mark)
        first = first or r
    return first or {"ok": False, "reason": "no terminal variant is clear of the copper already laid", "blockers": []}


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
    dl = ctx["P"].get("deadline")
    if dl and time.time() > dl:                                # B11: out of time budget: not searched, handed on like any failed pair
        e["status"], e["why"] = "not-laid", "pairs time budget ([pairlay] budget_min) reached before this pair was searched"
        return out
    D, O = grid.view()
    tier = ctx.get("tier")
    for q, fp in ctx.get("reserve", {}).items():           # corridors the negotiation gave to pairs not yet emitted
        # hard only against a LOWER tier (a larger (prio, kind)); peers and higher tiers see it as free space (the negotiation cost
        # already steers them): a peer's corridor must not seal a high-speed pair in (W16 PCIE_TX / PCIE_CLK)
        if q != pr and q not in ctx["done"] and (tier is None or tier(q) < tier(pr)):
            grid.put(D, O, fp, "", f"corridor of {q[0]}/{q[1]}")
    for q, fp in ctx.get("escape_reserve", {}).items():    # B5: the chosen exits of pairs not yet laid, hard for every other pair, released when it is laid
        if q != pr and q not in ctx["done"]:
            grid.put(D, O, fp, "", f"escape of {q[0]}/{q[1]}")
    mark = items.mark()
    tp = time.time()
    newdone = set()
    pitch = max(g[0] + g[1] for g in rule["geo"].values()) if rule["geo"] else rule["w"] + rule["gap"]
    for t in terms:                                           # double pads of one net: joined first, then the pair leaves the pair of pads
        if t.get("twin"):
            jn = twin_join(ctx, t, rule, pr)
            if jn is None:
                items.rollback(mark)
                e["status"], e["why"] = "not-laid", f"the two pads of each net at {t['name']} cannot be joined (no clear via / jumper beside the row)"
                return out
            for net, pcs in jn.items():
                lead = t["join_lead"].get(net, [])
                grid.put(D, O, [q for q in pcs if not any(q is x for x in lead)], net, "reserved")     # not the stub the pair itself leaves through
                # a tap via in that stub is the OTHER leg's obstacle: the own leg may run over it, the partner one pitch further out must
                # clear it.  The net-blind centre-line grid gets it one pitch smaller (exact for the own leg on the via side; the other
                # polarity is the exact check's, whose refusal now feeds back into the search)
                grid.put(D, O, [("via", q[1], q[2], q[3] - 2 * pitch) for q in lead if q[0] == "via"], net, "twin tap via")
                _claim(items, pcs, net)
                if not plan_only:
                    out["pcs"][net] += pcs
            t.pop("dog", None)                               # dog-bone exits found before the join (negotiation precompute) may foul it: found again against it
            e["twin_join"] = {"at": t["name"], "pieces": sum(len(v) for v in jn.values())}
            if plan_only:
                e.setdefault("join_pcs", {}).update({net: [list(p) for p in pcs] for net, pcs in jn.items()})
    for a, b in secs:
        ta, tb = terms[a], terms[b]
        r = _section(ctx, D, O, ta, tb, rule, pr, plan_only, ctx.get("preset", {}).get((pr, a, b)))
        s = {"from": ta["name"], "to": tb["name"], "ok": r["ok"], "mm": round(math.hypot(ta["c"][0] - tb["c"][0], ta["c"][1] - tb["c"][1]), 2)}
        if r["ok"] and plan_only:
            s.update(corridor=r["corridor"], vias=r["vias"], layers=r["layers"], via_exit=r.get("exit", []),
                     keepfree=[list(p) for p in r.get("keepfree", [])],
                     exits=[{net: [list(p) for p in pcs] for net, pcs in parts.items()} for _of, parts in r.get("newparts", [])])
            out["nok"] += 1
        elif r["ok"]:
            out["nok"] += 1
            out["pcs"][pr[0]] += r["P"]
            out["pcs"][pr[1]] += r["N"]
            for of, parts in r.get("newparts", []):                    # a new dog-bone exit is written once
                if of not in newdone:
                    newdone.add(of)
                    for net, pcs in parts.items():
                        out["pcs"][net] += pcs
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


def _lay_line(pr, c, tag=""):
    e = c["e"]
    return f"pairs: {tag}{pr[0]}/{pr[1]} {e['status']} ({e.get('secs', 0)} s)" + (f": {e['why'][:200]}" if e.get("why") and not c["full"] else "")


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


def plan(board, cons=None, spec=None, plan_only=False, only=None, B=None, log=lambda m: None, guide=None):
    """-> {pairs: [...], laid: {net: pieces}, strip: [nets whose old copper goes], keep, kill, B, secs, order}.
    Negotiated order: a pair that fails on copper laid by this stage is moved ahead of the pair that laid it and the
    order is laid again (`[pairlay] passes`); the best result (pairs complete, then total skew) is kept."""
    import re
    from . import routediag as RD, netclass as NC, pairaudit as PA, routeconn as RC, routerules as RR
    from . import constraints as C
    t0 = time.time()
    cons = cons or {}
    P = params(cons)
    from . import forbid as FB
    B = FB.augment(B or RD.load(board), FB.windows(cons))    # B7: a stripped mask window stays forbidden to the pair copper
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
        r["geo"] = layer_geometry(r, sig, planes, B["copper"], stackup, R.get("min_track", 0.0), cons=cons, tight=P.get("tight", False), board=board)
    open0 = RC.text_open(B)
    res = {"pairs": [], "laid": {}, "strip": [], "kill": [], "keep": [], "B": B, "signal_layers": sig, "plane_layers": planes, "passes": 0}
    if not pl:
        res["secs"] = round(time.time() - t0, 1)
        return res
    clr = max(r["clr"] for r in rules.values())
    edge = max(float((RR._pro_rules(board) if board else {}).get("min_copper_edge_clearance") or 0.0), 0.3)
    # only the copper of pairs still to lay is "old" (replaceable, no obstacle): a pair already connected on the input board keeps its
    # copper, which is then an obstacle like any foreign net's, so no later pair is laid through it and no fan-out kill removes it
    pair_nets = {n for pr in pl if open0.get(pr[0]) or open0.get(pr[1]) for n in pr}
    old = {}
    for kind, src in (("seg", B["segs"]), ("via", B["vias"])):
        for o in src:
            if o["net"] in pair_nets:
                old.setdefault(o["net"], []).append((kind, o))
    info, key = {}, {}
    for pr in pl:
        terms, lone = terminals(B, pr[0], pr[1], P["pad_max"], merge_twins=True)
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
    neg = {"reserve": {}, "preset": {}, "escape": {}, "escape_reserve": {}}

    def mk_ctx():
        grid = PL.Grid(B, sig, clr, edge, P["step"], pair_nets)
        items = G.Items.of_board(B, old_nets=pair_nets, edge_extra=max(0.0, edge - clr))
        from . import review as RV                       # PHASE3G G3d
        return {"grid": grid, "items": items, "P": P, "base_n": items.mark(), "skip": pair_nets, "min_clearance": R.get("min_clearance", 0.0),
                "gated": lambda pr: RV.pair_gated(cons, pr[0]),
                "closed": lambda pr: not open0.get(pr[0]) and not open0.get(pr[1]), "info": info, "rules": rules,
                "ign": {("seg", k) for k, s in enumerate(B["segs"]) if s["net"] in pair_nets} | {("via", k) for k, v in enumerate(B["vias"]) if v["net"] in pair_nets},
                "limit": lambda net: guard_limit(cons, net),
                "tol": lambda net: min(0.25 * guard_limit(cons, net), 0.25),
                "reserve": neg["reserve"], "preset": neg["preset"], "done": set(), "guide": guide,
                "escape": neg["escape"], "escape_reserve": neg["escape_reserve"],
                "tier": lambda q: key[q][:2] if q in key else (-1, -1)}

    def one_pass(order):
        ctx = mk_ctx()
        grid = ctx["grid"]
        ctx["left"] = lambda: len(order) - len(ctx["done"])          # pairs still to search in this pass (incl. the current one)
        st = {"res": {}, "grid": grid, "order": list(order)}
        prefix = ()
        for pr in order:
            c = cache.get((pr, prefix))
            if c is None:
                c = cache[(pr, prefix)] = _lay_pair(ctx, pr, info[pr], rules[pr], plan_only)
                log(_lay_line(pr, c))
            _apply(ctx, pr, c, plan_only)
            ctx["done"].add(pr)
            st["res"][pr] = c
            prefix += (pr,)
        st["score"] = (sum(1 for c in st["res"].values() if c["full"] or c["e"]["status"] == "kept"), sum(c["nok"] for c in st["res"].values()),
                       -round(sum(c["skew"] for c in st["res"].values()), 3))
        return st

    def near(c, pr):
        """smallest distance from the copper `c` laid to a terminal pad of pair `pr`"""
        best = 1e9
        for pcs in c["pcs"].values():
            for q in pcs:
                for t in info[pr][0]:
                    for pad in (t["p"], t["n"]):
                        if q[0] == "seg":
                            best = min(best, _pt_seg(pad["x"], pad["y"], q[2], q[3]) - q[4] / 2)
                        else:
                            best = min(best, math.hypot(pad["x"] - q[1], pad["y"] - q[2]) - q[3] / 2)
        return best

    def moves(st):
        """[(failed pair, the pair whose laid copper blocks it)] with the blocker earlier in the order.  The blocker is named by the
        search's report, or else by geometry: a laid pair whose copper runs within [pairlay] blocker_near_mm of a terminal pad of the
        failed pair (a failure reported against a pad is usually the copper beside the pad)."""
        out, pos = [], {pr: k for k, pr in enumerate(st["order"])}
        for f in st["order"]:
            c = st["res"][f]
            if c["full"] or c["e"]["status"] == "kept" or not info[f][2]:
                continue
            txt = c["e"].get("blocked_by", "")
            hit = [g for g in st["order"] if g != f and pos[g] < pos[f] and st["res"][g]["nok"]
                   and any(re.search(r"(^|[ \[(])" + re.escape(n) + r"($|[ \])])", txt) for n in g)] if txt else []
            if not hit:
                hit = [g for g in st["order"] if g != f and pos[g] < pos[f] and st["res"][g]["nok"] and near(st["res"][g], f) <= P["near_mm"]]
            if hit:
                out.append((f, min(hit, key=lambda g: pos[g])))
        return out

    order0 = sorted(pl, key=lambda pr: key[pr])
    res["negotiation"] = None
    live = [pr for pr in order0 if info[pr][2] and (open0.get(pr[0]) or open0.get(pr[1]))]
    res["escape"] = None
    if P["negotiate"] and len(live) > 1:
        # every section alone, in parallel; then only the pairs in a conflict are searched again (pairneg.py)
        from . import pairneg as NG
        tn = time.time()
        nctx = mk_ctx()
        if P["dogbone"]:                                     # exit candidates once, before the workers fork: the same list everywhere
            for pr in live:
                for t in info[pr][0]:
                    dogbone(nctx, t, rules[pr], pr)
        secs_of = {(pr, a, b): pr for pr in live for a, b in info[pr][2]
                   if math.hypot(info[pr][0][a]["c"][0] - info[pr][0][b]["c"][0], info[pr][0][a]["c"][1] - info[pr][0][b]["c"][1]) >= 1.5}
        envmax = max(2 * g[0] + g[1] for r in rules.values() for g in r["geo"].values())
        out, unresolved, used = NG.negotiate(nctx["grid"].F, sig, secs_of, lambda k, cost, bx: _alone(nctx, k, cost, bx), clr + envmax / 2,
                                             workers=P["workers"], passes=P["neg_passes"], log=log)
        for k, r in out.items():
            if r["ok"] and k[0] not in unresolved:
                neg["preset"][k] = (r["variant"], r["states"], r["pol"])
                neg["reserve"][k[0]] = neg["reserve"].get(k[0], []) + r["fp"]
        # a pair the negotiation could not settle has no corridor, so an earlier pair could take its break-out cells: the first
        # `exit_reserve_mm` of its alone path at each end stay reserved until it is laid (the lay finds the rest itself)
        for k, r in out.items():
            if r["ok"] and k[0] in unresolved and P["exit_reserve_mm"] > 0:
                ends = [info[k[0]][0][k[1]]["c"], info[k[0]][0][k[2]]["c"]]
                near_end = lambda x, y: min(math.hypot(x - c[0], y - c[1]) for c in ends) <= P["exit_reserve_mm"]
                neg["reserve"][k[0]] = neg["reserve"].get(k[0], []) + [
                    q for q in r["fp"] if (q[0] == "seg" and (near_end(q[2][0], q[2][1]) or near_end(q[3][0], q[3][1])))
                    or (q[0] == "via" and near_end(q[1], q[2]))]
        gates = {pr: sum(r.get("gates", 0) for k, r in out.items() if k[0] == pr and r["ok"]) for pr in live}
        alone_fail = sorted({"/".join(k[0]) for k, r in out.items() if not r["ok"]})
        # emission order: negotiated pairs first, most constrained (fewest gates) first; then the unresolved and failed-alone ones
        # W16 route-3: an unsettled pair is re-ordered only inside its own (prio, kind) tier, never behind a lower tier (PCIE_TX was 22nd of 34)
        rank = {pr: (key[pr][0], key[pr][1], pr in unresolved or "/".join(pr) in alone_fail, gates.get(pr, 0), key[pr][2:]) for pr in live}
        order0 = sorted(live, key=lambda pr: rank[pr]) + [pr for pr in order0 if pr not in live]
        res["negotiation"] = {"sections": len(secs_of), "passes": used, "unresolved": ["/".join(pr) for pr in sorted(unresolved)],
                              "failed_alone": alone_fail, "secs": round(time.time() - tn, 1), "workers": P["workers"]}
        log(f"pairs: negotiation {res['negotiation']}")
    best = one_pass(order0)
    if not plan_only:                                        # a plan claims no copper: nothing a retry or a rip could change
        _repair(best, info, rules, key, P, mk_ctx, near, plan_only, log)
    if live and not P["trial"]:                              # B5 repair: only when a pair failed its plain lay, never upfront (+790 s on W16 for no gain);
        # not in a ladder trial either: the main pass already asked the planner, each trial asked again for ~900 s per part (W16: 2700 s, no candidate)
        best = _escape_repair(best, order0, live, info, rules, key, cons, B, mk_ctx, one_pass, cache, neg, _lay_pair, _apply, log)
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
        failed = [f for f in best["order"] if not best["res"][f]["full"] and best["res"][f]["e"]["status"] != "kept" and info[f][2]]
        if failed:                                            # last resort: the failed pairs first, in the order they had
            cands.append(failed + [g for g in best["order"] if g not in failed])
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
        c["e"]["key"] = [key[pr][0], key[pr][1], round(key[pr][2], 3)]
        res["pairs"].append(c["e"])
        if c["nok"] and not plan_only and (c["full"] or P["keep_partial"]):   # a half-laid pair is no state: its copper is not written
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
    res["escape"] = best.get("escape_report")
    res["repair_warn"] = best.get("repair_warn", [])
    res["order"] = ["/".join(pr) for pr in best["order"]]
    res["secs"] = round(time.time() - t0, 1)
    return res


def _bad(c):
    return not (c["full"] or c["e"]["status"] == "kept")


def _quality(c, limit):
    """(vias, skew class) of a laid pair: class 0 = skew within the pair's limit, 1 = over it."""
    e = c["e"]
    return (sum(x.get("vias") or 0 for x in e.get("sections", [])), int((c.get("skew") or 0.0) > limit(e["pair"][0]) + 1e-9))


def _repair(st, info, rules, key, P, mk_ctx, near, plan_only, log):
    """End-of-pass repair of the pairs the pass left unlaid, on the pass's own result (the pairs that are fine are not searched again).
    A trial takes the failed pairs F and a set S of laid pairs off together (same or a lower tier only), lays F first and S after
    it on a state without presets and reservations (all are released at the end of a pass).  It is valid only when every ripped
    pair is complete again and at least one failed pair is laid, so a round never leaves fewer pairs than it found.
    S is tried smallest cause first: the pairs the failures NAME, then those plus the pairs whose copper runs beside the failed
    terminals; when no joint trial is valid, each failed pair alone with the same two sets.
    Quality guard: a ripped pair should not end worse than its first-pass lay (more vias, or skew over its limit when it was
    within).  A valid trial without such a loss is kept at once; otherwise the next S is tried and the valid trial with the
    smallest loss is kept, each loss logged as a WARN with its delta (a complete pair beats an open one).
    Rounds repeat while one gains ([pairlay] repair_rounds, 0 = off); the stage deadline stops it."""
    import re
    dl = P.get("deadline")
    over = lambda: bool(dl and time.time() > dl)
    nfail = [1]
    limit = None
    first = {}                                               # pair -> quality of its first-pass lay

    def state(skip):
        nonlocal limit
        ctx = mk_ctx()
        limit = limit or ctx.get("limit") or (lambda net: 0.0)
        ctx["preset"], ctx["reserve"], ctx["left"] = {}, {}, lambda: 2 * max(1, nfail[0])
        for q in st["order"]:
            if q not in skip and not _bad(st["res"][q]):       # a half-laid pair's copper is never written: no obstacle here
                _apply(ctx, q, st["res"][q], plan_only)
            ctx["done"].add(q)
        return ctx

    def blockers(f):
        """-> (the laid pairs the failure names, those + the ones beside the failed pair's terminals), each capped at repair_rip_max"""
        e = st["res"][f]["e"]
        txt = " | ".join([e.get("blocked_by", "")] + list(e.get("blockers", [])))
        cand = [g for g in st["order"] if g != f and st["res"][g]["full"] and st["res"][g]["nok"] and key[g][:2] >= key[f][:2]]
        hit = [g for g in cand if any(re.search(r"(^|[ \[(])" + re.escape(n) + r"($|[ \])])", txt) for n in g)]
        rest = sorted((g for g in cand if g not in hit and near(st["res"][g], f) <= P["near_mm"]), key=lambda g: near(st["res"][g], f))
        n = max(0, P["rip_max"])
        return hit[:n], (hit + rest)[:n]

    def trial(F, S, tag):
        """F then S on the state without both -> {pair: result} when valid, else None"""
        ctx, new = state(set(F) | set(S)), {}
        for q in list(F) + list(S):
            if over():
                return None
            c = new[q] = _lay_pair(ctx, q, info[q], rules[q], plan_only)
            log(_lay_line(q, c, tag))
            if q in S and not c["full"]:
                return None
            if c["full"]:                                    # a half-laid pair's copper is never written
                _apply(ctx, q, c, plan_only)
        return new if any(new[f]["full"] for f in F) else None

    def losses(new, S):
        out = []
        for g in S:
            a = first.setdefault(g, _quality(st["res"][g], limit))
            b = _quality(new[g], limit)
            if b[0] > a[0] or b[1] > a[1]:
                out.append((g, a, b))
        return out

    def attempt(F, sets, rnd):
        """the rip sets in order; the first valid trial without a quality loss is kept, else the valid one with the smallest loss"""
        best, seen = None, []
        for S in sets:
            S = [g for g in st["order"] if g in S]
            if S in seen:
                continue
            seen.append(S)
            new = trial(F, S, f"repair {rnd} " + (f"rip [{' '.join(g[0] for g in S)}] " if S else "retry "))
            if new is None:
                continue
            ls = losses(new, S)
            rank = (len(ls), sum(b[0] - a[0] for _g, a, b in ls))
            if best is None or rank < best[0]:
                best = (rank, new, ls)
            if not ls:
                break
        if best is None:
            return False
        for g, a, b in best[2]:
            log(f"pairs: WARN repair {rnd}: {g[0]}/{g[1]} re-laid worse than its first lay: vias {a[0]} -> {b[0]}"
                + (", skew now over its limit" if b[1] > a[1] else "") + " (kept: it completes " + ", ".join(f[0] for f in F if best[1][f]["full"]) + ")")
        st["res"].update(best[1])
        st.setdefault("repair_warn", []).extend({"pair": list(g), "vias": [a[0], b[0]], "skew_over": [a[1], b[1]]} for g, a, b in best[2])
        return True

    changed = False
    for rnd in range(1, max(0, P["repair_rounds"]) + 1):
        failed = [f for f in st["order"] if _bad(st["res"][f]) and info[f][2]]
        if not failed or over():
            break
        nfail[0] = len(failed)
        rip = {f: blockers(f) for f in failed}
        union = lambda k: [g for g in st["order"] if any(g in v[k] for v in rip.values())]
        gained = attempt(failed, [union(0), union(1)], rnd)
        if not gained and len(failed) > 1:                     # no joint trial is valid: one failed pair at a time
            for f in failed:
                gained = attempt([f], list(rip[f]), rnd) or gained
        changed = changed or gained
        if not gained:
            break
    if changed:
        st["grid"] = state(()).get("grid")
        st["score"] = (sum(1 for c in st["res"].values() if not _bad(c)), sum(c["nok"] for c in st["res"].values()),
                       -round(sum(c["skew"] for c in st["res"].values()), 3))
        st["repaired"] = True
    return st


def _escape_repair(best, order0, live, info, rules, key, cons, B, mk_ctx, one_pass, cache, neg, lay_pair, apply_, log):
    """B5: the pairs that failed the plain lay name the parts whose exits are planned jointly (pairescape.plan, focus = the failed pairs);
    the pass is repeated with the chosen exits and kept only when its score is higher (never rip a pair to gain nothing)."""
    failed = {q for q in best["order"] if not best["res"][q]["full"] and best["res"][q]["e"]["status"] != "kept" and info[q][2]}
    if not failed:
        return best
    from . import pairescape as PE

    def ghost(prs, escape, reserve):
        ctx = mk_ctx()
        ctx["escape"], ctx["escape_reserve"] = escape, reserve
        got = {}
        for q in prs:
            got[q] = c = lay_pair(ctx, q, info[q], rules[q], False)
            apply_(ctx, q, c, False)
            ctx["done"].add(q)
        return got
    try:
        esc = PE.plan(mk_ctx, ghost, info, rules, key, live, cons, B, lambda net: guard_limit(cons, net), log, order=order0, focus=failed)
    except Exception as e:                                   # noqa: BLE001 - without an escape plan the stage is what it was
        log(f"pairs: escape planner failed ({type(e).__name__}: {e}); generic exits")
        return best
    best["escape_report"] = esc["report"]
    if not esc["choice"]:
        return best
    neg["escape"].update(esc["choice"])
    neg["escape_reserve"].update(esc["reserve"])
    cache.clear()                                            # lay results depend on the exits
    from . import events as EV
    EV.emit("progress", "pairs", step=f"pairs: escape repair pass over {len(best['order'])} pair(s) with {len(esc['choice'])} planned exit(s)")
    st = one_pass(best["order"])
    st["escape_report"] = esc["report"]
    if st["score"] > best["score"]:
        log(f"pairs: escape repair score {st['score']} (was {best['score']})")
        return st
    neg["escape"].clear()
    neg["escape_reserve"].clear()
    cache.clear()
    log(f"pairs: escape repair gave {st['score']}, not above {best['score']}: dropped")
    return best


def has_twin_pads(board, cons, spec, P):
    """True when a pair ends on a part with two pads per net in an interleaved row (USB-C receptacle): the generic fan-out leaves four
    dead-end stubs there, so the pair exits are reserved before it runs (`[pairlay] reserve_exits` unset = this test)."""
    from . import routediag as RD, netclass as NC, pairaudit as PA
    try:
        B = RD.load(board)
        pairs = PA.pair_list(NC.classify(B, cons, spec))
        return any(t.get("twin") for pr in pairs for t in terminals(B, pr[0], pr[1], P["pad_max"], merge_twins=True)[0])
    except Exception:                                        # noqa: BLE001 - no verdict: the generic fan-out
        return False


def reserve_for_fanout(board, board_in, cons, spec, workdir):
    """pair-aware fan-out: plan every pair on the placement (no copper written), then give the fan-out a copy of its input
    board in which the planned exits are real locked copper - the first `reserve_mm` of both corridor ends at envelope
    width, the via pairs near them, new dog-bone exits.  -> (board with reservations, keys to strip, nets the pairs stage
    owns, seconds) | None.  `[pairlay] reserve_exits = false` keeps the generic fan-out."""
    from . import boardtext as BT
    P = params(cons)
    if not P["enabled"] or P["reserve_exits"] is False:
        return None
    if P["reserve_exits"] is None and not has_twin_pads(board_in, cons, spec, P):     # unset: on where the generic fan-out leaves dead stubs
        return None
    t0 = time.time()
    res = plan(board, cons, spec, plan_only=True)
    blks, keys, nets = [], set(), []
    u = lambda: str(uuid.uuid4())
    cu = res["B"]["copper"]

    def add(p, net):
        if p[0] == "seg":
            a, b = (round(p[2][0], 4), round(p[2][1], 4)), (round(p[3][0], 4), round(p[3][1], 4))
            if a == b:
                return
            keys.add(("s", tuple(sorted((a, b))), p[1], net))
            blks.append(f'\t(segment\n\t\t(start {_fmt(a[0])} {_fmt(a[1])})\n\t\t(end {_fmt(b[0])} {_fmt(b[1])})\n\t\t(width {_fmt(p[4])})\n'
                        f'\t\t(locked yes)\n\t\t(layer "{p[1]}")\n\t\t(net "{net}")\n\t\t(uuid "{u()}")\n\t)')
        else:
            x, y = round(p[1], 4), round(p[2], 4)
            keys.add(("v", x, y, net))
            blks.append(f'\t(via\n\t\t(at {_fmt(x)} {_fmt(y)})\n\t\t(size {_fmt(p[3])})\n\t\t(drill 0.2)\n'
                        f'\t\t(layers "{cu[0]}" "{cu[-1]}")\n\t\t(locked yes)\n\t\t(net "{net}")\n\t\t(uuid "{u()}")\n\t)')
    for e in res["pairs"]:
        if e.get("status") != "planned":
            continue                                        # a pair without a plan keeps the generic fan-out on its pins
        nets += e["pair"]
        for net, pcs in (e.get("join_pcs") or {}).items():
            for p in pcs:
                add(tuple(p), net)
        for s in e["sections"]:
            for p in s.get("keepfree", []):
                add(p, e["pair"][0])
            for parts in s.get("exits", []):
                for net, pcs in parts.items():
                    for p in pcs:
                        add(p, net)
    if not nets:
        return None
    dst = os.path.join(workdir, "pair-exits.kicad_pcb")
    with open(dst, "w", errors="surrogateescape") as fh:
        fh.write(BT.append_blocks(open(board_in, errors="surrogateescape").read(), blks))
    for ext in (".kicad_pro", ".kicad_dru"):
        side = os.path.splitext(board_in)[0] + ext
        if os.path.exists(side):
            import shutil
            shutil.copy(side, os.path.splitext(dst)[0] + ext)
    return dst, keys, nets, round(time.time() - t0, 1)


def strip_reservations(path, keys):
    """the fan-out result without the reservation copper of reserve_for_fanout -> items removed."""
    from . import boardtext as BT
    text = open(path, errors="surrogateescape").read()
    kill = []
    for _i, _j, key, blk in BT.scan(text):
        net = BT.net_name(blk)
        if (key[0] == "s" and ("s", tuple(sorted((key[1], key[2]))), key[4], net) in keys) or (key[0] == "v" and ("v", key[1], key[2], net) in keys):
            kill.append(key)
    if kill:
        with open(path, "w", errors="surrogateescape") as fh:
            fh.write(BT.remove_blocks(text, kill))
    return len(kill)


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


def run(board, cons=None, spec=None, out=None, plan_only=False, only=None, log=lambda m: None, fallback=True, cs_args=None, guide=None):
    """plan (and with `out`, write) -> report dict: complete / total, per pair length, skew, vias, layers, reasons."""
    from . import routediag as RD, pairaudit as PA, routeconn as RC, constraints as C, routerules as RR
    cons = cons or {}
    P0, cons_plan = params(cons), cons
    if P0["budget_s"] > 0 and not P0["deadline"] and not plan_only and out and fallback:      # B11: one wall budget for plan + ladder; the main pass gets 60 %
        from . import pairfb as FB0
        t_run = time.time()
        cons = FB0.variant(cons, _deadline=t_run + P0["budget_s"])
        cons_plan = FB0.variant(cons, _deadline=t_run + 0.6 * P0["budget_s"])
    res = plan(board, cons_plan, spec, plan_only=plan_only or not out, only=only, log=log, guide=guide)
    rep = {"board": board, "out": out, "plan_only": bool(plan_only or not out), "secs": res["secs"], "signal_layers": res["signal_layers"],
           "passes": res.get("passes", 0), "order": res.get("order", []), "negotiation": res.get("negotiation"), "escape": res.get("escape"),
           "repair_warn": res.get("repair_warn", []),
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
        pn = [n for e in res["pairs"] for n in e["pair"]]            # a via the pair passes straight through on one layer is a barrel stub: gone
        txt2, dead = PA.strip_dead_vias(open(out, errors="replace").read(), B2, pn)
        if dead:
            open(out, "w").write(txt2)
            B2 = RD.load(out)
            rep["dead_vias_removed"] = [[r["net"], r["x"], r["y"], r["stub_mm"]] for r in dead]
            log(f"pairs: PAIR_VIA_STUB removed {len(dead)} via(s) the pair never changes layer at: " + ", ".join(f"{r['net']}@({r['x']:.3f},{r['y']:.3f}) {r['stub_mm']:.2f} mm" for r in dead))
    opn = RC.text_open(B2)
    aud = PA.audit(B2, [tuple(e["pair"]) for e in res["pairs"]], cons, {n for n, k in opn.items() if k})
    # a terminal with its pads on two layers cannot give both legs one layer sequence: that copper stays (connected), reported as a mismatch
    odd = lambda e: any("@via-on-" in x for s_ in e["sections"] for x in s_.get("via_exit", [])) and \
        set(aud[tuple(e["pair"])].get("causes", [])) <= {"layers", "vias", "skew"}   # the odd pad's via is a via-height skew too
    only_skew = lambda e: aud[tuple(e["pair"])].get("causes") == ["skew"] and e["status"] == "laid"     # complete, same layers and vias: tune fixes skew
    bad = [e for e in res["pairs"] if e["status"] in ("laid", "partial") and aud[tuple(e["pair"])]["status"] == "mismatch" and not odd(e)
           and not only_skew(e)] \
        if out and not plan_only else []
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
               "unpaired": e.get("unpaired", []), "netlist": {n: [f"{q['ref']}.{q['num']}" + (f" ({B2['parts'][q['ref']]['value']})" if B2["parts"].get(q["ref"], {}).get("value") else "")
                                                                 for q in B2["pads"] if q["net"] == n and not q.get("np")] for n in pr}, "limit": C.skew_limit_checked(cons, pr[0])[0], "limit_from": C.skew_limit_checked(cons, pr[0])[1], "geometry": e.get("geometry", {}), "key": e.get("key"),
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
    if not rep["plan_only"] and fallback and out:
        from . import pairfb as FB
        FB.assign_states(rep)
        if FB.pending(rep):
            FB.ladder(rep, out, cons, spec, P, run, cs_args=cs_args, escape=(lambda b, nets: _escape(b, nets, cs_args, log)) if cs_args else None, log=log)
            if rep["fallback"]["steps"] or any(r.get("state") == "c" for r in rep["pairs"]):
                B2 = RD.load(out)
                opn = RC.text_open(B2)
                aud = PA.audit(B2, [tuple(r["pair"]) for r in rep["pairs"]], cons, {n for n, k in opn.items() if k})
                FB.reconcile(rep, aud, opn)
                FB.handoff_effort(rep, P)
        rep["complete"] = sum(1 for r in rep["pairs"] if r["status"] == "complete")
        rep["laid"] = laid_count(rep)
        rep["states"] = {k: sum(1 for r in rep["pairs"] if r.get("state") == k) for k in FB.STATES + ("a-mismatch",)}
        rep["handoff"] = sorted(n for r in rep["pairs"] if r.get("handoff") for n in r["pair"])
    rep["verdict"] = "PAIRS_OK" if rep["complete"] == rep["total"] else "PAIRS_WARN"
    return rep


def laid_count(rep):
    """Pairs laid as coupled copper: complete within the target skew + laid over it (audit 'mismatch'). Owner realistic-limit rule (D71ft)."""
    return sum(1 for r in rep["pairs"] if r["status"] in ("complete", "mismatch"))


def format_text(rep, top=None):
    out = [f"pairs: {rep['verdict']} - {rep['complete']}/{rep['total']} {'planned' if rep['plan_only'] else 'complete'}; "
           f"signal layers {' '.join(l[:-3] for l in rep['signal_layers'])}; {rep['secs']} s"
           + (f", negotiation {rep['negotiation']['passes']} pass(es) {rep['negotiation']['secs']} s on {rep['negotiation']['workers']} worker(s)" if rep.get("negotiation") else "")
           + (f"; {rep.get('added', 0)} locked items written, {rep.get('removed', 0)} fan-out items replaced" if not rep["plan_only"] else "")]
    rows = sorted(rep["pairs"], key=lambda r: (r["status"] in ("complete", "planned"), r["pair"]))
    if rep.get("states"):
        out.append("pairs states: " + ", ".join(f"{k} {v}" for k, v in rep["states"].items() if v)
                   + (f"; fallback {rep['fallback']['secs']} s" if rep.get("fallback") else "")
                   + (f"; {len(rep['handoff']) // 2} pair(s) handed to the router as ordinary nets (c)" if rep.get("handoff") else ""))
    if rep.get("handoff_effort"):
        out.append(rep["handoff_effort"]["summary"])
    for r in rows[: top or len(rows)]:
        nm = "/".join(r["pair"])
        if r["status"] == "complete" and "length" in r:
            inner = sorted({f"{l[:-3]} {g['width']:g}/{g['gap']:g} ({g['from']})" for l, g in r.get("geometry", {}).items()
                            if l in r["layers"] and g["from"] != "class"})
            out.append(f"  {nm}: {'[' + r['state'] + '] ' if r.get('state') else ''}complete {r['length'][0]:.2f} / {r['length'][1]:.2f} mm, skew {r['skew']:.3f} (limit {r['limit']:g} {r.get('limit_from', '')}), "
                       f"{r['vias']} via(s), {'>'.join(l[:-3] for l in r['layers']) or '-'}, {r['width']:g}/{r['gap']:g} {r['class']}"
                       + (f", {'; '.join(inner)}" if inner else "") + (f", exit via {' '.join(r['via_exit'])}" if r.get("via_exit") else "")
                       + f", {r.get('secs', 0)} s" + (" [kept]" if r["plan"] == "kept" else ""))
        elif r["status"] == "planned":
            out.append(f"  {nm}: planned, {r.get('vias', 0)} via pair(s), {' '.join(l[:-3] for l in r.get('layers', [])) or '-'}")
        else:
            out.append(f"PAIR_NOT_LAID {nm} [{r['status']}/{r['plan']}, {r.get('secs', 0)} s] {r['why'] or 'open connections remain'}"
                       + (f"; blocked by {', '.join(r['blockers'][:5])}" if r.get("blockers") else "")
                       + (f"; PAIR_HANDOFF state c: two ordinary nets of class {r['class']} for the router, ladder tried {', '.join(r.get('tried', [])) or '-'}" if r.get("handoff") else "")
                       + (f"; effort {', '.join(r['handoff_reasons'])}" if r.get("handoff_reasons") else ""))
    return "\n".join(out)


def pad_questions(rep, board, cons, answers=None, assume=False):
    """a pair pad without a partner pad is a netlist / spec question, not a silent failure -> questions.Session (may be open)."""
    from . import questions as QS
    S = QS.Session(board, cons, answers=answers, assume=assume, cmd="pairs")
    for r in rep["pairs"]:
        lone = sorted({x for x in (r.get("unpaired") or [])})
        if lone and r["status"] != "complete":
            r["question"] = f"pair-pads:{r['pair'][0]}"
            nl = "; ".join(f"{n} = {', '.join(v)}" for n, v in (r.get("netlist") or {}).items())
            r["answer"] = S.ask(r["question"], "pairs", f"{'/'.join(r['pair'])}: pad(s) {', '.join(lone)} have no partner pad of the other leg within reach "
                                f"(one leg has a drop the other has not, or the two legs land on parts far apart). Netlist: {nl}",
                                {"single-ended": "these pads are tied in as single-ended drops by the router; the coupled sections stay as laid",
                                 "netlist": "the netlist or spec is wrong / incomplete here: fix it and run again (the pair stays open)"},
                                "single-ended", "an [answers] entry, or the missing part / net in the schematic")
    return S


def _escape(board, nets, cs_args, log=lambda m: None):
    """ordinary fan-out escapes for the pins of `nets` on `board` (edited in place) -> {nets, required, escaped, blocked}."""
    import shutil
    out = {}
    import cli
    import contextlib
    import io
    tmp = os.path.splitext(board)[0] + ".completion.kicad_pcb"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            cli.main(["fanout", board, "--out", tmp, "--nets", ",".join(nets), "--no-drc"] + list(cs_args))
        except SystemExit:
            pass
    log(buf.getvalue())
    fj = os.path.splitext(tmp)[0] + ".fanout.json"
    if os.path.exists(tmp) and os.path.exists(fj):
        d = json.load(open(fj))
        out.update(nets=len(nets), required=d.get("required", 0), escaped=d.get("escaped", 0),
                   blocked=[f"{p['ref']}.{p['pin']} {p['net']}" for p in d.get("blocked", [])])
        shutil.move(tmp, board)
        x = os.path.splitext(tmp)[0] + ".fanout.json"
        if os.path.exists(x):
            shutil.move(x, os.path.splitext(board)[0] + ".completion.fanout.json")
    return out


def complete_fanout(board, src_board, cs_args, cons, spec, rep, log=lambda m: None):
    """after the pairs are laid: ordinary escapes for the pads of the pairs that were NOT laid (their pads were left to this
    stage by the pair-aware fan-out), and one more try (fanalt.py) for the pins the fan-out left blocked - the real pair copper
    is narrower than the reservation was.  Edits `board` in place -> {nets, escaped, required, blocked, retried, rescued}."""
    import shutil
    from . import routediag as RD, fanalt as FALT
    out = {"nets": 0, "required": 0, "escaped": 0, "blocked": [], "retried": 0, "rescued": 0, "still_blocked": []}
    B = RD.load(board)
    has = {s["net"] for s in B["segs"]} | {v["net"] for v in B["vias"]}
    nets = sorted({n for r in rep["pairs"] if r["status"] != "complete" for n in r["pair"] if n not in has})
    prev = os.path.splitext(src_board)[0] + ".fanout.json"
    params_ = None
    if os.path.exists(prev):
        try:
            pj = json.load(open(prev))
            params_ = pj.get("params")
            left = [p for p in pj.get("pins", []) if not p.get("ok")]
            if left and params_:
                st = FALT.rescue(board, {"pins": left, "params": params_}, cons, spec)
                out.update(retried=st["tried"], rescued=st["tried"] - st["left"], still_blocked=[f"{p['ref']}.{p['pin']} {p['net']}" for p in left if not p.get("ok")])
        except (OSError, ValueError, KeyError):
            pass
    if nets:
        out.update(_escape(board, nets, cs_args, log))
    return out


def cmd_only(a, cons, spec):
    """pairs --only: re-route named pairs whole on a routed board (pairsonly.py). Exit 0 all done, 1 some kept / refused /
    unmatched, 2 stopped by --budget or a signal (--out is the best accepted board)."""
    from . import pairsonly as PO
    out = a.out or os.path.splitext(a.board)[0] + ".pairs.kicad_pcb"
    if os.path.abspath(out) == os.path.abspath(a.board):
        raise SystemExit("pairs: --out must differ from the input board")
    rep = PO.reroute(a.board, out, [x for x in a.only.split(",") if x.strip()], cons, spec, budget=a.budget,
                     allow_shove=a.allow_shove, unlock=a.unlock, baseline=getattr(a, "baseline", None), accept_skew_x=getattr(a, "accept_skew_x", 1.0), log=lambda m: print(m, file=sys.stderr, flush=True) if getattr(a, "verbose", False) else None)
    rp = out + ".pairsonly.json"
    json.dump(rep, open(rp, "w"), indent=1)
    print(PO.format_text(rep))
    print(f"full report: {rp}\nwrote {out}")
    if rep["stopped"] in ("budget", "signal"):
        return 2
    return 0 if all(r["status"] in ("rerouted", "already_ok") for r in rep["pairs"]) and not rep["unmatched"] and rep["pairs"] else 1


def cmd_pairs(a):
    from . import constraints as C
    cons = C.load(a.constraints)
    spec = json.load(open(a.spec)) if a.spec else None
    if getattr(a, "only", None):
        return cmd_only(a, cons, spec)
    out = None if a.plan_only else (a.out or os.path.splitext(a.board)[0] + ".pairs.kicad_pcb")
    if out and os.path.abspath(out) == os.path.abspath(a.board):
        raise SystemExit("pairs: --out must differ from the input board")
    only = set(x.strip() for x in a.nets.split(",") if x.strip()) if a.nets else None
    if not a.plan_only:                    # the route stage's collision gate, before any pairs work: same rows, same message
        from . import placementcheck as PCK, routediag as RD
        fails = PCK.gate_failures(RD.load(a.board), cons)
        if fails:
            raise SystemExit(f"pairs: refused - {len(fails)} FAIL collision row(s) the route stage would also refuse "
                             f"(placement-check lists them):\n" + "\n".join(f"  COLLISION {x['msg'][:150]}" for x in fails[:6]))
    import sys
    cs0 = (["--constraints", a.constraints] if a.constraints else []) + (["--spec", a.spec] if a.spec else [])
    for x in getattr(a, "answer", None) or []:
        cs0 += ["--answer", x]
    rep = run(a.board, cons, spec, out, plan_only=a.plan_only, only=only, cs_args=cs0,
              log=(lambda m: print(m, file=sys.stderr, flush=True)) if getattr(a, "verbose", False) else (lambda m: None))
    rp = os.path.splitext(out or a.board)[0] + (".pairs.plan.json" if a.plan_only else ".pairs.json")
    if out and getattr(a, "complete_fanout", False):
        cs = (["--constraints", a.constraints] if a.constraints else []) + (["--spec", a.spec] if a.spec else [])
        for x in getattr(a, "answer", None) or []:
            cs += ["--answer", x]
        rep["completion"] = complete_fanout(out, a.board, cs, cons, spec, rep)
    from . import questions as QS
    S = pad_questions(rep, a.board, cons, QS.parse_answers(getattr(a, "answer", None)), getattr(a, "assume_defaults", False))
    json.dump(rep, open(rp, "w"), indent=1)
    q = getattr(a, "quiet", False)
    txt = format_text(rep, None if (getattr(a, "full_output", False) or getattr(a, "full", False)) else (a.top or 12))
    print(txt.splitlines()[0] if q else txt)
    if rep.get("completion"):
        c_ = rep["completion"]
        print(f"pairs: fan-out completion: {c_['escaped']}/{c_['required']} pins of {c_['nets']} unlaid pair net(s) escaped"
              + (f", blocked {', '.join(c_['blocked'][:8])}" if c_["blocked"] else "")
              + f"; earlier blocked pins retried {c_['retried']}, rescued {c_['rescued']}" + (f", still blocked {', '.join(c_['still_blocked'])}" if c_["still_blocked"] else ""))
    print(f"full report: {rp}")
    if out:
        try:
            from . import questions as QS
            QS.inherit(a.board, out)
        except Exception:                                   # noqa: BLE001 - the answers file is optional
            pass
        print(f"wrote {out}")
    if S.open:                                              # the board is written; the questions go to the user (exit 3)
        return S.gate(out=lambda m: print(m.replace(", nothing written", ", the board IS written (pairs laid); the open pads stay with the router")))
    S.persist()
    return 0 if rep["verdict"] == "PAIRS_OK" else 1


def _stage_status(path, state, message="", **fields):
    from . import routestatus as RS
    st = {"stage": "pairs", "state": state, "pid": os.getpid(), "current_step": str(message)[:240], "updated": time.time()}
    st.update(fields)
    RS._atomic(path, st)


def stage(c, n, board):
    """pipeline stage (constructive flow, after fan-out): incomplete pairs stay with the router (WARN); a stage error fails."""
    if not params(c.cons)["enabled"]:
        return {"verdict": "skipped", "line": "[pairlay] enabled = false", "board": board}
    d = c.dir(n, "pairs")
    out = os.path.join(d, "board.kicad_pcb")
    spath = os.path.join(d, "progress.json")
    _stage_status(spath, "running", "starting", board=out)
    try:
        cs_args = c.cs() + c.qs()
    except AttributeError:                                  # a bare context (tests): no fan-out re-escape, no rip of stubs
        cs_args = None
    try:
        rep = run(board, c.cons, c.spec, out, cs_args=cs_args, log=lambda m: _stage_status(spath, "running", m, board=out),
                  guide=getattr(c, "guide", None),
                  only=getattr(c, "only", None) or None)
    except Exception as e:                                  # noqa: BLE001 - an unmeasured board must not advance as if the stage ran
        msg = f"pairs failed ({type(e).__name__}: {str(e)[:160]})"
        with open(os.path.join(d, "stage.log"), "a") as fh:
            fh.write(msg + "\n")
        _stage_status(spath, "failed", msg, board=board)
        return {"verdict": "fail", "line": msg, "board": board, "next": ["pairs stage raised; no pair report was produced"]}
    try:
        rep["completion"] = complete_fanout(out, board, c.cs() + c.qs(), c.cons, c.spec, rep)
    except Exception as e:                                  # noqa: BLE001 - the laid pairs stand
        rep["completion"] = {"error": f"{type(e).__name__}: {e}"}
    json.dump(rep, open(os.path.join(d, "board.pairs.json"), "w"), indent=1)
    with open(os.path.join(d, "stage.log"), "a") as fh:
        fh.write(format_text(rep) + "\n")
    fm = os.path.splitext(board)[0] + ".fluxmap.json"
    if os.path.exists(fm):
        import shutil
        shutil.copy(fm, os.path.join(d, "board.fluxmap.json"))
    line = format_text(rep).splitlines()[0]
    _stage_status(spath, "done" if rep["verdict"] == "PAIRS_OK" else "warn", line, board=out,
                  complete=rep["complete"], total=rep["total"], report=os.path.join(d, "board.pairs.json"))
    res = {"verdict": "pass", "line": ("WARN " if rep["verdict"] != "PAIRS_OK" else "") + line, "board": out,
           "complete_pairs": [tuple(r["pair"]) for r in rep.get("pairs") or [] if r.get("status") == "complete" and len(r.get("pair") or ()) == 2],
           "metrics": {"complete": rep["complete"], "total": rep["total"],
                       "not_searched": sum(1 for r in rep.get("pairs") or [] if "budget" in str(r.get("why", "")))}}
    if rep["verdict"] != "PAIRS_OK":
        S = pad_questions(rep, out, c.cons, getattr(c, "answers", None), getattr(c, "assume", False))
        res["next"] = [f"question {q['id']}: {q['found']}" for q in S.open][:3] + \
            [l for l in format_text(rep).splitlines() if l.startswith("PAIR_NOT_LAID")][:5]
    return res


def add_parser(sub):
    p = sub.add_parser("pairs", help="lay the differential pairs by construction (coupled, locked) before routing; PAIRS report")
    p.add_argument("board")
    p.add_argument("--constraints", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--out", default=None, help="output board (default <board>.pairs.kicad_pcb)")
    p.add_argument("--plan-only", dest="plan_only", action="store_true", help="centre-line corridors only; writes no copper")
    p.add_argument("--nets", default=None, help="comma-separated nets: only the pairs of these nets")
    p.add_argument("--only", default=None, metavar="A,B",
                   help="re-route these pairs whole on a ROUTED board (pair names as the audit prints them, a net name or prefix); "
                        "a pair is kept as it was unless the new one is connected, in tolerance and DRC/open-clean")
    p.add_argument("--allow-shove", dest="allow_shove", action="store_true",
                   help="with --only: when blocked, remove the unlocked signal-net neighbours the blocker report names and report the nets opened")
    p.add_argument("--baseline", default=None, metavar="BOARD",
                   help="with --only: the previous version's board; each named pair is re-laid at the track width it has there "
                        "(else the input board's width above the DRC minimum; never the DRC minimum)")
    p.add_argument("--accept-skew-x", dest="accept_skew_x", type=float, default=1.0, metavar="X",
                   help="with --only: keep a coupled re-laid pair whose only defect is skew when it is within X times its limit "
                        "(default 1 = strict); the review still reports the skew")
    p.add_argument("--unlock", action="store_true",
                   help="with --only: temporarily unlock the named pairs' own copper; re-lock accepted results, restore original locks on failure")
    p.add_argument("--budget", type=float, default=None, metavar="S",
                   help="with --only: wall-clock seconds (default 120 x named pairs); out of time or SIGTERM writes the best accepted board")
    p.add_argument("--complete-fanout", dest="complete_fanout", action="store_true",
                   help="afterwards: ordinary escapes for the pads of unlaid pairs, and the blocked fan-out pins tried again")
    from . import questions as QS
    QS.add_args(p, json_flag=False)
    p.add_argument("-v", "--verbose", action="store_true", help="one line per pair and re-order pass on stderr")
    p.add_argument("--full", dest="full_output", action="store_true")
    p.add_argument("-q", "--quiet", dest="quiet", action="store_true")
    p.add_argument("--top", type=int, default=None)
    p.set_defaults(fn=cmd_pairs)


def guard_limit(cons, net):
    """Routing guard skew limit (mm). A ps limit with no known layer delay returns inf: the guard does not grade
    that pair (the review reports PAIR_DELAY_UNKNOWN); it never raises."""
    from . import constraints as C
    lim, _src = C.skew_limit_checked(cons, net)
    return float("inf") if lim is None else lim


# ------------------------------------------------ cross-check against an external pair router
_OK_OUTCOMES = {"coupled"}
_BAD_LISTS = ("failed_diff_pairs", "partial_diff_pairs", "member_incomplete_pairs", "single_ended_diff_pairs", "uncoupled_diff_pairs")


def _stem_of(stem, p, n):
    """the router names a pair by its common stem ("BT3_D" for BT3_DP / BT3_DM): both nets start with it and add <= 3 characters."""
    return bool(stem) and p.startswith(stem) and n.startswith(stem) and len(p) - len(stem) <= 3 and len(n) - len(stem) <= 3


def _read_maybe(x):
    """dict | JSON path | log text | log path | None -> (dict or None, text)."""
    if isinstance(x, dict):
        return x, ""
    if isinstance(x, (str, os.PathLike)):
        s = str(x)
        txt = open(s, errors="replace").read() if "\n" not in s and os.path.isfile(s) else s
        try:
            d = json.loads(txt)
            return (d if isinstance(d, dict) else None), ""
        except ValueError:
            return None, txt
    return None, ""


def crosscheck_external(pairs, summary=None, log=None):
    """The pairs fluxplace knows that the external pair router did NOT recognise or did NOT route as a pair.
    pairs: [(p, n)] (pairaudit.pair_list). summary: the router's `--json-out` dict or path (pair_reports, routed_diff_pairs,
    failed/partial/member_incomplete/single_ended/uncoupled lists, routed_single ...); log: its log text or path ("Routing diff pair
    STEM", "No differential pairs found ... Patterns provided: [...]"). Either may be missing.
    -> [{pair, p, n, reason, detail}], reason "not_recognised" (the router never saw it as a pair: absent, routed as two
    single nets, or its patterns matched no pair) or "not_routed_as_pair" (recognised, but not coupled / incomplete / fell back to
    single-ended). A pair the router did route coupled and complete is not listed. Hand the rows to fluxplace's own pair layer
    (`run(board, cons, spec, out, only={nets})` or `pairsonly.reroute`)."""
    d, t1 = _read_maybe(summary)
    d2, t2 = _read_maybe(log)
    d = d or d2 or {}
    text = t1 + "\n" + t2
    stems = set(re.findall(r"Routing diff pair (\S+)", text))
    nomatch = set()
    if "No differential pairs found" in text:
        m = re.search(r"Patterns provided: (\[[^\]]*\])", text)
        try:
            nomatch = set(__import__("ast").literal_eval(m.group(1))) if m else set()
        except (ValueError, SyntaxError):
            nomatch = set()
    reports = {frozenset((r.get("p_net"), r.get("n_net"))): r for r in d.get("pair_reports", []) or []}
    single = set(d.get("routed_single", []) or []) | set(d.get("failed_single", []) or []) | set(d.get("open_single", []) or [])
    bad = {k: list(d.get(k, []) or []) for k in _BAD_LISTS}
    good = list(d.get("routed_diff_pairs", []) or [])
    out = []
    for p, n in pairs:
        row = {"pair": f"{p}/{n}", "p": p, "n": n}
        r = reports.get(frozenset((p, n)))
        stem = next((s for s in good + sum(bad.values(), []) + sorted(stems) if _stem_of(s, p, n)), None)
        if r:
            probs = []
            if r.get("outcome") not in _OK_OUTCOMES:
                probs.append(f"outcome {r.get('outcome')}" + (f" at {r['failure_stage']}" if r.get("failure_stage") else ""))
            if r.get("incomplete_members"):
                probs.append("members incomplete: " + ", ".join(map(str, r["incomplete_members"])))
            if r.get("no_coupled_trunk"):
                probs.append("no coupled trunk")
            if r.get("member_audit_mismatch"):
                probs.append("member audit mismatch")
            if r.get("se_fallback"):
                probs.append("single-ended fallback")
            if r.get("failure_reason"):
                probs.append(str(r["failure_reason"])[:120])
            if probs:
                out.append(dict(row, reason="not_routed_as_pair", detail="; ".join(probs)))
            continue
        if stem is not None:
            hit = [k for k, v in bad.items() if stem in v]
            if hit:
                out.append(dict(row, reason="not_routed_as_pair", detail=f"{stem} listed in {', '.join(hit)}"))
            continue                                       # recognised and routed (stem in routed_diff_pairs or in the log)
        if p in nomatch or n in nomatch:
            why = "the router's --nets patterns matched no differential pair (net names without _P/_N, -P/-N, P/N or +/-)"
        elif p in single or n in single:
            why = "routed as single-ended nets, not as a pair"
        else:
            why = "absent from the router's summary and log"
        out.append(dict(row, reason="not_recognised", detail=why))
    return out
