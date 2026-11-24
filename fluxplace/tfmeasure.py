"""Trace-finish measures (TRACE-FINISH-SPEC 0, 3.1, 4, 7): pure functions on the routediag model (`routediag.load`).

No pcbnew, no board writes. Segments are routediag dicts: {net, a, b, w, mid (arc or None), layer, locked}.
theta is the TURN angle at a two-leg joint (0 straight, 45 mitre, 90 right, > 90 acute); tol = 0.5 deg.
"""
import math
from collections import defaultdict

TOL = 0.5          # deg (spec symbols)
EPS = 1e-3         # mm: joints are matched on coordinates rounded to this
MIN_POLY_SEG = 0.2 # mm: a polyline-arc segment is shorter than this (spec 3.2 C15)
MIN_POLY_N = 4     # same-turn segments that make a polyline arc


def _pt(s, e):
    return s[e]


def _len(s):
    return math.hypot(s["b"][0] - s["a"][0], s["b"][1] - s["a"][1])


def _k(net, layer, p):
    return (net, layer, round(p[0] / EPS) * EPS, round(p[1] / EPS) * EPS)


def _unit(dx, dy):
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 0 else (0.0, 0.0)


def _circle(p1, p2, p3):
    ax, ay = p1; bx, by = p2; cx, cy = p3
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def leave(s, e):
    """Unit vector pointing from the joint at end e ('a' or 'b') INTO the segment (arc tangent when s is an arc)."""
    P = s[e]
    other = s["b"] if e == "a" else s["a"]
    if s.get("mid") is None:
        return _unit(other[0] - P[0], other[1] - P[1])
    C = _circle(s["a"], s["mid"], s["b"])
    if C is None:
        return _unit(other[0] - P[0], other[1] - P[1])
    cx, cy, _r = C
    A = math.atan2(s["a"][1] - cy, s["a"][0] - cx)
    M = math.atan2(s["mid"][1] - cy, s["mid"][0] - cx)
    B = math.atan2(s["b"][1] - cy, s["b"][0] - cx)
    ccw = ((M - A) % (2 * math.pi)) < ((B - A) % (2 * math.pi))
    ang = A if e == "a" else B
    tx, ty = -math.sin(ang), math.cos(ang)          # ccw tangent at this end
    if e == "a":
        return (tx, ty) if ccw else (-tx, -ty)
    return (-tx, -ty) if ccw else (tx, ty)


def _ends(B):
    ends = defaultdict(list)
    segs = B["segs"]
    for i, s in enumerate(segs):
        if _len(s) < 1e-6:
            continue
        ends[_k(s["net"], s["layer"], s["a"])].append((i, "a"))
        ends[_k(s["net"], s["layer"], s["b"])].append((i, "b"))
    return ends


def kind_of(theta):
    if theta <= TOL:
        return "straight"
    if theta > 90 + TOL:
        return "acute"
    if abs(theta - 90) <= TOL:
        return "right"
    if abs(theta - 45) <= TOL:
        return "45"
    if theta < 45 - TOL:
        return "odd_lt45"
    return "odd_45_90"


def _interior(d1, d2):
    return math.degrees(math.acos(max(-1.0, min(1.0, d1[0] * d2[0] + d1[1] * d2[1]))))


def corners(B):
    """Every two-leg joint of same net and layer: {net, layer, x, y, theta, a, b, kind} (segment indices a, b)."""
    segs, out = B["segs"], []
    for key, lst in _ends(B).items():
        if len(lst) != 2 or lst[0][0] == lst[1][0]:
            continue
        (i, e1), (j, e2) = lst
        si, sj = segs[i], segs[j]
        theta = 180.0 - _interior(leave(si, e1), leave(sj, e2))
        p = si[e1]
        out.append({"net": key[0], "layer": key[1], "x": p[0], "y": p[1], "theta": theta,
                    "a": i, "b": j, "kind": kind_of(theta)})
    return out


def junctions(B):
    """Joints of three or more legs (same net, layer): {net, layer, x, y, n, legs}."""
    segs, out = B["segs"], []
    for key, lst in _ends(B).items():
        if len(lst) < 3:
            continue
        i0, e0 = lst[0]
        out.append({"net": key[0], "layer": key[1], "x": segs[i0][e0][0], "y": segs[i0][e0][1],
                    "n": len(lst), "legs": [i for i, _e in lst]})
    return out


