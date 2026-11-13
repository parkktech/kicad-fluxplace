"""Exact 2-D clearance for laid copper (the `pairs` stage): segment / via against pads, tracks, vias, rule areas, board edge.

`Items` is a bucket index of everything on the board with its net and layers.  `gap_seg` / `gap_via` return the smallest
copper-to-copper gap of a new track segment or via to a FOREIGN item (other net) and that item; the grid search of
pairlay.py proposes geometry, this module decides.  Pad shapes: rect (rotated), oval / circle exact; roundrect as its
bounding rect (conservative).  `offset` gives the mitred parallel of a polyline.  Pure Python, no pcbnew."""
import math

from . import geom as GM
from . import pocket as PK
from . import routeaudit as RA


def pt_seg(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 < 1e-18 else min(1.0, max(0.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def crosses(a, b, c, d):
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    return ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0)) and (o1 != 0 or o2 != 0)


def seg_seg(a, b, c, d):
    """distance between segments ab and cd (0 when they cross)."""
    if crosses(a, b, c, d):
        return 0.0
    return min(pt_seg(a, c, d), pt_seg(b, c, d), pt_seg(c, a, b), pt_seg(d, a, b))


def seg_box(a, b, hw, hh):
    """distance from segment ab to the axis-aligned box [-hw, hw] x [-hh, hh]."""
    if (abs(a[0]) <= hw and abs(a[1]) <= hh) or (abs(b[0]) <= hw and abs(b[1]) <= hh):
        return 0.0
    c = ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
    return min(seg_seg(a, b, c[k], c[(k + 1) % 4]) for k in range(4))


def length(pts):
    return sum(math.hypot(pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1]) for k in range(len(pts) - 1))


def clean(pts, eps=1e-6):
    out = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > eps:
            out.append(p)
    return out


def left(t):
    """the normal on the LEFT of travel t (cross(t, left) > 0 in board coordinates)."""
    return (-t[1], t[0])


def unit(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy) or 1.0
    return (dx / L, dy / L)


def offset(pts, s, t0=None):
    """polyline moved by s to the left of travel, mitred joins.  One point needs the travel direction t0."""
    n = len(pts)
    if n == 1:
        nn = left(t0)
        return [(pts[0][0] + nn[0] * s, pts[0][1] + nn[1] * s)]
    out = []
    for i in range(n):
        if i == 0 or i == n - 1:
            nn = left(unit(pts[0], pts[1]) if i == 0 else unit(pts[-2], pts[-1]))
            out.append((pts[i][0] + nn[0] * s, pts[i][1] + nn[1] * s))
            continue
        n1, n2 = left(unit(pts[i - 1], pts[i])), left(unit(pts[i], pts[i + 1]))
        bx, by = n1[0] + n2[0], n1[1] + n2[1]
        bl = math.hypot(bx, by) or 1.0
        bx, by = bx / bl, by / bl
        k = s / max(bx * n1[0] + by * n1[1], 0.3)
        out.append((pts[i][0] + bx * k, pts[i][1] + by * k))
    return out


