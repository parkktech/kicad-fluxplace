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
        self.gr = GR.GlobalRoute(B, self.cons, self.cls, self.pr, self.plan, True)
        self.gr.build()
        self.gr.route_all()
        self._measure()

    def _measure(self):
        self.ns = self.gr.net_stats()
        self.findings = list(self.plan.findings) + GR.findings(self.gr, self.ns)
        self.score, self.comp = PL.score_of(self.pr, self.findings, self.ns, self.cls)

    def snapshot(self):
        g = self.gr
        return {"pins": dict(self.plan.pins), "used": dict(self.plan.used), "findings": list(self.plan.findings), "extra": list(self.plan.extra), "groups": list(self.plan.groups),
                "gr": g.snapshot(), "nets": {n: (nt["terms"], nt.get("split")) for n, nt in g.nets.items()},
                "netset": dict(g.nets), "order": list(g.order), "capH": [list(a) for a in g.capH],
                "capV": [list(a) for a in g.capV], "vcap": list(g.vcap), "split": (set(g.split_pairs), dict(g.pair_origin)),
                "score": (self.score, self.comp, self.findings, self.ns)}

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
        self.score, self.comp, self.findings, self.ns = s["score"]

    def update(self, moved, old_courts):
        """`moved` parts changed pose (B already updated); `old_courts` {ref: court before the move}."""
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
        self.gr.update_after_move(set(moved), A, True)
        self._measure()
        return A


class PlanPlacer(RF.Refiner):
    def __init__(self, B, cons=None, spec=None, seed=0, log=None, time_limit=None, max_try=150, max_rounds=80):
        super().__init__(B, cons, spec, seed, log, time_limit)
        self.max_try, self.max_rounds_ = max_try, max_rounds
        self.ps = PlanState(B, self.cons, spec, cls=self.cls, log=log)
        self.score0 = self.ps.score
        self.score = self.ps.score
        self.comp0 = dict(self.ps.comp)
        self.moves_log = []
        self.key_of = {m: k for k, ms in self.U.items() for m in ms}
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
    def try_move(self, kind, poses, reason="", slack=0.0, gain=0.0, flip=None):
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
        self.ps.update(set(refs), old_courts)
        if self.ps.score < before - 1e-6:
            self.score = self.ps.score
            self.excess = ex
            self.stats[kind] += 1
            if flip:
                self.flips ^= {flip}
            self.moves_log.append({"refs": refs, "kind": kind, "reason": reason, "before": before, "after": self.ps.score,
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

    def run(self):
        P = self.B["parts"]
        self.log(f"plan objective: PLAN_SCORE {self.score:.1f}, {len(self.U)} unit(s), {len(self.fixed)} fixed")
        t_end = self.t_end
        rounds = 0
        while rounds < self.max_rounds_ and time.time() < t_end:
            rounds += 1
            props = self.proposals()
            acc = False
            for _i, kind, poses, reason in props[:self.max_try]:
                if time.time() > t_end:
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


def plan_place(B, cons=None, spec=None, seed=0, log=print, time_limit=None):
    """-> (moves {ref: (x, y, angle)}, stats).  Mutates B."""
    r = PlanPlacer(B, cons, spec, seed, log, time_limit)
    mv = r.run()
    return mv, {"score_before": r.score0, "score_after": r.score, "components_before": r.comp0,
                "components_after": r.ps.comp, "kept": dict(r.stats), "flips": sorted(r.flips), "reverted": r.reverted,
                "log": r.moves_log}
