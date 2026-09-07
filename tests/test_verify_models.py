"""Tests for fluxplace.model_verify — 3D model seating, front and back side.

Regression for the 2026-09-06 bug: verify-models never flagged J10/J11 (a
Hirose DF40C receptacle on B.Cu, model rotated -90,0,90 to stand a
sideways-authored STEP upright) floating 0.75mm off the board and into the
CM5 module mounted below it — the old code's hard skip on any 3-axis model
rotation meant the model was never checked at all.
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import model_verify as MV


# --------------------------------------------------------------- seat_gap
def test_seat_gap_seated_flush_at_zmin():
    # body spans [0, 1.5] (its own seat face already at the STEP origin);
    # a correct offset of 0 lands the seat exactly on the mount plane.
    assert MV.seat_gap(0.0, 1.5, offset_z=0.0) == 0.0


def test_seat_gap_floating_front_side():
    # same body, but a stray +0.75mm z-offset — the classic "floating" case.
    gap = MV.seat_gap(0.0, 1.5, offset_z=0.75)
    assert gap == 0.75


def test_seat_gap_back_side_seated_offset_zero():
    # a back-side (flipped) footprint with a correcting offset of 0 must
    # still read as seated — this is the "corrected variant" in the report.
    assert MV.seat_gap(0.0, 1.5, offset_z=0.0, flipped=True) == 0.0


def test_seat_gap_back_side_floating_matches_report():
    # this is the literal broken case: J10/J11's DF40C model, rotated
    # -90,0,90, has raw (rotated) z-extent [-2.05, 0.05] (measured from the
    # real STEP — fluxplace/model_verify._rotate_xyz on the vendor file),
    # offset.z = 0.752238806, footprint on B.Cu (flipped). Render-verified
    # (2026-09-06): this body FLOATS ~0.75-0.85mm above the mount plane,
    # into the CM5 module below it — a positive gap, not negative.
    gap = MV.seat_gap(-2.05, 0.05, offset_z=0.752238806, flipped=True)
    assert 0.6 < gap < 1.0, f"expected a ~+0.75-0.85mm gap, got {gap}"


def test_seat_gap_nearest_bound_is_the_seat_regardless_of_which_end():
    # zmax (not zmin) is nearer zero here — it, not zmin, is the seat face.
    assert MV.seat_gap(-5.0, 0.02, offset_z=0.0) == 0.02
    assert MV.seat_gap(-0.02, 5.0, offset_z=0.0) == -0.02


def test_seat_gap_flip_does_not_change_sign_or_magnitude():
    # render-verified 2026-09-06: KiCad applies the FP_3DMODEL offset in the
    # footprint's own local frame BEFORE the back-side mirror/flip, so a
    # positive offset_z floats a body away from the board on either side —
    # `flipped` must NOT invert the result.
    front = MV.seat_gap(0.0, 1.5, offset_z=0.75, flipped=False)
    back = MV.seat_gap(0.0, 1.5, offset_z=0.75, flipped=True)
    assert front == 0.75 and back == 0.75


def test_seat_gap_none_when_neither_bound_is_near_the_origin():
    # regression for the false-positive sweep this fix's first cut produced
    # on T1/T2 (SM-LP-5001 transformer, rot 270,0,0): real board numbers,
    # neither raw bound near 0 -> this model wasn't authored "seat at
    # origin", so a big offset.z is a deliberate alignment, not a defect.
    assert MV.seat_gap(-3.18, 4.18, offset_z=0.82) is None
    # same for Q1 (Vishay PowerPAK) and C12 (radial cap) — both real numbers
    assert MV.seat_gap(-3.31, 2.63, offset_z=0.79) is None
    assert MV.seat_gap(-2.00, 10.00, offset_z=0.0) is None


# --------------------------------------------------------- _rotate_xyz
def test_rotate_xyz_identity():
    pts = [(1.0, 2.0, 3.0), (-4.0, 5.0, -6.0)]
    out = MV._rotate_xyz(pts, 0, 0, 0)
    for (x0, y0, z0), (x1, y1, z1) in zip(pts, out):
        assert math.isclose(x0, x1, abs_tol=1e-9)
        assert math.isclose(y0, y1, abs_tol=1e-9)
        assert math.isclose(z0, z1, abs_tol=1e-9)


def test_rotate_xyz_matches_z_only_model_to_fp_when_no_pitch_or_roll():
    # with rx=ry=0 the new full-rotation helper must agree with the
    # existing (already-verified) Z-only path on the Z axis — rotating only
    # about Z can never change Z.
    pts = [(3.0, -2.0, 7.0), (0.0, 0.0, -4.0)]
    out = MV._rotate_xyz(pts, 0, 0, 40)
    for (_, _, z0), (_, _, z1) in zip(pts, out):
        assert math.isclose(z0, z1, abs_tol=1e-9)


def test_rotate_xyz_minus90_0_90_matches_real_df40c_geometry():
    # sanity check against the actual vendor STEP this bug was found on, if
    # it's present in this checkout's sibling project; skipped otherwise so
    # this test suite has no hard dependency on another repo's tree.
    step = ("/var/kicad/utv-comms-bridge/hardware/lib/3dmodels/"
           "DF40C-100DS-0.4V_51_.step")
    if not os.path.exists(step):
        import pytest
        pytest.skip("sibling utv-comms-bridge checkout not present")
    pts = MV.step_points(step)
    assert len(pts) > 50
    out = MV._rotate_xyz(pts, -90, 0, 90)
    zs = [z for _, _, z in out]
    zmin, zmax = min(zs), max(zs)
    # stacking height per the footprint's own descr is 1.5mm; the rotated
    # Z-extent must land in that ballpark, not the model's raw ~22.6mm
    # length (what you get if the rotation is skipped/ignored).
    assert 1.0 < (zmax - zmin) < 3.0
    # and the near-zero bound (the seat face) must genuinely be near zero —
    # that's what makes offset.z the whole story for this asset.
    seat = zmin if abs(zmin) <= abs(zmax) else zmax
    assert abs(seat) < 0.2


# --------------------------------------------------- verify_footprint (pcbnew)
import pytest  # noqa: E402

pcbnew = pytest.importorskip("pcbnew")


def _step_fixture(zmin, zmax, n=60):
    """A trivial STEP with >=50 CARTESIAN_POINTs spanning [zmin, zmax] in Z
    (x/y irrelevant to the seat check) — enough for step_points() to pass
    verify_footprint's `len(pts) < 50` "can't judge" gate."""
    lines = ["ISO-10303-21;", "HEADER; FILE_DESCRIPTION(('x'),'2;1'); ENDSEC;",
            "DATA;", "#1=SI_UNIT(.MILLI.,.METRE.);"]
    for i in range(n):
        z = zmin + (zmax - zmin) * i / (n - 1)
        lines.append(f"#{10+i}=CARTESIAN_POINT('',({i * 0.01},0.,{z}));")
    lines.append("ENDSEC; END-ISO-10303-21;")
    return ("\n".join(lines) + "\n").encode()


