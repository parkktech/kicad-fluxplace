"""`fluxplace closure BOARD`: why each open connection is open and what it would take to close it. Read-only.

For every open connection (opengate: pcbnew after a fresh fill) and both of its pads: the free POCKET per copper layer
(flood fill at design-rule clearance, pocket.py), the nets that WALL it in with their class, the legal via sites within a
radius (a through via that fits on every layer, outside every pad), and whether same-net copper is reachable.  Options,
cheapest first, each legal by construction on the grid at the stated clearance:

  1 track    short same-layer track to existing same-net copper
  2 via      via at a site to a same-net pour, or to a free path on another layer
  3 relay    re-lay a minimal set of low-speed segments/vias that wall the pad in (named), then 1 or 2
  4 nudge    move/rotate a small part by <= `nudge_max_mm` (legality.Legal poses, support-distance limit kept), then 1 or 2
  5 pair     as 3 but through controlled-pair / RF / clock copper: ALWAYS "needs owner decision", with the leg length
             and vias it touches

    [closure]
    track_mm / clearance_mm / via_mm   # default: the larger of the board's default class and the fab floors
    window_mm = 5.0          # half-size of the analysed window around a pad
    radius_mm = 3.0          # via sites are searched within this distance of the pad
    step_mm = 0.05
    nudge_max_mm = 1.0
    max_trials = 24          # field rebuilds per pad for options 3-5

Costs (lower first): track = its length; via = length + 3; relay = length + 3 + 8 per item; nudge = length + 10 + 5 per mm;
pair = 1000 + length.  Not probed: the router is not run (a relay is a geometric unlock, not a routed one)."""
import json
import math
import os

import numpy as np

from . import constraints as C
from . import geom as GM
from . import netclass as NC
from . import opengate as OG
from . import pocket as PK
from . import preroute as PR
from . import routeconn as RC
from . import routediag as RD
from . import routerules as RR

CONTROLLED = ("pair", "rf", "clock", "switch_node")


# ---------------------------------------------------------------- helpers
def _params(cons):
    c = dict((cons or {}).get("closure") or {})
    return {"window": float(c.get("window_mm", 5.0)), "radius": float(c.get("radius_mm", 3.0)),
            "step": float(c.get("step_mm", 0.05)), "nudge": float(c.get("nudge_max_mm", 1.0)),
            "trials": int(c.get("max_trials", 24)), "track": c.get("track_mm"), "clearance": c.get("clearance_mm"),
            "via": c.get("via_mm")}


class Ctx:
    def __init__(self, B, cons, spec, R, P):
        self.B, self.cons, self.spec, self.R, self.P = B, cons, spec, R, P
        self.cls = NC.classify(B, cons, spec)
        self.zone_nets = {z["net"] for z in B["zones"] if not z["keepout"] and z["net"]}
        self.fixed = PR.fixed_parts(B, cons)
        self.members = {}
        RC.text_open(B, members=self.members)
        self._legal = None

    def label(self, net):
        if not net:
            return "none"
        if net in self.zone_nets or NC.is_ground(net):
            return "plane"
        k = (self.cls.get(net) or {}).get("class", "signal")
        return {"pair": "pair", "rf": "rf", "clock": "clock", "switch_node": "switch_node"}.get(k, "low-speed")

    def controlled(self, net):
        return self.label(net) in CONTROLLED

    def legal(self):
        if self._legal is None:
            from . import legality as LGL, routable as RB
            self._legal = LGL.Legal(self.B, self.cons, RB._fixed_set(self.B, self.cons))
        return self._legal


def _pad_of(B, ref, num, x, y):
    c = [p for p in B["pads"] if p["ref"] == ref and p["num"] == num]
    return min(c, key=lambda p: math.hypot(p["x"] - x, p["y"] - y)) if c else None


