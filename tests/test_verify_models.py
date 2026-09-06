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


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except TypeError:
                continue   # needs a pytest fixture (tmp_path) — run under pytest
            print(name, "OK")
