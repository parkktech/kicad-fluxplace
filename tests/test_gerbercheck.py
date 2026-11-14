"""gerbercheck: re-reads kicad-cli Gerber/drill output and grades it.

A pcbnew-built 2-layer board with one seeded violation per check plus a clean
control, exported with the real kicad-cli (same flags as fab.emit). Skipped when
pcbnew or kicad-cli is missing.
"""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace import gerbercheck as GC                     # noqa: E402

pcbnew = pytest.importorskip("pcbnew")
KICAD_CLI = shutil.which("kicad-cli")
pytestmark = pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not installed")

MM = pcbnew.FromMM


def _track(b, x0, y0, x1, y1, w, layer=pcbnew.F_Cu):
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(pcbnew.VECTOR2I(MM(x0), MM(y0)))
    t.SetEnd(pcbnew.VECTOR2I(MM(x1), MM(y1)))
    t.SetWidth(MM(w))
    t.SetLayer(layer)
    b.Add(t)


def _via(b, x, y, dia, drill):
    v = pcbnew.PCB_VIA(b)
    v.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
    v.SetWidth(MM(dia))
    v.SetDrill(MM(drill))
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    b.Add(v)


def _edge(b, pts, closed=True):
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + (pts[:1] if closed else [])):
        s = pcbnew.PCB_SHAPE(b)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetStart(pcbnew.VECTOR2I(MM(x0), MM(y0)))
        s.SetEnd(pcbnew.VECTOR2I(MM(x1), MM(y1)))
        s.SetWidth(MM(0.1))
        b.Add(s)


def _export(tmp_path, board_obj, name, edge_pts=None):
    """Save a board, export like fab.emit does, return the fab dir."""
    bd = tmp_path / name
    bd.mkdir()
    p = str(bd / "board.kicad_pcb")
    board_obj.Save(p)
    fab = bd / "fab"
    g, d = fab / "gerbers", fab / "drill"
    g.mkdir(parents=True)
    d.mkdir(parents=True)
    subprocess.run([KICAD_CLI, "pcb", "export", "gerbers", "--output", str(g), "--no-x2",
                    "--subtract-soldermask", p], check=True, capture_output=True)
    subprocess.run([KICAD_CLI, "pcb", "export", "drill", "--output", str(d) + os.sep,
                    "--format", "excellon", "--drill-origin", "plot",
                    "--excellon-separate-th", "--generate-map", "--map-format", "gerberx2", p],
                   check=True, capture_output=True)
    return str(fab), p


def _board(outline=True):
    """2-layer board with something on every expected layer, so an empty
    layer is never the reason a check fails. The B.Cu track and the mask and
    silk lines sit well away from the F.Cu test items."""
    b = pcbnew.BOARD()
    b.SetCopperLayerCount(2)
    if outline:
        _edge(b, [(0, 0), (30, 0), (30, 20), (0, 20)])
    _track(b, 2, 16, 20, 16, 0.2, layer=pcbnew.B_Cu)
    for lay in (pcbnew.F_Mask, pcbnew.B_Mask, pcbnew.F_SilkS, pcbnew.B_SilkS):
        s = pcbnew.PCB_SHAPE(b)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetLayer(lay)
        s.SetStart(pcbnew.VECTOR2I(MM(2), MM(18)))
        s.SetEnd(pcbnew.VECTOR2I(MM(20), MM(18)))
        s.SetWidth(MM(0.15))
        b.Add(s)
    return b


CONS = {"fab": {"min_clearance_mm": 0.127, "min_track_mm": 0.127, "min_annular_mm": 0.1}}


def _codes(res, level=None):
    return [f["code"] for f in res["findings"] if level is None or f["level"] == level]


def test_control_board_is_clean(tmp_path):
    b = _board()
    _track(b, 2, 5, 20, 5, 0.2)                 # 0.2 mm track
    _track(b, 2, 8, 20, 8, 0.2)                 # 3 mm apart: well clear
    _via(b, 10, 11, 0.6, 0.3)                   # ring 0.15 >= 0.1
    fab, _ = _export(tmp_path, b, "control")
    res = GC.check_package(fab, cons=CONS, log=lambda m: None)
    assert res["status"] == "PASS", GC.format_report(res)
    assert res["counts"] == {}


