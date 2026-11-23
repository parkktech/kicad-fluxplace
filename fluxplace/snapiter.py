"""Via-centre snap with a DRC acceptance rule: every track end inside a same-net via's copper moves to the via centre; the
tracks named by any NEW violation (drcguard keys, unconnected included) keep their ends; repeat until nothing new appears."""
import json
import re
import math
import os
import shutil
import subprocess
from fluxplace import procs as _procs

from . import drcguard as DG
from . import hrgeom as HG

CENTRED = "track_not_centered_on_via"
GRID_MM = 2.0          # PHASE3F A6: spatial bucket of the clearance check (2 mm cells)
JOINT_MM = 0.005       # track ends this close to a snapped end move with it (the snapvias.snap_text rule)


def _report(board, cli="kicad-cli"):
    from . import drcguard as DG
    out = DG.report_slot(board)
    _procs.run([cli, "pcb", "drc", "--severity-error", "--format", "json", "--refill-zones", "-o", out, board],
                   capture_output=True, timeout=900)
    from . import drcfull as DF
    d = DF.complete(board, json.load(open(out)), fill=True)
    os.unlink(out)
    return d


_unc_nets = DG.unc_nets


def _keys(d):
    # Opens are counted per NET, not keyed by item description (drcguard.keys_per_net; W9/W13). PHASE3G G2a: the body
    # moved there; this keeps only the CENTRED filter.
    return DG.keys_per_net(d, keep=lambda v: v.get("type") != CENTRED)


def _pair_clr(x, y, layer, mm, dflt):
    """Clearance (mm) between two items on `layer`: the larger of their own clearances when this pcbnew reports them,
    else the board's minimum clearance `dflt`."""
    try:
        c = max(mm(x.GetOwnClearance(layer)), mm(y.GetOwnClearance(layer)))
    except Exception:                                    # noqa: BLE001 - this pcbnew has no per-item clearance call
        return dflt
    return c if c > 0 else dflt


def _seg_rect(a, b, box):
    """Distance (mm) from segment ab to the rectangle box = (x0, y0, x1, y1); 0 when they touch."""
    x0, y0, x1, y1 = box
    inside = lambda p: x0 <= p[0] <= x1 and y0 <= p[1] <= y1
    if inside(a) or inside(b):
        return 0.0
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return min(HG.seg_seg(a, b, corners[i], corners[(i + 1) % 4])[0] for i in range(4))


def obstacle_gap(a, bb, hq, kind, geom):
    """Gap (mm) between the centre line a-bb (half width hq) and a copper obstacle: kind "seg" geom = (c, d, half width),
    "via" geom = (centre, radius), "rect" geom = (x0, y0, x1, y1)."""
    if kind == "seg":
        return HG.seg_seg(a, bb, geom[0], geom[1])[0] - hq - geom[2]
    if kind == "via":
        return HG.pt_seg(geom[0], a, bb)[0] - hq - geom[1]
    return _seg_rect(a, bb, geom) - hq


