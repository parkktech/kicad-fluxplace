"""Fan-out alternatives: a pin the fan-out solver left blocked (straight stub + via in eight directions) gets, in order,

  via        a via at the nearest legal site reached by a ROUTED stub on the pad layer (the other side of the pad row, a jog
             along the row), stub <= `[escape] alt_reach_mm` (2.5); never in a pad (`via_pad_gap` to every pad of the net)
  share      plane / ground / power nets: a stub to an existing via of the same net within reach
  pour       plane / ground / power nets with a zone of that net on the pad layer: no copper when the pour can reach the pad
             (a channel of pour width leaves the pad for `lane` mm)
  small-via  the same via search with the smallest via the design rules allow (min via diameter / drill, annular kept)

Every stub is found on a clearance field (pocket.Field, 0.05 mm) and checked exactly (pairgeo.Items) before it is written
locked.  `rescue(board, res, cons, spec)` edits the fan-out output in place and marks the pins in `res["pins"]`
(`ok`, `kind` = alt-...).  `[escape] alternatives = false` turns it off.  Nothing board specific."""
import math
import uuid

import numpy as np

from . import pairgeo as G
from . import pocket as PK


def params(cons):
    e = dict((cons or {}).get("escape") or {})
    return {"on": e.get("alternatives", True) is not False, "reach": float(e.get("alt_reach_mm", 2.5))}


def _trace(F, d, j, i):
    """cell centres from (j, i) down the distance field to a seed, corners only."""
    pts = [F.xy(j, i)]
    while d[j, i] > 0:
        v, j2, i2 = min((d[j + a, i + b], j + a, i + b) for a in (-1, 0, 1) for b in (-1, 0, 1)
                        if (a or b) and 0 <= j + a < F.ny and 0 <= i + b < F.nx)
        if v >= d[j, i]:
            return None
        j, i = j2, i2
        pts.append(F.xy(j, i))
    out = [pts[0]]
    for k in range(1, len(pts) - 1):
        a, b, c = out[-1], pts[k], pts[k + 1]
        if abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) > 1e-7:
            out.append(b)
    out.append(pts[-1])
    return out