def _smd_footprint(board, ref, layer):
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetLayer(layer)
    p = pcbnew.PAD(fp)
    p.SetNumber("1")
    p.SetShape(pcbnew.PAD_SHAPE_RECT)
    p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    p.SetSize(pcbnew.VECTOR2I(int(0.6e6), int(0.6e6)))
    fp.Add(p)
    board.Add(fp)
    return fp


def _attach_model(fp, path, offset_z):
    m = pcbnew.FP_3DMODEL()
    m.m_Filename = str(path)
    m.m_Offset.x, m.m_Offset.y, m.m_Offset.z = 0.0, 0.0, offset_z
    m.m_Rotation.x, m.m_Rotation.y, m.m_Rotation.z = 0.0, 0.0, 0.0
    fp.Models().push_back(m)


def _find_seat_finding(findings):
    return [msg for _lvl, msg in findings if "seat gap" in msg]


def test_verify_footprint_flags_back_side_float(tmp_path):
    step = tmp_path / "body.step"
    step.write_bytes(_step_fixture(0.0, 1.5))
    board = pcbnew.BOARD()
    fp = _smd_footprint(board, "J10", pcbnew.B_Cu)
    assert fp.IsFlipped()
    _attach_model(fp, step, offset_z=0.752238806)   # the broken case

    findings = MV.verify_footprint(fp, resolve=lambda p: p, tol=0.6)
    seat = _find_seat_finding(findings)
    assert seat, f"expected a seat-gap finding, got {findings}"
    # render-verified: a positive offset.z floats the body ABOVE the mount
    # plane on either side of the board — not "buried".
    assert "floating above the mount plane" in seat[0]
    assert "+0.80mm" in seat[0] or "+0.75mm" in seat[0]


