"""`fluxplace wl-refine BOARD --constraints C --spec S --out O`: wire-length refinement of a LEGAL placement, subordinate to
routability and intent. Also the last step of `place --strategy routable` and `floorplan` when `[placement.wl] enabled = true`
(off by default: on a board placed up to the plan-route limit it finds nothing the gate accepts, and says so).

Objective (lower is better), evaluated incrementally (only the nets and support links of the parts a move touches):
  sum over signal nets of class weight x pin half-perimeter (plane/pour nets excluded: they leave by via; `[[intent.near]]` items
  count as two-pin nets with their weight) + the gate's penalty per mm over a net budget
  + `w_decap` x distance of every decoupling cap to the pin it serves, `w_support` x the same for the other support parts,
    + 5 x mm beyond the support limit (clusters.py owns who serves which pin; re-read every pass).
Moves, proposed toward the OPTIMAL REGION of the part (the weighted median of the boxes of the nets it is on, as in detailed
placers; that jump is the global step, nothing is moved blindly):
  move    one part to the optimal point / part of the way / a ring around it / a small slide, at 0 / 90 / 180 / 270 degrees
  unit    an IC with its support parts (routable.units) as one rigid body, translated or turned about the IC
  swap    two parts of the same footprint on the same side exchange seats
A move is kept only when ALL hold (cheapest test first):
  1. the objective drops;
  2. legal: no new legality.Legal issue (courtyards, outline, keep-outs, pad shorts, no-THT regions);
  3. no new blocked fine-pitch pin lane (the gate's PIN_ESCAPE_BLOCKED test, done locally);
  4. intent: the part enters no intent zone (regions, swept areas, connector fronts) it was out of; no hard near/away item
     becomes unmet and no soft one gets worse by more than `soft_tol_mm`. Parts held by an edge / group / region-under item never move;
  5. routing demand on the plan-route grid (bounding-box estimate): no cell that was within capacity goes above it, and no
     more demand in the cells the global router has filled (>= `hot_frac` of capacity) or overflowed;
     a support part inside its distance limit stays inside it;
  6. escapes: the escape planner, re-run for the part and its neighbours (planplace.PlanState.escapes, ~10 ms), finds no more
     sealed / oversubscribed / site-less pins than before.
"Not worse than the INPUT" (`worse`) = no more legality issues, blocked / sealed pins, nets at risk, unmet intent items; overflow,
the gate score (preroute.score) and PLAN_SCORE not higher. It is enforced at three levels:
  batch   every `batch` kept moves the calibrated global router is updated (PlanState, incremental) and the gate score recomputed;
          a worse batch is undone and replayed by halves, so only the offending moves are dropped
  pass    plan-route from scratch; a worse pass is undone and replayed by halves the same way (from scratch every time)
  result  the whole gate with the escape reservation; while worse, back one checkpoint (in the end: the input itself)
Deterministic for a given `--seed`.
Fixed parts, locked parts, anchors, mounting hardware and intent-held parts never move. Sides are not changed.
`[placement.wl]`: enabled, time_limit_s (600), passes (4), batch (40), max_try (6), reach_mm (3), w_decap (3), w_support (1),
soft_tol_mm (0.5), hot_frac (0.9), allow = {overflow = N, risk_nets = N, gate_score = N, plan_score = N} (what-if: how far those
four may end ABOVE the input; default nothing, and the record says what was allowed). Record: `<out stem>.wl-refine.json` (before / after / kept moves).
"""
import json
import math
import os
import random
import time
from collections import defaultdict

from . import clusters as CL
from . import escapeplan as EP
from . import geom as GM
from . import legality as LGL
from . import netclass as NC
from . import preroute as PR

DEFAULTS = {"enabled": False, "time_limit_s": 600.0, "passes": 4, "batch": 40, "max_try": 8, "reach_mm": 3.0, "w_decap": 3.0,
            "w_support": 1.0, "soft_tol_mm": 0.5, "hot_frac": 0.9}
ROT = {0: (1, 0, 0, 1), 90: (0, 1, -1, 0), 180: (-1, 0, 0, -1), 270: (0, -1, 1, 0)}   # geom.rot for right angles, exact
EPS = 1e-3
GRID = 0.05
HARD_PLAN = ("PLAN_PIN_SEALED", "PLAN_ESCAPE_OVERSUBSCRIBED", "PLAN_NO_VIA_SITE")
RECORD_KEYS = ("hpwl_all_mm", "hpwl_weighted", "decap_mean_mm", "decap_worst_mm", "decap_over", "blocked_pins", "overflow",
               "risk_nets", "intent_met", "gate_score", "plan_score")


def params(cons):
    p = dict(DEFAULTS)
    p.update(((cons or {}).get("placement") or {}).get("wl") or {})
    return p


def enabled(cons):
    return bool(params(cons)["enabled"])


def _snap(v):
    return round(round(v / GRID) * GRID, 4)


def _hp(xs, ys):
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def supports(B, cons, spec):
    """[{ref, via, ic, pad, limit, decap, sure, dist}] from clusters.build (dist as the gate measures it)."""
    from . import fluxmap as FM
    cl, _ = CL.build(B, cons, spec)
    return cl, [{"ref": s["ref"], "via": s["via"], "ic": c["ic"], "pad": s["pad"], "limit": s["limit"], "dist": s["dist"],
                 "decap": FM._kind(s["ref"], s["cls"]) == "decap", "sure": bool(s["sure"] or s["how"] == "spread")}
                for c in cl for s in c["supports"]]


