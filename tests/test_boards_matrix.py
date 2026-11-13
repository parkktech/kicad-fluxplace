"""Multi-board matrix on real boards: no crash, every command finishes inside its timeout.
Default: the two smallest boards found; FLUXPLACE_MATRIX_ALL=1 runs every board (long)."""
import os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import boardmatrix as M

BOARDS = M.default_boards()


@pytest.mark.skipif(not BOARDS, reason="no real boards on this machine")
def test_matrix_boards_do_not_crash(tmp_path):
    pick = BOARDS if os.environ.get("FLUXPLACE_MATRIX_ALL") else sorted(BOARDS, key=os.path.getsize)[:2]
    rows = [M.one(b, str(tmp_path), 240) for b in pick]
    for r in rows:
        assert not r["crashes"], r["crashes"]
        assert r["ran"] and r["layers"] >= 2 and r["parts"] > 0
        assert r["steps"]["fanout"]["rc"] in (0, 1) and r["steps"]["snap-vias"]["rc"] == 0
        assert "questions" in r
    assert "board | layers" in M.table(rows)


def test_strip_copper_removes_every_track_and_via(tmp_path):
    import pcbgen as G
    from fluxplace import routediag as RD
    body = G.two("R1", 10, 10, "A", "B") + '(segment (start 1 1) (end 2 2) (width 0.2) (layer "F.Cu") (net "A"))\n' + \
        '(via (at 2 2) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net "A"))\n'
    b = G.board(tmp_path, body)
    M.strip_copper(b, str(tmp_path / "s.kicad_pcb"))
    B = RD.load(str(tmp_path / "s.kicad_pcb"))
    assert not B["segs"] and not B["vias"] and len(B["parts"]) == 1
