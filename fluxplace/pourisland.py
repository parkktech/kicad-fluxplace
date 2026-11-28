"""Guarded same-net pour-island repair: one stitching via at a legal site, never an unverified island deletion."""
import math
import os
import re
import time

from . import handroute as H, planes as PL, pourtie as PTF, routerules as RR


def _start(records, net, at):
    if at is not None:
        return tuple(at)
    zones = [side for r in records if r["net"] == net for side in (r["a"], r["b"]) if side["kind"] == "zone"]
    if zones:
        return zones[0]["x"], zones[0]["y"]
    sides = [side for r in records if r["net"] == net for side in (r["a"], r["b"])]
    if not sides:
        raise ValueError(f"net {net!r} has no open pour-island connection")
    return sides[0]["x"], sides[0]["y"]


def outline_index(b, net):
    """-> {layer id: [(area, filled poly set, outline index)]}: the filled outlines of `net`'s copper pours, per layer."""
    rows = {}
    for z in b.Zones():
        if z.GetIsRuleArea() or z.GetNetname() != net:
            continue
        for lid in z.GetLayerSet().CuStack():
            ps = z.GetFilledPolysList(lid)
            for i in range(ps.OutlineCount()):
                rows.setdefault(lid, []).append((abs(ps.Outline(i).Area()), ps, i))
    return rows


def _one_layer_pads(M, net):
    """(ref.number, layer) of the pads of `net` whose copper island (handroute model: pads, tracks, vias and the fills they touch)
    lives on ONE copper layer. A pad not in here reaches another layer through a track, so its fill outline is tied even with no
    via inside the outline."""
    isl, omap = M.islands(net)
    single = {g["id"] for g in isl if len({l for o in g["items"] for l in o.layers} | {l for _z, l, _f in g["zones"]}) == 1}
    return {(o.ref, l) for o in M.obs if o.kind == "pad" and o.net == net and omap.get(o.id) in single for l in o.layers}


def _untied(b, net, M=None):
    """The untied pour islands of `net`: a fill outline that holds a pad of the net but no via and no through-hole pad of it and,
    with the copper model `M`, no pad that a track ties to another layer.
    -> yields (layer id, filled poly set, outline index, [pads of the net inside it])."""
    import pcbnew
    single = _one_layer_pads(M, net) if M is not None else None
    vias = [t.GetPosition() for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T and t.GetNetname() == net]
    pads = [p for f in b.GetFootprints() for p in f.Pads() if p.GetNetname() == net]
    for z in b.Zones():
        if z.GetIsRuleArea() or z.GetNetname() != net:
            continue
        for lid in z.GetLayerSet().CuStack():
            ps = z.GetFilledPolysList(lid)
            for i in range(ps.OutlineCount()):
                if any(ps.Contains(v, i) for v in vias) or any(p.HasHole() and ps.Contains(p.GetPosition(), i) for p in pads):
                    continue
                own = [p for p in pads if p.IsOnLayer(lid) and ps.Contains(p.GetPosition(), i)]
                if single is not None and any((f"{p.GetParentFootprint().GetReference()}.{p.GetNumber()}", b.GetLayerName(lid))
                                              not in single for p in own):
                    continue
                if own:
                    yield lid, ps, i, own


