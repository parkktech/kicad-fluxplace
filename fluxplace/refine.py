"""Local refinement of an already good, dense placement: `place --strategy routable --refine`.

Starts from the input board and only makes LOCAL moves, each accepted only when the gate's single
score (preroute.score) improves AND no legality violation (legality.Legal) is added:

  nudge    a unit (IC + its support parts, or a single part) by <= 3 mm: toward the weighted centroid of
           what it connects to first, then the 8 compass directions, steps 3 / 1.5 / 0.75 mm
  rotate   a unit by +90 or -90 degrees about its IC
  slide    a support part along its IC's edge (tangent to the edge its pin is on), 0.5 / 1 / 2 mm
  swap     two same-footprint parts (same side), when each is pulled toward the other's seat
  swapc    two units of similar size (courtyard area within 25 %), same condition

A cheap local filter (class-weighted length plus budget excess of the nets the move touches) discards
hopeless candidates before the full score is computed. First improvement is taken; passes repeat until a
whole pass accepts nothing, `[placement] time_limit_s` (600) runs out or `max_passes` (30) is reached.
Deterministic: candidates are visited in an order shuffled by `--seed` (default 0) from sorted keys.
Fixed parts, locked parts and mounting hardware never move.
"""
import math
import random
import time
from collections import defaultdict

from . import clusters as CL
from . import geom as GM
from . import legality as LGL
from . import netclass as NC
from . import preroute as PR

NUDGES = (3.0, 1.5, 0.75)
SLIDES = (0.5, 1.0, 2.0)
SIM = 0.25
EPS = 0.05
DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1), (0.7071, 0.7071), (0.7071, -0.7071), (-0.7071, 0.7071), (-0.7071, -0.7071)]


