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
import hashlib
import json
import math
import os
import multiprocessing as mp
import time

from . import events as EV
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
            "verify_reach": float(e.get("escape_verify_reach_mm", 8.0)), "grow_reach": float(e.get("escape_grow_reach_mm", 15.0)),
            "workers": max(1, int(e.get("escape_workers", min(4, os.cpu_count() or 1)))),
            "watch": [str(x) for x in (e.get("escape_watch") or [])], "cross": e.get("escape_cross", True) is not False}


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
    return [l for l in sig if l in v["p"]["layers"] and l in v["n"]["layers"]]


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
    """legal exit bundles of terminal `ti` of pair `pr` -> [bundle], cheapest first.  `others` = [pair terminal centres] (the heading).
    A dense break-out (fine-pitch part, fan-out vias already placed) has no clear straight leg of the full reach: the reach is shortened
    (1/2, then 1/3) before the terminal is called blocked."""
    for k in (1.0, 0.5, 1 / 3):
        out = _bundles_at(ctx, pr, ti, rule, others, p if k == 1.0 else dict(p, reach=p["reach"] * k))
        if out:
            return out
    return []


def _bundles_at(ctx, pr, ti, rule, others, p):
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
    nets = {"p": pr[0], "n": pr[1]}
    for v in _variants_of(ctx, t, rule, pr, p["variants"]):
        if v is not t and not v.get("via") and not v.get("new"):
            continue
        own = _pieces_of(v)
        flat = [x for pcs in own.values() for x in pcs]
        stub = sum(math.hypot(x[3][0] - x[2][0], x[3][1] - x[2][1]) for x in flat if x[0] == "seg")
        nv = sum(1 for x in flat if x[0] == "via")
        layers = _leave_layers(v, sig)
        # copper that exists already (a fan-out via pair of the board) is an obstacle already; only NEW copper is checked
        if v.get("new") and any(PL.check(items, pcs, net, clr, ctx["skip"])[0] < clr - 1e-6 for net, pcs in v["new"].items()):
            continue
        for layer in layers:
            cands = []
            for d in DIRS:
                align = d[0] * head[0] + d[1] * head[1]
                legs = _legs(v, layer, d, p["reach"], rule)
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
        sg = _material(asg)
        if sg in seen:
            continue
        seen.add(sg)
        out.append(asg)
        if len(out) >= shortlist:
            break
    return out, used


# ---------------------------------------------------------------- 3. verify

def verification_set(info, rules, key, live, targets, boxes, reach, watch=()):
    """targets + every live pair of equal or higher rank (smaller (prio, kind)) whose straight terminal-to-terminal corridor passes
    within `reach` of the neighbourhood boxes [(x0, y0, x1, y1)] -> list of pairs in emission order."""
    rk = max(key[q][:2] for q in targets)
    out = set(targets) | {q for q in watch if q in key}               # observed victims stay in, wherever their corridor runs
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


def _in_tol(c, lim):
    """a lay result laid in full (or kept) with its skew inside `lim`."""
    return bool(c) and (c["full"] or c["e"]["status"] == "kept") and c["skew"] <= lim + 1e-9


def _material(asg):
    """what makes two assignments materially different: variant + layer per terminal (a 45-degree heading change is a duplicate)."""
    return tuple(sorted(str((b["vkey"], b["layer"])) for b in asg.values()))


def _escape_of(asg, info):
    """an assignment -> (escape {terminal key: variant}, reserve {pair: pieces})."""
    esc = {tkey(pr, info[pr][0][ti]): b["var"] for (pr, ti), b in asg.items()}
    rsv = {}
    for (pr, ti), b in asg.items():
        rsv[pr] = rsv.get(pr, []) + [x for pcs in b["own"].values() for x in pcs] + b["foot"]
    return esc, rsv


