"""Pair escape planning (B5): choose the exits of every pair on a component TOGETHER, then prove the choice by routing.

The pairs stage lays pair after pair; the first pair on a part takes the break-out room the next one needs (W16: ETH_P0 walls
ETH_P1 in at its part).  Three steps, all geometric (no ref designators, no net names):

  1. bundles   per component with pair terminals and per pair terminal, every legal exit: the terminal variant (the pads, a fan-out
               via pair, a new dog-bone via pair), the layer the pair leaves on, the heading, and the copper that implies (stubs,
               vias, the two coupled legs for `reach` mm).  A bundle is dropped when it fouls the board, leaves a neighbouring pin
               (or rail via site) of the same part without a lane, or leaves no length budget (channel.predict).
  2. select    backtracking over the terminals of a neighbourhood with forward checking (a chosen bundle removes every bundle it
               conflicts with; an empty domain prunes at once), `[pairlay] escape_expansions` partial expansions (default 10 000,
               logged); complete assignments are ranked by cost and the best `escape_shortlist` (<= 5) are kept.
  3. verify    each shortlisted assignment is laid by the production lay (`ghost` callback) for the target pairs plus every live pair
               of equal or higher rank whose straight corridor crosses the neighbourhood (geometry only), and judged by what the
               pairs stage itself judges (laid in full, skew inside the guard limit).  A neighbourhood starts at one component and
               grows only to the owner of a blocker a failed probe names.

Probe memory: a geometric conflict forbids that combination; a timeout / budget stop deprioritises it; a contract refusal is
recorded (with the violation) and does not forbid.

The result is `{"choice": {terminal key: variant}, "reserve": {pair: pieces}, "report": {...}}`; pairs.py puts the chosen variant
first for its terminal and keeps the chosen exits reserved (hard for the other pairs) until the owning pair is laid."""
import math
import time

from . import pairgeo as G
from . import pairlay as PL

DIRS = [(1, 0), (0.7071, 0.7071), (0, 1), (-0.7071, 0.7071), (-1, 0), (-0.7071, -0.7071), (0, -1), (0.7071, -0.7071)]
MAX_DOMAIN = 60


def params(cons):
    e = dict((cons or {}).get("pairlay") or {})
    return {"on": e.get("escape", True) is not False, "expansions": int(e.get("escape_expansions", 10000)),
            "shortlist": max(1, min(5, int(e.get("escape_shortlist", 5)))), "reach": float(e.get("escape_reach_mm", 3.0)),
            "grow": int(e.get("escape_grow", 2)), "budget_s": float(e.get("escape_budget_s", 900.0)),
            "dirs": int(e.get("escape_dirs", 4)), "variants": int(e.get("escape_variants", 6)),
            "verify_reach": float(e.get("escape_verify_reach_mm", 8.0))}


def tkey(pr, t):
    """identity of a base terminal: pair + part(s) + position (rounded).  No ref names are interpreted, only compared."""
    return (tuple(pr), t["name"], round(t["c"][0], 3), round(t["c"][1], 3))


def vkey(v):
    return (v["name"], round(v["c"][0], 3), round(v["c"][1], 3))


def components(info, live):
    """{part: [(pair, terminal index)]} for every terminal of the live pairs; a terminal belongs to the part of its P pad."""
    out = {}
    for pr in live:
        for ti, t in enumerate(info[pr][0]):
            out.setdefault(t["p"]["ref"], []).append((pr, ti))
    return out


# ---------------------------------------------------------------- 1. bundles

def _pieces_of(v):
    out = {}
    for net, pcs in (v.get("new") or {}).items():
        out.setdefault(net, []).extend(pcs)
    for kind, o in v.get("keep", []):
        out.setdefault(o["net"], []).append(("seg", o["layer"], o["a"], o["b"], o["w"]) if kind == "seg" else ("via", o["x"], o["y"], o["d"]))
    return out


def _variants_of(ctx, t, rule, pr, nmax):
    """the terminal itself, its fan-out via pair, and up to `nmax` new dog-bone via pairs (more than the lay's default two)."""
    from . import pairs as PRS
    out = [t]
    if t.get("via"):
        out.append(t["via"])
    if ctx["P"]["dogbone"]:
        t.pop("dog", None)
        out += PRS.dogbone(ctx, t, rule, pr, limit=nmax)
        t.pop("dog", None)                                  # the lay's own list (limit 2) is built again on demand
    return out


