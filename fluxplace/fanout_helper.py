"""Runs under KiCad's python (/usr/bin/python3: pcbnew + numpy), NOT the fluxplace venv.

    python3 fanout_helper.py JOB.json BOARD OUT

Builds REAL escape copper for the pins that need it (see fanout.py for the policy that fills JOB):
exact pcbnew shapes against the static board (pads, tracks, vias, holes, keep-outs, outline), capsule
geometry (numpy) between candidates, a constraint-satisfaction search (MRV + forward checking on bit-sets,
node budget, deterministic) that picks one candidate per pin so that nothing conflicts, a lane check
(a free track lane must leave every end point) and a repair loop that bans a candidate whose lane the
neighbours sealed. Writes tracks/vias into OUT (grouped "fanout:<ref>", locked) unless job.check_only.
Prints "@@FANOUT@@" + JSON result.
"""
import json
import math
import sys
import time

import numpy as np
import pcbnew

MARK = "@@FANOUT@@"
import os
TRACE = os.environ.get("FANOUT_TRACE")
V2 = pcbnew.VECTOR2I
NM = 1e6


def nm(v):
    return int(round(v * NM))


def mmv(v):
    return v / NM


# ---------------------------------------------------------------- numpy capsule geometry
def pt_seg(p, a, b):
    """distance from points p (...,2) to segments a-b (...,2); broadcasting."""
    ab = b - a
    L2 = (ab * ab).sum(-1)
    t = np.where(L2 > 1e-12, ((p - a) * ab).sum(-1) / np.where(L2 > 1e-12, L2, 1.0), 0.0)
    t = np.clip(t, 0.0, 1.0)
    q = a + ab * t[..., None]
    d = p - q
    return np.sqrt((d * d).sum(-1))


def _cross(u, v):
    return u[..., 0] * v[..., 1] - u[..., 1] * v[..., 0]


def seg_seg(a0, a1, b0, b1):
    """distance between segments (broadcasting (...,2))."""
    d = np.minimum(np.minimum(pt_seg(a0, b0, b1), pt_seg(a1, b0, b1)),
                   np.minimum(pt_seg(b0, a0, a1), pt_seg(b1, a0, a1)))
    r = a1 - a0
    s = b1 - b0
    c1 = _cross(b0 - a0, r)
    c2 = _cross(b1 - a0, r)
    c3 = _cross(a0 - b0, s)
    c4 = _cross(a1 - b0, s)
    cross = (c1 * c2 < 0) & (c3 * c4 < 0)
    return np.where(cross, 0.0, d)


def rect_dist_pt(p, r):
    dx = np.maximum(np.maximum(r[0] - p[..., 0], p[..., 0] - r[2]), 0.0)
    dy = np.maximum(np.maximum(r[1] - p[..., 1], p[..., 1] - r[3]), 0.0)
    return np.sqrt(dx * dx + dy * dy)


def seg_rect(a, b, r):
    """distance from segments a-b to rectangle r=(x0,y0,x1,y1)."""
    d = np.minimum(rect_dist_pt(a, r), rect_dist_pt(b, r))
    cs = [(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])]
    for i in range(4):
        c0 = np.array(cs[i])
        c1 = np.array(cs[(i + 1) % 4])
        d = np.minimum(d, seg_seg(a, b, c0, c1))
    return d


DIRS = [(math.cos(math.radians(a)), math.sin(math.radians(a))) for a in range(0, 360, 15)]
NDIR = len(DIRS)


# ---------------------------------------------------------------- the board model
class Obs:
    __slots__ = ("bb", "net", "layers", "shapes", "what", "kind", "hole")

    def __init__(self, bb, net, layers, shapes, what, kind, hole=False):
        self.bb, self.net, self.layers, self.shapes, self.what, self.kind, self.hole = bb, net, layers, shapes, what, kind, hole


