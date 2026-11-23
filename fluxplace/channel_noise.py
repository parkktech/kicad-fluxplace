"""Noise environment of a differential pair, from board copper (routediag.load dict `B`): crosstalk to parallel
aggressors, broadside coupling on a thin-dielectric adjacent signal layer, reference-plane continuity, GND return
vias at layer changes, and distance to noisy nets (switching / RF per netclass.classify). Generic: nothing here names
a board, net or layer. Called from channel.evaluate; each result is a metric the same verdict tiers apply to.

Budget keys read from [channel.FAMILY] (all optional, a metric exists only when its key is set):
  xtalk_db_max / xtalk_db_target   worst aggressor NEXT or FEXT, dB (negative); window xtalk_window_w (8) x W edge to edge,
                                   rise_ps (default 350/(2 f_GHz)); runs shorter than xtalk_min_parallel_mm (1.0) ignored
  spacing_w_min / spacing_w_target aggressor edge gap in track widths on the same layer (fail below min; defaults 2 / 3
                                   when xtalk_db_* or a spacing key is set)
  broadside_mm_max / _target       parallel overlap with other nets' copper on a thin-dielectric adjacent signal layer
  plane_gap_mm_max / _target       path length with no reference-plane fill under it (nearest plane of each layer)
  return_via_mm / return_via_mm_target   nearest GND via to each pair via (si.return_via_findings)
  noisy_mm / noisy_mm_target       minimum edge distance to nets of class noisy_classes (default switch_node, rf)
"""
import math

from . import impedance as IM
from . import netclass as NC
from . import si as SI

SIN_PARALLEL = math.sin(math.radians(20.0))      # runs within 20 degrees of parallel count as parallel
STEP = 0.5                                       # mm, plane-continuity sample spacing
PAD_SKIP = 1.0                                   # mm around the pair's own pads where plane voids (antipads) are expected
NOISE_KEYS = ("xtalk_db_max", "xtalk_db_target", "spacing_w_min", "spacing_w_target", "broadside_mm_max",
              "broadside_mm_target", "plane_gap_mm_max", "plane_gap_mm_target", "return_via_mm",
              "return_via_mm_target", "noisy_mm", "noisy_mm_target", "rise_ps", "xtalk_window_w",
              "xtalk_min_parallel_mm")


# ------------------------------------------------------------------ geometry
def _run(v, t):
    """(overlap mm along victim segment v, mean centre-line distance mm) of segment t parallel to v, else None."""
    (ax, ay), (bx, by) = v["a"], v["b"]
    L = math.hypot(bx - ax, by - ay)
    if L < 1e-6:
        return None
    ux, uy = (bx - ax) / L, (by - ay) / L
    (cx, cy), (dx, dy) = t["a"], t["b"]
    lt = math.hypot(dx - cx, dy - cy)
    if lt < 1e-6 or abs(((dx - cx) * uy - (dy - cy) * ux) / lt) > SIN_PARALLEL:
        return None
    pa, pb = (cx - ax) * ux + (cy - ay) * uy, (dx - ax) * ux + (dy - ay) * uy
    ov = min(L, max(pa, pb)) - max(0.0, min(pa, pb))
    if ov <= 0:
        return None
    da, db = abs((cx - ax) * uy - (cy - ay) * ux), abs((dx - ax) * uy - (dy - ay) * ux)
    return ov, (da + db) / 2


def _seg_dist(p, a, b):
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


