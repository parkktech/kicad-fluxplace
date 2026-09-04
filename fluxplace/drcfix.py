"""Mechanical DRC fixes for the violations that repairs themselves create.

A footprint swap, a re-widthed RF trace or a ripped stub leaves a specific,
recognisable class of DRC noise behind. Each is a local, deterministic edit:

  shorting_items / solder_mask_bridge   a track now runs into a pad of a
                                        different net (a bigger land pattern
                                        landed on it): rip that track, prune
                                        what it leaves dangling locally, let
                                        the patcher re-route it
  clearance (RF track vs other item)    a widened RF segment grazes a via or
                                        track of another net: neck it back to
                                        the base width for +-0.6 mm around
                                        the pinch — a short discontinuity
                                        beats a short circuit
  track_not_centered_on_via             snap the track end onto the via
  track_dangling                        delete the stub
  silk_overlap / silk_over_copper       move the footprint's reference text
                                        above its body

Runs kicad-cli DRC itself, so it can be looped until the count stops
falling. Everything works off the DRC report's item UUIDs — no guessing
which track a message meant.
"""
import math
import os
import re

from . import repair as RP
from .tune import drc_counts

__all__ = ["run_drc", "fix", "loop"]


def run_drc(board_path, kicad_cli="kicad-cli"):
    import json
    import subprocess
    out = board_path + ".drc.json"
    subprocess.run([kicad_cli, "pcb", "drc", "--format", "json", "--severity-all",
                    "--all-track-errors", "-o", out, board_path],
                   capture_output=True, text=True, timeout=900)
    d = json.load(open(out))
    os.unlink(out)
    return d


_DESC = re.compile(r"^(Track|Via|Pad (\S+)) \[([^\]]*)\]"
                   r"(?: of (\S+))?(?: on ([^,]+))?(?:, length ([\d.]+) mm)?")


def _item(board, it):
    """Resolve a DRC report item to the board object. The report's UUID is
    tried first; failing that (KIID lookup is not exposed in every SWIG
    build) the description + position are matched geometrically: net,
    layer, length to 1 um for tracks, position for vias, ref/number for
    pads."""
    import pcbnew
    uuid = it.get("uuid", "")
    if uuid:
        try:
            obj = board.GetItem(pcbnew.KIID(uuid))
            if obj is not None and hasattr(obj, "GetClass"):
                return obj
        except Exception:
            pass
    m = _DESC.match(it.get("description", ""))
    if not m:
        return None
    kind, padnum, net, ref, layer, length = m.groups()
    pos = it.get("pos") or {}
    px, py = pos.get("x"), pos.get("y")
    if kind == "Track":
        best = None
        for t in board.GetTracks():
            if t.GetClass() != "PCB_TRACK" or t.GetNetname() != net:
                continue
            if layer and board.GetLayerName(t.GetLayer()) != layer.strip():
                continue
            if length and abs(t.GetLength() / 1e6 - float(length)) > 0.002:
                continue
            if px is not None and not t.HitTest(
                    pcbnew.VECTOR2I(int(px * 1e6), int(py * 1e6)), int(t.GetWidth())):
                continue
            best = t
            break
        return best
    if kind == "Via":
        for t in board.GetTracks():
            if t.GetClass() == "PCB_VIA" and t.GetNetname() == net:
                p = t.GetPosition()
                if px is None or math.hypot(p.x / 1e6 - px, p.y / 1e6 - py) < 0.01:
                    return t
        return None
    if kind.startswith("Pad") and ref:
        fp = board.FindFootprintByReference(ref)
        if fp:
            for p in fp.Pads():
                if p.GetNumber() == padnum:
                    return p
    return None