class Model:
    def __init__(self, board, job, refs=None):
        self.refs = refs
        self.b = board
        self.job = job
        P = job["params"]
        self.P = P
        self.T = P["track"]
        self.C = P["clear"]
        self.VD = P["via"]
        self.VDR = P["drill"]
        self.same_gap = P["via_pad_gap"]
        ds = board.GetDesignSettings()
        self.edge = max(P.get("edge", 0.0), mmv(ds.m_CopperEdgeClearance))
        self.hole_clr = P.get("hole_clr", P["clear"])
        self.cu = [board.GetLayerName(l) for l in board.GetEnabledLayers().CuStack()]
        self.lid = {board.GetLayerName(l): l for l in board.GetEnabledLayers().CuStack()}
        self.sig = [l for l in job["signal_layers"] if l in self.lid]
        self.plane_layers = [l for l in self.cu if l not in self.sig]
        self.obs = []
        self.cells = {}
        self.keepouts = []
        self.netcls = {}
        self.cell = 2.0
        self.net_pad_centres = {}
        self._keep = []
        self._build()

    # -- net rules
    def net_rules(self, net):
        r = self.netcls.get(net)
        if r is None:
            nc = self.b.GetDesignSettings().m_NetSettings.GetEffectiveNetClass(net)
            info = self.job["nets"].get(net, {})
            w, cl = self.T, self.C
            if info.get("controlled"):
                cl = max(self.C, mmv(nc.GetClearance()))
                if info.get("pair"):
                    w = max(self.T, mmv(nc.GetDiffPairWidth()))
            if info.get("width"):
                w = float(info["width"])
            r = (w, cl)
            self.netcls[net] = r
        return r

    def _add(self, o):
        self.obs.append(o)
        i = len(self.obs) - 1
        x0, y0, x1, y1 = o.bb
        c = self.cell
        for ix in range(int(math.floor(x0 / c)), int(math.floor(x1 / c)) + 1):
            for iy in range(int(math.floor(y0 / c)), int(math.floor(y1 / c)) + 1):
                self.cells.setdefault((ix, iy), []).append(i)

    def _build(self):
        b = self.b
        for fp in b.GetFootprints():
            ref = fp.GetReference()
            if self.refs is not None and ref not in self.refs:
                continue
            for p in fp.Pads():
                net = p.GetNetname()
                bb = p.GetBoundingBox()
                bbm = (mmv(bb.GetLeft()), mmv(bb.GetTop()), mmv(bb.GetRight()), mmv(bb.GetBottom()))
                lay, shapes = set(), {}
                for l in self.cu:
                    if p.IsOnLayer(self.lid[l]):
                        lay.add(l)
                        shapes[l] = p.GetEffectiveShape(self.lid[l])
                if lay:
                    self._add(Obs(bbm, net, lay, shapes, f"{ref}.{p.GetNumber()}", "pad"))
                if p.GetDrillSizeX() > 0:
                    hs = p.GetEffectiveHoleShape()
                    hb = hs.BBox()
                    self._add(Obs((mmv(hb.GetLeft()), mmv(hb.GetTop()), mmv(hb.GetRight()), mmv(hb.GetBottom())), net, set(self.cu),
                                  {"*": hs}, f"{ref}.{p.GetNumber()} hole", "hole", hole=True))
        if self.refs is not None:                    # isolated module: nothing but its own parts, no outline
            self.outline = None
            self.edge_chains = []
            self.bbox = (-1e6, -1e6, 1e6, 1e6)
            return
        for t in b.GetTracks():
            self.add_track(t)
        zs = list(b.Zones())
        for fp in b.GetFootprints():
            zs += list(fp.Zones())
        for z in zs:
            if z.GetIsRuleArea() and (z.GetDoNotAllowTracks() or z.GetDoNotAllowVias()):
                lay = {self.cu[i] for i, l in enumerate(b.GetEnabledLayers().CuStack()) if z.IsOnLayer(l)}
                self.keepouts.append({"name": z.GetZoneName(), "tracks": z.GetDoNotAllowTracks(), "vias": z.GetDoNotAllowVias(),
                                      "layers": lay, "poly": z.Outline(), "bb": z.GetBoundingBox()})
        ps = pcbnew.SHAPE_POLY_SET()
        b.GetBoardPolygonOutlines(ps, True)
        self.outline = ps
        self.edge_chains = []
        for i in range(ps.OutlineCount()):
            self.edge_chains.append(ps.Outline(i))
            for h in range(ps.HoleCount(i)):
                self.edge_chains.append(ps.Hole(i, h))
        bb = ps.BBox()
        self.bbox = (mmv(bb.GetLeft()), mmv(bb.GetTop()), mmv(bb.GetRight()), mmv(bb.GetBottom()))

    def add_track(self, t):
        net = t.GetNetname()
        if t.GetClass() == "PCB_VIA":
            r = mmv(t.GetWidth(pcbnew.F_Cu)) / 2
            c = t.GetPosition()
            self._add(Obs((mmv(c.x) - r, mmv(c.y) - r, mmv(c.x) + r, mmv(c.y) + r), net, set(self.cu), {"*": t.GetEffectiveShape(pcbnew.F_Cu)}, "via", "via"))
        else:
            bb = t.GetBoundingBox()
            self._add(Obs((mmv(bb.GetLeft()), mmv(bb.GetTop()), mmv(bb.GetRight()), mmv(bb.GetBottom())), net,
                          {self.b.GetLayerName(t.GetLayer())}, {self.b.GetLayerName(t.GetLayer()): t.GetEffectiveShape()}, "track", "track"))

    def add_item(self, net, item):
        """register a chosen candidate item as a static obstacle (thermal vias)."""
        k, layer, a, b2, w = item
        if k == "via":
            r = w / 2
            v = pcbnew.PCB_VIA(self.b)
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetPosition(V2(nm(a[0]), nm(a[1])))
            v.SetWidth(nm(w))
            v.SetDrill(nm(self.VDR))
            self._keep.append(v)
            self._add(Obs((a[0] - r, a[1] - r, a[0] + r, a[1] + r), net, set(self.cu), {"*": v.GetEffectiveShape(pcbnew.F_Cu)}, "fanout via", "via"))

    def near(self, bb, pad):
        c = self.cell
        out = set()
        for ix in range(int(math.floor((bb[0] - pad) / c)), int(math.floor((bb[2] + pad) / c)) + 1):
            for iy in range(int(math.floor((bb[1] - pad) / c)), int(math.floor((bb[3] + pad) / c)) + 1):
                out.update(self.cells.get((ix, iy), ()))
        return sorted(out)

    # -- exact static tests
    def hit_seg(self, net, layer, a, b, w, clr=None, ignore=()):
        """first blocker description for a track a-b (mm) of width w on `layer`, or None."""
        wn, cl = self.net_rules(net)
        clr = cl if clr is None else clr
        sh = pcbnew.SHAPE_SEGMENT(V2(nm(a[0]), nm(a[1])), V2(nm(b[0]), nm(b[1])), nm(w))
        bb = (min(a[0], b[0]) - w / 2, min(a[1], b[1]) - w / 2, max(a[0], b[0]) + w / 2, max(a[1], b[1]) + w / 2)
        for i in self.near(bb, max(clr, self.hole_clr) + 0.02):
            o = self.obs[i]
            if layer not in o.layers or i in ignore:
                continue
            if o.net == net and net and not o.hole:
                continue
            c = self.hole_clr if o.hole else clr
            if o.bb[0] - c > bb[2] or o.bb[2] + c < bb[0] or o.bb[1] - c > bb[3] or o.bb[3] + c < bb[1]:
                continue
            osh = o.shapes.get(layer) or o.shapes.get("*")
            if osh is not None and osh.Collide(sh, nm(c)):
                return (o.what, o.kind, i)
        return self._keep_edge(net, layer, sh, bb, w, False, (a, b))

    def hit_via(self, net, p, dia, ignore=(), own_ok=()):
        wn, cl = self.net_rules(net)
        r = dia / 2
        sh = pcbnew.SHAPE_SEGMENT(V2(nm(p[0]), nm(p[1])), V2(nm(p[0]), nm(p[1])), nm(dia))
        bb = (p[0] - r, p[1] - r, p[0] + r, p[1] + r)
        for i in self.near(bb, max(cl, self.same_gap, self.hole_clr) + 0.02):
            if i in ignore:
                continue
            o = self.obs[i]
            same = o.net == net and net
            if same and o.kind in ("track", "via", "hole"):
                continue
            if same and i in own_ok:
                continue
            c = self.hole_clr if o.hole else (max(self.same_gap, cl) if same else cl)
            if o.kind == "via":
                c = max(c, self.C)
            if o.bb[0] - c > bb[2] or o.bb[2] + c < bb[0] or o.bb[1] - c > bb[3] or o.bb[3] + c < bb[1]:
                continue
            osh = o.shapes.get("*")
            if osh is None:
                for l in self.sig + self.plane_layers:
                    if l in o.shapes:
                        if o.shapes[l].Collide(sh, nm(c)):
                            return (o.what, o.kind, i)
                continue
            if osh.Collide(sh, nm(c)):
                return (o.what, o.kind, i)
        return self._keep_edge(net, None, sh, bb, dia, True, (p,))

    def _keep_edge(self, net, layer, sh, bb, w, via, pts=()):
        for k in self.keepouts:
            if (via and not k["vias"]) or (not via and (not k["tracks"] or layer not in k["layers"])):
                continue
            kb = k["bb"]
            if mmv(kb.GetLeft()) > bb[2] or mmv(kb.GetRight()) < bb[0] or mmv(kb.GetTop()) > bb[3] or mmv(kb.GetBottom()) < bb[1]:
                continue
            if k["poly"].Collide(sh, 0):
                return ("keepout " + k["name"], "keepout", -1)
        for p in pts:
            if not self.inside(p):
                return ("board edge", "edge", -2)
        if not (self.bbox[0] + self.edge < bb[0] and bb[2] < self.bbox[2] - self.edge and self.bbox[1] + self.edge < bb[1] and bb[3] < self.bbox[3] - self.edge):
            for ch in self.edge_chains:
                if via:
                    hit = ch.Collide(V2(nm(pts[0][0]), nm(pts[0][1])), nm(w / 2 + self.edge))
                else:
                    hit = ch.Collide(pcbnew.SEG(V2(nm(pts[0][0]), nm(pts[0][1])), V2(nm(pts[1][0]), nm(pts[1][1]))), nm(w / 2 + self.edge))
                if hit:
                    return ("board edge", "edge", -2)
        return None

    def inside(self, p):
        if self.outline is None:
            return True
        return self.outline.Contains(V2(nm(p[0]), nm(p[1])))


