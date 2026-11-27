"""w109: a rail load of unknown current is a
kickoff finding (power.unknown_loads, readychecks.load_current_line)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fluxplace import power as PW  # noqa: E402


def test_unknown_load_is_named_by_the_kickoff_check(tmp_path):
    pytest.importorskip("pcbnew")
    from test_power_ladder import _br_board, CONS_BR
    from fluxplace import readychecks as KO
    p = _br_board(tmp_path, extra_pad=("U3", 14, 4, "VB"))
    assert PW.unknown_loads(p, CONS_BR, layers=2) == {"VB": ["U3"]}
    line = KO.load_current_line(p, CONS_BR)
    assert line["status"] == "WARN" and "U3" in line["detail"]
    ok = KO.load_current_line(_br_board(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path), CONS_BR)
    assert ok["status"] == "PASS"


# ---- the plane as a resistive sheet in the rail solve -------------------------------------------------------------

def _thin_trace_rail():
    segs = [{"layer": "F.Cu", "x1": 0.0, "y1": 0.0, "x2": 20.0, "y2": 0.0, "width": 0.15, "t_mm": 0.035}]
    pads = [{"ref": "U1", "x": 0.0, "y": 0.0, "layers": {"F.Cu"}, "box": None, "role": "source", "amps": None},
            {"ref": "U2", "x": 20.0, "y": 0.0, "layers": {"F.Cu"}, "box": None, "role": "load", "amps": 1.0}]
    return segs, [(0.0, 0.0, 0.3), (20.0, 0.0, 0.3)], pads


def test_trace_in_parallel_with_a_plane_carries_only_its_small_share():
    from fluxplace import powerflow as PF
    segs, vias, pads = _thin_trace_rail()
    alone = PF.solve(segs, [], pads, cap=1.0)["flow"][0]
    assert alone["amps"] == pytest.approx(1.0) and alone["mode"] == "tree"          # no plane: the tree solver, the trace carries the load
    plane = {"layer": "In1.Cu", "t_mm": 0.035, "contains": lambda x, y: True}
    both = PF.solve(segs, vias, pads, zones=[plane], cap=1.0)["flow"][0]
    # trace 0.0655 ohm vs plane ~0.65 mohm: ~1 % of the amp
    assert both["amps"] < 0.03 and both["mode"] == "mesh-solve"


def test_plane_islands_are_not_joined():
    from fluxplace import powerflow as PF
    segs, vias, pads = _thin_trace_rail()
    plane = {"layer": "In1.Cu", "t_mm": 0.035, "contains": lambda x, y: True, "island": lambda x, y: 0 if x < 10 else 1}
    both = PF.solve(segs, vias, pads, zones=[plane], cap=1.0)["flow"][0]
    assert both["amps"] == pytest.approx(1.0, rel=0.01)                              # two islands: the trace is the only path
