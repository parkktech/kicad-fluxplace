"""Final DRC summary lists zone islands apart from track opens (W11 lesson 5)."""
import json, os, sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
import cli
from fluxplace import drcguard as DG, progress as PG

REP = {"violations": [], "unconnected_items": [
    {"items": [{"description": "Zone [GND] on F.Cu, priority 0"}, {"description": "Pad 1 [GND]"}]},
    {"items": [{"description": "Zone [+3V3] on In3.Cu, priority 0"}]},
    {"items": [{"description": "Track [USB_DP_CM5] on B.Cu, length 0.2 mm"}, {"description": "Pad 2 [USB_DP_CM5]"}]}]}


def test_open_split_and_lines():
    sp = DG.open_split(REP)
    assert sp == {"islands": {("GND", "F.Cu"): 1, ("+3V3", "In3.Cu"): 1}, "track_opens": 1}
    lines = DG.island_lines(sp)
    assert lines[0] == "open connections: 1 track/via open(s), 2 zone island(s)"
    assert "ZONE_ISLAND GND F.Cu x1" in lines and "ZONE_ISLAND +3V3 In3.Cu x1" in lines
    assert "pour-island" in lines[-1]


def test_no_islands_no_pour_island_hint():
    lines = DG.island_lines(DG.open_split({"unconnected_items": [{"items": [{"description": "Track [A] on F.Cu"}]}]}))
    assert lines == ["open connections: 1 track/via open(s), 0 zone island(s)"]


def test_checkpoint_prints_islands_apart_from_track_opens(tmp_path, monkeypatch, capsys):
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    fake = lambda b: {"unconnected": 3, "errors": 0, "shorts": 0, "track_opens": 1, "zone_islands": 2,
                      "islands": [["GND", "F.Cu", 1], ["+3V3", "In3.Cu", 1]]}
    monkeypatch.setattr(PG, "measure", fake)
    cli.main(["progress", "checkpoint", str(board), "--workdir", str(tmp_path), "--measure"])
    out = capsys.readouterr().out
    assert "ZONE_ISLAND GND F.Cu x1" in out and "ZONE_ISLAND +3V3 In3.Cu x1" in out
    assert "1 track/via open(s), 2 zone island(s)" in out
    hist = PG.load(str(tmp_path))["history"][-1]
    assert "islands" not in hist["metrics"] and hist["metrics"]["zone_islands"] == 2
