"""Pour-first connections: a pad of a net that has a pour or plane is tied THROUGH that copper before the router runs.

tie stage (after fan-out, before pairs/route): every pad of a pour net is either already reached by its filled pour (on the
pad's own pour layer) or reached through same-net copper to a via that lands in filled copper of the net; the rest get a
short stub + via into the pour at a legal site (planes.via_at_points + hole_ok).  Every tie is verified against the FILLED
zone geometry after one refill; a tie that does not verify is undone.  Layers come from the board's zones, never named here.
The verified ties are written to pourtie.json; repair() re-checks them after routing and re-ties what routing cut off."""
import json
import math
import os

import pcbnew

from . import planes as PL

TOL = pcbnew.FromMM(0.02)


def _copper_lids(obj_layers):
    return [l for l in obj_layers if pcbnew.IsCopperLayer(l)]


def pour_map(board):
    """{net: [layer_id, ...]} for every net with a copper zone (rule areas and unnamed zones skipped)."""
    out = {}
    for z in board.Zones():
        if z.GetIsRuleArea() or not z.GetNetname():
            continue
        out.setdefault(z.GetNetname(), set()).update(_copper_lids(z.GetLayerSet().Seq()))
    return {n: sorted(v) for n, v in out.items()}


def fill_hit(board, net, lid, x, y):
    for z in board.Zones():
        if z.GetIsRuleArea() or z.GetNetname() != net or not z.IsOnLayer(lid):
            continue
        if z.HitTestFilledArea(lid, pcbnew.VECTOR2I(int(x), int(y)), 0):
            return True
    return False


def in_outline(board, net, x, y):
    """the point lies inside an outline of one of the net's copper zones (a pour cannot fill outside its outline)."""
    for z in board.Zones():
        if not z.GetIsRuleArea() and z.GetNetname() == net and z.Outline().Contains(pcbnew.VECTOR2I(int(x), int(y))):
            return True
    return False


def pad_copper_lids(pad):
    return _copper_lids(pad.GetLayerSet().Seq())


def _pad_xy(pad):
    p = pad.GetPosition()
    return p.x, p.y


def tie_status(board, net, pad, pours):
    """-> ("pour-layer", layer) | ("via", (x_mm, y_mm)) | (None, None): how the pad reaches the net's filled copper now."""
    x, y = _pad_xy(pad)
    for lid in pours:
        if pad.IsOnLayer(lid) and fill_hit(board, net, lid, x, y):
            return "pour-layer", board.GetLayerName(lid)
    # same-net copper graph from the pad centre: tracks join at endpoints, vias join every layer
    segs = [t for t in board.GetTracks() if t.GetNetname() == net]
    vias = [t for t in segs if t.Type() == pcbnew.PCB_VIA_T]
    tracks = [t for t in segs if t.Type() != pcbnew.PCB_VIA_T]
    # a stub starts on the pad's copper (its edge, not necessarily its centre): every same-net track or via end inside the
    # pad's box on the pad's layers, or the pad centre itself, is a start point
    box = pad.GetBoundingBox()
    box.Inflate(TOL, TOL)
    lids = set(pad_copper_lids(pad))
    starts = {(x, y)}
    for t in tracks:
        if t.GetLayer() in lids:
            for e in (t.GetStart(), t.GetEnd()):
                if box.Contains(e):
                    starts.add((e.x, e.y))
    for v in vias:
        if box.Contains(v.GetPosition()):
            starts.add((v.GetPosition().x, v.GetPosition().y))
    frontier = list(starts)
    seen = set(starts)
    checked = set()                    # vias already tested (a via is also a track end: `seen` must not hide it)
    for _hop in range(8):
        nxt = []
        for px, py in frontier:
            for v in vias:
                q = v.GetPosition()
                if abs(q.x - px) <= TOL and abs(q.y - py) <= TOL and (q.x, q.y) not in checked:
                    checked.add((q.x, q.y))
                    for lid in pours:
                        if fill_hit(board, net, lid, q.x, q.y):
                            return "via", (round(pcbnew.ToMM(q.x), 4), round(pcbnew.ToMM(q.y), 4))
                    nxt.append((q.x, q.y))
            for t in tracks:
                for e in (t.GetStart(), t.GetEnd()):
                    if abs(e.x - px) <= TOL and abs(e.y - py) <= TOL:
                        for f in (t.GetStart(), t.GetEnd()):
                            if (f.x, f.y) not in seen:
                                seen.add((f.x, f.y))
                                nxt.append((f.x, f.y))
        frontier = nxt
        if not frontier:
            break
    return None, None


