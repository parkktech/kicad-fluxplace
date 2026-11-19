"""PAIR_ROUTED_UNCOUPLED: a differential pair the general router closed as two ordinary nets (pairs-stage state c).
Connected beats open, so it is accepted, but its numbers are reported: length of each leg, skew and the length that does
not run beside the other leg (centre distance above 1.5 x (track + gap) or on another layer)."""
import math

from . import pairaudit as PA
from . import pairfb as FB

COUPLE = 1.5      # a point is coupled when the other leg is within COUPLE x (width + gap) centre to centre


def _pt_seg(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def uncoupled_samples(B, net, other, reach, step=0.2):
    """(x, y, mm) per sample of `net`'s copper with no copper of `other` within `reach` on the same layer."""
    mine = [s for s in B["segs"] if s["net"] == net]
    theirs = {}
    for s in B["segs"]:
        if s["net"] == other:
            theirs.setdefault(s["layer"], []).append(s)
    for s in mine:
        L = math.hypot(s["b"][0] - s["a"][0], s["b"][1] - s["a"][1])
        k = max(1, int(L / step + 0.999))
        cand = theirs.get(s["layer"], [])
        for i in range(k):
            f = (i + 0.5) / k
            p = (s["a"][0] + (s["b"][0] - s["a"][0]) * f, s["a"][1] + (s["b"][1] - s["a"][1]) * f)
            if not any(_pt_seg(p, o["a"], o["b"]) <= reach for o in cand):
                yield p[0], p[1], L / k


def uncoupled(B, net, other, reach, step=0.2):
    """mm of `net`'s copper with no copper of `other` within `reach` on the same layer."""
    return sum(mm for _x, _y, mm in uncoupled_samples(B, net, other, reach, step))


def breakout_ends(B, pair, terms, width, gap):
    """uncoupled mm of each leg per terminal: every uncoupled sample is assigned to its nearest terminal ->
    {terminal name: {"P": mm, "N": mm}} (terminals with no uncoupled copper are left out)."""
    if not terms:
        return {}
    reach = COUPLE * (width + gap)
    out = {}
    for leg, other, key in ((pair[0], pair[1], "P"), (pair[1], pair[0], "N")):
        for x, y, mm in uncoupled_samples(B, leg, other, reach):
            t = min(terms, key=lambda t: math.hypot(x - t["c"][0], y - t["c"][1]))
            out.setdefault(t["name"], {"P": 0.0, "N": 0.0})[key] += mm
    return {k: {"P": round(v["P"], 2), "N": round(v["N"], 2)} for k, v in out.items()}


def measure(B, pair, width, gap, limit_mm):
    """-> row for one connected pair."""
    reach = COUPLE * (width + gap)
    lp, ln = PA.profile(B, pair[0]), PA.profile(B, pair[1])
    up, un = uncoupled(B, pair[0], pair[1], reach), uncoupled(B, pair[1], pair[0], reach)
    return {"pair": list(pair), "connected": bool(lp["path"] and ln["path"]), "length": [round(lp["length"], 2), round(ln["length"], 2)],
            "skew": round(abs(lp["length"] - ln["length"]), 3), "uncoupled_mm": [round(up, 2), round(un, 2)],
            "vias": [lp["vias"], ln["vias"]], "limit_mm": limit_mm}


def rows(B, cons, spec, board, only=None):
    """measure every connected pair of the board (`only` = set of net names to restrict to) -> [row]."""
    from . import netclass as NC, pairs as PR
    cls = NC.classify(B, cons, spec)
    R = __import__("fluxplace.routerules", fromlist=["x"]).rules(cons, board, B=B)
    P = PR.params(cons)
    proj = PR.project_classes(board)
    out = []
    for pr in PA.pair_list(cls):
        if only is not None and pr[0] not in only and pr[1] not in only:
            continue
        r = PR.rules_for(board, B, cons, R, pr[0], P, proj)
        m = measure(B, pr, r["w"], r["gap"], FB.uncoupled_limit(cons, pr))
        if m["connected"]:
            out.append(m)
    return out


def flagged(rs):
    """PAIR_ROUTED_UNCOUPLED rows: a leg runs uncoupled for more than two ends' worth (2 x limit) of its length."""
    return [r for r in rs if max(r["uncoupled_mm"]) > 2 * r["limit_mm"]]


def detail(r):
    return (f"{r['pair'][0]}/{r['pair'][1]}: P {r['length'][0]} mm N {r['length'][1]} mm, skew {r['skew']} mm, "
            f"uncoupled P {r['uncoupled_mm'][0]} mm N {r['uncoupled_mm'][1]} mm (limit {2 * r['limit_mm']:g} mm), vias {r['vias'][0]}/{r['vias'][1]}")


def line(r):
    return "PAIR_ROUTED_UNCOUPLED " + detail(r)
