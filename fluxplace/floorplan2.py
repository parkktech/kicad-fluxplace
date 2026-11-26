"""`fluxplace floorplan BOARD --constraints C --out O [--grow L,R,T,B] [--size]`: place MODULES (modules.py) as rigid blocks.

Blocks translate and turn (0/90/180/270). Side flips are NOT searched: every block keeps its side (the lid side carries the
connectors and the M.2 socket by decision, and a flip mirrors the footprint); `[floorplan] flip = [...]` is reserved. Fixed modules
(`[placement] fixed`, `[lock.at]`, anchors, mounting hardware; minus `[floorplan] movable = [refs]`) stay where they are.

Objective, lexicographic in effect (hard constraint, then gates, then a weighted sum):
  1. LEGAL: every member legal by legality.Legal (courtyards, outline + edge clearance, keep-outs, no-THT, pad shorts); hard.
  2. FAN-OUT: after every search the whole-board fan-out is re-run (fanout.py); the modules that own a blocked pin get a halo
     (`--halo`, grows by 0.4 mm a round) and are searched again, up to `--rounds`. Passing = FANOUT_OK.
  3. crossings (port-to-port, ports = the pins of one net inside one block; edges chained by x) x `w_cross`, plus weighted
     wire length: sum over nets of class weight x pin half-perimeter, plus a penalty over the net budget (RF and pairs shortest);
     plane/pour nets are excluded (they leave by via).
  4. channel overflow: plan-route's calibrated global router on the result, reported (not searched).
  5. compactness: bounding box of the movable blocks, small weight.
Search: constructive (blocks in order of connection to what is already placed; candidate poses on a grid around the weighted
centroid of the connected ports, cheapest legal first), then annealing with translate / rotate / swap / jump-to-centroid moves,
fixed iteration budget and seed (deterministic).
Sizing (`--size`): when density or the fan-out says the outline is too small it grows one edge by 1 mm at a time (cheapest first:
highest band utilisation per mm of edge), up to `--max-grow` per edge; edge-hugging fixed parts ride with their edge; the plane
zones grow with the board. Reports the minimal passing size and the first failing reason at every smaller size.
"""
import argparse
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict

import numpy as np

from . import geom as GM
from . import modules as MD

THETA = (0, 90, 180, 270)
CS = {0: (1.0, 0.0), 90: (0.0, 1.0), 180: (-1.0, 0.0), 270: (0.0, -1.0)}      # (cos, sin) of KiCad rotation


# ---------------------------------------------------------------- model
def block_spread(X, Y, fixed, groups):
    """sum over functional blocks (groups: {name: index array}) of the x + y extent of their modules; a block of fixed
    modules only is not the optimiser's to pull and counts 0."""
    t = 0.0
    for idx in groups.values():
        if (~np.asarray(fixed)[idx]).any():
            t += float(np.ptp(X[idx]) + np.ptp(Y[idx]))
    return t


