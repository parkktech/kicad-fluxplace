"""The only module that imports pcbnew. Reads parts+nets from a board and writes
positions back. Works both in the KiCad GUI (pass the live board) and headless
(load a .kicad_pcb path)."""
import os

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
            drills=drills,      # raw drilled-pad count (any drill penetrates both sides)
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


def repair_pad_overlaps(board, clearance_mm=0.1, max_shrink=0.45):
    """Make stand-in footprints ROUTABLE: different-net pads that interpenetrate
    are illegal copper no router can legally land on (measured: every router —
    KRT and three Quilter jobs — stalls at exactly the parts preflight flags for
    PAD_OVERLAP). Shrink each offending pad toward its own centre (pin position
    unchanged) until the pair clears by clearance_mm, never below
    (1 - max_shrink) of the original size. Returns [(ref, padA, padB)] repaired
    and [(ref, padA, padB)] still overlapping (need a real footprint)."""
    fixed, stuck = [], []

    def gap(a, b):
        ab, bb = a.GetBoundingBox(), b.GetBoundingBox()
        ox = (min(ab.GetRight(), bb.GetRight()) - max(ab.GetLeft(), bb.GetLeft()))
        oy = (min(ab.GetBottom(), bb.GetBottom()) - max(ab.GetTop(), bb.GetTop()))
        return max(-ox, -oy) / 1e6          # >0 = separated on an axis by that many mm

    for fp in board.GetFootprints():
        pads = [p for p in fp.Pads() if p.GetNetname()
                and any(board.GetLayerName(l).endswith(".Cu")
                        for l in p.GetLayerSet().Seq())]
        floor = {p.GetPadName(): (int(p.GetSize().x * (1 - max_shrink)),
                                  int(p.GetSize().y * (1 - max_shrink)))
                 for p in pads}
        for i in range(len(pads)):
            for j in range(i + 1, len(pads)):
                a, b = pads[i], pads[j]
                if a.GetNetname() == b.GetNetname() or gap(a, b) >= clearance_mm:
                    continue
                changed = False
                for _ in range(14):
                    if gap(a, b) >= clearance_mm:
                        break
                    stepped = False
                    for p in (a, b):
                        s, fl = p.GetSize(), floor[p.GetPadName()]
                        ns = (max(fl[0], int(s.x * 0.92)),
                              max(fl[1], int(s.y * 0.92)))
                        if ns != (s.x, s.y):
                            p.SetSize(pcbnew.VECTOR2I(*ns))
                            stepped = changed = True
                    if not stepped:
                        break
                rec = (fp.GetReference(), a.GetPadName(), b.GetPadName())
                if gap(a, b) >= clearance_mm:
                    if changed:
                        fixed.append(rec)
                else:
                    stuck.append(rec)
    return fixed, stuck


