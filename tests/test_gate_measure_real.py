"""Review findings 1 and 8 (REVIEW-W16REF-A): the gate is a measure; an unmeasured pair or a crashed review source is never clean;
gate_measure writes nothing but stop.json.  Real pcbnew + kicad-cli: a board laid by pairs.run; only the failure is injected."""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pytest.importorskip("pcbnew")
assert shutil.which("kicad-cli"), "kicad-cli is required for these tests"
import planboards as PB
from fluxplace import gaps as GP, netlen, pairs as PR
from test_pairs import duo

CONS = {"pairs": {"ETH_": {"skew_mm": 0.5}}, "preroute": {"signal_layers": ["F.Cu"]}}
PAIR_KINDS = list(GP.PAIR_KINDS)


@pytest.fixture
def laid(tmp_path):
    b = PB.write(tmp_path, duo("U1", 20, 20, "ETH_P", "ETH_N") + duo("U2", 60, 25, "ETH_P", "ETH_N"))
    out = str(tmp_path / "laid.kicad_pcb")
    assert PR.run(b, CONS, None, out)["complete"] == 1
    return out


def test_measured_pair_gate_is_clean(laid, tmp_path):
    r = GP.gate_measure(laid, str(tmp_path / "gate"), cons=CONS, kinds=PAIR_KINDS)
    assert r["stop"] == "clean" and r["left"] == [] and r["review_failed"] == []


def test_an_unmeasured_gated_pair_is_an_open_gate(laid, tmp_path, monkeypatch):
    monkeypatch.setattr(netlen, "pair_lengths", lambda *a, **k: None)             # kicad-cli length probe fails
    r = GP.gate_measure(laid, str(tmp_path / "gate"), cons=CONS, kinds=PAIR_KINDS)
    assert r["stop"] == "open" and r["exit"] == 5
    assert [g["kind"] for g in r["left"]] == ["pair-unmeasured"] and r["left"][0]["class"] == "hs-pair"


def test_a_crashed_pair_audit_is_an_open_gate(laid, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("audit died")
    monkeypatch.setattr(GP, "_pair_audit", boom)
    r = GP.gate_measure(laid, str(tmp_path / "gate"), cons=CONS, kinds=PAIR_KINDS)
    assert r["stop"] == "open" and "pairs" in r["review_failed"]


def test_gate_measure_never_overwrites_best_or_the_memo_or_the_input(laid, tmp_path):
    work = tmp_path / "gate"
    work.mkdir()
    (work / "best.kicad_pcb").write_text("REPAIRED BEST")
    (work / "gaps.json").write_text(json.dumps({"memo": [["k"]], "round": 3}))
    before = open(laid, "rb").read()
    GP.gate_measure(laid, str(work), cons=CONS, kinds=PAIR_KINDS)
    assert (work / "best.kicad_pcb").read_text() == "REPAIRED BEST"
    assert json.load(open(work / "gaps.json")) == {"memo": [["k"]], "round": 3}
    assert open(laid, "rb").read() == before                                        # not refilled / saved in place
    assert sorted(os.listdir(work)) == ["best.kicad_pcb", "gaps.json", "stop.json"]


def test_a_stub_over_the_maximum_is_a_gap_not_silence(monkeypatch):
    row = {"p": "U_P", "n": "U_N", "status": "mismatch", "causes": ["stub"], "why": "spur 4.00 > 3.5 mm", "skew": 0.0, "limit": 1.0}
    monkeypatch.setattr(GP, "_pair_audit", lambda *a, **k: (None, {}, [row]))
    monkeypatch.setattr("fluxplace.power.check", lambda *a, **k: [])
    monkeypatch.setattr("fluxplace.boardfile.facts_from_file", lambda b: {})
    monkeypatch.setattr("fluxplace.review.check_pair_geometry", lambda *a, **k: [])
    monkeypatch.setattr("fluxplace.preroute.pin_escape_blocked", lambda *a, **k: [])
    rows, failed = GP.review_gaps("x.kicad_pcb", {"pairs": {"U_": {}}})
    assert "pair-stub" in [r["kind"] for r in rows]
