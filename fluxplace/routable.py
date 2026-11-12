"""Stage 4: connection-driven ("routable") placement on the text board model.

  1. anchors (locked parts, [lock.at], connectors/modules from anchors.detect, mounting
     hardware) stay where they are; misoriented ones are first turned to their best
     rotation (anchors.moves_for_best, rotating groups included);
  2. far support parts are re-seated around their ICs (clusters.reseat), so a unit is
     an IC with its support parts held RIGIDLY; every other part is a unit of one;
  3. each unit goes to the class-weighted centroid of what it connects to (rails
     excluded), is rotated 0/90/180/270 to make its pins face their partners
     (weighted length + crossings among its own nets), and parts of a signal chain
     between two fixed anchors (A -> B -> C over 2-pad nets) are ordered monotonically
     along the line between the chain's end anchors; repeat to convergence;
  4. legalise.legalise removes the overlaps with channels, support parts are re-seated
     again, and the result is verified (it raises instead of writing a bad board).
"""
import math
from collections import defaultdict

from . import anchors as AN
from . import clusters as CL
from . import geom as GM
from . import legalise as LG
from . import netclass as NC

_MECH_PREFIX = ("MH", "MK", "FID", "TP", "H")
XING_MM = 3.0       # one ratsnest crossing among a unit's own nets costs this much length


def _fixed_set(B, cons):
    fixed = {r for r, p in B["parts"].items() if p.get("locked")}
    fixed |= set(((cons or {}).get("lock") or {}).get("at", {}))
    fixed |= {r for r in B["parts"] if r.startswith(_MECH_PREFIX) and r[len(r.rstrip("0123456789")):].isdigit()
              and r.rstrip("0123456789") in _MECH_PREFIX}
    fixed |= {r for a in AN.detect(B, cons) for r in a["refs"]}
    fixed |= set(((cons or {}).get("placement") or {}).get("fixed", []))
    return fixed


def units(B, cl, fixed):
    """{key: [refs]} — IC + confident support parts as one rigid unit, others alone."""
    out, taken = {}, set()
    for c in cl:
        if c["ic"] in fixed:
            continue
        members = [c["ic"]] + [s["ref"] for s in c["supports"]
                               if s["sure"] and s["ref"] not in fixed and s["ref"] not in taken]
        out[c["ic"]] = members
        taken |= set(members)
    for r in sorted(B["parts"]):
        if r not in taken and r not in fixed:
            out[r] = [r]
    return out


def chains(B, cls, fixed, unit_of):
    """[[fixed_a, unit, unit, ..., fixed_b]] — paths over 2-pad non-rail nets whose
    interior units each have exactly two such neighbours."""
    nbr = defaultdict(set)
    for net, ps in NC.net_pads(B).items():
        info = cls.get(net)
        if not info or info["class"] == "rail" or len(ps) != 2:
            continue
        a, b = (unit_of.get(p["ref"], p["ref"]) for p in ps)
        if a != b:
            nbr[a].add(b)
            nbr[b].add(a)
    out, seen = [], set()
    fixed_keys = {k for k in nbr if k in fixed}
    for a in sorted(fixed_keys):
        for first in sorted(nbr[a]):
            if first in fixed or len(nbr[first]) != 2 or first in seen:
                continue
            path, prev, cur = [a, first], a, first
            while True:
                nxt = [n for n in nbr[cur] if n != prev]
                if len(nxt) != 1:
                    break
                prev, cur = cur, nxt[0]
                path.append(cur)
                if cur in fixed or len(nbr[cur]) != 2 or cur in path[:-1]:
                    break
            if path[-1] in fixed and path[-1] != a and len(path) >= 3:
                out.append(path)
                seen |= set(path[1:-1])
    return out


