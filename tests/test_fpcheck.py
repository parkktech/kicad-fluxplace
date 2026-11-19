"""Footprint verification (fluxplace/fpcheck.py): pcbnew geometry, pad tables, allow, waivers, the parts gate.

Footprints are built in code (no project library, no network). Each test is one defect class.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbnew  # noqa: E402

from fluxplace import fpcheck as FC  # noqa: E402
from fluxplace import partsgate as PG  # noqa: E402
from fluxplace import review as R  # noqa: E402

MM = pcbnew.FromMM


def _pad(fp, num, x, y, w, h, shape=pcbnew.PAD_SHAPE_RECT, attr=pcbnew.PAD_ATTRIB_SMD, drill=0.0,
         layers=None, angle=0.0, radius_ratio=None):
    p = pcbnew.PAD(fp)
    p.SetNumber(num)
    p.SetAttribute(attr)
    p.SetShape(shape)
    p.SetSize(pcbnew.VECTOR2I(MM(w), MM(h)))
    p.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
    p.SetFPRelativePosition(pcbnew.VECTOR2I(MM(x), MM(y)))
    if angle:
        p.SetOrientationDegrees(angle)
    if radius_ratio is not None:
        p.SetRoundRectRadiusRatio(radius_ratio)
    if drill:
        p.SetDrillSize(pcbnew.VECTOR2I(MM(drill), MM(drill)))
    p.SetLayerSet(layers or (pcbnew.LSET.AllCuMask() if attr != pcbnew.PAD_ATTRIB_SMD else pcbnew.LSET.FrontMask()))
    fp.Add(p)
    return p


_BOARD = pcbnew.BOARD()                    # one board owns every test footprint (a footprint must not outlive its board)


def _fp(board=None):
    b = board or _BOARD
    fp = pcbnew.FOOTPRINT(b)
    b.Add(fp)
    return fp


def _check(fp, clr=0.127, hole=0.25):
    return FC.check_footprint(pcbnew, fp, "U1", clr, hole)


def _codes(fs, level=None):
    return [f["code"] for f in fs if level is None or f["level"] == level]


def test_pads_005_apart_fail_clearance_and_report_numbers():
    fp = _fp()
    _pad(fp, "1", 0.0, 0.0, 0.5, 0.5)
    _pad(fp, "2", 0.55, 0.0, 0.5, 0.5)            # gap 0.05 mm
    out = _check(fp)
    f = [x for x in out if x["code"] == "FP_PAD_CLEARANCE" and x["level"] == "FAIL"]
    assert len(f) == 1
    assert sorted(f[0]["pads"]) == ["1", "2"] and abs(f[0]["actual"] - 0.05) < 0.005 and f[0]["floor"] == 0.127
    assert "0.050 mm" in f[0]["msg"] or "0.05" in f[0]["msg"]


def test_pads_02_apart_pass_clearance():
    fp = _fp()
    _pad(fp, "1", 0.0, 0.0, 0.5, 0.5)
    _pad(fp, "2", 0.7, 0.0, 0.5, 0.5)             # gap 0.2 mm
    assert "FP_PAD_CLEARANCE" not in _codes(_check(fp), "FAIL")


def test_same_pad_number_merge_is_exempt_and_says_so():
    fp = _fp()
    _pad(fp, "5", 0.0, 0.0, 0.5, 0.5)
    _pad(fp, "5", 0.5, 0.0, 0.5, 0.5)             # touching, same number: thermal segments
    out = _check(fp)
    assert "FP_PAD_CLEARANCE" not in _codes(out, "FAIL")
    assert any("same pad number" in x["msg"] and x["level"] == "INFO" for x in out)


def test_smd_pad_touching_npth_fails_hole_clearance():
    fp = _fp()
    _pad(fp, "NPTH", 0.0, 0.0, 3.0, 3.0, shape=pcbnew.PAD_SHAPE_CIRCLE, attr=pcbnew.PAD_ATTRIB_NPTH, drill=3.0,
         layers=pcbnew.LSET.AllCuMask())
    _pad(fp, "1", 1.82, 0.0, 0.64, 0.64)          # copper edge at 1.50 mm = the NPTH wall
    out = _check(fp)
    f = [x for x in out if x["code"] == "FP_PAD_HOLE_CLEARANCE" and x["level"] == "FAIL"]
    assert f and f[0]["actual"] < 0.01 and f[0]["floor"] == 0.25


def test_keepout_rule_area_containing_own_npth_fails():
    fp = _fp()
    _pad(fp, "NPTH", 0.0, 0.0, 1.0, 1.0, shape=pcbnew.PAD_SHAPE_CIRCLE, attr=pcbnew.PAD_ATTRIB_NPTH, drill=1.0,
         layers=pcbnew.LSET.AllCuMask())
    z = pcbnew.ZONE(fp)
    z.SetIsRuleArea(True)
    z.SetDoNotAllowPads(True)
    z.SetLayerSet(pcbnew.LSET.FrontMask())
    ps = pcbnew.SHAPE_POLY_SET()
    ps.NewOutline()
    for x, y in ((-3, -3), (3, -3), (3, 3), (-3, 3)):
        ps.Append(MM(x), MM(y))
    z.SetOutline(ps)
    fp.Add(z)
    assert "FP_KEEPOUT_SELF" in _codes(_check(fp), "FAIL")


def test_rotated_rounded_rect_pair_is_measured_not_bounding_boxed():
    # two 1.0 x 0.4 rounded rects at 45 deg, 0.6 mm apart across their short axis: edge gap 0.2 mm,
    # bounding boxes overlap. A rectangle or bbox shortcut would call this a collision.
    fp = _fp()
    d = 0.6 / 2 ** 0.5                       # KiCad 45 deg: the long axis runs along (1,-1), the short axis along (1,1)
    _pad(fp, "1", 0.0, 0.0, 1.0, 0.4, shape=pcbnew.PAD_SHAPE_ROUNDRECT, angle=45, radius_ratio=0.25)
    _pad(fp, "2", d, d, 1.0, 0.4, shape=pcbnew.PAD_SHAPE_ROUNDRECT, angle=45, radius_ratio=0.25)
    out = _check(fp)
    assert "FP_PAD_CLEARANCE" not in _codes(out, "FAIL")
    pair = [x for x in out if x["code"] == "FP_PAD_CLEARANCE"]
    assert not pair                                  # 0.2 mm is above the 0.127 floor
    sa = fp.Pads()[0].GetEffectiveShape(pcbnew.F_Cu)
    sb = fp.Pads()[1].GetEffectiveShape(pcbnew.F_Cu)
    assert abs(FC._gap(pcbnew, sa, sb, 1.0) - 0.2) < 0.01


def test_gap_helper_is_exact_for_touching_and_apart():
    fp = _fp()
    a = _pad(fp, "1", 0.0, 0.0, 0.5, 0.5)
    b = _pad(fp, "2", 0.5, 0.0, 0.5, 0.5)
    assert FC._gap(pcbnew, a.GetEffectiveShape(pcbnew.F_Cu), b.GetEffectiveShape(pcbnew.F_Cu), 0.5) < 0.001
    c = _pad(fp, "3", 1.0, 0.0, 0.5, 0.5)
    assert abs(FC._gap(pcbnew, a.GetEffectiveShape(pcbnew.F_Cu), c.GetEffectiveShape(pcbnew.F_Cu), 1.0) - 0.5) < 0.001


def test_pad_outside_courtyard_warns():
    fp = _fp()
    _pad(fp, "1", 0.0, 0.0, 0.5, 0.5)
    _pad(fp, "2", 3.0, 0.0, 0.5, 0.5)
    s = pcbnew.PCB_SHAPE(fp)                  # courtyard = closed graphics on F.CrtYd (GetCourtyard returns a copy)
    s.SetShape(pcbnew.SHAPE_T_RECT)
    s.SetLayer(pcbnew.F_CrtYd)
    s.SetStart(pcbnew.VECTOR2I(MM(-1), MM(-1)))
    s.SetEnd(pcbnew.VECTOR2I(MM(1), MM(1)))
    s.SetFilled(False)
    fp.Add(s)
    out = _check(fp)
    assert any(x["code"] == "FP_PAD_OUTSIDE_COURTYARD" and x["level"] == "WARN" and "2" in x["pads"] for x in out)


# ---- pad table (LANDPATTERN_PAD_*) ----
def _grid():
    """8 pads: 4 at x=0, 4 at x=2, pitch 1.0 along y; pad 8 is the corner at (2, 3)."""
    pts = [(str(i), 0.0, float(i - 1), 0.5, 0.5) for i in range(1, 5)]
    pts += [(str(i + 5), 2.0, float(i), 0.5, 0.5) for i in range(4)]
    return pts


def _cited(pts):
    return [{"num": n, "x": x, "y": y, "w": w, "h": h} for n, x, y, w, h in pts]


def test_pad_table_corner_pad_055_off_is_mismatch_summary_still_passes(tmp_path):
    drawn = _grid()
    drawn = [(n, x, (y + 0.55 if n == "8" else y), w, h) for n, x, y, w, h in drawn]   # pad 8 0.55 mm off along the column
    cited = _cited(_grid())
    out = FC.table_check("U1", drawn, cited, "footprint", 0.03)
    mm = [x for x in out if x["code"] == "LANDPATTERN_PAD_MISMATCH"]
    assert len(mm) == 1 and mm[0]["pads"] == ["8"] and abs(mm[0]["actual"] - 0.55) < 0.001
    assert "delta dx +0.000 dy +0.550" in mm[0]["msg"]
    # the summary check (pitch / rows / pad / pins) does not see it: this is the real 2026-11-19 case
    facts = {"parts": {"U1": {"footprint": "lib:Q", "pad_geom": drawn, "models": ["x.step"], "mech": False}}}
    spec = {"components": [{"ref": "U1", "landpattern": {"source": "x.pdf#p1", "pitch": 1.0, "rows": 2.0,
                                                         "pad": [0.5, 0.5], "pins": 8,
                                                         "pads": cited}}]}
    ds = str(tmp_path)
    open(os.path.join(ds, "x.pdf"), "wb").write(b"%PDF-1.4 stub")
    found = R.check_landpattern(facts, spec, ds, strict=True)
    codes = [x["code"] for x in found]
    assert "LANDPATTERN_MISMATCH" not in codes
    assert "LANDPATTERN_PAD_MISMATCH" in codes


def test_pad_table_missing_and_extra():
    cited = _cited(_grid())
    drawn = [p for p in _grid() if p[0] != "3"] + [("9", 5.0, 5.0, 0.5, 0.5)]
    out = FC.table_check("U1", drawn, cited, "footprint", 0.03)
    assert any(x["code"] == "LANDPATTERN_PAD_MISSING" and x["pads"] == ["3"] for x in out)
    assert any(x["code"] == "LANDPATTERN_PAD_EXTRA" and "9" in x["msg"] for x in out)


def test_pad1_origin_is_relative_to_pad_one():
    drawn = [(n, x + 10.0, y - 4.0, w, h) for n, x, y, w, h in _grid()]
    out = FC.table_check("U1", drawn, _cited(_grid()), "pad1", 0.03)
    assert out == []


def test_vendor_footprint_compared_pad_by_pad(tmp_path):
    kmod = tmp_path / "vendor.kicad_mod"
    kmod.write_text(
        '(footprint "V" (layer "F.Cu")\n'
        '  (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu"))\n'
        '  (pad "2" smd rect (at 0.7 0) (size 0.5 0.5) (layers "F.Cu"))\n)\n')
    pts = [("1", 0.0, 0.0, 0.5, 0.5), ("2", 0.8, 0.0, 0.5, 0.5)]    # pad 2 is 0.1 off the vendor
    spec = {"components": [{"ref": "U1", "landpattern": {"vendor_footprint": "vendor.kicad_mod"}}]}
    facts = {"parts": {"U1": {"footprint": "lib:V", "pad_geom": pts}}}
    out = FC.table_findings(facts, spec, str(tmp_path))
    assert [x["code"] for x in out] == ["LANDPATTERN_PAD_MISMATCH"] and out[0]["pads"] == ["2"]


# ---- waivers, allow ----
def test_waiver_of_footprint_code_is_refused():
    fnd = {"level": "FAIL", "code": "FP_PAD_CLEARANCE", "msg": "U1 pads 7 and 8", "refs": ["U1"]}
    assert R._waived(fnd, ["FP_PAD_CLEARANCE:.*"]) is False
    assert R._waived(fnd, ["FP_PAD_CLEARANCE"]) is False
    refused = FC.refused_waivers(["FP_PAD_CLEARANCE:^U1 ", "MODEL_STANDIN:^ANT1"])
    assert len(refused) == 1 and "footprint defects are fixed in the footprint" in refused[0]["msg"]
    # a non-footprint code still waives as before
    assert R._waived({"level": "WARN", "code": "MODEL_STANDIN", "msg": "ANT1 x", "refs": ["ANT1"]},
                     ["MODEL_STANDIN:^ANT1 "]) is True


def _allow_spec(source, value, pads=("7", "8"), code="FP_PAD_CLEARANCE"):
    return {"components": [{"ref": "U1", "landpattern": {"allow": [
        {"code": code, "pads": list(pads), "source": source, "value_mm": value}]}}]}


def _clearance_finding(actual=0.10):
    return {"level": "FAIL", "code": "FP_PAD_CLEARANCE", "msg": "U1 pads 7 and 8 on B.Cu", "refs": ["U1"],
            "pads": ["7", "8"], "actual": actual, "floor": 0.127}


def test_landpattern_allow_with_existing_source_is_accepted(tmp_path):
    open(tmp_path / "bt850.pdf", "wb").write(b"%PDF stub")
    out = FC.apply_allow([_clearance_finding(0.10)], _allow_spec("bt850.pdf#p23", 0.10), str(tmp_path))
    assert out[0]["level"] == "INFO" and "allowed" in out[0]["msg"]


def test_landpattern_allow_with_missing_source_is_refused(tmp_path):
    out = FC.apply_allow([_clearance_finding(0.10)], _allow_spec("missing.pdf#p23", 0.10), str(tmp_path))
    assert out[0]["level"] == "FAIL" and "refused" in out[0]["msg"]


def test_landpattern_allow_value_mismatch_is_refused(tmp_path):
    open(tmp_path / "bt850.pdf", "wb").write(b"%PDF stub")
    out = FC.apply_allow([_clearance_finding(0.10)], _allow_spec("bt850.pdf#p23", 0.20), str(tmp_path))
    assert out[0]["level"] == "FAIL" and "refused" in out[0]["msg"]


# ---- the parts gate ----
def _board_with_close_pads(path):
    b = pcbnew.BOARD()
    fp = _fp(b)                                   # added to b by _fp
    fp.SetReference("U1")
    _pad(fp, "1", 0.0, 0.0, 0.5, 0.5)
    _pad(fp, "2", 0.55, 0.0, 0.5, 0.5)            # 0.05 mm
    b.Save(path)
    return path


def test_parts_gate_blocks_fp_failure_and_allow_undocumented_cannot_pass_it(tmp_path):
    bp = _board_with_close_pads(str(tmp_path / "t.kicad_pcb"))
    facts = {"board_path": bp, "parts": {"U1": {"footprint": "lib:Q", "pad_geom": [], "models": [], "mech": False}}}
    res = {"findings": [], "rows": {}, "lookup": True, "stock_data": {}}
    g = PG.gate({"components": []}, {}, facts, res, qty=None, ds_dir=str(tmp_path))
    assert not g["ok"]
    assert any(x["code"] == "FP_PAD_CLEARANCE" for x in g["geometry"])
    assert any(it["code"] == "FP_PAD_CLEARANCE" and it["need"] == "package" for it in g["parts"]["U1"])
    assert FC.is_geometry("FP_PAD_CLEARANCE") and FC.is_geometry("LANDPATTERN_PAD_MISMATCH")
    assert not FC.is_geometry("LANDPATTERN_MISMATCH") and not FC.is_geometry("MODEL_STANDIN")
    assert "--allow-undocumented" in PG.gate.__doc__


def test_unloadable_board_is_not_checked_not_passed(tmp_path):
    bad = tmp_path / "fake.kicad_pcb"
    bad.write_text("(kicad_pcb (not a real board")
    out = FC.check_board(str(bad))
    assert [x["code"] for x in out] == ["FP_UNCHECKED"] and out[0]["level"] == "WARN"
