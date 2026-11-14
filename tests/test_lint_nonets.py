"""fix-c #13: a spec without nets and no board gives a clear error, not an AttributeError."""
import pytest
from fluxplace import ercnet as EN


def test_run_none_returns_clean_error_finding():
    out = EN.run(None, None, board_facts=None)
    assert out and out[0]["severity"] == "error" and out[0]["code"] == "ERC_NO_NETS"
    assert "--board" in out[0]["msg"]


def test_run_spec_without_nets_returns_clean_error_finding():
    out = EN.run({"components": []}, None, board_facts=None)
    assert out and out[0]["code"] == "ERC_NO_NETS"
