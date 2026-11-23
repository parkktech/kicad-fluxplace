"""Whole-pair audit.  A differential pair counts as routed only when BOTH legs are complete, run through the same layer
sequence with the same via count, and are within the pair's skew limit (`constraints.skew_limit_mm`).

  complete   both legs closed and matching
  mismatch   both legs closed but layer sequence / via count / skew differ (not counted as routed; copper kept)
  half       one leg closed, the other open: the router's copper of BOTH legs is removed (locked fan-out copper stays) and
             the pair is retried as ONE two-net job, never left half
  open       neither leg closed

A leg's profile (layers, vias) is measured along the copper path between its two farthest pads; its skew is KiCad's (netlen.py: all
copper of the net, via height, in-pad copper), falling back to the walk with a warning when kicad-cli fails."""
import heapq
import math
import os
import re
import sys

from . import routeconn as RC

TOL = 0.02


def _range(v, copper):
    ls = [l for l in v["layers"] if l in copper]
    if len(ls) >= 2:
        i, j = sorted((copper.index(ls[0]), copper.index(ls[-1])))
        return set(copper[i:j + 1])
    return set(copper) if not ls else set(ls)


def _graph(B, net, via_h=None):
    """the copper graph of `net`: nodes 0..len(pads)-1 are pads, then two per track segment, then one per via; edges are segment
    lengths (and, with `via_h(via)`, half a via height on each side of a via).  None when the net has fewer than two pads."""
    pads = [p for p in B["pads"] if p["net"] == net and not p.get("np")]
    if len(pads) < 2:
        return None
    cu = B["copper"]
    seg_idx = [i for i, s in enumerate(B["segs"]) if s["net"] == net]
    segs = [B["segs"][i] for i in seg_idx]
    vias = [v for v in B["vias"] if v["net"] == net]
    np_, ns = len(pads), len(segs)
    adj = {}

    def link(u, v, w=0.0, layer=None, sid=None):
        adj.setdefault(u, []).append((v, w, layer, sid))
        adj.setdefault(v, []).append((u, w, layer, sid))
    ends = []
    for k, s in enumerate(segs):
        a, b = np_ + 2 * k, np_ + 2 * k + 1
        link(a, b, math.hypot(s["a"][0] - s["b"][0], s["a"][1] - s["b"][1]), s["layer"], k)
        ends += [(a, s["a"], s["layer"]), (b, s["b"], s["layer"])]
    vn = [np_ + 2 * ns + j for j in range(len(vias))]
    vl = [_range(v, cu) for v in vias]
    hh = [(via_h(v) / 2 if via_h else 0.0) for v in vias]
    for i, (u, pu, lu) in enumerate(ends):
        for j, (w, pw, lw) in enumerate(ends[:i]):
            if lu == lw and math.hypot(pu[0] - pw[0], pu[1] - pw[1]) <= TOL:
                link(u, w)
        for j, v in enumerate(vias):
            if lu in vl[j] and math.hypot(pu[0] - v["x"], pu[1] - v["y"]) <= v["d"] / 2 + TOL:
                link(u, vn[j], hh[j])
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
    return {"pads": pads, "seg_idx": seg_idx, "vn": vn, "adj": adj}


def profile(B, net, want_path=False):
    """{path: bool, layers: [layer sequence], vias: int, length: mm} between the net's two farthest pads.
    want_path (PHASE3G G1b c): also "segs": indices into B["segs"] of the track segments ON that path (a stub or a branch
    of the net is not on it), so the tuner can add length only where the pad-to-pad length counts it."""
    g = _graph(B, net)
    if g is None:
        return _none_path(want_path)
    pads, seg_idx, vn, adj = g["pads"], g["seg_idx"], g["vn"], g["adj"]
    best = max(((math.hypot(p["x"] - q["x"], p["y"] - q["y"]), i, j) for i, p in enumerate(pads) for j, q in enumerate(pads) if i < j))
    _d, ia, ib = best
    dist, prev, heap = {ia: 0.0}, {}, [(0.0, ia)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, math.inf):
            continue
        if u == ib:
            break
        for v, w, lay, sid in adj.get(u, ()):
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v], prev[v] = nd, (u, lay, sid)
                heapq.heappush(heap, (nd, v))
    if ib not in dist:
        return _none_path(want_path)
    layers, nv, on, u = [], 0, [], ib
    while u != ia:
        pu, lay, sid = prev[u]
        if lay and (not layers or layers[-1] != lay):
            layers.append(lay)
        if sid is not None:
            on.append(seg_idx[sid])
        if u in vn:
            nv += 1
        u = pu
    out = {"path": True, "layers": layers[::-1], "vias": nv, "length": round(dist[ib], 4)}
    if want_path:
        out["segs"] = sorted(on)
    return out