def _split_neck(board, track, px, py, half_mm, base_w):
    """Split `track` so the part within half_mm of (px,py) is base_w wide."""
    import pcbnew
    s, e = track.GetStart(), track.GetEnd()
    sx, sy, ex, ey = s.x / 1e6, s.y / 1e6, e.x / 1e6, e.y / 1e6
    L = math.hypot(ex - sx, ey - sy)
    if L < 1e-6:
        return False
    ux, uy = (ex - sx) / L, (ey - sy) / L
    t = ((px - sx) * ux + (py - sy) * uy)
    a, b = max(0.0, t - half_mm), min(L, t + half_mm)
    if b - a <= 0.01:
        return False
    pts = [(sx, sy), (sx + ux * a, sy + uy * a), (sx + ux * b, sy + uy * b), (ex, ey)]
    widths = [track.GetWidth(), pcbnew.FromMM(base_w), track.GetWidth()]
    layer, code = track.GetLayer(), track.GetNetCode()
    RP._remove(board, track)
    for (x0, y0), (x1, y1), w in zip(pts, pts[1:], widths):
        if math.hypot(x1 - x0, y1 - y0) < 1e-4:
            continue
        nt = pcbnew.PCB_TRACK(board)
        nt.SetStart(pcbnew.VECTOR2I(int(round(x0 * 1e6)), int(round(y0 * 1e6))))
        nt.SetEnd(pcbnew.VECTOR2I(int(round(x1 * 1e6)), int(round(y1 * 1e6))))
        nt.SetLayer(layer)
        nt.SetWidth(w)
        nt.SetNetCode(code)
        board.Add(nt)
    return True


def _spot_clear(board, pt, r_mm, netcode, ignore=()):
    """No other-net copper or any hole within r_mm of pt on any copper
    layer — exact shape collision (a bounding-box test let a pushed via land
    0.116 mm from a pad, measured)."""
    import pcbnew
    circle = pcbnew.SHAPE_CIRCLE(pt, int(r_mm * 1e6))
    cu = [pcbnew.F_Cu, pcbnew.B_Cu] + [pcbnew.In1_Cu + 2 * i
                                       for i in range(max(0, board.GetCopperLayerCount() - 2))]
    for t in board.GetTracks():
        if any(t is g for g in ignore) or t.GetNetCode() == netcode:
            continue
        if t.GetClass() == "PCB_VIA":
            if t.HitTest(pt, int(r_mm * 1e6)):
                return False
            continue
        try:
            if t.GetEffectiveShape(t.GetLayer()).Collide(circle, 0):
                return False
        except Exception:
            if t.HitTest(pt, int(r_mm * 1e6)):
                return False
    for fp in board.GetFootprints():
        for p in fp.Pads():
            if p.GetNetCode() == netcode and p.GetDrillSize().x == 0:
                continue
            for l in cu:
                if not p.IsOnLayer(l):
                    continue
                try:
                    if p.GetEffectiveShape(l).Collide(circle, 0):
                        return False
                except Exception:
                    if p.HitTest(pt, int(r_mm * 1e6)):
                        return False
                break
    return True


def _relocate_via(board, via, np_):
    """Move `via` to np_ if the spot is clear on every layer; drag the ends
    of same-net tracks that terminate on it. Returns True if moved."""
    r = via.GetWidth(via.TopLayer()) / 2e6 + 0.125
    if not _spot_clear(board, np_, r, via.GetNetCode(), ignore=(via,)):
        return False
    vp = via.GetPosition()
    for tr in board.GetTracks():
        if tr.GetClass() != "PCB_TRACK" or tr.GetNetCode() != via.GetNetCode():
            continue
        if tr.GetStart() == vp:
            tr.SetStart(np_)
        if tr.GetEnd() == vp:
            tr.SetEnd(np_)
    via.SetPosition(np_)
    return True


def _shift_via(board, via, track, by_mm):
    """Move `via` away from `track` by by_mm along the perpendicular."""
    import pcbnew
    vp = via.GetPosition()
    s, e = track.GetStart(), track.GetEnd()
    dx, dy = e.x - s.x, e.y - s.y
    L = math.hypot(dx, dy)
    if L < 1:
        return False
    t = max(0.0, min(1.0, ((vp.x - s.x) * dx + (vp.y - s.y) * dy) / (L * L)))
    cx, cy = s.x + t * dx, s.y + t * dy
    nx, ny = vp.x - cx, vp.y - cy
    d = math.hypot(nx, ny)
    if d < 1:
        return False
    nx, ny = nx / d, ny / d
    np_ = pcbnew.VECTOR2I(int(vp.x + nx * by_mm * 1e6), int(vp.y + ny * by_mm * 1e6))
    return _relocate_via(board, via, np_)


