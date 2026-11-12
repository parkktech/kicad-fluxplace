"""Stage 8 gate checks: STALE_COPPER, CONNECTOR_BLOCKED, HEIGHT_REGION, ANCHOR_SPACING.
Pure text (routediag model). Findings use preroute's shape."""
import fnmatch
import math
import re

from . import geom as GM
from . import netclass as NC

PLUG_MM = 15.0
_HORIZ = re.compile(r"horizontal|right.?angle|angled|edge|_RA_|_H_|sideentry|side.?entry", re.I)
_VERT = re.compile(r"vertical|_V_|top.?entry|topentry|_Vert", re.I)
_DIRS = {"+x": (1, 0), "-x": (-1, 0), "+y": (0, 1), "-y": (0, -1)}


def _f(*a, **k):
    from .preroute import _f as f
    return f(*a, **k)


# ------------------------------------------------------------------ stale copper
def _pad_hit(p, x, y, tol):
    return abs(x - p["x"]) <= p["ex"] + tol and abs(y - p["y"]) <= p["ey"] + tol


def _seg_d(px, py, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / L))
    return math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy))


def stale_items(B):
    """(segment indices, via indices) whose ends no longer land on a pad, via or other
    track of their own net. Removing one can strand its neighbour, so this runs to a
    fixpoint (what a --strip-stale would leave is itself clean)."""
    pads = NC.net_pads(B)
    segs, vias = B["segs"], B["vias"]
    sdead, vdead = set(), set()
    zone_nets = {z["net"] for z in B["zones"] if z["net"] and not z["keepout"]}
    while True:
        by_s, by_v = {}, {}
        for i, s in enumerate(segs):
            if i not in sdead:
                by_s.setdefault(s["net"], []).append(i)
        for i, v in enumerate(vias):
            if i not in vdead:
                by_v.setdefault(v["net"], []).append(i)
        new_s, new_v = set(), set()

        def anchored(net, x, y, w, skip_seg=None, skip_via=None):
            tol = w / 2 + 0.02
            if any(_pad_hit(p, x, y, tol) for p in pads.get(net, ())):
                return True
            if any(math.hypot(x - vias[j]["x"], y - vias[j]["y"]) <= vias[j]["d"] / 2 + 0.02
                   for j in by_v.get(net, ()) if j != skip_via):
                return True
            return any(_seg_d(x, y, segs[j]["a"], segs[j]["b"]) <= segs[j]["w"] / 2 + 0.02
                       for j in by_s.get(net, ()) if j != skip_seg)

        for net, idx in by_s.items():
            for i in idx:
                s = segs[i]
                if not (anchored(net, *s["a"], s["w"], skip_seg=i) and anchored(net, *s["b"], s["w"], skip_seg=i)):
                    new_s.add(i)
        for net, idx in by_v.items():
            for i in idx:
                v = vias[i]
                # a via of a poured net is anchored by the pour
                if net in zone_nets:
                    continue
                if not anchored(net, v["x"], v["y"], v["d"], skip_via=i):
                    new_v.add(i)
        if not new_s and not new_v:
            return sorted(sdead), sorted(vdead)
        sdead |= new_s
        vdead |= new_v


def stale_copper(B):
    s, v = stale_items(B)
    if not (s or v):
        return []
    nets = sorted({B["segs"][i]["net"] for i in s} | {B["vias"][i]["net"] for i in v})
    return [_f("WARN", "STALE_COPPER",
               f"{len(s)} track(s) and {len(v)} via(s) have an end that no longer lands on a pad or "
               f"track of their net ({len(nets)} net(s), e.g. {', '.join(nets[:5])}); "
               f"--strip-stale removes them", value=len(s) + len(v))]


def strip_stale_text(text, B):
    """Text with the stale tracks/vias removed (indices follow routediag.load order)."""
    from . import boardwrite as BW
    s, v = stale_items(B)
    return BW.strip_indexed(text, set(s), set(v)), len(s) + len(v)


