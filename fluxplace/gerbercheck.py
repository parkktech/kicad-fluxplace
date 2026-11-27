"""Copper DFM check on the EXPORTED fab package, independent of the board file.

`fab.emit` writes Gerbers and Excellon drills with kicad-cli. This module
re-reads those files and checks what the fab will actually receive:

  GERBER_MIN_GAP     copper of two different connected regions on one layer closer
                     than [fab] min_clearance_mm (rasterized, see RESOLUTION).
                     WARN by default; FAIL with [fab] gerber_gap = "fail"
  GERBER_MIN_WIDTH   drawn trace aperture narrower than [fab] min_track_mm (exact)
  GERBER_ANNULAR     plated hole whose outer-layer copper ring is below
                     [fab] min_annular_mm (exact, from flash apertures)
  GERBER_DRILL_NO_PAD (WARN) plated hole with no copper under it on any layer
  GERBER_LAYER_MISSING  expected copper/mask/silk/edge layer absent (or Edge.Cuts empty)
  GERBER_LAYER_EMPTY    present but no features: copper WARN, mask/silk INFO
  GERBER_EDGE_OPEN   Edge.Cuts outline does not close on itself

Thresholds come from the constraints' [fab] table (fab_profile); nothing here
is board-specific. The board file is read ONLY for its copper layer count
(the one fact an export cannot tell you is how many layers were meant).

RESOLUTION: copper gaps are measured on a raster of `res_mm` pixels (default
0.02 mm). A gap is reported when its estimate (pixel distance - 1) * res is
below min_clearance - res, so the check never fires on copper that is clean
by a pixel, and it can MISS gaps narrower than one pixel (they read as touching,
so they merge into one region). Min-width, annular and edge checks are exact,
computed from the Gerber/Excellon coordinates, not the raster.

Coordinates: kicad-cli writes Gerber/Excellon Y-up, i.e. Y = -(board file Y).
Findings report the Gerber/fab coordinates.

Supported Gerber subset (what kicad-cli emits with --no-x2): FS L/T with
absolute coordinates, MOMM/MOIN, LPD/LPC, C/R/O/macro apertures (primitives
1, 4, 20, 21; exposure-0 primitives ignored), D01/D02/D03, G01/G02/G03 with
G74/G75, G36/G37 regions (even-odd fill). Arcs are expanded to polylines of
~0.05 mm chords. Excellon: METRIC/INCH, decimal coordinates, T-tool C-diameters.
"""
import math
import os
import re
import time

import numpy as np

from fluxplace import limits as LM

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"
RESOLUTION_MM = 0.02
ARC_CHORD_MM = 0.05

# Protel/KiCad extension -> layer, used when the filename stem is not KiCad-style.
_EXT = {"gtl": "F_Cu", "gbl": "B_Cu", "gts": "F_Mask", "gbs": "B_Mask",
        "gto": "F_Silkscreen", "gbo": "B_Silkscreen", "gtp": "F_Paste",
        "gbp": "B_Paste", "gm1": "Edge_Cuts", "gko": "Edge_Cuts"}
_STEM = re.compile(r"-((?:F|B|In\d+)_(?:Cu|Mask|Silkscreen|Paste)|Edge_Cuts)\.")


def _f(level, code, msg, layer=None, x=None, y=None):
    return {"level": level, "code": code, "msg": msg, "layer": layer, "x": x, "y": y}


def classify(name):
    """Layer name for an exported Gerber file name, or None."""
    m = _STEM.search(name)
    if m:
        return m.group(1)
    ext = name.rsplit(".", 1)[-1].lower()
    if ext in _EXT:
        return _EXT[ext]
    m = re.fullmatch(r"g(\d+)", ext)
    if m:
        return "In%s_Cu" % m.group(1)
    return None


def board_copper_layers(board_path):
    """Copper layer count declared by a .kicad_pcb (layers table), or None."""
    try:
        txt = open(board_path, errors="replace").read()
    except OSError:
        return None
    names = set(re.findall(r'\(\d+ "(F\.Cu|B\.Cu|In\d+\.Cu)" ', txt))
    return len(names) if names else None


# ------------------------------------------------------------------ Gerber
_MACRO = re.compile(r"%AM([A-Za-z_][\w.]*)\*(.*?)%", re.S)
_ALLOWED = re.compile(r"[\d\.\+\-\*/\(\)eE\s]+")


def _eval(expr, env):
    s = re.sub(r"\$(\d+)", lambda m: "(%r)" % env.get(int(m.group(1)), 0.0), expr.strip())
    s = s.replace("X", "*").replace("x", "*")
    if not _ALLOWED.fullmatch(s):
        raise ValueError("bad macro expression: " + expr)
    return float(eval(s, {"__builtins__": {}}, {}))


def _rot(pts, deg):
    if not deg:
        return pts
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [(x * c - y * s, x * s + y * c) for x, y in pts]