def _snap(src, dst, skip, locked_ok=False):
    """Snap every track end inside a same-net via to the via centre. PHASE3F A6: a snap that would bring the moved
    segment closer than the clearance to copper of ANOTHER net on its layer (tracks, vias reaching the layer, pads) is
    refused; the whole joint (every track end within JOINT_MM of the old point) stays. A joint with a locked or skipped
    track is not moved. Returns (moved ends, refused joints)."""
    import pcbnew
    mm = pcbnew.ToMM
    b = pcbnew.LoadBoard(src)
    dflt = mm(b.GetDesignSettings().m_MinClearance)
    tracks = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_TRACE_T]
    vias = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T]
    pads = list(b.GetPads())
    # obstacle index: cell -> items whose bounding box (copper half-size included) touches the cell
    grid = {}

    def cells(box):
        x0, y0, x1, y1 = box
        return [(i, j) for i in range(math.floor(x0 / GRID_MM), math.floor(x1 / GRID_MM) + 1)
                for j in range(math.floor(y0 / GRID_MM), math.floor(y1 / GRID_MM) + 1)]

    def add(item, box):
        for c in cells(box):
            grid.setdefault(c, []).append(item)

    def trk_geom(t):
        a = (mm(t.GetStart().x), mm(t.GetStart().y))
        bb = (mm(t.GetEnd().x), mm(t.GetEnd().y))
        return a, bb, mm(t.GetWidth()) / 2

    for t in tracks:
        a, bb, h = trk_geom(t)
        add(t, (min(a[0], bb[0]) - h, min(a[1], bb[1]) - h, max(a[0], bb[0]) + h, max(a[1], bb[1]) + h))
    for v in vias:
        c, r = (mm(v.GetPosition().x), mm(v.GetPosition().y)), mm(v.GetWidth(pcbnew.F_Cu)) / 2
        add(v, (c[0] - r, c[1] - r, c[0] + r, c[1] + r))
    pad_box = {}
    for p in pads:
        bx = p.GetBoundingBox()
        pad_box[id(p)] = (mm(bx.GetLeft()), mm(bx.GetTop()), mm(bx.GetRight()), mm(bx.GetBottom()))
        add(p, pad_box[id(p)])

    cmax = dflt                                          # the largest clearance any item asks for: the probe margin
    for x in tracks + vias + pads:
        try:
            cmax = max(cmax, mm(x.GetOwnClearance(pcbnew.F_Cu)), mm(x.GetOwnClearance(pcbnew.B_Cu)))
        except Exception:                                # noqa: BLE001 - no per-item clearance in this pcbnew
            break

    def clear_ok(q, a, bb, w, layer, net):
        """True when the segment a-bb (width w, item q) on `layer` keeps the clearance to every other-net copper there."""
        hq = w / 2
        reach = hq + cmax
        probe = (min(a[0], bb[0]) - reach, min(a[1], bb[1]) - reach, max(a[0], bb[0]) + reach, max(a[1], bb[1]) + reach)
        seen = set()
        for c in cells(probe):
            for it in grid.get(c, []):
                if id(it) in seen:
                    continue
                seen.add(id(it))
                if it.GetNetCode() == net:
                    continue
                if it.Type() == pcbnew.PCB_TRACE_T:
                    if it.GetLayer() != layer:
                        continue
                    o_a, o_b, o_h = trk_geom(it)
                    gap = obstacle_gap(a, bb, hq, "seg", (o_a, o_b, o_h))
                elif it.Type() == pcbnew.PCB_VIA_T:
                    if not it.IsOnLayer(layer):
                        continue
                    o_c, o_r = (mm(it.GetPosition().x), mm(it.GetPosition().y)), mm(it.GetWidth(pcbnew.F_Cu)) / 2
                    gap = obstacle_gap(a, bb, hq, "via", (o_c, o_r))
                else:                                    # pad: its bounding box
                    if not it.IsOnLayer(layer):
                        continue
                    gap = obstacle_gap(a, bb, hq, "rect", pad_box[id(it)])
                if gap < _pair_clr(q, it, layer, mm, dflt):
                    return False
        return True

    def cell(x, y):
        return (math.floor(x / GRID_MM), math.floor(y / GRID_MM))

    def near(x, y):
        cx, cy = cell(x, y)
        return [(cx + i, cy + j) for i in (-1, 0, 1) for j in (-1, 0, 1)]

    # ends and vias by 2 mm cell; entries go stale on a move and are re-checked against the live position
    end_at, via_at = {}, {}
    def index_end(u, w):
        q = u.GetStart() if w == 0 else u.GetEnd()
        end_at.setdefault((u.GetNetCode(), u.GetLayer(), *cell(mm(q.x), mm(q.y))), []).append((u, w))
    for u in tracks:
        index_end(u, 0)
        index_end(u, 1)
    for v in vias:
        via_at.setdefault(cell(mm(v.GetPosition().x), mm(v.GetPosition().y)), []).append(v)

    done, moved, refused = set(), 0, 0
    moved_ids = set()
    for t in tracks:
        if t.m_Uuid.AsString() in skip:
            continue
        for which in (0, 1):
            if (t.m_Uuid.AsString(), which) in done:
                continue
            p = t.GetStart() if which == 0 else t.GetEnd()
            vt = next((v for c in near(mm(p.x), mm(p.y)) for v in via_at.get(c, [])
                       if v.GetNetCode() == t.GetNetCode() and v.IsOnLayer(t.GetLayer())
                       and v.GetPosition() != p and
                       math.hypot(p.x - v.GetPosition().x, p.y - v.GetPosition().y) <= v.GetWidth(pcbnew.F_Cu) / 2), None)
            if vt is None:
                continue
            target, old = vt.GetPosition(), (mm(p.x), mm(p.y))
            # the joint: every track end of this net and layer at the old point
            joint, seen = [], set()
            for c in near(*old):
                for u, w in end_at.get((t.GetNetCode(), t.GetLayer(), *c), []):
                    q = u.GetStart() if w == 0 else u.GetEnd()
                    if (id(u), w) not in seen and math.hypot(mm(q.x) - old[0], mm(q.y) - old[1]) <= JOINT_MM:
                        seen.add((id(u), w))
                        joint.append((u, w))
            for u, w in joint:
                done.add((u.m_Uuid.AsString(), w))
            if any((u.IsLocked() and not locked_ok) or u.m_Uuid.AsString() in skip for u, _w in joint):
                continue
            new = (mm(target.x), mm(target.y))
            # each joint end's new segment: the other end of the track to the via centre
            if not all(clear_ok(u, trk_geom(u)[1] if w == 0 else trk_geom(u)[0], new, mm(u.GetWidth()),
                                u.GetLayer(), u.GetNetCode()) for u, w in joint):
                refused += 1
                continue
            for u, w in joint:
                (u.SetStart if w == 0 else u.SetEnd)(target)
                moved += 1
                moved_ids.add(id(u))
                index_end(u, w)
                a, bb, h = trk_geom(u)
                add(u, (min(a[0], bb[0]) - h, min(a[1], bb[1]) - h, max(a[0], bb[0]) + h, max(a[1], bb[1]) + h))
    gone = set()
    for u in tracks:                                     # both ends in one via: a zero-length stub, the via joins it
        if u.GetStart() == u.GetEnd() and (not u.IsLocked() or (locked_ok and id(u) in moved_ids)):   # (snapvias.snap_text drops it too; KiCad reports it as a
            b.Remove(u)                                  # crossing with any track near the via)
            gone.add(id(u))
    # a snap can collapse a track onto another (same net, layer, ends, width): the moved unlocked copy goes, one copy stays.  Only tracks
    # this call moved are touched: duplicates already on the board are counted in the protected manifest and stay.
    same = {}
    for u in tracks:
        if id(u) in gone:
            continue
        a, c = (u.GetStart().x, u.GetStart().y), (u.GetEnd().x, u.GetEnd().y)
        same.setdefault((u.GetNetCode(), u.GetLayer(), min(a, c), max(a, c), u.GetWidth()), []).append(u)
    for grp in same.values():
        if len(grp) < 2:
            continue
        movers = [u for u in grp if id(u) in moved_ids and (locked_ok or not u.IsLocked())]
        mv = {id(u) for u in movers}
        stay = [u for u in grp if id(u) not in mv]
        for u in (movers if stay else movers[1:]):
            b.Remove(u)
    pcbnew.SaveBoard(dst, b)
    return moved, refused


