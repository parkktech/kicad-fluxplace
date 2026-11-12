"""Escape planner: can every pin of a fine-pitch part (and every plane pin) get out, under the design
floors, BEFORE anything is routed.  Pure text model (routediag.load), no pcbnew.

(`escape.py` is the older router-driven rule annealer; this module plans from the placement alone.)

Per pin of a part with pitch <= 0.65 mm or >= 16 pads (and per signal pin that has no free lane):

  direct   a lane of `lane_len` mm from the pad edge outward (or inward) on the pad's own layer is free of
           foreign pads, track-forbidding keep-outs, the outline and (default mode) foreign copper
  slot     else a dog-bone: a stub to a via site on a hex lattice (pitch 0.6 mm >= via + clearance, so two
           lattice sites never conflict).  Slots are a finite resource shared between neighbouring pins and
           parts, solved as a maximum bipartite matching (Kuhn augmenting paths) on 4 lattice phases; the phase
           with the most pins served (then the shortest stubs) wins.
  sealed   no lane and no slot -> PLAN_PIN_SEALED, with the blockers named

Plane nets (GND, +5V, any net with an inner plane or a `pour = true` power entry, rails of >= `pour_pads`
pads) need a via site within `plane_radius` of EVERY pad (PLAN_NO_VIA_SITE).  A via may sit in an exposed
thermal pad of a plane net, never in a signal pad.  A pad of a net that has a pour on the pad's own layer
and a free lane counts as served by the pour ("pour").

Floors come from `[plan]` in the constraints (track_mm, clearance_mm, via_mm, drill_mm, ...) and default to
0.127 / 0.127 / 0.45 / 0.20.
"""
import fnmatch
import math
import re
from collections import Counter, defaultdict

from . import geom as GM
from . import netclass as NC

DEFAULTS = {"track_mm": 0.127, "clearance_mm": 0.127, "via_mm": 0.45, "drill_mm": 0.20, "cell_mm": 2.0,
            "lane_len_mm": 1.0, "reach_mm": 1.6, "plane_radius_mm": 1.5, "edge_clr_mm": 0.30,
            "fine_pitch_mm": 0.65, "fine_pads": 16, "pour_pads": 20, "thermal_pad_mm2": 2.0,
            "via_cost_mm": 3.0, "iterations": 10, "util": 0.8, "w_sealed": 100.0, "w_nosite": 40.0,
            "w_overflow": 5.0, "w_overbudget": 3.0, "w_overbudget_hard": 20.0, "w_pair": 30.0, "w_rf": 30.0, "w_unreach": 200.0,
            "w_len": 0.05, "jog_frac": 0.5, "via_units": 2.0, "via_density": 0.5, "max_risk_frac": 0.06, "max_risk_nets": 10.0, "design_track_mm": 0.0, "design_clearance_mm": 0.0}
SIDE_NAMES = {(1, 0): "E", (-1, 0): "W", (0, 1): "S", (0, -1): "N"}      # KiCad y grows downward
MECH = ("MH", "MK", "FID", "TP", "H")


def plan_params(cons=None, **over):
    """Design floors and planner knobs: DEFAULTS < [fab]/[plan] in the constraints < keyword overrides."""
    pr = dict(DEFAULTS)
    pc = (cons or {}).get("plan") or {}
    for k in DEFAULTS:
        if k in pc:
            pr[k] = float(pc[k])
    for k, v in over.items():
        if v is not None and k in DEFAULTS:
            pr[k] = float(v)
    pr["track_mm"] = max(pr["track_mm"], float((((cons or {}).get("fab")) or {}).get("min_track_mm", 0)))
    pr["clearance_mm"] = max(pr["clearance_mm"], float((((cons or {}).get("fab")) or {}).get("min_clearance_mm", 0)))
    # the owner's floor (`[plan] floor_mm`, default 0.127): a lower [plan] value is raised to it and recorded, unless the caller is
    # deliberately evaluating a sub-floor option (`[plan] allow_subfloor = true`, plan-options only; such results are never offered)
    pr["floor_mm"] = float(pc.get("floor_mm", 0.127))
    pr["floor_clamped"] = []
    if not pc.get("allow_subfloor"):
        for k in ("track_mm", "clearance_mm", "design_track_mm", "design_clearance_mm"):
            if 0 < pr[k] < pr["floor_mm"] - 1e-9:
                pr["floor_clamped"].append(f"{k} {pr[k]:g} -> {pr['floor_mm']:g}")
                pr[k] = pr["floor_mm"]
    pr["pitch_mm"] = pr["track_mm"] + pr["clearance_mm"]
    # geometry the controlled-impedance and power nets keep whatever the low-speed rule is (0 = the rule itself)
    pr["design_pitch_mm"] = (pr["design_track_mm"] or pr["track_mm"]) + (pr["design_clearance_mm"] or pr["clearance_mm"])
    pr["design_pitch_mm"] = max(pr["design_pitch_mm"], pr["pitch_mm"])
    pr["slot_mm"] = round(pr["via_mm"] + pr["clearance_mm"] + 0.02, 3)
    return pr


def prect(q):
    return (q["x"] - q["ex"], q["y"] - q["ey"], q["x"] + q["ex"], q["y"] + q["ey"])


