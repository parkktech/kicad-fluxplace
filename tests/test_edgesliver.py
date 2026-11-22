"""Edge-sliver island removal (edgesliver.py) and the drc report slots (drcguard.report_slot): unit level."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pcbnew = pytest.importorskip("pcbnew")
from fluxplace import edgesliver as ES, drcguard as DG
from test_planetie import make


def test_no_sliver_island_is_refused_without_output(tmp_path):
    src = make(str(tmp_path / "in.kicad_pcb"))
    out = str(tmp_path / "out.kicad_pcb")
    r = ES.repair(src, out, "NO_SUCH_NET")
    assert not r["accepted"] and not os.path.exists(out)


def test_interior_pour_is_not_an_edge_sliver(tmp_path):
    # the fixture's pour fills the board interior: no outline meets the edge band, so nothing is removed
    src = make(str(tmp_path / "in.kicad_pcb"))
    b = pcbnew.LoadBoard(src)
    assert ES.sliver_islands(b, "GND", 0.3) == []


def test_report_slots_are_unique(tmp_path):
    board = str(tmp_path / "b.kicad_pcb")
    open(board, "w").close()
    slots = {DG.report_slot(board) for _ in range(5)}
    assert len(slots) == 5 and all(os.path.exists(s) for s in slots)
    for s in slots:
        os.unlink(s)


def test_second_session_on_same_work_path_is_refused_and_leaves_first_untouched(tmp_path):
    import json
    from fluxplace import handroute as H
    work = str(tmp_path / "hand.kicad_pcb")
    S = H.Sess(work)
    os.makedirs(S.dir)
    S.save_meta({"source": "first-agent.kicad_pcb", "work": work})
    with pytest.raises(RuntimeError, match="own work path"):
        H.session_begin(str(tmp_path / "other.kicad_pcb"), work)
    assert json.load(open(S.meta_path))["source"] == "first-agent.kicad_pcb"
    assert not os.path.exists(work)


def test_tune_work_copy_carries_project_rules(tmp_path):
    from fluxplace import tune as T
    board = str(tmp_path / "b.kicad_pcb")
    work = str(tmp_path / "w.kicad_pcb")
    open(board, "w").close()
    open(str(tmp_path / "b.kicad_pro"), "w").write("{}")
    T._copy_sidecars(board, work)
    assert os.path.exists(str(tmp_path / "w.kicad_pro"))
