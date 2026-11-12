"""Runs under KiCad's python (/usr/bin/python3, has `pcbnew`), NOT the fluxplace venv:
    python3 flip_helper.py IN.kicad_pcb OUT.kicad_pcb REF [REF ...]
Flips the named footprints left/right about their origin with pcbnew's own Footprint.Flip, saves OUT and
prints JSON {ref: {layer, pos, angle, court, pads: {num: [[x, y, [layers]]...]}}} (mm) of the result so the
caller can verify pads, layers and courtyard against its own model."""
import json
import sys

import pcbnew

mm = pcbnew.ToMM


def facts(fp):
    pads = {}
    for p in fp.Pads():
        pads.setdefault(p.GetNumber(), []).append(
            [round(mm(p.GetPosition().x), 4), round(mm(p.GetPosition().y), 4),
             sorted(pcbnew.LayerName(l) for l in p.GetLayerSet().Seq())])
    lay = fp.GetLayer()
    c = fp.GetCourtyard(pcbnew.F_CrtYd if lay == pcbnew.F_Cu else pcbnew.B_CrtYd)
    bb = c.BBox() if c.OutlineCount() else None
    return {"layer": pcbnew.LayerName(lay), "pos": [round(mm(fp.GetPosition().x), 4), round(mm(fp.GetPosition().y), 4)],
            "angle": round(fp.GetOrientationDegrees(), 4),
            "court": None if bb is None else [round(mm(v), 4) for v in (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom())],
            "pads": pads}


def main():
    src, dst, refs = sys.argv[1], sys.argv[2], sys.argv[3:]
    b = pcbnew.LoadBoard(src)
    out = {}
    for r in refs:
        fp = b.FindFootprintByReference(r)
        if fp is None:
            out[r] = {"error": "no such footprint"}
            continue
        fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        out[r] = facts(fp)
    pcbnew.SaveBoard(dst, b)
    sys.stdout.write("\n@@FLIP@@" + json.dumps(out))


main()
