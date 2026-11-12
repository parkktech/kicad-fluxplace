import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import netclass as NC, routediag as RD
import pcbgen as g


def mk(tmp_path):
    b = ""
    # buck: U1 SW -> L1, C1 on VOUT; bypass cap on U2 supply; pair; crystal; rf
    b += g.ic("U1", 20, 20, ["VIN", "U1_SW", "GND", "FB", "VCC"])
    b += g.two("L1", 28, 20, "U1_SW", "VOUT")
    b += g.two("C1", 20, 30, "VCC", "GND")
    b += g.ic("U2", 60, 20, ["V3", "SIG1", "SIG2", "GND", "XI", "XO"])
    b += g.two("C2", 60, 30, "V3", "GND")
    b += g.two("Y1", 70, 30, "XI", "XO")
    b += g.two("R1", 40, 50, "USB_DP", "USB_DM")
    b += g.two("R2", 80, 60, "USB_DP", "x1") + g.two("R3", 82, 60, "USB_DM", "x2")
    b += g.two("R4", 10, 60, "RF_ANT1", "x3") + g.two("R5", 11, 60, "RF_ANT1", "x4")
    for i in range(8):
        b += g.two(f"R{10+i}", 5 + 3 * i, 70, "VDD", "n%d" % i)
    b += g.two("R30", 30, 70, "SIG1", "Z") + g.two("R31", 50, 70, "SIG2", "Z")
    return g.board(tmp_path, b)


def test_classes(tmp_path):
    B = RD.load(mk(tmp_path))
    c = NC.classify(B, {"power": {"VDD": {}}})
    assert c["U1_SW"]["class"] == "switch_node" and c["U1_SW"]["budget"] == 6.0
    assert c["VCC"]["class"] == "bypass" and c["V3"]["class"] == "bypass"
    assert c["GND"]["class"] == "rail" and c["GND"]["weight"] == 0
    assert c["USB_DP"]["class"] == "pair" and c["USB_DP"]["partner"] == "USB_DM"
    assert c["RF_ANT1"]["class"] == "rf"
    assert c["XI"]["class"] == "clock"
    assert c["VDD"]["class"] == "rail"
    assert c["SIG1"]["class"] == "signal" and c["SIG1"]["weight"] == 1.0


def test_overrides(tmp_path):
    B = RD.load(mk(tmp_path))
    cons = {"placement": {"budget": {"signal": 10}, "weight": {"signal": 2},
                          "net": {"SIG2": {"budget": 99, "weight": 7}, "USB_DP": {"class": "signal"}}}}
    c = NC.classify(B, cons)
    assert c["SIG1"]["budget"] == 10 and c["SIG1"]["weight"] == 2
    assert c["SIG2"]["budget"] == 99 and c["SIG2"]["weight"] == 7
    assert c["USB_DP"]["class"] == "signal" and c["USB_DM"]["class"] == "pair"


def test_spec_pin_name_marks_switch_node(tmp_path):
    b = g.ic("U1", 20, 20, ["VIN", "N7", "GND", "FB", "VCC"]) + g.two("L1", 28, 20, "N7", "VOUT")
    B = RD.load(g.board(tmp_path, b))
    assert NC.classify(B)["N7"]["class"] == "signal"
    spec = {"components": [{"ref": "U1", "pinmap": {"SW": "2"}}]}
    assert NC.classify(B, None, spec)["N7"]["class"] == "switch_node"


def test_mst_and_budget():
    assert abs(NC.mst_length([(0, 0), (3, 0), (3, 4)]) - 7) < 1e-9
    assert NC.effective_budget({"budget": 10, "pads": 4}) == 20
    assert NC.effective_budget({"budget": None, "pads": 4}) is None