def add_return_vias(path, pair_nets, max_mm=8.0, via_mm=0.6, drill_mm=0.3):
    """Stitch a GND return via near every pair-net via that lacks one within
    max_mm (the return current must change reference planes where the signal
    does). Candidate ring spots are kept clear of non-GND copper; caller guards
    with DRC before/after. Returns number of vias added."""
    import math
    b = pcbnew.LoadBoard(path)
    gnd = b.FindNet("GND")
    if gnd is None:
        return 0
    tracks = [(t, t.GetPosition()) for t in b.GetTracks()]
    pv, gv = [], []
    for t in b.GetTracks():
        if t.GetClass() != "PCB_VIA":
            continue
        if t.GetNetname() in pair_nets:
            pv.append(t.GetPosition())
        elif t.GetNetname() == "GND":
            gv.append(t.GetPosition())

    def clear_at(x, y):
        for t, p in tracks:
            if t.GetNetname() == "GND":
                continue
            if t.GetClass() == "PCB_VIA":
                if abs(p.x - x) < 1.2e6 and abs(p.y - y) < 1.2e6:
                    return False
            else:
                # coarse segment test: near either end or the midpoint
                e = t.GetEnd()
                for qx, qy in ((p.x, p.y), (e.x, e.y),
                               ((p.x + e.x) // 2, (p.y + e.y) // 2)):
                    if abs(qx - x) < 0.8e6 and abs(qy - y) < 0.8e6:
                        return False
        for fp in b.GetFootprints():
            fb = fp.GetBoundingBox(False, False)
            if (fb.GetLeft() - 0.5e6 < x < fb.GetRight() + 0.5e6 and
                    fb.GetTop() - 0.5e6 < y < fb.GetBottom() + 0.5e6):
                return False
        return True

    added = 0
    for p in pv:
        if any(((p.x - g.x) ** 2 + (p.y - g.y) ** 2) ** 0.5 < max_mm * 1e6
               for g in gv):
            continue
        placed = False
        for r in (1.0, 1.4, 1.9, 2.5):
            for k in range(8):
                a = math.tau * k / 8
                x = int(p.x + r * 1e6 * math.cos(a))
                y = int(p.y + r * 1e6 * math.sin(a))
                if clear_at(x, y):
                    v = pcbnew.PCB_VIA(b)
                    v.SetViaType(pcbnew.VIATYPE_THROUGH)
                    v.SetPosition(pcbnew.VECTOR2I(x, y))
                    v.SetDrill(pcbnew.FromMM(drill_mm))
                    v.SetWidth(pcbnew.FromMM(via_mm))
                    v.SetNet(gnd)
                    b.Add(v)
                    gv.append(pcbnew.VECTOR2I(x, y))
                    added += 1
                    placed = True
                    break
            if placed:
                break
    if added:
        pcbnew.SaveBoard(path, b)
    return added


def lock_net_copper(path, netnames):
    """Lock every track/via on the given nets (in-place file edit). KRT's bulk
    router documents 'KiCad-locked copper is never ripped' — this is how the
    pairs-first coupled routes survive the route-fresh rip-everything pass
    (measured without it: PCIE_TX re-routed single-ended with 22mm skew)."""
    b = pcbnew.LoadBoard(path)
    n = 0
    names = set(netnames)
    for t in b.GetTracks():
        if t.GetNetname() in names:
            t.SetLocked(True)
            n += 1
    pcbnew.SaveBoard(path, b)
    return n


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
        plist = []
        for p in fp.Pads():
            if not any(board.GetLayerName(l).endswith(".Cu")
                       for l in p.GetLayerSet().Seq()):
                continue                                   # paste apertures exempt
            pb = p.GetBoundingBox()                        # orientation-aware
            plist.append((pb.GetLeft(), pb.GetTop(), pb.GetRight(),
                          pb.GetBottom(), p.GetNetname()))
        clash = 0
        eps = int(0.02 * 1e6)          # >20um interpenetration = a real overlap
        for i in range(len(plist)):
            for j in range(i + 1, len(plist)):
                a2, b2 = plist[i], plist[j]
                if not a2[4] or not b2[4] or a2[4] == b2[4]:
                    # same-net stacks/stitching and netless mechanical pads are
                    # idioms; the short risk is two DIFFERENT nets overlapping
                    continue
                if (min(a2[2], b2[2]) - max(a2[0], b2[0]) > eps and
                        min(a2[3], b2[3]) - max(a2[1], b2[1]) > eps):
                    clash += 1
        if clash:
            out.append(("WARN", "PAD_OVERLAP",
                        f"{ref}: {clash} overlapping non-congruent pad pair(s) — "
                        f"reads as a padstack collision to strict checkers"))
        fpid = fp.GetFPID().GetUniStringLibId().upper()
        if any(t in fpid for t in _STANDIN_TOKENS):
            out.append(("WARN", "FP_STANDIN_NAME",
                        f"{ref}: footprint name marks it a stand-in — every "
                        f"router measured stalls on stand-in pin geometry; "
                        f"replace before layout (see replace-footprint)"))
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


def netlist_pin_nets(netlist_xml, ref):
    """{pin: netname} for one component from a kicadxml netlist — the schematic
    truth for pad-net assignment. Used by replace_footprint so pads the old
    (stand-in) footprint never had — e.g. power pins — get their nets back
    instead of inheriting the stand-in's holes (measured on the CM5 M.2 socket:
    the card-edge stand-in dropped every 3.3V pad, unpowering the module)."""
    import xml.etree.ElementTree as ET
    out = {}
    for net in ET.parse(netlist_xml).getroot().iter('net'):
        name = net.get('name')
        for node in net.iter('node'):
            if node.get('ref') == ref:
                out[node.get('pin')] = name
    return out


def replace_footprint(board, ref, pretty_dir, fp_name, net_by_pin=None,
                      renames=None):
    """Swap ref's footprint for a real library footprint in place: keep
    position, rotation, side, reference/value and the schematic link (KIID
    path), re-assign pad nets by pad number. net_by_pin ({pin: net}, e.g. from
    netlist_pin_nets) is the preferred truth and overrides nets inherited from
    the old pads; renames ({old_pad_num: new_pad_num}) maps vendor pad names
    onto schematic pin numbers before net lookup. Nets missing from the board
    are created. Returns a report dict; raises KeyError if ref or the library
    footprint is not found."""
    old = next((f for f in board.GetFootprints() if f.GetReference() == ref),
               None)
    if old is None:
        raise KeyError(f"no footprint with reference {ref}")
    new = pcbnew.FootprintLoad(pretty_dir, fp_name)
    if new is None:
        raise KeyError(f"footprint {fp_name!r} not found in {pretty_dir}")
    for pad in new.Pads():
        n = pad.GetPadName()
        if renames and n in renames:
            pad.SetPadName(renames[n])
    pin_net = {p.GetPadName(): p.GetNetname()
               for p in old.Pads() if p.GetNetname()}
    if net_by_pin:
        pin_net.update(net_by_pin)
    if old.IsFlipped():
        new.Flip(new.GetPosition(), False)
    new.SetOrientation(old.GetOrientation())
    new.SetPosition(old.GetPosition())
    new.SetReference(ref)
    new.SetValue(old.GetValue())
    new.SetPath(old.GetPath())          # keep the schematic <-> board link
    created, unnetted, assigned = [], [], 0
    for pad in new.Pads():
        net = pin_net.get(pad.GetPadName())
        if not net:
            if pad.GetPadName():
                unnetted.append(pad.GetPadName())
            continue
        ni = board.FindNet(net)
        if ni is None:
            ni = pcbnew.NETINFO_ITEM(board, net)
            board.Add(ni)
            created.append(net)
        pad.SetNet(ni)
        assigned += 1
    new_nums = {p.GetPadName() for p in new.Pads()}
    board.Remove(old)
    board.Add(new)
    return {"assigned": assigned,
            "unnetted_pads": sorted(set(unnetted), key=lambda s: (len(s), s)),
            "pins_without_pads": sorted(set(pin_net) - new_nums,
                                        key=lambda s: (len(s), s)),
            "created_nets": created}


def pad_net_parity(board, netlist_xml):
    """Board pads whose net disagrees with the schematic netlist:
    [(ref, pin, board_net, sch_net)]. Pads absent from the netlist are not
    reported; per-instance 'unconnected-*' names count as agreeing. Measured
    need: the CM5 carrier reached routing with its ENTIRE +3V3 rail absent
    from the PCB netlist — every router routed 'everything' and DRC passed,
    all against an incomplete net set."""
    import xml.etree.ElementTree as ET
    truth = {}
    for net in ET.parse(netlist_xml).getroot().iter('net'):
        name = net.get('name')
        for node in net.iter('node'):
            truth[(node.get('ref'), node.get('pin'))] = name
    diffs = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            t = truth.get((ref, pad.GetPadName()))
            cur = pad.GetNetname()
            if t is None or cur == t:
                continue
            if (t.startswith("unconnected-") and
                    cur.startswith("unconnected-")):
                continue                    # same meaning, per-instance names
            diffs.append((ref, pad.GetPadName(), cur, t))
    return diffs


def sync_pad_nets(board, netlist_xml):
    """Apply pad_net_parity: make the board's pad nets agree with the
    schematic netlist — the headless equivalent of 'Update PCB from
    Schematic' for nets only (no footprint changes). The netlist is truth;
    missing nets are created on the board. Walks every pad (duplicate pad
    numbers — GND fingers, stacked standoff names — all get set). Returns a
    report dict."""
    import xml.etree.ElementTree as ET
    truth = {}
    for net in ET.parse(netlist_xml).getroot().iter('net'):
        name = net.get('name')
        for node in net.iter('node'):
            truth[(node.get('ref'), node.get('pin'))] = name
    created, refs = [], {}
    assigned = 0
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            t = truth.get((ref, pad.GetPadName()))
            cur = pad.GetNetname()
            if t is None or cur == t:
                continue
            if (t.startswith("unconnected-") and
                    cur.startswith("unconnected-")):
                continue
            ni = board.FindNet(t)
            if ni is None:
                ni = pcbnew.NETINFO_ITEM(board, t)
                board.Add(ni)
                created.append(t)
            pad.SetNet(ni)
            assigned += 1
            refs[ref] = refs.get(ref, 0) + 1
    return {"assigned": assigned, "created_nets": created, "refs": refs}


_STANDIN_TOKENS = ("PROV", "STANDIN", "STAND-IN", "PLACEHOLDER")


def component_audit(board, netlist_xml=None):
    """Per-footprint order-readiness review — run BEFORE layout and BEFORE
    ordering parts. Flags the failure classes measured to sink whole layouts
    downstream: stand-in footprints (name tokens), schematic pins with no pad
    (with a netlist), missing courtyards, and missing or stand-in 3D models
    (tiny .wrl boxes). Returns [(level, ref, fpid, issue)]."""
    import os
    parity = pin_pad_parity(board, netlist_xml) if netlist_xml else {}
    out = []
    for fp in sorted(board.GetFootprints(), key=lambda f: f.GetReference()):
        ref = fp.GetReference()
        fpid = fp.GetFPID().GetUniStringLibId()
        up = fpid.upper()
        if any(t in up for t in _STANDIN_TOKENS):
            out.append(("FAIL", ref, fpid,
                        "stand-in footprint by name — replace with the real "
                        "vendor land pattern before layout or ordering"))
        if ref in parity:
            out.append(("FAIL", ref, fpid,
                        f"schematic pin(s) {parity[ref]} have no pad — symbol "
                        f"and footprint disagree about what the part is"))
        if not fp.Pads().size():
            continue
        crtyd = {pcbnew.F_CrtYd, pcbnew.B_CrtYd}
        if not any(g.GetLayer() in crtyd for g in fp.GraphicalItems()):
            out.append(("WARN", ref, fpid,
                        "no courtyard — placement cannot reserve the body"))
        models = list(fp.Models())
        if not models:
            out.append(("WARN", ref, fpid,
                        "no 3D model — mechanical fit unreviewed"))
        for m in models:
            p = str(m.m_Filename)
            full = _resolve_model_path(p, board)
            if full is None:
                out.append(("WARN", ref, fpid,
                            f"3D model {os.path.basename(p)} DOES NOT RESOLVE "
                            f"— renders as a missing body"))
            elif (p.lower().endswith(".wrl")
                  and os.path.getsize(full) < 2048):
                out.append(("WARN", ref, fpid,
                            f"3D model {os.path.basename(p)} is a tiny "
                            f".wrl stand-in box"))
    return out


def _resolve_model_path(path, board):
    """Expand a footprint 3D-model path the way KiCad does (KIPRJMOD + the
    versioned stock-model env vars / default install dirs). Returns the
    existing absolute path or None — None is what the 3D viewer shows as a
    silently missing body."""
    import glob as _glob
    prj = os.path.dirname(board.GetFileName())
    p = path.replace("${KIPRJMOD}", prj).replace("$(KIPRJMOD)", prj)
    if "${" in p or "$(" in p:
        stock = (os.environ.get("KICAD9_3DMODEL_DIR")
                 or os.environ.get("KICAD8_3DMODEL_DIR"))
        cands = [stock] if stock else sorted(
            _glob.glob("/usr/share/kicad*/3dmodels"), reverse=True)
        tail = p.split("}", 1)[-1].split(")", 1)[-1].lstrip("/")
        for c in cands:
            if c and os.path.exists(os.path.join(c, tail)):
                return os.path.join(c, tail)
        return None
    p = os.path.expandvars(p)
    if not os.path.isabs(p):
        p = os.path.join(prj, p)
    return p if os.path.exists(p) else None


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


def read_rule_areas(board):
    """Rule Areas -> (regions, keepouts), the Quilter KiCad convention
    (docs/QUILTER-DOCS-DIGEST.md §3): a NAMED rule area with every keepout
    item checkbox UNCHECKED is a placement REGION; one with keepout items
    checked is a KEEPOUT/obstacle. Both are returned with bbox in mm and the
    copper side they're linked to (F/B; areas on both coppers -> side "FB").

    regions:  [{name, side, bbox:(x0,y0,x1,y1)}]
    keepouts: [{x, y, w, h, side}]  (compact's obstacle rect format)
    """
    regions, keepouts = [], []
    fcu = board.GetLayerID("F.Cu")
    bcu = board.GetLayerID("B.Cu")
    for z in board.Zones():
        try:
            if not z.GetIsRuleArea():
                continue
        except Exception:
            continue
        ls = z.GetLayerSet()
        on_f = ls.Contains(fcu)
        on_b = ls.Contains(bcu)
        if not (on_f or on_b):
            continue
        side = "FB" if (on_f and on_b) else ("F" if on_f else "B")
        bb = z.Outline().BBox()
        bbox = (_mm(bb.GetLeft()), _mm(bb.GetTop()),
                _mm(bb.GetRight()), _mm(bb.GetBottom()))
        flags = []
        for getter in ("GetDoNotAllowTracks", "GetDoNotAllowVias",
                       "GetDoNotAllowPads", "GetDoNotAllowFootprints",
                       "GetDoNotAllowCopperPour", "GetDoNotAllowZoneFills"):
            try:
                flags.append(bool(getattr(z, getter)()))
            except Exception:
                pass
        name = ""
        try:
            name = z.GetZoneName() or ""
        except Exception:
            pass
        if any(flags):
            keepouts.append(dict(x=(bbox[0] + bbox[2]) / 2,
                                 y=(bbox[1] + bbox[3]) / 2,
                                 w=bbox[2] - bbox[0], h=bbox[3] - bbox[1],
                                 side=("F" if side != "B" else "B")))
        elif name:
            regions.append(dict(name=name, side=side, bbox=bbox))
    return regions, keepouts


def quilter_contract(board, parts):
    """Apply Quilter's I/O contract to the parts model: any part whose body
    center is INSIDE the board outline is locked (pre-placed), anything
    OUTSIDE is free to place. Lets one prepared board feed either engine and
    enables the 'save your progress' loop: keep what you like inside, push
    the rest out, rerun. Returns (n_locked, n_free)."""
    bb = board.GetBoardEdgesBoundingBox()
    x0, y0 = _mm(bb.GetLeft()), _mm(bb.GetTop())
    x1, y1 = _mm(bb.GetRight()), _mm(bb.GetBottom())
    n_locked = n_free = 0
    for ref, p in parts.items():
        inside = x0 <= p["x"] <= x1 and y0 <= p["y"] <= y1
        p["locked"] = inside
        n_locked += inside
        n_free += not inside
    return n_locked, n_free


def plane_intent(board, power_pick=None):
    """Layer-name plane semantics (Quilter's convention): copper layers named
    *gnd*/*ground* pour the ground net; *pwr*/*power* pour `power_pick` (or
    the highest-fanout power-family net). Returns [(layer_name, net_name)]."""
    import re as _re
    from .lint import GND_RE, POWER_RE, _basename
    gnd_net = None
    fanout = {}
    for n in board.GetNetsByName():
        name = str(n)
        if not name:
            continue
        base = _basename(name)
        if GND_RE.match(base) and gnd_net is None:
            gnd_net = name
        if POWER_RE.match(base):
            fanout[name] = fanout.get(name, 0)
    for pad_net in [p.GetNetname() for fp in board.GetFootprints()
                    for p in fp.Pads()]:
        if pad_net in fanout:
            fanout[pad_net] += 1
    pwr_net = power_pick or (max(sorted(fanout), key=lambda k: fanout[k])
                             if fanout else None)
    out = []
    for lid in board.GetEnabledLayers().CuStack():
        lname = board.GetLayerName(lid)
        low = lname.lower()
        if _re.search(r"gnd|ground", low) and gnd_net:
            out.append((lname, gnd_net))
        elif _re.search(r"pwr|power", low) and pwr_net:
            out.append((lname, pwr_net))
    return out


def flip_footprints(board, refs):
    """Flip the given footprints to the other side, in place (KiCad-standard
    left-right flip about each footprint's own position). Returns count."""
    n = 0
    for fp in board.GetFootprints():
        if fp.GetReference() in refs and not fp.IsLocked():
            fp.Flip(fp.GetPosition(), False)
            n += 1
    return n