def _cluster_items(ctx, net, pad_a, pad_b):
    """(copper of the cluster holding pad a, copper of the cluster holding pad b, exact) from the text connectivity."""
    key = lambda p: (p["ref"], p["num"])
    ma = mb = None
    for pads, segs, vias in ctx.members.get(net, []):
        ks = {key(p) for p in pads}
        if key(pad_a) in ks:
            ma = (pads, segs, vias)
        if key(pad_b) in ks:
            mb = (pads, segs, vias)
    if ma and mb and ma is not mb:
        return ma, mb, True
    allp = [p for p in ctx.B["pads"] if p["net"] == net and not p.get("np")]
    alls = [s for s in ctx.B["segs"] if s["net"] == net]
    allv = [v for v in ctx.B["vias"] if v["net"] == net]
    return ([pad_a], [], []), ([p for p in allp if key(p) != key(pad_a)], alls, allv), False


def _target_masks(ctx, F, net, tgt):
    """{layer: bool mask} of the target cluster's copper (+ same-net zones, where a pour can fill) in F's window."""
    pads, segs, vias = tgt
    out = {l: np.zeros((F.ny, F.nx), bool) for l in F.copper}
    for p in pads:
        if p.get("np"):
            continue
        for l in p["layers"]:
            if l in out and abs(p["x"] - (F.x0 + F.nx * F.step / 2)) < F.nx * F.step / 2 + 2:
                out[l] |= F.shape_mask(p)
    for s in segs:
        if s["layer"] in out:
            out[s["layer"]] |= F.seg_mask(s)
    for v in vias:
        for l in PK.RA._expand_layers(v, F.copper):
            out[l] |= F.via_mask(v)
    for z in ctx.B["zones"]:
        if z["net"] == net and not z["keepout"] and len(z["pts"]) > 2:
            m = PK.poly_mask(z["pts"], F.X, F.Y)
            for l in z["layers"]:
                if l in out and _in_zone(z, l, tgt, F.copper):
                    out[l] |= m & (F.dist[l] >= F.clr)
    return out


def _in_zone(z, layer, tgt, copper):
    """the zone on `layer` belongs to the target cluster: one of its pads, vias or track ends lies inside the polygon."""
    pads, segs, vias = tgt
    pts = z["pts"]
    return (any(layer in p["layers"] and GM.poly_has(pts, p["x"], p["y"]) for p in pads if not p.get("np"))
            or any(layer in PK.RA._expand_layers(v, copper) and GM.poly_has(pts, v["x"], v["y"]) for v in vias)
            or any(s["layer"] == layer and any(GM.poly_has(pts, e[0], e[1]) for e in (s["a"], s["b"])) for s in segs))


def _which(ctx, net, x, y, tgt):
    pads, segs, vias = tgt
    best = (1e9, "same-net copper")
    for p in pads:
        d = float(PK.pad_sdf(p, np.array(x), np.array(y)))
        if d < best[0]:
            best = (d, f"pad {p['ref']}.{p['num']}")
    for v in vias:
        d = math.hypot(v["x"] - x, v["y"] - y) - v["d"] / 2
        if d < best[0]:
            best = (d, f"via ({v['x']:.2f},{v['y']:.2f})")
    for s in segs:
        d = float(PK.seg_sdf(s["a"], s["b"], s["w"], np.array(x), np.array(y)))
        if d < best[0]:
            best = (d, f"track on {s['layer']}")
    for z in ctx.B["zones"]:
        if z["net"] == net and not z["keepout"] and len(z["pts"]) > 2 and GM.poly_has(z["pts"], x, y) \
                and any(_in_zone(z, l, tgt, ctx.B["copper"]) for l in z["layers"]):
            if best[0] > 0.01:
                best = (0.0, "pour")
    return best[1]


# ---------------------------------------------------------------- options 1 and 2 on one field
def _own_pad_zone(ctx, F, net, grow):
    m = np.zeros((F.ny, F.nx), bool)
    for p in ctx.B["pads"]:
        if p["net"] == net and abs(p["x"] - F.x0) < F.nx * F.step + 2 and abs(p["y"] - F.y0) < F.ny * F.step + 2:
            m |= F.shape_mask(p, grow)
    return m


