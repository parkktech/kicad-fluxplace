"""Plane strategy for the automated pipeline.

A 4/6-layer board's plane nets (GND, sometimes a power rail) should NEVER be
routed as traces: pour them on every copper layer, connect pads SOLID (thermal
spokes starve at fine pitch and DRC flags them), and drop a collision-safe
stitching via grid so surface pads reach the inner planes. The router is then
run WITHOUT the plane nets and, as a final step, asked to connect any pad the
fill physically can't reach (fine-pitch connector rows) — see cli.cmd_auto.

pcbnew gotchas encoded here (each cost a debug session):
  * ZONE.SetOutline() does NOT copy: if the SHAPE_POLY_SET is garbage-collected
    before Save(), KiCad segfaults inside Save. Hold refs until after Save.
  * Zone fill on freshly-created zones in the same process is fine, but always
    exit with os._exit() from scripts — interpreter teardown after heavy
    pcbnew use segfaults (harmless but masks your real exit status).
"""
import math
import collections

import pcbnew

_keepalive = []   # refs held so SWIG proxies outlive Save() — see module docstring


def _mm(v):
    return pcbnew.FromMM(v)


def pour(board, netname="GND", layers=None, clearance=0.25, min_thickness=0.2,
         solid=True):
    """Add a full-board pour of `netname` on each layer that doesn't already
    have a board-level zone. Returns number of zones added."""
    net = board.FindNet(netname)
    if net is None:
        return 0
    nc = net.GetNetCode()
    bb = board.GetBoardEdgesBoundingBox()
    m = _mm(0.5)
    x1, y1, x2, y2 = bb.GetX() + m, bb.GetY() + m, bb.GetRight() - m, bb.GetBottom() - m
    if layers is None:
        layers = [pcbnew.F_Cu, pcbnew.B_Cu]
        n_inner = pcbnew.PCB_LAYER_ID_COUNT  # not meaningful; caller passes real list
    have = {z.GetLayer() for z in board.Zones()}
    added = 0
    for L in layers:
        if L in have:
            continue
        z = pcbnew.ZONE(board)
        z.SetLayer(L)
        z.SetNetCode(nc)
        z.SetIsFilled(False)
        z.SetLocalClearance(_mm(clearance))
        z.SetMinThickness(_mm(min_thickness))
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL if solid
                           else pcbnew.ZONE_CONNECTION_THERMAL)
        poly = pcbnew.SHAPE_POLY_SET()
        poly.NewOutline()
        for (x, y) in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
            poly.Append(int(x), int(y))
        z.SetOutline(poly)
        board.Add(z)
        _keepalive.append((z, poly))
        added += 1
    return added


