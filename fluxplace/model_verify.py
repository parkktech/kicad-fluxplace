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


def _rotate_xyz(pts, rx_deg, ry_deg, rz_deg, scale=(1.0, 1.0, 1.0)):
    """Full X-then-Y-then-Z Euler rotation (R = Rz . Ry . Rx applied to each
    point) — the same order/sign KiCad's model rotation uses. Verified
    against the real DF40C-100DS-0.4V receptacle STEP (rot -90,0,90): raw
    model extents (22.6 x 3.4 x 2.1mm) map to a sane 22.6mm-long, 2.1mm-tall
    connector, not the reverse. Only used for the Z-seat check below — the
    XY pin-in-hole math in `verify_footprint` stays Z-rotation-only (its own
    `_model_to_fp`, already verified against seated boards) since a 3-axis
    rotation's effect on THAT math is unverified."""
    rx, ry, rz = math.radians(rx_deg), math.radians(ry_deg), math.radians(rz_deg)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    out = []
    for x, y, z in pts:
        x, y, z = x * scale[0], y * scale[1], z * scale[2]
        x, y, z = x, y * cx - z * sx, y * sx + z * cx           # Rx
        x, y, z = x * cy + z * sy, y, -x * sy + z * cy          # Ry
        x, y, z = x * cz - y * sz, x * sz + y * cz, z           # Rz
        out.append((x, y, z))
    return out


def seat_gap(zmin, zmax, offset_z, flipped=False, thickness=0.0,
             confidence_tol=0.3):
    """How far (mm) a model's seat face sits from its mounting plane, or
    None if this model's own geometry doesn't look authored with a seat at
    its origin (so there is no ground truth to judge the offset against).

    zmin/zmax: the model's bounding box in footprint-local mm, AFTER its own
    rotation/scale but BEFORE its FP_3DMODEL offset — the geometry as
    authored, still centered on whatever origin the STEP happened to use.
    offset_z: the FP_3DMODEL z-offset that is supposed to carry the model's
    seat face onto the mounting plane (footprint-local z=0 — KiCad's local
    frame calls the surface a footprint is placed on "zero" whether that
    copper layer is the front or the back of the board).
    flipped: True for a footprint on a back copper layer. Render-verified
    (2026-09-06, the J10/J11 case this was built for): KiCad applies the
    FP_3DMODEL offset in the footprint's OWN local frame BEFORE the
    back-side mirror/flip, so a positive offset_z moves a body away from
    the board on either side, front or back — the sign of a mis-seat is
    NOT flipped by which copper layer the footprint is on. `flipped` is
    kept as a parameter (some earlier, unverified doc guessed otherwise)
    in case a future asymmetric-model case needs it, but it does not
    change this function's result today.
    thickness: board thickness (mm) — accepted for a caller that wants the
    seat expressed as an absolute/world Z (front mount at 0, back mount at
    -thickness) instead of footprint-local; unused in the magnitude below,
    since local z=0 already means "this footprint's own mount plane" on
    either side of the board.

    Some models (vendor connector STEPs, in practice) are authored with the
    mating/seat face AT their own origin — the body/pins reach away from it
    on one side, so `zmin` or `zmax` sits near 0 even before any offset.
    That IS ground truth: a nonzero FP_3DMODEL offset then means exactly
    the seat gap (positive = floating above the mount plane, away from the
    board; negative = buried in the board). Many library body models
    (transformers, MOSFETs, radial caps) are authored some other way —
    centered, or measured from an arbitrary corner — where NEITHER bound is
    near 0 to begin with; their (possibly large) offset.z is a deliberate
    alignment, not a defect, and "nearest bound to 0" is not a seat face at
    all. `confidence_tol` gates on exactly that: only judge a model whose
    un-offset geometry already put a bound within it of the origin.
    """
    seat_raw = zmin if abs(zmin) <= abs(zmax) else zmax
    if abs(seat_raw) > confidence_tol:
        return None
    return seat_raw + offset_z


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
        off = (m.m_Offset.x, m.m_Offset.y, m.m_Offset.z)
        sc = (m.m_Scale.x, m.m_Scale.y, m.m_Scale.z)

        # Z-seat check — runs on every model regardless of rotation axes
        # (unlike the XY pin-fit math below, which only trusts a Z-only
        # rotation). Catches a body floating off/buried in the board even
        # when the STEP was authored lying on its side and needs a 3-axis
        # rotation to stand up (e.g. a receptacle rotated -90,0,90).
        rotated = _rotate_xyz(pts, m.m_Rotation.x, m.m_Rotation.y,
                              m.m_Rotation.z, sc)
        zs = [z for _, _, z in rotated]
        gap = seat_gap(min(zs), max(zs), off[2], flipped=fp.IsFlipped())
        if gap is not None and abs(gap) > tol:
            where = "buried in the board" if gap < 0 else "floating above the mount plane"
            findings.append(("WARN",
                             f"model {os.path.basename(path)}: seat gap "
                             f"{gap:+.2f}mm — {where} (tol {tol})"))

        if abs(m.m_Rotation.x) > 0.1 or abs(m.m_Rotation.y) > 0.1:
            continue          # 3-axis model rotation — outside the XY pin-fit math
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
