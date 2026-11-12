"""Stage 5: legalise a placement WITH channels. Pure text (routediag model).

Removes overlaps by pushing movable parts apart along the axis of least
penetration (so relative order survives), never touching fixed/locked parts, and
never accepting a result that is outside the outline (inset by
`[placement] edge_clearance_mm`, 0.3). What it keeps clear:

  * fine-pitch ICs (pad pitch <= 0.5 mm): each side inflated by an escape allowance,
    max(0.6, 0.06 * pins on that side) mm, so fanout has room;
  * connectors with >= 40 pads: >= 1.5 mm beside each pad row;
  * track-forbidding rule areas, and `[[keepout]]` rectangles from the constraints
    (through-hole parts always; SMD parts when the keepout names their side and has
    no max_height_mm) except the refs in its `allow` list;
  * corridors: cells where the coarse demand map (preroute.congestion) exceeds
    capacity; passives are moved out of them when a legal seat exists nearby.

`legalise` raises PlacementError rather than return a part outside the outline or
an overlap it could not remove. `check_outline` is the guard every strategy uses.
"""
import fnmatch
import math
from collections import defaultdict

from . import clusters as CL
from . import geom as GM
from . import netclass as NC

ESC_MIN, ESC_PER_PIN, CONN_ROW_MM, CONN_PADS = 0.6, 0.06, 1.5, 40
BIG_MM2 = 25.0      # bodies at least this big settle first


class PlacementError(RuntimeError):
    pass


def allowances(part, pads, court):
    """(left, top, right, bottom) mm of keep-clear inflation for this part."""
    n = len(set(p["num"] for p in pads))
    if n < 5 or not pads:
        return (0.0, 0.0, 0.0, 0.0)
    pitch = CL._pitch({"pads": pads})
    fine = pitch <= 0.5 and NC.is_ic("U0", {"n_pads": n}, 3) and part.get("is_ic", True)
    conn = n >= CONN_PADS
    if not (fine or conn):
        return (0.0, 0.0, 0.0, 0.0)
    cx, cy = (court[0] + court[2]) / 2, (court[1] + court[3]) / 2
    cnt = [0, 0, 0, 0]
    for p in pads:
        if p["w"] * p["h"] > 1.5:
            continue
        dx, dy = p["x"] - cx, p["y"] - cy
        hw, hh = max((court[2] - court[0]) / 2, 1e-6), max((court[3] - court[1]) / 2, 1e-6)
        if abs(dx) / hw >= abs(dy) / hh:
            cnt[2 if dx > 0 else 0] += 1
        else:
            cnt[3 if dy > 0 else 1] += 1
    out = []
    for c in cnt:
        if not c:
            out.append(0.0)
        elif fine:
            out.append(max(ESC_MIN, ESC_PER_PIN * c))
        else:
            out.append(CONN_ROW_MM)
    return tuple(out)


def keepout_rects(B, cons):
    """[(rect, sides, allow, max_height, name)] from [[keepout]] and from the named rectangles
    `[placement.keepout.NAME] = {rect = [x0, y0, x1, y1], side = "B"|"F"|"both", allow = [refs],
    reason = "..."}`: no part BODY of that side inside the rectangle. A body is on the side its courtyard is on (the
    placement layer); through-hole pads do not count on the other side (the no_tht rectangles govern
    them) and mounting holes (no pads or only NPTH pads) are never bodies."""
    out = []
    for name, k in sorted(((cons or {}).get("placement", {}) or {}).get("keepout", {}).items()):
        if isinstance(k, dict) and k.get("rect"):
            side = str(k.get("side", "both")).upper()
            out.append((tuple(float(v) for v in k["rect"]), {"F", "B"} if side in ("BOTH", "") else {side[0]},
                        list(k.get("allow", [])), None, name))
    for k in (cons or {}).get("keepout", []) or []:
        rect = k.get("rect")
        if not rect and k.get("around"):
            pts = [B["parts"][r]["pos"] for r in B["parts"]
                   if any(fnmatch.fnmatch(r, g) for g in k["around"])]
            if len(pts) >= 2:
                m = float(k.get("margin_mm", 0.0))
                rect = [min(p[0] for p in pts) - m, min(p[1] for p in pts) - m,
                        max(p[0] for p in pts) + m, max(p[1] for p in pts) + m]
        if rect:
            side = str(k.get("side", "both")).upper()
            out.append((tuple(rect), {"F", "B"} if side in ("BOTH", "") else {side[0]},
                        list(k.get("allow", [])), k.get("max_height_mm"), k.get("name", "keepout")))
    return out


