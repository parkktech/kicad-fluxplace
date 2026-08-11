"""Placement COMPACTION: shrink a known-good placement instead of re-placing.

The builder places at routable-but-loose density; this module takes a board
whose relative arrangement already routed (or gated at overflow 0) and pulls
every unlocked part toward an anchor, preserving the radial order of the
original placement, then legalizes and gravity-packs. Validated on the UTV
comms bridge board: 140x110 -> 76x69 routed clean (KRT, 0 unrouted) in one
pass; see NEXT.md 2026-08-10.

Hard-won rules baked in (do not relax):
- Obstacle zones (plug-on module shadows) must stay CLEAR of unlocked
  same-side parts: stuffing a SoM's under-module zone with passives strangles
  the connector escape area and the router fails catastrophically (UTV r2/r3:
  30+ unrouted at the same density that routed clean with the zone empty).
- THT parts avoid obstacles on EITHER side (drills penetrate).
- Locked parts never move and are packed around.

Coordinates are BODY CENTERS (the parts model of kicad_io.read_board).
"""
import math

__all__ = ["compact", "parse_obstacles"]


def parse_obstacles(specs, log=print):
    """--obstacle 'X:Y:W:H[:F|B]' strings -> [{x,y,w,h,side}] (centers, mm)."""
    out = []
    for spec in specs or []:
        fields = str(spec).split(":")
        try:
            ox, oy, ow, oh = [float(v) for v in fields[:4]]
        except ValueError:
            log(f"    ! bad --obstacle '{spec}' (want X:Y:W:H[:F|B] in mm) — skipped")
            continue
        side = fields[4].upper() if len(fields) > 4 else "F"
        out.append(dict(x=ox, y=oy, w=ow, h=oh, side=side))
    return out


def _model(parts):
    P = {}
    for ref, p in parts.items():
        P[ref] = dict(
            c=[p["x"], p["y"]], hw=p["w"] / 2.0, hh=p["h"] / 2.0,
            side=p.get("side", "F"),
            # any real drill penetrates both sides; fall back to the tht flag
            tht=(p.get("drills", 0) > 0 or p.get("tht", False)),
            locked=p.get("locked", False))
    return P


def _blocks(p, ob, tht_bands):
    """Does obstacle `ob` constrain part `p`?"""
    if p["locked"]:
        return False
    if p["tht"]:
        return True
    return p["side"] == ob["side"]


def _keepout_ok(p, obstacles, g, tht_bands):
    for ob in obstacles:
        if not _blocks(p, ob, tht_bands):
            continue
        mx = (float("inf") if (tht_bands and p["tht"])
              else ob["w"] / 2.0 + p["hw"] + g)
        my = ob["h"] / 2.0 + p["hh"] + g
        if abs(p["c"][0] - ob["x"]) < mx and abs(p["c"][1] - ob["y"]) < my:
            return False
    return True