def _push_via(board, via, other_pos, by_mm):
    """Move `via` straight away from the point other_pos by by_mm."""
    import pcbnew
    vp = via.GetPosition()
    dx, dy = vp.x - other_pos.x, vp.y - other_pos.y
    d = math.hypot(dx, dy)
    if d < 1:
        return False
    np_ = pcbnew.VECTOR2I(int(vp.x + dx / d * by_mm * 1e6), int(vp.y + dy / d * by_mm * 1e6))
    return _relocate_via(board, via, np_)


def _move_ref(board, ref, log):
    """Above the body first; below if it is already above; hidden on the
    silkscreen if below did not work either (the fab layer keeps it)."""
    import pcbnew
    fp = board.FindFootprintByReference(ref)
    if not fp:
        return False
    bb = fp.GetBoundingBox(False, False)
    r = fp.Reference()
    h = r.GetTextHeight()
    cy = r.GetPosition().y
    if cy < bb.GetTop():                      # already above -> try below
        if cy > bb.GetBottom():               # already below -> hide
            r.SetVisible(False)
            log(f"    {ref}: reference text hidden on silkscreen (fab layer keeps it)")
            return True
        r.SetPosition(pcbnew.VECTOR2I(int(bb.GetCenter().x), int(bb.GetBottom() + h)))
        log(f"    {ref}: reference text moved below the body")
    elif cy > bb.GetBottom():
        r.SetVisible(False)
        log(f"    {ref}: reference text hidden on silkscreen (fab layer keeps it)")
        return True
    else:
        r.SetPosition(pcbnew.VECTOR2I(int(bb.GetCenter().x), int(bb.GetTop() - h)))
        log(f"    {ref}: reference text moved above the body")
    r.SetTextAngle(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))
    return True


