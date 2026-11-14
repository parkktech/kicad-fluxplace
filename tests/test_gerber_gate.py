"""[fab] gerber_check gate: fail / warn / off, GERBER_CHECK_ERROR. No kicad-cli needed:
an empty fab dir is a FAIL package (every expected layer missing)."""
import pytest

from fluxplace import gerbercheck as GC


def _lines():
    out = []
    return out, out.append


def test_default_mode_is_warn_and_invalid_mode_is_rejected():
    assert GC.gerber_mode(None) == "warn"
    assert GC.gerber_mode({"fab": {"gerber_check": "FAIL"}}) == "fail"
    with pytest.raises(ValueError):
        GC.gerber_mode({"fab": {"gerber_check": "strict"}})


def test_fail_mode_blocks_on_fail_finding(tmp_path):
    lines, log = _lines()
    r = GC.run_stage(str(tmp_path), None, cons={"fab": {"gerber_check": "fail"}}, log=log)
    assert r["status"] == "FAIL" and r["blocking"] is True
    assert any("fab blocked" in s for s in lines)
    assert any("GERBER_LAYER_MISSING" in s for s in lines)   # printed, not only in the report


def test_warn_mode_prints_but_does_not_block(tmp_path):
    lines, log = _lines()
    r = GC.run_stage(str(tmp_path), None, cons=None, log=log)
    assert r["mode"] == "warn" and r["status"] == "FAIL" and r["blocking"] is False
    assert any("GERBER CHECK FAIL (gerber_check = warn)" in s for s in lines)


def test_off_mode_skips_the_check(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("check must not run when off")
    monkeypatch.setattr(GC, "check_package", boom)
    r = GC.run_stage(str(tmp_path), None, cons={"fab": {"gerber_check": "off"}}, log=lambda m: None)
    assert r == {"word": "OFF", "blocking": False, "mode": "off", "status": "OFF"}


def test_exception_is_gerber_check_error_and_blocks_only_in_fail_mode(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("raster exploded")
    monkeypatch.setattr(GC, "check_package", boom)
    lines, log = _lines()
    r = GC.run_stage(str(tmp_path), None, cons={"fab": {"gerber_check": "fail"}}, log=log)
    assert r["blocking"] is True and r["word"].startswith("ERROR")
    assert any(s.startswith("GERBER_CHECK_ERROR") for s in lines)
    r = GC.run_stage(str(tmp_path), None, cons={"fab": {"gerber_check": "warn"}}, log=lambda m: None)
    assert r["blocking"] is False
