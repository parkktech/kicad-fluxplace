"""`place --strategy routable --objective plan`: local moves from the input placement, kept only when the board stays
legal and PLAN_SCORE (planroute.py) does not get worse.

Moves are proposed from the findings, not at random:
  PLAN_PIN_SEALED    the blocking part shifts away along the lane by the missing length (then 0.5 / 1 / 2 mm); the sealed
                     part steps back from its blocker, or turns 90 / 180 / 270 degrees
  PLAN_NO_VIA_SITE   the neighbours of the pad step away from it; the pad's part steps away from its nearest neighbour
  PLAN_OVERFLOW      the parts bordering the region step away from its centre (1 / 2 mm) or turn; a part with a net partner
                     outside the region steps toward it
  PLAN_NET_OVER_BUDGET  the movable end of the net steps toward the other end
  support part beyond its limit (clusters.py)  steps toward the pin it serves
A unit (an IC with its support parts, routable.units) moves as one; a lone part moves alone.  Same-footprint parts of one group
swap; a side flip is tried only with `[placement.side] flip_cost`.  Fixed parts, locked parts, anchors and mounting hardware
never move (the Refiner's fixed set).

Re-evaluation is incremental (PlanState.update): escapes are re-planned for the moved parts and their neighbours within 3 mm
(lattice phase and the other parts' via sites held), the grid is re-rasterised only in the cells the parts touched, and only
the nets with a pad on those parts or a path through a changed cell are re-routed (3 negotiation rounds).  A rejected move
restores a snapshot.  planroute.run() on the result is the confirmation.
"""
import math
import time
from collections import defaultdict

from . import escapeplan as EP
from . import geom as GM
from . import globalroute as GR
from . import legality as LGL
from . import netclass as NC
from . import planroute as PL
from . import refine as RF

NEIGH_MM = 3.0