def _ring_sites(x, y):
    for r_mm in (0.0, 0.8, 1.1, 1.4):
        if r_mm == 0.0:
            yield r_mm, x, y
            continue
        for k in range(12):
            a = k * math.pi / 6
            yield r_mm, x + pcbnew.FromMM(r_mm * math.cos(a)), y + pcbnew.FromMM(r_mm * math.sin(a))


def _via_keys(board):
    return {(t.GetPosition().x, t.GetPosition().y) for t in board.GetTracks() if t.Type() == pcbnew.PCB_VIA_T}


def _add_tie(board, net, pad, pours, made, rules=None):
    """stub (if the site is off the pad) + via at the first legal ring site; the created objects go into `made`."""
    x, y = _pad_xy(pad)
    nc = board.FindNet(net)
    layer = pad_copper_lids(pad)[0]
    rules = rules or {"via": 0.6, "drill": 0.3, "track": 0.15}
    for r_mm, cx, cy in _ring_sites(x, y):
        if not PL.hole_ok(board, cx, cy, via_drill=rules["drill"]):
            continue
        before = _via_keys(board)
        if PL.via_at_points(board, net, [(pcbnew.ToMM(cx), pcbnew.ToMM(cy))], via_d=rules["via"], via_drill=rules["drill"]) != 1:
            continue
        made.extend(t for t in board.GetTracks() if t.Type() == pcbnew.PCB_VIA_T and (t.GetPosition().x, t.GetPosition().y) not in before)
        if r_mm > 0.0:
            tr = pcbnew.PCB_TRACK(board)
            tr.SetStart(pcbnew.VECTOR2I(int(x), int(y)))
            tr.SetEnd(pcbnew.VECTOR2I(int(cx), int(cy)))
            tr.SetWidth(pcbnew.FromMM(rules["track"]))
            tr.SetLayer(layer)
            board.Add(tr)
            tr.SetNet(nc)                  # the net is set once the track is on the board (a pre-Add net was lost on save)
            made.append(tr)
        return True
    return False


def _undo(board, made):
    for o in made:
        if o is not None:
            board.Delete(o)  # Delete, not Remove: a Remove() of a discarded item breaks pcbnew.LoadBoard for the process


