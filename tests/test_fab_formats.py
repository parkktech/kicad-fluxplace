"""fab.emit() extra formats (ODB++, IPC-2581, IPC-D-356) and deliver()'s
include_formats split. The real-export tests build a 20x20 mm board with
pcbnew and need kicad-cli + pcbnew; they skip cleanly without them. The
deliver tests use a hand-written package and run everywhere."""
import os
import shutil
import subprocess
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace import fab as F                              # noqa: E402

KICAD_CLI = shutil.which("kicad-cli") or "/usr/bin/kicad-cli"
try:
    import pcbnew                                           # noqa: E402
except ImportError:
    pcbnew = None

needs_kicad = pytest.mark.skipif(
    pcbnew is None or not os.path.exists(KICAD_CLI), reason="needs pcbnew + kicad-cli")


def _tiny_board(path):
    b = pcbnew.BOARD()
    r = pcbnew.PCB_SHAPE(b)
    r.SetShape(pcbnew.SHAPE_T_RECT)
    r.SetStart(pcbnew.VECTOR2I(0, 0))
    r.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(20), pcbnew.FromMM(20)))
    r.SetLayer(pcbnew.Edge_Cuts)
    b.Add(r)
    b.Save(path)
    return path


def _log_sink():
    lines = []
    return lines, lines.append


@needs_kicad
def test_emit_exports_all_supported_formats(tmp_path):
    board = _tiny_board(str(tmp_path / "tiny.kicad_pcb"))
    out = str(tmp_path / "fab")
    lines, log = _log_sink()
    res = F.emit(board, out, kicad_cli=KICAD_CLI, log=log)

    assert res["stages"]["odb"] == "ok", lines
    assert res["stages"]["ipc2581"] == "ok", lines
    assert res["stages"]["ipcd356"] == "ok", lines
    assert not [l for l in lines if l.startswith("FAIL")], lines
    for sdir, name in (("odb", "tiny_odb.zip"), ("ipc2581", "tiny.xml"),
                       ("netlist", "tiny.d356")):
        p = os.path.join(out, sdir, name)
        assert os.path.isfile(p) and os.path.getsize(p) > 0, p
    # ODB++ zip is a real zip; IPC-2581 is XML
    assert zipfile.is_zipfile(os.path.join(out, "odb", "tiny_odb.zip"))
    assert open(os.path.join(out, "ipc2581", "tiny.xml")).read(200).lstrip().startswith("<")

    man = open(os.path.join(out, "MANIFEST.txt")).read()
    assert "FILE         : odb/tiny_odb.zip  (" in man
    assert "FILE         : netlist/tiny.d356  (" in man
    assert "odb=ok" in man


@needs_kicad
def test_gerber_drill_place_layout_unchanged_by_formats(tmp_path):
    board = _tiny_board(str(tmp_path / "tiny.kicad_pcb"))
    a = F.emit(board, str(tmp_path / "with"), kicad_cli=KICAD_CLI, log=lambda *x: None)
    b = F.emit(board, str(tmp_path / "without"), kicad_cli=KICAD_CLI,
               formats=[], log=lambda *x: None)
    assert a["files"]["gerbers"] == b["files"]["gerbers"]
    assert a["files"]["drill"] == b["files"]["drill"]
    assert a["files"]["place"] == b["files"]["place"]
    assert not os.path.exists(os.path.join(str(tmp_path / "without"), "odb"))


def test_failed_format_is_a_FAIL_line_not_silent(tmp_path, monkeypatch):
    real_run = F._run

    def run(args, log):
        if "odb" in args:
            log("    ! seeded odb failure")
            return False
        return True
    monkeypatch.setattr(F, "_run", run)
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    lines, log = _log_sink()
    res = F.emit(str(board), str(tmp_path / "out"), kicad_cli="kicad-cli",
                 formats=["odb"], log=log)
    assert res["stages"]["odb"] == "FAIL"
    assert any(l.startswith("FAIL fab format odb") for l in lines), lines
    man = open(tmp_path / "out" / "MANIFEST.txt").read()
    assert "odb=FAIL" in man
    assert "FILE         : odb/" not in man
    assert real_run is not None


def test_unknown_format_is_a_FAIL_line(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "_run", lambda args, log: True)
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    lines, log = _log_sink()
    res = F.emit(str(board), str(tmp_path / "out"), kicad_cli="kicad-cli",
                 formats=["gerber_v9"], log=log)
    assert res["stages"]["gerber_v9"].startswith("FAIL")
    assert any("FAIL fab format gerber_v9" in l for l in lines)


def _fake_package(root):
    """A fab package with gerbers, drill, place and one odb file (sized)."""
    fab_dir = root / "fabpkg"
    for sub in ("gerbers", "drill", "place", "odb"):
        os.makedirs(fab_dir / sub)
    (fab_dir / "gerbers" / "F_Cu.gbr").write_text("g")
    (fab_dir / "drill" / "board-PTH.drl").write_text("d")
    (fab_dir / "place" / "pos.csv").write_text("Ref\n")
    (fab_dir / "odb" / "board_odb.zip").write_bytes(b"PK")
    (fab_dir / "drc.json").write_text("{}")
    with open(fab_dir / "MANIFEST.txt", "w") as f:
        f.write("fluxplace fab package\n")
        f.write("FILE         : gerbers/F_Cu.gbr\n")
        f.write("FILE         : drill/board-PTH.drl\n")
        f.write("FILE         : place/pos.csv\n")
        f.write("FILE         : odb/board_odb.zip  (2 bytes)\n")
    return fab_dir


def test_deliver_formats_loose_by_default(tmp_path):
    fab_dir = _fake_package(tmp_path)
    out = tmp_path / "delivered"
    res = F.deliver(str(fab_dir), str(out), "1-GERBERS-x", centroid_name=None,
                    log=lambda *a: None)
    with zipfile.ZipFile(res["zip"]) as z:
        names = z.namelist()
    assert not any("odb" in n for n in names), names
    assert (out / "odb" / "board_odb.zip").read_bytes() == b"PK"
    assert "odb/" in res["loose"]


def test_deliver_include_formats_puts_them_in_zip(tmp_path):
    fab_dir = _fake_package(tmp_path)
    out = tmp_path / "delivered"
    res = F.deliver(str(fab_dir), str(out), "1-GERBERS-x", centroid_name=None,
                    log=lambda *a: None, include_formats=("odb",))
    with zipfile.ZipFile(res["zip"]) as z:
        names = z.namelist()
    assert any(n.endswith("odb/board_odb.zip") for n in names), names
    assert not (out / "odb").exists()
    assert "odb/" not in res["loose"]


def test_manifest_parser_strips_size_suffix(tmp_path):
    (tmp_path / "MANIFEST.txt").write_text(
        "FILE         : odb/board_odb.zip  (2 bytes)\n")
    assert F._manifest_files(str(tmp_path)) == {"odb": ["board_odb.zip"]}
