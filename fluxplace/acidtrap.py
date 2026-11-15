"""Acid traps (acute copper junctions) and copper slivers / necks.

ACID_TRAP  two same-net track segments on one layer meeting at a shared end point
           at an interior angle below [fab] acid_trap_deg (default 60, see
           constraints._FAB_DEFAULTS). Etching undercuts the wedge; the fab
           cannot hold the acute tip. Junctions of three or more tracks, and
           tracks entering a pad, are NOT checked (pad outlines are not in the
           facts; see review.check_acid_traps).
SLIVER     copper of a filled zone narrower than [fab] min_track_mm: a neck or
           spike the etch cannot hold. Measured by a morphological opening with
           a SQUARE of side min_track_mm on a raster of `res` mm pixels. A square
           is lenient on diagonal necks (a 45-degree neck of width w survives a
           square of side w up to about w*sqrt(2)); the raster is approximate to
           one pixel.

fix_junctions() is the opt-in repair for ACID_TRAP (drc-fix --acid-traps): it
chamfers the wedge with one short same-net segment. The two tracks keep their
net and their connection through the chamfer, and the edit is kept only if
kicad-cli DRC violations and unconnected items do not rise (the drcfix guard).
"""
import math
import os
import shutil
from collections import defaultdict

import numpy as np

from fluxplace import drcguard as DG
from fluxplace import gerbercheck as GC

DEFAULT_ACID_DEG = 60.0


def acute_junctions(segs, min_deg=DEFAULT_ACID_DEG, decimals=3):
    """segs: dicts with net, layer, x1, y1, x2, y2 (mm) and optional kind ('track').
    Returns dicts net, layer, x, y, deg, a, b, ea, eb: a/b index segs, ea/eb are
    0 (start) or 1 (end) of the segment that sits at the junction."""
    ends = defaultdict(list)
    for i, s in enumerate(segs):
        if s.get("kind", "track") != "track" or not s.get("net"):
            continue
        ends[(s["net"], s["layer"], round(s["x1"], decimals), round(s["y1"], decimals))].append((i, 0))
        ends[(s["net"], s["layer"], round(s["x2"], decimals), round(s["y2"], decimals))].append((i, 1))
    out = []
    for key, lst in ends.items():
        if len(lst) != 2 or lst[0][0] == lst[1][0]:
            continue
        (ia, ea), (ib, eb) = lst
        sa, sb = segs[ia], segs[ib]
        px, py = (sa["x1"], sa["y1"]) if ea == 0 else (sa["x2"], sa["y2"])
        oa = (sa["x2"], sa["y2"]) if ea == 0 else (sa["x1"], sa["y1"])
        ob = (sb["x2"], sb["y2"]) if eb == 0 else (sb["x1"], sb["y1"])
        va = (oa[0] - px, oa[1] - py)
        vb = (ob[0] - px, ob[1] - py)
        la, lb = math.hypot(*va), math.hypot(*vb)
        if la < 1e-6 or lb < 1e-6:
            continue
        cos = max(-1.0, min(1.0, (va[0] * vb[0] + va[1] * vb[1]) / (la * lb)))
        deg = math.degrees(math.acos(cos))
        if deg < min_deg:
            out.append({"net": key[0], "layer": key[1], "x": px, "y": py,
                        "deg": round(deg, 2), "a": ia, "b": ib, "ea": ea, "eb": eb})
    out.sort(key=lambda j: (j["net"], j["layer"], j["x"], j["y"]))
    return out


def _box_sum(a, k):
    """Sum over every k x k window anchored at its top-left, shape (H-k+1, W-k+1)."""
    c = np.zeros((a.shape[0] + 1, a.shape[1] + 1), np.int64)
    c[1:, 1:] = a.cumsum(0, dtype=np.int64).cumsum(1)
    return c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]


def opening_thin(mask, k):
    """Pixels of `mask` that a k x k square cannot cover (the morphological opening
    removes them). k >= 1 pixels."""
    if k <= 1:
        return np.zeros_like(mask)
    H, W = mask.shape
    s = _box_sum(mask.astype(np.uint8), k)
    eroded = np.zeros((H, W), bool)
    eroded[:H - k + 1, :W - k + 1] = s == k * k          # square anchored at (y, x)
    e2 = np.pad(eroded.astype(np.uint8), ((k - 1, 0), (k - 1, 0)))
    dilated = _box_sum(e2, k) > 0                         # any anchor covering (y, x)
    return mask & ~dilated


def slivers(polys, min_mm, res_mm=None):
    """polys: dicts layer, net, outline=[(x, y)...] (mm), holes=[[(x, y)...]...].
    Returns dicts layer, net, x, y, px (pixels in the thin component) for copper
    thinner than min_mm (square opening)."""
    res = res_mm or max(min_mm / 4.0, 0.01)
    k = max(2, int(round(min_mm / res)))
    groups = defaultdict(list)
    for p in polys:
        groups[(p["layer"], p["net"])].append(p)
    out = []
    for (layer, net), plist in groups.items():
        xs = [x for p in plist for x, _ in p["outline"]]
        ys = [y for p in plist for _, y in p["outline"]]
        x0, y0 = min(xs) - 2 * res, min(ys) - 2 * res
        W = int(math.ceil((max(xs) - x0) / res)) + 4
        H = int(math.ceil((max(ys) - y0) / res)) + 4
        tr = lambda pts: [((x - x0) / res, (y - y0) / res) for x, y in pts]
        # outlines and holes together: even-odd, so a hole subtracts
        mask = GC.scan_mask(H, W, [tr(p["outline"]) for p in plist if len(p["outline"]) >= 3]
                            + [tr(h) for p in plist for h in p.get("holes", []) if len(h) >= 3])
        thin = opening_thin(mask, k)
        lab, n = GC._label8(thin)
        for i in range(1, n + 1):
            ys_i, xs_i = np.nonzero(lab == i)
            if ys_i.size < k:
                continue
            out.append({"layer": layer, "net": net,
                        "x": round(x0 + (xs_i.mean() + 0.5) * res, 3),
                        "y": round(y0 + (ys_i.mean() + 0.5) * res, 3),
                        "px": int(ys_i.size)})
    return out