def _macro_shapes(body, params):
    """Shapes for one macro aperture: ('circle', d) or ('poly', pts), local coords."""
    env = {i + 1: v for i, v in enumerate(params)}
    shapes = []
    for stmt in body.split("*"):
        stmt = stmt.strip()
        if not stmt or stmt[0] == "0":
            continue
        f = [v for v in stmt.split(",")]
        code = int(f[0])
        v = [_eval(t, env) for t in f[1:]]
        if not v or v[0] == 0:            # exposure 0 (clear) primitive: ignored
            continue
        if code == 1:
            shapes.append(("circle", v[1], v[2], v[3]))
        elif code == 20:
            w, x1, y1, x2, y2 = v[1], v[2], v[3], v[4], v[5]
            rot = v[6] if len(v) > 6 else 0
            dx, dy = x2 - x1, y2 - y1
            L = math.hypot(dx, dy) or 1e-9
            nx, ny = -dy / L * w / 2, dx / L * w / 2
            pts = [(x1 + nx, y1 + ny), (x2 + nx, y2 + ny), (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)]
            shapes.append(("poly", _rot(pts, rot)))
        elif code == 21:
            w, h, cx, cy = v[1], v[2], v[3], v[4]
            rot = v[5] if len(v) > 5 else 0
            pts = [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2),
                   (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]
            shapes.append(("poly", _rot(pts, rot)))
        elif code == 4:
            n = int(v[1])
            pts = [(v[2 + 2 * i], v[3 + 2 * i]) for i in range(n)]
            rot = v[2 + 2 * (n + 1)] if len(v) > 2 + 2 * (n + 1) else 0
            shapes.append(("poly", _rot(pts, rot)))
    return shapes


def _shape_min_dim(shapes):
    """Smaller side of the bounding box of the WHOLE aperture (all primitives
    united). A roundrect pad is a 1.5 mm box plus corner circles of 0.5 mm: the
    box is 2.0 mm across, so 2.0 is its size, not the 0.5 of one corner circle."""
    xs, ys = [], []
    for s in shapes:
        if s[0] == "circle":
            d, cx, cy = s[1], s[2], s[3]
            xs += [cx - d / 2, cx + d / 2]
            ys += [cy - d / 2, cy + d / 2]
        else:
            xs += [p[0] for p in s[1]]
            ys += [p[1] for p in s[1]]
    if not xs:
        return 0.0
    return min(max(xs) - min(xs), max(ys) - min(ys))


def _obround(w, h, n=24):
    r = min(w, h) / 2
    if w >= h:
        cx = (w - h) / 2
        pts = [(cx + r * math.cos(a), r * math.sin(a)) for a in
               [math.pi / 2 * k / n for k in range(n + 1)] + [-math.pi / 2 + math.pi * k / n for k in range(n + 1)]]
    else:
        cy = (h - w) / 2
        pts = [(r * math.cos(a), cy + r * math.sin(a)) for a in
               [math.pi * k / n for k in range(n + 1)] + [math.pi + math.pi * k / n for k in range(n + 1)]]
    return pts


def _arc_points(p0, p1, i, j, cw, scale_done=True):
    cx, cy = p0[0] + i, p0[1] + j
    r = math.hypot(p0[0] - cx, p0[1] - cy)
    if r < 1e-9:
        return [p1]
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    if math.hypot(p1[0] - p0[0], p1[1] - p0[1]) < 1e-9:
        sweep = -2 * math.pi if cw else 2 * math.pi
    elif cw:
        sweep = -((a0 - a1) % (2 * math.pi))
    else:
        sweep = (a1 - a0) % (2 * math.pi)
    n = max(2, int(math.ceil(abs(sweep) * r / ARC_CHORD_MM)))
    return [(cx + r * math.cos(a0 + sweep * k / n), cy + r * math.sin(a0 + sweep * k / n))
            for k in range(1, n + 1)]


class GerberUnsupported(Exception):
    """A construct this reader cannot scale or draw exactly. Reported as GERBER_UNSUPPORTED."""


