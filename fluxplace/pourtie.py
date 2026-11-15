"""Pour-first connections: a pad of a net that has a pour or plane is tied THROUGH that copper before the router runs.

tie stage (after fan-out, before pairs/route): every pad of a pour net is either already reached by its filled pour (on the
pad's own pour layer) or reached through same-net copper to a via that lands in filled copper of the net; the rest get a
short stub + via into the pour at a legal site (planes.via_at_points + hole_ok).  Every tie is verified against the FILLED
zone geometry after one refill; a tie that does not verify is undone.  Layers come from the board's zones, never named here.
The verified ties are written to pourtie.json; repair() re-checks them after routing and re-ties what routing cut off."""
import json
import math
import os

import pcbnew

from . import planes as PL

TOL = pcbnew.FromMM(0.02)


def _copper_lids(obj_layers):
    return [l for l in obj_layers if pcbnew.IsCopperLayer(l)]


def pour_map(board):
    """{net: [layer_id, ...]} for every net with a copper zone (rule areas and unnamed zones skipped)."""
    out = {}
    for z in board.Zones():
        if z.GetIsRuleArea() or not z.GetNetname():
            continue
        out.setdefault(z.GetNetname(), set()).update(_copper_lids(z.GetLayerSet().Seq()))
    return {n: sorted(v) for n, v in out.items()}


def fill_hit(board, net, lid, x, y):
    for z in board.Zones():
        if z.GetIsRuleArea() or z.GetNetname() != net or not z.IsOnLayer(lid):
            continue
        if z.HitTestFilledArea(lid, pcbnew.VECTOR2I(int(x), int(y)), 0):
            return True
    return False


def in_outline(board, net, x, y):
    """the point lies inside an outline of one of the net's copper zones (a pour cannot fill outside its outline)."""
    for z in board.Zones():
        if not z.GetIsRuleArea() and z.GetNetname() == net and z.Outline().Contains(pcbnew.VECTOR2I(int(x), int(y))):
            return True
    return False


def pad_copper_lids(pad):
    return _copper_lids(pad.GetLayerSet().Seq())


def _pad_xy(pad):
    p = pad.GetPosition()
    return p.x, p.y


def tie_status(board, net, pad, pours):
    """-> ("pour-layer", layer) | ("via", (x_mm, y_mm)) | (None, None): how the pad reaches the net's filled copper now."""
    x, y = _pad_xy(pad)
    for lid in pours:
        if pad.IsOnLayer(lid) and fill_hit(board, net, lid, x, y):
            return "pour-layer", board.GetLayerName(lid)
    # same-net copper graph from the pad centre: tracks join at endpoints, vias join every layer
    segs = [t for t in board.GetTracks() if t.GetNetname() == net]
    vias = [t for t in segs if t.Type() == pcbnew.PCB_VIA_T]
    tracks = [t for t in segs if t.Type() != pcbnew.PCB_VIA_T]
    # a stub starts on the pad's copper (its edge, not necessarily its centre): every same-net track or via end inside the
    # pad's box on the pad's layers, or the pad centre itself, is a start point
    box = pad.GetBoundingBox()
    box.Inflate(TOL, TOL)
    lids = set(pad_copper_lids(pad))
    starts = {(x, y)}
    for t in tracks:
        if t.GetLayer() in lids:
            for e in (t.GetStart(), t.GetEnd()):
                if box.Contains(e):
                    starts.add((e.x, e.y))
    for v in vias:
        if box.Contains(v.GetPosition()):
            starts.add((v.GetPosition().x, v.GetPosition().y))
    frontier = list(starts)
    seen = set(starts)
    checked = set()                    # vias already tested (a via is also a track end: `seen` must not hide it)
    for _hop in range(8):
        nxt = []
        for px, py in frontier:
            for v in vias:
                q = v.GetPosition()
                if abs(q.x - px) <= TOL and abs(q.y - py) <= TOL and (q.x, q.y) not in checked:
                    checked.add((q.x, q.y))
                    for lid in pours:
                        if fill_hit(board, net, lid, q.x, q.y):
                            return "via", (round(pcbnew.ToMM(q.x), 4), round(pcbnew.ToMM(q.y), 4))
                    nxt.append((q.x, q.y))
            for t in tracks:
                for e in (t.GetStart(), t.GetEnd()):
                    if abs(e.x - px) <= TOL and abs(e.y - py) <= TOL:
                        for f in (t.GetStart(), t.GetEnd()):
                            if (f.x, f.y) not in seen:
                                seen.add((f.x, f.y))
                                nxt.append((f.x, f.y))
        frontier = nxt
        if not frontier:
            break
    return None, None


