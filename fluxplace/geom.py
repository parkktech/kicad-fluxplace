"""Rigid-body geometry on the routediag board model: move/rotate a part (its pads
and courtyard move with it), rectangle tests, a small spatial hash."""
import math
from collections import defaultdict

from . import boardfile as BF


def rot(x, y, deg):
    return BF._rot(x, y, deg)


def pad_extent(w, h, ang):
    r = math.radians(ang)
    return (abs(w * math.cos(r)) / 2 + abs(h * math.sin(r)) / 2,
            abs(w * math.sin(r)) / 2 + abs(h * math.cos(r)) / 2)


def moved(part, x, y, ang):
    """(pads, court) of `part` as if its origin were at (x, y) with footprint
    angle `ang`. Pads are copies; the original part is untouched."""
    px, py = part["pos"]
    d = ang - part["angle"]
    pads = []
    for p in part["pads"]:
        rx, ry = rot(p["x"] - px, p["y"] - py, d)
        q = dict(p)
        q["x"], q["y"] = x + rx, y + ry
        q["angle"] = p.get("angle", 0.0) + d
        q["ex"], q["ey"] = pad_extent(p["w"], p["h"], q["angle"])
        pads.append(q)
    x0, y0, x1, y1 = part["court"]
    cs = [rot(cx - px, cy - py, d) for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    court = (x + min(c[0] for c in cs), y + min(c[1] for c in cs),
             x + max(c[0] for c in cs), y + max(c[1] for c in cs))
    return pads, court


def place(part, x, y, ang):
    """Commit a move on the board model in place (pads and courtyard follow)."""
    pads, court = moved(part, x, y, ang)
    for old, new in zip(part["pads"], pads):      # in place: B["pads"] shares these dicts
        old.update(new)
    part["court"], part["pos"], part["angle"] = court, (x, y), ang


def overlap(a, b, gap=0.0):
    return a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap


def inside(r, outer, eps=1e-3):
    return (r[0] >= outer[0] - eps and r[1] >= outer[1] - eps
            and r[2] <= outer[2] + eps and r[3] <= outer[3] + eps)


def poly_has(pts, x, y):
    ins, j = False, len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            ins = not ins
        j = i
    return ins


class Hash:
    """Spatial hash of rectangles keyed by (ref, side)."""

    def __init__(self, cell=5.0):
        self.cell, self.g, self.r = cell, defaultdict(set), {}

    def _cells(self, r):
        c = self.cell
        for i in range(int(math.floor(r[0] / c)), int(math.floor(r[2] / c)) + 1):
            for j in range(int(math.floor(r[1] / c)), int(math.floor(r[3] / c)) + 1):
                yield i, j

    def add(self, key, rect, side="F"):
        self.remove(key)
        self.r[key] = (rect, side)
        for c in self._cells(rect):
            self.g[c].add(key)

    def remove(self, key):
        if key in self.r:
            for c in self._cells(self.r[key][0]):
                self.g[c].discard(key)
            del self.r[key]

    def hits(self, rect, side="F", gap=0.0, ignore=()):
        g = (rect[0] - gap, rect[1] - gap, rect[2] + gap, rect[3] + gap)
        seen = set()
        for c in self._cells(g):
            for k in sorted(self.g.get(c, ()), key=str):      # deterministic: no set-order dependence
                if k in seen or k in ignore:
                    continue
                seen.add(k)
                r, s = self.r[k]
                if s == side and overlap(r, rect, gap):
                    yield k


# ---- board outline (Edge.Cuts segments, arcs flattened; even-odd region) -----------------
def seg_hits_rect(a, b, r):
    """Does segment ab touch rectangle r (x0, y0, x1, y1)? Liang-Barsky."""
    x0, y0 = a
    dx, dy = b[0] - x0, b[1] - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - r[0]), (dx, r[2] - x0), (-dy, y0 - r[1]), (dy, r[3] - y0)):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return t0 <= t1


def edge_has(edge, x, y):
    """Even-odd point-in-outline over a list of segments."""
    ins = False
    for (ax, ay), (bx, by) in edge:
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            ins = not ins
    return ins


def in_outline(B, rect, margin=0.0):
    """True when `rect` lies inside the board outline with at least `margin` mm to every
    outline segment. Rounded corners and cut-outs count; falls back to the bounding box
    when the board has no Edge.Cuts segments."""
    edge = B.get("edge")
    if not edge:
        bx = B["bbox"]
        return inside(rect, (bx[0] + margin, bx[1] + margin, bx[2] - margin, bx[3] - margin))
    bx = B["bbox"]
    if not inside(rect, (bx[0] + margin - 1e-3, bx[1] + margin - 1e-3, bx[2] - margin + 1e-3, bx[3] - margin + 1e-3)):
        return False
    infl = (rect[0] - margin, rect[1] - margin, rect[2] + margin, rect[3] + margin)
    for a, b in edge:
        if seg_hits_rect(a, b, infl):
            return False
    return edge_has(edge, (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