def _tie_pads(board, net, pads, pours, rules=None):
    """-> (tied: [record], untied: [record]); verified after one refill."""
    cands = []
    outside = []
    for pad in pads:
        how, where = tie_status(board, net, pad, pours)
        if how:
            continue
        x, y = _pad_xy(pad)
        if not in_outline(board, net, x, y):
            outside.append(pad)              # no via can land in a pour that does not reach the pad: the pour is the limit
            continue
        made = []
        cands.append((pad, made if _add_tie(board, net, pad, pours, made, rules) else None))
    PL.refill(board)
    tied, untied = [], []
    for pad in outside:
        x, y = _pad_xy(pad)
        untied.append({"net": net, "pad": pad.GetNumber(), "at": [round(pcbnew.ToMM(x), 4), round(pcbnew.ToMM(y), 4)],
                       "reason": "pad outside the net's pour outline (a larger pour or a track is needed)"})
    for pad, made in cands:
        x, y = _pad_xy(pad)
        if made is None:
            untied.append({"net": net, "pad": pad.GetNumber(), "at": [round(pcbnew.ToMM(x), 4), round(pcbnew.ToMM(y), 4)],
                           "reason": "no legal via site within 1.4 mm (hole, clearance or stub)"})
            continue
        via = [m for m in made if m.Type() == pcbnew.PCB_VIA_T]
        ok = bool(via) and any(fill_hit(board, net, lid, via[-1].GetPosition().x, via[-1].GetPosition().y) for lid in pours)
        if via:
            vx, vy = via[-1].GetPosition().x, via[-1].GetPosition().y
        if ok:
            tied.append({"net": net, "pad": pad.GetNumber(), "at": [round(pcbnew.ToMM(x), 4), round(pcbnew.ToMM(y), 4)],
                         "via": ([round(pcbnew.ToMM(vx), 4), round(pcbnew.ToMM(vy), 4)] if via else None)})
        else:
            _undo(board, made)
            untied.append({"net": net, "pad": pad.GetNumber(), "at": [round(pcbnew.ToMM(x), 4), round(pcbnew.ToMM(y), 4)],
                           "reason": "via does not land in the filled pour (verified on the filled zone)"})
    if tied or untied:
        PL.refill(board)
    return tied, untied


def _rect_pts(box):
    return [(box.GetLeft(), box.GetTop()), (box.GetRight(), box.GetTop()), (box.GetRight(), box.GetBottom()),
            (box.GetLeft(), box.GetBottom())]


def outline_report(board, net, pad, pour_lids):
    """how far the pad lies from the net's pour outline (mm), on the pour layers; the nearest zone."""
    x, y = _pad_xy(pad)
    best = None
    for z in board.Zones():
        if z.GetIsRuleArea() or z.GetNetname() != net or not any(z.IsOnLayer(l) for l in pour_lids):
            continue
        try:
            d = pcbnew.ToMM(int(z.Outline().Distance(pcbnew.VECTOR2I(int(x), int(y)))))
        except (TypeError, RuntimeError):
            continue
        if best is None or d < best[0]:
            best = (d, board.GetLayerName(z.GetLayer()))
    return best


def _grown_box(board, net, pad, pour_lids, max_mm):
    """-> (zone, old box, grown box, None) when the pad can be taken, else (None, None, None, reason). Nothing is changed."""
    x, y = _pad_xy(pad)
    zs = [z for z in board.Zones() if not z.GetIsRuleArea() and z.GetNetname() == net and any(z.IsOnLayer(l) for l in pour_lids)]
    if not zs:
        return None, None, None, "no pour of this net"
    zs = [q for q in zs if q.GetZoneName() != STRIP_NAME] or zs
    z = min(zs, key=lambda q: q.Outline().Distance(pcbnew.VECTOR2I(int(x), int(y))))
    d = pcbnew.ToMM(int(z.Outline().Distance(pcbnew.VECTOR2I(int(x), int(y)))))
    if d > max_mm:
        return None, None, None, f"{d:.2f} mm from the outline (extend_max_mm {max_mm:g})"
    if z.Outline().OutlineCount() != 1 or z.Outline().Outline(0).PointCount() != 4:
        return None, None, None, "outline is not a rectangle"
    old = z.GetBoundingBox()
    grown = pcbnew.BOX2I(old.GetPosition(), old.GetSize())
    grown.Merge(pad.GetBoundingBox())
    grown.Inflate(pcbnew.FromMM(0.3), pcbnew.FromMM(0.3))
    grown.Merge(old)
    clr = pcbnew.FromMM(0.2)
    added = lambda bx: bx.Intersects(grown) and not bx.Intersects(old)
    for t in board.GetTracks():
        if t.GetNetname() == net:
            continue
        if t.Type() != pcbnew.PCB_VIA_T and t.GetLayer() not in pour_lids:
            continue
        tb = t.GetBoundingBox()
        tb.Inflate(clr, clr)
        if added(tb):
            return None, None, None, f"other-net copper {t.GetNetname()} in the added area"
    for f in board.GetFootprints():
        for q in f.Pads():
            if q.GetNetname() == net or q.GetNetname() == "" or not any(l in pour_lids for l in pad_copper_lids(q)):
                continue
            qb = q.GetBoundingBox()
            qb.Inflate(clr, clr)
            if added(qb):
                return None, None, None, f"other-net pad {f.GetReference()}.{q.GetNumber()} in the added area"
    for k in board.Zones():
        if k is z or k.GetNetname() == net or not k.GetIsRuleArea():
            continue
        if added(k.GetBoundingBox()):
            return None, None, None, "keepout in the added area"
    return z, old, grown, None


