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
import time
from collections import defaultdict

from . import anchors as AN
from . import clusters as CL
from . import geom as GM
from . import legalise as LG
from . import legality as LGL
from . import netclass as NC
from . import preroute as PR
from . import templates as TP

_MECH_PREFIX = ("MH", "MK", "FID", "TP", "H")
XING_MM = 3.0       # one ratsnest crossing among a unit's own nets costs this much length


def _fixed_set(B, cons):
    return PR.fixed_parts(B, cons)


def units(B, cl, fixed, groups=None):
    """{key: [refs]} — IC + confident support parts as one rigid unit, others alone. `groups`
    = {template name: [refs]} (templates.group_of): each is ONE unit, keyed by its first movable member."""
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
    for name, grp in sorted((groups or {}).items()):
        grp = [r for r in grp if r not in fixed]
        gs = set(grp)
        for k in [k for k, ms in out.items() if set(ms) & gs]:
            del out[k]
        if grp:
            out[grp[0]] = grp
    return out


PASS_SHARE, PASS_RATIO, BUS_NODES = 0.6, 0.25, 4


def chains(B, cls, fixed, unit_of):
    """[[end, unit, unit, ..., end]] - signal chains, ends fixed OR movable.

    Graph: nodes are units (an IC with its support parts is one node); every non-rail net that
    reaches 2..4 distinct nodes links them with the net's class weight (a bigger net is a bus, not a
    link). A node is PASS-THROUGH when its two strongest neighbours carry >= 60 % of its total link
    weight and the weaker of the two >= 25 % of the stronger; anything else (a hub with many
    neighbours, a connector) ends a chain, and so does a node whose strong pair does not include the
    previous node (a controller that is a pass-through of its SPI bus only). A chain is end, pass-through nodes..., end, found by
    following each pass-through node's other strong neighbour.

    The previous detector needed (a) both ends FIXED, (b) 2-pad nets only and (c) exactly two
    neighbours per interior node; on the real board that missed audio U5 -> T500 -> K1 (neither end
    fixed, and T500 has a net to U5 directly as well as through U5's DC-block C500) and
    CAN controller -> isolated transceiver -> J400 (controller not fixed, CAN_H/L are 3-pad nets
    with the ESD diode, the transceiver has supply/enable neighbours besides)."""
    link = defaultdict(lambda: defaultdict(float))
    sig = {n: ps for n, ps in NC.net_pads(B).items()
           if cls.get(n) and cls[n]["class"] != "rail" and len(ps) >= 2}
    nets_of = defaultdict(set)
    node_of = lambda r: unit_of.get(r, r)
    for n, ps in sig.items():
        for p in ps:
            nets_of[node_of(p["ref"])].add(n)
    crowd = {n: len({node_of(p["ref"]) for p in ps}) for n, ps in sig.items()}
    parts = B["parts"]

    def shunt(node):
        """A small non-IC part that only hangs on nets shared by 3+ nodes (TVS, pull-up, termination)."""
        if node in fixed or node not in parts or NC.is_ic(node, parts[node]) or parts[node]["n_pads"] > 4:
            return False
        return all(crowd[n] >= 3 for n in nets_of[node]) or len(nets_of[node]) <= 1

    for net, ps in sig.items():
        info = cls[net]
        # shunts are never links
        nodes = sorted({node_of(p["ref"]) for p in ps if not shunt(node_of(p["ref"]))})
        if not 2 <= len(nodes) <= BUS_NODES:
            continue
        w = max(info["weight"], 1.0)
        for i, a in enumerate(nodes):
            for b in nodes[i + 1:]:
                link[a][b] += w
                link[b][a] += w

    def strong(n):
        return sorted(link[n].items(), key=lambda kv: (-kv[1], kv[0]))

    def through(n):
        s = strong(n)
        if n in fixed or len(s) < 2:
            return None
        tot = sum(w for _, w in s)
        if (s[0][1] + s[1][1]) / tot >= PASS_SHARE and s[1][1] >= PASS_RATIO * s[0][1]:
            return s[0][0], s[1][0]
        return None

    T = {n: through(n) for n in link}
    out, used = [], set()
    for a in sorted(n for n in link if not T[n]):
        for first in (m for m, _ in strong(a)):
            if not T.get(first) or a not in T[first] or first in used:
                continue
            path, prev, cur = [a, first], a, first
            while T.get(cur) and prev in T[cur]:
                nxt = T[cur][0] if T[cur][1] == prev else T[cur][1]
                if nxt in path:
                    break
                path.append(nxt)
                prev, cur = cur, nxt
            if len(path) >= 3 and path[0] != path[-1]:       # the last node ends the chain: a hub, or one that does not pass it on
                if set(path[1:-1]) & used:
                    continue
                if [path[-1]] + path[1:-1][::-1] + [path[0]] in out:
                    continue
                out.append(path)
                used |= set(path[1:-1])
    return out


