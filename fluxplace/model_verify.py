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
import glob
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


def _board_to_fp(fp, dx, dy):
    """Board-offset (mm, relative to the footprint anchor) -> true
    footprint-local frame (the same frame the raw .kicad_pcb pad `(at)`
    values and the FP_3DMODEL offset/rotate live in).

    Root cause (2026-09-06, J5 mis-rotation): the old inline un-rotation
    here used the matrix [[c, s], [-s, c]] on (dx, dy) — the SAME matrix
    KiCad's own placement uses to go local -> board, not its inverse. Two
    forward applications compose to Rot(-2*theta) relative to true local,
    which is the identity only at theta = 0/180 and an exact 180 DEGREE
    NEGATION at theta = +/-90 — invisible on this board's many 0/180
    footprints, silent and exact-180 on any 90/270 one. Verified against
    J5 (fp orientation 90): the old formula returned its TH peg holes as
    (2.89, 2.605) while the raw file — and the model's own untransformed
    pin geometry — put them at (2.89, -2.605); the corrected matrix
    [[c, -s], [s, c]] reproduces the raw file exactly. That silently-wrong
    180-degree target is what then made `solve_transform` "solve" a 180
    rotation for J5's model: the model's real (correct, rot-0) geometry
    was being scored against a target that was itself rotated 180 from
    the truth, so ONLY a 180-rotated model could ever land on it."""
    r = math.radians(fp.GetOrientationDegrees())
    c, s = math.cos(r), math.sin(r)
    return (dx * c - dy * s, dx * s + dy * c)


def _th_holes(fp, max_drill=2.0):
    """Footprint-local positions of pin-scale through holes. Drills at or
    above max_drill are mechanical (standoffs, M2.5+ mounting) — a body
    model owes them nothing."""
    out = []
    orig = fp.GetPosition()
    for p in fp.Pads():
        if p.GetDrillSize().x <= 0 or p.GetDrillSize().x >= max_drill * 1e6:
            continue
        dx = (p.GetPosition().x - orig.x) / 1e6
        dy = (p.GetPosition().y - orig.y) / 1e6
        out.append(_board_to_fp(fp, dx, dy))
    return out


def _smd_pads(fp):
    """Footprint-local positions of non-drilled (SMD) copper pads — the
    ground truth an SMD connector's leads must land on. Same corrected
    board->local transform as `_th_holes`."""
    out = []
    orig = fp.GetPosition()
    for p in fp.Pads():
        if p.GetDrillSize().x > 0:
            continue
        if not (p.IsOnLayer(pcbnew.F_Cu) or p.IsOnLayer(pcbnew.B_Cu)):
            continue
        dx = (p.GetPosition().x - orig.x) / 1e6
        dy = (p.GetPosition().y - orig.y) / 1e6
        out.append(_board_to_fp(fp, dx, dy))
    return out


def _fab_extent(fp):
    """Footprint-local (mm) (width, height) of the footprint's own F.Fab/
    B.Fab drawing, or its F.CrtYd/B.CrtYd courtyard when it carries no Fab
    graphics. Ground truth for `posture_gap`: the package outline as drawn,
    independent of whatever a 3D model's own raw geometry happens to be.
    Uses each graphic item's own board-frame bounding box, corner-mapped
    into the footprint-local frame via `_board_to_fp` — exact for the
    common case (rectangles/lines at the footprint's own 0/90/180/270
    orientation), a reasonable approximation otherwise. None when the
    footprint carries neither layer's graphics (e.g. a bare-pads test
    fixture)."""
    for layers in ((pcbnew.F_Fab, pcbnew.B_Fab), (pcbnew.F_CrtYd, pcbnew.B_CrtYd)):
        xs, ys = [], []
        orig = fp.GetPosition()
        for item in fp.GraphicalItems():
            if item.GetLayer() not in layers:
                continue
            bb = item.GetBoundingBox()
            for cx, cy in ((bb.GetLeft(), bb.GetTop()), (bb.GetRight(), bb.GetTop()),
                          (bb.GetLeft(), bb.GetBottom()), (bb.GetRight(), bb.GetBottom())):
                lx, ly = _board_to_fp(fp, (cx - orig.x) / 1e6, (cy - orig.y) / 1e6)
                xs.append(lx); ys.append(ly)
        if len(xs) >= 2:
            return max(xs) - min(xs), max(ys) - min(ys)
    return None


