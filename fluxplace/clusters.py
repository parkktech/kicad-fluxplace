"""IC support clusters: which 2-pin parts belong to which IC, and where they
should sit. Pure text (routediag.load).

Ownership of a 2-pin passive/crystal/ferrite (C R L FB Y X):
  * a pad on a net that carries pins of exactly ONE IC  -> that IC owns the part,
    and the pin it serves is that IC's nearest pad on the net;
  * a pad on a shared rail (ground, or a net with pins of several ICs) -> only
    when the other pad gives no owner: the rail's ICs are narrowed to the part's
    reference-designator block (C416 -> 4xx), then to the numerically nearest
    reference (U410), and any remaining tie is broken so caps spread evenly over
    the rail's ICs by supply-pin count. A rail capacitor is never given to an
    arbitrary far IC;
  * a part whose two pads reach two DIFFERENT ICs is a chain link (series R,
    ferrite), not support.
The spec JSON adds nothing here beyond pin names; it carries no grouping.
"""
import math
import re
from collections import defaultdict

from . import geom as GM
from . import netclass as NC

SUPPORT = re.compile(r"^(C|R|L|FB|Y|X|XO)\d")
LIMIT_DEFAULT = 5.0
LANE_MM = {True: 1.0, False: 0.5}


def block(ref):
    m = re.search(r"(\d+)$", ref)
    return int(m.group(1)) // 100 if m else 0


def number(ref):
    m = re.search(r"(\d+)$", ref)
    return int(m.group(1)) if m else 0


def _is_support(ref, part):
    return bool(SUPPORT.match(ref)) and part["n_pads"] == 2


def ownership(B, cls=None, cons=None, spec=None, min_pads=5):
    """({support_ref: {ic, net, pad (IC pad number), how}}, {link refs})."""
    cls = cls or NC.classify(B, cons, spec)
    parts, pads = B["parts"], NC.net_pads(B)
    ic_refs = {r for r, p in parts.items() if NC.is_ic(r, p, min_pads)}
    ics_on = {n: sorted({p["ref"] for p in ps if p["ref"] in ic_refs}) for n, ps in pads.items()}
    own, links, rail_caps = {}, set(), []

    def nearest_pad(ic, net, near):
        ps = [p for p in pads[net] if p["ref"] == ic]
        return min(ps, key=lambda p: math.hypot(p["x"] - near["x"], p["y"] - near["y"]))

    for ref in sorted(parts):
        part = parts[ref]
        if not _is_support(ref, part) or ref in ic_refs:
            continue
        sides = []
        for p in part["pads"]:
            n = p["net"]
            if not n or NC.is_ground(n):
                sides.append((p, None, "gnd"))
            elif len(ics_on.get(n, ())) == 1 and not (
                    ref.startswith("C") and any(
                        q["ref"] not in ic_refs and not SUPPORT.match(q["ref"]) for q in pads[n])):
                # (a capacitor on a net that also reaches a connector/module is
                # decoupling of a distribution rail, not of the one IC: block rule)
                sides.append((p, ics_on[n][0], "own"))
            elif len(ics_on.get(n, ())) >= 1 and ref.startswith("C"):
                sides.append((p, None, "rail"))
            elif len(ics_on.get(n, ())) > 1:
                sides.append((p, None, "rail"))
            else:
                sides.append((p, None, "none"))
        owners = {s[1] for s in sides if s[1]}
        if len(owners) > 1:
            links.add(ref)
            continue
        if owners:
            ic = next(iter(owners))
            cand = [s for s in sides if s[1]]
            # serve the pad on the highest-weight class net
            p = max((s[0] for s in cand), key=lambda q: cls.get(q["net"], {}).get("weight", 0))
            tgt = nearest_pad(ic, p["net"], p)
            own[ref] = {"ic": ic, "net": p["net"], "pad": tgt["num"], "via": p["num"], "how": "net"}
            continue
        rails = [s[0] for s in sides if s[2] == "rail"]
        if rails:
            rail_caps.append((ref, rails[0]))
    # shared-rail parts: block, then nearest reference, then spread by pin count
    load = defaultdict(int)
    for ref, p in rail_caps:
        net = p["net"]
        cand = ics_on[net]
        same = [c for c in cand if block(c) == block(ref)] if block(ref) else []
        how = "block"
        if not same:
            same, how = cand, "spread"      # flat numbering / no IC in the block: guess
        cand = same
        if len(cand) > 1 and how != "spread":
            d = {c: abs(number(c) - number(ref)) for c in cand}
            m = min(d.values())
            near = [c for c in cand if d[c] == m]
            how = "refdes"
            cand = near
        if len(cand) > 1:
            cand = sorted(cand, key=lambda c: (load[(net, c)] / max(1, sum(
                1 for q in pads[net] if q["ref"] == c)), c))
            how = "spread"
        ic = cand[0]
        load[(net, ic)] += 1
        tgt = nearest_pad(ic, net, p)
        own[ref] = {"ic": ic, "net": net, "pad": tgt["num"], "via": p["num"], "how": how}
    return own, links


