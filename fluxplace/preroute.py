"""Pre-route gate: is this placement worth routing? Pure text (routediag.load).

Each check yields findings {level, code, msg, refs, value, limit} with the number
and the threshold in the message. `[review] waive = ["CODE:REGEX"]` waives them
exactly like review findings (same matcher).

  NET_OVER_BUDGET      FAIL  MST length of a net > its class budget (netclass.py)
  SUPPORT_PART_FAR     FAIL  confidently-owned support part beyond its limit (clusters.py)
  ANCHOR_MISORIENTED   FAIL  an anchor > 15 % worse than its best rotation (anchors.py)
  PIN_ESCAPE_BLOCKED   FAIL  fine-pitch pin with a foreign obstacle in its outward
                             lane within track + clearance
  KEEPOUT_TRAPPED      FAIL  pad inside a track-forbidding rule area
  CONGESTION           FAIL  coarse global route: demand > capacity (2 mm cells)
  RATSNEST_CROSSINGS   WARN  crossings between MST edges of different signal nets
  PLACE_ESCAPE_UNRESERVED FAIL  a power pin / a passive crowded by a controlled pair cannot own a via site + lane (escres.py)
  PLACEMENT_ILLEGAL    FAIL  same-side courtyard overlap, part outside the outline (edge connectors
                             may overhang by their mating side), part in a forbidden region, through-hole
                             pad in a no-through-hole region (legality.py)

    [preroute]
    cell_mm = 2.0            # congestion grid
    utilisation = 0.7        # usable fraction of a cell's track slots
    max_overflow = 0         # tolerated total overflow (track-cells)
    signal_layers = 4        # count, or a list of names ["F.Cu", "In2.Cu", ...]; or plane_layers = [...]
    anchor_tolerance_pct = 15
"""
import math
from collections import defaultdict

from . import anchors as AN
from . import clusters as CL
from . import geom as GM
from . import netclass as NC
from . import legality as LGL
from . import placechecks as PCK

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"
CODES = ["NET_OVER_BUDGET", "SUPPORT_PART_FAR", "ANCHOR_MISORIENTED", "PIN_ESCAPE_BLOCKED",
         "KEEPOUT_TRAPPED", "CONGESTION", "RATSNEST_CROSSINGS", "ANCHOR_SPACING",
         "CONNECTOR_BLOCKED", "HEIGHT_REGION", "STALE_COPPER", "SIDE_POLICY", "PLACEMENT_ILLEGAL", "PLACE_ESCAPE_UNRESERVED"]


def _f(level, code, msg, refs=(), value=None, limit=None, cls=None):
    return {"level": level, "code": code, "msg": msg, "refs": sorted(set(refs)),
            "value": value, "limit": limit, "cls": cls}


HARD = ("rf", "switch_node", "bypass", "pair")      # budgets a move may not break


def explicit_fixed(B, cons):
    """Parts nobody may move: locked, [lock.at], [placement] fixed."""
    return ({r for r, p in B["parts"].items() if p.get("locked")}
            | set(((cons or {}).get("lock") or {}).get("at", {}))
            | set(((cons or {}).get("placement") or {}).get("fixed", [])))


# ------------------------------------------------------------------ checks
def fixed_parts(B, cons):
    """Parts the placer will not move: locked, [lock.at], [placement] fixed, mounting hardware, and
    the detected anchors (connectors, modules, rotating groups)."""
    import re
    mech = ("MH", "MK", "FID", "TP", "H")
    fx = set(explicit_fixed(B, cons))
    fx |= {r for r in B["parts"] if r.rstrip("0123456789") in mech and r[len(r.rstrip("0123456789")):].isdigit()}
    fx |= {r for a in AN.detect(B, cons) for r in a["refs"]}
    return fx


