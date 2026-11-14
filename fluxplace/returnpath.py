"""RETURN PATH checks for controlled nets: diff pairs, RF nets and `[review] return_path_nets`.

A high-speed or RF signal returns its current in the reference plane directly under it. Three
ways a routed board breaks that, each invisible to DRC (which checks copper against copper):

  REF_PLANE_GAP        a track segment crosses a stretch with no filled plane copper on either
                       adjacent reference layer (a slot cut by a foreign track, a split plane).
                       WARN when a contiguous unreferenced run exceeds `[review] plane_gap_mm`;
                       FAIL with `[review] return_path = "fail"`. The first/last plane_gap_mm of
                       a chain that starts/ends on a same-net pad or via are exempt (antipad).
  RETURN_VIA_MISSING   a via where the signal changes layer and the two layers reference different
                       planes, with no via on the reference net within `[review] return_via_mm`.
                       WARN; says so when the two planes are different nets.
  EDGE_TRACE           a controlled track closer than `[review] edge_trace_mm` to the outline. WARN.

Reference layers = the nearest copper layer above and the nearest below that carries a filled
plane (zone fill with a net). Layer order is the board's copper declaration order (the physical stack).

Geometry comes from the saved fills in the board file (`filled_polygon` entries, read with
boardfile's S-expression parser), so this runs with or without pcbnew and on any layer count.
Segments of one net on one layer are chained by shared endpoints, so a run is measured along the
copper, across segment joints. Branch points start a new chain (a run that crosses a T-junction is
measured on each branch separately).
"""
import math
import os
import re
from collections import defaultdict

from . import boardfile as BF
from . import graph as G
from . import routediag as RD

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"
STEP_MM = 0.25
TOL = 1e-3


def _f(level, code, msg, refs=()):
    return {"level": level, "code": code, "msg": msg, "refs": sorted(set(refs))}


def _key(p):
    return (round(p[0], 3), round(p[1], 3))


def _rings(path):
    """{(layer, net): [ring, ...]} from the saved zone fills (every filled_polygon of every zone)."""
    root = BF.parse(open(path, errors="replace").read())
    zones = list(BF._kids(root, "zone"))
    for fp in BF._kids(root, "footprint"):
        zones += BF._kids(fp, "zone")
    out = defaultdict(list)
    for z in zones:
        net = BF._net(z)
        for fpz in BF._kids(z, "filled_polygon"):
            lay = (BF._atoms(BF._first(fpz, "layer")) or BF._atoms(BF._first(fpz, "layers")) or [""])[0]
            pk = BF._first(fpz, "pts") or []
            pts = [tuple(BF._nums(xy)[:2]) for xy in BF._kids(pk, "xy")]
            if len(pts) >= 3 and net:
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                out[(lay, net)].append(((min(xs), min(ys), max(xs), max(ys)), pts))
    return out


def _inside(rings, x, y):
    """Even-odd over all rings of one (layer, net): holes are separate rings or nest, both count.
    `rings` items are (bbox, pts) from _rings (bbox skips rings the point cannot be in)."""
    n = 0
    for (x0, y0, x1, y1), pts in rings:
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            continue
        j = len(pts) - 1
        for i in range(len(pts)):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                n += 1
            j = i
    return n % 2 == 1


CELL_MM = 2.0
ANCHOR_MM = 0.5      # a chain endpoint this close to a same-net pad or via centre counts as anchored