def analyse_pad(ctx, F, pad, net, tmask, tgt, layers=None):
    """-> {pockets, walls, sites, options} for `pad` (a dict with x, y, layers, shape...) in field F."""
    R, P, step = ctx.R, ctx.P, F.step
    tw, vd = R["track"], R["via"]
    pl = [l for l in (layers or sorted(pad["layers"], key=F.copper.index)) if l in F.dist]
    seeds = F.shape_mask(pad)
    dL, pockets, walls = {}, {}, {}
    edge = np.zeros((F.ny, F.nx), bool)
    edge[:2, :] = edge[-2:, :] = True
    edge[:, :2] = edge[:, -2:] = True
    for l in pl:
        fr = F.free(l, tw)
        dL[l] = PK.dist(fr | seeds, seeds, step)
        pk = dL[l] < PK.INF / 2
        pockets[l] = {"area_mm2": round(PK.area(pk, step), 2), "open": bool(pk[edge].any())}
        if not pockets[l]["open"]:                       # walled: who touches the pocket
            near = pk & ~(F.free(l, tw) & PK.dist(fr, pk, step) > 0) if False else pk
            ring = np.zeros_like(pk)
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)):
                ring |= PK._shift(~fr, dy, dx, False)
            cells = pk & ring
            own = F.owner[l][cells]
            for o in np.unique(own[own >= 0]):
                kind, key, n, label = F.items[o]
                w = walls.setdefault((n or kind), {"net": n or "", "kind": kind, "class": ctx.label(n) if n else kind,
                                                   "mm": 0.0, "items": []})
                w["mm"] += float((own == o).sum()) * step
                w["items"].append((kind, key, label))
    wl = sorted(({"net": w["net"], "kind": w["kind"], "class": w["class"], "mm": round(w["mm"], 1),
                  "items": w["items"]} for w in walls.values()), key=lambda w: -w["mm"])
    opts = []
    # option 1: same-layer track
    for l in pl:
        hit = (dL[l] < PK.INF / 2) & tmask[l]
        if hit.any():
            d = np.where(hit, dL[l], PK.INF)
            j, i = np.unravel_index(int(np.argmin(d)), d.shape)
            x, y = F.xy(j, i)
            opts.append({"type": "track", "layer": l, "mm": round(float(d[j, i]), 2), "vias": 0,
                         "to": _which(ctx, net, x, y, tgt), "at": [round(x, 2), round(y, 2)]})
    # option 2: via
    ok = F.via_ok(vd)
    if not R.get("via_in_pad"):
        ok &= ~_own_pad_zone(ctx, F, net, vd / 2 + F.clr) & ~F.shape_mask(pad, vd / 2 + F.clr)
    xs = np.hypot(F.X - pad["x"], F.Y - pad["y"]) <= P["radius"]
    cand = None
    for l in pl:
        c = ok & (dL[l] < PK.INF / 2) & xs
        cand = c if cand is None else (cand | c)
    sites = []
    nearest = {}
    if cand is not None and cand.any():
        for l in pl:
            c = PK.thin(ok & (dL[l] < PK.INF / 2) & xs, step)
            for j, i in zip(*np.nonzero(c)):
                sites.append((float(dL[l][j, i]), l, int(j), int(i)))
        sites.sort()
    nsites = len({(j, i) for _d, _l, j, i in sites})
    best2 = None
    for M in F.copper:
        fr = F.free(M, tw) | tmask[M]
        if not tmask[M].any():
            continue
        dT = PK.dist(fr, tmask[M], step)
        for dl, l, j, i in sites:
            if M == l and l in pl:
                continue
            tot = dl + float(dT[j, i])
            if tot < PK.INF / 2 and (best2 is None or tot < best2[0]):
                best2 = (tot, dl, float(dT[j, i]), l, M, j, i)
    if best2:
        tot, dl, dm, l, M, j, i = best2
        x, y = F.xy(j, i)
        opts.append({"type": "via", "layer": l, "to_layer": M, "mm": round(tot, 2), "vias": 1,
                     "site": [round(x, 2), round(y, 2)], "kind": "pour" if dm == 0 else "track",
                     "to": f"{'pour' if dm == 0 else 'same-net copper'} on {M}"})
    if not best2 and sites and not any(m.any() for m in tmask.values()):
        # no same-net copper in the window: the pad can only be asked to ESCAPE (a via site on a free layer), the rest is routing
        dl, l, j, i = sites[0]
        x, y = F.xy(j, i)
        far = min((math.hypot(q["x"] - pad["x"], q["y"] - pad["y"]) for q in tgt[0]), default=0.0)
        opts.append({"type": "via", "layer": l, "mm": round(dl, 2), "vias": 1, "site": [round(x, 2), round(y, 2)],
                     "kind": "escape", "to": f"inner-layer escape (target copper {far:.1f} mm away, outside the window)"})
    near = [(round(F.xy(j, i)[0], 2), round(F.xy(j, i)[1], 2), round(dl, 2)) for dl, l, j, i in sites[:3]]
    return {"pockets": pockets, "walls": wl, "sites": {"count": nsites, "radius": P["radius"], "nearest": near},
            "options": opts}


