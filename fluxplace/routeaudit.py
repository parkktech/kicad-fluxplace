"""Post-batch audit: the router's output is not trusted.

`audit(base, new, R)` compares the copper a router run ADDED (tracks/vias in `new` that `base` lacks)
against the project rules `R` (routerules.rules) and returns a list of violations:

  VIA_IN_PAD   a new via's barrel overlaps ANY pad (exact pad shape, any net) -- when via_in_pad is false
  TRACK_WIDTH  a new track narrower than the track floor
  VIA_SIZE / VIA_DRILL   a new via below the via-diameter / drill floor
  CLEARANCE    new copper closer than the clearance floor to copper of another net (tracks, vias, pads;
               edge to edge, same-layer, exact rect/oval/circle pad shape)
  DRC_NEW      (R["audit_drc"]) a KiCad DRC error type that the input board did not have

A batch with any violation is discarded by the caller (batchroute / routepar), halved and retried; a
single net that still violates is a leftover with reason RULE_VIOLATION.
"""
import json
import math
import os
import shutil
import subprocess
import tempfile
from collections import Counter

from . import routediag as RD

EPS = 1e-3           # mm; the router's own float noise must not read as a violation
CELL = 2.0


def _r(v):
    return round(v, 4)


def seg_key(s):
    a, b = sorted((tuple(map(_r, s["a"])), tuple(map(_r, s["b"]))))
    return ("s", a, b, _r(s["w"]), s["layer"], s["net"])


def via_key(v):
    return ("v", _r(v["x"]), _r(v["y"]), _r(v["d"]), v["net"])


def new_copper(base, new):
    """-> (segs, vias) of `new` that `base` does not have."""
    bs = {seg_key(s) for s in base["segs"]}
    bv = {via_key(v) for v in base["vias"]}
    return ([s for s in new["segs"] if seg_key(s) not in bs],
            [v for v in new["vias"] if via_key(v) not in bv])


def removed_copper(base, new):
    """Copper of `base` that `new` no longer has -> (segs, vias): what a router run RIPPED."""
    ns = {seg_key(s) for s in new["segs"]}
    nv = {via_key(v) for v in new["vias"]}
    return ([s for s in base["segs"] if seg_key(s) not in ns],
            [v for v in base["vias"] if via_key(v) not in nv])


# ---------------------------------------------------------------- geometry
def seg_seg(a, b, c, d):
    """minimum distance between segments ab and cd."""
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    d1, d2, d3, d4 = cross(c, d, a), cross(c, d, b), cross(a, b, c), cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 * d2 < 0 and d3 * d4 < 0:
        return 0.0
    return min(RD._seg_dist(a[0], a[1], c[0], c[1], d[0], d[1]), RD._seg_dist(b[0], b[1], c[0], c[1], d[0], d[1]),
               RD._seg_dist(c[0], c[1], a[0], a[1], b[0], b[1]), RD._seg_dist(d[0], d[1], a[0], a[1], b[0], b[1]))


def _local(p, x, y):
    rad = math.radians(-(p.get("angle") or 0.0))
    dx, dy = x - p["x"], y - p["y"]
    return dx * math.cos(rad) - dy * math.sin(rad), dx * math.sin(rad) + dy * math.cos(rad)


def _seg_rect(a, b, hw, hh):
    """distance from segment ab to the axis-aligned rect [-hw,hw]x[-hh,hh] (0 when they touch)."""
    def inside(p):
        return abs(p[0]) <= hw and abs(p[1]) <= hh
    if inside(a) or inside(b):
        return 0.0
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    edges = [(corners[i], corners[(i + 1) % 4]) for i in range(4)]
    if any(seg_seg(a, b, e0, e1) == 0.0 for e0, e1 in edges):
        return 0.0
    return min(seg_seg(a, b, e0, e1) for e0, e1 in edges)