class Plan:
    def __init__(self, B, cons, spec, M, log=print, halo=None, base=None):
        from . import legality as LGL, netclass as NC, escapeplan as EP
        self.B, self.cons, self.spec, self.log = B, cons, spec, log
        self.mods = M["modules"]
        self.of = M["of"]
        self.n = len(self.mods)
        self.idx = {m["name"]: i for i, m in enumerate(self.mods)}
        self.fixed = np.array([m["fixed"] for m in self.mods])
        # [placement] use_blocks: the modules of one functional block are pulled together (cohesion term, `block`);
        # weight `[floorplan] block_weight` (1.0). Off by default: no term, the objective is unchanged.
        self.blk_w = float((cons.get("floorplan") or {}).get("block_weight", 1.0)) if (cons.get("placement") or {}).get("use_blocks") else 0.0
        groups = defaultdict(list)
        for i, m in enumerate(self.mods):
            if m.get("block") is not None:
                groups[m["block"]].append(i)
        self.blk = {k: np.array(v) for k, v in groups.items() if len(v) > 1} if self.blk_w > 0 else {}
        self.X = np.array([m["pos"][0] for m in self.mods])
        self.Y = np.array([m["pos"][1] for m in self.mods])
        self.T = np.zeros(self.n, int)
        self.src = (self.X.copy(), self.Y.copy())
        # pins.py: a unit released from its pin stays near the pose it was re-seated at (`[floorplan] stay`); absent = no term
        st = (cons.get("floorplan") or {}).get("stay") or {}
        sr = set(st.get("refs", []) or [])
        self.stay = np.array([float(st.get("weight", 5.0)) if sr & set(m["members"]) else 0.0 for m in self.mods])
        self.halo = np.zeros(self.n)
        fx = MD.fixed_set(B, cons)
        self.L = LGL.Legal(B, cons, fx)
        self.cls = NC.classify(B, cons, spec)
        pr = EP.plan_params(cons)
        self.plane = set(EP.plane_nets(B, cons, self.cls, pr))
        self._nets(NC)
        self.base_issues = base if base is not None else self.L.issues()      # `base`: the issues of the INPUT board; without it the board as handed in (already seated)
        self._ints = {}
        self.init_raster()
        self.cur = [(float(m['pos'][0]), float(m['pos'][1]), 0) for m in self.mods]
        self.parked, self.noseat = set(), set()        # modules whose parts stand at their input poses / that found no legal pose
        self.claims_rel = {}
        self.chash = GM.Hash(3.0)
        self.ko_rel, self.khash = {}, GM.Hash(3.0)             # --- module keep-outs (datasheet layout template): rects rel. to the anchor, all layers
        self.bo_rel, self.bhash = {}, GM.Hash(3.0)             # --- breakout (breakout.py): lane regions per module, hashed in world coordinates
        self.bo_reg = []
        self.bo_w = float((cons.get("floorplan") or {}).get("breakout_weight", 6.0))
        self.bo_apron = float((cons.get("floorplan") or {}).get("breakout_apron_mm", 2.0))
        from . import intent as IT
        self.intent = IT.PlanTerms(self)                      # --- intent: away penalty / legality

    # -- nets
    def _nets(self, NC):
        from . import intent as IT
        pads = dict(NC.net_pads(self.B))
        links, link_info = IT.plan_links(self.B, self.cons)   # --- intent: `near` items as two-pin nets with a budget
        pads.update(links)
        pm, px, py, pn = [], [], [], []
        self.net_name, self.net_w, self.net_budget = [], [], []
        ports = defaultdict(lambda: defaultdict(list))
        for net in sorted(pads):
            ps = pads[net]
            if net in self.plane or len(ps) < 2:
                continue
            mi = {self.idx[self.of[q["ref"]]] for q in ps}
            if len(mi) < 2:
                continue
            c = self.cls.get(net) or link_info.get(net, {})
            ni = len(self.net_name)
            self.net_name.append(net)
            self.net_w.append(float(c.get("weight", 1.0)))
            self.net_budget.append(c.get("budget") if c.get("budget") is not None else 1e9)
            for q in ps:
                m = self.idx[self.of[q["ref"]]]
                a = self.mods[m]["pos"]
                pm.append(m)
                px.append(q["x"] - a[0])
                py.append(q["y"] - a[1])
                pn.append(ni)
                ports[ni][m].append((q["x"] - a[0], q["y"] - a[1]))
        self.pm, self.px, self.py, self.pn = (np.array(v) for v in (pm, px, py, pn))
        self.net_w = np.array(self.net_w)
        self.net_budget = np.array(self.net_budget, float)
        # sorted by net for reduceat
        o = np.argsort(self.pn, kind="stable")
        self.pm, self.px, self.py, self.pn = self.pm[o], self.px[o], self.py[o], self.pn[o]
        self.starts = np.searchsorted(self.pn, np.arange(len(self.net_name)))
        # ports: one per (net, module)
        pt_n, pt_m, pt_x, pt_y = [], [], [], []
        for ni in sorted(ports):
            for m in sorted(ports[ni]):
                v = ports[ni][m]
                pt_n.append(ni)
                pt_m.append(m)
                pt_x.append(sum(a for a, b in v) / len(v))
                pt_y.append(sum(b for a, b in v) / len(v))
        self.tn, self.tm, self.tx, self.ty = (np.array(v) for v in (pt_n, pt_m, pt_x, pt_y))
        self.tstarts = np.searchsorted(self.tn, np.arange(len(self.net_name)))

    def world(self, m, x, y):
        """world coords of rel points (arrays) of module indices m."""
        th = self.T[m]
        c = np.array([CS[t][0] for t in th]) if len(th) else np.zeros(0)
        s = np.array([CS[t][1] for t in th]) if len(th) else np.zeros(0)
        return self.X[m] + x * c + y * s, self.Y[m] + (-x * s + y * c)

    def cost_parts(self, w_cross=4.0):
        wx, wy = self.world(self.pm, self.px, self.py)
        x0 = np.minimum.reduceat(wx, self.starts)
        x1 = np.maximum.reduceat(wx, self.starts)
        y0 = np.minimum.reduceat(wy, self.starts)
        y1 = np.maximum.reduceat(wy, self.starts)
        hp = (x1 - x0) + (y1 - y0)
        wl = float((self.net_w * hp).sum())
        over = float((self.net_w * np.maximum(0.0, hp - self.net_budget)).sum())
        cr = self.crossings()
        mov = ~self.fixed
        if mov.any():
            bx = float(np.ptp(self.X[mov])) + float(np.ptp(self.Y[mov]))
        else:
            bx = 0.0
        blk = block_spread(self.X, self.Y, self.fixed, self.blk)
        out = {"wl": wl, "over": over, "cross": cr, "compact": bx, "block": self.blk_w * blk,
               "intent": self.intent.penalty() if self.intent.items else 0.0}
        if self.bo_reg and self.bo_w:
            out["breakout"] = self.bo_w * self.breakout_pen()
        if self.stay.any():
            out["stay"] = float((self.stay * (np.abs(self.X - self.src[0]) + np.abs(self.Y - self.src[1]) + 2.0 * (self.T != 0))).sum())
        return out

    def total(self, p, w_cross=4.0):
        return p["wl"] + 4.0 * p["over"] + w_cross * p["cross"] + 0.2 * p["compact"] + p.get("block", 0.0) + p.get("intent", 0.0) + p.get("stay", 0.0) + p.get("breakout", 0.0)

    def crossings(self):
        """port-to-port crossing count: ports of one net chained by x, edges of different nets that intersect."""
        tx, ty = self.world(self.tm, self.tx, self.ty)
        order = np.lexsort((ty, tx, self.tn))
        n, x, y = self.tn[order], tx[order], ty[order]
        same = n[1:] == n[:-1]
        a0 = np.stack([x[:-1][same], y[:-1][same]], 1)
        a1 = np.stack([x[1:][same], y[1:][same]], 1)
        nn = n[:-1][same]
        if len(nn) < 2:
            return 0
        d1 = a1 - a0
        c1 = d1[:, None, 0] * (a0[None, :, 1] - a0[:, None, 1]) - d1[:, None, 1] * (a0[None, :, 0] - a0[:, None, 0])
        c2 = d1[:, None, 0] * (a1[None, :, 1] - a0[:, None, 1]) - d1[:, None, 1] * (a1[None, :, 0] - a0[:, None, 0])
        d2 = d1
        c3 = d2[None, :, 0] * (a0[:, None, 1] - a0[None, :, 1]) - d2[None, :, 1] * (a0[:, None, 0] - a0[None, :, 0])
        c4 = d2[None, :, 0] * (a1[:, None, 1] - a0[None, :, 1]) - d2[None, :, 1] * (a1[:, None, 0] - a0[None, :, 0])
        hit = (c1 * c2 < 0) & (c3 * c4 < 0) & (nn[:, None] != nn[None, :])
        return int(np.triu(hit, 1).sum())

    # -- occupancy raster (prefilter only: Legal decides)
    RES = 0.25

    def init_raster(self):
        from . import fluxmap as FM, legalise as LG
        x0, y0, x1, y1 = self.B["bbox"]
        self.r0 = (x0 - 1.0, y0 - 1.0)
        self.nx = int((x1 - x0 + 2.0) / self.RES) + 1
        self.ny = int((y1 - y0 + 2.0) / self.RES) + 1
        poly = FM._outline(self.B)
        cx = self.r0[0] + (np.arange(self.nx) + 0.5) * self.RES
        cy = self.r0[1] + (np.arange(self.ny) + 0.5) * self.RES
        GX, GY = np.meshgrid(cx, cy)
        inside = np.zeros(GX.shape, bool)
        n = len(poly)
        for k in range(n):
            ax, ay = poly[k]
            bx, by = poly[(k + 1) % n]
            if ay == by:
                continue
            m = ((ay > GY) != (by > GY)) & (GX < (bx - ax) * (GY - ay) / (by - ay) + ax)
            inside ^= m
        self.base = {"F": ~inside, "B": ~inside}
        self.base_edge = {"F": np.zeros_like(inside), "B": np.zeros_like(inside)}     # edge connectors may overhang the outline
        for rect, sides, allow, mh, name in LG.keepout_rects(self.B, self.cons):
            for sd in sides:
                self._fill(self.base[sd], rect)
                self._fill(self.base_edge[sd], rect)

    def _fill(self, arr, r):
        i0 = max(0, int((r[0] - self.r0[0]) / self.RES))
        i1 = min(self.nx, int(math.ceil((r[2] - self.r0[0]) / self.RES)))
        j0 = max(0, int((r[1] - self.r0[1]) / self.RES))
        j1 = min(self.ny, int(math.ceil((r[3] - self.r0[1]) / self.RES)))
        if i1 > i0 and j1 > j0:
            arr[j0:j1, i0:i1] = True

    def raster(self, skip=(), edge=False, skip_mod=-1):
        b = self.base_edge if edge else self.base
        occ = {s: b[s].copy() for s in ("F", "B")}
        for j in range(self.n):
            if j == skip_mod or self.cur[j][0] > 9e4:
                continue
            for rect, sides in self.claims_world(j):
                for sd in sides:
                    self._fill(occ[sd], rect)
        for ref, p in self.B["parts"].items():
            if ref in skip or p["pos"][0] > 9e4:
                continue
            for sd in (("F", "B") if p["tht"] else (p["side"][0],)):
                self._fill(occ[sd], p["court"])
        if skip_mod >= 0:                       # --- intent: the zones the module's own members may not enter (prefilter)
            P = self.B["parts"]
            for r in self.mods[skip_mod]["members"]:
                for _name, rect in self.L.M.intent.for_ref(r):
                    for sd in (("F", "B") if P[r]["tht"] else (P[r]["side"][0],)):
                        self._fill(occ[sd], rect)
        return occ

    def free_mask(self, i, occ, gx, gy, th, halo):
        """boolean array over candidate anchor positions (gx, gy): every member courtyard of module i free on its side(s)."""
        ok = np.ones(len(gx), bool)
        c, s = CS[th]
        P = self.B["parts"]
        # pose the members once at the origin
        for r, (px, py, ang) in MD.pose(self.mods[i], 0.0, 0.0, th).items():
            pads, court = GM.moved(P[r], px, py, ang)
            rect = (court[0] - halo, court[1] - halo, court[2] + halo, court[3] + halo)
            for sd in (("F", "B") if P[r]["tht"] else (P[r]["side"][0],)):
                ii = np.clip(((gx + rect[0] - self.r0[0]) / self.RES).astype(int), 0, self.nx)
                ii2 = np.clip((np.ceil((gx + rect[2] - self.r0[0]) / self.RES)).astype(int), 0, self.nx)
                jj = np.clip(((gy + rect[1] - self.r0[1]) / self.RES).astype(int), 0, self.ny)
                jj2 = np.clip((np.ceil((gy + rect[3] - self.r0[1]) / self.RES)).astype(int), 0, self.ny)
                I = self._integral(occ[sd])
                tot = I[jj2, ii2] - I[jj, ii2] - I[jj2, ii] + I[jj, ii]
                ok &= (tot == 0) & (gx + rect[0] > self.r0[0]) & (gy + rect[1] > self.r0[1])
        return ok

    def _integral(self, arr):
        k = id(arr)
        c = self._ints.get(k)
        if c is None or c[0] is not arr:
            I = np.zeros((self.ny + 1, self.nx + 1), np.int64)
            I[1:, 1:] = arr.astype(np.int64).cumsum(0).cumsum(1)
            self._ints[k] = (arr, I)
            return I
        return c[1]

    # -- legality
    def poses(self, i, X, Y, th):
        return MD.pose(self.mods[i], X, Y, th)

    def apply(self, i, X, Y, th):
        """put module i at (X, Y, th) in the board model, the courtyard hash and the claim hash; -> state to restore."""
        P = self.B["parts"]
        self.parked.discard(i)
        old = ({r: (P[r]["pos"][0], P[r]["pos"][1], P[r]["angle"]) for r in self.mods[i]["members"]}, self.cur[i])
        for r, (x, y, a) in self.poses(i, X, Y, th).items():
            GM.place(P[r], x, y, a)
            self.L.refresh(r)
            self._halo_hash(r, i)
        self.cur[i] = (X, Y, th)
        self._claims(i)
        return old

    def _halo_hash(self, r, i):
        """a module with a halo keeps others that far from it: its courtyards are stored inflated in the hash."""
        h = float(self.halo[i])
        if h:
            c = self.B["parts"][r]["court"]
            self.L.hash.add(r, (c[0] - h, c[1] - h, c[2] + h, c[3] + h), "x")

    def rehalo(self):
        for i in range(self.n):
            if self.halo[i] and self.cur[i][0] < 9e4:
                for r in self.mods[i]["members"]:
                    self._halo_hash(r, i)

    def restore(self, i, old):
        P = self.B["parts"]
        mem, cur = old
        for r, (x, y, a) in mem.items():
            GM.place(P[r], x, y, a)
            self.L.refresh(r)
            self._halo_hash(r, i)
        self.cur[i] = cur
        self._claims(i)

    # -- fan-out claims: the copper every module's own fan-out needs, as rectangles relative to its anchor
    def set_claims(self, rel):
        self.claims_rel = rel
        self.chash = GM.Hash(3.0)
        for i in range(self.n):
            self._claims(i)

    def set_keepouts(self, rel):
        """rel: {module index: [rect rel to the anchor]}. A module's keep-out is its own claim: its OWN parts are exempt, every other
        module's courtyard is refused inside it (both sides), and its members' courtyards are refused inside the others'."""
        self.ko_rel = {i: [tuple(r) for r in v] for i, v in rel.items() if v}
        self.khash = GM.Hash(3.0)
        for i in range(self.n):
            self._claims(i)

    def keepouts_world(self, i):
        X, Y, th = self.cur[i]
        out = []
        for x0, y0, x1, y1 in self.ko_rel.get(i, ()):
            ax, ay = GM.rot(x0, y0, th)
            bx, by = GM.rot(x1, y1, th)
            out.append((X + min(ax, bx), Y + min(ay, by), X + max(ax, bx), Y + max(ay, by)))
        return out

    def set_breakouts(self, regs):
        """regs: [(module index, rect rel to the anchor, outward unit vector rel, sides)] = the lane regions of the dense parts' templates."""
        self.bo_reg = list(regs)
        self.bo_rel = defaultdict(list)
        for i, rect, n, sd in regs:
            self.bo_rel[i].append((rect, sd))
        self.bo_m = np.array([r[0] for r in regs], int)
        self.bo_r = np.array([r[1] for r in regs], float).reshape(-1, 4)
        self.bo_n = np.array([r[2] for r in regs], float).reshape(-1, 2)
        self.bo_sd = ["F" if "F" in r[3] and "B" not in r[3] else "B" for r in regs]
        self.bhash = GM.Hash(3.0)
        for i in range(self.n):
            self._claims(i)

    def breakout_world(self, i=None):
        """world rects of the lane regions ([k] arrays x0, y0, x1, y1) and their outward vectors."""
        m = self.bo_m
        th = self.T[m]
        c = np.array([CS[t][0] for t in th])
        s_ = np.array([CS[t][1] for t in th])
        ax, ay = self.X[m] + self.bo_r[:, 0] * c + self.bo_r[:, 1] * s_, self.Y[m] + (-self.bo_r[:, 0] * s_ + self.bo_r[:, 1] * c)
        bx, by = self.X[m] + self.bo_r[:, 2] * c + self.bo_r[:, 3] * s_, self.Y[m] + (-self.bo_r[:, 2] * s_ + self.bo_r[:, 3] * c)
        nx = self.bo_n[:, 0] * c + self.bo_n[:, 1] * s_
        ny = -self.bo_n[:, 0] * s_ + self.bo_n[:, 1] * c
        return np.minimum(ax, bx), np.minimum(ay, by), np.maximum(ax, bx), np.maximum(ay, by), nx, ny

    def breakout_pen(self):
        """Orientation preference of the dense parts' breakouts: each lane region's apron (the region's outward extension by
        `[floorplan] breakout_apron_mm`) is charged for the share of it that is outside the board / in a keep-out, and for every
        breakout of another module it runs into. 0 = every breakout faces open board."""
        x0, y0, x1, y1, nx, ny = self.breakout_world()
        A = self.bo_apron
        ax0 = np.where(nx < -0.5, x0 - A, x0)
        ax1 = np.where(nx > 0.5, x1 + A, x1)
        ay0 = np.where(ny < -0.5, y0 - A, y0)
        ay1 = np.where(ny > 0.5, y1 + A, y1)
        pen = 0.0
        for k in range(len(x0)):
            arr = self.base[self.bo_sd[k]]
            i0 = int(math.floor((ax0[k] - self.r0[0]) / self.RES))
            i1 = int(math.ceil((ax1[k] - self.r0[0]) / self.RES))
            j0 = int(math.floor((ay0[k] - self.r0[1]) / self.RES))
            j1 = int(math.ceil((ay1[k] - self.r0[1]) / self.RES))
            tot = max(1, (i1 - i0) * (j1 - j0))
            sub = arr[max(0, j0):max(0, min(self.ny, j1)), max(0, i0):max(0, min(self.nx, i1))]
            pen += 1.0 - (sub.size - float(sub.sum())) / tot
        ox = np.maximum(0.0, np.minimum(ax1[:, None], x1[None, :]) - np.maximum(ax0[:, None], x0[None, :]))
        oy = np.maximum(0.0, np.minimum(ay1[:, None], y1[None, :]) - np.maximum(ay0[:, None], y0[None, :]))
        ov = (ox * oy > 1e-9) & (self.bo_m[:, None] != self.bo_m[None, :])
        return pen + 0.5 * float(ov.sum())

    def claims_world(self, i):
        X, Y, th = self.cur[i]
        out = []
        for (x0, y0, x1, y1), sides in self.claims_rel.get(i, ()):
            ax, ay = GM.rot(x0, y0, th)
            bx, by = GM.rot(x1, y1, th)
            out.append(((X + min(ax, bx), Y + min(ay, by), X + max(ax, bx), Y + max(ay, by)), sides))
        return out

    def _claims(self, i):
        for k in range(len(self.claims_rel.get(i, ())) + 8):
            for sd in ("F", "B"):
                self.chash.remove((i, k, sd))
        for k, (rect, sides) in enumerate(self.claims_world(i)):
            for sd in sides:
                self.chash.add((i, k, sd), rect, sd)
        for k in range(len(self.ko_rel.get(i, ())) + 8):
            self.khash.remove((i, k))
        for k, rect in enumerate(self.keepouts_world(i)):
            self.khash.add((i, k), rect, "x")
        for k in range(len(self.bo_rel.get(i, ())) + 8):
            for sd in ("F", "B"):
                self.bhash.remove((i, k, sd))
        X, Y, th = self.cur[i]
        for k, (rect, sides) in enumerate(self.bo_rel.get(i, ())):
            ax, ay = GM.rot(rect[0], rect[1], th)
            bx, by = GM.rot(rect[2], rect[3], th)
            w = (X + min(ax, bx), Y + min(ay, by), X + max(ax, bx), Y + max(ay, by))
            for sd in sides:
                self.bhash.add((i, k, sd), w, sd)

    def legal(self, i):
        """members of module i legal at their current pose (new violations only)."""
        P = self.B["parts"]
        for r in self.mods[i]["members"]:
            court = P[r]["court"]
            for kind, d in self.L.part_issues(r, court=court, pads=P[r]["pads"]):
                if kind == "overlap":
                    # halo-inflated overlap with a part of ANOTHER module counts; the source's own known overlaps do not
                    if frozenset((r, d)) in self.known:
                        continue
                    return False
                if kind == "hole":                     # a hole through a part of ANOTHER module (both sides); known ones excepted
                    if frozenset((r, d)) in self.known_hole:
                        continue
                    return False
                if kind == "pad_short" and d in self.known_short:
                    continue
                if kind == "outside" and r in self.known_out:
                    continue
                if kind in ("outside", "forbidden", "no_tht", "pad_short", "hardware"):
                    return False
        return self.claims_ok(i) and self.intent.ok(i)

    def claims_ok(self, i):
        """module i's fan-out copper (claims) clear of every other module's PADS, and its pads clear of their claims."""
        mem = set(self.mods[i]["members"])
        P = self.B["parts"]
        mine = {(i, k, sd) for k in range(len(self.claims_rel.get(i, ())) + 8) for sd in ("F", "B")}
        for r in mem:
            for q in P[r]["pads"]:
                rect = (q["x"] - q["ex"], q["y"] - q["ey"], q["x"] + q["ex"], q["y"] + q["ey"])
                sides = [("F" if l == "F.Cu" else "B") for l in q["layers"] if l in ("F.Cu", "B.Cu")] or ["F", "B"]
                if q.get("np"):                         # a hole goes through every layer
                    sides = ["F", "B"]
                for sd in sides:
                    if next(self.chash.hits(rect, sd, 0.0, ignore=mine), None) is not None:
                        return False
        if self.ko_rel and not self.keepouts_ok(i):
            return False
        mineb = {(i, k, sd) for k in range(len(self.bo_rel.get(i, ())) + 8) for sd in ("F", "B")}
        for k, (rect, sides) in enumerate(self.bo_rel.get(i, ())):      # two dense parts' breakouts may not overlap
            for sd in sides:
                if next(self.bhash.hits(rect_w(self, i, rect), sd, 0.0, ignore=mineb), None) is not None:
                    return False
        for rect, sides in self.claims_world(i):
            for (o, j) in self.L.phash.hits(rect, "x", 0.0, ignore=()):
                if o in mem:
                    continue
                q = P[o]["pads"][j]
                ps = {("F" if l == "F.Cu" else "B") for l in q["layers"] if l in ("F.Cu", "B.Cu")}
                if ps & set(sides):
                    return False
        return True

    def keepouts_ok(self, i):
        """module i's keep-outs hold no courtyard of another module, and none of its courtyards lies in another module's keep-out.
        A pair of fixed modules is a baseline fact, not the search's to fix."""
        mem = set(self.mods[i]["members"])
        P = self.B["parts"]
        for rect in self.keepouts_world(i):
            for o in self.L.hash.hits(rect, "x", 0.0, ignore=mem):
                j = self.idx.get(self.of.get(o))
                if j is None or j == i or not (self.fixed[i] and self.fixed[j]):
                    return False
        for r in mem:
            for (j, _k) in self.khash.hits(P[r]["court"], "x", 0.0, ignore=()):
                if j != i and not (self.fixed[i] and self.fixed[j]):
                    return False
        return True

    def freeze_baseline(self):
        """violations the fixed parts already have among themselves are not the search's to fix."""
        iss = self.base_issues
        fx = {r for i, m in enumerate(self.mods) if m["fixed"] for r in m["members"]}
        self.known = {frozenset(s.split("/")) for s in iss["overlap"] if set(s.split("/")) <= fx}
        self.known_short = set()
        self.known_hole = {frozenset(s.split("/")) for s in iss.get("hole", []) if set(s.split("/")) <= fx}
        self.known_out = {s.split(" ")[0] for s in iss["outside"] if s.split(" ")[0] in fx}

    def try_pose(self, i, X, Y, th):
        old = self.apply(i, X, Y, th)
        ok = self.legal(i)
        if not ok:
            self.restore(i, old)
        return ok, old

    def park(self, i, orig):
        """module i gives up its rigid pose: every member goes back to its pose on the input board, when that is legal where the others
        stand now (else the module stays unplaced). -> True when parked. The module is reported in `noseat` either way."""
        from . import legalgate as LG
        P = self.B["parts"]
        was = self.cur[i]
        mem = {r: (P[r]["pos"][0], P[r]["pos"][1], P[r]["angle"], P[r]["side"]) for r in self.mods[i]["members"]}
        for r in self.mods[i]["members"]:
            LG.revert_part(self.B, self.L, r, orig[r])
        kn = LG.known(self.base_issues)
        if any(LG.part_new(self.L, r, kn) for r in self.mods[i]["members"]):
            for r in self.mods[i]["members"]:
                LG.revert_part(self.B, self.L, r, mem[r])
            self.cur[i] = was
            if was[0] < 9e4:
                self.unplace(i)
            self.noseat.add(self.mods[i]["name"])
            return False
        self.cur[i] = (float(self.src[0][i]), float(self.src[1][i]), 0)
        self.X[i], self.Y[i], self.T[i] = self.src[0][i], self.src[1][i], 0
        self._claims(i)
        self.parked.add(i)
        self.noseat.add(self.mods[i]["name"])
        return True

    def unplaced(self, i):
        return self.cur[i][0] > 9e4

    def flip_single(self, i):
        """mirror the one-part module i to the other side (GM.flip about the part origin). The module must stand at th 0 (just unplaced).
        The module's relative data follow: the member's angle, the pad / port arrays of the nets, the box. Calling it twice restores."""
        (r, m), = self.mods[i]["members"].items()
        P = self.B["parts"][r]
        GM.flip(P)
        m["ang"] = P["angle"]
        d = m["dx"]
        sel = self.pm == i
        self.px[sel] = 2 * d - self.px[sel]
        sel = self.tm == i
        self.tx[sel] = 2 * d - self.tx[sel]
        b = self.mods[i].get("bbox")
        if b:
            self.mods[i]["bbox"] = [round(2 * d - b[2], 3), b[1], round(2 * d - b[0], 3), b[3]]
        self.L.refresh(r)

    def unplace(self, i):
        old = self.apply(i, 1e5 + 50.0 * i, 1e5, 0)
        for k in range(len(self.claims_rel.get(i, ())) + 8):
            for sd in ("F", "B"):
                self.chash.remove((i, k, sd))
        for k in range(len(self.ko_rel.get(i, ())) + 8):
            self.khash.remove((i, k))
        return old


