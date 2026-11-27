"""`fluxplace thermal BOARD --constraints C --out O [--stage pre|post]`: a thermal area from `[thermal.NAME]` blocks.

    [thermal.NAME]
    polygon = [[x, y], ...]  or  rect = [x0, y0, x1, y1]     (mm)
    side = "F" | "B"
    net = "GND"                                               (default)
    via = { diameter = 0.46, drill = 0.2, pitch = 2.0 }      (mm; required for pre)
    exclude_refs = ["H201"]                                   (their pads get a cut-out in the area)
    mask_open = true                                          (post: open the solder mask over the area)
    clearance_mm / cut_mm = 1.0 / grow_mm = 0.25 / sliver_mm = 0.5   (optional overrides)

pre  - locked through vias of `net` on a pitch grid inside the area, each skipped when it is within clearance of existing
       copper or holes (a via of this run counts too); a no-tracks rule area on `side` (pads, vias, pours and footprints
       allowed) with a circle cut-out around each exclude_refs pad (pad half-size + cut_mm).
post - the solder-mask opening of `side` (mask_open): the area minus the cut-outs, carved around every non-`net` via,
       pad and `side` track by grow_mm, then opened by sliver_mm so slivers under that width are dropped.
Each block is independent. pre refuses to run twice (a rule area named "thermal NAME" already on the board).
"""
import math
import os
import shutil

from . import geom as GM
from . import routediag as RD

CUT_MM, GROW_MM, SLIVER_MM = 1.0, 0.25, 0.5


def spec(name, cfg, pre=True):
    """Validated, unit-checked block -> dict (raises ValueError with the block name). `via` is required only for the
    pre stage (PHASE3F A7: a post / recut-only block may omit it)."""
    side = str(cfg.get("side", "")).upper()
    if side not in ("F", "B"):
        raise ValueError(f"[thermal.{name}]: side must be \"F\" or \"B\"")
    if "rect" in cfg:
        x0, y0, x1, y1 = [float(v) for v in cfg["rect"]]
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    elif "polygon" in cfg:
        pts = [(float(x), float(y)) for x, y in cfg["polygon"]]
        if len(pts) < 3:
            raise ValueError(f"[thermal.{name}]: polygon needs at least 3 points")
    else:
        raise ValueError(f"[thermal.{name}]: give polygon or rect")
    via = cfg.get("via") or {}
    missing = [k for k in ("diameter", "drill", "pitch") if k not in via]
    if missing and pre:
        raise ValueError(f"[thermal.{name}]: via needs {', '.join(missing)}")
    return {"name": name, "side": side, "pts": pts, "net": str(cfg.get("net", "GND")),
            "via": {k: float(via[k]) for k in ("diameter", "drill", "pitch") if k in via} or None,
            "exclude": [str(r) for r in cfg.get("exclude_refs", []) or []],
            "mask": bool(cfg.get("mask_open", False)),
            "clr": cfg.get("clearance_mm"), "cut": float(cfg.get("cut_mm", CUT_MM)),
            "grow": float(cfg.get("grow_mm", GROW_MM)), "sliver": float(cfg.get("sliver_mm", SLIVER_MM))}


def blocks(cons, pre=True):
    return [spec(n, c, pre) for n, c in sorted(((cons or {}).get("thermal") or {}).items()) if isinstance(c, dict)]


def _circle(cx, cy, r, n=72):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _cuts(PN, b, sp):
    """(x, y, r) mm per exclude_refs pad: r = pad's larger half-size + cut_mm."""
    found, out = set(), []
    for f in b.GetFootprints():
        ref = f.GetReference()
        if ref not in sp["exclude"]:
            continue
        found.add(ref)
        for pd in f.Pads():
            p, sz = pd.GetPosition(), pd.GetSize()
            out.append((PN.ToMM(p.x), PN.ToMM(p.y), PN.ToMM(max(sz.x, sz.y)) / 2 + sp["cut"]))
    missing = sorted(set(sp["exclude"]) - found)
    if missing:
        raise ValueError(f"[thermal.{sp['name']}]: exclude_refs not on the board: {', '.join(missing)}")
    return out


def _area(PN, sp, cuts):
    ps = PN.SHAPE_POLY_SET()
    ps.NewOutline()
    for x, y in sp["pts"]:
        ps.Append(PN.FromMM(x), PN.FromMM(y))
    for cx, cy, r in cuts:                   # a hole of the outline (a boolean subtract of a temporary left the zone unsaveable)
        ps.NewHole()
        for x, y in _circle(cx, cy, r):
            ps.Append(PN.FromMM(x), PN.FromMM(y), 0, 0)
    return ps


