"""Whole-pair audit.  A differential pair counts as routed only when BOTH legs are complete, run through the same layer
sequence with the same via count, and are within the pair's skew limit (`constraints.skew_limit_mm`).

  complete   both legs closed and matching
  mismatch   both legs closed but layer sequence / via count / skew differ (not counted as routed; copper kept)
  half       one leg closed, the other open: the router's copper of BOTH legs is removed (locked fan-out copper stays) and
             the pair is retried as ONE two-net job, never left half
  open       neither leg closed

A leg's profile (layers, vias) is measured along the copper path between its two farthest pads; the skew is netlen.pair_skew (D71cx):
KiCad's DRC lengths, delay skew where the budget is in ps; no fallback to own length math."""
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
    ia, ib = _twin_end(g, pads, ia, ib), _twin_end(g, pads, ib, ia)
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


TWIN_MM = 2.5


def twin_group(pads, i):
    """indices of the pads of the same net on the SAME footprint as pad `i` within TWIN_MM: the double pads of an interleaved
    receptacle row (a footprint pattern, never a ref name).  A pad with none is its own group."""
    p = pads[i]
    return [k for k, q in enumerate(pads) if q["ref"] == p["ref"] and math.hypot(q["x"] - p["x"], q["y"] - p["y"]) <= TWIN_MM]


def _twin_end(g, pads, i, other):
    """the member of pad i's twin group that is electrically nearest `other`: the pair leaves through that pad, the other
    members are joined to it by tap vias / jumpers (spurs, not part of the pad-to-pad path)."""
    grp = twin_group(pads, i)
    if len(grp) < 2:
        return i
    d = _dists(g, other)
    reach = [k for k in grp if k in d]
    return min(reach, key=lambda k: d[k]) if reach else i


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


_CH = {}


def _span(B):
    """span(l1, l2) -> mm copper centre to copper centre from the board's stackup, or None when the board has none."""
    try:
        from . import review as RV
        st = RV.parse_stackup_text(open(B["file"], errors="replace").read()) if B.get("file") else []
    except (OSError, ValueError):
        st = []
    pos = {r["name"]: k for k, r in enumerate(st) if r["type"] == "copper"}
    if not pos:
        return None

    def span(l1, l2):
        if l1 not in pos or l2 not in pos:
            return None
        a, b = sorted((pos[l1], pos[l2]))
        return 0.0 if a == b else st[a]["thickness"] / 2 + sum(r["thickness"] for r in st[a + 1:b]) + st[b]["thickness"] / 2
    return span


def via_stubs(B, nets, min_mm=0.05):
    """PAIR_VIA_STUB: vias on `nets` whose barrel runs past the copper that uses them.  The layers a via serves are those with a track
    end at its centre or a pad of the net over it; the barrel beyond the outermost of them (towards the via's top / bottom layer) has no
    copper on its far side and is an open stub.  -> [{net, x, y, used: [layers], stub_mm, dead}]; `dead` = the net uses ONE layer there with two
    or more track ends (the signal enters and leaves on the same layer: the via is a leftover and can go without opening anything).
    Empty when the board has no stackup (no barrel length)."""
    span = _span(B)
    if span is None:
        return []
    cu = B["copper"]
    NS = set(nets)
    out = []
    for v in B["vias"]:
        if v["net"] not in NS:
            continue
        vl = [l for l in cu if l in _range(v, cu)]
        if len(vl) < 2:
            continue
        ends = {}
        for sg in B["segs"]:
            if sg["net"] == v["net"] and sg["layer"] in vl:
                for e in (sg["a"], sg["b"]):
                    if math.hypot(e[0] - v["x"], e[1] - v["y"]) <= TOL:
                        ends[sg["layer"]] = ends.get(sg["layer"], 0) + 1
        pad_l = set()
        for q in B["pads"]:
            if q["net"] == v["net"] and not q.get("np") and RC._in_pad(q, v["x"], v["y"], TOL):
                pad_l |= {l for l in q["layers"] if l in vl}
        used = sorted(set(ends) | pad_l, key=cu.index)
        if not used:
            continue
        top, bot = span(vl[0], used[0]), span(used[-1], vl[-1])
        if top is None or bot is None:
            continue
        stub = top + bot
        if stub >= min_mm:
            out.append({"net": v["net"], "x": v["x"], "y": v["y"], "used": used, "stub_mm": round(stub, 3),
                        "dead": len(used) == 1 and not pad_l and ends.get(used[0], 0) >= 2})
    return out


