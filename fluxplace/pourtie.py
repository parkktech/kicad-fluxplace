"""Pour-first connections: a pad of a net that has a pour or plane is tied THROUGH that copper before the router runs.

tie stage (after fan-out, before pairs/route): every pad of a pour net is either already reached by its filled pour (on the
pad's own pour layer) or reached through same-net copper to a via that lands in filled copper of the net; the rest get a
short stub + via into the pour at a legal site (planes.via_at_points + hole_ok).  Every tie is verified against the FILLED
zone geometry after one refill; a tie that does not verify is undone.  Layers come from the board's zones, never named here.
The verified ties are written to pourtie.json; repair() re-checks them after routing and re-ties what routing cut off.

Durable ties (W16 C112.2 / L300.2): a pour on a routing layer is whatever the router leaves of it, so for a net that owns a PLANE
layer (solid_lids) the pour on a pad's own routing layer is no tie: the pad gets its stub + via into the plane here, before any
signal copper takes the site.  A pad with no legal site stays on its surface pour and is recorded `fragile` (stage WARN, re-checked
by repair); the placement gate POUR_TIE_BLOCKED (blocked()) names such a pad before any copper exists."""
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


def solid_lids(board, cons=None):
    """copper layers routing does not use (planes): `[preroute] signal_layers` / `plane_layers`, else the stackup's planes, else an INNER
    layer one netted zone covers >= 90 % of (the rule of preroute.layer_split, on the open board). -> {layer id}"""
    cu = list(board.GetEnabledLayers().CuStack())
    name = {l: board.GetLayerName(l) for l in cu}
    pc = (cons or {}).get("preroute", {})
    if isinstance(pc.get("signal_layers"), list):
        return {l for l in cu if name[l] not in pc["signal_layers"]}
    if isinstance(pc.get("plane_layers"), list):
        return {l for l in cu if name[l] in pc["plane_layers"]}
    try:
        from . import zgeom as ZG
        planes = set(ZG.resolve_stack(cons)[1] or [])
    except Exception:                                    # noqa: BLE001 - no stackup in the constraints: the pour test decides
        planes = set()
    if planes:
        return {l for l in cu[1:-1] if name[l] in planes}
    bb = board.GetBoardEdgesBoundingBox()
    area = max(float(bb.GetWidth()) * float(bb.GetHeight()), 1.0)
    out = set()
    for z in board.Zones():
        if z.GetIsRuleArea() or not z.GetNetname():
            continue
        zb = z.GetBoundingBox()
        if float(zb.GetWidth()) * float(zb.GetHeight()) >= 0.9 * area:
            out.update(l for l in cu[1:-1] if z.IsOnLayer(l))
    return out


def tie_layers(pours, solid):
    """the pour layers a tie must reach: the net's plane layers when durable ties are on (`solid` given) and it has one, else every
    pour layer (a rail poured on a signal layer only has nothing better)."""
    return [l for l in pours if l in solid] or list(pours) if solid is not None else list(pours)


def pad_copper_lids(pad):
    return _copper_lids(pad.GetLayerSet().Seq())


def _pad_xy(pad):
    p = pad.GetPosition()
    return p.x, p.y


def tie_status(board, net, pad, pours, solid=None):
    """-> ("pour-layer", layer) | ("via", (x_mm, y_mm)) | (None, None): how the pad reaches the net's filled copper now.
    `solid` (solid_lids) given: only the net's plane layers count (tie_layers), on the pad itself and under a via."""
    x, y = _pad_xy(pad)
    pours = tie_layers(pours, solid)
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


def _ring_sites(x, y, radii=(0.0, 0.8, 1.1, 1.4)):
    for r_mm in radii:
        if r_mm == 0.0:
            yield r_mm, x, y
            continue
        for k in range(12):
            a = k * math.pi / 6
            yield r_mm, x + pcbnew.FromMM(r_mm * math.cos(a)), y + pcbnew.FromMM(r_mm * math.sin(a))


