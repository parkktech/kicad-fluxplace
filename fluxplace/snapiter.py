"""Via-centre snap with a DRC acceptance rule: every track end inside a same-net via's copper moves to the via centre; the
tracks named by any NEW violation (drcguard keys, unconnected included) keep their ends; repeat until nothing new appears."""
import json
import math
import os
import shutil
import subprocess
from collections import Counter

from . import drcguard as DG

CENTRED = "track_not_centered_on_via"


def _report(board, cli="kicad-cli"):
    out = board + ".drc.json"
    subprocess.run([cli, "pcb", "drc", "--severity-error", "--format", "json", "--refill-zones", "-o", out, board],
                   capture_output=True, timeout=900)
    d = json.load(open(out))
    os.unlink(out)
    return d


def _keys(d):
    vio = [v for v in d.get("violations", []) if v.get("type") != CENTRED]
    return Counter([DG.key_of(v) for v in vio] + [DG.key_of(u, "U") for u in d.get("unconnected_items", [])])


def _snap(src, dst, skip):
    import pcbnew
    b = pcbnew.LoadBoard(src)
    vias = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T]
    n = 0
    for t in b.GetTracks():
        if t.Type() != pcbnew.PCB_TRACE_T or t.m_Uuid.AsString() in skip:
            continue
        for get, put in ((t.GetStart, t.SetStart), (t.GetEnd, t.SetEnd)):
            p = get()
            for v in vias:
                if v.GetNetCode() == t.GetNetCode() and v.IsOnLayer(t.GetLayer()) and v.GetPosition() != p and \
                        math.hypot(p.x - v.GetPosition().x, p.y - v.GetPosition().y) <= v.GetWidth() / 2:
                    put(v.GetPosition())
                    n += 1
                    break
    pcbnew.SaveBoard(dst, b)
    return n


def run(src, dst, iters=6, log=print):
    """-> {"moved", "excluded", "iters", "centred_left"}; `dst` = the last accepted board (src when none is)."""
    base = _keys(_report(src))
    skip, last = set(), None
    for i in range(iters):
        tmp = dst + f".i{i}.kicad_pcb"
        moved = _snap(src, tmp, skip)
        shutil.copy(os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(tmp)[0] + ".kicad_pro")
        d = _report(tmp)
        new = DG.new_keys(base, _keys(d))
        bad = [v for v in d["violations"] if v["type"] != CENTRED and DG.key_of(v) in new]
        bad += [u for u in d.get("unconnected_items", []) if DG.key_of(u, "U") in new]
        ids = {it["uuid"] for v in bad for it in v["items"] if "uuid" in it}
        log(f"snap iter {i}: moved {moved}, skipped {len(skip)}, new keys {sum(new.values())}")
        if not new:
            last = (tmp, moved, sum(v["type"] == CENTRED for v in d["violations"]))
            break
        if not ids - skip:
            break
        skip |= ids
    if last:
        shutil.copy(last[0], dst)
        shutil.copy(os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(dst)[0] + ".kicad_pro")
    return {"moved": last[1] if last else 0, "excluded": len(skip), "iters": i + 1, "centred_left": last[2] if last else None}
