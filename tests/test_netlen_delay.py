"""A2 / D71cx (amended): where a budget is in ps the pair is graded on delay skew = sum over layers of (KiCad segment length on the
layer x stackup ps/mm) + via barrels, reconciled with KiCad's total length per leg; tune equalises delay.  Fixture = the W15 ETH_P2
shape: legs of equal KiCad length whose copper splits differently between outer (6.0 ps/mm) and inner (7.2 ps/mm) layers."""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pytest.importorskip("pcbnew")
import planboards as PB
from fluxplace import netlen, pairaudit as PA, routediag as RD, tune as TU, kicad_io as IO

pytestmark = pytest.mark.skipif(not shutil.which("kicad-cli"), reason="kicad-cli not installed")
HEAD6 = ('(kicad_pcb (version 20260206) (generator "pcbnew")\n(layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) '
         '(8 "In3.Cu" signal) (10 "In4.Cu" signal) (2 "B.Cu" signal))\n')
EDGE = '(gr_rect (start 0 0) (end 100 80) (stroke (width 0.1) (type default)) (fill no) (layer "Edge.Cuts"))\n'
STACK = {"stackup": {"profile": "pcbway-6l-1.6-2116", "planes": ["In1.Cu", "In4.Cu"]}}


OX, OY = 30.0, 30.0                                  # clear of the board edge


def _seg(net, a, b, layer):
    return f'(segment (start {a[0] + OX} {a[1] + OY}) (end {b[0] + OX} {b[1] + OY}) (width 0.25) (layer "{layer}") (net "{net}"))\n'


def _via(net, x):
    return f'(via (at {x + OX} {{y}}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "{net}"))\n'


def _leg(net, y, outer):
    """F.Cu `outer` mm, via, In2.Cu (30 - 2 outer) mm, via, F.Cu `outer` mm: 30 mm of copper whatever the split."""
    a, b = outer, 30 - outer
    return (_seg(net, (0, y), (a, y), "F.Cu") + _via(net, a).format(y=y + OY) + _seg(net, (a, y), (b, y), "In2.Cu")
            + _via(net, b).format(y=y + OY) + _seg(net, (b, y), (30, y), "F.Cu"))


def board(tmp_path, p_outer, n_outer):
    body = PB.fp("U1", OX, OY, [PB.pad("1", 0, 0, "E_P"), PB.pad("2", 0, 2.0, "E_N")]) + \
        PB.fp("U2", OX, OY, [PB.pad("1", 30, 0, "E_P"), PB.pad("2", 30, 2.0, "E_N")]) + _leg("E_P", 0, p_outer) + _leg("E_N", 2.0, n_outer)
    p = tmp_path / "b.kicad_pcb"
    p.write_text(HEAD6 + EDGE + body + ")\n")
    return str(p)


def test_equal_kicad_length_but_unequal_delay(tmp_path):
    f = board(tmp_path, 4, 8)
    r = netlen.pair_skew(RD.load(f), "E_P", "E_N", STACK, grade_ps=True)
    assert r["status"] == "ok" and r["basis"] == "kicad"
    assert abs(r["skew_mm"]) < 0.02                                     # KiCad: the legs are the same length
    assert r["skew_ps"] > 6.0                                           # P has 8 mm more inner copper (7.2 vs 6.0 ps/mm)
    assert max(abs(r["recon"]["p"]), abs(r["recon"]["n"])) <= netlen.RECON_TOL_MM
    d = netlen.leg_decomposition(IO.load(f), "E_P", *netlen._rows(STACK, f))
    assert d["layer_mm"]["F.Cu"] == pytest.approx(8.0, abs=1e-3) and d["layer_mm"]["In2.Cu"] == pytest.approx(22.0, abs=1e-3)
    assert 6.0 < d["ps_per_mm"]["In2.Cu"] / 1.0 < 8.0 and d["ps_per_mm"]["In2.Cu"] > d["ps_per_mm"]["F.Cu"] + 0.8


