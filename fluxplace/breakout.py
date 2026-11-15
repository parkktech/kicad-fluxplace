"""Breakout: escape planning as part of placement.

A part whose pins cannot take a via beside the pad at the board's via size and clearances (fine-pitch rows, multi-row
connectors, QFN interiors: lateral pad pitch < pad width + via + 2 clearance) owns an ESCAPE PLAN TEMPLATE, derived from the
footprint geometry and the rules alone (no part names):

  per pad side   a row of pins leaves straight out from the row on the pad's layer (lane); the lanes of a row are
                 spread by 45 deg jogs when their pitch is under what a lane needs beside a via (via/2 + clearance + track/2),
                 then end in a STAGGERED via field beyond the row: m rows of via sites, m = ceil((via + clearance) / lane
                 pitch), pin i in row i mod m, the rows far enough apart that vias of neighbouring rows keep their clearance.
                 A pin whose net runs only on the pad's layer ends in a straight surface lane instead of a via; pins of
                 plane / pour nets get their plane via in the same field.
  breakout       the rectangle holding the lanes and vias of one side (depth beyond the row, width along it): other parts'
                 pads / holes and foreign copper must stay out of it, on the layers it uses.

`solve(B, cons, spec)` runs the template on the WHOLE board: every dense part together, each lane and via checked (exact
distances) against every other part's pads, holes, keep-outs, the outline, foreign copper and the lanes already planned.
A pin with no candidate (rows x lateral shifts) is reported with the gap to what blocks it.  `lay` writes the plan as locked
copper (lanes + vias).  Pure text model (routediag.load), no pcbnew.  Deterministic: sorted orders, no clocks.

    [breakout]   enabled = true   lane_start_mm = 0.15   max_extra_rows = 3   waive = ["REF.PIN", ...]
"""
import math
import os
import statistics
import uuid

from . import escapeplan as EP
from . import geom as GM
from . import netclass as NC

SIDES = ((-1, 0), (0, -1), (1, 0), (0, 1))               # outward normal of side 0 left / 1 top / 2 right / 3 bottom
SHIFTS = (0.0, 0.1, -0.1, 0.2, -0.2, 0.3, -0.3)
EPS = 1e-9


def conf(cons):
    b = dict((cons or {}).get("breakout") or {})
    return {"enabled": b.get("enabled", True) is not False, "a0": float(b.get("lane_start_mm", 0.15)),
            "extra": int(b.get("max_extra_rows", 3)), "waive": [str(x) for x in (b.get("waive") or [])]}


def params(cons, B=None):
    """Design floors: [plan] / [fab] floors and the fab profile's via / drill limits (never below them)."""
    from . import routerules as RR
    pr = EP.plan_params(cons)
    R = RR.rules(cons, None, B=B)
    c = conf(cons)
    return {"track": max(pr["track_mm"], R["min_track"]), "clr": max(pr["clearance_mm"], R["min_clearance"]),
            "via": max(pr["via_mm"], R["min_via"]), "drill": max(pr["drill_mm"], R["min_drill"]),
            "h2h": max(R["hole_to_hole"], 0.25), "edge": pr["edge_clr_mm"], "pr": pr, **c}


def _layer(q):
    for l in ("F.Cu", "B.Cu"):
        if l in q["layers"]:
            return l
    return None


def side_pads(part):
    """[[pads of side 0 left / 1 top / 2 right / 3 bottom]] of the SMD signal pads of a part. A pad belongs to the row (pads sharing
    its x, or its y, within 0.15 mm; the longer one) it sits in; a column of pads leaves left or right of the pad centroid, a line
    of pads up or down; short ties by the courtyard ratio."""
    ps = [q for q in part["pads"] if not q.get("np") and not q.get("tht") and _layer(q) is not None and q["w"] * q["h"] < EP.DEFAULTS["thermal_pad_mm2"]]
    out = [[], [], [], []]
    if not ps:
        return out
    cx = sum(q["x"] for q in ps) / len(ps)
    cy = sum(q["y"] for q in ps) / len(ps)
    c = part["court"]
    hw, hh = max((c[2] - c[0]) / 2, 1e-6), max((c[3] - c[1]) / 2, 1e-6)
    for q in ps:
        nx = sum(1 for o in ps if abs(o["x"] - q["x"]) < 0.15)
        ny = sum(1 for o in ps if abs(o["y"] - q["y"]) < 0.15)
        dx, dy = q["x"] - cx, q["y"] - cy
        horiz = (nx <= 2 and ny <= 2 and abs(dx) / hw >= abs(dy) / hh) if nx == ny else nx > ny
        if horiz:                                       # the column x = const: leaves left or right
            out[2 if dx > 0 else 0].append(q)
        else:
            out[3 if dy > 0 else 1].append(q)
    return out


