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
        bb = fp.GetBoundingBox(False, False)   # exclude text
        pos = fp.GetPosition()
        # pad offsets relative to footprint origin (mm), averaged per net -> pin anchors
        pin_sum = defaultdict(lambda: [0.0, 0.0, 0])
        for pad in fp.Pads():
            nn = pad.GetNetname()
            if not nn:
                continue
            pp = pad.GetPosition()
            s = pin_sum[nn]
            s[0] += _mm(pp.x - pos.x); s[1] += _mm(pp.y - pos.y); s[2] += 1
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
            x=_mm(pos.x), y=_mm(pos.y),
            locked=fp.IsLocked(),
            sheet=sheet.strip("/") or "root",
            pins=pins,          # {net: (dx_mm, dy_mm)} pin anchor offsets from center
        )
        for nn in pins:
            net_refs[nn].add(ref)
    nets = {n: sorted(rs) for n, rs in net_refs.items()}
    return parts, nets


def load(path):
    return pcbnew.LoadBoard(path)


def apply_positions(board, positions, skip_locked=True):
    """Move footprints to positions {ref:(x_mm,y_mm)}. Returns count moved."""
    n = 0
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref not in positions:
            continue
        if skip_locked and fp.IsLocked():
            continue
        x, y = positions[ref]
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(float(x)), pcbnew.FromMM(float(y))))
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