# ---------------------------------------------------------------- constructive + annealing
def rect_w(P, i, rect):
    X, Y, th = P.cur[i]
    ax, ay = GM.rot(rect[0], rect[1], th)
    bx, by = GM.rot(rect[2], rect[3], th)
    return (X + min(ax, bx), Y + min(ay, by), X + max(ax, bx), Y + max(ay, by))


def breakout_claims(B, cons, spec, M, P):
    """The ISOLATED breakout templates of the dense parts as module-relative rectangles: ({module index: [(rect, sides)]} for claims_ok
    = lane region on the pad layer + each via site on both, [(module, region rect, outward vector, sides)] for the placement cost)."""
    from . import breakout as BO
    if not BO.conf(cons)["enabled"]:
        return {}, []
    pr = BO.params(cons, B)
    res = BO.solve(B, cons, spec, pr=pr, others=False)
    claims, regs = defaultdict(list), []
    for ref, t in res["templates"].items():
        if ref not in M["of"]:
            continue
        i = P.idx[M["of"][ref]]
        ox, oy = M["modules"][i]["pos"]
        lay = "F" if (t["pins"] and t["pins"][0]["layer"] == "F.Cu") else "B"
        for s_ in t["sides"]:
            r = s_["rect"]
            rel = (r[0] - ox, r[1] - oy, r[2] - ox, r[3] - oy)
            regs.append((i, rel, BO.SIDES[s_["k"]], {lay}))
            claims[i].append((rel, [lay]))
        for rect, sd in BO.rects(B, ref, {"templates": {ref: dict(t, sides=[])}}, pr):
            claims[i].append(((rect[0] - ox, rect[1] - oy, rect[2] - ox, rect[3] - oy), sorted(sd)))
    return dict(claims), regs


def legalise_units(P, step=1.0):
    """every movable module with a member that adds an issue the input board did not have (pad short, courtyard overlap, hole, outline,
    keep-out), wherever it stands, is re-seated at the nearest legal pose. -> (names re-seated, names left where they stand)."""
    from . import legalgate as LG
    kn = LG.known(P.base_issues)
    bad = [i for i in range(P.n) if not P.fixed[i] and any(LG.part_new(P.L, r, kn) for r in P.mods[i]["members"])]
    moved, left = [], []
    for i in bad:
        (moved if reseat_module(P, i, step=step) else left).append(P.mods[i]["name"])
    return moved, left


def settle(P, orig, step=1.0):
    """nothing illegal stands in the model before it is written: a movable module with a member that adds an issue the input lacked is
    re-seated (wide search), else its parts go back to their input poses (parked), else it stays unplaced; a module still unplaced is
    parked when its input poses are free. -> {"reseated": [...], "parked": [...], "unplaced": [...]} (module names)."""
    from . import legalgate as LG
    kn = LG.known(P.base_issues)
    out = {"reseated": [], "parked": [], "unplaced": []}
    for i in range(P.n):
        if P.fixed[i] or i in P.parked:
            continue
        name = P.mods[i]["name"]
        if P.unplaced(i):
            out["parked" if P.park(i, orig) else "unplaced"].append(name)
        elif any(LG.part_new(P.L, r, kn) for r in P.mods[i]["members"]):
            if reseat_module(P, i, step=step):
                out["reseated"].append(name)
            else:
                out["parked" if P.park(i, orig) else "unplaced"].append(name)
    return out


def reseat_module(P, i, step=1.0):
    """move module i to the nearest legal pose (by the same cost as the constructive pass) outside the halos."""
    x0, y0, x1, y1 = P.B["bbox"]
    was_out = P.unplaced(i)
    old = None if was_out else P.apply(i, P.X[i], P.Y[i], int(P.T[i]))
    P.unplace(i)
    placed = set(j for j in range(P.n) if j != i and P.cur[j][0] < 9e4)
    occ = P.raster(skip=P.mods[i]["members"], edge=any(r in P.L.edge for r in P.mods[i]["members"]), skip_mod=i)
    P._ints = {}
    for R in (4.0, 8.0, 16.0, 40.0, 120.0):
        for c, X, Y, th in _grid(P, i, float(P.X[i]), float(P.Y[i]), R, step, placed, x0, y0, x1, y1, occ):
            ok, _ = P.try_pose(i, X, Y, th)
            if ok:
                P.X[i], P.Y[i], P.T[i] = X, Y, th
                return True
    if not was_out:
        P.apply(i, P.X[i], P.Y[i], int(P.T[i]))
    return False


def repair_claims(P, step=1.0):
    """the source poses ignore the modules' escape copper: every movable module that sits on another's claim (or whose
    claim sits on another's courtyard) is re-seated at the nearest legal pose. -> refs of the modules moved."""
    moved = []
    for i in range(P.n):
        if P.fixed[i] or P.claims_ok(i):
            continue
        if reseat_module(P, i, step=step):
            moved.append(P.mods[i]["name"])
    return moved


def claims_from(mods, clr=0.10, lane=0.0):
    """{module index: [(rect rel to anchor, [sides])]} from the isolated fan-out geometry of each module."""
    out = {}
    for i, m in enumerate(mods):
        rel = []
        for pin in (m.get("fanout") or {}).get("pins", ()):
            if not pin.get("ok"):
                continue
            w = float(pin.get("w", 0.15))
            side = lambda l: ["F"] if l == "F.Cu" else ["B"] if l == "B.Cu" else None
            if pin.get("stub") and side(pin.get("layer")):
                (x0, y0), (x1, y1) = pin["stub"]
                h = w / 2 + clr
                n = max(1, int(math.ceil(math.hypot(x1 - x0, y1 - y0) / 0.4)))
                for k in range(n):                      # a diagonal stub as a few small boxes, not one big one
                    ax, ay = x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n
                    bx, by = x0 + (x1 - x0) * (k + 1) / n, y0 + (y1 - y0) * (k + 1) / n
                    rel.append(((min(ax, bx) - h, min(ay, by) - h, max(ax, bx) + h, max(ay, by) + h), side(pin["layer"])))
            if pin.get("via"):
                vx, vy = pin["via"]
                h = 0.225 + clr
                rel.append(((vx - h, vy - h, vx + h, vy + h), ["F", "B"]))
            ln = pin.get("lane") if lane else None
            if ln and side(ln[1]) and pin.get("end"):
                ex, ey = pin["end"]
                ax = ex + lane * math.cos(math.radians(ln[0]))
                ay = ey + lane * math.sin(math.radians(ln[0]))
                h = w / 2 + clr
                rel.append(((min(ex, ax) - h, min(ey, ay) - h, max(ex, ax) + h, max(ey, ay) + h), side(ln[1])))
        out[i] = rel
    return out


CONSTRUCT_TRIES = 3                 # passes of the constructive placement: the modules that found no pose go to the front of the next order