def net_over_budget(B, cls, cons=None):
    """FAIL when a net is over budget and moving parts could fix it. ANCHOR_SPACING (WARN) when the
    fixed parts alone already break the budget: the floor is the MST over the pads of fixed parts
    (a sub-tree of the net that no placement move can shorten), so a multi-pad net with only some
    pads fixed is judged on that sub-tree. A net whose length exceeds both its budget and 1.15 x
    its floor + 1 mm still FAILs for the excess the movable parts add."""
    out = []
    fixed = fixed_parts(B, cons)
    pads = NC.net_pads(B)
    for net, info in sorted(cls.items()):
        lim = NC.effective_budget(info)
        if lim is None:
            continue
        ln = NC.mst_length([NC.pad_xy(p) for p in pads[net]])
        if ln <= lim:
            continue
        ends = sorted({p["ref"] for p in pads[net]})
        pr = f" (pair with {info['partner']})" if info["partner"] else ""
        why = f"; override: {info['override_reason']}" if info.get("override_reason") else ""
        fx = [p for p in pads[net] if p["ref"] in fixed]
        floor = NC.mst_length([NC.pad_xy(p) for p in fx]) if len(fx) >= 2 else 0.0
        if floor > lim:
            fr = sorted({p["ref"] for p in fx})
            out.append(_f(WARN, "ANCHOR_SPACING",
                          f"{net} [{info['class']}]{pr}: fixed {', '.join(fr[:4])} are {floor:.1f} mm apart "
                          f"(minimum achievable {floor:.1f} mm, budget {lim:g} mm, now {ln:.1f} mm); "
                          f"move an anchor or raise the budget{why}", refs=fr, value=round(floor, 2),
                          limit=lim, cls=info["class"]))
            if ln <= 1.15 * floor + 1.0:
                continue
            lim = floor                      # the rest of the excess is placement's
        out.append(_f(FAIL, "NET_OVER_BUDGET",
                      f"{net} [{info['class']}]{pr}: {ln:.1f} mm, budget {lim:g} mm"
                      f"{f' (the fixed parts alone need {floor:.1f})' if floor > 0 and lim == floor else ''} "
                      f"({' - '.join(ends[:4])}{'...' if len(ends) > 4 else ''}){why}",
                      refs=ends, value=round(ln, 2), limit=lim, cls=info["class"]))
    return out


def budget_overrides(B, cls):
    """[(net, mm, reason, length mm, ok)] for every net with a [placement.budget."NET"] or
    [placement.net.NET] budget override, so the reasons are always printed."""
    pads = NC.net_pads(B)
    rows = []
    for net, info in sorted(cls.items()):
        if info.get("override") and net in pads:
            ln = NC.mst_length([NC.pad_xy(p) for p in pads[net]])
            lim = NC.effective_budget(info)
            rows.append((net, info["budget"], info.get("override_reason", ""), ln, lim is None or ln <= lim))
    return rows


def support_far(B, cons, spec):
    cl, _ = CL.build(B, cons, spec)
    return [_f(FAIL, "SUPPORT_PART_FAR",
               f"{s['ref']} is {s['dist']:.1f} mm from {ic} pin {s['pad']} "
               f"({s['net']}, {s['cls']}; limit {s['limit']:g} mm)",
               refs=[s["ref"], ic], value=round(s["dist"], 2), limit=s["limit"])
            for ic, s in sorted(CL.far_parts(cl), key=lambda x: -x[1]["dist"])]


def keepout_trapped(B):
    by = defaultdict(list)
    for p in B["pads"]:
        part = B["parts"][p["ref"]]
        if p["tht"] and not p["layers"]:
            continue
        for z in B["zones"]:
            if (z["keepout"] and z["no_tracks"] and z["pts"] and p["layers"] <= z["layers"]
                    and GM.poly_has(z["pts"], p["x"], p["y"])
                    and p["ref"].upper() not in z["name"].upper()):   # the zone's own antenna
                by[(p["ref"], z["name"])].append(p["num"])
                break
    return [_f(FAIL, "KEEPOUT_TRAPPED",
               f"{ref} pad(s) {','.join(nums)} inside track-forbidding rule area "
               f"'{name}': no copper can reach them", refs=[ref])
            for (ref, name), nums in sorted(by.items())]


