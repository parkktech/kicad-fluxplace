"""Tests for fluxplace.models — offline parts only (no network, no pcbnew)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import models as M


def test_resolve_vars_kiprjmod(tmp_path):
    f = tmp_path / "x.step"
    f.write_bytes(b"ISO-10303-21;")
    p = M.resolve_vars("${KIPRJMOD}/x.step", str(tmp_path))
    assert os.path.exists(p)


def test_resolve_vars_unset_never_matches(tmp_path):
    p = M.resolve_vars("${NO_SUCH_VAR_XYZ}/x.step", str(tmp_path))
    assert not os.path.exists(p)


def test_fetch_step_cached(tmp_path):
    d = tmp_path / "models"
    d.mkdir()
    (d / "MPN-1.step").write_bytes(b"ISO-10303-21;" + b"x" * 2000)
    path, status = M.fetch_step("MPN-1", d, creds={}, tok=None)
    assert status == "cached" and path.name == "MPN-1.step"


def test_credentials_shape():
    creds = M.credentials()
    assert isinstance(creds, dict)
    for v in creds.values():
        assert isinstance(v, str) and v


if __name__ == "__main__":
    import tempfile
    import pathlib
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_"):
            if fn.__code__.co_argcount:
                with tempfile.TemporaryDirectory() as td:
                    fn(pathlib.Path(td))
            else:
                fn()
            print(name, "OK")


STEP_FIXTURE = b"""ISO-10303-21;
HEADER; FILE_DESCRIPTION(('x'),'2;1'); ENDSEC;
DATA;
#1=SI_UNIT(.MILLI.,.METRE.);
#10=CARTESIAN_POINT('',(0.,0.,0.));
#11=CARTESIAN_POINT('',(40.,10.,3.));
#12=CARTESIAN_POINT('',(20.,5.,1.5));
ENDSEC; END-ISO-10303-21;
"""


def test_step_bbox(tmp_path):
    f = tmp_path / "m.step"
    f.write_bytes(STEP_FIXTURE)
    lo, hi = M.step_bbox(f)
    assert lo == (0.0, 0.0, 0.0) and hi == (40.0, 10.0, 3.0)


def test_plan_alignment_centers_and_floors(tmp_path):
    f = tmp_path / "m.step"
    f.write_bytes(STEP_FIXTURE)
    plan = M.plan_alignment(f, fp_w=42.0, fp_h=12.0)   # landscape/landscape
    assert plan["rotation"] == (0.0, 0.0, 0.0)
    assert plan["offset"] == (-20.0, 5.0, 0.0)         # centered, floored


def test_plan_alignment_rotates_mismatched_aspect(tmp_path):
    f = tmp_path / "m.step"
    f.write_bytes(STEP_FIXTURE)                        # model is landscape
    plan = M.plan_alignment(f, fp_w=12.0, fp_h=42.0)   # footprint portrait
    assert plan["rotation"][2] == 90.0