def is_mech(ref):
    base = ref.rstrip("0123456789")
    return base in MECH and ref[len(base):].isdigit()


def part_pitch(part):
    ps = [q for q in part["pads"] if not q.get("np")]
    best = 999.0
    for i in range(len(ps)):
        for j in range(i + 1, len(ps)):
            if ps[i]["num"] == ps[j]["num"]:
                continue
            d = math.hypot(ps[i]["x"] - ps[j]["x"], ps[i]["y"] - ps[j]["y"])
            if 0.05 < d < best:
                best = d
    return best


def is_fine(part, pr):
    return part["n_pads"] >= pr["fine_pads"] or part_pitch(part) <= pr["fine_pitch_mm"]


def plane_nets(B, cons, cls, pr):
    """{net: {kind, layers}} for the nets that connect through a plane/pour rather than a trace."""
    from . import preroute as PR
    out = {}
    sig, planes = PR.layer_split(B, cons)
    for z in B["zones"]:
        if z["net"] and not z["keepout"] and z["pts"]:
            xs, ys = [q[0] for q in z["pts"]], [q[1] for q in z["pts"]]
            x0, y0, x1, y1 = B["bbox"]
            if (max(xs) - min(xs)) * (max(ys) - min(ys)) >= 0.6 * max((x1 - x0) * (y1 - y0), 1e-6):
                e = out.setdefault(z["net"], {"kind": "pour", "layers": set()})
                e["layers"] |= set(z["layers"])
                if set(z["layers"]) & set(planes):
                    e["kind"] = "plane"
    for n, v in ((cons or {}).get("power") or {}).items():
        if isinstance(v, dict) and v.get("pour"):
            out.setdefault(n, {"kind": "pour", "layers": set()})
    for n in (((cons or {}).get("plan") or {}).get("plane_nets") or []):
        out.setdefault(n, {"kind": "pour", "layers": set()})
    pads = NC.net_pads(B)
    for n, info in cls.items():
        if n not in out and NC.is_ground(n):
            out[n] = {"kind": "pour", "layers": set()}
    for n in list(pads):
        if NC.is_ground(n) and n not in out:
            out[n] = {"kind": "pour", "layers": set()}
    return out


def power_no_plane(B, cons, cls, pr, pnets):
    """{net: pins} for rails of >= pour_pads pads that have neither a plane nor a pour (nor `[plan] plane_nets`)."""
    return {n: info["pads"] for n, info in cls.items()
            if info["class"] == "rail" and info["pads"] >= pr["pour_pads"] and n not in pnets and not NC.is_ground(n)}


def plan_layers(B, cons):
    """(signal layers, plane layers): preroute.layer_split, but an inner layer that carries tracks (>= 20 segments, a mixed
    signal + pour layer such as In2/In3 after the +3V3 fill) stays a signal layer."""
    from . import preroute as PR
    sig, planes = PR.layer_split(B, cons)
    n = Counter(s["layer"] for s in B["segs"])
    mixed = [l for l in planes if n[l] >= 20]
    sig = [l for l in B["copper"] if l in sig or l in mixed]
    return sig, [l for l in planes if l not in mixed]


class Obstacles:
    """Spatial index of what a track or a via cannot touch."""

    def __init__(self, B, pr, ignore_routing=True, skip_refs=()):
        self.B, self.pr = B, pr
        self.h = GM.Hash(2.0)
        self.items = {}
        self.copper = not ignore_routing
        n = 0
        for ref, part in B["parts"].items():
            for q in part["pads"]:
                self.items[n] = ("pad", prect(q), q["net"], frozenset(q["layers"]), ref, q["num"])
                self.h.add(n, prect(q), "x")
                n += 1
        if self.copper:
            for s in B["segs"]:
                r = s["w"] / 2
                rc = (min(s["a"][0], s["b"][0]) - r, min(s["a"][1], s["b"][1]) - r,
                      max(s["a"][0], s["b"][0]) + r, max(s["a"][1], s["b"][1]) + r)
                self.items[n] = ("track", rc, s["net"], frozenset({s["layer"]}), "", "")
                self.h.add(n, rc, "x")
                n += 1
            for v in B["vias"]:
                r = v["d"] / 2
                rc = (v["x"] - r, v["y"] - r, v["x"] + r, v["y"] + r)
                self.items[n] = ("via", rc, v["net"], frozenset(B["copper"]), "", "")
                self.h.add(n, rc, "x")
                n += 1
        self.kt = [z for z in B["zones"] if z["keepout"] and z["no_tracks"] and z["pts"]]
        self.kv = [z for z in B["zones"] if z["keepout"] and z.get("no_vias", z["no_tracks"]) and z["pts"]]
        for z in self.kt + self.kv:
            xs, ys = [q[0] for q in z["pts"]], [q[1] for q in z["pts"]]
            z["_bb"] = (min(xs), min(ys), max(xs), max(ys))

    def hits(self, rect, gap, layers, net, ignore=()):
        """[(kind, rect, net, ref, num)] foreign copper within `gap` of rect on any of `layers`
        (net '' never matches an own-net exemption)."""
        out = []
        for k in self.h.hits(rect, "x", gap):
            kind, r, n, ly, ref, num = self.items[k]
            if (ref, num) in ignore or not (ly & layers):
                continue
            if net and n == net:
                continue
            out.append((kind, r, n, ref, num))
        return out

    def attached(self, q):
        """Default mode: does same-net copper (a track end or a via) already touch pad q?"""
        if not self.copper or not q["net"]:
            return False
        r = prect(q)
        for k in self.h.hits(r, "x", 0.02):
            kind, rc, n, ly, ref, num = self.items[k]
            if kind in ("track", "via") and n == q["net"] and (ly & frozenset(q["layers"])) and GM.overlap(rc, r, 0.0):
                return True
        return False

    def keepout_hit(self, x, y, layers, vias=False):
        for z in (self.kv if vias else self.kt):
            if not (z["layers"] & layers):
                continue
            b = z["_bb"]
            if b[0] <= x <= b[2] and b[1] <= y <= b[3] and GM.poly_has(z["pts"], x, y):
                return z.get("name") or z.get("owner") or "keep-out"
        return None

    def keepout_rect(self, rect, layers, vias=False):
        for z in (self.kv if vias else self.kt):
            if not (z["layers"] & layers) or not GM.overlap(z["_bb"], rect):
                continue
            for x in (rect[0], (rect[0] + rect[2]) / 2, rect[2]):
                for y in (rect[1], (rect[1] + rect[3]) / 2, rect[3]):
                    if GM.poly_has(z["pts"], x, y):
                        return z.get("name") or z.get("owner") or "keep-out"
            if any(rect[0] <= px <= rect[2] and rect[1] <= py <= rect[3] for px, py in z["pts"]):
                return z.get("name") or z.get("owner") or "keep-out"
        return None