def fix(board, drc, rf_base_w=0.15, is_rf=None, max_push_mm=0.12, log=print):
    """Apply the fixes above for every violation in `drc`. Returns counts."""
    import pcbnew
    from . import review as R
    is_rf = is_rf or R.is_rf
    n = {"ripped": 0, "necked": 0, "snapped": 0, "dangling": 0, "silk": 0, "skipped": 0}
    prune_after = []
    moved = set()
    for v in drc.get("violations", []):
        typ = v.get("type")
        items = v.get("items", [])
        pos = v.get("pos") or (items[0].get("pos") if items else None) or {}
        px, py = pos.get("x", 0.0), pos.get("y", 0.0)
        if typ in ("shorting_items", "solder_mask_bridge"):
            # only the footprint-swap case: a track into a PAD of another net.
            # A track shorting a via/track is a routing problem, and ripping
            # it cascades (measured: 7 -> 106 violations)
            if not any(it.get("description", "").startswith("Pad") for it in items):
                n["skipped"] += 1
                continue
            for it in items:
                if not it.get("description", "").startswith("Track"):
                    continue
                obj = _item(board, it)
                if obj is None or obj.GetClass() != "PCB_TRACK":
                    continue
                code = obj.GetNetCode()
                log(f"    rip {it['description'][:50]} (shorts at {px:.2f},{py:.2f})")
                RP._remove(board, obj)
                n["ripped"] += 1
                prune_after.append((code, (px, py)))
        elif typ == "clearance":
            tracks = [it for it in items if it.get("description", "").startswith("Track")]
            others = [it for it in items if not it.get("description", "").startswith("Track")]
            # the pinch is at the other item (a via/pad), not at the report's
            # nominal position, which is the track's own anchor
            if others and others[0].get("pos"):
                px, py = others[0]["pos"]["x"], others[0]["pos"]["y"]
            done = False
            # first choice: the OTHER item is a via of another net and the
            # deficit is small -> push that via off the RF corridor (tracks
            # ending on it follow). A neck is the fallback: it keeps the
            # discontinuity, the push removes it.
            m = re.search(r"clearance ([\d.]+) mm; actual ([\d.]+) mm", v.get("description", ""))
            ovias = [it for it in others if it.get("description", "").startswith("Via")]
            if m and ovias and tracks and float(m.group(1)) - float(m.group(2)) <= max_push_mm:
                via = _item(board, ovias[0])
                trk = _item(board, tracks[0])
                if via is not None and trk is not None and is_rf(trk.GetNetname()) \
                        and not is_rf(via.GetNetname()):
                    by = float(m.group(1)) - float(m.group(2)) + 0.02
                    if _shift_via(board, via, trk, by):
                        log(f"    push via {via.GetNetname()} {by:.3f} mm off {trk.GetNetname()}")
                        n["shifted"] = n.get("shifted", 0) + 1
                        done = True
            for it in ([] if done else tracks):
                obj = _item(board, it)
                if obj is None or obj.GetClass() != "PCB_TRACK":
                    continue
                if not is_rf(obj.GetNetname()):
                    continue
                if obj.GetWidth() <= pcbnew.FromMM(rf_base_w) + 1000:
                    continue
                if _split_neck(board, obj, px, py, 0.6, rf_base_w):
                    log(f"    neck {obj.GetNetname()} on {board.GetLayerName(obj.GetLayer())} "
                        f"to {rf_base_w} mm around ({px:.2f},{py:.2f})")
                    n["necked"] += 1
                    done = True
                    break
            vias_ = [it for it in items if it.get("description", "").startswith("Via")]
            pads_ = [it for it in items if it.get("description", "").startswith("Pad")]
            if not done and len(vias_) == 1 and len(pads_) == 1:
                # via grazing a pad: push the via straight off the pad
                m = re.search(r"clearance ([\d.]+) mm; actual ([\d.]+) mm", v.get("description", ""))
                via = _item(board, vias_[0])
                pad = _item(board, pads_[0])
                if m and via is not None and pad is not None and \
                        float(m.group(1)) - float(m.group(2)) <= max_push_mm:
                    by = float(m.group(1)) - float(m.group(2)) + 0.02
                    if _push_via(board, via, pad.GetPosition(), by):
                        log(f"    push via {via.GetNetname()} {by:.3f} mm off pad {pads_[0]['description'][:24]}")
                        n["shifted"] = n.get("shifted", 0) + 1
                        done = True
            if not done and len(vias_) == 2:
                # via grazing via by a few um: move the one nothing lands on
                # (a stitching via), else the second, straight away from the other
                m = re.search(r"clearance ([\d.]+) mm; actual ([\d.]+) mm", v.get("description", ""))
                a, b = _item(board, vias_[0]), _item(board, vias_[1])
                if m and a is not None and b is not None and float(m.group(1)) - float(m.group(2)) <= 0.05:
                    def _lands(via):
                        return sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"
                                   and (t.GetStart() == via.GetPosition() or t.GetEnd() == via.GetPosition()))
                    mv, other = (a, b) if _lands(a) <= _lands(b) else (b, a)
                    if _push_via(board, mv, other.GetPosition(), float(m.group(1)) - float(m.group(2)) + 0.01):
                        log(f"    push via {mv.GetNetname()} off via {other.GetNetname()}")
                        n["shifted"] = n.get("shifted", 0) + 1
                        done = True
            if not done:
                # a via grazing a track of another net by a few um: shift the
                # via away by the deficit (tracks ending on it follow)
                m = re.search(r"clearance ([\d.]+) mm; actual ([\d.]+) mm", v.get("description", ""))
                vias = [it for it in items if it.get("description", "").startswith("Via")]
                if m and vias and tracks and float(m.group(1)) - float(m.group(2)) <= 0.05:
                    via = _item(board, vias[0])
                    trk = _item(board, tracks[0])
                    if via is not None and trk is not None and _shift_via(board, via, trk,
                            float(m.group(1)) - float(m.group(2)) + 0.01):
                        log(f"    shift via {via.GetNetname()} by "
                            f"{float(m.group(1)) - float(m.group(2)) + 0.01:.3f} mm off "
                            f"{trk.GetNetname()}")
                        n["shifted"] = n.get("shifted", 0) + 1
                        done = True
            if not done:
                n["skipped"] += 1
        elif typ == "clearance_pad_track_placeholder":
            pass
        elif typ == "via_dangling":
            for it in items:
                obj = _item(board, it)
                if obj is not None and obj.GetClass() == "PCB_VIA":
                    RP._remove(board, obj)
                    n["dangling"] += 1
        elif typ == "track_not_centered_on_via":
            tr = via = None
            for it in items:
                obj = _item(board, it)
                if obj is None:
                    continue
                if obj.GetClass() == "PCB_TRACK":
                    tr = obj
                elif obj.GetClass() == "PCB_VIA":
                    via = obj
            if tr and via:
                vp = via.GetPosition()
                ds = math.hypot(tr.GetStart().x - vp.x, tr.GetStart().y - vp.y)
                de = math.hypot(tr.GetEnd().x - vp.x, tr.GetEnd().y - vp.y)
                if ds < de:
                    tr.SetStart(vp)
                else:
                    tr.SetEnd(vp)
                n["snapped"] += 1
            else:
                n["skipped"] += 1
        elif typ == "track_dangling":
            for it in items:
                obj = _item(board, it)
                if obj is not None and obj.GetClass() == "PCB_TRACK":
                    RP._remove(board, obj)
                    n["dangling"] += 1
        elif typ in ("silk_overlap", "silk_over_copper"):
            for it in items:
                d = it.get("description", "")
                if d.startswith("Reference field of "):
                    ref = d.split("Reference field of ", 1)[1].strip()
                    if ref not in moved and _move_ref(board, ref, log):
                        moved.add(ref)
                        n["silk"] += 1
                        break
        else:
            n["skipped"] += 1
    for code, near in prune_after:
        RP.prune_dangling(board, code, near=near, radius_mm=8.0)
    # plane islands are NOT auto-stitched: a via dropped inside an outer-layer
    # island lands on whatever the inner layers carry there (measured: 8 vias,
    # 34 hole-clearance violations). repair.stitch_islands exists for a
    # DRC-guarded caller.
    return n


