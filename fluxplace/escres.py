"""Escape reservation: a pin that must leave its footprint owns a via site and a track lane BEFORE routing starts.

Which pins (generic; nothing board specific):
  power-pin        an SMD pin of a supply rail (class rail / `[power]`, not ground): a plane inside the board is not UNDER the pad,
                   the pin needs a via site (power-pin-no-plane: the net has no plane or pour at all, `escapeplan.plane_nets`)
  passive-near-pair  a pad of a two-pad passive whose net is not a plane net, within `near_mm` (default 2.0) of a controlled
                   pair's pad or of the straight corridor between the pair's pads (pair / RF / clock nets): the copper that
                   routes first around it walls it in
Per pin: the nearest legal via site (pocket.Field: pad pocket at design clearance, a through via that fits on every layer,
outside every pad, clear of the reservations already made) with the stub lane to it and a one-track EXIT lane of `lane_mm` (1.5)
on some layer from the site.  `plan` returns reservations and the pins that could not be reserved (what crowds them: the walls
of the pocket).  Findings: PLACE_ESCAPE_UNRESERVED (FAIL) in route-ready.  `claim_text` writes each reservation as locked
stub + via copper (the fan-out stage), so no later routing can consume it.  The map keeps them under `escapes`.

    [escape]
    near_mm = 2.0   lane_mm = 1.5   window_mm = 4.0   radius_mm = 2.5   enabled = true"""
import math
import uuid

import numpy as np

from . import closure as CL
from . import escapeplan as EP
from . import netclass as NC
from . import pocket as PK
from . import routerules as RR

CONTROLLED = ("pair", "rf", "clock")


def params(cons):
    e = dict((cons or {}).get("escape") or {})
    return {"enabled": e.get("enabled", True) is not False, "near": float(e.get("near_mm", 2.0)), "lane": float(e.get("lane_mm", 1.5)),
            "window": float(e.get("window_mm", 4.0)), "radius": float(e.get("radius_mm", 2.5))}


def _corridors(B, cls):
    """[(a, b)] straight lines between the pads of every controlled pair / RF / clock net (its planned corridor)."""
    out = []
    pads = NC.net_pads(B)
    for n, info in cls.items():
        if info.get("class") in CONTROLLED:
            ps = pads.get(n, [])
            for i in range(len(ps) - 1):
                out.append(((ps[i]["x"], ps[i]["y"]), (ps[i + 1]["x"], ps[i + 1]["y"]), n))
    return out


def _attached(B, p):
    return any(s["net"] == p["net"] and s["layer"] in p["layers"] and any(
        CL.RC._in_pad(p, e[0], e[1], 0.02) for e in (s["a"], s["b"])) for s in B["segs"]) or any(
        v["net"] == p["net"] and CL.RC._in_pad(p, v["x"], v["y"], 0.02) for v in B["vias"])


def needy(B, cons, spec, cls=None):
    """[{ref, pin, net, why, pad}] pins that must own an escape."""
    cls = cls or NC.classify(B, cons, spec)
    pr = EP.plan_params(cons)
    pn = EP.plane_nets(B, cons, cls, pr)
    pw = set((cons or {}).get("power") or {})
    corr = _corridors(B, cls)
    ep = params(cons)
    out = []
    for ref, part in B["parts"].items():
        if EP.is_mech(ref) or part["tht"] or part["n_pads"] == 0:
            continue
        for p in part["pads"]:
            n = p["net"]
            if not n or p.get("np") or NC.is_ground(n) or _attached(B, p):
                continue
            info = cls.get(n) or {}
            why = None
            if info.get("class") == "rail" or n in pw:           # a plane under the board is not UNDER the pad: a via site is needed
                why = "power-pin" + ("" if n in pn else "-no-plane")
            elif n in pn:
                continue
            elif part["n_pads"] == 2 and info.get("class") not in CONTROLLED and corr:
                for a, b, cn in corr:
                    if cn != n and CL.RD._seg_dist(p["x"], p["y"], a[0], a[1], b[0], b[1]) - p["min"] / 2 <= ep["near"]:
                        why = f"passive-near-pair {cn}"
                        break
            if why:
                out.append({"ref": ref, "pin": p["num"], "net": n, "why": why, "pad": p})
    return out


