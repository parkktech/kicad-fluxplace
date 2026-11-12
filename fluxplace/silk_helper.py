"""Runs under KiCad's python (/usr/bin/python3, has `pcbnew`), NOT the fluxplace venv.
    python3 silk_helper.py scene BOARD            -> prints "@@SILK@@" + JSON scene (mm) of everything silkscreen touches
    python3 silk_helper.py apply BOARD OUT PLAN   -> applies PLAN.json {ref: {x,y,angle,h,w,t} | {hide: true}} to the
                                                     reference texts of those footprints, saves OUT, prints "@@SILK@@" + counts
The scene is geometry only (polygons, boxes); all decisions live in silk.py. Nothing but reference texts is ever
modified, and `apply` never touches a footprint, track, via or zone."""
import json
import math
import sys

import pcbnew

mm = pcbnew.ToMM
ERR = 15000                                   # nm, polygon approximation error
MARK = "@@SILK@@"


def _p(v):
    return [round(mm(v.x), 4), round(mm(v.y), 4)]


def _rings(ps):
    """SHAPE_POLY_SET -> list of rings {"pts": [[x, y]], "hole": bool} (outlines then their holes)."""
    out = []
    for i in range(ps.OutlineCount()):
        ch = ps.Outline(i)
        out.append({"pts": [_p(ch.CPoint(j)) for j in range(ch.PointCount())], "hole": False})
        for h in range(ps.HoleCount(i)):
            hc = ps.Hole(i, h)
            out.append({"pts": [_p(hc.CPoint(j)) for j in range(hc.PointCount())], "hole": True})
    return out


def _outer(ps):
    return [r["pts"] for r in _rings(ps) if not r["hole"] and len(r["pts"]) >= 3]


def _rot(x, y, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c          # KiCad: +angle is counter-clockwise on screen (y down)


def _ink(t):
    """Ink bounding box of a text in the text's own frame (angle 0), relative to its position, mm:
    (x0, y0, x1, y1) or None for blank text."""
    ps = pcbnew.SHAPE_POLY_SET()
    t.TransformTextToPolySet(ps, 0, ERR, pcbnew.ERROR_INSIDE)
    if ps.OutlineCount() == 0:
        return None
    bb = ps.BBox()
    pos = t.GetPosition()
    return (mm(bb.GetLeft() - pos.x), mm(bb.GetTop() - pos.y), mm(bb.GetRight() - pos.x), mm(bb.GetBottom() - pos.y))


def _text(t, kind, owner, side):
    ang = t.GetTextAngleDegrees()
    t.SetTextAngleDegrees(0)
    box = _ink(t)
    model = None
    if box is not None:
        hj, vj = t.GetHorizJustify(), t.GetVertJustify()
        t.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
        t.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_CENTER)
        c = _ink(t)
        t.SetHorizJustify(hj)
        t.SetVertJustify(vj)
        if c:
            model = {"x0": c[0], "y0": c[1], "x1": c[2], "y1": c[3]}
    t.SetTextAngleDegrees(ang)
    if box is None:
        return None
    pos = t.GetPosition()
    corners = []
    for (cx, cy) in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])):
        rx, ry = _rot(cx, cy, ang)
        corners.append([round(mm(pos.x) + rx, 4), round(mm(pos.y) + ry, 4)])
    return {"kind": kind, "owner": owner, "side": side, "text": t.GetText(), "angle": round(ang, 4),
            "h": round(mm(t.GetTextHeight()), 4), "w": round(mm(t.GetTextWidth()), 4),
            "t": round(mm(t.GetTextThickness()), 4), "mirror": bool(t.IsMirrored()),
            "pos": _p(pos), "quad": corners, "model": model}


def _shape_poly(item, layer):
    ps = pcbnew.SHAPE_POLY_SET()
    item.TransformShapeToPolygon(ps, layer, 0, ERR, pcbnew.ERROR_INSIDE)
    return _outer(ps)