def _bends(B):
    return [c for c in corners(B) if c["kind"] != "straight"]


# ---- net classes (spec 0) -------------------------------------------------------------------------------------------

def _pair_family(net, pairs):
    best = None
    for fam in (pairs or {}):
        if net.upper().startswith(fam.upper()) and (best is None or len(fam) > len(best)):
            best = fam
    return best


def classes(B, cons, spec):
    """{net: RF | HSF | HSS | PWR | ANA | GEN}. RF: [rf] nets, else review.is_rf. Pairs: [pairs.<fam>] by name prefix;
    HSF when the family's `maximum_mm` <= 0.2 (spec 0), else HSS. PWR: netclass rail / switch_node, never ground.
    ANA: netclass audio pair or slow role. Everything else GEN."""
    from . import netclass as NC, review as RV
    cons = cons or {}
    cls = NC.classify(B, cons, spec)
    nets = set(cls) | {s["net"] for s in B["segs"] if s.get("net")}
    rf_nets = set((cons.get("rf") or {}).get("nets") or [])
    pairs = cons.get("pairs") or {}
    out = {}
    for n in sorted(nets):
        c = cls.get(n, {})
        fam = _pair_family(n, pairs)
        if n in rf_nets or (not rf_nets and RV.is_rf(n)):
            out[n] = "RF"
        elif fam is not None and (c.get("partner") or c.get("class") == "pair"):      # a differential partner, not just a name
            mx = pairs[fam].get("maximum_mm")
            out[n] = "HSF" if (mx is not None and float(mx) <= 0.2) else "HSS"
        elif NC.is_ground(n):
            out[n] = "GEN"
        elif c.get("class") in ("rail", "switch_node") and not NC.is_ground(n):
            out[n] = "PWR"
        elif "audio" in str(c.get("reason", "")) or c.get("class") == "slow":
            out[n] = "ANA"
        else:
            out[n] = "GEN"
    return out


# ---- rule measures: each returns sites [{net, layer, x, y, state, ...}], state 'open' or 'already' -------------------

def _site(c, state, **extra):
    d = {"net": c["net"], "layer": c["layer"], "x": c["x"], "y": c["y"], "state": state}
    d.update(extra)
    return d


def c1_sites(B):
    return [_site(c, "open" if c["kind"] == "acute" else "already", theta=c["theta"]) for c in _bends(B)]


def c2_sites(B):
    return [_site(c, "open", theta=c["theta"]) for c in corners(B) if c["kind"] in ("right", "odd_45_90")]


def c4_sites(B):
    bend_pts = {(c["net"], c["layer"], round(c["x"], 3), round(c["y"], 3)) for c in _bends(B)}
    out = []
    for s in B["segs"]:
        if s.get("mid") is not None or _len(s) < 1e-6:
            continue
        ka = (s["net"], s["layer"], round(s["a"][0], 3), round(s["a"][1], 3))
        kb = (s["net"], s["layer"], round(s["b"][0], 3), round(s["b"][1], 3))
        if ka in bend_pts and kb in bend_pts:
            out.append({"net": s["net"], "layer": s["layer"], "x": s["a"][0], "y": s["a"][1],
                        "state": "open" if _len(s) < 2 * s["w"] else "already", "len": _len(s)})
    return out


def c13_sites(B):
    return [_site(c, "already", theta=c["theta"]) for c in corners(B) if c["kind"] == "odd_lt45"]


def c14_sites(B):
    out = []
    for j in junctions(B):
        dirs = [leave(B["segs"][i], e) for i in j["legs"] for e in ("a", "b")
                if abs(B["segs"][i][e][0] - j["x"]) < EPS and abs(B["segs"][i][e][1] - j["y"]) < EPS]
        mins = min((_interior(dirs[p], dirs[q]) for p in range(len(dirs)) for q in range(p + 1, len(dirs))), default=180.0)
        out.append(_site(j, "open" if mins < 90 else "already", min_interior=mins))
    return out


def c5_sites(B):
    segs = B["segs"]
    return [_site(c, "already" if (segs[c["a"]].get("mid") is not None or segs[c["b"]].get("mid") is not None) else "open",
                  theta=c["theta"]) for c in _bends(B)]