def test_verify_footprint_clean_when_offset_corrected(tmp_path):
    step = tmp_path / "body.step"
    step.write_bytes(_step_fixture(0.0, 1.5))
    board = pcbnew.BOARD()
    fp = _smd_footprint(board, "J10", pcbnew.B_Cu)
    _attach_model(fp, step, offset_z=0.0)            # the corrected case

    findings = MV.verify_footprint(fp, resolve=lambda p: p, tol=0.6)
    assert not _find_seat_finding(findings), findings


def test_verify_footprint_front_side_unchanged_behaviour(tmp_path):
    # top-side float detection must work the same way (flipped=False path);
    # this is the "keep top-side behaviour unchanged" regression guard.
    step = tmp_path / "body.step"
    step.write_bytes(_step_fixture(0.0, 1.5))
    board = pcbnew.BOARD()
    fp = _smd_footprint(board, "U1", pcbnew.F_Cu)
    assert not fp.IsFlipped()
    _attach_model(fp, step, offset_z=0.75)

    findings = MV.verify_footprint(fp, resolve=lambda p: p, tol=0.6)
    seat = _find_seat_finding(findings)
    assert seat and "floating above the mount plane" in seat[0]


# ------------------------------------------- J5 (2026-09-06) regressions
#
# Root cause: `_th_holes`'s board->footprint-local un-rotation used the
# SAME rotation matrix KiCad's own placement uses (local->board), not its
# inverse — two forward applications compose to Rot(-2*theta), invisible
# at theta = 0/180 and an exact 180-degree point negation at theta =
# +/-90. J5 sits at fp orientation 90: the corrupted "holes" list was the
# true holes rotated 180 from reality, so `solve_transform` correctly (by
# its own now-wrong yardstick) "solved" a 180-degree rotation for the
# model — a perfect fit to a 180-degree-wrong target. Fixed in
# `_board_to_fp`. The SMD-lead check and solver tie-break below are the
# second line of defense: even with holes correctly computed, J5's 2 TH
# pegs + 4 TH shell holes alone fit BETTER at 180 than at the true 0 (a
# property of this specific connector's hole layout, independent of the
# sign bug) — only the 16 real SMD signal leads tell the two rotations
# apart, and the old solver never looked at them.

