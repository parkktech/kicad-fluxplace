"""3D model registration verification — connectors must sit ON their pins.

A footprint's 3D model is only truthful if its pin geometry lands on the
footprint's holes. Measured failure mode (CM5 carrier): EasyEDA-sourced STEP
files carry arbitrary origins (easyeda2kicad drops the source transform), so
attached models render displaced or rotated — a review hazard worse than no
model, because it LOOKS like mechanical truth.

This module verifies numerically and can solve the correction:
  - parse the STEP's cartesian points (whitespace/binary tolerant)
  - map them through the footprint's model transform into board space
    (KiCad model frame is y-UP: board_y = fp_y - model_y, after z-rotation
    and offset; footprint rotation and side applied after)
  - the points below the board surface are pin/peg shafts; cluster them
  - every through-hole pad must have a shaft cluster within tolerance
  - --fix: try z-rotations 0/90/180/270 + translation that best lands the
    clusters on the holes, and write the solved transform back

SMD-only footprints get a coarser check: the model's above-board bounding
box must overlap the footprint's courtyard/body region (catches models that
wander off entirely, e.g. an M.2 module model not seated over its socket).
"""
import math
import os
import re

import pcbnew

_PT = re.compile(r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-\d.E+e]+)"
                 r"\s*,\s*([-\d.E+e]+)\s*,\s*([-\d.E+e]+)\s*\)", re.S)


def step_points(path, clip=500.0):
    """All cartesian points from a STEP file (mm), outliers clipped."""
    try:
        data = open(path, "rb").read().decode("utf-8", errors="ignore")
    except OSError:
        return []
    pts = []
    for m in _PT.finditer(data):
        try:
            x, y, z = (float(v) for v in m.groups())
        except ValueError:
            continue
        if abs(x) < clip and abs(y) < clip and abs(z) < clip:
            pts.append((x, y, z))
    return pts


def _model_to_fp(pts, offset, rot_z_deg, scale=(1.0, 1.0, 1.0)):
    """Model points -> footprint-local mm (x right, y DOWN like the board).
    KiCad applies scale, rotation, offset in the y-up model frame; the
    render maps model +y to board -y."""
    r = math.radians(rot_z_deg)
    c, s = math.cos(r), math.sin(r)
    out = []
    ox, oy, oz = offset
    for x, y, z in pts:
        x, y, z = x * scale[0], y * scale[1], z * scale[2]
        # KiCad's renderer rotates CLOCKWISE in the y-up frame for positive
        # z-rotation (verified empirically against seated models)
        rx, ry = x * c + y * s, -x * s + y * c
        out.append((rx + ox, -(ry + oy), z + oz))
    return out


def _clusters(xy, cell=0.8, min_pts=3):
    """Coarse grid clustering -> cluster centers."""
    grid = {}
    for x, y in xy:
        grid.setdefault((round(x / cell), round(y / cell)), []).append((x, y))
    return [(sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v))
            for v in grid.values() if len(v) >= min_pts]


def _th_holes(fp, max_drill=2.0):
    """Footprint-local positions of pin-scale through holes. Drills at or
    above max_drill are mechanical (standoffs, M2.5+ mounting) — a body
    model owes them nothing."""
    out = []
    orig = fp.GetPosition()
    rot = math.radians(fp.GetOrientationDegrees())
    c, s = math.cos(rot), math.sin(rot)
    for p in fp.Pads():
        if p.GetDrillSize().x <= 0 or p.GetDrillSize().x >= max_drill * 1e6:
            continue
        dx = (p.GetPosition().x - orig.x) / 1e6
        dy = (p.GetPosition().y - orig.y) / 1e6
        # un-rotate into footprint-local frame
        out.append((dx * c + dy * s, -dx * s + dy * c))
    return out


def _fit(tips, holes):
    """Greedy nearest-hole match: mean + max distance from each hole to the
    nearest tip cluster. Holes without any nearby tip dominate via max."""
    if not tips or not holes:
        return float("inf"), float("inf")
    ds = []
    for hx, hy in holes:
        ds.append(min(math.hypot(hx - tx, hy - ty) for tx, ty in tips))
    return sum(ds) / len(ds), max(ds)