def _fit(tips, holes):
    """Greedy nearest-hole match: mean + max distance from each hole to the
    nearest tip cluster. Holes without any nearby tip dominate via max."""
    if not tips or not holes:
        return float("inf"), float("inf")
    ds = []
    for hx, hy in holes:
        ds.append(min(math.hypot(hx - tx, hy - ty) for tx, ty in tips))
    return sum(ds) / len(ds), max(ds)


def _lenient_fit(a, b):
    """Worst distance checked from both directions, but — unlike
    `_dual_fit` — reports the BETTER (min) of the two: "flag only when
    BOTH misfit", same lenient gate `verify_footprint`'s TH-connector WARN
    already uses. Used for the SMD-lead-vs-pad check: a real STEP's z-band
    slice picks up extra structural points (shield tabs, corner posts)
    beyond just the leads, so requiring every model cluster to have a
    nearby pad (as `_dual_fit` does) fails even a correctly-seated model —
    measured on J5's real geometry, 2026-09-06 (58 clusters against 12
    unique pad X-positions). The pads-have-a-nearby-lead direction alone
    is still a clean, well-separated signal (0.33mm at the correct
    rotation vs 4.76mm at 180)."""
    if not a or not b:
        return float("inf")
    _, d_ab = _fit(a, b)
    _, d_ba = _fit(b, a)
    return min(d_ab, d_ba)


def _dual_fit(a, b):
    """Worst hole-to-pin distance checked from BOTH directions (every `a`
    has a nearby `b`, AND every `b` has a nearby `a`) — a partial/subset
    match (e.g. a rotation that only lines up half the points) cannot look
    good by scoring just one direction. Contrast with `verify_footprint`'s
    own `min(holes_d, tips_d)` — that one is a deliberately lenient
    "flag only when BOTH misfit" WARN gate; a solver choosing between
    candidate rotations wants the opposite bias, so this takes the max."""
    if not a or not b:
        return float("inf")
    _, d_ab = _fit(a, b)
    _, d_ba = _fit(b, a)
    return max(d_ab, d_ba)


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


def posture_gap(rotated_pts, fab_extent, shortfall_frac=0.5, recovery_frac=0.75):
    """WARN text (or None) when a model looks like it's standing on edge
    rather than lying flat against the board.

    `rotated_pts`: the model's points after its OWN rotation/scale (any
    axes — `_rotate_xyz`), footprint-local mm, translation still pending
    (extent is translation-invariant, so the FP_3DMODEL offset doesn't
    matter here). `fab_extent`: (width, height) of the footprint's own
    F.Fab/courtyard outline (`_fab_extent`) — the package footprint as
    drawn, ground truth for how the part should look from above.

    The signature of "on its side": ONE of the body's current XY extents
    is far SHORTER than the matching fab dimension (its true in-plane size
    got rotated into Z instead) while the body's Z extent is close to that
    same fab dimension — i.e. a 90-degree rotation about the OTHER axis
    would swap Z back into that XY slot and recover the size. Real case
    (Q1, PowerPAK SO-8, 7cb0f77): rot(0,0,0) gives XY (6.24, 1.50) against
    a (5.99, 5.00) fab outline — height collapsed to 30% of true — while Z
    depth is 5.93, almost exactly the missing height.

    Deliberately NOT a close-match-both-ways test (comparing the full XY
    bbox to the fab bbox within a tolerance): real STEP bodies routinely
    have leads/shields extending past the drawn Fab/Courtyard outline (a
    SOT-23's leads splay wider than its Fab body; an RJ45 magjack's shield
    tabs run past its Courtyard) — that is normal, not a posture defect,
    and a tight two-sided match flags it anyway. Requiring a genuine
    SHORTFALL recoverable by a swap is narrower and, measured against the
    real V1.5 board (133 modelled footprints, HEAD), fires on nothing but
    the injected defect."""
    if not fab_extent or not rotated_pts:
        return None
    fw, fh = fab_extent
    if fw <= 0 or fh <= 0:
        return None
    xs = [p[0] for p in rotated_pts]
    ys = [p[1] for p in rotated_pts]
    zs = [p[2] for p in rotated_pts]
    w, h, d = max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)

    if h < shortfall_frac * fh and d >= recovery_frac * fh:
        return (f"body on its side — X extent collapsed to {h:.2f}mm of a "
                f"{fh:.2f}mm fab outline while Z runs {d:.2f}mm; rotate "
                f"90° about X to lay it flat")
    if w < shortfall_frac * fw and d >= recovery_frac * fw:
        return (f"body on its side — Y extent collapsed to {w:.2f}mm of a "
                f"{fw:.2f}mm fab outline while Z runs {d:.2f}mm; rotate "
                f"90° about Y to lay it flat")
    return None


