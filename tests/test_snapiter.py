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
        {"violations": [{"type": "clearance", "items": [{"description": "Zone fill A"}]}], "unconnected_items": []},
        {"violations": [{"type": "clearance", "items": [{"description": "Zone fill B"}]}], "unconnected_items": []},
    ])
    monkeypatch.setattr(SI, "_report", lambda _board: next(reports))

    def snap(src, dst, skip):
        shutil.copy(src, dst)
        return 1

    monkeypatch.setattr(SI, "_snap", snap)
    rec = SI.run(str(src), str(src))
    assert rec["moved"] == 1 and rec["centred_left"] == 0