def _lane(p, part, L, w):
    """Outward lane rectangle of pad p: from its outer edge, `L` long, `w` wide."""
    cx = sum(q["x"] for q in part["pads"]) / len(part["pads"])
    cy = sum(q["y"] for q in part["pads"]) / len(part["pads"])
    dx, dy = p["x"] - cx, p["y"] - cy
    horiz = (p["ex"] > p["ey"]) if abs(p["ex"] - p["ey"]) > 0.25 * max(p["ex"], p["ey"]) \
        else abs(dx) >= abs(dy)
    if horiz:
        if dx >= 0:
            return (p["x"] + p["ex"], p["y"] - w / 2, p["x"] + p["ex"] + L, p["y"] + w / 2)
        return (p["x"] - p["ex"] - L, p["y"] - w / 2, p["x"] - p["ex"], p["y"] + w / 2)
    if dy >= 0:
        return (p["x"] - w / 2, p["y"] + p["ey"], p["x"] + w / 2, p["y"] + p["ey"] + L)
    return (p["x"] - w / 2, p["y"] - p["ey"] - L, p["x"] + w / 2, p["y"] - p["ey"])


def pin_escape_blocked(B):
    """A fine-pitch (<= 0.5 mm) IC pad whose outward lane (track wide, track +
    clearance long) contains another net's pad, another part's courtyard, a
    track-forbidding rule area or existing copper of another net."""
    L, w = B["track"] + B["clearance"], B["track"]
    pads = GM.Hash(2.0)
    for i, q in enumerate(B["pads"]):
        pads.add(i, (q["x"] - q["ex"], q["y"] - q["ey"], q["x"] + q["ex"], q["y"] + q["ey"]),
                 "x")
    segs = GM.Hash(2.0)
    for i, s in enumerate(B["segs"]):
        h = s["w"] / 2
        segs.add(i, (min(s["a"][0], s["b"][0]) - h, min(s["a"][1], s["b"][1]) - h,
                     max(s["a"][0], s["b"][0]) + h, max(s["a"][1], s["b"][1]) + h), "x")
    courts = GM.Hash(5.0)
    for r, part in B["parts"].items():
        courts.add(r, part["court"], "x")
    zones = [z for z in B["zones"] if z["keepout"] and z["no_tracks"] and z["pts"]]
    out = []
    for ref, part in sorted(B["parts"].items()):
        if not NC.is_ic(ref, part, 3) or part["n_pads"] < 5 or CL._pitch(part) > 0.5:
            continue
        hits = defaultdict(list)
        for p in part["pads"]:
            if not p["net"] or p["w"] * p["h"] > 1.5:
                continue
            lane = _lane(p, part, L, w)
            why = None
            for k in pads.hits(lane, "x"):
                q = B["pads"][k]
                if q["ref"] == ref and q["num"] == p["num"]:
                    continue
                if q["net"] != p["net"] and q["layers"] & p["layers"]:
                    why = f"pad {q['ref']}.{q['num']}"
                    break
            if not why:
                for k in segs.hits(lane, "x"):
                    s = B["segs"][k]
                    if s["net"] != p["net"] and s["layer"] in p["layers"] and _seg_in(lane, s):
                        why = f"copper of {s['net']}"
                        break
            if not why:
                for k in courts.hits(lane, "x"):
                    if k != ref and B["parts"][k]["side"] == part["side"]:
                        why = f"courtyard of {k}"
                        break
            if not why and zones and any(p["layers"] <= z["layers"] and GM.poly_has(
                    z["pts"], (lane[0] + lane[2]) / 2, (lane[1] + lane[3]) / 2) for z in zones):
                why = "keep-out"
            if why:
                hits[why].append(p["num"])
        if hits:
            n = sum(len(v) for v in hits.values())
            top = sorted(hits.items(), key=lambda kv: -len(kv[1]))[:3]
            out.append(_f(FAIL, "PIN_ESCAPE_BLOCKED",
                          f"{ref}: {n} fine-pitch pin(s) blocked within {L:.2f} mm "
                          f"(track {B['track']:g} + clearance {B['clearance']:g}): "
                          + "; ".join(f"{k} (pin {','.join(v[:4])})" for k, v in top),
                          refs=[ref], value=n, limit=0))
    return out