class Refiner:
    def __init__(self, B, cons=None, spec=None, seed=0, log=None, time_limit=None):
        from . import routable as RB
        self.B, self.cons, self.spec = B, cons or {}, spec
        self.log = log or (lambda m: None)
        pl = self.cons.get("placement") or {}
        self.t_end = time.time() + float(time_limit if time_limit is not None else pl.get("time_limit_s", 600))
        self.max_passes = int(pl.get("max_passes", 30))
        self.rng = random.Random(seed)
        self.cls = NC.classify(B, self.cons, spec)
        self.fixed = RB._fixed_set(B, self.cons)
        self.legal = LGL.Legal(B, self.cons, self.fixed)
        self.base = self._triples(self.B["parts"])
        self.pads = NC.net_pads(B)
        self.by_ref = defaultdict(set)
        for n, ps in self.pads.items():
            for p in ps:
                self.by_ref[p["ref"]].add(n)
        cl, _ = CL.build(B, self.cons, spec)
        self.cl = cl
        self.U = RB.units(B, cl, self.fixed)
        self.sup = {}                                   # support ref -> (ic, pad number)
        for c in cl:
            for s in c["supports"]:
                self.sup[s["ref"]] = (c["ic"], s["pad"])
        self.origin = {r: self._pose(r) for r in B["parts"]}
        self.score = PR.run(B, self.cons, spec)[1]["score"]
        self.score0 = self.score
        self.stats = defaultdict(int)

    # ---- state ----------------------------------------------------------
    def _pose(self, r):
        p = self.B["parts"][r]
        return (p["pos"][0], p["pos"][1], p["angle"])

    def _triples(self, refs):
        return {(r, k, str(d)) for r in refs for k, d in self.legal.part_issues(r)}

    def _apply(self, poses):
        for r, pose in poses.items():
            GM.place(self.B["parts"][r], *pose)
            self.legal.refresh(r)

    def _legal(self, refs):
        return self._triples(refs) <= self.base

    def local(self, refs):
        nets = set().union(*(self.by_ref[r] for r in refs)) if refs else set()
        t = 0.0
        for n in nets:
            info = self.cls.get(n)
            if not info or info["weight"] <= 0:
                continue
            ln = NC.mst_length([NC.pad_xy(p) for p in self.pads[n]])
            lim = NC.effective_budget(info)
            t += info["weight"] * ln
            if lim is not None and ln > lim:
                t += (PR.W_HARD if info["class"] in PR.HARD else PR.W_SOFT) * (ln - lim)
        return t

    def try_move(self, kind, poses, slack=0.5):
        """Apply `poses`; keep when legal and the full score improves. True when kept."""
        refs = list(poses)
        old = {r: self._pose(r) for r in refs}
        l0 = self.local(refs)
        self._apply(poses)
        ok = False
        if self._legal(refs) and self.local(refs) <= l0 + slack:
            sc = PR.run(self.B, self.cons, self.spec)[1]["score"]
            if sc < self.score - EPS:
                self.score, ok = sc, True
        if ok:
            self.stats[kind] += 1
        else:
            self._apply(old)
        return ok

    # ---- candidate generators -------------------------------------------
    def _unit_pose(self, ms, key, dx, dy, d):
        P = self.B["parts"]
        ox, oy = P[key]["pos"]
        out = {}
        for r in ms:
            p = P[r]
            rx, ry = GM.rot(p["pos"][0] - ox, p["pos"][1] - oy, d)
            out[r] = (ox + rx + dx, oy + ry + dy, p["angle"] + d)
        return out

    def _pull(self, ms):
        """Unit vector toward the weighted centroid of the unit's external partners (or None)."""
        mem = set(ms)
        sx = sy = sw = 0.0
        ox = oy = 0.0
        for r in ms:
            p = self.B["parts"][r]
            ox += p["pos"][0]
            oy += p["pos"][1]
        ox, oy = ox / len(ms), oy / len(ms)
        for n in set().union(*(self.by_ref[r] for r in ms)):
            info = self.cls.get(n)
            if not info or info["weight"] <= 0:
                continue
            for q in self.pads[n]:
                if q["ref"] not in mem:
                    sx += info["weight"] * q["x"]
                    sy += info["weight"] * q["y"]
                    sw += info["weight"]
        if not sw:
            return None, (ox, oy), None
        tx, ty = sx / sw, sy / sw
        d = math.hypot(tx - ox, ty - oy)
        return ((tx - ox) / d, (ty - oy) / d) if d > 1e-6 else None, (ox, oy), (tx, ty)

    def nudge(self, k):
        ms = self.U[k]
        pull, _c, _t = self._pull(ms)
        dirs = ([pull] if pull else []) + DIRS
        for st in NUDGES:
            for ux, uy in dirs:
                if self.try_move("nudge", self._unit_pose(ms, k, ux * st, uy * st, 0.0)):
                    return True
        return False

    def rotate(self, k):
        ms = self.U[k]
        for d in (90.0, -90.0):
            if self.try_move("rotate", self._unit_pose(ms, k, 0.0, 0.0, d)):
                return True
        return False

    def slide(self, ref):
        ic, pad = self.sup[ref]
        icp = self.B["parts"][ic]
        pin = next((p for p in icp["pads"] if p["num"] == pad), None)
        if not pin:
            return False
        ox, oy = CL._outward(icp, pin)
        tx, ty = -oy, ox
        x, y, a = self._pose(ref)
        for st in SLIDES:
            for s in (1, -1):
                if self.try_move("slide", {ref: (x + tx * st * s, y + ty * st * s, a)}):
                    return True
        return False

    def _area(self, ms):
        r = [self.B["parts"][m]["court"] for m in ms]
        return (max(c[2] for c in r) - min(c[0] for c in r)) * (max(c[3] for c in r) - min(c[1] for c in r))

    def swaps(self):
        """Part swaps and unit swaps where each side is pulled toward the other's seat."""
        P = self.B["parts"]
        tgt = {}
        for k, ms in self.U.items():
            _d, c, t = self._pull(ms)
            if t:
                tgt[k] = (c, t)

        def wants(a, b):
            ca, ta = tgt[a]
            cb, tb = tgt[b]
            return (math.hypot(ta[0] - cb[0], ta[1] - cb[1]) < math.hypot(ta[0] - ca[0], ta[1] - ca[1]) - 0.5
                    and math.hypot(tb[0] - ca[0], tb[1] - ca[1]) < math.hypot(tb[0] - cb[0], tb[1] - cb[1]) - 0.5)

        keys = sorted(tgt)
        self.rng.shuffle(keys)
        done = False
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                if time.time() > self.t_end:
                    return done
                ma, mb = self.U[a], self.U[b]
                if len(ma) == 1 and len(mb) == 1:
                    pa, pb = P[a], P[b]
                    if pa["fp"] != pb["fp"] or pa["side"] != pb["side"] or pa["n_pads"] != pb["n_pads"]:
                        continue
                    kind = "swap"
                elif len(ma) > 1 or len(mb) > 1:
                    if not (1 - SIM) <= self._area(ma) / max(self._area(mb), 1e-6) <= 1 + SIM:
                        continue
                    kind = "swapc"
                else:
                    continue
                if not wants(a, b):
                    continue
                pa0, pb0 = P[a]["pos"], P[b]["pos"]
                poses = {}
                poses.update(self._unit_pose(ma, a, pb0[0] - pa0[0], pb0[1] - pa0[1], 0.0))
                poses.update(self._unit_pose(mb, b, pa0[0] - pb0[0], pa0[1] - pb0[1], 0.0))
                if self.try_move(kind, poses):
                    done = True
        return done

    # ---- driver ---------------------------------------------------------
    def run(self):
        P = self.B["parts"]
        movable = sorted(k for k, ms in self.U.items() if all(
            m not in self.fixed and not P[m].get("locked") for m in ms))
        self.log(f"refine: score {self.score:.0f}, {len(movable)} movable unit(s), seed deterministic")
        for ps in range(self.max_passes):
            acc0 = sum(self.stats.values())
            order = movable[:]
            self.rng.shuffle(order)
            for k in order:
                if time.time() > self.t_end:
                    self.log("wall-clock limit reached")
                    break
                self.nudge(k)
                self.rotate(k)
                for m in self.U[k]:
                    if m in self.sup and m not in self.fixed and not P[m].get("locked") and m != k:
                        self.slide(m)
            else:
                self.swaps()
            gained = sum(self.stats.values()) - acc0
            self.log(f"pass {ps + 1}: {gained} move(s) kept, score {self.score:.0f} "
                     f"({', '.join(f'{k} {v}' for k, v in sorted(self.stats.items()))})")
            if not gained or time.time() > self.t_end:
                break
        base = self.origin
        norm = lambda a: (a + 180) % 360 - 180
        moves = {r: (round(x, 4), round(y, 4), a) for r, (x, y, a) in ((r, self._pose(r)) for r in P)
                 if abs(x - base[r][0]) > 1e-4 or abs(y - base[r][1]) > 1e-4 or abs(norm(a - base[r][2])) > 1e-6}
        new = sorted(self._triples(P) - self.base)
        if new:
            raise RuntimeError(f"refine produced {len(new)} new legality violation(s): {new[:5]}")
        return moves


def refine(B, cons=None, spec=None, seed=0, log=print, time_limit=None):
    """-> (moves {ref: (x, y, angle)}, stats dict). Mutates B."""
    r = Refiner(B, cons, spec, seed, log, time_limit)
    mv = r.run()
    return mv, {"score_before": r.score0, "score_after": r.score, "kept": dict(r.stats)}
