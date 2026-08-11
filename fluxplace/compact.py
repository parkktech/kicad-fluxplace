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
import re

__all__ = ["compact", "parse_obstacles", "pick_flips", "assign_regions",
           "cluster_anchor_map", "constraint_seed"]


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
            # small passives pack at production spacing; big parts keep air
            gs=0.55 if max(p["w"], p["h"]) < 2.2 else 1.0,
            # any real drill penetrates both sides; fall back to the tht flag
            tht=(p.get("drills", 0) > 0 or p.get("tht", False)),
            locked=p.get("locked", False))
    return P


def _g(p, q, g):
    """pair gap: scaled down when BOTH parts are small passives."""
    return g * max(p.get("gs", 1.0), q.get("gs", 1.0))


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


def assign_regions(parts, regions, members_spec=None):
    """Attach members to placement regions (Quilter model: membership is a
    HARD constraint, and region linkage pins the part's side).

    regions: [{name, side, bbox}] from kicad_io.read_rule_areas.
    members_spec: {name: "auto" | [refs...]} — "auto" claims every unlocked
    part whose center currently sits inside the region bbox (Altium-Room
    style auto-association). Returns regions with `members` sets."""
    out = []
    for rg in regions:
        spec = (members_spec or {}).get(rg["name"], "auto")
        x0, y0, x1, y1 = rg["bbox"]
        if spec == "auto":
            members = {r for r, p in parts.items()
                       if not p.get("locked")
                       and x0 <= p["x"] <= x1 and y0 <= p["y"] <= y1}
        else:
            members = {r for r in spec if r in parts}
        out.append(dict(rg, members=members))
    return out


def cluster_anchor_map(parts):
    """Anchor stickiness (Quilter's tier-2 control): a cluster containing
    locked part(s) gravitates toward THEIR centroid instead of the global
    anchor. Clusters come from schematic sheets. {ref: (ax, ay)}."""
    by_sheet = {}
    for r, p in parts.items():
        by_sheet.setdefault(p.get("sheet", "root"), []).append(r)
    amap = {}
    for sheet, refs in by_sheet.items():
        lk = [r for r in refs if parts[r].get("locked")]
        if not lk:
            continue
        ax = sum(parts[r]["x"] for r in lk) / len(lk)
        ay = sum(parts[r]["y"] for r in lk) / len(lk)
        for r in refs:
            if not parts[r].get("locked"):
                amap[r] = (ax, ay)
    return amap


def pick_flips(parts, mode, obstacles=(), comp=None):
    """Side-exploration candidate set: which unlocked front parts to move to
    the BACK side. Judged by the gate + freerouting, never assumed good.

      decaps    bypass caps (from comprehension when given, else plane-only
                heuristic) — the classic under-the-IC via-decoupling move
      passives  every small (<2.2 mm) unlocked front SMD passive

    THT parts never flip (drills pierce). Parts currently over a B-side
    obstacle shadow DO flip — compact()'s obstacle constraint relocates
    back-side parts out of shadows during legalization, and the router gate
    judges the result. (v1 pre-excluded them and flipped nothing on boards
    whose center IS the module shadow.)"""
    if mode in (None, "", "none"):
        return []
    decap_refs = None
    if comp:
        decap_refs = {b["cap"] for b in comp.get("bypass_caps", ())}
    out = []
    for r, p in sorted(parts.items()):
        if p.get("locked") or p.get("side", "F") != "F":
            continue
        if p.get("tht") or p.get("drills", 0) > 0:
            continue
        # size guard uses the FULL footprint bbox (silk included): an 0603
        # measures ~3 mm there. 3.5 admits 0402-0805 passives, still blocks
        # electrolytics and anything with a real body
        if max(p["w"], p["h"]) >= 3.5:
            continue
        if mode == "decaps":
            if decap_refs is not None:
                if r not in decap_refs:
                    continue
            elif not re.match(r"^C\d+$", r):
                continue
        elif mode == "passives":
            if not re.match(r"^[RC]\d+$", r):
                continue
        else:
            continue
        out.append(r)
    return out


