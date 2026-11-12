"""Global router on a coarse cell grid: where does the copper of a placement go, and does it fit?

Capacity.  A cell (default 2 mm) of a signal layer carries `floor(free_width / (track + clearance) * util)`
tracks in each direction, where free_width is the cell's width minus the union of the bands blocked by pads,
via barrels (planned and, in default mode, existing), track-forbidding keep-outs and, in default mode, existing
tracks.  Planes (inner layers with a full-board pour) carry no signals.  Via capacity per cell = free via
lattice sites (0.6 mm pitch) minus the escape plan's own vias.

Demand.  Every net with >= 2 pads that is not a plane net (and in default mode is not already routed) is
decomposed Prim-style from its escape points (escapeplan: lane end for a direct pin, the via site for a
dog-bone, the pad itself for a coarse part) into 2-pin connections, each routed over the (layer, cell) graph.
A differential pair is one demand of 3 pitches (2 tracks + gap) along one path; a pair that cannot stay
together is split (PLAN_PAIR_SPLIT).  RF nets get a heavy via cost and are reported when they change layers more
than `[rf] max_vias`.  A rail's demand is its width in pitches.  PathFinder negotiated congestion: present-
congestion factor grows each iteration, history cost accumulates on overflowing cell-directions, nets touching
overflow are ripped up and re-routed in a fixed deterministic order, best iteration kept.

Estimated copper length = octile distance between the connection ends + the detour the cell path adds.
"""
import heapq
import math
import time
from collections import defaultdict

from . import constraints as CS
from . import escapeplan as EP
from . import netclass as NC

HARD = ("rf", "switch_node", "bypass", "pair")
PRIO = {"rf": 0, "pair": 1, "switch_node": 2, "clock": 3, "bypass": 4, "signal": 5, "slow": 6, "rail": 7}
PAIR_UNITS = 3.0


def _union_len(iv, lo, hi):
    if not iv:
        return 0.0
    iv = sorted(iv)
    tot, cur_a, cur_b = 0.0, max(iv[0][0], lo), min(iv[0][1], hi)
    for a, b in iv[1:]:
        a, b = max(a, lo), min(b, hi)
        if a > cur_b:
            tot += max(0.0, cur_b - cur_a)
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    return tot + max(0.0, cur_b - cur_a)


def octile(dx, dy):
    dx, dy = abs(dx), abs(dy)
    return max(dx, dy) + 0.41421356 * min(dx, dy)


