"""The only module that imports pcbnew. Reads parts+nets from a board and writes
positions back. Works both in the KiCad GUI (pass the live board) and headless
(load a .kicad_pcb path)."""
import pcbnew


def _mm(nm):
    return nm / 1e6


def _escape_halo(padxy, cx, cy, pitch, npads):
    """Per-axis fanout room (ex, ey) for a fine-pitch part: a pad on a left/right edge
    (|dx|>=|dy|) escapes in X, a top/bottom pad in Y. Room scales with pins-per-side on
    that axis, capped. (0,0) for coarse parts."""
    if pitch > 0.55 or npads < 8:
        return (0.0, 0.0)
    nx = ny = 0
    for (px, py) in padxy:
        if abs(px - cx) >= abs(py - cy):
            nx += 1
        else:
            ny += 1
    return (min(1.6, 0.3 + 0.03 * nx), min(1.6, 0.3 + 0.03 * ny))


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
        padxy = []
        for pad in fp.Pads():
            npads += 1
            if pad.GetDrillSize().x > 0:
                drills += 1
            pp = pad.GetPosition()
            padxy.append((_mm(pp.x), _mm(pp.y)))
            nn = pad.GetNetname()
            if not nn:
                continue
            s = pin_sum[nn]
            s[0] += _mm(pp.x) - cx; s[1] += _mm(pp.y) - cy; s[2] += 1
        pins = {n: (s[0] / s[2], s[1] / s[2]) for n, s in pin_sum.items()}
        # pin PITCH = smallest centre-to-centre between pads: the escape-difficulty
        # signal. A fine-pitch part (<=0.5 mm) needs its fanout corridor kept clear or
        # a wirelength-greedy placer crowds it and the escape can't route at bulk width.
        pitch = 999.0
        if len(padxy) >= 2:
            for i in range(len(padxy)):
                ax, ay = padxy[i]
                for j in range(i + 1, len(padxy)):
                    d = abs(ax - padxy[j][0]) + abs(ay - padxy[j][1])
                    if 0.01 < d < pitch:
                        pitch = d
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
            # which side the SMD body sits on: opposite-side parts share the 2D
            # footprint but never physically collide, so the placer must not spread
            # them apart nor double-count their area (a double-sided board fits ~2x
            # the parts of a single-sided one of the same outline).
            side=("B" if fp.IsFlipped() else "F"),
            angle0=fp.GetOrientationDegrees(),   # angle the pin offsets were read at
            npads=npads,
            # a couple of mounting holes don't wall off a 200-pin module: THT means
            # a real drilled pin field (most pads drilled, or 5+ drills)
            tht=(drills >= 5 or (npads and drills / npads > 0.5)),
            sheet=sheet.strip("/") or "root",
            pitch=pitch,        # min pad centre spacing (mm); <=0.5 => fine-pitch escape
            # ANISOTROPIC escape halo (ex, ey): reserve fanout room on the axis the pins
            # actually escape — a pad on a left/right edge escapes in X, a top/bottom pad
            # in Y. So a rectangular TSSOP reserves room off its long (pin) sides, not its
            # ends; an LQFP reserves ~equally. Added as spacing only (pad>0). Because the
            # builder's rotation audition is router-scored, an anisotropic halo also makes
            # it PREFER orientations that face the dense pin bank into open space — i.e.
            # orientation-awareness falls out for free. 0 for coarse parts.
            escape=_escape_halo(padxy, cx, cy, pitch, npads),
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


def signal_layers(board):
    """Auto-detect the copper layers available for SIGNAL routing = every enabled copper
    layer minus those that are a poured PLANE (a large zone, i.e. a GND/PWR pour). Outer
    layers (F/B) are always signal. So the user never has to know the stackup:
      4-layer GND/PWR-plane carrier -> [F.Cu, B.Cu]
      6-layer sig/gnd/sig/sig/gnd/sig -> [F.Cu, In2.Cu, In3.Cu, B.Cu]"""
    names = [board.GetLayerName(l) for l in board.GetEnabledLayers().CuStack()]
    bb = board.GetBoundingBox()
    barea = (bb.GetWidth() * bb.GetHeight()) or 1
    plane = set()
    for z in board.Zones():
        if hasattr(z, "GetIsRuleArea") and z.GetIsRuleArea():
            continue
        zb = z.GetBoundingBox()
        if zb.GetWidth() * zb.GetHeight() > 0.4 * barea:      # a board-spanning pour = plane
            for l in z.GetLayerSet().CuStack():
                nm = board.GetLayerName(l)
                if nm not in ("F.Cu", "B.Cu"):                # outer layers stay signal
                    plane.add(nm)
    sig = [n for n in names if n not in plane]
    return sig or ["F.Cu", "B.Cu"]


def default_rules(board):
    """(track_mm, clearance_mm) from the board's default netclass — so the conservative
    bulk rule comes from the board, not a guess. Falls back to 0.2/0.2."""
    try:
        nc = board.GetDesignSettings().GetDefault()
        return (_mm(nc.GetTrackWidth()) or 0.2, _mm(nc.GetClearance()) or 0.2)
    except Exception:
        return (0.2, 0.2)


