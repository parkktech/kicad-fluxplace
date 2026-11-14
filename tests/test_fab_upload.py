"""fix-c #10: upload_package copies only the schematic sheets reachable from the root sheet, and injects
netclasses from the board actually packaged."""
import json
import os
from fluxplace import fab


def _proj(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "root.kicad_pro").write_text(json.dumps({"net_settings": {"classes": [], "netclass_patterns": []}}))
    (d / "root.kicad_sch").write_text('(kicad_sch (sheet (at 0 0) (size 1 1) (property "Sheetfile" "power.kicad_sch")))')
    (d / "power.kicad_sch").write_text('(kicad_sch (sheet (at 0 0) (size 1 1) (property "Sheetfile" "sub/deep.kicad_sch")))')
    (d / "sub").mkdir()
    (d / "sub" / "deep.kicad_sch").write_text("(kicad_sch)")
    (d / "stale_block.kicad_sch").write_text("(kicad_sch)")
    (d / "root.kicad_pcb").write_text("(kicad_pcb)")
    return d


def test_only_reachable_sheets_are_copied(tmp_path):
    d = _proj(tmp_path)
    out = tmp_path / "out"
    written = fab.upload_package(str(d / "root.kicad_pcb"), str(out), project_dir=str(d), log=lambda *_: None)
    names = sorted(os.path.basename(p) for p in written)
    assert names == ["deep.kicad_sch", "power.kicad_sch", "root.kicad_pcb", "root.kicad_pro", "root.kicad_sch"]
    assert not (out / "stale_block.kicad_sch").exists()


def test_netclasses_come_from_the_packaged_board(tmp_path, monkeypatch):
    from fluxplace import constraints as C, rules as RU
    d = _proj(tmp_path)
    other = tmp_path / "packaged.kicad_pcb"
    other.write_text("(kicad_pcb)")
    (d / "root.constraints.toml").write_text("")
    seen = {}
    monkeypatch.setattr(C, "load", lambda p: {"rule": []})
    monkeypatch.setattr(C, "inject_netclasses", lambda pro, cons, log=print, board=None: seen.setdefault("board", board))
    monkeypatch.setattr(fab, "quilter_csvs", lambda *a, **k: None)
    fab.upload_package(str(other), str(tmp_path / "out"), project_dir=str(d), log=lambda *_: None)
    assert seen["board"] == str(other)