def judge(res, targets, vset, base, limit):
    """production judgement of a ghost lay: every target laid in full and in tolerance; no other pair worse than its baseline.
    -> (ok, [reasons])"""
    why = []
    for q in vset:
        c = res.get(q)
        ok = _in_tol(c, limit(q[0]))
        bok = _in_tol(base.get(q), limit(q[0]))
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


def cache_dir(cons):
    d = ((cons or {}).get("pairlay") or {}).get("escape_cache_dir")
    return d or os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "fluxplace", "pair-escape")


def fingerprint(B, cons, live, focus=()):
    """sha of what the plan depends on: the placement (pads with net, position, size, layers), the board's rule areas and outline, the
    pairlay / pairs / channel / plan constraints and the pairs to lay.  Tracks and vias are NOT in it: the fan-out stage adds and strips
    copper, the exits planned before it are the ones the pairs stage reuses."""
    h = hashlib.sha256()
    pads = sorted((q["ref"], str(q["num"]), q["net"], round(q["x"], 3), round(q["y"], 3), round(q["w"], 3), round(q["h"], 3), sorted(q["layers"])) for q in B.get("pads", []))
    h.update(json.dumps(pads, sort_keys=True, default=str).encode())
    h.update(json.dumps([sorted(map(str, B.get("edge", []))), [(z.get("name"), z["pts"]) for z in B.get("zones", []) if z.get("keepout")]], default=str).encode())
    sub = {k: (cons or {}).get(k) for k in ("pairlay", "pairs", "channel", "plan", "fab", "netclass")}
    h.update(json.dumps(sub, sort_keys=True, default=str).encode())
    h.update(json.dumps(sorted("/".join(q) for q in live)).encode())
    h.update(json.dumps(sorted("/".join(q) for q in focus)).encode())
    return h.hexdigest()


def _save(cons, fp, out, info):
    ck = lambda k: "|".join([k[0][0], k[0][1], str(k[1]), str(k[2]), str(k[3])])
    doc = {"choice": {ck(k): list(vkey(v)) for k, v in out["choice"].items()},
           "reserve": {"|".join(pr): [list(x) if isinstance(x, tuple) else x for x in pcs] for pr, pcs in out["reserve"].items()}, "report": out["report"]}
    d = cache_dir(cons)
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f".{fp}.{os.getpid()}")
        json.dump(doc, open(tmp, "w"), default=str)
        os.replace(tmp, os.path.join(d, fp + ".json"))
    except OSError:
        pass


def _load(cons, fp, ctx, info, rules, p):
    """the cached plan with its variants rebuilt from the board (matched by terminal and variant key); None when a variant is gone."""
    try:
        doc = json.load(open(os.path.join(cache_dir(cons), fp + ".json")))
    except (OSError, ValueError):
        return None
    byk = {}
    for pr, (terms, _l, _s, _sp) in info.items():
        for t in terms:
            byk["|".join([pr[0], pr[1], t["name"], str(round(t["c"][0], 3)), str(round(t["c"][1], 3))])] = (pr, t)
    choice = {}
    for k, vk in doc["choice"].items():
        if k not in byk:
            return None
        pr, t = byk[k]
        v = next((x for x in _variants_of(ctx, t, rules[pr], pr, p["variants"]) if vkey(x) == tuple(vk)), None)
        if v is None:
            return None
        choice[tkey(pr, t)] = v
    reserve = {tuple(k.split("|")): [tuple(x) if isinstance(x, list) else x for x in pcs] for k, pcs in doc["reserve"].items()}
    rep = dict(doc["report"], cache="hit")
    return {"choice": choice, "reserve": reserve, "report": rep}


_G = {}


def _gcall(i):
    return _G["fn"](*_G["args"][i])


def pmap(fn, arglist, workers):
    """[fn(*a) for a in arglist] in input order, the calls spread over forked workers (fn and its closure are inherited, never pickled;
    only the arguments' indices and the results cross the pipe).  One worker or one call: in this process."""
    if workers <= 1 or len(arglist) < 2:
        return [fn(*a) for a in arglist]
    _G.clear()
    _G.update(fn=fn, args=arglist)
    try:
        with mp.get_context("fork").Pool(min(workers, len(arglist))) as pool:
            return pool.map(_gcall, range(len(arglist)), chunksize=1)
    finally:
        _G.clear()


