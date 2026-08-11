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
