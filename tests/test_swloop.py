"""SWLOOP: hot loop and switch node of a switching regulator on a placed, unrouted board."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as g
from fluxplace import swloop as SW


def _regulator(tmp_path, cin_x):
    """U1 (5 pads: VIN, GND, SW x2, GND), L1 SW->VOUT, input cap C1 (VIN, GND) at cin_x."""
    u1 = g.fp("U1", 50, 40, [
        g.pad("1", -2.0, -1.0, "VIN", 0.6, 0.6), g.pad("2", -2.0, 0.0, "GND", 0.6, 0.6),
        g.pad("3", 2.0, -1.0, "SW", 0.6, 0.6), g.pad("4", 2.0, 0.0, "SW", 0.6, 0.6),
        g.pad("5", 0.0, 2.0, "GND", 0.6, 0.6)], name="REG")
    l1 = g.fp("L1", 56, 40, [g.pad("1", -1.0, 0, "SW", 1.6, 1.6), g.pad("2", 1.0, 0, "VOUT", 1.6, 1.6)], name="IND")
    c1 = g.two("C1", cin_x, 39, "VIN", "GND")
    return g.board(tmp_path, u1 + l1 + c1, outline=(0, 0, 100, 80))


def _findings(path, cons=None):
    return SW.check_switch_loops({"board_path": path}, cons or {})


def test_tight_loop_is_info_only_and_far_cap_warns(tmp_path):
    tight_dir, far_dir = tmp_path / "t", tmp_path / "f"
    tight_dir.mkdir(); far_dir.mkdir()
    tight = _findings(_regulator(tight_dir, cin_x=46.0))
    far = _findings(_regulator(far_dir, cin_x=20.0))
    t_info = [f for f in tight if f["code"] == "SWLOOP"]
    f_info = [f for f in far if f["code"] == "SWLOOP"]
    assert len(t_info) == 1 and len(f_info) == 1
    assert t_info[0]["level"] == "INFO" and f_info[0]["level"] == "INFO"
    per = lambda f: float(f["msg"].split("hot loop ")[1].split(" mm")[0])
    assert per(f_info[0]) > per(t_info[0]) + 20          # far cap: the loop is ~58 mm, tight ~6 mm
    assert not [f for f in tight if f["code"] == "SWLOOP_LARGE"]
    large = [f for f in far if f["code"] == "SWLOOP_LARGE"]
    assert len(large) == 1 and large[0]["level"] == "WARN" and "3 x IC diagonal" in large[0]["msg"]


def test_budget_keys_turn_it_into_a_fail(tmp_path):
    (tmp_path / "t").mkdir()
    tight = _findings(_regulator(tmp_path / "t", cin_x=46.0), {"placement": {"hot_loop_max_mm": 10}})
    assert not [f for f in tight if f["code"] == "SWLOOP_LARGE"]   # ~6 mm loop is inside 10 mm
    (tmp_path / "f").mkdir()
    far = _findings(_regulator(tmp_path / "f", cin_x=20.0), {"placement": {"hot_loop_max_mm": 10}})
    fails = [f for f in far if f["code"] == "SWLOOP_LARGE"]
    assert len(fails) == 1 and fails[0]["level"] == "FAIL" and "hot_loop_max_mm" in fails[0]["msg"]


def test_no_regulator_no_findings(tmp_path):
    path = g.board(tmp_path, g.two("R1", 10, 10, "A", "B"), outline=(0, 0, 100, 80))
    assert _findings(path) == []