def measure(B, cons=None, spec=None, cls=None):
    """wire length and support proximity of a placement (no planner): the numbers the record reports."""
    cls = cls if cls is not None else NC.classify(B, cons, spec)
    allw = wt = 0.0
    for n, ps in NC.net_pads(B).items():
        if len(ps) < 2:
            continue
        h = _hp([q["x"] for q in ps], [q["y"] for q in ps])
        allw += h
        wt += h * (cls.get(n) or {}).get("weight", 1.0)
    _cl, sup = supports(B, cons, spec)
    dec = [s for s in sup if s["decap"]]
    oth = [s for s in sup if not s["decap"]]
    mean = lambda v: round(sum(s["dist"] for s in v) / len(v), 2) if v else 0.0
    worst = lambda v: round(max((s["dist"] for s in v), default=0.0), 2)
    over = lambda v: sum(1 for s in v if s["dist"] > s["limit"] + 1e-6)
    return {"hpwl_all_mm": round(allw, 1), "hpwl_weighted": round(wt, 1), "decap_n": len(dec), "decap_mean_mm": mean(dec),
            "decap_worst_mm": worst(dec), "decap_over": over(dec), "support_n": len(oth), "support_mean_mm": mean(oth),
            "support_worst_mm": worst(oth), "support_over": over(oth)}


def gate(B, cons, spec, state=None, full=True):
    """the whole gate: preroute score + counts, plan-route (escape plan + global route; from scratch unless `state` is given),
    INTENT rows. full=False leaves the escape reservation out (same score, `unreserved` not counted). -> (dict, PlanState)."""
    from . import intent as IT, planplace as PP
    f, card = PR.run(B, cons, spec, escape=full)
    ps = state or PP.PlanState(B, cons, spec)
    rows = IT.check(B, cons, spec)
    val = lambda code: sum(int(x.get("value") or 1) for x in f if x["code"] == code)
    c = ps.comp
    g = {"gate_score": card["score"], "illegal": val("PLACEMENT_ILLEGAL"), "pin_blocked": val("PIN_ESCAPE_BLOCKED"),
         "unreserved": sum(1 for x in f if x["code"] == "PLACE_ESCAPE_UNRESERVED") if full else None, "plan_score": ps.score,
         "sealed": c["sealed"] + c["oversub"] + c["no_via_site"] + c["unreached"], "overflow": c["overflow"],
         "risk_nets": c["risk_nets"], "intent_met": sum(1 for r in rows if r["met"] is True),
         "intent_hard_unmet": sum(1 for r in rows if r["met"] is False and r["hard"]),
         "intent_soft_unmet": sum(1 for r in rows if r["met"] is False and not r["hard"])}
    g["blocked_pins"] = g["pin_blocked"] + g["sealed"]
    g.update(measure(B, cons, spec))
    return g, ps


NOISY = ("overflow", "risk_nets", "gate_score", "plan_score")


def worse(g, g0, tol=None):
    """[reasons] why gate `g` is worse than `g0` (empty = not worse). `tol` = {key: allowance} for the NOISY planner numbers
    (`[placement.wl] allow`, a what-if the owner sets; default none); legality, blocked / sealed pins and intent never have one."""
    why = []
    tol = tol or {}
    for k in ("illegal", "pin_blocked", "unreserved", "sealed", "intent_hard_unmet", "intent_soft_unmet"):
        if g[k] is not None and g0[k] is not None and g[k] > g0[k]:
            why.append(f"{k} {g0[k]} -> {g[k]}")
    if g["intent_met"] < g0["intent_met"]:
        why.append(f"intent met {g0['intent_met']} -> {g['intent_met']}")
    for k in NOISY:
        if g[k] > g0[k] + float(tol.get(k, 0.0)) + 1e-6:
            why.append(f"{k} {g0[k]} -> {g[k]}")
    return why