def rescue(path, res, cons=None, spec=None, log=lambda m: None):
    """-> {tried, via, share, pour, small_via, left} ; the board at `path` gets the new locked copper."""
    from . import routediag as RD, netclass as NC, escapeplan as EP, routerules as RR, boardtext as BT
    A = params(cons)
    todo = [p for p in res.get("pins", []) if not p.get("ok")]
    stats = {"tried": len(todo), "via": 0, "share": 0, "pour": 0, "small-via": 0, "left": len(todo)}
    if not A["on"] or not todo:
        return stats
    B = RD.load(path)
    try:
        B = dict(B, pads=B["pads"] + G.holes(path, B["copper"]))
    except (OSError, ValueError, IndexError):
        pass
    P = res["params"]
    w, clr, vd, gap, lane, drill = P["track"], P["clear"], P["via"], P.get("via_pad_gap", 0.2), P.get("lane", 1.0), P.get("drill", 0.2)
    R = RR.rules(cons, path, B=B)
    ann = float((RR._pro_rules(path) or {}).get("min_via_annular_width") or 0.0)
    small = max(R.get("min_via", 0.0), R.get("min_drill", 0.0) + 2 * ann)
    small = (small, max(R.get("min_drill", 0.0), 0.1)) if 0 < small < vd - 1e-6 else None
    cls = NC.classify(B, cons, spec)
    pn = EP.plane_nets(B, cons, cls, EP.plan_params(cons))
    items = G.Items.of_board(B)
    step, reach = 0.05, A["reach"]
    blks = []
    u = lambda: str(uuid.uuid4())
    cu = B["copper"]

    def commit(pin, kind, pts, layer, via, net):
        segs = [(a, b) for a, b in zip(pts, pts[1:]) if math.hypot(a[0] - b[0], a[1] - b[1]) > 1e-4] if pts else []
        for a, b in segs:
            if items.gap_seg(a, b, w, layer, net, clr)[0] < clr - 1e-6:
                return False
        if via and items.gap_via(via[0], via[1], via[2], net, clr)[0] < clr - 1e-6:
            return False
        for a, b in segs:
            items.seg(a, b, w, layer, net)
            B["segs"].append({"net": net, "a": a, "b": b, "w": w, "layer": layer, "locked": True})
            blks.append(f'\t(segment\n\t\t(start {a[0]:.4f} {a[1]:.4f})\n\t\t(end {b[0]:.4f} {b[1]:.4f})\n\t\t(width {w})\n\t\t(locked yes)\n'
                        f'\t\t(layer "{layer}")\n\t\t(net "{net}")\n\t\t(uuid "{u()}")\n\t)')
        if via:
            items.via(via[0], via[1], via[2], net)
            B["vias"].append({"net": net, "x": via[0], "y": via[1], "d": via[2], "drill": via[3], "layers": {cu[0], cu[-1]}, "locked": True})
            blks.append(f'\t(via\n\t\t(at {via[0]:.4f} {via[1]:.4f})\n\t\t(size {via[2]})\n\t\t(drill {via[3]})\n\t\t(layers "{cu[0]}" "{cu[-1]}")\n'
                        f'\t\t(locked yes)\n\t\t(net "{net}")\n\t\t(uuid "{u()}")\n\t)')
        pin.update(ok=True, kind="alt-" + kind, layer=layer, stub=[[round(x, 4), round(y, 4)] for x, y in (pts or [])],
                   via=[round(via[0], 4), round(via[1], 4)] if via else None, w=w)
        pin.pop("blocker", None)
        stats[kind] += 1
        stats["left"] -= 1
        return True

    for pin in todo:
        net = pin["net"]
        pads = [q for q in B["pads"] if q["ref"] == pin["ref"] and q["num"] == str(pin["pin"]) and q["net"] == net]
        plane = net in pn or NC.is_ground(net) or (cls.get(net) or {}).get("class") == "rail"
        done = False
        for pad in pads:
            layer = next((l for l in (cu[0], cu[-1]) if l in pad["layers"]), None)
            if layer is None:
                continue
            F = PK.Field(B, (pad["x"], pad["y"]), reach + 1.0, step, own={net}, clearance=clr)
            seeds = F.shape_mask(pad)
            d = PK.dist(F.free(layer, w + 0.04) | seeds, seeds, step)
            own_pads = np.zeros((F.ny, F.nx), bool)
            for q in B["pads"]:
                if q["net"] == net and abs(q["x"] - pad["x"]) < reach + 2 and abs(q["y"] - pad["y"]) < reach + 2:
                    own_pads |= F.shape_mask(q, vd / 2 + gap)

            def via_try(dia, drl, kind):
                ok = F.via_ok(dia) & ~own_pads & (d <= reach)
                cost = np.where(ok, d, PK.INF)
                for _ in range(6):                           # nearest few sites
                    j, i = np.unravel_index(int(np.argmin(cost)), cost.shape)
                    if cost[j, i] >= PK.INF / 2:
                        return False
                    cost[max(0, j - 3):j + 4, max(0, i - 3):i + 4] = PK.INF
                    tr = _trace(F, d, j, i)
                    if tr and commit(pin, kind, [(pad["x"], pad["y"])] + tr[::-1], layer, F.xy(j, i) + (dia, drl), net):
                        return True
                return False
            if via_try(vd, drill, "via"):
                done = True
                break
            if plane:
                best = None
                for v in B["vias"]:                          # an existing via of the net within reach
                    if v["net"] == net and F.inside(v["x"], v["y"]):
                        j, i = F.cell(v["x"], v["y"])
                        if d[j, i] <= reach and (best is None or d[j, i] < best[0]):
                            best = (float(d[j, i]), j, i, v)
                if best:
                    tr = _trace(F, d, best[1], best[2])
                    if tr and commit(pin, "share", [(pad["x"], pad["y"])] + tr[::-1] + [(best[3]["x"], best[3]["y"])], layer, None, net):
                        done = True
                        break
                zone = any(z["net"] == net and not z["keepout"] and layer in z["layers"] and len(z["pts"]) > 2
                           and PK.poly_mask(z["pts"], np.array([[pad["x"]]]), np.array([[pad["y"]]]))[0, 0] for z in B["zones"])
                if zone:
                    zc = max(clr, 0.2)
                    m = (F.dist[layer] >= zc + 0.125) | seeds
                    dp = PK.dist(m, seeds, step)
                    if ((dp < PK.INF / 2) & (dp >= lane)).any():
                        pin.update(ok=True, kind="alt-pour", layer=layer, stub=[], via=None, w=0.0)
                        pin.pop("blocker", None)
                        stats["pour"] += 1
                        stats["left"] -= 1
                        done = True
                        break
            if small and via_try(small[0], small[1], "small-via"):
                done = True
                break
        if not done:
            log(f"fanout: no alternative for {pin['ref']}.{pin['pin']} ({net})")
    if blks:
        text = open(path, errors="surrogateescape").read()
        with open(path, "w", errors="surrogateescape") as fh:
            fh.write(BT.append_blocks(text, blks))
    return stats
