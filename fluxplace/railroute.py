"""Rails: nets with many pads and no plane or pour are never one router job.

`[route] rail_min_pads = 8` (default).  A rail is routed as a TREE: the connected clusters of its pads are joined by a
minimum spanning tree (distance = closest pad pair), one two-cluster router job per edge, nearest already-connected
cluster first; the jobs of a round merge like any others.  The job input has the net taken off every pad outside the
two clusters (`boardtext.strip_pad_nets`), so the router sees one connection.  Width: `[power."NET"] max_current_ma`
(constraints), else the router's own width for the net.  A rail the constraints declare a pour for (`[[plan.pour]]`,
`[power."NET"] pour = true`) is not routed: `fluxplace fanout --add-zones` writes the zone and fans out its pads."""
import math

from . import netclass as NC
from . import routeconn as RC

DEFAULT_MIN_PADS = 8


def min_pads(cons):
    return int(((cons or {}).get("route") or {}).get("rail_min_pads", DEFAULT_MIN_PADS))


def edges_per_round(cons):
    return max(1, int(((cons or {}).get("route") or {}).get("rail_edges_per_round", 3)))


def rail_nets(B, cons, cls=None, nets=None, plain=()):
    """-> (rails, poured): rails = nets of `nets` (default every net) with >= rail_min_pads pads and no plane/pour; poured = the
    ones that have one (existing zone, declared pour, or ground assumed to ride a plane)."""
    from . import escapeplan as EP
    cls = cls if cls is not None else NC.classify(B, cons)
    pn = EP.plane_nets(B, cons, cls, EP.plan_params(cons))
    lim = min_pads(cons)
    pads = NC.net_pads(B)
    rails, poured = [], []
    for n in sorted(pads) if nets is None else [x for x in nets if x in pads]:
        if len([p for p in pads[n] if not p.get("np")]) < lim or n in plain:
            continue
        (poured if n in pn else rails).append(n)
    return rails, poured


def clusters(B, nets):
    """{net: [[pad dicts of one connected cluster], ...]} for the nets of `nets` that are open (text connectivity)."""
    S = set(nets)
    B2 = dict(B, pads=[p for p in B["pads"] if p["net"] in S], segs=[s for s in B["segs"] if s["net"] in S],
              vias=[v for v in B["vias"] if v["net"] in S], zones=[z for z in B["zones"] if z["net"] in S])
    groups = {}
    RC.text_open(B2, groups)
    return groups


def sig(cluster):
    p = min(cluster, key=lambda q: (q["ref"], q["num"]))
    return (p["ref"], p["num"])


def _closest(A, Bc):
    best = None
    for p in A:
        for q in Bc:
            d = math.hypot(p["x"] - q["x"], p["y"] - q["y"])
            if best is None or d < best[0]:
                best = (d, p, q)
    return best


def tree_edges(cl, dead=()):
    """Prim from the largest cluster: [{a, b, pa, pb, d, keep}] in the order the tree grows (nearest unconnected cluster first).
    `dead` = {(sigA, sigB)} edges that already failed; a cluster with no live edge is left out."""
    n = len(cl)
    if n < 2:
        return []
    dead = {tuple(sorted(e)) for e in dead}
    start = max(range(n), key=lambda i: (len(cl[i]), -i))
    inside, edges = {start}, []
    while len(inside) < n:
        best = None
        for b in range(n):
            if b in inside:
                continue
            for a in inside:
                if tuple(sorted((sig(cl[a]), sig(cl[b])))) in dead:
                    continue
                d, pa, pb = _closest(cl[a], cl[b])
                if best is None or d < best[0]:
                    best = (d, a, b, pa, pb)
        if best is None:
            break
        d, a, b, pa, pb = best
        inside.add(b)
        edges.append({"a": a, "b": b, "pa": pa, "pb": pb, "d": round(d, 3), "key": tuple(sorted((sig(cl[a]), sig(cl[b])))),
                      "keep": {(p["ref"], p["num"]) for p in cl[a] + cl[b]}})
    return edges


def width_mm(cons, net):
    """Track width for a rail from `[power."NET"] max_current_ma`; None when the constraints say nothing."""
    from . import constraints as C
    ma = (((cons or {}).get("power") or {}).get(net) or {}).get("max_current_ma")
    return round(float(C.rail_width_mm(ma)), 3) if ma else None


def plan(B, cons, rails, dead_edges=None, per_round=None):
    """-> ({net: [edge, ...]}, {net: clusters left}) for the rails that are still open: at most `per_round` edges per rail."""
    per_round = per_round or edges_per_round(cons)
    dead_edges = dead_edges or {}
    cl = clusters(B, rails)
    out = {}
    for n, groups in cl.items():
        es = tree_edges(groups, dead_edges.get(n, ()))
        if es:
            out[n] = es[:per_round]
    return out, {n: len(g) for n, g in cl.items()}