def stitch(board, netname="GND", pitch=3.0, via_d=0.6, via_drill=0.3,
           keep=0.55, edge_in=1.0):
    """Collision-safe stitching via grid tying the surface pours to the inner
    planes. Avoids every non-plane pad and track with `keep` mm margin."""
    net = board.FindNet(netname)
    if net is None:
        return 0
    nc = net.GetNetCode()
    bb = board.GetBoardEdgesBoundingBox()
    K = _mm(keep)
    obst = []
    for f in board.GetFootprints():
        for pad in f.Pads():
            if pad.GetNetCode() == nc:
                continue
            p = pad.GetPosition(); s = pad.GetSize()
            obst.append((p.x - s.x // 2 - K, p.y - s.y // 2 - K,
                         p.x + s.x // 2 + K, p.y + s.y // 2 + K))
    for t in board.GetTracks():
        if t.GetNetCode() == nc:
            continue
        a = t.GetStart(); c = t.GetEnd(); w = t.GetWidth() // 2 + K
        obst.append((min(a.x, c.x) - w, min(a.y, c.y) - w,
                     max(a.x, c.x) + w, max(a.y, c.y) + w))
    B = _mm(5.0)
    grid = collections.defaultdict(list)
    for o in obst:
        for gx in range(int(o[0] // B), int(o[2] // B) + 1):
            for gy in range(int(o[1] // B), int(o[3] // B) + 1):
                grid[(gx, gy)].append(o)

    def clear(x, y):
        gx, gy = int(x // B), int(y // B)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for o in grid.get((gx + dx, gy + dy), ()):
                    if o[0] <= x <= o[2] and o[1] <= y <= o[3]:
                        return False
        return True

    P = _mm(pitch); E = _mm(edge_in); placed = 0
    x = bb.GetX() + E
    while x < bb.GetRight() - E:
        y = bb.GetY() + E
        while y < bb.GetBottom() - E:
            if clear(int(x), int(y)):
                v = pcbnew.PCB_VIA(board)
                v.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
                v.SetWidth(_mm(via_d)); v.SetDrill(_mm(via_drill))
                v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
                v.SetNetCode(nc)
                board.Add(v)
                _keepalive.append(v)
                placed += 1
            y += P
        x += P
    return placed


def via_stub_ends(board, netnames=("GND",), via_d=0.6, via_drill=0.3,
                  keep=0.2):
    """Targeted plane finalize: freerouting routes plane nets as short stubs
    and trusts the plane to complete them; where the refit pour has a void,
    connectivity breaks (kicad-cli 'missing connection' on GND/+5V only).
    Drop a through-via at every DANGLING plane-net track endpoint — an
    endpoint not already near a same-net via or pad — wherever it clears
    other-net copper. Redundant plane vias are harmless; missing ones are
    open circuits. Returns vias placed."""
    placed = 0
    K = _mm(keep)
    for netname in netnames:
        net = board.FindNet(netname)
        if net is None:
            continue
        nc = net.GetNetCode()
        anchors = []          # same-net via/pad positions (already tied down)
        for t in board.GetTracks():
            if t.GetNetCode() == nc and t.GetClass() == "PCB_VIA":
                p = t.GetPosition()
                anchors.append((p.x, p.y))
        for f in board.GetFootprints():
            for pad in f.Pads():
                if pad.GetNetCode() == nc:
                    p = pad.GetPosition()
                    anchors.append((p.x, p.y))
        obst = []             # other-net copper the new via must clear
        for f in board.GetFootprints():
            for pad in f.Pads():
                if pad.GetNetCode() == nc:
                    continue
                p = pad.GetPosition(); s = pad.GetSize()
                r = max(s.x, s.y) // 2 + _mm(via_d) // 2 + K
                obst.append((p.x, p.y, r))
        segs = []
        for t in board.GetTracks():
            if t.GetNetCode() == nc:
                continue
            a = t.GetStart(); b = t.GetEnd()
            segs.append((a.x, a.y, b.x, b.y,
                         t.GetWidth() // 2 + _mm(via_d) // 2 + K))

        def _seg_hit(x, y):
            for ax, ay, bx, by, r in segs:
                dx, dy = bx - ax, by - ay
                L2 = dx * dx + dy * dy
                u = 0.0 if L2 == 0 else max(
                    0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
                px, py = ax + u * dx, ay + u * dy
                if (x - px) ** 2 + (y - py) ** 2 < r * r:
                    return True
            return False

        NEAR = _mm(0.4)
        for t in list(board.GetTracks()):
            if t.GetNetCode() != nc or t.GetClass() == "PCB_VIA":
                continue
            for end in (t.GetStart(), t.GetEnd()):
                if any(abs(end.x - ax) < NEAR and abs(end.y - ay) < NEAR
                       for ax, ay in anchors):
                    continue
                if any((end.x - ox) ** 2 + (end.y - oy) ** 2 < r * r
                       for ox, oy, r in obst):
                    continue
                if _seg_hit(end.x, end.y):
                    continue
                v = pcbnew.PCB_VIA(board)
                v.SetPosition(pcbnew.VECTOR2I(end.x, end.y))
                v.SetWidth(_mm(via_d)); v.SetDrill(_mm(via_drill))
                v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
                v.SetNetCode(nc)
                board.Add(v)
                _keepalive.append(v)
                anchors.append((end.x, end.y))
                placed += 1
    return placed


def via_at_points(board, netname, points_mm, via_d=0.6, via_drill=0.3,
                  keep=0.2):
    """Drop a through-via at each (x, y) mm point on `netname`, skipping
    spots that would graze other-net copper. For plane nets this is the
    universal open-circuit repair: any two disconnected same-net islands
    both reach the inner plane through their via. Returns vias placed."""
    net = board.FindNet(netname)
    if net is None:
        return 0
    nc = net.GetNetCode()
    K = _mm(keep)
    obst = []
    for f in board.GetFootprints():
        for pad in f.Pads():
            if pad.GetNetCode() == nc:
                continue
            p = pad.GetPosition(); s = pad.GetSize()
            obst.append((p.x, p.y, max(s.x, s.y) // 2 + _mm(via_d) // 2 + K))
    segs = []
    for t in board.GetTracks():
        if t.GetNetCode() == nc:
            continue
        a = t.GetStart(); b = t.GetEnd()
        segs.append((a.x, a.y, b.x, b.y,
                     t.GetWidth() // 2 + _mm(via_d) // 2 + K))
    placed = 0
    for (xm, ym) in points_mm:
        x, y = _mm(xm), _mm(ym)
        if any((x - ox) ** 2 + (y - oy) ** 2 < r * r for ox, oy, r in obst):
            continue
        hit = False
        for ax, ay, bx, by, r in segs:
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            u = 0.0 if L2 == 0 else max(
                0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
            px, py = ax + u * dx, ay + u * dy
            if (x - px) ** 2 + (y - py) ** 2 < r * r:
                hit = True
                break
        if hit:
            continue
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
        v.SetWidth(_mm(via_d)); v.SetDrill(_mm(via_drill))
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        v.SetNetCode(nc)
        board.Add(v)
        _keepalive.append(v)
        placed += 1
    return placed


def tie_tracks_to_plane(board, netname, plane_layer, pitch=6.0, via_d=0.6,
                        via_drill=0.3, keep=0.2):
    """Guarantee plane-net connectivity: every track run on `netname` gets a
    through-via VERIFIED to land on filled plane copper (HitTestFilledArea),
    spaced ~pitch. Freerouting routes plane nets as disconnected stubs and
    trusts the plane; where the pour has voids the stub floats. Blind
    via-dropping can't fix that (a via in a void connects nothing) — the
    fill test is the difference. Returns vias placed."""
    net = board.FindNet(netname)
    if net is None:
        return 0
    nc = net.GetNetCode()
    lid = board.GetLayerID(plane_layer)
    zones = [z for z in board.Zones()
             if not z.GetIsRuleArea() and z.GetNetCode() == nc
             and z.IsOnLayer(lid)]
    if not zones:
        return 0
    K = _mm(keep)
    obst = []
    for f in board.GetFootprints():
        for pad in f.Pads():
            if pad.GetNetCode() == nc:
                continue
            p = pad.GetPosition(); s = pad.GetSize()
            obst.append((p.x, p.y, max(s.x, s.y) // 2 + _mm(via_d) // 2 + K))
    segs = []
    vias_here = []
    for t in board.GetTracks():
        if t.GetNetCode() != nc:
            a = t.GetStart(); b = t.GetEnd()
            segs.append((a.x, a.y, b.x, b.y,
                         t.GetWidth() // 2 + _mm(via_d) // 2 + K))
        elif t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            vias_here.append((p.x, p.y))

    def clear_at(x, y):
        if any((x - ox) ** 2 + (y - oy) ** 2 < r * r for ox, oy, r in obst):
            return False
        for ax, ay, bx, by, r in segs:
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            u = 0.0 if L2 == 0 else max(
                0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
            px, py = ax + u * dx, ay + u * dy
            if (x - px) ** 2 + (y - py) ** 2 < r * r:
                return False
        return True

    def on_fill(x, y):
        pt = pcbnew.VECTOR2I(int(x), int(y))
        return any(z.HitTestFilledArea(lid, pt, 0) for z in zones)

    P = _mm(pitch)
    placed = 0
    for t in list(board.GetTracks()):
        if t.GetNetCode() != nc or t.GetClass() == "PCB_VIA":
            continue
        a = t.GetStart(); b = t.GetEnd()
        # already tied close enough?
        if any(min((a.x - vx) ** 2 + (a.y - vy) ** 2,
                   (b.x - vx) ** 2 + (b.y - vy) ** 2) < P * P
               for vx, vy in vias_here):
            continue
        # sample along the segment: midpoint first, then quarters, then ends
        cands = [0.5, 0.25, 0.75, 0.0, 1.0]
        for u in cands:
            x = a.x + u * (b.x - a.x)
            y = a.y + u * (b.y - a.y)
            if on_fill(x, y) and clear_at(x, y):
                v = pcbnew.PCB_VIA(board)
                v.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
                v.SetWidth(_mm(via_d)); v.SetDrill(_mm(via_drill))
                v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
                v.SetNetCode(nc)
                board.Add(v)
                _keepalive.append(v)
                vias_here.append((int(x), int(y)))
                placed += 1
                break
    return placed


def tie_floating_clusters(board, netname, plane_layer, via_d=0.6,
                          via_drill=0.3, keep=0.2):
    """The correct plane finalize: freerouting counts plane nets complete
    because every fragment is SUPPOSED to reach the inner plane — but some
    connectivity clusters end up with no via and no drilled pad, so they
    float. Walk the real connectivity clusters (pcbnew BuildConnectivity),
    find clusters with no tie to the plane, and drop ONE fill-verified,
    clearance-checked via per floating cluster. Proximity heuristics fail
    here (a neighbouring via can belong to a different cluster); cluster
    membership is the only truth. Returns vias placed."""
    net = board.FindNet(netname)
    if net is None:
        return 0
    nc = net.GetNetCode()
    lid = board.GetLayerID(plane_layer)
    zones = [z for z in board.Zones()
             if not z.GetIsRuleArea() and z.GetNetCode() == nc
             and z.IsOnLayer(lid)]
    if not zones:
        return 0
    board.BuildConnectivity()
    conn = board.GetConnectivity()

    wires = [t for t in board.GetTracks()
             if t.GetNetCode() == nc and t.GetClass() != "PCB_VIA"]

    # obstacle model for clearance-checking the new via
    K = _mm(keep)
    obst = []
    for f in board.GetFootprints():
        for pad in f.Pads():
            if pad.GetNetCode() == nc:
                continue
            p = pad.GetPosition(); s = pad.GetSize()
            obst.append((p.x, p.y, max(s.x, s.y) // 2 + _mm(via_d) // 2 + K))
    segs = []
    for t in board.GetTracks():
        if t.GetNetCode() != nc:
            a = t.GetStart(); b = t.GetEnd()
            segs.append((a.x, a.y, b.x, b.y,
                         t.GetWidth() // 2 + _mm(via_d) // 2 + K))

    def clear_at(x, y):
        if any((x - ox) ** 2 + (y - oy) ** 2 < r * r for ox, oy, r in obst):
            return False
        for ax, ay, bx, by, r in segs:
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            u = 0.0 if L2 == 0 else max(
                0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
            px, py = ax + u * dx, ay + u * dy
            if (x - px) ** 2 + (y - py) ** 2 < r * r:
                return False
        return True

    def on_fill(x, y):
        pt = pcbnew.VECTOR2I(int(x), int(y))
        return any(z.HitTestFilledArea(lid, pt, 0) for z in zones)

    seen = set()
    placed = 0
    for t in wires:
        if t.m_Uuid.AsString() in seen:
            continue
        # walk this cluster
        cluster, stack = [], [t]
        has_tie = False
        while stack:
            cur = stack.pop()
            uid = cur.m_Uuid.AsString()
            if uid in seen:
                continue
            seen.add(uid)
            cluster.append(cur)
            for nxt in conn.GetConnectedTracks(cur):
                if nxt.GetClass() == "PCB_VIA":
                    # a via only ties the cluster down if the plane fill
                    # actually reaches it — a via standing in a fill void
                    # (other-net clearance carve-out) floats
                    p = nxt.GetPosition()
                    if on_fill(p.x, p.y):
                        has_tie = True
                else:
                    stack.append(nxt)
            for pad in conn.GetConnectedPads(cur):
                if pad.GetDrillSize().x > 0:
                    p = pad.GetPosition()
                    if on_fill(p.x, p.y):
                        has_tie = True
        if has_tie:
            continue
        # floating cluster: put one via on it, on filled plane copper
        done = False
        for seg in cluster:
            a = seg.GetStart(); b = seg.GetEnd()
            for u in (0.5, 0.25, 0.75, 0.0, 1.0):
                x = a.x + u * (b.x - a.x)
                y = a.y + u * (b.y - a.y)
                if on_fill(x, y) and clear_at(x, y):
                    v = pcbnew.PCB_VIA(board)
                    v.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
                    v.SetWidth(_mm(via_d)); v.SetDrill(_mm(via_drill))
                    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
                    v.SetNetCode(nc)
                    board.Add(v)
                    _keepalive.append(v)
                    placed += 1
                    done = True
                    break
            if done:
                break
    return placed


def snap_opens(board, pairs, max_gap=4.0):
    """Close 'missing connection' items by SNAPPING the dangling fragment's
    endpoint onto its partner copper — no new copper is drawn, so no new
    clearance surface is created (bridge_opens' straight-line patches graze
    neighbors in dense areas). For each pair: find the same-net track whose
    endpoint sits at a reported position, and move that endpoint to the
    partner item's nearest point (via center / partner endpoint / projected
    point on the partner segment). Returns endpoints moved."""
    moved = 0
    tracks = [t for t in board.GetTracks() if t.GetClass() != "PCB_VIA"]
    vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]

    def find_end(net, pos, tol=_mm(0.6)):
        best = None
        for t in tracks:
            if t.GetNetname() != net:
                continue
            for which in (0, 1):
                e = t.GetStart() if which == 0 else t.GetEnd()
                d2 = (e.x - pos[0]) ** 2 + (e.y - pos[1]) ** 2
                if d2 < tol * tol and (best is None or d2 < best[0]):
                    best = (d2, t, which)
        return best

    def nearest_point(net, pos, exclude):
        """Closest point on any same-net copper (via center, track segment)."""
        best = None
        for v in vias:
            if v.GetNetname() != net:
                continue
            p = v.GetPosition()
            d2 = (p.x - pos[0]) ** 2 + (p.y - pos[1]) ** 2
            if best is None or d2 < best[0]:
                best = (d2, (p.x, p.y))
        for t in tracks:
            if t.GetNetname() != net or t is exclude:
                continue
            a = t.GetStart(); b = t.GetEnd()
            dx, dy = b.x - a.x, b.y - a.y
            L2 = dx * dx + dy * dy
            u = 0.0 if L2 == 0 else max(0.0, min(
                1.0, ((pos[0] - a.x) * dx + (pos[1] - a.y) * dy) / L2))
            px, py = a.x + u * dx, a.y + u * dy
            d2 = (px - pos[0]) ** 2 + (py - pos[1]) ** 2
            if best is None or d2 < best[0]:
                best = (d2, (int(px), int(py)))
        return best

    G2 = _mm(max_gap) ** 2
    for net_name, pa, la, pb, lb in pairs:
        for (p_frag, p_anchor) in (((pa), (pb)), ((pb), (pa))):
            frag_pos = (_mm(p_frag[0]), _mm(p_frag[1]))
            hit = find_end(net_name, frag_pos)
            if hit is None:
                continue
            _, t, which = hit
            anchor_pos = (_mm(p_anchor[0]), _mm(p_anchor[1]))
            np_ = nearest_point(net_name, anchor_pos, t)
            if np_ is None or np_[0] > G2:
                continue
            tgt = pcbnew.VECTOR2I(*np_[1])
            if which == 0:
                t.SetStart(tgt)
            else:
                t.SetEnd(tgt)
            moved += 1
            break
    return moved


def bridge_opens(board, pairs, width=0.15, max_gap=3.0):
    """Close 'missing connection' DRC items by drawing a short same-net track
    between the two reported item positions. The freerouting .ses import
    leaves sub-mm gaps (grid rounding): fragments sit 0.2-2.5 mm from the
    copper they belong to, so a plane via can't help — copper must touch.

    pairs: [(net, (xa, ya), layers_a, (xb, yb), layers_b), ...] with layers
    as sets like {'F.Cu'} ({'F.Cu','B.Cu'} for a via). Returns bridges drawn.
    """
    placed = 0
    for net_name, pa, la, pb, lb in pairs:
        net = board.FindNet(net_name)
        if net is None:
            continue
        gap = ((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2) ** 0.5
        if gap > max_gap or gap == 0:
            continue
        common = (set(la) & set(lb)) or set(la) or set(lb)
        layer = board.GetLayerID(sorted(common)[0])
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(_mm(pa[0]), _mm(pa[1])))
        t.SetEnd(pcbnew.VECTOR2I(_mm(pb[0]), _mm(pb[1])))
        t.SetWidth(_mm(width))
        t.SetLayer(layer)
        t.SetNetCode(net.GetNetCode())
        board.Add(t)
        _keepalive.append(t)
        placed += 1
    return placed


def refill(board):
    """Refill every zone (call after routing edits, before DRC)."""
    filler = pcbnew.ZONE_FILLER(board)
    zones = pcbnew.ZONES()
    for z in board.Zones():
        zones.append(z)
    filler.Fill(zones)


# JLCPCB 4/6-layer manufacturable floor. The router works at its own clearance;
# these are the RULES the DRC judges by — they must not exceed fab capability
# nor sit above what the router actually used (else hundreds of false hits).
DFM = dict(min_track=0.088, min_clearance=0.088, min_via_d=0.4,
           min_drill=0.15, hole_to_hole=0.2, hole_clearance=0.1,
           annular=0.075)


def finalize_dfm(board, rules=None, clamp_vias=True, graze_repair=True,
                 clearance=0.0975):
    """Embed a consistent manufacturable rule set in the board, clamp any via
    below the floor (router rescue passes emit 0.25 mm vias), and shrink vias
    that graze a foreign pad by hundredths (the plane-finalize router places
    0.45 vias inline with 0.4 mm-pitch connector rows: geometrically 0.075 mm
    of air where the rules want ~0.1 — one size step down resolves it).
    Returns (clamped, shrunk, still_grazing:[(x_mm, y_mm, gap_mm), ...])."""
    r = dict(DFM); r.update(rules or {})
    bds = board.GetDesignSettings()
    bds.m_TrackMinWidth = _mm(r["min_track"])
    bds.m_ViasMinSize = _mm(r["min_via_d"])
    bds.m_MinThroughDrill = _mm(r["min_drill"])
    bds.m_HoleToHoleMin = _mm(r["hole_to_hole"])
    bds.m_HoleClearance = _mm(r["hole_clearance"])
    bds.m_MinClearance = _mm(r["min_clearance"])
    clamped = 0
    if clamp_vias:
        floor_d = _mm(r["min_via_d"])
        for t in board.GetTracks():
            if t.GetClass() == "PCB_VIA" and t.GetWidth() < floor_d:
                t.SetWidth(_mm(max(r["min_via_d"], 0.45)))
                if t.GetDrillValue() > _mm(0.25):
                    t.SetDrill(_mm(0.25))
                clamped += 1
    shrunk = 0; grazing = []
    if graze_repair:
        pads = []
        for f in board.GetFootprints():
            for pad in f.Pads():
                p = pad.GetPosition(); s = pad.GetSize()
                pads.append((p.x, p.y, s.x // 2, s.y // 2, pad.GetNetCode()))
        CL = _mm(clearance)
        floor_d = _mm(r["min_via_d"])
        for t in board.GetTracks():
            if t.GetClass() != "PCB_VIA":
                continue
            vx, vy = t.GetPosition().x, t.GetPosition().y
            vr = t.GetWidth() // 2
            worst = None
            for (px, py, hx, hy, nc) in pads:
                if nc == t.GetNetCode():
                    continue
                if abs(vx - px) > hx + vr + CL or abs(vy - py) > hy + vr + CL:
                    continue
                dx = max(abs(vx - px) - hx, 0); dy = max(abs(vy - py) - hy, 0)
                gap = math.hypot(dx, dy) - vr
                if gap < CL and (worst is None or gap < worst):
                    worst = gap
            if worst is None:
                continue
            need = CL - worst
            if t.GetWidth() - 2 * need >= floor_d:
                t.SetWidth(int(t.GetWidth() - 2 * need) - _mm(0.005))
                shrunk += 1
            else:
                grazing.append((pcbnew.ToMM(vx), pcbnew.ToMM(vy),
                                round(pcbnew.ToMM(int(worst)), 3)))
    return clamped, shrunk, grazing


def sync_project_rules(pro_path, rules=None, netclass_clearance=None):
    """Write the same rule floor into the adjacent .kicad_pro (it OVERRIDES the
    board's embedded settings when kicad-cli/GUI loads the project) and drop the
    Default netclass clearance to what the router actually used."""
    import json, os
    if not os.path.exists(pro_path):
        return False
    r = dict(DFM); r.update(rules or {})
    d = json.load(open(pro_path))
    dr = d.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})
    dr["min_track_width"] = r["min_track"]
    dr["min_clearance"] = r["min_clearance"]
    dr["min_via_diameter"] = r["min_via_d"]
    dr["min_through_hole_diameter"] = r["min_drill"]
    dr["min_hole_to_hole"] = r["hole_to_hole"]
    dr["min_hole_clearance"] = r["hole_clearance"]
    dr["min_via_annular_width"] = r["annular"]
    if netclass_clearance is not None:
        for c in d.get("net_settings", {}).get("classes", []):
            c["clearance"] = netclass_clearance
    json.dump(d, open(pro_path, "w"), indent=2)
    return True
