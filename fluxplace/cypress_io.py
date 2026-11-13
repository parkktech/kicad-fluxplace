"""Board model <-> the Bookshelf files NVIDIA Cypress (github.com/NVlabs/Cypress, a DREAMPlace fork) reads.

What Cypress takes: rectangles on a row grid (`.nodes`, `.scl`), nets as pin offsets from the cell centre
(`.nets`), one weight per net (`.wts`), a start position, an orientation N/W/S/E (0/90/180/270 CCW) with an
`F` prefix for the bottom side, and `/FIXED` cells (`.pl`). Nothing else. So this module turns

    courtyard (+ spacing, + the legaliser's escape allowance of fine-pitch parts)  -> a movable cell
    fixed parts (locks, anchors, connectors)                                      -> fixed cells (both sides when through-hole)
    keep-outs, rule areas and everything outside a non-rectangular outline         -> fixed cells without pins
    net class weights (netclass.classify)                                          -> net weights; weight-0 rails are dropped
    IC support clusters (clusters.build)                                           -> one 2-pin net per support part and IC pin
    rigid units (`macros`: an IC laid out with its support parts, a rigid group)   -> ONE movable cell with every member's pins
    relations (`links`: intent `near`, pair partners)                              -> extra weighted nets between named pads

The grid is y-up, the board y-down: y is mirrored, which keeps "counter-clockwise" the same in both.
"""
import math
import os

from . import boardfile as BF
from . import geom as GM

UNIT_MM = 0.1
ORIENT = {"N": 0.0, "W": 90.0, "S": 180.0, "E": 270.0}


def _side(part):
    return "B" if part["side"] == "B.Cu" else "F"


def outline_blocks(B, step=1.0):
    """Rectangles (mm) of the bounding box that lie outside the outline, as row runs of `step` squares."""
    edge = B.get("edge")
    if not edge:
        return []
    x0, y0, x1, y1 = B["bbox"]
    out = []
    ny, nx = int(math.ceil((y1 - y0) / step)), int(math.ceil((x1 - x0) / step))
    for j in range(ny):
        run = None
        for i in range(nx + 1):
            off = i < nx and not GM.edge_has(edge, x0 + (i + 0.5) * step, y0 + (j + 0.5) * step)
            if off and run is None:
                run = i
            elif not off and run is not None:
                out.append((x0 + run * step, y0 + j * step, min(x1, x0 + i * step), min(y1, y0 + (j + 1) * step)))
                run = None
    return out


