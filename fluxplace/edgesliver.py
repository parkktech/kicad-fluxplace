"""Edge-sliver island removal: a same-net pour island that sits in the board-edge clearance band is a fill sliver,
not a connection. The fix is zone island removal (KiCad drops isolated fill below a min area), never a via.
Guarded by the caller: the close loop accepts the output only if the open count falls and nothing else rises."""
import os

from . import planes as PL


def _cons_edge(constraints):
    from . import constraints as CONS
    cons = CONS.load(constraints) if constraints else {}
    return float(((cons or {}).get("placement") or {}).get("edge_clearance_mm", 0.3))


def sliver_islands(b, net, clearance_mm):
    """-> [(zone, layer id, outline index, area mm2)]: `net`'s filled outlines whose bbox meets the edge band
    (the board edge box shrunk by clearance_mm) and that hold no pad of the net on that layer."""
    import pcbnew
    edge = b.GetBoardEdgesBoundingBox()
    c = pcbnew.FromMM(clearance_mm)
    band = (edge.GetLeft() + c, edge.GetTop() + c, edge.GetRight() - c, edge.GetBottom() - c)
    pads = [p for f in b.GetFootprints() for p in f.Pads() if p.GetNetname() == net]
    out = []
    for z in b.Zones():
        if z.GetIsRuleArea() or z.GetNetname() != net:
            continue
        for lid in z.GetLayerSet().CuStack():
            ps = z.GetFilledPolysList(lid)
            for i in range(ps.OutlineCount()):
                bb = ps.Outline(i).BBox()
                if bb.GetRight() < band[0] or bb.GetLeft() > band[2] or bb.GetBottom() < band[1] or bb.GetTop() > band[3]:
                    continue
                if any(p.IsOnLayer(lid) and ps.Contains(p.GetPosition(), i) for p in pads):
                    continue
                out.append((z, lid, i, abs(ps.Outline(i).Area()) / 1e12))
    return out


def repair(board, out, net, constraints=None):
    """Remove the edge-band sliver islands of `net` by zone island removal (min area just above the largest one)."""
    import pcbnew
    b = pcbnew.LoadBoard(board)
    PL.refill(b)
    hits = sliver_islands(b, net, _cons_edge(constraints))
    if not hits:
        return {"accepted": False, "net": net, "reason": f"net {net!r} has no edge-band sliver island"}
    limit = int(max(a for *_r, a in hits) * 1e12 * 1.01) + 1
    zones = {id(z): z for z, *_r in hits}
    for z in zones.values():
        z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_AREA)
        z.SetMinIslandArea(max(limit, int(z.GetMinIslandArea())))
    PL.refill(b)
    b.Save(out)
    return {"accepted": os.path.exists(out), "net": net, "islands": len(hits), "zones": len(zones), "out": out}