_SNAP_REAL = _snap


def _snap_json(src, dst, skip, locked_ok=False):
    """_snap with a JSON-able signature (runs in a fresh process): -> [moved, refused]."""
    return list(_snap(src, dst, set(skip), locked_ok))


def _snap_fresh(src, dst, skip, locked_ok=False):
    """_snap in its own interpreter: its board.Remove() decays pcbnew's bindings for the process (freshproc), and the caller (the
    pipeline's krt-whole chain, then close) goes on loading boards."""
    if _snap is not _SNAP_REAL:                           # a test double stands in for the board edit: call it directly
        return _snap(src, dst, skip) if not locked_ok else _snap(src, dst, skip, locked_ok)
    from .freshproc import in_fresh_process
    moved, refused = in_fresh_process("fluxplace.snapiter", "_snap_json", src, dst, sorted(skip), locked_ok)
    return moved, refused


def _drop_trials(tmps):
    """The trial boards of a guarded loop and their sidecars are scratch (an 11 MB board per iteration stayed in every close folder)."""
    for tmp in tmps:
        for f in (tmp, os.path.splitext(tmp)[0] + ".kicad_pro", os.path.splitext(tmp)[0] + ".kicad_prl"):
            if os.path.exists(f):
                os.remove(f)


def run(src, dst, iters=6, log=print, locked_ok=False):
    """-> {"moved", "excluded", "iters", "centred_left", "refused_geom"}; `dst` = the last accepted board (src when none is).
    refused_geom: joints _snap refused for clearance (PHASE3F A6) on the accepted iteration."""
    base = _keys(_report(src))
    skip, last, tmps = set(), None, []
    for i in range(iters):
        tmp = dst + f".i{i}.kicad_pcb"
        tmps.append(tmp)
        moved, refused_geom = _snap_fresh(src, tmp, skip, locked_ok)
        shutil.copy(os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(tmp)[0] + ".kicad_pro")
        d = _report(tmp)
        new = DG.new_keys(base, _keys(d))
        bad = [v for v in d["violations"] if v["type"] != CENTRED and DG.key_of(v) in new]
        bad += [u for u in d.get("unconnected_items", []) if any(("Unet", n) in new for n in _unc_nets(u) or ["?"])]
        ids = {it["uuid"] for v in bad for it in v["items"] if "uuid" in it}
        log(f"snap iter {i}: moved {moved}, refused {refused_geom}, skipped {len(skip)}, new keys {sum(new.values())}")
        if not new:
            last = (tmp, moved, sum(v["type"] == CENTRED for v in d["violations"]), refused_geom)
            break
        if not ids - skip:
            break
        skip |= ids
    if last:
        shutil.copy(last[0], dst)
        src_pro, dst_pro = os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(dst)[0] + ".kicad_pro"
        if os.path.abspath(src_pro) != os.path.abspath(dst_pro):
            shutil.copy(src_pro, dst_pro)
    _drop_trials(tmps)
    return {"moved": last[1] if last else 0, "excluded": len(skip), "iters": i + 1,
            "centred_left": last[2] if last else None, "refused_geom": last[3] if last else 0}