def test_seeded_gap_is_found_at_its_location(tmp_path):
    b = _board()
    _track(b, 2, 5, 20, 5, 0.2)
    _track(b, 2, 5.25, 20, 5.25, 0.2)           # 0.05 mm edge-to-edge: different regions
    fab, _ = _export(tmp_path, b, "gap")
    res = GC.check_package(fab, cons=CONS, log=lambda m: None)
    gaps = [f for f in res["findings"] if f["code"] == "GERBER_MIN_GAP"]
    assert len(gaps) == 1, GC.format_report(res)
    assert gaps[0]["level"] == GC.WARN              # advisory by default
    # Gerber coordinates are Y-up: board Y=5.12 is reported at Y=-5.12
    # parallel tracks: any x along the 2..20 run is a closest point; y sits in the gap
    assert 1.5 <= gaps[0]["x"] <= 20.5 and abs(gaps[0]["y"] + 5.125) < 0.1
    assert "0.0" in gaps[0]["msg"]              # measured estimate reported


def test_seeded_narrow_track_is_found(tmp_path):
    b = _board()
    _track(b, 2, 5, 20, 5, 0.08)                # below 0.127 mm floor
    fab, _ = _export(tmp_path, b, "width")
    res = GC.check_package(fab, cons=CONS, log=lambda m: None)
    assert _codes(res, GC.FAIL) == ["GERBER_MIN_WIDTH"], GC.format_report(res)


def test_seeded_annular_ring_is_found(tmp_path):
    b = _board()
    _via(b, 10, 10, 0.35, 0.3)                  # ring 0.025 < 0.1
    fab, _ = _export(tmp_path, b, "annular")
    res = GC.check_package(fab, cons=CONS, log=lambda m: None)
    assert _codes(res, GC.FAIL) == ["GERBER_ANNULAR"], GC.format_report(res)


def test_missing_layer_and_open_edge_are_found(tmp_path):
    b = _board(outline=False)
    _edge(b, [(0, 0), (30, 0), (30, 20)], closed=False)   # open outline
    _track(b, 2, 5, 20, 5, 0.2)
    fab, _ = _export(tmp_path, b, "edge")
    res = GC.check_package(fab, cons=CONS, log=lambda m: None)
    assert "GERBER_EDGE_OPEN" in _codes(res, GC.FAIL), GC.format_report(res)
    assert "GERBER_LAYER_MISSING" not in _codes(res)   # every 2-layer item present
    os.remove(os.path.join(fab, "gerbers", "board-B_Mask.gbs"))
    res = GC.check_package(fab, cons=CONS, log=lambda m: None)
    assert "GERBER_LAYER_MISSING" in _codes(res, GC.FAIL)


def test_emit_stage_returns_status_and_never_raises(tmp_path):
    # no package at all: every layer is missing, so FAIL; the report write
    # cannot land in a directory that does not exist, which must not raise
    assert GC.emit_stage(str(tmp_path / "missing"), None, log=lambda m: None).startswith(("FAIL", "ERROR"))
    assert GC.emit_stage(str(tmp_path / "missing" / "deeper"), None, log=lambda m: None) \
        .startswith("ERROR")


def test_roundrect_macro_size_is_its_box_not_one_corner_circle():
    # KiCad roundrect pad: 1.5 mm box + four 0.5 mm corner circles = 2.0 mm across
    shapes = GC._macro_shapes(
        "1,1,0.5,0.75,0.75*\n20,1,0.5,-0.75,-0.75,0.75,-0.75,0*\n"
        "21,1,1.5,1.5,0,0,0*\n4,1,4,-0.75,-0.75,0.75,-0.75,0.75,0.75,-0.75,0.75,-0.75,-0.75,0*",
        [])
    assert GC._shape_min_dim(shapes) == pytest.approx(2.0, abs=0.02) or \
        GC._shape_min_dim(shapes) > 1.4


def _pad_board(attr):
    """One footprint with a 1.0 mm hole and a 1.02 mm pad (0.01 mm ring)."""
    b = _board()
    fp = pcbnew.FOOTPRINT(b)
    fp.SetPosition(pcbnew.VECTOR2I(MM(10), MM(10)))
    pad = pcbnew.PAD(fp)
    pad.SetAttribute(attr)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(MM(1.02), MM(1.02)))
    pad.SetDrillSize(pcbnew.VECTOR2I(MM(1.0), MM(1.0)))
    pad.SetLayerSet(pad.PTHMask())
    pad.SetPosition(pcbnew.VECTOR2I(MM(10), MM(10)))
    fp.Add(pad)
    b.Add(fp)
    return b


def test_plated_thin_ring_is_annular_finding(tmp_path):
    fab, _ = _export(tmp_path, _pad_board(pcbnew.PAD_ATTRIB_PTH), "plated-pad")
    res = GC.check_package(fab, cons=CONS, log=lambda m: None)
    assert _codes(res, GC.FAIL) == ["GERBER_ANNULAR"], GC.format_report(res)


