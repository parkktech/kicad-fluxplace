"""Coupled-path construction for one differential-pair section (used by pairs.py).

  Grid      whole-board clearance field (pocket.Field at `step`): per signal layer the cells where the pair's CENTRE LINE may
            run (envelope 2w + gap, plus clearance), and where a matched via pair fits on every copper layer
  astar     centre-line search between two terminals: 8 directions, 45 degree turns only, at least K cells between turns,
            layer change only as a via pair across the direction of travel; cost = length + turns + vias
  legs      both legs as the +-(w + gap)/2 mitred parallels of the centre line, fanned to the via pair at a layer change
  breakout  short uncoupled dog-leg from each pad to its leg end, checked exactly (pairgeo.Items)
  bump      skew compensation: a 45 degree trapezoid on the short leg, away from its partner

Nothing here knows a board: sizes come from the caller's rules, obstacles from the board file."""
import heapq
import math

import numpy as np

from . import pairgeo as G
from . import pocket as PK
from . import routeconn as RC

DIRS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))      # (di, dj)
DLEN = tuple(math.hypot(a, b) for a, b in DIRS)
DVEC = tuple((a / l, b / l) for (a, b), l in zip(DIRS, DLEN))
T22 = math.tan(math.radians(22.5))
SIN_MIN = 0.38                     # a gate never leaves a terminal along its pad axis


class Grid:
    def __init__(self, B, sig, clr, edge_clr, step, pair_nets):
        x0, y0, x1, y1 = B["bbox"]
        half = max(x1 - x0, y1 - y0) / 2 + 1.0
        ign = {("seg", k) for k, s in enumerate(B["segs"]) if s["net"] in pair_nets}
        ign |= {("via", k) for k, v in enumerate(B["vias"]) if v["net"] in pair_nets}
        self.F = PK.Field(B, ((x0 + x1) / 2, (y0 + y1) / 2), half, step, ignore=ign, clearance=clr, edge_clearance=edge_clr)
        self.B, self.sig, self.clr, self.step = B, [l for l in B["copper"] if l in sig], clr, step
        self.nx, self.ny = self.F.nx, self.F.ny
        self.old = {}
        for kind, src in (("seg", B["segs"]), ("via", B["vias"])):
            for o in src:
                if o["net"] in pair_nets:
                    self.old.setdefault(o["net"], []).append((kind, o))

    def cell(self, x, y):
        return self.F.cell(x, y)

    def xy(self, j, i):
        return self.F.xy(j, i)

    def view(self):
        """per-pair distance arrays (a copy of the base field).  The fan-out copper of pair nets is not in it: a laid pair
        replaces its own, and what an unlaid pair keeps is decided at the end (pairs.plan: only what the new copper does not foul)."""
        return {l: a.copy() for l, a in self.F.dist.items()}, {l: a.copy() for l, a in self.F.owner.items()}

    def drop_old(self, nets):
        for n in nets:
            self.old.pop(n, None)

    def claim(self, pieces, net):
        """laid copper becomes an obstacle for every later pair."""
        for p in pieces:
            if p[0] == "seg":
                self.F.block_seg(p[2], p[3], p[4], p[1], net)
            else:
                self.F.block_via(p[1], p[2], p[3], net)

    def label(self, idx):
        kind, key, net, lab = self.F.items[idx]
        if kind == "pad":
            return f"pad {lab}" + (f" ({net})" if net else "")
        return lab if kind in ("edge", "keepout") else f"{'track' if kind == 'seg' else kind} {net}" + (f" ({lab})" if lab else "")

    def blockers(self, D, O, layers, x, y, radius, thr, top=5):
        """what closes the cells around (x, y): owner labels by blocked-cell count."""
        j, i = self.cell(x, y)
        k = int(radius / self.step) + 1
        sl = (slice(max(0, j - k), j + k + 1), slice(max(0, i - k), i + k + 1))
        cnt = {}
        for l in layers:
            o = O[l][sl][D[l][sl] < thr]
            for v, c in zip(*np.unique(o[o >= 0], return_counts=True)):
                cnt[int(v)] = cnt.get(int(v), 0) + int(c)
        return [self.label(v) for v, _c in sorted(cnt.items(), key=lambda kv: -kv[1])[:top]]