def _none_path(want_path):
    out = {"path": False, "layers": [], "vias": 0, "length": 0.0}
    if want_path:
        out["segs"] = []
    return out


def _via_height(B):
    """via(v) -> mm: copper centre to copper centre over the via's layer span, from the board's stackup (what KiCad adds per via)."""
    try:
        from . import review as RV
        st = RV.parse_stackup_text(open(B["file"], errors="replace").read()) if B.get("file") else []
    except (OSError, ValueError):
        st = []
    cu = B["copper"]
    pos = {r["name"]: k for k, r in enumerate(st) if r["type"] == "copper"}

    def h(v):
        ls = [l for l in v["layers"] if l in cu]
        if len(ls) < 2 or not all(l in pos for l in ls):
            return 0.0
        a, b = sorted((pos[ls[0]], pos[ls[-1]]))
        return st[a]["thickness"] / 2 + sum(r["thickness"] for r in st[a + 1:b]) + st[b]["thickness"] / 2
    return h


def _dists(g, src):
    dist, heap = {src: 0.0}, [(0.0, src)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, math.inf):
            continue
        for v, w, _l, _s in g["adj"].get(u, ()):
            if d + w < dist.get(v, math.inf):
                dist[v] = d + w
                heapq.heappush(heap, (d + w, v))
    return dist


def multi_paths(B, p, n, pad_max=16.0):
    """Per signal path skew of a pair with more than two terminals where a part has several pads on the net (the two rows of a joined
    connector).  Terminals = P/N pad pairs (pairs.terminals).  A path runs between two terminals of different parts, per pad pair, P
    leg against N leg, via height included; a spur is the copper from a terminal to where it joins the rest of its part's pads
    (3-terminal junction distance).  -> None for a normal pair (two terminals) or an open leg, else
    {paths: [{a, b, p, n, skew}], stubs: [{at, p, n}], skew: worst path skew, stub: longest spur}."""
    from . import pairs as PRS
    terms, _lone = PRS.terminals(B, p, n, pad_max)
    if len(terms) < 3:
        return None
    gv = _via_height(B)
    gp, gn = _graph(B, p, gv), _graph(B, n, gv)
    if gp is None or gn is None:
        return None
    ix = lambda g, pad: next((i for i, q in enumerate(g["pads"]) if q is pad), None)
    ip = [ix(gp, t["p"]) for t in terms]
    inn = [ix(gn, t["n"]) for t in terms]
    if None in ip or None in inn:
        return None
    dp, dn = [_dists(gp, i) for i in ip], [_dists(gn, i) for i in inn]
    ref = [t["p"]["ref"] for t in terms]
    joined = [r for r in set(ref) if ref.count(r) > 1 and re.match(r"(J|P|X|CN|CON)\d", r.upper())]
    if not joined:                                         # joined rows are a CONNECTOR with several pads on the net (a flow-through chip's pins
        return None                                        # are one signal path inside the part): else KiCad's total stands
    ref = [r if r in joined else f"{r}#{k}" for k, r in enumerate(ref)]
    name = [f"{t['p']['ref']}.{t['p']['num']}/{t['n']['num']}" for t in terms]
    paths = []
    for a in range(len(terms)):
        for b in range(a + 1, len(terms)):
            if ref[a] == ref[b]:
                continue
            lp, ln = dp[a].get(ip[b]), dn[a].get(inn[b])
            if lp is None or ln is None:
                return None
            paths.append({"a": name[a], "b": name[b], "p": round(lp, 3), "n": round(ln, 3), "skew": round(abs(lp - ln), 3)})
    stubs = []
    for a in range(len(terms)):
        same = [j for j in range(len(terms)) if j != a and ref[j] == ref[a]]
        out = [c for c in range(len(terms)) if ref[c] != ref[a]]
        if not same or not out:
            continue
        sp = []
        for d, ia_, ix_ in ((dp, ip, ip), (dn, inn, inn)):
            v = [(d[a].get(ix_[j], math.inf) + d[a].get(ix_[c], math.inf) - d[j].get(ix_[c], math.inf)) / 2 for j in same for c in out]
            sp.append(max(0.0, min(v)))
        if all(x < math.inf for x in sp):
            stubs.append({"at": name[a], "p": round(sp[0], 3), "n": round(sp[1], 3)})
    if not paths:
        return None
    return {"paths": paths, "stubs": stubs, "skew": max(x["skew"] for x in paths),
            "stub": max([max(x["p"], x["n"]) for x in stubs] or [0.0])}


def pair_list(cls):
    """[(p, n)] from the net classes' partner links (sorted, each pair once)."""
    seen, out = set(), []
    for n, info in sorted((cls or {}).items()):
        q = info.get("partner")
        if info.get("class") == "pair" and q and q in cls and tuple(sorted((n, q))) not in seen:
            seen.add(tuple(sorted((n, q))))
            out.append(tuple(sorted((n, q))))
    return out


