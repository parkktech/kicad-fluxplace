"""[timing.*] budgets on pcbnew-built chains: data-vs-clock, data-vs-strobe and the clock flyby order.
Expected numbers are reproduced by hand: a microstrip on F.Cu has k ps/mm (impedance.route_delay_ps_per_mm, the
same function the review uses), so a net's flight time is k * its length on that layer."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import impedance as IM
from fluxplace import timing as TM
from fluxplace import review as RV

STACK = [{"type": "copper", "name": "F.Cu", "thickness": 0.035},
         {"type": "dielectric", "name": "", "thickness": 0.2, "epsilon_r": 4.5},
         {"type": "copper", "name": "In1.Cu", "thickness": 0.035}]
PLANES = ["In1.Cu"]


def build(path, nets):
    """nets = {name: [(ref, x_mm, y_mm), ...]}: one pad per point, a straight F.Cu segment between consecutive points."""
    import pcbnew
    b = pcbnew.BOARD()
    ni = {}
    for n in nets:
        ni[n] = pcbnew.NETINFO_ITEM(b, n)
        b.Add(ni[n])
    for n, pts in nets.items():
        for ref, x, y in pts:
            fp = pcbnew.FOOTPRINT(b)
            fp.SetReference(ref)
            fp.Reference().SetVisible(False)
            fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
            pad = pcbnew.PAD(fp)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.6), pcbnew.FromMM(0.6)))
            pad.SetNumber("1")
            pad.SetNet(ni[n])
            fp.Add(pad)
            b.Add(fp)
        for (_, x0, y0), (_, x1, y1) in zip(pts, pts[1:]):
            t = pcbnew.PCB_TRACK(b)
            t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x0), pcbnew.FromMM(y0)))
            t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
            t.SetLayer(pcbnew.F_Cu)
            t.SetWidth(pcbnew.FromMM(0.2))
            t.SetNet(ni[n])
            b.Add(t)
    b.Save(str(path))
    return str(path)


def k_ps_per_mm():
    return IM.route_delay_ps_per_mm(STACK, set(PLANES), {"F.Cu": 1.0})


def facts(path, nets):
    return {"board_path": path, "net_pads": {n: [] for n in nets}, "stackup": STACK, "plane_layers": PLANES}


def run(tmp_path, nets, cons):
    pytest.importorskip("pcbnew")
    import pcbnew
    path = build(tmp_path / "t.kicad_pcb", nets)
    return TM.check(pcbnew.LoadBoard(path), facts(path, nets), cons)


CLK = [("U1", 10, 10), ("D1", 30, 10), ("D2", 50, 10)]               # 40 mm of clock copper


def test_data_late_and_early_against_clock(tmp_path):
    k = k_ps_per_mm()
    nets = {"CK": CLK,
            "DQ_LATE": [("U1", 10, 20), ("X1", 65, 20)],             # 55 mm: 15 mm longer than CK
            "DQ_EARLY": [("U1", 10, 30), ("X2", 30, 30)],            # 20 mm: 20 mm shorter than CK
            "DQ_OK": [("U1", 10, 40), ("X3", 50, 40)]}               # 40 mm: equal
    # setup budget 20 mm-equivalent: DQ_LATE (+15 mm = +15k ps) passes; hold 15 mm-equivalent: DQ_EARLY (-20 mm = -20k ps) fails
    cons = {"timing": {"T": {"clock": "CK", "data": ["DQ_.*"], "setup_ps": 20 * k, "hold_ps": 15 * k}}}
    skew = [x for x in run(tmp_path, nets, cons) if x["code"] == "TIMING_SKEW"]
    assert len(skew) == 1 and skew[0]["level"] == "FAIL" and "EARLY" in skew[0]["msg"]
    assert "DQ_EARLY" in skew[0]["msg"] and f"{20 * k:.1f} ps EARLY" in skew[0]["msg"]


def test_late_flagged_with_tighter_budget_and_skew_matches_hand_value(tmp_path):
    k = k_ps_per_mm()
    nets = {"CK": CLK, "DQ_LATE": [("U1", 10, 20), ("X1", 65, 20)]}          # 55 vs 40 mm
    cons = {"timing": {"T": {"clock": "CK", "data": ["DQ_LATE"], "setup_ps": 10 * k, "hold_ps": 10 * k}}}
    late = [x for x in run(tmp_path, nets, cons) if x["code"] == "TIMING_SKEW"]
    assert len(late) == 1 and "LATE" in late[0]["msg"]
    assert f"{15 * k:.1f} ps LATE" in late[0]["msg"]                     # 15 mm x k


def test_strobe_group_checks_against_its_strobe_not_the_clock(tmp_path):
    nets = {"CK": CLK,                                                    # 40 mm
            "DQS0": [("U1", 10, 50), ("X4", 30, 50)],                    # 20 mm
            "DQ2": [("U1", 10, 60), ("X5", 30, 60)]}                     # 20 mm: equal to DQS0, 20 mm short of CK
    k = k_ps_per_mm()
    cons = {"timing": {"T": {"clock": "CK", "data": ["DQ2"], "setup_ps": 5 * k, "hold_ps": 5 * k,
                             "strobe_groups": {"DQS0": ["DQ2"]}}}}
    assert not [x for x in run(tmp_path, nets, cons) if x["code"] == "TIMING_SKEW"]


def test_flyby_order_pass_and_fail(tmp_path):
    nets = {"CK": [("U1", 10, 10), ("D1", 30, 10), ("D2", 50, 10), ("D3", 70, 10)]}   # reached at 20, 40, 60 mm
    ok = {"timing": {"T": {"clock": "CK", "data": ["NONE"], "setup_ps": 1, "hold_ps": 1,
                           "flyby_order": ["D1", "D2", "D3"], "flyby_origin": "U1"}}}
    bad = {"timing": {"T": {"clock": "CK", "data": ["NONE"], "setup_ps": 1, "hold_ps": 1,
                            "flyby_order": ["D1", "D3", "D2"], "flyby_origin": "U1"}}}
    assert not [x for x in run(tmp_path, nets, ok) if x["code"] == "TIMING_FLYBY_ORDER"]
    f = [x for x in run(tmp_path, nets, bad) if x["code"] == "TIMING_FLYBY_ORDER"]
    assert len(f) == 1 and f[0]["level"] == "FAIL" and "D2 at 40.00 mm" in f[0]["msg"] and "D3 at 60.00 mm" in f[0]["msg"]


def test_config_errors():
    with pytest.raises(ValueError):
        TM.groups({"timing": {"T": {"clock": "CK", "data": ["DQ"], "setup_ps": 1}}})       # no hold_ps
    with pytest.raises(ValueError):
        TM.groups({"timing": {"T": {"clock": "", "data": ["DQ"], "setup_ps": 1, "hold_ps": 1}}})