def buried_mass_gap(rotated_pts, offset_z, buried_tol=0.5, buried_frac=0.5,
                    min_pts=20):
    """WARN text (or None) when most of a model's own mass sits below the
    board's mount plane once its FP_3DMODEL Z offset is applied — a body
    correctly seated on the board reaches AWAY from it (z >= ~0 for most
    of its bulk); if instead most of it is buried more than `buried_tol`
    below the plane, with some part still above it, the model is oriented
    upside down (or badly misplaced) even though its own raw geometry
    never had a bound near its own origin (so `seat_gap` — which requires
    exactly that to trust a model's authored seat — has no ground truth to
    judge it against and correctly declines).

    Real case (T1/T2, Bourns SM-LP-5001, an earlier stand-in STEP,
    172264a/4719085): rot(90,0,0), offset.z 0.818 puts 92% of the model's
    points below z=-0.5mm (2-3mm buried) with only a thin cap above —
    `seat_gap` explicitly stays silent on this exact case (neither raw
    bound near 0 — see test_seat_gap_none_when_neither_bound_is_near_the_
    origin) because a large offset.z is trusted as a deliberate alignment
    on a body not authored seat-at-origin. This is a different question:
    not "is the offset a defect" but "which way is the body facing" —
    and the buried-mass signature answers it independent of authorship.
    Measured clean (zero false positives) against every modelled
    footprint on the real V1.5 board at HEAD."""
    if len(rotated_pts) < min_pts:
        return None
    zs = [p[2] + offset_z for p in rotated_pts]
    n = len(zs)
    below = sum(1 for z in zs if z < -buried_tol)
    above = sum(1 for z in zs if z > buried_tol)
    if above > 0 and below / n >= buried_frac:
        return (f"body upside down or badly buried — {below/n*100:.0f}% of "
                f"its points sit more than {buried_tol}mm below the mount "
                f"plane after the model's own offset/rotation")
    return None


