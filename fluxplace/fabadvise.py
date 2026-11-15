"""fab-advise: which manufacturer tier builds THIS board as designed, and what each constraint costs.

Measures the board's actual features (layer count, size, min track used, min space requested, via/PTH drills and rings,
impedance control, blind/buried, fine-pitch mask dams, rail copper) and grades each (vendor, tier) from
data/fab_capabilities.json. Recommends the cheapest tier that builds it unchanged (ranked by tier; the vendors publish
price steps as wording only, so no price is invented - "price not published"), and lists the smallest constraint change
that would drop one tier, with the number of objects affected.
"""
import math
from collections import Counter

from . import fabcaps as FC, power as PW


def _requested_vias(facts):
    """(dia, drill) of every via size the project's net classes request (a board not yet routed has no vias)."""
    import json
    import os
    bp = facts.get("board_path")
    try:
        d = json.load(open(os.path.splitext(bp)[0] + ".kicad_pro"))
        return sorted({(float(c["via_diameter"]), float(c["via_drill"])) for c in d["net_settings"]["classes"]
                       if c.get("via_diameter") and c.get("via_drill")})
    except (OSError, ValueError, KeyError, TypeError):
        return []


def features(facts, cons):
    """Measured/requested features of the board."""
    from . import constraints as C
    tg = [s for s in facts.get("track_geom") or [] if s.get("width")]
    vias = list(facts.get("vias") or [])
    vreq = _requested_vias(facts)
    vias_all = vias + [{"net": "(requested)", "dia_mm": d, "drill_mm": r, "requested": True} for d, r in vreq]
    pth = facts.get("pth_pads") or []
    route = (cons or {}).get("route") or {}
    dm = facts.get("design_min") or {}
    pairs = (cons or {}).get("pairs") or {}
    pair_gaps = {}
    for fam, p in pairs.items():
        z = int(p.get("impedance_diff", 0) or 0)
        g = p.get("gap_mm") or (C.PAIR_GEOM_BY_Z.get(z) or (None, None))[1]
        if g:
            pair_gaps[fam] = float(g)
    space_req = [float(v) for v in ((route.get("min_clearance_mm"),) if route.get("min_clearance_mm") else (dm.get("clearance_mm"),)) if v]
    track_req = [float(v) for v in (route.get("min_track_mm"),) if v]
    f = {"layers": facts.get("copper_layers"), "size_mm": facts.get("size_mm"),
         "track_used": min((s["width"] for s in tg), default=None),
         "track_requested": min(track_req) if track_req else None,
         "space_requested": min(space_req) if space_req else None,
         "pair_gaps": pair_gaps,
         "via_sizes": Counter((v.get("dia_mm"), v.get("drill_mm")) for v in vias), "via_requested": vreq,
         "n_vias": len(vias), "n_holes": len(vias) + len(pth),
         "drill_min": min([v["drill_mm"] for v in vias_all if v.get("drill_mm")] + [p["drill_mm"] for p in pth if p.get("drill_mm")], default=None),
         "via_ring_min": min(((v["dia_mm"] - v["drill_mm"]) / 2 for v in vias_all if v.get("dia_mm") and v.get("drill_mm")), default=None),
         "pth_ring_min": min(((p["dia_mm"] - p["drill_mm"]) / 2 for p in pth if p.get("dia_mm") and p.get("drill_mm")), default=None),
         "impedance": bool(pairs and any("impedance_diff" in p for p in pairs.values())) or bool((cons or {}).get("rf")),
         "blind_buried": sum(n for k, n in (facts.get("via_type_counts") or {}).items() if k != "through"),
         "copper_oz": PW.oz_from_stackup(facts.get("stackup")),
         "tracks": tg, "vias": vias_all, "pth": pth}
    return f


def _dams(facts, mask_exp=0.05):
    """Smallest pad-to-pad copper gap per part -> mask dam = gap - 2 * mask expansion. [(ref, dam_mm)] ascending."""
    out = []
    for ref, p in (facts.get("parts") or {}).items():
        g = p.get("pad_geom") or []
        best = None
        for i, a in enumerate(g):
            for b in g[i + 1:]:
                if a[0] == b[0]:
                    continue
                dx = abs(a[1] - b[1]) - (a[3] + b[3]) / 2
                dy = abs(a[2] - b[2]) - (a[4] + b[4]) / 2
                gap = max(dx, dy) if (dx > 0 and dy > 0) else (dx if dx > 0 else dy if dy > 0 else None)
                if gap is not None and gap > 0.01:
                    best = gap if best is None else min(best, gap)
        if best is not None and best - 2 * mask_exp < 0.12:
            out.append((ref, round(best - 2 * mask_exp, 3)))
    return sorted(out, key=lambda r: r[1])