class PlanState:
    """Escape plan + global route of the current placement, updated incrementally."""

    def __init__(self, B, cons, spec, cls=None, pr=None, log=None):
        self.B, self.cons, self.spec = B, cons, spec
        self.cls = cls if cls is not None else NC.classify(B, cons, spec)
        self.pr = pr or EP.plan_params(cons)
        self.log = log or (lambda m: None)
        self.full()

    def full(self):
        B = self.B
        self.plan = EP.plan_escapes(B, self.cons, self.cls, self.pr, ignore_routing=True)
        self.phase = self.plan.stats.get("phase", 0)
        self.gr = GR.GlobalRoute(B, self.cons, self.cls, self.pr, self.plan, True, variant=(self.cons.get("plan") or {}).get("variant"))
        self.gr.build()
        self.gr.route_all()
        self._measure()

    def _measure(self):
        self.ns = self.gr.net_stats()
        self.findings = list(self.plan.findings) + GR.findings(self.gr, self.ns)
        self.score, self.comp = PL.score_of(self.pr, self.findings, self.ns, self.cls)
        from . import planrules as PRL
        _cz, kind = PRL.overflow_causes(self.gr, self.B, self.pr)
        self.comp["risk_nets"] = len(PRL.risk_nets(self.gr, kind))
        self.risk_frac = round(self.comp["risk_nets"] / max(1, len(self.gr.nets)), 3)
        self.key = PL.rank_key({"pr": self.pr, "findings": self.findings, "comp": self.comp, "risk_frac": self.risk_frac, "score": self.score})

    def snapshot(self):
        g = self.gr
        return {"pins": dict(self.plan.pins), "used": dict(self.plan.used), "findings": list(self.plan.findings), "extra": list(self.plan.extra), "groups": list(self.plan.groups),
                "gr": g.snapshot(), "nets": {n: (nt["terms"], nt.get("split")) for n, nt in g.nets.items()},
                "netset": dict(g.nets), "order": list(g.order), "capH": [list(a) for a in g.capH],
                "capV": [list(a) for a in g.capV], "vcap": list(g.vcap), "split": (set(g.split_pairs), dict(g.pair_origin)),
                "score": (self.score, self.comp, self.findings, self.ns, self.risk_frac, self.key)}

    def restore(self, s, moved):
        g = self.gr
        self.plan.pins, self.plan.used, self.plan.findings = s["pins"], s["used"], s["findings"]
        self.plan.extra, self.plan.groups = s["extra"], s["groups"]
        g.nets, g.order = dict(s["netset"]), list(s["order"])
        for n, (t, sp) in s["nets"].items():
            g.nets[n]["terms"] = t
            if sp is not None:
                g.nets[n]["split"] = sp
        g.split_pairs, g.pair_origin = set(s["split"][0]), dict(s["split"][1])
        # obstacles follow the (already restored) poses
        for ref in moved:
            g.add_part(ref)
        g.via_used = defaultdict(int)
        g._plan_vias()
        g._lane_fields()
        g.capH, g.capV, g.vcap = [list(a) for a in s["capH"]], [list(a) for a in s["capV"]], list(s["vcap"])
        g.restore(s["gr"])
        self.score, self.comp, self.findings, self.ns, self.risk_frac, self.key = s["score"]

    def update(self, moved, old_courts):
        """`moved` parts changed pose (B already updated); `old_courts` {ref: court before the move}."""
        A = self.escapes(moved, old_courts)
        self.gr.update_after_move(set(moved), A, True)
        self._measure()
        return A

    def escapes(self, moved, old_courts):
        """the escape half of `update` (about 10 ms): re-plan the moved parts and their neighbours; the global route is NOT
        touched (wlrefine.py checks every candidate with this and routes once per batch). -> the re-planned refs."""
        B = self.B
        A = set(moved)
        boxes = [old_courts[r] for r in moved] + [B["parts"][r]["court"] for r in moved]
        for ref, p in B["parts"].items():
            if ref in A or EP.is_mech(ref):
                continue
            c = p["court"]
            for b in boxes:
                if c[0] < b[2] + NEIGH_MM and b[0] < c[2] + NEIGH_MM and c[1] < b[3] + NEIGH_MM and b[1] < c[3] + NEIGH_MM:
                    A.add(ref)
                    break
        plan = self.plan
        taken = {ij for ij, k in plan.used.items() if k[0] not in A}
        sub = EP.plan_escapes(B, self.cons, self.cls, self.pr, ignore_routing=True, only_refs=A, phase=self.phase,
                              taken=taken, pnets=plan.plane)
        plan.pins = {k: v for k, v in plan.pins.items() if k[0] not in A}
        plan.used = {ij: k for ij, k in plan.used.items() if k[0] not in A}
        plan.pins.update(sub.pins)
        plan.used.update(sub.used)
        plan.extra = [e for e in plan.extra if e.get("part") not in A] + [e for e in sub.extra if e.get("part") in A]
        plan.groups = [g for g in plan.groups if g["ref"] not in A] + [g for g in sub.groups if g["ref"] in A]
        EP.rebuild_findings(B, self.pr, plan, True)
        return A


def key_less(a, b, eq=False):
    """Lexicographic (FAIL flag, hard failures, risk_frac, PLAN_SCORE) comparison with tolerances; eq: `a <= b`."""
    for x, y, tol in zip(a, b, (0.5, 0.5, 1e-9, 1e-6)):
        if x < y - tol:
            return True
        if x > y + tol:
            return False
    return eq