def exit_dirs(part, q, cx, cy):
    """Candidate exit directions of a pad, outward first: along the long axis of an elongated pad, else all four."""
    ox, oy = q["x"] - cx, q["y"] - cy
    long_x = q["ex"] >= 1.6 * q["ey"]
    long_y = q["ey"] >= 1.6 * q["ex"]
    if long_x:
        c = [(1, 0), (-1, 0)]
    elif long_y:
        c = [(0, 1), (0, -1)]
    else:
        c = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    return sorted(c, key=lambda d: -(d[0] * ox + d[1] * oy))


def lane_free(ob, B, pr, q, d, layers, length):
    """Free length (mm) of a lane leaving pad q in direction d, and the first blocker (desc, distance)."""
    w, clr = pr["track_mm"], pr["clearance_mm"]
    sx = q["x"] + d[0] * q["ex"]
    sy = q["y"] + d[1] * q["ey"]
    ex_, ey_ = sx + d[0] * length, sy + d[1] * length
    hw = w / 2
    rect = (min(sx, ex_) - hw, min(sy, ey_) - hw, max(sx, ex_) + hw, max(sy, ey_) + hw)
    best, why = length, None
    for kind, r, n, ref, num in ob.hits(rect, clr, frozenset(layers), q["net"], ignore=((q["ref"], q["num"]),)):
        # distance along d from the lane start to the obstacle's near edge (minus clearance)
        if d[0] > 0:
            dist = r[0] - clr - sx
        elif d[0] < 0:
            dist = sx - (r[2] + clr)
        elif d[1] > 0:
            dist = r[1] - clr - sy
        else:
            dist = sy - (r[3] + clr)
        if dist < best:
            best, why = max(0.0, dist), (f"{ref}.{num}" if ref else f"{kind} of {n}")
    steps = max(1, int(length / 0.1))
    for k in range(steps + 1):
        s = length * k / steps
        if s >= best:
            break
        x, y = sx + d[0] * s, sy + d[1] * s
        z = ob.keepout_hit(x, y, frozenset(layers))
        if z:
            best, why = s, f"keep-out {z}"
            break
        if not GM.in_outline(B, (x - 0.01, y - 0.01, x + 0.01, y + 0.01), pr["edge_clr_mm"]):
            best, why = s, "board edge"
            break
    return best, why


def hexsites(phase, x0, y0, x1, y1, step):
    """Hex lattice points (step mm in-row, rows step*0.866) inside the box, at one of 4 phase offsets."""
    ox, oy = ((0.0, 0.0), (0.5, 0.0), (0.0, 0.5), (0.5, 0.5))[phase]
    rs = step * 0.8660254
    j0, j1 = int(math.floor(y0 / rs - oy)) - 1, int(math.ceil(y1 / rs - oy)) + 1
    for j in range(j0, j1 + 1):
        y = (j + oy) * rs
        sh = 0.5 if j % 2 else 0.0
        i0, i1 = int(math.floor(x0 / step - ox - sh)) - 1, int(math.ceil(x1 / step - ox - sh)) + 1
        for i in range(i0, i1 + 1):
            yield (i, j), ((i + ox + sh) * step, y)


def _rect_gap_pt(r, x, y):
    return math.hypot(max(0.0, r[0] - x, x - r[2]), max(0.0, r[1] - y, y - r[3]))


def _seg_rect_gap(a, b, r):
    """Distance from segment ab to rectangle r (0 when they touch)."""
    if GM.seg_hits_rect(a, b, r):
        return 0.0
    best = min(_rect_gap_pt(r, *a), _rect_gap_pt(r, *b))
    for c in ((r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])):
        best = min(best, RD_seg_dist(c, a, b))
    return best