def test_the_budget_unit_picks_the_verdict(tmp_path):
    B = RD.load(board(tmp_path, 4, 8))
    ps = dict(STACK, pairs={"E_": {"skew_ps": 2.0, "skew_max_ps": 4.0}})
    mm = dict(STACK, pairs={"E_": {"skew_mm": 0.1, "skew_max_mm": 0.2}})
    a_ps = PA.audit(B, [("E_P", "E_N")], ps, set())[("E_P", "E_N")]
    a_mm = PA.audit(B, [("E_P", "E_N")], mm, set())[("E_P", "E_N")]
    assert a_ps["unit"] == "ps" and "skew" in a_ps["causes"] and a_ps["skew"] > 4.0 and a_ps["limit"] == 4.0 and a_ps["target"] == 2.0
    assert a_mm["unit"] == "mm" and "skew" not in a_mm["causes"] and a_mm["skew"] < 0.1             # same board, mm budget: length matches


def test_measure_pair_reports_the_graded_unit_and_the_mm_a_meander_must_add(tmp_path):
    f = board(tmp_path, 4, 8)
    B = RD.load(f)
    m = TU.measure_pair(f, B, "E_P", "E_N", dict(STACK, pairs={"E_": {"skew_ps": 2.0, "skew_max_ps": 4.0}}))
    assert m["ok"] and m["unit"] == "ps" and m["skew"] > 6.0 and m["limit"] == 2.0
    assert 0.8 < m["eq_mm"] < 2.0 and m["eq_mm"] == pytest.approx(m["skew"] / 6.4, rel=0.15)        # ps / (mean ps per mm)
    m2 = TU.measure_pair(f, B, "E_P", "E_N", {"pairs": {"E_": {"skew_mm": 0.1}}}, limit_mm=0.1)
    assert m2["unit"] == "mm" and abs(m2["skew"]) < 0.02 and m2["limit"] == 0.1


def test_tune_equalises_delay_not_length(tmp_path):
    f = board(tmp_path, 4, 8)
    out = str(tmp_path / "t.kicad_pcb")
    shutil.copy(f, out)
    res = {}
    cons = dict(STACK, pairs={"E_": {"skew_ps": 2.0, "skew_max_ps": 4.0}})
    before = TU.measure_pair(f, RD.load(f), "E_P", "E_N", cons)["skew"]
    TU.tune_pairs(f, out, {"E_N": "E_P"}, 0.1, cons=cons, result=res, log=lambda m: None, max_rounds=3)
    row = res["pairs"][0]
    assert row["unit"] == "ps" and row["status"] in ("ok", "over")
    after = TU.measure_pair(out, RD.load(out), "E_P", "E_N", cons)
    assert abs(after["skew"]) < abs(before) - 3.0, (before, after)        # delay moved toward equal
    assert after["skew"] > -2.0 * 2                                       # and did not overshoot to the other side


def test_tune_without_a_ps_budget_leaves_equal_lengths_alone(tmp_path):
    f = board(tmp_path, 4, 8)
    out = str(tmp_path / "t.kicad_pcb")
    res = {}
    TU.tune_pairs(f, out, {"E_N": "E_P"}, 0.1, cons={"pairs": {"E_": {"skew_mm": 0.1}}}, result=res, log=lambda m: None)
    assert res["pairs"][0]["status"] == "ok" and res["pairs"][0]["unit"] == "mm" and res["accepted"] == 0


def test_board_without_a_stackup_reconciles_via_height_with_kicad(tmp_path):
    """no (stackup): KiCad's via height is the board thickness less half a copper layer; netlen._default_rows models it."""
    body = PB.fp("U1", 30, 30, [PB.pad("1", 0, 0, "E_P"), PB.pad("2", 0, 2.0, "E_N")]) + \
        PB.fp("U2", 30, 30, [PB.pad("1", 20, 0, "E_P"), PB.pad("2", 20, 2.0, "E_N")])
    for net, y in (("E_P", 0.0), ("E_N", 2.0)):
        body += (_seg(net, (0, y), (8, y), "F.Cu") + _via(net, 8).format(y=y + OY) + _seg(net, (8, y), (20, y), "B.Cu"))
    f = tmp_path / "b.kicad_pcb"
    f.write_text('(kicad_pcb (version 20260206) (generator "pcbnew")\n(layers (0 "F.Cu" signal) (2 "B.Cu" signal))\n' + EDGE + body + ")\n")
    r = netlen.pair_skew(RD.load(str(f)), "E_P", "E_N", {}, grade_ps=True)
    assert r["status"] == "ok" and max(abs(r["recon"]["p"]), abs(r["recon"]["n"])) <= netlen.RECON_TOL_MM, r["recon"]
    assert r["skew_ps"] is None                                   # no reference planes: no delay, so a ps budget would not grade it
