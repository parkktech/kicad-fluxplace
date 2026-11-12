"""Engineering-rule findings on a planned board (called by planroute.run):

  PLAN_PART_IN_CORRIDOR  a part sits in a reserved bus corridor (>= 3 connected cells whose layers are >= 80 % used) and takes
                         capacity there: move it out so the corridor keeps its capacity
  PLAN_RETURN_PATH       a planned path of an rf / pair / clock net changes layer between two layers that reference different planes
                         (or leaves its reference) with no GND via or GND pour pad within 3 mm of the layer change
  PLAN_ZONING            a switching-regulator hot loop part (switch-node net) closer than `[zoning] keepaway_mm` (5) to an RF or audio
                         part; `[zoning] hot = [refs]`, `sensitive = [refs]` add parts
"""
import fnmatch
import math
from collections import defaultdict

from . import geom as GM
from . import netclass as NC

HS = ("rf", "pair", "clock")


def corridor_cells(gr, util=0.8, min_run=3):
    """Reserved corridor cells: cell indices whose summed H+V use of some layer is >= util x capacity, in 4-connected groups >= min_run."""
    hot = set()
    for l in range(gr.nl):
        for c in range(gr.N):
            cap = gr.capH[l][c] + gr.capV[l][c]
            use = gr.uH[l][c] + gr.uV[l][c]
            if cap >= 4 and use >= util * cap and use >= 3:
                hot.add(c)
    keep, seen = set(), set()
    for c in sorted(hot):
        if c in seen:
            continue
        grp, st = [], [c]
        seen.add(c)
        while st:
            u = st.pop()
            grp.append(u)
            i, j = u % gr.nx, u // gr.nx
            for v in ((u + 1) if i + 1 < gr.nx else -1, (u - 1) if i else -1, u + gr.nx, u - gr.nx):
                if v in hot and v not in seen:
                    seen.add(v)
                    st.append(v)
        if len(grp) >= min_run:
            keep |= set(grp)
    return keep


def part_in_corridor(gr, B, fixed=()):
    cc = corridor_cells(gr)
    out = []
    if not cc:
        return out
    for ref, p in sorted(B["parts"].items()):
        if ref.startswith(("MH", "MK", "FID", "TP")) or p["n_pads"] > 6:
            continue
        hit = [c for c in gr._cells_of_rect(p["court"]) if c in cc]
        if not hit:
            continue
        # does the part cost capacity there? (its pads sit on a layer of a corridor cell)
        area = sum(4 * q["ex"] * q["ey"] for q in p["pads"])
        if area < 0.3 * gr.cell:
            continue
        x, y = gr.xy_of(hit[0])
        out.append({"code": "PLAN_PART_IN_CORRIDOR", "part": ref, "pin": p["pads"][0]["num"], "net": p["pads"][0]["net"], "value": len(hit),
                    "limit": 0, "at": [round(x, 1), round(y, 1)], "cells_xy": [[round(gr.xy_of(c)[0], 1), round(gr.xy_of(c)[1], 1)] for c in hit[:8]],
                    "msg": f"{ref} sits in a reserved corridor ({len(hit)} cell(s) near ({x:.0f}, {y:.0f}), layers >= 80 % used)",
                    "suggestion": f"move {ref} out of the corridor so its {len(hit)} cell(s) keep their capacity"})
    return out[:40]


def return_path(gr, plan, B, cons):
    from . import escapeplan as EP
    cu = B["copper"]
    _sig, planes = EP.plan_layers(B, cons)
    planes = list(planes) + [l for pz in EP.planned_pours(cons) if pz["region"] == "board" for l in pz["layers"] if l not in planes]   # planned pours are references
    refs = {}
    for l in gr.layers:
        k = cu.index(l) if l in cu else 0
        near = sorted(planes, key=lambda pl: abs(cu.index(pl) - k)) if planes else []
        refs[l] = near[0] if near else None
    stitch = []
    for rec in plan.pins.values():
        if NC.is_ground(rec["net"]) and rec.get("kind") in ("via", "pour", "attached"):
            xy = rec.get("via") or [rec["x"], rec["y"]]
            stitch.append((xy[0], xy[1]))
    out = []
    N = gr.N
    for name, nt in gr.nets.items():
        if nt["cls"] not in HS:
            continue
        r = gr.routes.get(name)
        if not r:
            continue
        for cn in r["conns"]:
            p = cn.get("path")
            if not p:
                continue
            for a, b in zip(p, p[1:]):
                la, lb = a // N, b // N
                if la == lb or refs[gr.layers[la]] == refs[gr.layers[lb]] and refs[gr.layers[la]] is not None:
                    continue
                x, y = gr.xy_of(a % N)
                if any(math.hypot(x - sx, y - sy) <= 3.0 for sx, sy in stitch):
                    continue
                out.append({"code": "PLAN_RETURN_PATH", "net": name, "value": 1, "limit": 0, "at": [round(x, 1), round(y, 1)],
                            "msg": f"{name} ({nt['cls']}) changes {gr.layers[la]} -> {gr.layers[lb]} near ({x:.0f}, {y:.0f}): the reference "
                                   f"plane changes ({refs[gr.layers[la]]} -> {refs[gr.layers[lb]]}) and no GND via is within 3 mm",
                            "suggestion": f"put a GND stitching via beside the layer change of {name}, or keep it on one layer"})
                break
    return out[:30]


