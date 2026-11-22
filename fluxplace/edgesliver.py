"""Edge-sliver island removal: a same-net pour island that sits in the board-edge clearance band is a fill sliver,
not a connection. The fix is zone island removal (KiCad drops isolated fill below a min area), never a via.
Guarded by the caller: the close loop accepts the output only if the open count falls and nothing else rises."""
import os

from . import planes as PL


SLIVER_DEPTH_MM = 1.0       # PHASE3I F8: a sliver lies within clearance + this depth of the board edge ([flow] sliver_depth_mm)
SLIVER_MAX_MM2 = 2.0        # and is no larger than this ([flow] sliver_max_mm2)


def _cons_edge(constraints):
    """-> (edge clearance mm, sliver depth mm, sliver max area mm2)."""
    from . import constraints as CONS
    cons = CONS.load(constraints) if constraints else {}
    flow = (cons or {}).get("flow") or {}
    return (float(((cons or {}).get("placement") or {}).get("edge_clearance_mm", 0.3)),
            float(flow.get("sliver_depth_mm", SLIVER_DEPTH_MM)), float(flow.get("sliver_max_mm2", SLIVER_MAX_MM2)))


def sliver_islands(b, net, clearance_mm, depth_mm=SLIVER_DEPTH_MM, max_mm2=SLIVER_MAX_MM2):
    """-> [(zone, layer id, outline index, area mm2)]: `net`'s filled outlines whose bbox lies wholly inside one edge strip
    (the board edge box, `clearance_mm + depth_mm` deep), that hold no pad and no via of the net, and whose area is at
    most max_mm2. An island in the middle of the board is not a sliver (PHASE3I F8, R13)."""
    import pcbnew
    edge = b.GetBoardEdgesBoundingBox()
    d = pcbnew.FromMM(clearance_mm + depth_mm)
    L, T, R, B_ = edge.GetLeft(), edge.GetTop(), edge.GetRight(), edge.GetBottom()
    pads = [p for f in b.GetFootprints() for p in f.Pads() if p.GetNetname() == net]
    vias = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T and t.GetNetname() == net]
    out = []
    for z in b.Zones():
        if z.GetIsRuleArea() or z.GetNetname() != net:
            continue
        for lid in z.GetLayerSet().CuStack():
            ps = z.GetFilledPolysList(lid)
            for i in range(ps.OutlineCount()):
                bb = ps.Outline(i).BBox()
                in_strip = bb.GetRight() <= L + d or bb.GetLeft() >= R - d or bb.GetBottom() <= T + d or bb.GetTop() >= B_ - d
                if not in_strip:
                    continue
                area = abs(ps.Outline(i).Area()) / 1e12
                if area > max_mm2:
                    continue
                if any(p.IsOnLayer(lid) and ps.Contains(p.GetPosition(), i) for p in pads):
                    continue
                m = pcbnew.FromMM(1.0)           # a via of the net on or beside the island (its fill is cut round the hole)
                if any(bb.GetLeft() - m <= v.GetPosition().x <= bb.GetRight() + m and bb.GetTop() - m <= v.GetPosition().y <= bb.GetBottom() + m
                       for v in vias):
                    continue
                out.append((z, lid, i, area))
    return out


def repair(board, out, net, constraints=None):
    """Remove the edge-band sliver islands of `net` by zone island removal (min area just above the largest one, never
    above the sliver cap)."""
    import pcbnew
    b = pcbnew.LoadBoard(board)
    PL.refill(b)
    clr, depth, cap = _cons_edge(constraints)
    hits = sliver_islands(b, net, clr, depth, cap)
    if not hits:
        return {"accepted": False, "net": net, "reason": f"net {net!r} has no edge-band sliver island"}
    limit = min(int(max(a for *_r, a in hits) * 1e12 * 1.01) + 1, int(cap * 1e12))
    zones = {id(z): z for z, *_r in hits}
    for z in zones.values():
        z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_AREA)
        z.SetMinIslandArea(max(limit, int(z.GetMinIslandArea())))
    PL.refill(b)
    b.Save(out)
    return {"accepted": os.path.exists(out), "net": net, "islands": len(hits), "zones": len(zones), "out": out}
