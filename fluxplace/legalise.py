"""Stage 5: legalise a placement WITH channels. Pure text (routediag model).

Removes overlaps by pushing movable parts apart along the axis of least
penetration (so relative order survives), never touching fixed/locked parts, and
never accepting a result that is outside the outline. What it keeps clear:

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
    """[(rect, sides, allow, max_height)] from [[keepout]]."""
    out = []
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
                        list(k.get("allow", [])), k.get("max_height_mm")))
    return out


class _Model:
    def __init__(self, B, cons, fixed, bounds):
        self.B, self.cons, self.bounds = B, cons, bounds
        self.fixed = set(fixed) | {r for r, p in B["parts"].items() if p.get("locked")}
        self.fixed |= set(((cons or {}).get("lock") or {}).get("at", {}))
        self.ko = keepout_rects(B, cons)
        self.zones = [(min(q[0] for q in z["pts"]), min(q[1] for q in z["pts"]),
                       max(q[0] for q in z["pts"]), max(q[1] for q in z["pts"]), z)
                      for z in B["zones"] if z["keepout"] and z["no_tracks"] and z["pts"]]
        self._rel = {}

    def rel(self, ref, ang):
        """(inflated rect, bare court) relative to the origin, cached per angle."""
        k = (ref, round(ang % 360, 3))
        if k not in self._rel:
            part = self.B["parts"][ref]
            pads, court = GM.moved(part, 0.0, 0.0, ang)
            a = allowances(part, pads, court) if NC.is_ic(ref, part, 3) or part["n_pads"] >= CONN_PADS \
                else (0.0,) * 4
            self._rel[k] = ((court[0] - a[0], court[1] - a[1], court[2] + a[2], court[3] + a[3]), court)
        return self._rel[k]

    def sides(self, ref):
        p = self.B["parts"][ref]
        return {"F", "B"} if p["tht"] else {"F" if p["side"] == "F.Cu" else "B"}

    def forbidden(self, ref, rect):
        """Rects this part must stay out of."""
        part = self.B["parts"][ref]
        sd = self.sides(ref)
        out = []
        for r, ks, allow, mh in self.ko:
            if any(fnmatch.fnmatch(ref, g) for g in allow):
                continue
            if (part["tht"] and sd & ks) or (not part["tht"] and sd & ks and mh is None):
                out.append(r)
        # track-forbidding rule areas: nothing can be routed to a pad inside one (the
        # area's own part, e.g. the antenna it is named after, is exempt)
        layer = "F.Cu" if part["side"] == "F.Cu" else "B.Cu"
        for x0, y0, x1, y1, z in self.zones:
            if (layer in z["layers"] or part["tht"]) and ref.upper() not in z["name"].upper():
                out.append((x0, y0, x1, y1))
        return out


def _rect(pos, rel):
    return (pos[0] + rel[0], pos[1] + rel[1], pos[0] + rel[2], pos[1] + rel[3])


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


def legalise(B, targets, fixed=(), cons=None, bounds=None, hot=(), max_iter=400, log=None):
    """`targets` = {ref: (x, y, angle)} desired origins of the MOVABLE parts (every
    other part stays put). -> ({ref: (x, y, angle)}, report). Raises PlacementError
    when a part cannot be kept inside the outline / out of overlap."""
    bounds = bounds or B["bbox"]
    M = _Model(B, cons, fixed, bounds)
    mov = {r: [v[0], v[1], v[2]] for r, v in targets.items() if r in B["parts"] and r not in M.fixed}
    parts = B["parts"]
    static = GM.Hash(5.0)
    for r, p in parts.items():
        if r not in mov:
            rel = M.rel(r, p["angle"])[0]
            static.add(r, _rect(p["pos"], rel), "x")

    def clamp(ref):
        rel = M.rel(ref, mov[ref][2])[1]
        w0, w1 = bounds[0] - rel[0], bounds[2] - rel[2]
        h0, h1 = bounds[1] - rel[1], bounds[3] - rel[3]
        if w1 < w0 or h1 < h0:
            raise PlacementError(f"{ref} is larger than the board outline")
        mov[ref][0] = min(max(mov[ref][0], w0), w1)
        mov[ref][1] = min(max(mov[ref][1], h0), h1)

    for r in mov:
        clamp(r)
    order = sorted(mov, key=lambda r: -(lambda c: (c[2] - c[0]) * (c[3] - c[1]))(M.rel(r, mov[r][2])[1]))
    for it in range(max_iter):
        moved_any = False
        dyn = GM.Hash(5.0)
        for r in order:
            dyn.add(r, _rect(mov[r], M.rel(r, mov[r][2])[0]), "x")
        for r in order:
            me = _rect(mov[r], M.rel(r, mov[r][2])[0])
            sd = M.sides(r)
            push = []
            for src in (static, dyn):
                for k in src.hits(me, "x", 0.0, ignore=(r,)):
                    if k in mov and k < r:
                        continue                    # each movable pair handled once
                    if not (sd & M.sides(k)):
                        continue
                    other = src.r[k][0]
                    if GM.overlap(me, other):
                        share = 1.0 if k not in mov else 0.5
                        push.append((other, share))
            for rect in M.forbidden(r, me):
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
                    mov[r][0] += (ox + 0.02) * share * (1 if cx >= ocx else -1)
                else:
                    mov[r][1] += (oy + 0.02) * share * (1 if cy >= ocy else -1)
                moved_any = True
                me = _rect(mov[r], M.rel(r, mov[r][2])[0])
            clamp(r)
        if not moved_any:
            break
    # corridors: lift passives out of hot cells when a legal seat is close
    if hot:
        _clear_hot(B, M, mov, static, hot)
    res = {r: (round(v[0], 4), round(v[1], 4), v[2]) for r, v in mov.items()}
    rep = verify(B, M, res, bounds)
    if rep["outside"] or rep["overlaps"] or rep["in_keepout"]:
        raise PlacementError(
            f"legalise failed: {len(rep['outside'])} outside the outline, {len(rep['overlaps'])} "
            f"overlap(s), {len(rep['in_keepout'])} in keep-outs "
            f"(e.g. {(rep['outside'] + [o for o in rep['overlaps']] + rep['in_keepout'])[:4]})")
    rep["iterations"] = it + 1
    return res, rep


def _clear_hot(B, M, mov, static, hot, reach=6.0, step=0.5):
    def in_hot(rect):
        return any(GM.overlap(rect, h) for h in hot)
    for r in sorted(mov):
        part = B["parts"][r]
        if part["n_pads"] != 2:
            continue
        rel_i, rel_c = M.rel(r, mov[r][2])
        if not in_hot(_rect(mov[r], rel_c)):
            continue
        cands = sorted(((math.hypot(i * step, j * step), i * step, j * step)
                        for i in range(-int(reach / step), int(reach / step) + 1)
                        for j in range(-int(reach / step), int(reach / step) + 1)))
        for _, dx, dy in cands:
            x, y = mov[r][0] + dx, mov[r][1] + dy
            ri, rc = _rect((x, y), rel_i), _rect((x, y), rel_c)
            if not GM.inside(rc, M.bounds) or in_hot(rc):
                continue
            if any(True for _ in static.hits(ri, "x")):
                continue
            if any(k != r and GM.overlap(ri, _rect(mov[k], M.rel(k, mov[k][2])[0]))
                   and (M.sides(r) & M.sides(k)) for k in mov):
                continue
            if any(GM.overlap(ri, f) for f in M.forbidden(r, ri)):
                continue
            mov[r][0], mov[r][1] = x, y
            break


def verify(B, M, res, bounds):
    """Final truth on BARE courtyards (the inflation is a request, not a rule)."""
    parts = B["parts"]
    court = {}
    for r, p in parts.items():
        court[r] = GM.moved(p, *res[r][:3])[1] if r in res else p["court"]
    outside = [r for r in res if not GM.inside(court[r], bounds)]
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
