"""Trace-finish measures (TRACE-FINISH-SPEC 0, 3.1, 4, 7): pure functions on the routediag model (`routediag.load`).

No pcbnew, no board writes. Segments are routediag dicts: {net, a, b, w, mid (arc or None), layer, locked}.
theta is the TURN angle at a two-leg joint (0 straight, 45 mitre, 90 right, > 90 acute); tol = 0.5 deg.
"""
import json
import math
import os
from collections import defaultdict

TOL = 0.5          # deg (spec symbols)
EPS = 1e-4         # mm: joints are matched on coordinates rounded to this grid (W15: 1e-3 split / merged joints, 1e-5 and 1e-4 give the
                   # same counts; KiCad coordinates are nm, so a joint is exact and 0.1 um is already generous)
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


def _ckey(s):
    """identity of a piece of copper: net, layer, unordered end points (and mid for an arc) on the EPS grid."""
    q = lambda p: (round(p[0] / EPS), round(p[1] / EPS))
    return (s["net"], s["layer"], tuple(sorted([q(s["a"]), q(s["b"])])), q(s["mid"]) if s.get("mid") is not None else None)


def live(B):
    """indices of segments that count as copper for corners and junctions: not zero length, first of any exact duplicate.
    Degenerate ones (T2 deletes them, M9) would otherwise make backtrack 'acute' corners and phantom legs; they are
    reported by degenerate() and m9_sites()."""
    seen, out = set(), []
    for i, s in enumerate(B["segs"]):
        if _len(s) < 1e-6:
            continue
        k = _ckey(s)
        if k in seen:
            continue
        seen.add(k)
        out.append(i)
    return out


def degenerate(B):
    """{'zero': n, 'duplicate': n}: the hygiene counts behind M9."""
    keep = set(live(B))
    zero = sum(1 for s in B["segs"] if _len(s) < 1e-6)
    return {"zero": zero, "duplicate": len(B["segs"]) - zero - len(keep)}


def _ends(B):
    ends = defaultdict(list)
    segs = B["segs"]
    for i in live(B):
        s = segs[i]
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
    HSF when the family's `skew_max_mm` (D71cv maximum; `maximum_mm` accepted) <= 0.2 (spec 0), else HSS. PWR: netclass rail / switch_node, never ground.
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
            pf = pairs[fam]
            mx = pf.get("skew_max_mm", pf.get("maximum_mm"))          # D71cv maximum: the key the constraints carry
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
        k = _ckey(s)
        bad = _len(s) < 1e-6 or k in seen
        seen.add(k)
        out.append({"net": s["net"], "layer": s["layer"], "x": s["a"][0], "y": s["a"][1], "state": "open" if bad else "already"})
    return out


# ---- score (spec 4) -------------------------------------------------------------------------------------------------

COUNT = {"already": 1.0, "applied": 1.0, "fallback": 0.5, "open": 0.0, "not_possible": 0.0, "refused": 0.0}


def score_of(states):
    """(score, eligible) for a list of per-site states; waived sites are not eligible. eligible 0 -> (None, 0)."""
    states = [s for s in states if s != "waived"]
    if not states:
        return None, 0
    return sum(COUNT[s] for s in states) / len(states), len(states)


def class_scores(rows, rules):
    """{cls: score, "board": score}: mean of `value` weighted by the rule's `weight` over rows with `limit > 0`;
    board = over all rows of every class (None when nothing is eligible); a class with no eligible row has no key."""
    w = {r["id"]: r.get("weight", 1) for r in rules}
    acc, out = {}, {}
    tot_v = tot_w = 0.0
    for r in rows:
        if not r.get("limit") or r.get("value") is None:
            continue
        wt = w.get(r["id"], 1)
        a = acc.setdefault(r["cls"], [0.0, 0.0])
        a[0] += r["value"] * wt
        a[1] += wt
        tot_v += r["value"] * wt
        tot_w += wt
    for c, (v, wt) in acc.items():
        out[c] = round(v / wt, 3)
    out["board"] = round(tot_v / tot_w, 3) if tot_w else None
    return out


def score_flags(prev, cur):
    """A score that rises while the eligible count falls is flagged, never silently accepted (T1a item 6)."""
    flags = []
    if prev and cur and cur["score"] > prev["score"] and cur["eligible"] < prev["eligible"]:
        flags.append("RISE_ON_FALLING_ELIGIBLE")
    return flags


# ======================================================================================================================
# T1b: context, shared indexes, and one measure per remaining rule (TRACE-FINISH-SPEC 2, 3.2; RECIPE-TRACE-FINISH T1b).
# Sites are {net, layer, x, y, state}; state is open / already, or not_possible with `reason` (report-only F7 rules: the
# stage cannot fix the site, reason NEEDS_ROUTE, `fix` says what the router would do). Nothing here names a ref, net or layer.
# ======================================================================================================================

import statistics

PAIR_CLS = ("HSF", "HSS")
EPS_G = 0.005       # spec symbol `eps` (KiCad max_error)


def _s(net, layer, x, y, state, **kw):
    d = {"net": net, "layer": layer, "x": x, "y": y, "state": state}
    d.update(kw)
    return d


def _np(net, layer, x, y, fix="", **kw):
    """a site the stage cannot move: report-only, needs the router (reason NEEDS_ROUTE) with a proposed fix."""
    return _s(net, layer, x, y, "not_possible", reason="NEEDS_ROUTE", fix=fix, **kw)


def _dist(p, q):
    return math.hypot(p[0] - q[0], p[1] - q[1])