def parse_gerber(path):
    """-> list of objects, mm units:
       ('draw', pol, ap, [pts]) ; ('flash', pol, ap, (x, y)) ; ('region', pol, [contours])
       ap = ('C', d) | ('shapes', shapes, min_dim)."""
    txt = open(path, errors="replace").read()
    if re.search(r"%SR", txt):
        raise GerberUnsupported("step-repeat (%SR) block: not implemented; re-export without step-and-repeat")
    if re.search(r"%AB", txt):
        raise GerberUnsupported("block aperture (%AB): not implemented")
    macros = {m.group(1): m.group(2) for m in _MACRO.finditer(txt)}
    txt = _MACRO.sub("", txt).replace("%", "")
    xf = yf = (2, 6)
    unit, pol, ap, mode_cw, multi = 1.0, "D", None, False, True
    aps, objs = {}, []
    cur = [0.0, 0.0]
    region = None
    arc_mode = None
    for s in (t.strip() for t in txt.split("*")):
        if not s or s.startswith("G04") or s.startswith(("TF", "TA", "TD", "TO")):
            continue
        if s.startswith("FS"):
            if s.startswith("FST"):
                raise GerberUnsupported("%FST trailing-zero omission: not implemented; "
                                        "re-export with leading-zero omission (%FSLA)")
            m = re.match(r"FSLAX(\d)(\d)Y(\d)(\d)", s)
            if not m:
                raise GerberUnsupported(f"unrecognised format statement %{s}%")
            xf, yf = (int(m.group(1)), int(m.group(2))), (int(m.group(3)), int(m.group(4)))
            continue
        if s.startswith("MO"):
            unit = 25.4 if s == "MOIN" else 1.0
            continue
        if s.startswith("LP"):
            pol = "C" if s == "LPC" else "D"
            continue
        if s.startswith("AD"):
            m = re.match(r"AD(?:D)?(\d+)([A-Za-z_][\w.]*)(?:,(.*))?$", s)
            if not m:
                continue
            code, kind, prm = m.group(1), m.group(2), m.group(3) or ""
            vals = [float(t) for t in prm.split("X") if t not in ("",)] if prm else []
            if kind == "C":
                aps[int(code)] = ("C", vals[0] * unit)
            elif kind in ("R", "O"):
                w, h = vals[0] * unit, vals[1] * unit
                shapes = [("poly", [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)])] \
                    if kind == "R" else [("poly", _obround(w, h))]
                aps[int(code)] = ("shapes", shapes, min(w, h))
            elif kind in macros:
                sh = _macro_shapes(macros[kind], vals)
                aps[int(code)] = ("shapes", [("circle", s_[1] * unit, s_[2] * unit, s_[3] * unit)
                                              if s_[0] == "circle" else
                                              ("poly", [(x * unit, y * unit) for x, y in s_[1]])
                                              for s_ in sh], _shape_min_dim(sh) * unit)
            continue
        if s.startswith("AM"):
            continue
        g = re.search(r"G(\d+)", s)
        gcode = int(g.group(1)) if g else None
        if gcode == 36:
            region = []
            continue
        if gcode == 37:
            if region is not None:
                objs.append(("region", pol, region))
            region = None
            continue
        if gcode in (1, 2, 3, 74, 75) and gcode is not None and not re.search(r"[XYIJD]", s):
            if gcode in (2, 3):
                arc_mode = gcode
            if gcode == 74:
                multi = False
            if gcode == 75:
                multi = True
            continue
        if s == "M02":
            break
        if gcode in (1, 2, 3):
            arc_mode = gcode
        if gcode == 74:
            multi = False
        elif gcode == 75:
            multi = True
        vals = {}
        dcode = None
        for m in re.finditer(r"([XYIJ])([+-]?\d+)|D(\d+)", s):
            if m.group(3):
                dcode = int(m.group(3))
            else:
                vals[m.group(1)] = m.group(2)
        if dcode is None and not vals:
            continue
        if dcode is not None and dcode >= 10:
            ap = aps.get(dcode, ap)
            continue
        if dcode is None:
            dcode = 1
        def val(axis, fmt):
            raw = vals[axis]
            sign = -1 if raw.startswith("-") else 1
            digits = raw.lstrip("+-")
            ints, decs = fmt
            return sign * int(digits) / (10 ** decs) * unit
        nx = val("X", xf) if "X" in vals else cur[0]
        ny = val("Y", yf) if "Y" in vals else cur[1]
        new = (nx, ny)
        if dcode == 2:
            if region is not None:
                region.append([new])
            cur = [nx, ny]
            continue
        if dcode == 1:
            ii = val("I", xf) if "I" in vals else 0.0
            jj = val("J", yf) if "J" in vals else 0.0
            if arc_mode in (2, 3):
                pts = _arc_points((cur[0], cur[1]), new, ii, jj, arc_mode == 2)
            else:
                pts = [new]
            if region is not None:
                if not region:
                    region.append([(cur[0], cur[1])])
                region[-1].extend(pts)
            else:
                prev = (cur[0], cur[1])
                for p in pts:
                    objs.append(("draw", pol, ap, [prev, p]))
                    prev = p
            cur = [nx, ny]
            continue
        if dcode == 3 and region is None:
            objs.append(("flash", pol, ap, new))
            cur = [nx, ny]
    return objs


def parse_gerber_or_empty(path):
    try:
        return parse_gerber(path)
    except GerberUnsupported as e:
        return {"unsupported": str(e)}
    except Exception as e:           # a file we cannot read is reported, not fatal
        return {"error": str(e)}


# ---------------------------------------------------------------- raster
def scan_mask(H, W, contours):
    """Even-odd fill of closed polylines on an H x W pixel grid (local px coords,
    pixel (j, i) has centre (i + 0.5, j + 0.5)). Pure numpy: rasterization does
    not depend on any imaging library's version. All contours are filled together,
    so overlapping contours toggle (even-odd, as Gerber regions)."""
    out = np.zeros((H, W), bool)
    edges = []
    for c in contours:
        if len(c) < 3:
            continue
        P = np.asarray(c, float)
        Q = np.roll(P, -1, axis=0)
        edges.append(np.hstack([P, Q]))
    if not edges:
        return out
    E = np.vstack(edges)
    x1, y1, x2, y2 = E[:, 0], E[:, 1], E[:, 2], E[:, 3]
    keep = y1 != y2
    x1, y1, x2, y2 = x1[keep], y1[keep], x2[keep], y2[keep]
    if x1.size == 0:
        return out
    lo, hi = np.minimum(y1, y2), np.maximum(y1, y2)
    r0 = max(0, int(math.floor(lo.min() - 0.5)))
    r1 = min(H, int(math.ceil(hi.max() - 0.5)) + 1)
    if r1 <= r0:
        return out
    rows = np.arange(r0, r1) + 0.5
    cross = (rows[:, None] >= lo[None, :]) & (rows[:, None] < hi[None, :])
    t = (rows[:, None] - y1[None, :]) / (y2 - y1)[None, :]
    xi = np.where(cross, x1[None, :] + t * (x2 - x1)[None, :], np.inf)
    xi.sort(axis=1)
    cnt = cross.sum(axis=1)
    nr = r1 - r0
    diff = np.zeros((nr, W + 1), np.int32)
    ridx = np.arange(nr)
    for k in range(0, xi.shape[1] - 1, 2):
        ok = (k + 1) < cnt
        if not ok.any():
            break
        a = np.clip(np.ceil(xi[:, k] - 0.5), 0, W).astype(np.int64)
        b = np.clip(np.ceil(xi[:, k + 1] - 0.5), 0, W).astype(np.int64)
        np.add.at(diff, (ridx[ok], a[ok]), 1)
        np.add.at(diff, (ridx[ok], b[ok]), -1)
    out[r0:r1] = (np.cumsum(diff[:, :W], axis=1) % 2) == 1
    return out


