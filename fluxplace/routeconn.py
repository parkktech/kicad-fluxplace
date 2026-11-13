"""Open connections from real connectivity: the work list of the batch router.

`open_by_net(board)` -> {net: open connections} for nets that still have any.  A net with copper
but unjoined pads counts; "a net with any copper is routed" is gone.

method "pcbnew": a helper process under /usr/bin/python3 (conn_helper.py) fills the zones first
(`fill`) and counts the pad clusters KiCad's connectivity finds.  method "text": the same count from
the file alone (routediag.load; pads, tracks, vias and zone polygons joined by touching; no zone
fill, so a pour joins the pads inside its outline on its layers).  "auto" = pcbnew when it works,
else text (flagged `approx` in the result of `measure`).
"""
import json
import math
import os
import subprocess

from . import routediag as RD

PY = os.environ.get("FLUXPLACE_KICAD_PYTHON", "/usr/bin/python3")
HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conn_helper.py")
TOL = 0.02


def _pcbnew(board, fill, timeout):
    r = subprocess.run([PY, HELPER, board] + (["--fill"] if fill else []),
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-300:])
    line = [l for l in r.stdout.splitlines() if l.startswith("{")][-1]
    return json.loads(line)["nets"]


# ---------------------------------------------------------------- text fallback
def _in_pad(p, x, y, tol):
    """point (x, y) inside the pad's rotated rectangle / circle / oval, grown by tol."""
    rad = math.radians(-(p.get("angle") or 0.0))
    dx, dy = x - p["x"], y - p["y"]
    lx, ly = dx * math.cos(rad) - dy * math.sin(rad), dx * math.sin(rad) + dy * math.cos(rad)
    w, h = p["w"] / 2 + tol, p["h"] / 2 + tol
    if p.get("shape") == "circle":
        return math.hypot(lx, ly) <= max(w, h)
    return abs(lx) <= w and abs(ly) <= h


def _in_poly(pts, x, y):
    ins = False
    for i in range(len(pts)):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % len(pts)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            ins = not ins
    return ins