def RD_seg_dist(p, a, b):
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


class Plan:
    """Escape plan: .pins {(ref, num): record}, .findings, .headroom, .slots (used sites)."""

    def __init__(self):
        self.pins, self.findings, self.headroom, self.used = {}, [], {}, {}
        self.nopour, self.extra, self.groups = {}, [], []
        self.stats = {}


def plan_escapes(B, cons=None, cls=None, pr=None, ignore_routing=True, only_refs=None, skip_nets=(), routed_nets=(),
                 phase=None, taken=(), pnets=None):
    """Plan every escape. `only_refs`: restrict the analysed parts (their pins still compete for shared sites
    only with each other). `routed_nets`: nets already complete on the board (default mode): not planned."""
    cons = cons or {}
    pr = pr or plan_params(cons)
    cls = cls if cls is not None else NC.classify(B, cons, None)
    VIP = list(((cons or {}).get("plan") or {}).get("via_in_pad") or [])
    plan = Plan()
    ob = Obstacles(B, pr, ignore_routing)
    pnets = pnets if pnets is not None else plane_nets(B, cons, cls, pr)
    plan.plane = pnets
    npw = power_no_plane(B, cons, cls, pr, pnets)
    plan.nopour = npw
    for n, cnt in sorted(npw.items()):
        plan.extra.append({"code": "PLAN_POWER_NO_PLANE", "net": n, "value": cnt, "at": [0, 0],
                              "msg": f"{n} has {cnt} pins and neither a plane nor a pour: its distribution is routed as a tree "
                                     f"and charged to the cells it crosses",
                              "suggestion": f"give {n} a pour on an inner signal layer (zone on In2/In3 with a via beside each pad), "
                                            f"or widen its distribution: {cnt} pins need >= {cnt} via sites or a 2 mm bus"})
    parts = B["parts"]
    refs = sorted(r for r in parts if (only_refs is None or r in only_refs) and not is_mech(r))
    lane_len, clr, via_r = pr["lane_len_mm"], pr["clearance_mm"], pr["via_mm"] / 2
    direct_lanes = {}                      # (ref, num) -> lane rect, only the first lane_len mm
    needs = []                             # pins to be matched to sites
    fine_refs = set()
    for ref in refs:
        part = parts[ref]
        fine = is_fine(part, pr)
        if fine:
            fine_refs.add(ref)
        pp = [q for q in part["pads"] if not q.get("np")]
        if not pp:
            continue
        cx = sum(q["x"] for q in pp) / len(pp)
        cy = sum(q["y"] for q in pp) / len(pp)
        out_layer = frozenset({part["side"]})
        for q in pp:
            net = q["net"]
            if not net or net in skip_nets or net in routed_nets:
                continue
            key = (ref, q["num"])
            if ob.attached(q):
                plan.pins[key] = {"ref": ref, "pad": q["num"], "net": net, "x": round(q["x"], 2), "y": round(q["y"], 2),
                                  "layer": sorted(q["layers"])[0], "fine": fine, "plane": net in pnets, "kind": "attached"}
                continue
            layers = frozenset(q["layers"]) if len(q["layers"]) < 3 else out_layer
            rec = {"ref": ref, "pad": q["num"], "net": net, "x": round(q["x"], 2), "y": round(q["y"], 2),
                   "layer": sorted(layers)[0], "fine": fine, "plane": net in pnets}
            if net in pnets:
                fl = sorted(pnets[net]["layers"]) or ["pour (pad layer)"]
                rec["feed"] = fl[0] if len(fl) == 1 else "/".join(l[:-3] for l in fl)
            plan.pins[key] = rec
            if net in pnets:
                pour = [z for z in B["zones"] if z["net"] == net and not z["keepout"] and (z["layers"] & layers)
                        and z["pts"] and GM.poly_has(z["pts"], q["x"], q["y"])]
                if pour:
                    best = max((lane_free(ob, B, pr, q, d, layers, lane_len)[0] for d in exit_dirs(part, q, cx, cy)),
                               default=0.0)
                    if best >= 0.5 or part["n_pads"] <= 2:
                        rec.update(kind="pour", lane=round(best, 2))
                        continue
                needs.append(("plane", key, q, part, layers, cx, cy))
                continue
            if not fine:
                rec.update(kind="direct", dir=None, lane=None)       # coarse part: assumed to escape
                continue
            best, bd, why = -1.0, None, None
            for d in exit_dirs(part, q, cx, cy):
                f, w_ = lane_free(ob, B, pr, q, d, layers, lane_len)
                if f > best:
                    best, bd, why = f, d, w_
                if f >= lane_len - 1e-6:
                    break
            if best >= lane_len - 1e-6:
                sx = q["x"] + bd[0] * q["ex"]
                sy = q["y"] + bd[1] * q["ey"]
                hw = pr["track_mm"] / 2
                direct_lanes[key] = (min(sx, sx + bd[0] * lane_len) - hw, min(sy, sy + bd[1] * lane_len) - hw,
                                     max(sx, sx + bd[0] * lane_len) + hw, max(sy, sy + bd[1] * lane_len) + hw)
                outward = (bd[0] * (q["x"] - cx) + bd[1] * (q["y"] - cy)) >= 0
                rec.update(kind="direct", dir=SIDE_NAMES[bd], lane=round(best, 2), inward=not outward)
            else:
                rec.update(lane=round(max(best, 0.0), 2), blocker=why, dir=SIDE_NAMES.get(bd) if bd else None)
                needs.append(("signal", key, q, part, layers, cx, cy))
    # ---- via sites ----------------------------------------------------------------------
    lh = GM.Hash(2.0)
    for k, r in direct_lanes.items():
        lh.add(k, r, "x")
    site_cache = {}

    def site_ok(ph, ij, xy):
        k = (ph, ij)
        if k in site_cache:
            return site_cache[k]
        x, y = xy
        ok, owner = True, None
        r = via_r + clr
        box = (x - r, y - r, x + r, y + r)
        allc = frozenset(B["copper"])
        if not GM.in_outline(B, (x - via_r, y - via_r, x + via_r, y + via_r), pr["edge_clr_mm"]):
            ok = False
        elif ob.keepout_hit(x, y, allc, vias=True) or ob.keepout_rect((x - via_r, y - via_r, x + via_r, y + via_r), allc, True):
            ok = False
        else:
            for kind, rc, n, ref_, num in ob.hits(box, 0.0, allc, ""):
                gap = _rect_gap_pt(rc, x, y)
                if kind == "pad":
                    pad = next((p for p in parts[ref_]["pads"] if p["num"] == num), None)
                    big = pad is not None and 4 * pad["ex"] * pad["ey"] >= pr["thermal_pad_mm2"] and (
                        pad["net"] in pnets) and not pad["tht"]
                    if not big and pad is not None and VIP and any(fnmatch.fnmatch(ref_, g) for g in VIP) and \
                            2 * min(pad["ex"], pad["ey"]) >= pr["via_mm"] + 0.1 and not pad["tht"]:
                        big = True                          # via-in-pad allowed for this part class (`[plan] via_in_pad`)
                    inside = rc[0] + via_r + 0.1 <= x <= rc[2] - via_r - 0.1 and rc[1] + via_r + 0.1 <= y <= rc[3] - via_r - 0.1
                    if big and inside:
                        owner = (ref_, num)
                        continue
                if gap < via_r + clr - 1e-9:
                    ok = False
                    break
        site_cache[k] = (ok, owner)
        return ok, owner

    cand = {}
    R_sig, R_pl = pr["reach_mm"], pr["plane_radius_mm"]
    phases = range(4) if phase is None else (phase,)
    for ph in phases:
        for kind, key, q, part, layers, cx, cy in needs:
            R = R_pl if kind == "plane" else R_sig
            rc = prect(q)
            lst = []
            tht = q["tht"]
            for ij, (x, y) in hexsites(ph, rc[0] - R - via_r, rc[1] - R - via_r, rc[2] + R + via_r, rc[3] + R + via_r,
                                       pr["slot_mm"]):
                gap = _rect_gap_pt(rc, x, y)
                if gap > R + via_r:
                    continue
                if ij in taken:
                    continue
                ok, owner = site_ok(ph, ij, (x, y))
                if not ok:
                    continue
                if owner and owner != (key[0], key[1]):
                    continue
                if not owner and gap < via_r + clr - 1e-9:
                    continue
                if any(k2 != key and GM.rect_gap(r2, (x - via_r, y - via_r, x + via_r, y + via_r)) < clr + pr["track_mm"] / 2
                       for k2, (r2, _s) in ((k, lh.r[k]) for k in lh.hits((x - via_r - 0.3, y - via_r - 0.3, x + via_r + 0.3,
                                                                          y + via_r + 0.3), "x", 0.0))):
                    continue
                if not tht and not owner:
                    # the stub from the pad edge to the via must clear foreign pads on the pad's layer
                    px = min(max(x, rc[0]), rc[2])
                    py = min(max(y, rc[1]), rc[3])
                    a, b = (px, py), (x, y)
                    box2 = (min(a[0], b[0]) - 0.3, min(a[1], b[1]) - 0.3, max(a[0], b[0]) + 0.3, max(a[1], b[1]) + 0.3)
                    bad = False
                    for kd, r3, n3, ref3, num3 in ob.hits(box2, 0.0, frozenset(layers), q["net"],
                                                         ignore=((key[0], key[1]),)):
                        if _seg_rect_gap(a, b, r3) < clr + pr["track_mm"] / 2 - 1e-9:
                            bad = True
                            break
                    if bad:
                        continue
                lst.append((gap, ij))
            lst.sort()
            cand[(ph, key)] = lst
    best_ph, best_m, best_key = phases[0], {}, (-1, 0.0)
    for ph in phases:
        order = sorted(needs, key=lambda n: (len(cand[(ph, n[1])]), n[1]))
        match, owner_of = {}, {}

        def aug(key, seen):
            for _g, ij in cand[(ph, key)]:
                if ij in seen:
                    continue
                seen.add(ij)
                if ij not in owner_of or aug(owner_of[ij], seen):
                    owner_of[ij] = key
                    match[key] = ij
                    return True
            return False

        import sys
        sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
        for n in order:
            if cand[(ph, n[1])]:
                aug(n[1], set())
        n_ok = len(match)
        dist = sum(next(g for g, ij in cand[(ph, k)] if ij == v) for k, v in match.items())
        if (n_ok, -dist) > (best_key[0], -best_key[1]):
            best_ph, best_m, best_key = ph, dict(match), (n_ok, dist)
    # ---- record -------------------------------------------------------------------------
    xyof = {}
    for ph in (best_ph,):
        for kind, key, q, part, layers, cx, cy in needs:
            rc = prect(q)
            R = R_pl if kind == "plane" else R_sig
            for ij, (x, y) in hexsites(ph, rc[0] - R - via_r, rc[1] - R - via_r, rc[2] + R + via_r, rc[3] + R + via_r,
                                       pr["slot_mm"]):
                xyof[ij] = (x, y)
    for kind, key, q, part, layers, cx, cy in needs:
        rec = plan.pins[key]
        ij = best_m.get(key)
        if ij:
            x, y = xyof[ij]
            rec.update(kind="via", via=[round(x, 2), round(y, 2)], stub=round(_rect_gap_pt(prect(q), x, y), 2),
                       cands=len(cand[(best_ph, key)]))
            plan.used[ij] = key
        else:
            rec["kind"] = "no_site" if kind == "plane" else "sealed"
            rec["cands"] = len(cand[(best_ph, key)])
    _stub_conflicts(plan, needs, pr)
    plan.stats = {"phase": best_ph, "sites_tested": len(site_cache), "needs": len(needs)}
    _findings(B, pr, plan, parts, ob, fine_refs)
    _oversubscribed(B, pr, plan, parts, ob, fine_refs, site_ok, best_ph)
    plan.findings = plan.extra + plan.findings
    _headroom(B, pr, plan, parts, ob, fine_refs, site_ok, best_ph)
    return plan


