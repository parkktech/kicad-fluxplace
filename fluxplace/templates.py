"""Structure templates: groups whose required physical SHAPE a centroid placer cannot find.

    [placement.template.bt_rf]          # a combining tree: leaves in pairs around each combiner,
    kind = "tree"                       # combiners toward the root
    leaves = ["U305", "U306", "U307", "U308"]
    combiners = ["U310", "U311", "U309"]   # bottom-up: first level (one per leaf pair), then the next
    root = "U312"                          # stays where it is; the tree grows toward the board centre
    direction = "+x"                       # optional: growth direction (default: root -> board centre)
    nets = ["BT1_RF", "BT_G1"]             # optional: nets to minimise (default: every signal net
                                           # between group members and their 2-3 pad passengers)

    [placement.template.audio]          # a signal chain, placed monotonically from the first to the last
    kind = "chain"
    refs = ["U5", "C500", "T500", "K1"]

Tree: the combiner list is read bottom-up. With 4 leaves and 3 combiners, combiners[0] takes
leaves[0:2], combiners[1] takes leaves[2:4], combiners[2] takes combiners[0:2]; the last combiner feeds
the root. Placement is top-down from the root: each combiner at the legal seat nearest the target
"ahead of its parent", its two children beside each other across the growth direction, then every
passenger (a 2-3 pad part such as an isolation resistor or DC-block capacitor that sits on the group's
nets) at the legal seat that minimises its class-weighted length, then a few passes that re-seat each
member at the legal seat nearest the weighted centroid of its partners, accepted only when the
class-weighted length of the listed nets improves.

Chain: the first and last parts stay; the interior parts are spread along the straight line between
them in order (equal gaps) and seated at the nearest legal seat. `monotonic` in the report says
whether the interior still progresses along the line.

Legality (legality.Legal) decides every seat: same-side courtyards, outline, keep-outs, no-THT regions.
After a template is placed the placer treats the whole group (members, passengers and the support
parts of member ICs) as ONE rigid unit; a group that contains a fixed part (the root of a fixed tree)
is fixed as a whole.
"""
import math
from collections import defaultdict

from . import geom as GM
from . import netclass as NC

PASSES = 4
SLACK_MM = 2.0


def load(B, cons):
    """[{name, kind, members, passengers_of, ...}] from [placement.template.*]; raises ValueError for a
    template naming an unknown part."""
    out = []
    for name, t in sorted((((cons or {}).get("placement") or {}).get("template") or {}).items()):
        if not isinstance(t, dict):
            continue
        kind = t.get("kind")
        if kind == "tree":
            refs = list(t.get("leaves", [])) + list(t.get("combiners", [])) + [t["root"]]
        elif kind == "chain":
            refs = list(t.get("refs", []))
        else:
            raise ValueError(f"template {name}: kind must be 'tree' or 'chain'")
        miss = [r for r in refs if r not in B["parts"]]
        if miss:
            raise ValueError(f"template {name}: unknown part(s) {', '.join(miss)}")
        out.append(dict(t, name=name, kind=kind, refs=refs))
    return out


# ---------------------------------------------------------------- cost
class _Cost:
    def __init__(self, B, cls, nets):
        self.B, self.cls = B, cls
        self.pads = NC.net_pads(B)
        self.nets = list(nets)

    def net(self, n, skip=()):
        ps = [(p["x"], p["y"]) for p in self.pads[n] if p["ref"] not in skip]
        return NC.mst_length(ps) * max(self.cls.get(n, {}).get("weight", 1.0), 1.0)

    def total(self, skip=()):
        return sum(self.net(n, skip) for n in self.nets)

    def at(self, ref, pads, skip=()):
        """Cost of the nets touching `ref` with its pads at `pads` (hypothetical copies)."""
        mine = {p["num"]: p for p in pads}
        tot = 0.0
        for n in self.nets:
            if not any(q["ref"] == ref for q in self.pads[n]):
                continue
            pts = []
            for q in self.pads[n]:
                if q["ref"] == ref:
                    m = mine.get(q["num"])
                    pts.append((m["x"], m["y"]) if m else (q["x"], q["y"]))
                elif q["ref"] not in skip:
                    pts.append((q["x"], q["y"]))
            tot += NC.mst_length(pts) * max(self.cls.get(n, {}).get("weight", 1.0), 1.0)
        return tot


_RING = {}


def _ring(step, reach):
    k = (step, reach)
    if k not in _RING:
        n = int(reach / step)
        _RING[k] = sorted((math.hypot(i * step, j * step), i * step, j * step)
                          for i in range(-n, n + 1) for j in range(-n, n + 1))
    return _RING[k]


