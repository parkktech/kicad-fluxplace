"""Read a .kicad_pcb straight from its S-expression text — no pcbnew.

KiCad deprecated the SWIG `pcbnew` python module in 9.0 and the KiCad 11
nightlies ship without it; its replacement, the IPC API, needs a running GUI
(no headless mode as of 2026-11). The review gate only ever READS the board,
so it does not have to wait for either: this module produces the same `facts`
dict as `review.facts_from_board()`'s SWIG path, from the file alone, on any
KiCad version whose board file it can tokenize.

`tests/test_boardfile.py` holds the contract: on a real board both backends
must agree field by field (footprint bounding boxes to a tolerance — pcbnew
includes pad-shape and line-width detail this reader approximates).

Everything that WRITES a board (place, repair, patch, finish, tune, planes)
still needs SWIG, i.e. KiCad <= 10 — see README "KiCad 11".
"""
import json
import math
import os
from collections import defaultdict

__all__ = ["parse", "facts_from_file", "board_version"]


# --------------------------------------------------------------------------
# S-expressions
# --------------------------------------------------------------------------
def parse(text):
    """Nested lists of str. Quoted and bare atoms both come back as str; an
    empty quoted string stays "" (a pad with `(net "")` is a real case)."""
    out, stack, i, n = None, [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "(":
            node = []
            if stack:
                stack[-1].append(node)
            stack.append(node)
            i += 1
        elif c == ")":
            out = stack.pop()
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                    buf.append({"n": "\n", "t": "\t"}.get(text[j], text[j]))
                else:
                    buf.append(text[j])
                j += 1
            stack[-1].append("".join(buf))
            i = j + 1
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            stack[-1].append(text[i:j])
            i = j
    return out


def _kids(node, tag):
    return [k for k in node if isinstance(k, list) and k and k[0] == tag]


def _first(node, tag):
    for k in node:
        if isinstance(k, list) and k and k[0] == tag:
            return k
    return None


def _atoms(node):
    return [a for a in (node or [])[1:] if isinstance(a, str)]


def _nums(node):
    out = []
    for a in _atoms(node):
        try:
            out.append(float(a))
        except ValueError:
            pass
    return out


def _net(node):
    """Net NAME of an item: KiCad 10 writes (net "GND"), 9 and earlier
    (net 5 "GND") on pads and a bare number on tracks (resolved by caller)."""
    k = _first(node, "net")
    if not k:
        return ""
    a = _atoms(k)
    return a[-1] if a else ""


def board_version(path):
    """(file format version, generator_version) without parsing the board."""
    head = open(path, errors="replace").read(400)
    import re
    v = re.search(r"\(version\s+(\d+)\)", head)
    g = re.search(r'\(generator_version\s+"?([^")]+)', head)
    return (int(v.group(1)) if v else None, g.group(1) if g else None)


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------
def _rot(x, y, deg):
    """KiCad's rotation: positive angle is counter-clockwise on a y-down
    screen."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


def _arc_pts(s, m, e, steps=24):
    """Points along the arc through start, mid, end (for a bounding box)."""
    ax, ay, bx, by, cx, cy = *s, *m, *e
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [s, m, e]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
          + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
          + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux)
    a1 = math.atan2(by - uy, bx - ux)
    a2 = math.atan2(cy - uy, cx - ux)
    tau = 2 * math.pi
    sweep = (a2 - a0) % tau
    if (a1 - a0) % tau > sweep:          # mid is on the other way round
        sweep -= tau
    return [(ux + r * math.cos(a0 + sweep * t / steps),
             uy + r * math.sin(a0 + sweep * t / steps)) for t in range(steps + 1)]


def _shape_pts(g):
    """Outline points of a gr_*/fp_* graphic, in its own frame, plus its
    half line width."""
    tag = g[0].split("_", 1)[-1]
    st = _first(g, "stroke")
    w = (_nums(_first(st, "width")) or [0.0])[0] if st else (
        _nums(_first(g, "width")) or [0.0])[0]
    P = lambda t: tuple((_nums(_first(g, t)) + [0.0, 0.0])[:2]) if _first(g, t) else None
    pts = []
    if tag == "line":
        pts = [P("start"), P("end")]
    elif tag == "rect":
        (x0, y0), (x1, y1) = P("start"), P("end")
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    elif tag == "circle":
        (cx, cy), (ex, ey) = P("center"), P("end")
        r = math.hypot(ex - cx, ey - cy)
        pts = [(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)]
    elif tag == "arc":
        if P("mid"):
            pts = _arc_pts(P("start"), P("mid"), P("end"))
        else:
            pts = [P("start"), P("end")]
    elif tag in ("poly", "curve"):
        for xy in _kids(_first(g, "pts") or [], "xy"):
            v = _nums(xy)
            if len(v) >= 2:
                pts.append((v[0], v[1]))
    return [p for p in pts if p], w / 2.0


def _pad_corners(size, shape):
    w, h = (size + [0.0, 0.0])[:2]
    return [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]


# --------------------------------------------------------------------------
# facts
# --------------------------------------------------------------------------
def facts_from_file(board_path, mpn_map=None, plane_track_max_mm=200.0):
    """The dict `review.facts_from_board` returns, read from the file."""
    from . import review as R
    from . import sourcing as S
    raw = open(board_path, errors="replace").read()
    root = parse(raw)
    copper = [a[1] for a in (_first(root, "layers") or [])[1:]
              if isinstance(a, list) and len(a) > 1 and a[1].endswith(".Cu")]
    order = {l: i for i, l in enumerate(copper)}
    # KiCad <= 9: tracks carry a net NUMBER, names are in a top-level table
    netname = {}
    for k in _kids(root, "net"):
        a = _atoms(k)
        if len(a) >= 2:
            netname[a[0]] = a[1]
    name_of = lambda nm: netname.get(nm, nm)

    parts, net_pads = {}, defaultdict(list)
    dup_bare, pth_pads = set(), []
    for fp in _kids(root, "footprint"):
        props = {}
        for pr in _kids(fp, "property"):
            a = _atoms(pr)
            if len(a) >= 2:
                props.setdefault(a[0], a[1])
        ref = props.get("Reference", "")
        at = _nums(_first(fp, "at")) + [0.0, 0.0, 0.0]
        fx, fy, fang = at[0], at[1], at[2]
        side = "B" if (_atoms(_first(fp, "layer")) or ["F.Cu"])[0] == "B.Cu" else "F"
        pads, conn, total, tht = {}, 0, 0, False
        pad_geom, xs, ys = [], [], []
        for pad in _kids(fp, "pad"):
            a = _atoms(pad)
            num, ptype, shape = a[0], (a[1] if len(a) > 1 else ""), (a[2] if len(a) > 2 else "")
            pat = _nums(_first(pad, "at")) + [0.0, 0.0, 0.0]
            size = _nums(_first(pad, "size"))
            drill = _nums(_first(pad, "drill"))
            dx, dy = _rot(pat[0], pat[1], fang)
            ax, ay = fx + dx, fy + dy
            if drill and drill[0] > 0:
                tht = True
            # absolute pad rectangle: the file's pad angle is already absolute
            for cx, cy in _pad_corners(size, shape):
                rx, ry = _rot(cx, cy, pat[2])
                xs.append(ax + rx)
                ys.append(ay + ry)
            pad_geom.append((num, pat[0], pat[1], (size + [0, 0])[0], (size + [0, 0])[1]))
            if not num:
                continue
            if num.isdigit():
                total += 1
            if ptype == "thru_hole" and drill and drill[0] > 0:
                pth_pads.append({"ref": ref, "pad": num,
                                 "drill_mm": round(drill[0], 4),
                                 "dia_mm": round(min(size[:2]), 4) if size else 0.0,
                                 "x": round(ax, 4), "y": round(ay, 4)})
            if num not in pads and num.isdigit():
                conn += 1
            nn = name_of(_net(pad))
            if num in pads and pads[num] and not nn:
                dup_bare.add((ref, num))
                continue
            if num in pads and nn and not pads[num]:
                dup_bare.add((ref, num))
            pads[num] = nn or pads.get(num, "")
            if nn:
                net_pads[nn].append((ref, num))
        for g in fp:
            if isinstance(g, list) and g and g[0].startswith("fp_") and g[0] != "fp_text":
                pts, hw = _shape_pts(g)
                for px, py in pts:
                    rx, ry = _rot(px, py, fang)
                    xs += [fx + rx - hw, fx + rx + hw]
                    ys += [fy + ry - hw, fy + ry + hw]
        fpname = (_atoms(fp) or [""])[0]
        parts[ref] = {
            "pos": (round(fx, 4), round(fy, 4)), "side": side,
            "bbox": ((round(min(xs), 4), round(min(ys), 4),
                      round(max(xs), 4), round(max(ys), 4)) if xs
                     else (round(fx, 4), round(fy, 4), round(fx, 4), round(fy, 4))),
            "value": props.get("Value", ""),
            "footprint": fpname,
            "pads": pads, "connectable_pads": conn, "pads_total": total,
            "models": [(_atoms(m) or [""])[0] for m in _kids(fp, "model")],
            "pad_geom": pad_geom,
            "mech": R.is_mechanical(ref, fpname, pads),
            "tht": tht,
        }

    tracks, layer_len = {}, defaultdict(float)
    vias, via_type_counts, track_geom = [], defaultdict(int), []

    def entry(n):
        return tracks.setdefault(n, {"length": 0.0, "layers": defaultdict(float),
                                     "vias": 0, "segments": defaultdict(float)})

    for node in root:
        if not isinstance(node, list) or not node:
            continue
        tag = node[0]
        if tag not in ("segment", "arc", "via"):
            continue
        n = name_of(_net(node))
        if not n:
            continue
        d = entry(n)
        if tag == "via":
            d["vias"] += 1
            ls = sorted(_atoms(_first(node, "layers")), key=lambda l: order.get(l, 99))
            top, bot = (ls[0], ls[-1]) if ls else ("F.Cu", "B.Cu")
            flags = [a for a in node[1:] if isinstance(a, str)]
            outer = (copper[0], copper[-1]) if copper else ("F.Cu", "B.Cu")
            if "micro" in flags:
                kind = "micro"
            elif "buried" in flags:
                kind = "buried"
            elif "blind" in flags:
                kind = "blind" if (top in outer or bot in outer) else "buried"
            elif top == outer[0] and bot == outer[1]:
                kind = "through"
            else:
                kind = "blind" if (top in outer or bot in outer) else "buried"
            via_type_counts[kind] += 1
            pos = _nums(_first(node, "at")) + [0.0, 0.0]
            vias.append({"net": n, "type": kind,
                         "x": round(pos[0], 4), "y": round(pos[1], 4),
                         "layers": f"{top}-{bot}",
                         "dia_mm": round((_nums(_first(node, "size")) or [0.0])[0], 4),
                         "drill_mm": round((_nums(_first(node, "drill")) or [0.0])[0], 4)})
            continue
        lname = (_atoms(_first(node, "layer")) or [""])[0]
        w = round((_nums(_first(node, "width")) or [0.0])[0], 4)
        s = (_nums(_first(node, "start")) + [0.0, 0.0])[:2]
        e = (_nums(_first(node, "end")) + [0.0, 0.0])[:2]
        track_geom.append({"net": n, "layer": lname, "width": w,
                           "x": round((s[0] + e[0]) / 2, 4),
                           "y": round((s[1] + e[1]) / 2, 4),
                           "kind": "arc" if tag == "arc" else "track"})
        if tag != "segment":
            continue
        ln = math.hypot(e[0] - s[0], e[1] - s[1])
        d["length"] += ln
        d["layers"][lname] += ln
        d["segments"][(lname, w)] += ln
        layer_len[lname] += ln
    for n, d in tracks.items():
        d["layers"] = dict(d["layers"])
        d["segments"] = [(l, w, mm) for (l, w), mm in d["segments"].items()]

    zones = defaultdict(set)
    for z in _kids(root, "zone"):
        zl = _atoms(_first(z, "layer")) + _atoms(_first(z, "layers"))
        zn = name_of(_net(z))
        for l in zl:
            if l == "*.Cu":
                targets = copper
            elif l == "F&B.Cu":
                targets = [copper[0], copper[-1]] if copper else []
            else:
                targets = [l]
            for t in targets:
                zones[t].add(zn)
    planes = [l for l, nets in zones.items()
              if nets and layer_len.get(l, 0.0) <= plane_track_max_mm]

    ex, ey = [], []
    for g in root:
        if isinstance(g, list) and g and g[0].startswith("gr_") and (
                _atoms(_first(g, "layer")) or [""])[0] == "Edge.Cuts":
            pts, hw = _shape_pts(g)
            for px, py in pts:
                ex += [px - hw, px + hw]
                ey += [py - hw, py + hw]
    size = (max(ex) - min(ex), max(ey) - min(ey)) if ex else (0.0, 0.0)
    outline_bbox = ([round(min(ex), 4), round(min(ey), 4), round(max(ex), 4),
                     round(max(ey), 4)] if ex else None)
    from . import sheet as SH
    page = SH.page_from_tree(root)

    mp = {}
    path = mpn_map or S.find_map(board_path)
    if path and os.path.exists(path):
        for ref, mpn in json.load(open(path)).items():
            if not ref.startswith("_") and isinstance(mpn, str) and mpn:
                mp[ref] = mpn

    design_min = {}
    pro = os.path.splitext(board_path)[0] + ".kicad_pro"
    if os.path.exists(pro):
        try:
            r = json.load(open(pro))["board"]["design_settings"]["rules"]
            design_min = {"track_mm": round(float(r.get("min_track_width", 0.0)), 4),
                          "via_mm": round(float(r.get("min_via_diameter", 0.0)), 4),
                          "drill_mm": round(float(r.get("min_through_hole_diameter", 0.0)), 4),
                          "clearance_mm": round(float(r.get("min_clearance", 0.0)), 4)}
        except (KeyError, ValueError, TypeError):
            design_min = {}
    return {
        "board_path": board_path, "board": board_path, "backend": "file",
        "size_mm": size,
        "outline_bbox": outline_bbox, "page": page,
        "copper_layers": len(copper),
        "parts": parts,
        "net_pads": dict(net_pads),
        "net_tracks": tracks,
        "stackup": R.parse_stackup_text(raw),
        "plane_layers": sorted(planes),
        "layer_track_mm": dict(layer_len),
        "mpn_map": mp, "mpn_map_path": path,
        "dup_bare_pads": sorted(dup_bare),
        "vias": vias, "via_type_counts": dict(via_type_counts),
        "track_geom": track_geom,
        "pth_pads": pth_pads,
        "design_min": design_min,
    }