def via_masks(viaok, kv_s, kv_d):
    """{axis 0..3: mask} cells where a via pair fits at +-k cells ACROSS travel direction d (axis = d % 4)."""
    out = {}
    for ax in range(4):
        di, dj = DIRS[(ax + 2) % 8]
        k = kv_s if (di == 0 or dj == 0) else kv_d
        out[ax] = PK._shift(viaok, dj * k, di * k, False) & PK._shift(viaok, -dj * k, -di * k, False)
    return out


def coarse_dist(free, viaok, goals, f, step, via_cost):
    """distance (mm) to the goal cells on a grid f times coarser (a coarse cell is open when any of its cells is):
    free [nl bool arrays], goals [(li, j, i)] -> float32 [nl, cj, ci].  The search heuristic: it knows the way round walls."""
    nl = len(free)
    cj, ci = free[0].shape[0] // f, free[0].shape[1] // f
    C = np.stack([a[:cj * f, :ci * f].reshape(cj, f, ci, f).any(axis=(1, 3)) for a in free])
    Vc = viaok[:cj * f, :ci * f].reshape(cj, f, ci, f).any(axis=(1, 3)) if viaok is not None and nl > 1 else None
    d = np.full(C.shape, PK.INF, np.float32)
    for li, j, i in goals:
        if j // f < cj and i // f < ci:
            d[li, j // f, i // f] = 0.0
    w1, w2 = np.float32(step * f), np.float32(step * f * math.sqrt(2))
    dirs = [(0, 1, w1), (0, -1, w1), (1, 0, w1), (-1, 0, w1), (1, 1, w2), (1, -1, w2), (-1, 1, w2), (-1, -1, w2)]
    for _ in range(4 * max(cj, ci)):
        nd = d.copy()
        for l in range(nl):
            for dy, dx, w in dirs:
                np.minimum(nd[l], PK._shift(d[l], dy, dx, PK.INF) + w, out=nd[l])
        if Vc is not None:
            m = d.min(axis=0) + np.float32(via_cost)
            for l in range(nl):
                np.minimum(nd[l], np.where(Vc, m, PK.INF), out=nd[l])
        nd[~C] = PK.INF
        for li, j, i in goals:
            if j // f < cj and i // f < ci:
                nd[li, j // f, i // f] = 0.0
        if np.array_equal(nd, d):
            break
        d = nd
    return d


def astar(nx, ny, layers, allowed, vmask, seeds, goals, H, f, K, step, turn_cost, via_cost, budget):
    """seeds [(cost, li, j, i, d, pol)], goals {cell * nl + li: {d * 2 + pol: cost}}, H {pol: coarse_dist} -> (states | None, info).
    Moves: one cell straight; a 45 degree turn followed by K cells straight; a via pair followed by K cells straight; the
    first move from a gate is K cells straight.  A state is (li, j, i, d); the path lists them from seed to goal."""
    nl = len(layers)
    A = [allowed[l].tobytes() for l in layers]
    V = {ax: m.tobytes() for ax, m in vmask.items()} if vmask and nl > 1 else None
    w = 1.2
    slack = 2.0 * f * step
    Hl = {pol: [[row.tolist() for row in a] for a in arr] for pol, arr in H.items()}
    cjm, cim = (len(next(iter(Hl.values()))[0]) - 1, len(next(iter(Hl.values()))[0][0]) - 1) if Hl else (0, 0)
    BIG = 1e8

    def h(j, i, li, pol):
        v = Hl[pol][li][min(j // f, cjm)][min(i // f, cim)]
        return BIG if v > BIG else (v - slack if v > slack else 0.0)

    def run(li, j, i, d, n):
        di, dj = DIRS[d]
        a = A[li]
        for _k in range(n):
            i += di
            j += dj
            if i < 0 or j < 0 or i >= nx or j >= ny or not a[j * nx + i]:
                return None
        return j, i
    Gc, par, heap = {}, {}, []
    for c, li, j, i, d, pol in seeds:
        key = ((((j * nx + i) * nl + li) * 8 + d) * 2 + 1) * 2 + pol
        hh = h(j, i, li, pol)
        if hh < BIG and c < Gc.get(key, 1e18):
            Gc[key] = c
            heapq.heappush(heap, (c + w * hh, -c, key))
    pops, near = 0, (1e18, None)

    def push(g, li, j, i, d, pol, parent, via):
        hh = h(j, i, li, pol)
        if hh >= BIG:
            return
        nk = ((((j * nx + i) * nl + li) * 8 + d) * 2) * 2 + pol
        if g < Gc.get(nk, 1e18):
            Gc[nk] = g
            par[nk] = (parent, via)
            heapq.heappush(heap, (g + w * hh, -g, nk))
    best, seen = (1e18, None), None
    while heap:
        _f, g, key = heapq.heappop(heap)             # ties: the deeper state first (heap holds -g)
        g = -g
        if g > Gc.get(key, 1e18):
            continue
        pol = key & 1
        fresh = (key >> 1) & 1
        cl, d = divmod(key >> 2, 8)
        cell, li = divmod(cl, nl)
        j, i = divmod(cell, nx)
        gg = goals.get(cl)
        if gg is not None and (d * 2 + pol) in gg and g + gg[d * 2 + pol] < best[0]:
            best = (g + gg[d * 2 + pol], key)                    # the gate nearest the pads wins, not the first one reached
            if seen is None:
                seen = pops
        if best[1] is not None and (_f >= best[0] or pops > budget or pops > 1.5 * seen + 5000):
            return _finish(best, par, nl, nx, pops)
        pops += 1
        if pops > budget:
            return None, {"reason": "search budget", "near": near[1], "pops": pops}
        hh = h(j, i, li, pol)
        if hh < near[0]:
            near = (hh, (li, j, i))
        if fresh:
            e = run(li, j, i, d, K)
            if e:
                push(g + K * DLEN[d] * step, li, e[0], e[1], d, pol, key, False)
            continue
        e = run(li, j, i, d, 1)
        if e:
            push(g + DLEN[d] * step, li, e[0], e[1], d, pol, key, False)
        for nd in ((d + 1) & 7, (d - 1) & 7):
            e = run(li, j, i, nd, K)
            if e:
                push(g + K * DLEN[nd] * step + turn_cost, li, e[0], e[1], nd, pol, key, False)
        if V is not None and V[d % 4][cell]:
            for l2 in range(nl):
                if l2 != li and A[l2][cell]:
                    e = run(l2, j, i, d, K)
                    if e:
                        push(g + via_cost + K * DLEN[d] * step, l2, e[0], e[1], d, pol, key, True)
    if best[1] is not None:
        return _finish(best, par, nl, nx, pops)
    return None, {"reason": "no path", "near": near[1], "pops": pops}


def _finish(best, par, nl, nx, pops):
    out = []
    k = best[1]
    pol = k & 1
    while k is not None:
        c2, d2 = divmod(k >> 2, 8)
        ce, l2 = divmod(c2, nl)
        out.append((l2, ce // nx, ce % nx, d2))
        k, via = par.get(k, (None, False))
        if via:
            c3 = (k >> 2) // 8 // nl
            out.append((l2, c3 // nx, c3 % nx, d2))
    return out[::-1], {"pops": pops, "pol": 1 if pol else -1, "cost": best[0]}


def runs_of(states, layers, xy):
    """states -> [{layer, pts, t0, t1}] per layer run (centre-line corner points) and the via points between runs."""
    runs, vias = [], []
    cur = None
    for k, (li, j, i, d) in enumerate(states):
        p = xy(j, i)
        if cur is None:
            cur = {"layer": layers[li], "pts": [p], "t0": DVEC[d], "t1": DVEC[d], "d": d}
            continue
        pl, pj, pi, pd = states[k - 1]
        if li != pl:                                    # via pair at this cell
            runs.append(cur)
            vias.append({"at": p, "t": DVEC[d], "d": d})
            cur = {"layer": layers[li], "pts": [p], "t0": DVEC[d], "t1": DVEC[d], "d": d}
            continue
        if d != cur["d"]:                               # the turn sits at the previous cell
            cur["d"] = d
        elif len(cur["pts"]) > 1:
            cur["pts"].pop()
        cur["pts"].append(p)
        cur["t1"] = DVEC[d]
    runs.append(cur)
    return runs, vias


def legs(runs, vias, dist, hv, w, via_d, sign):
    """one leg (sign +1 = left of travel) -> pieces [("seg", layer, a, b, w) | ("via", x, y, d)], start point, end point."""
    out, pts_prev = [], None
    first = last = None
    for k, r in enumerate(runs):
        pts = G.offset(r["pts"], sign * dist, r["t0"])
        if k > 0:                                       # leaves a via: fan back in to the coupled offset
            v = vias[k - 1]
            n, t = G.left(v["t"]), v["t"]
            a = hv[v["d"]] - dist
            vp = (v["at"][0] + n[0] * sign * hv[v["d"]], v["at"][1] + n[1] * sign * hv[v["d"]])
            pts = [vp, (v["at"][0] + t[0] * a + n[0] * sign * dist, v["at"][1] + t[1] * a + n[1] * sign * dist)] + pts[1:]
        if k < len(runs) - 1:                           # enters a via: fan out
            v = vias[k]
            n, t = G.left(v["t"]), v["t"]
            a = hv[v["d"]] - dist
            vp = (v["at"][0] + n[0] * sign * hv[v["d"]], v["at"][1] + n[1] * sign * hv[v["d"]])
            pts = pts[:-1] + [(v["at"][0] - t[0] * a + n[0] * sign * dist, v["at"][1] - t[1] * a + n[1] * sign * dist), vp]
        pts = G.clean(pts)
        if first is None:
            first = pts[0]
        last = pts[-1]
        for a_, b_ in zip(pts, pts[1:]):
            out.append(("seg", r["layer"], a_, b_, w))
        if k < len(runs) - 1:
            out.append(("via", pts[-1][0], pts[-1][1], via_d))
    return out, first, last


def piece_len(pieces):
    return sum(math.hypot(p[3][0] - p[2][0], p[3][1] - p[2][1]) for p in pieces if p[0] == "seg")


def check(items, pieces, net, clr, skip_old):
    """worst exact gap of the pieces to foreign copper -> (gap, item)."""
    worst, who = 1e9, None
    for p in pieces:
        g, it = items.gap_seg(p[2], p[3], p[4], p[1], net, clr, skip_old) if p[0] == "seg" else items.gap_via(p[1], p[2], p[3], net, clr, skip_old)
        if g < worst:
            worst, who = g, it
    return worst, who


def doglegs(q, e, t, exts=(0.0, 0.15, 0.3, 0.5, 0.8, 1.2)):
    """candidate polylines pad anchor q -> leg end e; t = the leg's direction AWAY from the terminal at e."""
    out = []
    for ext in exts:
        e2 = (e[0] - t[0] * ext, e[1] - t[1] * ext)
        dx, dy = e2[0] - q[0], e2[1] - q[1]
        k = min(abs(dx), abs(dy))
        dg = (math.copysign(k, dx), math.copysign(k, dy))
        st = (dx - dg[0], dy - dg[1])
        for first in (dg, st):
            pts = G.clean([q, (q[0] + first[0], q[1] + first[1]), e2, e], 1e-4)
            if len(pts) < 2:
                continue
            dirs = [G.unit(a, b) for a, b in zip(pts, pts[1:])] + [t]
            if all(u[0] * v[0] + u[1] * v[1] > -1e-6 for u, v in zip(dirs, dirs[1:])) and pts not in out:
                out.append(pts)
    return out


def stub(B, items, base_n, ignore, pad, e, layer, w, net, clr, skip_old, step=0.05):
    """grid-routed single track from pad to the leg end e (the dog-legs did not fit) -> (pts pad -> e) | None."""
    half = max(abs(e[0] - pad["x"]), abs(e[1] - pad["y"])) / 2 + 1.5
    F = PK.Field(B, ((pad["x"] + e[0]) / 2, (pad["y"] + e[1]) / 2), half, step, own={net}, ignore=ignore, clearance=clr)
    for it in items.it[base_n:]:
        if it["net"] == net:
            continue
        for l in it["layers"]:
            if it["k"] == "seg" and l == layer:
                F.block_seg(it["a"], it["b"], it["w"], l, it["net"] or "")
        if it["k"] == "via":
            F.block_via(it["c"][0], it["c"][1], it["d"], it["net"] or "")
    seeds = F.shape_mask(pad)
    d = PK.dist(F.free(layer, w + 0.04) | seeds, seeds, step)
    j, i = F.cell(*e)
    if not (0 <= j < F.ny and 0 <= i < F.nx) or d[j, i] >= PK.INF / 2:
        return None
    pts = [e]
    while d[j, i] > 0:
        v, j2, i2 = min((d[j + a, i + b], j + a, i + b) for a in (-1, 0, 1) for b in (-1, 0, 1)
                        if (a or b) and 0 <= j + a < F.ny and 0 <= i + b < F.nx)
        if v >= d[j, i]:
            return None
        j, i = j2, i2
        pts.append(F.xy(j, i))
    out = [pts[0]]
    for k in range(1, len(pts) - 1):                      # corners only
        a, b, c = out[-1], pts[k], pts[k + 1]
        if abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) > 1e-7 and k > 1:
            out.append(b)
    out.append(pts[-1])
    out = G.clean(out[::-1], 1e-4)
    if len(out) < 2:
        return None
    c, dx, dy = out[-2], e[0] - out[-2][0], e[1] - out[-2][1]
    k = min(abs(dx), abs(dy))
    dg = (math.copysign(k, dx), math.copysign(k, dy))
    for m in ((c[0] + dg[0], c[1] + dg[1]), (c[0] + dx - dg[0], c[1] + dy - dg[1]), None):      # the last hop off the grid as a 45 degree dog-leg
        pts = G.clean(out[:-1] + ([m] if m else []) + [e], 1e-4)
        pcs = [("seg", layer, a, b, w) for a, b in zip(pts, pts[1:])]
        if pcs and check(items, pcs, net, clr, skip_old)[0] >= clr - 1e-6:
            return pts
    return None


def _eff(pts, pad):
    inside = [k for k, q in enumerate(pts) if RC._in_pad(pad, q[0], q[1], 0.02)]      # the audit measures from the last joint in the pad
    return G.length(pts[max(inside, default=0):])


def breakout(items, pads, ends, t, layer, w, nets, clr, skip_old, bias=0.0, fallback=None):
    """break-outs of both legs at one terminal -> ([pieces P], [pieces N], length P - length N) | (None, reason, item).
    pads = (pad P, pad N), ends = (leg end P, leg end N).  Dog-legs first (the pair of candidates with the least
    |skew + bias| wins); `fallback(pad, e, net)` grid-routes a leg the dog-legs cannot reach."""
    def dog(k):
        ok, worst = [], (None, None)
        for pts in doglegs((pads[k]["x"], pads[k]["y"]), ends[k], t):
            pcs = [("seg", layer, a, b, w) for a, b in zip(pts, pts[1:])]
            g, it = check(items, pcs, nets[k], clr, skip_old)
            if g >= clr - 1e-6:
                ok.append((pts, pcs, _eff(pts, pads[k])))
            elif worst[0] is None or g > worst[0]:
                worst = (g, it)
        return ok, worst[1]
    cands, why = [], [None, None]
    for k in (0, 1):
        ok, why[k] = dog(k)
        cands.append(ok)
    best = None
    for a in cands[0]:
        for b in cands[1]:
            if min(G.seg_seg(p[2], p[3], q[2], q[3]) for p in a[1] for q in b[1]) < w + clr - 1e-6:
                continue
            key = (round(abs(a[2] - b[2] + bias), 3), a[2] + b[2])
            if best is None or key < best[0]:
                best = (key, a, b)
    if best is not None:
        return best[1][1], best[2][1], best[1][2] - best[2][2]
    if fallback:
        for first in (0, 1):                               # one leg at a time; the second sees the first
            mark = items.mark()
            got = {}
            for k in (first, 1 - first):
                ok, _w = dog(k)
                c = min(ok, key=lambda x: x[2]) if ok else None
                if c is None:
                    pts = fallback(pads[k], ends[k], layer, nets[k])
                    c = (pts, [("seg", layer, a, b, w) for a, b in zip(pts, pts[1:])], _eff(pts, pads[k])) if pts else None
                if c is None:
                    break
                got[k] = c
                for p in c[1]:
                    items.seg(p[2], p[3], p[4], p[1], nets[k])
            items.rollback(mark)
            if len(got) == 2:
                return got[0][1], got[1][1], got[0][2] - got[1][2]
    bad = 0 if not cands[0] else 1
    if not cands[bad]:
        return None, f"no clear break-out for {pads[bad]['ref']}.{pads[bad]['num']}", why[bad]
    return None, f"break-outs at {pads[0]['ref']} foul each other", None


def bump(items, pieces, net, extra, side, clr, skip_old, lo, hi, hmax=0.6, flat=0.3, lead=0.15):
    """lengthen `pieces[lo:hi]` (the coupled part of the short leg) by `extra` mm with 45 degree trapezoids on the side
    away from the partner (`side` = +1 left of travel), nearest the ends first.  -> (pieces, mm still missing)."""
    out = list(pieces)
    k2 = 2 * (math.sqrt(2) - 1)
    for k in sorted(range(lo, hi), key=lambda k: min(k - lo, hi - 1 - k)):
        p = out[k]
        if extra <= 0.02:
            break
        if p[0] != "seg":
            continue
        a, b = p[2], p[3]
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        u = G.unit(a, b)
        n = G.left(u)
        o = (n[0] * side, n[1] * side)
        new, s0 = [], lead
        prev = a
        while extra > 0.02:
            hgt = min(hmax, extra / k2)
            if s0 + 2 * hgt + flat + lead > L:
                break
            p1 = (a[0] + u[0] * s0, a[1] + u[1] * s0)
            p2 = (p1[0] + (u[0] + o[0]) * hgt, p1[1] + (u[1] + o[1]) * hgt)
            p3 = (p2[0] + u[0] * flat, p2[1] + u[1] * flat)
            p4 = (p3[0] + (u[0] - o[0]) * hgt, p3[1] + (u[1] - o[1]) * hgt)
            trap = [("seg", p[1], x, y, p[4]) for x, y in ((p1, p2), (p2, p3), (p3, p4))]
            if check(items, trap, net, clr, skip_old)[0] >= clr - 1e-6:
                new += [("seg", p[1], prev, p1, p[4])] + trap
                prev = p4
                extra -= hgt * k2
                s0 += 2 * hgt + flat + 2 * lead
            else:
                s0 += 0.4
        if new:
            out[k] = new + [("seg", p[1], prev, b, p[4])]
    flat_out = []
    for p in out:
        flat_out += p if isinstance(p, list) else [p]
    return flat_out, max(0.0, extra)