TONGUE_MM = 1.0


def _blocked(board, net, box, pour_lids, z):
    """why the added `box` (BOX2I) cannot hold pour copper of `net` on `pour_lids`, else None: other-net track/via/pad, keepout or
    other-net zone outline there (0.2 mm clearance)."""
    clr = pcbnew.FromMM(0.2)
    for t in board.GetTracks():
        if t.GetNetname() == net or (t.Type() != pcbnew.PCB_VIA_T and t.GetLayer() not in pour_lids):
            continue
        tb = t.GetBoundingBox()
        tb.Inflate(clr, clr)
        if tb.Intersects(box):
            return f"other-net copper {t.GetNetname()} in the added area"
    for f in board.GetFootprints():
        for q in f.Pads():
            if q.GetNetname() in (net, "") or not any(l in pour_lids for l in pad_copper_lids(q)):
                continue
            qb = q.GetBoundingBox()
            qb.Inflate(clr, clr)
            if qb.Intersects(box):
                return f"other-net pad {f.GetReference()}.{q.GetNumber()} in the added area"
    for k in board.Zones():
        if k is z or k.GetNetname() == net or not any(k.IsOnLayer(l) for l in pour_lids):
            continue
        if k.GetBoundingBox().Intersects(box):
            return "keepout in the added area" if k.GetIsRuleArea() else f"other-net zone {k.GetNetname()} in the added area"
    return None


def _tongue(board, net, pad, pour_lids, max_mm):
    """K14: a pad too far from its pour for the bounding-box growth (or whose box holds other copper): a narrow Manhattan strip
    (TONGUE_MM wide, <= max_mm long) from the pour's rectangle to the pad, either horizontal-first or vertical-first, whichever holds
    no other-net copper, pad, keepout or zone. -> (zone, [BOX2I legs], None) | (None, None, reason). Nothing is changed."""
    x, y = _pad_xy(pad)
    zs = [z for z in board.Zones() if not z.GetIsRuleArea() and z.GetNetname() == net and any(z.IsOnLayer(l) for l in pour_lids)]
    if not zs:
        return None, None, "no pour of this net"
    zs = [q for q in zs if q.GetZoneName() != STRIP_NAME] or zs       # a strip starts at the pour itself, not at an earlier strip
    z = min(zs, key=lambda q: q.Outline().Distance(pcbnew.VECTOR2I(int(x), int(y))))
    if z.Outline().Outline(0).PointCount() != 4:
        return None, None, "outline is not a rectangle"
    bb, ins = z.Outline().Outline(0).BBox(), pcbnew.FromMM(0.5)      # the first contour: the pour's own rectangle, not later strips
    sx = min(max(x, bb.GetLeft() + ins), bb.GetRight() - ins)
    sy = min(max(y, bb.GetTop() + ins), bb.GetBottom() - ins)
    h = pcbnew.FromMM(TONGUE_MM) // 2
    why = "no clear strip"
    for mid in ((x, sy), (sx, y)):                             # horizontal-first, vertical-first
        pts = [(sx, sy), mid, (x, y)]
        length = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(pts, pts[1:]))
        if pcbnew.ToMM(int(length)) > max_mm:
            return None, None, f"{pcbnew.ToMM(int(length)):.2f} mm of strip (extend_max_mm {max_mm:g})"
        legs = []
        for a, b in zip(pts, pts[1:]):
            if a == b:
                continue
            box = pcbnew.BOX2I(pcbnew.VECTOR2I(int(min(a[0], b[0]) - h), int(min(a[1], b[1]) - h)),
                               pcbnew.VECTOR2I(int(abs(a[0] - b[0]) + 2 * h), int(abs(a[1] - b[1]) + 2 * h)))
            legs.append(box)
        bad = next((w for w in (_blocked(board, net, bx, pour_lids, z) for bx in legs) if w), None)
        if not bad:
            return z, legs, None
        why = bad
    legs = _tongue_search(board, net, (x, y), bb, ins, pour_lids, z, max_mm)
    if legs:
        return z, legs, None
    return None, None, why