def build_model(B, fixed, cls, clusters=(), keepouts=(), allowance=None, unit=UNIT_MM, gap=0.2,
                cluster_weight=8.0, rail_weight=0.0, max_degree=40, macros=None, links=(), net_weights=None,
                fixed_allowance=False):
    """-> model dict: {unit, size (W, H), cells [{name, ref, members, w, h, x, y, side, fixed, off}], nets [{name, weight, pins}]}.
    Cell x, y = lower-left in grid units; `off` = cell centre minus the part origin (mm, at the part's current angle);
    `members` = {ref: off} (one entry, or every part of a macro).
    `keepouts` = [(rect mm, sides {"F","B"})]; `allowance(ref, part)` -> (left, top, right, bottom) mm of extra room.
    `macros` = {key: [refs]}: movable parts that are ONE cell (the union of their rectangles, on the side of the first ref).
    `links` = [{name, pads: [(ref, pad number), ...], weight}] extra nets; `net_weights` = {net: weight} overrides the class weight;
    a cluster entry may carry its own `link_weight`. `fixed_allowance`: a fixed part's cell includes its allowance too
    (the escape room of a held fine-pitch part is not offered to Cypress)."""
    x0, y0, x1, y1 = B["bbox"]
    W, H = int(math.ceil((x1 - x0) / unit)), int(math.ceil((y1 - y0) / unit))
    cells, where = [], {}

    def add(name, rect, side, fx, ref=None, origin=None):
        w = max(1, int(math.ceil((rect[2] - rect[0]) / unit - 1e-9)))
        h = max(1, int(math.ceil((rect[3] - rect[1]) / unit - 1e-9)))
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        c = {"name": name, "ref": ref, "w": w, "h": h, "side": side, "fixed": fx,
             "x": (cx - x0) / unit - w / 2.0, "y": (y1 - cy) / unit - h / 2.0,
             "off": (cx - origin[0], cy - origin[1]) if origin else (0.0, 0.0)}
        cells.append(c)
        return c

    def room(ref, g=gap / 2.0):
        p = B["parts"][ref]
        c = p["court"]
        a = allowance(ref, p) if allowance else (0.0,) * 4
        return (c[0] - a[0] - g, c[1] - a[1] - g, c[2] + a[2] + g, c[3] + a[3] + g)

    n = 0
    in_macro = set()
    for key in sorted(macros or {}):
        refs = [r for r in macros[key] if r in B["parts"] and r not in fixed and r not in in_macro
                and B["parts"][r]["court"][2] > B["parts"][r]["court"][0]]
        if len(refs) < 2:
            continue
        rs = [room(r) for r in refs]
        u = (min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs))
        cell = add(f"m{n}", u, _side(B["parts"][refs[0]]), False, refs[0], B["parts"][refs[0]]["pos"])
        cx, cy = (u[0] + u[2]) / 2, (u[1] + u[3]) / 2
        cell["members"] = {r: (cx - B["parts"][r]["pos"][0], cy - B["parts"][r]["pos"][1]) for r in refs}
        cell["macro"] = key
        for r in refs:
            where[r] = cell
        in_macro |= set(refs)
        n += 1
    for ref in sorted(B["parts"]):
        p = B["parts"][ref]
        c = p["court"]
        if c[2] - c[0] <= 0 or c[3] - c[1] <= 0 or ref in in_macro:
            continue
        if ref in fixed:
            sides = {"F", "B"} if p["tht"] else ({_side(p)} | set(p.get("court_layers", ())))
            first = _side(p) if _side(p) in sides else sorted(sides)[0]
            where[ref] = add(f"f{n}", room(ref, 0.0) if fixed_allowance else c, first, True, ref, p["pos"])
            for s in sorted(sides - {first}):                     # the other side is occupied too
                add(f"f{n}x", c, s, True)
        else:
            where[ref] = add(f"c{n}", room(ref), _side(p), False, ref, p["pos"])
        where[ref]["members"] = {ref: where[ref]["off"]}
        n += 1
    blocks = [(r, {"F", "B"}) for r in outline_blocks(B)] + list(keepouts)
    for k, (r, sides) in enumerate(blocks):
        r = (max(r[0], x0), max(r[1], y0), min(r[2], x1), min(r[3], y1))
        if r[2] - r[0] > 0 and r[3] - r[1] > 0:
            for s in sorted(sides):
                add(f"k{k}{s}", r, s, True)

    def pin(pad):
        c = where.get(pad["ref"])
        if c is None:
            return None
        ox, oy = B["parts"][pad["ref"]]["pos"]
        off = c["members"][pad["ref"]]
        return (c["name"], (pad["x"] - ox - off[0]) / unit, -(pad["y"] - oy - off[1]) / unit)

    by = {}
    for q in B["pads"]:
        if q["net"]:
            by.setdefault(q["net"], []).append(q)
    nets = []
    for net in sorted(by):
        info = cls.get(net)
        wt = info["weight"] if info else 1.0
        if info and wt <= 0:
            wt = rail_weight
        wt = (net_weights or {}).get(net, wt)
        pins = [t for t in (pin(q) for q in by[net]) if t]
        if len({t[0] for t in pins}) < 2:                    # every pin inside one cell (a macro): nothing to pull
            continue
        if wt <= 0 or len(pins) < 2 or len(pins) > max_degree:
            continue
        if all(where[q["ref"]]["fixed"] for q in by[net] if q["ref"] in where):
            continue
        nets.append({"name": f"n{len(nets)}", "net": net, "weight": float(wt), "pins": pins})
    pad_of = {(q["ref"], q["num"]): q for q in B["pads"]}
    for c in clusters:
        for s in c["supports"]:
            a, b = pad_of.get((s["ref"], s.get("via"))), pad_of.get((c["ic"], s["pad"]))
            pa, pb = (pin(a) if a else None), (pin(b) if b else None)
            if pa and pb and pa[0] != pb[0] and not (where[s["ref"]]["fixed"] and where[c["ic"]]["fixed"]):
                nets.append({"name": f"n{len(nets)}", "net": f"support:{s['ref']}", "pins": [pa, pb],
                             "weight": float(s.get("link_weight") or max(cluster_weight, s.get("weight") or 0.0))})
    for l in links:
        pins = [t for t in (pin(pad_of[k]) for k in l["pads"] if k in pad_of) if t]
        cs = {t[0] for t in pins}
        if len(pins) >= 2 and len(cs) >= 2 and not all(c["fixed"] for c in cells if c["name"] in cs):
            nets.append({"name": f"n{len(nets)}", "net": l["name"], "pins": pins, "weight": float(l["weight"])})
    return {"unit": unit, "size": (W, H), "cells": cells, "nets": nets, "bbox": (x0, y0, x1, y1)}