class Placer:
    def __init__(self, B, cons=None, spec=None, log=None):
        self.B, self.cons, self.spec, self.log = B, cons or {}, spec, log or (lambda m: None)
        self.cls = NC.classify(B, self.cons, spec)
        self.fixed = _fixed_set(B, self.cons)
        self.moves = {}          # ref -> (x, y, ang): everything this run changed
        pl = self.cons.get("placement") or {}
        self.t_end = time.time() + float(pl.get("time_limit_s", 600))
        self.max_rounds = int(pl.get("max_rounds", 6))
        self.origin = self.snapshot()
        self.bad0 = self.issues()
        self.templates = TP.load(B, self.cons)
        self.groups = {}         # template name -> refs, rigid after the template is placed
        self.reports = []
        self.best = None         # (score, snapshot)
        self.history = []

    def snapshot(self):
        return {r: (p["pos"][0], p["pos"][1], p["angle"]) for r, p in self.B["parts"].items()}

    def restore(self, snap):
        for r, (x, y, a) in snap.items():
            p = self.B["parts"][r]
            if (p["pos"][0], p["pos"][1], p["angle"]) != (x, y, a):
                GM.place(p, x, y, a)
        self._nets()

    def score(self):
        """The gate's own scalar (preroute.score) on the live model."""
        f, card = PR.run(self.B, self.cons, self.spec)
        return card["score"]

    def issues(self):
        """Every legality violation on the live model, as a set of strings."""
        res = LGL.Legal(self.B, self.cons, self.fixed).issues()
        return {f"{k}:{x}" for k, v in res.items() for x in v}

    def valid(self):
        """No violation beyond the ones the input board already had (legality.py: same-side
        courtyards, the real outline, forbidden regions, no-through-hole regions)."""
        return self.issues() <= self.bad0

    def consider(self, label):
        """Keep the best valid state seen; True when the search should stop (plateau/time)."""
        if not self.valid():
            self.log(f"{label}: state not legal yet, not scored")
            return False
        sc = self.score()
        prev = self.best[0] if self.best else None
        better = prev is None or sc < prev
        if better:
            self.best = (sc, self.snapshot())
        self.history.append(sc)
        self.log(f"{label}: score {sc:.0f}" + ("" if better else f" (best {prev:.0f})"))
        if time.time() > self.t_end:
            self.log("wall-clock limit reached")
            return True
        h = self.history
        if len(h) >= 3 and (min(h[:-2]) - min(h[-2:])) <= 0.01 * abs(min(h[:-2])):
            self.log("plateau: < 1 % improvement in 2 rounds")
            return True
        return False

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
            hard = 3.0 if info["class"] in PR.HARD else 1.0       # hard budgets pull 3x
            res.append((hard * info["weight"] / max(1, len(self.pads[n]) - 1), o, c, n))
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

    def apply_templates(self):
        """Place every [placement.template.*], then keep each group rigid. A group containing a fixed
        part is fixed as a whole."""
        if not self.templates:
            return
        L = LGL.Legal(self.B, self.cons, self.fixed)
        for t in self.templates:
            mv, rep = TP.place(self.B, self.cons, self.spec, t, self.cls, self.fixed, log=self.log, legal=L)
            self.moves.update(mv)
            self._nets()
            self.log(TP.format_report(rep))
            self.reports.append(rep)
            cl, _ = CL.build(self.B, self.cons, self.spec)
            x0, y0, x1, y1 = self.B["bbox"]
            mv2 = CL.reseat(self.B, [c for c in cl if c["ic"] in t["refs"] and c["ic"] not in self.fixed],
                            everything=True, bounds=(x0 + .2, y0 + .2, x1 - .2, y1 - .2), legal=L)
            self.moves.update(mv2)
            self._nets()
            grp = TP.group_of(self.B, self.cls, t, cl)
            self.groups[t["name"]] = grp
            if any(r in self.fixed for r in t["refs"]):
                self.fixed |= set(grp)
        self.rigid = {g[0] for g in (
            [r for r in grp if r not in self.fixed] for grp in self.groups.values()) if g}

    rigid = frozenset()

    def converge(self, cl, rounds=6, sweeps=25, tol=0.25):
        self._nets()
        P = self.B["parts"]
        U = units(self.B, cl, self.fixed, self.groups)
        unit_of = {r: k for k, ms in U.items() for r in ms}
        ch = chains(self.B, self.cls, self.fixed, unit_of)
        self.log(f"{len(U)} movable unit(s), {len(ch)} signal chain(s)")
        order = sorted(U, key=lambda k: (-len(U[k]), k))
        for rnd in range(min(rounds, self.max_rounds)):
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
                if k in self.fixed or k in self.rigid or P[k]["n_pads"] < 5:
                    continue
                base = self.cost(ms, k, 0, 0, 0.0)
                best = min(((self.cost(ms, k, 0, 0, d), d) for d in (90.0, 180.0, 270.0)), default=(base, 0.0))
                if best[0] < 0.92 * base:
                    for r, (x, y, a, _) in self.unit_pose(ms, k, 0, 0, best[1]).items():
                        self.commit(r, x, y, a)
                    self._nets()
                    turned += 1
            self.log(f"round {rnd + 1}: {turned} unit(s) turned")
            self.legalise(U, strict=False)
            for ms in U.values():              # units may have been split: refresh support seats first
                pass
            if self.consider(f"round {rnd + 1}"):
                break
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

    def legalise(self, U, strict=True, escape=True):
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
        res, rep = LG.legalise(self.B, targets, fixed=self.fixed, cons=self.cons, hot=hot, strict=strict, units=U, escape=escape,
                                rigid=self.rigid)
        for r, (x, y, a) in res.items():
            if (round(P[r]["pos"][0], 4), round(P[r]["pos"][1], 4), P[r]["angle"]) != (x, y, a):
                self.commit(r, x, y, a)
        self._nets()
        return rep

    def run(self, rounds=6):
        sc0 = self.score()                      # the input board is the first candidate
        self.best = (sc0, self.snapshot())
        self.history.append(sc0)
        self.log(f"input board: score {sc0:.0f}")
        self.turn_anchors()
        cl = self.seat_support()
        self.apply_templates()
        U = self.converge(cl, rounds)
        try:                                    # settle for real: with escape allowances, else without
            self.legalise(U)
        except LG.PlacementError:
            self.log("legalise with escape allowances failed; retrying without them")
            try:
                self.legalise(U, escape=False)
            except LG.PlacementError as e:
                self.log(f"legalise failed ({str(e)[:80]}...): falling back to the best legal state seen")
                self.restore(self.best[1] if self.best else self.origin)
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
        if self.valid():
            self.consider("final")
        # the result is the best state seen (the input board is the first candidate), never
        # the live state: moves are the difference between that snapshot and the input
        snap = self.best[1]
        self.restore(snap)
        base = self.origin
        norm = lambda a: (a + 180) % 360 - 180
        self.moves = {r: (round(x, 4), round(y, 4), a) for r, (x, y, a) in snap.items()
                      if abs(x - base[r][0]) > 1e-4 or abs(y - base[r][1]) > 1e-4
                      or abs(norm(a - base[r][2])) > 1e-6}
        if not self.moves:
            self.log("no state beat the input board: nothing moved")
        new = sorted(self.issues() - self.bad0)
        if new:
            raise LG.PlacementError(f"routable: {len(new)} new legality violation(s): {new[:6]}")
        return self.moves


def PRX(a, b, c, d):
    """Do segments ab and cd properly cross?"""
    def ccw(p, q, r):
        return (r[1] - p[1]) * (q[0] - p[0]) - (q[1] - p[1]) * (r[0] - p[0])
    if a == c or a == d or b == c or b == d:
        return False
    return ccw(a, b, c) * ccw(a, b, d) < 0 and ccw(c, d, a) * ccw(c, d, b) < 0


def place(B, cons=None, spec=None, rounds=6, log=print):
    """-> {ref: (x, y, angle)} for every part that changes. Mutates B."""
    return Placer(B, cons, spec, log).run(rounds)