class Planes:
    """Filled plane copper per layer, and the reference layers of every copper layer."""

    def __init__(self, path, copper):
        self.copper = list(copper)
        self.rings = _rings(path)
        self.nets_on = defaultdict(set)
        self.grid = {}   # (layer, net) -> {cell: [ring]}: each ring listed in every cell its bbox touches
        for (lay, net), rs in self.rings.items():
            self.nets_on[lay].add(net)
            g = defaultdict(list)
            for item in rs:
                (x0, y0, x1, y1), _ = item
                for cx in range(math.floor(x0 / CELL_MM), math.floor(x1 / CELL_MM) + 1):
                    for cy in range(math.floor(y0 / CELL_MM), math.floor(y1 / CELL_MM) + 1):
                        g[(cx, cy)].append(item)
            self.grid[(lay, net)] = g

    def ref_layers(self, layer):
        """Nearest plane layer above and below `layer` (only layers carrying a filled plane)."""
        i = self.copper.index(layer)
        up = next((self.copper[k] for k in range(i - 1, -1, -1) if self.nets_on.get(self.copper[k])), None)
        dn = next((self.copper[k] for k in range(i + 1, len(self.copper)) if self.nets_on.get(self.copper[k])), None)
        return [l for l in (up, dn) if l]

    def nets_at(self, layer, x, y):
        cell = (math.floor(x / CELL_MM), math.floor(y / CELL_MM))
        return {net for net in self.nets_on.get(layer, ())
                if _inside(self.grid[(layer, net)].get(cell, ()), x, y)}


def _chains(segs):
    """Segments of one net+layer as ordered polylines (walk through shared endpoints)."""
    at = defaultdict(list)
    for i, s in enumerate(segs):
        at[_key(s["a"])].append(i)
        at[_key(s["b"])].append(i)
    used, out = set(), []
    for i, s in enumerate(segs):
        if i in used or _key(s["a"]) == _key(s["b"]):
            continue
        used.add(i)
        pts = [s["a"], s["b"]]
        for forward in (True, False):
            while True:
                p = pts[-1] if forward else pts[0]
                nxt = [j for j in at[_key(p)] if j not in used and _key(segs[j]["a"]) != _key(segs[j]["b"])]
                if not nxt:
                    break
                j = nxt[0]
                used.add(j)
                q = segs[j]["b"] if _key(segs[j]["a"]) == _key(p) else segs[j]["a"]
                if forward:
                    pts.append(q)
                else:
                    pts.insert(0, q)
        out.append(pts)
    return out


def _samples(pts, step=STEP_MM):
    """(x, y, s) every `step` mm along a polyline, endpoints included."""
    out, s = [(pts[0][0], pts[0][1], 0.0)], 0.0
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        L = math.hypot(bx - ax, by - ay)
        if L <= 0:
            continue
        n = max(1, int(math.ceil(L / step)))
        for k in range(1, n + 1):
            t = k / n
            out.append((ax + (bx - ax) * t, ay + (by - ay) * t, s + L * t))
        s += L
    return out


def _seg_dist(p, edge):
    return min(RD._seg_dist(p[0], p[1], a[0], a[1], b[0], b[1]) for a, b in edge)


def controlled_nets(facts, cons):
    from . import review as R
    cons = cons or {}
    nets = set(facts.get("net_pads") or {})
    pairs = G.diff_pairs(sorted(nets))
    out = set(pairs) | set(pairs.values()) | {n for n in nets if R.is_rf(n)}
    for rx in (cons.get("review") or {}).get("return_path_nets", []) or []:
        out |= {n for n in nets if re.search(rx, n)}
    return out