def grade(feat, vendor, tier, facts=None):
    """-> {name, rank, offers, fails:[(what, have, need, n_affected)], notes:[...], fit: bool}."""
    info = FC.tier_info(vendor, tier)
    layers = feat["layers"]
    if not FC.offers(vendor, tier, layers):
        return {**info, "offers": False, "fit": False, "fails": [("layers", layers, "not offered", 0)], "notes": []}
    lim, unv = FC.row_for(vendor, tier, layers)
    fails, notes = [], []

    def need(key, have, label, n=0, below=True):
        lv = lim.get(key)
        if have is not None and lv is not None and (have < lv - 1e-6 if below else have > lv + 1e-6):
            fails.append((label, have, lv, n))

    nt = lambda w: sum(1 for s in feat["tracks"] if s["width"] < w - 1e-6)
    lt = lim.get("track_min")
    need("track_min", feat["track_used"], "track width used", nt(lt) if lt else 0)
    need("track_min", feat["track_requested"], "track width requested ([route])")
    need("space_min", feat["space_requested"], "clearance requested")
    for fam, g in sorted(feat["pair_gaps"].items()):
        need("space_min", g, f"pair gap {fam}")
    dl = lim.get("drill_min")
    need("drill_min", feat["drill_min"], "smallest drill",
         (sum(1 for v in feat["vias"] if v.get("drill_mm") and v["drill_mm"] < dl - 1e-6 and not v.get("requested")) +
          sum(1 for p in feat["pth"] if p.get("drill_mm") and p["drill_mm"] < dl - 1e-6)) if dl else 0)
    vr = lim.get("via_annular_min")
    need("via_annular_min", feat["via_ring_min"], "via annular ring",
         sum(1 for v in feat["vias"] if v.get("dia_mm") and v.get("drill_mm") and (v["dia_mm"] - v["drill_mm"]) / 2 < vr - 1e-6 and not v.get("requested")) if vr else 0)
    vd = lim.get("via_dia_min")
    if vd:
        n = sum(1 for v in feat["vias"] if v.get("dia_mm") and v["dia_mm"] < vd - 1e-6 and (v.get("drill_mm") or 9) < 0.3)
        if n:
            fails.append(("via pad dia (drill < 0.3)", min(v["dia_mm"] for v in feat["vias"] if v.get("dia_mm")), vd, n))
    pr = lim.get("pth_annular_min")
    need("pth_annular_min", feat["pth_ring_min"], "PTH annular ring",
         sum(1 for p in feat["pth"] if p.get("dia_mm") and p.get("drill_mm") and (p["dia_mm"] - p["drill_mm"]) / 2 < pr - 1e-6) if pr else 0)
    oz = feat.get("copper_oz")
    if oz:
        for kind, val, key in (("outer", oz[0], "outer_oz"), ("inner", oz[1], "inner_oz")):
            opts = lim.get(key)
            if layers > 2 and opts and val not in opts:
                fails.append((f"{kind} copper weight in the board stackup (oz)", val, f"offered {opts}", 0))
    sz = feat["size_mm"]
    ms = lim.get("max_size")
    if sz and ms and (max(sz) > max(ms) or min(sz) > min(ms)):
        fails.append(("board size", f"{sz[0]:.0f}x{sz[1]:.0f}", f"{ms[0]}x{ms[1]}", 0))
    if feat["impedance"] and not lim.get("impedance_control", False) and layers >= 4:
        notes.append("impedance control not offered at this layer count")
        fails.append(("impedance control", "required", "not offered", 0))
    elif feat["impedance"]:
        notes.append("impedance control (a price option: " + data_feature(vendor, "impedance") + ")")
    adv = lim.get("pth_annular_advisory")
    if adv and feat["pth_ring_min"] is not None and feat["pth_ring_min"] < adv - 1e-6:
        n = sum(1 for p in feat["pth"] if p.get("dia_mm") and p.get("drill_mm") and (p["dia_mm"] - p["drill_mm"]) / 2 < adv - 1e-6)
        notes.append(f"ADVISORY: {n} PTH pad(s) have ring {feat['pth_ring_min']:g} < {adv:g} (the vendor's Advanced-table "
                     f"component-hole figure; its Standard table says {lim.get('pth_annular_min'):g}) - confirm with CAM before ordering")
    if feat["blind_buried"]:
        notes.append(f"{feat['blind_buried']} blind/buried/micro via(s): " + data_feature(vendor, "blind_buried"))
    if unv:
        notes.append("unverified in this row: " + "; ".join(unv))
    return {**info, "offers": True, "fit": not fails, "fails": fails, "notes": notes}