def _only_hit(only, v, mm):
    c = v.GetPosition()
    return only is None or any(n == v.GetNetname() and abs(x - mm(c.x)) < 0.01 and abs(y - mm(c.y)) < 0.01 for n, x, y in only)


def _via_to_end(src, dst, skip, only=None, locked_ok=False, protect=()):
    """The clearance-refused cases: where the track ends meeting a via off its centre all sit on ONE point P, the via moves to P (and the
    ends still at the old centre, on any layer, move with it): no copper gets closer to a neighbour than the via's own annulus does at P.
    The caller's DRC guard judges the result.  Runs in a fresh process (board.Remove / bindings, see freshproc).
    A locked via, or a via with a locked track end on it, is left alone unless `locked_ok` (D71cz: the caller then re-seeds the manifest);
    a via of a net in `protect` (pair legs, RF, manifest nets: a move changes their length) is always left alone.
    -> [moved vias, skipped [[net, x, y, why]], touched {item uuid: via uuid}].  `skip`: uuids (a via, or any item `touched` maps to it)
    the guard excluded.  `only`: [[net, x, y]] the vias DRC names (the others are left alone)."""
    import pcbnew
    mm = pcbnew.ToMM
    b = pcbnew.LoadBoard(src)
    tracks = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_TRACE_T]
    vias = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T]
    protect = set(protect or ())
    moved, skipped, touched = 0, [], {}
    for v in vias:
        vid = v.m_Uuid.AsString()
        if vid in skip or v.GetNetname() in protect or not _only_hit(only, v, mm):
            continue
        c = v.GetPosition()
        row = [v.GetNetname(), round(mm(c.x), 3), round(mm(c.y), 3)]
        r = mm(v.GetWidth(pcbnew.F_Cu)) / 2
        off, at = [], []
        for t in tracks:
            if t.GetNetCode() != v.GetNetCode() or not v.IsOnLayer(t.GetLayer()):
                continue
            for w in (0, 1):
                e = t.GetStart() if w == 0 else t.GetEnd()
                d = math.hypot(mm(e.x - c.x), mm(e.y - c.y))
                if d <= 0.005:
                    at.append((t, w))
                elif d <= r:
                    off.append((t, w, e))
        if not off:
            continue
        if any(t.m_Uuid.AsString() in skip for t, *_ in off + at):
            continue
        if not locked_ok and (v.IsLocked() or any(t.IsLocked() for t, _w in at)):
            skipped.append(row + ["locked"])
            continue
        pts = {(round(mm(e.x), 3), round(mm(e.y), 3)) for _t, _w, e in off}
        if len(pts) != 1:
            skipped.append(row + [f"{len(pts)} different end points"])
            continue
        target = pcbnew.VECTOR2I(off[0][2].x, off[0][2].y)
        if any((p.IsOnLayer(pcbnew.F_Cu) or p.IsOnLayer(pcbnew.B_Cu)) and p.GetNetCode() == v.GetNetCode() and p.HitTest(c) for p in b.GetPads()):
            skipped.append(row + ["via sits in a pad"])
            continue
        v.SetPosition(target)
        for t, w in at:
            (t.SetStart if w == 0 else t.SetEnd)(target)
        for t, *_ in off + at:
            touched[t.m_Uuid.AsString()] = vid
        moved += 1
    pcbnew.SaveBoard(dst, b)
    return [moved, skipped, touched]