def island_groups(b, net, step=0.4, per_island=12, M=None):
    """Via sites inside the untied pour islands of `net` (_untied): such an island has no path to the other layers. KiCad reports
    the open at the zone's anchor corner, not at the island, so the sites are taken from the fill itself: grid points inside the
    outline, nearest its pads first. -> [[(x, y)] per island] in mm."""
    import pcbnew
    out = []
    for _lid, ps, i, pads in _untied(b, net, M):
        own = [p.GetPosition() for p in pads]
        bb = ps.Outline(i).BBox()
        st = pcbnew.FromMM(step)
        pts = []
        x = bb.GetLeft() + st // 2
        while x < bb.GetRight():
            y = bb.GetTop() + st // 2
            while y < bb.GetBottom():
                if ps.Contains(pcbnew.VECTOR2I(x, y), i):
                    pts.append((min((x - o.x) ** 2 + (y - o.y) ** 2 for o in own), x, y))
                y += st
            x += st
        pts.sort()
        rest = pts[per_island:]                     # k15: a large island also gets sites spread over the rest of it
        pick = pts[:per_island] + rest[::max(1, len(rest) // per_island)][:per_island]
        out.append([(pcbnew.ToMM(x), pcbnew.ToMM(y)) for _d, x, y in pick])
    return out


STUB_R = (0.6, 0.9, 1.3)            # k11: via distance (mm) from an island pad for a pad stub + via
STUB_PER_ISLAND = 16


def island_stubs(b, net, M=None):
    """k11: an island too small (or too crowded) for a via inside it is tied by a short track from one of its pads to a via
    outside it that lands on the net's pour on another layer. -> [(island index in _untied order, path string)] (handroute path
    syntax), nearest first."""
    import pcbnew
    lids = {lid for z in b.Zones() if not z.GetIsRuleArea() and z.GetNetname() == net for lid in z.GetLayerSet().CuStack()}
    out = []
    for k, (lid, _ps, _i, own) in enumerate(_untied(b, net, M)):
        rows = []
        for p in own:
            c = p.GetPosition()
            for r in STUB_R:
                for k in range(8):
                    vx, vy = c.x + pcbnew.FromMM(r) * math.cos(k * math.pi / 4), c.y + pcbnew.FromMM(r) * math.sin(k * math.pi / 4)
                    if any(PTF.fill_hit(b, net, o, vx, vy) for o in lids - {lid}):
                        px, py, qx, qy = (pcbnew.ToMM(v) for v in (c.x, c.y, vx, vy))
                        rows.append((r, f"{b.GetLayerName(lid)}: {px:.4f},{py:.4f} {qx:.4f},{qy:.4f}; via {qx:.4f},{qy:.4f}"))
        out += [(k, path) for _r, path in sorted(rows, key=lambda t: t[0])[:STUB_PER_ISLAND]]
    return out


LINK_TARGETS = 3                    # nearest tied vias / through pads of the net tried per island pad
LINK_REACH = 4.0                    # mm: farthest tied via / through pad a link is routed to


def _tied_points(b, net):
    """Positions of the vias and through-hole pads of `net` (copper that has its layer change)."""
    import pcbnew
    return ([t.GetPosition() for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T and t.GetNetname() == net]
            + [p.GetPosition() for f in b.GetFootprints() for p in f.Pads() if p.GetNetname() == net and p.HasHole()])


def _link(b, board, net, pad, lid, tied, constraints, M, rip=None, copper_only=False):
    """One track on layer `lid` from `pad` to the nearest of the `tied` points within LINK_REACH (handroute.suggest; it ends on
    whatever copper of the target's island it meets first). rip = (names, cons): when only unlocked tracks of other nets are in the
    way, the relaxed search names them and the link is routed without them. -> (path, ids to rip first) or None."""
    import pcbnew
    c = pad.GetPosition()
    src = (pcbnew.ToMM(c.x), pcbnew.ToMM(c.y))
    kw = dict(constraints=constraints, layers=[b.GetLayerName(lid)], M=M, margin=1.0, copper_only=copper_only)
    for d, vx, vy in sorted((math.hypot(v.x - c.x, v.y - c.y), v.x, v.y) for v in tied)[:LINK_TARGETS]:
        if d > pcbnew.FromMM(LINK_REACH):
            break
        dst = (pcbnew.ToMM(vx), pcbnew.ToMM(vy))
        r = H.suggest(board, net, src, dst, **kw)
        ids = set()
        if not r.get("found") and rip:
            soft = H.suggest(board, net, src, dst, relax=True, **kw)
            short = {x["id"] for x in soft.get("blockers") or ()}
            hits = [o for o in M.obs if o.id and o.kind in ("track", "via") and H._sid(o.id, M) in short]
            ids = _rip_ids(M, hits, *rip) if soft.get("found") and hits else None
            r = H.suggest(board, net, src, dst, skip_ids=tuple(ids), **kw) if ids else {}
        if r.get("found"):
            return r["path"], frozenset(ids)
    return None


def island_links(b, board, net, constraints, M, names, cons, skip=()):
    """A sealed island (no via site in it, none beside its pads) is tied on its OWN layer by a routed track from one of its pads to
    copper of the net that already has its layer change: the nearest existing vias / through pads (_link, rips allowed). One
    candidate per island pad; the islands in `skip` (indices) are left out.
    -> [(island index, path, ids)]: ids = the tracks to rip first (empty: the link is free)."""
    tied = _tied_points(b, net)
    out = []
    for k, (lid, _ps, _i, own) in enumerate(_untied(b, net, M)):
        for p in own if k not in skip else ():
            hit = _link(b, board, net, p, lid, tied, constraints, M, (names, cons))
            if hit:
                out.append((k, *hit))
    return out


def link_pads(board, out, pads, constraints=None):
    """Pads that reach their plane only through a surface pour (pourtie's `pour` state: no via site of their own) get a track on
    their layer to the nearest via / through pad of the net (_link, nothing ripped), each one a guarded edit (no new DRC error,
    no net opened). `pads` = [{net, ref, pad, at [x, y] mm}]. Writes `out` when at least one link is laid.
    -> [{net, ref, pad, at, state: "link" | "pour", path | reason}]."""
    import pcbnew
    from . import pourtie as PT
    keep, cur, rows = H.FILL_CACHE_DIR, board, []
    if keep is None:
        H.FILL_CACHE_DIR = out + ".fill"
    tmp = os.path.splitext(out)[0] + ".link.kicad_pcb"
    try:
        for t in pads:
            M = H._model(cur, constraints)
            pad = PT._pad_at(M.b, t["net"], t["at"])
            row = {k: t.get(k) for k in ("net", "ref", "pad", "at")}
            rows.append(row)
            lids = [lid for lid in M.b.GetEnabledLayers().CuStack() if pad is not None and pad.IsOnLayer(lid)]
            hit = _link(M.b, cur, t["net"], pad, lids[0], _tied_points(M.b, t["net"]), constraints, M, copper_only=True) if lids else None
            if not hit:
                row.update(state="pour", reason=f"no free track on its layer to a via of {t['net']} within {LINK_REACH:g} mm")
                continue
            rule = M.net_rule(t["net"])
            res = _guarded(cur, tmp, t["net"], hit[0], constraints, (rule["via_dia"], rule["via_drill"]), close=False)[1]
            if res["accepted"]:
                os.replace(tmp, out)
                pro = os.path.splitext(board)[0] + ".kicad_pro"
                if os.path.exists(pro) and os.path.abspath(board) != os.path.abspath(out):
                    import shutil
                    shutil.copyfile(pro, os.path.splitext(out)[0] + ".kicad_pro")
                cur = out
                row.update(state="link", path=hit[0])
            else:
                row.update(state="pour", reason="link refused: " + str(res.get("reason"))[:160])
    finally:
        for f in (tmp, os.path.splitext(tmp)[0] + ".kicad_pro", tmp + ".session", out + ".session"):
            _drop(f)
        if keep is None:
            _drop(H.FILL_CACHE_DIR)
            H.FILL_CACHE_DIR = None
    return rows


def repair(board, out, net, constraints=None, at=None, max_sites=24, budget=None):
    """Join an open same-net pour island with one guarded via, stub or link. Writes `out` only when its open count falls.
    budget: seconds this call may spend; no further guarded edit starts after it (the result says so)."""
    keep = H.FILL_CACHE_DIR
    if keep is None:                                  # every model of one board content in this call shares one zone refill
        H.FILL_CACHE_DIR = out + ".fill"
    try:
        return _repair(board, out, net, constraints, at, max_sites, None if budget is None else time.monotonic() + budget)
    finally:
        if keep is None:
            _drop(H.FILL_CACHE_DIR)
            H.FILL_CACHE_DIR = None


def _late(stop):
    return stop is not None and time.monotonic() > stop


def _repair(board, out, net, constraints, at, max_sites, stop):
    import pcbnew
    R = RR.rules(constraints and __import__("fluxplace.constraints", fromlist=["load"]).load(constraints), board)
    via = (R["via"], R["drill"])
    M = H._model(board, constraints)                 # k11: one refilled copper model (and its board) for every probe of this call
    b = M.b
    pours = [z for z in b.Zones() if not z.GetIsRuleArea() and z.GetNetname() == net]
    if not pours:
        return {"accepted": False, "net": net, "reason": f"net {net!r} has no copper pour"}
    records = H.open_nets(board, constraints, M=M)["connections"]
    if at is None and not any(r["net"] == net for r in records):   # the rung calls until nothing is left: not an error
        return {"accepted": False, "net": net, "reason": f"net {net!r} has no open connection"}
    x, y = _start(records, net, at)
    sites = [(None, x, y)]                            # (island index or None, x, y)
    for radius in (0.3, 0.6, 1.0, 1.5):
        sites += [(None, x + radius * math.cos(k * math.pi / 4), y + radius * math.sin(k * math.pi / 4)) for k in range(8)]
    if at is None:
        inside = [(k, ix, iy) for k, g in enumerate(island_groups(b, net, M=M)) for ix, iy in g]
        sites = inside + sites
        max_sites += len(inside)
    reasons, futile = [], set()

    def free(k, path, action, where):
        """Probe and lay one candidate that rips nothing -> the accepted result, or None (the refusal is noted for _rip_lay)."""
        probe = H.probe(board, net, path, constraints, via=via, M=M)
        if not probe["ok"]:
            reasons.append(dict(where, island=k, reason="probe: " + str(probe["violations"][:1])))
            return None
        if _late(stop):
            return None
        result = H.lay(board, net, path, out=out, constraints=constraints, via=via)
        if result["accepted"]:
            return dict(where, accepted=True, action=action, net=net, via=list(via), out=out, measure=result.get("measure", {}))
        reasons.append(dict(where, island=k, reason=result.get("reason")))
        _note_futile(futile, k, result)
        _drop(out + ".session")
        return None
    for k, cx, cy in sites[:max_sites]:
        if k in futile:
            continue
        pos = pcbnew.VECTOR2I(pcbnew.FromMM(cx), pcbnew.FromMM(cy))
        hit_layers = {lid for lid in {lid for z in pours for lid in z.GetLayerSet().Seq()}
                      if PTF.fill_hit(b, net, lid, pcbnew.FromMM(cx), pcbnew.FromMM(cy))}
        pads = [p for f in b.GetFootprints() for p in f.Pads() if p.GetNetname() == net]
        hit_layers.update(lid for p in pads for lid in p.GetLayerSet().Seq() if p.HitTest(pos, int(via[0] * 1e6 / 2)))
        if len(hit_layers) < 2:
            continue
        hit = free(k, f"via {cx:.4f},{cy:.4f}", "via", {"at": [round(cx, 4), round(cy, 4)]})
        if hit:
            return hit
    for k, path in (island_stubs(b, net, M) if at is None else []):   # k11: no via site inside: a pad stub to a via outside the island
        hit = None if k in futile else free(k, path, "stub+via", {"path": path})
        if hit:
            return hit
    if at is None:
        opens0 = {}
        for r in records:
            opens0[r["net"]] = opens0.get(r["net"], 0) + 1
        cons, names = _policy(constraints, M)
        links = island_links(b, board, net, constraints, M, names, cons, futile)
        for k, path, ids in links:                    # a free track on the island's layer to copper of the net that is already tied
            hit = None if ids or k in futile else free(k, path, "link", {"path": path})
            if hit:
                return hit
        # k15: every free site is blocked: rip the blocking unlocked track, then lay
        hit = _rip_lay(board, out, net, constraints, via, M, reasons, opens0, [l for l in links if l[2]], futile, stop)
        if hit:
            return hit
    if _late(stop):
        return {"accepted": False, "net": net, "reason": "out of time before a guarded repair was found", "attempts": reasons}
    return {"accepted": False, "net": net, "reason": "no legal guarded stitching-via site joins the island", "attempts": reasons}


RIP_IDS = 16                        # k15: most blocking track pieces one rip-and-lay may remove (routed nets come in short pieces)
RIP_NETS = 2                        # k15: most nets one rip-and-lay may open (each is re-laid by the caller)
RIP_TRIES = 8                       # k15: rip-and-lay candidates tried per call (each is one guarded edit)
RIP_KINDS = ("clearance", "crossing", "via_clearance")
RELAY_ROUNDS = 6                    # connections one rip-and-lay re-lays by search before the rest goes to the caller
_STILL = re.compile(r"target not closed: open connections of \S+ (\d+) -> (\d+)")


def _drop(path):
    import shutil
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.exists(path):
        os.remove(path)


def _policy(constraints, M):
    """-> (constraints dict, every net name of the board)."""
    from . import constraints as CN
    return CN.load(constraints) if isinstance(constraints, str) else (constraints or {}), sorted({o.net for o in M.obs if o.net})


def _rip_ids(M, hits, names, cons):
    """Ids of the copper `hits` when a rip may take all of it: unlocked tracks of non-hard nets, <= RIP_IDS pieces of <= RIP_NETS
    nets; else None."""
    from . import lockpolicy as LP
    if any(o.kind != "track" or o.locked or LP.level(o.net, cons, names) == LP.HARD for o in hits):
        return None
    ids = {o.id for o in hits}
    return ids if 0 < len(ids) <= RIP_IDS and len({o.net for o in hits}) <= RIP_NETS else None


def _rippable(M, net, viols, names, cons):
    """Full ids of the tracks behind `viols` when every violation is one an unlocked, non-hard track of another net causes; else
    None."""
    hits, named = [], {str(v.get("item")) for v in viols if v.get("id")}
    for v in viols:
        if not v.get("id") and v.get("kind") == "hole_clearance" and v.get("with") in named:
            continue                                  # the hole check of a via against a track that is ripped anyway
        if v.get("kind") not in RIP_KINDS or not str(v.get("item", "")).startswith("track ") or v.get("locked") or v.get("net") in (None, "", net):
            return None
        hit = [o for o in H._resolve(M, v["id"]) if o.kind == "track" and not o.locked]
        if not hit:
            return None
        hits += hit
    return _rip_ids(M, hits, names, cons)


def _note_futile(futile, k, result):
    """A candidate of island `k` that is DRC-clean and opens nothing but closes no open of the net: the island is not an open one
    (the copper model missed its tie), so its other candidates are skipped."""
    m = _STILL.fullmatch(str(result.get("reason") or ""))
    if k is not None and m and m.group(1) == m.group(2):
        futile.add(k)


def _strip_ids(board, dst, ids):
    """Copy `board` to `dst` without the tracks whose uuid is in `ids` -> ({net: count}, {net: widest ripped track mm})."""
    import pcbnew
    b = pcbnew.LoadBoard(board)
    cut, wide = {}, {}
    for t in list(b.GetTracks()):
        if t.m_Uuid.AsString() in ids:
            cut[t.GetNetname()] = cut.get(t.GetNetname(), 0) + 1
            wide[t.GetNetname()] = max(wide.get(t.GetNetname(), 0.0), pcbnew.ToMM(t.GetWidth()))
            b.Delete(t)
    b.Save(dst)
    pro = os.path.splitext(board)[0] + ".kicad_pro"
    if os.path.exists(pro):
        import shutil
        shutil.copyfile(pro, os.path.splitext(dst)[0] + ".kicad_pro")
    return cut, wide


def _guarded(board, out, net, path, constraints, via, ids=(), width=None, close=True, any_angle=False, may_open=False):
    """Rip `ids` and lay `path` (at `width`, else the net's rule width) in ONE guarded edit of `board`: refused when a DRC error
    appears, when any net opens (unless `may_open`: the caller re-lays and re-counts) and, with `close`, when no open of `net`
    closes (handroute._edit). -> ({net: ripped count}, lay result)."""
    cut = {}

    def mutate(b, M):
        for t in list(b.GetTracks()):
            if t.m_Uuid.AsString() in ids:
                cut[t.GetNetname()] = cut.get(t.GetNetname(), 0) + 1
                b.Delete(t)
        prims = H.G.parse_path(path, M.cu)
        pr = H.probe_prims(M, net, prims, width, via, any_angle, skip_ids=ids)
        if not pr["ok"]:
            raise ValueError("probe failed: " + str(pr["violations"][:2]))
        H._apply_prims(b, M, net, prims, width or M.net_rule(net)["width"], via)
        return {"ripped": dict(cut)}
    return cut, H._edit(board, out, mutate, "shove" if may_open else "lay", net, path, constraints, False, close)


def _relay(board, nets, wide, opens0, constraints, cons, layers):
    """Re-lay in place what a rip opened: each open connection of `nets` gets a searched path (handroute.suggest on `layers`, the
    signal layers of the board before the rip: a re-laid track never slots a plane) laid as wide as the copper the rip took from
    that net (a [power] rail: no wider than its rail width), one guarded edit each. The link laid before it holds the island's
    channel, so this is where a ripped net's layer change moves off the island. Nothing is searched while none of `nets` has more
    opens than `opens0`. -> {net: open connections} of the whole board afterwards (kicad-cli DRC)."""
    from . import power as PW
    wide = {n: min(w, PW.track_width_mm(cons, n) or w) for n, w in wide.items()}
    tmp = os.path.splitext(board)[0] + ".relay.kicad_pcb"
    dead, count = set(), {}
    try:
        for rnd in range(RELAY_ROUNDS + 1):
            count = dict(H.summarize_drc(H.drc_raw(board))["unc_by_net"])
            if rnd == RELAY_ROUNDS or not any(count.get(n, 0) > opens0.get(n, 0) for n in nets):
                break
            M = H._model(board, constraints)
            recs = H.open_nets(board, constraints, M=M)["connections"]
            todo = [r for r in recs if r["net"] in nets and (r["net"], r["a"]["x"], r["a"]["y"], r["b"]["x"], r["b"]["y"]) not in dead]
            if not todo:
                break
            r = todo[0]
            done = False
            for free_angle in (False, True):          # a stub too short for the 45-degree grid (ends that all but touch) is laid straight
                found = H.suggest(board, r["net"], rec=r, constraints=constraints, width=wide.get(r["net"]), layers=layers,
                                  any_angle=free_angle, M=M)
                done = found.get("found") and _guarded(board, tmp, r["net"], found["path"], constraints, tuple(found["via"]),
                                                       width=found["width"], any_angle=free_angle)[1]["accepted"]
                if done or "exact probe" not in str(found.get("reason")):
                    break
            if done:
                os.replace(tmp, board)
            else:
                dead.add((r["net"], r["a"]["x"], r["a"]["y"], r["b"]["x"], r["b"]["y"]))
    finally:
        for f in (tmp, os.path.splitext(tmp)[0] + ".kicad_pro", tmp + ".session", os.path.splitext(tmp)[0] + ".ledger.json"):
            _drop(f)
    return count


def _prune(board, nets):
    """Delete in place the copper of `nets` that connects nothing (launder.dead_copper): what a rip and re-lay of them left behind
    (the old run beyond the new join), and pull a free track end back to the last point another track of the net joins it (a
    re-lay that starts mid-track leaves the rest of that track as a stub). -> pieces removed or trimmed."""
    import pcbnew
    from . import launder as LA
    b = pcbnew.LoadBoard(board)
    dead = LA.dead_copper(b, {t.GetNetname() for t in b.GetTracks()} - set(nets)) if nets else []
    for t in dead:
        b.Remove(t)
    if dead:
        b.BuildConnectivity()
    conn, n = b.GetConnectivity(), len(dead)
    for t in [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_TRACE_T and t.GetNetname() in nets and not t.IsLocked()]:
        a, c = t.GetStart(), t.GetEnd()
        d = math.hypot(c.x - a.x, c.y - a.y)
        others = list(conn.GetConnectedTracks(t)) + list(conn.GetConnectedPads(t))
        if not d or not others or any(o.Type() == pcbnew.PCB_PAD_T for o in others):
            continue
        ends = [p for o in others for p in ((o.GetPosition(),) if o.Type() == pcbnew.PCB_VIA_T else (o.GetStart(), o.GetEnd())) if t.HitTest(p)]
        par = sorted(((p.x - a.x) * (c.x - a.x) + (p.y - a.y) * (c.y - a.y)) / (d * d) for p in ends)
        used = [any(o.HitTest(e) for o in others) for e in (a, c)]
        if len(par) < 1 or used[0] == used[1]:
            continue
        cut = max(par) if used[0] else min(par)       # the free end moves to the outermost join
        if (1.0 - cut if used[0] else cut) * d < pcbnew.FromMM(0.01) or not 0.0 < cut < 1.0:
            continue
        at = pcbnew.VECTOR2I(int(round(a.x + (c.x - a.x) * cut)), int(round(a.y + (c.y - a.y) * cut)))
        t.SetEnd(at) if used[0] else t.SetStart(at)
        n += 1
    if n:
        pcbnew.SaveBoard(board, b)
    return n


def _rip_lay(board, out, net, constraints, via, M, reasons, opens0=None, links=(), futile=(), stop=None):
    """k15 (sealed pour islands): a via site / pad stub refused only by unlocked tracks of other (non-hard) nets, or a link to tied
    copper of the net routed through such tracks (`links` [(island, path, ids)]), is laid after those tracks are ripped (links first:
    a track holds the island's channel where a freed pour would be walled in again; then fewest rips; a rail's copper last) and
    the ripped nets are re-laid by search (_relay). A [power] rail must come out whole; a plain net still open is named in `ripped`
    {net: tracks} for the caller's router (the close guard judges the whole). -> the accepted result, or None."""
    cons, names = _policy(constraints, M)
    from . import gaps as GP, power as PW
    rails = {n for n, r in PW.plan(cons).items() if r and r.get("track_mm") is not None}
    opens0, futile, layers = opens0 or {}, set(futile), GP.signal_layers(board)
    cands = [(True, path, ids, k) for k, path, ids in links]
    for r in reasons:
        path = r.get("path") or (f"via {r['at'][0]:.4f},{r['at'][1]:.4f}" if r.get("at") else None)
        if not path or not str(r.get("reason", "")).startswith("probe"):
            continue
        pr = H.probe(board, net, path, constraints, via=via, M=M)
        ids = None if pr["ok"] else _rippable(M, net, pr["violations"], names, cons)
        if ids and H.probe_prims(M, net, H.G.parse_path(path, M.cu), None, via, skip_ids=ids)["ok"]:
            cands.append((False, path, ids, r.get("island")))
    nets_of = {o.id: o.net for o in M.obs if o.id}
    seen = set()
    for link, path, ids, k in sorted(cands, key=lambda c: (any(nets_of.get(i) in rails for i in c[2]), not c[0], len(c[2]))):
        key = (frozenset(ids), link)
        if key in seen or len(seen) >= RIP_TRIES or k in futile or _late(stop):
            continue
        seen.add(key)
        rail, relay = any(nets_of.get(i) in rails for i in ids), True
        if link:                                      # ONE guarded edit: rip + link (laid even when the rip alone frees the pour)
            cut, result = _guarded(board, out, net, path, constraints, via, ids, close=False, may_open=True)
            wide = {}
            for o in M.obs:
                if o.id in ids and o.kind == "track":
                    wide[o.net] = max(wide.get(o.net, 0.0), 2 * o.g[3])
        else:
            cut, result = _guarded(board, out, net, path, constraints, via, ids) if rail else ({}, {"accepted": False})
            relay = not result["accepted"]
        if not link and relay:                        # a rip that opens its net: lay on the stripped board, then re-lay the ripped nets
            _drop(out)
            tmp = out + ".rip.kicad_pcb"
            cut, wide = _strip_ids(board, tmp, ids)
            try:
                result = H.lay(tmp, net, path, out=out, constraints=constraints, via=via)
                m = _STILL.search(str(result.get("reason") or ""))
                if not result["accepted"] and m and int(m.group(1)) < opens0.get(net, 0):
                    import shutil                        # the rip alone let the net's own pour reach the island: the stripped board is the repair
                    shutil.copyfile(tmp, out)
                    path, result = "rip only", {"accepted": True}
                else:
                    _note_futile(futile, k, result)
            finally:
                for f in (tmp, os.path.splitext(tmp)[0] + ".kicad_pro"):
                    _drop(f)
        if relay and result["accepted"]:
            count = _relay(out, set(cut), wide, opens0, constraints, cons, layers)
            left = sorted(n for n in set(cut) & rails if count.get(n, 0) > opens0.get(n, 0))
            other = sorted(n for n in count if n not in cut and n != net and count[n] > opens0.get(n, 0))
            if left:                                     # k15: a rail is never left open for a later router
                result = {"accepted": False, "reason": "rip opened rail(s) the re-lay could not close: " + ", ".join(left)}
            elif other:
                result = {"accepted": False, "reason": "lay opened connection(s): " + ", ".join(other)}
            elif count.get(net, 0) >= opens0.get(net, 0):
                result = {"accepted": False, "reason": f"target not closed: open connections of {net} {opens0.get(net, 0)} -> {count.get(net, 0)}"}
                _note_futile(futile, k, result)
            if not result["accepted"]:
                _drop(out)
            else:                                        # a re-laid net keeps no free end of its old run
                _prune(out, {n for n in cut if count.get(n, 0) <= opens0.get(n, 0)})
        if result["accepted"]:
            kind = "rip" if path == "rip only" else "rip+" + ("link" if link else "via" if path.startswith("via") else "stub+via")
            return {"accepted": True, "action": kind, "net": net, "path": path,
                    "via": list(via), "out": out, "ripped": cut, "measure": result.get("measure", {})}
        reasons.append({"path": path, "island": k, "rip": sorted(cut), "reason": result.get("reason")})
        _drop(out + ".session")
    return None


def cmd(a):
    import json
    out = a.out or os.path.splitext(a.board)[0] + ".pour-island.kicad_pcb"
    if os.path.abspath(out) == os.path.abspath(a.board):
        raise SystemExit("pour-island: --out must differ from the input board")
    at = tuple(float(v) for v in a.at.split(",")) if a.at else None
    result = repair(a.board, out, a.net, a.constraints, at)
    if a.report:
        json.dump(result, open(a.report, "w"), indent=1)
    print(json.dumps(result, indent=1) if a.json else
          (f"pour-island: via placed on {a.net} at {result['at']} -> {out}" if result["accepted"] else
           f"pour-island: no guarded repair for {a.net}: {result['reason']}"))
    return 0 if result["accepted"] else 1


def add_parser(sub):
    p = sub.add_parser("pour-island", help="join a same-net pour island with one guarded stitching via")
    p.add_argument("board")
    p.add_argument("--net", required=True)
    p.add_argument("--out", default=None)
    p.add_argument("--at", default=None, help="optional island-centre hint X,Y mm")
    p.add_argument("--constraints", default=None)
    p.add_argument("--report", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd)