class GlobalRoute:
    def __init__(self, B, cons=None, cls=None, pr=None, plan=None, ignore_routing=True, routed_nets=(),
                 groups=None, log=None, variant=None):
        from . import preroute as PR
        self.B, self.cons = B, cons or {}
        self.pr = pr or EP.plan_params(cons)
        self.cls = cls if cls is not None else NC.classify(B, cons, None)
        self.plan = plan
        self.ignore_routing = ignore_routing
        self.routed = set(routed_nets)
        self.groups = groups or {}
        self.log = log or (lambda m: None)
        sig, planes = EP.plan_layers(B, self.cons)
        self.layers = [l for l in B["copper"] if l in sig] or list(B["copper"])
        self.variant = variant
        if variant == "plane3v3" and len(self.layers) > 3:          # one inner signal layer becomes a dedicated plane
            self.layers.remove([l for l in self.layers if l not in (B["copper"][0], B["copper"][-1])][-1])
        self.real = list(self.layers)
        if variant == "8layer":                                      # two more signal layers, no pads, full capacity
            self.layers += ["In5.Cu", "In6.Cu"]
        self.planes = planes
        self.nl = len(self.layers)
        self.lid = {l: k for k, l in enumerate(self.layers)}
        x0, y0, x1, y1 = B["bbox"]
        self.cell = float(self.pr["cell_mm"])
        self.x0, self.y0 = x0 - 0.5, y0 - 0.5
        self.nx = int((x1 - self.x0) / self.cell) + 2
        self.ny = int((y1 - self.y0) / self.cell) + 2
        self.N = self.nx * self.ny
        self.obs = defaultdict(lambda: defaultdict(list))       # (layer id, cell) -> tag -> [rect]
        self.vobs = defaultdict(lambda: defaultdict(list))      # cell -> tag -> [rect] (all layers: via sites)
        self.tag_cells = defaultdict(set)
        self.capH = [[0] * self.N for _ in range(self.nl)]
        self.capV = [[0] * self.N for _ in range(self.nl)]
        self.vcap = [0] * self.N
        self.plane_nets = plan.plane if plan is not None else EP.plane_nets(B, self.cons, self.cls, self.pr)
        self.routes = {}
        self.nets = {}
        self.split_pairs = set()
        self.pair_origin = {}
        self.t_build = 0.0
        self.built = False

    # ------------------------------------------------------------------ grid helpers
    def cell_of(self, x, y):
        i = min(max(int((x - self.x0) / self.cell), 0), self.nx - 1)
        j = min(max(int((y - self.y0) / self.cell), 0), self.ny - 1)
        return j * self.nx + i

    def xy_of(self, c):
        return (self.x0 + (c % self.nx + 0.5) * self.cell, self.y0 + (c // self.nx + 0.5) * self.cell)

    def _cells_of_rect(self, r):
        c = self.cell
        i0, i1 = max(int((r[0] - self.x0) / c), 0), min(int((r[2] - self.x0) / c), self.nx - 1)
        j0, j1 = max(int((r[1] - self.y0) / c), 0), min(int((r[3] - self.y0) / c), self.ny - 1)
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                yield j * self.nx + i

    def _cell_box(self, c):
        i, j = c % self.nx, c // self.nx
        return (self.x0 + i * self.cell, self.y0 + j * self.cell, self.x0 + (i + 1) * self.cell, self.y0 + (j + 1) * self.cell)

    # ------------------------------------------------------------------ obstacles
    def _add_rect(self, tag, rect, layer_ids, grow):
        r = (rect[0] - grow, rect[1] - grow, rect[2] + grow, rect[3] + grow)
        for c in self._cells_of_rect(r):
            for l in layer_ids:
                self.obs[(l, c)][tag].append(r)
            self.tag_cells[tag].add(c)

    def add_part(self, ref):
        """(Re)register the pads of a part as obstacles of every layer they occupy."""
        self.remove_tag(ref)
        g = self.pr["clearance_mm"] + self.pr["track_mm"] / 2
        vg = self.pr["clearance_mm"] + self.pr["via_mm"] / 2
        for q in self.B["parts"][ref]["pads"]:
            r = EP.prect(q)
            ids = [self.lid[l] for l in q["layers"] if l in self.lid]
            if q["tht"]:
                ids += [self.lid[l] for l in self.layers if l not in self.real]
            if ids:
                self._add_rect(ref, r, ids, g)
            vr = (r[0] - vg, r[1] - vg, r[2] + vg, r[3] + vg)
            for c in self._cells_of_rect(vr):
                self.vobs[c][ref].append(vr)
                self.tag_cells[ref].add(c)

    def remove_tag(self, tag):
        for c in self.tag_cells.pop(tag, ()):
            for l in range(self.nl):
                d = self.obs.get((l, c))
                if d is not None:
                    d.pop(tag, None)
            d = self.vobs.get(c)
            if d is not None:
                d.pop(tag, None)

    def build_obstacles(self):
        B, pr = self.B, self.pr
        g = pr["clearance_mm"] + pr["track_mm"] / 2
        vg = pr["clearance_mm"] + pr["via_mm"] / 2
        for ref in B["parts"]:
            self.add_part(ref)
        allids = list(range(self.nl))
        for k, z in enumerate(B["zones"]):
            if not (z["keepout"] and z["pts"]) or not (z["no_tracks"] or z.get("no_vias")):
                continue
            ids = [self.lid[l] for l in z["layers"] if l in self.lid]
            xs, ys = [p[0] for p in z["pts"]], [p[1] for p in z["pts"]]
            bb = (min(xs), min(ys), max(xs), max(ys))
            from . import geom as GM
            for c in self._cells_of_rect(bb):
                cb = self._cell_box(c)
                n = 0
                for a in range(4):
                    for b in range(4):
                        if GM.poly_has(z["pts"], cb[0] + (a + .5) * self.cell / 4, cb[1] + (b + .5) * self.cell / 4):
                            n += 1
                if n == 0:
                    continue
                f = n / 16.0
                full = (cb[0], cb[1], cb[2], cb[3])
                if f > 0.9:
                    rects = [full]
                else:        # a thin sliver: block the covered share of the width, both directions
                    w = f * self.cell
                    rects = [(cb[0], cb[1], cb[0] + w, cb[3]), (cb[0], cb[1], cb[2], cb[1] + w)]
                if z["no_tracks"]:
                    for l in ids:
                        for r in rects:
                            self.obs[(l, c)][f"ko{k}"].append(r)
                    self.tag_cells[f"ko{k}"].add(c)
                if z.get("no_vias", z["no_tracks"]):
                    for r in rects:
                        self.vobs[c][f"ko{k}"].append(r)
                    self.tag_cells[f"ko{k}"].add(c)
        if not self.ignore_routing:
            for s in B["segs"]:
                l = self.lid.get(s["layer"])
                if l is None or s["net"] in ("",):
                    continue
                self._add_seg(l, s, g)
            for v in B["vias"]:
                r = v["d"] / 2
                self._add_rect("copper", (v["x"] - r, v["y"] - r, v["x"] + r, v["y"] + r), allids, g)
                vr = (v["x"] - r - vg + pr["via_mm"] / 2, v["y"] - r - vg + pr["via_mm"] / 2,
                      v["x"] + r + vg - pr["via_mm"] / 2, v["y"] + r + vg - pr["via_mm"] / 2)
                for c in self._cells_of_rect(vr):
                    self.vobs[c]["copper"].append(vr)
                    self.tag_cells["copper"].add(c)
        # planned via sites (escape plan): barrels through every layer
        if self.plan is not None:
            self._plan_vias()
            self._lane_fields()

    def _lane_fields(self):
        """A run of fine-pitch pins that escape straight out forms a lane field on the pad's layer: through-traffic
        cannot cross it, so its bounding rectangle is an obstacle (rows of pins closer than 1.2 mm join)."""
        self.remove_tag("lanes")
        rows = defaultdict(list)
        P = self.B["parts"]
        for (ref, num), rec in self.plan.pins.items():
            if rec.get("kind") != "direct" or not rec.get("fine") or not rec.get("dir"):
                continue
            q = next((p for p in P[ref]["pads"] if p["num"] == num), None)
            if q is None:
                continue
            d = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}[rec["dir"]]
            L = self.pr["lane_len_mm"]
            sx, sy = q["x"] + d[0] * q["ex"], q["y"] + d[1] * q["ey"]
            r = (min(sx, sx + d[0] * L), min(sy, sy + d[1] * L), max(sx, sx + d[0] * L), max(sy, sy + d[1] * L))
            lay = rec["layer"]
            rows[(ref, rec["dir"], lay, round(sx if d[1] else sy, 1))].append(r)
        g = self.pr["clearance_mm"] + self.pr["track_mm"] / 2
        for (ref, dr, lay, _k), rs in rows.items():
            l = self.lid.get(lay)
            if l is None:
                continue
            rs.sort(key=lambda r: (r[1], r[0]) if dr in "EW" else (r[0], r[1]))
            cur = list(rs[0])
            for r in rs[1:] + [None]:
                join = r is not None and ((dr in "EW" and r[1] - cur[3] <= 1.2) or (dr in "NS" and r[0] - cur[2] <= 1.2))
                if join:
                    cur = [min(cur[0], r[0]), min(cur[1], r[1]), max(cur[2], r[2]), max(cur[3], r[3])]
                else:
                    if len(rs) >= 3:
                        self._add_rect("lanes", tuple(cur), [l], g)
                    if r is not None:
                        cur = list(r)

    def _add_seg(self, l, s, g):
        (ax, ay), (bx, by) = s["a"], s["b"]
        L = math.hypot(bx - ax, by - ay)
        hw = s["w"] / 2
        n = max(1, int(L / 1.0)) if (abs(ax - bx) > 1e-6 and abs(ay - by) > 1e-6) else 1
        for k in range(n):
            t0, t1 = k / n, (k + 1) / n
            x0, x1 = ax + (bx - ax) * t0, ax + (bx - ax) * t1
            y0, y1 = ay + (by - ay) * t0, ay + (by - ay) * t1
            self._add_rect("copper", (min(x0, x1) - hw, min(y0, y1) - hw, max(x0, x1) + hw, max(y0, y1) + hw), [l], g)

    def _plan_vias(self):
        self.remove_tag("planvia")
        r = self.pr["via_mm"] / 2
        g = self.pr["clearance_mm"] + self.pr["track_mm"] / 2
        for rec in self.plan.pins.values():
            v = rec.get("via")
            if v:
                self._add_rect("planvia", (v[0] - r, v[1] - r, v[0] + r, v[1] + r), range(self.nl), g)
                self.via_used[self.cell_of(v[0], v[1])] += 1

    # ------------------------------------------------------------------ capacity
    def cap_cell(self, c):
        pitch, util = self.pr["pitch_mm"], self.pr["util"]
        cb = self._cell_box(c)
        for l in range(self.nl):
            d = self.obs.get((l, c))
            if not d:
                self.capH[l][c] = self.capV[l][c] = int(self.cell / pitch * util + 1e-6)
                continue
            ivy, ivx = [], []
            jog = self.pr["jog_frac"] * self.cell
            for rects in d.values():
                for r in rects:
                    # an obstacle covering only a sliver of the cell along a track's run is jogged around
                    if min(r[2], cb[2]) - max(r[0], cb[0]) >= jog:
                        ivy.append((r[1], r[3]))
                    if min(r[3], cb[3]) - max(r[1], cb[1]) >= jog:
                        ivx.append((r[0], r[2]))
            fy = self.cell - _union_len(ivy, cb[1], cb[3])
            fx = self.cell - _union_len(ivx, cb[0], cb[2])
            self.capH[l][c] = int(max(0.0, fy) / pitch * util + 1e-6)
            self.capV[l][c] = int(max(0.0, fx) / pitch * util + 1e-6)
        # via sites: free fraction of the cell (all layers' pad/keep-out/via blockers) on a 0.6 mm lattice
        d = self.vobs.get(c)
        full = (self.cell / self.pr["slot_mm"]) ** 2 * 0.5
        if d:
            blocked = 0.0
            ivy = []
            area = 0.0
            for rects in d.values():
                for r in rects:
                    a = (min(r[2], cb[2]) - max(r[0], cb[0])) * (min(r[3], cb[3]) - max(r[1], cb[1]))
                    area += max(0.0, a)
            frac = min(1.0, area / (self.cell * self.cell))
            full *= (1.0 - frac)
        self.vcap[c] = max(0, int(full) - self.via_used[c])

    def build(self):
        t = time.time()
        self.via_used = defaultdict(int)
        self.build_obstacles()
        for c in range(self.N):
            self.cap_cell(c)
        self.uH = [[0.0] * self.N for _ in range(self.nl)]
        self.uV = [[0.0] * self.N for _ in range(self.nl)]
        self.vu = [0.0] * self.N
        self.hH = [[0.0] * self.N for _ in range(self.nl)]
        self.hV = [[0.0] * self.N for _ in range(self.nl)]
        self.hv = [0.0] * self.N
        self.build_nets()
        self.built = True
        self.t_build = time.time() - t

    # ------------------------------------------------------------------ nets
    def terminal(self, ref, num):
        """((layer ids allowed or None=all), cell, (x, y), kind) for one pad."""
        self._fine = False
        q = next(p for p in self.B["parts"][ref]["pads"] if p["num"] == num)
        rec = self.plan.pins.get((ref, num)) if self.plan is not None else None
        part = self.B["parts"][ref]
        if rec and rec.get("kind") == "via":
            x, y = rec["via"]
            return None, self.cell_of(x, y), (x, y), "via"
        lay = [self.lid[l] for l in q["layers"] if l in self.lid]
        allowed = None if (q["tht"] or len(lay) > 1) else lay
        x, y = q["x"], q["y"]
        self._fine = False
        if rec and rec.get("kind") == "direct" and rec.get("dir"):
            d = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}[rec["dir"]]
            L = min(self.pr["lane_len_mm"], rec.get("lane") or self.pr["lane_len_mm"])
            x, y = q["x"] + d[0] * (q["ex"] + L), q["y"] + d[1] * (q["ey"] + L)
            self._fine = bool(rec.get("fine"))
            self._lane_dir = 0 if d[0] else 1
        return allowed, self.cell_of(x, y), (x, y), (rec or {}).get("kind", "direct")

    def _terms(self, ps):
        terms, seen = [], set()
        for p in ps:
            al, c, xy, kind = self.terminal(p["ref"], p["num"])
            key = (c, tuple(al) if al else None)
            if key in seen:
                continue
            seen.add(key)
            t = {"pad": f"{p['ref']}.{p['num']}", "layers": al, "cell": c, "xy": xy, "kind": kind,
                 "pxy": (p["x"], p["y"]), "free": (0, 1), "fine": False}
            if self._fine:                # a fine-pitch lane: only its own direction is free, its via is charged
                t["free"], t["fine"] = (self._lane_dir,), True
            terms.append(t)
        return terms

    def split_pair(self, name):
        """Replace a pair demand by its two member nets (PLAN_PAIR_SPLIT)."""
        nt = self.nets.get(name)
        if not nt or not nt["pair"] or name in self.split_pairs:
            return []
        self.rip(name)
        del self.nets[name]
        new = []
        for m in nt["members"]:
            t = nt["split"][m]
            if len(t) < 2:
                continue
            self.nets[m] = dict(nt, name=m, members=[m], units=1.0, terms=t, pair=False, split=None, was_pair=name)
            new.append(m)
        k = self.order.index(name)
        self.order[k:k + 1] = new
        self.split_pairs.add(name)
        self.pair_origin[name] = list(nt["members"])
        return new

    def build_nets(self):
        B, cls, pr = self.B, self.cls, self.pr
        pads = NC.net_pads(B)
        pair_done, nets = set(), {}
        rfc = (self.cons.get("rf") or {})
        for net in sorted(cls):
            info = cls[net]
            if net in self.plane_nets or net in self.routed or net in pair_done:
                continue
            ps = [p for p in pads[net] if not p.get("np")]
            if len(ps) < 2:
                continue
            if net in self.groups:                 # partly routed: one terminal per cluster
                ps = []
                gs = self.groups[net]
                cen = [(sum(p["x"] for p in g) / len(g), sum(p["y"] for p in g) / len(g)) for g in gs]
                for gi, g in enumerate(gs):
                    ox = [c for k, c in enumerate(cen) if k != gi]
                    tx, ty = sum(c[0] for c in ox) / len(ox), sum(c[1] for c in ox) / len(ox)
                    ps.append(min(g, key=lambda p: math.hypot(p["x"] - tx, p["y"] - ty)))
            members = [net]
            units = 1.0
            part = info.get("partner")
            if info["class"] == "pair" and part and part in cls and part not in self.plane_nets and part not in self.routed \
                    and len([p for p in pads[part] if not p.get("np")]) == len(ps) and part not in pair_done and part != net:
                members, units = sorted([net, part]), PAIR_UNITS
                pair_done.add(net)
                pair_done.add(part)
                if members[0] != net:
                    continue
                net = members[0]
                ps = [p for p in pads[net] if not p.get("np")]
            elif info["class"] == "rail":
                w = CS.power_width_mm(self.cons, net, pr["track_mm"])
                units = max(1.0, math.ceil((w + pr["clearance_mm"]) / pr["pitch_mm"] - 1e-9))
            terms = self._terms(ps)
            if len(terms) < 2:
                continue
            lay = None
            if info["class"] == "rf":
                lay = None
            nets[net] = {"name": net, "members": members, "units": units, "terms": terms, "cls": info["class"],
                         "info": info, "rf": info["class"] == "rf", "pair": len(members) > 1,
                         "max_vias": int(rfc.get("max_vias", 1))}
            if len(members) > 1:
                nets[net]["split"] = {m: self._terms([p for p in pads[m] if not p.get("np")]) for m in members}
        self.nets = nets
        self.order = sorted(nets, key=lambda n: (PRIO.get(nets[n]["cls"], 9),
                                                 -NC.mst_length([t["pxy"] for t in nets[n]["terms"]]), n))

    # ------------------------------------------------------------------ search
    def route_net(self, name, pres, bbm=7):
        nt = self.nets[name]
        terms = nt["terms"]
        w = nt["units"]
        nx, N, nl = self.nx, self.N, self.nl
        capH, capV, uH, uV, hH, hV = self.capH, self.capV, self.uH, self.uV, self.hH, self.hV
        cell = self.cell
        via_cost = self.pr["via_cost_mm"] * (4.0 if nt["rf"] else 1.0)
        # Prim order over terminal cells
        pts = [(t["cell"] % nx, t["cell"] // nx) for t in terms]
        n = len(terms)
        order, done = [0], {0}
        while len(order) < n:
            b = min((k for k in range(n) if k not in done),
                    key=lambda k: (min(abs(pts[k][0] - pts[j][0]) + abs(pts[k][1] - pts[j][1]) for j in order), k))
            order.append(b)
            done.add(b)
        tree = set()
        mine = set()                      # (l, c, dir) already carried by this net: free to reuse
        conns, vias, path_nodes = [], 0, []
        free = set()                      # a pad's own cell costs no capacity: the track starts inside it
        tcells = {t["cell"] for t in terms if not t["fine"]}
        for t in terms:
            for l in (t["layers"] if t["layers"] is not None else range(nl)):
                for d_ in t["free"]:
                    free.add((l, t["cell"], d_))
        mine |= free
        first = terms[0]
        for l in (first["layers"] if first["layers"] is not None else range(nl)):
            tree.add(l * N + first["cell"])
        for k in order[1:]:
            t = terms[k]
            srcs = [l * N + t["cell"] for l in (t["layers"] if t["layers"] is not None else range(nl))]
            tx = [nd % N % nx for nd in tree]
            ty = [nd % N // nx for nd in tree]
            bx0, bx1, by0, by1 = min(tx), max(tx), min(ty), max(ty)
            sx, sy = pts[k]
            lo_x, hi_x = min(bx0, sx) - bbm, max(bx1, sx) + bbm
            lo_y, hi_y = min(by0, sy) - bbm, max(by1, sy) + bbm
            res = None
            for attempt in (0, 1):
                if attempt:
                    lo_x, hi_x, lo_y, hi_y = 0, nx - 1, 0, self.ny - 1
                res = self._dij(srcs, tree, mine, w, pres, via_cost, (lo_x, hi_x, lo_y, hi_y), (bx0, bx1, by0, by1))
                if res:
                    break
            if not res:
                conns.append({"from": t["pad"], "path": None})
                continue
            path = res
            conns.append({"from": t["pad"], "path": path, "a": t["xy"]})
            for a, b in zip(path, path[1:]):
                if a // N != b // N:
                    vias += 1
            for nd in path:
                tree.add(nd)
            self._mark(path, mine)
        # commit usage of this net
        keys = mine - free
        for (l, c, d) in keys:
            (self.uH if d == 0 else self.uV)[l][c] += w
        vcells = set()
        for cn in conns:
            p = cn.get("path")
            if p:
                for a, b in zip(p, p[1:]):
                    if a // N != b // N and a % N not in tcells:
                        vcells.add(a % N)
        for c in vcells:
            self.vu[c] += w if not nt["pair"] else 2.0
        # a via's barrel, annulus and clearance block every layer it passes: charge the layers the net does not use there
        vuse = []
        vb = self.pr["via_units"]
        if vb > 0:
            for c in vcells:
                used = {nd // N for cn in conns if cn.get("path") for nd in cn["path"] if nd % N == c}
                for l in range(nl):
                    if l not in used:
                        uH[l][c] += vb
                        uV[l][c] += vb
                        vuse.append((l, c))
        self.routes[name] = {"conns": conns, "keys": keys, "vcells": vcells, "vias": vias, "vuse": vuse}
        return all(c.get("path") for c in conns)

    def _mark(self, path, mine):
        N, nx = self.N, self.nx
        for a, b in zip(path, path[1:]):
            la, lb = a // N, b // N
            if la != lb:
                continue
            ca, cb = a % N, b % N
            d = 0 if (ca // nx == cb // nx) else 1
            mine.add((la, ca, d))
            mine.add((la, cb, d))

    def _dij(self, srcs, tree, mine, w, pres, via_cost, box, tbox):
        nx, ny, N, nl, cell = self.nx, self.ny, self.N, self.nl, self.cell
        lo_x, hi_x, lo_y, hi_y = box
        tx0, tx1, ty0, ty1 = tbox
        capH, capV, uH, uV, hH, hV = self.capH, self.capV, self.uH, self.uV, self.hH, self.hV
        vcap, vu, hv = self.vcap, self.vu, self.hv
        dist, prev = {}, {}
        pq = []
        for s in srcs:
            if s in tree:
                return [s]
            dist[s] = 0.0
            c = s % N
            i, j = c % nx, c // nx
            h = (max(tx0 - i, 0, i - tx1) + max(ty0 - j, 0, j - ty1)) * cell
            heapq.heappush(pq, (h, 0.0, s))
        while pq:
            f, g, u = heapq.heappop(pq)
            if g > dist.get(u, 1e18):
                continue
            if u in tree:
                path = [u]
                while path[-1] in prev:
                    path.append(prev[path[-1]])
                return path[::-1]
            l, c = divmod(u, N)
            i, j = c % nx, c // nx
            for di, dj, dr in ((1, 0, 0), (-1, 0, 0), (0, 1, 1), (0, -1, 1)):
                ni, nj = i + di, j + dj
                if ni < lo_x or ni > hi_x or nj < lo_y or nj > hi_y or ni < 0 or nj < 0 or ni >= nx or nj >= ny:
                    continue
                nc = nj * nx + ni
                if dr == 0:
                    key, cap, use, hist = (l, nc, 0), capH[l][nc], uH[l][nc], hH[l][nc]
                else:
                    key, cap, use, hist = (l, nc, 1), capV[l][nc], uV[l][nc], hV[l][nc]
                if key in mine:
                    step = cell
                else:
                    over = use + w - cap
                    step = cell * (1.0 + hist) + (pres * cell * over if over > 0 else 0.0)
                v = l * N + nc
                nd = g + step
                if nd < dist.get(v, 1e18):
                    dist[v] = nd
                    prev[v] = u
                    hh = (max(tx0 - ni, 0, ni - tx1) + max(ty0 - nj, 0, nj - ty1)) * cell
                    heapq.heappush(pq, (nd + hh, nd, v))
            for l2 in range(nl):
                if l2 == l:
                    continue
                if abs(l2 - l) > 1 and nl > 2 and False:
                    continue
                vo = vu[c] + w - vcap[c]
                st = via_cost * abs(l2 - l) + hv[c] + (pres * cell * vo if vo > 0 else 0.0)
                v = l2 * N + c
                nd = g + st
                if nd < dist.get(v, 1e18):
                    dist[v] = nd
                    prev[v] = u
                    hh = (max(tx0 - i, 0, i - tx1) + max(ty0 - j, 0, j - ty1)) * cell
                    heapq.heappush(pq, (nd + hh, nd, v))
        return None

    # ------------------------------------------------------------------ incremental update (placement loop)
    def update_after_move(self, moved, redo_pins, pins_changed, iters=3):
        """Parts `moved` changed pose; the escape plan of `redo_pins` (a set of refs) was recomputed.  Rebuild the
        obstacles of those parts, the planned vias and lane fields, the capacity of every cell they touch, the
        terminals of the nets with a pad on a part in `redo_pins`, and re-route those nets plus the nets crossing a
        changed cell.  Returns the set of re-routed nets."""
        dirty = set()
        for ref in moved:
            dirty |= self.tag_cells.get(ref, set())
        for ref in moved:
            self.add_part(ref)
            dirty |= self.tag_cells.get(ref, set())
        for tag in ("planvia", "lanes"):
            dirty |= self.tag_cells.get(tag, set())
        self.via_used = defaultdict(int)
        self._plan_vias()
        self._lane_fields()
        for tag in ("planvia", "lanes"):
            dirty |= self.tag_cells.get(tag, set())
        for c in dirty:
            self.cap_cell(c)
        pads = NC.net_pads(self.B)
        names = set()
        for n, nt in list(self.nets.items()):
            mem = nt["members"]
            refs = {p["ref"] for m in mem for p in pads.get(m, [])}
            r = self.routes.get(n)
            if refs & redo_pins or (r and any(k[1] in dirty for k in r["keys"])) or (r and r["vcells"] & dirty):
                names.add(n)
        for n in names:
            self.rip(n)
            nt = self.nets[n]
            nt["terms"] = self._terms([p for p in pads[nt["members"][0]] if not p.get("np")]) if not nt["pair"] else \
                self._terms([p for p in pads[nt["members"][0]] if not p.get("np")])
            if nt.get("split"):
                nt["split"] = {m: self._terms([p for p in pads[m] if not p.get("np")]) for m in nt["members"]}
        if names:
            self.route_all(iters=iters, only=names)
        return names

    def rip(self, name):
        r = self.routes.pop(name, None)
        if not r:
            return
        w = self.nets[name]["units"]
        for (l, c, d) in r["keys"]:
            (self.uH if d == 0 else self.uV)[l][c] -= w
        for c in r["vcells"]:
            self.vu[c] -= w if not self.nets[name]["pair"] else 2.0
        vb = self.pr["via_units"]
        for (l, c) in r.get("vuse", ()):
            self.uH[l][c] -= vb
            self.uV[l][c] -= vb

    # ------------------------------------------------------------------ negotiation
    def overflow_cells(self):
        """{(l, c): (over, dem, cap, dir)} for every cell-direction above capacity (worst dir per cell)."""
        out = {}
        for l in range(self.nl):
            uH, uV, cH, cV = self.uH[l], self.uV[l], self.capH[l], self.capV[l]
            for c in range(self.N):
                oh, ov = uH[c] - cH[c], uV[c] - cV[c]
                if oh > 1e-9 or ov > 1e-9:
                    out[(l, c)] = (max(oh, 0) + max(ov, 0), (uH[c], uV[c]), (cH[c], cV[c]))
        return out

    def total_overflow(self):
        t = 0.0
        for l in range(self.nl):
            uH, uV, cH, cV = self.uH[l], self.uV[l], self.capH[l], self.capV[l]
            for c in range(self.N):
                if uH[c] > cH[c]:
                    t += uH[c] - cH[c]
                if uV[c] > cV[c]:
                    t += uV[c] - cV[c]
        for c in range(self.N):
            if self.vu[c] > self.vcap[c]:
                t += self.vu[c] - self.vcap[c]
        return t

    def route_all(self, iters=None, time_limit=None, only=None, pres0=0.5, pres_mul=1.7):
        """Negotiate. `only`: re-route just these nets (the rest keep their routes and usage)."""
        t0 = time.time()
        iters = int(iters if iters is not None else self.pr["iterations"])
        todo = [n for n in self.order if (only is None or n in only)]
        for n in todo:
            self.rip(n)
        pres = pres0
        for n in todo:
            self.route_net(n, pres)
        best, best_state = self.total_overflow(), self._save(todo)
        stall = 0
        for it in range(1, iters + 1):
            if best <= 1e-9 or (time_limit and time.time() - t0 > time_limit):
                break
            ov = self.overflow_cells()
            vo = {c for c in range(self.N) if self.vu[c] > self.vcap[c]}
            for (l, c), (o, _d, _c) in ov.items():
                if self.uH[l][c] > self.capH[l][c]:
                    self.hH[l][c] += 0.3
                if self.uV[l][c] > self.capV[l][c]:
                    self.hV[l][c] += 0.3
            for c in vo:
                self.hv[c] += 0.5
            bad = {c for (_l, c) in ov} | vo
            hit = [n for n in todo if n in self.routes and (any(k[1] in bad for k in self.routes[n]["keys"])
                                                          or self.routes[n]["vcells"] & bad)]
            hit += [n for n in todo if n not in self.routes]
            if not hit:
                break
            pres *= pres_mul
            if it >= 2:
                for n in [n for n in hit if n in self.nets and self.nets[n]["pair"] and self._pair_pinched(n)]:
                    new = self.split_pair(n)
                    todo = [x for x in todo if x != n] + new
                    hit = [x for x in hit if x != n] + new
                    best, best_state = float("inf"), None          # the structure changed: restart the best-so-far
            hs = set(hit)
            for n in [n for n in todo if n in hs]:
                self.rip(n)
            for n in [n for n in todo if n in hs]:
                self.route_net(n, pres)
            cur = self.total_overflow()
            self.log(f"  iter {it}: overflow {cur:g}, {len(hit)} net(s) re-routed")
            if best_state is None or cur < best - 1e-9:
                best, best_state, stall = cur, self._save(todo), 0
            else:
                stall += 1
                if stall >= 3:
                    break
        if best_state is not None:
            self._load(todo, best_state)
        return self.total_overflow()

    def _pair_pinched(self, name):
        """A pair is split only when it crosses a cell-direction that cannot carry its demand even when empty."""
        u = self.nets[name]["units"]
        r = self.routes.get(name)
        return bool(r) and any((self.capH if d == 0 else self.capV)[l][c] < u and
                               (self.uH if d == 0 else self.uV)[l][c] > (self.capH if d == 0 else self.capV)[l][c]
                               for (l, c, d) in r["keys"])

    def _save(self, names):
        return {n: self.routes.get(n) for n in names}

    def _load(self, names, state):
        for n in names:
            self.rip(n)
        for n, r in state.items():
            if r is None or n not in self.nets:
                continue
            self.routes[n] = r
            w = self.nets[n]["units"]
            for (l, c, d) in r["keys"]:
                (self.uH if d == 0 else self.uV)[l][c] += w
            for c in r["vcells"]:
                self.vu[c] += w if not self.nets[n]["pair"] else 2.0
            for (l, c) in r.get("vuse", ()):
                self.uH[l][c] += self.pr["via_units"]
                self.uV[l][c] += self.pr["via_units"]

    def snapshot(self):
        return {"routes": dict(self.routes), "uH": [list(a) for a in self.uH], "uV": [list(a) for a in self.uV],
                "vu": list(self.vu), "hH": [list(a) for a in self.hH], "hV": [list(a) for a in self.hV],
                "hv": list(self.hv)}

    def restore(self, s):
        self.routes = dict(s["routes"])
        self.uH, self.uV = [list(a) for a in s["uH"]], [list(a) for a in s["uV"]]
        self.vu, self.hH, self.hV, self.hv = list(s["vu"]), [list(a) for a in s["hH"]], [list(a) for a in s["hV"]], list(s["hv"])

    # ------------------------------------------------------------------ results
    def net_stats(self):
        """{net: {len, vias, layers, unreached, conns}} with the estimated copper length."""
        out = {}
        for name, nt in self.nets.items():
            r = self.routes.get(name)
            if not r:
                out[name] = {"len": 0.0, "vias": 0, "layers": [], "unreached": len(nt["terms"]) - 1, "cells": 0}
                continue
            ln, un, lays, cells = 0.0, 0, set(), 0
            for cn in r["conns"]:
                p = cn.get("path")
                if not p:
                    un += 1
                    continue
                a = cn["a"]
                ex, ey = self.xy_of(p[-1] % self.N)
                steps = sum(1 for u, v in zip(p, p[1:]) if u // self.N == v // self.N)
                man = abs(p[0] % self.N % self.nx - p[-1] % self.N % self.nx) + abs(p[0] % self.N // self.nx - p[-1] % self.N // self.nx)
                ln += octile(a[0] - ex, a[1] - ey) + max(0.0, steps - man) * self.cell
                for nd in p:
                    lays.add(self.layers[nd // self.N])
                cells += len(p)
            out[name] = {"len": round(ln, 2), "vias": r["vias"], "layers": sorted(lays), "unreached": un, "cells": cells}
        return out

    def summary(self):
        ov = self.overflow_cells()
        vo = [(c, self.vu[c], self.vcap[c]) for c in range(self.N) if self.vu[c] > self.vcap[c]]
        return {"overflow": round(self.total_overflow(), 2), "overflow_cells": len({c for (_l, c) in ov}) + len(vo),
                "nets": len(self.nets), "routed": sum(1 for n in self.nets if n in self.routes)}


def findings(gr, ns, parts_near=None):
    """PLAN_* findings from a routed GlobalRoute and its net stats."""
    B, pr = gr.B, gr.pr
    out = []
    ov = gr.overflow_cells()
    # --- overflow regions: 4-connected cell groups over all layers
    cells = defaultdict(dict)
    for (l, c), (o, d, cp) in ov.items():
        cells[c][l] = (o, d, cp)
    seen, regions = set(), []
    for c in sorted(cells):
        if c in seen:
            continue
        stack, reg = [c], []
        seen.add(c)
        while stack:
            u = stack.pop()
            reg.append(u)
            i, j = u % gr.nx, u // gr.nx
            for v in ((j * gr.nx + i + 1) if i + 1 < gr.nx else -1, (j * gr.nx + i - 1) if i else -1,
                      u + gr.nx, u - gr.nx):
                if v in cells and v not in seen:
                    seen.add(v)
                    stack.append(v)
        regions.append(reg)
    through = defaultdict(set)
    for n, r in gr.routes.items():
        for (l, c, d) in r["keys"]:
            through[(l, c)].add(n)
    for reg in regions:
        tot = sum(o for c in reg for (o, _d, _c) in cells[c].values())
        worst = max(((o, c, l) for c in reg for l, (o, _d, _c) in cells[c].items()), key=lambda t: (t[0], -t[1]))
        wc = worst[1]
        xs = [gr.xy_of(c)[0] for c in reg]
        ys = [gr.xy_of(c)[1] for c in reg]
        bb = (min(xs) - gr.cell / 2, min(ys) - gr.cell / 2, max(xs) + gr.cell / 2, max(ys) + gr.cell / 2)
        involved = set()
        for c in reg:
            for l in cells[c]:
                involved |= through[(l, c)]
        near = _bordering(B, bb, gr.cell)
        o, _d, cpv = cells[wc][worst[2]]
        k = 0 if (_d[0] - cpv[0]) >= (_d[1] - cpv[1]) else 1
        dem, cap = _d[k], cpv[k]
        mem = sorted(involved)
        nm = [x for n in mem for x in gr.nets[n]["members"]]
        out.append({"code": "PLAN_OVERFLOW", "at": [round(gr.xy_of(wc)[0], 1), round(gr.xy_of(wc)[1], 1)],
                    "layer": gr.layers[worst[2]], "demand": round(dem, 1), "capacity": cap,
                    "overflow": round(tot, 1), "cells": len(reg), "box": [round(v, 1) for v in bb],
                    "nets": nm[:40], "n_nets": len(nm), "parts": near[:4],
                    "cells_xy": [[round(gr.xy_of(c)[0], 1), round(gr.xy_of(c)[1], 1)] for c in reg[:40]],
                    "msg": f"{len(reg)} cell(s) over capacity near ({gr.xy_of(wc)[0]:.1f}, {gr.xy_of(wc)[1]:.1f}), worst "
                           f"{gr.layers[worst[2]]} demand {dem:g} / capacity {cap}; overflow {tot:.1f}; {len(nm)} net(s) "
                           f"({', '.join(nm[:5])}{'...' if len(nm) > 5 else ''}); bordering {', '.join(near[:4]) or 'no part'}",
                    "suggestion": _widen_suggestion(gr, B, bb, near, tot)})
    # pin groups after routing: tracks the row's fan-out edge still has (capacity minus through traffic) + via slots vs pins
    seen_static = {(f["part"], f["side"]) for f in (gr.plan.extra if gr.plan is not None else []) if f["code"] == "PLAN_ESCAPE_OVERSUBSCRIBED"}
    for g in (gr.plan.groups if gr.plan is not None else []):
        l = gr.lid.get(g["layer"])
        if l is None or (g["ref"], g["side"]) in seen_static:
            continue
        cs = {gr.cell_of(x, y) for x, y in g["edge"]}
        own = {n for n, nt in gr.nets.items() if set(nt["members"]) & set(g["nets"])}
        sup = 0.0
        for c in cs:
            use = (gr.uH if g["dir"] == 0 else gr.uV)[l][c]
            mine = sum(gr.nets[n]["units"] for n in own if n in gr.routes and (l, c, g["dir"]) in gr.routes[n]["keys"])
            cap = (gr.capH if g["dir"] == 0 else gr.capV)[l][c]
            sup += max(0.0, cap - (use - mine))
        supply = int(sup) + g["slots"]
        if g["n"] > supply:
            out.append({"code": "PLAN_ESCAPE_OVERSUBSCRIBED", "part": g["ref"], "side": g["side"], "pin": g["pin"], "net": g["nets"][0],
                        "at": g["at"], "needed": g["n"], "slots": supply, "value": g["n"] - supply, "limit": 0,
                        "msg": f"{g['ref']} side {g['side']} ({g['layer']}): {g['n']} signal pins need escapes, the fan-out edge has "
                               f"{int(sup)} free track(s) after through traffic + {g['slots']} via slot(s) = {supply}",
                        "suggestion": f"{g['n'] - supply} pin(s) of {g['ref']}'s {g['side']} side have no escape: widen the channel in front of "
                                      f"that side (move the neighbours or the part) or move through traffic to another layer"})
    for c in range(gr.N):
        if gr.vu[c] > gr.vcap[c]:
            x, y = gr.xy_of(c)
            out.append({"code": "PLAN_OVERFLOW", "at": [round(x, 1), round(y, 1)], "layer": "via",
                        "demand": round(gr.vu[c], 1), "capacity": gr.vcap[c], "overflow": round(gr.vu[c] - gr.vcap[c], 1),
                        "cells": 1, "nets": [], "n_nets": 0, "parts": _bordering(B, (x - 1, y - 1, x + 1, y + 1), 1.0)[:4],
                        "msg": f"via capacity at ({x:.1f}, {y:.1f}): {gr.vu[c]:g} via(s) for {gr.vcap[c]} free site(s)",
                        "suggestion": "spread the parts around the cell: via sites are all taken by pads and planned escapes"})
    for name, nt in gr.nets.items():
        st = ns[name]
        info = nt["info"]
        lim = NC.effective_budget(info)
        if lim is not None and st["len"] > lim + 1e-6 and not st["unreached"]:
            ends = sorted({t["pad"].split(".")[0] for t in nt["terms"]})
            out.append({"code": "PLAN_NET_OVER_BUDGET", "net": name, "value": st["len"], "limit": round(lim, 1), "cls": info["class"],
                        "msg": f"{name} ({info['class']}) estimated copper {st['len']:.1f} mm > budget {lim:.1f} mm "
                               f"(parts {', '.join(ends[:4])})",
                        "suggestion": f"bring {' and '.join(ends[:2])} closer by {st['len'] - lim:.1f} mm"})
        if st["unreached"]:
            out.append({"code": "PLAN_UNREACHABLE", "net": name, "value": st["unreached"],
                        "msg": f"{name}: {st['unreached']} connection(s) found no path (every route is walled by capacity-0 cells)",
                        "suggestion": "open a channel from the pad group: a keep-out or part wall closes all layers"})
        if nt["rf"] and st["vias"] > nt["max_vias"]:
            out.append({"code": "PLAN_RF_LAYER_CHANGES", "net": name, "value": st["vias"], "limit": nt["max_vias"],
                        "msg": f"{name} (RF) needs {st['vias']} layer change(s) > {nt['max_vias']} allowed",
                        "suggestion": "put both ends on the same side or clear a straight outer-layer channel"})
    for pn, mem in sorted(gr.pair_origin.items()):
        out.append({"code": "PLAN_PAIR_SPLIT", "net": "/".join(mem),
                    "msg": f"pair {'/'.join(mem)} could not stay together: its {PAIR_UNITS:g}-pitch demand exceeds the "
                           f"channel, the members were routed apart",
                    "suggestion": "widen the channel between the pair's ends (see the PLAN_OVERFLOW regions it crosses)"})
    return out


def _bordering(B, bb, margin):
    e = (bb[0] - margin, bb[1] - margin, bb[2] + margin, bb[3] + margin)
    sc = []
    for ref, p in B["parts"].items():
        if ref.startswith(("MH", "MK", "FID", "TP")):
            continue
        c = p["court"]
        if c[0] < e[2] and e[0] < c[2] and c[1] < e[3] and e[1] < c[3]:
            sc.append((-p["n_pads"], ref))
    return [r for _n, r in sorted(sc)]


def _widen_suggestion(gr, B, bb, near, over):
    best = None
    P = B["parts"]
    for a in near[:6]:
        for b in near[:6]:
            if a >= b:
                continue
            ca, cb = P[a]["court"], P[b]["court"]
            gx = max(cb[0] - ca[2], ca[0] - cb[2])
            gy = max(cb[1] - ca[3], ca[1] - cb[3])
            gap, ax = (gx, "x") if gx > gy else (gy, "y")
            if gap > 0 and (best is None or gap < best[0]):
                best = (gap, a, b, ax)
    nl = max(1, len(gr.layers))
    widen = max(0.5, over * gr.pr["pitch_mm"] / nl)
    if best:
        return (f"over-subscribed channel: widen the {best[0]:.1f} mm gap between {best[1]} and {best[2]} by about "
                f"{widen:.1f} mm along {best[3]}, or move one of the bordering parts' pin groups to another side")
    return f"over-subscribed window: free ~{widen:.1f} mm of channel width around ({(bb[0]+bb[2])/2:.1f}, {(bb[1]+bb[3])/2:.1f})"
