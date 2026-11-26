"""_link_lib: workdir/lib links the project lib so stage boards resolve ${KIPRJMOD}/../lib."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import pipeline as PL


def test_links_project_lib(tmp_path):
    proj = tmp_path / "project"
    (proj / "lib" / "3dmodels").mkdir(parents=True)
    (proj / "W").mkdir()
    board = proj / "W" / "x.kicad_pcb"
    board.write_text("")
    work = proj / "W" / "run"
    work.mkdir()
    link = PL._link_lib(str(work), str(board))
    assert link == os.path.join(str(work), "lib")
    assert os.path.realpath(os.path.join(str(work), "lib")) == os.path.realpath(str(proj / "lib"))


def test_existing_link_not_replaced(tmp_path):
    proj = tmp_path / "project"
    (proj / "lib" / "3dmodels").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    (proj / "W").mkdir()
    board = proj / "W" / "x.kicad_pcb"
    board.write_text("")
    work = tmp_path / "run"
    work.mkdir()
    (work / "lib").symlink_to(other)
    link = PL._link_lib(str(work), str(board))
    assert link == os.path.join(str(work), "lib")
    assert os.path.realpath(os.path.join(str(work), "lib")) == os.path.realpath(str(other))


def test_no_lib_returns_none(tmp_path):
    (tmp_path / "W").mkdir()
    board = tmp_path / "W" / "x.kicad_pcb"
    board.write_text("")
    work = tmp_path / "run"
    work.mkdir()
    assert PL._link_lib(str(work), str(board)) is None
    assert not os.path.lexists(os.path.join(str(work), "lib"))