def test_non_plated_hole_is_not_annular_finding(tmp_path):
    fab, _ = _export(tmp_path, _pad_board(pcbnew.PAD_ATTRIB_NPTH), "npth-pad")
    res = GC.check_package(fab, cons=CONS, log=lambda m: None)
    assert "GERBER_ANNULAR" not in _codes(res), GC.format_report(res)


def test_gap_is_warn_by_default_and_fail_when_opted_in(tmp_path):
    b = _board()
    _track(b, 2, 5, 20, 5, 0.2)
    _track(b, 2, 5.25, 20, 5.25, 0.2)
    fab, _ = _export(tmp_path, b, "gap-level")
    warn = GC.check_package(fab, cons=CONS, log=lambda m: None)
    assert _codes(warn, GC.WARN) == ["GERBER_MIN_GAP"] and "GERBER_MIN_GAP" not in _codes(warn, GC.FAIL)
    strict = dict(CONS, fab=dict(CONS["fab"], gerber_gap="fail"))
    assert "GERBER_MIN_GAP" in _codes(GC.check_package(fab, cons=strict, log=lambda m: None), GC.FAIL)


def test_cons_threads_the_board_clearance_floor(tmp_path):
    # 0.1 mm gap: clean at the 0.1 mm default, a gap at a 0.2 mm board rule
    b = _board()
    _track(b, 2, 5, 20, 5, 0.2)
    _track(b, 2, 5.3, 20, 5.3, 0.2)            # 0.1 mm edge to edge
    fab, _ = _export(tmp_path, b, "floor")
    assert "GERBER_MIN_GAP" not in _codes(GC.check_package(fab, log=lambda m: None))
    strict = {"fab": {"min_clearance_mm": 0.2}}
    assert "GERBER_MIN_GAP" in _codes(GC.check_package(fab, cons=strict, log=lambda m: None))


def _raster_extent_mm(ap, at, bbox=(0.0, 0.0, 10.0, 10.0), res=0.01):
    """Flash `ap` at `at` on a Y-up canvas; return the lit extent (x0,y0,x1,y1) in mm."""
    import numpy as np
    cv = GC._Canvas(bbox, res)
    cv.paint([("flash", "D", ap, at)])
    rows, cols = np.nonzero(cv.a)
    assert rows.size, "flash rasterised nothing"
    x0 = bbox[0] + cols.min() * res
    x1 = bbox[0] + (cols.max() + 1) * res
    y0 = bbox[1] + rows.min() * res
    y1 = bbox[1] + (rows.max() + 1) * res
    return x0, y0, x1, y1


def _macro_ap(body, params):
    sh = GC._macro_shapes(body, params)
    return ("shapes", [("circle", s[1], s[2], s[3]) if s[0] == "circle" else ("poly", s[1])
                       for s in sh], GC._shape_min_dim(sh))


def _analytic_box(ox, oy, cx, cy, w, h, deg=0.0):
    """Box w x h centred (cx, cy) in aperture coords, turned by deg about the origin, flashed at (ox, oy)."""
    pts = [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]
    pts = [(x + ox, y + oy) for x, y in GC._rot(pts, deg)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _close_box(got, want, tol=0.03):
    return all(abs(g - w) <= tol for g, w in zip(got, want))


def test_offset_rect_macro_is_y_up_not_mirrored():
    # AMOFF: 1.0 x 0.5 rectangle centred 0.25 mm above the flash point.
    ap = _macro_ap("21,1,1.0,0.5,0,0.25", [])
    got = _raster_extent_mm(ap, (5.0, 5.0))
    assert _close_box(got, (4.5, 5.0, 5.5, 5.5)), got   # y 5.00-5.50, not 4.50-5.00


def test_rotated_rect_macro_matches_analytic_box():
    ap = _macro_ap("21,1,2.0,0.4,0.5,0.3,30", [])
    got = _raster_extent_mm(ap, (5.0, 5.0))
    # Gerber rotates the whole primitive about the flash origin: centre (0.5,0.3) turns too.
    want = _analytic_box(5.0, 5.0, 0.5, 0.3, 2.0, 0.4, 30)
    assert _close_box(got, want, 0.03), (got, want)


def test_off_centre_circle_primitive_is_y_up():
    ap = _macro_ap("1,1,0.6,0.0,0.4", [])     # circle d=0.6 at (0.0, +0.4)
    got = _raster_extent_mm(ap, (5.0, 5.0))
    assert _close_box(got, (4.7, 5.1, 5.3, 5.7), 0.03), got