class _Model:
    def __init__(self, B, cons, fixed, bounds, escape=True):
        self.B, self.cons, self.bounds, self.escape = B, cons, bounds, escape
        self.fixed = set(fixed) | {r for r, p in B["parts"].items() if p.get("locked")}
        self.fixed |= set(((cons or {}).get("lock") or {}).get("at", {}))
        self.ko = keepout_rects(B, cons)
        self.zones = [(min(q[0] for q in z["pts"]), min(q[1] for q in z["pts"]),
                       max(q[0] for q in z["pts"]), max(q[1] for q in z["pts"]), z)
                      for z in B["zones"] if z["keepout"] and z["no_tracks"] and z["pts"]]
        self._rel = {}
        self.em = float(((cons or {}).get("placement") or {}).get("edge_clearance_mm", 0.3))

    def in_outline(self, rect):
        """Courtyard inside the real outline (rounded corners, cut-outs) with the edge clearance."""
        return GM.in_outline(self.B, rect, self.em)

    def rel(self, ref, ang):
        """(inflated rect, bare court) relative to the origin, cached per angle."""
        k = (ref, round(ang % 360, 3))
        if k not in self._rel:
            part = self.B["parts"][ref]
            pads, court = GM.moved(part, 0.0, 0.0, ang)
            a = allowances(part, pads, court) if self.escape and (NC.is_ic(ref, part, 3) or part["n_pads"] >= CONN_PADS) \
                else (0.0,) * 4
            self._rel[k] = ((court[0] - a[0], court[1] - a[1], court[2] + a[2], court[3] + a[3]), court)
        return self._rel[k]

    def sides(self, ref):
        """Sides a part OCCUPIES for the courtyard-overlap test: through-hole = both; otherwise its own
        side plus every side it has a courtyard on or SMD copper on (an edge-launch connector has pads
        and a courtyard on both, KiCad checks each layer's courtyards)."""
        p = self.B["parts"][ref]
        if p["tht"]:
            return {"F", "B"}
        s = {"F" if p["side"] == "F.Cu" else "B"} | set(p.get("court_layers", ()))
        for q in p["pads"]:
            s |= {"F" if l == "F.Cu" else "B" for l in q["layers"] if l in ("F.Cu", "B.Cu")}
        return s

    def body_side(self, ref):
        """Side of a part's BODY (keep-out sides): the side its courtyard is on = its placement layer,
        whatever its through-hole pads do. Mounting holes (no pads, or only NPTH) are no body: set()."""
        p = self.B["parts"][ref]
        if not any(not q.get("np") for q in p["pads"]):
            return set()
        return {"F" if p["side"] == "F.Cu" else "B"}

    def forbidden(self, ref, rect):
        """Rects this part must stay out of."""
        return [r for _n, r in self.forbidden_named(ref)]

    def forbidden_named(self, ref):
        """[(name, rect)] this part must stay out of."""
        part = self.B["parts"][ref]
        sd = self.body_side(ref)
        out = []
        for r, ks, allow, mh, name in self.ko:
            if any(fnmatch.fnmatch(ref, g) for g in allow):
                continue
            if sd & ks and (part["tht"] or mh is None):
                out.append((name, r))
        # track-forbidding rule areas: nothing can be routed to a pad inside one (the
        # area's own part, e.g. the antenna it is named after, is exempt)
        layer = "F.Cu" if part["side"] == "F.Cu" else "B.Cu"
        for x0, y0, x1, y1, z in self.zones:
            if (layer in z["layers"] or part["tht"]) and ref.upper() not in z["name"].upper() \
                    and z.get("owner") != ref:
                out.append((z["name"] or "rule area", (x0, y0, x1, y1)))
        return out


def _rect(pos, rel):
    return (pos[0] + rel[0], pos[1] + rel[1], pos[0] + rel[2], pos[1] + rel[3])