def _oversubscribed(B, pr, plan, parts, ob, fine_refs, site_ok, phase):
    """Per pin GROUP (one row/side of a fine-pitch part): the signal pins of the row must all leave through the fan-out region.
    Supply = tracks the free part of the region's outer edge carries on the pad layer (edge_len / pitch) + via slots in the
    region (assigned or free).  Needed > supply -> PLAN_ESCAPE_OVERSUBSCRIBED (part, side, pins needed vs slots available)."""
    reach, tw = pr["reach_mm"], pr["pitch_mm"]
    for ref in sorted(fine_refs):
        part = parts[ref]
        pp = [q for q in part["pads"] if not q.get("np")]
        if not pp:
            continue
        cx = sum(q["x"] for q in pp) / len(pp)
        cy = sum(q["y"] for q in pp) / len(pp)
        rows = defaultdict(list)
        for q in pp:
            rec = plan.pins.get((ref, q["num"]))
            if not rec or rec.get("kind") in (None, "pour", "attached", "no_site") or rec.get("plane"):
                continue
            if rec.get("kind") == "direct" and rec.get("dir"):
                d = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}[rec["dir"]]
                if rec.get("inward"):
                    d = (-d[0], -d[1])
            else:
                d = exit_dirs(part, q, cx, cy)[0]
            rows[(d, rec["layer"], round((q["y"] if d[1] else q["x"]) / 0.3))].append((q, d))
        for (d, layer, _k), items in rows.items():
            n = len(items)
            if n < 4:
                continue
            qs = [q for q, _d in items]
            along = (lambda q: q["y"]) if d[0] else (lambda q: q["x"])
            t0, t1 = min(along(q) for q in qs) - 0.4, max(along(q) for q in qs) + 0.4
            if d[0]:
                edge = (max(q["x"] + q["ex"] for q in qs) if d[0] > 0 else min(q["x"] - q["ex"] for q in qs)) + d[0] * reach
            else:
                edge = (max(q["y"] + q["ey"] for q in qs) if d[1] > 0 else min(q["y"] - q["ey"] for q in qs)) + d[1] * reach
            free, steps = 0.0, max(1, int((t1 - t0) / 0.1))
            lay = frozenset({layer})
            for k in range(steps):
                t = t0 + (k + 0.5) * (t1 - t0) / steps
                x, y = (edge, t) if d[0] else (t, edge)
                r = (x - 0.06, y - 0.06, x + 0.06, y + 0.06)
                if ob.hits(r, pr["clearance_mm"], lay, "") or ob.keepout_hit(x, y, lay) or not GM.in_outline(B, r, pr["edge_clr_mm"]):
                    continue
                free += (t1 - t0) / steps
            n_dir = sum(1 for q in qs if plan.pins[(ref, q["num"])].get("kind") == "direct")
            tracks = min(n_dir, int(free / tw))
            # slots only where a lane (or the stub of one) reaches: depth = the best free lane of the row + a via
            depth = min(reach, max((plan.pins[(ref, q["num"])].get("lane") or 0.0) for q in qs) + pr["via_mm"] + pr["clearance_mm"])
            if n_dir:
                depth = reach
            edge = edge - d[0] * (reach - depth) - 0.0 if d[0] else edge
            if d[1]:
                edge = edge - d[1] * (reach - depth)
            # via slots in the region between the row and the edge
            if d[0]:
                box = (min(edge, min(q["x"] for q in qs)), t0, max(edge, max(q["x"] for q in qs)), t1)
            else:
                box = (t0, min(edge, min(q["y"] for q in qs)), t1, max(edge, max(q["y"] for q in qs)))
            slots = 0
            for ij, (x, y) in hexsites(phase, box[0], box[1], box[2], box[3], pr["slot_mm"]):
                if box[0] <= x <= box[2] and box[1] <= y <= box[3] and ok_site(site_ok, phase, ij, x, y):
                    slots += 1
            supply = tracks + slots
            pts = []
            for k in range(int((t1 - t0) / 0.5) + 1):
                t = t0 + k * 0.5
                pts.append((round(edge, 2), round(t, 2)) if d[0] else (round(t, 2), round(edge, 2)))
            plan.groups.append({"ref": ref, "side": SIDE_NAMES[d], "layer": layer, "dir": 0 if d[0] else 1, "n": n, "slots": slots,
                                "edge": pts, "nets": sorted({plan.pins[(ref, q["num"])]["net"] for q in qs}),
                                "pin": qs[0]["num"], "at": [round(qs[0]["x"], 2), round(qs[0]["y"], 2)]})
            if n > supply:
                side = SIDE_NAMES[d]
                plan.extra.append({"code": "PLAN_ESCAPE_OVERSUBSCRIBED", "part": ref, "side": side, "pin": qs[0]["num"],
                                   "net": plan.pins[(ref, qs[0]["num"])]["net"], "at": [round(qs[0]["x"], 2), round(qs[0]["y"], 2)],
                                   "needed": n, "slots": supply, "value": n - supply, "limit": 0,
                                   "msg": f"{ref} side {side} ({layer}): {n} signal pins need {n} escapes but the fan-out region offers "
                                          f"{supply} ({tracks} tracks over {free:.1f} mm of free edge + {slots} via slots)",
                                   "suggestion": f"open the {_dirword(d, True)} fan-out of {ref}: clear {n - supply} more track/slot place(s) "
                                                 f"(move the part or the neighbours in front of {ref}'s {_dirword(d, True)} side), "
                                                 f"or reassign {n - supply} signal(s) to another side"})