def constraint_seed(parts, comp, log=print, obstacles=()):
    """PRC-driven pre-seed: before compacting, walk unlocked members of the
    placement constraints next to their anchor part — converter hot-loop
    members ring their U, diff-pair series elements pair up side by side,
    crystal clusters hug the driver. The seed is only a proposal: soft/hard
    legalization resolves collisions and the router gate has the last word.
    Returns the number of parts moved."""
    moved = 0

    def _ring_slots(aref, member_dims):
        a = parts[aref]
        slots = []
        k = 0
        for (mw, mh) in member_dims:
            side = k % 4
            step = (k // 4 + 1)
            if side == 0:
                slots.append((a["x"] + a["w"] / 2 + mw / 2 + 0.4,
                              a["y"] + (step - 1) * (mh + 0.4)))
            elif side == 1:
                slots.append((a["x"] - a["w"] / 2 - mw / 2 - 0.4,
                              a["y"] + (step - 1) * (mh + 0.4)))
            elif side == 2:
                slots.append((a["x"], a["y"] + a["h"] / 2 + mh / 2 + 0.4))
            else:
                slots.append((a["x"], a["y"] - a["h"] / 2 - mh / 2 - 0.4))
            k += 1
        return slots

    def _tht_banned(m, x, y):
        """A THT member must not be seeded into an obstacle band (its pins
        pierce both sides); a seed there just gets relocated to the board
        edge and stretches the extent (q1: C12 walked to U1 -> y=67)."""
        p = parts[m]
        if not (p.get("tht") or p.get("drills", 0) > 0):
            return False
        for ob in obstacles:
            if (abs(x - ob["x"]) < ob["w"] / 2 + p["w"] / 2 and
                    abs(y - ob["y"]) < ob["h"] / 2 + p["h"] / 2):
                return True
        return False

    def _seed_group(aref, members):
        nonlocal moved
        if aref not in parts:
            return
        live = [m for m in members
                if m in parts and not parts[m].get("locked")]
        if not live:
            return
        slots = _ring_slots(aref, [(parts[m]["w"], parts[m]["h"])
                                   for m in live])
        for m, (x, y) in zip(live, slots):
            if _tht_banned(m, x, y):
                continue
            if abs(parts[m]["x"] - x) + abs(parts[m]["y"] - y) > 4.0:
                parts[m]["x"] = x
                parts[m]["y"] = y
                moved += 1

    for cv in comp.get("converters", ()):
        _seed_group(cv["u"], [m for m in cv.get("hot_loop", ())
                              if m != cv["u"]])
    for xt in comp.get("crystals", ()):
        _seed_group(xt["crystal"], list(xt.get("series_r", ())) +
                    list(xt.get("load_caps", ())))
    for pr in comp.get("diff_pairs", ()):
        segs = set(pr.get("segments", ()))
        if len(segs) <= 2:
            continue
        series = sorted(r for r, p in parts.items()
                        if re.match(r"^[RC]\d+$", r)
                        and not p.get("locked")
                        and len(set(p.get("pins", {})) & segs) == 2)
        # side-by-side: snap partner next to the first of each pair
        for i in range(0, len(series) - 1, 2):
            a, b = series[i], series[i + 1]
            tx = parts[a]["x"] + parts[a]["w"] / 2 + parts[b]["w"] / 2 + 0.35
            ty = parts[a]["y"]
            if abs(parts[b]["x"] - tx) + abs(parts[b]["y"] - ty) > 2.0:
                parts[b]["x"] = tx
                parts[b]["y"] = ty
                moved += 1
    return moved


def compact(parts, sx, sy, anchor=None, gap=0.42, pack=5, obstacles=(),
            tht_bands=False, iters=600, log=print, regions=(),
            bounds=None, cluster_anchors=None):
    """Scale unlocked parts toward `anchor`, legalize, gravity-pack.

    regions: [{name, side, bbox, members}] — HARD placement regions (Quilter
    semantics): members never leave their bbox and are pinned to the region's
    side; violations are counted in stats["outside"] rather than silently
    spread.
    bounds: (x0, y0, x1, y1) — HARD outline: every unlocked part must fit
    inside; violations counted in stats["outside"] (the caller fails loudly —
    Quilter treats the outline as a constraint to satisfy, not an output).
    cluster_anchors: {ref: (ax, ay)} per-part gravity override (anchoring).

    Returns (pos, stats): pos {ref: (x, y)} body centers for ALL parts
    (locked ones unchanged), stats dict.
    """
    P = _model(parts)
    refs = sorted(P)
    g = gap
    obstacles = list(obstacles)
    cluster_anchors = dict(cluster_anchors or {})

    region_of = {}
    for rg in regions:
        for r in rg.get("members", ()):
            if r in P:
                region_of[r] = rg
                if rg.get("side") in ("F", "B"):
                    P[r]["side"] = rg["side"]   # region linkage pins the side

    def _clamp(ref):
        """Pull a part inside its region / the hard bounds (center clamp)."""
        p = P[ref]
        if p["locked"]:
            return
        boxes = []
        rg = region_of.get(ref)
        if rg:
            boxes.append(rg["bbox"])
        if bounds is not None:
            boxes.append(bounds)
        for (x0, y0, x1, y1) in boxes:
            p["c"][0] = min(max(p["c"][0], x0 + p["hw"]), x1 - p["hw"])
            p["c"][1] = min(max(p["c"][1], y0 + p["hh"]), y1 - p["hh"])

    def _inside_ok(ref, x, y):
        p = P[ref]
        boxes = []
        rg = region_of.get(ref)
        if rg:
            boxes.append(rg["bbox"])
        if bounds is not None and not p["locked"]:
            boxes.append(bounds)
        for (x0, y0, x1, y1) in boxes:
            if not (x0 + p["hw"] <= x <= x1 - p["hw"] and
                    y0 + p["hh"] <= y <= y1 - p["hh"]):
                return False
        return True

    if anchor is None:
        lk = [p for p in P.values() if p["locked"]]
        src = lk if lk else list(P.values())
        anchor = (sum(p["c"][0] for p in src) / len(src),
                  sum(p["c"][1] for p in src) / len(src))
    ax, ay = anchor

    # ---- scale toward anchor (per-cluster anchors when given) ------------
    for r in refs:
        p = P[r]
        if p["locked"]:
            continue
        cax, cay = cluster_anchors.get(r, (ax, ay))
        p["c"][0] = cax + sx * (p["c"][0] - cax)
        p["c"][1] = cay + sy * (p["c"][1] - cay)
        _clamp(r)

    def collides(p, q):
        if p["side"] != q["side"] and not (p["tht"] or q["tht"]):
            return False
        gg = _g(p, q, g)
        return (abs(p["c"][0] - q["c"][0]) < p["hw"] + q["hw"] + gg and
                abs(p["c"][1] - q["c"][1]) < p["hh"] + q["hh"] + gg)

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
                    gg = _g(p, q, g)
                    ox = p["hw"] + q["hw"] + gg - abs(p["c"][0] - q["c"][0])
                    oy = p["hh"] + q["hh"] + gg - abs(p["c"][1] - q["c"][1])
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
                if region_of.get(r1) or bounds is not None:
                    _clamp(r1)     # regions/bounds are HARD — re-pin every pass
            if not moved:
                break
        return it + 1

    # ---- hard resolve: spiral-relocate anything still stuck --------------
    def free_at(ref, x, y):
        if not _inside_ok(ref, x, y):
            return False
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
            gg = _g(p, q, g)
            if abs(p["c"][oth] - q["c"][oth]) >= h[oth] + qh[oth] + gg:
                continue
            edge = h[axis] + qh[axis] + gg
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

    def _target(r):
        return cluster_anchors.get(r, (ax, ay))

    for _ in range(pack):
        order = sorted((r for r in refs if not P[r]["locked"]),
                       key=lambda r: math.hypot(P[r]["c"][0] - _target(r)[0],
                                                P[r]["c"][1] - _target(r)[1]))
        for axis in (0, 1):
            for ref in order:
                new = blocked_at(ref, axis, _target(ref)[axis])
                slid += abs(new - P[ref]["c"][axis])
                P[ref]["c"][axis] = new
                if region_of.get(ref) or bounds is not None:
                    _clamp(ref)

    # pack starts from a clean state and cannot create overlaps, but belt
    # and braces: one more soft/hard cycle if anything slipped through
    if resid_count():
        it += soft_pass(iters)
        hard += hard_pass()
    resid = resid_count()
    # hard-constraint audit: a member outside its region, or any unlocked
    # part outside the hard bounds, is a FAILURE to report loudly — never
    # silently spread past a constraint (Quilter fails the job instead)
    outside = sum(1 for r in refs
                  if (region_of.get(r) or bounds is not None)
                  and not P[r]["locked"]
                  and not _inside_ok(r, P[r]["c"][0], P[r]["c"][1]))
    xs0 = [p["c"][0] - p["hw"] for p in P.values()]
    ys0 = [p["c"][1] - p["hh"] for p in P.values()]
    xs1 = [p["c"][0] + p["hw"] for p in P.values()]
    ys1 = [p["c"][1] + p["hh"] for p in P.values()]
    for ob in obstacles:  # module shadows stay on-board
        xs0.append(ob["x"] - ob["w"] / 2); ys0.append(ob["y"] - ob["h"] / 2)
        xs1.append(ob["x"] + ob["w"] / 2); ys1.append(ob["y"] + ob["h"] / 2)
    stats = dict(nudges=nudges, iters=it + 1, hard=hard, resid=resid,
                 slid=slid, anchor=(ax, ay), outside=outside,
                 sides={r: P[r]["side"] for r in refs},
                 extent=(min(xs0), min(ys0), max(xs1), max(ys1)))
    return {r: tuple(P[r]["c"]) for r in refs}, stats