def write_bookshelf(model, folder, name="board"):
    """Write name.{aux,nodes,nets,wts,pl,scl} into `folder`; -> path of the .aux file."""
    os.makedirs(folder, exist_ok=True)
    W, H = model["size"]
    cells = sorted(model["cells"], key=lambda c: c["fixed"])            # movable first
    nt = sum(1 for c in cells if c["fixed"])
    p = lambda ext: os.path.join(folder, f"{name}.{ext}")
    with open(p("nodes"), "w") as f:
        f.write(f"UCLA nodes 1.0\n\nNumNodes : {len(cells)}\nNumTerminals : {nt}\n\n")
        for c in cells:
            f.write(f"{c['name']} {c['w']} {c['h']}" + (" terminal\n" if c["fixed"] else "\n"))
    with open(p("nets"), "w") as f:
        f.write(f"UCLA nets 1.0\n\nNumNets : {len(model['nets'])}\nNumPins : {sum(len(n['pins']) for n in model['nets'])}\n\n")
        for n in model["nets"]:
            f.write(f"NetDegree : {len(n['pins'])} {n['name']}\n")
            for cell, dx, dy in n["pins"]:
                f.write(f"\t{cell} I : {dx:.4f} {dy:.4f}\n")
    with open(p("wts"), "w") as f:
        f.write("UCLA wts 1.0\n\n")
        for n in model["nets"]:
            f.write(f"{n['name']} {n['weight']:g}\n")
    with open(p("pl"), "w") as f:
        f.write("UCLA pl 1.0\n\n")
        for c in cells:
            o = ("F" if c["side"] == "B" else "") + "N"
            x = min(max(c["x"], 0), max(0, W - c["w"])) if not c["fixed"] else c["x"]
            y = min(max(c["y"], 0), max(0, H - c["h"])) if not c["fixed"] else c["y"]
            f.write(f"{c['name']} {int(round(x))} {int(round(y))} : {o}" + (" /FIXED\n" if c["fixed"] else "\n"))
    with open(p("scl"), "w") as f:
        f.write(f"UCLA scl 1.0\n\nNumRows : {H}\n\n")
        for j in range(H):
            f.write(f"CoreRow Horizontal\n    Coordinate   : {j}\n    Height       : 1\n    Sitewidth    : 1\n"
                    f"    Sitespacing  : 1\n    Siteorient   : {j % 2}\n    Sitesymmetry : 1\n"
                    f"    SubrowOrigin : 0  NumSites : {W}\nEnd\n")
    with open(p("aux"), "w") as f:
        f.write(f"RowBasedPlacement : {name}.nodes {name}.nets {name}.wts {name}.pl {name}.scl\n")
    return p("aux")


def read_pl(path):
    """{cell name: (x, y, orient string)} from a Bookshelf .pl file (fixed cells included)."""
    out = {}
    for line in open(path, errors="replace"):
        t = line.split()
        if len(t) >= 5 and t[3] == ":":
            try:
                out[t[0]] = (float(t[1]), float(t[2]), t[4])
            except ValueError:
                pass
    return out


def targets(model, B, placed):
    """Cypress cell positions -> {ref: (x, y, angle)} origins in board mm for the movable parts.
    A cell is turned about its centre, so the origin is the centre minus the turned centre offset."""
    x0, _y0, _x1, y1 = model["bbox"]
    u = model["unit"]
    out = {}
    for c in model["cells"]:
        if c["fixed"] or not c["ref"] or c["name"] not in placed:
            continue
        x, y, o = placed[c["name"]]
        th = ORIENT.get(o.lstrip("F")[:1] or "N", 0.0)
        cx, cy = x0 + (x + c["w"] / 2.0) * u, y1 - (y + c["h"] / 2.0) * u
        for ref, off in c["members"].items():                 # a macro turns as one body about the cell centre
            dx, dy = BF._rot(off[0], off[1], th)
            out[ref] = (round(cx - dx, 4), round(cy - dy, 4), (B["parts"][ref]["angle"] + th) % 360.0)
    return out


def utilisation(model):
    """Movable cell area over the free area (grid minus fixed cells, overlaps counted once per 1/8 grid), worst side."""
    W, H = model["size"]
    k = max(1, min(W, H) // 256)
    worst = 0.0
    for side in ("F", "B"):
        gw, gh = W // k + 1, H // k + 1
        occ = bytearray(gw * gh)
        for c in model["cells"]:
            if c["fixed"] and c["side"] == side:
                for j in range(max(0, int(c["y"]) // k), min(gh, int(c["y"] + c["h"]) // k + 1)):
                    i0, i1 = max(0, int(c["x"]) // k), min(gw, int(c["x"] + c["w"]) // k + 1)
                    occ[j * gw + i0:j * gw + i1] = b"\x01" * max(0, i1 - i0)
        free = max(1.0, float(W * H) - sum(occ) * k * k)
        worst = max(worst, sum(c["w"] * c["h"] for c in model["cells"] if not c["fixed"] and c["side"] == side) / free)
    return worst