def _leave_layers(v, sig):
    both = [l for l in sig if l in v["p"]["layers"] and l in v["n"]["layers"]]
    return both


def _legs(v, layer, d, reach, rule):
    """the two coupled legs leaving variant `v` along unit `d` on `layer` for `reach` mm: {net-slot: piece}."""
    w = rule["geo"].get(layer, (rule["w"],))[0]
    out = {}
    for slot in ("p", "n"):
        pad = v[slot]
        a = (pad["x"], pad["y"])
        out[slot] = ("seg", layer, a, (a[0] + d[0] * reach, a[1] + d[1] * reach), w)
    return out


def _lane_free(items, pad, clr, w, via_d, extra, skip):
    """a neighbouring pin keeps a way out: some octilinear lane of 1.0 mm from its centre clears the board and `extra` (the
    candidate's copper), ending where a via fits."""
    layer = next(iter(pad["layers"]), None)
    for d in DIRS:
        e = (pad["x"] + d[0] * 1.0, pad["y"] + d[1] * 1.0)
        pcs = [("seg", layer, (pad["x"], pad["y"]), e, w), ("via", e[0], e[1], via_d)]
        if PL.check(items, pcs, pad["net"], clr, skip)[0] < clr - 1e-6:
            continue
        if all(_gap_piece(pcs, x) >= clr - 1e-6 for x in extra):
            return True
    return False


def _piece_dist(p, q):
    if p[0] == "seg" and q[0] == "seg":
        return G.seg_seg(p[2], p[3], q[2], q[3]) - (p[4] + q[4]) / 2
    if p[0] == "via" and q[0] == "via":
        return math.hypot(p[1] - q[1], p[2] - q[2]) - (p[3] + q[3]) / 2
    s, v = (p, q) if p[0] == "seg" else (q, p)
    return G.pt_seg((v[1], v[2]), s[2], s[3]) - (s[4] + v[3]) / 2


def _gap_piece(pcs, x):
    """smallest gap of a list of pieces to piece `x`; pieces on different layers never touch (a via is on every layer)."""
    best = 1e9
    for p in pcs:
        if p[0] == "seg" and x[0] == "seg" and p[1] != x[1]:
            continue
        best = min(best, _piece_dist(p, x))
    return best


def foot_conflict(fa, fb, clr):
    """True when two footprints (lists of pieces) come closer than `clr` on a shared layer (vias span all layers)."""
    for p in fa:
        for q in fb:
            if p[0] == "seg" and q[0] == "seg" and p[1] != q[1]:
                continue
            if _piece_dist(p, q) < clr - 1e-9:
                return True
    return False