def _uv(q, n):
    t = (-n[1], n[0])
    return q["x"] * t[0] + q["y"] * t[1], q["x"] * n[0] + q["y"] * n[1]


def _xy(u, v, n):
    t = (-n[1], n[0])
    return (u * t[0] + v * n[0], u * t[1] + v * n[1])


def _ext(q, n):
    """(half extent along the normal, half extent across it)."""
    return (q["ex"], q["ey"]) if n[0] else (q["ey"], q["ex"])


def dense_sides(part, pr):
    """[(k, pads)] sides of `part` that need a template: >= 3 pads and a lateral pad pitch under pad width + via + 2 clearance
    (no via fits beside a pad), or pads in more than one row."""
    if len({q["num"] for q in part["pads"] if not q.get("np")}) < 6:
        return []
    out = []
    for k, ps in enumerate(side_pads(part)):
        if len(ps) < 3:
            continue
        n = SIDES[k]
        us = sorted(_uv(q, n)[0] for q in ps)
        gaps = [b - a for a, b in zip(us, us[1:]) if b - a > 0.05]
        vs = sorted({round(_uv(q, n)[1] / 0.15) for q in ps})
        wlat = 2 * statistics.median(_ext(q, n)[1] for q in ps)
        if len(vs) > 1 or (gaps and statistics.median(gaps) < wlat + pr["via"] + 2 * pr["clr"] - EPS):
            out.append((k, ps))
    return out