def zoning(B, cls, cons):
    z = (cons or {}).get("zoning") or {}
    keep = float(z.get("keepaway_mm", 5.0))
    P = B["parts"]
    pads = NC.net_pads(B)
    hot = {p["ref"] for n, i in cls.items() if i["class"] == "switch_node" for p in pads[n]}
    hot |= {r for r in P if any(fnmatch.fnmatch(r, g) for g in z.get("hot", []))}
    sens = {p["ref"] for n, i in cls.items() if i["class"] == "rf" for p in pads[n]}
    sens |= {p["ref"] for n, i in cls.items() if i["class"] == "pair" and i.get("kind") == "audio" for p in pads[n]}
    sens |= {r for r in P if any(fnmatch.fnmatch(r, g) for g in z.get("sensitive", []))}
    hot -= {r for r in hot if r.startswith(("MH", "MK", "FID", "TP"))}
    out = []
    for h in sorted(hot):
        for s in sorted(sens - {h}):
            if P[h]["side"] != P[s]["side"] and not (P[h]["tht"] or P[s]["tht"]):
                continue
            d = GM.rect_gap(P[h]["court"], P[s]["court"])
            if d < keep:
                out.append({"code": "PLAN_ZONING", "part": h, "pin": P[h]["pads"][0]["num"], "net": P[h]["pads"][0]["net"], "value": keep - d,
                            "limit": 0, "at": [round(P[h]["pos"][0], 1), round(P[h]["pos"][1], 1)], "other": s,
                            "msg": f"hot-loop part {h} is {d:.1f} mm from {s} (RF/audio), keep-away {keep:g} mm",
                            "suggestion": f"move {h} or {s} so they are >= {keep:g} mm apart"})
    return sorted(out, key=lambda f: -f["value"])[:30]


def overflow_causes(gr, B, pr):
    """Split the overflow (track units over capacity, summed over cell-layer-directions) by where it sits:
    courtyard  = the cell centre lies under a part's courtyard ON A LAYER THE PART'S PADS BLOCK (an SMD part blocks only its own side, a part with
                 through-hole pads blocks every layer): the pads cover the cell, the escape planner's business;
    fanout     = within 2 mm of a fine-pitch part's courtyard on any side/layer, and every inner-layer cell under an SMD part (real fan-out congestion);
    channel    = open channel between parts.   -> ({units, cells} per cause, {(layer id, cell): cause})."""
    from . import escapeplan as EP
    P = B["parts"]
    fine = [p["court"] for r, p in P.items() if EP.is_fine(p, pr) and not EP.is_mech(r)]
    allp = [(p["court"], p["side"], p["tht"]) for r, p in P.items() if not EP.is_mech(r) and p["n_pads"] >= 1]
    out = {"courtyard": 0.0, "fanout": 0.0, "channel": 0.0}
    cells = {"courtyard": 0, "fanout": 0, "channel": 0}
    kind = {}
    for l in range(gr.nl):
        lname = gr.layers[l]
        for c in range(gr.N):
            o = max(0.0, gr.uH[l][c] - gr.capH[l][c]) + max(0.0, gr.uV[l][c] - gr.capV[l][c])
            if o <= 0:
                continue
            x, y = gr.xy_of(c)
            under = [(sd, tht) for cb, sd, tht in allp if cb[0] <= x <= cb[2] and cb[1] <= y <= cb[3]]
            if any(tht or sd == lname for sd, tht in under):
                k = "courtyard"
            elif under or any(cb[0] - 2 <= x <= cb[2] + 2 and cb[1] - 2 <= y <= cb[3] + 2 for cb in fine):
                k = "fanout"
            else:
                k = "channel"
            out[k] += o
            cells[k] += 1
            kind[(l, c)] = k
    return {"units": {k: round(v, 1) for k, v in out.items()}, "cells": cells}, kind


def risk_nets(gr, kind):
    """Nets whose planned path crosses an over-capacity cell-direction that is NOT inside a part courtyard."""
    risk = set()
    for n, r in gr.routes.items():
        for (l, c, d) in r["keys"]:
            if kind.get((l, c)) in ("fanout", "channel") and (gr.uH if d == 0 else gr.uV)[l][c] > (gr.capH if d == 0 else gr.capV)[l][c]:
                risk.add(n)
                break
    return risk
