"""Clearance field around a spot of a board: what a track or a via may touch, per copper layer.

`Field(B, (x, y), half)` rasters a window of the parsed board (routediag.load) at `step` mm.  Per copper layer it
holds the DISTANCE from each cell centre to the edge of the nearest foreign item (pad, track, via, hole, keep-out,
board edge; nets in `own` and `ignore` items never count) and which item that was.  From that:

  free(layer, w)       cells where a track of width w can run at the design clearance
  via_ok(d)            cells where a through via of diameter d fits on EVERY copper layer
  flood(mask, seeds)   cells reachable from the seeds through the mask (the "pocket")
  dist(mask, seeds)    path length in mm to every cell (octile chamfer, exact enough at 0.05 mm)

Pad shapes are exact for rect / oval / circle (roundrect is taken as a rect: conservative).  Used by closure.py and the
escape reservation (escres.py).  Pure numpy; no pcbnew."""
import math

import numpy as np

from . import routeaudit as RA

CAP = 3.0                      # distances are clipped here (mm)
INF = np.float32(1e9)


def _rot(dx, dy, ang):
    r = math.radians(-(ang or 0.0))
    c, s = math.cos(r), math.sin(r)
    return dx * c - dy * s, dx * s + dy * c


def pad_sdf(p, X, Y):
    """signed distance from points to the pad edge (negative inside)."""
    lx, ly = _rot(X - p["x"], Y - p["y"], p.get("angle"))
    w, h = p["w"] / 2, p["h"] / 2
    if p.get("shape") in ("oval", "circle"):
        r = min(w, h)
        if w >= h:
            ax = np.clip(lx, -(w - r), w - r)
            return np.hypot(lx - ax, ly) - r
        ay = np.clip(ly, -(h - r), h - r)
        return np.hypot(lx, ly - ay) - r
    qx, qy = np.abs(lx) - w, np.abs(ly) - h
    return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0)


def seg_sdf(a, b, w, X, Y):
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = np.clip(((X - ax) * dx + (Y - ay) * dy) / L2, 0, 1) if L2 > 1e-12 else 0.0
    return np.hypot(X - (ax + t * dx), Y - (ay + t * dy)) - w / 2


def poly_mask(pts, X, Y):
    """even-odd point-in-polygon, vectorised."""
    ins = np.zeros(X.shape, bool)
    n = len(pts)
    for i in range(n):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
        if y1 == y2:
            continue
        cond = (y1 > Y) != (y2 > Y)
        xi = (x2 - x1) * (Y - y1) / (y2 - y1) + x1
        ins ^= cond & (X < xi)
    return ins