class Solver:
    """Checks lanes and vias against the board; keeps what it planned."""

    def __init__(self, B, pr, ignore_routing=False, isolated=False):
        self.B, self.pr = B, pr
        self.isolated = isolated                        # one part alone: no outline, no keep-outs
        epr = dict(pr["pr"], track_mm=pr["track"], clearance_mm=pr["clr"])
        self.ob = EP.Obstacles(B, epr, ignore_routing)
        self.all_layers = frozenset(B["copper"])
        self.h = GM.Hash(1.0)
        self.items = {}
        self.holes = GM.Hash(1.0)
        self.hl = {}
        for ref in sorted(B["parts"]):
            for k, h in enumerate(GM.holes_of(B["parts"][ref])):
                r = h.get("drill", 0) / 2
                self.hl[(ref, k)] = (h["x"], h["y"], r)
                self.holes.add((ref, k), (h["x"] - r, h["y"] - r, h["x"] + r, h["y"] + r), "x")
        self.n = 0

    # ---- planned copper
    def add(self, rec):
        for a, b in zip(rec["pts"], rec["pts"][1:]):
            if math.hypot(a[0] - b[0], a[1] - b[1]) > 1e-6:
                self._put(("seg", a, b, rec["width"], rec["layer"], rec["net"]))
        if rec.get("via"):
            self._put(("via", rec["via"], rec["net"]))

    def _put(self, it):
        self.n += 1
        if it[0] == "seg":
            r = (min(it[1][0], it[2][0]) - it[3] / 2, min(it[1][1], it[2][1]) - it[3] / 2, max(it[1][0], it[2][0]) + it[3] / 2, max(it[1][1], it[2][1]) + it[3] / 2)
        else:
            d = self.pr["via"] / 2
            r = (it[1][0] - d, it[1][1] - d, it[1][0] + d, it[1][1] + d)
        self.items[self.n] = it
        self.h.add(self.n, r, "x")

    # ---- checks: [(slack mm, what)] (slack < 0 = violation)
    def check_seg(self, a, b, w, layer, net, own):
        pr, out = self.pr, []
        hw = w / 2
        rect = (min(a[0], b[0]) - hw, min(a[1], b[1]) - hw, max(a[0], b[0]) + hw, max(a[1], b[1]) + hw)
        for kind, r, n, ref, num in self.ob.hits(rect, pr["clr"], frozenset({layer}), net, ignore=(own,)):
            g = EP._seg_rect_gap(a, b, r) - hw
            out.append((g - pr["clr"], f"{ref}.{num}" if ref else f"{kind} of {n}"))
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        for i in range(max(1, int(L / 0.1)) + 1):
            s = i / max(1, int(L / 0.1))
            x, y = a[0] + (b[0] - a[0]) * s, a[1] + (b[1] - a[1]) * s
            z = self.ob.keepout_hit(x, y, frozenset({layer}))
            if z:
                out.append((-hw, f"keep-out {z}"))
                break
            if not self.isolated and not GM.in_outline(self.B, (x - 0.01, y - 0.01, x + 0.01, y + 0.01), pr["edge"]):
                out.append((-hw, "board edge"))
                break
        for k in self.h.hits(rect, "x", pr["clr"] + pr["via"] / 2):
            it = self.items[k]
            if it[0] == "seg":
                if it[5] == net or it[4] != layer:
                    continue
                g = EP._seg_seg_gap(a, b, it[1], it[2]) - hw - it[3] / 2
                out.append((g - pr["clr"], f"lane of {it[5]}"))
            else:
                if it[2] == net:
                    continue
                g = EP.RD_seg_dist(it[1], a, b) - hw - pr["via"] / 2
                out.append((g - pr["clr"], f"via of {it[2]}"))
        return [o for o in out if o[0] < EPS] or []

    def check_via(self, p, net, own):
        pr, out = self.pr, []
        d = pr["via"]
        r = d / 2
        rect = (p[0] - r, p[1] - r, p[0] + r, p[1] + r)
        for kind, rc, n, ref, num in self.ob.hits(rect, pr["clr"], self.all_layers, net, ignore=(own,)):
            g = EP._rect_gap_pt(rc, p[0], p[1]) - r
            out.append((g - pr["clr"], f"{ref}.{num}" if ref else f"{kind} of {n}"))
        for key in self.holes.hits(rect, "x", pr["clr"] + 1.0):
            hx, hy, hr = self.hl[key]
            g = math.hypot(hx - p[0], hy - p[1])
            out.append((min(g - hr - r - pr["clr"], g - hr - pr["drill"] / 2 - pr["h2h"]), f"hole of {key[0]}"))
        if not self.isolated and self.ob.keepout_hit(p[0], p[1], self.all_layers, vias=True):
            out.append((-r, "keep-out"))
        if not self.isolated and not GM.in_outline(self.B, (p[0] - r, p[1] - r, p[0] + r, p[1] + r), pr["edge"]):
            out.append((-r, "board edge"))
        for k in self.h.hits(rect, "x", pr["clr"] + 1.0):
            it = self.items[k]
            if it[0] == "seg":
                if it[5] == net:
                    continue
                g = EP.RD_seg_dist(p, it[1], it[2]) - it[3] / 2 - r
                out.append((g - pr["clr"], f"lane of {it[5]}"))
            else:
                g = math.hypot(p[0] - it[1][0], p[1] - it[1][1])
                need = (pr["drill"] + pr["h2h"]) if it[2] == net else d + pr["clr"]
                out.append((g - need, f"via of {it[2]}"))
        return [o for o in out if o[0] < EPS]