def _inside(sp, cuts, x, y):
    return GM.poly_has(sp["pts"], x, y) and all(math.hypot(x - cx, y - cy) > r for cx, cy, r in cuts)


def _copper(PN, b):
    """Obstacles for the via clearance test: segments, vias, holes and pad bounding boxes (conservative)."""
    segs, vias, holes, boxes = [], [], [], []      # plain numbers only: wrappers of board items must not outlive the call
    for t in b.GetTracks():
        if t.Type() == PN.PCB_VIA_T:
            p = t.GetPosition()
            vias.append((PN.ToMM(p.x), PN.ToMM(p.y), PN.ToMM(t.GetWidth(PN.F_Cu)) / 2))
        else:
            s, e = t.GetStart(), t.GetEnd()
            segs.append((PN.ToMM(s.x), PN.ToMM(s.y), PN.ToMM(e.x), PN.ToMM(e.y), PN.ToMM(t.GetWidth()) / 2))
    for f in b.GetFootprints():
        for pd in f.Pads():
            p = pd.GetPosition()
            d = max(PN.ToMM(pd.GetDrillSizeX()), PN.ToMM(pd.GetDrillSizeY()))
            if d > 0:
                holes.append((PN.ToMM(p.x), PN.ToMM(p.y), d / 2))
            bb = pd.GetBoundingBox()
            boxes.append((PN.ToMM(bb.GetLeft()), PN.ToMM(bb.GetTop()), PN.ToMM(bb.GetRight()), PN.ToMM(bb.GetBottom())))
    return segs, vias, holes, boxes


def _seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _clear(PN, obs, x, y, r, edge):
    """True if a via of radius r at (x, y) keeps `edge` mm to every obstacle."""
    segs, vias, holes, boxes = obs
    if any(_seg_dist(x, y, a, b_, c, d) - hw - r < edge for a, b_, c, d, hw in segs):
        return False
    if any(math.hypot(x - vx, y - vy) - vr - r < edge for vx, vy, vr in vias):
        return False
    if any(math.hypot(x - hx, y - hy) - hr - r < edge for hx, hy, hr in holes):
        return False
    for bx0, by0, bx1, by1 in boxes:         # pad bounding boxes (conservative)
        dx = max(bx0 - x, 0.0, x - bx1)
        dy = max(by0 - y, 0.0, y - by1)
        if math.hypot(dx, dy) - r < edge:
            return False
    return True


def _pre(PN, b, sp, log, board_clearance, keep):
    if any(z.GetZoneName() == f"thermal {sp['name']}" for z in b.Zones()):
        raise ValueError(f"[thermal.{sp['name']}]: the board already has rule area 'thermal {sp['name']}' (pre runs once)")
    net = b.FindNet(sp["net"])
    if net is None:
        raise ValueError(f"[thermal.{sp['name']}]: net {sp['net']!r} is not on the board")
    keep.append(net)                         # vias point at this net object: its wrapper must outlive the function
    cuts = _cuts(PN, b, sp)
    if sp["via"] is None:
        raise ValueError(f"[thermal.{sp['name']}]: via needs diameter, drill, pitch")
    v = sp["via"]
    r = v["diameter"] / 2
    edge = float(sp["clr"]) if sp["clr"] is not None else board_clearance
    obs = _copper(PN, b)
    xs = [p[0] for p in sp["pts"]]
    ys = [p[1] for p in sp["pts"]]
    x0, y0 = min(xs) + v["pitch"] / 2, min(ys) + v["pitch"] / 2
    placed = in_cut = near_cu = 0
    i = 0
    while x0 + i * v["pitch"] <= max(xs):
        j = 0
        while y0 + j * v["pitch"] <= max(ys):
            x, y = x0 + i * v["pitch"], y0 + j * v["pitch"]
            j += 1
            if not GM.poly_has(sp["pts"], x, y):
                continue
            if not _inside(sp, cuts, x, y):
                in_cut += 1
                continue
            if not _clear(PN, obs, x, y, r, edge):
                near_cu += 1
                continue
            via = PN.PCB_VIA(b)
            via.SetPosition(PN.VECTOR2I(PN.FromMM(x), PN.FromMM(y)))
            via.SetWidth(PN.FromMM(v["diameter"]))
            via.SetDrill(PN.FromMM(v["drill"]))
            via.SetViaType(PN.VIATYPE_THROUGH)
            via.SetLayerPair(PN.F_Cu, PN.B_Cu)
            via.SetNet(net)
            via.SetLocked(True)
            b.Add(via)
            keep.append(via)                 # the board holds the C++ object; keep the wrapper alive until the save
            obs[1].append((x, y, r))
            placed += 1
        i += 1
    z = PN.ZONE(b)
    z.SetLayer(PN.B_Cu if sp["side"] == "B" else PN.F_Cu)
    z.SetIsRuleArea(True)
    z.SetZoneName(f"thermal {sp['name']}")
    z.SetDoNotAllowTracks(True)
    z.SetDoNotAllowPads(False)
    z.SetDoNotAllowVias(False)
    z.SetDoNotAllowZoneFills(False)
    z.SetDoNotAllowFootprints(False)
    area = _area(PN, sp, cuts)
    z.SetOutline(area)                       # the zone keeps a pointer to the outline: it must outlive the call
    b.Add(z)
    keep += [z, area]
    log.append(f"thermal {sp['name']} pre: {placed} locked {sp['net']} via(s) placed, skipped (cut-out) {in_cut}, "
               f"skipped (copper or hole within {edge:g} mm) {near_cu}; rule area on {sp['side']} with {len(cuts)} cut-out(s)")