def cross_scores(ghost, cands, info, vset, targets, base, limit, hood, rep, workers=1):
    """diagnostic (outside review 2026-11-26): two materially different candidate exits crossed with the two emission orders of the target
    pairs, each pair scored on its own (laid in full, skew inside the guard limit).  Never changes the selection or the pairs order."""
    picks, seen = [], set()
    for asg in cands:
        m = _material(asg)
        if m not in seen:
            seen.add(m)
            picks.append(asg)
        if len(picks) == 2:
            break
    jobs = []
    for ci, asg in enumerate(picks):
        esc, rsv = _escape_of(asg, info)
        tl = [q for q in vset if q in targets]
        for oname, order in (("emission", list(vset)), ("targets-reversed", [q for q in vset if q not in targets] + tl[::-1])):
            jobs.append((ci, asg, oname, order, esc, rsv))
    results = pmap(ghost, [(order, esc, rsv) for _ci, _asg, _o, order, esc, rsv in jobs], workers)     # independent probes: any order, merged in this one
    rows = []
    for (ci, asg, oname, _order, _esc, _rsv), res in zip(jobs, results):
        rep["ghost_runs"] += 1
        sc = {"/".join(q): _in_tol(res.get(q), limit(q[0])) for q in vset}
        rows.append({"hood": hood, "candidate": ci, "order": oname, "exits": {"/".join(pr) + f"@{ti}": [b["vkey"][0], b["layer"]] for (pr, ti), b in asg.items()}, "scores": sc})
    return rows