def _mk_pts(n, u0, vs, wl, vv, s, a0, surface):
    """lane polyline (world) from the pad edge: stub, 45 deg jog to the lane's lateral slot, straight, final shift to the via."""
    pts = [(u0, vs), (u0, vs + a0)]
    j = wl - u0
    if abs(j) > 1e-6:
        pts.append((wl, vs + a0 + abs(j)))
    if surface:
        pts.append((wl, max(vv, pts[-1][1])))
        return [_xy(u, v, n) for u, v in pts], None
    if abs(s) > 1e-6:
        pts.append((wl, vv - abs(s)))
        pts.append((wl + s, vv))
        via = _xy(wl + s, vv, n)
    else:
        pts.append((wl, vv))
        via = _xy(wl, vv, n)
    return [_xy(u, v, n) for u, v in pts], via


def _spread(us, dmin):
    """lateral lane slots: neighbours at least dmin apart; a run that had to be widened is recentred on where it started."""
    w = list(us)
    for i in range(1, len(w)):
        w[i] = max(w[i], w[i - 1] + dmin)
    out, i = [], 0
    while i < len(w):
        j = i
        while j + 1 < len(w) and w[j + 1] - w[j] <= dmin + 1e-6:
            j += 1
        sh = sum(w[k] - us[k] for k in range(i, j + 1)) / (j - i + 1)
        out += [w[k] - sh for k in range(i, j + 1)]
        i = j + 1
    return out


def plan_side(S, part, ref, k, pads, info, prior=()):
    """Plan every pin of one side with solver S. -> (pin records, failures)."""
    pr = S.pr
    n = SIDES[k]
    d, clr, w = pr["via"], pr["clr"], pr["track"]
    dpass = d / 2 + clr + w / 2 + 0.003
    pins = sorted(pads, key=lambda q: (_uv(q, n)[0], str(q["num"])))
    uv = [_uv(q, n) for q in pins]
    ve = [uv[i][1] + _ext(q, n)[0] for i, q in enumerate(pins)]
    vmax = max(ve)
    us = [u for u, _ in uv]
    wl = _spread(us, dpass + 0.004) if any(b - a < dpass for a, b in zip(us, us[1:])) else us
    gap = min([b - a for a, b in zip(wl, wl[1:])] or [9.0])
    m = max(1, int(math.ceil((d + clr) / max(gap, 0.05) - 1e-6))) if len(wl) > 1 else 1
    if gap >= d + clr:
        m = 1
    drow = math.sqrt(max(0.0, (d + clr) ** 2 - min(gap, d + clr) ** 2)) + 0.02 if m > 1 else 0.0
    jogmax = max(abs(a - b) for a, b in zip(wl, us))
    base = max(d / 2 + clr, pr["a0"] + jogmax + d / 2 + clr)
    recs, fail = [], []
    # row-pattern index inside runs of lanes closer than a via pitch
    for i, q in enumerate(pins):
        net = q["net"]
        if not net or S.ob.attached(q):
            continue
        layer = _layer(q)
        others = [o for o in info["netpads"].get(net, []) if o is not q and o["ref"] != ref]
        surface = bool(others) and net not in info["plane"] and all(layer in o["layers"] and not o.get("tht") for o in others)
        rows = [i % m] + [r for r in range(m + pr["extra"]) if r != i % m]
        best = None
        for r in rows:
            for s in (SHIFTS if not surface else (0.0,)):
                vv = vmax + base + r * drow
                pts, via = _mk_pts(n, uv[i][0], uv[i][1] + _ext(q, n)[0], wl[i], vv, s, pr["a0"], surface)
                bad = []
                for a, b in zip(pts, pts[1:]):
                    if math.hypot(a[0] - b[0], a[1] - b[1]) > 1e-6:
                        bad += S.check_seg(a, b, w, layer, net, (ref, q["num"]))
                if via:
                    bad += S.check_via(via, net, (ref, q["num"]))
                if not bad:
                    best = {"ref": ref, "pin": str(q["num"]), "net": net, "side": k, "layer": layer, "pad": (q["x"], q["y"]), "pts": pts,
                            "via": via, "via_d": d, "row": r, "kind": "surface" if surface else ("plane" if net in info["plane"] else "via"), "width": w}
                    break
                if best is None and r == rows[0] and s == 0.0:
                    first_bad = sorted(bad)[0]
            if best:
                break
        if not best:                                    # last resort: leave the pad in another direction (a corner pad, a pad beside a row)
            best = _alt_dir(S, q, ref, k, info, surface, w)
        if best:
            S.add(best)
            recs.append(best)
        else:
            fail.append({"ref": ref, "pin": str(q["num"]), "net": net, "side": k, "gap": round(first_bad[0], 3), "blocker": first_bad[1],
                         "msg": f"{ref}.{q['num']} ({net}): no escape lane/via site; {first_bad[1]} short by {-first_bad[0]:.3f} mm"})
    return recs, fail