def compact(parts, sx, sy, anchor=None, gap=0.42, pack=5, obstacles=(),
            tht_bands=False, iters=600, log=print):
    """Scale unlocked parts toward `anchor`, legalize, gravity-pack.

    Returns (pos, stats): pos {ref: (x, y)} body centers for ALL parts
    (locked ones unchanged), stats dict.
    """
    P = _model(parts)
    refs = sorted(P)
    g = gap
    obstacles = list(obstacles)

    if anchor is None:
        lk = [p for p in P.values() if p["locked"]]
        src = lk if lk else list(P.values())
        anchor = (sum(p["c"][0] for p in src) / len(src),
                  sum(p["c"][1] for p in src) / len(src))
    ax, ay = anchor

    # ---- scale toward anchor ---------------------------------------------
    for p in P.values():
        if p["locked"]:
            continue
        p["c"][0] = ax + sx * (p["c"][0] - ax)
        p["c"][1] = ay + sy * (p["c"][1] - ay)

    def collides(p, q):
        if p["side"] != q["side"] and not (p["tht"] or q["tht"]):
            return False
        return (abs(p["c"][0] - q["c"][0]) < p["hw"] + q["hw"] + g and
                abs(p["c"][1] - q["c"][1]) < p["hh"] + q["hh"] + g)

    # ---- soft legalize: pairwise push-apart + obstacle keep-out ----------
    def soft_pass(iters):
        nonlocal nudges
        it = 0
        for it in range(iters):
            moved = False
            for i, r1 in enumerate(refs):
                p = P[r1]
                for r2 in refs[i + 1:]:
                    q = P[r2]
                    if not collides(p, q):
                        continue
                    ox = p["hw"] + q["hw"] + g - abs(p["c"][0] - q["c"][0])
                    oy = p["hh"] + q["hh"] + g - abs(p["c"][1] - q["c"][1])
                    axis = 0 if ox < oy else 1
                    pen = (ox if axis == 0 else oy) + 0.01
                    s = 1.0 if p["c"][axis] < q["c"][axis] else -1.0
                    wp = 0.0 if p["locked"] else (0.5 if not q["locked"] else 1.0)
                    wq = 0.0 if q["locked"] else (0.5 if not p["locked"] else 1.0)
                    if wp == wq == 0.0:
                        continue
                    p["c"][axis] -= s * pen * wp
                    q["c"][axis] += s * pen * wq
                    moved = True
                    nudges += 1
                for ob in obstacles:
                    if not _blocks(p, ob, tht_bands):
                        continue
                    band = tht_bands and p["tht"]
                    exx = ob["w"] / 2 + p["hw"] + g - abs(p["c"][0] - ob["x"])
                    exy = ob["h"] / 2 + p["hh"] + g - abs(p["c"][1] - ob["y"])
                    if exy > 0 and (band or exx > 0):
                        if band or exy <= exx:
                            p["c"][1] += math.copysign(exy + 0.01, p["c"][1] - ob["y"])
                        else:
                            p["c"][0] += math.copysign(exx + 0.01, p["c"][0] - ob["x"])
                        moved = True
                        nudges += 1
            if not moved:
                break
        return it + 1

    # ---- hard resolve: spiral-relocate anything still stuck --------------
    def free_at(ref, x, y):
        p = dict(P[ref], c=[x, y])
        return _keepout_ok(p, obstacles, g, tht_bands) and not any(
            collides(p, P[r2]) for r2 in refs if r2 != ref)

    def hard_pass():
        moved = 0
        for r1 in refs:
            p = P[r1]
            if p["locked"]:
                continue
            if _keepout_ok(p, obstacles, g, tht_bands) and not any(
                    collides(p, P[r2]) for r2 in refs if r2 != r1):
                continue
            placed = False
            # spiral out to board scale — a 58 mm cap starved dense boards
            for radius in [k * 1.5 for k in range(1, 80)]:
                steps = max(12, int(radius * 2.5))
                for k in range(steps):
                    th = 2 * math.pi * k / steps
                    if free_at(r1, p["c"][0] + radius * math.cos(th),
                               p["c"][1] + radius * math.sin(th)):
                        p["c"] = [p["c"][0] + radius * math.cos(th),
                                  p["c"][1] + radius * math.sin(th)]
                        placed = True
                        moved += 1
                        break
                if placed:
                    break
        return moved

    def resid_count():
        return sum(1 for i, r1 in enumerate(refs) for r2 in refs[i + 1:]
                   if collides(P[r1], P[r2]))

    # ---- converge: alternate soft and hard until clean -------------------
    nudges = 0
    it = hard = 0
    for cycle in range(4):
        it += soft_pass(iters)
        if resid_count() == 0:
            break
        hard += hard_pass()
        if resid_count() == 0:
            break

    # ---- gravity pack: slide toward anchor, nearest-first ----------------
    def blocked_at(ref, axis, want):
        p = P[ref]
        lo = min(p["c"][axis], want)
        hi = max(p["c"][axis], want)
        oth = 1 - axis
        h = (p["hw"], p["hh"])
        for r2 in refs:
            if r2 == ref:
                continue
            q = P[r2]
            if q["side"] != p["side"] and not (p["tht"] or q["tht"]):
                continue
            qh = (q["hw"], q["hh"])
            if abs(p["c"][oth] - q["c"][oth]) >= h[oth] + qh[oth] + g:
                continue
            edge = h[axis] + qh[axis] + g
            if q["c"][axis] >= p["c"][axis] and q["c"][axis] - edge < hi:
                hi = max(lo, q["c"][axis] - edge)
            if q["c"][axis] <= p["c"][axis] and q["c"][axis] + edge > lo:
                lo = min(hi, q["c"][axis] + edge)
        for ob in obstacles:
            if not _blocks(p, ob, tht_bands):
                continue
            mh = [ob["w"] / 2 + h[0] + g, ob["h"] / 2 + h[1] + g]
            if tht_bands and p["tht"]:
                mh[0] = float("inf")
            oc = (ob["x"], ob["y"])
            if abs(p["c"][oth] - oc[oth]) < mh[oth]:
                if oc[axis] >= p["c"][axis]:
                    hi = min(hi, max(lo, oc[axis] - mh[axis]))
                else:
                    lo = max(lo, min(hi, oc[axis] + mh[axis]))
        return max(lo, min(hi, want))

    slid = 0.0
    for _ in range(pack):
        order = sorted((r for r in refs if not P[r]["locked"]),
                       key=lambda r: math.hypot(P[r]["c"][0] - ax,
                                                P[r]["c"][1] - ay))
        for axis, want in ((0, ax), (1, ay)):
            for ref in order:
                new = blocked_at(ref, axis, want)
                slid += abs(new - P[ref]["c"][axis])
                P[ref]["c"][axis] = new

    # pack starts from a clean state and cannot create overlaps, but belt
    # and braces: one more soft/hard cycle if anything slipped through
    if resid_count():
        it += soft_pass(iters)
        hard += hard_pass()
    resid = resid_count()
    xs0 = [p["c"][0] - p["hw"] for p in P.values()]
    ys0 = [p["c"][1] - p["hh"] for p in P.values()]
    xs1 = [p["c"][0] + p["hw"] for p in P.values()]
    ys1 = [p["c"][1] + p["hh"] for p in P.values()]
    for ob in obstacles:  # module shadows stay on-board
        xs0.append(ob["x"] - ob["w"] / 2); ys0.append(ob["y"] - ob["h"] / 2)
        xs1.append(ob["x"] + ob["w"] / 2); ys1.append(ob["y"] + ob["h"] / 2)
    stats = dict(nudges=nudges, iters=it + 1, hard=hard, resid=resid,
                 slid=slid, anchor=(ax, ay),
                 extent=(min(xs0), min(ys0), max(xs1), max(ys1)))
    return {r: tuple(P[r]["c"]) for r in refs}, stats