def guarded_islands(work, kicad_cli, log=print):
    """stitch_islands under a DRC guard: keep only if violations do not rise
    and unconnected items fall."""
    import pcbnew
    import shutil
    v0, u0 = drc_counts(work, kicad_cli)
    b = pcbnew.LoadBoard(work)
    n = RP.stitch_islands(b, log=log)
    if not n:
        return False
    tmp = work + ".isl.kicad_pcb"
    pcbnew.SaveBoard(tmp, b)
    for ext in (".kicad_pro", ".kicad_dru"):
        side = os.path.splitext(work)[0] + ext
        if os.path.exists(side):
            shutil.copy(side, os.path.splitext(tmp)[0] + ext)
    v1, u1 = drc_counts(tmp, kicad_cli)
    if v1 <= v0 and u1 < u0:
        shutil.move(tmp, work)
        log(f"    island vias kept: {v0}/{u0} -> {v1}/{u1}")
        return True
    os.unlink(tmp)
    log(f"    island vias rejected: {v0}/{u0} -> {v1}/{u1}")
    return False


def loop(board_path, out_path, kicad_cli="kicad-cli", rounds=3, islands=False, log=print):
    """DRC -> fix -> save, repeated while the violation count keeps falling.
    Fresh pcbnew session per round (SWIG decay)."""
    import pcbnew
    import shutil
    work = out_path
    if os.path.abspath(board_path) != os.path.abspath(out_path):
        shutil.copy(board_path, out_path)
    hist = []
    for r in range(rounds):
        d = run_drc(work, kicad_cli)
        v, u = len(d.get("violations", [])), len(d.get("unconnected_items", []))
        hist.append((v, u))
        log(f"  round {r + 1}: {v} violations, {u} unconnected")
        if v == 0:
            break
        b = pcbnew.LoadBoard(work)
        n = fix(b, d, log=log)
        log(f"    fixes: {n}")
        if not any(n.get(k) for k in ("ripped", "necked", "snapped", "dangling",
                                      "silk", "shifted", "island_vias")):
            break
        pcbnew.SaveBoard(work, b)
    if islands:
        guarded_islands(work, kicad_cli, log=log)
    d = run_drc(work, kicad_cli)
    hist.append((len(d.get("violations", [])), len(d.get("unconnected_items", []))))
    log(f"  final: {hist[-1][0]} violations, {hist[-1][1]} unconnected")
    return hist, d
