"""gerbercheck format handling: constructs it cannot read are a GERBER_UNSUPPORTED FAIL
naming the construct, never a silent mis-scale. Inch Excellon is implemented."""
import os

from fluxplace import gerbercheck as GC

HDR = "%MOMM*%\n%LPD*%\n%ADD10C,0.200000*%\nD10*\n"


def _gbr(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body)
    return str(p)


def _unsupported(res):
    return [f for f in res["findings"] if f["code"] == "GERBER_UNSUPPORTED"]


def test_trailing_zero_omission_fs_is_rejected(tmp_path):
    p = _gbr(tmp_path, "t.gtl", "%FSTAX46Y46*%\n" + HDR + "X2000000Y0000000D02*\nM02*\n")
    got = GC.parse_gerber_or_empty(p)
    assert isinstance(got, dict) and "trailing-zero" in got["unsupported"], got


def test_leading_zero_fs_still_parses(tmp_path):
    p = _gbr(tmp_path, "l.gtl", "%FSLAX46Y46*%\n" + HDR + "X2000000Y0000000D02*\nX20000000Y0000000D01*\nM02*\n")
    objs = GC.parse_gerber_or_empty(p)
    assert isinstance(objs, list) and objs[0][3] == [(2.0, 0.0), (20.0, 0.0)], objs


def test_step_repeat_block_is_rejected(tmp_path):
    p = _gbr(tmp_path, "sr.gtl", "%FSLAX46Y46*%\n%SRX2Y1I5.0J0.0*%\n" + HDR
             + "X2000000Y0000000D03*\n%SR*%\nM02*\n")
    got = GC.parse_gerber_or_empty(p)
    assert isinstance(got, dict) and "step-repeat" in got["unsupported"], got


def test_check_package_reports_unsupported_as_fail(tmp_path):
    g = tmp_path / "gerbers"
    g.mkdir()
    (g / "board-F_Cu.gtl").write_text("%FSTAX46Y46*%\nM02*\n")
    res = GC.check_package(str(tmp_path), log=lambda m: None)
    fails = _unsupported(res)
    assert fails and fails[0]["level"] == GC.FAIL and "trailing-zero" in fails[0]["msg"], res
    assert "GERBER_LAYER_MISSING" not in [f["code"] for f in res["findings"] if f["layer"] == "F_Cu"]


def test_excellon_without_decimal_point_is_rejected(tmp_path):
    d = tmp_path / "x.drl"
    d.write_text("M48\nMETRIC,TZ\nT1C0.300\n%\nT1\nX12345Y6789\nM30\n")
    try:
        GC.parse_excellon(str(d))
        raise AssertionError("integer coordinates must be rejected")
    except GC.GerberUnsupported as e:
        assert "decimal point" in str(e)


def test_excellon_inch_units_are_scaled(tmp_path):
    d = tmp_path / "in.drl"
    d.write_text("M48\nINCH,TZ\nT1C0.0400\n%\nT1\nX1.0Y0.5\nM30\n")
    (x, y, dia), = GC.parse_excellon(str(d))
    assert abs(x - 25.4) < 1e-9 and abs(y - 12.7) < 1e-9 and abs(dia - 1.016) < 1e-9


def test_excellon_unsupported_is_fail_in_check_package(tmp_path):
    dd = tmp_path / "drill"
    dd.mkdir()
    (dd / "x.drl").write_text("M48\nMETRIC,TZ\nT1C0.300\n%\nT1\nX12345Y6789\nM30\n")
    res = GC.check_package(str(tmp_path), log=lambda m: None)
    fails = _unsupported(res)
    assert fails and fails[0]["level"] == GC.FAIL and "decimal point" in fails[0]["msg"], res
