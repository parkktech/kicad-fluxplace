"""Copper laundering — delete the parasitic copper KiCad's own DRC names.

The routed boards carry classes of junk copper that no router pass will
fix and that swamp the violation count (measured on dig/CM5 after the
board-limits fix):

  - track_dangling / via_dangling — stubs and one-layer vias left by
    capped batch routing and reverted experiments
  - shorting_items — GND stitching vias dropped onto foreign pads/tracks
  - hole_to_hole / hole_clearance — vias drilled inside or against other
    holes (worst measured: a GND via INSIDE J10's NPTH)

Strategy: parse the DRC report, resolve each named Track/Via item back to
a board object (class + net + HitTest position match), delete only
tracks/vias (never pads, never zones), refill pours, and accept the round
only if violations went DOWN and unconnected did not go UP. Iterate:
deleting a stub can orphan its neighbour into the next report.
"""
import json
import os
import re
import subprocess


_DELETABLE = {
    "track_dangling": "single",     # remove the dangling track/via itself
    "via_dangling": "single",
    "shorting_items": "via_first",  # remove the via (or track) of the pair
    "hole_to_hole": "via_only",     # a via drilled against another hole
    "hole_clearance": "gnd_only",   # copper jammed against an NPTH: only
                                    # GND stubs (pours re-heal), guard-safe
}


def _drc(board_path, kicad_cli):
    out = board_path + ".launder_drc.json"
    subprocess.run([kicad_cli, "pcb", "drc", "--format", "json",
                    "--output", out, board_path],
                   capture_output=True, timeout=600)
    with open(out) as f:
        d = json.load(f)
    os.unlink(out)
    return d


def _resolve(board, item, pcbnew):
    """DRC item -> board track/via object (never pads/zones)."""
    desc = item.get("description", "")
    want_via = desc.startswith("Via")
    want_trk = desc.startswith("Track")
    if not (want_via or want_trk):
        return None
    m = re.search(r"\[([^\]]*)\]", desc)
    net = m.group(1) if m else None
    pos = item.get("pos", {})
    v = pcbnew.VECTOR2I(int(pos.get("x", 0) * 1e6),
                        int(pos.get("y", 0) * 1e6))
    for t in board.GetTracks():
        if (t.GetClass() == "PCB_VIA") != want_via:
            continue
        if net is not None and t.GetNetname() != net:
            continue
        if t.HitTest(v):
            return t
    return None


def launder_board(board_path, out_path, kicad_cli="kicad-cli",
                  max_rounds=4, log=print):
    """Returns a summary dict. Writes out_path only when at least one
    round was accepted; otherwise the input is copied through."""
    import pcbnew
    import shutil
    from .patch import refill_zones

    work = out_path + ".launder_work.kicad_pcb"
    shutil.copy(board_path, work)
    for ext in (".kicad_dru", ".kicad_pro"):
        s = os.path.splitext(board_path)[0] + ext
        if os.path.exists(s):
            for tgt in (work, out_path):
                try:
                    shutil.copy(s, os.path.splitext(tgt)[0] + ext)
                except OSError:
                    pass
    d = _drc(work, kicad_cli)
    vio0 = len(d.get("violations", []))
    unc0 = len(d.get("unconnected_items", []))
    total_removed = 0
    accepted_rounds = 0
    vio_prev, unc_prev = vio0, unc0
    for rnd in range(max_rounds):
        board = pcbnew.LoadBoard(work)
        removed = 0
        seen = set()
        for v in d.get("violations", []):
            mode = _DELETABLE.get(v.get("type"))
            if mode is None:
                continue
            items = v.get("items", [])
            objs = [_resolve(board, it, pcbnew) for it in items]
            objs = [o for o in objs if o is not None and id(o) not in seen
                    and o.GetBoard() is not None]
            if not objs:
                continue
            pick = None
            if mode == "single":
                pick = objs[0]
            elif mode == "via_first":
                vias = [o for o in objs if o.GetClass() == "PCB_VIA"]
                pick = vias[0] if vias else objs[0]
            elif mode == "via_only":
                vias = [o for o in objs if o.GetClass() == "PCB_VIA"]
                pick = vias[0] if vias else None
            elif mode == "gnd_only":
                gnd = [o for o in objs if o.GetNetname() == "GND"]
                pick = gnd[0] if gnd else None
            if pick is None:
                continue
            seen.add(id(pick))
            board.Remove(pick)
            removed += 1
        if not removed:
            break
        refill_zones(board)
        tmp = work + ".round.kicad_pcb"
        pcbnew.SaveBoard(tmp, board)
        for ext in (".kicad_dru", ".kicad_pro"):
            s = os.path.splitext(work)[0] + ext
            if os.path.exists(s):
                shutil.copy(s, os.path.splitext(tmp)[0] + ext)
        d1 = _drc(tmp, kicad_cli)
        vio1 = len(d1.get("violations", []))
        unc1 = len(d1.get("unconnected_items", []))
        if vio1 < vio_prev and unc1 <= unc_prev:
            os.replace(tmp, work)
            log(f"    launder round {rnd + 1}: removed {removed} item(s), "
                f"violations {vio_prev}->{vio1}, unconnected "
                f"{unc_prev}->{unc1}")
            vio_prev, unc_prev = vio1, unc1
            total_removed += removed
            accepted_rounds += 1
            d = d1
        else:
            os.unlink(tmp)
            log(f"    launder round {rnd + 1}: removed {removed} but DRC "
                f"{vio_prev}->{vio1}/{unc_prev}->{unc1} — round reverted")
            break
    if accepted_rounds:
        os.replace(work, out_path)
    else:
        shutil.copy(board_path, out_path)
        if os.path.exists(work):
            os.unlink(work)
    return {"rounds": accepted_rounds, "removed": total_removed,
            "violations": (vio0, vio_prev), "unconnected": (unc0, unc_prev)}
