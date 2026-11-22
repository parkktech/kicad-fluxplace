from fluxplace import lookahead as LA


def test_lay_legs_gated_only():
    P = LA.params({"lookahead": {"enabled": True}})
    assert P["lay"] == "gated"
    pairs = [("USB3_RX_N", "USB3_RX_P"), ("MIC_N", "MIC_P"), ("PCIE_TX_N", "PCIE_TX_P")]
    assert LA.lay_legs(pairs, {}, P) == ["PCIE_TX_N", "PCIE_TX_P", "USB3_RX_N", "USB3_RX_P"]


def test_lay_legs_all():
    P = LA.params({"lookahead": {"enabled": True, "lay": "all"}})
    assert len(LA.lay_legs([("MIC_N", "MIC_P"), ("ETH_P0_N", "ETH_P0_P")], {}, P)) == 4


def test_lay_bad_value():
    import pytest
    with pytest.raises(ValueError):
        LA.params({"lookahead": {"lay": "some"}})
