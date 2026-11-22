"""AI hand-routing toolkit: the commands that let an agent finish the last open nets of a routed board the way a layout
engineer does (docs/AI-FINISH.md). Small, deterministic, JSON out; the decisions belong to the caller.

  open_nets(board)            every unconnected connection with endpoints, rules, locks, islands, closest approach
  window(board, ...)          one region as data (+ PNG/SVG): pads, tracks, vias, free corridors, free via sites
  probe(board, net, path)     check a proposed path without writing anything
  lay(...)                    apply a probed path, DRC + connectivity guard, write only on success
  shove / unlay(...)          remove copper; locked removal needs explicit IDs and a session record
  session_*                   begin / undo / status / commit with a ledger of every attempt
  suggest(...)                grid search for a legal path between two islands (a proposal; probe decides)

Rules of record: the floors come from the board's own design rules, raised by `--constraints` ([route] and the [fab]
profile); nothing is ever laid below them. Locked copper changes only by explicit guarded commands. Measurement is
kicad-cli DRC with zones refilled, the project file beside the board."""
import heapq
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter

from . import hrgeom as G


# ======================================================================================================= DRC / state
def _unlink_quiet(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _tmp_beside(board, tag):
    """A unique scratch board next to `board` (mkstemp: repeated or concurrent runs never share a slot), with the
    project/rules sidecars copied so kicad-cli sees the same rules. The file exists (empty); the caller fills it."""
    d, base = os.path.split(os.path.abspath(board))
    stem = os.path.splitext(base)[0]
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f"{stem}.{tag}.", suffix=".kicad_pcb")
    os.close(fd)
    os.chmod(tmp, 0o644)
    tstem = os.path.splitext(tmp)[0]
    for ext in (".kicad_pro", ".kicad_dru"):
        side = os.path.join(d, stem + ext)
        if os.path.exists(side):
            shutil.copy(side, tstem + ext)
    return tmp


def _drop_tmp(tmp):
    stem = os.path.splitext(tmp)[0]
    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_prl"):
        _unlink_quiet(stem + ext)


def drc_raw(board, kicad_cli="kicad-cli", timeout=600):
    # a unique report slot: two runs on one board must not unlink each other's report (the pour-island missing-file crash)
    fd, out = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(board)), prefix=os.path.basename(board) + ".", suffix=".drc.json")
    os.close(fd)
    try:
        subprocess.run([kicad_cli, "pcb", "drc", "--format", "json", "--severity-all", "--all-track-errors", "--refill-zones", "-o", out, board],
                       capture_output=True, text=True, timeout=timeout)
        if os.path.getsize(out) == 0:
            raise RuntimeError("kicad-cli produced no DRC report for " + board)
        from . import drcfull as DF
        with open(out) as fh:
            return DF.complete(board, json.load(fh), fill=True)
    finally:
        _unlink_quiet(out)


_NETRE = re.compile(r"\[([^\]]*)\]")


def _item_net(it):
    m = _NETRE.search(it.get("description", ""))
    return m.group(1) if m else ""


def summarize_drc(d):
    """-> dict: keys (Counter, drcguard identity), errors {type: n}, unconnected n, unc_by_net {net: n}, pairs {net: [pair..]}."""
    from . import drcguard as DG
    keys, (nv, nu) = DG.keys_from_report(d)
    unc = d.get("unconnected_items", [])
    byn, pairs = Counter(), {}
    for u in unc:
        its = u.get("items", [])
        net = next((n for n in (_item_net(i) for i in its) if n), "")
        byn[net] += 1
        pairs.setdefault(net, []).append(u)
    errs = Counter(v["type"] for v in d.get("violations", []) if v.get("severity") == "error")
    return {"keys": keys, "errors": dict(errs), "unconnected": nu, "unc_by_net": dict(byn), "pairs": pairs, "violations": nv}


def _fmt_errs(e):
    return ", ".join(f"{k} {v}" for k, v in sorted(e.items())) or "none"


# ======================================================================================================= helpers
def _fmtp(p):
    return [round(p[0], 4), round(p[1], 4)]


def _parse_xy(s, n=2):
    v = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", s)]
    if len(v) != n:
        raise ValueError(f"expected {n} numbers in {s!r}")
    return v


def _sid(i):
    return i[:8]


def _resolve(M, ident):
    full = [o for o in M.obs if o.id.startswith(ident)]
    return full


def _model(board, constraints=None, refill=True):
    if not os.path.exists(os.path.splitext(board)[0] + ".kicad_pro"):
        print(f"warning: no {os.path.splitext(os.path.basename(board))[0]}.kicad_pro beside the board: default rules apply", file=sys.stderr)
    return G.Model(board, constraints, refill=refill)


def _item_desc(it):
    d = it.get("description", "")
    m = re.match(r"(Pad|Track|Via|Zone)\b", d)
    kind = m.group(1).lower() if m else "item"
    lm = re.search(r" on ([A-Za-z0-9_.]+Cu)", d)
    pin = re.match(r"Pad (\S+) \[[^\]]*\] of (\S+)", d)
    uid = it.get("uuid", "")
    return {"kind": kind, "id": _sid(uid), "uuid": uid, "x": it["pos"]["x"], "y": it["pos"]["y"], "layer": lm.group(1) if lm else None,
            "pin": f"{pin.group(2)}.{pin.group(1)}" if pin else None, "text": d}


def _endpoint_ob(M, net, side):
    """Resolve a DRC endpoint without assuming that its display-length UUID prefix is unique."""
    kind = side["kind"]
    if kind not in ("pad", "track", "via"):
        return None
    candidates = [o for o in M.obs if o.net == net and o.kind == kind]
    uid = side.get("uuid")
    exact = next((o for o in candidates if o.id == uid), None)
    if exact:
        return exact
    if side.get("layer"):
        candidates = [o for o in candidates if side["layer"] in o.layers]
    pt = (side["x"], side["y"])
    if not candidates:
        return None
    return min(candidates, key=lambda o: G.ob_gap(pt, pt, 0.0, o)[0])


def _fill_point(fp):
    """PHASE3H B4: a point inside fill polygon `fp` (grid search over its bbox), or None."""
    import numpy as np
    from . import hrgeom as _G
    x0, y0, x1, y1 = fp["bb"]
    X, Y = np.meshgrid(np.linspace(x0, x1, 21)[1:-1], np.linspace(y0, y1, 21)[1:-1])
    P = np.column_stack([X.ravel(), Y.ravel()])
    ins = _G.in_fill(P, fp)
    if not ins.any():
        return None
    k = int(np.argmax(ins))
    return float(P[k][0]), float(P[k][1])


def _zone_islands(M, net, a, b):
    """PHASE3H B4: both DRC ends on one zone (same layer): side a moves to a point inside the smallest island of that
    zone/layer, side b to one inside the largest; the sides keep their island ids and fill ids (island_fills)."""
    isl, _ = M.islands(net)
    zi, lay = a["zone"], a["layer"]
    cands = []
    for g in isl:
        ent = [(z, l, f) for (z, l, f) in g["zones"] if z == zi and l == lay]
        if ent:
            area = sum((M.zones[z]["fills"][l][f]["bb"][2] - M.zones[z]["fills"][l][f]["bb"][0])
                       * (M.zones[z]["fills"][l][f]["bb"][3] - M.zones[z]["fills"][l][f]["bb"][1]) for z, l, f in ent)
            cands.append((area, g, ent))
    if len(cands) < 2:
        return
    cands.sort(key=lambda c: c[0])
    for side, (_, g, ent) in ((a, cands[0]), (b, cands[-1])):
        pts = [q for q in (_fill_point(M.zones[z]["fills"][l][f]) for z, l, f in ent) if q]
        if not pts:
            return
        side["drc"] = [side["x"], side["y"]]
        side["x"], side["y"] = round(pts[0][0], 4), round(pts[0][1], 4)
        side["island"] = g["id"]
        side["island_fills"] = [list(e) for e in ent]


def _island_dist(M, net, i, j):
    isl, _ = M.islands(net)
    best = None
    for a in isl[i]["items"]:
        for b in isl[j]["items"]:
            d, pa, pb = G.ob_ob_gap(a, b)
            if best is None or d < best[0]:
                best = (d, pa, pb, a, b)
    return best


# ======================================================================================================= open-nets
def open_nets(board, constraints=None, raw=None, M=None):
    """Every unconnected connection (kicad-cli DRC, zones refilled) with the copper facts an agent needs."""
    raw = raw or drc_raw(board)
    M = M or _model(board, constraints)
    out = []
    for net, us in sorted(summarize_drc(raw)["pairs"].items()):
        rule = M.net_rule(net) if net else {}
        locked_items = [o for o in M.obs if o.net == net and o.locked and o.kind in ("track", "via")]
        for u in us:
            its = u.get("items", [])
            if len(its) < 2:
                continue
            a, b = _item_desc(its[0]), _item_desc(its[1])
            rec = {"net": net, "a": a, "b": b, "straight_mm": round(math.hypot(a["x"] - b["x"], a["y"] - b["y"]), 3),
                   "rules": {"class": rule.get("class"), "width": rule.get("width"), "clearance": rule.get("clearance"),
                             "via_dia": rule.get("via_dia"), "via_drill": rule.get("via_drill")},
                   "protected": bool(locked_items), "locked_copper_items": len(locked_items)}
            for side in (a, b):
                if side["kind"] == "zone":
                    from . import hrgeom as _G
                    import numpy as _np
                    isl, _om = M.islands(net)
                    P = _np.array([[side["x"], side["y"]]])
                    for g in isl:
                        for (zi, zl, fi) in g["zones"]:
                            if zl == side["layer"] and _G.in_fill(P, M.zones[zi]["fills"][zl][fi])[0]:
                                side["island"], side["zone"] = g["id"], zi
                    side["locked"] = None
                    continue
                ob = _endpoint_ob(M, net, side)
                side["locked"] = bool(ob.locked) if ob else None
                if ob:
                    isl, omap = M.islands(net)
                    side["island"] = omap.get(ob.id)
            if a["kind"] == "zone" and b["kind"] == "zone" and a.get("zone") is not None \
                    and a.get("zone") == b.get("zone") and a["layer"] == b["layer"]:
                _zone_islands(M, net, a, b)
            ia, ib = a.get("island"), b.get("island")
            if ia is not None and ib is not None and ia != ib:
                best = _island_dist(M, net, ia, ib)
                if best:
                    rec["closest"] = {"gap_mm": round(best[0], 3), "a": _fmtp(best[1]), "b": _fmtp(best[2]),
                                      "a_item": best[3].ref or _sid(best[3].id), "b_item": best[4].ref or _sid(best[4].id)}
            out.append(rec)
    out.sort(key=lambda r: r["straight_mm"])
    return {"board": board, "open": len(out), "unconnected_drc": summarize_drc(raw)["unconnected"], "connections": out}


def _print_open(R):
    print(f"open-nets: {R['open']} connection(s)")
    for i, r in enumerate(R["connections"]):
        a, b = r["a"], r["b"]
        cl = r.get("closest")
        print(f"  [{i}] {r['net']:<18} {a.get('pin') or a['kind']}@{a['layer']} ({a['x']:.2f},{a['y']:.2f}) -> "
              f"{b.get('pin') or b['kind']}@{b['layer']} ({b['x']:.2f},{b['y']:.2f})  {r['straight_mm']} mm  "
              f"w {r['rules']['width']} clr {r['rules']['clearance']}  locked_copper {r['locked_copper_items']}"
              + (f"  closest {cl['gap_mm']} mm" if cl else ""))