class Refiner:
    def __init__(self, B, cons=None, spec=None, seed=0, log=None, time_limit=None, state=None):
        from . import intent as IT
        self.B, self.cons, self.spec = B, cons or {}, spec
        self.log = log or (lambda m: None)
        self.pr = params(self.cons)
        self.t_end = time.time() + float(time_limit if time_limit is not None else self.pr["time_limit_s"])
        self.rng = random.Random(seed)
        self.cls = NC.classify(B, self.cons, spec)
        P = self.P = B["parts"]
        it = IT.items(B, self.cons)
        self.fixed = set(PR.fixed_parts(B, self.cons))
        held = {r for e in it["edge"] for r in e["refs"]} | {r for g in it["group"] for r in g["refs"]}
        held |= {r["under"] for r in it["region"] if r["under"]} | set(LGL.edge_mounted(B, self.cons))
        self.held = held
        self.movable = sorted(r for r in P if r not in self.fixed and r not in held and not P[r].get("locked")
                              and not EP.is_mech(r))
        self.mov = set(self.movable)
        self.legal = LGL.Legal(B, self.cons, self.fixed)
        self.base = self._triples(P)
        self.Z = IT.Zones(B, self.cons)
        self.irefs = {r for n in it["near"] for r in (n.get("chain") or (n["refs"] + [n["to"]]))}
        self.irefs |= {r for a in it["away"] for r in a["a"] + a["b"]}
        self.rows, self.far = self._irows(), self._far()
        from . import planplace as PP
        self.ps = state or PP.PlanState(B, self.cons, spec)
        self.stats = defaultdict(int)
        self.pending, self.batch_A, self.kept_pass = [], set(), []
        self._lanes_init()
        self._model()
        self._grid_init()

    # ---- model -----------------------------------------------------------------------------------------------------
    def _model(self):
        """nets (weighted, non-plane, intent links), support links and their caches; rebuilt at every pass."""
        from . import intent as IT
        B, P = self.B, self.P
        where = {id(q): (r, i) for r, p in P.items() for i, q in enumerate(p["pads"])}
        plane = set(EP.plane_nets(B, self.cons, self.cls, EP.plan_params(self.cons)))
        pads = {n: ps for n, ps in NC.net_pads(B).items() if n not in plane}
        links, linfo = IT.plan_links(B, self.cons)
        self.nets, self.pn = [], defaultdict(list)
        for name in sorted(set(pads) | set(links)):
            info = linfo.get(name) or self.cls.get(name) or {}
            ps = [q for q in (links.get(name) or pads[name]) if id(q) in where]
            w = float(info.get("weight", 1.0))
            if len(ps) < 2 or w <= 0:
                continue
            lim = info.get("budget") if name in linfo else NC.effective_budget(info) if info else None
            n = {"name": name, "w": w, "lim": lim, "pen": 0.0 if lim is None else (PR.W_HARD if info.get("class") in PR.HARD
                                                                                else PR.W_SOFT), "q": ps}
            ni = len(self.nets)
            self.nets.append(n)
            for k, q in enumerate(ps):
                r, i = where[id(q)]
                self.pn[r].append((ni, k, i))
        cl, sup = supports(B, self.cons, self.spec)
        self.cl = cl
        self.sup, self.sup_by = [], defaultdict(list)
        halo = {c["ic"]: (CL.HALO_MM if CL._pitch(P[c["ic"]]) <= 0.5 else 0.0) for c in cl}
        for s in sup:
            pi = next((i for i, q in enumerate(P[s["ref"]]["pads"]) if q["num"] == s["via"]), None)
            qi = next((i for i, q in enumerate(P[s["ic"]]["pads"]) if q["num"] == s["pad"]), None)
            if pi is None or qi is None:
                continue
            w = float(self.pr["w_decap"] if s["decap"] else self.pr["w_support"]) * (1.0 if s["sure"] else 0.5)
            e = {"ref": s["ref"], "pi": pi, "ic": s["ic"], "qi": qi, "lim": s["limit"] + halo[s["ic"]], "w": w,
                 "wo": PR.W_SUPPORT if s["sure"] else 0.0}
            self.sup_by[s["ref"]].append(len(self.sup))
            self.sup_by[s["ic"]].append(len(self.sup))
            self.sup.append(e)
        self.off = {r: [(q["x"] - p["pos"][0], q["y"] - p["pos"][1]) for q in p["pads"]] for r, p in P.items()}
        for n in self.nets:
            self._net_cache(n)
        for s in self.sup:
            s["d"] = self._sdist(s, {})
            s["c"] = self._scost(s, s["d"])
        self.J = sum(n["c"] for n in self.nets) + sum(s["c"] for s in self.sup)
        self.U = {}
        from . import routable as RB
        for k, ms in RB.units(B, cl, self.fixed).items():
            if len(ms) > 1 and all(m in self.mov for m in ms):
                self.U[k] = ms
        self.by_fp = defaultdict(list)
        for r in self.movable:
            self.by_fp[(P[r]["fp"], P[r]["side"])].append(r)

    def _net_cache(self, n):
        n["x"], n["y"] = [q["x"] for q in n["q"]], [q["y"] for q in n["q"]]
        n["c"] = self._ncost(n, n["x"], n["y"])
        n["bb"] = (min(n["x"]), min(n["y"]), max(n["x"]), max(n["y"]))

    @staticmethod
    def _ncost(n, xs, ys):
        h = _hp(xs, ys)
        return n["w"] * h + (n["pen"] * (h - n["lim"]) if n["pen"] and h > n["lim"] else 0.0)

    def _padxy(self, ref, i, new):
        m = new.get(ref)
        if m is None:
            q = self.P[ref]["pads"][i]
            return q["x"], q["y"]
        x, y, d = m
        a, b, c, e = ROT[d]
        dx, dy = self.off[ref][i]
        return x + a * dx + b * dy, y + c * dx + e * dy

    def _sdist(self, s, new):
        ax, ay = self._padxy(s["ref"], s["pi"], new)
        bx, by = self._padxy(s["ic"], s["qi"], new)
        return math.hypot(ax - bx, ay - by)

    @staticmethod
    def _scost(s, d):
        return s["w"] * d + (s["wo"] * (d - s["lim"]) if d > s["lim"] else 0.0)

    def delta(self, new):
        """objective change of the hypothetical poses `new` = {ref: (x, y, turn in degrees about the part's origin)}."""
        touched = {}
        sups = set()
        for ref, (x, y, d) in new.items():
            a, b, c, e = ROT[d]
            off = self.off[ref]
            for ni, k, i in self.pn[ref]:
                dx, dy = off[i]
                touched.setdefault(ni, []).append((k, x + a * dx + b * dy, y + c * dx + e * dy))
            sups.update(self.sup_by[ref])
        dj = 0.0
        for ni, sub in touched.items():
            n = self.nets[ni]
            xs, ys = n["x"][:], n["y"][:]
            for k, X, Y in sub:
                xs[k], ys[k] = X, Y
            dj += self._ncost(n, xs, ys) - n["c"]
        for si in sups:
            s = self.sup[si]
            dj += self._scost(s, self._sdist(s, new)) - s["c"]
        return dj

    def _sync(self, refs):
        """caches follow the poses of `refs` (after a kept move, or after a revert)."""
        P = self.P
        nets, sups = set(), set()
        for r in refs:
            p = P[r]
            self.off[r] = [(q["x"] - p["pos"][0], q["y"] - p["pos"][1]) for q in p["pads"]]
            nets.update(ni for ni, _k, _i in self.pn[r])
            sups.update(self.sup_by[r])
            self._lanes_set(r)
        for ni in nets:
            n = self.nets[ni]
            self._rudy(n, -1.0)
            self.J -= n["c"]
            self._net_cache(n)
            self.J += n["c"]
            self._rudy(n, 1.0)
        for si in sups:
            s = self.sup[si]
            self.J -= s["c"]
            s["d"] = self._sdist(s, {})
            s["c"] = self._scost(s, s["d"])
            self.J += s["c"]

    # ---- legality, lanes, intent, demand, escapes ------------------------------------------------------------------
    def _triples(self, refs):
        return {(r, k, str(d)) for r in refs for k, d in self.legal.part_issues(r)}

    def _pose(self, r):
        p = self.P[r]
        return (p["pos"][0], p["pos"][1], p["angle"])

    def _apply(self, poses):
        for r, (x, y, a) in poses.items():
            GM.place(self.P[r], x, y, a)
            self.legal.refresh(r)

    def _lanes_init(self):
        self.lh, self.lane = GM.Hash(2.0), {}
        self.L, self.Wd = self.B["track"] + self.B["clearance"], self.B["track"]
        self.owners = {r for r, p in self.P.items() if NC.is_ic(r, p, 3) and p["n_pads"] >= 5 and CL._pitch(p) <= 0.5}
        for r in sorted(self.owners):
            self._lanes_set(r)

    def _lanes_set(self, r):
        if r not in self.owners:
            return
        p = self.P[r]
        for i, q in enumerate(p["pads"]):
            if not q["net"] or q["w"] * q["h"] > 1.5:
                continue
            self.lh.remove((r, i))
            self.lane[(r, i)] = PR._lane(q, p, self.L, self.Wd)
            self.lh.add((r, i), self.lane[(r, i)], "x")

    def _lane_blocked(self, key, ignore=()):
        r, i = key
        lane, P = self.lane[key], self.P
        q = P[r]["pads"][i]
        for o, j in self.legal.phash.hits(lane, "x"):
            if o in ignore or (o == r and j == i):
                continue
            t = P[o]["pads"][j]
            if t["net"] != q["net"] and t["layers"] & q["layers"]:
                return True
        for o in self.legal.hash.hits(lane, "x"):
            if o != r and o not in ignore and P[o]["side"] == P[r]["side"]:
                return True
        return False

    def _lanes_near(self, refs):
        out = set()
        for r in refs:
            c = self.P[r]["court"]
            out.update(k for k in self.lh.hits((c[0] - 0.3, c[1] - 0.3, c[2] + 0.3, c[3] + 0.3), "x"))
            if r in self.owners:
                out.update(k for k in self.lane if k[0] == r)
        return out

    def _irows(self):
        from . import intent as IT
        return {r["name"]: r for r in IT.check(self.B, self.cons, self.spec, only_kinds={"near", "away"})}

    def _intent_ok(self, refs, old_courts):
        for r in refs:
            c, c0 = self.P[r]["court"], old_courts[r]
            for _name, rect in self.Z.for_ref(r):
                if GM.overlap(c, rect, -0.02) and not GM.overlap(c0, rect, -0.02):
                    return False
        if not self.irefs.intersection(refs):
            return True
        tol = float(self.pr["soft_tol_mm"])
        rows = self._irows()
        for name, r in rows.items():
            r0 = self.rows.get(name)
            if r["met"] is not False or r0 is None:
                continue
            w = (r0["value"] - r["value"]) if r["kind"] == "away" else (r["value"] - r0["value"])     # > 0 = worse
            if (r["hard"] and (r0["met"] is not False or w > 1e-6)) or (not r["hard"] and w > tol):
                return False
        self._rows_new = rows
        return True

    def _far(self):
        """(support parts beyond their limit, mm beyond in total) as the gate counts them (ownership re-read)."""
        far = CL.far_parts(CL.build(self.B, self.cons, self.spec)[0])
        return len(far), round(sum(s["dist"] - s["limit"] for _ic, s in far), 3)

    def _support_ok(self, refs):
        if not any(r in self.sup_by for r in refs):
            return True
        for si in sorted({si for r in refs for si in self.sup_by[r]}):
            s = self.sup[si]
            if s["d"] <= s["lim"] + 1e-9 < self._sdist(s, {}):
                return False                            # inside its limit before, outside after
        n, mm = self._far()
        if n > self.far[0] or mm > self.far[1] + 1e-6:
            return False
        self._far_new = (n, mm)
        return True

    def _grid_init(self):
        """demand grid = the plan-route grid (globalroute.py): capacity per cell from its obstacle model, `hot` = the cells its
        routes fill or overflow now."""
        import numpy as np
        gr = self.ps.gr
        self.cell, self.gx0, self.gy0, self.nx, self.ny = gr.cell, gr.x0, gr.y0, gr.nx, gr.ny
        self.dem = np.zeros((self.nx, self.ny))
        for n in self.nets:
            self._rudy(n, 1.0)
        self._hot()

    def _hot(self):
        import numpy as np
        gr = self.ps.gr
        tot = lambda rows: np.array(rows, float).sum(axis=0).reshape(self.ny, self.nx).T
        cap, use = tot(gr.capH) + tot(gr.capV), tot(gr.uH) + tot(gr.uV)
        self.cap = cap
        self.hot = use >= float(self.pr["hot_frac"]) * cap - 1e-9
        self.hot &= use > 0
        for (_l, c) in gr.overflow_cells():
            self.hot[c % self.nx, c // self.nx] = True

    def _ij(self, x, y):
        return (min(self.nx - 1, max(0, int((x - self.gx0) / self.cell))), min(self.ny - 1, max(0, int((y - self.gy0) / self.cell))))

    def _box(self, bb):
        i0, j0 = self._ij(bb[0], bb[1])
        i1, j1 = self._ij(bb[2], bb[3])
        return i0, j0, i1 + 1, j1 + 1

    def _rudy(self, n, sign, dem=None, bb=None, shift=(0, 0)):
        """bounding-box demand: one horizontal and one vertical run of the net spread over its box (track-cells per cell)."""
        i0, j0, i1, j1 = self._box(bb or n["bb"])
        d = self.dem if dem is None else dem
        d[i0 - shift[0]:i1 - shift[0], j0 - shift[1]:j1 - shift[1]] += sign * (1.0 / (i1 - i0) + 1.0 / (j1 - j0))

    def _demand_ok(self, refs):
        """no cell that was within capacity goes above it, and no more demand in the cells that are full already."""
        nets = sorted({ni for r in refs for ni, _k, _i in self.pn[r]})
        if not nets:
            return True
        new = {}
        for ni in nets:
            q = self.nets[ni]["q"]
            xs, ys = [t["x"] for t in q], [t["y"] for t in q]
            new[ni] = (min(xs), min(ys), max(xs), max(ys))
        boxes = [self._box(self.nets[ni]["bb"]) for ni in nets] + [self._box(b) for b in new.values()]
        i0, j0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        i1, j1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        before = self.dem[i0:i1, j0:j1]
        after = before.copy()
        for ni in nets:
            self._rudy(self.nets[ni], -1.0, after, None, (i0, j0))
            self._rudy(self.nets[ni], 1.0, after, new[ni], (i0, j0))
        cap, hot = self.cap[i0:i1, j0:j1], self.hot[i0:i1, j0:j1]
        if ((after > cap + 1e-9) & (before <= cap + 1e-9)).any():
            return False
        return not hot.any() or float(after[hot].sum()) <= float(before[hot].sum()) + 1e-9

    def _hard(self):
        return sum(1 for f in self.ps.plan.findings if f["code"] in HARD_PLAN)

    def _escape_ok(self, refs, old_courts):
        plan = self.ps.plan
        keep = (dict(plan.pins), dict(plan.used), list(plan.findings), list(plan.extra), list(plan.groups))
        h0 = self._hard()
        A = self.ps.escapes(refs, old_courts)
        if self._hard() > h0:
            plan.pins, plan.used, plan.findings, plan.extra, plan.groups = keep
            return False
        self.batch_A |= A
        return True

    # ---- one candidate ---------------------------------------------------------------------------------------------
    def _abs(self, new):
        return {r: (x, y, (self.P[r]["angle"] + d) % 360.0) for r, (x, y, d) in new.items()}

    def _checks(self, refs, old, oldc, lanes0, escapes=True):
        if not self._triples(refs) <= self.base:
            return "illegal"
        for r in refs:
            self._lanes_set(r)
        blk = lambda k: self._lane_blocked(k)
        b0, near1 = lanes0
        for k in self._lanes_near(refs):
            if blk(k) and (k not in b0) and (k in near1 or not self._lane_blocked(k, ignore=refs)):
                return "lane"
        if not self._intent_ok(refs, oldc):
            return "intent"
        if not self._demand_ok(refs):
            return "demand"
        if not self._support_ok(refs):
            return "support"
        if escapes and not self._escape_ok(refs, oldc):
            return "escape"
        return ""

    def try_move(self, kind, new, escapes=True, record=True):
        """apply `new` {ref: (x, y, turn)}; keep when every test passes. True when kept."""
        refs = sorted(new)
        old = {r: self._pose(r) for r in refs}
        oldc = {r: self.P[r]["court"] for r in refs}
        near0 = self._lanes_near(refs)
        lanes0 = ({k for k in near0 if self._lane_blocked(k)}, near0)
        self._rows_new = self._far_new = None
        self._apply(self._abs(new))
        why = self._checks(refs, old, oldc, lanes0, escapes)
        if why:
            self.stats["rejected_" + why] += 1
            self._apply(old)
            for r in refs:
                self._lanes_set(r)
            return False
        if self._rows_new:
            self.rows = self._rows_new
        if self._far_new:
            self.far = self._far_new
        self._sync(refs)
        self.stats[kind] += 1
        if record:
            self.pending.append((kind, old, {r: self._pose(r) for r in refs}, oldc))
        return True

    # ---- proposals -------------------------------------------------------------------------------------------------
    def _target(self, members, anchor, d):
        """optimal anchor position of the rigid set `members` turned by `d` about `anchor`: weighted median of the boxes of
        the other pins of its nets (and the pins its support parts serve). None when nothing pulls."""
        P = self.P
        mem = set(members)
        ax, ay = P[anchor]["pos"]
        a, b, c, e = ROT[d]
        xs, ys = [], []
        for r in members:
            ox, oy = P[r]["pos"][0] - ax, P[r]["pos"][1] - ay
            for ni, k, i in self.pn[r]:
                n = self.nets[ni]
                ext = [t for t in range(len(n["q"])) if n["q"][t]["ref"] not in mem]
                if not ext:
                    continue
                dx, dy = self.off[r][i][0] + ox, self.off[r][i][1] + oy
                px, py = a * dx + b * dy, c * dx + e * dy
                x0, x1 = min(n["x"][t] for t in ext), max(n["x"][t] for t in ext)
                y0, y1 = min(n["y"][t] for t in ext), max(n["y"][t] for t in ext)
                xs += [(x0 - px, n["w"]), (x1 - px, n["w"])]
                ys += [(y0 - py, n["w"]), (y1 - py, n["w"])]
            for si in self.sup_by[r]:
                s = self.sup[si]
                if s["ref"] in mem and s["ic"] in mem:
                    continue
                mine, other = ((s["ref"], s["pi"]), (s["ic"], s["qi"])) if s["ref"] == r else ((s["ic"], s["qi"]), (s["ref"], s["pi"]))
                dx, dy = self.off[r][mine[1]][0] + ox, self.off[r][mine[1]][1] + oy
                px, py = a * dx + b * dy, c * dx + e * dy
                tx, ty = self._padxy(other[0], other[1], {})
                xs += [(tx - px, s["w"] + s["wo"])] * 2
                ys += [(ty - py, s["w"] + s["wo"])] * 2
        if not xs:
            return None

        def med(v):
            v = sorted(v)
            half, acc = sum(w for _x, w in v) / 2.0, 0.0
            for x, w in v:
                acc += w
                if acc >= half - 1e-12:
                    return x
            return v[-1][0]
        return med(xs), med(ys)

    def _rigid(self, members, anchor, x, y, d):
        P = self.P
        ax, ay = P[anchor]["pos"]
        a, b, c, e = ROT[d]
        out = {}
        for r in members:                               # the anchor is on the grid, the others keep their exact offsets
            dx, dy = P[r]["pos"][0] - ax, P[r]["pos"][1] - ay
            out[r] = (round(x + a * dx + b * dy, 4), round(y + c * dx + e * dy, 4), d)
        return out

    def _positions(self, x0, y0, t, reach):
        pts = []
        if t is not None:
            tx, ty = t
            dist = math.hypot(tx - x0, ty - y0)
            if dist > GRID:
                pts.append((tx, ty))
                pts += [(x0 + (tx - x0) * f, y0 + (ty - y0) * f) for f in (0.75, 0.5, 0.25, 0.125)]
                for rad in (0.5, 1.0, 2.0, reach):
                    if rad < dist:
                        pts += [(tx + rad * math.cos(k * math.pi / 4), ty + rad * math.sin(k * math.pi / 4)) for k in range(8)]
        for s in (0.1, 0.25, 0.5, 1.0, 2.0):
            pts += [(x0 + s, y0), (x0 - s, y0), (x0, y0 + s), (x0, y0 - s)]
        return pts

    def improve(self, members, anchor, kind, turns):
        """best-first: every candidate pose is scored by `delta` (cheap), the best `max_try` improving ones are tested."""
        x0, y0 = self.P[anchor]["pos"]
        cands = []
        for d in turns:
            t = self._target(members, anchor, d)
            if d:
                cands.append((self.delta(self._rigid(members, anchor, x0, y0, d)), d, x0, y0))
            for x, y in self._positions(x0, y0, t, float(self.pr["reach_mm"])):
                x, y = _snap(x), _snap(y)
                cands.append((self.delta(self._rigid(members, anchor, x, y, d)), d, x, y))
        self.stats["scored"] += len(cands)
        cands = sorted(c for c in cands if c[0] < -EPS)[:int(self.pr["max_try"])]
        for _dj, d, x, y in cands:
            if self.try_move(kind, self._rigid(members, anchor, x, y, d)):
                return True
        return False

    def swap(self, r):
        P = self.P
        t = self._target([r], r, 0)
        if t is None:
            return False
        peers = [o for o in self.by_fp[(P[r]["fp"], P[r]["side"])] if o != r and (o not in self.inunit) == (r not in self.inunit)]
        peers = sorted(peers, key=lambda o: (math.hypot(P[o]["pos"][0] - t[0], P[o]["pos"][1] - t[1]), o))[:6]
        cands = []
        for o in peers:
            da = (P[o]["angle"] - P[r]["angle"]) % 360.0
            if da not in ROT:
                continue
            for extra in (0, 180):
                new = {r: (P[o]["pos"][0], P[o]["pos"][1], int(da + extra) % 360),
                       o: (P[r]["pos"][0], P[r]["pos"][1], int(360 - da + extra) % 360)}
                cands.append((self.delta(new), o, extra, new))
        self.stats["scored"] += len(cands)
        for _dj, _o, _e, new in sorted((c for c in cands if c[0] < -EPS), key=lambda c: c[:3])[:int(self.pr["max_try"])]:
            if self.try_move("swap", new):
                return True
        return False

    # ---- batches: global route + the gate score ------------------------------------------------------------------------
    def _worse(self, state):
        g, ps = gate(self.B, self.cons, self.spec, state=state, full=False)
        return worse(g, self.g0, self.tol), g, ps

    def flush(self):
        """route the batch (incremental) and score it; when it is worse than the input, undo it and replay it by halves."""
        batch, self.pending = self.pending, []
        if not batch:
            return
        moved = sorted({r for _k, old, _new, _c in batch for r in old})
        self.ps.gr.update_after_move(set(moved), self.batch_A | set(moved), True)
        self.ps._measure()
        self.batch_A = set()
        why = self._worse(self.ps)[0]
        if os.environ.get("FLUXPLACE_WL_DEBUG"):
            self.log(f"    batch {len(batch)}: J {self.J:.0f} {why}")
        if not why:
            self.psnap = self.ps.snapshot()
            self.kept_pass += batch
            self._hot()
            return
        self._undo(batch)
        self.ps.restore(self.psnap, moved)
        self.stats["batch_replayed"] += 1
        self._halves(batch, False)
        self._hot()

    def _halves(self, batch, fresh):
        if len(batch) > (3 if fresh else 1) and time.time() < self.t_end:
            h = len(batch) // 2
            self._replay(batch[:h], fresh)
            self._replay(batch[h:], fresh)
        else:
            self._drop(batch)

    def _undo(self, batch):
        for _k, old, _new, _c in reversed(batch):
            self._apply(old)
            self._sync(sorted(old))
        self.rows, self.far = self._irows(), self._far()

    def _drop(self, batch):
        for k, _o, _n, _c in batch:
            self.stats[k] -= 1
            self.stats["dropped_gate"] += 1

    def _replay(self, batch, fresh):
        """apply the moves of `batch` again (those still legal and still shortening), judge them together; `fresh` = plan-route
        from scratch instead of the incremental update."""
        from . import planplace as PP
        done, oldc = [], {}
        for kind, old, new, _c in batch:
            cur = {r: self._pose(r) for r in old}
            j0 = self.J
            courts = {r: self.P[r]["court"] for r in old}
            turn = {r: (new[r][0], new[r][1], int(round((new[r][2] - cur[r][2]) % 360.0)) % 360) for r in new}
            self.stats[kind] -= 1
            if all(t[2] in ROT for t in turn.values()) and self.try_move(kind, turn, escapes=False, record=False):
                if self.J < j0 - EPS:
                    done.append((kind, cur, new, courts))
                    for r, c in courts.items():
                        oldc.setdefault(r, c)
                    continue
                self.stats[kind] -= 1
                self._apply(cur)
                self._sync(sorted(cur))
                self.rows, self.far = self._irows(), self._far()
            self.stats["dropped_gate"] += 1
        if not done:
            return
        moved = sorted(oldc)
        if fresh:
            st = PP.PlanState(self.B, self.cons, self.spec)
            ok = not self._worse(st)[0]
            if ok:
                self.ps = st
        else:
            self.ps.update(moved, oldc)
            ok = not self._worse(self.ps)[0]
        if ok:
            self.psnap = self.ps.snapshot()
            self.kept_pass += done
            return
        self._undo(done)
        if not fresh:
            self.ps.restore(self.psnap, moved)
        self._halves(done, fresh)

    # ---- run -------------------------------------------------------------------------------------------------------
    def _poses(self):
        return {r: self._pose(r) for r in self.movable}

    def _restore(self, poses):
        ch = [r for r in poses if self._pose(r) != poses[r]]
        self._apply({r: poses[r] for r in ch})
        return ch

    def run(self):
        P = self.P
        t0 = time.time()
        g0, self.ps = gate(self.B, self.cons, self.spec, self.ps)
        self.g0 = g = g0
        self.tol = {k: float(v) for k, v in (self.pr.get("allow") or {}).items() if k in NOISY}
        if self.tol:
            self.log("wl-refine: ALLOWED to get worse than the input by ([placement.wl] allow): " + ", ".join(f"{k} +{v:g}" for k, v in sorted(self.tol.items())))
        self.log(f"wl-refine: {len(self.movable)} movable part(s), {len(self.nets)} net(s) in the objective, {len(self.sup)} support link(s); "
                 f"objective {self.J:.0f}")
        origin = {r: self._pose(r) for r in P}
        marks = [self._poses()]                         # checkpoints: every one passed plan-route from scratch and the gate score
        self.passes = []
        idle = 0
        for ps in range(int(self.pr["passes"])):
            if time.time() > self.t_end or idle >= 2:
                break
            snap0 = self.psnap = self.ps.snapshot()
            self.kept_pass = []
            self.inunit = {m for ms in self.U.values() for m in ms}
            order = [("unit", k) for k in sorted(self.U)] + [("move", r) for r in self.movable]
            self.rng.shuffle(order)
            j0, nb = self.J, 0
            for kind, k in order:
                if time.time() > self.t_end:
                    self.log("wl-refine: time limit reached")
                    break
                if kind == "unit":
                    self.improve(self.U[k], k, "unit", (0, 90, 180, 270))
                else:
                    small = P[k]["n_pads"] <= 8 and k not in self.U
                    if not self.improve([k], k, "move", (0, 90, 180, 270) if small else (0,)):
                        self.swap(k)
                if len(self.pending) >= int(self.pr["batch"]):
                    self.flush()
                    nb += 1
                    if nb >= 2 and not self.kept_pass:
                        self.log("wl-refine: the first two batches left nothing the gate accepts; the placement has no room for shorter wires")
                        break
            self.flush()
            n1 = len(self.kept_pass)
            why, g1, fresh = self._worse(None) if n1 else ([], g, self.ps)
            if why:                                     # from scratch the pass is worse: undo it, keep the halves that are not
                batch, self.kept_pass = self.kept_pass, []
                self._undo(batch)
                self.ps.restore(snap0, sorted({r for _k, old, _n, _c in batch for r in old}))
                self._halves(batch, True)
                _w, g1, fresh = self._worse(self.ps)
                self.stats["pass_replayed"] += 1
            n = len(self.kept_pass)
            self.log(f"wl-refine pass {ps + 1}: kept {n}" + (f" of {n1}" if n != n1 else "") + f", objective {j0:.0f} -> {self.J:.0f}, "
                     f"class-weighted HPWL {g1['hpwl_weighted']:.0f}, decap mean {g1['decap_mean_mm']:.2f} mm, overflow {g1['overflow']}, "
                     f"risk nets {g1['risk_nets']}, plan {g1['plan_score']:.0f}, gate {g1['gate_score']:.0f}"
                     + (f"; from scratch the whole pass was worse ({'; '.join(why)})" if why else ""))
            self.passes.append({"kept": n, "objective": [round(j0, 1), round(self.J, 1)], "replayed": why})
            if n:
                marks.append(self._poses())
                g = g1
            idle = 0 if n else idle + 1
            self._rebuild(fresh)
        # the whole gate, escape reservation included, on the result: walk back through the checkpoints while it is worse
        while len(marks) > 1:
            g, self.ps = gate(self.B, self.cons, self.spec, self.ps)
            why = worse(g, g0, self.tol)
            if not why:
                break
            self.log(f"wl-refine: result worse than the input on the whole gate ({'; '.join(why)}); back one checkpoint")
            self.stats["checkpoint_back"] += 1
            marks.pop()
            self._restore(marks[-1])
            self._rebuild(None)
        if len(marks) == 1:
            g = g0
        self.g1 = g
        norm = lambda a: (a + 180) % 360 - 180
        moves = {r: (round(x, 4), round(y, 4), a) for r, (x, y, a) in ((r, self._pose(r)) for r in P)
                 if abs(x - origin[r][0]) > 1e-4 or abs(y - origin[r][1]) > 1e-4 or abs(norm(a - origin[r][2])) > 1e-6}
        self.secs = round(time.time() - t0, 1)
        return moves

    def _rebuild(self, state):
        from . import planplace as PP
        self.ps = state or PP.PlanState(self.B, self.cons, self.spec)
        self.legal = LGL.Legal(self.B, self.cons, self.fixed)
        self._lanes_init()
        self._model()
        self._grid_init()
        self.rows, self.far = self._irows(), self._far()


def summary(rec):
    b, a = rec["before"], rec["after"]
    f = lambda k, n=0: f"{b[k]:.{n}f} -> {a[k]:.{n}f}"
    return (f"wl-refine: {rec['moved']} part(s) moved; HPWL all {f('hpwl_all_mm')} mm, class-weighted {f('hpwl_weighted')}; "
            f"decap mean {f('decap_mean_mm', 2)} mm (worst {f('decap_worst_mm', 1)}, over limit {b['decap_over']} -> {a['decap_over']}); "
            f"blocked pins {b['blocked_pins']} -> {a['blocked_pins']}, overflow {f('overflow', 1)}, risk nets {b['risk_nets']} -> {a['risk_nets']}, "
            f"intent met {b['intent_met']} -> {a['intent_met']}; gate {f('gate_score')}, plan {f('plan_score')}; {rec['seconds']} s")


def refine(B, cons=None, spec=None, seed=0, log=print, time_limit=None):
    """-> (moves {ref: (x, y, angle)}, record). Mutates B. The result is never worse on the gate than the input."""
    r = Refiner(B, cons, spec, seed, log, time_limit)
    mv = r.run()
    rec = {"before": r.g0, "after": r.g1, "moved": len(mv), "kept": {k: v for k, v in sorted(r.stats.items())}, "allowance": r.tol,
           "passes": r.passes, "seed": seed, "seconds": r.secs, "movable": len(r.movable)}
    return mv, rec


def refine_file(board_in, board_out, cons, spec, seed=0, log=print, time_limit=None):
    """the pass on a board file (pure text moves). -> record; `<out stem>.wl-refine.json` holds it."""
    from . import boardwrite as BW, routediag as RD
    B = RD.load(board_in)
    mv, rec = refine(B, cons, spec, seed, log, time_limit)
    if mv or os.path.abspath(board_in) != os.path.abspath(board_out):
        BW.move_file(board_in, board_out, mv)
    rec["board"] = board_out
    rec["line"] = summary(rec)
    json.dump(rec, open(os.path.splitext(board_out)[0] + ".wl-refine.json", "w"), indent=1, default=str)
    return rec


def short(rec):
    """the before -> after numbers in under 80 characters (pipeline state)."""
    b, a = rec["before"], rec["after"]
    return (f"hpwl {b['hpwl_weighted']:.0f}>{a['hpwl_weighted']:.0f} decap {b['decap_mean_mm']:.1f}>{a['decap_mean_mm']:.1f} "
            f"blocked {b['blocked_pins']}>{a['blocked_pins']} ovf {b['overflow']:.1f}>{a['overflow']:.1f}")


def stage_step(board, cons, spec, log=None, check=None):
    """the placement-stage hook (`place --strategy routable`, `floorplan`): refine `board` in place unless
    `[placement.wl] enabled = false`. `check(path) -> blocked pins` (the flow's own fan-out) is run before and after; more
    blocked pins after = the refined board is thrown away. Never fails the stage: on an error the board stays as it was.
    -> the record (with "line") or None."""
    import shutil
    if not enabled(cons):
        return None
    keep = os.path.splitext(board)[0] + ".pre-wl.kicad_pcb"
    shutil.copyfile(board, keep)
    try:
        rec = refine_file(board, board, cons, spec, seed=int(params(cons).get("seed", 0)),
                          log=log or (lambda m: print("  " + m, flush=True)))
        if check and rec["moved"]:
            b0, b1 = check(keep), check(board)
            rec["fanout_blocked"] = [b0, b1]
            if b1 > b0:
                shutil.copyfile(keep, board)
                rec.update(after=rec["before"], moved=0, discarded=f"the fan-out blocks {b1} pin(s) after, {b0} before")
                rec["line"] = f"wl-refine: discarded ({rec['discarded']}); the placement is unchanged"
            json.dump(rec, open(os.path.splitext(board)[0] + ".wl-refine.json", "w"), indent=1, default=str)
    except Exception as e:
        shutil.copyfile(keep, board)
        print(f"wl-refine: skipped ({e.__class__.__name__}: {e}); the placement is unchanged")
        return None
    finally:
        if os.path.exists(keep):
            os.remove(keep)
    print(rec["line"])
    return rec


def cmd_wl_refine(a):
    from . import constraints as C
    cons = C.load(a.constraints)
    spec = json.load(open(a.spec)) if a.spec else None
    out = a.out or os.path.splitext(a.board)[0] + ".wl.kicad_pcb"
    if os.path.abspath(out) == os.path.abspath(a.board):
        raise SystemExit("wl-refine: --out must differ from the input board")
    if a.passes is not None:
        cons.setdefault("placement", {}).setdefault("wl", {})["passes"] = a.passes
    if a.allow:
        cons.setdefault("placement", {}).setdefault("wl", {})["allow"] = {k.strip(): float(v) for k, v in (kv.split("=") for kv in a.allow.split(","))}
    quiet = getattr(a, "quiet", False)
    rec = refine_file(a.board, out, cons, spec, seed=a.seed, time_limit=a.time_limit,
                      log=(lambda m: None) if quiet else (lambda m: print("  " + m, flush=True)))
    print(rec["line"])
    if not quiet:
        print(f"  kept: {rec['kept']}")
        print(f"  wrote {out}; record {os.path.splitext(out)[0]}.wl-refine.json")
    return 0


def add_parser(sub):
    p = sub.add_parser("wl-refine", help="shorten wire length; routability, fan-out and intent never worse than the input")
    p.add_argument("board")
    p.add_argument("--constraints", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--out", default=None, help="result board (default <board>.wl.kicad_pcb)")
    p.add_argument("--seed", type=int, default=0, help="visiting order (deterministic)")
    p.add_argument("--time-limit", type=float, default=None, help="seconds (default [placement.wl] time_limit_s, 600)")
    p.add_argument("--passes", type=int, default=None, help="default [placement.wl] passes, 4")
    p.add_argument("--allow", default=None, metavar="K=V,...",
                   help="what-if: let overflow / risk_nets / gate_score / plan_score end this much above the input (default: nothing)")
    p.set_defaults(fn=cmd_wl_refine)
    return p