def preflight(board):
    """Parse-level sanity findings — the class of issue a downstream consumer
    (fab house, assembly, or another EDA tool's parser) rejects a board for.
    Returns [(level, code, msg)], level FAIL or WARN. Checks: closed outline,
    every pad inside it, pads with no copper layer, and netted footprints
    excluded from position files (a pos-file-driven parser then sees pins
    belonging to a component that is 'not on the board' — measured: Quilter
    rejected the CM5 carrier over exactly that on the M.2 module stand-in)."""
    out = []
    edges = [d for d in board.GetDrawings() if d.GetLayer() == pcbnew.Edge_Cuts]
    bb = board.GetBoardEdgesBoundingBox()
    if not edges or bb.GetWidth() <= 0 or bb.GetHeight() <= 0:
        out.append(("FAIL", "OUTLINE_MISSING", "no closed Edge.Cuts outline"))
        return out
    x0, y0, x1, y1 = _mm(bb.GetLeft()), _mm(bb.GetTop()), _mm(bb.GetRight()), _mm(bb.GetBottom())
    exclude_pos = getattr(pcbnew, "FP_EXCLUDE_FROM_POS_FILES", 4)
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        off = ncu = netted = 0
        for pad in fp.Pads():
            p = pad.GetPosition()
            if not (x0 <= _mm(p.x) <= x1 and y0 <= _mm(p.y) <= y1):
                off += 1
            has_cu = any(board.GetLayerName(l).endswith(".Cu")
                         for l in pad.GetLayerSet().Seq())
            if pad.GetNetname():
                netted += 1
                if not has_cu:      # unnetted copper-less pads are paste apertures
                    ncu += 1
        if off:
            out.append(("FAIL", "PAD_OFF_BOARD",
                        f"{ref}: {off} pad(s) outside the board outline"))
        if ncu:
            out.append(("WARN", "PAD_NO_COPPER",
                        f"{ref}: {ncu} NETTED pad(s) on no copper layer"))
        if netted and (fp.GetAttributes() & exclude_pos):
            out.append(("WARN", "POS_EXCLUDED_NETTED",
                        f"{ref}: {netted} netted pin(s) but excluded from position "
                        f"files — pos-driven parsers treat it as not-on-board"))
        # overlapping non-congruent pads inside one footprint read as a padstack
        # collision to strict checkers; exact stacked clones (same pos+size) are
        # the accepted multi-number idiom and stay silent
        plist = [(p.GetPosition().x, p.GetPosition().y,
                  p.GetSize().x, p.GetSize().y, p.GetNetname())
                 for p in fp.Pads()
                 if any(board.GetLayerName(l).endswith(".Cu")
                        for l in p.GetLayerSet().Seq())]   # paste apertures exempt
        clash = 0
        for i in range(len(plist)):
            for j in range(i + 1, len(plist)):
                a2, b2 = plist[i], plist[j]
                if not a2[4] or not b2[4] or a2[4] == b2[4]:
                    # same-net stacks/stitching and netless mechanical pads are
                    # idioms; the short risk is two DIFFERENT nets overlapping
                    continue
                if (abs(a2[0] - b2[0]) < (a2[2] + b2[2]) / 2 and
                        abs(a2[1] - b2[1]) < (a2[3] + b2[3]) / 2):
                    clash += 1
        if clash:
            out.append(("WARN", "PAD_OVERLAP",
                        f"{ref}: {clash} overlapping non-congruent pad pair(s) — "
                        f"reads as a padstack collision to strict checkers"))
        ec = sum(1 for g in fp.GraphicalItems()
                 if g.GetLayer() == pcbnew.Edge_Cuts)
        if ec:
            out.append(("FAIL", "FP_EDGE_CUTS",
                        f"{ref}: {ec} Edge.Cuts segment(s) drawn by the footprint — "
                        f"a closed interior loop parses as a board CUTOUT, putting "
                        f"every pad inside it off-board (card-edge templates like "
                        f"Connector_PCBEdge do this; move them to Dwgs.User)"))
    return out


def pin_pad_parity(board, netlist_xml):
    """Cross-check the schematic netlist (kicadxml path) against the board: for each
    component, schematic pins that have NO pad on its footprint. A strict parser
    ('pins not on the board') or KiCad's own parity checker rejects these. Returns
    {ref: [missing_pin, ...]}."""
    import xml.etree.ElementTree as ET
    comp_pins = {}
    for net in ET.parse(netlist_xml).getroot().iter('net'):
        for node in net.iter('node'):
            comp_pins.setdefault(node.get('ref'), set()).add(node.get('pin'))
    fps = {f.GetReference(): {p.GetPadName() for p in f.Pads()}
           for f in board.GetFootprints()}
    return {r: sorted(p - fps[r]) for r, p in comp_pins.items()
            if r in fps and p - fps[r]}


def parts_extent(parts, pos, angles, eff_size):
    """(x0, y0, x1, y1) of the placed part bodies (mm) — the containment rect the
    outline must cover for every pad to be on-board."""
    xs0 = [pos[r][0] - eff_size(parts, r, angles.get(r, 0.0), 0.0)[0] / 2 for r in pos]
    ys0 = [pos[r][1] - eff_size(parts, r, angles.get(r, 0.0), 0.0)[1] / 2 for r in pos]
    xs1 = [pos[r][0] + eff_size(parts, r, angles.get(r, 0.0), 0.0)[0] / 2 for r in pos]
    ys1 = [pos[r][1] + eff_size(parts, r, angles.get(r, 0.0), 0.0)[1] / 2 for r in pos]
    return min(xs0), min(ys0), max(xs1), max(ys1)


def shrinkwrap_outline(board, x0_mm, y0_mm, x1_mm, y1_mm, margin=2.0):
    """Delete existing Edge.Cuts and draw a tight rectangle around the given placement
    bounds (mm). Caller supplies bounds from the placement — this is what makes the
    board actually compact (the outline follows the parts)."""
    for d in list(board.GetDrawings()):
        if d.GetLayer() == pcbnew.Edge_Cuts:
            board.Delete(d)   # Delete, not Remove: Remove leaves a dangling owner
                              # that corrupts SWIG proxies for later board calls
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
                board.Delete(item)
            board.Delete(g)
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