def seat(B, legal, cost, ref, tx, ty, skip=(), angles=None, reach=30.0, step=0.5, slack=SLACK_MM):
    """(x, y, angle) of the legal seat for `ref` near (tx, ty): for each angle the nearest legal
    seat, then the one among those within `slack` mm of the nearest with the lowest `cost.at`.
    None when no legal seat exists within `reach`."""
    part = B["parts"][ref]
    best = None
    for ang in (angles if angles is not None else [(part["angle"] + d) % 360 for d in (0.0, 90.0, 180.0, 270.0)]):
        first = None
        for d, dx, dy in _ring(step, reach):
            if first is not None and d > first + slack:
                break
            x, y = tx + dx, ty + dy
            pads, court = GM.moved(part, x, y, ang)
            if legal.part_issues(ref, court, pads):
                continue
            if first is None:
                first = d
            c = cost.at(ref, pads, skip) + 0.05 * d      # 0.05: stay near the target when costs tie
            if best is None or c < best[0]:
                best = (c, x, y, ang)
    return None if best is None else (round(best[1], 4), round(best[2], 4), best[3])


def _issue_set(B, cons, fixed):
    from . import legality as LGL
    return {f"{k}:{x}" for k, v in LGL.Legal(B, cons, fixed).issues().items() for x in v}


def _gate_score(B, cons, spec):
    from . import preroute as PR
    return PR.run(B, cons, spec)[1]["score"]


def _put(B, legal, ref, pose):
    GM.place(B["parts"][ref], *pose)
    legal.refresh(ref)


def _ext(B, ref, ux, uy):
    c = B["parts"][ref]["court"]
    return abs(ux) * (c[2] - c[0]) + abs(uy) * (c[3] - c[1])


def _centroid_of_partners(B, cost, ref, skip):
    pts = []
    for n in cost.nets:
        ps = cost.pads[n]
        if any(q["ref"] == ref for q in ps):
            w = max(cost.cls.get(n, {}).get("weight", 1.0), 1.0)
            pts += [(q["x"], q["y"], w) for q in ps if q["ref"] != ref and q["ref"] not in skip]
    sw = sum(w for *_, w in pts)
    if not sw:
        return None
    return sum(x * w for x, y, w in pts) / sw, sum(y * w for x, y, w in pts) / sw


def passengers(B, members, cls, fixed=()):
    """2-3 pad non-member parts with >= 2 signal nets, all reaching a member: isolation resistors, DC-block
    caps, series parts (a pull-up or shunt with one signal net belongs to its IC's cluster instead)."""
    mem = set(members)
    pads = NC.net_pads(B)
    out = []
    for ref, p in sorted(B["parts"].items()):
        if ref in mem or ref in fixed or p["n_pads"] > 3 or p.get("locked"):
            continue
        nets = [q["net"] for q in p["pads"] if q["net"] and cls.get(q["net"], {}).get("class") != "rail"]
        if len(set(nets)) >= 2 and all(any(x["ref"] in mem for x in pads[n]) for n in nets):
            out.append(ref)
    return out


def _tree_parents(leaves, combiners, root):
    parent, level, ci = {}, list(leaves), 0
    while ci < len(combiners) and len(level) >= 1:
        nxt = []
        for i in range(0, len(level), 2):
            if ci >= len(combiners):
                nxt += level[i:]
                break
            c = combiners[ci]
            ci += 1
            for ch in level[i:i + 2]:
                parent[ch] = c
            nxt.append(c)
        level = nxt
        if len(level) == 1:
            break
    parent[level[0]] = root
    for ch in level[1:]:
        parent[ch] = root
    return parent