def support_limit(info):
    """Max pad-to-pin distance (mm) for a support part on a net of this class."""
    if info and info["class"] in ("switch_node", "bypass") and info["budget"]:
        return info["budget"]
    if info and info["class"] == "clock" and info["reason"] == "crystal/oscillator":
        return info["budget"]
    return LIMIT_DEFAULT


_CAPV = re.compile(r"([\d.]+)\s*([pnuµm]?)F", re.I)
BULK_UF = 4.7


def cap_uf(value):
    m = _CAPV.search(value or "")
    if not m:
        return None
    return float(m.group(1)) * {"p": 1e-6, "n": 1e-3, "u": 1.0, "µ": 1.0, "m": 1e3, "": 1.0}[m.group(2).lower()]


def build(B, cons=None, spec=None):
    """[{ic, supports: [{ref, net, pad, cls, weight, dist, limit}]}], ordered:
    switch-node parts first, then by class weight, then by distance."""
    cls = NC.classify(B, cons, spec)
    own, links = ownership(B, cls, cons, spec)
    parts = B["parts"]
    by = defaultdict(list)
    for ref, o in own.items():
        part = parts[ref]
        sp = next(p for p in part["pads"] if p["num"] == o["via"])
        pin = next(p for p in parts[o["ic"]]["pads"] if p["num"] == o["pad"])
        info = cls.get(o["net"])
        lim = support_limit(info)
        sure, how, ic_, pad_, dist = o["how"] != "spread", o["how"], o["ic"], o["pad"], math.hypot(
            sp["x"] - pin["x"], sp["y"] - pin["y"])
        if o["how"] != "net" and o["how"] != "spread" or o["how"] == "spread":
            # shared rail: a cap already within its limit of ANY IC pin on the rail serves that pin
            best = min(((math.hypot(q["x"] - sp["x"], q["y"] - sp["y"]), q) for q in NC.net_pads(B)[o["net"]]
                        if NC.is_ic(q["ref"], parts[q["ref"]])), key=lambda t: t[0], default=None)
            if best and best[0] <= lim:
                ic_, pad_, dist, sure, how = best[1]["ref"], best[1]["num"], best[0], True, "near"
            elif (cap_uf(part["value"]) or 0) > BULK_UF:
                sure, how = False, "bulk"          # bulk storage is not per-pin bypass
        by[ic_].append({
            "ref": ref, "net": o["net"], "pad": pad_, "via": o["via"], "how": how,
            "sure": sure,
            "cls": info["class"] if info else "signal", "weight": info["weight"] if info else 1.0,
            "dist": dist, "limit": lim})
    out = []
    for ic in sorted(by):
        sup = sorted(by[ic], key=lambda s: (-s["weight"], s["dist"], s["ref"]))
        out.append({"ic": ic, "supports": sup})
    return out, links