def plan(B, cons, spec, cls=None, log=lambda m: None):
    """-> {reserved: [...], unreserved: [{ref, pin, net, why, crowd}], needy: n}."""
    cls = cls or NC.classify(B, cons, spec)
    ep = params(cons)
    R = RR.rules(cons, None, B=B)
    R["track"] = float(max(R["track"], B["track"]))
    R["clearance"] = float(max(R["clearance"], B["clearance"]))
    P = dict(CL._params(cons), window=ep["window"], radius=ep["radius"])
    ctx = CL.Ctx(B, cons, spec, R, P)
    tw, vd, step = R["track"], R["via"], P["step"]
    corr = _corridors(B, cls)
    todo = needy(B, cons, spec, cls)
    # most crowded first: fewest free pad-side cells would be ideal; the ratsnest-short ones are rarely stuck, so order by corridor distance
    todo.sort(key=lambda t: (min((CL.RD._seg_dist(t["pad"]["x"], t["pad"]["y"], a[0], a[1], b[0], b[1]) for a, b, _n in corr), default=99), t["ref"], t["pin"]))
    res, bad = [], []
    for t in todo:
        pad, net = t["pad"], t["net"]
        F = PK.Field(B, (pad["x"], pad["y"]), ep["window"], step, own={net}, clearance=R["clearance"])
        for r in res:
            if r["net"] != net and abs(r["site"][0] - pad["x"]) < ep["window"] + 1 and abs(r["site"][1] - pad["y"]) < ep["window"] + 1:
                F.block_via(r["site"][0], r["site"][1], vd, r["net"])
                pts = [r["pad_xy"]] + r["lane"]
                for a, b in zip(pts, pts[1:]):
                    F.block_seg(a, b, tw, r["layer"], r["net"])
                F.block_seg(r["site"], r["exit"][1], tw, r["exit_layer"], r["net"])
        pl = sorted(pad["layers"], key=F.copper.index)
        seeds = F.shape_mask(pad)
        ok = F.via_ok(vd) & ~F.shape_mask(pad, vd / 2 + F.clr) & ~CL._own_pad_zone(ctx, F, net, vd / 2 + F.clr)
        ok &= np.hypot(F.X - pad["x"], F.Y - pad["y"]) <= ep["radius"]
        best = None
        for l in pl:
            d = PK.dist(F.free(l, tw) | seeds, seeds, step)
            cand = ok & (d < PK.INF / 2)
            if not cand.any():
                continue
            cost = np.where(cand, d, PK.INF)
            for _ in range(8):                                   # nearest few sites: the first with an exit lane wins
                j, i = np.unravel_index(int(np.argmin(cost)), cost.shape)
                if cost[j, i] >= PK.INF / 2:
                    break
                cost[max(0, j - 4):j + 5, max(0, i - 4):i + 5] = PK.INF
                ex = _exit(F, j, i, l, tw, step, ep["lane"])
                if ex:
                    best = (float(d[j, i]), l, j, i, d, ex)
                    break
            if best:
                break
        if not best:
            F0 = F
            pocket = CL.analyse_pad(ctx, F0, pad, net, {l: np.zeros((F0.ny, F0.nx), bool) for l in F0.copper}, ([], [], []))
            crowd = ", ".join(f"{w['net'] or w['kind']}({w['class']}) {w['mm']} mm" for w in pocket["walls"][:4]) or \
                f"no via site within {ep['radius']} mm that fits on every layer"
            bad.append({"ref": t["ref"], "pin": t["pin"], "net": net, "why": t["why"], "crowd": crowd})
            continue
        dl, l, j, i, d, ex = best
        lane = _backtrack(F, d, j, i, step)
        x, y = F.xy(j, i)
        res.append({"ref": t["ref"], "pin": t["pin"], "net": net, "why": t["why"], "layer": l, "pad_xy": [round(float(pad["x"]), 3), round(float(pad["y"]), 3)],
                    "site": [round(float(x), 3), round(float(y), 3)], "lane": lane, "width": tw, "via": vd, "mm": round(dl, 2),
                    "exit_layer": ex[0], "exit": [[round(float(x), 3), round(float(y), 3)], [float(v) for v in ex[1]]]})
    return {"reserved": res, "unreserved": bad, "needy": len(todo)}