def place(B, cons, spec, t, cls=None, fixed=(), log=None, legal=None):
    """Place one template on the live board model B (mutated). -> (moves, report) where
    moves = {ref: (x, y, angle)} for every part that moved and report has the group, the nets, their
    lengths before/after and (chain) whether the order is monotonic. Parts that cannot be seated
    legally stay and are listed under report["unplaced"].

    ATOMIC: the members' current positions are one candidate. When the constructed layout leaves a member
    without a legal seat, adds a legality violation, makes the listed nets' worst length longer or the
    gate score (preroute.score) worse than the input, every member goes back to where it was, moves is {}
    and report["rejected"] says why ("template rejected: ...", with the numbers)."""
    from . import legality as LGL
    log = log or (lambda m: None)
    cls = cls or NC.classify(B, cons, spec)
    legal = legal or LGL.Legal(B, cons, fixed)
    P = B["parts"]
    start = {r: (p["pos"][0], p["pos"][1], p["angle"]) for r, p in P.items()}
    iss0 = _issue_set(B, cons, fixed)
    sc0 = _gate_score(B, cons, spec)
    members = list(t["refs"])
    pas = passengers(B, members, cls, fixed)
    group = members + pas
    pads = NC.net_pads(B)
    if t.get("nets"):
        nets = [n for n in t["nets"] if n in pads]
    else:
        nets = sorted(n for n, ps in pads.items() if cls.get(n, {}).get("class") != "rail"
                      and len({q["ref"] for q in ps} & set(group)) >= 2)
    cost = _Cost(B, cls, nets)
    before = {n: round(NC.mst_length([(q["x"], q["y"]) for q in pads[n]]), 2) for n in nets}
    movable = [r for r in members if r not in fixed and not P[r].get("locked")]
    unplaced = []
    placed = set(r for r in members if r not in movable)     # parts that stay count from the start
    skip = lambda: set(group) - placed
    for r in movable + pas:
        legal.hash.remove(r)                                   # the group re-seats; old seats do not block

    def go(ref, tx, ty, angles=None):
        pose = seat(B, legal, cost, ref, tx, ty, skip=skip() - {ref}, angles=angles)
        if pose is None:
            unplaced.append(ref)
            pose = start[ref]
        _put(B, legal, ref, pose)
        placed.add(ref)

    if t["kind"] == "tree":
        root = t["root"]
        parent = _tree_parents(t["leaves"], t["combiners"], root)
        kids = defaultdict(list)
        for ch, pa in parent.items():
            kids[pa].append(ch)
        rx, ry = P[root]["pos"]
        d = t.get("direction")
        if d in ("+x", "-x", "+y", "-y"):
            ux, uy = {"+x": (1, 0), "-x": (-1, 0), "+y": (0, 1), "-y": (0, -1)}[d]
        else:
            bx = B["bbox"]
            vx, vy = (bx[0] + bx[2]) / 2 - rx, (bx[1] + bx[3]) / 2 - ry
            n = math.hypot(vx, vy) or 1.0
            ux, uy = vx / n, vy / n
        px, py = -uy, ux
        order = [root]
        for r in order:                       # breadth-first from the root
            for ch in kids.get(r, []):
                order.append(ch)
        for r in order[1:]:
            pa = parent[r]
            ax, ay = P[pa]["pos"]
            sibs = kids[pa]
            k = sibs.index(r)
            side = (k - (len(sibs) - 1) / 2) * 2 if len(sibs) > 1 else 0.0
            sep = (_ext(B, pa, ux, uy) + _ext(B, r, ux, uy)) / 2 + 0.5
            off = side * ((_ext(B, r, px, py)) / 2 + 0.5) if side else 0.0
            if r in movable:
                go(r, ax + ux * sep + px * off, ay + uy * sep + py * off)
    else:                                      # chain
        refs = t["refs"]
        a, b = P[refs[0]]["pos"], P[refs[-1]]["pos"]
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        ux, uy = ((b[0] - a[0]) / L, (b[1] - a[1]) / L) if L > 1e-6 else (1.0, 0.0)
        mid = refs[1:-1]
        w = [_ext(B, r, ux, uy) for r in mid]
        free = L - (_ext(B, refs[0], ux, uy) + _ext(B, refs[-1], ux, uy)) / 2 - sum(w)
        gap = max(free, 0.0) / (len(mid) + 1)
        s = _ext(B, refs[0], ux, uy) / 2
        for r, wi in zip(mid, w):
            s += gap + wi / 2
            if r in movable:
                go(r, a[0] + ux * s, a[1] + uy * s)
            s += wi / 2
    for r in pas:
        c = _centroid_of_partners(B, cost, r, skip() - {r})
        if c:
            go(r, *c)
        else:
            placed.add(r)
            _put(B, legal, r, start[r])
    # refinement: each member (and passenger) to the legal seat nearest its partners' weighted centroid,
    # kept only when the listed nets get shorter
    for _ in range(PASSES):
        improved = False
        for r in movable + pas:
            if r in unplaced:
                continue
            cur = cost.total()
            c = _centroid_of_partners(B, cost, r, set())
            if not c:
                continue
            old = (P[r]["pos"][0], P[r]["pos"][1], P[r]["angle"])
            legal.hash.remove(r)
            pose = seat(B, legal, cost, r, *c, skip=set())
            if pose is None or pose == old:
                legal.refresh(r)
                continue
            _put(B, legal, r, pose)
            if cost.total() < cur - 0.01:
                improved = True
            else:
                _put(B, legal, r, old)
        if not improved:
            break
    for r in group:
        legal.refresh(r)
    after = {n: round(NC.mst_length([(q["x"], q["y"]) for q in pads[n]]), 2) for n in nets}
    why = []
    if unplaced:
        why.append(f"no legal seat for {', '.join(unplaced)}")
    new_iss = sorted(_issue_set(B, cons, fixed) - iss0)
    if new_iss:
        why.append(f"{len(new_iss)} new legality violation(s): {', '.join(new_iss[:4])}")
    if before and max(after.values()) > max(before.values()) + 0.05:
        wb = max(before, key=before.get)
        wa = max(after, key=after.get)
        why.append(f"worst net {wa} {after[wa]:.1f} mm > input worst {wb} {before[wb]:.1f} mm")
    sc1 = _gate_score(B, cons, spec) if not why else None
    if sc1 is not None and sc1 > sc0 + 0.5:
        why.append(f"gate score {sc0:.0f} -> {sc1:.0f}")
    if why:
        for r in set(group) | set(members):
            _put(B, legal, r, start[r])
        for r, (x, y, a) in start.items():              # nothing outside the group may have moved either
            if (P[r]["pos"][0], P[r]["pos"][1], P[r]["angle"]) != (x, y, a):
                _put(B, legal, r, (x, y, a))
        worse = {n: round(NC.mst_length([(q["x"], q["y"]) for q in pads[n]]), 2) for n in nets}
        rep_rej = ("template rejected: " + "; ".join(why) + " (candidate nets: " +
                   ", ".join(f"{n} {before[n]:.1f}->{after[n]:.1f}" for n in nets) + ")")
        log(rep_rej)
        after_c = after
        after = worse
        unplaced = []
    moves = {r: (round(P[r]["pos"][0], 4), round(P[r]["pos"][1], 4), P[r]["angle"]) for r in group
             if abs(P[r]["pos"][0] - start[r][0]) > 1e-4 or abs(P[r]["pos"][1] - start[r][1]) > 1e-4
             or abs((P[r]["angle"] - start[r][2] + 180) % 360 - 180) > 1e-6}
    rep = {"name": t["name"], "kind": t["kind"], "members": members, "passengers": pas,
           "nets": nets, "before": before, "after": after, "unplaced": unplaced,
           "weighted_before": round(sum(before[n] * max(cls.get(n, {}).get("weight", 1.0), 1.0) for n in nets), 1),
           "weighted_after": round(sum(after[n] * max(cls.get(n, {}).get("weight", 1.0), 1.0) for n in nets), 1)}
    if why:
        rep["rejected"] = rep_rej
        rep["candidate"] = after_c
    if t["kind"] == "chain":
        refs = t["refs"]
        a = P[refs[0]]["pos"]
        proj = [((P[r]["pos"][0] - a[0]) * ux + (P[r]["pos"][1] - a[1]) * uy) for r in refs]
        rep["monotonic"] = all(proj[i] < proj[i + 1] for i in range(len(proj) - 1))
    return moves, rep


def group_of(B, cls, t, cl=None):
    """Every part the placer must keep rigid for template `t`: its members, passengers and the support
    parts of member ICs (clusters)."""
    members = list(t["refs"])
    grp = members + passengers(B, members, cls)
    if cl:
        for c in cl:
            if c["ic"] in members:
                grp += [s["ref"] for s in c["supports"] if s["sure"]]
    seen, out = set(), []
    for r in grp:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def format_report(rep):
    L = [f"template {rep['name']} ({rep['kind']}): {len(rep['members'])} member(s) + "
         f"{len(rep['passengers'])} passenger(s) {', '.join(rep['passengers'])}"]
    for n in rep["nets"]:
        L.append(f"  {n:14s} {rep['before'][n]:7.1f} -> {rep['after'][n]:6.1f} mm")
    L.append(f"  class-weighted length {rep['weighted_before']:.1f} -> {rep['weighted_after']:.1f} mm")
    if "monotonic" in rep:
        L.append(f"  monotonic along the line: {rep['monotonic']}")
    if rep.get("rejected"):
        L.append("  " + rep["rejected"])
    if rep["unplaced"]:
        L.append(f"  no legal seat (left in place): {', '.join(rep['unplaced'])}")
    return "\n".join(L)