_NOTED = set()


def over_target_line(p, n, skew, target, mx):
    return f"PAIR_SKEW_OVER_TARGET {p}/{n} skew {skew:.3f} mm, target {target:g} (+{skew - target:.3f} over), max {mx:g}: pass"


def _note_over_target(p, n, skew, target, mx):
    """a pair between its target and its maximum passes, and the user is told once per value: stderr line + the run's event stream."""
    key = (p, n, round(skew, 3), target, mx)
    if key in _NOTED:
        return
    _NOTED.add(key)
    line = over_target_line(p, n, skew, target, mx)
    print(line, file=sys.stderr)
    try:
        from . import events as EV
        EV.emit("pair_skew_over_target", pair=f"{p}/{n}", skew_mm=round(skew, 3), target_mm=target, over_mm=round(skew - target, 3),
                max_mm=mx, verdict="pass", msg=line)
    except Exception:                                  # noqa: BLE001 - the stream is advice
        pass


def audit(B, pairs, cons, open_nets):
    """-> {(p, n): {status, why, p: profile, n: profile}}; `open_nets` = nets with open connections (any container with `in`)."""
    from . import constraints as C, netlen
    res = {}
    closed = [(p, n) for p, n in pairs if p not in open_nets and n not in open_nets]
    kl = None                                          # KiCad is the length authority (its skew DRC); the pad-to-pad walk is the fallback
    if closed and isinstance(B, dict) and B.get("file") and not os.environ.get("FLUXPLACE_AUDIT_OWN_LENGTHS"):
        kl = netlen.pair_lengths(B["file"], closed)
        if kl is None:
            print("pairaudit: WARNING KiCad lengths unavailable (kicad-cli failed): skew from the pad-to-pad walk, which skips in-pad copper, "
                  "via height and branches", file=sys.stderr)
    for p, n in pairs:
        po, no = p in open_nets, n in open_nets
        e = {"status": "open", "why": ""}
        if not po and not no:
            pp, pn = profile(B, p), profile(B, n)
            e.update(p=pp, n=pn)
            lim, lim_src = C.skew_limit_checked(cons or {}, p)     # lim None: PAIR_DELAY_UNKNOWN, not graded
            if lim is None:                            # a ps limit with no layer delay: never graded as passing
                e.update(causes=[], skew=None, limit=None, limit_from=lim_src, status="unknown",
                         why=f"{lim_src}; pair not graded")
                res[(p, n)] = e
                continue
            why, causes = [], []
            if pp["layers"] != pn["layers"]:
                why.append(f"layers {'>'.join(x[:-3] for x in pp['layers'])} vs {'>'.join(x[:-3] for x in pn['layers'])}")
                causes.append("layers")
            if pp["vias"] != pn["vias"]:
                why.append(f"vias {pp['vias']} vs {pn['vias']}")
                causes.append("vias")
            skew = abs(pp["length"] - pn["length"])
            e["length_src"] = "audit"
            k = (kl or {}).get((p, n))
            if k:                                      # KiCad's total copper length per net; equal lengths report nothing (skew 0)
                skew, e["length_src"] = abs(k["skew"]), "kicad"
                if k["p"] is not None:
                    pp, pn = dict(pp, length=round(k["p"], 4), path_length=pp["length"]), dict(pn, length=round(k["n"], 4), path_length=pn["length"])
                    e.update(p=pp, n=pn)
            mp = multi_paths(B, p, n)                  # more than two terminals: judged per signal path (each orientation), spurs apart
            if mp:
                skew, e["length_src"] = mp["skew"], "path"
                e.update(paths=mp["paths"], stubs=mp["stubs"])
                sm = C.stub_max_mm(cons or {}, p)
                if sm is not None and mp["stub"] > sm:
                    why.append(f"spur {mp['stub']:.2f} > {sm:g} mm")
                    causes.append("stub")
            mx, _mx_src = C.skew_max_checked(cons or {}, p)           # FAILS only past the maximum (D71cv); the limit is the target
            mx = lim if mx is None else mx
            if skew > mx:
                why.append(f"skew {skew:.2f} > {mx:g} mm")
                causes.append("skew")
            elif skew > lim:
                e["over_target"] = round(skew - lim, 3)
                _note_over_target(p, n, skew, lim, mx)
            e.update(causes=causes, skew=round(skew, 3), limit=mx, target=lim, limit_from=lim_src)
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
    out = {"complete": [], "mismatch": [], "half": [], "open": [], "unknown": []}
    for (p, n), e in res.items():
        out[e["status"]].append(f"{p}/{n}")
    return out