def data_feature(vendor, key):
    return FC.data()["vendors"][vendor]["features"].get(key, {}).get("price_step", "price not published")


def advise(facts, cons):
    feat = features(facts, cons)
    rows = [grade(feat, v, t) for v, t in FC.all_tiers()]
    fit = [r for r in rows if r["fit"]]
    best_rank = min((r["rank"] for r in fit), default=None)
    best = [r for r in fit if r["rank"] == best_rank]
    drop = []
    if best_rank is not None:
        for r in rows:
            if r["offers"] and not r["fit"] and r["rank"] <= best_rank:
                drop.append(r)
    return {"features": feat, "tiers": rows, "recommended": best, "cheaper_if": drop,
            "mask_dams": _dams(facts)}


def _fmt(x):
    return f"{x:g}" if isinstance(x, (int, float)) else str(x)


def format_text(res, name=""):
    f = res["features"]
    L = [f"FAB-ADVISE {name}",
         f"  board: {f['layers']} copper layers, {f['size_mm'][0]:.1f} x {f['size_mm'][1]:.1f} mm, {f['n_vias']} vias, "
         f"{f['n_holes']} holes (requested via sizes {f['via_requested']}); track used min {_fmt(f['track_used'])} (requested {_fmt(f['track_requested'])}), "
         f"space requested {_fmt(f['space_requested'])}, pair gaps {', '.join(f'{k}{_fmt(v)}' for k, v in f['pair_gaps'].items()) or '-'}; "
         f"drill min {_fmt(f['drill_min'])}, via ring {_fmt(f['via_ring_min'])}, PTH ring {_fmt(f['pth_ring_min'])}; "
         f"impedance {'yes' if f['impedance'] else 'no'}",
         "  vias on board (dia/drill: n): " + (", ".join(f"{d}/{r}: {n}" for (d, r), n in sorted(f["via_sizes"].items(), key=lambda kv: -kv[1])) or "none routed"),
         "  price: vendors publish price-step wording only (quote form for prices) - price not published; tiers ranked by that wording",
         f"  {'tier':22s} {'rank':4s} verdict"]
    for r in res["tiers"]:
        v = "FITS" if r["fit"] else ("no: " + "; ".join(
            f"{w} {_fmt(h)} vs limit {_fmt(n)}" + (f" ({c} affected)" if c else "") for w, h, n, c in r["fails"]))
        L.append(f"  {r['name']:22s} {r['rank']:<4d} {v}")
        for n in r["notes"]:
            L.append(f"  {'':22s}      - {n}")
    rec = res["recommended"]
    if rec:
        L.append("  RECOMMENDED (cheapest tier that builds it as designed): " + ", ".join(r["name"] for r in rec)
                 + f" (rank {rec[0]['rank']}; price step: {rec[0]['price_step']})")
    else:
        L.append("  NO tier builds this board as designed")
    for r in res["cheaper_if"]:
        L.append(f"  to ALSO fit {r['name']} (rank {r['rank']}, not above the recommended tier): " + "; ".join(
            f"change {w} {_fmt(h)} -> {_fmt(n)}" + (f" ({c} objects affected)" if c else "") for w, h, n, c in r["fails"]))
    if rec:
        L.append("  what raises the tier (vendor wording): " + " | ".join(
            f"{r['name']}: {r['price_step']}" for r in res["tiers"] if r["rank"] > rec[0]["rank"] and r["offers"]
            and r["vendor"] in {x["vendor"] for x in rec}))
    if res["mask_dams"]:
        L.append("  mask dams under 0.12 mm after 0.05 mm expansion (vendors list dam 0.10 mm, advisory): " +
                 ", ".join(f"{a} {b:g}" for a, b in res["mask_dams"][:10]))
    return "\n".join(L)