class PlanPlacer(RF.Refiner):
    def __init__(self, B, cons=None, spec=None, seed=0, log=None, time_limit=None, max_try=150, max_rounds=80, max_evals=400):
        super().__init__(B, cons, spec, seed, log, time_limit)
        # bounded by evaluations, not by the clock (deterministic); a wall-clock limit applies only when one is given explicitly
        self.max_evals, self.evals = int(max_evals), 0
        if time_limit is None:
            self.t_end = float("inf")
        self.max_try, self.max_rounds_ = max_try, max_rounds
        self.ps = PlanState(B, self.cons, spec, cls=self.cls, log=log)
        self.score0 = self.ps.score
        self.score = self.ps.score
        self.comp0 = dict(self.ps.comp)
        self.moves_log = []
        self.key_of = {m: k for k, ms in self.U.items() for m in ms}
        self.flip_wanted = set()
        self.groups = {n: list(g.get("refs", [])) for n, g in (((self.cons.get("plan") or {}).get("group")) or {}).items()}
        # support-part guard: a move may not push a support part further beyond its limit (SUPPORT_PART_FAR of the gate)
        self.sup_lim = {s["ref"]: (c["ic"], s["pad"], s["via"], s["limit"]) for c in self.cl for s in c["supports"] if s["sure"]}
        self.excess = self.support_excess()

    def support_excess(self):
        from . import clusters as CL
        P, tot = self.B["parts"], 0.0
        for ref, (ic, pad, via, lim) in self.sup_lim.items():
            sp, icp = P.get(ref), P.get(ic)
            if not sp or not icp:
                continue
            a = next((q for q in sp["pads"] if q["num"] == via), None)
            b = next((q for q in icp["pads"] if q["num"] == pad), None)
            if not a or not b:
                continue
            d = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
            if CL._pitch(icp) <= 0.5:
                d = max(0.0, d - CL.HALO_MM)
            tot += max(0.0, d - lim)
        return tot

    # ---- the one place a move is judged
    def out_of_budget(self):
        return self.evals >= self.max_evals or time.time() > self.t_end

    def try_move(self, kind, poses, reason="", slack=0.0, gain=0.0, flip=None, allow_equal=False):
        refs = list(poses)
        old = {r: self._pose(r) for r in refs}
        old_courts = {r: self.B["parts"][r]["court"] for r in refs}
        self._apply(poses)
        if flip:
            GM.flip(self.B["parts"][flip])
            self.legal.refresh(flip)
        if not self._legal(refs):
            self._undo(old, flip)
            return False
        ex = self.support_excess()
        if ex > self.excess + 1e-6:                       # would push a support part further beyond its limit
            self._undo(old, flip)
            return False
        snap = self.ps.snapshot()
        before = self.ps.score
        before_key = self.ps.key
        ov_before = {(f["part"], f["side"]) for f in self.ps.findings if f["code"] == "PLAN_ESCAPE_OVERSUBSCRIBED"}
        self.evals += 1
        self.ps.update(set(refs), old_courts)
        if key_less(self.ps.key, before_key, eq=allow_equal):
            self.score = self.ps.score
            self.excess = ex
            self.stats[kind] += 1
            if flip:
                self.flips ^= {flip}
            self.moves_log.append({"refs": refs, "kind": kind, "reason": reason, "before": before, "after": self.ps.score,
                                   "key_before": list(before_key), "key_after": list(self.ps.key),
                                   "resolved_oversub": sorted(f"{a}/{b}" for a, b in ov_before - {(f["part"], f["side"]) for f in self.ps.findings
                                                                                              if f["code"] == "PLAN_ESCAPE_OVERSUBSCRIBED"}),
                                   "from": {r: [round(v, 3) for v in old[r]] for r in refs},
                                   "to": {r: [round(v, 3) for v in self._pose(r)] for r in refs}})
            return True
        self._undo(old, flip)
        self.ps.restore(snap, set(refs))
        return False

    def _undo(self, old, flip):
        if flip:
            GM.flip(self.B["parts"][flip])
        self._apply(old)
        if flip:
            self.legal.refresh(flip)

    # ---- proposals ----------------------------------------------------------
    def _movable(self, ref):
        P = self.B["parts"]
        return ref in P and ref not in self.fixed and not P[ref].get("locked") and not EP.is_mech(ref)

    def _shift(self, ref, dx, dy, rot=0.0):
        """Poses for moving `ref` (its whole unit when it is the unit's head, else alone)."""
        P = self.B["parts"]
        k = self.key_of.get(ref, ref)
        if k == ref and ref in self.U and len(self.U[ref]) > 1:
            ms = self.U[ref]
            if any(not self._movable(m) for m in ms):
                return None
            return self._unit_pose(ms, ref, dx, dy, rot)
        x, y = P[ref]["pos"]
        return {ref: (x + dx, y + dy, P[ref]["angle"] + rot)}

    def proposals(self):
        P = self.B["parts"]
        groups, seen = [], set()
        cur = []

        def add(ref, dx, dy, rot, reason):
            if not self._movable(ref):
                return
            key = (ref, round(dx, 2), round(dy, 2), rot)
            if key in seen:
                return
            seen.add(key)
            poses = self._shift(ref, dx, dy, rot)
            if poses:
                cur.append((0, "plan", poses, reason))

        def away(ax, ay, bx, by, d):
            vx, vy = bx - ax, by - ay
            n = math.hypot(vx, vy) or 1.0
            return vx / n * d, vy / n * d

        for f in PL.rank(self.ps.findings)[:24]:
            cur = []
            groups.append(cur)
            c = f["code"]
            if c == "PLAN_PIN_SEALED":
                part, pad = f["part"], f["pin"]
                blk = (f.get("blocker") or "").split(".")[0]
                ax, ay = f["at"]
                if blk in P and blk != part:
                    bx, by = P[blk]["pos"]
                    ex, ey = P[part]["pos"]
                    for d in (0.3, 0.5, 1.0, 2.0):
                        dx, dy = away(ax, ay, bx, by, d)
                        add(blk, dx, dy, 0.0, f"{part}.{pad} sealed: {blk} steps {d:g} mm away from the pad")
                    for d in (0.3, 0.6, 1.0):
                        dx, dy = away(bx, by, ex, ey, d)
                        add(part, dx, dy, 0.0, f"{part}.{pad} sealed: {part} steps {d:g} mm back from {blk}")
                for r in (90.0, 180.0, 270.0):
                    add(part, 0.0, 0.0, r, f"{part}.{pad} sealed: rotate {part} by {r:g}")
            elif c == "PLAN_NO_VIA_SITE":
                part = f["part"]
                ax, ay = f["at"]
                near = sorted((math.hypot(p["pos"][0] - ax, p["pos"][1] - ay), r) for r, p in P.items()
                              if r != part and not EP.is_mech(r))[:4]
                for _d, r in near:
                    for d in (0.4, 0.8):
                        dx, dy = away(ax, ay, P[r]["pos"][0], P[r]["pos"][1], d)
                        add(r, dx, dy, 0.0, f"{part}.{f['pin']} has no via site: {r} steps {d:g} mm away")
                if near:
                    r = near[0][1]
                    for d in (0.4, 0.8):
                        dx, dy = away(P[r]["pos"][0], P[r]["pos"][1], P[part]["pos"][0], P[part]["pos"][1], d)
                        add(part, dx, dy, 0.0, f"{part}.{f['pin']} has no via site: {part} steps {d:g} mm from {r}")
            elif c == "PLAN_OVERFLOW" and f.get("cells_xy"):
                cx = sum(p[0] for p in f["cells_xy"]) / len(f["cells_xy"])
                cy = sum(p[1] for p in f["cells_xy"]) / len(f["cells_xy"])
                for r in f["parts"]:
                    for d in (1.0, 2.0):
                        dx, dy = away(cx, cy, P[r]["pos"][0], P[r]["pos"][1], d)
                        add(r, dx, dy, 0.0, f"overflow at ({cx:.0f}, {cy:.0f}): {r} steps {d:g} mm out of the channel")
                    if P[r]["n_pads"] >= 4:
                        for rr in (90.0, -90.0):
                            add(r, 0.0, 0.0, rr, f"overflow at ({cx:.0f}, {cy:.0f}): rotate {r} by {rr:g}")
                    k = self.key_of.get(r, r)
                    if k in self.U:
                        pull, _c, _t = self._pull(self.U[k])
                        if pull:
                            add(r, pull[0] * 1.0, pull[1] * 1.0, 0.0, f"overflow at ({cx:.0f}, {cy:.0f}): {r} toward its partners")
            elif c == "PLAN_ESCAPE_OVERSUBSCRIBED":
                part, side = f["part"], f["side"]
                for r in (90.0, 180.0, 270.0):
                    add(part, 0.0, 0.0, r, f"oversubscribed {part}/{side}: rotate {part} by {r:g}")
                if self.flip_cost is not None:
                    self.flip_wanted.add(part)
                g = next((g for g in self.ps.plan.groups if g["ref"] == part and g["side"] == side), None)
                if g:
                    xs, ys = [e[0] for e in g["edge"]], [e[1] for e in g["edge"]]
                    box = (min(xs) - 1.0, min(ys) - 1.0, max(xs) + 1.0, max(ys) + 1.0)
                    cx0, cy0 = P[part]["pos"]
                    for r, p in sorted(P.items()):
                        if r == part or EP.is_mech(r):
                            continue
                        cb = p["court"]
                        if cb[0] < box[2] and box[0] < cb[2] and cb[1] < box[3] and box[1] < cb[3]:
                            for d in (1.0, 2.0):
                                dx, dy = away(cx0, cy0, p["pos"][0], p["pos"][1], d)
                                add(r, dx, dy, 0.0, f"oversubscribed {part}/{side}: blocker {r} steps {d:g} mm out of the fan-out")
            elif c in ("PLAN_PART_IN_CORRIDOR", "PLAN_ZONING") and f.get("part"):
                r = f["part"]
                cx, cy = (f["cells_xy"][0] if f.get("cells_xy") else f["at"])
                for d in (1.0, 2.0, 3.0):
                    dx, dy = away(cx, cy, P[r]["pos"][0], P[r]["pos"][1], d)
                    add(r, dx, dy, 0.0, f"{c[5:].lower()} at ({cx:.0f}, {cy:.0f}): {r} steps {d:g} mm out")
                if f.get("other") and f["other"] in P:
                    o = f["other"]
                    for d in (2.0, 4.0):
                        dx, dy = away(P[r]["pos"][0], P[r]["pos"][1], P[o]["pos"][0], P[o]["pos"][1], -d)
                        add(r, dx, dy, 0.0, f"zoning: {r} steps {d:g} mm away from {o}")
            elif c == "PLAN_NET_OVER_BUDGET":
                nt = next((p for p in self.ps.gr.nets.values() if f["net"] in p["members"]), None)
                if nt:
                    refs = sorted({t["pad"].split(".")[0] for t in nt["terms"]})
                    if len(refs) >= 2:
                        over = max(0.5, f["value"] - f["limit"])
                        for r in refs:
                            o = [q for q in refs if q != r]
                            ox = sum(P[q]["pos"][0] for q in o) / len(o)
                            oy = sum(P[q]["pos"][1] for q in o) / len(o)
                            for d in (min(over, 12.0), min(over, 12.0) / 2, min(over, 12.0) / 4, 1.0):
                                dx, dy = away(P[r]["pos"][0], P[r]["pos"][1], ox, oy, d)
                                add(r, dx, dy, 0.0, f"{f['net']} over budget by {over:.1f} mm: {r} steps {d:.1f} mm toward its partners")
        # support parts beyond their limit
        cur = []
        groups.append(cur)
        for ref, (ic, pad) in sorted(self.sup.items()):
            icp = P.get(ic)
            if not icp:
                continue
            pin = next((q for q in icp["pads"] if q["num"] == pad), None)
            sp = P.get(ref)
            if not pin or not sp:
                continue
            d = math.hypot(sp["pos"][0] - pin["x"], sp["pos"][1] - pin["y"])
            if d > 4.0:
                dx, dy = away(sp["pos"][0], sp["pos"][1], pin["x"], pin["y"], min(1.0, d / 2))
                add(ref, dx, dy, 0.0, f"{ref} is {d:.1f} mm from {ic}.{pad}: steps toward it")
        out = []
        for i in range(max((len(g) for g in groups), default=0)):       # one candidate per finding in turn
            for g in groups:
                if i < len(g):
                    out.append(g[i])
        return out

    def spread_pass(self, grow):
        """Board grown by grow = (l, r, t, b): re-seat the movable units along the new room, in proportion to their distance from the
        opposite edge (units next to the grown edge move most), each kept when legal and PLAN_SCORE does not get worse."""
        gl, gr_, gt, gb = grow
        x0, y0, x1, y1 = self.B["bbox"]
        W, H = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
        P = self.B["parts"]
        n, tot = 0, 0.0
        for k in sorted((k for k in self.U if self._movable(k)), key=lambda k: P[k]["pos"][0] + P[k]["pos"][1]):
            if self.out_of_budget():
                break
            x, y = P[k]["pos"]
            dx = -gl * (x1 - x) / W + gr_ * (x - x0) / W
            dy = -gt * (y1 - y) / H + gb * (y - y0) / H
            if abs(dx) < 0.05 and abs(dy) < 0.05:
                continue
            poses = self._shift(k, dx, dy)
            if poses and self.try_move("spread", poses, f"spread into the new room: {k} by ({dx:+.1f}, {dy:+.1f})", allow_equal=True):
                n += 1
                tot += math.hypot(dx, dy)
        return n, tot

    # ---- rigid group moves ------------------------------------------------------------------------------------------
    def group_of(self, ref):
        """The rigid group of a part: an explicit `[plan.group.NAME] refs = [...]`, else its unit (IC + support parts), else the part alone."""
        for n, refs in self.groups.items():
            if ref in refs:
                return sorted(r for r in refs if r in self.B["parts"])
        k = self.key_of.get(ref, ref)
        return sorted(self.U.get(k, [ref])) if ref in self.U.get(k, []) else [ref]

    def rigid(self, refs, anchor, new_xy, d):
        """Poses of the whole group translated so `anchor` lands on new_xy and rotated by d about it (relative placement kept)."""
        P = self.B["parts"]
        ax, ay = P[anchor]["pos"]
        out = {}
        for r in refs:
            x, y = P[r]["pos"]
            rx, ry = GM.rot(x - ax, y - ay, d)
            out[r] = (new_xy[0] + rx, new_xy[1] + ry, P[r]["angle"] + d)
        return out

    def group_candidates(self, f):
        """Re-seat candidates for an over-budget net: pull the movable end group next to the (possibly fixed) other end; when both ends can move,
        also move both to free sites along the board edge.  -> [(poses, reason)] best first (closest to the target first)."""
        nt = next((n for n in self.ps.gr.nets.values() if f["net"] in n["members"]), None)
        if not nt:
            return
        P = self.B["parts"]
        ends = []
        for t in nt["terms"]:
            r = t["pad"].split(".")[0]
            if r in P and r not in ends:
                ends.append(r)
        groups = []
        for r in ends:
            g = tuple(self.group_of(r))
            if g not in [x[0] for x in groups]:
                groups.append((g, r))
        if len(groups) < 2:
            return
        out = []
        movable = lambda g: all(self._movable(m) for m in g)
        sizes = {g: len(g) for g, _r in groups}
        for (ga, ra) in groups:
            if not movable(ga) or sizes[ga] > 12:
                continue
            for (gb, rb) in groups:
                if gb == ga:
                    continue
                tx, ty = P[rb]["pos"]
                x0, y0, x1, y1 = self.B["bbox"]
                # every grid site of the board (2 mm), nearest to the other end first: the legaliser finds the free areas
                gx = [x0 + 2 + 2.0 * i for i in range(int((x1 - x0 - 4) / 2.0) + 1)]
                gy = [y0 + 2 + 2.0 * j for j in range(int((y1 - y0 - 4) / 2.0) + 1)]
                for x in gx:
                    for y in gy:
                        dist = math.hypot(x - tx, y - ty)
                        if 2.0 < dist <= 40.0:
                            for d in (0.0, 90.0, 180.0, 270.0):
                                out.append((dist, (ga, ra, x, y, d, rb), None))
        # both ends movable: the pair goes to a free site along the board edge
        if len(groups) == 2 and all(movable(g) and sizes[g] <= 12 for g, _r in groups):
            (ga, ra), (gb, rb) = groups
            x0, y0, x1, y1 = self.B["bbox"]
            inset = 5.0
            sites = []
            for t in range(int((x1 - x0) // 6) + 1):
                sites += [(x0 + inset + 6 * t, y0 + inset), (x0 + inset + 6 * t, y1 - inset)]
            for t in range(int((y1 - y0) // 6) + 1):
                sites += [(x0 + inset, y0 + inset + 6 * t), (x1 - inset, y0 + inset + 6 * t)]
            for si, (sx, sy) in enumerate(sorted(set(sites))):
                for d in (0.0, 90.0, 180.0, 270.0):
                    for rr, ang in ((7.0, 0), (7.0, 90), (7.0, 180), (7.0, 270)):
                        a = math.radians(ang)
                        poses = self.rigid(list(ga), ra, (sx, sy), d)
                        poses.update(self.rigid(list(gb), rb, (sx + rr * math.cos(a), sy + rr * math.sin(a)), 0.0))
                        out.append((rr + 100.0, poses, f"group pair move: {ra} group and {rb} group to the edge site ({sx:.0f}, {sy:.0f})"))
        out.sort(key=lambda t: t[0])
        for _k, p, r in out:
            if r is None:
                ga, ra, x, y, d, rb = p
                yield self.rigid(list(ga), ra, (x, y), d), f"group move: {','.join(ga[:4])}{'...' if len(ga) > 4 else ''} re-seated at ({x:.0f}, {y:.0f}) rot {d:g}, {math.hypot(x - P[rb]['pos'][0], y - P[rb]['pos'][1]):.0f} mm from {rb} (net {f['net']} over budget)"
            else:
                yield p, r

    def try_groups(self, findings, max_eval=30):
        """Try the re-seat candidates of every hard over-budget net; plan evaluations are bounded (`max_eval`), illegal sites cost only a legality test."""
        start = self.evals
        for f in findings:
            if f["code"] != "PLAN_NET_OVER_BUDGET" or f.get("cls") not in GR.HARD or self.out_of_budget():
                continue
            for poses, reason in self.group_candidates(f):
                if self.evals - start >= max_eval or self.out_of_budget():
                    break
                if self.try_move("group", poses, reason):
                    return True
        return False

    def rotation_pass(self, min_pads=6):
        """A1: for every movable unit with >= 6 pads, or any part with a finding on it, big first: try the other three rotations (and the
        other side when `[placement.side] flip_cost` allows) and keep each that lowers PLAN_SCORE (the score carries pin direction,
        escape headroom and the budget terms)."""
        P = self.B["parts"]
        hit = {f.get("part") for f in self.ps.findings if f.get("part")}
        order = sorted((k for k, ms in self.U.items() if self._movable(k) and (P[k]["n_pads"] >= min_pads or k in hit)),
                       key=lambda k: (-P[k]["n_pads"], k))
        n = 0
        for k in order:
            if self.out_of_budget():
                break
            for rot in (90.0, 180.0, 270.0):
                poses = self._shift(k, 0.0, 0.0, rot)
                if poses and self.try_move("rotate", poses, f"A1 rotation: {k} by {rot:g} (pin direction / escape headroom)"):
                    n += 1
            if self.flip_cost is not None:
                self.flip(k)
        return n

    def run(self):
        P = self.B["parts"]
        for _ in range(3):                                   # rigid group re-seats first: they are the moves that change a hard failure
            if not self.try_groups(PL.rank(self.ps.findings)):
                break
            self.log(f"group move kept, PLAN_SCORE {self.score:.1f}")
        if getattr(self, "spread", None):
            n, tot = self.spread_pass(self.spread)
            self.log(f"spread pass: {n} unit(s) re-seated, mean {tot / max(n, 1):.1f} mm")
        if getattr(self, "do_rotation_pass", True):
            self.log(f"A1 rotation pass: {self.rotation_pass()} rotation(s) kept, PLAN_SCORE {self.score:.1f}")
        self.log(f"plan objective: PLAN_SCORE {self.score:.1f}, {len(self.U)} unit(s), {len(self.fixed)} fixed")
        rounds = 0
        while rounds < self.max_rounds_ and not self.out_of_budget():
            rounds += 1
            for fr in sorted(self.flip_wanted):                       # oversubscribed group: the other side, when the side policy allows
                self.flip(fr)
            self.flip_wanted = set()
            props = self.proposals()
            acc = False
            if self.try_groups(PL.rank(self.ps.findings)):
                self.log(f"  round {rounds}: group move -> PLAN_SCORE {self.score:.1f}")
                continue
            for _i, kind, poses, reason in props[:self.max_try]:
                if self.out_of_budget():
                    break
                if self.try_move(kind, poses, reason):
                    acc = True
                    self.log(f"  round {rounds}: {reason} -> PLAN_SCORE {self.score:.1f}")
                    break
            if not acc:
                break
        self.reverted, whole = LGL.self_audit(self.B, self.cons, self.fixed, self.input_issues, self.origin_full, self.log)
        if self.reverted:
            self.flips = {r for r in P if P[r]["side"] != self.origin_full[r][3]}
        norm = lambda a: (a + 180) % 360 - 180
        base = self.origin
        moves = {r: (round(x, 4), round(y, 4), a) for r, (x, y, a) in ((r, self._pose(r)) for r in P)
                 if abs(x - base[r][0]) > 1e-4 or abs(y - base[r][1]) > 1e-4 or abs(norm(a - base[r][2])) > 1e-6}
        for r in self.flips:
            moves[r] = (round(P[r]["pos"][0], 4), round(P[r]["pos"][1], 4), P[r]["angle"])
        return moves


def plan_place(B, cons=None, spec=None, seed=0, log=print, time_limit=None, rotation=True, spread=None, max_evals=None):
    """-> (moves {ref: (x, y, angle)}, stats).  Mutates B.  Moves are accepted on the incremental lexicographic key; afterwards a fresh FULL
    plan is run and, if it is worse than the full plan of the input, EVERYTHING is reverted (moves = {}, stats["reverted_to_input"])."""
    cons = cons or {}
    pl = cons.get("placement") or {}
    R0 = PL.run(B, cons, spec, ignore_routing=True)
    r = PlanPlacer(B, cons, spec, seed, log, time_limit, max_evals=int(max_evals if max_evals is not None else pl.get("max_evals", 400)))
    r.do_rotation_pass = rotation
    r.spread = spread
    mv = r.run()
    R1 = PL.run(B, cons, spec, ignore_routing=True)
    k0, k1 = PL.rank_key(R0), PL.rank_key(R1)
    reverted = key_less(k0, k1)
    if reverted:
        for ref in sorted(r.flips):
            GM.flip(B["parts"][ref])
        r._apply(r.origin)
        mv, r.flips = {}, set()
        log(f"full plan after the moves is worse than the input ({k1} vs {k0}): reverted to the input")
    return mv, {"score_before": R0["score"], "score_after": r.score, "score_confirmed": R0["score"] if reverted else R1["score"],
                "key_input": list(k0), "key_final_full": list(k1), "reverted_to_input": reverted, "evals": r.evals,
                "components_before": R0["comp"], "components_after": R0["comp"] if reverted else R1["comp"], "kept": dict(r.stats),
                "flips": sorted(r.flips), "reverted": r.reverted, "log": [] if reverted else r.moves_log}
