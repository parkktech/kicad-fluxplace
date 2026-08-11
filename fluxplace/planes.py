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
