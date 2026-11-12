import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import netclass as NC, routediag as RD, preroute as PR
import pcbgen as g


def _three(tmp_path, ux, uy):
    b = (g.fp("J1", 10, 10, [g.pad("1", 0, 0, "N")], locked=True) + g.fp("J2", 90, 10, [g.pad("1", 0, 0, "N")], locked=True)
         + g.fp("R3", ux, uy, [g.pad("1", 0, 0, "N"), g.pad("2", 1, 0, "Z")]))
    return RD.load(g.board(tmp_path, b))


CONS = {"placement": {"budget": {"signal": 30.0}}}


def test_multi_pad_net_with_fixed_subtree_over_budget_is_anchor_spacing(tmp_path):
    B = _three(tmp_path, 50, 10)                       # the movable pad sits on the line: nothing to gain
    f = PR.net_over_budget(B, NC.classify(B, CONS), CONS)
    assert [(x["code"], x["level"]) for x in f] == [("ANCHOR_SPACING", "WARN")]
    assert abs(f[0]["value"] - 80.0) < 0.1 and f[0]["limit"] == 45.0       # floor 80 mm, 30 x (1 + 0.5)
    B = _three(tmp_path, 50, 70)                       # the movable part adds excess: that part still FAILs
    f = PR.net_over_budget(B, NC.classify(B, CONS), CONS)
    assert {x["code"] for x in f} == {"ANCHOR_SPACING", "NET_OVER_BUDGET"}
    assert "fixed parts alone need" in [x for x in f if x["code"] == "NET_OVER_BUDGET"][0]["msg"]


def test_movable_net_over_budget_is_still_a_fail(tmp_path):
    b = g.fp("R1", 10, 10, [g.pad("1", 0, 0, "N")]) + g.fp("R2", 90, 10, [g.pad("1", 0, 0, "N")])
    B = RD.load(g.board(tmp_path, b))
    f = PR.net_over_budget(B, NC.classify(B, CONS), CONS)
    assert [x["code"] for x in f] == ["NET_OVER_BUDGET"]


def test_per_net_override_prints_its_reason(tmp_path):
    b = g.fp("R1", 10, 10, [g.pad("1", 0, 0, "N")]) + g.fp("R2", 90, 10, [g.pad("1", 0, 0, "N")])
    B = RD.load(g.board(tmp_path, b))
    cons = {"placement": {"budget": {"N": {"mm": 50, "reason": "hub is across the board"}}}}
    f, card = PR.run(B, cons)
    over = [x for x in f if x["code"] == "NET_OVER_BUDGET"]
    assert over and "hub is across the board" in over[0]["msg"] and over[0]["limit"] == 50
    assert card["overrides"][0]["net"] == "N" and not card["overrides"][0]["ok"]
    assert "hub is across the board" in PR.format_text(f, card)


def test_defaults_have_physical_basis():
    assert NC.rf_budget_mm(2.4, 0.5) == 17.7 and NC.rf_budget_mm(1.575, 0.5) == 26.9
    for n, k in (("USB_HUB_DP", "usb2"), ("USB3_M2_RX_P", "usb3"), ("PCIE_TX_N", "pcie"), ("ETH_P0_P", "eth"),
                 ("MIC_P", "audio"), ("FOO_P", "other")):
        assert NC.pair_kind(n) == k, n
    assert NC.is_slow("I2C_SDA") and NC.is_slow("HUB_RST_N") and NC.is_slow("CAN2_L") and NC.is_slow("RX_ATT")
    assert not NC.is_slow("CAN_MOSI") and not NC.is_slow("I2S_ADC") and not NC.is_slow("BT_INT_ANT")


def test_oscillator_is_a_clock_crystal_keeps_8mm(tmp_path):
    osc = g.fp("Y1", 20, 10, [g.pad("1", 0, 0, ""), g.pad("2", 1, 0, "GND"), g.pad("3", 2, 0, "MCLK"), g.pad("4", 3, 0, "+3V3")])
    xt = g.fp("Y2", 40, 10, [g.pad("1", 0, 0, "XIN"), g.pad("2", 1, 0, "XOUT")])
    u = g.ic("U1", 30, 30, ["MCLK", "XIN", "XOUT", "GND", "a", "b"])
    B = RD.load(g.board(tmp_path, osc + xt + u))
    cls = NC.classify(B, {})
    assert cls["MCLK"]["budget"] == 20.0 and cls["MCLK"]["reason"] == "active oscillator"
    assert cls["XIN"]["budget"] == 8.0
    cls = NC.classify(B, {"placement": {"budget": {"pair": 80.0, "slow": 500.0}}})
    assert cls["MCLK"]["budget"] == 20.0