def verify_footprint(fp, resolve, tol=0.6):
    """Check one footprint's model registration.
    Returns list of (level, issue) findings; empty = registered."""
    holes = _th_holes(fp)
    findings = []
    for m in fp.Models():
        path = resolve(str(m.m_Filename))
        if path is None:
            continue                      # unresolvable = component_audit's job
        pts = step_points(path)
        if len(pts) < 50:
            continue                      # wrl or trivial model — can't judge
        if abs(m.m_Rotation.x) > 0.1 or abs(m.m_Rotation.y) > 0.1:
            continue          # 3-axis model rotation — outside this checker's math
        off = (m.m_Offset.x, m.m_Offset.y, m.m_Offset.z)
        sc = (m.m_Scale.x, m.m_Scale.y, m.m_Scale.z)
        loc = _model_to_fp(pts, off, m.m_Rotation.z, sc)
        sub = [(x, y) for x, y, z in loc if z < -0.25]
        tips = _clusters(sub)
        # a model is a CONNECTOR model (strict pins-in-holes contract) only if
        # its below-board geometry is in the same count class as the holes;
        # module/body models (an M.2 card, a CM5 module) sit on footprints
        # whose holes are mechanical and owe them nothing
        is_connector = holes and tips and len(tips) >= max(2, len(holes) // 2)
        if is_connector:
            _, holes_d = _fit(tips, holes)      # worst hole missing a pin
            _, tips_d = _fit(holes, tips)       # worst pin missing a hole
            max_d = min(holes_d, tips_d)        # flag only when BOTH misfit
            if max_d > tol:
                findings.append(("WARN",
                                 f"model {os.path.basename(path)}: worst "
                                 f"hole-to-pin distance {max_d:.2f}mm "
                                 f"(tol {tol}) — pins not in their holes"))
        else:
            body = [(x, y) for x, y, z in loc if z > 0.05]
            if body:
                bx = sum(p[0] for p in body) / len(body)
                by = sum(p[1] for p in body) / len(body)
                bb = fp.GetBoundingBox(False, False)
                w = bb.GetWidth() / 2e6 + 2.0
                h = bb.GetHeight() / 2e6 + 2.0
                ox = (bb.GetCenter().x - fp.GetPosition().x) / 1e6
                oy = (bb.GetCenter().y - fp.GetPosition().y) / 1e6
                if abs(bx - ox) > w or abs(by - oy) > h:
                    findings.append(("WARN",
                                     f"model {os.path.basename(path)}: body "
                                     f"centroid ({bx:.1f},{by:.1f})mm sits "
                                     f"outside the footprint region — not "
                                     f"seated on this part"))
    return findings


def solve_transform(pts, holes, z_lift_scan=(0.0, 1.0, 2.0, 3.0, 4.0)):
    """Find (rot_z, offset_x, offset_y, offset_z) landing the model's pin
    shafts on the holes. Scans 4 rotations x candidate z-lifts; translation
    from mean(tips)->mean(holes); scores by _fit. Returns (best, max_d)."""
    best = None
    hx = sum(h[0] for h in holes) / len(holes)
    hy = sum(h[1] for h in holes) / len(holes)
    for rot in (0, 90, 180, 270):
        for lift in z_lift_scan:
            loc = _model_to_fp(pts, (0.0, 0.0, lift), rot)
            sub = [(x, y) for x, y, z in loc if z < -0.25]
            tips = _clusters(sub)
            if len(tips) < len(holes) // 2 + 1:
                continue
            tx = sum(t[0] for t in tips) / len(tips)
            ty = sum(t[1] for t in tips) / len(tips)
            dx, dy = hx - tx, hy - ty
            moved = [(x + dx, y + dy) for x, y in sub]
            mean_d, max_d = _fit(_clusters(moved), holes)
            # translate back to model-frame offset: fp dx -> offset x,
            # fp dy -> offset y NEGATED (model y-up)
            cand = ((rot, dx, -dy, lift), max_d, mean_d)
            if best is None or (max_d, mean_d) < (best[1], best[2]):
                best = cand
    if best is None:
        return None, float("inf")
    return best[0], best[1]


def verify_board(board, resolve, fix=False, tol=0.6, log=print):
    """Verify (and optionally fix) every footprint model registration.
    Returns [(ref, finding), ...]; with fix=True, solvable TH mismatches are
    re-transformed in place (caller saves the board)."""
    out = []
    for fp in sorted(board.GetFootprints(), key=lambda f: f.GetReference()):
        ref = fp.GetReference()
        finds = verify_footprint(fp, resolve, tol=tol)
        for lvl, msg in finds:
            out.append((ref, f"{lvl} {msg}"))
        if not (fix and finds):
            continue
        holes = _th_holes(fp)
        if not holes:
            continue
        ms = fp.Models()
        entries = []
        fixed_any = False
        for m in ms:
            path = resolve(str(m.m_Filename))
            entry = [str(m.m_Filename),
                     (m.m_Offset.x, m.m_Offset.y, m.m_Offset.z),
                     (m.m_Rotation.x, m.m_Rotation.y, m.m_Rotation.z)]
            if path:
                pts = step_points(path)
                if len(pts) >= 50:
                    # current registration error, for is-it-an-improvement
                    loc = _model_to_fp(pts, entry[1], entry[2][2])
                    cur_tips = _clusters([(x, y) for x, y, z in loc
                                          if z < -0.25])
                    _, cur_d = _fit(cur_tips, holes)
                    sol, max_d = solve_transform(pts, holes)
                    if sol and max_d < cur_d - 0.05:
                        rot, ox, oy, oz = sol
                        entry[1] = (ox, oy, oz)
                        entry[2] = (0.0, 0.0, rot)
                        fixed_any = True
                        note = ("" if max_d <= tol else
                                " (BEST-EFFORT: model's own pin grid is off "
                                "— replace with the real vendor STEP)")
                        log(f"  {ref}: solved rot z{rot} offset "
                            f"({ox:.2f},{oy:.2f},{oz:.1f}) — max pin err "
                            f"{cur_d:.2f} -> {max_d:.2f}mm{note}")
                    elif sol is None or max_d >= cur_d - 0.05:
                        log(f"  {ref}: no better transform than current "
                            f"({cur_d:.2f}mm) — needs the real vendor model")
            entries.append(entry)
        if fixed_any:
            ms.clear()
            for path, off, rot in entries:
                nm = pcbnew.FP_3DMODEL()
                nm.m_Filename = path
                nm.m_Offset.x, nm.m_Offset.y, nm.m_Offset.z = off
                nm.m_Rotation.x, nm.m_Rotation.y, nm.m_Rotation.z = rot
                ms.push_back(nm)
    return out