def _via_keys(board):
    return {(t.GetPosition().x, t.GetPosition().y) for t in board.GetTracks() if t.Type() == pcbnew.PCB_VIA_T}


REACH_MM = 1.4                     # the farthest tie site from its pad (_ring_sites)


def _near(board, pad, layer):
    """the pads (any net) and the other-net tracks on `layer` a tie site of `pad` can meet."""
    box = pad.GetBoundingBox()
    box.Inflate(pcbnew.FromMM(REACH_MM + 1.0) + min(box.GetWidth(), box.GetHeight()) // 2)
    pads = [p for f in board.GetFootprints() if f.GetBoundingBox(False, False).Intersects(box) for p in f.Pads() if p.GetBoundingBox().Intersects(box)]
    trks = [t for t in board.GetTracks() if t.GetNetCode() != pad.GetNetCode() and t.IsOnLayer(layer) and t.GetBoundingBox().Intersects(box)]
    return pads, trks


def _site_free(pad, layer, x, y, cx, cy, rules, avoid, near):
    """durable-tie site rules: the via barrel in no pad (unless `via_in_pad`), outside every keep-free rect (mm), and its stub
    `clearance` off other-net pads and tracks on the pad's layer."""
    pads, trks = near
    if not rules.get("via_in_pad"):
        at = pcbnew.VECTOR2I(int(cx), int(cy))
        if any(p.HitTest(at, pcbnew.FromMM(rules["via"]) // 2) for p in pads):
            return False
    mx, my = pcbnew.ToMM(int(cx)), pcbnew.ToMM(int(cy))
    h = rules["via"] / 2
    if any(a - h < mx < c + h and b - h < my < d + h for a, b, c, d in avoid):
        return False
    acc = pcbnew.FromMM(rules["track"] / 2 + rules.get("clearance", 0.2))
    n = max(1, int(math.hypot(cx - x, cy - y) / pcbnew.FromMM(0.05)))
    pts = [pcbnew.VECTOR2I(int(x + (cx - x) * k / n), int(y + (cy - y) * k / n)) for k in range(n + 1)]
    other = [o for o in pads if o.GetNetCode() != pad.GetNetCode() and o.IsOnLayer(layer)] + trks
    return not any(o.HitTest(q, acc) for o in other for q in pts)


def _add_tie(board, net, pad, pours, made, rules=None, avoid=None):
    """stub (if the site is off the pad) + via at the first legal ring site; the created objects go into `made`.
    `avoid` given (durable ties): the site also passes _site_free."""
    x, y = _pad_xy(pad)
    nc = board.FindNet(net)
    layer = pad_copper_lids(pad)[0]
    rules = rules or {"via": 0.6, "drill": 0.3, "track": 0.15}
    near = _near(board, pad, layer) if avoid is not None else None
    radii = (0.0, 0.8, 1.1, 1.4)
    if avoid is not None and not rules.get("via_in_pad"):   # the rings start outside the pad's own copper, whatever its size
        sz = pad.GetSize(layer)
        r0 = pcbnew.ToMM(min(sz.x, sz.y)) / 2 + rules["via"] / 2 + rules.get("clearance", 0.2)
        radii = tuple(round(r0 + k * (REACH_MM - 0.8) / 2, 3) for k in range(3)) if r0 > 0.8 else radii[1:]
        if pcbnew.ToMM(min(sz.x, sz.y)) >= 2 * rules["via"]:    # a pad two vias wide (thermal pad) takes the via in its own copper
            radii = (0.0,) + radii
            near = ([p for p in near[0] if not (p.GetNumber() == pad.GetNumber() and p.GetPosition() == pad.GetPosition())], near[1])
    for r_mm, cx, cy in _ring_sites(x, y, radii):
        if avoid is not None and not _site_free(pad, layer, x, y, cx, cy, rules, avoid, near):
            continue
        if not PL.hole_ok(board, cx, cy, via_drill=rules["drill"], hgap_mm=max(0.25, rules.get("hole_to_hole", 0.0))):
            continue
        before = _via_keys(board)
        if PL.via_at_points(board, net, [(pcbnew.ToMM(cx), pcbnew.ToMM(cy))], via_d=rules["via"], via_drill=rules["drill"]) != 1:
            continue
        new = [t for t in board.GetTracks() if t.Type() == pcbnew.PCB_VIA_T and (t.GetPosition().x, t.GetPosition().y) not in before]
        made.extend(new)
        if r_mm > 0.0:
            tr = pcbnew.PCB_TRACK(board)
            tr.SetStart(pcbnew.VECTOR2I(int(x), int(y)))
            tr.SetEnd(new[-1].GetPosition() if new else pcbnew.VECTOR2I(int(cx), int(cy)))     # on the via's own centre (mm round trip)
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


def _tie_pads(board, net, pads, pours, rules=None, solid=None, avoid=None):
    """-> (tied: [record], untied: [record]); verified after one refill.  `solid` given (durable ties): a tie reaches a plane
    layer of the net; an untied pad its surface pour still reaches carries `surface` = that layer."""
    cands = []
    outside = []
    all_pours, pours = pours, tie_layers(pours, solid)
    if solid is not None and avoid is None:
        avoid = ()
    surface = {}
    for pad in pads:
        how, where = tie_status(board, net, pad, pours)
        if how:
            continue
        if solid is not None:
            h0, w0 = tie_status(board, net, pad, all_pours)
            if h0 == "pour-layer":
                surface[tuple(_mm_at(pad))] = w0
        x, y = _pad_xy(pad)
        if not in_outline(board, net, x, y):
            outside.append(pad)              # no via can land in a pour that does not reach the pad: the pour is the limit
            continue
        made = []
        cands.append((pad, made if _add_tie(board, net, pad, pours, made, rules, avoid) else None))
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
    for u in untied:
        if tuple(u["at"]) in surface:
            u["surface"] = surface[tuple(u["at"])]
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
    for old in [x for x in board.Zones() if x.GetZoneName() == STRIP_NAME and x.GetNetCode() == z.GetNetCode()
                and set(x.GetLayerSet().Seq()) == set(z.GetLayerSet().Seq())]:
        inter = pcbnew.SHAPE_POLY_SET(ps)               # a strip that touches an earlier strip of the same net merges with it:
        inter.BooleanIntersection(old.Outline())        # two same-net, same-priority zones that overlap are a zones_intersect error
        if inter.OutlineCount():
            ps.BooleanAdd(old.Outline())
            board.Remove(old)
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
        nz.SetAssignedPriority(z.GetAssignedPriority() + 1)   # a same-priority overlap with its own pour is a zones_intersect error
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
    if R.get("via_in_pad"):
        rules["via_in_pad"] = True
    if R.get("hole_to_hole"):
        rules["hole_to_hole"] = R["hole_to_hole"]
    rec = {"nets": {}, "tied": [], "untied": [], "satisfied": [], "fragile": [], "rules": rules}
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
    solid = (solid_lids(board, cons) or None) if pcfg.get("durable_ties", True) else None
    avoid = pair_aprons(board_in, cons, R) if solid else None
    extend_on = bool(pcfg.get("extend_to_pads", True))
    ext_max = float(pcfg.get("extend_max_mm", 15.0))
    rec["outside"] = []
    from . import forbid as FB
    wins = FB.windows(cons)                      # B7: a foreign-net via is forbidden in a mask window (dd0cde6 ties laid one there)
    for net in sorted(pm):
        if nets is not None and net not in nets:
            continue
        pads = [p for f in board.GetFootprints() for p in f.Pads() if p.GetNetname() == net and not p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH]
        if not pads:
            continue
        already_pads = [p for p in pads if tie_status(board, net, p, pm[net], solid)[0]]
        todo = [p for p in pads if p not in already_pads]
        av = _with_windows(avoid, wins, net)
        tied, untied = _tie_pads(board, net, todo, pm[net], rules, solid, av) if todo else ([], [])
        fragile = [u for u in untied if u.get("surface")]       # no site for a via into the plane: the surface pour is all it has
        untied = [u for u in untied if not u.get("surface")]
        for u in fragile:
            u["ref"] = _ref(_pad_at(board, net, u["at"]))
            u["reason"] = f"tied by the {u['surface']} pour only; " + u["reason"]
        rec["fragile"] += fragile
        rec["satisfied"] += [{"net": net, "ref": u["ref"], "pad": u["pad"], "at": u["at"]} for u in fragile]
        rec["nets"][net] = {"pour_layers": [board.GetLayerName(l) for l in pm[net]], "pads": len(pads),
                            "tied_before": len(already_pads) + len(fragile), "tied_by_via": len(tied), "untied": len(untied),
                            "fragile": len(fragile)}
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


def _with_windows(avoid, wins, net):
    """`avoid` plus the bounding box (mm, grown by the mask margin) of every mask window `net` does not own; `avoid` untouched when none."""
    from . import geom as GM
    extra = []
    for w in wins:
        if w["owner"] != net and len(w["pts"]) >= 3:
            xs, ys = [q[0] for q in w["pts"]], [q[1] for q in w["pts"]]
            m = GM.MASK_MARGIN
            extra.append((min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m))
    return (list(avoid or []) + extra) if extra else avoid


def pair_aprons(board_in, cons, R):
    """escape aprons of the controlled pairs (escreserve.pair_aprons) as keep-free rects in mm: a tie via laid before the pairs
    stage must not stand where a pair leaves its pins. [] when the board has none (or is not a board file)."""
    try:
        from . import routediag as RD, netclass as NC, escreserve as ES
        B = RD.load(board_in)
        cls = NC.classify(B, cons or {}, None)
        return [tuple(a["rect"]) for a in ES.pair_aprons(B, cls, max(R["clearance"], B["clearance"]), R["via"])]
    except Exception:                                    # noqa: BLE001 - no pair model for this board: no keep-free rects
        return []


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
    for t in rec.get("fragile", []):                     # surface-pour pads: tied while their fill island still holds a via / drilled pad
        pad = _pad_at(board, t["net"], t["at"])
        if pad is None or not pm.get(t["net"]) or island_tied(board, t["net"], pad):
            continue
        lost.append(t)
        tied, untied = _tie_pads(board, t["net"], [pad], pm[t["net"]], rec.get("rules"), solid_lids(board), ())
        if tied:
            repaired += 1
        else:
            unrepaired += [dict(u, reason="surface pour sealed by routing; " + u["reason"]) for u in untied]
    PL.refill(board)
    return {"checked": len(rec["tied"]) + len(rec.get("fragile", [])), "lost": len(lost), "repaired": repaired, "unrepaired": unrepaired}


def island_tied(board, net, pad):
    """the filled outline of `net` under an SMD pad holds a via or a drilled pad of the net (else the island is sealed: the fill
    touches the pad and reaches no other layer)."""
    c = pad.GetPosition()
    vias = [t.GetPosition() for t in board.GetTracks() if t.Type() == pcbnew.PCB_VIA_T and t.GetNetname() == net]
    holes = [p.GetPosition() for f in board.GetFootprints() for p in f.Pads() if p.GetNetname() == net and p.HasHole()]
    for z in board.Zones():
        if z.GetIsRuleArea() or z.GetNetname() != net:
            continue
        for lid in z.GetLayerSet().CuStack():
            if not pad.IsOnLayer(lid):
                continue
            ps = z.GetFilledPolysList(lid)
            for i in range(ps.OutlineCount()):
                if ps.Contains(c, i) and any(ps.Contains(v, i) for v in vias + holes):
                    return True
    return False


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