def strip_dead_vias(text, B, nets):
    """board text without the leftover vias of `nets` (via_stubs `dead`: the pair never changes layer there) -> (text, [stub rows])."""
    from . import boardtext as BT
    dead = [r for r in via_stubs(B, nets) if r["dead"]]
    if not dead:
        return text, []
    want = {(round(r["x"], 4), round(r["y"], 4), r["net"]) for r in dead}
    kill = [k for _i, _j, k, _b in BT.scan(text)
            if k[0] == "v" and (k[1], k[2], k[4].strip('"')) in want]
    return BT.remove_blocks(text, kill), dead


def pair_list(cls):
    """[(p, n)] from the net classes' partner links (sorted, each pair once). Coupled-preferred pairs (netclass `coupled_pref`:
    analog pairs through passives or a transformer, no two terminals) are not pairs here: the router lays their legs."""
    seen, out = set(), []
    for n, info in sorted((cls or {}).items()):
        q = info.get("partner")
        if info.get("class") == "pair" and q and q in cls and not info.get("coupled_pref") and tuple(sorted((n, q))) not in seen:
            seen.add(tuple(sorted((n, q))))
            out.append(tuple(sorted((n, q))))
    return out


_NOTED = set()


def over_target_line(p, n, skew, target, mx, unit="mm"):
    return f"PAIR_SKEW_OVER_TARGET {p}/{n} skew {skew:.3f} {unit}, target {target:g} (+{skew - target:.3f} over), max {mx:g}: pass"


def _note_over_target(p, n, skew, target, mx, unit="mm"):
    """a pair between its target and its maximum passes, and the user is told once per value: stderr line + the run's event stream."""
    key = (p, n, round(skew, 3), target, mx, unit)
    if key in _NOTED:
        return
    _NOTED.add(key)
    line = over_target_line(p, n, skew, target, mx, unit)
    print(line, file=sys.stderr)
    try:
        from . import events as EV
        EV.emit("pair_skew_over_target", pair=f"{p}/{n}", skew=round(skew, 3), unit=unit, target=target, over=round(skew - target, 3),
                max=mx, verdict="pass", msg=line)
    except Exception:                                  # noqa: BLE001 - the stream is advice
        pass


def ps_budget(cons, master):
    """(target_ps, max_ps, source) when the pair's budget is stated in ps (an explicit [channel.F] skew_ps_*, or the winning
    skew entry of the constraints is *_ps), else None (the budget is in mm).  D71cx: a ps budget is graded on delay skew."""
    from . import constraints as C, channel as CH
    b, lab, _fam = CH.resolve_budget(cons or {}, master)
    if "skew_ps_max" in b and str(lab.get("skew_ps_max", "")).startswith("[channel."):
        mx = b["skew_ps_max"]
        return b.get("skew_ps_target", mx), mx, lab["skew_ps_max"]
    try:
        C.skew_limit_source(cons or {}, master)
        return None
    except ValueError:
        pass
    tgt, src = C.skew_limit_source(cons or {}, master, 1.0)              # ps_per_mm = 1: the value comes back in ps
    mx, _ = C.skew_max_source(cons or {}, master, 1.0)
    return tgt, max(mx, tgt), src


