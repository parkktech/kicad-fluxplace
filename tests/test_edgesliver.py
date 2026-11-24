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


def _strip_zone_board(tmp_path, via=False):
    """make()'s board plus a small GND zone on In1.Cu in the top edge strip (y 0.2..0.8, x 20..24): a fill island there."""
    src = make(str(tmp_path / "in.kicad_pcb"))
    b = pcbnew.LoadBoard(src)
    gnd = b.FindNet("GND")
    z = pcbnew.ZONE(b)
    z.SetNet(gnd)
    z.SetLayer(b.GetLayerID("In1.Cu"))
    z.SetIsRuleArea(False)
    z.Outline().NewOutline()
    for x, y in ((20, 0.2), (24, 0.2), (24, 0.8), (20, 0.8)):
        z.Outline().Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    b.Add(z)
    if via:
        v = pcbnew.PCB_VIA(b)
        v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(22), pcbnew.FromMM(0.5)))
        v.SetNet(gnd)
        v.SetWidth(pcbnew.FromMM(0.6))
        v.SetDrill(pcbnew.FromMM(0.3))
        v.SetLayerPair(b.GetLayerID("F.Cu"), b.GetLayerID("B.Cu"))
        b.Add(v)
    pcbnew.SaveBoard(src, b)
    return src


def test_edge_strip_island_is_a_sliver_and_is_removed(tmp_path):
    src = _strip_zone_board(tmp_path)
    b = pcbnew.LoadBoard(src)
    PL_refill = __import__("fluxplace.planes", fromlist=["refill"]).refill
    PL_refill(b)
    assert ES.sliver_islands(b, "GND", 0.3) != []                          # the strip island is found
    out = str(tmp_path / "out.kicad_pcb")
    r = ES.repair(src, out, "GND")
    assert r["accepted"] and os.path.exists(out) and r["islands"] == 1
    # the zone's minimum island area is never raised above the sliver cap by repair (it already was 10 mm2 here, so kept)
    zs = [z for z in pcbnew.LoadBoard(out).Zones() if z.GetNetname() == "GND" and not z.GetIsRuleArea()]
    assert all(z.GetMinIslandArea() <= max(2.0 * 1e12, 10.0 * 1e12) for z in zs)


def test_edge_strip_island_with_a_via_is_kept(tmp_path):
    src = _strip_zone_board(tmp_path, via=True)
    b = pcbnew.LoadBoard(src)
    __import__("fluxplace.planes", fromlist=["refill"]).refill(b)
    assert ES.sliver_islands(b, "GND", 0.3) == []                          # a via of the net on the island: a connection


def test_sliver_rule_is_the_edge_strip_not_the_middle(tmp_path):
    src = make(str(tmp_path / "in.kicad_pcb"))
    b = pcbnew.LoadBoard(src)
    before = {z.GetMinIslandArea() for z in b.Zones()}
    r = ES.repair(src, str(tmp_path / "o.kicad_pcb"), "GND")
    assert not r["accepted"] and "no edge-band sliver island" in r["reason"]
    assert {z.GetMinIslandArea() for z in pcbnew.LoadBoard(src).Zones()} == before


def test_pour_is_judged_at_the_end_not_by_a_gate(tmp_path):
    """PHASE3I F8 (B6): no gate default lists open-pour; the final close (no scope) still dispatches it."""
    from fluxplace import gaps as GP
    assert all("open-pour" not in v for v in GP.GATE_DEFAULTS.values())
    assert GP.DISPATCH["open-pour"] == ["edgesliver", "pourisland", "region", "ai"]
