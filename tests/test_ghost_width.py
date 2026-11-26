"""B10: the ghost judge lays rails and RF at contract width (B1 contract when it exists, today's route-step sources otherwise)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import lookahead as LA, netclass as NC, routediag as RD  # noqa: E402
import pcbgen as g  # noqa: E402


def _board(tmp_path):
    return g.board(tmp_path, g.ic("U1", 10, 10, ["RAIL", "SIG", "RFX", "a"]) + g.two("C1", 30, 10, "RAIL", "GND") + g.two("R1", 40, 10, "SIG", "RFX"))


def test_contract_widths_read_the_b1_contract_when_it_exists(tmp_path, monkeypatch):
    fake = {"RFX": {"width": {"F.Cu": 0.30, "B.Cu": 0.34}, "width_min": None},
            "RAIL": {"width": None, "width_min": 0.5},
            "SIG": {"width": None, "width_min": None}}
    monkeypatch.setattr(NC, "contract", lambda B, cons, spec, **kw: fake, raising=False)
    w = LA.contract_widths(_board(tmp_path), {}, None, {"RFX", "RAIL", "SIG"}, ["F.Cu", "B.Cu"])
    assert w == {"RFX": 0.32, "RAIL": 0.5}                  # RF: mean of its layer widths; rail: the minimum; plain: default


def test_contract_widths_fall_back_to_the_route_step_sources_without_b1(tmp_path, monkeypatch):
    # TODO(B1 / w49-netcontract): this test and the fallback go when netclass.contract is merged.
    if hasattr(NC, "contract"):
        monkeypatch.delattr(NC, "contract")
    cons = {"power": {"RAIL": {"current_a": 3.0}}}
    w = LA.contract_widths(_board(tmp_path), cons, None, {"RAIL", "SIG"}, ["F.Cu", "B.Cu"])
    assert "SIG" not in w and (w.get("RAIL") or 0) > 0.15


def test_contract_widths_never_raise(tmp_path):
    assert LA.contract_widths("/nonexistent.kicad_pcb", {}, None, {"X"}) == {}