def _seg_in(r, s):
    """Does a track segment (with its width) touch rectangle r?"""
    (ax, ay), (bx, by), h = s["a"], s["b"], s["w"] / 2
    n = max(1, int(math.hypot(bx - ax, by - ay) / 0.05))
    for i in range(n + 1):
        t = i / n
        x, y = ax + (bx - ax) * t, ay + (by - ay) * t
        if r[0] - h <= x <= r[2] + h and r[1] - h <= y <= r[3] + h:
            return True
    return False


# ------------------------------------------------------------------ ratsnest
def _mst_edges(pts):
    n = len(pts)
    if n < 2:
        return []
    d = [math.inf] * n
    par = [0] * n
    d[0] = 0
    done, edges = [False] * n, []
    for _ in range(n):
        i = min((k for k in range(n) if not done[k]), key=lambda k: d[k])
        done[i] = True
        if i:
            edges.append((pts[par[i]], pts[i]))
        for k in range(n):
            if not done[k]:
                dd = math.hypot(pts[i][0] - pts[k][0], pts[i][1] - pts[k][1])
                if dd < d[k]:
                    d[k], par[k] = dd, i
    return edges


def routed_edges(B, cls):
    """[(net, a, b)] MST edges of every non-rail net (the ratsnest)."""
    out = []
    for net, ps in NC.net_pads(B).items():
        info = cls.get(net)
        if not info or info["class"] == "rail" or len(ps) < 2:
            continue
        for a, b in _mst_edges([NC.pad_xy(p) for p in ps]):
            out.append((net, a, b))
    return out


def _ccw(a, b, c):
    return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])


def _cross(e, f):
    a, b, c, d = e[1], e[2], f[1], f[2]
    if a == c or a == d or b == c or b == d:
        return False
    return (_ccw(a, b, c) * _ccw(a, b, d) < 0) and (_ccw(c, d, a) * _ccw(c, d, b) < 0)


def ratsnest_crossings(edges):
    h = GM.Hash(8.0)
    for i, (n, a, b) in enumerate(edges):
        h.add(i, (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), "x")
    cnt = 0
    for i, e in enumerate(edges):
        r = (min(e[1][0], e[2][0]), min(e[1][1], e[2][1]), max(e[1][0], e[2][0]), max(e[1][1], e[2][1]))
        for j in h.hits(r, "x"):
            if j > i and edges[j][0] != e[0] and _cross(e, edges[j]):
                cnt += 1
    return cnt


# ------------------------------------------------------------------ congestion
def layer_split(B, cons=None):
    """(signal layers, plane layers) as counted by the congestion model. Override with
    `[preroute] signal_layers = ["F.Cu", ...]` (names) or `plane_layers = [...]`.
    Default: an INNER layer is a plane when a pour zone with a net covers >= 60 % of
    the outline there (a pour on an outer layer is routable copper, not a plane)."""
    pc = (cons or {}).get("preroute", {})
    cu = list(B["copper"])
    if isinstance(pc.get("signal_layers"), list):
        sig = [l for l in cu if l in pc["signal_layers"]]
        return sig, [l for l in cu if l not in sig]
    if isinstance(pc.get("plane_layers"), list):
        return [l for l in cu if l not in pc["plane_layers"]], [l for l in cu if l in pc["plane_layers"]]
    x0, y0, x1, y1 = B["bbox"]
    board = max((x1 - x0) * (y1 - y0), 1e-6)
    plane = set()
    for z in B["zones"]:
        if z["net"] and not z["keepout"] and z["pts"]:
            xs, ys = [q[0] for q in z["pts"]], [q[1] for q in z["pts"]]
            if (max(xs) - min(xs)) * (max(ys) - min(ys)) >= 0.6 * board:
                plane |= {l for l in z["layers"] if l in cu[1:-1]}
    return [l for l in cu if l not in plane], [l for l in cu if l in plane]


def signal_layer_count(B, cons=None):
    pc = (cons or {}).get("preroute", {})
    if isinstance(pc.get("signal_layers"), int):
        return int(pc["signal_layers"])
    return max(2, len(layer_split(B, cons)[0]))