def construct(P, seed=1, log=print, step=1.0):
    """place the movable modules, the largest first, each at the legal pose nearest the modules it connects to. A module that finds no
    pose in a pass goes to the front of the next order (CONSTRUCT_TRIES passes, deterministic; the pass with the fewest failures
    stands). A module with no legal pose is left UNPLACED (P.unplaced(i)): the source pose is taken only when it is itself legal there.
    -> names of the modules left unplaced."""
    mov = [i for i in range(P.n) if not P.fixed[i]]
    order = sorted(mov, key=lambda i: (-P.mods[i]["area"], P.mods[i]["name"]))
    conn = np.zeros((P.n, P.n))
    for ni in range(len(P.net_name)):
        ms = sorted(set(P.tm[P.tstarts[ni]:(P.tstarts[ni + 1] if ni + 1 < len(P.net_name) else len(P.tm))]))
        for a in ms:
            for b in ms:
                if a != b:
                    conn[a, b] += P.net_w[ni] / max(1, len(ms) - 1)
    best, last = None, None
    for _try in range(CONSTRUCT_TRIES):
        failed = _construct_pass(P, order, conn, step)
        last = list(order)
        if best is None or len(failed) < len(best[0]):
            best = (list(failed), list(order))
        if not failed:
            break
        order = sorted(failed, key=lambda i: (-P.mods[i]["area"], P.mods[i]["name"])) + [i for i in order if i not in failed]
    if best[1] != last:                    # the last pass was not the best one: place it again, same order
        failed = _construct_pass(P, best[1], conn, step)
    for i in failed:                                             # the arranged source pose, only when it is legal where it stands
        ok, _ = P.try_pose(i, float(P.src[0][i]), float(P.src[1][i]), 0)
        if ok:
            P.X[i], P.Y[i], P.T[i] = P.src[0][i], P.src[1][i], 0
    return [P.mods[i]["name"] for i in failed if P.unplaced(i)]


def _construct_pass(P, order, conn, step):
    for i in order:
        P.unplace(i)
    placed = set(i for i in range(P.n) if P.fixed[i])
    x0, y0, x1, y1 = P.B["bbox"]
    failed = []
    for i in order:
        w = conn[i]
        pl = [j for j in placed if w[j] > 0]
        if pl:
            tx = sum(w[j] * P.X[j] for j in pl) / sum(w[j] for j in pl)
            ty = sum(w[j] * P.Y[j] for j in pl) / sum(w[j] for j in pl)
        else:
            tx, ty = (x0 + x1) / 2, (y0 + y1) / 2
        ok = False
        edge_mod = any(r in P.L.edge for r in P.mods[i]["members"])
        occ = P.raster(skip=P.mods[i]["members"], edge=edge_mod, skip_mod=i)
        P._ints = {}
        for R in (6.0, 12.0, 24.0, 60.0, 200.0):
            cands = _grid(P, i, tx, ty, R, step, placed, x0, y0, x1, y1, occ)
            n_try = 0
            for c, X, Y, th in cands:
                n_try += 1
                if n_try > (600 if edge_mod else 60):
                    break
                good, _ = P.try_pose(i, X, Y, th)
                if good:
                    P.X[i], P.Y[i], P.T[i] = X, Y, th
                    ok = True
                    break
            if ok:
                break
        if ok:
            placed.add(i)
        else:
            failed.append(i)
    return failed


def _grid(P, i, tx, ty, R, step, placed, x0, y0, x1, y1, occ):
    """[(cost, X, Y, th)] sorted: weighted half-perimeter growth of the module's nets against what is placed."""
    mod = P.mods[i]
    sel = np.where(P.pm == i)[0]
    xs = np.arange(max(x0, tx - R), min(x1, tx + R) + 1e-9, step)
    ys = np.arange(max(y0, ty - R), min(y1, ty + R) + 1e-9, step)
    if len(xs) == 0 or len(ys) == 0:
        return []
    GX, GY = np.meshgrid(xs, ys)
    gx, gy = GX.ravel(), GY.ravel()
    placed_l = np.array(sorted(placed), dtype=int)
    isp = np.zeros(P.n, bool)
    isp[placed_l] = True
    out = []
    for th in THETA:
        c, s = CS[th]
        free = P.free_mask(i, occ, gx, gy, th, float(P.halo[i]))
        if not free.any():
            continue
        gx_, gy_ = gx, gy
        gx, gy = gx_[free], gy_[free]
        cost = np.zeros(len(gx))
        nets = sorted(set(P.pn[sel]))
        for ni in nets:
            k = np.where(P.pn == ni)[0]
            mine = k[P.pm[k] == i]
            oth = k[(P.pm[k] != i) & isp[P.pm[k]]]
            if len(oth) == 0:
                continue
            ox, oy = P.world(P.pm[oth], P.px[oth], P.py[oth])
            bx0, bx1, by0, by1 = ox.min(), ox.max(), oy.min(), oy.max()
            rx, ry = P.px[mine], P.py[mine]
            wx = gx[:, None] + rx[None, :] * c + ry[None, :] * s
            wy = gy[:, None] + (-rx[None, :] * s + ry[None, :] * c)
            nx0 = np.minimum(bx0, wx.min(1))
            nx1 = np.maximum(bx1, wx.max(1))
            ny0 = np.minimum(by0, wy.min(1))
            ny1 = np.maximum(by1, wy.max(1))
            hp = (nx1 - nx0) + (ny1 - ny0)
            cost += P.net_w[ni] * (hp + 4.0 * np.maximum(0.0, hp - P.net_budget[ni]))
        # a small pull to the target so unconnected modules stay compact
        cost += 0.05 * np.hypot(gx - tx, gy - ty)
        for k in range(len(gx)):
            out.append((float(cost[k]), float(gx[k]), float(gy[k]), th))
        gx, gy = gx_, gy_
    out.sort(key=lambda t: (round(t[0], 6), t[3], t[1], t[2]))
    return out


WATCH_EVERY = 250                   # anneal moves between two calls of the live view


def _within(cost, cap):
    return cap is None or (cost["wl"] <= cap["wl"] + 1e-6 and cost["cross"] <= cap["cross"])


def anneal(P, iters=4000, seed=1, t0=None, log=print, watch=None, cap=None):
    """cap = {"wl":, "cross":}: a state above either is never kept as the best (the source start must not end worse than it began)."""
    rng = random.Random(seed)
    mov = [i for i in range(P.n) if not P.fixed[i] and i not in P.parked]
    if not mov:
        return 0, 0
    cur = P.cost_parts()
    cs = P.total(cur)
    best = (cs, P.X.copy(), P.Y.copy(), P.T.copy())
    T0 = t0 if t0 is not None else max(1.0, cs * 0.002)
    acc = 0
    x0, y0, x1, y1 = P.B["bbox"]
    for it in range(iters):
        if watch is not None and it % WATCH_EVERY == 0:      # F16 (liveview): no random draw, nothing of the search is touched
            watch(it, iters, cs)
        frac = it / max(1, iters)
        T = T0 * (0.02 ** frac)
        sig = max(0.5, 8.0 * (1 - frac))
        r = rng.random()
        i = mov[rng.randrange(len(mov))]
        if r < 0.55:
            X, Y, th = P.X[i] + rng.gauss(0, sig), P.Y[i] + rng.gauss(0, sig), int(P.T[i])
        elif r < 0.7:
            X, Y, th = P.X[i], P.Y[i], THETA[(THETA.index(int(P.T[i])) + rng.choice((1, 2, 3))) % 4]
        elif r < 0.85:
            # jump toward the weighted centroid of the connected modules
            sel = np.where(P.pm == i)[0]
            nets = set(P.pn[sel])
            xs, ys, ws = [], [], []
            for ni in nets:
                k = np.where(P.pn == ni)[0]
                o = k[P.pm[k] != i]
                if len(o):
                    ox, oy = P.world(P.pm[o], P.px[o], P.py[o])
                    xs.append(ox.mean())
                    ys.append(oy.mean())
                    ws.append(P.net_w[ni])
            if not ws or sum(ws) <= 0:
                continue
            tx = sum(a * b for a, b in zip(xs, ws)) / sum(ws)
            ty = sum(a * b for a, b in zip(ys, ws)) / sum(ws)
            X, Y, th = P.X[i] + 0.5 * (tx - P.X[i]) + rng.gauss(0, 1.0), P.Y[i] + 0.5 * (ty - P.Y[i]) + rng.gauss(0, 1.0), int(P.T[i])
        else:
            j = mov[rng.randrange(len(mov))]
            if j == i or P.unplaced(i) or P.unplaced(j):
                continue
            old_i = P.apply(i, P.X[j], P.Y[j], int(P.T[i]))
            old_j = P.apply(j, P.X[i], P.Y[i], int(P.T[j]))
            ok = P.legal(i) and P.legal(j)
            if ok:
                Xi, Yi, Xj, Yj = P.X[i], P.Y[i], P.X[j], P.Y[j]
                P.X[i], P.Y[i], P.X[j], P.Y[j] = Xj, Yj, Xi, Yi
                nc = P.cost_parts()
                ns = P.total(nc)
                if ns <= cs or rng.random() < math.exp(-(ns - cs) / T):
                    cs, cur = ns, nc
                    acc += 1
                    if cs < best[0] and _within(nc, cap):
                        best = (cs, P.X.copy(), P.Y.copy(), P.T.copy())
                    continue
                P.X[i], P.Y[i], P.X[j], P.Y[j] = Xi, Yi, Xj, Yj
            P.restore(j, old_j)
            P.restore(i, old_i)
            continue
        if not (x0 < X < x1 and y0 < Y < y1):
            continue
        sx, sy, st = P.X[i], P.Y[i], P.T[i]
        P.X[i], P.Y[i], P.T[i] = X, Y, th
        nc = P.cost_parts()
        ns = P.total(nc)
        if not (ns <= cs or rng.random() < math.exp(-(ns - cs) / T)):
            P.X[i], P.Y[i], P.T[i] = sx, sy, st
            continue
        ok, old = P.try_pose(i, X, Y, th)
        if not ok:
            P.X[i], P.Y[i], P.T[i] = sx, sy, st
            continue
        cs, cur = ns, nc
        acc += 1
        if cs < best[0] and _within(nc, cap):
            best = (cs, P.X.copy(), P.Y.copy(), P.T.copy())
    # back to the best state
    _, bx, by, bt = best
    for i in mov:
        if (P.X[i], P.Y[i], P.T[i]) != (bx[i], by[i], bt[i]):
            P.X[i], P.Y[i], P.T[i] = bx[i], by[i], bt[i]
            P.apply(i, bx[i], by[i], int(bt[i]))
    return acc, best[0]

HOME_TRY = 24
NUDGE_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1))


def _ref_in(text, refs):
    """the first ref of `refs` that `text` names as REF.pad (any ref spelling: U1A, J_USB1)."""
    for t in re.split(r"[\s,;()]+", str(text or "")):
        for m in re.finditer(r"\.", t):
            if t[:m.start()] in refs:
                return t[:m.start()]
    return None


def _window(cons):
    return float((cons.get("floorplan") or {}).get("block_window_mm", 3.0))


def _seat_near(P, i, pin, s, halo, dist, step, x0, y0, x1, y1):
    """module i (one support, unplaced here) at the legal pose nearest the pin it serves whose distance is within its limit, the one with the
    lowest crossings then wire among HOME_TRY; -> True when seated (the model, P.X/Y/T and the claim hash follow)."""
    placed = set(j for j in range(P.n) if j != i and P.cur[j][0] < 9e4)
    occ = P.raster(skip=P.mods[i]["members"], edge=any(m in P.L.edge for m in P.mods[i]["members"]), skip_mod=i)
    P._ints = {}
    R = float(s["limit"]) + halo + 2.0
    cand = sorted(_grid(P, i, pin["x"], pin["y"], R, step, placed, x0, y0, x1, y1, occ),
                  key=lambda t: (round(math.hypot(t[1] - pin["x"], t[2] - pin["y"]), 6), t[3], t[1], t[2]))
    pick, seen = None, 0
    for _c, X, Y, th in cand:
        if seen >= HOME_TRY:
            break
        good, old = P.try_pose(i, X, Y, th)
        if not good:
            continue
        if dist() <= s["limit"]:
            seen += 1
            P.X[i], P.Y[i], P.T[i] = X, Y, th
            k = P.cost_parts()
            k = (round(k["cross"] + k["wl"] / 4.0, 6), seen)
            if pick is None or k < pick[0]:
                pick = (k, X, Y, th)
        P.restore(i, old)
    if pick is None:
        return False
    _, X, Y, th = pick
    P.apply(i, X, Y, th)
    P.X[i], P.Y[i], P.T[i] = X, Y, th
    return True