def _alt_dir(S, q, ref, k, info, surface, w):
    """a single pin out of its pad in one of the three other directions (straight lane, via row 0.. beyond, small shifts)."""
    pr = S.pr
    d, clr = pr["via"], pr["clr"]
    net, layer = q["net"], _layer(q)
    for k2 in range(4):
        if k2 == k:
            continue
        n = SIDES[k2]
        u0, v0 = _uv(q, n)
        ve = v0 + _ext(q, n)[0]
        for r in range(pr["extra"] + 2):
            for s in (SHIFTS if not surface else (0.0,)):
                vv = ve + max(d / 2 + clr, pr["a0"] + d / 2 + clr) + r * (d + clr + 0.02)
                pts, via = _mk_pts(n, u0, ve, u0, vv, s, pr["a0"], surface)
                bad = []
                for a, b in zip(pts, pts[1:]):
                    if math.hypot(a[0] - b[0], a[1] - b[1]) > 1e-6:
                        bad += S.check_seg(a, b, w, layer, net, (ref, q["num"]))
                if via:
                    bad += S.check_via(via, net, (ref, q["num"]))
                if not bad:
                    return {"ref": ref, "pin": str(q["num"]), "net": net, "side": k, "layer": layer, "pad": (q["x"], q["y"]), "pts": pts, "via": via,
                            "via_d": d, "row": r, "alt": k2, "kind": "surface" if surface else ("plane" if net in info["plane"] else "via"), "width": w}
    return None


def _region(recs, pads, k, pr):
    n = SIDES[k]
    xs, ys = [], []
    r = pr["via"] / 2 + pr["clr"]
    for rc in recs:
        for p in rc["pts"]:
            h = rc["width"] / 2 + pr["clr"]
            xs += [p[0] - h, p[0] + h]
            ys += [p[1] - h, p[1] + h]
        if rc.get("via"):
            xs += [rc["via"][0] - r, rc["via"][0] + r]
            ys += [rc["via"][1] - r, rc["via"][1] + r]
    if not xs:
        return None
    rect = (min(xs), min(ys), max(xs), max(ys))
    vm = max(_uv(q, n)[1] + _ext(q, n)[0] for q in pads)
    corners = ((rect[0], rect[1]), (rect[2], rect[3]), (rect[0], rect[3]), (rect[2], rect[1]))
    depth = max(x * n[0] + y * n[1] for x, y in corners) - vm
    return {"rect": tuple(round(v, 4) for v in rect), "depth": round(depth, 3), "width": round((rect[3] - rect[1]) if n[0] else (rect[2] - rect[0]), 3),
            "layers": sorted({rc["layer"] for rc in recs} | ({"through"} if any(rc.get("via") for rc in recs) else set()))}


def _info(B, cons, spec):
    cls = NC.classify(B, cons, spec)
    pr = EP.plan_params(cons)
    return {"netpads": NC.net_pads(B), "plane": set(EP.plane_nets(B, cons, cls, pr))}