def _blockers(board, net, pour_lids, z, win):
    """(l, t, r, b) boxes (0.2 mm clearance added) inside `win` that a strip of `net` on `pour_lids` may not touch: the items `_blocked` refuses."""
    clr, out = pcbnew.FromMM(0.2), []

    def add(bx, grow=clr):
        if bx.Intersects(win):
            out.append((bx.GetLeft() - grow, bx.GetTop() - grow, bx.GetRight() + grow, bx.GetBottom() + grow))
    for t in board.GetTracks():
        if t.GetNetname() != net and (t.Type() == pcbnew.PCB_VIA_T or t.GetLayer() in pour_lids):
            add(t.GetBoundingBox())
    for f in board.GetFootprints():
        for q in f.Pads():
            if q.GetNetname() not in (net, "") and any(l in pour_lids for l in pad_copper_lids(q)):
                add(q.GetBoundingBox())
    for k in board.Zones():
        if k is not z and k.GetNetname() != net and any(k.IsOnLayer(l) for l in pour_lids):
            add(k.GetBoundingBox(), 0)
    return out


def _tongue_search(board, net, at, bb, ins, pour_lids, z, max_mm, step_mm=0.25):
    """The strip as a searched path: breadth-first over a `step_mm` grid from the pad to the pour's rectangle, every cell a
    TONGUE_MM square free of `_blockers`, at most max_mm long; the cell path becomes one box per straight run. The two fixed
    L shapes of `_tongue` fail when one via, mounting-hole pad or inner track stands on both (W16: U5.7, U10.3, C27, C56, FB5 of
    +3V3), while a strip that steps around it exists. -> [BOX2I legs] | None."""
    from collections import deque
    x, y = at
    step, h = pcbnew.FromMM(step_mm), pcbnew.FromMM(TONGUE_MM) // 2
    reach = pcbnew.FromMM(max_mm)
    win = pcbnew.BOX2I(pcbnew.VECTOR2I(int(x - reach), int(y - reach)), pcbnew.VECTOR2I(int(2 * reach), int(2 * reach)))
    blk = _blockers(board, net, pour_lids, z, win)
    L, T, Rr, Bt = bb.GetLeft() + ins, bb.GetTop() + ins, bb.GetRight() - ins, bb.GetBottom() - ins
    e = board.GetBoardEdgesBoundingBox()
    eL, eT, eR, eB = e.GetLeft() + 2 * h, e.GetTop() + 2 * h, e.GetRight() - 2 * h, e.GetBottom() - 2 * h

    def free(i, j):
        cx, cy = x + i * step, y + j * step
        if not (eL <= cx <= eR and eT <= cy <= eB):
            return False
        return not any(l < cx + h and cx - h < r and t < cy + h and cy - h < b for l, t, r, b in blk)

    if not free(0, 0):
        return None
    n_max = int(max_mm / step_mm)
    par, q, goal = {(0, 0): None}, deque([((0, 0), 0, None)]), None
    while q:
        (i, j), n, dirn = q.popleft()
        cx, cy = x + i * step, y + j * step
        if L <= cx <= Rr and T <= cy <= Bt:
            goal = (i, j)
            break
        if n >= n_max:
            continue
        nb = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        if dirn in nb:                                     # straight on first: fewer runs
            nb.remove(dirn)
            nb.insert(0, dirn)
        for d in nb:
            c = (i + d[0], j + d[1])
            if c not in par and free(*c):
                par[c] = (i, j)
                q.append((c, n + 1, d))
    if goal is None:
        return None
    cells = [goal]
    while par[cells[-1]] is not None:
        cells.append(par[cells[-1]])
    corners = [cells[0]]
    for a, b, c in zip(cells, cells[1:], cells[2:]):
        if (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1]):
            corners.append(b)
    corners.append(cells[-1])
    legs = []
    for a, b in zip(corners, corners[1:]):
        x0, x1 = sorted((x + a[0] * step, x + b[0] * step))
        y0, y1 = sorted((y + a[1] * step, y + b[1] * step))
        legs.append(pcbnew.BOX2I(pcbnew.VECTOR2I(int(x0 - h), int(y0 - h)), pcbnew.VECTOR2I(int(x1 - x0 + 2 * h), int(y1 - y0 + 2 * h))))
    return legs


