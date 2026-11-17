import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import snapiter as SI  # noqa: E402


def test_same_input_and_output_board(tmp_path, monkeypatch):
    import shutil
    src = tmp_path / "b.kicad_pcb"
    src.write_text("board")
    (tmp_path / "b.kicad_pro").write_text("{}")
    monkeypatch.setattr(SI, "_report", lambda _board: {"violations": [], "unconnected_items": []})

    def snap(src, dst, skip):
        shutil.copy(src, dst)
        return 1

    monkeypatch.setattr(SI, "_snap", snap)
    rec = SI.run(str(src), str(src))
    assert rec == {"moved": 1, "excluded": 0, "iters": 1, "centred_left": 0}


def test_zone_fill_flicker_does_not_reject_accepted_snap(tmp_path, monkeypatch):
    import shutil
    src = tmp_path / "b.kicad_pcb"
    src.write_text("board")
    (tmp_path / "b.kicad_pro").write_text("{}")
    reports = iter([
        {"violations": [{"type": "solder_mask_bridge", "items": [{"description": "Zone fill A"}]}], "unconnected_items": []},
        {"violations": [{"type": "solder_mask_bridge", "items": [{"description": "Zone fill B"}]}], "unconnected_items": []},
    ])
    monkeypatch.setattr(SI, "_report", lambda _board: next(reports))

    def snap(src, dst, skip):
        shutil.copy(src, dst)
        return 1

    monkeypatch.setattr(SI, "_snap", snap)
    rec = SI.run(str(src), str(src))
    assert rec["moved"] == 1 and rec["centred_left"] == 0


def test_zone_clearance_increase_is_not_ignored_by_snap_guard(tmp_path, monkeypatch):
    import shutil
    src = tmp_path / "b.kicad_pcb"
    src.write_text("original")
    (tmp_path / "b.kicad_pro").write_text("{}")
    violation = {"type": "clearance", "items": [{"uuid": "track1", "description": "Track [N] on F.Cu"},
                                                {"uuid": "zone1", "description": "Zone [M] on In1.Cu"}]}
    reports = iter([
        {"violations": [violation], "unconnected_items": []},
        {"violations": [violation, violation], "unconnected_items": []},
        {"violations": [violation, violation], "unconnected_items": []},
    ])
    monkeypatch.setattr(SI, "_report", lambda _board: next(reports))
    monkeypatch.setattr(SI, "_snap", lambda src, dst, skip: shutil.copy(src, dst) or 1)
    rec = SI.run(str(src), str(src))
    assert rec["moved"] == 0 and rec["excluded"] == 2
    assert src.read_text() == "original"