# ======================================================================================================= window
def _free_fields(M, net, layers, bbox, width, pitch, via=None, skip=None):
    """-> (xs, ys, per-layer track slack arrays, via slack (min over layers) array, hole slack array)."""
    import numpy as np
    x0, y0, x1, y1 = bbox
    xs, ys, X, Y, pitch = G.grid(bbox, pitch)
    near = (x0 - 3, y0 - 3, x1 + 3, y1 + 3)
    slack_t, slack_v, slack_h = {}, np.full(X.shape, 9.0), np.full(X.shape, 9.0)
    cache = {}
    inb = np.zeros(X.shape, bool)
    for o in M.obs:
        if o.kind == "edge":
            (ax, ay), (bx, by) = o.g[1], o.g[2]
            if ay != by:
                inb ^= ((ay > Y) != (by > Y)) & (X < (bx - ax) * (Y - ay) / (by - ay) + ax)
    all_layers = M.cu
    for lay in all_layers:
        st = np.full(X.shape, 9.0)
        sv = np.full(X.shape, 9.0)
        for o in M.in_box(near, lay):
            if skip and skip(o):
                continue
            for kind in ("track", "via"):
                if not M.foreign(o, net, kind):
                    continue
                key = (id(o))
                if key not in cache:
                    cache[key] = G.ob_dist_np(X, Y, o)
                d = cache[key]
                if o.kind == "hole":
                    if kind == "track" and o.extra.get("npth"):
                        st = np.minimum(st, d - o.clr)
                    continue
                need = 0.0 if o.kind == "ko" else (o.clr if o.kind == "edge" else M.need(net, o))
                if kind == "track":
                    st = np.minimum(st, d - need)
                else:
                    sv = np.minimum(sv, d - need)
                    if o.kind in ("pad", "track", "via"):
                        slack_h = np.minimum(slack_h, d - M.hole_clr)
            if o.kind == "hole" and lay == all_layers[0]:
                d = cache.setdefault(id(o), G.ob_dist_np(X, Y, o))
                slack_h = np.minimum(slack_h, d - o.clr_hole)
        slack_t[lay] = np.where(inb, st, -9.0)
        slack_v = np.minimum(slack_v, np.where(inb, sv, -9.0))
    return xs, ys, pitch, slack_t, slack_v, slack_h


def window(board, at=None, size=None, net=None, conn=None, layers=None, constraints=None, width=None, via=None, pitch=0.05,
           margin=1.5, png=None, svg=None, overlay=None, free_net=None, M=None, raw=None):
    M = M or _model(board, constraints)
    cu = M.cu
    lays = [G.norm_layer(l, cu) for l in layers] if layers else list(cu)
    info = {}
    if conn is not None:
        R = open_nets(board, constraints, raw=raw, M=M)
        rec = R["connections"][int(conn)]
        net = rec["net"]
        pts = [(rec["a"]["x"], rec["a"]["y"]), (rec["b"]["x"], rec["b"]["y"])]
        if rec.get("closest"):
            pts = [tuple(rec["closest"]["a"]), tuple(rec["closest"]["b"])]
        x0, x1 = min(p[0] for p in pts) - margin, max(p[0] for p in pts) + margin
        y0, y1 = min(p[1] for p in pts) - margin, max(p[1] for p in pts) + margin
        bb = (x0, y0, x1, y1)
        info["connection"] = rec
    elif net and not at:
        # frame the net's open connection(s)
        R = open_nets(board, constraints, raw=raw, M=M)
        recs = [r for r in R["connections"] if r["net"] == net]
        if not recs:
            raise ValueError(f"net {net} has no open connection")
        rec = recs[0]
        pts = [(rec["a"]["x"], rec["a"]["y"]), (rec["b"]["x"], rec["b"]["y"])]
        if rec.get("closest"):
            pts = [tuple(rec["closest"]["a"]), tuple(rec["closest"]["b"])]
        bb = (min(p[0] for p in pts) - margin, min(p[1] for p in pts) - margin, max(p[0] for p in pts) + margin, max(p[1] for p in pts) + margin)
        info["connection"] = rec
    else:
        cx, cy = at
        w, h = size
        bb = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
    fnet = free_net if free_net is not None else (net or "")
    rule = M.net_rule(fnet) if fnet else {"width": M.floors["track"] if width is None else width, "clearance": M.floors["clr"],
                                          "via_dia": M.floors["via_dia"], "via_drill": M.floors["via_drill"], "class": None}
    tw = width or rule["width"]
    vd, vdr = (via or (rule["via_dia"], rule["via_drill"]))
    x0, y0, x1, y1 = bb
    W = {"window": [round(v, 3) for v in bb], "layers": lays, "net": net, "for_net": fnet, "track": {"width": tw, "clearance": rule["clearance"]},
         "via": {"dia": vd, "drill": vdr}, "floors": {k: round(v, 4) for k, v in M.floors.items()}}
    W.update(info)
    pads, tracks, vias, geom_pads = [], [], [], []
    ko, edge, zones, holes = [], [], [], []
    isl_of = {}
    for o in M.in_box(bb):
        if o.kind == "pad":
            if not (o.layers & set(lays)):
                continue
            isl = M.islands(o.net)[1].get(o.id) if o.net else None
            pads.append({"ref": o.ref, "net": o.net, "shape": o.extra["shape"], "size": [round(v, 3) for v in o.extra["size"]], "pos": _fmtp(o.extra["pos"]),
                         "layers": sorted(o.layers & set(lays), key=cu.index) if len(o.layers) < len(cu) else ["*"], "hole": round(o.extra["hole"], 3) or None,
                         "locked": o.locked, "island": isl})
            geom_pads.append({"ref": o.ref, "net": o.net, "layers": o.layers, "poly": o.g[1], "pos": o.extra["pos"]})
        elif o.kind == "track" and o.layers & set(lays):
            lay = next(iter(o.layers))
            isl = M.islands(o.net)[1].get(o.id) if o.net else None
            tracks.append({"id": _sid(o.id), "net": o.net, "layer": lay, "w": round(o.extra["width"], 4), "a": _fmtp(o.g[1]), "b": _fmtp(o.g[2]),
                           "locked": o.locked, "island": isl})
        elif o.kind == "via":
            isl = M.islands(o.net)[1].get(o.id) if o.net else None
            vias.append({"id": _sid(o.id), "net": o.net, "pos": _fmtp(o.g[1]), "dia": round(o.extra["dia"], 3), "drill": round(o.extra["drill"], 3),
                         "locked": o.locked, "island": isl})
        elif o.kind == "ko":
            ko.append({"name": o.desc, "layers": sorted(o.layers, key=cu.index), "no_tracks": o.extra["no_tracks"], "no_vias": o.extra["no_vias"],
                       "no_pads": o.extra["no_pads"], "pts": o.g[1]})
        elif o.kind == "edge":
            edge.append((o.g[1], o.g[2]))
        elif o.kind == "hole" and o.extra.get("npth"):
            holes.append({"ref": o.ref, "pos": _fmtp(o.g[1]), "dia": round(o.g[3] * 2, 3)})
    for z in M.zones:
        for l in z["layers"]:
            if l in lays:
                xs = [p[0] for p in z["outline"]] or [0]
                ys = [p[1] for p in z["outline"]] or [0]
                if max(xs) < x0 or min(xs) > x1 or max(ys) < y0 or min(ys) > y1:
                    continue
                pieces = []
                zi = M.zones.index(z)
                isl_g = M.islands(z["net"])[0] if z["net"] else []
                for fi, fpz in enumerate(z["fills"].get(l) or []):
                    bx0, by0, bx1, by1 = fpz["bb"]
                    if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
                        continue
                    area = abs(_area(fpz["outline"])) - sum(abs(_area(h)) for h in fpz["holes"])
                    gid = next((g["id"] for g in isl_g if (zi, l, fi) in g["zones"]), None)
                    pieces.append({"piece": fi, "bbox": [round(v, 2) for v in fpz["bb"]], "area_mm2": round(area, 3), "island": gid})
                zones.append({"net": z["net"], "layer": l, "name": z["name"], "filled": bool(z["fills"].get(l)), "priority": z["priority"],
                              "outline": z["outline"], "fill_pieces": pieces})
    W["pads"] = pads
    W["tracks"] = tracks
    W["vias"] = vias
    W["zones"] = [{k: v for k, v in z.items() if k != "outline"} for z in zones]
    W["keepouts"] = [{k: v for k, v in k_.items() if k != "pts"} for k_ in ko]
    W["npth_holes"] = holes
    W["edge_segments"] = [[_fmtp(a), _fmtp(b)] for a, b in edge]
    # free corridors per layer + free via sites
    import numpy as np
    xs, ys, pitch2, st, sv, sh = _free_fields(M, fnet, lays, bb, tw, pitch)
    W["grid_pitch"] = round(pitch2, 4)
    free = {}
    free_mask = {}
    comp_at = {}
    for lay in lays:
        mask = st[lay] >= tw / 2 - G.EPS
        lab, n = G.label(mask)
        comps = []
        for c in range(1, n + 1):
            ii = np.argwhere(lab == c)
            if len(ii) < 3:
                continue
            cx0, cx1 = xs[ii[:, 1].min()], xs[ii[:, 1].max()]
            cy0, cy1 = ys[ii[:, 0].min()], ys[ii[:, 0].max()]
            peak = float(2 * st[lay][lab == c].max())
            sides = [s for s, hit in (("W", ii[:, 1].min() == 0), ("E", ii[:, 1].max() == len(xs) - 1),
                                      ("N", ii[:, 0].min() == 0), ("S", ii[:, 0].max() == len(ys) - 1)) if hit]
            comps.append({"id": c, "bbox": [round(float(cx0), 3), round(float(cy0), 3), round(float(cx1), 3), round(float(cy1), 3)],
                          "area_mm2": round(float(len(ii) * pitch2 * pitch2), 3), "max_track_mm": round(min(peak, 9.0), 3), "touches_window_edge": sides})
        comps.sort(key=lambda c: -c["area_mm2"])
        free[lay] = comps[:12]
        comp_at[lay] = (lab, mask)
    W["free_corridors"] = free
    # via sites
    vr = vd / 2
    ok = (sv >= vr - G.EPS) & (sh >= vdr / 2 - G.EPS)
    ij = np.argwhere(ok)
    slack = np.minimum(sv[ok] - vr, sh[ok] - vdr / 2)
    order = np.argsort(-slack)
    sites = [[round(float(xs[ij[k][1]]), 3), round(float(ys[ij[k][0]]), 3), round(float(slack[k]), 3)] for k in order]
    keep, step = [], max(1, int(round(0.2 / pitch2)))
    taken = set()
    for s in sites:
        cell = (int(round((s[0] - x0) / (pitch2 * step))), int(round((s[1] - y0) / (pitch2 * step))))
        if cell in taken:
            continue
        taken.add(cell)
        keep.append(s)
    W["via_sites"] = {"count": int(ok.sum()), "pitch": pitch2, "need_slack_ge": 0.0, "listed": len(keep[:150]), "sites": keep[:150],
                      "note": "[x, y, margin mm]; best margin first, one per ~0.2 mm cell"}
    # connection view
    if "connection" in W:
        rec = W["connection"]
        view = {}
        for tag in ("a", "b"):
            e = rec[tag]
            per = {}
            for lay in lays:
                lab, mask = comp_at[lay]
                ix, iy = int(round((e["x"] - xs[0]) / pitch2)), int(round((e["y"] - ys[0]) / pitch2))
                if 0 <= iy < lab.shape[0] and 0 <= ix < lab.shape[1] and lab[iy, ix]:
                    per[lay] = int(lab[iy, ix])
            view[tag] = {"item": e.get("pin") or e["kind"], "free_component_by_layer": per}
        both = [l for l in lays if view["a"]["free_component_by_layer"].get(l) and view["a"]["free_component_by_layer"].get(l) == view["b"]["free_component_by_layer"].get(l)]
        view["direct_corridor_layers"] = both
        W["connection_view"] = view
    W["_geom"] = {"pads": geom_pads, "tracks": [{"net": t["net"], "layer": t["layer"], "w": t["w"], "a": t["a"], "b": t["b"], "locked": t["locked"]} for t in tracks],
                  "vias": [{"net": v["net"], "pos": v["pos"], "dia": v["dia"], "drill": v["drill"]} for v in vias],
                  "zones": zones, "keepouts": ko, "edge": edge, "via_sites": [(s[0], s[1]) for s in keep[:150]]}
    for out, kind in ((png, "png"), (svg, "svg")):
        if out:
            from . import hrrender as RN
            RN.render(W, out, overlay=overlay)
            W.setdefault("images", []).append(out)
    return W


def _area(pts):
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))


def _window_public(W):
    return {k: v for k, v in W.items() if k != "_geom"}