class _Ent:
    """One rigid movable entity: a single part, or a unit (IC + its support parts)
    that must keep its relative arrangement. `pos` is the entity origin (= its first
    member's origin); members keep their offsets and angles."""

    def __init__(self, M, members, targets):
        self.members = members
        x0, y0 = targets[members[0]][:2]
        self.pos = [x0, y0]
        self.off = {m: (targets[m][0] - x0, targets[m][1] - y0) for m in members}
        self.ang = {m: targets[m][2] for m in members}
        rels = [(M.rel(m, self.ang[m]), self.off[m]) for m in members]
        self.infl = _union([(_rect(o, r[0])) for r, o in rels])
        self.bare = _union([(_rect(o, r[1])) for r, o in rels])
        self.sides = set().union(*(M.sides(m) for m in members))
        self.tht = any(M.B["parts"][m]["tht"] for m in members)

    def member_pos(self, m):
        return (self.pos[0] + self.off[m][0], self.pos[1] + self.off[m][1], self.ang[m])


SPRAWL = 2.5        # a unit whose bounding box is > this x its parts' own area is not kept rigid


def _sprawl(M, ms, want):
    rs = [_rect(want[m][:2], M.rel(m, want[m][2])[1]) for m in ms]
    box = _union(rs)
    own = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rs)
    return (box[2] - box[0]) * (box[3] - box[1]) / max(own, 1e-6)


def _union(rs):
    return (min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs))


def check_outline(B, positions, bounds=None):
    """{ref: (x, y, ang)} -> list of refs whose courtyard leaves `bounds`
    (default the board's Edge.Cuts box)."""
    bounds = bounds or B["bbox"]
    bad = []
    for ref, (x, y, a) in positions.items():
        court = GM.moved(B["parts"][ref], x, y, a)[1]
        if not GM.inside(court, bounds):
            bad.append(ref)
    return bad


