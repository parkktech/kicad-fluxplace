"""FAB_BOM_POS_MISMATCH: BOM / placement-file cross-check against the board.

Boards are hand-written minimal .kicad_pcb text (no kicad-cli, no pcbnew), so
these run everywhere. Each test seeds one specific mismatch."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace import fabxcheck as X                        # noqa: E402

BOARD = """(kicad_pcb (version 20240108)
{fps}
)"""


def _fp(ref, layer="F.Cu", rot=0, attr="smd"):
    return (f'(footprint "Lib:X" (layer "{layer}") (at 1 2 {rot})\n'
            f'  (property "Reference" "{ref}" (at 0 0 0))\n'
            f'  (attr {attr}))')


def _board(tmp_path, fps):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(BOARD.format(fps="\n".join(fps)))
    return str(p)


def _pos(tmp_path, rows):
    p = tmp_path / "pos.csv"
    lines = ["Ref,Val,Package,PosX,PosY,Rot,Side"]
    lines += [f'"{r}","v","p",1,2,{rot},{side}' for r, side, rot in rows]
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def _bom(tmp_path, refs):
    p = tmp_path / "bom.csv"
    p.write_text('"Item","Qty","Reference(s)","Value"\n'
                 + "\n".join(f'1,1,"{r}",v' for r in refs) + "\n")
    return str(p)


def _standard_board(tmp_path):
    return _board(tmp_path, [
        _fp("R1", "F.Cu", 0),
        _fp("C2", "B.Cu", 90),
        _fp("R3", "F.Cu", 0, attr="smd dnp"),                    # DNP: not expected in pos/BOM
        _fp("J4", "F.Cu", 0, attr="through_hole exclude_from_pos_files exclude_from_bom"),
        _fp("U5", "F.Cu", 180),
    ])


def test_clean_board_passes(tmp_path):
    b = _standard_board(tmp_path)
    pos = _pos(tmp_path, [("R1", "top", 0), ("C2", "bottom", 90), ("U5", "top", 180)])
    bom = _bom(tmp_path, ["R1", "C2", "U5", "R3"])   # DNP parts stay in the BOM
    assert X.check(b, pos=pos, bom=bom) == []


def test_missing_from_pos_and_bom(tmp_path):
    b = _standard_board(tmp_path)
    pos = _pos(tmp_path, [("R1", "top", 0), ("U5", "top", 180)])
    bom = _bom(tmp_path, ["R1", "U5"])
    out = X.check(b, pos=pos, bom=bom)
    assert any(m == "FAB_BOM_POS_MISMATCH: missing from pos: C2" for m in out), out
    assert any(m == "FAB_BOM_POS_MISMATCH: missing from BOM: C2" for m in out), out


def test_excluded_from_bom_is_not_expected_in_bom(tmp_path):
    b = _standard_board(tmp_path)
    pos = _pos(tmp_path, [("R1", "top", 0), ("C2", "bottom", 90), ("U5", "top", 180)])
    bom = _bom(tmp_path, ["R1", "C2", "U5"])     # J4 is exclude_from_bom: absent is correct
    assert X.check(b, pos=pos, bom=bom) == []


def test_refs_not_on_board_and_excluded_on_board(tmp_path):
    b = _standard_board(tmp_path)
    pos = _pos(tmp_path, [("R1", "top", 0), ("C2", "bottom", 90), ("U5", "top", 180),
                          ("R9", "top", 0), ("R3", "top", 0)])
    bom = _bom(tmp_path, ["R1", "C2", "U5", "R7"])
    out = X.check(b, pos=pos, bom=bom)
    assert "FAB_BOM_POS_MISMATCH: in pos but not on the board: R9" in out, out
    assert "FAB_BOM_POS_MISMATCH: in BOM but not on the board: R7" in out, out
    assert any("in pos but excluded on the board" in m and "R3" in m for m in out), out


def test_duplicate_in_pos_and_bom(tmp_path):
    b = _standard_board(tmp_path)
    pos = _pos(tmp_path, [("R1", "top", 0), ("R1", "top", 0), ("C2", "bottom", 90),
                          ("U5", "top", 180)])
    bom = _bom(tmp_path, ["R1", "C2", "U5", "C2"])
    out = X.check(b, pos=pos, bom=bom)
    assert "FAB_BOM_POS_MISMATCH: duplicate in pos: R1" in out, out
    assert "FAB_BOM_POS_MISMATCH: duplicate in BOM: C2" in out, out


def test_side_and_rotation_disagreements(tmp_path):
    b = _standard_board(tmp_path)
    # C2 is on B.Cu but the pos says top; R1 is rotated 0 on board, 90 in pos
    pos = _pos(tmp_path, [("R1", "top", 90), ("C2", "top", 90), ("U5", "top", 180)])
    out = X.check(b, pos=pos)
    assert "FAB_BOM_POS_MISMATCH: side mismatch C2: pos=top board=bottom" in out, out
    assert "FAB_BOM_POS_MISMATCH: rotation mismatch R1: pos=90 board=0" in out, out


def test_rotation_wraps_mod_360(tmp_path):
    b = _board(tmp_path, [_fp("R1", "F.Cu", -90)])
    pos = _pos(tmp_path, [("R1", "top", 270)])
    assert X.check(b, pos=pos) == []


def test_unreadable_pos_is_a_fail_not_a_crash(tmp_path):
    b = _standard_board(tmp_path)
    bad = tmp_path / "pos.csv"
    bad.write_text("Foo,Bar\n1,2\n")
    out = X.check(b, pos=str(bad))
    assert len(out) == 1 and "no Ref column" in out[0]


def test_reference_text_fallback_for_old_format(tmp_path):
    b = tmp_path / "old.kicad_pcb"
    b.write_text('(kicad_pcb (footprint "X" (layer "F.Cu") (at 0 0 0)'
                 ' (fp_text reference "R8" (at 0 0)) (attr smd)))')
    fps, dupes = X.board_footprints(str(b))
    assert set(fps) == {"R8"} and dupes == []


def test_emit_logs_seeded_mismatch_as_FAIL_and_manifest(tmp_path, monkeypatch):
    from fluxplace import fab as F
    b = _standard_board(tmp_path)

    def run(args, log):
        if "pos" in args:
            # seeded: C2 missing from the position file
            os.makedirs(os.path.dirname(args[args.index("--output") + 1]), exist_ok=True)
            open(args[args.index("--output") + 1], "w").write(
                "Ref,Val,Package,PosX,PosY,Rot,Side\n"
                '"R1","v","p",1,2,0,top\n"U5","v","p",1,2,180,top\n')
        return True
    monkeypatch.setattr(F, "_run", run)
    lines = []
    res = F.emit(b, str(tmp_path / "out"), kicad_cli="kicad-cli", formats=[],
                 log=lines.append)
    assert res["xcheck"] == ["FAB_BOM_POS_MISMATCH: missing from pos: C2"]
    assert "FAIL FAB_BOM_POS_MISMATCH: missing from pos: C2" in lines
    man = (tmp_path / "out" / "MANIFEST.txt").read_text()
    assert "checks       : FAB_BOM_POS_MISMATCH=FAIL (1)" in man


def test_deliver_gate_exits_1_on_seeded_bom_mismatch(tmp_path, capsys):
    import types
    import pytest
    import cli
    b = _standard_board(tmp_path)
    fab_dir = tmp_path / "fab"
    (fab_dir / "place").mkdir(parents=True)
    (fab_dir / "place" / "pos.csv").write_text(
        "Ref,Val,Package,PosX,PosY,Rot,Side\n"
        '"R1","v","p",1,2,0,top\n"C2","v","p",1,2,90,bottom\n"U5","v","p",1,2,180,top\n')
    bom = _bom(tmp_path, ["R1", "U5"])             # seeded: C2 missing from BOM
    a = types.SimpleNamespace(board=b, fab_dir=str(fab_dir))
    with pytest.raises(SystemExit) as e:
        cli._deliver_xcheck(a, bom=bom)
    assert e.value.code == 1
    assert "FAIL FAB_BOM_POS_MISMATCH: missing from BOM: C2" in capsys.readouterr().out
    # clean BOM passes and returns normally
    cli._deliver_xcheck(a, bom=_bom(tmp_path, ["R1", "C2", "U5"]))