# ------------------------------------------------------------------ connectors
def opening(B, ref, cons):
    """World unit vector of a connector's opening, or None: declared
    `[placement.connectors] J12 = {opening = "-y"}` (footprint coordinates) else the side
    where the courtyard extends > 1 mm beyond the pad bounding box."""
    part = B["parts"][ref]
    decl = (((cons or {}).get("placement") or {}).get("connectors") or {}).get(ref, {}).get("opening")
    if decl == "z":
        return None, "declared"          # mates along z: no in-plane plug keep-out
    if decl in _DIRS:
        lx, ly = _DIRS[decl]
        dx, dy = GM.rot(lx, ly, part["angle"])
        return (round(dx, 6), round(dy, 6)), "declared"
    name = str(part.get("fp", ""))
    horiz = bool(_HORIZ.search(name))
    if not horiz and _VERT.search(name):
        return None, "vertical"          # footprint name says it mates along z
    pads = [(p["x"] - part["pos"][0], p["y"] - part["pos"][1], p["ex"], p["ey"]) for p in part["pads"]]
    # undo the footprint rotation to work in footprint coordinates
    loc = [GM.rot(x, y, -part["angle"]) + (ex, ey) for x, y, ex, ey in pads]
    cx0, cy0, cx1, cy1 = part["court"]
    cs = [GM.rot(x - part["pos"][0], y - part["pos"][1], -part["angle"]) for x, y in ((cx0, cy0), (cx1, cy0), (cx1, cy1), (cx0, cy1))]
    c = (min(a for a, _ in cs), min(b for _, b in cs), max(a for a, _ in cs), max(b for _, b in cs))
    pb = (min(l[0] - 0 for l in loc), min(l[1] for l in loc), max(l[0] for l in loc), max(l[1] for l in loc))
    slack = {"-x": pb[0] - c[0], "+x": c[2] - pb[2], "-y": pb[1] - c[1], "+y": c[3] - pb[3]}
    k, v = max(slack.items(), key=lambda kv: kv[1])
    if v < 1.0 or sorted(slack.values())[-2] > 0.6 * v:
        return None, "ambiguous"
    if not horiz:   # name is silent: the asymmetry must be strong AND the part at a board edge
        bx0, by0, bx1, by1 = B["bbox"]
        if v < 2.0 or min(cx0 - bx0, cy0 - by0, bx1 - cx1, by1 - cy1) >= 1.5:
            return None, "ambiguous"
    dx, dy = GM.rot(*_DIRS[k], part["angle"])
    return (round(dx, 6), round(dy, 6)), "inferred"


def connector_blocked(B, cons=None):
    """Edge connectors: the opening must face outward across the nearest board edge and
    the plug box projected outward (`plug_mm`, 15) must hit no other part."""
    from . import anchors as AN
    pl = (cons or {}).get("placement") or {}
    plug = float(pl.get("plug_mm", PLUG_MM))
    bx0, by0, bx1, by1 = B["bbox"]
    refs = set((pl.get("connectors") or {}))
    for a in AN.detect(B, cons):
        for r in a["refs"]:
            c = B["parts"][r]["court"]
            if min(c[0] - bx0, c[1] - by0, bx1 - c[2], by1 - c[3]) < 1.5:
                refs.add(r)
    out = []
    for ref in sorted(refs & set(B["parts"])):
        part = B["parts"][ref]
        d, how = opening(B, ref, cons)
        if d is None:
            continue
        c = part["court"]
        edges = {(-1, 0): c[0] - bx0, (1, 0): bx1 - c[2], (0, -1): c[1] - by0, (0, 1): by1 - c[3]}
        near = min(edges, key=edges.get)
        if d[0] * near[0] + d[1] * near[1] < 0.7:
            out.append(_f("FAIL" if how == "declared" else "WARN", "CONNECTOR_BLOCKED",
                          f"{ref}: opening ({how}) faces ({d[0]:+.0f},{d[1]:+.0f}) but the nearest board edge is "
                          f"({near[0]:+d},{near[1]:+d}), {edges[near]:.1f} mm away; the plug cannot enter",
                          refs=[ref]))
            continue
        if abs(d[0]) > abs(d[1]):
            box = (c[2], c[1], c[2] + plug, c[3]) if d[0] > 0 else (c[0] - plug, c[1], c[0], c[3])
        else:
            box = (c[0], c[3], c[2], c[3] + plug) if d[1] > 0 else (c[0], c[1] - plug, c[2], c[1])
        hit = sorted(r for r, p in B["parts"].items() if r != ref and p["side"] == part["side"]
                     and GM.overlap(box, p["court"], -0.05) and not r.startswith(("MH", "MK", "FID", "TP")))
        # the plug is outside the board when the box leaves the outline: nothing to hit there
        if hit:
            out.append(_f("FAIL" if how == "declared" else "WARN", "CONNECTOR_BLOCKED",
                          f"{ref}: a {plug:g} mm plug box ({how} opening) hits {', '.join(hit[:5])}",
                          refs=[ref] + hit[:5], value=len(hit), limit=0))
    return out


# ------------------------------------------------------------------ height regions
def height_regions(B, cons=None):
    """[[placement.height_region]] name, rect=[x0,y0,x1,y1], side="B", max_height_mm; part
    heights from [placement.heights] (ref or glob -> mm). A part in the region with no known
    height is reported as unknown (WARN); a known one above the limit is a FAIL."""
    pl = (cons or {}).get("placement") or {}
    heights = pl.get("heights") or {}
    out = []
    for k in pl.get("height_region", []) or []:
        rect, side, mx = tuple(k["rect"]), str(k.get("side", "B")).upper()[0], float(k["max_height_mm"])
        unknown, over = [], []
        for ref, p in sorted(B["parts"].items()):
            if p["side"][0] != side or ref.startswith(("MH", "MK", "FID", "TP")) \
                    or not GM.overlap(p["court"], rect, -0.01):
                continue
            h = next((float(v) for g, v in heights.items() if fnmatch.fnmatch(ref, g)), None)
            if h is None:
                unknown.append(ref)
            elif h > mx:
                over.append((ref, h))
        name = k.get("name", "height region")
        if over:
            out.append(_f("FAIL", "HEIGHT_REGION",
                          f"{name}: {', '.join(f'{r} ({h:g} mm)' for r, h in over[:6])} exceed {mx:g} mm",
                          refs=[r for r, _ in over], value=max(h for _, h in over), limit=mx))
        if unknown:
            out.append(_f("WARN", "HEIGHT_REGION",
                          f"{name}: height unknown for {len(unknown)} part(s) in the region "
                          f"({', '.join(unknown[:8])}); give [placement.heights] (no STEP bbox is read)",
                          refs=unknown))
    return out


