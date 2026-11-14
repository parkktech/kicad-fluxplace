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
    assert gaps[0]["level"] == GC.FAIL
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