def _disk_mask(H, W, cx, cy, r):
    x0, x1 = max(0, int(math.floor(cx - r)) - 1), min(W, int(math.ceil(cx + r)) + 2)
    y0, y1 = max(0, int(math.floor(cy - r)) - 1), min(H, int(math.ceil(cy + r)) + 2)
    out = np.zeros((H, W), bool)
    if x1 <= x0 or y1 <= y0:
        return out
    yy = (np.arange(y0, y1) + 0.5)[:, None]
    xx = (np.arange(x0, x1) + 0.5)[None, :]
    out[y0:y1, x0:x1] = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    return out


class _Canvas:
    def __init__(self, bbox, res):
        (x0, y0, x1, y1) = bbox
        self.x0, self.y0, self.res = x0, y0, res
        self.W = int(math.ceil((x1 - x0) / res)) + 2
        self.H = int(math.ceil((y1 - y0) / res)) + 2
        self.a = np.zeros((self.H, self.W), np.uint8)

    def px(self, p):
        return ((p[0] - self.x0) / self.res, (p[1] - self.y0) / self.res)

    def stamp(self, polys, circles, val):
        """polys: lists of px points (filled); circles: (cx, cy, r) px."""
        xs, ys = [], []
        for pl in polys:
            xs += [p[0] for p in pl]
            ys += [p[1] for p in pl]
        for cx, cy, r in circles:
            xs += [cx - r, cx + r]
            ys += [cy - r, cy + r]
        if not xs:
            return
        bx0, by0 = max(0, int(math.floor(min(xs))) - 2), max(0, int(math.floor(min(ys))) - 2)
        bx1, by1 = min(self.W, int(math.ceil(max(xs))) + 3), min(self.H, int(math.ceil(max(ys))) + 3)
        if bx1 <= bx0 or by1 <= by0:
            return
        h, w = by1 - by0, bx1 - bx0
        m = np.zeros((h, w), bool)
        for pl in polys:
            if len(pl) >= 3:
                m |= scan_mask(h, w, [[(p[0] - bx0, p[1] - by0) for p in pl]])
        for cx, cy, r in circles:
            m |= _disk_mask(h, w, cx - bx0, cy - by0, r)
        self.a[by0:by1, bx0:bx1][m] = val

    def shape_px(self, ap, x, y):
        """(polys, circles) in px for aperture `ap` flashed at mm (x, y)."""
        cx, cy = self.px((x, y))
        if ap[0] == "C":
            return [], [(cx, cy, ap[1] / 2 / self.res)]
        polys, circles = [], []
        for sh in ap[1]:
            if sh[0] == "circle":
                d = sh[1]
                circles.append((cx + sh[2] / self.res, cy + sh[3] / self.res, d / 2 / self.res))
            else:
                polys.append([(cx + px_ / self.res, cy + py_ / self.res) for px_, py_ in sh[1]])
        return polys, circles

    def draw_seg(self, ap, a, b, val):
        w = ap[1] if ap[0] == "C" else ap[2]
        r = w / 2 / self.res
        ax, ay = self.px(a)
        bx, by = self.px(b)
        L = math.hypot(bx - ax, by - ay)
        polys, circles = [], [(ax, ay, r), (bx, by, r)]
        if L > 1e-9:
            nx, ny = -(by - ay) / L * r, (bx - ax) / L * r
            polys.append([(ax + nx, ay + ny), (bx + nx, by + ny), (bx - nx, by - ny), (ax - nx, ay - ny)])
        self.stamp(polys, circles, val)

    def region(self, contours, val):
        pts = [[self.px(p) for p in c] for c in contours if len(c) >= 3]
        if not pts:
            return
        xs = [p[0] for c in pts for p in c]
        ys = [p[1] for c in pts for p in c]
        bx0, by0 = max(0, int(min(xs)) - 2), max(0, int(min(ys)) - 2)
        bx1, by1 = min(self.W, int(max(xs)) + 3), min(self.H, int(max(ys)) + 3)
        if bx1 <= bx0 or by1 <= by0:
            return
        m = scan_mask(by1 - by0, bx1 - bx0,
                      [[(p[0] - bx0, p[1] - by0) for p in c] for c in pts])   # even-odd
        self.a[by0:by1, bx0:bx1][m] = val

    def paint(self, objs):
        for o in objs:
            val = 255 if o[1] == "D" else 0
            if o[0] == "flash":
                self.stamp(*self.shape_px(o[2], o[3][0], o[3][1]), val)
            elif o[0] == "draw":
                pts = o[3]
                for a, b in zip(pts, pts[1:]):
                    self.draw_seg(o[2], a, b, val)
            elif o[0] == "region":
                self.region(o[2], val)


def _bbox(objs):
    xs, ys = [], []
    for o in objs:
        if o[0] == "flash":
            xs.append(o[3][0]); ys.append(o[3][1])
        elif o[0] == "draw":
            xs += [p[0] for p in o[3]]; ys += [p[1] for p in o[3]]
        elif o[0] == "region":
            for c in o[2]:
                xs += [p[0] for p in c]; ys += [p[1] for p in c]
    if not xs:
        return None
    m = 1.0
    return (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)