class Placer:
    def __init__(self, B, cons=None, spec=None, log=None):
        self.B, self.cons, self.spec, self.log = B, cons or {}, spec, log or (lambda m: None)
        self.cls = NC.classify(B, self.cons, spec)
        self.fixed = _fixed_set(B, self.cons)
        self.moves = {}          # ref -> (x, y, ang): everything this run changed

    # ---- helpers on the live model --------------------------------------
    def commit(self, ref, x, y, ang):
        GM.place(self.B["parts"][ref], x, y, ang)
        self.moves[ref] = (round(x, 4), round(y, 4), ang)

    def _nets(self):
        self.pads = NC.net_pads(self.B)

    def unit_pose(self, members, ic, dx, dy, d):
        """{ref: (x, y, ang, pads)} for a rigid unit shifted by (dx, dy) and turned d about its IC origin."""
        P = self.B["parts"]
        ox, oy = P[ic]["pos"]
        out = {}
        for r in members:
            p = P[r]
            rx, ry = GM.rot(p["pos"][0] - ox, p["pos"][1] - oy, d)
            x, y, a = ox + rx + dx, oy + ry + dy, p["angle"] + d
            out[r] = (x, y, a, GM.moved(p, x, y, a)[0])
        return out

    def pulls(self, members, pose=None):
        """[(weight, own pin centroid, other pads centroid, net)] for the unit's external nets."""
        mem, res = set(members), []
        by = defaultdict(list)
        P = self.B["parts"]
        for r in members:
            for q in (pose[r][3] if pose else P[r]["pads"]):
                if q["net"]:
                    by[q["net"]].append(q)
        for n, own in by.items():
            info = self.cls.get(n)
            if not info or info["weight"] <= 0:
                continue
            oth = [p for p in self.pads[n] if p["ref"] not in mem]
            if not oth:
                continue
            c = (sum(p["x"] for p in oth) / len(oth), sum(p["y"] for p in oth) / len(oth))
            o = (sum(p["x"] for p in own) / len(own), sum(p["y"] for p in own) / len(own))
            res.append((info["weight"] / max(1, len(self.pads[n]) - 1), o, c, n))
        return res

    @staticmethod
    def _xings(pulls):
        n = 0
        for i in range(len(pulls)):
            for j in range(i + 1, len(pulls)):
                if PRX(pulls[i][1], pulls[i][2], pulls[j][1], pulls[j][2]):
                    n += 1
        return n

    def cost(self, members, ic, dx, dy, d):
        pose = self.unit_pose(members, ic, dx, dy, d)
        pl = self.pulls(members, pose)
        return (sum(w * math.hypot(c[0] - o[0], c[1] - o[1]) for w, o, c, _ in pl)
                + XING_MM * sum(w for w, *_ in pl) / max(1, len(pl)) * self._xings(pl))

    # ---- the passes -------------------------------------------------------
    def turn_anchors(self):
        res = AN.evaluate(self.B, self.cons, self.spec, self.cls)
        keep = set(((self.cons.get("placement") or {}).get("fixed", [])))
        mv = AN.moves_for_best(self.B, res, self.cons, self.spec,
                               float(((self.cons.get("preroute") or {}).get("anchor_tolerance_pct", 15.0))),
                               skip=keep)
        for r, (x, y, a) in mv.items():
            self.moves[r] = (x, y, a)
        if mv:
            self.log(f"anchors turned: {', '.join(sorted(mv))}")
            self._nets()
        return mv

    def seat_support(self):
        cl, _ = CL.build(self.B, self.cons, self.spec)
        x0, y0, x1, y1 = self.B["bbox"]
        # macro cells: every confident support part laid out beside its own IC on its own
        # (overlaps with other parts are legalise's job, with the unit moving as one body)
        mv = CL.reseat(self.B, [c for c in cl if c["ic"] not in self.fixed], everything=True,
                       bounds=(x0 + .2, y0 + .2, x1 - .2, y1 - .2), isolate=True)
        self.moves.update(mv)
        self._nets()
        return cl

    def converge(self, cl, rounds=3, sweeps=25, tol=0.25):
        self._nets()
        P = self.B["parts"]
        U = units(self.B, cl, self.fixed)
        unit_of = {r: k for k, ms in U.items() for r in ms}
        ch = chains(self.B, self.cls, self.fixed, unit_of)
        self.log(f"{len(U)} movable unit(s), {len(ch)} signal chain(s)")
        order = sorted(U, key=lambda k: (-len(U[k]), k))
        for rnd in range(rounds):
            for sw in range(sweeps):
                big = 0.0
                for k in order:
                    ms = U[k]
                    pl = self.pulls(ms)
                    if not pl:
                        continue
                    sw_ = sum(w for w, *_ in pl)
                    dx = sum(w * (c[0] - o[0]) for w, o, c, _ in pl) / sw_
                    dy = sum(w * (c[1] - o[1]) for w, o, c, _ in pl) / sw_
                    dx, dy = 0.7 * dx, 0.7 * dy
                    if math.hypot(dx, dy) < 1e-3:
                        continue
                    for r, (x, y, a, _) in self.unit_pose(ms, k, dx, dy, 0.0).items():
                        self.commit(r, x, y, a)
                    big = max(big, math.hypot(dx, dy))
                    self._nets()
                self._order_chains(ch, U)
                if big < tol:
                    break
                if sw % 6 == 5:
                    self.legalise(U, strict=False)       # keep the pull from piling parts up
            # facing: rotate multi-pin units to the best of 0/90/180/270 (+ stay)
            turned = 0
            for k in order:
                ms = U[k]
                if k in self.fixed or P[k]["n_pads"] < 5:
                    continue
                base = self.cost(ms, k, 0, 0, 0.0)
                best = min(((self.cost(ms, k, 0, 0, d), d) for d in (90.0, 180.0, 270.0)), default=(base, 0.0))
                if best[0] < 0.92 * base:
                    for r, (x, y, a, _) in self.unit_pose(ms, k, 0, 0, best[1]).items():
                        self.commit(r, x, y, a)
                    self._nets()
                    turned += 1
            self.log(f"round {rnd + 1}: {turned} unit(s) turned")
            self.legalise(U)
        return U

    def _order_chains(self, chains_, U):
        """Order the interior of each chain monotonically along the end-to-end line."""
        P = self.B["parts"]
        for path in chains_:
            a, b = P[path[0]]["pos"], P[path[-1]]["pos"]
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            if L < 1e-6:
                continue
            ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
            mids = path[1:-1]
            t = {k: (P[k]["pos"][0] - a[0]) * ux + (P[k]["pos"][1] - a[1]) * uy for k in mids}
            want = sorted(t.values())
            for k, tt in zip(mids, want):
                if abs(t[k] - tt) > 1e-6:
                    sh = tt - t[k]
                    for r, (x, y, ang, _) in self.unit_pose(U[k], k, sh * ux, sh * uy, 0.0).items():
                        self.commit(r, x, y, ang)
            self._nets()

    def legalise(self, U, strict=True):
        P = self.B["parts"]
        targets = {r: (P[r]["pos"][0], P[r]["pos"][1], P[r]["angle"])
                   for ms in U.values() for r in ms if r not in self.fixed}
        from . import preroute as PR
        hot = []
        try:
            cg = PR.congestion(self.B, self.cls, self.cons)
            c = cg["cell_mm"]
            hot = [(x - c / 2, y - c / 2, x + c / 2, y + c / 2) for _o, x, y in cg["worst"]]
        except Exception:
            pass
        res, rep = LG.legalise(self.B, targets, fixed=self.fixed, cons=self.cons, hot=hot, strict=strict, units=U)
        for r, (x, y, a) in res.items():
            if (round(P[r]["pos"][0], 4), round(P[r]["pos"][1], 4), P[r]["angle"]) != (x, y, a):
                self.commit(r, x, y, a)
        self._nets()
        return rep

    def run(self, rounds=3):
        self.turn_anchors()
        cl = self.seat_support()
        U = self.converge(cl, rounds)
        # legalisation may have split a loose unit: put every support part that ended up
        # far from its IC back beside it (ICs stay; seats are free space only, so this
        # cannot create an overlap), then prove the whole board
        for _ in range(3):
            cl, _l = CL.build(self.B, self.cons, self.spec)
            if not [1 for c in cl if c["ic"] not in self.fixed for s in c["supports"]
                    if s["sure"] and s["dist"] > s["limit"]]:
                break
            x0, y0, x1, y1 = self.B["bbox"]
            em = float((self.cons.get("placement") or {}).get("edge_clearance_mm", 0.3))
            mv = CL.reseat(self.B, [c for c in cl if c["ic"] not in self.fixed],
                           bounds=(x0 + em, y0 + em, x1 - em, y1 - em))
            self.moves.update(mv)
            self._nets()
        for r in list(self.moves):
            p = self.B["parts"][r]
            self.moves[r] = (round(p["pos"][0], 4), round(p["pos"][1], 4), p["angle"])
        M = LG._Model(self.B, self.cons, self.fixed, self.B["bbox"])
        rep = LG.verify(self.B, M, {r: m for r, m in self.moves.items()}, self.B["bbox"])
        if rep["outside"] or rep["overlaps"]:
            raise LG.PlacementError(f"routable: {len(rep['outside'])} outside the outline, "
                                    f"{len(rep['overlaps'])} overlap(s): {(rep['outside'] + rep['overlaps'])[:6]}")
        return self.moves


def PRX(a, b, c, d):
    """Do segments ab and cd properly cross?"""
    def ccw(p, q, r):
        return (r[1] - p[1]) * (q[0] - p[0]) - (q[1] - p[1]) * (r[0] - p[0])
    if a == c or a == d or b == c or b == d:
        return False
    return ccw(a, b, c) * ccw(a, b, d) < 0 and ccw(c, d, a) * ccw(c, d, b) < 0


def place(B, cons=None, spec=None, rounds=3, log=print):
    """-> {ref: (x, y, angle)} for every part that changes. Mutates B."""
    return Placer(B, cons, spec, log).run(rounds)