def congestion(B, cls, cons=None):
    """Coarse global route: each non-rail net's MST as L-shapes (the less loaded
    orientation) on a cell grid; capacity per cell = signal layers * track slots,
    half per direction, derated by pads and rule areas and by `utilisation`."""
    pc = (cons or {}).get("preroute", {})
    cell = float(pc.get("cell_mm", 2.0))
    util = float(pc.get("utilisation", 0.7))
    nl = signal_layer_count(B, cons)
    x0, y0, x1, y1 = B["bbox"]
    nx, ny = int((x1 - x0) / cell) + 1, int((y1 - y0) / cell) + 1
    slots = cell / (B["track"] + B["clearance"])
    blocked = defaultdict(float)             # (i, j) -> blocked fraction summed over layers
    per_layer = nl
    for p in B["pads"]:
        area = max(0.0, 4 * p["ex"] * p["ey"])
        i, j = int((p["x"] - x0) / cell), int((p["y"] - y0) / cell)
        frac = area / (cell * cell)
        blocked[(i, j)] += frac * (per_layer if p["tht"] else 1.0)
    for z in B["zones"]:
        if z["keepout"] and z["no_tracks"] and z["pts"]:
            for i in range(nx):
                for j in range(ny):
                    if GM.poly_has(z["pts"], x0 + (i + .5) * cell, y0 + (j + .5) * cell):
                        blocked[(i, j)] += per_layer
    cap = {}
    for i in range(nx):
        for j in range(ny):
            c = max(0.0, (per_layer - min(per_layer, blocked[(i, j)])) * slots * util / 2.0)
            cap[(i, j)] = c
    H, V = defaultdict(float), defaultdict(float)

    def cells(a, b, hfirst):
        ai, aj = int((a[0] - x0) / cell), int((a[1] - y0) / cell)
        bi, bj = int((b[0] - x0) / cell), int((b[1] - y0) / cell)
        mid = (bi, aj) if hfirst else (ai, bj)
        hs, vs = [], []
        for (p, q) in (((ai, aj), mid), (mid, (bi, bj))):
            if p[1] == q[1]:
                hs += [(i, p[1]) for i in range(min(p[0], q[0]), max(p[0], q[0]) + 1)]
            else:
                vs += [(p[0], j) for j in range(min(p[1], q[1]), max(p[1], q[1]) + 1)]
        return hs, vs

    edges = routed_edges(B, cls)
    for net, a, b in sorted(edges, key=lambda e: -math.hypot(e[1][0] - e[2][0], e[1][1] - e[2][1])):
        best = None
        for hf in (True, False):
            hs, vs = cells(a, b, hf)
            worst = max([H[c] / max(cap[c], .01) for c in hs] + [V[c] / max(cap[c], .01) for c in vs] + [0])
            if best is None or worst < best[0]:
                best = (worst, hs, vs)
        for c in best[1]:
            H[c] += 1
        for c in best[2]:
            V[c] += 1
    over = {}
    for c in cap:
        o = max(0.0, H[c] - cap[c]) + max(0.0, V[c] - cap[c])
        if o > 0:
            over[c] = o
    return {"cells": len(cap), "cell_mm": cell, "layers": nl,
            "layer_names": layer_split(B, cons)[0] if not isinstance(pc.get("signal_layers"), int) else [], "overflow_cells": len(over),
            "overflow": round(sum(over.values()), 1), "demand": len(edges),
            "worst": sorted(((round(o, 1), round(x0 + (i + .5) * cell, 1), round(y0 + (j + .5) * cell, 1))
                             for (i, j), o in over.items()), reverse=True)[:10]}


def congestion_findings(rep, cons=None):
    lim = float((cons or {}).get("preroute", {}).get("max_overflow", 0))
    if rep["overflow"] <= lim:
        return []
    w = rep["worst"][0] if rep["worst"] else None
    return [_f(FAIL, "CONGESTION",
               f"coarse route over capacity: {rep['overflow_cells']} of {rep['cells']} cells "
               f"({rep['cell_mm']:g} mm cells, {rep['layers']} signal layers: {', '.join(rep.get('layer_names') or [])}), overflow {rep['overflow']:g} "
               f"track-cells > {lim:g}; worst {w[0]:g} at ({w[1]}, {w[2]})" if w else "over capacity",
               value=rep["overflow"], limit=lim)]