def audit(B, pairs, cons, open_nets):
    """-> {(p, n): {status, why, p: profile, n: profile}}; `open_nets` = nets with open connections (any container with `in`).
    THE skew is netlen.pair_skew (D71cx): KiCad's DRC lengths for a 2-terminal pair, per-orientation paths for a multi-terminal one,
    delay skew (ps) where the budget is in ps.  kicad-cli failing leaves the pair `unknown` (UNMEASURED), never graded from own math;
    a leg whose own decomposition misses KiCad's total is `unknown` (MEASURE_DISAGREE)."""
    from . import constraints as C, netlen
    res = {}
    closed = [(p, n) for p, n in pairs if p not in open_nets and n not in open_nets]
    kl = None
    if closed and isinstance(B, dict) and B.get("file"):
        kl = netlen.pair_lengths(B["file"], closed)
    for p, n in pairs:
        po, no = p in open_nets, n in open_nets
        e = {"status": "open", "why": ""}
        if not po and not no:
            pp, pn = profile(B, p), profile(B, n)
            e.update(p=pp, n=pn)
            psb = ps_budget(cons, p)
            lim, lim_src = (psb[0], psb[2]) if psb else C.skew_limit_checked(cons or {}, p)
            unit = "ps" if psb else "mm"
            ps = netlen.pair_skew(B, p, n, cons, lengths=kl if kl is not None else {}, grade_ps=bool(psb))
            e.update(basis=ps["basis"], skew_mm=ps["skew_mm"], skew_ps=ps["skew_ps"], recon=ps["recon"], unit=unit, length_src=ps["basis"])
            if ps["status"] != "ok":                    # unmeasured / MEASURE_DISAGREE: cannot pass, no fallback to own math
                code = "MEASURE_DISAGREE" if ps["status"] == "MEASURE_DISAGREE" else "PAIR_UNMEASURED"
                e.update(causes=[], skew=None, limit=None, limit_from=lim_src, status="unknown", code=code,
                         why=f"{code}: {ps['why']}; pair not graded")
                res[(p, n)] = e
                continue
            if ps["p_mm"] is not None:
                pp, pn = dict(pp, length=round(ps["p_mm"], 4), path_length=pp["length"]), dict(pn, length=round(ps["n_mm"], 4), path_length=pn["length"])
                e.update(p=pp, n=pn)
            if lim is None:                            # a ps limit with no layer delay: never graded as passing
                e.update(causes=[], skew=None, limit=None, limit_from=lim_src, status="unknown",
                         why=f"{lim_src}; pair not graded")
                res[(p, n)] = e
                continue
            if psb and ps["skew_ps"] is None:
                e.update(causes=[], skew=None, limit=None, limit_from=lim_src, status="unknown", code="PAIR_DELAY_UNKNOWN",
                         why="PAIR_DELAY_UNKNOWN (a layer of the pair has no reference plane); pair not graded")
                res[(p, n)] = e
                continue
            why, causes = [], []
            if pp["layers"] != pn["layers"]:
                why.append(f"layers {'>'.join(x[:-3] for x in pp['layers'])} vs {'>'.join(x[:-3] for x in pn['layers'])}")
                causes.append("layers")
            if pp["vias"] != pn["vias"]:
                why.append(f"vias {pp['vias']} vs {pn['vias']}")
                causes.append("vias")
            skew = abs(ps["skew_ps"]) if psb else abs(ps["skew_mm"])
            e.update(paths=ps["paths"], stubs=[{"path": f"{r['a']}>{r['b']}", "mm": r["stub"]} for r in ps["paths"] if r["a"]],
                     skew_signed=ps["skew_ps"] if psb else ps["skew_mm"])
            if ps["basis"] == "path":
                sm = C.stub_max_mm(cons or {}, p)
                if sm is not None and ps["stub_mm"] > sm:
                    why.append(f"spur {ps['stub_mm']:.2f} > {sm:g} mm")
                    causes.append("stub")
            if psb:
                mx = psb[1]
            else:
                mx, _mx_src = C.skew_max_checked(cons or {}, p)       # FAILS only past the maximum (D71cv); the limit is the target
                mx = lim if mx is None else mx
            if skew > mx:
                why.append(f"skew {skew:.2f} > {mx:g} {unit}")
                causes.append("skew")
            elif skew > lim:
                e["over_target"] = round(skew - lim, 3)
                _note_over_target(p, n, skew, lim, mx, unit)
            e.update(causes=causes, skew=round(skew, 3), limit=mx, target=lim, limit_from=lim_src)
            vs = via_stubs(B, (p, n)) if isinstance(B, dict) and B.get("file") else []
            if vs:
                e["via_stubs"] = vs
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