def solve(B, cons=None, spec=None, refs=None, pr=None, ignore_routing=None, others=True):
    """Template solve of every dense part on the board `B` (or of `refs`), all together.  `others=False` = in isolation (only the
    part's own pads and nothing else on the board).  -> {"templates": {ref: {"sides": [...], "pins": [...]}}, "pins": {(ref, pin): rec},
    "failed": [...], "dense": [refs]}."""
    pr = pr or params(cons, B)
    if not pr["enabled"]:
        return {"templates": {}, "pins": {}, "failed": [], "dense": []}
    if ignore_routing is None:
        ignore_routing = not B["segs"] and not B["vias"]
    info = _info(B, cons, spec) if others else {"netpads": {}, "plane": set()}      # in isolation every pin is a via pin
    dense = []
    for ref in sorted(B["parts"]):
        part = B["parts"][ref]
        if EP.is_mech(ref):
            continue
        ds = dense_sides(part, pr)
        if ds and (not refs or ref in refs):
            dense.append((-sum(len(p) for _, p in ds), ref, ds))
    dense.sort(key=lambda t: (t[0], t[1]))
    S = Solver(B, pr, ignore_routing) if others else None
    res = {"templates": {}, "pins": {}, "failed": [], "dense": [r for _, r, _ in dense]}
    for _, ref, ds in dense:
        part = B["parts"][ref]
        if not others:
            Bi = dict(B, parts={ref: part}, pads=part["pads"], segs=[], vias=[], zones=[])
            Si = Solver(Bi, pr, True, isolated=True)
        sv = Si if not others else S
        tpl = {"ref": ref, "sides": [], "pins": []}
        for k, pads in ds:
            recs, fail = plan_side(sv, part, ref, k, pads, info)
            tpl["pins"] += recs
            for r in recs:
                res["pins"][(ref, r["pin"])] = r
            res["failed"] += fail
            reg = _region(recs, pads, k, pr)
            if reg:
                reg.update(k=k, npins=len(recs), rows=max(r["row"] for r in recs) + 1)
                tpl["sides"].append(reg)
        res["templates"][ref] = tpl
    waive = set(pr["waive"])
    res["waived"] = [f for f in res["failed"] if f"{f['ref']}.{f['pin']}" in waive]
    res["failed"] = [f for f in res["failed"] if f"{f['ref']}.{f['pin']}" not in waive]
    return res


def reach(B, ref, pr):
    """{side k: (reach beyond the courtyard edge, lateral lo, hi)} of the ideal (isolated) breakout of part `ref` at its pose: what
    the placer keeps free outside the courtyard on that side.  Cached per pose."""
    part = B["parts"][ref]
    key = (ref, round(part["pos"][0], 3), round(part["pos"][1], 3), part["angle"], pr["via"], pr["clr"], pr["track"], pr["a0"])
    cache = B.setdefault("_breakout_reach", {})
    if key not in cache:
        res = solve(B, None, None, refs={ref}, pr=pr, others=False)
        c = part["court"]
        out = {}
        for sd in (res["templates"].get(ref) or {"sides": []})["sides"]:
            k = sd["k"]
            r = sd["rect"]
            edge = (c[0], c[1], c[2], c[3])[k]
            n = SIDES[k]
            far = (r[0] if n[0] < 0 else r[2]) if n[0] else (r[1] if n[1] < 0 else r[3])
            out[k] = (max(0.0, (edge - far) if (n[0] < 0 or n[1] < 0) else (far - edge)),
                      r[1] if n[0] else r[0], r[3] if n[0] else r[2])
        cache[key] = out
    return cache[key]


def gate(B, cons=None, spec=None, res=None):
    """Whole-board gate: every signal / power pin of every dense part has a feasible escape in the presence of every other part
    and reservation. -> (ok, result, lines)."""
    res = res or solve(B, cons, spec)
    lines = []
    npins = len(res["pins"]) + len(res["failed"]) + len(res["waived"])
    lines.append(f"breakout: {'PASS' if not res['failed'] else 'FAIL'} - {len(res['pins'])}/{npins} pins of {len(res['dense'])} dense parts have an escape on the whole board"
                 + (f", {len(res['waived'])} waived" if res["waived"] else ""))
    for f in res["waived"]:
        lines.append(f"BREAKOUT_PIN_WAIVED {f['ref']}.{f['pin']} {f['net']} ({f['blocker']}, {f['gap']} mm) - WAIVED by [breakout] waive")
    for f in res["failed"]:
        lines.append(f"BREAKOUT_PIN_BLOCKED {f['msg']}")
    return not res["failed"], res, lines