def _chains(B):
    """Polyline-arc chains: runs of >= MIN_POLY_N short straight segments joined at 2-leg joints, all turning one way."""
    segs = B["segs"]
    short = {i for i, s in enumerate(segs) if s.get("mid") is None and 1e-6 < _len(s) < MIN_POLY_SEG}
    links = defaultdict(list)
    for key, lst in _ends(B).items():
        if len(lst) == 2 and lst[0][0] in short and lst[1][0] in short:
            (i, ei), (j, ej) = lst
            links[i].append((j, ei, ej))
            links[j].append((i, ej, ei))
    seen, out = set(), []
    for start in sorted(links):
        if start in seen or len(links[start]) != 1:
            continue
        walk, cur = [[start, None, None]], start          # [segment, entry end, exit end]
        seen.add(start)
        while True:
            nxt = [(j, ei, ej) for (j, ei, ej) in links[cur] if j not in seen]
            if not nxt:
                break
            j, ei, ej = nxt[0]
            walk[-1][2] = ei
            walk.append([j, ej, None])
            seen.add(j)
            cur = j
        if len(walk) < MIN_POLY_N:
            continue
        signs, ok = [], True
        for k in range(1, len(walk)):
            (pi, _pe, pexit), (ni, nentry, _nx) = walk[k - 1], walk[k]
            t_in = tuple(-v for v in leave(segs[pi], pexit))
            t_out = leave(segs[ni], nentry)
            cr = t_in[0] * t_out[1] - t_in[1] * t_out[0]
            dot = t_in[0] * t_out[0] + t_in[1] * t_out[1]
            theta = math.degrees(math.atan2(abs(cr), dot))
            if theta <= TOL or theta > 45 + TOL:
                ok = False
            signs.append(1 if cr > 0 else -1)
        if ok and len(set(signs)) == 1:
            p = segs[walk[0][0]]["a"]
            out.append({"net": segs[walk[0][0]]["net"], "layer": segs[walk[0][0]]["layer"], "x": p[0], "y": p[1],
                        "n": len(walk)})
    return out


def c15_sites(B):
    out = [_site({"net": s["net"], "layer": s["layer"], "x": s["a"][0], "y": s["a"][1]}, "already")
           for s in B["segs"] if s.get("mid") is not None]
    out += [_site(c, "open", n=c["n"]) for c in _chains(B)]
    return out


def m1_sites(B, min_deg):
    from . import acidtrap as AT
    segs = [{"net": s["net"], "layer": s["layer"], "x1": s["a"][0], "y1": s["a"][1], "x2": s["b"][0], "y2": s["b"][1]}
            for s in B["segs"]]
    return [_site({"net": j["net"], "layer": j["layer"], "x": j["x"], "y": j["y"]}, "open", deg=j["deg"])
            for j in AT.acute_junctions(segs, min_deg=min_deg)]


def m9_sites(B):
    out, seen = [], set()
    for s in B["segs"]:
        key = (s["net"], s["layer"], tuple(sorted([(round(s["a"][0], 3), round(s["a"][1], 3)),
                                                   (round(s["b"][0], 3), round(s["b"][1], 3))])))
        zero = _len(s) < 1e-6
        dup = key in seen
        seen.add(key)
        out.append({"net": s["net"], "layer": s["layer"], "x": s["a"][0], "y": s["a"][1],
                    "state": "open" if (zero or dup) else "already"})
    return out


# ---- score (spec 4) -------------------------------------------------------------------------------------------------

COUNT = {"already": 1.0, "applied": 1.0, "open": 0.0, "not_possible": 0.0, "refused": 0.0}


def score_of(states):
    """(score, eligible) for a list of per-site states; waived sites are not eligible. eligible 0 -> (None, 0)."""
    states = [s for s in states if s != "waived"]
    if not states:
        return None, 0
    return sum(COUNT[s] for s in states) / len(states), len(states)


def score_flags(prev, cur):
    """A score that rises while the eligible count falls is flagged, never silently accepted (T1a item 6)."""
    flags = []
    if prev and cur and cur["score"] > prev["score"] and cur["eligible"] < prev["eligible"]:
        flags.append("RISE_ON_FALLING_ELIGIBLE")
    return flags