def _replicate(points, jitter=0.01):
    """Each (x,y,z) repeated with tiny jitter — >=3 copies per position so
    `_clusters`'s default min_pts=3 finds it, and >=50 total so
    step_points()/verify_footprint's "can't judge" gate clears, without
    moving the cluster center."""
    out = []
    reps = max(1, 60 // max(1, len(points)) + 1)
    for x, y, z in points:
        for i in range(reps):
            dx = jitter * (i % 3 - 1)
            dy = jitter * ((i // 3) % 3 - 1)
            out.append((x + dx, y + dy, z))
    return out


def _pts_step(points, jitter=0.01):
    """A trivial STEP with >=50 CARTESIAN_POINTs at the given (x,y,z) mm
    positions, each replicated with tiny jitter so step_points() clears
    verify_footprint's `len(pts) < 50` "can't judge" gate without moving
    the cluster center."""
    lines = ["ISO-10303-21;", "HEADER; FILE_DESCRIPTION(('x'),'2;1'); ENDSEC;",
            "DATA;", "#1=SI_UNIT(.MILLI.,.METRE.);"]
    for n, (x, y, z) in enumerate(_replicate(points, jitter)):
        lines.append(f"#{10+n}=CARTESIAN_POINT('',({x},{y},{z}));")
    lines.append("ENDSEC; END-ISO-10303-21;")
    return ("\n".join(lines) + "\n").encode()


# J5's real geometry (measured off the actual board, 2026-09-06): 2 TH
# alignment pegs + 4 TH shell/mounting holes, footprint-local (rot 0).
_J5_TH_HOLES = [(-2.89, -2.605), (2.89, -2.605),
                (-4.32, -3.105), (4.32, -3.105),
                (-4.32, 1.075), (4.32, 1.075)]
# a representative row of SMD signal leads along the front edge
_J5_SMD_PADS = [(x, -3.68) for x in (-3.2, -2.4, -1.25, -0.25, 0.25, 1.25, 2.4, 3.2)]


def _j5_like_footprint(board, orientation=90.0, anchor_mm=(50.0, 50.0)):
    """A J5-like footprint: TH pegs/shell holes + a row of SMD leads,
    placed at a NON-zero orientation — the exact condition that exposed
    the `_th_holes` sign bug (invisible at 0/180)."""
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("J5")
    ax, ay = (int(anchor_mm[0] * 1e6), int(anchor_mm[1] * 1e6))
    fp.SetPosition(pcbnew.VECTOR2I(ax, ay))
    fp.SetOrientationDegrees(orientation)
    theta = math.radians(orientation)
    c, s = math.cos(theta), math.sin(theta)

    def board_pos(lx, ly):
        bx = lx * c + ly * s
        by = -lx * s + ly * c
        return pcbnew.VECTOR2I(ax + int(bx * 1e6), ay + int(by * 1e6))

    for i, (lx, ly) in enumerate(_J5_TH_HOLES):
        p = pcbnew.PAD(fp)
        p.SetNumber(f"TH{i}")
        p.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        p.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        p.SetSize(pcbnew.VECTOR2I(int(1.0e6), int(1.0e6)))
        p.SetDrillSize(pcbnew.VECTOR2I(int(0.6e6), int(0.6e6)))
        p.SetPosition(board_pos(lx, ly))
        fp.Add(p)

    for i, (lx, ly) in enumerate(_J5_SMD_PADS):
        p = pcbnew.PAD(fp)
        p.SetNumber(f"A{i}")
        p.SetShape(pcbnew.PAD_SHAPE_RECT)
        p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        p.SetSize(pcbnew.VECTOR2I(int(0.4e6), int(0.6e6)))
        lset = pcbnew.LSET()
        lset.AddLayer(pcbnew.F_Cu)
        p.SetLayerSet(lset)
        p.SetPosition(board_pos(lx, ly))
        fp.Add(p)

    board.Add(fp)
    return fp


def _j5_like_model_points(rot180_leads=False, th_at_true_holes=False):
    """Model geometry (its OWN local frame, before any solved rotation).

    Pin shafts (z<-0.25) are authored at the true holes NEGATED — i.e.
    exactly where the true holes land after a 180 rotation. That means a
    solver trying rotations will find the pins fit the true holes BEST at
    z-rotation 180 (mirrors the real, measured J5 STEP: its TH fit is
    strictly better at 180 than at the correct 0). Set
    `th_at_true_holes=True` to instead author them at the true (rot-0)
    positions, for the "already seated, do nothing" test.

    Leads (z in [-0.05, 0.05]) are authored at the true SMD pad positions
    (clean at rot 0); `rot180_leads=True` authors them pre-negated instead
    (so they land on the pads only after a 180 rotation) — the
    "mis-rotated body" case verify_footprint's SMD check must catch.

    `_model_to_fp` (the module's own model-frame -> footprint-frame map)
    is, at z-rotation 0, (x, y) -> (x, -y) — a Y-only flip, from the
    y-up-model/y-down-board convention — not the identity. So "author a
    point that lands on true position (fx, fy) at rotation R" needs that
    map's actual inverse at R, not a naive negation:
      R=0:   input (fx, -fy)  ->  output (fx, fy)
      R=180: input (-fx, fy)  ->  output (fx, fy)
    (`_model_to_fp`'s 2x2 transform is involutory — its own inverse — at
    any z-rotation, verified against these two cases directly.)
    """
    pts = []
    for lx, ly in _J5_TH_HOLES:
        x, y = (lx, -ly) if th_at_true_holes else (-lx, ly)
        pts.append((x, y, -1.0))
    for lx, ly in _J5_SMD_PADS:
        x, y = (-lx, ly) if rot180_leads else (lx, -ly)
        pts.append((x, y, 0.0))
    return pts


def test_th_holes_and_smd_pads_match_true_local_at_90deg():
    # regression for the _th_holes/_smd_pads sign bug: at a 90-degree
    # footprint orientation the old un-rotation returned every position
    # negated (180 degrees off) from the true footprint-local frame.
    board = pcbnew.BOARD()
    fp = _j5_like_footprint(board, orientation=90.0)
    holes = MV._th_holes(fp)
    pads = MV._smd_pads(fp)
    for got, want in zip(sorted(holes), sorted(_J5_TH_HOLES)):
        assert math.isclose(got[0], want[0], abs_tol=0.01)
        assert math.isclose(got[1], want[1], abs_tol=0.01)
    for got, want in zip(sorted(pads), sorted(_J5_SMD_PADS)):
        assert math.isclose(got[0], want[0], abs_tol=0.01)
        assert math.isclose(got[1], want[1], abs_tol=0.01)


def test_verify_footprint_smd_leads_clean_when_seated(tmp_path):
    step = tmp_path / "j5.step"
    step.write_bytes(_pts_step(_j5_like_model_points(rot180_leads=False,
                                                      th_at_true_holes=True)))
    board = pcbnew.BOARD()
    fp = _j5_like_footprint(board)
    _attach_model(fp, step, offset_z=0.0)

    findings = MV.verify_footprint(fp, resolve=lambda p: p, tol=0.6)
    smd = [m for _lvl, m in findings if "SMD leads" in m]
    assert not smd, findings


def test_verify_footprint_smd_leads_warn_when_body_rotated_180(tmp_path):
    step = tmp_path / "j5.step"
    step.write_bytes(_pts_step(_j5_like_model_points(rot180_leads=True,
                                                      th_at_true_holes=True)))
    board = pcbnew.BOARD()
    fp = _j5_like_footprint(board)
    _attach_model(fp, step, offset_z=0.0)

    findings = MV.verify_footprint(fp, resolve=lambda p: p, tol=0.6)
    smd = [m for _lvl, m in findings if "SMD leads" in m]
    assert smd, findings
    assert "rotated/mirrored" in smd[0]


def test_solve_transform_identity_wins_over_th_only_180_fit():
    # the literal J5 defect: TH pegs+holes alone fit BETTER at 180 than at
    # the true 0 (pins authored pre-negated); the solver must still pick
    # identity because the SMD leads (authored correctly, at true rot-0
    # positions) would be wrecked by a 180 rotation.
    pts = _replicate(_j5_like_model_points(rot180_leads=False,
                                           th_at_true_holes=False))
    holes = _J5_TH_HOLES
    smd_pads = _J5_SMD_PADS

    # sanity: confirm the TH-only fit really is ambiguous/better at 180,
    # so this test is exercising the veto and not a non-existent scenario
    loc0 = MV._model_to_fp(pts, (0.0, 0.0, 0.0), 0)
    tips0 = MV._clusters([(x, y) for x, y, z in loc0 if z < -0.25], min_pts=1)
    _, d0 = MV._fit(tips0, holes)
    loc180 = MV._model_to_fp(pts, (0.0, 0.0, 0.0), 180)
    tips180 = MV._clusters([(x, y) for x, y, z in loc180 if z < -0.25], min_pts=1)
    _, d180 = MV._fit(tips180, holes)
    assert d180 < d0 - 0.05, "fixture no longer reproduces the ambiguous-TH case"

    sol, max_d = MV.solve_transform(pts, holes, smd_pads=smd_pads, tol=0.6)
    assert sol is not None
    rot = sol[0]
    assert rot == 0, f"solver picked rotation {rot}, expected identity (0)"


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except TypeError:
                continue   # needs a pytest fixture (tmp_path) — run under pytest
            print(name, "OK")