class Field:
    def __init__(self, B, center, half=5.0, step=0.05, own=(), ignore=(), clearance=None, edge_clearance=0.3,
                 skip_refs=(), extra_pads=()):
        self.B, self.step, self.own = B, step, set(own)
        self.clr = float(clearance if clearance is not None else B.get("clearance", 0.127))
        cx, cy = center
        self.x0, self.y0 = cx - half, cy - half
        self.nx = self.ny = int(round(2 * half / step))
        xs = self.x0 + (np.arange(self.nx) + 0.5) * step
        ys = self.y0 + (np.arange(self.ny) + 0.5) * step
        self.X, self.Y = np.meshgrid(xs, ys)
        self.copper = list(B["copper"])
        self.dist = {l: np.full((self.ny, self.nx), CAP, np.float32) for l in self.copper}
        self.owner = {l: np.full((self.ny, self.nx), -1, np.int32) for l in self.copper}
        self.items = []                       # (kind, key, net, label)
        self.ignore = set(ignore)
        self._build(edge_clearance, set(skip_refs), list(extra_pads))

    # -- construction
    def _sl(self, x0, y0, x1, y1, pad=CAP):
        i0 = max(0, int((x0 - pad - self.x0) / self.step))
        i1 = min(self.nx, int((x1 + pad - self.x0) / self.step) + 1)
        j0 = max(0, int((y0 - pad - self.y0) / self.step))
        j1 = min(self.ny, int((y1 + pad - self.y0) / self.step) + 1)
        return (slice(j0, j1), slice(i0, i1)) if i1 > i0 and j1 > j0 else None

    def _put(self, layers, sl, sdf, idx):
        for l in layers:
            if l not in self.dist:
                continue
            d, o = self.dist[l][sl], self.owner[l][sl]
            m = sdf < d
            d[m] = sdf[m]
            o[m] = idx

    def _add(self, kind, key, net, label):
        self.items.append((kind, key, net, label))
        return len(self.items) - 1

    def _build(self, edge_clr, skip_refs, extra_pads):
        B = self.B
        x1, y1 = self.x0 + self.nx * self.step, self.y0 + self.ny * self.step
        for p in list(B["pads"]) + extra_pads:
            if p["ref"] in skip_refs and p not in extra_pads:
                continue
            if p["net"] and p["net"] in self.own:
                continue
            if p["x"] < self.x0 - CAP - p["ex"] or p["x"] > x1 + CAP + p["ex"] or p["y"] < self.y0 - CAP - p["ey"] \
                    or p["y"] > y1 + CAP + p["ey"]:
                continue
            sl = self._sl(p["x"] - p["ex"], p["y"] - p["ey"], p["x"] + p["ex"], p["y"] + p["ey"])
            if sl:
                i = self._add("pad", (p["ref"], p["num"]), p["net"], f"{p['ref']}.{p['num']}")
                self._put(p["layers"] if not p.get("np") else self.copper, sl, pad_sdf(p, self.X[sl], self.Y[sl]), i)
        for k, s in enumerate(B["segs"]):
            if s["net"] in self.own or ("seg", k) in self.ignore:
                continue
            sl = self._sl(min(s["a"][0], s["b"][0]), min(s["a"][1], s["b"][1]), max(s["a"][0], s["b"][0]),
                          max(s["a"][1], s["b"][1]))
            if sl:
                i = self._add("seg", k, s["net"], "")
                self._put([s["layer"]], sl, seg_sdf(s["a"], s["b"], s["w"], self.X[sl], self.Y[sl]), i)
        for k, v in enumerate(B["vias"]):
            if v["net"] in self.own or ("via", k) in self.ignore:
                continue
            sl = self._sl(v["x"], v["y"], v["x"], v["y"])
            if sl:
                i = self._add("via", k, v["net"], "")
                self._put(RA._expand_layers(v, self.copper), sl,
                          np.hypot(self.X[sl] - v["x"], self.Y[sl] - v["y"]) - v["d"] / 2, i)
        for z in B["zones"]:
            if not z["keepout"] or not z["no_tracks"] or len(z["pts"]) < 3:
                continue
            xs, ys = [q[0] for q in z["pts"]], [q[1] for q in z["pts"]]
            sl = self._sl(min(xs), min(ys), max(xs), max(ys), 0.0)
            if sl:
                i = self._add("keepout", z.get("name") or z.get("owner") or "keep-out", "", "keep-out")
                inside = poly_mask(z["pts"], self.X[sl], self.Y[sl])
                self._put(z["layers"] or self.copper, sl, np.where(inside, -1.0, CAP).astype(np.float32), i)
        for (a, b) in B.get("edge", []):
            sl = self._sl(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
            if sl:
                i = self._add("edge", None, "", "board edge")
                self._put(self.copper, sl, seg_sdf(a, b, 0.0, self.X[sl], self.Y[sl]) - (edge_clr - self.clr), i)

    def block_via(self, x, y, d, net=""):
        """a via reserved/claimed elsewhere: foreign to everything but its own net (all copper layers)."""
        if net in self.own:
            return
        sl = self._sl(x, y, x, y)
        if sl:
            self._put(self.copper, sl, np.hypot(self.X[sl] - x, self.Y[sl] - y) - d / 2, self._add("via", None, net, "reserved"))

    def block_seg(self, a, b, w, layer, net=""):
        if net in self.own:
            return
        sl = self._sl(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
        if sl:
            self._put([layer], sl, seg_sdf(a, b, w, self.X[sl], self.Y[sl]), self._add("seg", None, net, "reserved"))

    # -- queries
    def cell(self, x, y):
        return int((y - self.y0) / self.step), int((x - self.x0) / self.step)

    def xy(self, j, i):
        return self.x0 + (i + 0.5) * self.step, self.y0 + (j + 0.5) * self.step

    def free(self, layer, width):
        return self.dist[layer] >= self.clr + width / 2

    def via_ok(self, d, layers=None):
        m = np.ones((self.ny, self.nx), bool)
        for l in (layers or self.copper):
            m &= self.dist[l] >= self.clr + d / 2
        return m

    def shape_mask(self, p, grow=0.0):
        """cells inside pad p (grown by `grow`) as a full-window bool array."""
        out = np.zeros((self.ny, self.nx), bool)
        sl = self._sl(p["x"] - p["ex"], p["y"] - p["ey"], p["x"] + p["ex"], p["y"] + p["ey"], grow + self.step)
        if sl:
            out[sl] = pad_sdf(p, self.X[sl], self.Y[sl]) <= grow
        return out

    def seg_mask(self, s, grow=0.0):
        out = np.zeros((self.ny, self.nx), bool)
        sl = self._sl(min(s["a"][0], s["b"][0]), min(s["a"][1], s["b"][1]), max(s["a"][0], s["b"][0]),
                      max(s["a"][1], s["b"][1]), grow + s["w"] + self.step)
        if sl:
            out[sl] = seg_sdf(s["a"], s["b"], s["w"], self.X[sl], self.Y[sl]) <= grow
        return out

    def via_mask(self, v, grow=0.0):
        out = np.zeros((self.ny, self.nx), bool)
        sl = self._sl(v["x"], v["y"], v["x"], v["y"], v["d"] / 2 + grow + self.step)
        if sl:
            out[sl] = np.hypot(self.X[sl] - v["x"], self.Y[sl] - v["y"]) <= v["d"] / 2 + grow
        return out

    def inside(self, x, y):
        return self.x0 <= x < self.x0 + self.nx * self.step and self.y0 <= y < self.y0 + self.ny * self.step


def _shift(a, dy, dx, fill):
    out = np.full_like(a, fill)
    ys = slice(max(dy, 0), a.shape[0] + min(dy, 0))
    yd = slice(max(-dy, 0), a.shape[0] + min(-dy, 0))
    xs = slice(max(dx, 0), a.shape[1] + min(dx, 0))
    xd = slice(max(-dx, 0), a.shape[1] + min(-dx, 0))
    out[ys, xs] = a[yd, xd]
    return out


def dist(mask, seeds, step):
    """path length (mm) from the seed cells to every cell of `mask` through `mask` (inf elsewhere)."""
    d = np.full(mask.shape, INF, np.float32)
    d[seeds & mask] = 0.0
    w1, w2 = np.float32(step), np.float32(step * math.sqrt(2))
    dirs = [(0, 1, w1), (0, -1, w1), (1, 0, w1), (-1, 0, w1), (1, 1, w2), (1, -1, w2), (-1, 1, w2), (-1, -1, w2)]
    for _ in range(2 * max(mask.shape)):
        nd = d
        for dy, dx, w in dirs:
            nd = np.minimum(nd, _shift(d, dy, dx, INF) + w)
        nd = np.where(mask, nd, INF)
        if np.array_equal(nd, d):
            break
        d = nd
    return d


def flood(mask, seeds):
    d = dist(mask, seeds, 1.0)
    return d < INF / 2


def area(m, step):
    return float(m.sum()) * step * step


def thin(m, step, every=0.2):
    """representative cells of a bool mask on a grid of `every` mm."""
    k = max(1, int(round(every / step)))
    out = np.zeros_like(m)
    out[::k, ::k] = m[::k, ::k]
    return out
