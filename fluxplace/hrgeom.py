"""Hand-routing geometry model (docs/AI-FINISH.md). A pcbnew-built obstacle model of one board plus the scalar
and numpy distance primitives the window / probe / suggest commands share. Millimetres, KiCad y-down.

Obstacle = one copper (or hole / keepout / edge) thing: kind pad|track|via|zone|ko|edge|hole, net name, layer set,
geometry ('seg', a, b, half_width) (a == b is a disc) or ('poly', pts). `clr` is the clearance its own net class asks
for; `clr_hole` the hole-to-it clearance. Nothing here writes a board."""
import math
import os
import re

import numpy as np

EPS = 3e-4          # mm: slack below which a gap counts as a violation (DRC compares exact nm; we stay one tick safe)


# ---- scalar primitives -------------------------------------------------------------------------------------
def pt_seg(p, a, b):
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L))
    q = (ax + t * dx, ay + t * dy)
    return math.hypot(p[0] - q[0], p[1] - q[1]), q


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def segs_cross(a, b, c, d):
    d1, d2, d3, d4 = _cross(c, d, a), _cross(c, d, b), _cross(a, b, c), _cross(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0


def seg_seg(a, b, c, d):
    """-> (distance, point on ab, point on cd)."""
    if segs_cross(a, b, c, d):
        # intersection point
        r = (b[0] - a[0], b[1] - a[1])
        s = (d[0] - c[0], d[1] - c[1])
        den = r[0] * s[1] - r[1] * s[0]
        t = ((c[0] - a[0]) * s[1] - (c[1] - a[1]) * s[0]) / den if den else 0
        p = (a[0] + t * r[0], a[1] + t * r[1])
        return 0.0, p, p
    best = None
    for p, (s0, s1), first in ((a, (c, d), True), (b, (c, d), True), (c, (a, b), False), (d, (a, b), False)):
        dist, q = pt_seg(p, s0, s1)
        cand = (dist, p, q) if first else (dist, q, p)
        if best is None or cand[0] < best[0]:
            best = cand
    return best


def pip(p, pts):
    x, y = p
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def poly_edges(pts):
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


class Ob:
    __slots__ = ("kind", "net", "layers", "g", "bb", "clr", "clr_hole", "ref", "id", "locked", "desc", "extra")

    def __init__(self, kind, net, layers, g, clr=0.0, clr_hole=0.0, ref="", id="", locked=False, desc="", extra=None):
        self.kind, self.net, self.layers, self.g = kind, net, frozenset(layers), g
        self.clr, self.clr_hole, self.ref, self.id, self.locked, self.desc = clr, clr_hole, ref, id, locked, desc
        self.extra = extra or {}
        if g[0] == "seg":
            hw = g[3]
            xs, ys = (g[1][0], g[2][0]), (g[1][1], g[2][1])
            self.bb = (min(xs) - hw, min(ys) - hw, max(xs) + hw, max(ys) + hw)
        else:
            xs, ys = [p[0] for p in g[1]], [p[1] for p in g[1]]
            self.bb = (min(xs), min(ys), max(xs), max(ys))


def ob_gap(a, b, hw, ob):
    """Edge-to-edge gap between capsule (a, b, hw) and obstacle; <= 0 means overlap.
    -> (gap, point on obstacle nearest the capsule)."""
    g = ob.g
    if g[0] == "seg":
        d, pa, pb = seg_seg(a, b, g[1], g[2])
        return d - hw - g[3], pb
    pts = g[1]
    if pip(a, pts) or pip(b, pts):
        return -hw, a
    best = (1e9, None)
    for e0, e1 in poly_edges(pts):
        d, _pa, pb = seg_seg(a, b, e0, e1)
        if d < best[0]:
            best = (d, pb)
    return best[0] - hw, best[1]


def ob_ob_gap(a, b):
    """Edge-to-edge gap between two obstacles -> (gap, point on a, point on b)."""
    ga, gb = a.g, b.g
    if ga[0] == "seg" and gb[0] == "seg":
        d, pa, pb = seg_seg(ga[1], ga[2], gb[1], gb[2])
        return d - ga[3] - gb[3], pa, pb
    if ga[0] == "seg":
        g, pt = ob_gap(ga[1], ga[2], ga[3], b)
        return g, ga[1], pt or ga[1]
    if gb[0] == "seg":
        g, pt = ob_gap(gb[1], gb[2], gb[3], a)
        return g, pt or gb[1], gb[1]
    if any(pip(p, gb[1]) for p in ga[1][:1]) or any(pip(p, ga[1]) for p in gb[1][:1]):
        return 0.0, ga[1][0], ga[1][0]
    best = (1e9, None, None)
    for e0, e1 in poly_edges(ga[1]):
        for f0, f1 in poly_edges(gb[1]):
            d, pa, pb = seg_seg(e0, e1, f0, f1)
            if d < best[0]:
                best = (d, pa, pb)
    return best


# ---- numpy fields ------------------------------------------------------------------------------------------
def _pt_seg_np(X, Y, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    if L == 0:
        return np.hypot(X - a[0], Y - a[1])
    t = np.clip(((X - a[0]) * dx + (Y - a[1]) * dy) / L, 0.0, 1.0)
    return np.hypot(X - (a[0] + t * dx), Y - (a[1] + t * dy))


def ob_dist_np(X, Y, ob):
    """Signed distance from every grid point to the obstacle edge (negative inside)."""
    g = ob.g
    if g[0] == "seg":
        return _pt_seg_np(X, Y, g[1], g[2]) - g[3]
    pts = g[1]
    d = None
    inside = np.zeros(X.shape, bool)
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        e = _pt_seg_np(X, Y, pts[j], pts[i])
        d = e if d is None else np.minimum(d, e)
        if yi != yj:
            cond = ((yi > Y) != (yj > Y)) & (X < (xj - xi) * (Y - yi) / (yj - yi) + xi)
            inside ^= cond
        j = i
    return np.where(inside, -d, d)


def grid(bbox, pitch, max_cells=450000):
    x0, y0, x1, y1 = bbox
    while ((x1 - x0) / pitch + 1) * ((y1 - y0) / pitch + 1) > max_cells:
        pitch *= 1.25
    xs = np.arange(x0, x1 + 1e-9, pitch)
    ys = np.arange(y0, y1 + 1e-9, pitch)
    X, Y = np.meshgrid(xs, ys)
    return xs, ys, X, Y, pitch


def label(mask):
    """4-connected components of a boolean grid -> (labels int array, count). Iterative, no scipy."""
    lab = np.zeros(mask.shape, np.int32)
    n = 0
    H, W = mask.shape
    for sy in range(H):
        for sx in range(W):
            if mask[sy, sx] and not lab[sy, sx]:
                n += 1
                stack = [(sy, sx)]
                lab[sy, sx] = n
                while stack:
                    y, x = stack.pop()
                    for yy, xx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                        if 0 <= yy < H and 0 <= xx < W and mask[yy, xx] and not lab[yy, xx]:
                            lab[yy, xx] = n
                            stack.append((yy, xx))
    return lab, n


# ---- the model ---------------------------------------------------------------------------------------------
def _pts(poly):
    out = []
    for oi in range(poly.OutlineCount()):
        o = poly.Outline(oi)
        out.append([(o.CPoint(i).x * 1e-6, o.CPoint(i).y * 1e-6) for i in range(o.PointCount())])
    return out


def _fill_polys(poly):
    """SHAPE_POLY_SET fill -> [{'outline': pts, 'holes': [pts], 'bb': box}] (one entry per separate copper piece)."""
    out = []
    for oi in range(poly.OutlineCount()):
        o = poly.Outline(oi)
        pts = [(o.CPoint(i).x * 1e-6, o.CPoint(i).y * 1e-6) for i in range(o.PointCount())]
        holes = []
        for hi in range(poly.HoleCount(oi)):
            h = poly.Hole(oi, hi)
            holes.append([(h.CPoint(i).x * 1e-6, h.CPoint(i).y * 1e-6) for i in range(h.PointCount())])
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        out.append({"outline": pts, "holes": holes, "bb": (min(xs), min(ys), max(xs), max(ys))})
    return out


def pip_many(P, pts):
    """Vectorised even-odd point-in-polygon: P (n,2) array -> bool array."""
    x, y = P[:, 0], P[:, 1]
    inside = np.zeros(len(P), bool)
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if yi != yj:
            inside ^= ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / (yj - yi) + xi)
        j = i
    return inside


def in_fill(P, fp):
    ins = pip_many(P, fp["outline"])
    for h in fp["holes"]:
        ins &= ~pip_many(P, h)
    return ins


def _arc_pts(s, m, e, steps=10):
    ax, ay = s
    bx, by = m
    cx, cy = e
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [s, e]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, a1, am = (math.atan2(p[1] - uy, p[0] - ux) for p in (s, e, m))
    two = 2 * math.pi

    def ccw(a, b):
        return (b - a) % two
    if ccw(a0, am) <= ccw(a0, a1):
        sweep = ccw(a0, a1)
    else:
        sweep = -((a0 - a1) % two)
    return [(ux + r * math.cos(a0 + sweep * i / steps), uy + r * math.sin(a0 + sweep * i / steps)) for i in range(steps + 1)]


class Model:
    """Everything the geometry commands need, read once with pcbnew."""

    def __init__(self, path, constraints=None, refill=True):
        import pcbnew
        self.pcbnew = pcbnew
        self.path = path
        self.b = b = pcbnew.LoadBoard(path)
        mm = lambda v: v * 1e-6
        self.cu = [b.GetLayerName(i) for i in b.GetEnabledLayers().CuStack()]
        self.lid = {b.GetLayerName(i): i for i in b.GetEnabledLayers().CuStack()}
        ds = b.GetDesignSettings()
        self.ns = ds.m_NetSettings
        fl = {"clr": mm(ds.m_MinClearance), "track": mm(ds.m_TrackMinWidth), "via_dia": mm(ds.m_ViasMinSize),
              "via_drill": mm(ds.m_MinThroughDrill)}
        self.hole_clr, self.h2h, self.edge_clr = mm(ds.m_HoleClearance), mm(ds.m_HoleToHoleMin), mm(ds.m_CopperEdgeClearance)
        self.floors = dict(fl)
        self.floor_src = {"project": dict(fl)}
        if constraints:
            self._apply_constraints(constraints, path)
        self._classes = {}
        if refill:
            pcbnew.ZONE_FILLER(b).Fill(b.Zones())
        self.obs = []
        self.zones = []
        self._build()
        self._isl = {}

    def _apply_constraints(self, cpath, board_path):
        from . import constraints as C
        cons = C.load(cpath)
        r = (cons.get("route") or {})
        fab = C.fab_profile(cons, board=board_path)
        f = self.floors
        f["clr"] = max(f["clr"], float(r.get("min_clearance_mm", 0)), float(fab["min_clearance_mm"]))
        f["track"] = max(f["track"], float(r.get("min_track_mm", 0)), float(fab["min_track_mm"]))
        f["via_dia"] = max(f["via_dia"], float(fab["min_via_dia_mm"]))
        f["via_drill"] = max(f["via_drill"], float(fab["min_drill_mm"]))
        self.floor_src["constraints"] = {"route": {k: r[k] for k in r if k.startswith("min_")}, "fab_profile": fab.get("profile")}

    def net_rule(self, net):
        """Width / clearance / via of a net (class values, never below the floors)."""
        if net in self._classes:
            return self._classes[net]
        nc = self.ns.GetEffectiveNetClass(net)
        mm = lambda v: v * 1e-6
        f = self.floors
        d = {"class": nc.GetName(), "width": max(mm(nc.GetTrackWidth()), f["track"]),
             "clearance": max(mm(nc.GetClearance()), f["clr"]),
             "via_dia": max(mm(nc.GetViaDiameter()), f["via_dia"]),
             "via_drill": max(mm(nc.GetViaDrill()), f["via_drill"])}
        self._classes[net] = d
        return d

    # -- build --
    def _clr(self, net):
        return self.net_rule(net)["clearance"] if net else self.floors["clr"]

    def _build(self):
        pcbnew, b = self.pcbnew, self.b
        cu = self.cu
        allcu = tuple(cu)
        for fp in b.GetFootprints():
            ref = fp.GetReference()
            for pad in fp.Pads():
                net = pad.GetNetname()
                ds = pad.GetDrillSize()
                drill = (ds.x * 1e-6, ds.y * 1e-6)
                npth = pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
                pos = (pad.GetPosition().x * 1e-6, pad.GetPosition().y * 1e-6)
                lay = [l for l in cu if pad.IsOnLayer(self.lid[l])]
                locked = fp.IsLocked()
                if lay and not npth:
                    try:
                        polys = _pts(pad.GetEffectivePolygon(self.lid[lay[0]]))
                    except Exception:                                # noqa: BLE001
                        polys = []
                    sz = pad.GetSize()
                    shp = {0: "circle", 1: "rect", 2: "oval", 3: "trapezoid", 4: "roundrect", 5: "chamfered", 6: "custom"}.get(
                        int(pad.GetShape()), str(pad.GetShape()))
                    for poly in polys[:1]:
                        if len(poly) >= 3:
                            self.obs.append(Ob("pad", net, lay, ("poly", poly), self._clr(net), self.hole_clr, ref=f"{ref}.{pad.GetNumber()}",
                                               id=pad.m_Uuid.AsString(), locked=locked,
                                               extra={"pos": pos, "shape": shp, "size": (sz.x * 1e-6, sz.y * 1e-6), "hole": drill[0]}))
                if drill[0] > 0:
                    r = max(drill) / 2
                    hw = min(drill) / 2
                    a = b2 = pos
                    if abs(drill[0] - drill[1]) > 1e-6:
                        # slot: capsule along the long axis (rotation of the pad)
                        ang = math.radians(pad.GetOrientation().AsDegrees())
                        L = (max(drill) - min(drill)) / 2
                        ux, uy = (math.cos(ang), -math.sin(ang)) if drill[0] >= drill[1] else (math.sin(ang), math.cos(ang))
                        a = (pos[0] - L * ux, pos[1] - L * uy)
                        b2 = (pos[0] + L * ux, pos[1] + L * uy)
                        r = hw
                    self.obs.append(Ob("hole", "", allcu, ("seg", a, b2, r), self.hole_clr if npth else 0.0, self.h2h,
                                       ref=f"{ref}.{pad.GetNumber()}", id=pad.m_Uuid.AsString(), locked=locked,
                                       extra={"npth": npth}))
            for z in fp.Zones():
                self._zone(z)
            for gi in fp.GraphicalItems():
                if gi.GetLayer() == pcbnew.Edge_Cuts:
                    self._edge(gi)
        for t in b.GetTracks():
            cls = t.GetClass()
            net = t.GetNetname()
            tid = t.m_Uuid.AsString()
            if cls == "PCB_VIA":
                p = t.GetPosition()
                c = (p.x * 1e-6, p.y * 1e-6)
                dia = t.GetWidth() * 1e-6
                drill = t.GetDrillValue() * 1e-6
                self.obs.append(Ob("via", net, allcu, ("seg", c, c, dia / 2), self._clr(net), self.hole_clr, id=tid, locked=t.IsLocked(),
                                   extra={"dia": dia, "drill": drill}))
                self.obs.append(Ob("hole", "", allcu, ("seg", c, c, drill / 2), 0.0, self.h2h, id=tid, locked=t.IsLocked(), extra={"via": True}))
            else:
                lay = b.GetLayerName(t.GetLayer())
                w = t.GetWidth() * 1e-6
                if cls == "PCB_ARC":
                    pts = _arc_pts((t.GetStart().x * 1e-6, t.GetStart().y * 1e-6), (t.GetMid().x * 1e-6, t.GetMid().y * 1e-6),
                                   (t.GetEnd().x * 1e-6, t.GetEnd().y * 1e-6))
                else:
                    pts = [(t.GetStart().x * 1e-6, t.GetStart().y * 1e-6), (t.GetEnd().x * 1e-6, t.GetEnd().y * 1e-6)]
                for i in range(len(pts) - 1):
                    self.obs.append(Ob("track", net, [lay], ("seg", pts[i], pts[i + 1], w / 2), self._clr(net), self.hole_clr, id=tid,
                                       locked=t.IsLocked(), extra={"width": w, "arc": cls == "PCB_ARC"}))
        for z in b.Zones():
            self._zone(z)
        for d in b.GetDrawings():
            if d.GetLayer() == pcbnew.Edge_Cuts:
                self._edge(d)

    def _zone(self, z):
        cu = [l for l in self.cu if z.IsOnLayer(self.lid[l])]
        if not cu:
            return
        polys = _pts(z.Outline())
        if z.GetIsRuleArea():
            for poly in polys:
                if len(poly) >= 3:
                    self.obs.append(Ob("ko", "", cu, ("poly", poly), 0.0, 0.0, id=z.m_Uuid.AsString(), desc=z.GetZoneName() or "keepout",
                                       extra={"no_tracks": z.GetDoNotAllowTracks(), "no_vias": z.GetDoNotAllowVias(),
                                              "no_pads": z.GetDoNotAllowPads()}))
            return
        fills = {}
        for l in cu:
            try:
                fp = z.GetFilledPolysList(self.lid[l])
                fills[l] = _fill_polys(fp) if fp.OutlineCount() else []
            except Exception:                                        # noqa: BLE001
                fills[l] = []
        self.zones.append({"net": z.GetNetname(), "layers": cu, "outline": polys[0] if polys else [], "fills": fills,
                           "id": z.m_Uuid.AsString(), "name": z.GetZoneName(), "priority": z.GetAssignedPriority()})

    def _edge(self, d):
        pcbnew = self.pcbnew
        s = d.GetShape()
        P = lambda v: (v.x * 1e-6, v.y * 1e-6)
        pts, closed = [], False
        if s == pcbnew.SHAPE_T_SEGMENT:
            pts = [P(d.GetStart()), P(d.GetEnd())]
        elif s == pcbnew.SHAPE_T_RECT:
            a, c = P(d.GetStart()), P(d.GetEnd())
            pts, closed = [a, (c[0], a[1]), c, (a[0], c[1])], True
        elif s == pcbnew.SHAPE_T_ARC:
            pts = _arc_pts(P(d.GetStart()), P(d.GetArcMid()), P(d.GetEnd()), 16)
        elif s == pcbnew.SHAPE_T_CIRCLE:
            c = P(d.GetCenter())
            r = d.GetRadius() * 1e-6
            pts, closed = [(c[0] + r * math.cos(i * math.pi / 16), c[1] + r * math.sin(i * math.pi / 16)) for i in range(32)], True
        elif s == pcbnew.SHAPE_T_POLY:
            ps = _pts(d.GetPolyShape())
            pts, closed = (ps[0] if ps else []), True
        for i in range(len(pts) - (0 if closed else 1)):
            a, c = pts[i], pts[(i + 1) % len(pts)]
            self.obs.append(Ob("edge", "", self.cu, ("seg", a, c, 0.0), self.edge_clr, 0.0, id=d.m_Uuid.AsString()))

    # -- queries --
    def in_box(self, bb, layer=None):
        x0, y0, x1, y1 = bb
        for o in self.obs:
            if o.bb[2] < x0 or o.bb[0] > x1 or o.bb[3] < y0 or o.bb[1] > y1:
                continue
            if layer is not None and layer not in o.layers:
                continue
            yield o

    def need(self, net, ob, for_via=False):
        """Required edge-to-edge clearance between copper of `net` and `ob`."""
        return max(self.floors["clr"], self.net_rule(net)["clearance"] if net else 0.0, ob.clr)

    def foreign(self, ob, net, kind):
        """Does `ob` obstruct copper of `net` (kind 'track' or 'via')?"""
        if ob.kind in ("pad", "track", "via"):
            return ob.net != net or not ob.net
        if ob.kind == "ko":
            return ob.extra.get("no_tracks" if kind == "track" else "no_vias", False)
        if ob.kind == "edge":
            return True
        if ob.kind == "hole":
            return kind == "via" or ob.extra.get("npth")
        return False

    # -- same-net islands (pads, tracks, vias, zone fill) --
    def islands(self, net):
        """-> (list of island dicts {id, items:[Ob], zones:[(zone idx, layer)]}, map ob id -> island idx)."""
        if net in self._isl:
            return self._isl[net]
        items = [o for o in self.obs if o.net == net and o.kind in ("pad", "track", "via")]
        nodes = list(range(len(items)))
        par = list(nodes)

        def find(i):
            while par[i] != i:
                par[i] = par[par[i]]
                i = par[i]
            return i

        def union(i, j):
            par[find(i)] = find(j)
        for i, a in enumerate(items):
            for j in range(i + 1, len(items)):
                c = items[j]
                if a.bb[2] < c.bb[0] - 1e-3 or c.bb[2] < a.bb[0] - 1e-3 or a.bb[3] < c.bb[1] - 1e-3 or c.bb[3] < a.bb[1] - 1e-3:
                    continue
                if not (a.layers & c.layers):
                    continue
                if a.kind == "pad" and c.kind == "pad":
                    pass
                if _touch(a, c):
                    union(i, j)
        zpar = {}
        for zi, z in enumerate(self.zones):
            if z["net"] != net:
                continue
            for l in z["layers"]:
                for fi, fpoly in enumerate(z["fills"].get(l) or []):
                    zpar[(zi, l, fi)] = len(par)
                    par.append(len(par))
        if zpar:
            anchors = [(k, _anchors(o)) for k, o in enumerate(items)]
            for (zi, l, fi), node in zpar.items():
                fpoly = self.zones[zi]["fills"][l][fi]
                bx0, by0, bx1, by1 = fpoly["bb"]
                cand = [(k, a) for k, a in anchors if l in items[k].layers and items[k].bb[2] >= bx0 and items[k].bb[0] <= bx1 and items[k].bb[3] >= by0 and items[k].bb[1] <= by1]
                if not cand:
                    continue
                P = np.array([pt for k, a in cand for pt in a])
                ins = in_fill(P, fpoly)
                pos = 0
                for k, a in cand:
                    if ins[pos:pos + len(a)].any():
                        union(k, node)
                    pos += len(a)
        groups = {}
        for i in range(len(items)):
            groups.setdefault(find(i), {"items": [], "zones": []})["items"].append(items[i])
        for key, node in zpar.items():
            groups.setdefault(find(node), {"items": [], "zones": []})["zones"].append(key)
        isl = sorted(groups.values(), key=lambda g: (-len(g["items"]), g["items"][0].id if g["items"] else ""))
        for k, g in enumerate(isl):
            g["id"] = k
        omap = {}
        for g in isl:
            for o in g["items"]:
                omap.setdefault(o.id, g["id"])
        self._isl[net] = (isl, omap)
        return self._isl[net]


def _anchors(o):
    """Probe points of a copper item for zone-fill membership: centre plus (pads) their corner points."""
    g = o.g
    if g[0] == "seg":
        return [g[1], g[2]]
    return [_anchor(o)] + list(g[1][:: max(1, len(g[1]) // 8)])


def _anchor(o):
    g = o.g
    return g[1] if g[0] == "seg" else o.extra.get("pos") or (sum(p[0] for p in g[1]) / len(g[1]), sum(p[1] for p in g[1]) / len(g[1]))


def _touch(a, c):
    """Copper of two same-net obstacles touches (gap <= 0.001 mm)."""
    ga, gc = a.g, c.g
    if ga[0] == "seg" and gc[0] == "seg":
        return seg_seg(ga[1], ga[2], gc[1], gc[2])[0] - ga[3] - gc[3] <= 1e-3
    if ga[0] == "poly" and gc[0] == "poly":
        return any(pip(p, gc[1]) for p in ga[1]) or any(pip(p, ga[1]) for p in gc[1]) or \
            any(segs_cross(e0, e1, f0, f1) for e0, e1 in poly_edges(ga[1]) for f0, f1 in poly_edges(gc[1]))
    if ga[0] == "poly":
        ga, gc, a, c = gc, ga, c, a
    return ob_gap(ga[1], ga[2], ga[3], c)[0] <= 1e-3


# ---- path syntax -------------------------------------------------------------------------------------------
LAYER_ALIAS = {"F": "F.Cu", "B": "B.Cu", "TOP": "F.Cu", "BOT": "B.Cu", "BOTTOM": "B.Cu"}


def norm_layer(s, cu):
    s = s.strip()
    u = s.upper()
    if u in LAYER_ALIAS:
        return LAYER_ALIAS[u]
    for l in cu:
        if l.upper() == u or l.upper().replace(".CU", "") == u:
            return l
    raise ValueError(f"unknown layer {s!r}; copper layers: {', '.join(cu)}")


def parse_path(text, cu):
    """'F.Cu: 1,2 3,4 5,6; via 5,6; In2.Cu: 5,6 8,6' -> [('seg', layer, a, b) | ('via', pt)] (via sits between chunks)."""
    prims, last = [], None
    for tok in [t.strip() for t in text.split(";") if t.strip()]:
        if tok.lower().startswith("via"):
            m = re.findall(r"-?\d+(?:\.\d+)?", tok)
            if len(m) != 2:
                raise ValueError(f"bad via token {tok!r}")
            prims.append(("via", (float(m[0]), float(m[1]))))
            continue
        if ":" not in tok:
            raise ValueError(f"segment chunk needs 'LAYER:' prefix: {tok!r}")
        lay, rest = tok.split(":", 1)
        lay = norm_layer(lay, cu)
        pts = []
        for p in rest.replace("|", " ").split():
            xy = p.split(",")
            if len(xy) != 2:
                raise ValueError(f"bad point {p!r}")
            pts.append((float(xy[0]), float(xy[1])))
        if len(pts) < 2:
            raise ValueError(f"chunk needs >= 2 points: {tok!r}")
        for i in range(len(pts) - 1):
            prims.append(("seg", lay, pts[i], pts[i + 1]))
    return prims
