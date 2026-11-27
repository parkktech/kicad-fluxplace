"""w79: on a re-net / incremental run the route gate baselines on the gaps of the input board (no pcbnew)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import gaps as GP, pipeline as PL

PW = {"id": "power-width:+5V", "kind": "power-width", "class": "power", "nets": ["+5V"], "at": None, "mag": 0.3}


class Ctx:
    def __init__(self, tmp, scope=True):
        self.tmp, self.board_in, self.cons_path, self.cons = str(tmp), "in.kicad_pcb", None, {}
        self.state = {"route_scope": {"input": "in.kicad_pcb", "nets": ["N"]}} if scope else {}

    def dir(self, n, k):
        d = os.path.join(self.tmp, f"{n}-{k}")
        os.makedirs(d, exist_ok=True)
        return d


def _run(tmp, monkeypatch, left, base, scope=True):
    close = {"stop": "flat", "start": {"opens": 0}, "end": {"opens": 0}, "best": "b.kicad_pcb", "left": left, "packets": []}
    monkeypatch.setattr(GP, "close", lambda *a, **k: close)
    monkeypatch.setattr(GP, "gate_measure", lambda *a, **k: {"left": base})
    c = Ctx(tmp, scope)
    return PL._gate_check(c, 12, "route", "b.kicad_pcb", ["power-width"], later=("close",))


def test_only_preexisting_gap_is_a_warn_and_the_flow_continues(tmp_path, monkeypatch):
    g = _run(tmp_path, monkeypatch, [PW], [PW])
    assert g["verdict"] == "warn" and g["gate"]["stop"] == "preexisting" and "pre-existing" in g["line"] and g["gate"]["preexisting"] == [PW]


def test_a_new_gap_stops_the_gate(tmp_path, monkeypatch):
    new = dict(PW, id="power-width:+3V3")
    assert _run(tmp_path, monkeypatch, [PW, new], [PW])["verdict"] == "fail"


def test_a_worse_gap_stops_the_gate(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, [dict(PW, mag=0.9)], [PW])["verdict"] == "fail"


def test_whole_board_run_keeps_the_strict_gate(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, [PW], [PW], scope=False)["verdict"] == "fail"