def scene(path):
    b = pcbnew.LoadBoard(path)
    silk = {pcbnew.F_SilkS: "F", pcbnew.B_SilkS: "B"}
    ps = pcbnew.SHAPE_POLY_SET()
    b.GetBoardPolygonOutlines(ps, False)
    out = {"outline": _rings(ps) if ps.OutlineCount() else [], "texts": [], "gfx": [], "pads": [], "holes": [],
           "courts": [], "parts": {}}
    for fp in b.GetFootprints():
        ref = fp.GetReference()
        fside = "B" if fp.GetLayer() == pcbnew.B_Cu else "F"
        crt = fp.GetCourtyard(pcbnew.F_CrtYd if fside == "F" else pcbnew.B_CrtYd)
        court = _outer(crt) if crt.OutlineCount() else []
        pbb = None
        for p in fp.Pads():
            bb = p.GetBoundingBox()
            v = (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom())
            pbb = v if pbb is None else (min(pbb[0], v[0]), min(pbb[1], v[1]), max(pbb[2], v[2]), max(pbb[3], v[3]))
        part = {"side": fside, "name": str(fp.GetFPID().GetLibItemName()), "value": fp.GetValue(),
                "locked": bool(fp.IsLocked()), "n_pads": len(list(fp.Pads())), "court": court,
                "ref_visible": bool(fp.Reference().IsVisible()),
                "ref_layer": "F" if fp.Reference().GetLayer() == pcbnew.F_SilkS else
                             ("B" if fp.Reference().GetLayer() == pcbnew.B_SilkS else "-"),
                "pad_bbox": None if pbb is None else [round(mm(c), 4) for c in pbb],
                "silk_gfx": 0, "silk_marks": 0}
        out["parts"][ref] = part
        if court:
            out["courts"].append({"owner": ref, "side": fside, "polys": court})
        # texts: fields (reference, value, others) and free footprint texts
        for f in fp.GetFields():
            lay = silk.get(f.GetLayer())
            if lay and f.IsVisible() and f.GetText().strip():
                kind = "ref" if f.IsReference() else ("value" if f.IsValue() else "fptext")
                d = _text(f, kind, ref, lay)
                if d:
                    out["texts"].append(d)
        for g in fp.GraphicalItems():
            lay = silk.get(g.GetLayer())
            if not lay:
                continue
            if g.GetClass() == "PCB_TEXT":
                g = pcbnew.Cast_to_PCB_TEXT(g)
                if g.IsVisible() and g.GetText().strip():
                    d = _text(g, "fptext", ref, lay)
                    if d:
                        out["texts"].append(d)
                if lay == fside:
                    part["silk_gfx"] += 1
                    part["silk_marks"] += 1
            else:
                polys = _shape_poly(g, g.GetLayer())
                if polys:
                    out["gfx"].append({"owner": ref, "side": lay, "polys": polys})
                if lay == fside:
                    part["silk_gfx"] += 1
                    if g.GetClass() == "PCB_SHAPE" and g.GetShapeStr() in ("Circle", "Polygon", "Arc"):
                        part["silk_marks"] += 1
        for p in fp.Pads():
            attr = p.GetAttribute()
            npth = attr == pcbnew.PAD_ATTRIB_NPTH
            if not npth:
                for lay, cu, mk in (("F", pcbnew.F_Cu, pcbnew.F_Mask), ("B", pcbnew.B_Cu, pcbnew.B_Mask)):
                    ls = p.GetLayerSet()
                    if not (ls.Contains(cu) or ls.Contains(mk)):
                        continue
                    ex = max(0, p.GetSolderMaskExpansion(mk))
                    q = pcbnew.SHAPE_POLY_SET()
                    p.TransformShapeToPolygon(q, cu if ls.Contains(cu) else mk, ex, ERR, pcbnew.ERROR_OUTSIDE)
                    for poly in _outer(q):
                        out["pads"].append({"owner": ref, "side": lay, "poly": poly, "num": p.GetNumber()})
            if p.HasHole() and npth:
                q = pcbnew.SHAPE_POLY_SET()
                p.TransformHoleToPolygon(q, 0, ERR, pcbnew.ERROR_OUTSIDE)
                for poly in _outer(q):
                    out["holes"].append({"owner": ref, "kind": "npth", "sides": ["F", "B"], "poly": poly})
    for t in b.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            v = pcbnew.Cast_to_PCB_VIA(t)
            sides = [s for s, ml in (("F", pcbnew.F_Mask), ("B", pcbnew.B_Mask)) if not v.IsTented(ml)]
            if not sides:
                continue
            c = v.GetPosition()
            r = mm(v.GetDrillValue()) / 2.0
            poly = [[round(mm(c.x) + r * math.cos(k * math.pi / 8), 4),
                     round(mm(c.y) + r * math.sin(k * math.pi / 8), 4)] for k in range(16)]
            out["holes"].append({"owner": "", "kind": "via", "sides": sides, "poly": poly})
    for d in b.GetDrawings():
        lay = silk.get(d.GetLayer())
        if not lay:
            continue
        if d.GetClass() == "PCB_TEXT":
            d = pcbnew.Cast_to_PCB_TEXT(d)
            if d.IsVisible() and d.GetText().strip():
                x = _text(d, "board", "", lay)
                if x:
                    out["texts"].append(x)
        else:
            try:
                polys = _shape_poly(d, d.GetLayer())
            except Exception:
                polys = []
            if polys:
                out["gfx"].append({"owner": "", "side": lay, "polys": polys})
    out["fab"] = {"net_count": b.GetNetCount()}
    return out


def apply(path, dst, plan):
    b = pcbnew.LoadBoard(path)
    n = {"moved": 0, "hidden": 0, "missing": 0}
    for ref, p in sorted(plan.items()):
        fp = b.FindFootprintByReference(ref)
        if fp is None:
            n["missing"] += 1
            continue
        r = fp.Reference()
        back = fp.GetLayer() == pcbnew.B_Cu
        if p.get("hide"):
            r.SetLayer(pcbnew.B_Fab if back else pcbnew.F_Fab)
            r.SetMirrored(back)
            r.SetVisible(True)
            n["hidden"] += 1
            continue
        r.SetLayer(pcbnew.B_SilkS if back else pcbnew.F_SilkS)
        r.SetVisible(True)
        r.SetMirrored(back)
        r.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
        r.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_CENTER)
        r.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(p["w"]), pcbnew.FromMM(p["h"])))
        r.SetTextThickness(pcbnew.FromMM(p["t"]))
        r.SetTextAngleDegrees(p["angle"])
        r.SetPosition(pcbnew.VECTOR2I(int(round(p["x"] * 1e6)), int(round(p["y"] * 1e6))))
        n["moved"] += 1
    pcbnew.SaveBoard(dst, b)
    return n


def main():
    cmd = sys.argv[1]
    if cmd == "scene":
        res = scene(sys.argv[2])
    elif cmd == "apply":
        res = apply(sys.argv[2], sys.argv[3], json.load(open(sys.argv[4])))
    else:
        raise SystemExit("usage: silk_helper.py scene BOARD | apply BOARD OUT PLAN")
    sys.stdout.write("\n" + MARK + json.dumps(res) + "\n")
    sys.stdout.flush()


main()
