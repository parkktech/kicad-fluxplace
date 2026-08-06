"""The only module that imports pcbnew. Reads parts+nets from a board and writes
positions back. Works both in the KiCad GUI (pass the live board) and headless
(load a .kicad_pcb path)."""
import pcbnew


def _mm(nm):
    return nm / 1e6


def read_board(board):
    """Return (parts, nets).
    parts: {ref: {value, footprint, w, h, x, y, locked}}
    nets:  {netname: [ref, ref, ...]}"""
    parts, nets = {}, {}
    from collections import defaultdict
    net_refs = defaultdict(set)
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        pos = fp.GetPosition()
        # KEEP-OUT extent = the FULL footprint bounding box (pads + silk + fab + courtyard).
        # This is the true physical size for every part: a module's outline (bigger than its
        # courtyard), a connector's body, an M.2 card region. The bbox is NOT centered on the
        # footprint origin for many connectors, so track the body-center offset explicitly.
        bb = fp.GetBoundingBox(False, False)   # False,False = exclude reference/value text
        w = _mm(bb.GetWidth()); h = _mm(bb.GetHeight())
        cx = _mm(bb.GetCenter().x); cy = _mm(bb.GetCenter().y)
        offx = cx - _mm(pos.x); offy = cy - _mm(pos.y)   # body center relative to origin
        # pad anchors, expressed relative to the BODY CENTER (so they track the body)
        pin_sum = defaultdict(lambda: [0.0, 0.0, 0])
        drills = npads = 0
        for pad in fp.Pads():
            npads += 1
            if pad.GetDrillSize().x > 0:
                drills += 1
            nn = pad.GetNetname()
            if not nn:
                continue
            pp = pad.GetPosition()
            s = pin_sum[nn]
            s[0] += _mm(pp.x) - cx; s[1] += _mm(pp.y) - cy; s[2] += 1
        pins = {n: (s[0] / s[2], s[1] / s[2]) for n, s in pin_sum.items()}
        try:
            sheet = fp.GetSheetname() or ""
        except Exception:
            sheet = ""
        parts[ref] = dict(
            value=fp.GetValue(),
            footprint=str(fp.GetFPID().GetLibItemName()),
            w=max(0.5, _mm(bb.GetWidth())),
            h=max(0.5, _mm(bb.GetHeight())),
            x=cx, y=cy,                 # BODY CENTER (not the footprint origin)
            off=(offx, offy),           # body-center offset from origin, for write-back
            locked=fp.IsLocked(),
            angle0=fp.GetOrientationDegrees(),   # angle the pin offsets were read at
            npads=npads,
            # a couple of mounting holes don't wall off a 200-pin module: THT means
            # a real drilled pin field (most pads drilled, or 5+ drills)
            tht=(drills >= 5 or (npads and drills / npads > 0.5)),
            sheet=sheet.strip("/") or "root",
            pins=pins,          # {net: (dx_mm, dy_mm)} pin anchor offsets from body center
        )
        for nn in pins:
            net_refs[nn].add(ref)
    nets = {n: sorted(rs) for n, rs in net_refs.items()}
    return parts, nets


def load(path):
    return pcbnew.LoadBoard(path)


def apply_positions(board, positions, parts=None, skip_locked=True):
    """Move footprints so their BODY CENTER lands at positions {ref:(x_mm,y_mm)}. The
    footprint origin is often offset from the body center (connectors), so set
    origin = center - offset, computing the offset live from the current bbox."""
    n = 0
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref not in positions:
            continue
        if skip_locked and fp.IsLocked():
            continue
        cur = fp.GetPosition()
        bb = fp.GetBoundingBox(False, False)
        offx = _mm(bb.GetCenter().x) - _mm(cur.x)
        offy = _mm(bb.GetCenter().y) - _mm(cur.y)
        x, y = positions[ref]
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(float(x) - offx),
                                       pcbnew.FromMM(float(y) - offy)))
        n += 1
    return n


def apply_orientations(board, angles, skip_locked=True):
    """Rotate footprints to angles {ref: degrees}. Returns count rotated."""
    n = 0
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref not in angles:
            continue
        if skip_locked and fp.IsLocked():
            continue
        fp.SetOrientationDegrees(float(angles[ref]))
        n += 1
    return n


def save(board, path=None):
    pcbnew.SaveBoard(path or board.GetFileName(), board)


def board_center(board):
    bb = board.GetBoundingBox()
    return _mm(bb.GetCenter().x), _mm(bb.GetCenter().y)


def shrinkwrap_outline(board, x0_mm, y0_mm, x1_mm, y1_mm, margin=2.0):
    """Delete existing Edge.Cuts and draw a tight rectangle around the given placement
    bounds (mm). Caller supplies bounds from the placement — this is what makes the
    board actually compact (the outline follows the parts)."""
    for d in list(board.GetDrawings()):
        if d.GetLayer() == pcbnew.Edge_Cuts:
            board.Remove(d)
    m = margin
    x0 = pcbnew.FromMM(x0_mm - m); y0 = pcbnew.FromMM(y0_mm - m)
    x1 = pcbnew.FromMM(x1_mm + m); y1 = pcbnew.FromMM(y1_mm + m)
    for (ax, ay, bx, by) in ((x0, y0, x1, y0), (x1, y0, x1, y1),
                             (x1, y1, x0, y1), (x0, y1, x0, y0)):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(pcbnew.VECTOR2I(ax, ay))
        seg.SetEnd(pcbnew.VECTOR2I(bx, by))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(pcbnew.FromMM(0.15))
        board.Add(seg)
    return (x1_mm - x0_mm + 2 * m, y1_mm - y0_mm + 2 * m)


def emit_route_guides(board, rep, layer_name="Eco1.User"):
    """Draw the global router's corridors as grouped polylines on a user layer, so
    interactive routing can follow the reserved plan ("the traces are already in
    place"). Idempotent: re-emitting replaces the previous fluxplace-guides group."""
    # drop any previous guides group (and its members)
    for g in list(board.Groups()):
        if g.GetName() == "fluxplace-guides":
            for item in list(g.GetItems()):
                board.Remove(item)
            board.Remove(g)
    layer = board.GetLayerID(layer_name)
    grp = pcbnew.PCB_GROUP(board)
    grp.SetName("fluxplace-guides")
    board.Add(grp)
    grid = rep["grid"]
    n = 0
    for net, paths in sorted(rep["routed"].items()):
        for path in paths:
            if len(path) < 2:
                continue
            # merge colinear runs so a straight corridor is ONE segment
            pts = [grid.cell_center(c) for c in path]
            keep = [pts[0]]
            for i in range(1, len(pts) - 1):
                (x0, y0), (x1, y1), (x2, y2) = keep[-1], pts[i], pts[i + 1]
                if (x1 - x0) * (y2 - y1) != (y1 - y0) * (x2 - x1):
                    keep.append(pts[i])
            keep.append(pts[-1])
            for a, b in zip(keep, keep[1:]):
                seg = pcbnew.PCB_SHAPE(board)
                seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
                seg.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
                seg.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
                seg.SetLayer(layer)
                seg.SetWidth(pcbnew.FromMM(0.15))
                board.Add(seg)
                grp.AddItem(seg)
                n += 1
    return n


def export_dsn(board, path):
    """Specctra DSN export for external-router calibration (freerouting)."""
    return bool(pcbnew.ExportSpecctraDSN(board, path))