# ------------------------------------------------------------------ side policy
def side_policy(B, cons=None, spec=None):
    """`[placement.side]`: default = "F"|"B" (side movable parts belong on), `override` =
    {ref-or-glob: side}, `flip_cost` (documented only: nothing here flips a part). WARN for
    every part off its side without an override, and for a support part on a different side
    than its IC unless it sits directly beneath one of the IC's pins (< 1 mm)."""
    from . import clusters as CL
    sp = ((cons or {}).get("placement") or {}).get("side") or {}
    if not sp:
        return []
    want = str(sp.get("default", "")).upper()[:1]
    over = sp.get("override") or {}
    skip = ("MH", "MK", "FID", "TP")
    off = []
    for ref, p in sorted(B["parts"].items()):
        if ref.startswith(skip) or p.get("locked"):
            continue
        side = p["side"][0]
        o = next((str(v).upper()[:1] for g, v in over.items() if fnmatch.fnmatch(ref, g)), None)
        if (o or want) and side != (o or want):
            off.append(ref)
    out = []
    if off:
        out.append(_f("WARN", "SIDE_POLICY",
                      f"{len(off)} part(s) are not on their policy side ({want or 'per override'}): "
                      f"{', '.join(off[:10])}", refs=off, value=len(off), limit=0))
    cl, _ = CL.build(B, cons, spec)
    bad = []
    for c in cl:
        ic = B["parts"][c["ic"]]
        for s in c["supports"]:
            part = B["parts"][s["ref"]]
            if part["side"] == ic["side"]:
                continue
            under = any(math.hypot(q["x"] - p["x"], q["y"] - p["y"]) < 1.0
                        for q in ic["pads"] for p in part["pads"])
            if not under:
                bad.append(s["ref"])
    if bad:
        out.append(_f("WARN", "SIDE_POLICY",
                      f"{len(bad)} support part(s) are on the opposite side of their IC and not directly "
                      f"beneath one of its pins: {', '.join(bad[:10])}", refs=bad, value=len(bad), limit=0))
    return out


# ------------------------------------------------------------------ block cohesion
def blocks_of(B, spec=None, cons=None):
    """{block: [refs]} from `[placement.blocks]` (name = [refs or globs]), `[placement] blocks_dir`
    (a folder of block JSONs like docs/v16-blocks: {"block": name, "add_components": [{"ref": ...}]}),
    spec["blocks"] / a component "block" field, else (with `[placement] block_refdes = true`) the
    reference-designator hundreds digit."""
    import glob
    import json
    import os
    out = {}
    pl = (cons or {}).get("placement") or {}
    for k, v in (pl.get("blocks") or {}).items():
        if isinstance(v, list):
            refs = [r for r in B["parts"] if any(fnmatch.fnmatch(r, g) for g in v)]
            out.setdefault(k, []).extend(refs)
    if pl.get("blocks_dir"):
        for f in sorted(glob.glob(os.path.join(pl["blocks_dir"], "*.json"))):
            try:
                d = json.load(open(f))
            except (OSError, ValueError):
                continue
            name = d.get("block") or os.path.splitext(os.path.basename(f))[0]
            for c in list(d.get("add_components", [])) + list(d.get("modify_components", [])):
                if isinstance(c, dict) and c.get("ref") in B["parts"] and c["ref"] not in out.get(name, []):
                    out.setdefault(name, []).append(c["ref"])
    if spec and not out:
        for k, v in (spec.get("blocks") or {}).items():
            out[k] = [r for r in v if r in B["parts"]]
        for c in spec.get("components", []):
            if c.get("block") and c["ref"] in B["parts"]:
                out.setdefault(c["block"], []).append(c["ref"])
    if not out and pl.get("block_refdes"):
        from .clusters import block
        for r in B["parts"]:
            if block(r) and not r.startswith(("MH", "MK", "FID", "TP")):
                out.setdefault(f"{block(r)}xx", []).append(r)
    return {k: v for k, v in out.items() if len(v) >= 2}


def block_mm(B, spec=None, cons=None):
    """Sum over blocks of the half-perimeter of their parts' bounding box (mm): the
    cohesion term of the placement score."""
    t = 0.0
    for refs in blocks_of(B, spec, cons).values():
        xs = [B["parts"][r]["pos"][0] for r in refs]
        ys = [B["parts"][r]["pos"][1] for r in refs]
        t += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return t
