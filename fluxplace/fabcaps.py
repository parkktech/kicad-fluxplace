"""Manufacturer capability profiles: `[fab] profile = "<vendor>-<tier>"` + the board's copper layer count select
a row of data/fab_capabilities.json. Those numbers are HARD limits (review FAIL, router never below). The
owner's `[route]` floors are the PREFERRED general floor above them; `judge()` classifies a value against both.

Legacy names in profiles.PROFILES ("jlcpcb", "jlcpcb-advanced", "proto") stay valid. Any other name is a loud
`FabProfileError` listing what exists (it used to fall back silently to the .kicad_pro).
"""
import json
import os
import re

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fab_capabilities.json")
_CACHE = {}

# limits key -> the [fab] key the rest of the tool reads
FLOOR_MAP = {"track_min": "min_track_mm", "space_min": "min_clearance_mm", "drill_min": "min_drill_mm",
             "via_annular_min": "min_annular_mm", "silk_width_min": "min_silk_width_mm"}


class FabProfileError(ValueError):
    """Unknown profile name, or a layer count the tier does not offer."""


def data():
    if "d" not in _CACHE:
        with open(_DATA) as f:
            _CACHE["d"] = json.load(f)
    return _CACHE["d"]


def _legacy():
    from .profiles import PROFILES
    return PROFILES


def names():
    """Every valid `[fab] profile` name: new `<vendor>-<tier>` ones plus the legacy ones."""
    out = [f"{v}-{t}" for v, vd in data()["vendors"].items() for t in vd["tiers"]]
    return sorted(out) + sorted(_legacy())


def is_legacy(name):
    return name in _legacy()


def split(name):
    """'pcbway-fine-drill' -> ('pcbway', 'fine-drill') or raise FabProfileError."""
    for v, vd in data()["vendors"].items():
        if name.startswith(v + "-") and name[len(v) + 1:] in vd["tiers"]:
            return v, name[len(v) + 1:]
    raise FabProfileError(f"unknown [fab] profile {name!r} - valid names: {', '.join(names())}")


def tier_rows(vendor, tier):
    """[(layers_lo, layers_hi, limits, unverified)] with `inherits`/`override` resolved."""
    vd = data()["vendors"][vendor]
    t = vd["tiers"][tier]
    if "inherits" in t:
        base = tier_rows(vendor, t["inherits"])
        ov = t.get("override", {})
        return [(lo, hi, {**lim, **ov}, unv + list(t.get("unverified", []))) for lo, hi, lim, unv in base]
    return [(r["layers"][0], r["layers"][1], dict(r["limits"]), list(r.get("unverified", []))) for r in t["rows"]]


def tier_info(vendor, tier):
    vd = data()["vendors"][vendor]
    t = vd["tiers"][tier]
    return {"vendor": vendor, "tier": tier, "name": f"{vendor}-{tier}", "rank": t["rank"], "description": t["description"],
            "price_step": t["price_step"], "source": t["source"], "retrieved": t["retrieved"]}


def all_tiers():
    """[(vendor, tier)] ordered by rank then vendor."""
    return sorted(((v, t) for v, vd in data()["vendors"].items() for t in vd["tiers"]),
                  key=lambda vt: (data()["vendors"][vt[0]]["tiers"][vt[1]]["rank"], vt[0]))


def row_for(vendor, tier, layers):
    """-> (limits, unverified) of the row covering `layers`; FabProfileError when the tier does not offer it."""
    rows = tier_rows(vendor, tier)
    for lo, hi, lim, unv in rows:
        if lo <= layers <= hi:
            return lim, unv
    have = ", ".join(f"{lo}-{hi}" for lo, hi, _l, _u in rows)
    raise FabProfileError(f"{vendor}-{tier} does not offer {layers} copper layers (rows: {have})")


def offers(vendor, tier, layers):
    try:
        row_for(vendor, tier, layers)
        return True
    except FabProfileError:
        return False