class Items:
    def __init__(self, copper, cell=1.0):
        self.copper, self.cell, self.it, self.h = list(copper), cell, [], {}

    def _add(self, it, x0, y0, x1, y1):
        k = len(self.it)
        c = self.cell
        it["bk"] = [(i, j) for i in range(int(math.floor(x0 / c)), int(math.floor(x1 / c)) + 1)
                    for j in range(int(math.floor(y0 / c)), int(math.floor(y1 / c)) + 1)]
        self.it.append(it)
        for b in it["bk"]:
            self.h.setdefault(b, []).append(k)
        return k

    def mark(self):
        return len(self.it)

    def rollback(self, mark):
        for it in self.it[mark:]:
            for b in it["bk"]:
                self.h[b] = [k for k in self.h[b] if k < mark]
        del self.it[mark:]

    def retire(self, nets=None, which=None):
        """old (fan-out) copper that leaves the board stops being an obstacle: all of `nets`, or the routediag items in `which`."""
        ids = {id(o) for o in which} if which is not None else None
        for it in self.it:
            if it["old"] and ((nets and it["net"] in nets) or (ids and id(it.get("src")) in ids)):
                it["dead"] = True

    def pad(self, p):
        r = math.hypot(p["w"], p["h"]) / 2
        self._add({"k": "pad", "p": p, "net": p["net"], "layers": frozenset(self.copper if p.get("np") else p["layers"]), "old": False,
                   "label": f"pad {p['ref']}.{p['num']}" + (f" ({p['net']})" if p["net"] else "")},
                  p["x"] - r, p["y"] - r, p["x"] + r, p["y"] + r)

    def seg(self, a, b, w, layer, net, old=False, extra=0.0, label=None, src=None):
        return self._add({"k": "seg", "a": a, "b": b, "w": w, "net": net, "src": src, "layers": frozenset([layer]) if layer else frozenset(self.copper),
                          "old": old, "extra": extra, "label": label or f"track {net} {layer}"},
                         min(a[0], b[0]) - w / 2, min(a[1], b[1]) - w / 2, max(a[0], b[0]) + w / 2, max(a[1], b[1]) + w / 2)

    def via(self, x, y, d, net, old=False, layers=None, src=None):
        return self._add({"k": "via", "c": (x, y), "d": d, "net": net, "src": src, "layers": frozenset(layers or self.copper), "old": old,
                          "label": f"via {net}"}, x - d / 2, y - d / 2, x + d / 2, y + d / 2)

    def poly(self, pts, layers, label):
        xs, ys = [q[0] for q in pts], [q[1] for q in pts]
        self._add({"k": "poly", "pts": list(pts), "net": None, "layers": frozenset(layers or self.copper), "old": False, "label": label},
                  min(xs), min(ys), max(xs), max(ys))

    @classmethod
    def of_board(cls, B, old_nets=(), edge_extra=0.0):
        """every pad, track, via, track-forbidding rule area and edge of a routediag board; copper of `old_nets` is tagged old."""
        I = cls(B["copper"])
        for p in B["pads"]:
            I.pad(p)
        for s in B["segs"]:
            I.seg(s["a"], s["b"], s["w"], s["layer"], s["net"], old=s["net"] in old_nets, src=s)
        for v in B["vias"]:
            I.via(v["x"], v["y"], v["d"], v["net"], old=v["net"] in old_nets, layers=RA._expand_layers(v, B["copper"]), src=v)
        for z in B["zones"]:
            if z["keepout"] and z["no_tracks"] and len(z["pts"]) >= 3:
                I.poly(z["pts"], [l for l in z["layers"] if l in B["copper"]], f"rule area {z.get('name') or z.get('owner') or ''}".strip())
        for a, b in B.get("edge", []):
            I.seg(a, b, 0.0, None, None, extra=edge_extra, label="board edge")
        return I

    def _near(self, x0, y0, x1, y1):
        c = self.cell
        seen = set()
        for i in range(int(math.floor(x0 / c)), int(math.floor(x1 / c)) + 1):
            for j in range(int(math.floor(y0 / c)), int(math.floor(y1 / c)) + 1):
                for k in self.h.get((i, j), ()):
                    if k not in seen:
                        seen.add(k)
                        yield self.it[k]

    @staticmethod
    def _dist(it, a, b):
        """distance from the centre line ab to the item's copper edge."""
        k = it["k"]
        if k == "seg":
            return seg_seg(a, b, it["a"], it["b"]) - it["w"] / 2 - it.get("extra", 0.0)
        if k == "via":
            return pt_seg(it["c"], a, b) - it["d"] / 2
        if k == "pad":
            p = it["p"]
            la = PK._rot(a[0] - p["x"], a[1] - p["y"], p.get("angle"))
            lb = PK._rot(b[0] - p["x"], b[1] - p["y"], p.get("angle"))
            w, h = p["w"] / 2, p["h"] / 2
            if p.get("shape") in ("oval", "circle"):
                r = min(w, h)
                ax = ((-(w - r), 0.0), (w - r, 0.0)) if w >= h else ((0.0, -(h - r)), (0.0, h - r))
                return seg_seg(la, lb, ax[0], ax[1]) - r
            return seg_box(la, lb, w, h)
        pts = it["pts"]
        if GM.poly_has(pts, a[0], a[1]) or GM.poly_has(pts, b[0], b[1]):
            return -1.0
        return min(seg_seg(a, b, pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts)))

    def gap_seg(self, a, b, w, layer, net, need, skip_old=()):
        """(smallest gap, item) of a new track to foreign copper on its layer; only gaps under `need` are searched."""
        r = w / 2 + need + 0.6
        best, who = 1e9, None
        for it in self._near(min(a[0], b[0]) - r, min(a[1], b[1]) - r, max(a[0], b[0]) + r, max(a[1], b[1]) + r):
            if layer not in it["layers"] or (it["net"] and it["net"] == net) or (it["old"] and (it["net"] in skip_old or it.get("dead"))):
                continue
            g = self._dist(it, a, b) - w / 2
            if g < best:
                best, who = g, it
        return best, who

    def gap_via(self, x, y, d, net, need, skip_old=()):
        r = d / 2 + need + 0.6
        best, who = 1e9, None
        for it in self._near(x - r, y - r, x + r, y + r):
            if (it["net"] and it["net"] == net) or (it["old"] and (it["net"] in skip_old or it.get("dead"))):
                continue
            g = self._dist(it, (x, y), (x, y)) - d / 2
            if g < best:
                best, who = g, it
        return best, who