STRIP_NAME = "pourtie-strip"


def _strip_zone(board, z, boxes):
    """The strip as its own zone(s): a copy of `z` (net, layer, priority, clearances) whose outline is the union of `boxes`.
    Was: extra contours on z's outline. KiCad keeps one contour per zone on save, so the strips were in the saved FILL but not in
    the outline, and the first refill by a later stage or by DRC removed them: pourtie.json said tied, the board said open
    (W16 route-10: U313, Y500, C20, C315 "tied" by K14, all four still unconnected in the DRC of that board)."""
    ps = pcbnew.SHAPE_POLY_SET()
    for bx in boxes:
        q = pcbnew.SHAPE_POLY_SET()
        q.NewOutline()
        for px, py in _rect_pts(bx):
            q.Append(int(px), int(py))
        ps.BooleanAdd(q)
    made = []
    for i in range(ps.OutlineCount()):
        nz = z.Duplicate(False).Cast()
        o = nz.Outline()
        o.RemoveAllContours()
        o.NewOutline()
        c = ps.Outline(i)
        for k in range(c.PointCount()):
            o.Append(c.CPoint(k).x, c.CPoint(k).y)
        nz.SetZoneName(STRIP_NAME)
        board.Add(nz)
        made.append(nz)
    return made


LANDING_MM = 1.6          # half side of the pour square added round a strip's pad end: covers the tie-via ring (1.4 mm)


def _landing(board, net, pad, pour_lids, z):
    """A strip is TONGUE_MM wide, so only tie-via sites on its centre line landed in copper (W16 U5.7, U10.3, C27.1: strip laid, "no
    legal via site"). A square round the pad lets every ring site land; the zone fill keeps its own clearance to the foreign copper
    inside it, so only another zone or a keep-out on the pour layers refuses it. -> [BOX2I] | []."""
    x, y = _pad_xy(pad)
    h = pcbnew.FromMM(LANDING_MM)
    box = pcbnew.BOX2I(pcbnew.VECTOR2I(int(x - h), int(y - h)), pcbnew.VECTOR2I(int(2 * h), int(2 * h)))
    for k in board.Zones():
        if k is not z and k.GetNetname() != net and any(k.IsOnLayer(l) for l in pour_lids) and k.GetBoundingBox().Intersects(box):
            return []
    return [box]


def try_extend(board, net, pad, max_mm, pour_lids, commit=False):
    """grow a rectangular pour outline to take the pad when the added area holds no other-net copper, pad or keepout on
    the pour's layers; else (K14) add a narrow strip from the pour to the pad. -> (ok, reason). Changes the zone only when `commit`."""
    z, old, grown, why = _grown_box(board, net, pad, pour_lids, max_mm)
    if why:
        z, legs, why2 = _tongue(board, net, pad, pour_lids, max_mm)
        if why2:
            return False, f"{why}; strip: {why2}"
        if commit:
            _strip_zone(board, z, legs + _landing(board, net, pad, pour_lids, z))
        return True, None
    if commit:
        o = z.Outline()
        o.RemoveAllContours()
        o.NewOutline()
        for px, py in _rect_pts(grown):
            o.Append(px, py)
    return True, None