def _ring_sites(x, y):
    for r_mm in (0.0, 0.8, 1.1, 1.4):
        if r_mm == 0.0:
            yield r_mm, x, y
            continue
        for k in range(12):
            a = k * math.pi / 6
            yield r_mm, x + pcbnew.FromMM(r_mm * math.cos(a)), y + pcbnew.FromMM(r_mm * math.sin(a))


def _via_keys(board):
    return {(t.GetPosition().x, t.GetPosition().y) for t in board.GetTracks() if t.Type() == pcbnew.PCB_VIA_T}


def _add_tie(board, net, pad, pours, made):
    """stub (if the site is off the pad) + via at the first legal ring site; the created objects go into `made`."""
    x, y = _pad_xy(pad)
    nc = board.FindNet(net)
    layer = pad_copper_lids(pad)[0]
    for r_mm, cx, cy in _ring_sites(x, y):
        if not PL.hole_ok(board, cx, cy):
            continue
        before = _via_keys(board)
        if PL.via_at_points(board, net, [(pcbnew.ToMM(cx), pcbnew.ToMM(cy))]) != 1:
            continue
        made.extend(t for t in board.GetTracks() if t.Type() == pcbnew.PCB_VIA_T and (t.GetPosition().x, t.GetPosition().y) not in before)
        if r_mm > 0.0:
            tr = pcbnew.PCB_TRACK(board)
            tr.SetStart(pcbnew.VECTOR2I(int(x), int(y)))
            tr.SetEnd(pcbnew.VECTOR2I(int(cx), int(cy)))
            tr.SetWidth(pcbnew.FromMM(0.15))
            tr.SetLayer(layer)
            board.Add(tr)
            tr.SetNet(nc)                  # the net is set once the track is on the board (a pre-Add net was lost on save)
            made.append(tr)
        return True
    return False


def _undo(board, made):
    for o in made:
        if o is not None:
            board.Remove(o)


def _tie_pads(board, net, pads, pours):
    """-> (tied: [record], untied: [record]); verified after one refill."""
    cands = []
    outside = []
    for pad in pads:
        how, where = tie_status(board, net, pad, pours)
        if how:
            continue
        x, y = _pad_xy(pad)
        if not in_outline(board, net, x, y):
            outside.append(pad)              # no via can land in a pour that does not reach the pad: the pour is the limit
            continue
        made = []
        cands.append((pad, made if _add_tie(board, net, pad, pours, made) else None))
    PL.refill(board)
    tied, untied = [], []
    for pad in outside:
        x, y = _pad_xy(pad)
        untied.append({"net": net, "pad": pad.GetNumber(), "at": [round(pcbnew.ToMM(x), 4), round(pcbnew.ToMM(y), 4)],
                       "reason": "pad outside the net's pour outline (a larger pour or a track is needed)"})
    for pad, made in cands:
        x, y = _pad_xy(pad)
        if made is None:
            untied.append({"net": net, "pad": pad.GetNumber(), "at": [round(pcbnew.ToMM(x), 4), round(pcbnew.ToMM(y), 4)],
                           "reason": "no legal via site within 1.4 mm (hole, clearance or stub)"})
            continue
        via = [m for m in made if m.Type() == pcbnew.PCB_VIA_T]
        ok = bool(via) and any(fill_hit(board, net, lid, via[-1].GetPosition().x, via[-1].GetPosition().y) for lid in pours)
        if via:
            vx, vy = via[-1].GetPosition().x, via[-1].GetPosition().y
        if ok:
            tied.append({"net": net, "pad": pad.GetNumber(), "at": [round(pcbnew.ToMM(x), 4), round(pcbnew.ToMM(y), 4)],
                         "via": ([round(pcbnew.ToMM(vx), 4), round(pcbnew.ToMM(vy), 4)] if via else None)})
        else:
            _undo(board, made)
            untied.append({"net": net, "pad": pad.GetNumber(), "at": [round(pcbnew.ToMM(x), 4), round(pcbnew.ToMM(y), 4)],
                           "reason": "via does not land in the filled pour (verified on the filled zone)"})
    if tied or untied:
        PL.refill(board)
    return tied, untied


