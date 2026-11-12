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


def inside(r, outer):
    return r[0] >= outer[0] and r[1] >= outer[1] and r[2] <= outer[2] and r[3] <= outer[3]


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
            for k in self.g.get(c, ()):
                if k in seen or k in ignore:
                    continue
                seen.add(k)
                r, s = self.r[k]
                if s == side and overlap(r, rect, gap):
                    yield k
