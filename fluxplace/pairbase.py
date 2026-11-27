"""Pair geometry from the pair's own history: a re-laid / re-netted pair is laid at the width it had on the baseline board (the shipped
version), never at the DRC minimum.  Order: `[pairs.<family>] width_mm` (explicit) > the pair's modal track width on the baseline board
> its modal width on the input board counting only tracks wider than the DRC minimum > the stackup solve (zgeom).  The gap is then
solved for the target impedance at that width (zgeom.pair_geometry, "explicit width_mm, gap solved"), so the pair stays coupled.
`narrowed` is the review guard: update-laid tracks narrower than the same pair on the baseline."""
import copy
from collections import defaultdict


def modal_width(segs, nets, above=0.0):
    """Length-weighted most common width (mm) of the copper tracks of `nets`, counting only widths > `above`; None when none."""
    acc = defaultdict(float)
    for s in segs:
        if s["net"] in nets and s["w"] > above + 1e-6:
            (ax, ay), (bx, by) = s["a"], s["b"]
            acc[round(s["w"], 4)] += max(((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5, 1e-3)
    return max(acc, key=acc.get) if acc else None


def modal_gap(segs, pair, wmax=1.5):
    """Median edge-to-edge gap (mm) between the two legs where they run parallel and side by side, weighted by overlap; None if they never do."""
    P = [x for x in segs if x["net"] == pair[0]]
    N = [x for x in segs if x["net"] == pair[1]]
    acc = []
    for a in P:
        (ax, ay), (bx, by) = a["a"], a["b"]
        la = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
        if la < 0.3:
            continue
        ux, uy = (bx - ax) / la, (by - ay) / la
        for n in N:
            if n["layer"] != a["layer"]:
                continue
            (cx, cy), (dx, dy) = n["a"], n["b"]
            ln = ((dx - cx) ** 2 + (dy - cy) ** 2) ** 0.5
            if ln < 0.3 or abs(ux * (dy - cy) - uy * (dx - cx)) / ln > 0.02:
                continue
            off = abs((cx - ax) * -uy + (cy - ay) * ux)
            if off > wmax:
                continue
            t0, t1 = sorted(((cx - ax) * ux + (cy - ay) * uy, (dx - ax) * ux + (dy - ay) * uy))
            ov = min(la, t1) - max(0.0, t0)
            if ov > 0.2:
                acc.append((off - (a["w"] + n["w"]) / 2.0, ov))
    if not acc:
        return None
    acc.sort()
    half, tot = sum(o for _g, o in acc) / 2.0, 0.0
    for g, o in acc:
        tot += o
        if tot >= half:
            return round(g, 4)


def _segs(path):
    from . import routediag as RD
    B = RD.load(path)
    return B["segs"], B


def widths_for(board, baseline, pairs, cons=None):
    """{(master, slave): (width mm, source)} for pairs that have a width worth keeping."""
    segs_b, _Bb = _segs(board)
    from . import zgeom as ZG
    floor = ZG._project_floors(board)[0]
    if (cons or {}).get("fab") is None:
        from . import limits as LM
        floor = max(floor, LM.house_floors(cons, LM.board_layers(board))[0].get("track_min", 0.0))   # house hard + margin, not the DRC minimum alone
    base_segs = _segs(baseline)[0] if baseline else None
    out = {}
    for pr in pairs:
        nets = set(pr)
        w = modal_width(base_segs, nets) if base_segs is not None else None
        if w:
            out[tuple(pr)] = (w, "baseline board", modal_gap([x for x in base_segs if abs(x["w"] - w) < 1e-6], pr))
            continue
        w = modal_width(segs_b, nets, above=floor)
        if w:
            out[tuple(pr)] = (w, "input board (tracks above the DRC minimum)", None)
    return out


def with_widths(cons, board, baseline, pairs):
    """-> (cons copy, {pair: (w, source)}): each pair's nets get a `[pairs.<net>]` row (the family's rules plus width_mm) unless the
    family already states width_mm.  zgeom.family_of takes the longest matching prefix, so the full net name wins."""
    from . import zgeom as ZG
    if not cons:
        return cons, {}                                  # no constraints: fail closed, nothing is stated for the pair
    got = widths_for(board, baseline, pairs, cons)
    if not got:
        return cons, {}                                  # nothing to state: the constraints stay exactly as given (an empty one fails closed)
    c = copy.deepcopy(cons or {})
    fams = c.setdefault("pairs", {})
    used = {}
    for pr, (w, src, gap) in got.items():
        fam = ZG.family_of(cons, pr[0])
        base = dict(fams.get(fam) or {}) if fam else {}
        if base.get("width_mm"):
            continue
        base["width_mm"] = w
        if gap and gap >= 0.05:
            base["gap_mm"] = gap                      # the pair's own gap: width and gap both stated, zgeom keeps them as the pair had them
        for n in pr:
            fams[n] = dict(base)
        used[pr] = (w, src + (", gap %g mm" % gap if base.get("gap_mm") else ""))
    return c, used


def narrowed(board, baseline, tol=0.005):
    """[(pair label, narrowest new width, baseline width, n tracks)] for pairs whose update-laid tracks (not identical to a baseline
    track) are narrower than that pair's modal width on the baseline."""
    from . import graph as G
    segs, _ = _segs(board)
    bsegs, _ = _segs(baseline)
    have = {(s["net"], s["layer"], tuple(s["a"]), tuple(s["b"]), s["w"]) for s in bsegs}
    names = {s["net"] for s in segs} & {s["net"] for s in bsegs}
    out = []
    for slave, master in sorted(G.diff_pairs(names).items()):
        w0 = modal_width(bsegs, {master, slave})
        if not w0:
            continue
        new = [s for s in segs if s["net"] in (master, slave) and (s["net"], s["layer"], tuple(s["a"]), tuple(s["b"]), s["w"]) not in have]
        thin = [s["w"] for s in new if s["w"] < w0 - tol]
        if thin:
            out.append((f"{master}/{slave}", min(thin), w0, len(thin)))
    return out


def findings(board, baseline):
    """review findings (FAIL PAIR_NARROWED) for `narrowed`."""
    try:
        rows = narrowed(board, baseline)
    except (IndexError, OSError, ValueError):            # a board or baseline that does not parse has no measurable tracks: nothing to compare
        return []
    return [{"level": "FAIL", "code": "PAIR_NARROWED", "refs": [], "value": w, "limit": w0,
             "msg": f"{p}: {n} update-laid track(s) as narrow as {w:g} mm, the same pair on the baseline is {w0:g} mm "
                    "- re-lay it at its own geometry (`fluxplace pairs --only ... --baseline`)"}
            for p, w, w0, n in rows]
