"""Congestion negotiation for the `pairs` stage (PathFinder style).

Every pair section is first searched ALONE against the fixed obstacles (in parallel processes).  Paths that overlap are
then negotiated: the cells of an over-used area get a present cost (growing per pass) and a history cost, and only the
pairs in a conflict are searched again - the most constrained pair of each conflict (fewest gates) keeps its path, the
others move.  The result is a corridor per pair; pairs.py then emits the pairs one by one, each seeing the corridors of
the pairs not yet emitted as obstacles, so no pair takes another's way.  Nothing here knows a board."""
import multiprocessing as mp

import numpy as np

from . import pocket as PK

_G = {}                                                    # shared with forked workers


def mask_idx(F, fp, grow, core=False):
    """{layer: flat cell indices} within `grow` of the footprint pieces [("seg", layer, a, b, width) | ("via", x, y, d)];
    core=True ignores the track width (centre line only)."""
    out = {}
    for p in fp:
        if p[0] == "seg":
            a, b = p[2], p[3]
            r = (0.0 if core else p[4] / 2) + grow
            sl = F._sl(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]), r)
            if not sl:
                continue
            m, layers = PK.seg_sdf(a, b, 0.0, F.X[sl], F.Y[sl]) <= r, [p[1]]
        else:
            r = p[3] / 2 + grow
            sl = F._sl(p[1], p[2], p[1], p[2], r)
            if not sl:
                continue
            m, layers = np.hypot(F.X[sl] - p[1], F.Y[sl] - p[2]) <= r, list(F.copper)
        jj, ii = np.nonzero(m)
        idx = (jj + sl[0].start) * F.nx + (ii + sl[1].start)
        for l in layers:
            out.setdefault(l, []).append(idx)
    return {l: np.unique(np.concatenate(v)) for l, v in out.items()}


def _call(k):
    g = _G
    cost = None
    if g.get("occ") is not None:
        pr = g["pair_of"][k]
        cost = []
        for l in g["layers"]:
            c = g["occ"][l].astype(np.int32).ravel()
            own = g["pmask"].get(pr, {}).get(l)
            if own is not None:
                c[own] -= 1
            cost.append(np.clip(g["pres"] * c + g["hist"][l].ravel(), 0, 255).astype(np.uint8).tobytes())
    return k, g["fn"](k, cost, g.get("bx", 1))


def run_parallel(fn, ids, workers, **shared):
    _G.clear()
    _G.update(fn=fn, **shared)
    if workers <= 1 or len(ids) < 3:
        return dict(_call(k) for k in ids)
    with mp.get_context("fork").Pool(min(workers, len(ids))) as pool:
        return dict(pool.map(_call, ids, chunksize=1))


def negotiate(F, layers, pair_of, search, grow, workers=4, passes=8, pres=30, hstep=6, log=lambda m: None):
    """pair_of {section id: pair}; search(id, cost bytes per layer | None, budget factor) -> {ok, fp, gates, reason ...}.
    -> (results {id: result}, unresolved pairs, passes used)."""
    ids = list(pair_of)
    res = run_parallel(search, ids, workers)
    again = [k for k in ids if not res[k]["ok"] and "search budget" in res[k].get("reason", "")]
    if again:                                               # failed alone on the budget: one retry at four times the budget
        r2 = run_parallel(search, again, workers, bx=4)
        res.update({k: v for k, v in r2.items() if v["ok"]})
    shape = (F.ny, F.nx)
    hist = {l: np.zeros(shape, np.int16) for l in layers}
    used, unresolved = 0, set()
    stuck, prefer = {}, set()                              # a pair that moved and is still in the way: next time its partner moves

    def masks():
        pm, core = {}, {}
        for k in ids:
            if not res[k]["ok"]:
                continue
            pr = pair_of[k]
            for store, m in ((pm, mask_idx(F, res[k]["fp"], grow)), (core, mask_idx(F, res[k]["fp"], F.step * 0.75, core=True))):
                e = store.setdefault(pr, {})
                for l, idx in m.items():
                    e[l] = np.union1d(e[l], idx) if l in e else idx
        return pm, core

    for it in range(passes + 1):
        pm, core = masks()
        occ = {l: np.zeros(shape, np.int16) for l in layers}
        for pr, m in pm.items():
            for l, idx in m.items():
                if l in occ:
                    occ[l].ravel()[idx] += 1
        hot = [pr for pr, m in core.items() if any(l in occ and (occ[l].ravel()[idx] > 1).any() for l, idx in m.items())]
        if not hot:
            unresolved = set()
            break
        graph = {a: [b for b in hot if b != a and any(l in pm[b] and np.intersect1d(idx, pm[b][l], assume_unique=True).size
                                                      for l, idx in core[a].items())] for a in hot}
        gates = {pr: sum(res[k].get("gates", 0) for k in ids if pair_of[k] == pr and res[k]["ok"]) for pr in hot}
        keep = []
        for pr in list(stuck):
            if pr not in hot:
                stuck.pop(pr)
        for pr in sorted(hot, key=lambda pr: (pr not in prefer, gates[pr], pr)):      # the most constrained pair of a conflict keeps its path
            if not any(q in keep for q in graph[pr]):
                keep.append(pr)
        move = [pr for pr in hot if pr not in keep and graph[pr]]
        unresolved = set(move)
        for pr in move:
            stuck[pr] = stuck.get(pr, 0) + 1
            if stuck[pr] == 2:
                prefer.add(pr)                               # moved twice and still in conflict: it keeps, the others move
        move = [pr for pr in move if stuck[pr] <= 4]
        if it == passes or not move:
            break
        for l in layers:
            hist[l] += (occ[l] > 1).astype(np.int16) * hstep
        todo = [k for k in ids if pair_of[k] in move and res[k]["ok"]]
        r2 = run_parallel(search, todo, workers, occ=occ, hist=hist, pmask=pm, pair_of=pair_of, layers=layers, pres=pres * (it + 1), bx=0.5)
        res.update({k: v for k, v in r2.items() if v["ok"]})
        used = it + 1
        log(f"pairs: negotiation pass {used}: {len(hot)} pair(s) in conflict, {len(move)} moved")
    return res, unresolved, used
