"""Placement rules derived from the board: module keep-outs and mounting-hardware circles.

`[placement.module.NAME] = {refs = [...], side = "F"|"B", outline = [x0, y0, x1, y1] | margin_mm = 1.0,
allow = [...], tht = false, reason = "..."}`
    expands onto the ordinary mechanisms: `[placement.keepout.module.NAME]` (no part body of `side` in the outline,
    allow = refs + allow) and, when `tht = false`, `[placement.no_tht.module.NAME]` (same outline, same allow). With
    `margin_mm` the outline is the bounding box of the refs' pads and holes grown by the margin (needs the board).
    `expand(cons)` (called by `constraints.load`) writes the explicit-outline ones; `expand(cons, B)` (called where
    keep-outs are read) writes the rest. Idempotent: an entry that already exists is kept.
    `under_height_mm = H` (default 0 = no part at all) with `under_margin_mm` (0.2): a part of the module's face inside the
    outline is allowed when its known height <= H - margin; unknown height is NOT allowed. Expanded to an
    `[[intent.region]]` with max_height_mm (the mechanism that already does this per part, exact in the gate and in the
    placers). Heights: `[placement.heights]` (ref or footprint glob -> mm, first match), else the chip-size estimate
    (intent.height). A module with a height allowance has no `module.NAME` keep-out row; see `under_report`.

`[placement.hardware]` (off unless the table exists): keep-out circles around mounting hardware.
    table = [[max_drill_mm, keepout_diameter_mm], ...]  default [[2.4, 5.0], [2.9, 6.0], [3.6, 7.0]] (washer sizes
                                                         for M2, M2.5, M3); a drill above the last row uses the last row
    min_drill_mm = 2.0       smallest drill that counts as a mounting hole
    refs = [...]             extra hardware refs (a standoff without a drill: give diameter_mm)
    exclude = [...]          refs never treated as hardware (fnmatch)
    allow = [...]            refs allowed inside any circle (fnmatch), e.g. the standoff's own mating part
    diameter_mm = {REF = d}  per-ref override
    A mounting-hole footprint = a part whose pads all sit on ONE spot and whose largest drill >= min_drill_mm and has
    no net (or a ground net) on that pad. Circle centre = that spot (the part origin for `refs` without a drill).
    The circle is exact in the legality gate (reason `hardware`); the placers see its bounding square as a keep-out
    (side both, allow = the hole's own ref).
"""
import fnmatch
import math

from . import netclass as NC

SIDES = ("F", "B")
TABLE = ((2.4, 5.0), (2.9, 6.0), (3.6, 7.0))


def _pl(cons):
    return (cons or {}).get("placement") or {}


def hardware_cfg(cons):
    """The [placement.hardware] table or None (feature off)."""
    h = _pl(cons).get("hardware")
    return h if isinstance(h, dict) else None


def _match(ref, pats):
    return any(fnmatch.fnmatch(ref, g) for g in pats or [])


def hardware(B, cons):
    """[(ref, (x, y), diameter_mm)] of mounting hardware; [] when `[placement.hardware]` is absent."""
    h = hardware_cfg(cons)
    if h is None:
        return []
    table = sorted((float(a), float(b)) for a, b in (h.get("table") or TABLE))
    min_d = float(h.get("min_drill_mm", 2.0))
    over = {k: float(v) for k, v in (h.get("diameter_mm") or {}).items()}
    out = []

    def dia(ref, drill):
        if ref in over:
            return over[ref]
        for mx, d in table:
            if drill <= mx + 1e-9:
                return d
        return table[-1][1]

    for ref, p in sorted(B["parts"].items()):
        if _match(ref, h.get("exclude")):
            continue
        holes = [(q["x"], q["y"], q.get("drill", 0.0), q.get("net", "")) for q in p["pads"] if q.get("drill", 0.0) > 0]
        holes += [(q["x"], q["y"], q["drill"], "") for q in p.get("holes", [])]
        pts = [(q["x"], q["y"]) for q in p["pads"]] + [(q["x"], q["y"]) for q in p.get("holes", [])]
        if ref in (h.get("refs") or []):
            if holes:
                x, y, dr, _n = max(holes, key=lambda t: t[2])
            else:
                (x, y), dr = p["pos"], 0.0
            out.append((ref, (round(x, 4), round(y, 4)), dia(ref, dr)))
            continue
        if not holes or not pts:
            continue
        x, y, dr, net = max(holes, key=lambda t: t[2])
        if dr < min_d or (net and not NC.is_ground(net)) or any(math.hypot(a - x, b - y) > 0.05 for a, b in pts):
            continue
        out.append((ref, (round(x, 4), round(y, 4)), dia(ref, dr)))
    return out


def hardware_squares(B, cons):
    """[(rect, ref)] bounding squares of the hardware circles (what rect-only placers are given)."""
    return [((x - d / 2, y - d / 2, x + d / 2, y + d / 2), ref) for ref, (x, y), d in hardware(B, cons)]


def circle_hit(circle, rect, pads=()):
    """Distance from the circle centre ((x, y), r) to the nearest of the boxes `rect` / `pads` ((x0, y0, x1, y1)),
    or None when every box is outside the circle."""
    (x, y), r = circle
    best = min(math.hypot(max(c[0] - x, 0.0, x - c[2]), max(c[1] - y, 0.0, y - c[3])) for c in (rect, *pads))
    return best if best < r - 1e-6 else None