def run(board_in, board_out, workdir, nets=None, cons=None):
    """pour-first tie for every net with a copper zone -> record (also written to workdir/pourtie.json)."""
    from . import routerules as RR
    board = pcbnew.LoadBoard(board_in)
    R = RR.rules(cons, board_in)
    rules = {"track": R["track"], "clearance": RR.request_clearance(R), "via": R["via"], "drill": R["drill"]}
    rec = {"nets": {}, "tied": [], "untied": [], "satisfied": [], "rules": rules}
    if board is None:                            # not a KiCad board (a stub or an unreadable file): nothing to tie
        import shutil
        shutil.copy(board_in, board_out)
        rec["skipped"] = "board unreadable"
        with open(os.path.join(workdir, "pourtie.json"), "w") as fh:
            json.dump(rec, fh, indent=1)
        return rec
    pm = pour_map(board)
    if not pm:                                   # no pour on this board: nothing to tie (and no zone filler run)
        pcbnew.SaveBoard(board_out, board)
        with open(os.path.join(workdir, "pourtie.json"), "w") as fh:
            json.dump(rec, fh, indent=1)
        return rec
    PL.refill(board)
    pm = pour_map(board)
    pcfg = ((cons or {}).get("pours") or {})
    extend_on = bool(pcfg.get("extend_to_pads", True))
    ext_max = float(pcfg.get("extend_max_mm", 15.0))
    rec["outside"] = []
    for net in sorted(pm):
        if nets is not None and net not in nets:
            continue
        pads = [p for f in board.GetFootprints() for p in f.Pads() if p.GetNetname() == net and not p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH]
        if not pads:
            continue
        already_pads = [p for p in pads if tie_status(board, net, p, pm[net])[0]]
        todo = [p for p in pads if p not in already_pads]
        tied, untied = _tie_pads(board, net, todo, pm[net], rules) if todo else ([], [])
        rec["nets"][net] = {"pour_layers": [board.GetLayerName(l) for l in pm[net]], "pads": len(pads),
                            "tied_before": len(already_pads), "tied_by_via": len(tied), "untied": len(untied)}
        for p in already_pads:
            rec["satisfied"].append({"net": net, "ref": _ref(p), "pad": p.GetNumber(), "at": _mm_at(p)})
        for t in tied:
            rec["satisfied"].append(dict(t, ref=_ref(_pad_at(board, net, t["at"]))))
        for u in untied:
            u["ref"] = _ref(_pad_at(board, net, u["at"]))
            if "outside" in u["reason"]:
                p_ = _pad_at(board, net, u["at"])
                rep = outline_report(board, net, p_, pm[net]) if p_ else None
                u["outline_mm"] = round(rep[0], 3) if rep else None
                u["pour_layer"] = rep[1] if rep else None
                rec["outside"].append({"net": net, "ref": u["ref"], "pad": u["pad"], "at": u["at"],
                                       "dist_mm": u["outline_mm"], "layer": u["pour_layer"],
                                       "extendable": None, "why": None})
        rec["tied"] += tied
        rec["untied"] += untied
    if extend_on and rec["outside"]:                   # opt-in: grow the outline to the pad when the added area is clear
        for o in rec["outside"]:
            p_ = _pad_at(board, o["net"], o["at"])
            ok, why = try_extend(board, o["net"], p_, ext_max, pm[o["net"]], commit=True)
            o["extendable"], o["why"] = ok, why
            if ok:
                PL.refill(board)                           # K14: the grown pour is filled before the tie is verified against it
                tied, _u = _tie_pads(board, o["net"], [p_], pm[o["net"]], rules)
                if not tied and tie_status(board, o["net"], p_, pm[o["net"]])[0]:     # the grown pour already reaches it (tied through copper/via)
                    x_, y_ = _pad_xy(p_)
                    tied = [{"net": o["net"], "pad": p_.GetNumber(), "at": o["at"], "via": None}]
                for u in _u:                               # still untied after the growth: the row carries the tie's reason, not "outside"
                    for row in rec["untied"]:
                        if row["net"] == o["net"] and row["at"] == o["at"]:
                            row["reason"] = f"pour grown to the pad, then: {u['reason']}"
                if tied:
                    rec["tied"] += tied
                    rec["satisfied"].append(dict(tied[0], ref=o["ref"]))
                    rec["untied"] = [u for u in rec["untied"] if not (u["net"] == o["net"] and u["at"] == o["at"])]
                    rec["nets"][o["net"]]["tied_by_via"] += 1
                    rec["nets"][o["net"]]["untied"] -= 1
    else:
        for o in rec["outside"]:
            p_ = _pad_at(board, o["net"], o["at"])
            o["extendable"], o["why"] = try_extend(board, o["net"], p_, ext_max, pm[o["net"]]) if p_ else (False, "pad not found")
            if o["extendable"]:                       # report only: the zone is not grown
                o["extendable"], o["why"] = True, "clear; extend_to_pads = false, not grown"
    PL.refill(board)
    pcbnew.SaveBoard(board_out, board)
    with open(os.path.join(workdir, "pourtie.json"), "w") as fh:
        json.dump(rec, fh, indent=1)
    return rec