# ---------------------------------------------------------------- candidates
class Cand:
    __slots__ = ("kind", "layer", "stub", "via", "w", "cost", "ang", "slen", "cap", "items", "lanes", "end")

    def __init__(self, kind, layer, stub, via, w, cost, ang, slen):
        self.kind, self.layer, self.stub, self.via, self.w, self.cost, self.ang, self.slen = kind, layer, stub, via, w, cost, ang, slen
        self.cap = None
        self.lanes = None
        self.end = via if via is not None else stub[1]


class Pin:
    def __init__(self, ref, num, net, pad, centre, side, need):
        self.ref, self.num, self.net, self.pad, self.c, self.side, self.need = ref, num, net, pad, centre, side, need
        self.cands = []
        self.blame = {}
        self.partner = None
        self.tht = False
        self.dom = []
        self.group = None
        self.key = f"{ref}.{num}"
        self.status = None
        self.pick = None
        self.pad_obs = None


def pad_poly_pts(pad, layer):
    ps = pad.GetEffectivePolygon(layer)
    out = []
    for i in range(ps.OutlineCount()):
        ch = ps.Outline(i)
        out.append([(mmv(ch.CPoint(j).x), mmv(ch.CPoint(j).y)) for j in range(ch.PointCount())])
    return out


def pt_in_poly(pt, poly):
    x, y = pt
    c = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def poly_edge_dist(pt, poly):
    best = 1e9
    n = len(poly)
    for i in range(n):
        a = np.array(poly[i])
        b = np.array(poly[(i + 1) % n])
        best = min(best, float(pt_seg(np.array(pt), a, b)))
    return best