def _cost(o):
    t = o["type"]
    base = o["mm"] + (3.0 if o.get("vias") else 0.0)
    if t in ("track", "via"):
        return base
    if t == "relay":
        return base + 8.0 * len(o["items"])
    if t == "nudge":
        return base + 10.0 + 5.0 * o["move_mm"]
    return 1000.0 + base


# ---------------------------------------------------------------- options 3, 4, 5
def _items_near(ctx, F, pad, radius):
    """[(dist, kind, idx)] foreign segs/vias within `radius` of the pad, other nets only."""
    B, out = ctx.B, []
    for k, s in enumerate(B["segs"]):
        if s["net"] == pad["net"]:
            continue
        d = RD._seg_dist(pad["x"], pad["y"], s["a"][0], s["a"][1], s["b"][0], s["b"][1]) - s["w"] / 2
        if d <= radius:
            out.append((d, "seg", k))
    for k, v in enumerate(B["vias"]):
        if v["net"] == pad["net"]:
            continue
        d = math.hypot(v["x"] - pad["x"], v["y"] - pad["y"]) - v["d"] / 2
        if d <= radius:
            out.append((d, "via", k))
    return sorted(out)


def _item_net(ctx, kind, k):
    return (ctx.B["segs"] if kind == "seg" else ctx.B["vias"])[k]["net"]


def _describe(ctx, kind, k):
    o = (ctx.B["segs"] if kind == "seg" else ctx.B["vias"])[k]
    if kind == "seg":
        return {"kind": "seg", "net": o["net"], "layer": o["layer"], "a": [round(o["a"][0], 2), round(o["a"][1], 2)],
                "b": [round(o["b"][0], 2), round(o["b"][1], 2)], "mm": round(math.hypot(o["a"][0] - o["b"][0], o["a"][1] - o["b"][1]), 2)}
    return {"kind": "via", "net": o["net"], "at": [round(o["x"], 2), round(o["y"], 2)], "mm": 0.0}


def _is_escape(o):
    return o.get("kind") == "escape" or (o.get("then") or {}).get("kind") == "escape"


def _best_of(res):
    o = [x for x in res["options"] if x["type"] in ("track", "via")]
    return min(o, key=_cost) if o else None


def _unlock(ctx, pad, net, base_args, allow, walls, mult=1):
    """smallest set of foreign segs/vias (nets allowed by `allow(net)`) whose removal gives option 1/2 -> (set, result) | None."""
    P = ctx.P
    near = _items_near(ctx, None, pad, P["radius"] + 1.5)
    wall_items = {(k_, key) for w in walls for (k_, key, _l) in w["items"]}
    cand = [(0 if (kind, k) in wall_items else 1, d, kind, k) for d, kind, k in near if allow(_item_net(ctx, kind, k))]
    cand.sort()
    chosen, trials, hit = [], 0, None
    for _w, _d, kind, k in cand:
        if trials >= P["trials"] * mult:
            break
        chosen.append((kind, k))
        trials += 1
        res = base_args(set(chosen))
        if _best_of(res):
            hit = res
            break
    if not hit:
        return None
    for it in list(chosen):                                   # prune to a minimal set
        if len(chosen) == 1 or trials >= P["trials"] * mult + 8:
            break
        rest = [c for c in chosen if c != it]
        trials += 1
        r = base_args(set(rest))
        if _best_of(r):
            chosen, hit = rest, r
    return chosen, hit