def home_supports(P, cons, spec, step=0.5, back="auto"):
    """after the search: a support the budget rows grade (confidently owned or spread; the clusters' own limits) that is its own single-part
    module and stands beyond its limit is put at the legal pose (courtyards, claims, keep-outs, intent: as the search checks them)
    nearest the pin it serves, within its limit. With no legal seat within the limit, one movable neighbour near the pin is nudged aside
    (<= 1 mm, legal, via try_pose) and the seat tried again; kept only when crossings and the weighted cost do not rise.
    -> (refs seated, refs left)."""
    from . import clusters as CL
    from . import backside as BS
    cl, _ = CL.build(P.B, cons, spec)
    if back == "auto":                                   # `[placement] sides` both / both-light-back: the back is a seat; else None = no flip
        back = BS.guard(P.B, cons, spec, template={s_["ref"] for c_ in cl if c_.get("layout") for s_ in c_["supports"]})
    parts = P.B["parts"]
    x0, y0, x1, y1 = P.B["bbox"]
    done, left = [], []
    for c in cl:
        halo = CL.HALO_MM if CL._pitch(parts[c["ic"]]) <= 0.5 else 0.0
        for s in sorted(c["supports"], key=lambda s: s["ref"]):
            r = s["ref"]
            if not (s["sure"] or s.get("how") == "spread") or r not in P.of:
                continue
            i = P.idx[P.of[r]]
            if P.fixed[i] or i in P.parked or P.unplaced(i) or len(P.mods[i]["members"]) != 1:
                continue
            pin = next(q for q in parts[c["ic"]]["pads"] if q["num"] == s["pad"])
            dist = lambda: CL.support_dist(s, next(q for q in parts[r]["pads"] if q["num"] == s["via"]), pin, halo)
            if dist() <= s["limit"]:
                continue
            sx, sy, st = float(P.X[i]), float(P.Y[i]), int(P.T[i])
            c0 = P.cost_parts()
            P.apply(i, sx, sy, st)
            P.unplace(i)
            ok = _seat_near(P, i, pin, s, halo, dist, step, x0, y0, x1, y1)
            if not ok:                                   # no legal seat within the limit: one neighbour steps aside
                win = s["limit"] + halo + 2.0
                for j in range(P.n):
                    if ok or j == i or P.fixed[j] or j in P.parked or P.unplaced(j):
                        continue
                    if not any(parts[m]["court"][0] - win < pin["x"] < parts[m]["court"][2] + win
                               and parts[m]["court"][1] - win < pin["y"] < parts[m]["court"][3] + win for m in P.mods[j]["members"]):
                        continue
                    jx, jy, jt = float(P.X[j]), float(P.Y[j]), int(P.T[j])
                    for mag in (0.5, 1.0):
                        for dx, dy in NUDGE_DIRS:
                            g, oldj = P.try_pose(j, jx + dx * mag, jy + dy * mag, jt)
                            if not g:
                                continue
                            P.X[j], P.Y[j] = jx + dx * mag, jy + dy * mag
                            if _seat_near(P, i, pin, s, halo, dist, step, x0, y0, x1, y1):
                                c1 = P.cost_parts()
                                if c1["cross"] <= c0["cross"] and P.total(c1) <= P.total(c0) + 1e-6:
                                    ok = True
                                    break
                                P.unplace(i)
                            P.restore(j, oldj)
                            P.X[j], P.Y[j] = jx, jy
                        if ok:
                            break
            if not ok and back is not None and not c.get("layout") and not parts[r]["tht"] and back(r):
                P.flip_single(i)                         # no seat on its side: the back, directly under the owner's pin, when the policy lets this part go
                ok = _seat_near(P, i, pin, s, halo, dist, step, x0, y0, x1, y1)
                if not ok:
                    P.flip_single(i)
            if not ok:
                P.X[i], P.Y[i], P.T[i] = sx, sy, st
                P.apply(i, sx, sy, st)
            (done if ok else left).append(r)
    return done, left


def all_moves(P):
    out = {}
    for i, m in enumerate(P.mods):
        if m["fixed"] or P.unplaced(i) or i in P.parked:                  # an unplaced module keeps its parts where the input has them
            continue
        out.update(P.poses(i, float(P.X[i]), float(P.Y[i]), int(P.T[i])))
    return {r: (round(x, 4), round(y, 4), round(a, 4)) for r, (x, y, a) in out.items()}


# ---------------------------------------------------------------- outline growth
_XY = re.compile(r"\((start|end|mid|center|xy)\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\)")


def grow_text(text, bbox, grow, edge_parts=()):
    """grow the outline: every Edge.Cuts coordinate right of the centre moves +R, left -L, below +B, above -T (straight edges
    stretch, rounded corners move whole); plane-size zone polygons follow. `grow` = (L, R, T, B) mm."""
    from . import boardwrite as BW
    gl, gr, gt, gb = grow
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2

    def mv(m):
        x, y = float(m.group(3)), float(m.group(2 + 0)) if False else float(m.group(3))
        return m.group(0)

    def sub(seg):
        def f(m):
            x, y = float(m.group(2)), float(m.group(3))
            nx = x + gr if x > cx else x - gl
            ny = y + gb if y > cy else y - gt
            return f"({m.group(1)} {BW._num(nx)} {BW._num(ny)})"
        return _XY.sub(f, seg)

    root_end = BW._end(text, text.index("("))
    out, last = [], 0
    bw = bbox[2] - bbox[0]
    bh = bbox[3] - bbox[1]
    for cs, ce in BW._children(text, 0, root_end):
        seg = text[cs:ce]
        if seg.startswith(("(gr_line", "(gr_arc", "(gr_rect", "(gr_poly", "(gr_circle")) and '"Edge.Cuts"' in seg:
            out.append(text[last:cs])
            out.append(sub(seg))
            last = ce
        elif seg.startswith("(zone"):
            m = re.search(r"\(polygon\s*\(pts(.*?)\)\s*\)", seg, re.S)
            if not m:
                continue
            pts = [(float(a), float(b)) for a, b in re.findall(r"\(xy\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\)", m.group(1))]
            if len(pts) < 3:
                continue
            w = max(p[0] for p in pts) - min(p[0] for p in pts)
            h = max(p[1] for p in pts) - min(p[1] for p in pts)
            if w * h < 0.5 * bw * bh or "(keepout" in seg:
                continue
            seg2 = re.sub(r"\(filled_polygon.*?\n\t\t\)\n", "", seg, flags=re.S)
            ns = seg2[:seg2.index("(polygon")] + sub(seg2[seg2.index("(polygon"):])
            out.append(text[last:cs])
            out.append(ns)
            last = ce
    out.append(text[last:])
    return "".join(out)


def edge_riders(B, cons, side, fixed):
    """fixed parts hugging an edge ride with it when the edge grows: courtyard within 1.5 mm of the old edge."""
    x0, y0, x1, y1 = B["bbox"]
    out = []
    for r, p in B["parts"].items():
        if r not in fixed:
            continue
        c = p["court"]
        if (side == "L" and c[0] - x0 < 1.5) or (side == "R" and x1 - c[2] < 1.5) or (side == "T" and c[1] - y0 < 1.5) or (side == "B" and y1 - c[3] < 1.5):
            out.append(r)
    return out


def apply_growth(src_text, B, cons, grow, fixed):
    """-> (text, {ref: (x, y, angle)} for the riders). Riders move with their edge; zones and outline grow."""
    gl, gr, gt, gb = grow
    text = grow_text(src_text, B["bbox"], grow)
    moves = {}
    for side, (dx, dy) in {"L": (-gl, 0), "R": (gr, 0), "T": (0, -gt), "B": (0, gb)}.items():
        if not (dx or dy):
            continue
        for r in edge_riders(B, cons, side, fixed):
            p = B["parts"][r]
            x, y, a = moves.get(r, (p["pos"][0], p["pos"][1], p["angle"]))
            moves[r] = (x + dx, y + dy, a)
    return text, moves


# ---------------------------------------------------------------- metrics
def metrics(B, cons, spec, cls, board_path=None):
    from . import netclass as NC
    pads = NC.net_pads(B)
    out = {}
    for n in ("RF_EXT",):
        if n in pads:
            out[n] = round(NC.mst_length([(q["x"], q["y"]) for q in pads[n]]), 1)
    spans = {}
    for n, c in cls.items():
        if c["class"] == "pair" and c.get("kind") in ("eth", "usb2", "usb3", "pcie"):
            spans[n] = round(NC.mst_length([(q["x"], q["y"]) for q in pads[n]]), 1)
    out["pair_spans"] = spans
    return out


def span_summary(spans):
    by = defaultdict(list)
    for n, v in spans.items():
        k = "ETH" if n.startswith("ETH") else "USB" if "USB" in n else "PCIe" if "PCIE" in n else "other"
        by[k].append(v)
    return {k: {"n": len(v), "mean": round(sum(v) / len(v), 1), "max": round(max(v), 1)} for k, v in sorted(by.items())}


# ---------------------------------------------------------------- driver
def _pro(src, dst):
    """the project file (netclasses, design rules) travels with every board written: the fan-out and the router read it."""
    import shutil
    pro = os.path.splitext(src)[0] + ".kicad_pro"
    if os.path.exists(pro):
        shutil.copyfile(pro, os.path.splitext(dst)[0] + ".kicad_pro")


def floorplan_once(board, cons, spec, grow, args, text_in, workdir, tag, log=print):
    """one size: both starts (constructive, source poses), each annealed and fan-out repaired; the better one wins
    (passes first, then fewer blocked pins, then the weighted cost). -> (result, Plan, board model)."""
    best = None
    for start in (["construct", "source"] if args.start == "both" else [args.start]):
        r = _one(board, cons, spec, grow, args, text_in, workdir, f"{tag}_{start}", start, log)
        a = r[0]["annealed"]                    # measured on the written board: the passing start with fewer crossings, then less wire, wins
        key = (0, a["cross"], a["wl"]) if passes(r[0]) else (1, len(r[0]["blocked"]), r[1].total(a))
        r[0]["start"] = start
        log(f"  [{tag}] start {start}: {'pass' if passes(r[0]) else 'FAIL'}, fan-out {r[0]['escaped']}/{r[0]['required']}, crossings {a['cross']}, wire length {a['wl']:.0f}")
        if best is None or key < best[0]:
            best = (key, r)
    res = best[1][0]
    res["variants"] = "both" if args.start == "both" else args.start
    return best[1]


def write_flips(src, dst, flipped, tag=""):
    """flip `flipped` in file `src` through pcbnew, write `dst`, and check every part against the model and the re-parsed file
    (flip.verify). A disagreement raises: a flipped part the model and KiCad disagree about must never be written on."""
    from . import flip as FL, routediag as RD
    facts = FL.flip_file(src, dst, flipped)
    bad = FL.verify(RD.load(src), dst, flipped, facts)
    n_bad = len({b.split(":")[0].split(".")[0] for b in bad})
    print(f"  [{tag}] back side: {len(flipped)} part(s) flipped, verified by pcbnew / model / file: {len(flipped) - n_bad}/{len(flipped)}"
          + (f" ({bad[0]})" if bad else ""), flush=True)
    if bad:
        raise RuntimeError(f"back side: {n_bad} flipped part(s) disagree between the model, pcbnew and the file: " + "; ".join(bad[:3]))
    return facts