def check_return_path(facts, cons):
    board_path = facts.get("board_path") or facts.get("board")
    if not isinstance(board_path, str) or not os.path.isfile(board_path):
        return []
    B = RD.load(board_path)
    if not B["segs"]:
        return []
    rv = dict((cons or {}).get("review") or {})
    gap_lim = float(rv.get("plane_gap_mm", 1.0))
    via_lim = float(rv.get("return_via_mm", 2.0))
    edge_lim = float(rv.get("edge_trace_mm", 0.5))
    gap_level = FAIL if str(rv.get("return_path", "warn")).lower() == "fail" else WARN
    ctrl = controlled_nets(facts, cons)
    if not ctrl:
        return []
    P = Planes(board_path, B["copper"])
    out = []
    # same-net copper a track may legitimately start or end on (pad or via): the plane is antipadded there
    anchors = defaultdict(list)
    for p in B["pads"]:
        if p["net"]:
            anchors[p["net"]].append((p["x"], p["y"]))
    for v in B["vias"]:
        anchors[v["net"]].append((v["x"], v["y"]))
    by = defaultdict(list)
    for s in B["segs"]:
        if s["net"] in ctrl and s["layer"] in B["copper"] and s["layer"]:
            by[(s["net"], s["layer"])].append(s)
    for (net, layer), segs in sorted(by.items()):
        refs = P.ref_layers(layer)
        for pts in _chains(segs):
            samp = _samples(pts)
            total = samp[-1][2]
            near = lambda q: any(math.hypot(q[0] - ax, q[1] - ay) <= ANCHOR_MM for ax, ay in anchors[net])
            a0, a1 = near(pts[0]), near(pts[-1])
            # the first/last plane_gap_mm of a chain that starts/ends on a same-net pad or via count as referenced:
            # the antipad of that pad or via is expected there and is not a return-path break
            exempt = [(a0 and s < gap_lim) or (a1 and total - s < gap_lim) for _, _, s in samp]
            ref_ok = [exempt[k] or any(P.nets_at(r, x, y) for r in refs) for k, (x, y, _) in enumerate(samp)]
            run_start = None
            best = None
            for k, (x, y, s) in enumerate(samp):
                if not ref_ok[k]:
                    if run_start is None:
                        run_start = k
                    run_len = s - samp[run_start][2]
                    if best is None or run_len > best[0]:
                        best = (run_len, run_start)
                else:
                    run_start = None
            if best and best[0] > gap_lim:
                x, y, _ = samp[best[1]]
                out.append(_f(gap_level, "REF_PLANE_GAP",
                              f"{net} on {layer}: {best[0]:.2f} mm with no plane copper on "
                              f"{'/'.join(refs) or 'no reference layer'}, from x={x:.2f} y={y:.2f} "
                              f"(limit {gap_lim:g} mm)", refs=[net]))
            if B["edge"]:
                near = min(samp, key=lambda p: _seg_dist(p, B["edge"]))
                d = _seg_dist(near, B["edge"])
                if d < edge_lim:
                    out.append(_f(WARN, "EDGE_TRACE",
                                  f"{net} on {layer}: track {d:.2f} mm from the board outline at "
                                  f"x={near[0]:.2f} y={near[1]:.2f} (limit {edge_lim:g} mm)", refs=[net]))
    out += _return_vias(B, P, ctrl, via_lim)
    return out


def _return_vias(B, P, ctrl, via_lim):
    out = []
    segs_of = defaultdict(list)
    for s in B["segs"]:
        segs_of[s["net"]].append(s)
    for v in B["vias"]:
        if v["net"] not in ctrl:
            continue
        touched = set()
        for s in segs_of[v["net"]]:
            if s["layer"] in v["layers"]:
                if math.hypot(s["a"][0] - v["x"], s["a"][1] - v["y"]) < TOL or \
                        math.hypot(s["b"][0] - v["x"], s["b"][1] - v["y"]) < TOL:
                    touched.add(s["layer"])
        if len(touched) < 2:
            continue
        ref_sets = {frozenset(P.ref_layers(l)) for l in touched}
        if len(ref_sets) < 2:
            continue
        need = sorted({r for l in touched for r in P.ref_layers(l)})
        missing, nets_all = [], set()
        for r in need:
            rnets = P.nets_on.get(r, set())
            nets_all |= rnets
            near = [math.hypot(w["x"] - v["x"], w["y"] - v["y"]) for w in B["vias"]
                    if w is not v and w["net"] in rnets and w["net"] != v["net"]]
            if any(d <= via_lim for d in near):
                continue
            nearest = f"nearest {min(near):.2f} mm" if near else "none on that net"
            missing.append(f"{r} ({'/'.join(sorted(rnets)) or '-'}; {nearest})")
        if not missing:
            continue
        diff = (" — the reference planes are different nets" if len(nets_all) > 1 else "")
        out.append(_f(WARN, "RETURN_VIA_MISSING",
                      f"{v['net']} via at x={v['x']:.2f} y={v['y']:.2f} changes {'/'.join(sorted(touched))}: "
                      f"no return via within {via_lim:g} mm of the reference plane(s) "
                      f"{'; '.join(missing)}{diff}", refs=[v["net"]]))
    return out