def _nudge(ctx, pad, net, mk_field, tmask_fn, tgt, tmask):
    """smallest legal nudge/rotation of the pad's part that gives option 1/2 -> option dict | None."""
    B, P = ctx.B, ctx.P
    ref = pad["ref"]
    part = B["parts"][ref]
    if ref in ctx.fixed or part.get("locked") or part["n_pads"] > 3 or part["tht"]:
        return None
    L = ctx.legal()
    px, py = part["pos"]
    attached = [("seg", k) for k, s in enumerate(B["segs"]) if any(
        RC._in_pad(q, e[0], e[1], 0.02) and s["layer"] in q["layers"] for q in part["pads"] for e in (s["a"], s["b"]))]
    attached += [("via", k) for k, v in enumerate(B["vias"]) if any(RC._in_pad(q, v["x"], v["y"], 0.02) for q in part["pads"])]
    lim = None
    try:
        from . import clusters as CL
        for c in ctx.clusters():
            for s in c["supports"]:
                if s["ref"] == ref:
                    pin = next(p for p in B["parts"][c["ic"]]["pads"] if p["num"] == s["pad"])
                    lim = (pin, max(s["limit"], s["dist"]) + 1e-6, s["via"])
    except Exception:
        lim = None
    step, mx = 0.1, P["nudge"]
    poses = []
    for ix in range(-int(mx / step), int(mx / step) + 1):
        for iy in range(-int(mx / step), int(mx / step) + 1):
            d = math.hypot(ix * step, iy * step)
            if d <= mx + 1e-9:
                for da in (0, 90, 180, 270):
                    if (ix or iy or da):
                        poses.append((d + (0.5 if da else 0.0), ix * step, iy * step, da))
    poses.sort()
    tried = 0
    for score, dx, dy, da in poses:
        if tried >= P["trials"] * 3:
            break
        pads2, court = GM.moved(part, px + dx, py + dy, part["angle"] + da)
        if L.part_issues(ref, court=court, pads=pads2):
            continue
        if lim:
            q = next(p for p in pads2 if p["num"] == lim[2])
            if math.hypot(q["x"] - lim[0]["x"], q["y"] - lim[0]["y"]) > lim[1]:
                continue
        mp = next(p for p in pads2 if p["num"] == pad["num"])
        tried += 1
        res = mk_field(mp, pads2, set(attached))
        if res is None:
            continue
        b = _best_of(res)
        if b:
            return {"type": "nudge", "ref": ref, "dx": round(dx, 2), "dy": round(dy, 2), "rot": da,
                    "move_mm": round(math.hypot(dx, dy), 2), "attached": len(attached), "mm": b["mm"],
                    "vias": b.get("vias", 0), "then": b}
    return None


def _field(ctx, pad, net, ignore=(), skip=(), extra=()):
    P = ctx.P
    return PK.Field(ctx.B, (pad["x"], pad["y"]), P["window"], P["step"], own={net}, ignore=ignore,
                    clearance=ctx.R["clearance"], skip_refs=skip, extra_pads=list(extra))


def analyse_end(ctx, pad, net, tgt):
    P = ctx.P
    F0 = _field(ctx, pad, net)
    tmask = _target_masks(ctx, F0, net, tgt)
    res = analyse_pad(ctx, F0, pad, net, tmask, tgt)
    if _best_of(res):
        return res
    base = lambda ign: analyse_pad(ctx, _field(ctx, pad, net, ignore=ign), pad, net, tmask, tgt)
    low = lambda n: not ctx.controlled(n)
    u = _unlock(ctx, pad, net, base, low, res["walls"])
    if u:
        items, r2 = u
        b = _best_of(r2)
        res["options"].append({"type": "relay", "mm": b["mm"], "vias": b.get("vias", 0),
                               "items": [_describe(ctx, k, i) for k, i in items], "then": b})
    part = ctx.B["parts"][pad["ref"]]

    def mk(mp, pads2, attached):
        F = _field(ctx, mp, net, ignore=attached, skip={pad["ref"]}, extra=[q for q in pads2 if q["num"] != pad["num"]])
        # the nudged pad must itself clear foreign copper
        sd = F.dist
        m = F.shape_mask(mp)
        if any(float(sd[l][m].min()) < ctx.R["clearance"] - 1e-6 for l in mp["layers"] if l in sd and m.any()):
            return None
        return analyse_pad(ctx, F, mp, net, tmask, tgt, layers=[l for l in mp["layers"] if l in F.dist])
    if part["n_pads"] <= 3:
        n4 = _nudge(ctx, pad, net, mk, None, tgt, tmask)
        if n4:
            res["options"].append(n4)
    if not u:                                                    # through controlled copper: owner decision
        u5 = _unlock(ctx, pad, net, base, lambda n: True, res["walls"], 2)
        if u5:
            items, r2 = u5
            b = _best_of(r2)
            ctrl = sorted({_item_net(ctx, k, i) for k, i in items if ctx.controlled(_item_net(ctx, k, i))})
            leg = sum(_describe(ctx, k, i)["mm"] for k, i in items if k == "seg" and ctx.controlled(_item_net(ctx, k, i)))
            vias = sum(1 for k, i in items if k == "via" and ctx.controlled(_item_net(ctx, k, i)))
            res["options"].append({"type": "pair", "mm": b["mm"], "vias": b.get("vias", 0), "nets": ctrl,
                                   "items": [_describe(ctx, k, i) for k, i in items],
                                   "controlled_mm": round(leg, 2), "controlled_vias": vias,
                                   "needs": "owner decision: re-lay " + ", ".join(ctrl) + f" ({leg:.1f} mm, {vias} via(s) of controlled copper); "
                                            "its leg length / via count change, re-tune the pair after", "then": b})
    return res