def seg_pad(a, b, p):
    """distance from segment ab (centre line) to the pad's copper shape."""
    la, lb = _local(p, *a), _local(p, *b)
    hw, hh = p["w"] / 2, p["h"] / 2
    shape = p.get("shape")
    if shape in ("circle", "oval"):
        r = min(hw, hh)
        core = (max(hw, hh) - r)
        c0, c1 = ((-core, 0.0), (core, 0.0)) if hw >= hh else ((0.0, -core), (0.0, core))
        return max(0.0, seg_seg(la, lb, c0, c1) - r)
    return _seg_rect(la, lb, hw, hh)


def pt_pad(p, x, y):
    return seg_pad((x, y), (x, y), p)


def _expand_layers(v, copper):
    ls = [l for l in v["layers"] if l in copper]
    if len(ls) >= 2:
        i, j = sorted((copper.index(ls[0]), copper.index(ls[-1])))
        return set(copper[i:j + 1])
    return set(copper) if not ls else set(ls)


class _Index:
    def __init__(self, B):
        self.B = B
        self.cu = B["copper"]
        self.items = []
        self.cells = {}
        for s in B["segs"]:
            self._add(("s", s), s["a"], s["b"], s["w"] / 2)
        for v in B["vias"]:
            self._add(("v", v), (v["x"], v["y"]), (v["x"], v["y"]), v["d"] / 2)
        for p in B["pads"]:
            r = math.hypot(p["w"], p["h"]) / 2
            self._add(("p", p), (p["x"], p["y"]), (p["x"], p["y"]), r)

    def _add(self, it, a, b, r):
        i = len(self.items)
        self.items.append(it)
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        for cx in range(int((x0 - r) // CELL), int((x1 + r) // CELL) + 1):
            for cy in range(int((y0 - r) // CELL), int((y1 + r) // CELL) + 1):
                self.cells.setdefault((cx, cy), []).append(i)

    def near(self, a, b, r):
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        out = set()
        for cx in range(int((x0 - r) // CELL), int((x1 + r) // CELL) + 1):
            for cy in range(int((y0 - r) // CELL), int((y1 + r) // CELL) + 1):
                out.update(self.cells.get((cx, cy), ()))
        return [self.items[i] for i in sorted(out)]


def _gap(kind, o, kind2, o2, cu):
    """edge-to-edge gap between two copper items; None when they share no layer."""
    if kind == "p" and kind2 != "p":
        return _gap(kind2, o2, kind, o, cu)
    if kind == "s":
        la = {o["layer"]}
        a, b, r = o["a"], o["b"], o["w"] / 2
    elif kind == "v":
        la = _expand_layers(o, cu)
        a = b = (o["x"], o["y"])
        r = o["d"] / 2
    else:
        return None
    if kind2 == "s":
        lb = {o2["layer"]}
        c, d, r2 = o2["a"], o2["b"], o2["w"] / 2
        if not la & lb:
            return None
        return seg_seg(a, b, c, d) - r - r2
    if kind2 == "v":
        lb = _expand_layers(o2, cu)
        if not la & lb:
            return None
        c = (o2["x"], o2["y"])
        return seg_seg(a, b, c, c) - r - o2["d"] / 2
    if kind2 == "p":
        if not la & (o2["layers"] or set(cu)):
            return None
        return seg_pad(a, b, o2) - r
    return None


def audit(base, new, R, new_only=None):
    """`base` / `new`: routediag.load dicts (or board paths). -> [violation dicts] (empty = clean)."""
    if isinstance(base, str):
        base = RD.load(base)
    if isinstance(new, str):
        new = RD.load(new)
    segs, vias = new_only if new_only is not None else new_copper(base, new)
    out = []
    cu = new["copper"]
    idx = _Index(new)

    def add(code, net, msg, x, y):
        out.append({"code": code, "net": net, "msg": msg, "x": round(x, 3), "y": round(y, 3)})

    for s in segs:
        if R.get("min_track") and s["w"] < R["min_track"] - EPS:
            add("TRACK_WIDTH", s["net"], f"{s['net']} {s['layer']} {s['w']:g} mm < {R['min_track']:g}", *s["a"])
    for v in vias:
        if R.get("min_via") and v["d"] < R["min_via"] - EPS:
            add("VIA_SIZE", v["net"], f"{v['net']} via {v['d']:g} mm < {R['min_via']:g}", v["x"], v["y"])
        if R.get("min_drill") and v.get("drill") and v["drill"] < R["min_drill"] - EPS:
            add("VIA_DRILL", v["net"], f"{v['net']} drill {v['drill']:g} mm < {R['min_drill']:g}", v["x"], v["y"])
        if not R.get("via_in_pad"):
            vl = _expand_layers(v, cu)
            for p in new["pads"]:
                if abs(p["x"] - v["x"]) > v["d"] + max(p["w"], p["h"]) or abs(p["y"] - v["y"]) > v["d"] + max(p["w"], p["h"]):
                    continue
                if not vl & (p["layers"] or set(cu)):
                    continue
                if pt_pad(p, v["x"], v["y"]) < v["d"] / 2 - EPS:
                    add("VIA_IN_PAD", v["net"], f"{v['net']} via in pad {p['ref']}.{p['num']}"
                        + (f" ({p['net']})" if p["net"] else ""), v["x"], v["y"])
                    break
    floor = R.get("min_clearance") or 0.0
    if floor:
        seen = set()
        for kind, items in (("s", segs), ("v", vias)):
            for o in items:
                a, b, r = ((o["a"], o["b"], o["w"] / 2) if kind == "s" else
                           ((o["x"], o["y"]), (o["x"], o["y"]), o["d"] / 2))
                for k2, o2 in idx.near(a, b, r + floor + 3.0):
                    if o2 is o or (o["net"] and o2["net"] == o["net"]):
                        continue
                    g = _gap(kind, o, k2, o2, cu)
                    if g is not None and g < floor - EPS:
                        key = (o["net"], o2.get("net"), round(a[0], 1), round(a[1], 1))
                        if key in seen:
                            continue
                        seen.add(key)
                        add("CLEARANCE", o["net"], f"{o['net']} to {o2.get('net') or 'pad ' + str(o2.get('ref', ''))} "
                            f"gap {max(g, 0):.3f} mm < {floor:g}", *a)
    return out


# ---------------------------------------------------------------- KiCad DRC (optional)
def drc_types(path, cli="kicad-cli", timeout=900):
    """{type: count} of KiCad DRC errors on `path` (kicad-cli pcb drc, json)."""
    d = tempfile.mkdtemp(prefix="fxdrc-")
    try:
        rep = os.path.join(d, "drc.json")
        subprocess.run([cli, "pcb", "drc", "--format", "json", "--severity-error", "--output", rep, path],
                       capture_output=True, timeout=timeout)
        j = json.load(open(rep))
        return dict(Counter(v.get("type", "?") for v in j.get("violations", [])))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def new_drc_types(base_path, new_path, cli="kicad-cli", base_types=None, timeout=900):
    """-> (violations, base_types): DRC error types that `new_path` has and `base_path` did not."""
    bt = base_types if base_types is not None else drc_types(base_path, cli, timeout)
    nt = drc_types(new_path, cli, timeout)
    v = [{"code": "DRC_NEW", "net": "", "msg": f"KiCad DRC {t} x{c} (input had {bt.get(t, 0)})", "x": 0, "y": 0}
         for t, c in sorted(nt.items()) if c > bt.get(t, 0) and t not in bt]
    return v, bt


def summarize(viol, n=3):
    c = Counter(v["code"] for v in viol)
    return ", ".join(f"{k} x{x}" for k, x in c.most_common()) + (
        "; e.g. " + "; ".join(v["msg"] for v in viol[:n]) if viol else "")
