"""Whole-pair audit.  A differential pair counts as routed only when BOTH legs are complete, run through the same layer
sequence with the same via count, and are within the pair's skew limit (`constraints.skew_limit_mm`).

  complete   both legs closed and matching
  mismatch   both legs closed but layer sequence / via count / skew differ (not counted as routed; copper kept)
  half       one leg closed, the other open: the router's copper of BOTH legs is removed (locked fan-out copper stays) and
             the pair is retried as ONE two-net job, never left half
  open       neither leg closed

A leg's profile is measured along the copper path between its two farthest pads."""
import heapq
import math

from . import routeconn as RC

TOL = 0.02


def _range(v, copper):
    ls = [l for l in v["layers"] if l in copper]
    if len(ls) >= 2:
        i, j = sorted((copper.index(ls[0]), copper.index(ls[-1])))
        return set(copper[i:j + 1])
    return set(copper) if not ls else set(ls)


def profile(B, net):
    """{path: bool, layers: [layer sequence], vias: int, length: mm} between the net's two farthest pads."""
    pads = [p for p in B["pads"] if p["net"] == net and not p.get("np")]
    if len(pads) < 2:
        return {"path": False, "layers": [], "vias": 0, "length": 0.0}
    cu = B["copper"]
    segs = [s for s in B["segs"] if s["net"] == net]
    vias = [v for v in B["vias"] if v["net"] == net]
    best = max(((math.hypot(p["x"] - q["x"], p["y"] - q["y"]), i, j) for i, p in enumerate(pads) for j, q in enumerate(pads) if i < j))
    _d, ia, ib = best
    np_, ns = len(pads), len(segs)
    adj = {}

    def link(u, v, w=0.0, layer=None):
        adj.setdefault(u, []).append((v, w, layer))
        adj.setdefault(v, []).append((u, w, layer))
    ends = []
    for k, s in enumerate(segs):
        a, b = np_ + 2 * k, np_ + 2 * k + 1
        link(a, b, math.hypot(s["a"][0] - s["b"][0], s["a"][1] - s["b"][1]), s["layer"])
        ends += [(a, s["a"], s["layer"]), (b, s["b"], s["layer"])]
    vn = [np_ + 2 * ns + j for j in range(len(vias))]
    vl = [_range(v, cu) for v in vias]
    for i, (u, pu, lu) in enumerate(ends):
        for j, (w, pw, lw) in enumerate(ends[:i]):
            if lu == lw and math.hypot(pu[0] - pw[0], pu[1] - pw[1]) <= TOL:
                link(u, w)
        for j, v in enumerate(vias):
            if lu in vl[j] and math.hypot(pu[0] - v["x"], pu[1] - v["y"]) <= v["d"] / 2 + TOL:
                link(u, vn[j])
        for k, p in enumerate(pads):
            if lu in p["layers"] and RC._in_pad(p, pu[0], pu[1], TOL):
                link(u, k)
    for j, v in enumerate(vias):
        for k, p in enumerate(pads):
            if vl[j] & set(p["layers"]) and RC._in_pad(p, v["x"], v["y"], TOL):
                link(vn[j], k)
        for j2 in range(j):
            if math.hypot(v["x"] - vias[j2]["x"], v["y"] - vias[j2]["y"]) <= (v["d"] + vias[j2]["d"]) / 2 + TOL:
                link(vn[j], vn[j2])
    dist, prev, heap = {ia: 0.0}, {}, [(0.0, ia)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, math.inf):
            continue
        if u == ib:
            break
        for v, w, lay in adj.get(u, ()):
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v], prev[v] = nd, (u, lay)
                heapq.heappush(heap, (nd, v))
    if ib not in dist:
        return {"path": False, "layers": [], "vias": 0, "length": 0.0}
    layers, nv, u = [], 0, ib
    while u != ia:
        pu, lay = prev[u]
        if lay and (not layers or layers[-1] != lay):
            layers.append(lay)
        if u in vn:
            nv += 1
        u = pu
    return {"path": True, "layers": layers[::-1], "vias": nv, "length": round(dist[ib], 4)}


def pair_list(cls):
    """[(p, n)] from the net classes' partner links (sorted, each pair once)."""
    seen, out = set(), []
    for n, info in sorted((cls or {}).items()):
        q = info.get("partner")
        if info.get("class") == "pair" and q and q in cls and tuple(sorted((n, q))) not in seen:
            seen.add(tuple(sorted((n, q))))
            out.append(tuple(sorted((n, q))))
    return out


def audit(B, pairs, cons, open_nets):
    """-> {(p, n): {status, why, p: profile, n: profile}}; `open_nets` = nets with open connections (any container with `in`)."""
    from . import constraints as C
    res = {}
    for p, n in pairs:
        po, no = p in open_nets, n in open_nets
        e = {"status": "open", "why": ""}
        if not po and not no:
            pp, pn = profile(B, p), profile(B, n)
            e.update(p=pp, n=pn)
            lim = C.skew_limit_mm(cons or {}, p)
            why, causes = [], []
            if pp["layers"] != pn["layers"]:
                why.append(f"layers {'>'.join(x[:-3] for x in pp['layers'])} vs {'>'.join(x[:-3] for x in pn['layers'])}")
                causes.append("layers")
            if pp["vias"] != pn["vias"]:
                why.append(f"vias {pp['vias']} vs {pn['vias']}")
                causes.append("vias")
            skew = abs(pp["length"] - pn["length"])
            if skew > lim:
                why.append(f"skew {skew:.2f} > {lim:g} mm")
                causes.append("skew")
            e.update(causes=causes, skew=round(skew, 3), limit=lim)
            e["status"], e["why"] = ("mismatch", "; ".join(why)) if why else ("complete", "")
        elif po != no:
            e["status"], e["why"] = "half", f"{p if no else n} routed, {p if po else n} open"
        res[(p, n)] = e
    return res


def strip_legs(text, nets):
    """board text without the unlocked tracks and vias of `nets` -> (text, removed count)."""
    from . import boardtext as BT
    S = set(nets)
    kill = [k for _i, _j, k, b in BT.scan(text) if BT.net_name(b) in S and not BT.is_locked(b)]
    return BT.remove_blocks(text, kill), len(kill)


def summary(res):
    out = {"complete": [], "mismatch": [], "half": [], "open": []}
    for (p, n), e in res.items():
        out[e["status"]].append(f"{p}/{n}")
    return out