def plan(ctx_of, ghost, info, rules, key, live, cons, B, limit, log=lambda m: None, order=None, focus=()):
    """-> {"choice", "reserve", "report"}.
    ctx_of() -> fresh lay context; ghost(pairs, escape, reserve) -> {pair: lay result} (production lay on a fresh state, in `pairs` order)."""
    p = params(cons)
    pos = {q: k for k, q in enumerate(order or live)}
    t0 = time.time()
    rep = {"neighbourhoods": [], "expansions": 0, "limit": p["expansions"], "ghost_runs": 0, "memory": {"forbidden": 0, "deprioritised": 0, "recorded": []}}
    out = {"choice": {}, "reserve": {}, "report": rep}
    if not p["on"] or not live:
        return out
    comps = components(info, live)
    seeds = [c for c, ts in comps.items() if len({q for q, _ti in ts}) >= 2 and (not focus or any(q in focus for q, _ti in ts))]      # a part with two or more pairs on it (one of them a failed pair, in a repair)
    ctx0 = ctx_of()
    fp = fingerprint(B, cons, live, focus)
    hit = _load(cons, fp, ctx0, info, rules, p)
    if hit is not None:
        return hit
    from . import budget as BG
    ctx0["budget"] = ctx0.get("budget") or {}
    forbidden = set()                                     # a timeout / budget stop is only counted (deprioritised), never forbidden
    watch = {q for q in live if any(n.startswith(w) for n in q for w in p["watch"])}          # [pairlay] escape_watch = net prefixes (board intent in the constraints)
    rep["watch"] = sorted("/".join(q) for q in watch)
    rep["cross"] = []
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
                if pr not in ctx0["budget"]:
                    try:
                        ctx0["budget"][pr] = BG.pair_limits(cons, pr[0])[0]
                    except Exception:                            # noqa: BLE001 - no channel family: no length term
                        ctx0["budget"][pr] = {}
                doms[(pr, ti)] = bundles(ctx0, pr, ti, rule, others, p)
            blocked = sorted("/".join(pr) + f"@{ti}" for (pr, ti), d in doms.items() if not d)
            if blocked:                                       # a terminal with no legal bundle keeps its generic exit; the others are still planned
                nh["why"].append("no legal bundle (generic exit): " + ", ".join(blocked))
                doms = {k: d for k, d in doms.items() if d}
                if not doms:
                    break
            boxes = [part_box(B, c) for c in hood]
            vset = sorted(verification_set(info, rules, key, live, targets, boxes, p["verify_reach"], watch), key=lambda q: pos.get(q, 1e9))
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
            W = p["workers"]
            for b0 in range(0, len(cands), W):                  # probes of one batch run side by side; judged in shortlist order, the first good one wins
                if chosen or time.time() - t0 > p["budget_s"]:
                    if not chosen:
                        nh["why"].append("escape budget spent")
                    break
                batch = cands[b0:b0 + W]
                escs = [_escape_of(asg, info) for asg in batch]
                got = pmap(ghost, [(vset, esc, rsv) for esc, rsv in escs], W)
                for asg, (esc, rsv), res in zip(batch, escs, got):
                    if time.time() - t0 > p["budget_s"]:
                        nh["why"].append("escape budget spent")
                        break
                    nh["candidates"] += 1
                    rep["ghost_runs"] += 1
                    EV.iteration("escape", rep["ghost_runs"], None, {"hood": len(hood), "candidates": nh["candidates"], "expansions": rep["expansions"],
                                                                      "secs": round(time.time() - t0, 1)}, note="/".join(hood))
                    ok, why = judge(res, set(targets), vset, base, limit)
                    sig = frozenset(b["sig"] for b in asg.values())
                    if ok:
                        chosen = (esc, rsv, asg)
                        break
                    nh["why"] += why[:2]
                    for q in vset:                                    # a pair laid before and not now is an observed victim: it stays in every later verification
                        if q not in targets and base.get(q) and (base[q]["full"] or base[q]["e"]["status"] == "kept") and not (res.get(q) or {}).get("full"):
                            if q not in watch:
                                rep.setdefault("victims", []).append("/".join(q))
                            watch.add(q)
                    geom = [c for c in res.values() if c["e"].get("status") in ("not-laid", "partial")]
                    if any("search budget" in (c["e"].get("why") or "") for c in geom):
                        rep["memory"]["deprioritised"] += 1
                    else:
                        forbidden.add(sig)
                        rep["memory"]["forbidden"] += 1
                    for c in res.values():
                        blockers |= blocker_parts(c, set(comps))
            if p["cross"] and cands and not any(c.get("hood") == tuple(hood) for c in rep["cross"]):
                rep["cross"] += cross_scores(ghost, cands, info, vset, targets, base, limit, tuple(hood), rep, p["workers"])
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
            if not grow_to:                                        # a blocked channel / via bank names no part: the nearest part with pairs on it
                gb = [(math.hypot((a[0] + a[2]) / 2 - (b[0] + b[2]) / 2, (a[1] + a[3]) / 2 - (b[1] + b[3]) / 2), c) for c in comps if c not in hood
                      for a in [part_box(B, c)] for b in boxes[:1]]
                near = sorted(gb)[:1]
                grow_to = [near[0][1]] if near and near[0][0] <= p["grow_reach"] else []
            if not grow_to:
                nh["verdict"] = "no candidate verified"
                break
            hood.append(grow_to[0])
            nh["grown"].append(grow_to[0])
            nh["parts"] = list(hood)
        rep["neighbourhoods"].append(nh)
        EV.iteration("escape-hood", len(rep["neighbourhoods"]), len(seeds), {"ghost_runs": rep["ghost_runs"], "secs": round(time.time() - t0, 1)},
                     note=f"{'/'.join(nh['parts'])}: {nh['verdict']}")
        log(f"pairs: escape {nh['parts']} {nh['verdict']} ({nh.get('candidates', 0)} candidates, {nh.get('expansions', 0)} expansions)")
    rep["secs"] = round(time.time() - t0, 1)
    if time.time() - t0 <= p["budget_s"]:
        _save(cons, fp, out, info)
    return out