# ======================================================================================================= probe
def _clear_fix(obs_pt, a, b, shortfall):
    """Direction (unit vector, obstacle -> path) to move a path segment a-b and the shift (rounded up to 0.01 mm) that cures `shortfall` mm."""
    if obs_pt is None or shortfall <= 0:
        return {}
    _, q = G.pt_seg(obs_pt, a, b)
    dx, dy = q[0] - obs_pt[0], q[1] - obs_pt[1]
    n = math.hypot(dx, dy)
    if n < 1e-9:
        return {}
    return {"clear_dir": [round(dx / n, 4), round(dy / n, 4)], "clear_shift_mm": round(math.ceil(shortfall / 0.01 - 1e-6) * 0.01, 2)}


def probe_prims(M, net, prims, width=None, via=None, any_angle=False, skip_ids=()):
    rule = M.net_rule(net)
    w = width or rule["width"]
    vd, vdr = via or (rule["via_dia"], rule["via_drill"])
    f = M.floors
    skip = set(skip_ids or ())
    in_box = (lambda *a: [o for o in M.in_box(*a) if o.id not in skip]) if skip else M.in_box
    viol, items, notes = [], [], []
    if w < f["track"] - 1e-6:
        viol.append({"kind": "width_below_floor", "need": f["track"], "have": w})
    elif w < rule["width"] - 1e-6:
        notes.append(f"neck-down: width {w} below class width {rule['width']} (floor {f['track']})")
    if prims and any(p[0] == "via" for p in prims):
        if vd < f["via_dia"] - 1e-6 or vdr < f["via_drill"] - 1e-6:
            viol.append({"kind": "via_below_floor", "need": [f["via_dia"], f["via_drill"]], "have": [vd, vdr]})
    # continuity
    prev_end, prev_layer = None, None
    for i, p in enumerate(prims):
        if p[0] == "seg":
            if prev_end is not None and prev_layer == p[1] and math.hypot(prev_end[0] - p[2][0], prev_end[1] - p[2][1]) > 1e-4 and not prims[i - 1][0] == "via":
                viol.append({"kind": "discontinuity", "at": _fmtp(p[2]), "note": "segment does not start where the previous one ended"})
            prev_end, prev_layer = p[3], p[1]
        else:
            if prev_end is not None and math.hypot(prev_end[0] - p[1][0], prev_end[1] - p[1][1]) > 1e-4:
                viol.append({"kind": "discontinuity", "at": _fmtp(p[1]), "note": "via is not at the end of the previous segment"})
            prev_end = p[1]
            nxt = next((q for q in prims[i + 1:] if q[0] == "seg"), None)
            if nxt and math.hypot(nxt[2][0] - p[1][0], nxt[2][1] - p[1][1]) > 1e-4:
                viol.append({"kind": "discontinuity", "at": _fmtp(p[1]), "note": "next segment does not start at the via"})
    for i, p in enumerate(prims):
        if p[0] == "seg":
            _, lay, a, b = p
            ln = math.hypot(b[0] - a[0], b[1] - a[1])
            rec = {"i": i, "type": "seg", "layer": lay, "a": _fmtp(a), "b": _fmtp(b), "len": round(ln, 3), "nearest": None}
            if ln < 1e-6:
                viol.append({"kind": "zero_length", "i": i, "at": _fmtp(a)})
            elif not any_angle and ln >= 0.1:
                ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 45
                if min(ang, 45 - ang) > 0.01:
                    viol.append({"kind": "angle_not_45_90", "i": i, "at": _fmtp(a), "deg": round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])), 2)})
            best = None
            for o in in_box((min(a[0], b[0]) - w / 2 - 1.5, min(a[1], b[1]) - w / 2 - 1.5, max(a[0], b[0]) + w / 2 + 1.5, max(a[1], b[1]) + w / 2 + 1.5), lay):
                if not M.foreign(o, net, "track"):
                    continue
                gap, pt = G.ob_gap(a, b, w / 2, o)
                need = (o.clr if o.kind in ("edge",) else 0.0 if o.kind == "ko" else o.clr if o.kind == "hole" else M.need(net, o))
                margin = gap - need
                lab = o.ref or (f"{o.kind} {o.net}" if o.net else o.kind)
                cand = {"item": lab, "kind": o.kind, "net": o.net, "id": _sid(o.id) if o.id else None, "gap": round(gap, 4), "need": round(need, 4),
                        "margin": round(margin, 4), "at": _fmtp(pt) if pt else None, "locked": o.locked}
                if best is None or margin < best["margin"]:
                    best = cand
                if margin < -G.EPS:
                    v = dict(cand)
                    v.update({"kind": "crossing" if gap <= 0 and o.kind in ("track", "via", "pad") else "clearance" if o.kind != "ko" and o.kind != "edge" else o.kind + "_hit",
                              "i": i, "with": lab, "short_if_laid": gap <= 0 and o.kind in ("track", "via", "pad")})
                    if v["kind"] == "clearance":
                        v.update(_clear_fix(pt, a, b, -margin))
                    viol.append(v)
            rec["nearest"] = best
            items.append(rec)
        else:
            c = p[1]
            rec = {"i": i, "type": "via", "at": _fmtp(c), "dia": vd, "drill": vdr, "nearest": None}
            best = None
            for lay in M.cu:
                for o in in_box((c[0] - vd / 2 - 1.5, c[1] - vd / 2 - 1.5, c[0] + vd / 2 + 1.5, c[1] + vd / 2 + 1.5), lay):
                    if o.kind == "hole" and not o.extra.get("via") and False:
                        continue
                    if not M.foreign(o, net, "via"):
                        continue
                    if o.kind == "hole":
                        if lay != M.cu[0]:
                            continue
                        gap, pt = G.ob_gap(c, c, vdr / 2, o)
                        need = M.h2h
                    else:
                        gap, pt = G.ob_gap(c, c, vd / 2, o)
                        need = 0.0 if o.kind == "ko" else (o.clr if o.kind == "edge" else M.need(net, o))
                    margin = gap - need
                    lab = o.ref or (f"{o.kind} {o.net}" if o.net else o.kind)
                    cand = {"item": lab, "kind": o.kind, "layer": lay, "net": o.net, "id": _sid(o.id) if o.id else None, "gap": round(gap, 4),
                            "need": round(need, 4), "margin": round(margin, 4), "at": _fmtp(pt) if pt else None, "locked": o.locked}
                    if best is None or margin < best["margin"]:
                        best = cand
                    if margin < -G.EPS:
                        v = dict(cand)
                        v.update({"kind": "hole_to_hole" if o.kind == "hole" else "via_clearance" if o.kind not in ("ko", "edge") else o.kind + "_hit", "i": i, "with": lab})
                        if v["kind"] in ("via_clearance", "hole_to_hole"):
                            v.update(_clear_fix(pt, c, c, -margin))
                        viol.append(v)
                    if o.kind in ("pad", "track", "via"):
                        hgap = gap + (vd / 2 - vdr / 2)
                        hmargin = hgap - M.hole_clr
                        if hmargin < -G.EPS:
                            viol.append({"kind": "hole_clearance", "i": i, "layer": lay, "with": lab, "gap": round(hgap, 4), "need": M.hole_clr, "margin": round(hmargin, 4),
                                         "at": _fmtp(pt) if pt else None})
            rec["nearest"] = best
            items.append(rec)
    for i, p in enumerate(prims):
        if p[0] != "seg":
            continue
        for pt in (p[2], p[3]):
            for o in in_box((pt[0] - 0.5, pt[1] - 0.5, pt[0] + 0.5, pt[1] + 0.5), p[1]):
                if o.kind == "via" and o.net == net:
                    c = o.g[1]
                    d = math.hypot(pt[0] - c[0], pt[1] - c[1])
                    if 0.0005 < d < o.g[3]:
                        out_v = {"kind": "track_not_centered_on_via", "i": i, "at": _fmtp(pt), "via_at": _fmtp(c), "off_mm": round(d, 4),
                                 "fix": "end the segment exactly on the via centre"}
                        viol.append(out_v)
            for pn in prims:
                if pn[0] == "via" and p[1] in M.cu:
                    d = math.hypot(pt[0] - pn[1][0], pt[1] - pn[1][1])
                    if 0.0005 < d < vd / 2:
                        viol.append({"kind": "track_not_centered_on_via", "i": i, "at": _fmtp(pt), "via_at": _fmtp(pn[1]), "off_mm": round(d, 4)})
    # dedupe identical violations
    seen, out = set(), []
    for v in viol:
        k = json.dumps(v, sort_keys=True)
        if k not in seen:
            seen.add(k)
            out.append(v)
    # joins: which own-net copper the path ends touch
    joins = []
    ends = []
    if prims:
        ends = [((p[1],), p[2]) if p[0] == "seg" else (tuple(M.cu), p[1]) for p in (prims[0],)]
        ends += [((p[1],), p[3]) if p[0] == "seg" else (tuple(M.cu), p[1]) for p in (prims[-1],)]
    for lays_e, pt in ends:
        hit, lay = None, lays_e[0]
        for lay in lays_e:
            for o in in_box((pt[0] - 0.01, pt[1] - 0.01, pt[0] + 0.01, pt[1] + 0.01), lay):
                if o.net == net and o.kind in ("pad", "track", "via") and G.ob_gap(pt, pt, 0.0, o)[0] <= 1e-3:
                    isl = M.islands(net)[1].get(o.id)
                    hit = {"item": o.ref or o.kind, "id": _sid(o.id), "island": isl}
                    break
            if hit:
                break
        joins.append({"at": _fmtp(pt), "layer": lay, "touches": hit})
    return {"net": net, "ok": not out, "width": w, "via": [vd, vdr], "violations": out, "items": items, "ends_join": joins, "notes": notes,
            "rule": {"width": rule["width"], "clearance": rule["clearance"], "class": rule["class"]}}


def probe(board, net, path, constraints=None, width=None, via=None, any_angle=False, M=None):
    M = M or _model(board, constraints)
    if net not in {o.net for o in M.obs if o.net}:
        raise ValueError(f"unknown net {net!r}")
    prims = G.parse_path(path, M.cu)
    return probe_prims(M, net, prims, width, via, any_angle)


def _print_probe(R):
    print(f"probe {R['net']}: {'OK' if R['ok'] else 'FAIL'}  width {R['width']} (class {R['rule']['width']}, clr {R['rule']['clearance']})")
    for it in R["items"]:
        n = it["nearest"]
        print(f"  [{it['i']}] {it['type']} {it.get('layer', '')} {it.get('a', it.get('at'))}" + (f"->{it['b']}" if "b" in it else "") +
              (f"  nearest {n['item']} gap {n['gap']} need {n['need']} margin {n['margin']} at {n['at']}" if n else ""))
    for v in R["violations"]:
        print("  VIOLATION", json.dumps(v))
    for e in R["ends_join"]:
        print("  end", e)
    for n in R["notes"]:
        print("  note", n)


# ======================================================================================================= edits
def _apply_prims(b, M, net, prims, width, via, lock=False):
    pcbnew = M.pcbnew
    pnet = b.FindNet(net)
    if pnet is None:
        raise ValueError(f"net {net} not on board")
    added = []
    P = lambda p: pcbnew.VECTOR2I(int(round(p[0] * 1e6)), int(round(p[1] * 1e6)))      # noqa: E731
    for p in prims:
        if p[0] == "seg":
            t = pcbnew.PCB_TRACK(b)
            t.SetStart(P(p[2]))
            t.SetEnd(P(p[3]))
            t.SetWidth(int(round(width * 1e6)))
            t.SetLayer(M.lid[p[1]])
            t.SetNet(pnet)
            t.SetLocked(lock)
            b.Add(t)
            added.append(("seg", p[1], p[2], p[3], t.m_Uuid.AsString()))
        else:
            pos = P(p[1])
            old = next((t for t in b.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetCode() == pnet.GetNetCode()
                        and t.GetPosition() == pos and t.GetWidth(pcbnew.F_Cu) == int(round(via[0] * 1e6))
                        and t.GetDrill() == int(round(via[1] * 1e6))), None)
            if old is not None:
                added.append(("via-existing", p[1], old.m_Uuid.AsString()))
                continue
            v = pcbnew.PCB_VIA(b)
            v.SetPosition(pos)
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            v.SetWidth(int(round(via[0] * 1e6)))
            v.SetDrill(int(round(via[1] * 1e6)))
            v.SetNet(pnet)
            v.SetLocked(lock)
            b.Add(v)
            added.append(("via", p[1], v.m_Uuid.AsString()))
    return added