def _one(board, cons, spec, grow, args, text_in, workdir, tag, start, log=print):
    from . import routediag as RD, boardwrite as BW, fanout as FO, legality as LGL
    t0 = time.time()
    path_g = board
    fixed_all = None
    moves_g = {}
    if any(grow):
        from . import routediag as RD0
        B0 = RD0.load(board)
        fixed_all = MD.fixed_set(B0, cons)
        text, moves_g = apply_growth(text_in, B0, cons, grow, fixed_all)
        path_g = os.path.join(workdir, f"{tag}.grown.kicad_pcb")
        text2, done = BW.apply_moves(text, moves_g)
        open(path_g, "w", errors="surrogateescape").write(text2)
        _pro(board, path_g)
    B = RD.load(path_g)
    from . import legalgate as LG
    base_in = getattr(args, "legal_base", None)
    if base_in is None:                          # what the input board already violates, taken before any seating
        base_in = LG.baseline(RD.load(board) if any(grow) else B, cons)
    orig = LG.poses(RD.load(path_g))             # every part's pose on the input: where a part that cannot be seated legally stands
    from . import fanout as FO0
    fa = argparse.Namespace(track=None, clearance=None, via=None, drill=None, via_pad_gap=0.2, reach=2.2, lane=1.0, max_dom=60,
                            nodes=200000, repairs=12, check_only=True)
    _B0, fres0 = FO0.run(path_g, cons, spec, None, fa, None)
    in_blocked = any(v["blocked"] for v in FO0.summarise(_B0, fres0).values())
    unseat = set()
    for outer in range(2):
        for attempt in range(3):
            M = MD.build(B, cons, spec, arrange=True, strict=(start != "construct"), unseat=unseat)   # the source start keeps the others at their input poses: a seat is kept only when legal against them
            if start == "construct" or in_blocked or attempt == 2:
                break
            more = _seats_that_block(B, M, path_g, orig, cons, spec, workdir, f"{tag}.arr{attempt}", fa)
            if not more:
                break
            print(f"  [{tag}] seats that blocked a pin's escape on the input's own fan-out and stay at their input pose: {', '.join(sorted(more)[:12])}{' ...' if len(more) > 12 else ''}", flush=True)
            unseat |= more
            B = RD.load(path_g)                      # build mutates the model: again from the input
        from . import liveview as LV0                # step 5 criteria on the live view: supports home and seated
        nsup = sum(len(m.get("members") or []) for m in M.get("modules") or [] if not m.get("fixed"))
        LV0.narrate("floorplan", f"Seating {nsup} support parts around {len(M.get('modules') or [])} modules' anchor parts (candidate {tag}).", "doing")
        LV0.progress("floorplan", "construct", always=True, cand=tag, why=f"{len(M.get('rehomed') or {})} bypass part(s) re-homed",
                     criteria=[{"id": "supports seated", "ok": not M.get("unseated"), "value": len(M.get("unseated") or []), "limit": 0,
                                "text": ", ".join(f"{u['ref']} ({u['why']})" for u in (M.get("unseated") or [])[:12])},
                               {"id": "pins bared by re-homing", "ok": not M.get("bared"), "value": len(M.get("bared") or []), "limit": 0,
                                "text": ", ".join(f"{b['ic']}.{b['pad']} ({b['was']})" for b in (M.get("bared") or [])[:12])},
                               {"id": "template units legal where they stand", "ok": not M.get("unit_illegal"), "value": len(M.get("unit_illegal") or []),
                                "limit": 0, "text": ", ".join(f"{u['ref']} ({'; '.join(u['issues'])})" for u in (M.get("unit_illegal") or [])[:12])}])
        for u in M.get("unseated") or []:
            print(f"  [{tag}] support {u['ref']} stays {u['dist']:g} mm from {u['ic']}.{u['pad']} (limit {u['limit']:g}): {u['why']}", flush=True)
        if M.get("backfill"):
            bf = M["backfill"]
            print(f"  [{tag}] back fill: {len(bf['singles'])} single part(s) flipped, {len(bf['modules'])} module(s) mirrored ({', '.join(bf['modules'][:8])}), {bf['mm2']:.0f} mm2"
                  + (f"; skipped {bf['skipped']}" if bf["skipped"] else ""), flush=True)
        P = Plan(B, cons, spec, M, log=log, base=base_in)
        fa = argparse.Namespace(track=None, clearance=None, via=None, drill=None, via_pad_gap=0.2, reach=2.2, lane=1.0, max_dom=60,
                                nodes=200000, repairs=12, check_only=True)
        # claims = the escape copper the WHOLE-board fan-out found on the board as it comes (a solution that coexists with the
        # neighbours), as geometry relative to each module. (Using the isolated solution for the pins it could not escape was
        # tried and made the result worse: RN400.7/.8 and U302.2 came back.)
        whole = defaultdict(list)
        okpins = set()
        for pin in fres0["pins"]:
            if pin.get("ok") and pin["ref"] in M["of"]:
                okpins.add((pin["ref"], pin["pin"]))
                m = M["modules"][P.idx[M["of"][pin["ref"]]]]
                ox, oy = m["pos"]
                e = {"ok": True, "w": pin.get("w", 0.15), "layer": pin.get("layer")}
                mx = -1.0 if m.get("mirror") else 1.0            # a module mirrored to the back (backfill.py): the input's escape copper is its mirror image, on the other layer
                if mx < 0 and e["layer"] in ("F.Cu", "B.Cu"):
                    e["layer"] = "B.Cu" if e["layer"] == "F.Cu" else "F.Cu"
                if pin.get("stub"):
                    e["stub"] = [[mx * (q[0] - ox), q[1] - oy] for q in pin["stub"]]
                if pin.get("via"):
                    e["via"] = [mx * (pin["via"][0] - ox), pin["via"][1] - oy]
                whole[m["name"]].append(e)
        for m in M["modules"]:
            m["fanout"] = {"pins": whole.get(m["name"], [])}
        cl = claims_from(M["modules"])
        bcl, bregs = breakout_claims(B, cons, spec, M, P)
        for i, extra in bcl.items():
            cl[i] = list(cl.get(i, [])) + extra
        P.set_claims(cl)
        P.set_keepouts({i: m["keepouts"] for i, m in enumerate(M["modules"]) if m.get("keepouts")})
        P.set_breakouts(bregs)
        P.freeze_baseline()

        clash = [] if start == "construct" else [i for i in range(P.n) if not P.fixed[i] and not P.claims_ok(i)]
        more = {r for i in clash for r in P.mods[i]["members"] if r in orig and r != P.mods[i]["anchor"] and r not in unseat
                and LG.displaced(P.B["parts"][r], orig[r]) > LG.MOVE_EPS}      # a seat on another module's claim / keep-out / breakout region stays at its input pose
        if not more or outer == 1:
            break
        print(f"  [{tag}] seats on a claim, keep-out or breakout region that stay at their input pose: {', '.join(sorted(more)[:12])}{' ...' if len(more) > 12 else ''}", flush=True)
        unseat |= more
        B = RD.load(path_g)
    if start != "construct":                     # the construct start places every module afresh; the source start keeps the seated poses
        moved_u, left_u = legalise_units(P, step=args.step)
        if moved_u or left_u:
            print(f"  [{tag}] units illegal where they stand: {len(moved_u)} re-seated ({', '.join(moved_u[:8])}), {len(left_u)} left ({', '.join(left_u[:8])})", flush=True)
    src = P.cost_parts()
    if start == "construct":
        failed = construct(P, seed=args.seed, step=args.step)
        P.noseat |= set(failed)
    else:
        failed = []
        from . import breakout as BO0
        bo_in = BO0.solve(RD.load(path_g), cons, spec)["failed"] if BO0.conf(cons)["enabled"] else []
        bo_mods = sorted({P.idx[P.of[f["ref"]]] for f in bo_in if f["ref"] in P.of and not P.fixed[P.idx[P.of[f["ref"]]]]})
        rep = []
        if in_blocked:                           # the input's own fan-out has blocked pins: modules on another's escape copper are re-seated (a claim a seat made was unseated above; the input's own are its baseline)
            rep = repair_claims(P, step=args.step)
            print(f"  [{tag}] {len(rep)} module(s) sat on another module's escape copper and were re-seated: {', '.join(rep[:12])}", flush=True)
        owners = [P.mods[i]["name"] for i in bo_mods if P.mods[i]["name"] not in rep and reseat_module(P, i, step=args.step)]
        if owners:
            print(f"  [{tag}] re-seated the module(s) of input breakout pins that fail: {', '.join(owners[:12])}", flush=True)
        if not (in_blocked or bo_mods):
            print(f"  [{tag}] input fan-out, breakout and claims are clean: no claim repair", flush=True)
    st0 = settle(P, orig, args.step)
    if any(st0.values()):
        print(f"  [{tag}] settled before the search: re-seated {len(st0['reseated'])}, at input poses {len(st0['parked'])}, unplaced {len(st0['unplaced'])}"
              + (f" ({', '.join(st0['unplaced'][:6])})" if st0["unplaced"] else ""), flush=True)
    init = P.cost_parts()
    from . import liveview as LV                 # F16: the constructed placement and the running anneal, when a stream is on
    LV.candidate("floorplan", f"{tag}-construct", P.B, {"modules": P.mods, "of": P.of}, [["cost", round(float(P.total(init)), 3)]], None, cons,
                 lane="trial", phase="construct", verdict="running", full=True)
    cap = None if start == "construct" else {"wl": init["wl"], "cross": init["cross"]}      # the source start never keeps a state worse than the one it starts the search from
    acc, best = anneal(P, iters=args.iters, seed=args.seed, watch=LV.watcher("floorplan", tag, P, cons), cap=cap)
    if start != "construct":                     # the search leaves supports where the input had them: those beyond their limit go home
        c0 = P.cost_parts()
        hd, hl = home_supports(P, cons, spec)
        c1 = P.cost_parts()
        print(f"  [{tag}] supports seated near their pin after the search: {len(hd)} ({', '.join(hd[:12])}), left {len(hl)}; model wl {c0['wl']:.0f} -> {c1['wl']:.0f}, crossings {c0['cross']} -> {c1['cross']}", flush=True)
    cur = P.cost_parts()
    reverted = {}
    res = {"tag": tag, "grow": list(grow), "modules": P.n, "movable": int((~P.fixed).sum()), "construct_failed": failed,
           "source": src, "constructed": init, "annealed": cur, "accepted": acc, "rounds": []}
    out_path = os.path.join(workdir, f"{tag}.kicad_pcb")
    base = path_g
    st1 = settle(P, orig, args.step)
    flipped = sorted({r for r in M.get("flipped") or []} | {r for r, p_ in P.B["parts"].items() if r in orig and p_["side"] != orig[r][3]})
    flipped = [r for r in flipped if r in P.of and not P.unplaced(P.idx[P.of[r]]) and P.idx[P.of[r]] not in P.parked and P.B["parts"][r]["side"] != orig[r][3]]
    if flipped:                                  # P5b S1: a support seated on the other side is flipped (pcbnew) before the moves are written
        base = os.path.join(workdir, f"{tag}.flipped.kicad_pcb")
        write_flips(path_g, base, flipped, tag)
        _pro(board, base)
    for rd in range(args.rounds + 1):
        if rd:
            settle(P, orig, args.step)
        moves = dict(all_moves(P))
        BW.move_file(base, out_path, moves, post=False)
        _pro(board, out_path)
        rep_ = LG.repair_file(out_path, path_g, cons, base_in, log=lambda t: print(f"  [{tag}] {t}", flush=True))     # nothing illegal is written: a part that adds an issue goes back
        reverted.update(rep_["reverted"])
        for r, why in rep_["reverted"].items():   # the model learns it: the module of a part that went back to its input pose stands at the input poses from here on
            if not any(w.startswith("input") for w in why):
                continue
            i = P.idx[P.of[r]]
            if not P.fixed[i] and i not in P.parked:
                P.park(i, orig)
        a = argparse.Namespace(track=None, clearance=None, via=None, drill=None, via_pad_gap=0.2, reach=2.2, lane=1.0, max_dom=60,
                               nodes=200000, repairs=12, check_only=True)
        Bn, fres = FO.run(out_path, cons, spec, None, a, None)
        per = FO.summarise(Bn, fres)
        req = sum(v["req"] for v in per.values())
        ok = sum(v["ok"] for v in per.values())
        blocked = [p for v in per.values() for p in v["blocked"]]
        print(f"  [{tag}] round {rd}: fan-out {ok}/{req}, blocked {len(blocked)}: " + ", ".join(f"{p['ref']}.{p['pin']}" for p in blocked[:8]), flush=True)
        res["rounds"].append({"round": rd, "escaped": ok, "required": req, "blocked": [f"{p['ref']}.{p['pin']}" for p in blocked],
                              "why": [f"{p['ref']}.{p['pin']} {p['net']} {p.get('blocker')}" for p in blocked[:8]]})
        if not blocked or rd == args.rounds:
            break
        # halo the modules that own a blocked pin, the module whose pad or escape blocks it, and the movable modules within
        # 3 mm of the pin; each of those that can move is re-seated at the nearest legal pose outside the halo
        bad = set()
        win = _window(cons)
        for p in blocked:
            bad.add(P.idx[P.of[p["ref"]]])
            br = _ref_in(p.get("blocker"), P.of)
            if br:
                bad.add(P.idx[P.of[br]])
            q = Bn["parts"].get(p["ref"])
            if q:
                for pad in q["pads"]:
                    if str(pad["num"]) == str(p["pin"]):
                        for j in range(P.n):
                            if P.fixed[j]:
                                continue
                            for r in P.mods[j]["members"]:
                                c = P.B["parts"][r]["court"]
                                if c[0] - win < pad["x"] < c[2] + win and c[1] - win < pad["y"] < c[3] + win:
                                    bad.add(j)
        for i in sorted(bad):
            P.halo[i] += 0.4
        P.rehalo()
        for i in sorted(bad):
            if not P.fixed[i] and i not in P.parked:
                reseat_module(P, i, step=args.step)
        anneal(P, iters=max(500, args.iters // 3), seed=args.seed + rd + 1, t0=1.0)
    res["escaped"], res["required"] = ok, req
    res["blocked"] = [p for v in per.values() for p in v["blocked"]]
    res["fanout_ok"] = not blocked
    res["board"] = out_path
    iss = LGL.Legal(Bn, cons, MD.fixed_set(Bn, cons)).issues()
    res["legality"] = {k: len(v) for k, v in iss.items()}
    res["legality_new"] = LG.new_issues(iss, base_in)
    res["construct_failed"] = sorted(P.noseat)
    try:                                         # crossings and wire length of the board that is written (and of the input), not of the search's model
        res["source"] = measured(path_g, cons, spec)
        res["annealed"] = measured(out_path, cons, spec)
    except Exception as e:                       # noqa: BLE001 - the report must never fail the floorplan
        print(f"  [{tag}] written-board metrics unavailable: {type(e).__name__}: {e}", flush=True)
    res["reverted"] = {r: w for r, w in sorted(reverted.items())}
    res["moved_modules"] = int(sum(1 for i in range(P.n) if not P.fixed[i] and (abs(P.X[i] - P.src[0][i]) > 0.05 or abs(P.Y[i] - P.src[1][i]) > 0.05 or P.T[i])))
    res["moved_parts"] = len(all_moves(P))
    res["secs"] = round(time.time() - t0, 1)
    res["metrics"] = metrics(Bn, cons, spec, P.cls)
    return res, P, Bn


def _seats_that_block(B, M, path_g, orig, cons, spec, workdir, tag, fa):
    """the supports whose seat blocks a pin the whole-board fan-out cannot escape on the arranged board (the input's own fan-out escapes
    every pin, so the seating did it): the pin's own part or the part named as its blocker when that is a moved support; else the moved
    supports whose courtyard is within `[floorplan] block_window_mm` of the pad. -> set of refs."""
    from . import boardwrite as BW, fanout as FO, flip as FL, legalgate as LG
    moves = {r: (round(p["pos"][0], 4), round(p["pos"][1], 4), round(p["angle"], 4)) for r, p in B["parts"].items()
             if LG.displaced(p, orig[r]) > LG.MOVE_EPS}              # a flipped part too: it was flipped, and stands where the arrangement put it
    flipped = [r for r, p in B["parts"].items() if p["side"] != orig[r][3]]
    base = path_g
    if flipped:
        base = os.path.join(workdir, f"{tag}.flipped.kicad_pcb")
        FL.flip_file(path_g, base, flipped)
        _pro(path_g, base)
    f = os.path.join(workdir, f"{tag}.kicad_pcb")
    BW.move_file(base, f, moves, post=False)
    _pro(path_g, f)
    Bn, fres = FO.run(f, cons, spec, None, fa, None)
    blocked = [p for v in FO.summarise(Bn, fres).values() for p in v["blocked"]]
    mods = {m["name"]: m for m in M["modules"]}
    sup = {r for m in M["modules"] if not m["fixed"] for r in m["members"]
           if r != m["anchor"] and r in orig and LG.displaced(B["parts"][r], orig[r]) > LG.MOVE_EPS}     # a moved support
    win = _window(cons)
    out = set()
    for p in blocked:
        named = {r for r in (p["ref"], _ref_in(p.get("blocker"), B["parts"])) if r in sup}
        if not named:
            q = Bn["parts"].get(p["ref"])
            for pad in (q["pads"] if q else ()):
                if str(pad["num"]) == str(p["pin"]):
                    named |= {r for r in sup if Bn["parts"][r]["court"][0] - win < pad["x"] < Bn["parts"][r]["court"][2] + win
                              and Bn["parts"][r]["court"][1] - win < pad["y"] < Bn["parts"][r]["court"][3] + win}
        out |= named
    return out


def measured(path, cons, spec):
    """the cost terms (wl, crossings, ...) of the placement as written in `path`."""
    from . import routediag as RD0
    B = RD0.load(path)
    return Plan(B, cons, spec, MD.build(B, cons, spec, arrange=False), log=lambda *a, **k: None).cost_parts()


def fanout_blocked(path, cons, spec):
    """pins the whole-board fan-out cannot escape on the board at `path` (check only, nothing written)."""
    from . import routediag as RD, fanout as FO
    a = argparse.Namespace(track=None, clearance=None, via=None, drill=None, via_pad_gap=0.2, reach=2.2, lane=1.0, max_dom=60,
                           nodes=200000, repairs=12, check_only=True)
    Bn, fres = FO.run(path, cons, spec, None, a, None)
    return sum(len(v["blocked"]) for v in FO.summarise(Bn, fres).values())


def plan_summary(path, cons, spec):
    """plan-route's calibrated global router on a placement (no copper considered): score, overflow, over-budget, return path."""
    from . import planroute as PL, routediag as RD
    R = PL.run(RD.load(path), cons, spec, ignore_routing=True, cell=None, board_path=path, open_method="text", log=None)
    c = R["comp"]
    return {"score": round(R["score"]), "overflow": c["overflow"], "over_budget_mm": c["over_budget_mm"], "sealed": c["sealed"],
            "pair_split": c["pair_split"], "return_path": c["return_path"]}


def first_failure(res):
    if res["construct_failed"]:
        cf = res["construct_failed"]
        return f"no legal pose for {len(cf)} module(s): {', '.join(cf[:6])}" + (" ..." if len(cf) > 6 else "")
    if res["legality_new"] and any(v for v in res["legality_new"].values()):
        return "legality: " + "; ".join(f"{k} {', '.join(v[:3])}" for k, v in res["legality_new"].items() if v)
    if not res["fanout_ok"]:
        return "fan-out: " + "; ".join(res["rounds"][-1]["why"][:3])
    return "ok"


def passes(res):
    return res["fanout_ok"] and not res["construct_failed"] and not any(res["legality_new"].values())


def edge_order(B, cons):
    """edges cheapest first: mean utilisation of the density cells on the edge (more crowded = growing there helps more),
    per mm of edge length (the area a mm of growth costs)."""
    from . import density as DN
    d = DN.compute(B, cons)
    x0, y0, x1, y1 = d["bbox"]
    g = d["grid"]
    heat = {}
    for k, test in {"L": lambda r: r["x"] <= x0 + 1e-6, "R": lambda r: r["x"] + g >= x1 - 1e-6,
                    "T": lambda r: r["y"] <= y0 + 1e-6, "B": lambda r: r["y"] + g >= y1 - 1e-6}.items():
        v = [min(r["util"], 2.0) for r in d["regions"] if test(r)]
        heat[k] = sum(v) / len(v) if v else 0.05
    ln = {"L": y1 - y0, "R": y1 - y0, "T": x1 - x0, "B": x1 - x0}
    return sorted(ln, key=lambda k: (ln[k] / max(heat[k], 0.05), k))


def _live_key(res):
    """The floorplan result as a judge key for the live view (liveview.progress): lower is better, left first."""
    return [["fan-out blocked", res["required"] - res["escaped"]], ["crossings", res["annealed"]["cross"]], ["wire length", round(res["annealed"]["wl"], 1)]]


def _live_crit(res):
    return [{"id": "fan-out", "ok": passes(res), "value": res["escaped"], "limit": res["required"], "text": str(res.get("why_not") or "")}]


def cmd_floorplan(a):
    from . import constraints as C, routediag as RD
    spec = json.load(open(a.spec)) if a.spec else None
    cons = C.load(a.constraints)
    from . import questions as QS
    S, cons, spec = QS.guard(a, "floorplan", a.board, cons, spec)
    out = a.out or os.path.splitext(a.board)[0] + ".floorplan.kicad_pcb"
    if os.path.abspath(out) == os.path.abspath(a.board):
        raise SystemExit("floorplan: --out must differ from the input board")
    wd = os.path.splitext(out)[0] + ".work"
    os.makedirs(wd, exist_ok=True)
    from . import intent as IT
    src_board = a.board
    from . import liveview as LV                 # F16: owner rows that came in before the search (no inbox folder: nothing changes)
    a.board, cons, fb = LV.stage_start(a.board, cons, spec, "floorplan", wd)
    from . import ready as RDY, parts as PTS                 # C7b: the rows of the input, measured now; the output is set against them
    ds_dir = PTS.resolve_dir((cons.get("docs") or {}).get("datasheets"), [a.constraints, a.board])
    rows_in = RDY.stage_rows(spec, cons, RD.load(a.board), a.board, ds_dir)
    ex = LV.Exporter(cons, a.constraints, stage="floorplan") if LV.work() else None
    from . import pins as PN
    a.board, cons, pin_res = PN.prepare(a.board, cons, spec, wd)       # --- pins: collisions among pinned parts, resolved by precedence
    if pin_res is not None and pin_res.unresolved:
        print("floorplan: FAIL - PIN_CONFLICT (see pins: above); nothing was placed")
        raise SystemExit(1)
    a.board, cons, seated = IT.prepare(a.board, cons, spec, wd)        # --- intent: hard items seated and held before the search
    text_in = open(a.board, errors="surrogateescape").read()
    B0 = RD.load(a.board)
    from . import legalgate as LG
    a.legal_base = LG.baseline(RD.load(src_board), cons)       # what the input board as it came in already violates: the stage may add nothing to it
    grow = tuple(float(v) for v in a.grow.split(",")) if a.grow else (0, 0, 0, 0)
    from . import density as DN
    ga = DN.parse_grow(QS.board_grow_answer(S))                # the owner's answer to `board-grow` of an earlier run: that outline growth
    if ga and not a.grow:
        grow = ga
        print(f"floorplan: board-grow answered: grow L{ga[0]:g} R{ga[1]:g} T{ga[2]:g} B{ga[3]:g} mm", flush=True)
    try:                                                       # crowding of the input, before the back fill
        rep_in = DN.crowding_report(B0, cons, spec, with_size=False)
        print(f"floorplan: {rep_in['line']}  [input, before the back fill]", flush=True)
    except Exception as e:                                     # noqa: BLE001 - a measurement must never stop the floorplan
        rep_in = None
        print(f"floorplan: density unavailable: {type(e).__name__}: {e}", flush=True)
    if len(grow) != 4:
        raise SystemExit("floorplan: --grow L,R,T,B (four numbers)")
    history = []
    cur = list(grow)
    order = edge_order(B0, cons)
    steps = 0
    t0 = time.time()
    while True:
        tag = "g_" + "_".join(f"{v:g}" for v in cur)
        res, P, Bn = floorplan_once(a.board, cons, spec, tuple(cur), a, text_in, wd, tag)
        res["why_not"] = first_failure(res)
        history.append(res)
        print(f"floorplan grow L{cur[0]:g} R{cur[1]:g} T{cur[2]:g} B{cur[3]:g}: {'FANOUT_OK' if passes(res) else 'FAIL'} - "
              f"fan-out {res['escaped']}/{res['required']}, {res['moved_modules']}/{res['movable']} modules moved, "
              f"cross {res['source']['cross']} -> {res['annealed']['cross']}, wl {res['source']['wl']:.0f} -> {res['annealed']['wl']:.0f}, "
              f"{res['secs']} s; {res['why_not']}")
        LV.candidate("floorplan", tag, Bn, {"modules": P.mods, "of": P.of} if P is not None else None, _live_key(res), _live_crit(res), cons, ex, board=res["board"],
                     generation=len(history), verdict="kept" if passes(res) else "dropped", why=res["why_not"], is_leader=True, leader=tag)
        if passes(res) or not a.size or steps >= a.max_grow * 4:
            break
        e = order[steps % 4]
        steps += 1
        k = {"L": 0, "R": 1, "T": 2, "B": 3}[e]
        cur[k] += 1.0
    best = history[-1]
    import shutil
    shutil.copyfile(best["board"], out)
    _pro(src_board, out)
    if pin_res is not None:
        PN.write(PN.sidecar_path(out), pin_res)
    elif os.path.exists(PN.sidecar_path(out)):
        os.remove(PN.sidecar_path(out))
    crowd = None
    try:                                                       # crowding after the back fill (and after the search): verdicts, size when still over
        pr_out = plan_summary(out, cons, spec)
    except Exception:                                          # noqa: BLE001
        pr_out = None
    try:
        Bc = RD.load(out)
        crowd = DN.crowding_report(Bc, cons, spec, overflow=(pr_out or {}).get("overflow"), order=edge_order(Bc, cons))
        vd = crowd["verdicts"]
        print(f"floorplan: {crowd['line']}  [output, after the back fill]", flush=True)
        for s_ in DN.SIDES:
            if vd[s_]["verdict"] != "ok":
                print(f"  crowding {s_} {vd[s_]['verdict']}: {'; '.join(vd[s_]['reasons'])}", flush=True)
        far_hot = any("hot cells" in r for s_ in DN.SIDES for r in vd[s_]["reasons"]) and any(vd[s_]["verdict"] == "fail" for s_ in DN.SIDES)
        over = any(vd[s_]["verdict"] == "fail" for s_ in DN.SIDES)
        if any(vd[s_]["verdict"] != "ok" for s_ in DN.SIDES):
            for ln in DN.size_lines(crowd["suggestion"]):
                print("floorplan: " + ln if ln.startswith("size") else ln, flush=True)
            ch, _r, ops = QS.ask_crowding(S, Bc, cons, spec, rep=crowd, out=lambda t: print("floorplan: " + t if t.startswith("crowding") else t, flush=True))
            crowd["options"] = DN.report_options(crowd, ops)
            crowd["question"] = {"id": "crowding", "choice": ch, "open": any(q["id"] == "crowding" for q in S.open)}
        import fluxplace.backside as BS0
        print("floorplan: " + BS0.assembly_note(Bc, cons, spec), flush=True)
        best["crowding"] = DN.jsonable({"input": rep_in and {k: v for k, v in rep_in.items() if k != "usage"}, "output": {k: v for k, v in crowd.items() if k not in ("usage", "options")},
                                        "options": crowd.get("options"), "question": crowd.get("question"),
                                        "usage_output": {s_: {k: v for k, v in crowd["usage"]["sides"][s_].items() if k not in ("cells",)} for s_ in DN.SIDES},
                                        "eligible_out": {k: v for k, v in crowd["usage"]["eligible"].items() if k != "refs"}})
    except Exception as e:                                     # noqa: BLE001
        print(f"floorplan: crowding unavailable: {type(e).__name__}: {e}", flush=True)
    S.mark(out)
    from . import wlrefine as WL                    # --- wire length: shorter where the fan-out, the plan and the intent allow it
    wl = WL.stage_step(out, cons, spec, check=lambda p: fanout_blocked(p, cons, spec))
    if wl and wl["moved"]:
        Bn = RD.load(out)
    rp = os.path.splitext(out)[0] + ".floorplan.json"
    json.dump({"history": [{k: v for k, v in h.items() if k != "blocked"} for h in history], "final": best["tag"]}, open(rp, "w"), indent=1, default=str)
    print(f"floorplan: {'PASS' if passes(best) else 'FAIL'} at grow {best['grow']}; wrote {out}; report {rp}; total {time.time() - t0:.0f} s")
    print(f"  metrics: RF_EXT {best['metrics'].get('RF_EXT')} mm; pair spans {span_summary(best['metrics']['pair_spans'])}")
    try:
        a0, a1 = plan_summary(a.board, cons, spec), plan_summary(out, cons, spec)
        print(f"  plan-route channels (WARN only), source -> result: {a0} -> {a1}")
        best["plan_route"] = {"source": a0, "result": a1}
    except Exception as e:                  # the report must never fail the floorplan
        print(f"  plan-route summary unavailable: {e}")
    print(f"  legality new: {best['legality_new']}")
    from . import breakout as BO                     # --- breakout gate: every pin of every dense part has an escape on the WHOLE placed board
    if BO.conf(cons)["enabled"]:
        fix = BO.repair(out, cons, spec, baseline=a.legal_base)                  # parts that wall a dense part in are moved out of its breakout first
        if fix["moved"]:
            print(f"breakout: {len(fix['moved'])} part(s) moved out of breakout regions; pins without an escape {fix['before']} -> {fix['after']}")
            Bn = RD.load(out)
    rp2 = LG.repair_file(out, a.board, cons, a.legal_base, log=lambda t: print(t, flush=True))      # a move after the search (breakout, wire length) adds nothing illegal
    if rp2["reverted"]:
        Bn = RD.load(out)
    drc = LG.gate(out, a.board, cons, a.legal_base, ref_path=src_board, log=lambda t: print(t, flush=True))     # KiCad DRC of the written board against the input's
    moved_after = bool(rp2["reverted"] or (drc and drc.get("reverted")) or (fix["moved"] if BO.conf(cons)["enabled"] else False))
    if moved_after:                                 # a repair after the search moved parts: the fan-out is measured again on the board that stands
        Bn = RD.load(out)
        nb = fanout_blocked(out, cons, spec)
        print(f"floorplan: fan-out re-checked after the repairs: {nb} pin(s) blocked")
        if nb and best["fanout_ok"]:
            best["fanout_ok"], best["why_not"] = False, f"fan-out: {nb} pin(s) blocked after the legality repairs"
    pgres = None
    if drc is not None and not drc["items"] and not rp2["left"]:     # --- placement gaps (rows): repair a row by a move, or say what it needs (a question)
        from . import placegaps as PG
        try:
            import shutil
            wd_pg = os.path.join(os.path.dirname(os.path.abspath(out)), os.path.splitext(os.path.basename(out))[0] + ".placegaps.work")
            ctx_pg = PG.Ctx(stage="floorplan", board=out, cons=cons, spec=spec, ds_dir=ds_dir, session=S)
            ev_pg = PG.FileEval(ctx_pg, out, src_board, wd_pg, legal_base=a.legal_base, measure=lambda p_: measured(p_, cons, spec),
                                guards={"fanout": lambda p_: fanout_blocked(p_, cons, spec)}, drc_ref_board=src_board)
            pgres = PG.close(ctx_pg, ["row-cin-side", "row-template", "row-pin-bypass", "row-away", "row-flight", "row-rail"], ev_pg, stage="floorplan")
            if pgres["accepted"]:
                shutil.copyfile(ev_pg.board_path, out)
                Bn = RD.load(out)
                try:                                            # the report of the search is refreshed to the board that stands now
                    from . import netclass as NC_
                    d_ = json.load(open(rp))
                    d_["rail_repair"] = {"accepted": [{k: x[k] for k in ("gap", "rung", "candidate", "refs", "moves")} for x in pgres["accepted"]],
                                         "measured": {k: round(float(v), 3) for k, v in measured(out, cons, spec).items()},
                                         "metrics": metrics(Bn, cons, spec, NC_.classify(Bn, cons, spec))}
                    json.dump(d_, open(rp, "w"), indent=1, default=str)
                except Exception as e:                          # noqa: BLE001
                    print(f"floorplan: rows repaired, report not refreshed ({type(e).__name__}: {str(e)[:120]})", flush=True)
                print(f"floorplan: rows: {len(pgres['accepted'])} repair(s) kept ({', '.join(x['candidate'] for x in pgres['accepted'])}); "
                      f"hard rows not honoured {pgres['score_in'][PG.TERMS.index('hard_rows')]} -> {pgres['score_out'][PG.TERMS.index('hard_rows')]}", flush=True)
            for rf in pgres["refused"][:6]:
                print(f"floorplan: row repair {rf.get('candidate', rf['rung'])} for {rf['gap'].split(':', 1)[1].split('|')[0]} refused by {rf['check']}", flush=True)
            for rec in PG.away_questions(ctx_pg, pgres):
                print(f"floorplan: row {rec['name']} cannot be met ({rec['value']} mm, limit {rec['limit']:g} mm, best reachable {rec['best']} mm, "
                      f"refused by {rec['refusing']}); question row:{rec['name']}", flush=True)
            for rp_ in sorted((ctx_pg.get("row_reports") or {}).values(), key=lambda t: t["name"]):
                print(f"floorplan: row {rp_['name']}: {rp_['why']}", flush=True)
            for rec in PG.pour_questions(ctx_pg, pgres):
                net = rec["net"]
                print(f"floorplan: rail {net} needs copper x{rec['copper_factor']:g} ({len(rec['loads_over'])} of {rec['loads']} loads beyond {rec['limit_mm']:g} mm: "
                      f"{', '.join(rec['loads_over'])}); question rail-pour:{net}", flush=True)
        except (Exception, SystemExit) as e:                    # noqa: BLE001 - a repair (or a guard) that cannot run leaves the board as it was
            done = bool(pgres and pgres.get("accepted"))        # the copy may already have happened: say which of the two it is
            print(f"floorplan: rows {'repaired, report not refreshed' if done else 'not repaired'}: {type(e).__name__}: {str(e)[:200]}", flush=True)
            if done:
                Bn = RD.load(out)
    ill = drc is None or bool(drc["items"]) or bool(rp2["left"])      # fails closed: a gate that did not run, or a part nothing could put right, is a failure
    if drc is not None and drc["items"]:
        print(LG.drc_line(drc))
        best["why_not"] = LG.drc_line(drc).replace("floorplan: ", "")
    elif ill:
        why = "legality gate did not run (kicad-cli gave no answer)" if drc is None else \
            f"{len(rp2['left'])} issue(s) no part could be moved out of: " + "; ".join(f"{r} {k} {d}".strip() for r, k, d in rp2["left"][:3])
        print(f"floorplan: ILLEGAL - {why}")
        best["why_not"] = why
    try:
        w = measured(out, cons, spec)
        print(f"  written board: crossings {w['cross']}, weighted wire length {w['wl']:.0f} (input {best['source']['cross']}, {best['source']['wl']:.0f})")
    except Exception as e:                          # noqa: BLE001
        print(f"  written-board metrics unavailable: {type(e).__name__}: {e}")
    brc = BO.gate_file(out, cons, spec, rp)
    irc = IT.gate(out, cons, spec) if IT.section(cons) or IT.items(Bn, cons)["edge"] else 0     # --- INTENT gate
    refused = not passes(best) or bool(irc or brc) or ill
    hon = RDY.stage_honoured("floorplan", spec, cons, rows_in, out, RD.load(out), failed=refused, ds_dir=ds_dir)
    print(hon["line"])
    from . import placegaps as PG; pgd = PG.detect_stage("floorplan", out, cons, spec, os.path.dirname(os.path.abspath(out)), base=os.path.splitext(os.path.basename(out))[0] + ".placegaps", ds_dir=ds_dir, attempts=pgres)
    nbd = PG.impossible(pgd["gaps"])                      # A8: a net over its budget even in free space refuses the placement
    for g_ in nbd:
        print(f"floorplan: FAIL net-bound {g_['detail']}")
    LV.candidate("floorplan", "result", Bn, None, _live_key(best), (_live_crit(best) or []) + (hon["criteria"] or []) or None, cons, ex, board=out,
                 lane="accepted", phase="gate", verdict="refused" if refused else "kept", why=best["why_not"], full=True)
    for line in fb.finish(Bn) if fb is not None else []:
        print(line)
    if ex is not None:
        ex.close()
    waive = (cons.get("floorplan") or {}).get("waive_fail")
    if not passes(best) and waive:
        print(f"floorplan: WAIVED - the FAIL above is accepted by [floorplan] waive_fail = {str(waive)!r} (recorded)")
    if ill or (not passes(best) and not waive) or irc or brc or nbd:
        raise SystemExit(1)                 # a FAIL must reach the caller (the pipeline stage): never a silent pass
    return 0


def add_parser(sub):
    p = sub.add_parser("floorplan", help="place modules as rigid blocks (construct + anneal), re-check every fan-out, optionally grow the outline")
    p.add_argument("board")
    p.add_argument("--constraints", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--grow", default=None, help="L,R,T,B mm to grow the outline first")
    p.add_argument("--size", action="store_true", help="sizing loop: grow one edge 1 mm at a time until it passes")
    p.add_argument("--max-grow", dest="max_grow", type=int, default=10, help="mm per edge at most (default 10)")
    p.add_argument("--iters", type=int, default=30000)
    p.add_argument("--rounds", type=int, default=3, help="fan-out repair rounds per size")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--start", choices=("both", "construct", "source"), default="both",
                   help="constructive placement, the source poses, or both (the better result wins)")
    p.add_argument("--step", type=float, default=1.0, help="constructive grid mm")
    p.add_argument("--full", dest="full_output", action="store_true")
    p.add_argument("-q", "--quiet", dest="quiet", action="store_true")
    p.add_argument("--top", type=int, default=None)
    from . import questions as QS
    QS.add_args(p)
    p.set_defaults(fn=cmd_floorplan)