def _pt_seg(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _arc_len(s):
    if s.get("mid") is None:
        return _len(s)
    C = _circle(s["a"], s["mid"], s["b"])
    if C is None:
        return _len(s)
    cx, cy, R = C
    A, M, Bq = (math.atan2(p[1] - cy, p[0] - cx) for p in (s["a"], s["mid"], s["b"]))
    ccw = ((M - A) % (2 * math.pi)) < ((Bq - A) % (2 * math.pi))
    sweep = (Bq - A) % (2 * math.pi) if ccw else (A - Bq) % (2 * math.pi)
    return R * sweep


class Grid:
    """spatial hash of segments by bounding box: near(x, y, r) -> candidate segment indices."""
    def __init__(self, segs, idxs, cell=2.0):
        self.c, self.cells, self.segs = cell, defaultdict(list), segs
        for i in idxs:
            s = segs[i]
            x0, x1 = sorted((s["a"][0], s["b"][0]))
            y0, y1 = sorted((s["a"][1], s["b"][1]))
            for gx in range(int(x0 // cell), int(x1 // cell) + 1):
                for gy in range(int(y0 // cell), int(y1 // cell) + 1):
                    self.cells[(gx, gy)].append(i)

    def near(self, x, y, r):
        c, out = self.c, set()
        for gx in range(int((x - r) // c), int((x + r) // c) + 1):
            for gy in range(int((y - r) // c), int((y + r) // c) + 1):
                out.update(self.cells.get((gx, gy), ()))
        return out


def context(B, cons, spec=None, board=None):
    """Everything the measures need that is not copper geometry: classes, pairs, stackup, reference planes, fab floors,
    part roles. Board-agnostic: pairs come from netclass partners of nets in [pairs.*] families, planes from [stackup] planes
    and the layer order, roles from the spec's library names (never a ref prefix)."""
    from . import netclass as NC, impedance as IM, review as RV
    cons = cons or {}
    cls = classes(B, cons, spec)
    ncl = NC.classify(B, cons, spec)
    path = board or B.get("file")
    try:
        st = RV.parse_stackup_text(open(path, errors="replace").read()) if path else []
    except OSError:
        st = []
    planes = list((cons.get("stackup") or {}).get("planes") or [])
    cu = [r["name"] for r in st if r.get("type") == "copper" and r.get("name")] or list(B.get("copper") or [])
    geom = {l: (IM.geometry(st, planes, l) if st else None) for l in cu if l not in planes}

    def refplane(layer):
        g = geom.get(layer)
        if not g or layer not in cu:
            return None
        i = cu.index(layer)
        up = next((cu[j] for j in range(i - 1, -1, -1) if cu[j] in planes), None)
        dn = next((cu[j] for j in range(i + 1, len(cu)) if cu[j] in planes), None)
        if g["hu"] is None:
            return dn
        if g["hd"] is None:
            return up
        return up if g["hu"] <= g["hd"] else dn

    ref = {l: refplane(l) for l in geom}
    hgt = {l: min(v for v in (g["hu"], g["hd"]) if v is not None) for l, g in geom.items() if g and (g["hu"] or g["hd"])}
    pf_all = cons.get("pairs") or {}
    pairs = {}
    for n, c in ncl.items():
        q = c.get("partner")
        if q and n < q and cls.get(n) in PAIR_CLS and cls.get(q) == cls.get(n):
            fam = _pair_family(n, pf_all)
            pf = pf_all.get(fam) or {}
            mx = pf.get("skew_max_mm", pf.get("maximum_mm"))
            tg = pf.get("skew_mm")
            ws = [s["w"] for s in B["segs"] if s["net"] in (n, q)]
            pairs[(n, q)] = {"fam": fam, "cls": cls[n], "target": tg, "max": mx if mx is not None else tg,
                             "imp": pf.get("impedance_diff"), "window": pf.get("impedance_window"), "w": statistics.median(ws) if ws else 0.15,
                             "gap": pf.get("gap_mm"), "max_vias": pf.get("max_vias")}
    pair_of = {}
    for k in pairs:
        pair_of[k[0]] = pair_of[k[1]] = k
    comps = {c.get("ref"): c for c in ((spec or {}).get("components") or [])}
    roles = defaultdict(set)
    for ref_, c in comps.items():
        tok = f"{c.get('lib', '')} {c.get('fp', '')}".lower()
        if tok.startswith("connector") or " connector" in tok:
            roles[ref_].add("connector")
        if "testpoint" in tok:
            roles[ref_].add("testpoint")
        if "coaxial" in tok or "antenna" in tok:
            roles[ref_].add("antenna")
    esd = set()
    try:
        from . import powerbudget as PB
        facts, mpn_map = PB.facts_for(cons, spec)
        esd = {r for r, m in (mpn_map or {}).items() if isinstance((facts.get(m) or {}).get("layout"), dict)
               and facts[m]["layout"].get("esd_round")}
    except Exception:
        esd = set()
    try:
        from . import constraints as C
        fab = C.fab_profile(cons, layers=len(B.get("copper") or [])) if cons.get("fab") is not None else {}
    except Exception:
        fab = {}
    return {"cls": cls, "ncl": ncl, "pairs": pairs, "pair_of": pair_of, "stack": st, "planes": planes, "cu": cu, "geom": geom,
            "ref": ref, "h": hgt, "f_ghz": float((((cons.get("placement") or {}).get("rf") or {}).get("freq_ghz")) or 2.4),
            "rf": cons.get("rf") or {}, "fab": fab, "cons": cons, "roles": dict(roles), "esd": esd}


# ---- runs: maximal chains of live segments joined at two-leg joints ------------------------------------------------

def _other(e):
    return "b" if e == "a" else "a"


def _mkrun(B, sl):
    segs = B["segs"]
    pts, lens, turns = [], [], []
    for k, (i, ein) in enumerate(sl):
        s = segs[i]
        pts.append(s[ein])
        lens.append(_arc_len(s))
        if k:
            pi, pe = sl[k - 1]
            t_in = tuple(-v for v in leave(segs[pi], _other(pe)))
            t_out = leave(s, ein)
            cr = t_in[0] * t_out[1] - t_in[1] * t_out[0]
            turns.append(math.degrees(math.atan2(cr, t_in[0] * t_out[0] + t_in[1] * t_out[1])))
    pts.append(segs[sl[-1][0]][_other(sl[-1][1])])
    s0 = segs[sl[0][0]]
    return {"net": s0["net"], "layer": s0["layer"], "sl": sl, "pts": pts, "lens": lens, "turns": turns}


def runs(B, oriented=True):
    """[{net, layer, sl: [(seg, entry end)], pts, lens, turns}]: turns[k-1] is the signed turn (deg, left +) at pts[k].
    oriented: each run travels from its lexicographically smaller end point (pair members then share a travel direction)."""
    segs, ends = B["segs"], _ends(B)

    def nxt(i, e):
        lst = ends.get(_k(segs[i]["net"], segs[i]["layer"], segs[i][e]), ())
        if len(lst) != 2:
            return None
        return next(((j, ej) for j, ej in lst if j != i), None)
    seen, out = set(), []
    for i in live(B):
        if i in seen:
            continue
        ci, ce, back = i, "a", {i}
        while True:
            p = nxt(ci, ce)
            if p is None or p[0] in back:
                break
            ci, ce = p[0], _other(p[1])
            back.add(ci)
        sl, loc = [(ci, ce)], {ci}
        while True:
            p = nxt(sl[-1][0], _other(sl[-1][1]))
            if p is None or p[0] in loc:
                break
            sl.append((p[0], p[1]))
            loc.add(p[0])
        seen |= loc
        r = _mkrun(B, sl)
        if oriented:
            a, b = [tuple(round(v, 3) for v in p) for p in (r["pts"][0], r["pts"][-1])]
            if a > b:
                r = _mkrun(B, [(j, _other(e)) for j, e in reversed(sl)])
        out.append(r)
    return out


def _bend_ix(r):
    """joint indices k (pts[k]) whose turn is a bend."""
    return [k + 1 for k, t in enumerate(r["turns"]) if abs(t) > TOL]


def _legs(r):
    """[(joint k, leg before mm, leg after mm, before is an end?, after is an end?)] for every bend joint: legs run to the
    next bend or to the run's end (pad, via, junction or dangling end)."""
    bs = _bend_ix(r)
    out = []
    for n, k in enumerate(bs):
        k0 = bs[n - 1] if n else 0
        k1 = bs[n + 1] if n + 1 < len(bs) else len(r["lens"])
        out.append((k, sum(r["lens"][k0:k]), sum(r["lens"][k:k1]), n == 0, n + 1 == len(bs)))
    return out


def _wlen(B, r, k):
    return B["segs"][r["sl"][min(k, len(r["sl"]) - 1)][0]]["w"]


# ---- C rules --------------------------------------------------------------------------------------------------------

def c3_sites(B, chamfers=()):
    """chamfers made by F3: [{net, layer, x, y, w, leg}] (empty at F0, so the rule has no eligible site yet). leg >= 2 w scores 1,
    1 w to 2 w scores 0.5 (state `fallback`), shorter is open."""
    out = []
    for c in chamfers:
        r = c["leg"] / c["w"] if c["w"] else 0.0
        out.append(_s(c["net"], c["layer"], c["x"], c["y"], "already" if r >= 2 - 1e-9 else "fallback" if r >= 1 - 1e-9 else "open", leg=c["leg"]))
    return out


def c6_sites(B, X):
    out = []
    for s in B["segs"]:
        if s.get("mid") is None:
            continue
        c = X["cls"].get(s["net"])
        if c not in ("RF",) + PAIR_CLS:
            continue
        C_ = _circle(s["a"], s["mid"], s["b"])
        if C_ is None:
            continue
        lim = (5 if (c == "RF" and X["f_ghz"] >= 5) else 3) * s["w"]
        out.append(_s(s["net"], s["layer"], s["mid"][0], s["mid"][1], "already" if C_[2] >= lim - 1e-6 else "open", R=C_[2], limit=lim))
    return out


def c7_sites(B, X):
    """same-way neighbours: two consecutive bends of one turn direction on RF / pair copper. Legs of 5 w or more keep two
    arcs apart (already); shorter legs are a merge candidate (open)."""
    out = []
    for r in runs(B):
        if X["cls"].get(r["net"]) not in ("RF",) + PAIR_CLS:
            continue
        bs = _bend_ix(r)
        for a, b in zip(bs, bs[1:]):
            if (r["turns"][a - 1] > 0) == (r["turns"][b - 1] > 0):
                leg = sum(r["lens"][a:b])
                out.append(_s(r["net"], r["layer"], r["pts"][b][0], r["pts"][b][1],
                              "already" if leg >= 5 * _wlen(B, r, a) else "open", leg=leg))
    return out


def _gap_of(B, X, pair, layer=None):
    """pair gap (edge to edge): [pairs.F] gap_mm, else the most common measured edge gap of parallel straight runs."""
    pe = X["pairs"][pair]
    if pe.get("gap") is not None and layer is None:
        return float(pe["gap"])
    key = (pair, layer)
    cache = X.setdefault("_gap", {})
    if key in cache:
        return cache[key]
    p, n = pair
    segs = B["segs"]
    g = Grid(segs, [i for i in live(B) if segs[i]["net"] == n and (layer is None or segs[i]["layer"] == layer)], 1.0)
    vals = []
    for i in live(B):
        s = segs[i]
        if s["net"] != p or s.get("mid") is not None or (layer is not None and s["layer"] != layer) or _len(s) < 0.3:
            continue
        m = ((s["a"][0] + s["b"][0]) / 2, (s["a"][1] + s["b"][1]) / 2)
        d = min((_pt_seg(m, segs[j]["a"], segs[j]["b"]) for j in g.near(m[0], m[1], 1.0) if segs[j]["layer"] == s["layer"]), default=None)
        if d is not None and d < 0.6:
            vals.append(round(d, 2))
    if not vals:
        cache[key] = float(pe.get("gap") or 0.15)
        return cache[key]
    from collections import Counter
    cache[key] = max(0.0, Counter(vals).most_common(1)[0][0] - pe["w"])          # centre distance mode - w = edge gap
    return cache[key]


def c8_sites(B, X):
    """pair arcs concentric: a partner arc with the same centre (eps) and radius difference = p = w + gap."""
    out, arcs = [], defaultdict(list)
    for s in B["segs"]:
        if s.get("mid") is not None and X["cls"].get(s["net"]) in PAIR_CLS:
            C_ = _circle(s["a"], s["mid"], s["b"])
            if C_:
                arcs[(s["net"], s["layer"])].append((s, C_))
    for (net, layer), lst in arcs.items():
        pk = X["pair_of"].get(net)
        if not pk:
            continue
        partner = pk[1] if net == pk[0] else pk[0]
        p = X["pairs"][pk]["w"] + _gap_of(B, X, pk)
        for s, C_ in lst:
            ok = any(_dist(C_[:2], D[:2]) <= EPS_G and abs(abs(C_[2] - D[2]) - p) <= EPS_G for _t, D in arcs.get((partner, layer), ()))
            out.append(_s(net, layer, s["mid"][0], s["mid"][1], "already" if ok else "open"))
    return out


def _seg_seg(s, o):
    """smallest distance between two straight segments that do not cross (arcs by their chord)."""
    return min(_pt_seg(s["a"], o["a"], o["b"]), _pt_seg(s["b"], o["a"], o["b"]), _pt_seg(o["a"], s["a"], s["b"]), _pt_seg(o["b"], s["a"], s["b"]))


def c9_sites(B, X):
    """pair gap through every bend: a pair corner whose partner is coupled (within 2 p) keeps centre distance p."""
    out, segs = [], B["segs"]
    byl = {}
    for pk in X["pairs"]:
        for n in pk:
            byl[n] = Grid(segs, [i for i in live(B) if segs[i]["net"] == n], 1.0)
    for c in corners(B):
        pk = X["pair_of"].get(c["net"])
        if not pk or c["kind"] == "straight":
            continue
        partner = pk[1] if c["net"] == pk[0] else pk[0]
        p = X["pairs"][pk]["w"] + _gap_of(B, X, pk)
        legs = [segs[c["a"]], segs[c["b"]]]
        near = set()
        for lg in legs:
            near |= byl[partner].near(lg["a"][0], lg["a"][1], 2 * p) | byl[partner].near(lg["b"][0], lg["b"][1], 2 * p)
        d = min((_seg_seg(lg, segs[j]) for lg in legs for j in near if segs[j]["layer"] == c["layer"]), default=None)
        if d is None or d > 2 * p:
            continue
        out.append(_s(c["net"], c["layer"], c["x"], c["y"], "already" if abs(d - p) <= EPS_G + 1e-3 else "open", d=d, p=p))
    return out


def c11_sites(X, audit):
    """one site per pair: skew vs the target (D71cm). `audit` = pairaudit.audit result {(p, n): {skew, target, status}};
    a pair the checker could not measure is not_possible LENGTH."""
    out = []
    for pk, e in sorted(audit.items()):
        net = pk[0]
        if e.get("skew") is None or e.get("target") is None:
            out.append(_s(net, "", 0.0, 0.0, "not_possible", reason="LENGTH", pair=pk[1]))
        else:
            out.append(_s(net, "", 0.0, 0.0, "already" if e["skew"] <= e["target"] + 1e-9 else "open", skew=e["skew"], target=e["target"]))
    return out


def c12_sites(B, X):
    """compensation bumps on pair members: four consecutive bends (s, -s, -s, s) of at least 45 deg. Both side legs must reach
    max(3 w, 3 h); the leg to the next tuned section is >= 4 h."""
    out = []
    for r in runs(B):
        if r["net"] not in X["pair_of"]:
            continue
        h = X["h"].get(r["layer"], 0.0)
        w = _wlen(B, r, 0)
        bs = _bend_ix(r)
        n = -1
        while n + 1 < len(bs) - 3:
            n += 1
            ks = bs[n:n + 4]
            sg = [1 if r["turns"][k - 1] > 0 else -1 for k in ks]
            if sg == [sg[0], -sg[0], -sg[0], sg[0]] and all(abs(r["turns"][k - 1]) >= 45 - TOL for k in ks):
                side1, side2 = sum(r["lens"][ks[0]:ks[1]]), sum(r["lens"][ks[2]:ks[3]])
                ok = min(side1, side2) >= max(3 * w, 3 * h) - 1e-9
                nxt_leg = sum(r["lens"][ks[3]:bs[n + 4]]) if n + 4 < len(bs) else None
                if nxt_leg is not None and nxt_leg < 4 * h:
                    ok = False
                out.append(_s(r["net"], r["layer"], r["pts"][ks[0]][0], r["pts"][ks[0]][1], "already" if ok else "open"))
                n += 3                                   # one bump is not counted again as the start of the next
    return out


def c16_sites(B, X):
    """signed bend sum per pair (oriented runs, both members): bends add skew = |sum theta| (rad) x p. Already when that is
    inside the pair's skew target; the sum is kept on the site."""
    sums = defaultdict(float)
    for r in runs(B):
        pk = X["pair_of"].get(r["net"])
        if pk:
            sums[pk] += sum(r["turns"]) / 2          # oriented runs: both members turn the same way at a coupled bend
    out = []
    for pk, tot in sorted(sums.items()):
        p = X["pairs"][pk]["w"] + _gap_of(B, X, pk)
        sk = abs(math.radians(tot)) * p
        tg = X["pairs"][pk]["target"]
        out.append(_s(pk[0], "", 0.0, 0.0, "already" if (tg is None or sk <= tg) else "open", sum_deg=tot, skew_mm=sk))
    return out


def c17_sites(B, X):
    """RF run with three or more bends at equal pitch (+-10 %): a regular corner train, listed."""
    out = []
    for r in runs(B):
        if X["cls"].get(r["net"]) != "RF":
            continue
        bs = _bend_ix(r)
        if len(bs) < 3:
            continue
        pitch = [_dist(r["pts"][a], r["pts"][b]) for a, b in zip(bs, bs[1:])]
        train = any(max(pitch[i:i + 2]) > 0 and abs(pitch[i] - pitch[i + 1]) <= 0.1 * max(pitch[i:i + 2]) for i in range(len(pitch) - 1))
        out.append(_s(r["net"], r["layer"], r["pts"][bs[0]][0], r["pts"][bs[0]][1], "open" if train else "already"))
    return out


def c18_sites(B, X):
    """pair bend spacing: legs between bends >= 5 w, legs to a pad / via / run end >= 1.5 w. One site per bend."""
    out = []
    for r in runs(B):
        if r["net"] not in X["pair_of"]:
            continue
        w = _wlen(B, r, 0)
        for k, before, after, first, last in _legs(r):
            ok = before >= (1.5 if first else 5) * w - 1e-9 and after >= (1.5 if last else 5) * w - 1e-9
            out.append(_s(r["net"], r["layer"], r["pts"][k][0], r["pts"][k][1], "already" if ok else "open", before=before, after=after))
    return out


def c19_sites(B, X):
    """ESD segments: nets that join a pad of an esd_round part to a connector pad; their corners (not RF, not pairs) are arcs."""
    esd, roles = X["esd"], X["roles"]
    nets = set()
    for p in B["pads"]:
        if p["ref"] in esd:
            nets.add(p["net"])
    nets = {n for n in nets if any("connector" in roles.get(q["ref"], ()) for q in B["pads"] if q["net"] == n)}
    segs = B["segs"]
    out = []
    for c in _bends(B):
        if c["net"] in nets and X["cls"].get(c["net"]) not in ("RF",) + PAIR_CLS:
            arc = segs[c["a"]].get("mid") is not None or segs[c["b"]].get("mid") is not None
            out.append(_site(c, "already" if arc else "open"))
    return out


def c20_sites(B, X):
    """RF right angles: the optimal mitre M % = 52 + 65 exp(-1.35 W/H), valid W/H 0.5-2.0 (CRN-05); outside it OUT_OF_RANGE."""
    out = []
    segs = B["segs"]
    for c in corners(B):
        if c["kind"] != "right" or X["cls"].get(c["net"]) != "RF":
            continue
        w = segs[c["a"]]["w"]
        h = X["h"].get(c["layer"])
        wh = w / h if h else None
        if wh is None or not 0.5 <= wh <= 2.0:
            out.append(_s(c["net"], c["layer"], c["x"], c["y"], "not_possible", reason="OUT_OF_RANGE", wh=wh))
        else:
            out.append(_site(c, "open", mitre_pct=52 + 65 * math.exp(-1.35 * wh)))
    return out


# ---- P rules: pad and via entry -------------------------------------------------------------------------------------

def _grid_pts(items, key, cell=2.0):
    g = defaultdict(list)
    for it in items:
        x, y = key(it)
        g[(int(x // cell), int(y // cell))].append(it)
    return g, cell


def _near_pts(gc, x, y, r):
    g, c = gc
    out = []
    for gx in range(int((x - r) // c), int((x + r) // c) + 1):
        for gy in range(int((y - r) // c), int((y + r) // c) + 1):
            out += g.get((gx, gy), ())
    return out


def _pad_idx(B):
    if "_padgrid" not in B:
        B["_padgrid"] = _grid_pts([p for p in B["pads"] if not p.get("np")], lambda p: (p["x"], p["y"]), 3.0)
    return B["_padgrid"]


def pad_ends(B):
    """[(pad, seg index, end)] for every live segment end inside a same-net pad on its layer."""
    from . import routeconn as RC
    gc, out, segs = _pad_idx(B), [], B["segs"]
    for i in live(B):
        s = segs[i]
        for e in ("a", "b"):
            x, y = s[e]
            for p in _near_pts(gc, x, y, 2.5):
                if p["net"] == s["net"] and s["layer"] in p["layers"] and RC._in_pad(p, x, y, 0.005):
                    out.append((p, i, e))
                    break
    return out


def via_ends(B):
    """[(via, seg index, end)] for every live segment end on a same-net via barrel's copper on the segment's layer."""
    gc, out, segs = _grid_pts(B["vias"], lambda v: (v["x"], v["y"]), 3.0), [], B["segs"]
    cu = B["copper"]
    for i in live(B):
        s = segs[i]
        for e in ("a", "b"):
            x, y = s[e]
            for v in _near_pts(gc, x, y, 1.0):
                if v["net"] == s["net"] and s["layer"] in _via_layers(v, cu) and _dist((x, y), (v["x"], v["y"])) <= v["d"] / 2 + 0.005:
                    out.append((v, i, e))
                    break
    return out


def _via_layers(v, cu):
    ls = [l for l in cu if l in v["layers"]]
    if len(ls) < 2:
        return set(ls)
    a, b = cu.index(ls[0]), cu.index(ls[-1])
    return set(cu[min(a, b):max(a, b) + 1])


def _first_straight(B, r, k_end):
    """mm of straight copper from the run's end `k_end` (0 or last joint) to the first bend, collinear joints included."""
    bs = _bend_ix(r)
    if not bs:
        return sum(r["lens"])
    return sum(r["lens"][:bs[0]]) if k_end == 0 else sum(r["lens"][bs[-1]:])


def _run_at(B, runs_, i, e):
    """(run, end index 0 or -1) of the run that has segment i's end e as an end of the run."""
    for r in runs_:
        (fi, fe), (li, le) = r["sl"][0], r["sl"][-1]
        if (fi, fe) == (i, e):
            return r, 0
        if (li, _other(le)) == (i, e):
            return r, -1
    return None, None


def _half_ext(p, dx, dy):
    return abs(dx) * p["ex"] + abs(dy) * p["ey"]


def p1_sites(B, X):
    """pad exit on a pad axis: the last segment runs along a pad axis (+-TOL) with its centre line through the pad centre
    (0.02 mm), and the first straight is >= 1 w (pairs 1.5 w)."""
    rs, out = runs(B, oriented=False), []
    for p, i, e in pad_ends(B):
        s = B["segs"][i]
        d = leave(s, e)
        ang = math.degrees(math.atan2(d[1], d[0])) - (p.get("angle") or 0.0)
        off = abs((p["x"] - s[e][0]) * d[1] - (p["y"] - s[e][1]) * d[0])
        aligned = min(abs(((ang + 45) % 90) - 45), 90) <= TOL
        r, k = _run_at(B, rs, i, e)
        straight = (_first_straight(B, r, 0 if k == 0 else len(r["pts"]) - 1) if r else _len(s)) - _half_ext(p, d[0], d[1])
        need = (1.5 if s["net"] in X["pair_of"] else 1.0) * s["w"]
        out.append(_s(s["net"], s["layer"], p["x"], p["y"], "already" if aligned and off <= 0.02 and straight >= need - 1e-9 else "open",
                      off=off, straight=straight))
    return out


def _bends_near(B, X, targets, per_pair=1.5):
    """for every bend of a net near one of its `targets` (x, y, half-extent fn(dir)->mm): the clear distance to the target edge."""
    out = []
    for c in _bends(B):
        out.append(c)
    return out


def p2_sites(B, X):
    """no bend inside a pad or nearer than 1 w (pairs 1.5 w) to its edge. Eligible: bends within 5 w of a same-net pad."""
    gc, out = _pad_idx(B), []
    for c in _bends(B):
        w = B["segs"][c["a"]]["w"]
        need = (1.5 if c["net"] in X["pair_of"] else 1.0) * w
        for p in _near_pts(gc, c["x"], c["y"], 5 * w + 2.5):
            if p["net"] != c["net"] or c["layer"] not in p["layers"]:
                continue
            dx, dy = abs(c["x"] - p["x"]) - p["ex"], abs(c["y"] - p["y"]) - p["ey"]
            clear = math.hypot(max(dx, 0), max(dy, 0)) if (dx > 0 or dy > 0) else min(dx, dy)
            if clear <= 5 * w:
                out.append(_s(c["net"], c["layer"], c["x"], c["y"], "already" if clear >= need else "open", clear=clear))
                break
    return out


def p3_sites(B, X):
    """concave pocket under 90 deg between the trace edge and the pad edge: the trace enters a rectangular pad off its axis
    or at a pad corner (a trace edge flush with or beyond the pad side)."""
    out = []
    for p, i, e in pad_ends(B):
        if p.get("shape") in ("circle", "oval"):
            continue
        s = B["segs"][i]
        d = leave(s, e)
        ang = math.degrees(math.atan2(d[1], d[0])) - (p.get("angle") or 0.0)
        aligned = abs(((ang + 45) % 90) - 45) <= TOL
        rad = math.radians(-(p.get("angle") or 0.0))
        dx, dy = s[e][0] - p["x"], s[e][1] - p["y"]
        lx, ly = dx * math.cos(rad) - dy * math.sin(rad), dx * math.sin(rad) + dy * math.cos(rad)
        along_x = abs(math.cos(math.radians(ang))) > 0.5
        across, half = (ly, p["h"] / 2) if along_x else (lx, p["w"] / 2)
        corner = abs(across) + s["w"] / 2 > half + 1e-6
        out.append(_s(s["net"], s["layer"], p["x"], p["y"], "already" if aligned and not corner else "open"))
    return out


def _first_bend_dist(B, rs, i, e):
    r, k = _run_at(B, rs, i, e)
    if r is None:
        return None
    bs = _bend_ix(r)
    if not bs:
        return None
    return sum(r["lens"][:bs[0]]) if k == 0 else sum(r["lens"][bs[-1]:])


def p4_sites(B, X):
    """pair members enter pads of one part symmetrically: first-corner distances within 0.05 mm (a member without a bend
    against one with a bend is unequal)."""
    rs, byref = runs(B, oriented=False), defaultdict(dict)
    for p, i, e in pad_ends(B):
        pk = X["pair_of"].get(p["net"])
        if pk:
            byref[(pk, p["ref"])].setdefault(p["net"], (p, i, e))
    out = []
    for (pk, ref_), d in sorted(byref.items()):
        if len(d) != 2:
            continue
        (pa, ia, ea), (pb, ib, eb) = d[pk[0]], d[pk[1]]
        da, db = _first_bend_dist(B, rs, ia, ea), _first_bend_dist(B, rs, ib, eb)
        ok = (da is None and db is None) or (da is not None and db is not None and abs(da - db) <= 0.05)
        out.append(_s(pk[0], B["segs"][ia]["layer"], pa["x"], pa["y"], "already" if ok else "open", d_p=da, d_n=db))
    return out


def _pitch(B, p):
    part = B["parts"].get(p["ref"])
    ps = [q for q in (part["pads"] if part else []) if q is not p and q["num"] != p["num"]]
    return min((_dist((p["x"], p["y"]), (q["x"], q["y"])) for q in ps), default=99.0)


def p5_sites(B, X):
    """neck-down: the first segment from a pad is narrower than the next one on the run. Allowed at a fine-pitch pad
    (pitch <= 0.65 mm) with width >= 0.127 (RF: and length <= [rf] neck_max_mm); anywhere else open."""
    rs, out = runs(B, oriented=False), []
    neck_max = float((X["rf"] or {}).get("neck_max_mm", 99.0))
    for p, i, e in pad_ends(B):
        r, k = _run_at(B, rs, i, e)
        if r is None or len(r["sl"]) < 2:
            continue
        order = r["sl"] if k == 0 else list(reversed(r["sl"]))
        w0, w1 = B["segs"][order[0][0]]["w"], B["segs"][order[1][0]]["w"]
        if w0 >= w1 - 1e-6:
            continue
        ok = _pitch(B, p) <= 0.65 and w0 >= 0.127 - 1e-9
        if ok and X["cls"].get(p["net"]) == "RF":
            ok = _len(B["segs"][order[0][0]]) <= neck_max
        out.append(_s(p["net"], B["segs"][i]["layer"], p["x"], p["y"], "already" if ok else "open", w0=w0, w1=w1))
    return out


def width_changes(B):
    """joints of two same-net, same-layer segments of different width (the 117 on W15)."""
    segs, out = B["segs"], []
    for k, lst in _ends(B).items():
        if len(lst) == 2 and lst[0][0] != lst[1][0]:
            a, b = segs[lst[0][0]], segs[lst[1][0]]
            if abs(a["w"] - b["w"]) > 1e-6:
                out.append({"net": k[0], "layer": k[1], "x": k[2], "y": k[3], "a": lst[0][0], "b": lst[1][0], "w1": a["w"], "w2": b["w"]})
    return out


def p6_sites(B, X):
    """width step on a run: both legs 2 w_wide from the next bend. Report only (F6 reports, the router changes widths)."""
    rs = runs(B, oriented=False)
    ix = {}
    for r in rs:
        for k, (i, _e) in enumerate(r["sl"]):
            ix[i] = (r, k)
    out = []
    for wc in width_changes(B):
        r, k = ix[wc["a"]]
        rb, kb = ix[wc["b"]]
        wide = max(wc["w1"], wc["w2"])
        j = max(k, kb) if r is rb else None
        if r is rb and j is not None:
            joint = j
            bs = _bend_ix(r)
            before = max([b for b in bs if b <= joint], default=0)
            after = min([b for b in bs if b > joint], default=len(r["lens"]))
            la, lb = sum(r["lens"][before:joint]), sum(r["lens"][joint:after])
            ok = (not [b for b in bs if b <= joint] or la >= 2 * wide) and (not [b for b in bs if b > joint] or lb >= 2 * wide)
        else:
            ok = True
        out.append(_s(wc["net"], wc["layer"], wc["x"], wc["y"], "already") if ok else
                   _np(wc["net"], wc["layer"], wc["x"], wc["y"], f"keep the width step {2 * wide:.2f} mm from the bend"))
    return out


def p7_sites(B, X):
    """RF trace entering a pad: pad cross width within 10 % of the trace width, else listed (taper or pad = trace)."""
    out = []
    for p, i, e in pad_ends(B):
        s = B["segs"][i]
        if X["cls"].get(s["net"]) != "RF":
            continue
        d = leave(s, e)
        ang = math.degrees(math.atan2(d[1], d[0])) - (p.get("angle") or 0.0)
        cross = p["h"] if abs(math.cos(math.radians(ang))) > 0.5 else p["w"]
        step = abs(cross - s["w"]) / s["w"]
        out.append(_s(s["net"], s["layer"], p["x"], p["y"], "already", step=step) if step <= 0.1 else
                   _np(s["net"], s["layer"], p["x"], p["y"], f"taper {s['w']:.2f} to {cross:.2f} mm or match the pad", step=step, warn=X["f_ghz"] >= 3))
    return out


def _td_params_ok(B):
    return True


def _in_zone_same_net(B, net, layer, x, y):
    from . import routeconn as RC
    for z in B["zones"]:
        if not z.get("keepout") and z["net"] == net and layer in z["layers"] and z["pts"] and RC._in_poly(z["pts"], x, y):
            return True
    return False


def _entry_width(B, rs, kind_ends):
    out = defaultdict(float)
    for obj, i, e in kind_ends:
        out[id(obj)] = max(out[id(obj)], B["segs"][i]["w"])
    return out


def p8_sites(B, X):
    """teardrops on vias and PTH pads of GEN / PWR / ANA nets: the track is narrower than 0.9 x the pad and than 0.508 mm,
    and the via is not inside a same-net zone on that layer (waived). Eligible and `already` when the teardrop flag is set."""
    out = []
    ents = [(v, i, e) for v, i, e in via_ends(B)] + [(p, i, e) for p, i, e in pad_ends(B) if p["tht"]]
    wid = _entry_width(B, None, ents)
    seen = set()
    for obj, i, e in ents:
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        net = obj["net"]
        if X["cls"].get(net) not in ("GEN", "PWR", "ANA"):
            continue
        d = obj.get("d") or min(obj["w"], obj["h"])
        w = wid[id(obj)]
        if not (w < 0.9 * d and w < 0.508):
            continue
        x, y = (obj["x"], obj["y"])
        layer = B["segs"][i]["layer"]
        if "d" in obj and _in_zone_same_net(B, net, layer, x, y):
            out.append(_s(net, layer, x, y, "waived"))
            continue
        out.append(_s(net, layer, x, y, "already" if obj.get("td") else "open"))
    return out


def p9_sites(B, X):
    """no teardrop on RF vias and pads."""
    out = [_s(v["net"], "", v["x"], v["y"], "open" if v.get("td") else "already") for v in B["vias"] if X["cls"].get(v["net"]) == "RF"]
    out += [_s(p["net"], "", p["x"], p["y"], "open" if p.get("td") else "already") for p in B["pads"] if X["cls"].get(p["net"]) == "RF" and not p.get("np")]
    return out


def p10_sites(B, X):
    """pair vias: HSF none; HSS both members identical (the partner via within 2 mm has the same flag)."""
    out, vs = [], [v for v in B["vias"] if v["net"] in X["pair_of"]]
    gc = _grid_pts(vs, lambda v: (v["x"], v["y"]), 3.0)
    for v in vs:
        if X["cls"].get(v["net"]) == "HSF":
            out.append(_s(v["net"], "", v["x"], v["y"], "open" if v.get("td") else "already"))
            continue
        pk = X["pair_of"][v["net"]]
        other = pk[1] if v["net"] == pk[0] else pk[0]
        mates = [u for u in _near_pts(gc, v["x"], v["y"], 2.0) if u["net"] == other and _dist((u["x"], u["y"]), (v["x"], v["y"])) <= 2.0]
        same = bool(mates) and bool(min(mates, key=lambda u: _dist((u["x"], u["y"]), (v["x"], v["y"]))).get("td")) == bool(v.get("td"))
        out.append(_s(v["net"], "", v["x"], v["y"], "already" if same else "open"))
    return out


def p12_sites(B, X):
    """via entry: the trace ends within 0.02 mm of the via centre, no bend nearer than 1 w (pairs 1.5 w) to the via edge."""
    out, vs = [], B["vias"]
    gc = _grid_pts(vs, lambda v: (v["x"], v["y"]), 3.0)
    for c in _bends(B):
        w = B["segs"][c["a"]]["w"]
        need = (1.5 if c["net"] in X["pair_of"] else 1.0) * w
        for v in _near_pts(gc, c["x"], c["y"], 5 * w + 1.0):
            if v["net"] != c["net"]:
                continue
            clear = _dist((c["x"], c["y"]), (v["x"], v["y"])) - v["d"] / 2
            if clear <= 5 * w:
                out.append(_s(c["net"], c["layer"], c["x"], c["y"], "already" if clear >= need else "open", clear=clear))
                break
    for v, i, e in via_ends(B):
        s = B["segs"][i]
        off = _dist(s[e], (v["x"], v["y"]))
        if off > 0.02:
            out.append(_s(s["net"], s["layer"], v["x"], v["y"], "open", off=off))
    return out


def p13_sites(B, X, term_of=None):
    """pair break-out (uncoupled copper) per end <= 6.35 mm (DP-03), listed per end. Ends = the parts' pad clusters of the pair."""
    from . import pairuncoupled as PU
    out = []
    for pk, pe in X["pairs"].items():
        terms = {}
        for p in B["pads"]:
            if p["net"] in pk and not p.get("np"):
                terms.setdefault(p["ref"], []).append(p)
        T = [{"name": r, "c": (sum(q["x"] for q in ps) / len(ps), sum(q["y"] for q in ps) / len(ps))} for r, ps in terms.items()]
        gap = _gap_of(B, X, pk)
        rows = PU.breakout_ends(B, pk, T, pe["w"], gap)
        for term, v in rows.items():
            mm = max(v["P"], v["N"])
            tc = next(t["c"] for t in T if t["name"] == term)
            out.append(_s(pk[0], "", tc[0], tc[1], "already", mm=mm) if mm <= 6.35 else
                       _np(pk[0], "", tc[0], tc[1], f"couple the pair within 6.35 mm of {term}", mm=mm))
    return out


def p14_sites(B, X):
    """two-pin 0603-or-smaller parts: each entry trace narrower than its pad, both pads' entries equal width. Reported."""
    out, ends = [], defaultdict(list)
    for p, i, e in pad_ends(B):
        ends[p["ref"]].append((p, B["segs"][i]["w"], B["segs"][i]["layer"]))
    for ref_, es in sorted(ends.items()):
        part = B["parts"].get(ref_)
        if not part or part["n_pads"] != 2 or len(part["pads"]) != 2:
            continue
        xs = [q["x"] for q in part["pads"]]
        ys = [q["y"] for q in part["pads"]]
        span = max(max(xs) - min(xs) + max(q["ex"] for q in part["pads"]) * 2, max(ys) - min(ys) + max(q["ey"] for q in part["pads"]) * 2)
        if span > 2.6:                       # 0603 land pattern 2.4 mm; 0805 is 2.9
            continue
        ws = [w for _p, w, _l in es]
        ok = all(w < p["min"] for p, w, _l in es) and (len(es) < 2 or max(ws) - min(ws) <= 0.01)
        p0, _w, l0 = es[0]
        out.append(_s(p0["net"], l0, p0["x"], p0["y"], "already") if ok else
                   _np(p0["net"], l0, p0["x"], p0["y"], "entry traces narrower than the pad, equal on both pads"))
    return out


def p15_sites(B, X):
    """pair vias with a layer in their span that carries no copper of the net: `remove unused layers` must be set."""
    out, segs = [], B["segs"]
    vs = [v for v in B["vias"] if v["net"] in X["pair_of"]]
    used = defaultdict(set)
    for v, i, e in via_ends(B):
        used[id(v)].add(segs[i]["layer"])
    for p in B["pads"]:
        pass
    for v in vs:
        span = _via_layers(v, B["copper"])
        inner = [l for l in span if l not in v["layers"]]
        un = [l for l in inner if l not in used[id(v)]]
        if un:
            out.append(_s(v["net"], "", v["x"], v["y"], "already" if v.get("rm_unused") else "open", unused=un))
    return out


# ---- M rules and findings from DRC ------------------------------------------------------------------------------------

def drc_sites(violations, vtype):
    """sites for report-only zone findings from a kicad-cli DRC violation list (copper_sliver -> M2, isolated_copper -> M4)."""
    out = []
    for v in violations:
        if v.get("type") != vtype:
            continue
        it = (v.get("items") or [{}])[0]
        pos = it.get("pos") or {}
        out.append(_np("", "", pos.get("x", 0.0), pos.get("y", 0.0), "refill the zone with islands removed / widen the neck",
                       text=it.get("description", "")))
    return out


def mask_expansion(path):
    """(pad_to_mask_clearance mm, solder_mask_min_width rule mm) from the board and the project beside it."""
    import re
    exp = 0.0
    try:
        m = re.search(r"\(pad_to_mask_clearance\s+([-0-9.]+)\)", open(path, errors="replace").read())
        exp = float(m.group(1)) if m else 0.0
    except OSError:
        pass
    rule = 0.0
    pro = os.path.splitext(path)[0] + ".kicad_pro"
    try:
        rule = float(json.load(open(pro))["board"]["design_settings"]["rules"].get("solder_mask_min_width", 0.0) or 0.0)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return exp, rule


def mask_webs(B, expansion, reach=0.5):
    """[(pad a, pad b, web mm)] for pads of one side whose mask openings (pad + expansion each side) leave a web under `reach`.
    web <= 0 means the openings merge."""
    gc, out = _grid_pts([p for p in B["pads"] if not p.get("np")], lambda p: (p["x"], p["y"]), 3.0), []
    seen = set()
    for p in B["pads"]:
        if p.get("np"):
            continue
        for q in _near_pts(gc, p["x"], p["y"], 3.0):
            if q is p or id(q) in seen or not (p["layers"] & q["layers"] & {B["copper"][0], B["copper"][-1]}):
                continue
            if p["ref"] == q["ref"] and p["num"] == q["num"]:
                continue
            gap = max(0.0, abs(p["x"] - q["x"]) - p["ex"] - q["ex"]) if abs(p["x"] - q["x"]) - p["ex"] - q["ex"] > abs(p["y"] - q["y"]) - p["ey"] - q["ey"] \
                else max(0.0, abs(p["y"] - q["y"]) - p["ey"] - q["ey"])
            gap = math.hypot(max(abs(p["x"] - q["x"]) - p["ex"] - q["ex"], 0), max(abs(p["y"] - q["y"]) - p["ey"] - q["ey"], 0))
            web = gap - 2 * expansion
            if web < reach:
                out.append((p, q, web))
        seen.add(id(p))
    return out


def m11_sites(B, X, webs=None):
    """mask web target >= 0.13 mm between neighbouring pad openings (a placement matter: reported)."""
    exp = X.get("mask_exp", 0.0)
    out = []
    for p, q, web in (webs if webs is not None else mask_webs(B, exp)):
        out.append(_s(p["net"], "", (p["x"] + q["x"]) / 2, (p["y"] + q["y"]) / 2, "already", web=web) if web >= 0.13 - 1e-9 else
                   _np(p["net"], "", (p["x"] + q["x"]) / 2, (p["y"] + q["y"]) / 2, "move the pads apart or shrink the mask opening", web=web))
    return out


# ---- S rules ----------------------------------------------------------------------------------------------------------

def _plane_nets(B, X):
    return {z["net"] for z in B["zones"] if not z.get("keepout") and z["net"] and any(l in X["planes"] for l in z["layers"])}


def dangling(B):
    """[(seg index, end, tail mm, tee)] for live segment ends that touch no pad, via or same-net copper; `tee` = the end rests on the
    interior of another same-net segment (a T, not a stub). tail = length of the run the end belongs to."""
    segs, ends = B["segs"], _ends(B)
    held = {(i, e) for _o, i, e in pad_ends(B)} | {(i, e) for _o, i, e in via_ends(B)}
    gr = Grid(segs, live(B), 2.0)
    rs = runs(B, oriented=False)
    tail = {}
    for r in rs:
        for i, _e in r["sl"]:
            tail[i] = sum(r["lens"])
    out = []
    for i in live(B):
        s = segs[i]
        for e in ("a", "b"):
            if (i, e) in held or len(ends[_k(s["net"], s["layer"], s[e])]) != 1:
                continue
            tee = any(j != i and segs[j]["net"] == s["net"] and segs[j]["layer"] == s["layer"] and segs[j].get("mid") is None
                      and _pt_seg(s[e], segs[j]["a"], segs[j]["b"]) <= 0.005 for j in gr.near(s[e][0], s[e][1], 0.05))
            out.append((i, e, tail.get(i, _len(s)), tee))
    return out


def s1_sites(B, X):
    """RF and pair copper: a dangling end is deleted (open); a T on them is listed (open)."""
    segs, out = B["segs"], []
    for i, e, tail, tee in dangling(B):
        s = segs[i]
        if X["cls"].get(s["net"]) in ("RF",) + PAIR_CLS:
            out.append(_s(s["net"], s["layer"], s[e][0], s[e][1], "open", tail=tail, tee=tee))
    for j in junctions(B):
        if X["cls"].get(j["net"]) in ("RF",) + PAIR_CLS:
            out.append(_s(j["net"], j["layer"], j["x"], j["y"], "open", legs=j["n"], kind="T"))
    return out


def s2_sites(B, X):
    """other nets: a dangling end (stub) is deleted."""
    segs = B["segs"]
    return [_s(segs[i]["net"], segs[i]["layer"], segs[i][e][0], segs[i][e][1], "open", tail=tail)
            for i, e, tail, tee in dangling(B) if X["cls"].get(segs[i]["net"]) not in ("RF",) + PAIR_CLS and not tee]


def via_used(B):
    """{id(via): set of copper layers where a same-net segment end or pad sits on the via}."""
    from . import routeconn as RC
    used = defaultdict(set)
    segs = B["segs"]
    for v, i, e in via_ends(B):
        used[id(v)].add(segs[i]["layer"])
    gc = _grid_pts([p for p in B["pads"] if not p.get("np")], lambda p: (p["x"], p["y"]), 3.0)
    for v in B["vias"]:
        for p in _near_pts(gc, v["x"], v["y"], 2.5):
            if p["net"] == v["net"] and RC._in_pad(p, v["x"], v["y"], 0.005):
                used[id(v)] |= (p["layers"] & _via_layers(v, B["copper"]))
    return used


def plane_change_vias(B, X):
    """vias on pair or RF nets that join layers served by different reference planes (RET-02): {id(via): (via, used layers)}."""
    used, out = via_used(B), {}
    for v in B["vias"]:
        if X["cls"].get(v["net"]) not in ("RF",) + PAIR_CLS:
            continue
        planes = {X["ref"].get(l) for l in used[id(v)] if X["ref"].get(l)}
        if len(planes) > 1:
            out[id(v)] = (v, sorted(used[id(v)], key=B["copper"].index))
    return out


def s4_sites(B, X):
    """a reference-plane change has a ground via beside the signal via: pairs one per member within 1.27 mm, RF two within 1.0 mm."""
    gnd = [v for v in B["vias"] if v["net"] in _plane_nets(B, X)]
    gc = _grid_pts(gnd, lambda v: (v["x"], v["y"]), 3.0)
    out = []
    for v, used in plane_change_vias(B, X).values():
        rf = X["cls"].get(v["net"]) == "RF"
        lim, need = (1.0, 2) if rf else (1.27, 1)
        n = sum(1 for g in _near_pts(gc, v["x"], v["y"], lim) if _dist((g["x"], g["y"]), (v["x"], v["y"])) <= lim)
        out.append(_s(v["net"], "", v["x"], v["y"], "already", n=n, used=used) if n >= need else
                   _np(v["net"], "", v["x"], v["y"], f"ground via within {lim} mm, e.g. at ({v['x'] + 0.9 * lim:.2f}, {v['y']:.2f})", n=n, used=used))
    return out


def s5_sites(B, X):
    """no via on an RF run: [rf] max_vias per net; every via of an RF net is listed."""
    mx = (X["rf"] or {}).get("max_vias", 99)
    cnt = collections_Counter(v["net"] for v in B["vias"] if X["cls"].get(v["net"]) == "RF")
    return [_s(v["net"], "", v["x"], v["y"], "already") if cnt[v["net"]] <= mx else
            _np(v["net"], "", v["x"], v["y"], f"route {v['net']} with at most {mx} via(s)", n=cnt[v["net"]])
            for v in B["vias"] if X["cls"].get(v["net"]) == "RF"]


def collections_Counter(it):
    from collections import Counter
    return Counter(it)


def s6_sites(B, X):
    """pair vias: equal count on P and N and within the family limit ([pairs.F] max_vias)."""
    cnt = collections_Counter(v["net"] for v in B["vias"] if v["net"] in X["pair_of"])
    out = []
    for pk, pe in sorted(X["pairs"].items()):
        a, b = cnt[pk[0]], cnt[pk[1]]
        if a == 0 and b == 0:
            continue
        ok = a == b and (pe.get("max_vias") is None or a <= pe["max_vias"])
        out.append(_s(pk[0], "", 0.0, 0.0, "already", p=a, n=b) if ok else
                   _np(pk[0], "", 0.0, 0.0, "equal via count on both members, side by side", p=a, n=b))
    return out


def lambda_sub(X, layer=None):
    """guided wavelength in the substrate (mm) at f_ghz with the outer dielectric er (spec: 2.87 mm = lambda / 20 at 2.48 GHz)."""
    g = next((v for v in X["geom"].values() if v), None)
    for l in ([layer] if layer else []) + list(X["geom"]):
        gg = X["geom"].get(l)
        if gg:
            g = gg
            break
    er = (g.get("ed") if g and g.get("hu") is None else g.get("eu")) if g else 4.4
    return 299.792458 / (X["f_ghz"] * math.sqrt(er or 4.4))


def s8_sites(B, X):
    """RF via fence: ground vias within 2 mm of an RF run; each one's nearest-fence-via pitch <= lambda/20 (bad > lambda/10)."""
    lam = lambda_sub(X)
    gnd = [v for v in B["vias"] if v["net"] in _plane_nets(B, X)]
    gc = _grid_pts(gnd, lambda v: (v["x"], v["y"]), 3.0)
    segs, out = B["segs"], []
    for net in sorted({s["net"] for s in segs if X["cls"].get(s["net"]) == "RF"}):
        near = {}
        for s in segs:
            if s["net"] != net:
                continue
            m = ((s["a"][0] + s["b"][0]) / 2, (s["a"][1] + s["b"][1]) / 2)
            for g in _near_pts(gc, m[0], m[1], 2.0 + _len(s) / 2):
                if _pt_seg((g["x"], g["y"]), s["a"], s["b"]) <= 2.0:
                    near[id(g)] = g
        fence = list(near.values())
        for g in fence:
            pitch = min((_dist((g["x"], g["y"]), (h["x"], h["y"])) for h in fence if h is not g), default=None)
            if pitch is None:
                continue
            out.append(_s(net, "", g["x"], g["y"], "already", pitch=pitch) if pitch <= lam / 20 + 1e-9 else
                       _np(net, "", g["x"], g["y"], f"add fence vias at pitch <= {lam / 20:.2f} mm", pitch=pitch, bad=pitch > lam / 10))
    return out


def s9_sites(B, X):
    """spacing from RF / pair copper to other nets' parallel copper: edge gap >= max(5 w, 0.76 mm) (1.27 mm to clocks, not
    detectable here: only the first value), never under 3 h. One site per RF / pair segment with such a neighbour."""
    segs = B["segs"]
    gr = Grid(segs, live(B), 2.0)
    out = []
    for i in live(B):
        s = segs[i]
        if X["cls"].get(s["net"]) not in ("RF",) + PAIR_CLS or s.get("mid") is not None or _len(s) < 1e-6:
            continue
        need = max(5 * s["w"], 0.76)
        da = (s["b"][0] - s["a"][0], s["b"][1] - s["a"][1])
        La = math.hypot(*da)
        best = None
        for j in gr.near((s["a"][0] + s["b"][0]) / 2, (s["a"][1] + s["b"][1]) / 2, La / 2 + need + 1.0):
            o = segs[j]
            if o["net"] == s["net"] or o["layer"] != s["layer"] or o.get("mid") is not None or X["pair_of"].get(o["net"]) == X["pair_of"].get(s["net"]) and s["net"] in X["pair_of"]:
                continue
            db = (o["b"][0] - o["a"][0], o["b"][1] - o["a"][1])
            Lb = math.hypot(*db)
            if Lb < 1e-6 or abs(da[0] * db[1] - da[1] * db[0]) / (La * Lb) > math.sin(math.radians(15)):
                continue
            mids = [(s["a"][0] + (s["b"][0] - s["a"][0]) * f, s["a"][1] + (s["b"][1] - s["a"][1]) * f) for f in (0.0, 0.5, 1.0)]
            d = min(_pt_seg(m, o["a"], o["b"]) for m in mids) - (s["w"] + o["w"]) / 2
            if best is None or d < best:
                best = d
        if best is not None and best < need + 1.0:
            out.append(_s(s["net"], s["layer"], (s["a"][0] + s["b"][0]) / 2, (s["a"][1] + s["b"][1]) / 2, "already", gap=best) if best >= need else
                       _np(s["net"], s["layer"], (s["a"][0] + s["b"][0]) / 2, (s["a"][1] + s["b"][1]) / 2, f"move the neighbour {need - best:.2f} mm away", gap=best))
    return out


def s10_sites(B, X):
    """through-via stub on a pair via, from the stackup: <= 0.38 mm or listed."""
    from . import pairaudit as PA
    nets = [n for pk in X["pairs"] for n in pk]
    out = []
    for r in PA.via_stubs(B, nets, min_mm=0.0):
        out.append(_s(r["net"], "", r["x"], r["y"], "already", stub=r["stub_mm"], used=r["used"]) if r["stub_mm"] <= 0.38 + 1e-9 else
                   _np(r["net"], "", r["x"], r["y"], "back-drill / route on the layer pair that leaves no stub", stub=r["stub_mm"], used=r["used"]))
    return out


def _inner_signal(X):
    cu = X["cu"]
    return [l for l in cu[1:-1] if l not in X["planes"]]


def s11_sites(B, X):
    """pairs run on outer layers: length on inner signal layers is listed per pair."""
    inner, out = set(_inner_signal(X)), []
    for pk in sorted(X["pairs"]):
        mm = sum(_arc_len(s) for s in B["segs"] if s["net"] in pk and s["layer"] in inner)
        out.append(_s(pk[0], "", 0.0, 0.0, "already", inner_mm=mm) if mm <= 0 else
                   _np(pk[0], "", 0.0, 0.0, "route the pair on an outer layer", inner_mm=mm))
    return out


def s12_sites(B, X):
    """no parallel overlap between adjacent signal layers with no plane between (pairs and ANA nets first)."""
    cu, segs = X["cu"], B["segs"]
    adj = [(a, b) for a, b in zip(cu, cu[1:]) if a not in X["planes"] and b not in X["planes"]]
    grids = {l: Grid(segs, [i for i in live(B) if segs[i]["layer"] == l], 2.0) for pair in adj for l in pair}
    out = []
    for a, b in adj:
        for la, lb in ((a, b), (b, a)):
            tot = defaultdict(float)
            for i in live(B):
                s = segs[i]
                if s["layer"] != la or X["cls"].get(s["net"]) not in PAIR_CLS + ("ANA",) or s.get("mid") is not None:
                    continue
                d = (s["b"][0] - s["a"][0], s["b"][1] - s["a"][1])
                L = math.hypot(*d)
                if L < 1e-6:
                    continue
                u = (d[0] / L, d[1] / L)
                for j in grids[lb].near((s["a"][0] + s["b"][0]) / 2, (s["a"][1] + s["b"][1]) / 2, L / 2 + 1.0):
                    o = segs[j]
                    if o.get("mid") is not None or o["net"] == s["net"]:
                        continue
                    e = (o["b"][0] - o["a"][0], o["b"][1] - o["a"][1])
                    Lo = math.hypot(*e)
                    if Lo < 1e-6 or abs(u[0] * e[1] - u[1] * e[0]) / Lo > math.sin(math.radians(15)):
                        continue
                    off = abs((o["a"][0] - s["a"][0]) * u[1] - (o["a"][1] - s["a"][1]) * u[0])
                    if off > max(s["w"], o["w"]):
                        continue
                    t0 = (o["a"][0] - s["a"][0]) * u[0] + (o["a"][1] - s["a"][1]) * u[1]
                    t1 = (o["b"][0] - s["a"][0]) * u[0] + (o["b"][1] - s["a"][1]) * u[1]
                    lo, hi = max(0.0, min(t0, t1)), min(L, max(t0, t1))
                    if hi > lo:
                        tot[s["net"]] += hi - lo
            for net, mm in sorted(tot.items()):
                out.append(_np(net, la, 0.0, 0.0, f"separate the layers {a} / {b} or add a plane", overlap_mm=mm))
    nets = {s["net"] for s in segs if X["cls"].get(s["net"]) in PAIR_CLS + ("ANA",) and any(s["layer"] in p for p in adj)}
    have = {o["net"] for o in out}
    out += [_s(n, "", 0.0, 0.0, "already", overlap_mm=0.0) for n in sorted(nets - have)]
    return out


# ---- filled-copper tests (S3, S13) -----------------------------------------------------------------------------------

def fills(B):
    """{layer: [(net, bbox, numpy Nx2)]} from the filled polygons of the board file (cached on B)."""
    if "_fills" in B:
        return B["_fills"]
    import numpy as np
    from . import boardfile as BF
    root = BF.parse(open(B["file"], errors="replace").read())
    out = defaultdict(list)
    for z in BF._kids(root, "zone"):
        net = BF._atoms(BF._first(z, "net_name"))
        for fp_ in BF._kids(z, "filled_polygon"):
            lay = (BF._atoms(BF._first(fp_, "layer")) or [""])[0]
            pts = np.array([BF._nums(xy)[:2] for xy in BF._kids(BF._first(fp_, "pts") or [], "xy")], dtype=float)
            if len(pts) >= 3:
                out[lay].append((net[0] if net else "", (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()), pts))
    B["_fills"] = dict(out)
    return B["_fills"]


def _inside(polys, x, y):
    """even-odd over the layer's filled polygons: is (x, y) on filled copper."""
    import numpy as np
    for _n, (x0, y0, x1, y1), P in polys:
        if x0 <= x <= x1 and y0 <= y <= y1:
            xs, ys = P[:, 0], P[:, 1]
            xs2, ys2 = np.roll(xs, -1), np.roll(ys, -1)
            cond = (ys > y) != (ys2 > y)
            with np.errstate(divide="ignore", invalid="ignore"):
                xi = (xs2 - xs) * (y - ys) / (ys2 - ys) + xs
            if int(np.count_nonzero(cond & (x < xi))) % 2 == 1:
                return True
    return False


def _samples(s, step=1.0):
    L = _len(s)
    n = max(1, int(L / step + 0.999))
    return [(s["a"][0] + (s["b"][0] - s["a"][0]) * (k + 0.5) / n, s["a"][1] + (s["b"][1] - s["a"][1]) * (k + 0.5) / n) for k in range(n)]


def _ring(x, y, r):
    return [(x + r * math.cos(a * math.pi / 4), y + r * math.sin(a * math.pi / 4)) for a in range(8)]


def s3_sites(B, X):
    """RF / pair copper keeps off plane voids and plane edges: the reference plane covers a ring of max(1.5 w, 4 h) around every
    sample of the copper (1 mm steps). One site per segment; a miss is reported (the router moves it)."""
    F = fills(B)
    out = []
    for i in live(B):
        s = B["segs"][i]
        if X["cls"].get(s["net"]) not in ("RF",) + PAIR_CLS or s.get("mid") is not None:
            continue
        plane = X["ref"].get(s["layer"])
        polys = F.get(plane)
        if not polys:
            continue
        m = max(1.5 * s["w"], 4 * X["h"].get(s["layer"], 0.0))
        bad = next(((x, y) for x, y in _samples(s) if any(not _inside(polys, px, py) for px, py in [(x, y)] + _ring(x, y, m))), None)
        out.append(_s(s["net"], s["layer"], (s["a"][0] + s["b"][0]) / 2, (s["a"][1] + s["b"][1]) / 2, "already") if bad is None else
                   _np(s["net"], s["layer"], bad[0], bad[1], f"keep {m:.2f} mm from the void / plane edge", margin=m))
    return out


def s13_sites(B, X):
    """same-layer pour distance to pairs >= 0.5 mm (RF is the exception: a coplanar pour is wanted)."""
    F = fills(B)
    out = []
    for i in live(B):
        s = B["segs"][i]
        if X["cls"].get(s["net"]) not in PAIR_CLS or s.get("mid") is not None or s["layer"] in X["planes"]:
            continue
        polys = [p for p in F.get(s["layer"], ()) if p[0] != s["net"]]
        if not polys:
            continue
        bad = next(((x, y) for x, y in _samples(s) if any(_inside(polys, px, py) for px, py in _ring(x, y, 0.5))), None)
        out.append(_s(s["net"], s["layer"], (s["a"][0] + s["b"][0]) / 2, (s["a"][1] + s["b"][1]) / 2, "already") if bad is None else
                   _np(s["net"], s["layer"], bad[0], bad[1], "pour keep-out 0.5 mm along the pair"))
    return out


# ---- HARD measures (H3, H7, H8, H9; H6 as far as the board allows) ----------------------------------------------------

def h3(B, X):
    """via annular ring >= [fab] min_annular_mm (PCBWay via 0.127 / project 0.13), PTH pad ring >= the fab's PTH ring (0.15).
    -> {base: violations, limit, sites}."""
    fab, hard = X.get("fab") or {}, (X.get("fab") or {}).get("hard") or {}
    vlim = float(fab.get("min_annular_mm") or hard.get("via_annular_min") or 0.127)
    plim = float(hard.get("pth_annular_min") or 0.15)
    sites = [_s(v["net"], "", v["x"], v["y"], "open", ring=(v["d"] - v["drill"]) / 2, limit=vlim) for v in B["vias"] if (v["d"] - v["drill"]) / 2 < vlim - 1e-9]
    sites += [_s(p["net"], "", p["x"], p["y"], "open", ring=(p["min"] - p["drill"]) / 2, limit=plim)
              for p in B["pads"] if p["tht"] and not p.get("np") and p["drill"] > 0 and (p["min"] - p["drill"]) / 2 < plim - 1e-9]
    return {"base": len(sites), "limit": {"via": vlim, "pth": plim}, "sites": sites}


def h7(B, X):
    """impedance inside the window: pairs (impedance_diff +-10 %, optional [pairs.F] impedance_window [lo, hi] intersected), RF
    ([rf] target_z, tolerance_pct). One row per pair / net and layer from the median width and the measured gap (same
    model before and after the stage). -> {base: rows out of window, rows}."""
    from . import impedance as IM
    rows = []
    for pk, pe in sorted(X["pairs"].items()):
        if pe.get("imp") is None:
            continue
        for layer in sorted({s["layer"] for s in B["segs"] if s["net"] in pk}):
            g = X["geom"].get(layer)
            if not g or g.get("model") is None:
                continue
            w = statistics.median(s["w"] for s in B["segs"] if s["net"] in pk and s["layer"] == layer)
            gap = _gap_of(B, X, pk, layer)
            z = IM.z_diff(w, gap, g)
            lo, hi = pe["imp"] * 0.9, pe["imp"] * 1.1
            if pe.get("window"):
                lo, hi = max(lo, float(pe["window"][0])), min(hi, float(pe["window"][1]))
            rows.append({"net": pk[0], "layer": layer, "z": z, "lo": lo, "hi": hi, "ok": z is not None and lo <= z <= hi})
    rf = X["rf"] or {}
    tz = rf.get("target_z")
    if tz:
        tol = float(rf.get("tolerance_pct", 10)) / 100
        for net in sorted({s["net"] for s in B["segs"] if X["cls"].get(s["net"]) == "RF"}):
            for layer in sorted({s["layer"] for s in B["segs"] if s["net"] == net}):
                g = X["geom"].get(layer)
                if not g or g.get("model") is None:
                    continue
                w = statistics.median(s["w"] for s in B["segs"] if s["net"] == net and s["layer"] == layer)
                z = IM.z_single(w, g)
                rows.append({"net": net, "layer": layer, "z": z, "lo": tz * (1 - tol), "hi": tz * (1 + tol),
                             "ok": z is not None and tz * (1 - tol) <= z <= tz * (1 + tol)})
    return {"base": sum(1 for r in rows if not r["ok"]), "rows": rows}


def pair_audit(B, X, cons):
    """pairaudit.audit over the [pairs.*] pairs of X (kicad-cli lengths, D71cx). -> {(p, n): entry}."""
    from . import pairaudit as PA, routeconn as RC
    opens = set((RC.text_open(B) or {}))
    return PA.audit(B, sorted(X["pairs"]), cons, opens)


def h8(audit):
    """pairs over their maximum skew (D71cv); pairs the checker could not measure are counted apart."""
    over = [pk for pk, e in sorted(audit.items()) if "skew" in (e.get("causes") or [])]
    return {"base": len(over), "pairs": [list(k) for k in over],
            "unmeasured": [list(k) for k, e in sorted(audit.items()) if e.get("status") == "unknown"]}


def h9(audit, cons):
    """joined USB-C rows: each branch stub <= the family's stub limit (3.5 mm, constraints.stub_max_mm). Pairs with no limit are
    not graded (no joined row)."""
    from . import constraints as C
    over, graded = [], 0
    for pk, e in sorted(audit.items()):
        sm = C.stub_max_mm(cons or {}, pk[0])
        if sm is None:
            continue
        graded += 1
        mm = max([s["mm"] for s in (e.get("stubs") or [])] or [0.0])
        if mm > sm + 1e-9:
            over.append({"pair": list(pk), "stub_mm": mm, "limit": sm})
    return {"base": len(over), "graded": graded, "over": over}


def h6(B, X, path=None):
    """solder-mask web floor. Measured only when the board's own rule `solder_mask_min_width` is set (> 0): then the count of web
    sites under the fab floor. Otherwise not measured with the gap 16 reason (the DRC rule is 0 on W15)."""
    exp, rule = mask_expansion(path or B["file"])
    if rule <= 0:
        return {"not_measured": "gap 16: board rule solder_mask_min_width is 0, so DRC cannot gate H6; set it to 0.1016 in the next board version"}
    floor = max(rule, float((((X.get("fab") or {}).get("hard") or {}).get("mask_dam")) or 0.1016))
    bad = [w for w in mask_webs(B, exp) if w[2] < floor - 1e-9]
    return {"base": len(bad), "limit": floor, "sites": [_s(p["net"], "", (p["x"] + q["x"]) / 2, (p["y"] + q["y"]) / 2, "open", web=wb) for p, q, wb in bad]}


# ---- facts that the W15 acceptance of T1b names -------------------------------------------------------------------------

def facts(B, X):
    from . import netclass as NC
    segs = B["segs"]
    cs = corners(B)
    short3 = {"pair": [0, 0], "RF": [0, 0]}
    for c in cs:
        if c["kind"] != "45":
            continue
        k = "RF" if X["cls"].get(c["net"]) == "RF" else "pair" if X["cls"].get(c["net"]) in PAIR_CLS else None
        if k:
            short3[k][1] += 1
            if min(_len(segs[c["a"]]), _len(segs[c["b"]])) < 3 * segs[c["a"]]["w"]:
                short3[k][0] += 1
    tiny = collections_Counter(c["kind"] for c in cs if c["kind"] in ("acute", "right") and min(_len(segs[c["a"]]), _len(segs[c["b"]])) < 0.1)
    pc = plane_change_vias(B, X)
    fam = collections_Counter(X["pairs"][X["pair_of"][v["net"]]]["fam"] for v, _u in pc.values() if v["net"] in X["pair_of"])
    inner = collections_Counter(X["pairs"][X["pair_of"][v["net"]]]["fam"] for v, u in pc.values()
                                if v["net"] in X["pair_of"] and not set(u) & {B["copper"][0], B["copper"][-1]})
    return {"width_changes": len(width_changes(B)), "vias": len(B["vias"]), "vias_ground": sum(1 for v in B["vias"] if NC.is_ground(v["net"])),
            "via_sizes": {f"{d}/{dr}": n for (d, dr), n in collections_Counter((v["d"], v["drill"]) for v in B["vias"]).items()},
            "pads": sum(1 for p in B["pads"] if not p.get("np")), "pads_pth": sum(1 for p in B["pads"] if p["tht"] and not p.get("np")),
            "teardrops": sum(1 for v in B["vias"] if v.get("td")) + sum(1 for p in B["pads"] if p.get("td")),
            "short_leg_45_under_3w": short3, "tiny_leg_corners_under_0.1mm": dict(tiny), "degenerate": degenerate(B),
            "plane_change_vias_by_family": dict(fam), "plane_change_vias_between_inner_layers": dict(inner)}


# ---- registry: rule id -> measure(B, X) for those that take only the board and the context ----------------------------

SITES = {"C6": c6_sites, "C7": c7_sites, "C8": c8_sites, "C9": c9_sites, "C12": c12_sites, "C16": c16_sites, "C17": c17_sites,
         "C18": c18_sites, "C19": c19_sites, "C20": c20_sites, "P1": p1_sites, "P2": p2_sites, "P3": p3_sites, "P4": p4_sites,
         "P5": p5_sites, "P6": p6_sites, "P7": p7_sites, "P8": p8_sites, "P9": p9_sites, "P10": p10_sites, "P12": p12_sites,
         "P13": p13_sites, "P14": p14_sites, "P15": p15_sites, "M11": m11_sites, "S1": s1_sites, "S2": s2_sites, "S3": s3_sites,
         "S4": s4_sites, "S5": s5_sites, "S6": s6_sites, "S8": s8_sites, "S9": s9_sites, "S10": s10_sites, "S11": s11_sites,
         "S12": s12_sites, "S13": s13_sites}