def gate_file(path, cons, spec, report=None, log=print):
    """gate on the board at `path`; prints the lines, records the result in the JSON report; -> 1 on FAIL else 0.  Not waived by
    --allow-incomplete or [floorplan] waive_fail: only a named pin in [breakout] waive is (reported loudly)."""
    from . import routediag as RD
    if not conf(cons)["enabled"]:
        return 0
    B = RD.load(path)
    ok, res, lines = gate(B, cons, spec)
    for l in lines:
        log(l)
    if report:
        import json
        try:
            d = json.load(open(report))
        except (OSError, ValueError):
            d = {}
        d["breakout"] = {"ok": ok, "dense": res["dense"], "pins": len(res["pins"]), "failed": res["failed"], "waived": res["waived"],
                         "templates": {r: t["sides"] for r, t in res["templates"].items()}}
        json.dump(d, open(report, "w"), indent=1, default=str)
    return 0 if ok else 1


def rects(B, ref, res, pr=None):
    """[(rect, sides {'F','B'})] of the breakout of `ref`: the lane region on the pad layer, each via site (through) on both."""
    pr = pr or params(None, B)
    t = res["templates"].get(ref)
    out = []
    if not t:
        return out
    r = pr["via"] / 2 + pr["clr"]
    for s in t["sides"]:
        lay = {"F" if l == "F.Cu" else "B" for l in s["layers"] if l in ("F.Cu", "B.Cu")}
        out.append((tuple(s["rect"]), lay))
    for p in t["pins"]:
        if p.get("via"):
            out.append(((p["via"][0] - r, p["via"][1] - r, p["via"][0] + r, p["via"][1] + r), {"F", "B"}))
    return out


def claims(B, cons, spec, pr=None):
    """{ref: {"lane": [(rect, sides)], "via": [(rect, sides)]}} of the ISOLATED templates (the ideal breakout of each part)."""
    res = solve(B, cons, spec, pr=pr, others=False)
    out = {}
    for ref, t in res["templates"].items():
        lane = [(tuple(s["rect"]), {"F" if t["pins"][0]["layer"] == "F.Cu" else "B"}) for s in t["sides"] if t["pins"]]
        r = (pr or params(cons, B))["via"] / 2 + (pr or params(cons, B))["clr"]
        vias = [((p["via"][0] - r, p["via"][1] - r, p["via"][0] + r, p["via"][1] + r), {"F", "B"}) for p in t["pins"] if p.get("via")]
        out[ref] = {"lane": lane, "via": vias, "sides": t["sides"]}
    return out


def lay_text(text, pins, drill):
    """board text with the planned lanes and vias as locked copper, grouped `breakout:<ref>`. -> (text, segments, vias)."""
    from . import boardtext as BT
    blks, ns, nv = [], 0, 0
    u = lambda: str(uuid.uuid4())
    for key in sorted(pins, key=lambda k: (k[0], pins[k]["side"], pins[k]["row"], k[1])):
        r = pins[key]
        for a, b in zip(r["pts"], r["pts"][1:]):
            if math.hypot(a[0] - b[0], a[1] - b[1]) > 1e-6:
                blks.append(f'\t(segment\n\t\t(start {a[0]:.4f} {a[1]:.4f})\n\t\t(end {b[0]:.4f} {b[1]:.4f})\n\t\t(width {r["width"]})\n'
                            f'\t\t(layer "{r["layer"]}")\n\t\t(net "{r["net"]}")\n\t\t(locked yes)\n\t\t(uuid "{u()}")\n\t)')
                ns += 1
        if r.get("via"):
            blks.append(f'\t(via\n\t\t(at {r["via"][0]:.4f} {r["via"][1]:.4f})\n\t\t(size {r.get("via_d", 0.45)})\n\t\t(drill {drill})\n\t\t(layers "F.Cu" "B.Cu")\n'
                        f'\t\t(net "{r["net"]}")\n\t\t(locked yes)\n\t\t(uuid "{u()}")\n\t)')
            nv += 1
    return BT.append_blocks(text, blks), ns, nv


def _pad_rect(q, m=0.0):
    return (q["x"] - q["ex"] - m, q["y"] - q["ey"] - m, q["x"] + q["ex"] + m, q["y"] + q["ey"] + m)