# -------------------------------------------------------------- repair (opt-in)
def _pt(x, y):
    import pcbnew
    return pcbnew.VECTOR2I(int(round(x * 1e6)), int(round(y * 1e6)))


def _junction_is_bare(board, P, layer, net):
    """No via and no pad at the junction point: otherwise shortening the tracks
    would disconnect the via/pad from the copper it feeds."""
    import pcbnew
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            if abs(pcbnew.ToMM(p.x) - P[0]) < 0.002 and abs(pcbnew.ToMM(p.y) - P[1]) < 0.002:
                return False
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.HitTest(_pt(*P)):
                return False
    return True


def chamfer_junctions(board, min_deg=DEFAULT_ACID_DEG, max_chamfer_mm=0.3, log=print):
    """Chamfer every bare acute junction on `board` (pcbnew object, edited in place).
    Each wedge tip is cut by one segment of the narrower track's width between
    points on the two tracks, `min(max_chamfer, 0.4 * length)` from the tip.
    Junctions under 5 degrees (overlapping or folded tracks) are left alone:
    a chamfer there is a zero-length cut and the copper problem is the overlap.
    Returns the number of junctions chamfered."""
    import pcbnew
    tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"]
    segs = []
    for t in tracks:
        s, e = t.GetStart(), t.GetEnd()
        segs.append({"net": t.GetNetname(), "layer": board.GetLayerName(t.GetLayer()),
                     "x1": pcbnew.ToMM(s.x), "y1": pcbnew.ToMM(s.y),
                     "x2": pcbnew.ToMM(e.x), "y2": pcbnew.ToMM(e.y), "kind": "track"})
    done = 0
    touched = set()
    for j in acute_junctions(segs, min_deg):
        if j["a"] in touched or j["b"] in touched:
            continue            # one chamfer per track per pass; DRC loop repeats
        if j["deg"] < 5.0:
            continue            # ~0 deg = overlapping or folded tracks, not a wedge to cut
        P = (j["x"], j["y"])
        if not _junction_is_bare(board, P, j["layer"], j["net"]):
            continue
        ta, tb = tracks[j["a"]], tracks[j["b"]]
        sa, sb = segs[j["a"]], segs[j["b"]]
        la = math.hypot(sa["x2"] - sa["x1"], sa["y2"] - sa["y1"])
        lb = math.hypot(sb["x2"] - sb["x1"], sb["y2"] - sb["y1"])
        c = min(max_chamfer_mm, 0.4 * la, 0.4 * lb)
        if c <= 0.005:
            continue
        pts = []
        for s, e_ in ((sa, j["ea"]), (sb, j["eb"])):
            ox, oy = (s["x2"], s["y2"]) if e_ == 0 else (s["x1"], s["y1"])
            L = math.hypot(ox - P[0], oy - P[1])
            pts.append((P[0] + (ox - P[0]) / L * c, P[1] + (oy - P[1]) / L * c))
        for t, e_, q in ((ta, j["ea"], pts[0]), (tb, j["eb"], pts[1])):
            if e_ == 0:
                t.SetStart(_pt(*q))
            else:
                t.SetEnd(_pt(*q))
        w = min(ta.GetWidth(), tb.GetWidth())
        nt = pcbnew.PCB_TRACK(board)
        nt.SetStart(_pt(*pts[0]))
        nt.SetEnd(_pt(*pts[1]))
        nt.SetWidth(w)
        nt.SetLayer(ta.GetLayer())
        nt.SetNetCode(ta.GetNetCode())
        board.Add(nt)
        touched.update((j["a"], j["b"]))
        done += 1
    log(f"    acid-trap chamfers applied: {done}")
    return done


def fix_junctions(work, kicad_cli="kicad-cli", min_deg=DEFAULT_ACID_DEG, log=print):
    """DRC-guarded: chamfer the bare acute junctions of `work` (a .kicad_pcb, edited
    in place only if kept). Kept when DRC violations and unconnected items do not
    rise. Returns the number of junctions chamfered (0 when nothing was kept)."""
    import pcbnew
    from fluxplace import tune as TU
    base = DG.drc_keys(work, kicad_cli)
    b = pcbnew.LoadBoard(work)
    n = chamfer_junctions(b, min_deg=min_deg, log=log)
    if not n:
        return 0
    tmp = work + ".acid.kicad_pcb"
    pcbnew.SaveBoard(tmp, b)
    stem = os.path.splitext(work)[0]
    for ext in (".kicad_pro", ".kicad_dru"):
        if os.path.exists(stem + ext):
            shutil.copy(stem + ext, os.path.splitext(tmp)[0] + ext)
    ok, cur, new = DG.check(tmp, base, kicad_cli, log=None)
    v0, v1 = base[1], cur[1]
    if ok:
        shutil.move(tmp, work)
        log(f"    acid-trap fix kept: DRC {v0[0]}/{v0[1]} -> {v1[0]}/{v1[1]}, keys +0 "
            f"(-{len(base[0] - cur[0])} removed)")
        return n
    os.unlink(tmp)
    log(f"    acid-trap fix rejected: DRC {v0[0]}/{v0[1]} -> {v1[0]}/{v1[1]}, {len(new)} new key(s)")
    return 0