def ok_site(site_ok, ph, ij, x, y):
    ok, owner = site_ok(ph, ij, (x, y))
    return ok and not owner


def _seg_seg_gap(a, b, c, d):
    """Distance between segments ab and cd."""
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    if (cross(a, b, c) * cross(a, b, d) < 0) and (cross(c, d, a) * cross(c, d, b) < 0):
        return 0.0
    return min(RD_seg_dist(a, c, d), RD_seg_dist(b, c, d), RD_seg_dist(c, a, b), RD_seg_dist(d, a, b))


def _stub_conflicts(plan, needs, pr):
    """Dog-bone stubs are checked against each other and against the other pins' vias: two stubs of different nets closer than track + clearance,
    or a via closer than via radius + clearance + half a track to another pin's stub, cannot both be built.  The later pin loses its slot and is
    reported sealed ('stub of REF.PAD')."""
    stubs = []
    for kind, key, q, part, layers, cx, cy in needs:
        rec = plan.pins.get(key)
        if rec and rec.get("kind") == "via" and not q["tht"]:
            rc = prect(q)
            v = rec["via"]
            stubs.append((key, rec["net"], (min(max(v[0], rc[0]), rc[2]), min(max(v[1], rc[1]), rc[3])), tuple(v), frozenset(layers)))
    stubs.sort(key=lambda t: t[0])
    need = pr["track_mm"] + pr["clearance_mm"]
    vr = pr["via_mm"] / 2 + pr["clearance_mm"] + pr["track_mm"] / 2
    dead = set()
    for i in range(len(stubs)):
        ki, ni, ai, bi, li = stubs[i]
        if ki in dead:
            continue
        for j in range(i + 1, len(stubs)):
            kj, nj, aj, bj, lj = stubs[j]
            if kj in dead or ni == nj or not (li & lj):
                continue
            if abs(bi[0] - bj[0]) > 4 or abs(bi[1] - bj[1]) > 4:
                continue
            if _seg_seg_gap(ai, bi, aj, bj) < need - 1e-9 or RD_seg_dist(bj, ai, bi) < vr - 1e-9 or RD_seg_dist(bi, aj, bj) < vr - 1e-9:
                dead.add(kj)
    for k in dead:
        rec = plan.pins[k]
        for ij, kk in list(plan.used.items()):
            if kk == k:
                del plan.used[ij]
        rec.update(kind="sealed", blocker="stub conflict", cands=0)
        rec.pop("via", None)