def _sides_of(q):
    return {"F" if l == "F.Cu" else "B" for l in q["layers"] if l in ("F.Cu", "B.Cu")} or {"F", "B"}


def repair(path, cons, spec, rounds=4, step=0.25, reach=5.0, log=print):
    """Evict what walls a dense part in: every movable, non-dense part whose pad blocks a pin's template (the gate's blocker) is moved
    to the nearest legal pose (same angle first, then turned) whose pads stay out of every dense part's breakout region, then the
    whole board is solved again; up to `rounds`.  Parts the placement holds (fixed, locked, pinned) never move.  Writes `path` in place.
    -> {"moved": {ref: (x, y, angle, mm)}, "before": failed pins, "after": failed pins}."""
    from . import boardwrite as BW, legality as LGL, modules as MD, routediag as RD
    pr0 = None
    B = RD.load(path)
    if not conf(cons)["enabled"]:
        return {"moved": {}, "before": 0, "after": 0}
    pr0 = params(cons, B)
    fixed = MD.fixed_set(B, cons) | {r for r, p in B["parts"].items() if p.get("locked")}
    L = LGL.Legal(B, cons, fixed)
    origin = {r: (p["pos"][0], p["pos"][1], p["angle"]) for r, p in B["parts"].items()}
    res = solve(B, cons, spec, pr=pr0)
    n0 = len(res["failed"])
    moved = {}
    for _ in range(rounds):
        if not res["failed"]:
            break
        dense = set(res["dense"])
        blockers = sorted({f["blocker"].split(".")[0] for f in res["failed"] if "." in f["blocker"] and not f["blocker"].startswith(("via", "lane", "keep", "board", "hole"))})
        blockers = [r for r in blockers if r in B["parts"] and r not in fixed and r not in dense]
        if not blockers:
            break
        iso = solve(B, cons, spec, pr=pr0, others=False)
        regs = [(r, s) for ref in sorted(iso["templates"]) for r, s in rects(B, ref, iso, pr0)]
        progressed = False
        for ref in blockers:
            part = B["parts"][ref]
            x0, y0, a0 = part["pos"][0], part["pos"][1], part["angle"]
            base = set(LGL.Legal.part_issues(L, ref))
            cands = []
            R = int(reach / step)
            for i in range(-R, R + 1):
                for j in range(-R, R + 1):
                    d = math.hypot(i, j) * step
                    if 0 < d <= reach:
                        for ai, da in enumerate((0, 180, 90, 270)):
                            cands.append((round(d + 0.3 * ai, 4), i, j, da))
            cands.sort()
            hit = None
            for _d, i, j, da in cands:
                x, y, a = x0 + i * step, y0 + j * step, (a0 + da) % 360
                pads, court = GM.moved(part, x, y, a)
                m = pr0["clr"] + 0.05
                if any(GM.overlap(_pad_rect(q, m), rc, 0.0) and (_sides_of(q) & sd) for q in pads if not q.get("np") for rc, sd in regs):
                    continue
                if set(L.part_issues(ref, court=court, pads=pads)) - base:
                    continue
                hit = (x, y, a)
                break
            if hit:
                GM.place(part, *hit)
                L.refresh(ref)
                moved[ref] = hit
                progressed = True
        if not progressed:
            break
        res = solve(B, cons, spec, pr=pr0)
    if moved:
        mv = {r: v for r, v in moved.items()}
        BW.move_file(path, path + ".tmp", mv, post=False)
        os.replace(path + ".tmp", path)
        for r, (x, y, a) in sorted(moved.items()):
            ox, oy, oa = origin[r]
            log(f"breakout: moved {r} {math.hypot(x - ox, y - oy):.2f} mm" + (f", turned {(a - oa) % 360:g}" if a != oa else "") + " out of a breakout region")
    return {"moved": {r: (v[0], v[1], v[2], round(math.hypot(v[0] - origin[r][0], v[1] - origin[r][1]), 3)) for r, v in moved.items()},
            "before": n0, "after": len(res["failed"])}