def bundles(ctx, pr, ti, rule, others, p):
    """legal exit bundles of terminal `ti` of pair `pr` -> [bundle], cheapest first.  `others` = [pair terminal centres] (the heading)."""
    from . import channel as CH
    info = ctx["info"][pr][0]
    t = info[ti]
    items, clr = ctx["items"], rule["clr"]
    sig = ctx["grid"].sig
    tgt = others[0] if others else None
    head = G.unit(t["c"], tgt) if tgt else (0.0, 0.0)
    budget = ctx.get("budget", {}).get(pr)
    out = []
    ref = t["p"]["ref"]
    nbrs = [q for q in ctx["grid"].B["pads"] if q["ref"] == ref and q["net"] and q["net"] not in pr and not q.get("np")]
    for v in _variants_of(ctx, t, rule, pr, p["variants"]):
        own = _pieces_of(v)
        flat = [x for pcs in own.values() for x in pcs]
        stub = sum(math.hypot(x[3][0] - x[2][0], x[3][1] - x[2][1]) for x in flat if x[0] == "seg")
        nv = sum(1 for x in flat if x[0] == "via")
        if v is not t and not v.get("via") and not v.get("new"):
            continue
        layers = _leave_layers(v, sig)
        if v is t and any(len(q["layers"]) <= 1 for q in (v["p"], v["n"])):
            layers = _leave_layers(v, sig)
        # copper that exists already (a fan-out via pair of the board) is an obstacle already; only NEW copper is checked
        if v.get("new") and any(PL.check(items, pcs, net, clr, ctx["skip"])[0] < clr - 1e-6 for net, pcs in v["new"].items()):
            continue
        for layer in layers:
            cands = []
            for d in DIRS:
                align = d[0] * head[0] + d[1] * head[1]
                legs = _legs(v, layer, d, p["reach"], rule)
                nets = {"p": pr[0], "n": pr[1]}
                if any(PL.check(items, [legs[s]], nets[s], clr, ctx["skip"])[0] < clr - 1e-6 for s in ("p", "n")):
                    continue
                cands.append((-align, d, legs))
            cands.sort(key=lambda c: c[0])
            for na, d, legs in cands[:p["dirs"]]:
                foot = flat + [legs["p"], legs["n"]]
                if nbrs:                                       # neighbour pins and rail via sites keep a lane
                    near = [q for q in nbrs if min(_dist_pad(q, x) for x in foot) < 2.0]
                    if any(not _lane_free(items, q, clr, rule["w"], rule["via"], foot, ctx["skip"]) and
                           _lane_free(items, q, clr, rule["w"], rule["via"], [], ctx["skip"]) for q in near):
                        continue
                margin = None
                if budget and tgt:
                    try:
                        pr_ = CH.predict([v["c"], tgt], layer, budget)
                        margin = pr_["length_margin_mm"]
                        margin = None if margin is None else margin - 2 * stub
                    except Exception:                         # noqa: BLE001 - no channel model: no length term
                        margin = None
                    if margin is not None and margin < 0:
                        continue
                cost = stub + ctx["P"]["via_cost"] * 0.1 * nv + 0.6 * (1.0 + na) + (0.0 if layer == sig[0] or layer == sig[-1] else 0.3)
                if margin is not None:
                    cost += max(0.0, 2.0 - margin) * 0.2
                out.append({"pair": pr, "ti": ti, "var": v, "vkey": vkey(v), "layer": layer, "dir": d, "foot": foot, "own": own,
                            "cost": round(cost, 4), "stub": round(stub, 3), "vias": nv, "margin": margin,
                            "sig": (vkey(v), layer, d)})
    out.sort(key=lambda b: b["cost"])
    return out[:MAX_DOMAIN]


def _dist_pad(q, x):
    if x[0] == "seg":
        return G.pt_seg((q["x"], q["y"]), x[2], x[3])
    return math.hypot(q["x"] - x[1], q["y"] - x[2])


# ---------------------------------------------------------------- 2. select

def select(domains, clr, expansions, shortlist, forbidden=frozenset(), access=None):
    """backtracking with forward checking -> (assignments best first [{var: bundle}], expansions used).
    `domains` {var: [bundle]}; `forbidden` = set of frozensets of bundle signatures that must not appear together (probe memory);
    `access(assignment)` False drops a complete assignment."""
    vars_ = sorted(domains, key=lambda v: len(domains[v]))
    sols, used = [], 0
    conf = {}

    def conflict(a, b):
        k = (id(a), id(b))
        r = conf.get(k)
        if r is None:
            r = conf[k] = conf[(id(b), id(a))] = foot_conflict(a["foot"], b["foot"], clr)
        return r

    def rec(i, chosen, doms):
        nonlocal used
        if used >= expansions:
            return
        if i == len(vars_):
            asg = dict(zip(vars_, chosen))
            sigs = frozenset(b["sig"] for b in chosen)
            if any(f <= sigs for f in forbidden) or (access and not access(asg)):
                return
            sols.append((sum(b["cost"] for b in chosen), asg))
            return
        v = vars_[i]
        for b in doms[v]:
            if used >= expansions:
                return
            used += 1
            nd = {u: [c for c in doms[u] if not conflict(b, c)] for u in vars_[i + 1:]}
            if any(not x for x in nd.values()):
                continue                                       # early conflict pruning
            rec(i + 1, chosen + [b], nd)
            if len(sols) >= 40 * shortlist:
                return
    if vars_ and all(domains[v] for v in vars_):
        rec(0, [], {v: list(domains[v]) for v in vars_})
    sols.sort(key=lambda s: s[0])
    out, seen = [], set()
    for _c, asg in sols:
        sg = tuple(sorted(str(b["sig"]) for b in asg.values()))
        if sg in seen:
            continue
        seen.add(sg)
        out.append(asg)
        if len(out) >= shortlist:
            break
    return out, used