def far_parts(cl):
    """Confidently-owned support parts beyond their limit (guessed ownership,
    `how == "spread"`, is never graded)."""
    return [(c["ic"], s) for c in cl for s in c["supports"] if s["sure"] and s["dist"] > s["limit"]]


# ---------------------------------------------------------------- seating
def _lanes(B, ic, side_ref=None):
    """Escape-lane rectangles of every pad of `ic`: from the pad's outer edge
    outward (away from the part centre), as wide as the pad's narrow side."""
    part = B["parts"][ic]
    cx = sum(p["x"] for p in part["pads"]) / len(part["pads"])
    cy = sum(p["y"] for p in part["pads"]) / len(part["pads"])
    fine = _pitch(part) <= 0.5
    L = LANE_MM[fine]
    out = []
    for p in part["pads"]:
        dx, dy = p["x"] - cx, p["y"] - cy
        horiz = abs(dx) * p["ey"] >= abs(dy) * p["ex"] if (p["ex"] and p["ey"]) else abs(dx) >= abs(dy)
        if abs(p["ex"] - p["ey"]) > 0.25 * max(p["ex"], p["ey"]):
            horiz = p["ex"] > p["ey"]
        if horiz:
            s = 1 if dx >= 0 else -1
            out.append((p["x"] + s * p["ex"] if s > 0 else p["x"] - p["ex"] - L, p["y"] - p["ey"],
                        p["x"] + p["ex"] + L if s > 0 else p["x"] - p["ex"], p["y"] + p["ey"]))
        else:
            s = 1 if dy >= 0 else -1
            out.append((p["x"] - p["ex"], p["y"] + p["ey"] if s > 0 else p["y"] - p["ey"] - L,
                        p["x"] + p["ex"], p["y"] + p["ey"] + L if s > 0 else p["y"] - p["ey"]))
    return out


def _pitch(part):
    pts = [(p["x"], p["y"]) for p in part["pads"]]
    best = 999.0
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
            if 0.01 < d < best:
                best = d
    return best


def _outward(part, pin):
    cx = sum(p["x"] for p in part["pads"]) / len(part["pads"])
    cy = sum(p["y"] for p in part["pads"]) / len(part["pads"])
    dx, dy = pin["x"] - cx, pin["y"] - cy
    n = math.hypot(dx, dy) or 1.0
    if abs(dx) >= abs(dy):
        return (1 if dx >= 0 else -1, 0)
    return (0, 1 if dy >= 0 else -1)