def _ref(pad):
    if pad is None:
        return ""
    f = pad.GetParentFootprint()
    return f.GetReference() if f is not None else ""


def _mm_at(pad):
    p = pad.GetPosition()
    return [round(pcbnew.ToMM(p.x), 4), round(pcbnew.ToMM(p.y), 4)]


def _pad_at(board, net, at_mm):
    """the pad of `net` whose centre is at (x_mm, y_mm) (pad numbers repeat across parts; positions do not)."""
    x, y = pcbnew.FromMM(at_mm[0]), pcbnew.FromMM(at_mm[1])
    for f in board.GetFootprints():
        for p in f.Pads():
            if p.GetNetname() == net and abs(p.GetPosition().x - x) <= TOL and abs(p.GetPosition().y - y) <= TOL:
                return p
    return None


def repair_in_memory(board, rec):
    """the check and the re-tie on an open board: every verified tie of `rec` that no longer reaches the filled pour is
    re-tied at a legal site. -> {checked, lost, repaired, unrepaired}."""
    PL.refill(board)
    pm = pour_map(board)
    lost, repaired, unrepaired = [], 0, []
    for t in rec["tied"]:
        pad = _pad_at(board, t["net"], t["at"])
        if pad is None or not pm.get(t["net"]):
            continue
        if not tie_status(board, t["net"], pad, pm[t["net"]])[0]:
            lost.append(t)
            tied, untied = _tie_pads(board, t["net"], [pad], pm[t["net"]], rec.get("rules"))
            if tied:
                repaired += 1
            else:
                unrepaired += [dict(u, reason="lost after routing; " + u["reason"]) for u in untied]
    PL.refill(board)
    return {"checked": len(rec["tied"]), "lost": len(lost), "repaired": repaired, "unrepaired": unrepaired}


def repair(board_in, board_out, tie_dir, workdir):
    """file wrapper of repair_in_memory: reads tie_dir/pourtie.json, writes board_out and workdir/pour_repair.json."""
    rec = json.load(open(os.path.join(tie_dir, "pourtie.json")))
    board = pcbnew.LoadBoard(board_in)
    out = repair_in_memory(board, rec)
    pcbnew.SaveBoard(board_out, board)
    out["board"] = board_out
    with open(os.path.join(workdir, "pour_repair.json"), "w") as fh:
        json.dump(out, fh, indent=1)
    return out