# ---------------------------------------------------------------- driver
def _clusters(ctx):
    if not hasattr(ctx, "_cl"):
        from . import clusters as CL
        try:
            ctx._cl = CL.build(ctx.B, ctx.cons, ctx.spec)[0]
        except Exception:
            ctx._cl = []
    return ctx._cl


Ctx.clusters = _clusters


def run(board, cons=None, spec=None, conns=None, log=lambda m: None):
    """-> {board, open, connections: [...]}; `conns` (opengate.connections format) defaults to the measured ones."""
    cons = cons or {}
    P = _params(cons)
    B = RD.load(board)
    R = RR.rules(cons, board, B=B)
    R["track"] = float(P["track"] or max(R["track"], B["track"]))
    R["clearance"] = float(P["clearance"] or max(R["clearance"], B["clearance"]))
    R["via"] = float(P["via"] or R["via"])
    ctx = Ctx(B, cons, spec, R, P)
    conns = conns if conns is not None else OG.measure(board, cross=False)["connections"]
    out = []
    for c in conns:
        net = c["net"]
        pa = _pad_of(B, c["a"]["ref"], c["a"]["num"], c["a"]["x"], c["a"]["y"])
        pb = _pad_of(B, c["b"]["ref"], c["b"]["num"], c["b"]["x"], c["b"]["y"])
        if not pa or not pb:
            continue
        ca, cb, exact = _cluster_items(ctx, net, pa, pb)
        ends = []
        for pad, tgt in ((pa, cb), (pb, ca)):
            log(f"closure: {net} {pad['ref']}.{pad['num']}")
            r = analyse_end(ctx, pad, net, tgt)
            r.update(pad=f"{pad['ref']}.{pad['num']}", layers=sorted(pad["layers"]), x=round(pad["x"], 3), y=round(pad["y"], 3))
            ends.append(r)
        opts, esc = [], []
        for e in ends:
            esc.append(min((dict(o, pad=e["pad"], cost=round(_cost(o), 1)) for o in e["options"] if _is_escape(o)),
                           key=lambda o: o["cost"], default=None))
            for o in e["options"]:
                if not _is_escape(o):
                    opts.append(dict(o, pad=e["pad"], cost=round(_cost(o), 1)))
        if esc[0] and esc[1]:          # no same-net copper in either window: both pads must escape, the rest is routing
            opts.append({"type": "escape", "pad": f"{esc[0]['pad']}+{esc[1]['pad']}", "parts": esc, "mm": c["mm"],
                         "cost": round(esc[0]["cost"] + esc[1]["cost"], 1)})
        opts.sort(key=lambda o: o["cost"])
        out.append({"net": net, "class": ctx.label(net), "a": ends[0]["pad"], "b": ends[1]["pad"], "mm": c["mm"],
                    "exact_clusters": exact, "ends": ends, "options": opts})
    out.sort(key=lambda r: (r["options"][0]["cost"] if r["options"] else 1e9, r["net"]))
    return {"board": board, "open": len(out), "params": P, "clearance": R["clearance"], "track": R["track"], "via": R["via"],
            "connections": out}