def run(board_in, board_out, workdir, nets=None):
    """pour-first tie for every net with a copper zone -> record (also written to workdir/pourtie.json)."""
    board = pcbnew.LoadBoard(board_in)
    rec = {"nets": {}, "tied": [], "untied": []}
    if board is None:                            # not a KiCad board (a stub or an unreadable file): nothing to tie
        import shutil
        shutil.copy(board_in, board_out)
        rec["skipped"] = "board unreadable"
        with open(os.path.join(workdir, "pourtie.json"), "w") as fh:
            json.dump(rec, fh, indent=1)
        return rec
    pm = pour_map(board)
    if not pm:                                   # no pour on this board: nothing to tie (and no zone filler run)
        pcbnew.SaveBoard(board_out, board)
        with open(os.path.join(workdir, "pourtie.json"), "w") as fh:
            json.dump(rec, fh, indent=1)
        return rec
    PL.refill(board)
    pm = pour_map(board)
    for net in sorted(pm):
        if nets is not None and net not in nets:
            continue
        pads = [p for f in board.GetFootprints() for p in f.Pads() if p.GetNetname() == net and not p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH]
        if not pads:
            continue
        already = sum(1 for p in pads if tie_status(board, net, p, pm[net])[0])
        todo = [p for p in pads if not tie_status(board, net, p, pm[net])[0]]
        tied, untied = _tie_pads(board, net, todo, pm[net]) if todo else ([], [])
        rec["nets"][net] = {"pour_layers": [board.GetLayerName(l) for l in pm[net]], "pads": len(pads),
                            "tied_before": already, "tied_by_via": len(tied), "untied": len(untied)}
        rec["tied"] += tied
        rec["untied"] += untied
    PL.refill(board)
    pcbnew.SaveBoard(board_out, board)
    with open(os.path.join(workdir, "pourtie.json"), "w") as fh:
        json.dump(rec, fh, indent=1)
    return rec


def _pad_at(board, net, at_mm):
    """the pad of `net` whose centre is at (x_mm, y_mm) (pad numbers repeat across parts; positions do not)."""
    x, y = pcbnew.FromMM(at_mm[0]), pcbnew.FromMM(at_mm[1])
    for f in board.GetFootprints():
        for p in f.Pads():
            if p.GetNetname() == net and abs(p.GetPosition().x - x) <= TOL and abs(p.GetPosition().y - y) <= TOL:
                return p
    return None


def repair_in_memory(board, rec):
    """the check and the re-tie on an open board: every verified tie of `rec` that no longer reaches the filled pour is
    re-tied at a legal site. -> {checked, lost, repaired, unrepaired}."""
    PL.refill(board)
    pm = pour_map(board)
    lost, repaired, unrepaired = [], 0, []
    for t in rec["tied"]:
        pad = _pad_at(board, t["net"], t["at"])
        if pad is None or not pm.get(t["net"]):
            continue
        if not tie_status(board, t["net"], pad, pm[t["net"]])[0]:
            lost.append(t)
            tied, untied = _tie_pads(board, t["net"], [pad], pm[t["net"]])
            if tied:
                repaired += 1
            else:
                unrepaired += [dict(u, reason="lost after routing; " + u["reason"]) for u in untied]
    PL.refill(board)
    return {"checked": len(rec["tied"]), "lost": len(lost), "repaired": repaired, "unrepaired": unrepaired}


def repair(board_in, board_out, tie_dir, workdir):
    """file wrapper of repair_in_memory: reads tie_dir/pourtie.json, writes board_out and workdir/pour_repair.json."""
    rec = json.load(open(os.path.join(tie_dir, "pourtie.json")))
    board = pcbnew.LoadBoard(board_in)
    out = repair_in_memory(board, rec)
    pcbnew.SaveBoard(board_out, board)
    out["board"] = board_out
    with open(os.path.join(workdir, "pour_repair.json"), "w") as fh:
        json.dump(out, fh, indent=1)
    return out