def board_layer_count(board_path):
    """Copper layer count read from a .kicad_pcb's (layers ...) table; None when unreadable."""
    try:
        with open(board_path, errors="replace") as f:
            head = f.read(40000)
    except OSError:
        return None
    m = re.search(r"\(layers\b(.*?)\n\t\)", head, re.S) or re.search(r"\(layers\b(.*?)\n\s*\)", head, re.S)
    if not m:
        return None
    n = len(re.findall(r'"[^"]*\.Cu"', m.group(1)))
    return n or None


def profile_list(name):
    """`[fab] profile` as a list of names: a string is one profile, a list (a board built at several fabs) is all of them."""
    if name is None or name == "":
        return []
    if isinstance(name, (list, tuple)):
        out = []
        for n in name:
            if str(n) not in out:
                out.append(str(n))
        return out
    return [str(name)]


def _strict_hi(k, x):
    """True when the larger number is the stricter one for limits key `k` (every minimum / spacing / dam / edge figure)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and (
        k.endswith(("_min", "_advisory")) or k.startswith(("hole_to", "mask_dam", "copper_to")))


def merge_limits(rows):
    """rows = [(profile name, limits dict)] -> (limits, provenance). The STRICTEST value per rule wins: larger for every
    minimum/spacing, smaller board size, intersection of the offered copper weights and thickness range, impedance
    control only when every fab offers it. provenance[key] = [profile names whose value equals the winner]."""
    lim, prov = {}, {}
    for name, l in rows:
        for k, x in l.items():
            if k not in lim:
                lim[k], prov[k] = x, [name]
                continue
            cur = lim[k]
            if _strict_hi(k, x) and _strict_hi(k, cur):
                if x > cur + 1e-9:
                    lim[k], prov[k] = x, [name]
                elif abs(x - cur) <= 1e-9:
                    prov[k].append(name)
            elif k in ("outer_oz", "inner_oz") and isinstance(x, list) and isinstance(cur, list):
                lim[k] = [v for v in cur if v in x]
                prov[k].append(name)
            elif k == "thickness_options" and isinstance(x, list) and isinstance(cur, list) and len(x) == 2 == len(cur):
                lim[k] = [max(cur[0], x[0]), min(cur[1], x[1])]
                prov[k].append(name)
            elif k == "max_size" and isinstance(x, list) and isinstance(cur, list) and len(x) == 2 == len(cur):
                lim[k] = [min(cur[0], x[0]), min(cur[1], x[1])]
                prov[k].append(name)
            elif k == "impedance_control":
                lim[k] = bool(cur) and bool(x)
                prov[k].append(name)
    for name, l in rows:
        for k in lim:
            if k not in l and k in ("outer_oz", "inner_oz", "impedance_control") and k in prov and name not in prov[k]:
                prov[k].append(name)
    return lim, prov


def limits(name, layers=None):
    """Resolved hard limits for profile `name` (one name, or a list = the strictest of all) at `layers` copper layers.
    -> dict(limits={...}, floors={[fab] keys}, vendor, tier, layers, unverified, legacy, profiles, provenance, floor_prov);
    layers None = strictest row. A list gives vendor/tier None and `name` = the names joined with ' + '."""
    names = profile_list(name)
    if len(names) > 1:
        parts = [limits(n, layers) for n in names]
        lim, prov = merge_limits([(r["name"], r["limits"]) for r in parts if r["limits"]])
        fl, fprov = {}, {}
        for r in parts:
            for k, x in r["floors"].items():
                if k not in fl or x > fl[k] + 1e-9:
                    fl[k], fprov[k] = x, [r["name"]]
                elif abs(x - fl[k]) <= 1e-9:
                    fprov[k].append(r["name"])
        if lim:                              # the merged row's own drill+ring floor can exceed either row's
            drill, ann = lim.get("drill_min"), lim.get("via_annular_min")
            if drill is not None and ann is not None:
                vd = round(max(lim.get("via_dia_min", 0.0), drill + 2 * ann), 4)
                if vd > fl.get("min_via_dia_mm", 0.0) + 1e-9:
                    fl["min_via_dia_mm"], fprov["min_via_dia_mm"] = vd, prov.get("via_dia_min", []) + ["combined drill + 2 x ring"]
        unv = [f"{r['name']}: {u}" for r in parts for u in r["unverified"]]
        return {"limits": lim, "floors": fl, "vendor": None, "tier": None, "layers": layers, "unverified": unv,
                "legacy": all(r["legacy"] for r in parts), "name": " + ".join(names), "profiles": names,
                "provenance": prov, "floor_prov": fprov}
    name = names[0] if names else str(name)
    if is_legacy(name):
        p = _legacy()[name]
        fl = {"min_track_mm": p["track_min"], "min_clearance_mm": p["clearance_min"], "min_drill_mm": p["via_drill_min"],
              "min_via_dia_mm": p["via_dia_min"], "min_annular_mm": round((p["via_dia_min"] - p["via_drill_min"]) / 2, 4)}
        return {"limits": {}, "floors": fl, "vendor": None, "tier": None, "layers": layers, "unverified": [], "legacy": True,
                "name": name, "profiles": [name], "provenance": {}, "floor_prov": {k: [name] for k in fl}}
    v, t = split(name)
    if layers is None:
        rows = tier_rows(v, t)
        lim = {}
        for _lo, _hi, l, _u in rows:
            for k, x in l.items():
                if isinstance(x, (int, float)) and not isinstance(x, bool) and (k.endswith("_min") or k.startswith(("hole_to", "mask_dam", "copper_to"))):
                    lim[k] = max(lim.get(k, 0.0), x)
                else:
                    lim.setdefault(k, x)
        unv = ["layer count unknown: strictest row across all layer counts used"]
    else:
        lim, unv = row_for(v, t, int(layers))
    fl = {FLOOR_MAP[k]: lim[k] for k in FLOOR_MAP if k in lim}
    drill, ann = lim.get("drill_min"), lim.get("via_annular_min")
    if drill is not None and ann is not None:
        fl["min_via_dia_mm"] = round(max(lim.get("via_dia_min", 0.0), drill + 2 * ann), 4)
    return {"limits": lim, "floors": fl, "vendor": v, "tier": t, "layers": layers, "unverified": unv, "legacy": False,
            "name": name, "profiles": [name], "provenance": {k: [name] for k in lim}, "floor_prov": {k: [name] for k in fl}}


def judge(value_mm, preferred_mm, hard_mm):
    """'ok' at/above the preferred floor; 'below_preferred' between preferred and the fab limit (legal, reported);
    'illegal' below the fab limit."""
    eps = 1e-6
    if hard_mm is not None and value_mm < hard_mm - eps:
        return "illegal"
    if preferred_mm is not None and value_mm < preferred_mm - eps:
        return "below_preferred"
    return "ok"


def cheapest_tier_for(requirements, layers):
    """requirements: {'track': mm, 'space': mm, 'drill': mm, 'via_dia': mm, 'via_annular': mm, 'pth_annular': mm}
    (None entries skipped). -> list of dict(per tier: name, rank, offers, fails=[str]) ordered by rank."""
    out = []
    for v, t in all_tiers():
        info = tier_info(v, t)
        if not offers(v, t, layers):
            out.append({**info, "offers": False, "fails": [f"does not offer {layers} copper layers"]})
            continue
        lim, _u = row_for(v, t, layers)
        fails = []
        for req, key, cmp_ in (("track", "track_min", "<"), ("space", "space_min", "<"), ("drill", "drill_min", "<"),
                               ("via_dia", "via_dia_min", "<"), ("via_annular", "via_annular_min", "<"),
                               ("pth_annular", "pth_annular_min", "<")):
            have, need = requirements.get(req), lim.get(key)
            if have is not None and need is not None and have < need - 1e-6:
                fails.append(f"{req} {have:g} < {key} {need:g}")
        out.append({**info, "offers": True, "fails": fails})
    return sorted(out, key=lambda r: (r["rank"], r["name"]))