def _outline(B, m):
    if m.get("outline"):
        return tuple(float(v) for v in m["outline"])
    if B is None or m.get("margin_mm") is None:
        return None
    xs0, ys0, xs1, ys1 = [], [], [], []
    for ref in m.get("refs", []):
        p = B["parts"].get(ref)
        if not p:
            continue
        for q in p["pads"]:
            r = max(q.get("drill", 0.0) / 2, 0.0)
            xs0.append(q["x"] - max(q["ex"], r)); xs1.append(q["x"] + max(q["ex"], r))
            ys0.append(q["y"] - max(q["ey"], r)); ys1.append(q["y"] + max(q["ey"], r))
        for q in p.get("holes", []):
            xs0.append(q["x"] - q["drill"] / 2); xs1.append(q["x"] + q["drill"] / 2)
            ys0.append(q["y"] - q["drill"] / 2); ys1.append(q["y"] + q["drill"] / 2)
    if not xs0:
        return None
    g = float(m["margin_mm"])
    return (min(xs0) - g, min(ys0) - g, max(xs1) + g, max(ys1) + g)


def expand(cons, B=None):
    """Write the `[placement.module.NAME]` expansions into cons["placement"]["keepout"] / ["no_tht"] (in place;
    returns cons). Without `B` only modules with an explicit `outline` are expanded."""
    pl = _pl(cons)
    mods = pl.get("module")
    if not isinstance(mods, dict):
        return cons
    ko, nt = pl.get("keepout") or {}, pl.get("no_tht") or {}
    pl["keepout"], pl["no_tht"] = ko, nt
    for name, m in sorted(mods.items()):
        if not isinstance(m, dict) or f"module.{name}" in ko or f"module.{name}" in nt or any(
                r.get("name") == f"module.{name}" for r in (cons.get("intent") or {}).get("region", []) or []):
            continue
        rect = _outline(B, m)
        if rect is None:
            continue
        allow = list(dict.fromkeys(list(m.get("refs", [])) + list(m.get("allow", []))))
        why = m.get("reason", f"module {name}")
        lim = float(m.get("under_height_mm", 0) or 0) - float(m.get("under_margin_mm", 0.2))
        if float(m.get("under_height_mm", 0) or 0) > 0 and lim > 0:
            reg = cons.setdefault("intent", {}).setdefault("region", [])
            if not any(r.get("name") == f"module.{name}" for r in reg):
                reg.append({"name": f"module.{name}", "rect": list(rect), "side": str(m.get("side", "both")), "keep": "out",
                            "max_height_mm": lim, "allow": allow, "hard": True})
            if not m.get("tht", True):
                nt[f"module.{name}"] = {"rect": list(rect), "allow": allow, "reason": why}
            continue
        ko[f"module.{name}"] = {"rect": list(rect), "side": str(m.get("side", "both")), "allow": allow, "reason": why}
        if not m.get("tht", True):
            nt[f"module.{name}"] = {"rect": list(rect), "allow": allow, "reason": why}
    return cons


def height_info(B, cons, ref):
    """(height mm | None, source) of a part: `[placement.heights]` entry, chip-size estimate, or unknown."""
    from . import intent as IT
    fp = str(B["parts"][ref].get("fp", "")).split(":")[-1]
    for g in (_pl(cons).get("heights") or {}):
        if fnmatch.fnmatch(ref, g) or fnmatch.fnmatch(fp, g):
            return IT.height(B, cons, ref), f"[placement.heights] {g}"
    h = IT.height(B, cons, ref)
    return h, ("chip-size estimate" if h is not None else "unknown")


def under_report(B, cons):
    """[{module, side, outline, under_height_mm, limit_mm, parts: [{ref, face, height_mm, source, verdict}]}] for the
    modules that carry `under_height_mm`; verdict ok / too tall / height unknown."""
    expand(cons, B)
    out = []
    for name, m in sorted((_pl(cons).get("module") or {}).items()):
        uh = float(m.get("under_height_mm", 0) or 0)
        rect = _outline(B, m)
        if uh <= 0 or rect is None:
            continue
        lim = uh - float(m.get("under_margin_mm", 0.2))
        allow = set(m.get("refs", [])) | set(m.get("allow", []))
        side = str(m.get("side", "both")).upper()[:1]
        rows = []
        for ref, p in sorted(B["parts"].items()):
            f = "F" if p["side"] == "F.Cu" else "B"
            c = p["court"]
            if ref in allow or (side in SIDES and f != side):
                continue
            if not any(not q.get("np") for q in p["pads"]) or not (c[0] < rect[2] and c[2] > rect[0] and c[1] < rect[3] and c[3] > rect[1]):
                continue
            h, src = height_info(B, cons, ref)
            rows.append({"ref": ref, "face": f, "height_mm": h, "source": src,
                         "verdict": "height unknown" if h is None else "ok" if h <= lim + 1e-9 else "too tall"})
        out.append({"module": name, "side": side, "outline": list(rect), "under_height_mm": uh, "limit_mm": round(lim, 3), "parts": rows})
    return out