def _via_names(d0):
    """[[net, x, y]] of the vias a DRC report names in its off-centre violations (a description without a [net] is skipped)."""
    out = []
    for v in d0["violations"]:
        if v["type"] != CENTRED:
            continue
        for it in v["items"]:
            m = re.search(r"\[([^\]]*)\]", it.get("description", ""))
            if m and it["description"].startswith("Via") and "pos" in it:
                out.append([m.group(1), it["pos"]["x"], it["pos"]["y"]])
    return out


def via_run(src, dst, iters=3, log=print, func="_via_to_end", locked_ok=False, protect=()):
    """After `run`: the ends it refused for clearance are fixed by moving the via to the track end (see _via_to_end), DRC-guarded like `run`
    (no new violation, no more opens; a via whose move creates one is left: the items a new violation names are mapped back to their via
    through the worker's `touched`, so one bad via does not cost the batch).  Locks and `protect` nets: see _via_to_end.
    -> {"moved", "excluded", "skipped", "centred_left"}."""
    from .freshproc import in_fresh_process
    d0 = _report(src)
    base = _keys(d0)
    only = _via_names(d0)
    if not only:
        return {"moved": 0, "excluded": 0, "skipped": [], "centred_left": sum(v["type"] == CENTRED for v in d0["violations"])}
    skip, last, skipped, tmps = set(), None, [], []
    for i in range(iters):
        tmp = dst + f".v{i}.kicad_pcb"
        tmps.append(tmp)
        moved, skipped, touched = in_fresh_process("fluxplace.snapiter", func, src, tmp, sorted(skip), only, locked_ok, sorted(protect or ()))
        shutil.copy(os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(tmp)[0] + ".kicad_pro")
        d = _report(tmp)
        new = DG.new_keys(base, _keys(d))
        bad = [v for v in d["violations"] if v["type"] != CENTRED and DG.key_of(v) in new]
        bad += [u for u in d.get("unconnected_items", []) if any(("Unet", n) in new for n in _unc_nets(u) or ["?"])]
        ids = {it["uuid"] for v in bad for it in v["items"] if "uuid" in it}
        ids |= {touched[u] for u in ids if u in touched}        # the via behind a moved end or a joiner
        log(f"via iter {i}: moved {moved}, new keys {sum(new.values())}")
        if not new:
            last = (tmp, moved, sum(v["type"] == CENTRED for v in d["violations"]))
            break
        if not ids - skip:
            break
        skip |= ids
    if last and last[1]:
        shutil.copy(last[0], dst)
        src_pro, dst_pro = os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(dst)[0] + ".kicad_pro"
        if os.path.abspath(src_pro) != os.path.abspath(dst_pro):
            shutil.copy(src_pro, dst_pro)
    _drop_trials(tmps)
    return {"moved": last[1] if last else 0, "excluded": len(skip), "skipped": skipped, "centred_left": last[2] if last else None}