# ---------------------------------------------------------------- 3. verify

def verification_set(info, rules, key, live, targets, boxes, reach):
    """targets + every live pair of equal or higher rank (smaller (prio, kind)) whose straight terminal-to-terminal corridor passes
    within `reach` of the neighbourhood boxes [(x0, y0, x1, y1)] -> list of pairs in emission order."""
    rk = max(key[q][:2] for q in targets)
    out = set(targets)
    for q in live:
        if q in out or key[q][:2] > rk or not info[q][2]:
            continue
        for a, b in info[q][2]:
            ca, cb = info[q][0][a]["c"], info[q][0][b]["c"]
            if any(_seg_box(ca, cb, bx, reach) for bx in boxes):
                out.add(q)
                break
    return sorted(out, key=lambda q: key[q])


def _seg_box(a, b, box, reach):
    x0, y0, x1, y1 = box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach
    cx, cy, hw, hh = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    return G.seg_box((a[0] - cx, a[1] - cy), (b[0] - cx, b[1] - cy), hw, hh) <= 0.0


def part_box(B, ref):
    ps = [q for q in B["pads"] if q["ref"] == ref]
    return (min(q["x"] - q["w"] / 2 for q in ps), min(q["y"] - q["h"] / 2 for q in ps),
            max(q["x"] + q["w"] / 2 for q in ps), max(q["y"] + q["h"] / 2 for q in ps))


def judge(res, targets, vset, base, limit):
    """production judgement of a ghost lay: every target laid in full and in tolerance; no other pair worse than its baseline.
    -> (ok, [reasons])"""
    why = []
    for q in vset:
        c = res.get(q)
        ok = bool(c) and (c["full"] or c["e"]["status"] == "kept") and (c["skew"] <= limit(q[0]) + 1e-9)
        b = base.get(q)
        bok = bool(b) and (b["full"] or b["e"]["status"] == "kept") and (b["skew"] <= limit(q[0]) + 1e-9)
        if q in targets and not ok:
            why.append(f"{'/'.join(q)}: not laid in tolerance ({(c or {}).get('e', {}).get('status')})")
        elif q not in targets and bok and not ok:
            why.append(f"{'/'.join(q)}: was laid in tolerance before, not with this escape")
    return not why, why


def blocker_parts(c, pairs_pads):
    """parts named by a failed lay's blockers (`pad REF.NUM (net)` strings) -> set of part refs (geometry owner, no name parsing of nets)."""
    out = set()
    txt = (c or {}).get("e", {}).get("blocked_by", "") or ""
    for tok in txt.replace("|", " ").split():
        if "." in tok and tok.split(".")[0] in pairs_pads:
            out.add(tok.split(".")[0])
    return out