def rebuild_findings(B, pr, plan, ignore_routing=True):
    """Regenerate plan.findings from plan.pins (after an incremental update)."""
    ob = Obstacles(B, pr, ignore_routing)
    plan.findings = []
    fine = {r for r, p in B["parts"].items() if is_fine(p, pr)}
    _findings(B, pr, plan, B["parts"], ob, fine)
    plan.findings = plan.extra + plan.findings
    return plan.findings


def _suggest(B, pr, rec, part, ob):
    """Which part to move, which way, how far, so that the pin gets its lane back."""
    q = next(p for p in part["pads"] if p["num"] == rec["pad"])
    cx = sum(p["x"] for p in part["pads"]) / len(part["pads"])
    cy = sum(p["y"] for p in part["pads"]) / len(part["pads"])
    best = None
    for d in exit_dirs(part, q, cx, cy):
        layers = frozenset(q["layers"]) if len(q["layers"]) < 3 else frozenset({part["side"]})
        f, why = lane_free(ob, B, pr, q, d, layers, pr["lane_len_mm"])
        other = bool(why) and not why.startswith(("keep-out", "board")) and why.split(".")[0] != rec["ref"]
        # prefer a lane another part blocks (that part can move) over one the pin's own part closes
        if best is None or (other, f) > (best[3], best[0]):
            best = (f, why, d, other)
    f, why, d, _o = best
    need = pr["lane_len_mm"] - f
    blocker = (why or "").split(".")[0] if why and not why.startswith(("keep-out", "board")) else None
    if blocker and blocker in B["parts"] and blocker != rec["ref"]:
        return (f"move {blocker} {_dirword(d)} by >= {need:.2f} mm "
                f"(its pad blocks the {_dirword(d, True)} lane of {rec['ref']}.{rec['pad']} at {f:.2f} mm), or rotate "
                f"{rec['ref']}")
    if why:
        return f"clear {why}: it closes the {_dirword(d, True)} lane of {rec['ref']}.{rec['pad']} at {f:.2f} mm"
    return f"free the area around {rec['ref']}.{rec['pad']}: no lane, no via site within {pr['reach_mm']:g} mm"