class Fan:
    def __init__(self, board, job, log, refs=None):
        self.b = board
        self.job = job
        self.log = log
        self.P = job["params"]
        self.M = Model(board, job, refs)
        self.pins = []
        self.t0 = time.time()
        self.thermal = []
        self.fps = {fp.GetReference(): fp for fp in board.GetFootprints()}
        self.pad_of = {}

    # -- pin extraction ------------------------------------------------
    def collect(self):
        job = self.job
        M = self.M
        parts = job["parts"]
        nets = job["nets"]
        fps = {fp.GetReference(): fp for fp in self.b.GetFootprints()}
        want = sorted(r for r in parts if r in fps)
        self.fps = fps
        for ref in want:
            mode = parts[ref]["mode"]
            if mode == "skip":
                continue
            fp = fps[ref]
            side = "B" if fp.IsFlipped() else "F"
            pads = [p for p in fp.Pads() if p.GetNetname() and p.GetAttribute() in (pcbnew.PAD_ATTRIB_SMD, pcbnew.PAD_ATTRIB_PTH)]
            for p in pads:
                net = p.GetNetname()
                ni = nets.get(net, {})
                c = p.GetPosition()
                cm = (mmv(c.x), mmv(c.y))
                plane = bool(ni.get("plane"))
                if plane and ni.get("region") and not in_region(ni["region"], cm):
                    plane = False
                tht = p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
                if plane:
                    need = "plane"
                    if tht:
                        continue                                   # the barrel reaches the plane
                elif mode == "all" and ni.get("npads", 2) >= 2:
                    need = "signal"
                else:
                    continue
                if (ref, str(p.GetNumber())) in self.thermal_set:
                    continue
                if self.already(fp, p, net):
                    continue
                pin = Pin(ref, p.GetNumber(), net, p, cm, side, need)
                pin.tht = tht
                self.pins.append(pin)
        self.pins.sort(key=lambda q: (q.ref, str_num(q.num)))

    def already(self, fp, p, net):
        """a pad that already has a same-net track or via ending in it is escaped."""
        return False

    # -- candidate generation -------------------------------------------
    def part_court(self, ref):
        fp = self.fps[ref]
        side = pcbnew.F_CrtYd if not fp.IsFlipped() else pcbnew.B_CrtYd
        bb = fp.GetCourtyard(side).BBox()
        if bb.GetWidth() <= 0:
            bb = fp.GetBoundingBox(False)
        return (mmv(bb.GetLeft()), mmv(bb.GetTop()), mmv(bb.GetRight()), mmv(bb.GetBottom()))

    def gen(self, pin):
        M, P = self.M, self.P
        w, cl = M.net_rules(pin.net)
        court = self.part_court(pin.ref)
        cx, cy = pin.c
        layer0 = M.cu[0] if pin.side == "F" else M.cu[-1]
        pad_ids = [i for i in M.near((cx, cy, cx, cy), 0.6) if M.obs[i].what == f"{pin.ref}.{pin.num}" and M.obs[i].kind == "pad"]
        pin.pad_obs = set(pad_ids)
        cands = []
        blame = {}
        reach = P["reach"]
        layers = [layer0] if not pin.tht else list(M.sig)
        coupled = self.job["coupling"].get(f"{pin.ref}.{pin.num}") if "coupling" in self.job else None
        vlay = [layer0]
        pitch = 0.1
        ncount = 0
        if pin.need == "plane" or True:
            rho = 0.25
            rhos = []
            while rho <= reach + 1e-9:
                rhos.append(rho)
                rho += pitch
            if not pin.tht:
                for ia, (dx, dy) in enumerate(DIRS):
                    for rho in rhos:
                        v = (cx + dx * rho, cy + dy * rho)
                        if not M.inside(v):
                            continue
                        h = M.hit_via(pin.net, v, M.VD, own_ok=())
                        if TRACE == pin.key:
                            print("via", ia, rho, v, h, file=sys.stderr)
                        if h:
                            blame[h[0]] = blame.get(h[0], 0) + 1
                            continue
                        h = M.hit_seg(pin.net, layer0, (cx, cy), v, w, ignore=pin.pad_obs)
                        if h:
                            blame[h[0]] = blame.get(h[0], 0) + 1
                            continue
                        pen = 0.0
                        cap = self.job.get("away", {}).get(pin.key)
                        if cap:
                            pen = 0.6 * max(0.0, dx * cap[0] + dy * cap[1])
                        c = Cand("via", layer0, ((cx, cy), v), v, w, rho + P["via_cost"] + pen, ia, rho)
                        cands.append(c)
        if pin.need == "signal":
            # direct pad-out candidates: leave the courtyard
            for ia, (dx, dy) in enumerate(DIRS):
                ex = exit_dist((cx, cy), (dx, dy), court)
                if ex is None or ex > reach:
                    continue
                for extra in (0.25, 0.8):
                    ln = ex + extra
                    e = (cx + dx * ln, cy + dy * ln)
                    if not M.inside(e):
                        continue
                    for L in layers:
                        h = M.hit_seg(pin.net, L, (cx, cy), e, w, ignore=pin.pad_obs)
                        if h:
                            blame[h[0]] = blame.get(h[0], 0) + 1
                            continue
                        cands.append(Cand("direct", L, ((cx, cy), e), None, w, ln * 0.5, ia, ln))
        pin.blame = blame
        return cands

    def lanes_static(self, pin, cd, limit=None):
        """[(dir index, layer)] of lanes leaving cd.end that are free of static obstacles."""
        M, P = self.M, self.P
        ex, ey = cd.end
        w, cl = M.net_rules(pin.net)
        Ls = [cd.layer] if cd.kind == "direct" else list(M.sig)
        out = []
        pref = []
        for ia, (dx, dy) in enumerate(DIRS):
            pref.append((-(dx * (ex - pin.c[0]) + dy * (ey - pin.c[1])), ia))
        for _, ia in sorted(pref):
            dx, dy = DIRS[ia]
            e2 = (ex + dx * P["lane"], ey + dy * P["lane"])
            if not M.inside(e2):
                continue
            for L in Ls:
                ign = pin.pad_obs if L == cd.layer or cd.kind == "via" else ()
                if M.hit_seg(pin.net, L, (ex, ey), e2, w, ignore=()):
                    continue
                out.append((ia, L))
                if limit and len(out) >= limit:
                    return out
        return out

    def build_domains(self):
        P = self.P
        for pin in self.pins:
            cands = self.gen(pin)
            if pin.need == "signal":
                keep = []
                for cd in cands:
                    ln = self.lanes_static(pin, cd, limit=1)
                    if ln:
                        keep.append(cd)
                    else:
                        pin.blame["no free lane at end"] = pin.blame.get("no free lane at end", 0) + 1
                cands = keep
            seen = {}
            cands.sort(key=lambda c: (round(c.cost, 4), c.ang, c.layer))
            keep = []
            for cd in cands:
                k = (cd.kind, cd.ang, cd.layer)
                if seen.get(k, 0) >= P["per_dir"]:
                    continue
                seen[k] = seen.get(k, 0) + 1
                keep.append(cd)
            pin.cands = keep[:P["max_dom"]]
            pin.nstatic = len(pin.cands)

    # -- pairing ------------------------------------------------------
    def link_pairs(self):
        nets = self.job["nets"]
        by = {}
        for pin in self.pins:
            by.setdefault((pin.ref, pin.net), []).append(pin)
        done = set()
        for pin in self.pins:
            part = nets.get(pin.net, {}).get("pair")
            if not part or pin.need != "signal" or id(pin) in done:
                continue
            others = [q for q in by.get((pin.ref, part), []) if id(q) not in done]
            if not others:
                continue
            q = min(others, key=lambda q: math.hypot(q.c[0] - pin.c[0], q.c[1] - pin.c[1]))
            if math.hypot(q.c[0] - pin.c[0], q.c[1] - pin.c[1]) > 4.0:
                continue
            pin.partner, q.partner = q, pin
            done.add(id(pin))
            done.add(id(q))

    # -- conflict arcs --------------------------------------------------
    def caps(self, pin, cd):
        """capsules [(layer or None, a, b, r)] of a candidate."""
        out = [(cd.layer, np.array(cd.stub[0]), np.array(cd.stub[1]), cd.w / 2)]
        if cd.via is not None:
            out.append((None, np.array(cd.via), np.array(cd.via), self.M.VD / 2))
        return out

    def arc(self, i, j):
        """boolean matrix compat[a][b] between domains of pins i and j."""
        pi, pj = self.pins[i], self.pins[j]
        same = pi.net == pj.net
        ci, cj = pi.cands, pj.cands
        na, nb = len(ci), len(cj)
        if not na or not nb:
            return None
        wi, cli = self.M.net_rules(pi.net)
        wj, clj = self.M.net_rules(pj.net)
        clr = max(cli, clj)
        bad = np.zeros((na, nb), bool)

        def arr(cs, which):
            # which: 0 stub, 1 via ; returns a0,a1,r,layer-ids,valid
            n = len(cs)
            a0 = np.zeros((n, 2))
            a1 = np.zeros((n, 2))
            r = np.zeros(n)
            lay = np.full(n, -1)
            ok = np.zeros(n, bool)
            for k, cd in enumerate(cs):
                if which == 0:
                    a0[k], a1[k], r[k] = cd.stub[0], cd.stub[1], cd.w / 2
                    lay[k] = self.M.cu.index(cd.layer)
                    ok[k] = True
                elif cd.via is not None:
                    a0[k] = a1[k] = cd.via
                    r[k] = self.M.VD / 2
                    ok[k] = True
            return a0, a1, r, lay, ok

        Ai = [arr(ci, 0), arr(ci, 1)]
        Bj = [arr(cj, 0), arr(cj, 1)]
        for ki in (0, 1):
            for kj in (0, 1):
                if same and not (ki == 1 and kj == 1):
                    continue
                a0, a1, ra, la, oka = Ai[ki]
                b0, b1, rb, lb, okb = Bj[kj]
                d = seg_seg(a0[:, None, :], a1[:, None, :], b0[None, :, :], b1[None, :, :])
                need = ra[:, None] + rb[None, :] + (self.M.C if same else clr)
                hit = d < need - 1e-9
                m = oka[:, None] & okb[None, :]
                if ki == 0 and kj == 0:
                    m = m & (la[:, None] == lb[None, :])
                bad |= hit & m
        compat = ~bad
        if pi.partner is pj:
            sym = np.zeros((na, nb), bool)
            for a, x in enumerate(ci):
                for b2, y in enumerate(cj):
                    sym[a, b2] = x.kind == y.kind and abs(x.slen - y.slen) <= self.P["pair_len_tol"] and (x.layer == y.layer or x.kind == "via")
            compat &= sym
        return compat

    def build_arcs(self):
        n = len(self.pins)
        R = 2 * self.P["reach"] + 2 * self.P["lane"] + 0.8
        cells = {}
        for i, p in enumerate(self.pins):
            cells.setdefault((int(p.c[0] // R), int(p.c[1] // R)), []).append(i)
        self.adj = [dict() for _ in range(n)]
        for i, p in enumerate(self.pins):
            kx, ky = int(p.c[0] // R), int(p.c[1] // R)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in cells.get((kx + dx, ky + dy), ()):
                        if j <= i:
                            continue
                        q = self.pins[j]
                        if math.hypot(p.c[0] - q.c[0], p.c[1] - q.c[1]) > R:
                            continue
                        if p.side != q.side and not (p.tht or q.tht) and p.need == "plane" and q.need == "plane":
                            pass
                        m = self.arc(i, j)
                        if m is None:
                            continue
                        if m.all():
                            continue
                        # bitsets: for a in Di, mask over Dj ; for b in Dj mask over Di
                        ma = [int("".join("1" if v else "0" for v in row[::-1]), 2) for row in m]
                        mb = [int("".join("1" if v else "0" for v in col[::-1]), 2) for col in m.T]
                        self.adj[i][j] = ma
                        self.adj[j][i] = mb

    # -- search ---------------------------------------------------------
    def solve(self, banned):
        """returns {pin index: cand index}. components solved separately."""
        n = len(self.pins)
        dom = []
        for i, p in enumerate(self.pins):
            m = 0
            for k in range(len(p.cands)):
                if (i, k) not in banned:
                    m |= 1 << k
            dom.append(m)
        seen = [False] * n
        sol = {}
        for s in range(n):
            if seen[s]:
                continue
            comp, st = [], [s]
            seen[s] = True
            while st:
                u = st.pop()
                comp.append(u)
                for v in self.adj[u]:
                    if not seen[v]:
                        seen[v] = True
                        st.append(v)
            sol.update(self._solve_comp(sorted(comp), dom))
        return sol

    def _solve_comp(self, comp, dom0):
        P = self.P
        budget = [P["nodes"]]
        best = {"n": -1, "asg": {}}
        dom = {i: dom0[i] for i in comp}
        order_all = comp

        def first_bits(m, limit=None):
            out = []
            while m:
                low = m & -m
                out.append(low.bit_length() - 1)
                m ^= low
                if limit and len(out) >= limit:
                    break
            return out

        asg = {}

        def rec(dom, nasg, nskip):
            if budget[0] <= 0:
                return True
            budget[0] -= 1
            if nasg > best["n"] or (nasg == best["n"] and nskip < best.get("skip", 1e9)):
                best["n"], best["asg"], best["skip"] = nasg, dict(asg), nskip
            if nasg + nskip == len(comp):
                return nskip == 0
            if best["n"] == len(comp):
                return True
            # MRV among unassigned/unskipped
            cand = [i for i in comp if i not in asg and i not in skipped]
            v = min(cand, key=lambda i: (bin(dom[i]).count("1"), i))
            bits = first_bits(dom[v])
            for k in bits:
                nd = dict(dom)
                ok = True
                for j, ma in self.adj[v].items():
                    if j in asg:
                        continue
                    nd[j] = nd[j] & ma[k] if j in nd else 0
                    if nd[j] == 0 and j not in skipped:
                        ok = False
                        break
                if not ok:
                    continue
                asg[v] = k
                if rec(nd, nasg + 1, nskip):
                    return True
                del asg[v]
                if budget[0] <= 0:
                    return True
            # skip v (blocked) and go on
            if nskip + 1 <= best.get("skip_cap", 10 ** 9):
                skipped.add(v)
                r = rec(dom, nasg, nskip + 1)
                skipped.discard(v)
                return r
            return False

        skipped = set()
        # empty-domain pins are skipped up front
        for i in comp:
            if dom[i] == 0:
                skipped.add(i)
        base_skip = len(skipped)
        sys.setrecursionlimit(100000)
        best["skip_cap"] = 10 ** 9
        # Restrict: when the whole component is satisfiable DFS returns fast; otherwise budget caps it.
        rec(dom, 0, base_skip)
        return dict(best["asg"])

    # -- lanes after the search --------------------------------------------
    def dynamic_lanes(self, sol):
        """returns pins index list whose chosen candidate has no free lane given the other choices."""
        M, P = self.M, self.P
        bad = []
        placed = []
        for i, k in sol.items():
            placed.append((i, self.pins[i].cands[k]))
        for i, k in sol.items():
            pin = self.pins[i]
            cd = pin.cands[k]
            if pin.need != "signal":
                continue
            lanes = self.lanes_static(pin, cd)
            w, cl = M.net_rules(pin.net)
            ok = None
            for ia, L in lanes:
                dx, dy = DIRS[ia]
                e = np.array(cd.end)
                e2 = e + np.array((dx, dy)) * P["lane"]
                free = True
                for j, cj in placed:
                    if j == i:
                        continue
                    q = self.pins[j]
                    if q.net == pin.net:
                        continue
                    if abs(q.c[0] - pin.c[0]) > 8 or abs(q.c[1] - pin.c[1]) > 8:
                        continue
                    for (lj, a, b2, r) in self.caps(q, cj):
                        if lj is not None and lj != L:
                            continue
                        d = float(seg_seg(e, e2, a, b2))
                        if d < w / 2 + r + max(cl, M.net_rules(q.net)[1]) - 1e-9:
                            free = False
                            break
                    if not free:
                        break
                if free:
                    ok = (ia, L)
                    break
            cd.lanes = ok
            if ok is None:
                bad.append(i)
        return bad

    # -- thermal pads -----------------------------------------------------
    def thermal_vias(self):
        """vias inside exposed GND/plane pads on a grid (allowed inside the pad)."""
        P, M, job = self.P, self.M, self.job
        out = []
        for ref in sorted(job["parts"]):
            if job["parts"][ref]["mode"] == "skip" or ref not in self.fps:
                continue
            fp = self.fps[ref]
            for p in fp.Pads():
                net = p.GetNetname()
                ni = job["nets"].get(net, {})
                if not net or not ni.get("ground") or p.GetAttribute() != pcbnew.PAD_ATTRIB_SMD or not _is_ic(ref):
                    continue
                sz = p.GetSize()
                area = mmv(sz.x) * mmv(sz.y)
                if area < P["thermal_area"] or min(mmv(sz.x), mmv(sz.y)) < 1.2:
                    continue
                lay = pcbnew.F_Cu if not fp.IsFlipped() else pcbnew.B_Cu
                polys = pad_poly_pts(p, lay)
                if not polys:
                    continue
                poly = polys[0]
                xs, ys = [q[0] for q in poly], [q[1] for q in poly]
                pit = P["thermal_pitch"]
                m = M.VD / 2 + P["thermal_edge"]
                nx = int((max(xs) - min(xs) - 2 * m) // pit) + 1
                ny = int((max(ys) - min(ys) - 2 * m) // pit) + 1
                cxm, cym = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
                pads_i = [i for i in M.near((cxm, cym, cxm, cym), 0.5) if M.obs[i].what == f"{ref}.{p.GetNumber()}" and M.obs[i].kind == "pad"]
                placed = []
                for ix in range(max(nx, 1)):
                    for iy in range(max(ny, 1)):
                        x = cxm + (ix - (nx - 1) / 2) * pit
                        y = cym + (iy - (ny - 1) / 2) * pit
                        if not pt_in_poly((x, y), poly) or poly_edge_dist((x, y), poly) < m - 1e-6:
                            continue
                        if M.hit_via(net, (x, y), M.VD, own_ok=set(pads_i)):
                            continue
                        placed.append((x, y))
                        M.add_item(net, ("via", None, (x, y), None, M.VD))
                if placed:
                    out.append({"ref": ref, "pin": str(p.GetNumber()), "net": net, "vias": placed, "area": round(area, 2)})
        return out


def _is_ic(ref):
    return ref.startswith("U") or ref.startswith("IC")


def exit_dist(p, d, court):
    """distance along d from p to leave the courtyard box (None if p outside / never)."""
    t = 1e9
    x0, y0, x1, y1 = court
    if abs(d[0]) > 1e-9:
        t = min(t, ((x1 if d[0] > 0 else x0) - p[0]) / d[0])
    if abs(d[1]) > 1e-9:
        t = min(t, ((y1 if d[1] > 0 else y0) - p[1]) / d[1])
    if t >= 1e8:
        return None
    return max(t, 0.0)


def in_region(region, pt):
    if region == "board" or region is None:
        return True
    if isinstance(region[0], (int, float)):
        return region[0] <= pt[0] <= region[2] and region[1] <= pt[1] <= region[3]
    return pt_in_poly(pt, [tuple(q) for q in region])


def str_num(s):
    try:
        return (0, int(s), "")
    except ValueError:
        return (1, 0, str(s))


def main_modules(job, board):
    """job["modules"] = {name: {"refs": [...], "origin": [x, y]}}: the fan-out of each module in isolation (its own parts only),
    returned as geometry relative to the module origin."""
    res = {}
    for name in sorted(job["modules"]):
        m = job["modules"][name]
        refs = set(m["refs"])
        sub = dict(job)
        sub["parts"] = {r: v for r, v in job["parts"].items() if r in refs}
        F = Fan(board, sub, [], refs)
        F.thermal_data = F.thermal_vias()
        F.thermal_set = {(t["ref"], t["pin"]) for t in F.thermal_data}
        F.collect()
        F.build_domains()
        F.link_pairs()
        F.build_arcs()
        banned = set()
        sol = {}
        for it in range(job["params"]["repairs"]):
            sol = F.solve(banned)
            bad = F.dynamic_lanes(sol)
            if not bad:
                break
            for i in bad:
                banned.add((i, sol[i]))
        r = F.report(sol, banned, (0, 0, 0, 0, 0))
        ox, oy = m["origin"]
        pins = []
        for d in r["pins"]:
            e = {k: d[k] for k in ("ref", "pin", "net", "need", "ok")}
            for k in ("kind", "layer", "w"):
                if k in d:
                    e[k] = d[k]
            for k in ("end", "via"):
                if d.get(k):
                    e[k] = [round(d[k][0] - ox, 3), round(d[k][1] - oy, 3)]
            if d.get("stub"):
                e["stub"] = [[round(q[0] - ox, 3), round(q[1] - oy, 3)] for q in d["stub"]]
            if d.get("lane"):
                e["lane"] = d["lane"]
            if not d["ok"]:
                e["blocker"] = d.get("blocker")
            pins.append(e)
        res[name] = {"pins": pins, "thermal": [{"ref": t["ref"], "pin": t["pin"], "vias": [[round(x - ox, 3), round(y - oy, 3)] for x, y in t["vias"]]}
                                                for t in F.thermal_data]}
    print(MARK + json.dumps(res))


def main():
    job = json.load(open(sys.argv[1]))
    board = pcbnew.LoadBoard(sys.argv[2])
    if job.get("modules"):
        return main_modules(job, board)
    out = sys.argv[3]
    log = []
    t0 = time.time()
    F = Fan(board, job, log)
    F.thermal_data = F.thermal_vias()
    F.thermal_set = {(t["ref"], t["pin"]) for t in F.thermal_data}
    F.collect()
    tc = time.time()
    F.build_domains()
    F.link_pairs()
    td = time.time()
    F.build_arcs()
    ta = time.time()
    banned = set()
    sol = {}
    for it in range(job["params"]["repairs"]):
        sol = F.solve(banned)
        bad = F.dynamic_lanes(sol)
        # a to-cap candidate is only valid when its cap pin got its via (handled by the wrapper: none yet)
        if not bad:
            break
        for i in bad:
            banned.add((i, sol[i]))
    res = F.report(sol, banned, (t0, tc, td, ta, time.time()))
    if not job.get("check_only"):
        F.write(sol, out)
        res["written"] = out
    print(MARK + json.dumps(res))


def _report(self, sol, banned, tm):
    pins = []
    M = self.M
    for i, p in enumerate(self.pins):
        k = sol.get(i)
        d = {"ref": p.ref, "pin": str(p.num), "net": p.net, "need": p.need, "ok": False, "n": len(p.cands)}
        if k is not None:
            cd = p.cands[k]
            lane = cd.lanes if cd.lanes else None
            ok = p.need == "plane" or lane is not None
            d.update({"ok": ok, "kind": cd.kind, "layer": cd.layer, "stub": [list(map(lambda v: round(v, 4), q)) for q in cd.stub],
                      "via": [round(cd.via[0], 4), round(cd.via[1], 4)] if cd.via else None, "w": cd.w,
                      "end": [round(cd.end[0], 4), round(cd.end[1], 4)]})
            if lane:
                d["lane"] = [round(math.degrees(math.atan2(DIRS[lane[0]][1], DIRS[lane[0]][0])), 1), lane[1]]
        else:
            d["blocker"], d["dist"] = self.blame_of(i, sol)
            d["blame"] = sorted(p.blame.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
            d["nstatic"] = len(p.cands)
        pins.append(d)
    return {"pins": pins, "thermal": self.thermal_data, "time": {"collect": round(tm[1] - tm[0], 1), "domains": round(tm[2] - tm[1], 1),
                                                                 "arcs": round(tm[3] - tm[2], 1), "solve": round(tm[4] - tm[3], 1)},
            "repairs": len(banned)}


def _blame_of(self, i, sol):
    p = self.pins[i]
    M = self.M
    if not p.cands:
        if p.blame:
            top = sorted(p.blame.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            return top, self.gap_to(p, top)
        return "no candidate site", None
    # competition: which assigned neighbour kills most of the domain
    cnt = {}
    for j, ma in self.adj[i].items():
        if j not in sol:
            continue
        # arc matrix from i's view: adj[i][j] is a list over Di of masks over Dj
        k = sol[j]
        killed = sum(1 for a in range(len(p.cands)) if not (self.adj[i][j][a] >> k) & 1)
        cnt[j] = killed
    if cnt:
        j = max(sorted(cnt), key=lambda x: cnt[x])
        q = self.pins[j]
        return f"escape of {q.key} ({q.net})", round(math.hypot(p.c[0] - q.c[0], p.c[1] - q.c[1]), 2)
    if p.need == "signal":
        return "lane sealed by neighbouring escapes", None
    return "search budget", None


def _gap_to(self, p, what):
    M = self.M
    if what in ("board edge",) or what.startswith("keepout"):
        return None
    best = None
    for i in M.near((p.c[0], p.c[1], p.c[0], p.c[1]), 3.0):
        o = M.obs[i]
        if o.what == what:
            d = math.hypot((o.bb[0] + o.bb[2]) / 2 - p.c[0], (o.bb[1] + o.bb[3]) / 2 - p.c[1])
            if best is None or d < best:
                best = d
    return None if best is None else round(best, 2)


def _write(self, sol, out):
    b = self.b
    M = self.M
    groups = {}
    done_vias = {}
    n = 0
    for ref in sorted({p.ref for p in self.pins}):
        # remove an older fanout group of this part
        for g in list(b.Groups()):
            if g.GetName() == f"fanout:{ref}":
                for it in list(g.GetItems()):
                    b.Remove(it)
                b.Remove(g)
    for i, k in sorted(sol.items()):
        p = self.pins[i]
        cd = p.cands[k]
        if p.need == "signal" and cd.lanes is None:
            continue
        g = groups.get(p.ref)
        if g is None:
            g = pcbnew.PCB_GROUP(b)
            g.SetName(f"fanout:{p.ref}")
            b.Add(g)
            groups[p.ref] = g
        net = b.FindNet(p.net)
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(V2(nm(cd.stub[0][0]), nm(cd.stub[0][1])))
        t.SetEnd(V2(nm(cd.stub[1][0]), nm(cd.stub[1][1])))
        t.SetWidth(nm(cd.w))
        t.SetLayer(M.lid[cd.layer])
        t.SetNet(net)
        t.SetLocked(True)
        b.Add(t)
        g.AddItem(t)
        n += 1
        if cd.via is not None:
            v = pcbnew.PCB_VIA(b)
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetPosition(V2(nm(cd.via[0]), nm(cd.via[1])))
            v.SetWidth(nm(M.VD))
            v.SetDrill(nm(M.VDR))
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            v.SetNet(net)
            v.SetLocked(True)
            b.Add(v)
            g.AddItem(v)
            n += 1
    for th in self.thermal_data:
        net = b.FindNet(th["net"])
        g = groups.get(th["ref"])
        if g is None:
            g = pcbnew.PCB_GROUP(b)
            g.SetName(f"fanout:{th['ref']}")
            b.Add(g)
            groups[th["ref"]] = g
        for (x, y) in th["vias"]:
            v = pcbnew.PCB_VIA(b)
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetPosition(V2(nm(x), nm(y)))
            v.SetWidth(nm(M.VD))
            v.SetDrill(nm(M.VDR))
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            v.SetNet(net)
            v.SetLocked(True)
            b.Add(v)
            g.AddItem(v)
            n += 1
    b.Save(out)


Fan.report = _report
Fan.blame_of = _blame_of
Fan.gap_to = _gap_to
Fan.write = _write

if __name__ == "__main__":
    main()