# ------------------------------------------------------------------ gate
def use_plan(cons):
    """`[route_ready] use_plan` (default FALSE: placement-only calibration of the plan is 10-41 %): the gate also runs plan-route
    (planroute.py).  Its findings are WARN unless `[route_ready] plan_gate = "fail"` (then the plan verdict predicate blocks).  Only the gate
    entry points ask for it (plan=True); optimisers that call run() per move do not."""
    v = ((cons or {}).get("route_ready") or {}).get("use_plan", False)
    return bool(v)


def plan_gate_fail(cons):
    return str(((cons or {}).get("route_ready") or {}).get("plan_gate", "warn")).lower() == "fail"


def run(B, cons=None, spec=None, waivers=(), anchors=True, plan=False):
    """-> (findings, scorecard). Findings already have waivers applied.  plan=True adds the PLAN_* findings
    (escape planner + global router, copper of the board respected) and card["plan"]."""
    from . import review as R
    cons = cons or {}
    cls = NC.classify(B, cons, spec)
    f = []
    f += net_over_budget(B, cls, cons)
    f += support_far(B, cons, spec)
    if anchors:
        f += AN.findings(B, cons, spec, cls)
    f += pin_escape_blocked(B)
    f += keepout_trapped(B)
    f += PCK.connector_blocked(B, cons)
    f += PCK.height_regions(B, cons)
    f += PCK.stale_copper(B)
    f += PCK.side_policy(B, cons, spec)
    f += LGL.findings(B, cons)
    plan_v = None
    if plan:
        from . import planroute as PL
        PRES = PL.run(B, cons, spec, ignore_routing=False, cls=cls)
        plan_v = PL.verdict(PRES)
        for x in PL.rank(PRES["findings"]):
            lvl = FAIL if (plan_gate_fail(cons) and PL.is_hard_failure(x, PRES["pr"])) else WARN
            f.append(_f(lvl, x["code"],
                        x["msg"] + (f" -> {x['suggestion']}" if x.get("suggestion") else ""),
                        refs=[x["part"]] if x.get("part") else x.get("parts", []), value=x.get("value", x.get("overflow")),
                        limit=x.get("limit", 0)))
    try:
        from . import escres as ER
        f += ER.findings(B, cons, spec, cls)
    except Exception as e:           # a gate that cannot run says so
        f.append(_f(WARN, "PLACE_ESCAPE_UNRESERVED", f"escape reservation could not run: {e}"))
    cg = congestion(B, cls, cons)
    f += congestion_findings(cg, cons)
    edges = routed_edges(B, cls)
    xc = ratsnest_crossings(edges)
    f.append(_f(WARN, "RATSNEST_CROSSINGS", f"{xc} crossing(s) between ratsnest edges of different nets",
                value=xc))
    waivers = list(waivers) + list((cons.get("review") or {}).get("waive", []))
    f = [x for x in f if not R._waived(x, waivers)]
    pads = NC.net_pads(B)
    weighted = sum(info["weight"] * NC.mst_length([NC.pad_xy(p) for p in pads[n]])
                   for n, info in cls.items())
    card = {"weighted_ratsnest_mm": round(weighted, 1), "congestion": cg, "crossings": xc,
            "counts": {c: sum(1 for x in f if x["code"] == c) for c in CODES},
            "fail": sum(1 for x in f if x["level"] == FAIL),
            "warn": sum(1 for x in f if x["level"] == WARN)}
    if plan_v is not None:
        card["plan"] = plan_v
        for k, c in plan_v["counts"].items():
            card["counts"][k] = c
    card["overrides"] = [{"net": n, "mm": mm, "reason": r, "length": round(ln, 1), "ok": ok}
                         for n, mm, r, ln, ok in budget_overrides(B, cls)]
    card["block_mm"] = round(PCK.block_mm(B, spec, cons), 1)
    card["score"] = score(f, card)
    return f, card


# ---- the ONE objective: every optimiser minimises this, the gate prints it --------------
W_HARD, W_SOFT, W_SUPPORT, W_STRUCT, W_CONG, W_XING, W_ANCHOR, W_BLOCK = 20.0, 3.0, 5.0, 500.0, 25.0, 0.05, 300.0, 0.5


