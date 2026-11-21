"""Via-centre snap with a DRC acceptance rule: every track end inside a same-net via's copper moves to the via centre; the
tracks named by any NEW violation (drcguard keys, unconnected included) keep their ends; repeat until nothing new appears."""
import json
import re
import math
import os
import shutil
import subprocess

from . import drcguard as DG
from . import hrgeom as HG

CENTRED = "track_not_centered_on_via"
GRID_MM = 2.0          # PHASE3F A6: spatial bucket of the clearance check (2 mm cells)
JOINT_MM = 0.005       # track ends this close to a snapped end move with it (the snapvias.snap_text rule)


def _report(board, cli="kicad-cli"):
    out = board + ".drc.json"
    subprocess.run([cli, "pcb", "drc", "--severity-error", "--format", "json", "--refill-zones", "-o", out, board],
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


def _snap(src, dst, skip):
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
            if any(u.IsLocked() or u.m_Uuid.AsString() in skip for u, _w in joint):
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
                index_end(u, w)
                a, bb, h = trk_geom(u)
                add(u, (min(a[0], bb[0]) - h, min(a[1], bb[1]) - h, max(a[0], bb[0]) + h, max(a[1], bb[1]) + h))
    for u in tracks:                                     # both ends in one via: a zero-length stub, the via joins it
        if u.GetStart() == u.GetEnd() and not u.IsLocked():   # (snapvias.snap_text drops it too; KiCad reports it as a
            b.Remove(u)                                  # crossing with any track near the via)
    pcbnew.SaveBoard(dst, b)
    return moved, refused


def run(src, dst, iters=6, log=print):
    """-> {"moved", "excluded", "iters", "centred_left", "refused_geom"}; `dst` = the last accepted board (src when none is).
    refused_geom: joints _snap refused for clearance (PHASE3F A6) on the accepted iteration."""
    base = _keys(_report(src))
    skip, last = set(), None
    for i in range(iters):
        tmp = dst + f".i{i}.kicad_pcb"
        moved, refused_geom = _snap(src, tmp, skip)
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
    return {"moved": last[1] if last else 0, "excluded": len(skip), "iters": i + 1,
            "centred_left": last[2] if last else None, "refused_geom": last[3] if last else 0}