class Sess:
    """A hand-routing session: WORK board + WORK.session/ (start copy, undo stack, ledger)."""

    def __init__(self, work):
        self.work = os.path.abspath(work)
        self.dir = self.work + ".session"
        self.meta_path = os.path.join(self.dir, "meta.json")

    @property
    def exists(self):
        return os.path.isdir(self.dir) and os.path.exists(self.meta_path)

    def meta(self):
        return json.load(open(self.meta_path))

    def save_meta(self, m):
        json.dump(m, open(self.meta_path, "w"), indent=1)

    def log(self, entry):
        path = os.path.join(self.dir, "ledger.json")
        L = json.load(open(path)) if os.path.exists(path) else []
        entry["n"] = len(L) + 1
        entry["t"] = time.strftime("%H:%M:%S")
        L.append(entry)
        json.dump(L, open(path, "w"), indent=1, default=str)
        _write_text_ledger(L, os.path.join(self.dir, "ledger.txt"))
        return entry


def _write_text_ledger(L, path):
    lines = []
    for e in L:
        ms = e.get("measure", {})
        lines.append(f"#{e['n']} {e['t']} {e['op']:<7} {'ACCEPT' if e.get('accepted') else 'REJECT':<6} net={e.get('net', '-')}  {e.get('what', '')}")
        if e.get("reason"):
            lines.append(f"    why: {e['reason']}")
        if ms:
            lines.append("    " + "  ".join(f"{k}={v}" for k, v in ms.items()))
        for item in e.get("removed_locked", []):
            lines.append("    unlocked-and-removed: " + json.dumps(item, sort_keys=True))
        for opened in e.get("opened", []):
            lines.append("    opened: " + json.dumps(opened, sort_keys=True))
        if e.get("args") is not None:                        # PHASE3I F5 (R12): replay() lays with the original arguments
            lines.append("    args: " + json.dumps(e["args"], sort_keys=True))
        if e.get("stitched"):                                # PHASE3I B2: replay() re-applies a stitched lay with stitch=True
            lines.append("    stitched: " + json.dumps(e["stitched"], sort_keys=True))
    open(path, "w").write("\n".join(lines) + "\n")


def _copy_board(src, dst):
    shutil.copy(src, dst)
    s, d = os.path.splitext(src)[0], os.path.splitext(dst)[0]
    for ext in (".kicad_pro", ".kicad_dru"):
        if os.path.exists(s + ext) and os.path.abspath(s + ext) != os.path.abspath(d + ext):
            shutil.copy(s + ext, d + ext)


def session_begin(board, work, kicad_cli="kicad-cli"):
    work = os.path.abspath(work)
    S = Sess(work)
    if S.exists:
        raise RuntimeError(f"session already exists at {S.dir} (source {S.meta().get('source')}): "
                           "give this agent its own work path; the existing session is untouched")
    os.makedirs(os.path.join(S.dir, "undo"), exist_ok=True)
    _copy_board(board, work)
    shutil.copy(work, os.path.join(S.dir, "start.kicad_pcb"))
    raw = drc_raw(work, kicad_cli)
    json.dump(raw, open(os.path.join(S.dir, "start_drc.json"), "w"))
    json.dump(raw, open(os.path.join(S.dir, "cur_drc.json"), "w"))
    sm = summarize_drc(raw)
    S.save_meta({"source": os.path.abspath(board), "work": work, "undo": 0, "started": time.strftime("%Y-%m-%d %H:%M:%S")})
    return {"work": work, "session": S.dir, "unconnected": sm["unconnected"], "errors": sm["errors"]}


def _snap(path):
    from . import audit as AU
    return AU.snapshot(path, checks=False, project=os.path.splitext(path)[0] + ".kicad_pro")


def _copper_sig(M, locked_only=False):
    sig = set()
    for o in M.obs:
        if o.kind in ("track", "via") and (o.locked or not locked_only):
            sig.add((o.kind, o.net, tuple(sorted(o.layers))[:1], tuple(round(v, 3) for v in (*o.g[1], *o.g[2])), round(o.g[3], 3)))
    return sig


def _locked_sig(M):
    return _copper_sig(M, locked_only=True)


def _edit_violation_keys(new, net, loose=()):
    out = {}
    for k, v in new.items():
        if k[0].startswith("U") or k[0] in loose:
            continue
        if k[0] == "track_not_centered_on_via" and not any(f"[{net}]" in d for d in k[1]):
            continue
        out[k] = v
    return out


def _opened_nets(cand_unc, base_unc):
    """PHASE3H-C: {net: (before, after)} for every net whose unconnected count rose (keys are net names, not items)."""
    return {n: (base_unc.get(n, 0), c) for n, c in cand_unc.items() if c > base_unc.get(n, 0)}


def _zone_nets(b):
    return {z.GetNetname() for z in b.Zones() if not z.GetIsRuleArea() and z.GetNetname()}


def _stitch_ok(J, base, net):
    """PHASE3H-C acceptance: the stitched candidate has no refusal, the target's opens fell, and no net is more open."""
    if J["reasons"]:
        return False
    if J["cand"]["unc_by_net"].get(net, 0) >= base["unc_by_net"].get(net, 0):
        return False
    return all(c <= base["unc_by_net"].get(n, 0) for n, c in J["cand"]["unc_by_net"].items())


def _place_vias(b, M, att):
    for n, x, y, vv in att:
        _apply_prims(b, M, n, [("via", (x, y))], 0, vv)


def _drop_vias(b, M, att):
    pcbnew = M.pcbnew
    for n, x, y, vv in att:
        pnet = b.FindNet(n)
        pos = pcbnew.VECTOR2I(int(round(x * 1e6)), int(round(y * 1e6)))
        for t in [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetPosition() == pos and pnet is not None
                  and t.GetNetCode() == pnet.GetNetCode()]:
            b.Remove(t)


def _edit(board, out, mutate, op, net, what, constraints=None, partial=False, require_close=True, kicad_cli="kicad-cli", extra_check=None, refill=True,
          stitch=None, args=None):
    """The common edit path. mutate(pcbnew_board, Model) -> info dict (raises ValueError to refuse). Writes `out` only when accepted.
    stitch(b, M, nets) -> iterable of attempts [(net, x, y, via_dims)...]: used only when the refusal is a lay that opened
    connections of zone nets alone; each attempt's vias go into the same in-memory board and the same guard decides (PHASE3H-C).
    -> result dict {accepted, reason, measure, info, stitched?}."""
    S = Sess(out if out else board)
    in_session = out is None
    if in_session and not S.exists:
        raise RuntimeError("no --out and no session for this board: give --out, or begin a session")
    target = S.work if in_session else out
    src = board
    base_raw = json.load(open(os.path.join(S.dir, "cur_drc.json"))) if in_session else drc_raw(src, kicad_cli)
    base = summarize_drc(base_raw)
    M = _model(src, constraints, refill=refill)
    res = {"op": op, "net": net, "what": what, "accepted": False}
    if args is not None:
        res["args"] = args
    try:
        b = M.b
        info = mutate(b, M)
    except ValueError as e:
        res.update(reason=str(e), measure={})
        _record(S, res, in_session, out)
        return res
    tmp = _tmp_beside(src, "hrtry")

    def judge(path):
        from . import audit as AU
        cand_raw = drc_raw(path, kicad_cli)
        cand = summarize_drc(cand_raw)
        new = cand["keys"] - base["keys"]
        # a shove leaves loose ends by design (the caller re-lays); the commit gate judges the end state
        loose = ("track_dangling", "via_dangling") if op in ("shove", "unlay") else ()
        new_nonu = _edit_violation_keys(new, net, loose)
        new_u = _opened_nets(cand["unc_by_net"], base["unc_by_net"])
        before_n = base["unc_by_net"].get(net, 0)
        after_n = cand["unc_by_net"].get(net, 0)
        meas = {"unconnected": f"{base['unconnected']}->{cand['unconnected']}", f"open[{net}]": f"{before_n}->{after_n}",
                "new_violation_keys": len(new_nonu), "new_unconnected_keys": len(new_u),
                "errors_before": _fmt_errs(base["errors"]), "errors_after": _fmt_errs(cand["errors"])}
        reasons = []
        if new_nonu:
            ex = sorted(new_nonu)[0]
            reasons.append(f"new DRC violation(s): {len(new_nonu)}, e.g. {ex[0]} {list(ex[1])[:2]}")
        diff = AU.connectivity_diff(_snap(src), _snap(path))
        if not diff["ok"]:
            reasons.append("connectivity guard: " + "; ".join(diff["violations"][:3]))
        if op == "lay" and new_u:
            reasons.append("lay opened connection(s): " + ", ".join(f"{n} {x}->{y}" for n, (x, y) in new_u.items()))
        if op == "lay" and require_close and not partial and after_n >= before_n:
            reasons.append(f"target not closed: open connections of {net} {before_n} -> {after_n}")
        if extra_check:
            r = extra_check(M, cand, base)
            if r:
                reasons.append(r)
        return {"cand_raw": cand_raw, "cand": cand, "reasons": reasons, "meas": meas, "new_u": new_u}

    try:
        M.pcbnew.SaveBoard(tmp, b)
        J = judge(tmp)
        stitched = []
        if stitch and len(J["reasons"]) == 1 and J["new_u"] and set(J["new_u"]) <= _zone_nets(b):
            # the lay alone split a zone of other nets: stitch those islands in the same transaction, judged by the same guard
            for att in stitch(b, M, sorted(J["new_u"])):
                _place_vias(b, M, att)
                M.pcbnew.SaveBoard(tmp, b)
                J2 = judge(tmp)
                if _stitch_ok(J2, base, net):
                    J, stitched = J2, [{"net": n, "x": round(x, 4), "y": round(y, 4)} for n, x, y, _vv in att]
                    break
                _drop_vias(b, M, att)
                M.pcbnew.SaveBoard(tmp, b)
        meas, reasons = J["meas"], J["reasons"]
        cand_raw, cand = J["cand_raw"], J["cand"]
        res["measure"] = meas
        res["cand_unc_pairs"] = cand["pairs"].get(net, [])
        res["info"] = info
        if stitched:
            res["stitched"] = stitched
        if reasons:
            res["reason"] = " | ".join(reasons)
        else:
            res["accepted"] = True
            if in_session:
                _push_undo(S)
                shutil.copy(tmp, S.work)
                json.dump(cand_raw, open(os.path.join(S.dir, "cur_drc.json"), "w"))
            else:
                shutil.copy(tmp, out)
                s, d = os.path.splitext(src)[0], os.path.splitext(out)[0]
                if os.path.exists(s + ".kicad_pro") and os.path.abspath(s) != os.path.abspath(d):
                    shutil.copy(s + ".kicad_pro", d + ".kicad_pro")
            if op == "shove":
                prev = {frozenset(i.get("uuid") for i in u["items"]) for u in base["pairs"].get(net, [])}
                res["opened"] = [{"a": _item_desc(u["items"][0]), "b": _item_desc(u["items"][1])} for u in cand["pairs"].get(net, [])
                                 if frozenset(i.get("uuid") for i in u["items"]) not in prev]
    finally:
        _drop_tmp(tmp)
    _record(S, res, in_session, out)
    return res


def _push_undo(S):
    m = S.meta()
    n = m["undo"] + 1
    shutil.copy(S.work, os.path.join(S.dir, "undo", f"{n:03d}.kicad_pcb"))
    shutil.copy(os.path.join(S.dir, "cur_drc.json"), os.path.join(S.dir, "undo", f"{n:03d}.drc.json"))
    m["undo"] = n
    S.save_meta(m)


def _record(S, res, in_session, out):
    ent = {k: res.get(k) for k in ("op", "net", "what", "accepted", "reason", "measure")}
    if res.get("stitched"):
        ent["stitched"] = res["stitched"]
    if res.get("args") is not None:
        ent["args"] = res["args"]
    removed = (res.get("info") or {}).get("removed") or []
    removed_locked = [x for x in removed if isinstance(x, dict) and x.get("unlocked_and_removed")] if isinstance(removed, list) else []
    if removed_locked:
        ent["removed_locked"] = removed_locked
        ent["opened"] = res.get("opened") or []
    if in_session:
        S.log(ent)
    elif out:
        Sx = Sess(out)
        os.makedirs(Sx.dir, exist_ok=True)
        Sx.log(ent)