def _dirword(d, side=False):
    n = {(1, 0): "east", (-1, 0): "west", (0, 1): "south", (0, -1): "north"}[(d[0], d[1])]
    if side:
        return n
    return {"east": "east (+x)", "west": "west (-x)", "south": "south (+y)", "north": "north (-y)"}[n]


def _findings(B, pr, plan, parts, ob, fine_refs):
    by_part = defaultdict(list)
    for k, rec in plan.pins.items():
        if rec.get("kind") in ("sealed", "no_site"):
            by_part[(rec["kind"], rec["ref"])].append(rec)
    for (kind, ref), recs in sorted(by_part.items()):
        for rec in sorted(recs, key=lambda r: (r["pad"].zfill(4))):
            part = parts[ref]
            if kind == "sealed":
                plan.findings.append({
                    "code": "PLAN_PIN_SEALED", "part": ref, "pin": rec["pad"], "net": rec["net"],
                    "at": [rec["x"], rec["y"]], "blocker": rec.get("blocker"),
                    "msg": f"{ref}.{rec['pad']} ({rec['net']}) has no free {pr['lane_len_mm']:g} mm lane "
                           f"(best {rec.get('lane', 0):.2f} mm, blocked by {rec.get('blocker') or 'the part itself'}) "
                           f"and no free via site within {pr['reach_mm']:g} mm ({rec.get('cands', 0)} candidates taken)",
                    "suggestion": _suggest(B, pr, rec, part, ob)})
            else:
                plan.findings.append({
                    "code": "PLAN_NO_VIA_SITE", "part": ref, "pin": rec["pad"], "net": rec["net"],
                    "at": [rec["x"], rec["y"]],
                    "msg": f"{ref}.{rec['pad']} ({rec['net']}, plane net) has no free via site within "
                           f"{pr['plane_radius_mm']:g} mm ({rec.get('cands', 0)} candidates, all taken or blocked)",
                    "suggestion": f"open a via site beside {ref}.{rec['pad']}: move a neighbouring part >= "
                                  f"{pr['slot_mm']:g} mm clear of the pad, or give the net a pour on the pad's layer"})


def _headroom(B, pr, plan, parts, ob, fine_refs, site_ok, phase):
    """Per fine-pitch part: pins by outcome and the number of free via sites left on each compass side."""
    for ref in sorted(fine_refs):
        part = parts[ref]
        pp = [q for q in part["pads"] if not q.get("np")]
        if not pp:
            continue
        x0 = min(q["x"] - q["ex"] for q in pp) - pr["reach_mm"]
        y0 = min(q["y"] - q["ey"] for q in pp) - pr["reach_mm"]
        x1 = max(q["x"] + q["ex"] for q in pp) + pr["reach_mm"]
        y1 = max(q["y"] + q["ey"] for q in pp) + pr["reach_mm"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        free = defaultdict(int)
        for ij, (x, y) in hexsites(phase, x0, y0, x1, y1, pr["slot_mm"]):
            if ij in plan.used:
                continue
            gaps = min(_rect_gap_pt(prect(q), x, y) for q in pp)
            if gaps > pr["reach_mm"] or not site_ok(phase, ij, (x, y))[0]:
                continue
            if site_ok(phase, ij, (x, y))[1]:
                continue
            dx, dy = x - cx, y - cy
            side = ("E" if dx > 0 else "W") if abs(dx) >= abs(dy) else ("S" if dy > 0 else "N")
            free[side] += 1
        cnt = defaultdict(int)
        for (r_, n_), rec in plan.pins.items():
            if r_ == ref:
                cnt[rec.get("kind", "?")] += 1
        plan.headroom[ref] = {"pins": dict(cnt), "free_slots": dict(sorted(free.items())),
                              "free_total": sum(free.values())}