def _joiners(src, dst, skip, only=None, locked_ok=False, protect=()):
    """Fallback for ends neither the snap nor a via move can fix: leave the via and the segment line alone, shorten the segment along its own
    direction until its round end lies inside the via annulus, and add a joiner (same net, layer, width, lock) from that end to the exact via
    centre.  The joiner lies inside via copper and shortening only removes copper, so no clearance gets worse; the net's track length grows
    by up to the via radius, so nets in `protect` (length-matched legs) are left alone.  A locked segment is changed only with `locked_ok`.
    `only`: [[net, x, y]] the vias DRC names.  -> [joiners added, skipped [[net, x, y, why]], touched {item uuid: via uuid}]."""
    import pcbnew
    mm = pcbnew.ToMM
    b = pcbnew.LoadBoard(src)
    tracks = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_TRACE_T]
    vias = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T]
    protect = set(protect or ())
    added, skipped, touched = 0, [], {}
    for v in vias:
        vid = v.m_Uuid.AsString()
        if vid in skip or v.GetNetname() in protect or not _only_hit(only, v, mm):
            continue
        c = v.GetPosition()
        cx, cy = mm(c.x), mm(c.y)
        r = mm(v.GetWidth(pcbnew.F_Cu)) / 2
        for t in tracks:
            if t.GetNetCode() != v.GetNetCode() or not v.IsOnLayer(t.GetLayer()) or t.m_Uuid.AsString() in skip:
                continue
            for w in (0, 1):
                e, o = (t.GetStart(), t.GetEnd()) if w == 0 else (t.GetEnd(), t.GetStart())
                ex, ey, ox, oy = mm(e.x), mm(e.y), mm(o.x), mm(o.y)
                d = math.hypot(ex - cx, ey - cy)
                if d <= 0.005 or d > r:
                    continue
                if t.IsLocked() and not locked_ok:
                    skipped.append([v.GetNetname(), round(cx, 3), round(cy, 3), "locked"])
                    continue
                hw = mm(t.GetWidth()) / 2
                if hw >= r:
                    skipped.append([v.GetNetname(), round(cx, 3), round(cy, 3), "track wider than the via"])
                    continue
                L = math.hypot(ox - ex, oy - ey)
                ux, uy = ((ox - ex) / L, (oy - ey) / L) if L else (0.0, 0.0)
                tt, lim = 0.0, r - hw - 0.005
                while math.hypot(ex + ux * tt - cx, ey + uy * tt - cy) > lim and tt < L - 0.05:
                    tt += 0.01                                      # shorten along the segment's own line
                if math.hypot(ex + ux * tt - cx, ey + uy * tt - cy) > lim:
                    tt = 0.0           # the line only leaves the via: keep the end where it is; the joiner's round end is the segment's own cap
                ne = pcbnew.VECTOR2I(pcbnew.FromMM(ex + ux * tt), pcbnew.FromMM(ey + uy * tt))
                (t.SetStart if w == 0 else t.SetEnd)(ne)
                j = pcbnew.PCB_TRACK(b)
                j.SetStart(ne)
                j.SetEnd(c)
                j.SetWidth(t.GetWidth())
                j.SetLayer(t.GetLayer())
                j.SetNet(t.GetNet())
                j.SetLocked(t.IsLocked())
                b.Add(j)
                touched[j.m_Uuid.AsString()] = vid
                touched[t.m_Uuid.AsString()] = vid
                added += 1
    pcbnew.SaveBoard(dst, b)
    return [added, skipped, touched]


def joiner_run(src, dst, iters=3, log=print, locked_ok=False, protect=()):
    """Last fallback for ends `run` and `via_run` left (D71cz): joiners to the exact via centre (see _joiners), DRC-guarded like `run`."""
    return via_run(src, dst, iters, log, func="_joiners", locked_ok=locked_ok, protect=protect)