def _outline_sites(contains, bb, ref, step=0.4, per=12):
    """PHASE3H-C: grid points (mm) inside one filled outline, nearest the reference points `ref` first.
    contains(x, y) -> bool; bb = (x0, y0, x1, y1) mm; ref = [(x, y)] (the outline's own pads, or its centre)."""
    x0, y0, x1, y1 = bb
    pts = []
    x = x0 + step / 2
    while x < x1:
        y = y0 + step / 2
        while y < y1:
            if contains(x, y):
                pts.append((min((x - rx) ** 2 + (y - ry) ** 2 for rx, ry in ref), round(x, 4), round(y, 4)))
            y += step
        x += step
    return [(x, y) for _d, x, y in sorted(pts)[:per]]


def _split_sites(b, net, step=0.4, per=12):
    """PHASE3H-C: via sites inside the split-off piece of `net`'s zones: on each layer that now has two or more filled
    outlines of the net, the smallest one (the new island). -> [(x, y)] mm, nearest that island's own pads first."""
    import pcbnew
    from . import pourisland as PI
    out = []
    for lid, outs in PI.outline_index(b, net).items():
        if len(outs) < 2:
            continue
        _area, ps, i = min(outs, key=lambda r: r[0])
        bb = ps.Outline(i).BBox()
        own = [(pcbnew.ToMM(p.GetPosition().x), pcbnew.ToMM(p.GetPosition().y)) for f in b.GetFootprints() for p in f.Pads()
               if p.GetNetname() == net and p.IsOnLayer(lid) and ps.Contains(p.GetPosition(), i)]
        ref = own or [(pcbnew.ToMM(bb.GetCenter().x), pcbnew.ToMM(bb.GetCenter().y))]
        box = (pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop()), pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom()))
        out += _outline_sites(lambda x, y: ps.Contains(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)), i), box, ref, step, per)
    return out


def _pick_pair(sites, j, sep=1.0):
    """PHASE3H-C: attempt j's stitching set for one net: site j, plus one more site at least `sep` mm away (up to 2 vias)."""
    if j >= len(sites):
        return []
    pick = [sites[j]]
    for s in sites:
        if len(pick) == 2:
            break
        if math.hypot(s[0] - pick[0][0], s[1] - pick[0][1]) >= sep:
            pick.append(s)
    return pick


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _via_site_clear(b, M, net, x, y, vd, vdr):
    """PHASE3H-C: a stitching via at (x, y) is legal if the probe against the pre-lay obstacles is clean and no copper of
    another net (track, via) on the in-memory board, including the new lay track, is inside its radius plus clearance."""
    import pcbnew
    if not probe_prims(M, net, [("via", (x, y))], None, (vd, vdr))["ok"]:
        return False
    clr, r = M.net_rule(net)["clearance"], vd / 2
    for t in b.GetTracks():
        if t.GetNetname() == net:
            continue
        if t.GetClass() == "PCB_VIA":
            c = t.GetPosition()
            d = math.hypot(pcbnew.ToMM(c.x) - x, pcbnew.ToMM(c.y) - y)
            if d < r + pcbnew.ToMM(t.GetWidth(pcbnew.F_Cu)) / 2 + clr:
                return False
        elif t.GetClass() == "PCB_TRACK":
            a, e = t.GetStart(), t.GetEnd()
            d = _seg_dist(x, y, pcbnew.ToMM(a.x), pcbnew.ToMM(a.y), pcbnew.ToMM(e.x), pcbnew.ToMM(e.y))
            if d < r + pcbnew.ToMM(t.GetWidth()) / 2 + clr:
                return False
    return True


def _stitch_attempts(b, M, nets, via=None, tries=3):
    """PHASE3H-C: yields attempts [(net, x, y, via_dims)...]: up to 2 legal vias per zone net in its split-off island."""
    import pcbnew
    f = pcbnew.ZONE_FILLER(b)
    zs = pcbnew.ZONES()
    for z in b.Zones():
        zs.append(z)
    f.Fill(zs)
    per = {}
    for n in nets:
        rule = M.net_rule(n)
        vv = via or (rule["via_dia"], rule["via_drill"])
        sites = [(x, y) for x, y in _split_sites(b, n) if _via_site_clear(b, M, n, x, y, *vv)]
        per[n] = (vv, sites)
    for j in range(tries):
        att = []
        for n in nets:
            vv, sites = per[n]
            att += [(n, x, y, vv) for x, y in _pick_pair(sites, j)]
        if att:
            yield att


def lay(board, net, path, out=None, constraints=None, width=None, via=None, any_angle=False, partial=False, kicad_cli="kicad-cli", lock=False,
        stitch=False):
    from . import power as PW
    from . import constraints as CN
    cons = CN.load(constraints) if isinstance(constraints, str) else (constraints or {})
    need = PW.track_width_mm(cons, net)            # PHASE3I B4: a [power] net is laid at its required width, never less
    def mutate(b, M):
        prims = G.parse_path(path, M.cu)
        if net not in {o.net for o in M.obs if o.net}:
            raise ValueError(f"unknown net {net!r}")
        rule = M.net_rule(net)
        w = width or rule["width"]
        if need is not None:
            if width is None:
                w = max(w, need)
            elif width < need - 1e-9:
                raise ValueError(f"width {width:g} mm is below the {need:g} mm that [power.{net}] needs")
        vv = via or (rule["via_dia"], rule["via_drill"])
        pr = probe_prims(M, net, prims, w, vv, any_angle)
        if not pr["ok"]:
            raise ValueError("probe failed: " + json.dumps(pr["violations"][:4]))
        added = _apply_prims(b, M, net, prims, w, vv, lock)
        return {"added": [[a[0], *[_fmtp(x) if isinstance(x, tuple) else x for x in a[1:-1]], a[-1][:8]] for a in added], "width": w, "locked": lock}
    hook = (lambda b, M, nets: _stitch_attempts(b, M, nets, via)) if stitch else None
    args = {"width": width, "via": list(via) if via else None, "any_angle": any_angle, "lock": lock}   # PHASE3I F5 (R12)
    return _edit(board, out, mutate, "lay", net, path, constraints, partial, True, kicad_cli, stitch=hook, args=args)


def _net_copper(b, net):
    return [t for t in b.GetTracks() if t.GetNetname() == net]


def copy_net(src, dst, net, out=None, constraints=None, kicad_cli="kicad-cli", lock=False):
    """Replace the copper of `net` on board `dst` (unlocked tracks/arcs/vias, refused if any is locked) by the copper of the same net NAME
    on board `src`; items are rebuilt explicitly on dst's own net (no Duplicate(): it keeps the source net codes). The copied count must equal
    the source count; then the same DRC + connectivity guard as `lay`. Writes nothing on failure."""
    def mutate(b, M):
        pc = M.pcbnew
        sb = pc.LoadBoard(src)
        items = _net_copper(sb, net)
        if not items:
            raise ValueError(f"source has no copper of net {net!r}")
        dn = b.FindNet(net)
        if dn is None:
            raise ValueError(f"net {net!r} is not on the destination board")
        old = _net_copper(b, net)
        if any(t.IsLocked() for t in old):
            raise ValueError(f"destination has locked copper of net {net!r}: {sum(1 for t in old if t.IsLocked())} item(s); unlock or leave it")
        for t in old:
            b.Remove(t)
        for t in items:
            cls = t.GetClass()
            if cls == "PCB_VIA":
                n = pc.PCB_VIA(b)
                n.SetPosition(t.GetPosition())
                n.SetViaType(t.GetViaType())
                n.SetLayerPair(t.TopLayer(), t.BottomLayer())
                n.SetWidth(t.GetWidth(pc.F_Cu))
                n.SetDrill(t.GetDrill())
            elif cls == "PCB_ARC":
                n = pc.PCB_ARC(b)
                n.SetStart(t.GetStart())
                n.SetMid(t.GetMid())
                n.SetEnd(t.GetEnd())
                n.SetWidth(t.GetWidth())
                n.SetLayer(t.GetLayer())
            else:
                n = pc.PCB_TRACK(b)
                n.SetStart(t.GetStart())
                n.SetEnd(t.GetEnd())
                n.SetWidth(t.GetWidth())
                n.SetLayer(t.GetLayer())
            n.SetNet(dn)
            n.SetLocked(lock or t.IsLocked())
            b.Add(n)
        got = len(_net_copper(b, net))
        if got != len(items):
            raise ValueError(f"copied item count {got} != source count {len(items)}")
        info = {"removed": len(old), "copied": got, "source": src}
        if lock:
            info["locked"] = True
        return info
    return _edit(dst, out, mutate, "copy-net", net, f"from {os.path.basename(src)}", constraints, False, False, kicad_cli)


def _lock_drc_unchanged(cand, base):
    violation_keys = lambda s: Counter({k: v for k, v in s["keys"].items() if not k[0].startswith("U")})  # noqa: E731
    if violation_keys(cand) != violation_keys(base):
        return "DRC keys changed"
    if cand["unc_by_net"] != base["unc_by_net"]:
        return "open connections by net changed"
    return None


def lock_net(board, net, out=None, ids=None, unlock=False, constraints=None, kicad_cli="kicad-cli"):
    """Lock or unlock all (or explicitly selected) tracks/vias of a net without changing geometry, DRC errors or opens per net."""
    def mutate(b, M):
        items = _net_copper(b, net)
        if not items:
            raise ValueError(f"no tracks/vias of net {net!r}")
        selected = items
        if ids:
            selected = []
            for ident in ids:
                hits = [t for t in items if t.m_Uuid.AsString().startswith(ident)]
                if not hits:
                    raise ValueError(f"no track/via of net {net} with id {ident}")
                if len(hits) > 1:
                    raise ValueError(f"ambiguous id {ident}: use a longer UUID prefix")
                selected.extend(hits)
        changed = []
        for t in selected:
            if t.IsLocked() == unlock:
                uid = t.m_Uuid.AsString()
                t.SetLocked(not unlock)
                changed.append({"id": _sid(uid), "uuid": uid, "kind": "via" if t.GetClass() == "PCB_VIA" else "track",
                                "locked": not unlock})
        return {"selected": len(selected), "changed": changed, "locked": not unlock}

    what = f"ids={ids} unlock={unlock}"
    return _edit(board, out, mutate, "lock-net", net, what, constraints, False, False, kicad_cli,
                 lambda M, cand, base: _lock_drc_unchanged(cand, base), refill=False)


def _chain(M, seed):
    """Segments connected to `seed` through plain same-layer joints (degree 2, no pad / via / branch)."""
    tracks = [o for o in M.obs if o.kind == "track" and o.net == seed.net and o.layers == seed.layers and not o.extra.get("arc")]
    hold = {o.id: o for o in tracks}
    stops = [o for o in M.obs if o.net == seed.net and o.kind in ("pad", "via")]

    def ends(o):
        return (o.g[1], o.g[2])

    def at(p):
        return [t for t in tracks if any(math.hypot(p[0] - q[0], p[1] - q[1]) < 2e-3 for q in ends(t))]

    def stop_at(p):
        return any(G.ob_gap(p, p, 0.0, s)[0] <= 1e-3 for s in stops if seed.layers & s.layers)
    chain, todo = {seed.id: seed}, [seed]
    while todo:
        cur = todo.pop()
        for p in ends(cur):
            if stop_at(p):
                continue
            nb = [t for t in at(p) if t.id != cur.id]
            if len(nb) == 1 and nb[0].id not in chain and len(at(p)) == 2:
                chain[nb[0].id] = nb[0]
                todo.append(nb[0])
    return list(chain.values())


