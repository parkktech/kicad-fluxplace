"""fab.emit() / fab.deliver(): no stale output survives a re-cut.

utv-comms V1.5's blind-via fix (2026-09-08) re-cut the board to through
vias, but the previous cut's back-In2/In3 blind-via drill files (and their
gerberX2 map) were still sitting in fab-v1.5/drill/ — emit() only ever
makes kicad-cli WRITE what the current board needs, it never deletes what
a past cut left that the current board no longer produces — and deliver()
zipped them into the PCBWay gerber upload right along with the real ones.

These tests fake out kicad-cli (fab._run) so they run without it installed:
they only exercise the directory-hygiene logic (_reset_dir, the manifest's
FILE lines, deliver()'s manifest-driven zip), not the real CLI export.
"""
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace import fab as F                              # noqa: E402


def _fake_run(gdir, ddir, pdir):
    """Stand-in for fab._run: writes the one file each export stage would
    leave for a tiny fixed board, instead of shelling out to kicad-cli."""
    def run(args, log):
        if "gerbers" in args:
            open(os.path.join(gdir, "F_Cu.gbr"), "w").write("g")
        elif "drill" in args:
            open(os.path.join(ddir, "board-PTH.drl"), "w").write("d")
        elif "pos" in args:
            open(os.path.join(pdir, "pos.csv"), "w").write("p")
        return True
    return run


def test_emit_clears_stale_output_before_export(tmp_path, monkeypatch):
    out = str(tmp_path / "fab-out")
    gdir, ddir, pdir = (os.path.join(out, s) for s in ("gerbers", "drill", "place"))
    os.makedirs(ddir)
    stale_drl = os.path.join(ddir, "board-back-in2.drl")
    stale_map = os.path.join(ddir, "board-back-in2_map.gbr")
    open(stale_drl, "w").write("stale blind-via drill")
    open(stale_map, "w").write("stale blind-via map")

    board = str(tmp_path / "board.kicad_pcb")
    open(board, "w").write("(kicad_pcb)")

    monkeypatch.setattr(F, "_run", _fake_run(gdir, ddir, pdir))
    res = F.emit(board, out, log=lambda *a: None)

    # the stale blind-via files are gone, only this cut's file remains
    assert not os.path.exists(stale_drl)
    assert not os.path.exists(stale_map)
    assert sorted(os.listdir(ddir)) == ["board-PTH.drl"]

    # emit()'s own record of what it wrote matches the directory exactly
    assert res["files"]["drill"] == ["board-PTH.drl"]
    assert res["files"]["gerbers"] == ["F_Cu.gbr"]
    assert res["files"]["place"] == ["pos.csv"]

    manifest = open(os.path.join(out, "MANIFEST.txt")).read()
    assert "FILE         : drill/board-PTH.drl" in manifest
    assert "FILE         : gerbers/F_Cu.gbr" in manifest
    assert "back-in2" not in manifest


def test_emit_reruns_clean_on_an_already_populated_output_dir(tmp_path, monkeypatch):
    """A second emit() into the SAME --out (the normal re-cut workflow) must
    not accumulate files from the first run."""
    out = str(tmp_path / "fab-out")
    gdir, ddir, pdir = (os.path.join(out, s) for s in ("gerbers", "drill", "place"))
    board = str(tmp_path / "board.kicad_pcb")
    open(board, "w").write("(kicad_pcb)")

    monkeypatch.setattr(F, "_run", _fake_run(gdir, ddir, pdir))
    F.emit(board, out, log=lambda *a: None)
    # simulate an obsolete file left by a differently-configured first cut
    open(os.path.join(ddir, "board-back-in3.drl"), "w").write("old state")
    res = F.emit(board, out, log=lambda *a: None)

    assert sorted(os.listdir(ddir)) == ["board-PTH.drl"]
    assert res["files"]["drill"] == ["board-PTH.drl"]


def test_deliver_zips_from_manifest_not_directory_glob(tmp_path):
    fab_dir = tmp_path / "fabpkg"
    gdir, ddir = fab_dir / "gerbers", fab_dir / "drill"
    os.makedirs(gdir)
    os.makedirs(ddir)
    (gdir / "F_Cu.gbr").write_text("g")
    (ddir / "board-PTH.drl").write_text("d")
    # a leftover file NOT recorded in the manifest — deliver() must ignore it
    (ddir / "board-back-in2.drl").write_text("stale, not in manifest")
    (fab_dir / "drc.json").write_text("{}")
    with open(fab_dir / "MANIFEST.txt", "w") as f:
        f.write("fluxplace fab package\n")
        f.write("FILE         : gerbers/F_Cu.gbr\n")
        f.write("FILE         : drill/board-PTH.drl\n")

    out_dir = tmp_path / "delivered"
    res = F.deliver(str(fab_dir), str(out_dir), "1-GERBERS-test",
                    centroid_name=None, log=lambda *a: None)

    with zipfile.ZipFile(res["zip"]) as z:
        names = z.namelist()
    assert any(n.endswith("board-PTH.drl") for n in names)
    assert any(n.endswith("F_Cu.gbr") for n in names)
    assert not any("back-in2" in n for n in names)


def test_manifest_files_parses_file_lines_and_ignores_missing_manifest(tmp_path):
    fab_dir = tmp_path / "fabpkg"
    os.makedirs(fab_dir)
    assert F._manifest_files(str(fab_dir)) == {}   # no MANIFEST.txt yet
    with open(fab_dir / "MANIFEST.txt", "w") as f:
        f.write("fluxplace fab package\n")
        f.write("FILE         : gerbers/F_Cu.gbr\n")
        f.write("FILE         : gerbers/B_Cu.gbr\n")
        f.write("FILE         : drill/board-PTH.drl\n")
    assert F._manifest_files(str(fab_dir)) == {
        "gerbers": ["F_Cu.gbr", "B_Cu.gbr"],
        "drill": ["board-PTH.drl"],
    }