def legalise(B, targets, fixed=(), cons=None, bounds=None, hot=(), max_iter=400, log=None,
             strict=True, units=None, escape=True, rigid=()):
    """`targets` = {ref: (x, y, angle)} desired origins of the MOVABLE parts (every
    other part stays put). `units` = {key: [refs]} makes those parts move as one
    rigid body (an IC with its support parts); keys in `rigid` (structure templates) are never split. -> ({ref: (x, y, angle)}, report).
    Raises PlacementError when a part cannot be kept inside the outline / out of
    overlap (strict=False returns the best effort and the report instead)."""
    bounds = bounds or B["bbox"]
    em = float(((cons or {}).get("placement") or {}).get("edge_clearance_mm", 0.3))
    bounds = (bounds[0] + em, bounds[1] + em, bounds[2] - em, bounds[3] - em)   # copper-to-edge
    M = _Model(B, cons, fixed, bounds, escape)
    parts = B["parts"]
    want = {r: tuple(v) for r, v in targets.items() if r in parts and r not in M.fixed}
    groups, seen = [], set()
    for k, ms in (units or {}).items():
        ms = [m for m in ms if m in want]
        if len(ms) > 1 and k not in rigid and _sprawl(M, ms, want) > SPRAWL:
            continue                # too loose to move as one body: its parts move singly
        if ms:
            groups.append(ms)
            seen |= set(ms)
    groups += [[r] for r in sorted(want) if r not in seen]
    E = {g[0]: _Ent(M, g, want) for g in groups}
    mem_of = {m: k for k, e in E.items() for m in e.members}
    static = GM.Hash(5.0)
    for r, p in parts.items():
        if r not in want:
            static.add(r, _rect(p["pos"], M.rel(r, p["angle"])[0]), "x")

    def erect(k, inflated=True):
        e = E[k]
        return _rect(e.pos, e.infl if inflated else e.bare)

    def clamp(k):
        e = E[k]
        w0, w1 = bounds[0] - e.bare[0], bounds[2] - e.bare[2]
        h0, h1 = bounds[1] - e.bare[1], bounds[3] - e.bare[3]
        if w1 < w0 or h1 < h0:
            raise PlacementError(f"{k} is larger than the board outline")
        e.pos[0] = min(max(e.pos[0], w0), w1)
        e.pos[1] = min(max(e.pos[1], h0), h1)

    def forb(k, rect):
        out = []
        for m in E[k].members:
            out += M.forbidden(m, rect)
        return out

    ctx = _Ctx(M, E, static, bounds, forb, erect)
    area = lambda k: (lambda c: (c[2] - c[0]) * (c[3] - c[1]))(E[k].bare)
    # two tiers: big bodies (ICs, transformers, units) settle first against the fixed
    # parts; small ones then fill the gaps. Settling everything at once strands the
    # big parts in a crowd of already-placed passives.
    big = [k for k in E if area(k) >= BIG_MM2]
    tiers = [big, [k for k in E if k not in set(big)]] if big else [list(E)]
    iters = 0
    for tier in tiers:
        for k in tier:
            clamp(k)
        order = sorted(tier, key=lambda k: -area(k))
        for it in range(max_iter):
            iters += 1
            moved_any = False
            dyn = GM.Hash(5.0)
            for k in order:
                dyn.add(k, erect(k), "x")
            for k in order:
                me, e = erect(k), E[k]
                push = []
                for src, is_dyn in ((ctx.static, False), (dyn, True)):
                    for o in src.hits(me, "x", 0.0, ignore=(k,)):
                        if is_dyn and o < k:
                            continue                # each movable pair handled once
                        if not (e.sides & ctx.sides_of(o)):
                            continue
                        other = src.r[o][0]
                        if GM.overlap(me, other):
                            push.append((other, 0.5 if is_dyn else 1.0))
                for rect in forb(k, me):
                    if GM.overlap(me, rect):
                        push.append((rect, 1.0))
                for other, share in push:
                    ox = min(me[2], other[2]) - max(me[0], other[0])
                    oy = min(me[3], other[3]) - max(me[1], other[1])
                    if ox <= 0 or oy <= 0:
                        continue
                    cx, cy = (me[0] + me[2]) / 2, (me[1] + me[3]) / 2
                    ocx, ocy = (other[0] + other[2]) / 2, (other[1] + other[3]) / 2
                    if ox <= oy:
                        e.pos[0] += (ox + 0.02) * share * (1 if cx >= ocx else -1)
                    else:
                        e.pos[1] += (oy + 0.02) * share * (1 if cy >= ocy else -1)
                    moved_any = True
                    me = erect(k)
                clamp(k)
            if not moved_any:
                break
        # entities the pushing could not settle (crowded areas oscillate): nearest free seat
        stuck = _stuck(ctx, tier)
        if stuck:
            _seat(ctx, stuck, tier, inflate=True) or _seat(ctx, stuck, tier, inflate=False)
        for k in tier:                                  # this tier is now an obstacle
            ctx.static.add(("E", k), erect(k), "x")
    it = iters - 1
    if hot:
        _clear_hot(ctx, hot)
    res = {}
    for e in E.values():
        for m in e.members:
            x, y, a = e.member_pos(m)
            res[m] = (round(x, 4), round(y, 4), a)
    rep = verify(B, M, res, bounds)
    if strict and (rep["outside"] or rep["overlaps"] or rep["in_keepout"]):
        raise PlacementError(
            f"legalise failed: {len(rep['outside'])} outside the outline, {len(rep['overlaps'])} "
            f"overlap(s), {len(rep['in_keepout'])} in keep-outs "
            f"(e.g. {(rep['outside'] + [o for o in rep['overlaps']] + rep['in_keepout'])[:4]})")
    rep["iterations"] = it + 1
    return res, rep


class _Ctx:
    def __init__(self, M, E, static, bounds, forb, erect):
        self.M, self.E, self.static, self.bounds, self.forb, self.erect = M, E, static, bounds, forb, erect

    def sides_of(self, o):
        return self.E[o[1]].sides if isinstance(o, tuple) else self.M.sides(o)


def _stuck(c, tier):
    """Entities of `tier` still overlapping a static part, a finished tier or another
    entity of the tier (inflated rects) or outside the bounds, largest first."""
    dyn = GM.Hash(5.0)
    for k in tier:
        dyn.add(k, c.erect(k), "x")
    bad = []
    for k in tier:
        e = c.E[k]
        me = c.erect(k)
        hit = not GM.inside(c.erect(k, False), c.bounds) or not c.M.in_outline(c.erect(k, False)) \
            or any(GM.overlap(me, f) for f in c.forb(k, me))
        if not hit:
            hit = any(e.sides & c.sides_of(o) for o in c.static.hits(me, "x", 0.0)) or any(
                e.sides & c.E[o].sides for o in dyn.hits(me, "x", 0.0, ignore=(k,)))
        if hit:
            bad.append(k)
    return sorted(bad, key=lambda k: -(lambda r: (r[2] - r[0]) * (r[3] - r[1]))(c.E[k].bare))