def text_open(B, groups=None, members=None):
    """{net: open connections} from the parsed board B (routediag.load). `groups` (a dict) is filled
    with {net: [[pad dicts of one connected cluster], ...]} for the nets that are open.  `members` (a dict) is filled with
    {net: [(pads, segs, vias) of one cluster, ...]}: the copper of each cluster too."""
    out, _gout = {}, groups
    pads_by = {}
    for p in B["pads"]:
        if p["net"] and not p.get("np"):
            pads_by.setdefault(p["net"], []).append(p)
    for net, ps in pads_by.items():
        if len(ps) < 2:
            continue
        segs = [s for s in B["segs"] if s["net"] == net]
        vias = [v for v in B["vias"] if v["net"] == net]
        zones = [z for z in B["zones"] if z["net"] == net and not z["keepout"] and len(z["pts"]) > 2]
        items = [("pad", p) for p in ps] + [("seg", s) for s in segs] + [("via", v) for v in vias] + \
                [("zone", z) for z in zones]
        par = list(range(len(items)))

        def find(i):
            while par[i] != i:
                par[i] = par[par[i]]
                i = par[i]
            return i

        def union(i, j):
            par[find(i)] = find(j)

        def pts_of(it):                       # (x, y, layers) anchor points of an item
            k, o = it
            if k == "seg":
                return [(o["a"][0], o["a"][1], {o["layer"]}), (o["b"][0], o["b"][1], {o["layer"]})]
            if k == "via":
                return [(o["x"], o["y"], o["layers"] | set(B["copper"]))]
            return []

        def touches(i, j):
            (ka, a), (kb, b) = items[i], items[j]
            if ka == "zone" and kb == "zone":
                return False
            if ka in ("pad",) and kb in ("seg", "via"):
                return any(_in_pad(a, x, y, TOL) and (l & a["layers"]) for x, y, l in pts_of(items[j]))
            if ka in ("seg", "via") and kb in ("pad",):
                return touches(j, i)
            if ka == "zone" and kb in ("pad", "seg", "via"):
                if kb == "pad":
                    return bool(a["layers"] & b["layers"]) and _in_poly(a["pts"], b["x"], b["y"])
                return any(_in_poly(a["pts"], x, y) and (l & a["layers"]) for x, y, l in pts_of(items[j]))
            if kb == "zone":
                return touches(j, i)
            if ka == "via" and kb == "via":
                return math.hypot(a["x"] - b["x"], a["y"] - b["y"]) <= (a["d"] + b["d"]) / 2 + TOL
            if ka == "via" or kb == "via":
                v, g = (a, b) if ka == "via" else (b, a)
                return RD._seg_dist(v["x"], v["y"], g["a"][0], g["a"][1], g["b"][0], g["b"][1]) <= \
                    g["w"] / 2 + v["d"] / 2 + TOL
            if a["layer"] != b["layer"]:
                return False
            ends = [(x, y, b) for x, y, _l in pts_of(items[i])] + [(x, y, a) for x, y, _l in pts_of(items[j])]
            return any(RD._seg_dist(x, y, o["a"][0], o["a"][1], o["b"][0], o["b"][1]) <= o["w"] / 2 + TOL
                       for x, y, o in ends)

        cells, zl = {}, []
        for i, (k, o) in enumerate(items):
            if k == "zone":
                zl.append(i)
                continue
            if k == "seg":
                x0, x1 = sorted((o["a"][0], o["b"][0]))
                y0, y1 = sorted((o["a"][1], o["b"][1]))
                r = o["w"] / 2
            elif k == "via":
                x0 = x1 = o["x"]
                y0 = y1 = o["y"]
                r = o["d"] / 2
            else:
                x0 = x1 = o["x"]
                y0 = y1 = o["y"]
                r = math.hypot(o["w"], o["h"]) / 2
            for cx in range(int((x0 - r - TOL) // 1.0), int((x1 + r + TOL) // 1.0) + 1):
                for cy in range(int((y0 - r - TOL) // 1.0), int((y1 + r + TOL) // 1.0) + 1):
                    cells.setdefault((cx, cy), []).append(i)
        pairs = set()
        for lst in cells.values():
            for x in range(len(lst)):
                for y in range(x + 1, len(lst)):
                    pairs.add((lst[x], lst[y]))
        pairs.update((z, i) for z in zl for i in range(len(items)) if i != z)
        for i, j in pairs:
            if items[i][0] == "pad" and items[j][0] == "pad":
                continue
            if find(i) != find(j) and touches(i, j):
                union(i, j)
        groups = {find(i) for i, it in enumerate(items) if it[0] == "pad"}
        if len(groups) > 1:
            out[net] = len(groups) - 1
            if _gout is not None:
                cl = {}
                for i, it in enumerate(items):
                    if it[0] == "pad":
                        cl.setdefault(find(i), []).append(it[1])
                _gout[net] = sorted(cl.values(), key=lambda g: (g[0]["ref"], g[0]["num"]))
            if members is not None:
                mm = {}
                for i, (k, o) in enumerate(items):
                    if k in ("pad", "seg", "via"):
                        mm.setdefault(find(i), ([], [], []))[("pad", "seg", "via").index(k)].append(o)
                members[net] = [m for m in mm.values() if m[0]]
    return out


def open_by_net(board, fill=True, method="auto", timeout=900):
    """-> {net: open connections > 0}."""
    return measure(board, fill, method, timeout)["nets"]


def measure(board, fill=True, method="auto", timeout=900):
    """-> {nets, total, method, approx}."""
    if method in ("auto", "pcbnew"):
        try:
            nets = _pcbnew(board, fill, timeout)
            return {"nets": nets, "total": sum(nets.values()), "method": "pcbnew", "approx": False}
        except (OSError, RuntimeError, ValueError, IndexError, subprocess.TimeoutExpired):
            if method == "pcbnew":
                raise
    nets = text_open(RD.load(board))
    return {"nets": nets, "total": sum(nets.values()), "method": "text", "approx": True}