def _removed_track_record(t, o):
    uid = t.m_Uuid.AsString()
    cls = t.GetClass()
    rec = {"kind": o.kind, "id": _sid(uid), "uuid": uid, "net": t.GetNetname(),
           "layer": next(iter(o.layers)) if o.kind == "track" else "via",
           "a": _fmtp(o.g[1]), "b": _fmtp(o.g[2]) if o.kind == "track" else None,
           "was_locked": t.IsLocked(), "lock_origin": "board" if t.IsLocked() else None}
    if cls == "PCB_VIA":
        geometry = {"type": "via", "x": t.GetPosition().x, "y": t.GetPosition().y, "width": t.GetWidth(t.TopLayer()),
                    "drill": t.GetDrill(), "via_type": int(t.GetViaType()), "top_layer": t.TopLayer(), "bottom_layer": t.BottomLayer()}
    else:
        geometry = {"type": "arc" if cls == "PCB_ARC" else "track", "start": [t.GetStart().x, t.GetStart().y],
                    "end": [t.GetEnd().x, t.GetEnd().y], "width": t.GetWidth(), "layer": t.GetLayer()}
        if cls == "PCB_ARC":
            geometry["mid"] = [t.GetMid().x, t.GetMid().y]
    rec["geometry"] = geometry
    if t.IsLocked():
        rec["unlocked_and_removed"] = True
    return rec


_LAY_ROW = re.compile(r"^#\d+ .*\blay +ACCEPT net=(\S+)\s+(.*)$")


def accepted_lays(ledger):
    """PHASE3I B2: the ACCEPT `lay` rows of a session ledger.txt -> [(net, path, stitched, args)] in ledger order.
    A row's `    stitched:` line marks it stitched; its `    args:` line (PHASE3I F5) holds the lay arguments ({} for an
    older ledger). The path is the row's `what` text, exactly as lay was given it."""
    out = []
    for line in open(ledger, errors="replace"):
        line = line.rstrip("\n")
        m = _LAY_ROW.match(line)
        if m:
            out.append([m.group(1), m.group(2).strip(), False, {}])
        elif line.startswith("    stitched:") and out:
            out[-1][2] = True
        elif line.startswith("    args:") and out:
            out[-1][3] = json.loads(line[len("    args:"):])
    return [tuple(r) for r in out]


def _merge_current(dst):
    """the highest m<N>.kicad_pcb of the merge dir `dst` -> (N, path)."""
    ns = [int(m.group(1)) for f in os.listdir(dst) for m in [re.match(r"^m(\d+)\.kicad_pcb$", f)] if m]
    if not ns:
        raise ValueError(f"merge dir {dst} holds no m<N>.kicad_pcb")
    n = max(ns)
    return n, os.path.join(dst, f"m{n}.kicad_pcb")


def replay(ledger, dst, constraints=None, kicad_cli="kicad-cli", net_rx=None):
    """PHASE3I B2 (port of W14/scripts/replay-lays.sh): re-apply the ACCEPT lay rows of a session `ledger` onto the merge
    dir `dst` (the highest m<N>.kicad_pcb is current; each applied lay writes m<N+1>). Every lay goes through lay's guard
    again; a refused row is requeued, never forced. net_rx: optional regex on the net name. -> {applied, requeue, board}."""
    from . import constraints as CN
    cons = CN.load(constraints) if isinstance(constraints, str) else constraints
    n, cur = _merge_current(dst)
    before, applied, requeue, mine = cur, [], [], []
    for net, path, stitched, args in accepted_lays(ledger):
        if net_rx and not re.search(net_rx, net):
            continue
        out = os.path.join(dst, f"m{n + 1}.kicad_pcb")
        kw = dict(args or {})
        if isinstance(kw.get("via"), list):
            kw["via"] = tuple(kw["via"])
        try:
            res = lay(cur, net, path, out=out, constraints=cons, kicad_cli=kicad_cli, stitch=stitched, **kw)
            ok, why = bool(res.get("accepted")), res.get("reason", "")
        except (ValueError, RuntimeError) as e:
            ok, why = False, str(e)[:200]
        if ok and os.path.exists(out):
            applied.append(net)
            mine.append((net, path))
            n, cur = n + 1, out
        else:
            requeue.append({"net": net, "path": path, "why": why or "refused"})
    if mine and cur != before:                     # PHASE3I F5 (R6): this ledger judged once against the board before it
        why = _r6_refusal(before, cur, cons, kicad_cli)
        if why:                                    # refused: the board goes back to before this ledger, every row requeued
            for net, path in mine:
                requeue.append({"net": net, "path": path, "why": why})
            del applied[len(applied) - len(mine):]
            cur = before
    return {"applied": applied, "requeue": requeue, "board": cur}


def _r6_refusal(base, cand, cons, kicad_cli="kicad-cli", protect=()):
    """PHASE3I F5 (R6, shared by session_commit and replay): the close guard's R6 test of `cand` against `base`, on copies
    (the two boards are never refilled or saved). -> the refusal reason, or None when the candidate is not worse."""
    from . import gaps as GP
    tmp = tempfile.mkdtemp(prefix="r6-", dir=os.path.dirname(os.path.abspath(cand)))
    try:
        b0, c0 = os.path.join(tmp, "base.kicad_pcb"), os.path.join(tmp, "cand.kicad_pcb")
        _copy_board(base, b0)
        _copy_board(cand, c0)
        base_m = GP._remeasure(b0, kicad_cli)
        GP._classify_into(base_m, cons or {}, b0)
        cand_m = GP._remeasure(c0, kicad_cli)
        GP._classify_into(cand_m, cons or {}, c0)
        ok6, why6 = GP._accept(base_m, b0, cand_m, c0, [], cand_m["gaps"], None, protect)
        return None if ok6 else why6
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def merge_sessions(ledgers, dst, constraints=None, kicad_cli="kicad-cli"):
    """PHASE3I B2: replay each region session's ledger onto one merge dir, in the order given. -> {applied, requeue, board}."""
    applied, requeue, board = [], [], None
    for led in ledgers:
        r = replay(led, dst, constraints=constraints, kicad_cli=kicad_cli)
        applied += r["applied"]
        requeue += r["requeue"]
        board = r["board"]
    return {"applied": applied, "requeue": requeue, "board": board}


def shove(board, net, out=None, ids=None, window_box=None, chain=False, constraints=None, kicad_cli="kicad-cli", op="shove", allow_locked=False):
    """Remove copper of `net`; locked items need an explicit, session-only opt-in and are logged with full geometry."""
    def mutate(b, M):
        if allow_locked and (out is not None or not ids or window_box):
            raise ValueError("--allow-locked requires a session and explicit --ids (no --window or --out)")
        sel, named = {}, set()
        if ids:
            for i in ids:
                hit = [o for o in _resolve(M, i) if o.kind in ("track", "via")]
                if not hit:
                    raise ValueError(f"no track/via with id {i}")
                for o in hit:
                    if o.net != net:
                        raise ValueError(f"{i} belongs to net {o.net}, not {net}")
                    sel[o.id] = o
                    named.add(o.id)
        if window_box:
            x, y, w, h = window_box
            for o in M.in_box((x, y, x + w, y + h)):
                if o.net == net and o.kind in ("track", "via"):
                    sel[o.id] = o
        if chain:
            for o in list(sel.values()):
                if o.kind == "track":
                    for c in _chain(M, o):
                        sel[c.id] = c
        if not sel:
            raise ValueError("nothing selected: no copper of that net matched")
        locked = [o for o in sel.values() if o.locked]
        unnamed = [o for o in locked if o.id not in named]
        if allow_locked and unnamed:
            raise ValueError("every locked item must be explicitly named: " + ", ".join(_sid(o.id) for o in unnamed))
        # PHASE3I B5: the lock policy follows the net's class. Hard (gated pair legs, RF) needs the explicit opt-in;
        # soft and free locked copper may be ripped by the ordinary edit guard (the caller re-lays soft copper).
        from . import constraints as CN_lp, lockpolicy as LP
        cons_lp = CN_lp.load(constraints) if isinstance(constraints, str) else (constraints or {})
        hard = [o for o in locked if o.net in LP.hard_locked({o.net}, cons_lp, {o.net for o in M.obs if o.net})]
        if hard and not allow_locked:
            raise ValueError("refused, hard-locked copper selected (high-speed pair or RF): " + ", ".join(f"{o.kind} {_sid(o.id)} at {_fmtp(o.g[1])}" for o in hard[:6]))
        removed = []
        for t in list(b.GetTracks()):
            if t.m_Uuid.AsString() in sel:
                rec = _removed_track_record(t, sel[t.m_Uuid.AsString()])
                removed.append(rec)
                if rec["was_locked"]:
                    t.SetLocked(False)
                b.Delete(t)
        return {"removed": removed}
    what = f"ids={ids} window={window_box} chain={chain} allow_locked={allow_locked}"
    return _edit(board, out, mutate, op, net, what, constraints, False, False, kicad_cli)


def session_undo(work):
    S = Sess(work)
    m = S.meta()
    if m["undo"] <= 0:
        return {"undone": False, "reason": "nothing to undo"}
    n = m["undo"]
    # snapshot n holds the board as it was BEFORE edit n
    prev = os.path.join(S.dir, "undo", f"{n:03d}.kicad_pcb")
    prevd = os.path.join(S.dir, "undo", f"{n:03d}.drc.json")
    shutil.copy(prev, S.work)
    shutil.copy(prevd, os.path.join(S.dir, "cur_drc.json"))
    for f in (f"{n:03d}.kicad_pcb", f"{n:03d}.drc.json"):
        os.unlink(os.path.join(S.dir, "undo", f))
    m["undo"] = n - 1
    S.save_meta(m)
    S.log({"op": "undo", "accepted": True, "what": f"edit {n} reverted", "measure": {}})
    return {"undone": True, "edits_left": n - 1}


def session_status(work):
    S = Sess(work)
    m = S.meta()
    st = summarize_drc(json.load(open(os.path.join(S.dir, "start_drc.json"))))
    cu = summarize_drc(json.load(open(os.path.join(S.dir, "cur_drc.json"))))
    L = json.load(open(os.path.join(S.dir, "ledger.json"))) if os.path.exists(os.path.join(S.dir, "ledger.json")) else []
    return {"work": S.work, "edits_applied": m["undo"], "attempts": len(L), "rejected": sum(1 for e in L if not e.get("accepted")),
            "start": {"unconnected": st["unconnected"], "errors": st["errors"], "by_net": st["unc_by_net"]},
            "now": {"unconnected": cu["unconnected"], "errors": cu["errors"], "by_net": cu["unc_by_net"]},
            "ledger": os.path.join(S.dir, "ledger.txt")}


def _active_locked_reescapes(S):
    path = os.path.join(S.dir, "ledger.json")
    ledger = json.load(open(path)) if os.path.exists(path) else []
    active, edit = {}, 0
    for ent in ledger:
        op = ent.get("op")
        if op == "undo" and ent.get("accepted"):
            m = re.search(r"edit (\d+) reverted", ent.get("what", ""))
            if m:
                active.pop(int(m.group(1)), None)
            continue
        if op == "commit" or not ent.get("accepted"):
            continue
        edit += 1
        active[edit] = ent
    return [ent for ent in active.values() if ent.get("removed_locked")]


def _open_pair_key(items):
    return tuple(sorted(i.get("uuid", "") for i in items))