def _seat(c, stuck, tier, inflate=True, step=0.5, reach=80.0):
    """Give each stuck entity the nearest position that is legal against everything
    already settled. True when every one found a seat."""
    settled = GM.Hash(5.0)
    for k in tier:
        if k not in stuck:
            settled.add(k, c.erect(k, inflate), "x")
    ring = _RING.get((step, reach))
    if ring is None:
        ring = _RING[(step, reach)] = sorted(
            ((math.hypot(i * step, j * step), i * step, j * step)
             for i in range(-int(reach / step), int(reach / step) + 1)
             for j in range(-int(reach / step), int(reach / step) + 1)))
    ok = True
    for k in stuck:
        e = c.E[k]
        rel = e.infl if inflate else e.bare
        x0, y0 = e.pos
        done = False
        for _d, dx, dy in ring:
            x, y = x0 + dx, y0 + dy
            if not GM.inside(_rect((x, y), e.bare), c.bounds) or not c.M.in_outline(_rect((x, y), e.bare)):
                continue
            me = _rect((x, y), rel)
            if any(GM.overlap(me, f) for f in c.forb(k, me)):
                continue
            if any(e.sides & c.sides_of(o) for o in c.static.hits(me, "x", 0.0)):
                continue
            if any(e.sides & c.E[o].sides for o in settled.hits(me, "x", 0.0)):
                continue
            e.pos[0], e.pos[1] = x, y
            settled.add(k, me, "x")
            done = True
            break
        ok = ok and done
        if not done:
            settled.add(k, _rect(e.pos, rel), "x")
    return ok


_RING = {}


def _clear_hot(c, hot, reach=6.0, step=0.5):
    """Lift single passives out of congested cells when a legal seat is near."""
    def in_hot(rect):
        return any(GM.overlap(rect, h) for h in hot)
    for k in sorted(c.E):
        e = c.E[k]
        if len(e.members) != 1 or c.M.B["parts"][k]["n_pads"] != 2 or not in_hot(_rect(e.pos, e.bare)):
            continue
        cands = sorted(((math.hypot(i * step, j * step), i * step, j * step)
                        for i in range(-int(reach / step), int(reach / step) + 1)
                        for j in range(-int(reach / step), int(reach / step) + 1)))
        for _, dx, dy in cands:
            x, y = e.pos[0] + dx, e.pos[1] + dy
            rc, ri = _rect((x, y), e.bare), _rect((x, y), e.infl)
            if not GM.inside(rc, c.bounds) or not c.M.in_outline(rc) or in_hot(rc):
                continue
            if any(o != ("E", k) for o in c.static.hits(ri, "x")):
                continue
            if any(o != k and GM.overlap(ri, c.erect(o)) and (e.sides & c.E[o].sides) for o in c.E):
                continue
            if any(GM.overlap(ri, f) for f in c.forb(k, ri)):
                continue
            e.pos[0], e.pos[1] = x, y
            break


def verify(B, M, res, bounds):
    """Final truth on BARE courtyards (the inflation is a request, not a rule)."""
    parts = B["parts"]
    court = {}
    for r, p in parts.items():
        court[r] = GM.moved(p, *res[r][:3])[1] if r in res else p["court"]
    outside = [r for r in res if not GM.inside(court[r], bounds) or not M.in_outline(court[r])]
    h = GM.Hash(5.0)
    for r, c in court.items():
        h.add(r, c, "x")
    over = []
    for r in res:
        for k in h.hits(court[r], "x", -0.001, ignore=(r,)):
            if (k not in res or k > r) and (M.sides(r) & M.sides(k)):
                over.append(f"{r}/{k}")
    ko = [r for r in res if any(GM.overlap(court[r], f) for f in M.forbidden(r, court[r]))]
    return {"outside": outside, "overlaps": sorted(over), "in_keepout": ko}