def score(findings, card):
    """score = class-weighted ratsnest mm
             + 20  x mm over budget of rf/switch_node/bypass/pair nets      (HARD)
             +  3  x mm over budget of other nets                           (soft)
             +  5  x mm support parts sit beyond their limit
             + 500 x (PIN_ESCAPE_BLOCKED + KEEPOUT_TRAPPED + CONNECTOR_BLOCKED + HEIGHT_REGION FAILs + each PLACEMENT_ILLEGAL violation)
             + 25  x congestion overflow (track-cells)
             + 0.05 x ratsnest crossings
             + 300 x ANCHOR_MISORIENTED
             + 0.5 x block bounding-box half-perimeters (mm), only when blocks are defined
    Lower is better. Waived findings do not count (the gate has already dropped them)."""
    t = card["weighted_ratsnest_mm"]
    for x in findings:
        c = x["code"]
        over = max(0.0, (x["value"] or 0) - (x["limit"] or 0))
        if c == "NET_OVER_BUDGET":
            t += (W_HARD if x.get("cls") in HARD else W_SOFT) * over
        elif c == "SUPPORT_PART_FAR":
            t += W_SUPPORT * over
        elif c == "PLACEMENT_ILLEGAL":
            t += W_STRUCT * (x["value"] or 1)
        elif c in ("PIN_ESCAPE_BLOCKED", "KEEPOUT_TRAPPED", "CONNECTOR_BLOCKED") or (
                c == "HEIGHT_REGION" and x["level"] == FAIL):
            t += W_STRUCT
        elif c == "ANCHOR_MISORIENTED":
            t += W_ANCHOR
    t += W_CONG * card["congestion"]["overflow"] + W_XING * card["crossings"] + W_BLOCK * card.get("block_mm", 0.0)
    return round(t, 1)


def worst(findings, n=10, per_code=4):
    """The n worst FAILs by overshoot (value / limit), at most `per_code` per code."""
    def key(x):
        if x["value"] is not None and x["limit"]:
            return -(x["value"] / x["limit"])
        return -1e9 if x["code"] == "CONGESTION" else -(x["value"] or 0)
    seen, out = defaultdict(int), []
    for x in sorted([x for x in findings if x["level"] == FAIL], key=key):
        if seen[x["code"]] < per_code:
            seen[x["code"]] += 1
            out.append(x)
    return out[:n]


def format_text(findings, card, title=""):
    L = [f"ROUTE-READY {title}".rstrip(),
         f"  score (single objective, lower=better) : {card['score']:.0f}",
         f"  class-weighted ratsnest length : {card['weighted_ratsnest_mm']:.0f} mm",
         f"  ratsnest crossings             : {card['crossings']}",
         f"  congestion                     : overflow {card['congestion']['overflow']:g} in "
         f"{card['congestion']['overflow_cells']} cell(s) ({card['congestion']['layers']} signal layers: "
         f"{', '.join(card['congestion'].get('layer_names') or [])})"]
    for c in CODES:
        L.append(f"  {c:20s} {card['counts'][c]}")
    if card.get("plan"):
        p = card["plan"]
        L.append(f"  plan-route                     : PLAN_SCORE {p['score']:g} {p['result']}  "
                 + ", ".join(f"{k} {v}" for k, v in sorted(p["counts"].items())))
    if card.get("overrides"):
        L.append("  budget overrides in effect:")
        for o in card["overrides"]:
            L.append(f"    {o['net']:18s} {('none' if o['mm'] is None else format(o['mm'], 'g') + ' mm'):>8s}  now {o['length']:.1f} mm "
                     f"{'ok' if o['ok'] else 'OVER'}  - {o['reason'] or '(no reason given)'}")
    L.append("  worst 10:")
    for x in worst(findings):
        L.append(f"    {x['code']:19s} {x['msg']}")
    L.append(f"  RESULT: {'NOT READY' if card['fail'] else 'ready'} ({card['fail']} FAIL, {card['warn']} WARN)")
    return "\n".join(L)
