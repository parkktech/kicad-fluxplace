"""zgeom.floors: constraints without [fab]/[route] floors fall back to the board's own .kicad_pro rules (not 0.05 mm)."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import zgeom as ZG


def _board(tmp_path):
    b = tmp_path / "b.kicad_pcb"
    b.write_text("(kicad_pcb)")
    (tmp_path / "b.kicad_pro").write_text(json.dumps({"board": {"design_settings": {"rules": {"min_track_width": 0.088, "min_clearance": 0.088}}},
                                                       "net_settings": {"classes": [{"name": "Default", "clearance": 0.12}]}}))
    return str(b)


def test_floors_read_the_board_project_rules_when_constraints_give_none(tmp_path):
    pref, ht, hs = ZG.floors({}, _board(tmp_path))
    assert (ht, hs) == (0.088, 0.12) and pref == 0.12


def test_constraints_floors_win_and_no_board_keeps_the_old_default(tmp_path):
    assert ZG.floors({"route": {"min_track_mm": 0.1, "min_clearance_mm": 0.1}}, _board(tmp_path))[1:] == (0.1, 0.1)
    assert ZG.floors({}, None)[1:] == (0.05, 0.05)
    assert ZG.floors({}, "/nonexistent/x.kicad_pcb")[1:] == (0.05, 0.05)