def _label8(mask):
    """8-connected labels of a bool raster, numpy only (no scipy: it does not
    import alongside the numpy 2 that fluxplace runs on). Runs per row, then
    union-find over runs that touch diagonally or orthogonally."""
    H, W = mask.shape
    lab = np.zeros((H, W), np.int32)
    m = np.pad(mask, ((0, 0), (1, 1))).astype(np.int8)
    dif = np.diff(m, axis=1)
    st = np.argwhere(dif == 1)                     # orig col = c
    en = np.argwhere(dif == -1)                    # orig col = c - 1
    if st.size == 0:
        return lab, 0
    r, s0 = st[:, 0].astype(np.int64), st[:, 1].astype(np.int64)
    e0 = en[:, 1].astype(np.int64) - 1
    n = r.size
    BIG = W + 4
    key = r * BIG + e0                              # sorted: runs ordered by (row, end)
    parent = np.arange(n)

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    prev_row = r - 1
    lo = np.searchsorted(key, prev_row * BIG + (s0 - 1), side="left")
    keyS = r * BIG + s0                             # sorted too: starts ascend within a row
    hi = np.searchsorted(keyS, prev_row * BIG + (e0 + 2), side="left")
    hi = np.maximum(hi, lo)
    cnt = hi - lo
    if cnt.sum():
        src = np.repeat(np.arange(n), cnt)
        offs = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        dst = lo[src] + offs
        ok = (r[dst] == prev_row[src]) & (s0[dst] <= e0[src] + 1)
        for a, b in zip(src[ok].tolist(), dst[ok].tolist()):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    roots = np.array([find(i) for i in range(n)])
    uniq, ids = np.unique(roots, return_inverse=True)
    ids = ids + 1
    for i in range(n):
        lab[r[i], s0[i]:e0[i] + 1] = ids[i]
    return lab, int(uniq.size)