def seat(B, cl, bounds=None, step=0.25, reach=8.0, log=None, keepouts=True, isolate=False):
    """Re-seat the movable support parts of cluster `cl` around its (fixed) IC.
    Returns {ref: (x, y, angle)}. Rigid: each part keeps its footprint; only its
    origin and rotation change. Candidate positions are tried in order of
    served-pad-to-pin distance (plus a ground-return term for caps); with `isolate` only
    the IC counts as an obstacle, so the unit is laid out as a compact macro cell and
    placed among the other parts afterwards; the first
    one that stays out of every other courtyard, the IC's escape lanes, the
    board edge and track-forbidding rule areas wins."""
    parts = B["parts"]
    ic = cl["ic"]
    icp = parts[ic]
    occ = GM.Hash()
    for r, p in parts.items():
        if not isolate or r == ic:          # isolate: lay the unit out on its own (macro cell)
            occ.add(r, p["court"], p["side"])
    lanes = _lanes(B, ic)
    zones = [z for z in B["zones"] if z["keepout"] and z["no_tracks"] and z["pts"]] if keepouts else []
    moves = {}
    for s in cl["supports"]:
        ref = s["ref"]
        part = parts[ref]
        if part.get("locked"):
            continue
        pin = next(p for p in icp["pads"] if p["num"] == s["pad"])
        ox, oy = _outward(icp, pin)
        gnd = [p for p in icp["pads"] if NC.is_ground(p["net"])]
        other = [p for p in part["pads"] if p["num"] != s["via"]]
        is_gnd_cap = bool(other and NC.is_ground(other[0]["net"]) and gnd)
        n_d = int(reach / step)
        base = (pin["x"] + ox * pin["ex"], pin["y"] + oy * pin["ey"])
        grid = sorted(((math.hypot(ia * step, it * step), ia * step, it * step)
                       for ia in range(0, n_d + 1) for it in range(-n_d, n_d + 1)))
        pick, best = None, math.inf
        for approx, d, t in grid:
            if approx > best + 2.5:
                break
            cx = base[0] + ox * d + (-oy) * t
            cy = base[1] + oy * d + ox * t
            for ang in (0.0, 90.0, 180.0, 270.0):
                pads, court = GM.moved(part, cx, cy, ang)
                sp = next(p for p in pads if p["num"] == s["via"])
                cost = math.hypot(sp["x"] - pin["x"], sp["y"] - pin["y"])
                if is_gnd_cap:
                    op = next(p for p in pads if p["num"] != s["via"])
                    cost += 0.3 * min(math.hypot(op["x"] - g["x"], op["y"] - g["y"]) for g in gnd)
                if cost >= best:
                    continue
                if bounds and not GM.inside(court, bounds):
                    continue
                if any(GM.overlap(court, ln) for ln in lanes):
                    continue
                if any(True for _ in occ.hits(court, part["side"], 0.0, ignore=(ref,))):
                    continue
                if zones and _in_zone(zones, part, court):
                    continue
                pick, best = (cx, cy, ang, court, cost), cost
        if pick:
            cx, cy, ang, court, cost = pick
            moves[ref] = (round(cx, 4), round(cy, 4), ang)
            occ.add(ref, court, part["side"])
            if log:
                log(f"  {ref} -> ({cx:.2f}, {cy:.2f}) {ang:g} deg  "
                    f"{s['dist']:.1f} -> {cost:.1f} mm (pin {ic}.{s['pad']})")
        elif log:
            log(f"  {ref}: no free seat within {reach:g} mm of {ic}.{s['pad']} - left in place")
    return moves


def _in_zone(zones, part, court):
    pts = [((court[0] + court[2]) / 2, (court[1] + court[3]) / 2), (court[0], court[1]),
           (court[2], court[1]), (court[2], court[3]), (court[0], court[3])]
    return any(part["side"] in z["layers"] and any(GM.poly_has(z["pts"], x, y) for x, y in pts)
               for z in zones)


def reseat(B, cl, everything=False, guess=False, bounds=None, log=None, isolate=False):
    """Seat the far support parts of every cluster around their ICs (ICs never
    move). Mutates the board model B as it goes so later clusters see earlier
    moves. Parts already within their limit stay put unless `everything`;
    guessed ownership (`spread`) is skipped unless `guess`. -> {ref: (x, y, angle)}"""
    allm = {}
    for c in cl:
        sup = [s for s in c["supports"]
               if (everything or s["dist"] > s["limit"]) and (guess or s["sure"])]
        if not sup:
            continue
        if log:
            log(f"{c['ic']}: re-seating {len(sup)} part(s)")
        m = seat(B, {"ic": c["ic"], "supports": sup}, bounds=bounds, log=log, isolate=isolate)
        for ref, (x, y, a) in m.items():
            GM.place(B["parts"][ref], x, y, a)
        allm.update(m)
    return allm


def format_text(cl, links=()):
    L = []
    for c in cl:
        L.append(f"{c['ic']}  ({len(c['supports'])} support part(s))")
        for s in c["supports"]:
            flag = "FAR " if s["dist"] > s["limit"] else "    "
            g = "" if s["sure"] else " (guessed owner)"
            L.append(f"  {flag}{s['ref']:6s} pin {s['pad']:>4s} {s['net']:20s} {s['cls']:11s} "
                     f"{s['dist']:6.1f} mm (limit {s['limit']:g}){g}")
    if links:
        L.append(f"chain links (series parts between two ICs, not support): {', '.join(sorted(links))}")
    return "\n".join(L)