def session_commit(work, out, constraints=None, kicad_cli="kicad-cli", mode="repair"):
    """Guard the whole session, including closure and relocking of every active locked-copper re-escape.
    PHASE3H B1: mode "repair" (default) also judges R2/R4 (DRC keys, score); mode "edit" judges only R0/R1/R3/R5 and
    the locked checks, and leaves R2/R4/R6 to the caller (close's _accept)."""
    S = Sess(work)
    start = os.path.join(S.dir, "start.kicad_pcb")
    sraw = json.load(open(os.path.join(S.dir, "start_drc.json")))
    wraw = drc_raw(S.work, kicad_cli)
    a, b = summarize_drc(sraw), summarize_drc(wraw)
    reasons = []
    from . import drcguard as DG
    from . import constraints as CN
    cons = CN.load(constraints) if isinstance(constraints, str) else (constraints or {})
    protect = DG.protected_nets(cons, DG.snap_nets(_snap(start)))
    ok, why = DG.accept(DG.measure_from_report(sraw, severity="all"), DG.measure_from_report(wraw, severity="all"),
                        mode, base_board=start, cand_board=S.work, snap=_snap, protect=protect)
    if not ok:
        reasons.append(why)
    elif mode == "repair":                          # PHASE3I B4: R6 (hs_pair/rf/power/power_mag) judged as close judges it
        why6 = _r6_refusal(start, S.work, cons, kicad_cli, protect)     # PHASE3I F5 (R11): on copies
        if why6:
            reasons.append(why6)
    Ma, Mb = G.Model(start), G.Model(S.work)
    reescapes = _active_locked_reescapes(S)
    reescape_nets = {x["net"] for ent in reescapes for x in ent["removed_locked"]}
    original_locked = {sig for sig in _locked_sig(Ma) if sig[1] not in reescape_nets}
    if not original_locked.issubset(_copper_sig(Mb)):
        reasons.append("locked copper geometry changed")
    current_pairs = {(net, _open_pair_key(u["items"])) for net, us in b["pairs"].items() for u in us}
    unresolved = set()
    for ent in reescapes:
        for opened in ent.get("opened", []):
            key = _open_pair_key([opened["a"], opened["b"]])
            if (ent["net"], key) in current_pairs:
                unresolved.add(ent["net"])
    if unresolved:
        reasons.append("locked re-escape left opened connection(s): " + ", ".join(sorted(unresolved)))
    unlocked = {o.net for o in Mb.obs if o.net in reescape_nets and o.kind in ("track", "via") and not o.locked}
    if unlocked:
        reasons.append("locked re-escape has restored/re-laid copper still unlocked: " + ", ".join(sorted(unlocked)))
    unrestored = {net for net in reescape_nets if not any(o.net == net and o.kind in ("track", "via") for o in Mb.obs)}
    if unrestored:
        reasons.append("locked re-escape has no restored copper: " + ", ".join(sorted(unrestored)))
    from . import audit as AU
    d = AU.connectivity_diff(_snap(start), _snap(S.work))
    if not d["ok"]:
        reasons.append("connectivity guard: " + "; ".join(d["violations"][:3]))
    res = {"accepted": not reasons, "reasons": reasons, "locked_reescape": sorted(reescape_nets),
           "start": {"unconnected": a["unconnected"], "errors": a["errors"], "by_net": a["unc_by_net"]},
           "work": {"unconnected": b["unconnected"], "errors": b["errors"], "by_net": b["unc_by_net"]}, "out": None, "ledger": os.path.join(S.dir, "ledger.txt")}
    if not reasons:
        _copy_board(S.work, out)
        res["out"] = out
    S.log({"op": "commit", "accepted": not reasons, "reason": "; ".join(reasons) or None, "what": f"-> {out}",
           "measure": {"unconnected": f"{a['unconnected']}->{b['unconnected']}", "errors_after": _fmt_errs(b["errors"])}})
    return res


# ======================================================================================================= suggest
def _octi(a, v):
    """Two octilinear dog-legs a -> q -> v (diagonal first / straight first); [] when already octilinear."""
    dx, dy = v[0] - a[0], v[1] - a[1]
    ang = math.degrees(math.atan2(dy, dx)) % 45
    if min(ang, 45 - ang) < 0.01 or math.hypot(dx, dy) < 0.1:
        return [[a, v]]
    m = min(abs(dx), abs(dy))
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    q1 = (a[0] + sx * m, a[1] + sy * m)
    q2 = (v[0] - sx * m, v[1] - sy * m)
    return [[a, q1, v], [a, q2, v]]


def _render_prims(prs):
    parts, cur = [], None
    for p in prs:
        if p[0] == "via":
            parts.append(f"via {p[1][0]},{p[1][1]}")
            cur = None
            continue
        if cur is None or cur[0] != p[1] or cur[1][-1] != p[2]:
            cur = [p[1], [p[2]]]
            parts.append(cur)
        cur[1].append(p[3])
    return "; ".join(x if isinstance(x, str) else f"{x[0]}: " + " ".join(f"{q[0]:.4f},{q[1]:.4f}" for q in x[1]) for x in parts)


def _rev_prims(prims):
    return [("seg", p[1], p[3], p[2]) if p[0] == "seg" else p for p in reversed(prims)]


def _island_target(M, net, items, pt, lay):
    """Where a path end at `pt` on `lay` should finish on its island: None when it already touches a track (or there is nothing on `lay`),
    else the centre of the pad / via it is in or nearest to (nearest point of a track when the end is off the island)."""
    best = None
    for o in items:
        if o.net != net or o.kind not in ("pad", "via", "track") or lay not in o.layers:
            continue
        gap, near = G.ob_gap(pt, pt, 0.0, o)
        if best is None or gap < best[0]:
            best = (gap, o, near)
    if best is None:
        return None
    gap, o, near = best
    if o.kind == "via":
        return o.g[1]
    if o.kind == "pad":
        return ((o.bb[0] + o.bb[2]) / 2, (o.bb[1] + o.bb[3]) / 2)
    return None if gap <= 1e-3 else near


def _tail_options(M, net, items, prims, layer):
    """Variants of `prims` whose last end is carried on to its island (via, then pad-layer segment, then to the pad centre in 45/90
    segments); the unextended prims come last."""
    last = prims[-1]
    pt, lay = (last[3], last[1]) if last[0] == "seg" else (last[1], layer)
    tgt = _island_target(M, net, items, pt, lay)
    out = []
    if tgt is not None and math.hypot(pt[0] - tgt[0], pt[1] - tgt[1]) > 5e-4:
        for pts in _octi(pt, tgt):
            out.append(list(prims) + [("seg", lay, pts[k], pts[k + 1]) for k in range(len(pts) - 1)])
    out.append(list(prims))
    return out


def _snap_ends(M, net, pathstr):
    """A path end that lands inside an own-net via is moved onto its centre (KiCad: track_not_centered_on_via), with an octilinear
    dog-leg if the last segment is no longer 45/90. -> candidate path strings (probe each)."""
    prims = G.parse_path(pathstr, M.cu)
    segs = [k for k, p in enumerate(prims) if p[0] == "seg"]
    if not segs:
        return [pathstr]
    vias = [o for o in M.obs if o.kind == "via" and o.net == net]

    def snap(pt):
        for o in vias:
            if math.hypot(pt[0] - o.g[1][0], pt[1] - o.g[1][1]) < o.g[3]:
                return o.g[1]
        return pt

    f, l = segs[0], segs[-1]
    last = prims[l]
    v = snap(last[3])
    cands = []
    for pts in _octi(last[2], v):
        pr = list(prims[:l]) + [("seg", last[1], pts[k], pts[k + 1]) for k in range(len(pts) - 1)] + list(prims[l + 1:])
        first = pr[f]
        pr[f] = ("seg", first[1], snap(first[2]), first[3])
        cands.append(_render_prims(pr))
    return cands