def plan(ctx_of, ghost, info, rules, key, live, cons, B, limit, log=lambda m: None, order=None):
    """-> {"choice", "reserve", "report"}.
    ctx_of() -> fresh lay context; ghost(pairs, escape, reserve) -> {pair: lay result} (production lay on a fresh state, in `pairs` order)."""
    p = params(cons)
    pos = {q: k for k, q in enumerate(order or live)}
    t0 = time.time()
    rep = {"neighbourhoods": [], "expansions": 0, "limit": p["expansions"], "ghost_runs": 0, "memory": {"forbidden": 0, "deprioritised": 0, "recorded": []}}
    out = {"choice": {}, "reserve": {}, "report": rep}
    if not p["on"] or len(live) < 1:
        return out
    comps = components(info, live)
    seeds = [c for c, ts in comps.items() if len({q for q, _ti in ts}) >= 2]      # a part with two or more pairs on it
    ctx0 = ctx_of()
    forbidden, depri = set(), set()
    done_parts = set()
    for seed in sorted(seeds):
        if seed in done_parts or time.time() - t0 > p["budget_s"]:
            continue
        hood = [seed]
        nh = {"parts": list(hood), "grown": [], "candidates": 0, "verdict": "none", "why": []}
        for _grow in range(p["grow"] + 1):
            vars_ = [(pr, ti) for c in hood for pr, ti in comps.get(c, [])]
            targets = sorted({q for q, _ti in vars_}, key=lambda q: key[q])
            doms, clr = {}, 0.0
            for pr, ti in vars_:
                rule = rules[pr]
                clr = max(clr, rule["clr"])
                others = [t["c"] for k, t in enumerate(info[pr][0]) if k != ti]
                ctx0["budget"] = ctx0.get("budget") or {}
                if pr not in ctx0["budget"]:
                    from . import budget as BG
                    try:
                        ctx0["budget"][pr] = BG.pair_limits(cons, pr[0])[0]
                    except Exception:                            # noqa: BLE001 - no channel family: no length term
                        ctx0["budget"][pr] = {}
                doms[(pr, ti)] = bundles(ctx0, pr, ti, rule, others, p)
            if any(not d for d in doms.values()):
                nh["why"].append("a terminal has no legal bundle")
                break
            boxes = [part_box(B, c) for c in hood]
            vset = sorted(verification_set(info, rules, key, live, targets, boxes, p["verify_reach"]), key=lambda q: pos.get(q, 1e9))
            base = ghost(vset, {}, {})
            rep["ghost_runs"] += 1
            okb, whyb = judge(base, set(targets), vset, base, limit)
            if okb:
                nh["verdict"] = "baseline already laid"
                break
            cands, used = select(doms, clr, p["expansions"] - rep["expansions"], p["shortlist"], frozenset(forbidden))
            rep["expansions"] += used
            nh["expansions"] = used
            blockers, chosen = set(), None
            for asg in cands:
                if time.time() - t0 > p["budget_s"]:
                    nh["why"].append("escape budget spent")
                    break
                nh["candidates"] += 1
                esc = {tkey(pr, info[pr][0][ti]): b["var"] for (pr, ti), b in asg.items()}
                rsv = {}
                for (pr, ti), b in asg.items():
                    rsv[pr] = rsv.get(pr, []) + [x for pcs in b["own"].values() for x in pcs] + b["foot"]
                res = ghost(vset, esc, rsv)
                rep["ghost_runs"] += 1
                ok, why = judge(res, set(targets), vset, base, limit)
                sig = frozenset(b["sig"] for b in asg.values())
                if ok:
                    chosen = (esc, rsv, asg)
                    break
                nh["why"] += why[:2]
                geom = [c for c in res.values() if c["e"].get("status") in ("not-laid", "partial")]
                if any("search budget" in (c["e"].get("why") or "") for c in geom):
                    depri.add(sig)
                    rep["memory"]["deprioritised"] += 1
                else:
                    forbidden.add(sig)
                    rep["memory"]["forbidden"] += 1
                for c in res.values():
                    blockers |= blocker_parts(c, set(comps))
            if chosen:
                esc, rsv, asg = chosen
                out["choice"].update(esc)
                for pr, pcs in rsv.items():
                    out["reserve"][pr] = out["reserve"].get(pr, []) + pcs
                nh["verdict"] = "chosen"
                nh["exits"] = {"/".join(pr) + f"@{ti}": {"variant": b["vkey"][0], "layer": b["layer"], "dir": b["dir"]} for (pr, ti), b in asg.items()}
                done_parts |= set(hood)
                break
            grow_to = sorted(c for c in blockers if c not in hood and c in comps)
            if not grow_to:
                nh["verdict"] = "no candidate verified"
                break
            hood.append(grow_to[0])
            nh["grown"].append(grow_to[0])
            nh["parts"] = list(hood)
        rep["neighbourhoods"].append(nh)
        log(f"pairs: escape {nh['parts']} {nh['verdict']} ({nh.get('candidates', 0)} candidates, {nh.get('expansions', 0)} expansions)")
    rep["secs"] = round(time.time() - t0, 1)
    return out
