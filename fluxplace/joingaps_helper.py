"""Runs under KiCad's python (has `pcbnew`), NOT the fluxplace venv:
    python3 joingaps_helper.py BOARD [--gap 0.3] [--extra-clearance 0] [--nets A,B]
Finds same-net copper ends (track end <-> track end / pad / via) in different connectivity clusters, a small gap apart, where
a straight segment of the end track's width keeps the required clearance to all other-net copper, holes, rule areas and the
board edge.  Prints JSON {"add": [{net, layer, a, b, width, kind}], "skipped": [{net, layer, at, reason}], "gap": g};
the board is not written (the caller appends the segments as text)."""
import json
import sys

import pcbnew

NM = 1e6


def main():
    path = sys.argv[1]
    gap = float(sys.argv[sys.argv.index("--gap") + 1]) if "--gap" in sys.argv else 0.3
    extra = float(sys.argv[sys.argv.index("--extra-clearance") + 1]) if "--extra-clearance" in sys.argv else 0.0
    b = pcbnew.LoadBoard(path)
    ds = b.GetDesignSettings()
    b.BuildConnectivity()
    conn = b.GetConnectivity()
    gnm, xnm = int(gap * NM), int(extra * NM)
    layers = [l for l in b.GetEnabledLayers().CuStack()]
    lname = {l: b.GetLayerName(l) for l in layers}
    tracks = [t for t in b.GetTracks()]
    segs = [t for t in tracks if t.Type() == pcbnew.PCB_TRACE_T]
    vias = [t for t in tracks if t.Type() == pcbnew.PCB_VIA_T]
    pads = list(b.GetPads())
    outline = pcbnew.SHAPE_POLY_SET()
    b.GetBoardPolygonOutlines(outline, True)
    chains = []
    for i in range(outline.OutlineCount()):
        chains.append(outline.Outline(i))
        chains.extend(outline.Hole(i, h) for h in range(outline.HoleCount(i)))
    areas = [z for z in b.Zones() if z.GetIsRuleArea() and (z.GetDoNotAllowTracks() or z.GetDoNotAllowVias())]

    def box(it):
        r = it.GetBoundingBox()
        return (r.GetLeft(), r.GetTop(), r.GetRight(), r.GetBottom())

    def near(bx, c, m):
        return not (bx[2] < c[0] - m or bx[0] > c[2] + m or bx[3] < c[1] - m or bx[1] > c[3] + m)
    items = []                      # other-net copper: (item, bbox, net, kind)
    for t in segs:
        items.append((t, box(t), t.GetNetCode(), "track"))
    for v in vias:
        items.append((v, box(v), v.GetNetCode(), "via"))
    for p in pads:
        items.append((p, box(p), p.GetNetCode(), "pad"))
    cache = {}

    def cid(it):
        """stable id of the connectivity cluster of `it` (smallest member uuid)."""
        k = it.m_Uuid.AsString()
        if k not in cache:
            cache[k] = min({q.m_Uuid.AsString() for q in conn.GetConnectedItems(it)} | {k})
        return cache[k]

    def on(it, l):
        return it.IsOnLayer(l)

    def mk(a, c, w, l, net):
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(a)
        t.SetEnd(c)
        t.SetWidth(w)
        t.SetLayer(l)
        t.SetNetCode(net)
        return t

    def legal(t, l, net):
        """-> None when the segment is legal, else the reason."""
        sh = t.GetEffectiveShape()
        bx = box(t)
        own = t.GetOwnClearance(l) + xnm
        for it, ib, inet, kind in items:
            if inet == net or not near(bx, ib, own + 2 * NM) or not it.IsOnLayer(l):
                continue
            clr = max(own, it.GetOwnClearance(l) + xnm)
            if it.GetEffectiveShape(l).Collide(sh, clr):
                return f"clearance to {kind} of net {it.GetNetname()}"
            if kind in ("pad", "via"):
                hs = it.GetEffectiveHoleShape()
                if hs is not None and hs.Collide(sh, ds.m_HoleClearance + xnm):
                    return f"hole clearance to {kind} of net {it.GetNetname()}"
        seg_ = pcbnew.SEG(t.GetStart(), t.GetEnd())
        lim = t.GetWidth() // 2 + ds.m_CopperEdgeClearance + xnm
        for ch in chains:                                # distance to the outline's edges (Collide would count "inside" too)
            n = ch.PointCount()
            for i in range(n):
                if seg_.Distance(pcbnew.SEG(ch.CPoint(i), ch.CPoint((i + 1) % n))) < lim:
                    return "board edge clearance"
        a0, a1 = t.GetStart(), t.GetEnd()
        n = max(2, int((a1 - a0).EuclideanNorm() / 50000))
        pts = [pcbnew.VECTOR2I(a0.x + (a1.x - a0.x) * i // n, a0.y + (a1.y - a0.y) * i // n) for i in range(n + 1)]
        for z in areas:
            if z.IsOnLayer(l) and z.GetDoNotAllowTracks() and any(z.Outline().Contains(q) for q in pts):
                return "rule area (keepout)"
        return None

    def dist_pt(shape, pt):
        return shape.Distance(pt)

    # same-net targets by net
    by_net = {}
    for t in segs:
        by_net.setdefault(t.GetNetCode(), []).append(t)
    pv = {}
    for it, ib, inet, kind in items:
        if kind in ("pad", "via"):
            pv.setdefault(inet, []).append(it)
    only = sys.argv[sys.argv.index("--nets") + 1].split(",") if "--nets" in sys.argv else None
    open_nets = set()                                   # only nets whose pads are not all joined can have a gap to close
    for net, ps in pv.items():
        if only is not None and ps[0].GetNetname() not in only:
            continue
        if len({cid(p) for p in ps}) > 1:
            open_nets.add(net)
    cands = []
    cell = max(gnm, 1)
    end_grid, pv_grid = {}, {}                          # (net, cx, cy) -> [(x, y, seg)] / [pad or via]
    for o in segs:
        for oe in (o.GetStart(), o.GetEnd()):
            end_grid.setdefault((o.GetNetCode(), oe.x // cell, oe.y // cell), []).append((oe, o))
    for net, lst in pv.items():
        for o in lst:
            bx = box(o)
            for cx in range((bx[0] - gnm) // cell, (bx[2] + gnm) // cell + 1):
                for cy in range((bx[1] - gnm) // cell, (bx[3] + gnm) // cell + 1):
                    pv_grid.setdefault((net, cx, cy), []).append(o)
    for t in segs:
        net = t.GetNetCode()
        if net <= 0 or net not in open_nets:
            continue
        l = t.GetLayer()
        for end in (t.GetStart(), t.GetEnd()):
            opts = []
            cx, cy = end.x // cell, end.y // cell
            seen = set()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for oe, o in end_grid.get((net, cx + dx, cy + dy), ()):
                        if o is t or o.GetLayer() != l:
                            continue
                        d = (oe - end).EuclideanNorm()
                        if 0 < d <= gnm:
                            opts.append((d, oe, "track", o))
                    for o in pv_grid.get((net, cx + dx, cy + dy), ()):
                        k = o.m_Uuid.AsString()
                        if k in seen or not on(o, l):
                            continue
                        seen.add(k)
                        d = dist_pt(o.GetEffectiveShape(l), end)
                        if 0 < d <= gnm:
                            opts.append((d, o.GetPosition(), o.GetClass().lower(), o))
            ct = cid(t) if opts else None
            for opt in sorted(opts, key=lambda x: x[0]):          # nearest partner in ANOTHER cluster
                if cid(opt[3]) != ct:
                    cands.append((opt[0], t, end, opt))
                    break
    cands.sort(key=lambda c: c[0])
    added, skipped, new_items = [], [], []
    merged = {}                                         # cluster-key union: key -> root
    def root(k):
        while merged.get(k, k) != k:
            k = merged[k]
        return k
    seen_pairs = set()
    for d, t, end, (dd, tgt, kind, o) in cands:
        net, l = t.GetNetCode(), t.GetLayer()
        pk = (l, frozenset(((end.x, end.y), (tgt.x, tgt.y))))
        if pk in seen_pairs:                            # the same gap seen from its other end
            continue
        seen_pairs.add(pk)
        info = {"net": t.GetNetname(), "layer": lname[l], "at": [round(end.x / NM, 4), round(end.y / NM, 4)],
                "gap": round(d / NM, 4)}
        if root(cid(t)) == root(cid(o)):
            skipped.append(dict(info, reason="already connected"))
            continue
        if d == 0:
            skipped.append(dict(info, reason="touching but not connected (not a gap)"))
            continue
        w = t.GetWidth()
        if kind == "track":
            w = min(w, o.GetWidth())
        seg = mk(end, tgt, w, l, net)
        why = legal(seg, l, net)
        if why is None:
            for q in new_items:
                if q.GetNetCode() != net and q.GetEffectiveShape().Collide(seg.GetEffectiveShape(), seg.GetOwnClearance(l) + xnm):
                    why = "clearance to a segment added in this pass"
                    break
        if why:
            skipped.append(dict(info, reason=why))
            continue
        new_items.append(seg)
        merged[root(cid(t))] = root(cid(o))
        added.append(dict(info, a=[end.x / NM, end.y / NM], b=[tgt.x / NM, tgt.y / NM], width=w / NM, kind=kind))
    print(json.dumps({"add": added, "skipped": skipped, "gap": gap, "candidates": len(cands)}))


main()