def _seal_report(M, net, reached, lays, free, xs, ys, w, pit, viaok):
    """The measured reason a search stalled: region reached per layer and the foreign obstacles that bound it (most frequent first)."""
    import numpy as np
    per = {}
    for (k, iy, ix, d) in reached:
        r = per.setdefault(lays[k], [1e9, 1e9, -1e9, -1e9, 0])
        x, y = float(xs[ix]), float(ys[iy])
        r[0], r[1], r[2], r[3], r[4] = min(r[0], x), min(r[1], y), max(r[2], x), max(r[3], y), r[4] + 1
    H, W = free[lays[0]].shape
    seen = set()
    border = []
    for (k, iy, ix, d) in reached:
        if (k, iy, ix) in seen:
            continue
        seen.add((k, iy, ix))
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            jy, jx = iy + dy, ix + dx
            if 0 <= jy < H and 0 <= jx < W and not free[lays[k]][jy, jx]:
                border.append((k, jy, jx))
    step = max(1, len(border) // 400)
    cnt = {}
    for k, jy, jx in border[::step]:
        pt = (float(xs[jx]), float(ys[jy]))
        best = None
        for o in M.in_box((pt[0] - 0.8, pt[1] - 0.8, pt[0] + 0.8, pt[1] + 0.8), lays[k]):
            if not M.foreign(o, net, "track") or o.kind == "hole":
                continue
            gap, _ = G.ob_gap(pt, pt, w / 2, o)
            m = gap - (0.0 if o.kind == "ko" else o.clr if o.kind == "edge" else M.need(net, o))
            if best is None or m < best[0]:
                best = (m, o)
        if best:
            o = best[1]
            e = cnt.setdefault(o.id, {"item": o.ref or f"{o.kind} {o.net}", "kind": o.kind, "net": o.net, "id": _sid(o.id), "uuid": o.id,
                                      "layer": lays[k], "locked": o.locked, "border_cells": 0})
            e["border_cells"] += 1
    return {"reached_cells": {l: {"bbox": [round(v, 2) for v in r[:4]], "cells": r[4]} for l, r in per.items()},
            "limiting_obstacles": sorted(cnt.values(), key=lambda e: -e["border_cells"])[:12], "via_sites_in_window": int(viaok.sum())}


def _endpoints_join(pr, ia, ib):
    joins = pr.get("ends_join") or []
    if len(joins) != 2 or any(not j.get("touches") for j in joins):
        return False
    return ((ia is None or joins[0]["touches"].get("island") == ia)
            and (ib is None or joins[-1]["touches"].get("island") == ib))


def _auto_endpoints(M, net, isl):
    """Closest pair of distinct same-net islands -> (ia, ib, point_a, point_b), or Nones when no pair exists."""
    best = None
    for i in range(len(isl)):
        for j in range(i + 1, len(isl)):
            d = _island_dist(M, net, i, j)
            if d is not None and (best is None or d[0] < best[0]):
                best = (d[0], i, j, d[1], d[2])
    return best[1:] if best else (None, None, None, None)


def suggest(board, net, src_pt=None, dst_pt=None, conn=None, constraints=None, width=None, via=None, pitch=0.05, margin=2.0,
            via_cost=2.0, layers=None, max_nodes=2500000, any_angle=False, M=None, raw=None, avoid=None, relax=False, relax_cost=1.5, skip_ids=(), explore=False,
            include_locked=False):
    """Grid A* between two islands of `net`: sources = the island of the item nearest src_pt, targets = the island nearest dst_pt (or the
    connection's two islands). A proposal as a path string, already probed; reports the measured reason when none exists."""
    import numpy as np
    M = M or _model(board, constraints)
    if conn is not None:
        R = open_nets(board, constraints, raw=raw, M=M)
        rec = R["connections"][int(conn)]
        net = rec["net"]
    rule = M.net_rule(net)
    w = width or rule["width"]
    vd, vdr = via or (rule["via_dia"], rule["via_drill"])
    isl, omap = M.islands(net)
    if conn is not None:
        ia, ib = rec["a"].get("island"), rec["b"].get("island")
        pa = rec["closest"]["a"] if rec.get("closest") else (rec["a"]["x"], rec["a"]["y"])
        pb = rec["closest"]["b"] if rec.get("closest") else (rec["b"]["x"], rec["b"]["y"])
    elif src_pt is None and dst_pt is None:
        ia, ib, pa, pb = _auto_endpoints(M, net, isl)
    elif src_pt is None or dst_pt is None:
        return {"found": False, "reason": "both --from and --to are required when either endpoint is given"}
    else:
        def nearest(pt):
            best = min(((G.ob_gap(pt, pt, 0.0, o)[0], o) for o in M.obs if o.net == net and o.kind in ("pad", "track", "via")), key=lambda t: t[0])
            return omap.get(best[1].id)
        ia, ib = nearest(src_pt), nearest(dst_pt)
        pa, pb = src_pt, dst_pt
    if ia is None or ib is None or ia == ib:
        return {"found": False, "reason": f"endpoints are not two separate islands (islands {ia}, {ib})"}
    lays = [G.norm_layer(l, M.cu) for l in layers] if layers else list(M.cu)
    bb = (min(pa[0], pb[0]) - margin, min(pa[1], pb[1]) - margin, max(pa[0], pb[0]) + margin, max(pa[1], pb[1]) + margin)
    last, ep_fail = {"violations": []}, False
    for attempt, extra in enumerate((0.0, pitch * 0.5, pitch * 1.0)):
        sk = (lambda o: o.id in skip_ids) if skip_ids else None
        xs, ys, pit, st, sv, sh = _free_fields(M, net, lays, bb, w, pitch, skip=sk)
        H, Wd = len(ys), len(xs)
        free = {l: st[l] >= w / 2 + extra - G.EPS for l in lays}
        soft = {}
        if relax:
            _, _, _, st_h, sv_h, sh_h = _free_fields(M, net, lays, bb, w, pitch, skip=lambda o: (o.kind in ("track", "via") and not o.locked and o.net != net) or o.id in skip_ids)
            hard = {l: st_h[l] >= w / 2 + extra - G.EPS for l in lays}
            soft = {l: hard[l] & ~free[l] for l in lays}
            free = hard
            viaok_strict = (sv >= vd / 2 + extra - G.EPS) & (sh >= vdr / 2 - G.EPS)
            sv, sh = sv_h, sh_h
        if avoid:
            for (ax, ay, ar) in avoid:
                X, Y = np.meshgrid(xs, ys)
                for l in lays:
                    free[l] &= (np.hypot(X - ax, Y - ay) > ar)
        viaok = (sv >= vd / 2 + extra - G.EPS) & (sh >= vdr / 2 - G.EPS)

        def cells_of(index):
            res = {l: np.zeros((H, Wd), bool) for l in lays}
            X, Y = np.meshgrid(xs, ys)
            for o in isl[index]["items"]:
                for l in lays:
                    if l in o.layers:
                        res[l] |= (G.ob_dist_np(X, Y, o) <= (-(o.g[3] - pit * 1.2) if o.kind == "via" else 0.0))
            for zi, l, fi in isl[index]["zones"]:
                if l in res:
                    fp = M.zones[zi]["fills"][l][fi]
                    bx0, by0, bx1, by1 = fp["bb"]
                    if bx1 >= xs[0] and bx0 <= xs[-1] and by1 >= ys[0] and by0 <= ys[-1]:
                        res[l] |= G.in_fill(np.stack([X.ravel(), Y.ravel()], 1), fp).reshape(X.shape)
            return res
        S0, T0 = cells_of(ia), cells_of(ib)
        for l in lays:
            S0[l] &= free[l]
            T0[l] &= free[l]
        if not any(S0[l].any() for l in lays) or not any(T0[l].any() for l in lays):
            return {"found": False, "reason": "an endpoint island has no free grid cell at this track width/clearance (pad sealed in at this pitch: try --pitch smaller or --width smaller)",
                    "source_free_cells": {l: int(S0[l].sum()) for l in lays}, "target_free_cells": {l: int(T0[l].sum()) for l in lays}}
        tx = np.argwhere(np.stack([T0[l] for l in lays]).any(0))
        tgt_c = (float(xs[tx[:, 1]].mean()), float(ys[tx[:, 0]].mean()))
        dirs = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0), (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142)]
        li = {l: k for k, l in enumerate(lays)}
        dist, par = {}, {}
        pq = []
        for l in lays:
            for iy, ix in np.argwhere(S0[l]):
                n = (li[l], int(iy), int(ix), 8)
                dist[n] = 0.0
                heapq.heappush(pq, (math.hypot(xs[ix] - tgt_c[0], ys[iy] - tgt_c[1]), 0.0, n))
        goal = None
        pops = 0
        while pq:
            f, g, n = heapq.heappop(pq)
            if g > dist.get(n, 1e18) + 1e-12:
                continue
            pops += 1
            if pops > max_nodes:
                break
            k, iy, ix, d = n
            lay = lays[k]
            if T0[lay][iy, ix]:
                goal = n
                break
            for di, (dx, dy, c) in enumerate(dirs):
                jy, jx = iy + dy, ix + dx
                if 0 <= jy < H and 0 <= jx < Wd and free[lay][jy, jx]:
                    if dx and dy and not (free[lay][iy, jx] and free[lay][jy, ix]):
                        continue
                    pen = (0.0 if (d == 8 or d == di) else 0.15) + (relax_cost if soft and soft[lay][jy, jx] else 0.0)
                    m = (k, jy, jx, di)
                    ng = g + c * pit + pen
                    if ng < dist.get(m, 1e18) - 1e-12:
                        dist[m] = ng
                        par[m] = n
                        heapq.heappush(pq, (ng + math.hypot(xs[jx] - tgt_c[0], ys[jy] - tgt_c[1]), ng, m))
            if viaok[iy, ix] and (d != 9):
                for k2, l2 in enumerate(lays):
                    if k2 != k and free[l2][iy, ix]:
                        m = (k2, iy, ix, 9)
                        ng = g + via_cost + (6 * relax_cost if relax and not viaok_strict[iy, ix] else 0.0)
                        if ng < dist.get(m, 1e18) - 1e-12:
                            dist[m] = ng
                            par[m] = n
                            heapq.heappush(pq, (ng + math.hypot(xs[ix] - tgt_c[0], ys[iy] - tgt_c[1]), ng, m))
        if goal is None:
            plain = {"found": False, "reason": f"no grid path at width {w} (searched {pops} nodes, pitch {pit}, window {[round(v, 2) for v in bb]}, layers {lays})",
                     "width": w, "pitch": pit, "window": [round(v, 3) for v in bb], "sealed_by": _seal_report(M, net, dist, lays, free, xs, ys, w, pit, viaok)}
            if not explore:
                return plain
            if explore:
                res = {"found": False, "reason": f"no grid path at width {w}", "sealed_by": _seal_report(M, net, dist, lays, free, xs, ys, w, pit, viaok), "explore": []}

                def removal_item(o):
                    item = {"net": o.net, "id": _sid(o.id), "uuid": o.id, "kind": o.kind, "locked": o.locked}
                    if o.locked:
                        item["lock_origin"] = "board"
                    return item

                units, seen_units = [], set()
                for e in res["sealed_by"]["limiting_obstacles"]:
                    if e["kind"] not in ("track", "via"):
                        continue
                    ob = next(o for o in M.obs if o.id == e["uuid"])
                    copper = _chain(M, ob) if ob.kind == "track" else [ob]
                    ids = frozenset(o.id for o in copper)
                    if ids not in seen_units:
                        seen_units.add(ids)
                        units.append({"ids": ids, "remove": [removal_item(o) for o in sorted(copper, key=lambda o: o.id)]})
                units = units[:6]
                unlocked = [u for u in units if not any(x["locked"] for x in u["remove"])]
                combos = [([u], u["ids"], u["remove"]) for u in unlocked]
                for i in range(len(unlocked)):
                    for j in range(i + 1, len(unlocked)):
                        us = [unlocked[i], unlocked[j]]
                        items = {x["uuid"]: x for u in us for x in u["remove"]}
                        combos.append((us, unlocked[i]["ids"] | unlocked[j]["ids"], [items[k] for k in sorted(items)]))
                for us, ids, items in combos:
                    r = suggest(board, net, src_pt, dst_pt, conn, constraints, width, via, pitch, margin, via_cost, layers, max_nodes, any_angle, M=M, raw=raw,
                                avoid=avoid, relax=False, skip_ids=tuple(set(skip_ids) | ids), explore=False)
                    res["explore"].append({"remove": items, "then_found": bool(r.get("found")),
                                           "path": r.get("path"), "length_mm": r.get("length_mm")})
                    if r.get("found") and len(us) == 1:
                        break
                if include_locked:
                    res["locked_explore"] = []
                    for u in [u for u in units if any(x["locked"] for x in u["remove"])]:
                        r = suggest(board, net, src_pt, dst_pt, conn, constraints, width, via, pitch, margin, via_cost, layers, max_nodes, any_angle, M=M, raw=raw,
                                    avoid=avoid, relax=False, skip_ids=tuple(set(skip_ids) | u["ids"]), explore=False)
                        res["locked_explore"].append({"remove": u["remove"], "then_found": bool(r.get("found")),
                                                      "path": r.get("path"), "length_mm": r.get("length_mm")})
                res["reason"] = plain["reason"]
                return res
        seq = [goal]
        while seq[-1] in par:
            seq.append(par[seq[-1]])
        seq.reverse()
        blockers = {}
        if relax:
            for (k, iy, ix, d) in seq:
                if d == 9 and not viaok_strict[iy, ix]:
                    pt = (float(xs[ix]), float(ys[iy]))
                    for lay in M.cu:
                        for o in M.in_box((pt[0] - vd, pt[1] - vd, pt[0] + vd, pt[1] + vd), lay):
                            if o.kind in ("track", "via") and not o.locked and o.net != net:
                                gap, _ = G.ob_gap(pt, pt, vd / 2, o)
                                if gap - M.need(net, o) < -G.EPS or gap + (vd / 2 - vdr / 2) - M.hole_clr < -G.EPS:
                                    b = blockers.setdefault(o.id, {"id": _sid(o.id), "net": o.net, "kind": o.kind, "layer": next(iter(o.layers)) if o.kind == "track" else "via",
                                                                   "at": _fmtp(pt), "cells": 0, "blocks": "via"})
                                    b["cells"] += 1
                if soft[lays[k]][iy, ix]:
                    pt = (float(xs[ix]), float(ys[iy]))
                    for o in M.in_box((pt[0] - w - 0.5, pt[1] - w - 0.5, pt[0] + w + 0.5, pt[1] + w + 0.5), lays[k]):
                        if o.kind in ("track", "via") and not o.locked and o.net != net:
                            gap, _ = G.ob_gap(pt, pt, w / 2, o)
                            if gap - M.need(net, o) < -G.EPS:
                                b = blockers.setdefault(o.id, {"id": _sid(o.id), "net": o.net, "kind": o.kind, "layer": next(iter(o.layers)) if o.kind == "track" else "via",
                                                               "at": _fmtp(pt), "cells": 0})
                                b["cells"] += 1
        # to path string: chunks per layer, vias between; collapse collinear
        chunks = []
        for n in seq:
            k, iy, ix, d = n
            pt = (round(float(xs[ix]), 4), round(float(ys[iy]), 4))
            if not chunks or chunks[-1][0] != lays[k]:
                if chunks and chunks[-1][1][-1] != pt:
                    pass
                chunks.append([lays[k], [pt]])
            elif chunks[-1][1][-1] != pt:
                chunks[-1][1].append(pt)

        def simplify(pts):
            out = [pts[0]]
            for i in range(1, len(pts) - 1):
                a, b, c = out[-1], pts[i], pts[i + 1]
                if abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) > 1e-9:
                    out.append(b)
            out.append(pts[-1])
            return out
        prims_g = []
        for i, (l, pts) in enumerate(chunks):
            if i:
                prims_g.append(("via", pts[0]))
            pts = simplify(pts) if len(pts) > 1 else pts
            for q in range(len(pts) - 1):
                prims_g.append(("seg", l, pts[q], pts[q + 1]))
        pathstr = _render_prims(prims_g)
        if relax:
            return {"found": True, "needs_shove": bool(blockers), "path": pathstr, "blockers": sorted(blockers.values(), key=lambda b: -b["cells"]),
                    "note": "relaxed search: unlocked foreign tracks/vias are passable at a cost; shove the blockers, then run suggest again without --relax",
                    "width": w, "pitch": pit}
        variants, seen_c = [], set()
        for tp in _tail_options(M, net, isl[ib]["items"], prims_g, chunks[-1][0]):
            for hp in _tail_options(M, net, isl[ia]["items"], _rev_prims(tp), chunks[0][0]):
                for cs in _snap_ends(M, net, _render_prims(_rev_prims(hp))):
                    if cs not in seen_c:
                        seen_c.add(cs)
                        variants.append(cs)
        for cand in variants:
            try:
                prims = G.parse_path(cand, M.cu)
            except ValueError:
                last = {"violations": [{"kind": "degenerate"}]}
                continue
            pr = probe_prims(M, net, prims, w, (vd, vdr), any_angle, skip_ids)
            if pr["ok"] and _endpoints_join(pr, ia, ib):
                return {"found": True, "path": cand, "length_mm": round(sum(it.get("len", 0) for it in pr["items"]), 3), "vias": sum(1 for p in prims if p[0] == "via"),
                        "width": w, "via": [vd, vdr], "probe": "OK", "grid_margin_extra": extra, "pitch": pit, "expanded": pops}
            if pr["ok"]:
                ep_fail = True
                pr = dict(pr, violations=[{"kind": "endpoint_not_connected", "at": [j["at"] for j in pr.get("ends_join", []) if not j.get("touches")]}])
            last = pr
            pathstr = cand
    return {"found": False, "reason": ("endpoint extension failed: grid path found but no variant joins both islands after margin retries" if ep_fail
                                       else "grid path failed exact probe after margin retries"), "path": pathstr, "violations": last["violations"][:4]}