def _min_gaps(lab, min_mm, res):
    """Pairs of different connected regions closer than `min_mm`.
    -> list of (gap_mm_estimate, label_a, label_b, x_mm, y_mm) for the closest
    boundary-pixel pair of each region pair, in px-grid coordinates."""
    fg = lab > 0
    er = fg.copy()                                  # 4-neighbour erosion, numpy only
    er[1:, :] &= fg[:-1, :]; er[:-1, :] &= fg[1:, :]
    er[:, 1:] &= fg[:, :-1]; er[:, :-1] &= fg[:, 1:]
    bnd = fg & ~er
    by, bx = np.nonzero(bnd)
    if by.size == 0:
        return []
    t_px = min_mm / res + 1.0
    R = int(math.ceil(t_px))
    pad = np.pad(lab, R, mode="constant")
    la = lab[by, bx]
    keys, dists, idxs = [], [], []
    ids = np.arange(by.size)
    for dy in range(-R, R + 1):
        for dx in range(-R, R + 1):
            d = math.hypot(dx, dy)
            if d == 0 or d > t_px:
                continue
            nb = pad[by + R + dy, bx + R + dx]
            hit = (nb > 0) & (nb != la)
            if not hit.any():
                continue
            h = ids[hit]
            a = la[hit].astype(np.int64)
            b = nb[hit].astype(np.int64)
            lo, hi = np.minimum(a, b), np.maximum(a, b)
            keys.append(lo * (lab.max() + 1) + hi)
            dists.append(np.full(h.size, d))
            idxs.append(h)
    if not keys:
        return []
    K = np.concatenate(keys); D = np.concatenate(dists); I = np.concatenate(idxs)
    order = np.lexsort((D, K))
    K, D, I = K[order], D[order], I[order]
    first = np.r_[True, K[1:] != K[:-1]]
    out = []
    M = lab.max() + 1
    for k, d, i in zip(K[first], D[first], I[first]):
        a, b = int(k // M), int(k % M)
        gap = max(0.0, (d - 1.0) * res)
        if gap < min_mm - res:
            out.append((gap, a, b, bx[i] * res, by[i] * res))
    return out


def _check_copper(objs, layer, prof, bbox, res, findings, log, gap_level=WARN):
    # exact: aperture width of drawn tracks
    hb = (prof.get("_house") or {}).get("track_min")
    if hb:                                 # house row (limits registry): below hard = FAIL, below recommended = WARN
        ws = [(_ap_w(o[2]), o) for o in objs if o[0] == "draw"]
        for lvl, lo, hi, tag in ((FAIL, 0, hb[0], "hard limit"), (WARN, hb[0], hb[1], "recommended")):
            bad = [(w, o) for w, o in ws if hi is not None and lo - HOUSE_TOL_MM <= w < hi - HOUSE_TOL_MM]
            if bad:
                w, o = min(bad, key=lambda t: t[0])
                findings.append(_f(lvl, "GERBER_MIN_WIDTH", f"{len(bad)} drawn segment(s) below the house {tag} {hi:g} mm; narrowest "
                                   f"{w:.4f} mm [{hb[2]}]", layer, o[3][0][0], o[3][0][1]))
        thin = []
    else:
        thin = [o for o in objs if o[0] == "draw" and _ap_w(o[2]) < prof["min_track_mm"] - 1e-6]
    if thin:
        o = thin[0]
        p = o[3][0]
        findings.append(_f(FAIL, "GERBER_MIN_WIDTH",
                           f"{len(thin)} drawn segment(s) narrower than min_track "
                           f"{prof['min_track_mm']:g} mm; narrowest "
                           f"{min(_ap_w(t[2]) for t in thin):.4f} mm", layer, p[0], p[1]))
    cv = _Canvas(bbox, res)
    cv.paint(objs)
    lab, n = _label8(cv.a > 0)
    gaps = _min_gaps(lab, prof["min_clearance_mm"], res)
    for gap, a, b, x, y in sorted(gaps)[:50]:
        findings.append(_f(gap_level, "GERBER_MIN_GAP",
                           f"regions {a} and {b} (different connected copper) "
                           f"{gap:.3f} mm apart (raster {res} mm); min_clearance "
                           f"{prof['min_clearance_mm']:g} mm", layer, bbox[0] + x, bbox[1] + y))
    if len(gaps) > 50:
        findings.append(_f(gap_level, "GERBER_MIN_GAP", f"{len(gaps)} gap pairs total; first 50 listed", layer))
    log(f"  {layer}: {n} connected regions, {len(gaps)} gap pair(s) < min_clearance")
    return cv.a > 0


def _ap_w(ap):
    return ap[1] if ap[0] == "C" else ap[2]


# ------------------------------------------------------------------ drill
def parse_excellon(path):
    """-> list of (x_mm, y_mm, d_mm) for every hit (slots/routes ignored)."""
    tools, hits = {}, []
    unit = 1.0
    cur_tool, x, y = None, 0.0, 0.0
    for raw in open(path, errors="replace"):
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line == "INCH" or line.startswith("INCH"):
            unit = 25.4
        if line.startswith("METRIC"):
            unit = 1.0
        m = re.match(r"T(\d+)C([\d.]+)", line)
        if m:
            if "." not in m.group(2):
                raise GerberUnsupported(f"Excellon tool diameter {m.group(0)!r} has no decimal point: not implemented")
            tools[m.group(1)] = float(m.group(2)) * unit
            continue
        m = re.match(r"T(\d+)$", line)
        if m:
            cur_tool = m.group(1)
            continue
        if line.startswith(("X", "Y")) and "G85" not in line:
            mx = re.search(r"X([+-]?[\d.]+)", line)
            my = re.search(r"Y([+-]?[\d.]+)", line)
            for mm_ in (mx, my):
                if mm_ and "." not in mm_.group(1):
                    raise GerberUnsupported(f"Excellon coordinate {mm_.group(0)!r} has no decimal point "
                                            "(integer/zero-omitted format): not implemented; "
                                            "re-export with decimal coordinates")
            if mx:
                x = float(mx.group(1)) * unit
            if my:
                y = float(my.group(1)) * unit
            if cur_tool is not None and (mx or my):
                hits.append((x, y, tools.get(cur_tool, 0.0)))
    return hits


# ----------------------------------------------------------------- edge
def _edge_open(objs):
    ends = {}
    for o in objs:
        if o[0] == "draw":
            for p in (o[3][0], o[3][-1]):
                k = (round(p[0], 4), round(p[1], 4))
                ends[k] = ends.get(k, 0) + 1
        elif o[0] == "region":
            continue
    if not ends:
        return None
    bad = [k for k, c in ends.items() if c != 2]
    return bad


# ----------------------------------------------------------------- package
HOUSE_TOL_MM = 0.003            # mil -> mm rounding (5 mil = 0.127 vs a 0.125 ring): not a violation
VIA_MAX_DRILL_MM = 0.5          # plated holes up to this drill are vias (via ring rule), larger are component holes


def _profile(cons):
    from fluxplace import constraints as C
    return C.fab_profile(cons)     # cons None -> the [fab] defaults


def check_package(fab_dir, board=None, cons=None, copper_layers=None, res_mm=RESOLUTION_MM,
                  log=print):
    """Check fab_dir/gerbers and fab_dir/drill. -> dict(status, findings, counts,
    seconds, resolution_mm). status: 'PASS' | 'WARN' | 'FAIL'."""
    t0 = time.time()
    prof = _profile(cons)
    # Gap is a raster estimate (see RESOLUTION): advisory unless the project opts in.
    gap_level = FAIL if str(((cons or {}).get("fab") or {}).get("gerber_gap", "warn")).lower() == "fail" else WARN
    gdir = os.path.join(fab_dir, "gerbers")
    ddir = os.path.join(fab_dir, "drill")
    findings = []
    files = {}
    for name in sorted(os.listdir(gdir)) if os.path.isdir(gdir) else []:
        lay = classify(name)
        if lay:
            files.setdefault(lay, os.path.join(gdir, name))
    if copper_layers is None and board:
        copper_layers = board_copper_layers(board)
    cu = [l for l in files if re.fullmatch(r"(F|B|In\d+)_Cu", l)]
    if copper_layers is None:
        inner = [int(re.fullmatch(r"In(\d+)_Cu", l).group(1)) for l in cu if l.startswith("In")]
        copper_layers = max([2] + [i + 2 for i in inner])
    expected = ["F_Cu", "B_Cu"] + ["In%d_Cu" % i for i in range(1, copper_layers - 1)] \
        + ["F_Mask", "B_Mask", "F_Silkscreen", "B_Silkscreen", "Edge_Cuts"]
    parsed = {}
    for lay in expected:
        path = files.get(lay)
        if path is None:
            findings.append(_f(FAIL, "GERBER_LAYER_MISSING", f"expected layer {lay} not exported", lay))
            continue
        objs = parse_gerber_or_empty(path)
        if isinstance(objs, dict) and "unsupported" in objs:
            findings.append(_f(FAIL, "GERBER_UNSUPPORTED", f"{lay}: {objs['unsupported']}", lay))
            continue
        if isinstance(objs, dict):
            findings.append(_f(FAIL, "GERBER_LAYER_MISSING", f"{lay}: unreadable ({objs['error']})", lay))
            continue
        if not objs:
            # present but no features: a single-sided board legitimately has empty back layers
            if lay == "Edge_Cuts":
                findings.append(_f(FAIL, "GERBER_LAYER_MISSING", "Edge_Cuts is empty (no board outline)", lay))
            elif re.fullmatch(r"(F|B|In\d+)_Cu", lay):
                findings.append(_f(WARN, "GERBER_LAYER_EMPTY", f"{lay}: copper layer has no features", lay))
            else:
                findings.append(_f(INFO, "GERBER_LAYER_EMPTY", f"{lay} has no features (nothing to mask or print)", lay))
        parsed[lay] = objs
    log(f"gerber check: {len(parsed)} layer(s) parsed, copper {copper_layers} layer(s)")
    prof["_house"] = LM.fab_bounds(prof, copper_layers)

    # edge closure
    if parsed.get("Edge_Cuts"):
        bad = _edge_open(parsed["Edge_Cuts"])
        if bad:
            findings.append(_f(FAIL, "GERBER_EDGE_OPEN",
                               f"Edge.Cuts has {len(bad)} open end point(s), e.g. "
                               f"({bad[0][0]:.3f}, {bad[0][1]:.3f})", "Edge_Cuts", bad[0][0], bad[0][1]))

    # copper: widths exact, gaps rastered
    holes_cov = None
    cu_objs = {l: parsed[l] for l in cu if l in parsed}
    outer_flashes = []
    gbox = None
    for l in cu_objs:
        bb = _bbox(cu_objs[l])
        if bb:
            gbox = bb if gbox is None else (min(gbox[0], bb[0]), min(gbox[1], bb[1]),
                                           max(gbox[2], bb[2]), max(gbox[3], bb[3]))
    if gbox is None:
        gbox = (0, 0, 1, 1)
    for l in sorted(cu_objs, key=lambda s: (s not in ("F_Cu",), s not in ("B_Cu",), s)):
        mask = _check_copper(cu_objs[l], l, prof, gbox, res_mm, findings, log, gap_level)
        if l in ("F_Cu", "B_Cu"):
            for o in cu_objs[l]:
                if o[0] == "flash":
                    outer_flashes.append((o[3][0], o[3][1], _ap_w(o[2]) if o[2][0] == "C" else o[2][2], l))
        if mask is not None:          # all copper layers share the gbox raster
            holes_cov = mask if holes_cov is None else (holes_cov | mask)

    # drill: annular ring + no-pad (plated holes only)
    holes = []
    if os.path.isdir(ddir):
        for name in sorted(os.listdir(ddir)):
            if not name.lower().endswith((".drl", ".xln", ".txt")):
                continue
            if "NPTH" in name.upper():
                continue
            try:
                holes += parse_excellon(os.path.join(ddir, name))
            except GerberUnsupported as e:
                findings.append(_f(FAIL, "GERBER_UNSUPPORTED", f"{name}: {e}"))
    house_rings = {}
    if holes:
        if outer_flashes:
            fx = np.array([f[0] for f in outer_flashes])
            fy = np.array([f[1] for f in outer_flashes])
            pd = np.array([f[2] for f in outer_flashes])
            pl = [f[3] for f in outer_flashes]
            for hx, hy, hd in holes:
                near = np.flatnonzero(np.hypot(fx - hx, fy - hy) <= 0.02)   # same hole
                for jj in near.tolist():
                    ring = (pd[jj] - hd) / 2
                    hb = (prof.get("_house") or {}).get("via_annular_min" if hd <= VIA_MAX_DRILL_MM else "pth_annular_min")
                    if hb:
                        for lvl, lo, hi in ((FAIL, 0, hb[0]), (WARN, hb[0], hb[1])):
                            if hi is not None and lo - HOUSE_TOL_MM <= ring < hi - HOUSE_TOL_MM:
                                house_rings.setdefault(lvl, []).append((ring, hd, hx, hy, pl[jj], hb, hi))
                                break
                        continue
                    if ring < prof["min_annular_mm"] - 1e-6:
                        findings.append(_f(FAIL, "GERBER_ANNULAR",
                                           f"hole d{hd:.3f} at ({hx:.3f},{hy:.3f}): "
                                           f"{pl[jj]} ring {ring:.3f} mm < min_annular "
                                           f"{prof['min_annular_mm']:g} mm", pl[jj], hx, hy))
                        break
        for lvl, rows in house_rings.items():              # one finding per level: the count and the worst ring
            ring, hd, hx, hy, lay, hb, hi = min(rows, key=lambda t: t[0])
            findings.append(_f(lvl, "GERBER_ANNULAR", f"{len(rows)} plated hole(s) with a ring below the house "
                               f"{'hard limit' if lvl == FAIL else 'recommended'} {hi:g} mm; worst d{hd:.3f} at ({hx:.3f},{hy:.3f}) "
                               f"{lay} ring {ring:.3f} mm [{hb[2]}]", lay, hx, hy))
        if holes_cov is not None:
            ox0, oy0, ox1, oy1 = gbox
            nocopper = 0
            first = None
            for hx, hy, hd in holes:
                px = int(round((hx - ox0) / res_mm)); py = int(round((hy - oy0) / res_mm))
                inside = 0 <= py < holes_cov.shape[0] and 0 <= px < holes_cov.shape[1]
                if not inside or not holes_cov[py, px]:
                    nocopper += 1
                    first = first or (hx, hy, hd)
            if nocopper:
                findings.append(_f(WARN, "GERBER_DRILL_NO_PAD",
                                   f"{nocopper} plated hole(s) with no copper under them; first "
                                   f"d{first[2]:.3f} at ({first[0]:.3f},{first[1]:.3f})",
                                   None, first[0], first[1]))

    fails = sum(1 for f in findings if f["level"] == FAIL)
    warns = sum(1 for f in findings if f["level"] == WARN)
    status = "FAIL" if fails else ("WARN" if warns else "PASS")
    counts = {}
    for f in findings:
        counts[f["code"]] = counts.get(f["code"], 0) + 1
    return {"status": status, "findings": findings, "counts": counts,
            "seconds": round(time.time() - t0, 2), "resolution_mm": res_mm,
            "copper_layers": copper_layers, "holes": len(holes)}


def format_report(res):
    lines = [f"gerber check : {res['status']}  ({res['seconds']} s, raster {res['resolution_mm']} mm, "
             f"{res['copper_layers']} copper layer(s), {res['holes']} drill hit(s))"]
    for code, n in sorted(res["counts"].items()):
        lines.append(f"  {code}: {n}")
    for f in res["findings"][:20]:
        loc = "" if f["x"] is None else f" @({f['x']:.3f},{f['y']:.3f})"
        lines.append(f"  [{f['level']}] {f['code']} {f['layer'] or ''}{loc}: {f['msg']}")
    if len(res["findings"]) > 20:
        lines.append(f"  ... {len(res['findings']) - 20} more")
    return "\n".join(lines)



GATE_MODES = ("fail", "warn", "off")


def gerber_mode(cons):
    """[fab] gerber_check: "fail" (FAIL-level GERBER_* blocks fab/deliver), "warn"
    (default: print prominently, exit code unchanged) or "off"."""
    v = str(((cons or {}).get("fab") or {}).get("gerber_check", "warn")).strip().lower()
    if v not in GATE_MODES:
        raise ValueError(f"[fab] gerber_check = {v!r}: must be one of fail, warn, off")
    return v


def run_stage(out, board=None, cons=None, log=print):
    """fab.emit / deliver gate. Checks the package under `out`, writes out/gerbercheck.txt,
    and returns dict(word, blocking, mode, status). `word` is the MANIFEST stages value.
    `blocking` is True only in "fail" mode when the check FAILs or raises (GERBER_CHECK_ERROR).
    Never raises: an exception becomes word "ERROR (...)" and, in fail mode, blocking."""
    mode = gerber_mode(cons)
    if mode == "off":
        return {"word": "OFF", "blocking": False, "mode": mode, "status": "OFF"}
    try:
        r = check_package(out, board=board, cons=cons, log=log)
    except Exception as e:
        log(f"GERBER_CHECK_ERROR: gerber check could not run: {e}")
        return {"word": "ERROR (%s)" % e, "blocking": mode == "fail", "mode": mode, "status": "ERROR"}
    log(format_report(r).splitlines()[0])
    try:
        with open(os.path.join(out, "gerbercheck.txt"), "w") as f:
            f.write(format_report(r) + "\n")
    except OSError as e:
        log(f"GERBER_CHECK_ERROR: could not write gerbercheck.txt: {e}")
        return {"word": "ERROR (%s)" % e, "blocking": mode == "fail", "mode": mode, "status": "ERROR"}
    if r["status"] != "PASS":
        tag = "GERBER CHECK FAILED - fab blocked (gerber_check = fail)" if (
            mode == "fail" and r["status"] == "FAIL") else f"GERBER CHECK {r['status']} (gerber_check = {mode})"
        log(f"*** {tag}")
        for f in r["findings"]:
            if f["level"] in (FAIL, WARN):
                log(f"***   [{f['level']}] {f['code']} {f['layer'] or ''}: {f['msg']}")
    return {"word": r["status"], "blocking": mode == "fail" and r["status"] == "FAIL",
            "mode": mode, "status": r["status"]}


def emit_stage(out, board=None, cons=None, log=print):
    """Compatibility wrapper: the MANIFEST status word only."""
    return run_stage(out, board=board, cons=cons, log=log)["word"]


if __name__ == "__main__":
    import sys
    import argparse
    ap = argparse.ArgumentParser(description="Re-check an exported fab package (gerbers + drill).")
    ap.add_argument("fab_dir")
    ap.add_argument("--board", default=None, help=".kicad_pcb, for its copper layer count only")
    ap.add_argument("--constraints", default=None, help="constraints TOML ([fab] floors)")
    ap.add_argument("--res", type=float, default=RESOLUTION_MM)
    a = ap.parse_args()
    cons = None
    if a.constraints:
        from fluxplace import constraints as C
        cons = C.load(a.constraints)
    r = check_package(a.fab_dir, board=a.board, cons=cons, res_mm=a.res)
    print(format_report(r))
    sys.exit(1 if r["status"] == "FAIL" else 0)