def _corner(PN):
    CORNER = getattr(PN.SHAPE_POLY_SET, "CORNER_STRATEGY_ROUND_ALL_CORNERS", None)
    return CORNER if CORNER is not None else PN.CORNER_STRATEGY_ROUND_ALL_CORNERS


def _carve(PN, b, area, layer, net, box, grow):
    """PHASE3F A7 (shared by the block path and the inferred path): subtract from `area` the copper of every item not of
    `net` on `layer` inside `box` (mm): vias, tracks and pads grown by `grow`, and the filled zones of other nets on the
    layer (inflated by `grow`). -> counts {via, track, pad, zone}."""
    CORNER = _corner(PN)
    carve = PN.SHAPE_POLY_SET()
    n = {"via": 0, "pad": 0, "track": 0, "zone": 0}

    def rect(cx, cy, hx, hy, ang=0.0):
        c, s = math.cos(ang), math.sin(ang)
        carve.NewOutline()
        for dx, dy in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)):
            carve.Append(PN.FromMM(cx + dx * c - dy * s), PN.FromMM(cy + dx * s + dy * c))

    def inbox(x, y, m=0.0):
        return box[0] - m <= x <= box[2] + m and box[1] - m <= y <= box[3] + m

    def crosses(x0, y0, x1, y1, m):
        """the segment meets the box grown by m (Liang-Barsky): a track that crosses the box with both ends outside must be carved too"""
        t0, t1 = 0.0, 1.0
        for d, lo, hi, a in ((x1 - x0, box[0] - m, box[2] + m, x0), (y1 - y0, box[1] - m, box[3] + m, y0)):
            if abs(d) < 1e-12:
                if a < lo or a > hi:
                    return False
                continue
            u0, u1 = (lo - a) / d, (hi - a) / d
            t0, t1 = max(t0, min(u0, u1)), min(t1, max(u0, u1))
            if t0 > t1:
                return False
        return True

    for t in b.GetTracks():
        if t.GetNetname() == net:
            continue
        if t.Type() == PN.PCB_VIA_T:
            p = t.GetPosition()
            x, y = PN.ToMM(p.x), PN.ToMM(p.y)
            if inbox(x, y, PN.ToMM(t.GetWidth(PN.F_Cu)) / 2 + grow):          # a via just outside the box still bites into it
                carve.NewOutline()
                r = PN.ToMM(t.GetWidth(PN.F_Cu)) / 2 + grow
                for px, py in _circle(x, y, r, 24):
                    carve.Append(PN.FromMM(px), PN.FromMM(py))
                n["via"] += 1
        elif t.GetLayer() == layer:
            s, e = t.GetStart(), t.GetEnd()
            sx, sy, ex, ey = PN.ToMM(s.x), PN.ToMM(s.y), PN.ToMM(e.x), PN.ToMM(e.y)
            L = math.hypot(ex - sx, ey - sy)
            h = PN.ToMM(t.GetWidth()) / 2 + grow
            if L == 0 or not crosses(sx, sy, ex, ey, h):
                continue
            rect((sx + ex) / 2, (sy + ey) / 2, L / 2 + h, h, math.atan2(ey - sy, ex - sx))
            n["track"] += 1
    for pd in [p for f in b.GetFootprints() for p in f.Pads()]:
        if pd.GetNetname() == net:
            continue
        if not pd.GetLayerSet().Contains(layer) and pd.GetAttribute() != PN.PAD_ATTRIB_NPTH:
            continue
        bb = pd.GetBoundingBox()
        x0, y0, x1, y1 = PN.ToMM(bb.GetLeft()), PN.ToMM(bb.GetTop()), PN.ToMM(bb.GetRight()), PN.ToMM(bb.GetBottom())
        if x1 < box[0] or x0 > box[2] or y1 < box[1] or y0 > box[3]:
            continue
        rect((x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2 + grow, (y1 - y0) / 2 + grow)
        n["pad"] += 1
    for z in b.Zones():                      # foreign-net zone fills on this copper layer (the 18 zone bridges of W14)
        if z.GetIsRuleArea() or z.GetNetname() == net or not z.IsOnLayer(layer):
            continue
        zb = z.GetBoundingBox()
        if PN.ToMM(zb.GetRight()) < box[0] or PN.ToMM(zb.GetLeft()) > box[2] or \
                PN.ToMM(zb.GetBottom()) < box[1] or PN.ToMM(zb.GetTop()) > box[3]:
            continue
        fills = PN.SHAPE_POLY_SET(z.GetFilledPolysList(layer))
        fills.Inflate(PN.FromMM(grow), CORNER, PN.FromMM(0.01), False)
        carve.Append(fills)
        n["zone"] += 1
    if carve.OutlineCount():
        carve.Simplify()
        area.BooleanSubtract(carve)
    return n


def _mask_shape(PN, b, area, mask, sliver, keep):
    """Sliver cut of `area` and a filled board-level polygon on `mask` (the mask opening). -> the kept poly set."""
    CORNER = _corner(PN)
    kept = PN.SHAPE_POLY_SET(area)
    kept.Deflate(PN.FromMM(sliver / 2), CORNER, PN.FromMM(0.01))
    kept.Inflate(PN.FromMM(sliver / 2), CORNER, PN.FromMM(0.01), False)
    shp = PN.PCB_SHAPE(b)
    shp.SetShape(PN.SHAPE_T_POLY)
    shp.SetLayer(mask)
    shp.SetFilled(True)
    shp.SetWidth(0)
    shp.SetPolyShape(kept)
    b.Add(shp)
    keep += [shp, kept]
    return kept


def _post(PN, b, sp, log, keep):
    layer = PN.B_Cu if sp["side"] == "B" else PN.F_Cu
    mask = PN.B_Mask if sp["side"] == "B" else PN.F_Mask
    cuts = _cuts(PN, b, sp)
    area = _area(PN, sp, cuts)
    xs = [p[0] for p in sp["pts"]]
    ys = [p[1] for p in sp["pts"]]
    box = (min(xs) - 0.5, min(ys) - 0.5, max(xs) + 0.5, max(ys) + 0.5)
    g = sp["grow"]
    n = _carve(PN, b, area, layer, sp["net"], box, g)
    kept = _mask_shape(PN, b, area, mask, sp["sliver"], keep)
    log.append(f"thermal {sp['name']} post: mask opening on {sp['side']}; carved around {n['via']} via(s), "
               f"{n['pad']} pad(s), {n['track']} track(s), {n['zone']} zone fill(s) (+{g:g} mm); {kept.OutlineCount()} "
               f"outline(s) after the {sp['sliver']:g} mm sliver cut")


def _infer_net(PN, b, poly, layer, step=0.5):
    """PHASE3F A7: the net owning the most copper area on `layer` inside `poly` (zone fills and pads), sampled on a
    `step` mm grid. None when no copper lies inside."""
    bb = poly.BBox()
    x0, y0, x1, y1 = PN.ToMM(bb.GetLeft()), PN.ToMM(bb.GetTop()), PN.ToMM(bb.GetRight()), PN.ToMM(bb.GetBottom())
    zones = [(z.GetNetname(), PN.SHAPE_POLY_SET(z.GetFilledPolysList(layer))) for z in b.Zones()
             if not z.GetIsRuleArea() and z.GetNetname() and z.IsOnLayer(layer)]
    pads = [(pd.GetNetname(), pd.GetBoundingBox()) for f in b.GetFootprints() for pd in f.Pads()
            if pd.GetNetname() and pd.GetLayerSet().Contains(layer)]
    count = {}
    nx, ny = int((x1 - x0) / step) + 1, int((y1 - y0) / step) + 1
    for i in range(nx):
        for j in range(ny):
            x, y = x0 + i * step, y0 + j * step
            pt = PN.VECTOR2I(PN.FromMM(x), PN.FromMM(y))
            if not poly.Collide(pt):
                continue
            hit = {net for net, fills in zones if fills.Collide(pt)}
            hit |= {net for net, bx in pads if bx.Contains(pt)}
            for net in hit:
                count[net] = count.get(net, 0) + 1
    return max(count, key=count.get) if count else None


def _covered(PN, d, sp):
    """Does the board-level mask polygon `d` belong to block `sp` (its side's mask layer, bounding box overlapping the block's)?
    The one rule of recut (which replaces such polygons) and strip_covered (which removes them before routing)."""
    if d.GetLayer() != (PN.B_Mask if sp["side"] == "B" else PN.F_Mask):
        return False
    xs, ys = [p[0] for p in sp["pts"]], [p[1] for p in sp["pts"]]
    bb = d.GetBoundingBox()
    return not (PN.ToMM(bb.GetRight()) < min(xs) or PN.ToMM(bb.GetLeft()) > max(xs) or
                PN.ToMM(bb.GetBottom()) < min(ys) or PN.ToMM(bb.GetTop()) > max(ys))


def strip_covered(board, cons, out):
    """The settled order (thermal mask after routing, D71cd): the board-level mask openings a `[thermal.NAME] mask_open` block will re-make
    in `recut` are taken off the working board before the first copper stage, so routers and fan-out may put any net's copper under the
    window (recut carves it round every non-owner via afterwards).  An opening no block covers stays on the board and is honoured as
    forbidden for non-owner copper (routediag.mask_openings).  -> {"stripped": n, "log": [line]}; writes `out` only when n > 0."""
    import pcbnew as PN
    res = {"stripped": 0, "log": []}
    specs = [sp for sp in blocks(cons, pre=False) if sp["mask"]]
    if not specs:
        return res
    b = PN.LoadBoard(board)
    for d in [d for d in b.GetDrawings() if d.GetClass() == "PCB_SHAPE" and d.GetLayer() in (PN.F_Mask, PN.B_Mask)
              and d.GetShape() == PN.SHAPE_T_POLY]:
        sp = next((s for s in specs if _covered(PN, d, s)), None)
        if sp:
            bb = d.GetBoundingBox()
            res["log"].append(f"thermal {sp['name']}: mask opening at ({PN.ToMM(bb.GetLeft()):.1f}, {PN.ToMM(bb.GetTop()):.1f}) taken off "
                              f"the working board before routing; recut re-makes it after the copper")
            b.Delete(d)  # Delete, not Remove: a Remove() of a discarded item breaks pcbnew.LoadBoard for the process
            res["stripped"] += 1
    if res["stripped"]:
        PN.SaveBoard(out, b)
        src_pro = os.path.splitext(board)[0] + ".kicad_pro"
        if os.path.exists(src_pro):
            shutil.copy(src_pro, os.path.splitext(out)[0] + ".kicad_pro")
    return res


def recut(board, cons, out):
    """PHASE3F A7: re-cut the mask openings after routing (copper is not touched).
    1. every [thermal.NAME] block with mask_open: the board-level filled polygons of its side whose bounding box overlaps
       the block are removed, then a fresh opening is made from the block (_post).
    2. every other board-level filled polygon on F.Mask / B.Mask: its net = the net owning the most copper inside it;
       no copper -> skipped (untouched); else it is carved round the foreign copper (_carve) and replaced.
    -> {"blocks", "inferred", "skipped": [reason], "log": [line]}. Writes `out` only when a polygon changed."""
    import pcbnew as PN
    if os.path.abspath(out) == os.path.abspath(board):
        raise ValueError("--out must differ from the input board")
    res = {"blocks": 0, "inferred": 0, "skipped": [], "log": []}
    specs = [sp for sp in blocks(cons, pre=False) if sp["mask"]]
    b = PN.LoadBoard(board)
    keep = []
    layers = (PN.F_Mask, PN.B_Mask)
    polys = [d for d in b.GetDrawings() if d.GetClass() == "PCB_SHAPE" and d.GetLayer() in layers
             and d.GetShape() == PN.SHAPE_T_POLY and d.IsPolyShapeValid()]
    for sp in specs:                          # 1. block path
        for d in list(polys):
            if not _covered(PN, d, sp):
                continue
            b.Delete(d)  # Delete, not Remove: a Remove() of a discarded item breaks pcbnew.LoadBoard for the process
            polys.remove(d)
        _post(PN, b, sp, res["log"], keep)
        res["blocks"] += 1
    for d in polys:                           # 2. inferred path (the polygons the blocks did not replace)
        layer = PN.B_Cu if d.GetLayer() == PN.B_Mask else PN.F_Cu
        poly = d.GetPolyShape()
        bb = poly.BBox()
        at = (round(PN.ToMM(bb.GetLeft()), 2), round(PN.ToMM(bb.GetTop()), 2))
        net = _infer_net(PN, b, poly, layer)
        if net is None:
            res["skipped"].append(f"polygon at {at}: no copper inside, untouched")
            continue
        box = (PN.ToMM(bb.GetLeft()) - 0.5, PN.ToMM(bb.GetTop()) - 0.5, PN.ToMM(bb.GetRight()) + 0.5,
               PN.ToMM(bb.GetBottom()) + 0.5)
        area = PN.SHAPE_POLY_SET(poly)
        n = _carve(PN, b, area, layer, net, box, GROW_MM)
        dlayer = d.GetLayer()                     # read before Delete: the item is gone after
        b.Delete(d)  # Delete, not Remove: a Remove() of a discarded item breaks pcbnew.LoadBoard for the process
        _mask_shape(PN, b, area, dlayer, SLIVER_MM, keep)
        res["inferred"] += 1
        res["log"].append(f"polygon at {at} on {'B' if layer == PN.B_Cu else 'F'}: net {net}; carved around "
                          f"{n['via']} via(s), {n['pad']} pad(s), {n['track']} track(s), {n['zone']} zone fill(s)")
    if res["blocks"] or res["inferred"]:
        PN.SaveBoard(out, b)
        src_pro = os.path.splitext(board)[0] + ".kicad_pro"
        if os.path.exists(src_pro):
            shutil.copy(src_pro, os.path.splitext(out)[0] + ".kicad_pro")
    return res


def run(board, cons, out, stage="pre"):
    """Apply every [thermal.NAME] block to `board` -> `out`. -> [log line]. Raises ValueError on a refusal."""
    import pcbnew as PN
    if os.path.abspath(out) == os.path.abspath(board):
        raise ValueError("--out must differ from the input board")
    specs = blocks(cons, pre=(stage == "pre"))
    if not specs:
        raise ValueError("no [thermal.NAME] block in the constraints")
    b = PN.LoadBoard(board)
    log, keep = [], []                       # keep: wrappers of items added to the board (else the GC frees them)
    for sp in specs:
        if stage == "pre":
            _pre(PN, b, sp, log, RD.load(board)["clearance"], keep)
        elif sp["mask"]:                     # PHASE3I B6: a mask opening is cut in the finish stage (recut), never before routing
            log.append(f"thermal {sp['name']} post: deferred to the finish stage (recut); no mask opening written")
        else:
            log.append(f"thermal {sp['name']} post: skipped (mask_open is not set)")
    pours = [z for z in b.Zones() if not z.GetIsRuleArea()]   # rule areas have no fill; the filler crashes on them
    if pours:                                # (and on an empty list), so only real pours are refilled
        PN.ZONE_FILLER(b).Fill(pours)
    PN.SaveBoard(out, b)
    src_pro = os.path.splitext(board)[0] + ".kicad_pro"
    if os.path.exists(src_pro):
        shutil.copy(src_pro, os.path.splitext(out)[0] + ".kicad_pro")
    log.append(f"saved {out}")
    return log


def cmd(a):
    from . import constraints as C
    try:
        lines = run(a.board, C.load(a.constraints), a.out, a.stage)
    except ValueError as e:
        raise SystemExit(f"thermal: {e}")
    print("\n".join(lines))


def add_parser(sub):
    p = sub.add_parser("thermal", help="thermal area from [thermal.NAME]: pre = locked GND vias + rule area, "
                       "post = solder-mask opening")
    p.add_argument("board")
    p.add_argument("--constraints", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--stage", choices=["pre", "post"], default="pre")
    p.set_defaults(fn=cmd)