def _segs_dist(s, t):
    """Minimum centre-line distance between two segments (0 when they cross)."""
    (a, b), (c, d) = (s["a"], s["b"]), (t["a"], t["b"])

    def cr(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    if cr(a, b, c) * cr(a, b, d) < 0 and cr(c, d, a) * cr(c, d, b) < 0:
        return 0.0
    return min(_seg_dist(c, a, b), _seg_dist(d, a, b), _seg_dist(a, c, d), _seg_dist(b, c, d))


def _inside(pt, poly):
    """Even-odd point in polygon (a KiCad filled polygon carries its holes as keyhole slits)."""
    x, y = pt
    n, c, j = len(poly), False, len(poly) - 1
    for i in range(n):
        xi, yi, xj, yj = poly[i][0], poly[i][1], poly[j][0], poly[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            c = not c
        j = i
    return c


# ------------------------------------------------------------------ stackup helpers
def _copper(rows):
    return [r["name"] for r in rows if r.get("type") == "copper" and r.get("name")]


def ref_plane(rows, planes, layer):
    """(nearest reference plane layer name, dielectric+copper distance mm to it) or (None, None)."""
    idx = next((i for i, r in enumerate(rows) if r.get("name") == layer), None)
    best = (None, None)
    for step in (-1, 1):
        i, d = idx + step, 0.0
        while idx is not None and 0 <= i < len(rows):
            r = rows[i]
            if r.get("type") == "copper" and r.get("name") in planes:
                if best[1] is None or d < best[1]:
                    best = (r["name"], d)
                break
            d += float(r.get("thickness") or 0)
            i += step
    return best


def coupled_layers(rows, planes, layer):
    """{layer} plus adjacent SIGNAL layers whose dielectric to `layer` is thinner than the plane spacing of `layer`
    (the signal sees that layer's copper more strongly than its own plane): -> {other layer: dielectric mm}."""
    cu = _copper(rows)
    _p, hplane = ref_plane(rows, planes, layer)
    out = {}
    i = cu.index(layer) if layer in cu else None
    if i is None or hplane is None:
        return out
    for j in (i - 1, i + 1):
        if 0 <= j < len(cu) and cu[j] not in planes:
            a, b = sorted((rows.index(next(r for r in rows if r.get("name") == cu[k])) for k in (i, j)))
            t = sum(float(r.get("thickness") or 0) for r in rows[a + 1:b] if r.get("type") != "copper")
            if t < hplane:
                out[cu[j]] = t
    return out


def _h(rows, planes, layer):
    g = IM.geometry(rows, set(planes), layer)
    if not g or g.get("model") is None:
        return None, None, None
    hs = [h for h in (g["hu"], g["hd"]) if h]
    return min(hs), g["model"], IM.delay_ps_per_mm(g)


# ------------------------------------------------------------------ analysis
def analyse(B, rows, planes, legs, b, cons=None, spec=None, cls=None):
    """-> [dict(name, unit, value, target, limit, detail, minimum, digits)] for the pair `legs` (two net
    names). b = resolved budget dict (channel.resolve_budget)."""
    legset = set(legs)
    mine = [s for s in B["segs"] if s["net"] in legset]
    segs_by_layer = {}
    for s in B["segs"]:
        if s["net"] and s["net"] not in legset and not NC.is_ground(s["net"]):
            segs_by_layer.setdefault(s["layer"], []).append(s)
    out = []

    def add(name, unit, value, tgt, lim, detail="", minimum=False, digits=1):
        out.append({"name": name, "unit": unit, "value": value, "target": tgt, "limit": lim, "detail": detail,
                    "minimum": minimum, "digits": digits})

    # (1) crosstalk and (2)-spacing: aggressors on the same layer
    want_x = any(k in b for k in ("xtalk_db_max", "xtalk_db_target", "spacing_w_min", "spacing_w_target"))
    if want_x and mine:
        win = float(b.get("xtalk_window_w", 8.0))
        minpar = float(b.get("xtalk_min_parallel_mm", 1.0))
        f = float(b.get("freq_ghz", 1.0))
        tr = float(b.get("rise_ps") or 350.0 / (2 * f))
        agg = {}                                   # (victim leg, aggressor net) -> accumulators
        for v in mine:
            h, model, dly = _h(rows, planes, v["layer"])
            if h is None:
                continue
            for t in segs_by_layer.get(v["layer"], ()):
                r = _run(v, t)
                if not r:
                    continue
                ov, cc = r
                gap = cc - (v["w"] + t["w"]) / 2
                if gap > win * v["w"]:
                    continue
                k = 1.0 / (1.0 + (cc / h) ** 2)
                a = agg.setdefault((v["net"], t["net"]), {"L": 0.0, "kL": 0.0, "gap": 9e9, "dly": dly, "model": model, "w": v["w"], "gl": 0.0})
                a["L"] += ov
                a["kL"] += k * ov
                if ov >= minpar and gap < a["gap"]:
                    a["gap"], a["w"] = gap, v["w"]
        worst, wd = None, ""
        for (vn, an), a in agg.items():
            if a["L"] < minpar:
                continue
            kavg = a["kL"] / a["L"]
            # Johnson & Graham, High-Speed Digital Design ch.12: NEXT Kb = (Cm/C + Lm/L)/4 saturating at 2*Td = t_r; for a
            # homogeneous (stripline) pair FEXT = 0, for microstrip |Kf| ~ Lm/L/4; Lm/L ~ 1/(1+(D/h)^2) (D centre spacing,
            # h height over the plane). Single-ended aggressor onto ONE leg: the pair's common-mode rejection is not credited.
            next_ = (kavg / 2) * min(1.0, 2 * a["L"] * a["dly"] / tr)
            fext = 0.0 if a["model"] != "microstrip" else min(kavg / 4, (kavg / 4) * a["L"] * a["dly"] / tr)
            xt = max(next_, fext)
            db = 20 * math.log10(xt) if xt > 0 else -200.0
            if worst is None or db > worst:
                worst, wd = db, f"{an} onto {vn}: parallel {a['L']:.1f} mm, gap {a['gap'] if a['gap'] < 9e8 else 0:.2f} mm, " \
                                f"{'NEXT' if next_ >= fext else 'FEXT'}"
        if "xtalk_db_max" in b or "xtalk_db_target" in b:
            add("xtalk", "dB", worst, b.get("xtalk_db_target"), b.get("xtalk_db_max"), wd or "no parallel aggressor", digits=1)
        if "spacing_w_min" in b or "spacing_w_target" in b or "xtalk_db_max" in b or "xtalk_db_target" in b:
            tgt, mn = float(b.get("spacing_w_target", 3.0)), float(b.get("spacing_w_min", 2.0))
            best = min(((a["gap"] / a["w"], vn, an, a["L"]) for (vn, an), a in agg.items()
                        if a["L"] >= minpar and a["gap"] < 9e8), default=None)
            add("spacing", "W", best[0] if best else None, tgt, mn,
                f"{best[2]} beside {best[1]}, parallel {best[3]:.1f} mm" if best else "no parallel aggressor", True, 1)

    # (2) broadside coupling on adjacent signal layers with a thin dielectric
    if "broadside_mm_max" in b or "broadside_mm_target" in b:
        tot, who = {}, {}
        for v in mine:
            for lay, t_d in coupled_layers(rows, planes, v["layer"]).items():
                for t in segs_by_layer.get(lay, ()):
                    r = _run(v, t)
                    if r and r[1] < (v["w"] + t["w"]) / 2:
                        tot[t["net"]] = tot.get(t["net"], 0.0) + r[0]
                        who[t["net"]] = (lay, t_d, v["layer"])
        n, ln = max(tot.items(), key=lambda kv: kv[1], default=(None, 0.0))
        add("broadside", "mm", ln, b.get("broadside_mm_target"), b.get("broadside_mm_max"),
            f"{n} on {who[n][0]} over {who[n][2]} ({who[n][1]:.3f} mm dielectric)" if n else "none")

    # (3) reference-plane continuity and GND return vias
    if "plane_gap_mm_max" in b or "plane_gap_mm_target" in b:
        pads = [p for p in B["pads"] if p["net"] in legset]
        gap_mm, runs, nets, unfilled = 0.0, 0, set(), set()
        for v in mine:
            plane, _d = ref_plane(rows, planes, v["layer"])
            if not plane:
                continue
            zs = [z for z in B["zones"] if plane in z["layers"] and not z["keepout"]]
            polys = [pts for z in zs for (ly, pts) in z["fills"] if ly == plane and len(pts) > 2]
            if zs and not polys:
                unfilled.add(plane)
                polys = [z["pts"] for z in zs]
            (ax, ay), (bx, by) = v["a"], v["b"]
            L = math.hypot(bx - ax, by - ay)
            n = max(1, int(L / STEP))
            prev_void = False
            for k in range(n):
                pt = (ax + (bx - ax) * (k + .5) / n, ay + (by - ay) * (k + .5) / n)
                if any(math.hypot(pt[0] - p["x"], pt[1] - p["y"]) < PAD_SKIP for p in pads):
                    prev_void = False
                    continue
                hit = next((z for z in zs for (ly, pts) in (z["fills"] or [(plane, z["pts"])])
                            if ly == plane and len(pts) > 2 and _inside(pt, pts)), None)
                if hit is None:
                    gap_mm += L / n
                    runs += (not prev_void)
                    prev_void = True
                else:
                    nets.add(hit["net"])
                    prev_void = False
        refs = ", ".join(sorted(x or "?" for x in nets)) or "none"
        add("plane", "mm", gap_mm, b.get("plane_gap_mm_target"), b.get("plane_gap_mm_max"),
            f"{runs} void run(s); reference net(s) {refs}" + (f"; {sorted(unfilled)} zone not filled, outline used" if unfilled else ""), digits=2)
    if "return_via_mm" in b or "return_via_mm_target" in b:
        pv = [(v["net"], v["x"], v["y"]) for v in B["vias"] if v["net"] in legset]
        gv = [(None, v["x"], v["y"]) for v in B["vias"] if NC.is_ground(v["net"])]
        if pv:
            _f, table = SI.return_via_findings(pv, gv, float(b.get("return_via_mm", 10.0)))
            far = max(table, key=lambda r: 9e9 if r[3] is None else r[3])
            add("return_via", "mm", far[3], b.get("return_via_mm_target"), b.get("return_via_mm"),
                f"{len(pv)} via(s); worst {far[0]} at ({far[1]:.1f},{far[2]:.1f})", digits=2)

    # (4) distance to noisy nets
    if "noisy_mm" in b or "noisy_mm_target" in b:
        classes = set(b.get("noisy_classes") or ("switch_node", "rf"))
        cls = cls if cls is not None else NC.classify(B, cons or {}, spec)
        noisy = {n for n, c in cls.items() if c["class"] in classes}
        best = (None, 9e9)
        for v in mine:
            lay = {v["layer"], *coupled_layers(rows, planes, v["layer"])}
            for t in B["segs"]:
                if t["net"] in noisy and t["layer"] in lay:
                    d = _segs_dist(v, t) - (v["w"] + t["w"]) / 2
                    if d < best[1]:
                        best = (f"{t['net']} track on {t['layer']}", d)
            for p in B["pads"]:
                if p["net"] in noisy and (set(p.get("layers") or lay) & lay):
                    d = _seg_dist((p["x"], p["y"]), v["a"], v["b"]) - v["w"] / 2 - max(p.get("ex", 0), p.get("ey", 0))
                    if d < best[1]:
                        best = (f"{p['net']} pad {p['ref']}.{p['num']}", d)
            for x in B["vias"]:
                if x["net"] in noisy:
                    d = _seg_dist((x["x"], x["y"]), v["a"], v["b"]) - v["w"] / 2 - x["d"] / 2
                    if d < best[1]:
                        best = (f"{x['net']} via", d)
        add("noisy", "mm", max(0.0, best[1]) if best[0] else None, b.get("noisy_mm_target"), b.get("noisy_mm"),
            best[0] or f"no {'/'.join(sorted(classes))} net within reach", True, 2)
    return out
