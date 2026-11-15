"""fix-c #4: finish never overwrites an existing <out>.kicad_pro."""
import os
import cli


def _setup(tmp_path, out_pro_text=None):
    board = tmp_path / "in.kicad_pcb"
    board.write_text("(kicad_pcb)")
    (tmp_path / "in.kicad_pro").write_text('{"from": "input"}')
    out = tmp_path / "out.kicad_pcb"
    if out_pro_text is not None:
        (tmp_path / "out.kicad_pro").write_text(out_pro_text)
    return str(board), str(out)


def test_project_beside_writes_when_absent(tmp_path, capsys):
    board, out = _setup(tmp_path)
    status = cli._project_beside(board, out)
    assert (tmp_path / "out.kicad_pro").read_text() == '{"from": "input"}'
    assert status == "wrote"


def test_project_beside_never_overwrites_existing(tmp_path):
    board, out = _setup(tmp_path, out_pro_text='{"hand": "edited"}')
    status = cli._project_beside(board, out)
    assert (tmp_path / "out.kicad_pro").read_text() == '{"hand": "edited"}'
    assert status == "kept existing"