def _opt_line(o):
    t = o["type"]
    if t == "track":
        return f"track on {o['layer']} {o['mm']} mm from {o['pad']} to {o['to']} at {o['at']}"
    if t == "via":
        return f"via at {o['site']} ({o['pad']}), {o['mm']} mm of track, to {o['to']}"
    if t == "escape":
        return (f"both pads escape (not probed: the {o['mm']} mm between them is routing): "
                + " | ".join(_opt_line(x) for x in o["parts"]))
    th = o.get("then") or {}
    nxt = f"then {th.get('type')} {th.get('mm')} mm" if th else ""
    if t == "relay":
        its = "; ".join(f"{i['net']} {i['layer'] + ' ' if 'layer' in i else ''}{i['kind']} {i.get('a', i.get('at'))}"
                        + (f"-{i['b']}" if "b" in i else "") for i in o["items"][:6])
        return f"re-lay {len(o['items'])} low-speed item(s) at {o['pad']}: {its}; {nxt}"
    if t == "nudge":
        return f"nudge {o['ref']} by ({o['dx']}, {o['dy']}) mm rot {o['rot']} ({o['attached']} attached track/via follow); {nxt}"
    return f"NEEDS OWNER DECISION: {o['needs']}; {nxt}"


def format_text(rep, top=10):
    L = [f"closure: {rep['open']} open connection(s) in {rep['board']} (clearance {rep['clearance']}, track {rep['track']}, "
         f"via {rep['via']}; window {rep['params']['window']} mm, via radius {rep['params']['radius']} mm)"]
    for r in rep["connections"][:top]:
        L.append(f"{r['net']} [{r['class']}]  {r['a']} - {r['b']}  {r['mm']} mm")
        for e in r["ends"]:
            pk = ", ".join(f"{l} {v['area_mm2']} mm2{' OPEN' if v['open'] else ''}" for l, v in e["pockets"].items())
            wl = ", ".join(f"{w['net'] or w['kind']}({w['class']}) {w['mm']}" for w in e["walls"][:4]) or "-"
            L.append(f"  {e['pad']} @({e['x']},{e['y']}) {'/'.join(e['layers'])}: pocket {pk}; walls {wl}; via sites {e['sites']['count']} "
                     f"within {e['sites']['radius']} mm")
        for k, o in enumerate(r["options"][:3], 1):
            L.append(f"  {k}. [{o['cost']}] {_opt_line(o)}")
        if not r["options"]:
            L.append("  no option found within the window (pad boxed in; widen [closure] window_mm / nudge_max_mm)")
    return "\n".join(L)


def cmd_closure(a):
    cons = C.load(a.constraints)
    spec = json.load(open(a.spec)) if a.spec else None
    cl = dict(cons.get("closure") or {})
    for k in ("track", "clearance", "via"):
        if getattr(a, k, None):
            cl[k + "_mm"] = getattr(a, k)
    cons = dict(cons, closure=cl)
    rep = run(a.board, cons, spec, log=(lambda m: print(m, flush=True)) if getattr(a, "verbose", False) else (lambda m: None))
    base = os.path.splitext(a.board)[0] + ".closure"
    top = a.top if a.top else 10
    text = format_text(rep, top)
    print(text)
    with open(base + ".json", "w") as fh:
        json.dump(rep, fh, indent=1, default=lambda o: sorted(o) if isinstance(o, set) else str(o))
    with open(base + ".txt", "w") as fh:
        fh.write(format_text(rep, 10 ** 6))
    print(f"closure: full report {base}.json / .txt")
    if a.json:
        print(json.dumps(rep, default=str))


def add_parser(sub):
    p = sub.add_parser("closure", help="open connections: pocket, walls, via sites and ranked ways to close each (read-only)")
    p.add_argument("board")
    p.add_argument("--constraints")
    p.add_argument("--spec")
    p.add_argument("--json", action="store_true", help="also print the full JSON")
    p.add_argument("--top", type=int, default=0, help="connections shown (default 10)")
    p.add_argument("--track", type=float, help="track width mm (default: board class / fab floor)")
    p.add_argument("--clearance", type=float)
    p.add_argument("--via", type=float)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_closure)