def _exit(F, j, i, via_layer, tw, step, lane):
    """a one-track lane of `lane` mm leaving the via site on some layer -> (layer, end point) | None."""
    seed = np.zeros((F.ny, F.nx), bool)
    seed[j, i] = True
    best = None
    for M in F.copper:
        d = PK.dist(F.free(M, tw) | seed, seed, step)
        far = np.where((d < PK.INF / 2) & (d <= lane + step), d, -1)
        jj, ii = np.unravel_index(int(np.argmax(far)), far.shape)
        if far[jj, ii] >= lane - 2 * step:
            x, y = F.xy(jj, ii)
            # a straight lane: the end point must be visible along the segment (free cells all the way)
            n = max(2, int(math.hypot(jj - j, ii - i)))
            if all(F.free(M, tw)[int(round(j + (jj - j) * k / n)), int(round(i + (ii - i) * k / n))] for k in range(n + 1)):
                return (M, [round(float(x), 3), round(float(y), 3)])
        if far[jj, ii] > 0 and (best is None or far[jj, ii] > best[0]):
            best = (far[jj, ii], M)
    return None


def _backtrack(F, d, j, i, step):
    pts = [F.xy(j, i)]
    while d[j, i] > step * 0.5:
        nb = [(d[j + dj, i + di], j + dj, i + di) for dj in (-1, 0, 1) for di in (-1, 0, 1) if (dj or di)
              and 0 <= j + dj < d.shape[0] and 0 <= i + di < d.shape[1]]
        v, j2, i2 = min(nb)
        if v >= d[j, i]:
            break
        j, i = j2, i2
        pts.append(F.xy(j, i))
    pts = pts[::-1]
    out = [pts[0]]
    for k in range(1, len(pts) - 1):                      # keep corners only
        a, b, c = out[-1], pts[k], pts[k + 1]
        if abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) > 1e-6:
            out.append(b)
    out.append(pts[-1])
    return [[round(float(x), 3), round(float(y), 3)] for x, y in out[1:]]


def findings(B, cons, spec, cls=None):
    """route-ready findings: PLACE_ESCAPE_UNRESERVED (FAIL) per pin that cannot own an escape."""
    if not params(cons)["enabled"]:
        return []
    r = plan(B, cons, spec, cls)
    return [{"level": "FAIL", "code": "PLACE_ESCAPE_UNRESERVED", "refs": [x["ref"]], "value": None, "limit": None, "cls": None,
             "msg": f"{x['ref']}.{x['pin']} ({x['net']}, {x['why']}) has no reservable via site + lane; crowded by {x['crowd']}"}
            for x in r["unreserved"]]


def claim_text(text, reserved):
    """board text with every reservation as locked stub + via copper -> (text, n). The stub is on the pad layer."""
    from . import boardtext as BT
    blks = []
    u = lambda: str(uuid.uuid4())
    for r in reserved:
        pts = [r["pad_xy"]] + r["lane"]
        for a, b in zip(pts, pts[1:]):
            if math.hypot(a[0] - b[0], a[1] - b[1]) > 1e-4:
                blks.append(f'\t(segment\n\t\t(start {a[0]} {a[1]})\n\t\t(end {b[0]} {b[1]})\n\t\t(width {r["width"]})\n'
                            f'\t\t(layer "{r["layer"]}")\n\t\t(net "{r["net"]}")\n\t\t(locked yes)\n\t\t(uuid "{u()}")\n\t)')
        blks.append(f'\t(via\n\t\t(at {r["site"][0]} {r["site"][1]})\n\t\t(size {r["via"]})\n\t\t(drill 0.2)\n\t\t(layers "F.Cu" "B.Cu")\n'
                    f'\t\t(net "{r["net"]}")\n\t\t(locked yes)\n\t\t(uuid "{u()}")\n\t)')
    return BT.append_blocks(text, blks), len(reserved)