def _library_models(mod_path):
    """[(model_filename, offset_xyz, rotate_xyz), ...] for every (model
    ...) block in a .kicad_mod file — a small paren-balanced text scan (no
    full s-expression parser needed for files this size, same approach the
    rest of this module uses for STEP files)."""
    try:
        data = open(mod_path, "r", errors="ignore").read()
    except OSError:
        return []
    out = []
    for m in re.finditer(r'\(model\s+"([^"]+)"', data):
        start = m.start()
        depth, i = 0, start
        while i < len(data):
            if data[i] == "(":
                depth += 1
            elif data[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        block = data[start:i + 1]
        off = re.search(r'\(offset\s*\(xyz\s+([-\d.eE]+)\s+([-\d.eE]+)\s+([-\d.eE]+)\)\s*\)', block)
        rot = re.search(r'\(rotate\s*\(xyz\s+([-\d.eE]+)\s+([-\d.eE]+)\s+([-\d.eE]+)\)\s*\)', block)
        offset = tuple(float(v) for v in off.groups()) if off else (0.0, 0.0, 0.0)
        rotate = tuple(float(v) for v in rot.groups()) if rot else (0.0, 0.0, 0.0)
        out.append((m.group(1), offset, rotate))
    return out


def find_library_footprint(fp_name, project_libs=()):
    """Absolute path to <fp_name>.kicad_mod in a project .pretty ([docs]
    project_libs) or a stock KiCad footprint library, or None. Board
    footprints in this project carry no library nickname (FPID lib is
    blank once placed), so this matches by footprint NAME alone across
    every .pretty directory — same precedence review.check_landpattern
    already uses for project libraries, extended to KiCad's own stock
    libraries (glob is self-contained; no lib-table lookup needed)."""
    for d in project_libs:
        cand = os.path.join(d, fp_name + ".kicad_mod")
        if os.path.exists(cand):
            return cand
    for pretty in sorted(glob.glob("/usr/share/kicad*/footprints/*.pretty")):
        cand = os.path.join(pretty, fp_name + ".kicad_mod")
        if os.path.exists(cand):
            return cand
    return None


def library_transform_diff(board_file, board_offset, board_rotate,
                           lib_file, lib_offset, lib_rotate, tol=0.05):
    """(level, message) when a board footprint's FP_3DMODEL transform
    disagrees with the same-named library footprint's own — None when
    they agree within `tol` (mm for offset, degrees for rotate).

    WARN when the board still references the SAME model file the library
    does (the transform alone silently diverged from the library's own —
    real case, J5 2026-09-06: a `--fix` run flipped the board's copy of
    the stock USB-C receptacle model 180 degrees about Z while the
    library's own footprint kept rotate (0,0,0)). INFO when the file
    differs too — a deliberately swapped-in body (a different real STEP,
    or the project's own self-contained copy of the same asset under
    hardware/lib/3dmodels) is a different situation, not a mis-transform,
    and gets a lighter finding."""
    d_off = max(abs(a - b) for a, b in zip(board_offset, lib_offset))
    d_rot = max(abs(a - b) for a, b in zip(board_rotate, lib_rotate))
    if d_off <= tol and d_rot <= tol:
        return None
    same_file = os.path.basename(str(board_file)) == os.path.basename(str(lib_file))
    level = "WARN" if same_file else "INFO"
    return (level,
            f"transform differs from the library's (offset "
            f"{tuple(round(v, 3) for v in board_offset)} vs "
            f"{tuple(round(v, 3) for v in lib_offset)}, rotate "
            f"{tuple(round(v, 1) for v in board_rotate)} vs "
            f"{tuple(round(v, 1) for v in lib_rotate)})")


def overlay_registration_gap(rotated_pts, offset, pad_bbox, margin=5.0,
                             z_tol=0.3, min_pts=10):
    """WARN text (or None) when a large module/overlay model — a second
    body on a footprint, its own XY footprint far bigger than the
    footprint it's attached to (a CM5 module drawn on its mating
    connector's footprint, say) — doesn't have enough of its own geometry
    sitting near the mount plane AND inside this footprint's own pad
    field. `pad_bbox` is (xmin, xmax, ymin, ymax), footprint-local mm
    (holes + SMD pads, unioned) — where THIS footprint's actual pins are,
    as opposed to the overlay's own (much larger) bounding box.

    Real case (J10, CM5R5 module overlay, 3beae58): the module was drawn
    rotated the wrong way about Z for two revisions. Its symmetric
    mounting holes still lined up (blind to the error), but its
    asymmetric mating plug landed off J10's actual pad field. Empirically
    tuned against that one case (5 points landed in-window on the broken
    revision vs 15 on the corrected one — the only footprint on this
    board whose models even qualify as an overlay at all, so there is no
    wider population to validate `min_pts` against; treat it as a first
    cut)."""
    ox, oy, oz = offset
    x0, x1, y0, y1 = pad_bbox
    x0, x1, y0, y1 = x0 - margin, x1 + margin, y0 - margin, y1 + margin
    inwin = sum(1 for x, y, z in rotated_pts
               if abs(z + oz) <= z_tol and x0 <= x + ox <= x1 and y0 <= y + oy <= y1)
    if inwin < min_pts:
        return (f"overlay's mating geometry does not land on this "
                f"footprint ({inwin} of its points sit near the mount "
                f"plane inside this footprint's pad field, want >= {min_pts})")
    return None


def model_file_readable(path):
    """True if a 3D model file looks like it will actually render
    something rather than silently coming up blank in the 3D viewer: a
    STEP with a real point cloud (>=50 CARTESIAN_POINTs — the same floor
    `verify_footprint` uses before it trusts a model's own geometry), or a
    WRL that at least declares a Shape/IndexedFaceSet node. An unknown
    extension is not this function's call to make (returns True) — some
    other check's job.

    Real case (J12, RJ45 magjack, 2026-09-04-ish): an EasyEDA-fetched
    STEP/WRL that existed on disk and resolved to a real path, but whose
    content the 3D viewer could never actually turn into geometry — every
    upstream check (file exists, path resolves) passed; nothing checked
    this far."""
    if not path or not os.path.exists(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext in (".step", ".stp"):
        return len(step_points(path)) >= 50
    if ext == ".wrl":
        try:
            data = open(path, "rb").read().decode("utf-8", errors="ignore")
        except OSError:
            return False
        return bool(re.search(r"\bShape\b", data)) and "IndexedFaceSet" in data
    return True


def verify_footprint(fp, resolve, tol=0.6, project_libs=()):
    """Check one footprint's model registration.
    Returns list of (level, issue) findings; empty = registered.

    `project_libs`: .pretty directories to search first for a same-named
    library footprint (the LIBRARY-TRANSFORM check, `library_transform_
    diff`) — passed through from [docs] project_libs; KiCad's own stock
    libraries are always searched too."""
    holes = _th_holes(fp)
    smd_pads = _smd_pads(fp)
    fab_extent = _fab_extent(fp)
    all_pads = holes + smd_pads
    pad_bbox = None
    if all_pads:
        pxs = [p[0] for p in all_pads]
        pys = [p[1] for p in all_pads]
        pad_bbox = (min(pxs), max(pxs), min(pys), max(pys))
    fp_bbox_area = (fab_extent[0] * fab_extent[1]) if fab_extent else None
    lib_path = find_library_footprint(str(fp.GetFPID().GetLibItemName()), project_libs)
    lib_models = _library_models(lib_path) if lib_path else []
    models = list(fp.Models())
    findings = []
    for idx, m in enumerate(models):
        board_off = (m.m_Offset.x, m.m_Offset.y, m.m_Offset.z)
        board_rot = (m.m_Rotation.x, m.m_Rotation.y, m.m_Rotation.z)

        # LIBRARY-TRANSFORM — needs only the transform numbers, not the
        # model's own geometry, so it runs even for a model this function
        # otherwise can't judge (unresolvable path, WRL, trivial point
        # count).
        if idx < len(lib_models):
            lib_file, lib_off, lib_rot = lib_models[idx]
            diff = library_transform_diff(str(m.m_Filename), board_off, board_rot,
                                          lib_file, lib_off, lib_rot)
            if diff is not None:
                level, msg = diff
                findings.append((level,
                                 f"model {os.path.basename(str(m.m_Filename))}: {msg}"))

        path = resolve(str(m.m_Filename))
        if path is None:
            continue                      # unresolvable = component_audit's job
        pts = step_points(path)
        if len(pts) < 50:
            continue                      # wrl or trivial model — can't judge
        off, sc = board_off, (m.m_Scale.x, m.m_Scale.y, m.m_Scale.z)

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

        # POSTURE — on its side rather than flat.
        posture = posture_gap(rotated, fab_extent)
        if posture is not None:
            findings.append(("WARN", f"model {os.path.basename(path)}: {posture}"))

        # UPSIDE-DOWN — most of the body's mass buried below the plane.
        buried = buried_mass_gap(rotated, off[2])
        if buried is not None:
            findings.append(("WARN", f"model {os.path.basename(path)}: {buried}"))

        # MATED OVERLAY — a second, much-larger body (a module drawn on
        # its mating connector's footprint) whose own mating geometry
        # doesn't land inside this footprint's actual pad field.
        if len(models) >= 2 and fp_bbox_area and pad_bbox:
            xs = [p[0] for p in rotated]
            ys = [p[1] for p in rotated]
            area = (max(xs) - min(xs)) * (max(ys) - min(ys))
            if area > 3 * fp_bbox_area:
                overlay = overlay_registration_gap(rotated, off, pad_bbox)
                if overlay is not None:
                    findings.append(("WARN", f"model {os.path.basename(path)}: {overlay}"))

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

        # SMD-lead check — independent of, and in addition to, the TH pin
        # fit above. A connector like J5 (USB-C: 2 TH alignment pegs + 4 TH
        # shell/mounting holes, 16 SMD signal leads) goes through the
        # is_connector TH branch on its pegs/shell holes alone; that branch
        # never looks at the 16 real signal leads at all. Catches a body
        # that is rotated/mirrored in a way the (small, sometimes
        # near-symmetric) TH hole pattern doesn't expose.
        #
        # z-band: leads sit right at the mount plane (a sharp cluster at
        # z==0 on the real J5 STEP, 185 of ~3400 points) — narrower than
        # the +0.35 upper bound first tried, which also swept in shield/
        # shroud geometry above the leads and made even a correctly-seated
        # model fail (58 spurious clusters against 12 real pad positions,
        # measured 2026-09-06). [-0.05, 0.05] isolates the lead flange.
        if len(smd_pads) >= 4:
            leads_raw = [(x, y) for x, y, z in loc if -0.05 <= z <= 0.05]
            leads = _clusters(leads_raw)
            if len(leads) >= max(2, len(smd_pads) // 2):
                smd_worst = _lenient_fit(leads, smd_pads)
                if smd_worst > tol:
                    findings.append(("WARN",
                                     f"model {os.path.basename(path)}: SMD "
                                     f"leads not on their pads (worst "
                                     f"{smd_worst:.2f}mm, tol {tol}) — body "
                                     f"rotated/mirrored?"))
    return findings


def solve_transform(pts, holes, smd_pads=None, tol=0.6,
                    z_lift_scan=(0.0, 1.0, 2.0, 3.0, 4.0)):
    """Find (rot_z, offset_x, offset_y, offset_z) landing the model's pin
    shafts on the holes. Scans 4 rotations x candidate z-lifts; translation
    from mean(tips)->mean(holes); scores by the worse of both fit
    directions (_dual_fit — a partial/subset match cannot look good).

    Identity (rot 0) is the prior: a non-zero rotation is only chosen when
    it clears identity's own score by a solid margin AND — when smd_pads
    is given — does not make the SMD-lead fit worse than identity's own
    (J5, 2026-09-06: its 2 TH pegs + 4 TH shell holes fit BETTER at 180
    than at the correct 0 by the TH metric alone; the SMD leads, ignored
    by the old code, tell the two apart). Returns (best, max_d)."""
    best = None
    zero = None
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
            moved = _clusters([(x + dx, y + dy) for x, y in sub])
            worst_d = _dual_fit(moved, holes)

            smd_worst = None
            if smd_pads:
                leads = _clusters([(x + dx, y + dy) for x, y, z in loc
                                   if -0.05 <= z <= 0.05])
                if len(leads) >= max(2, len(smd_pads) // 2):
                    smd_worst = _lenient_fit(leads, smd_pads)

            # translate back to model-frame offset: fp dx -> offset x,
            # fp dy -> offset y NEGATED (model y-up)
            cand = ((rot, dx, -dy, lift), worst_d, smd_worst)
            if rot == 0 and (zero is None or worst_d < zero[1]):
                zero = cand
            if best is None or worst_d < best[1]:
                best = cand

    if best is None:
        return None, float("inf")
    if zero is not None and best[0][0] != 0:
        improves = best[1] < zero[1] - 0.05
        smd_ok = True
        if best[2] is not None and zero[2] is not None:
            smd_ok = best[2] <= zero[2] + 0.05 and best[2] <= tol
        elif best[2] is not None:
            smd_ok = best[2] <= tol
        if not (improves and smd_ok):
            best = zero
    return best[0], best[1]


def verify_board(board, resolve, fix=False, tol=0.6, log=print, project_libs=()):
    """Verify (and optionally fix) every footprint model registration.
    Returns [(ref, finding), ...]; with fix=True, solvable TH mismatches are
    re-transformed in place (caller saves the board)."""
    out = []
    for fp in sorted(board.GetFootprints(), key=lambda f: f.GetReference()):
        ref = fp.GetReference()
        finds = verify_footprint(fp, resolve, tol=tol, project_libs=project_libs)
        for lvl, msg in finds:
            out.append((ref, f"{lvl} {msg}"))
        if not (fix and finds):
            continue
        holes = _th_holes(fp)
        if not holes:
            continue
        smd_pads = _smd_pads(fp)
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
                    # current (on-board) registration is the PRIOR — for a
                    # KiCad-library footprint this is exactly the library's
                    # own model transform (usually offset/rotate all-zero).
                    # If it already fits within tol, on both metrics, it is
                    # never rewritten — regardless of whether some other
                    # rotation scores marginally better (J5, 2026-09-06:
                    # its 2 TH pegs + 4 TH shell holes alone fit BETTER at
                    # 180 than at the correct 0; without this guard the old
                    # code happily "solved" its way to the wrong rotation).
                    loc = _model_to_fp(pts, entry[1], entry[2][2])
                    cur_tips = _clusters([(x, y) for x, y, z in loc
                                          if z < -0.25])
                    cur_d = _dual_fit(cur_tips, holes)
                    cur_smd_d = None
                    if smd_pads:
                        cur_leads = _clusters([(x, y) for x, y, z in loc
                                               if -0.05 <= z <= 0.05])
                        if len(cur_leads) >= max(2, len(smd_pads) // 2):
                            cur_smd_d = _lenient_fit(cur_leads, smd_pads)
                    cur_ok = cur_d <= tol and (cur_smd_d is None or cur_smd_d <= tol)
                    if cur_ok:
                        log(f"  {ref}: current transform already within "
                            f"tol ({cur_d:.2f}mm TH"
                            + (f", {cur_smd_d:.2f}mm SMD" if cur_smd_d is not None else "")
                            + ") — kept as the prior")
                        entries.append(entry)
                        continue
                    sol, max_d = solve_transform(pts, holes, smd_pads=smd_pads, tol=tol)
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
