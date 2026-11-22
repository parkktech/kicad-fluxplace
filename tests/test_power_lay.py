"""PHASE3I B4: a [power] net is laid at its required width (lay default, refusal below it) and session_commit judges R6
through the close guard (G._accept, real) in repair mode; only the measure is stubbed. Uses the gap_board fixture of test_handroute (kicad-cli, no router)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pcbnew = pytest.importorskip("pcbnew")
from fluxplace import handroute as H, gaps as G
from test_handroute import Brd, LOWER

@pytest.fixture
def cons_path(tmp_path):
    p = tmp_path / "cons.toml"
    p.write_text("[power.B]\nwidth_mm = 0.2\n")
    return str(p)


def test_power_net_refused_below_required_width_and_laid_at_it(tmp_path, cons_path):
    B = Brd()                                       # an open board: B1 -> B2 is the only copper, so 0.2 mm fits
    B.edge(-2, -8, 22, 8)
    B.pad("B1", "B", 4, -3)
    B.pad("B2", "B", 16, -3)
    p = B.save(str(tmp_path))
    work = str(tmp_path / "work.kicad_pcb")
    H.session_begin(p, work)
    r = H.lay(work, "B", LOWER, width=0.15, constraints=cons_path)
    assert not r["accepted"] and "below the 0.2 mm" in r["reason"]
    r = H.lay(work, "B", LOWER, constraints=cons_path)          # no width: the required width (0.2) is used
    assert r["accepted"], r.get("reason")


def test_session_commit_repair_judges_r6_through_close_guard(tmp_path, monkeypatch):
    B = Brd()
    B.edge(-2, -8, 22, 8)
    B.pad("B1", "B", 4, -3)
    B.pad("B2", "B", 16, -3)
    p = B.save(str(tmp_path))
    work = str(tmp_path / "work.kicad_pcb")
    H.session_begin(p, work)
    assert H.lay(work, "B", LOWER, width=0.25)["accepted"]      # a real gain: the R2/R4 guard passes, R6 is the judge
    real = G._remeasure

    def measure(path, kicad_cli="kicad-cli"):              # the candidate carries one more power gap than the start
        m = real(path, kicad_cli)
        m["gaps"] = list(m.get("gaps") or [])
        if os.path.basename(path) == "cand.kicad_pcb":
            m["gaps"] = m["gaps"] + [{"id": "pw1", "kind": "power-width", "class": "power", "nets": ["B"], "at": None,
                                            "sev": 0, "mag": 0}]
        return m
    monkeypatch.setattr(G, "_remeasure", measure)
    monkeypatch.setattr(G, "_classify_into", lambda *a, **k: None)
    c = H.session_commit(work, str(tmp_path / "out.kicad_pcb"))
    assert not c["accepted"] and any(x.startswith("R6 power worse") for x in c["reasons"])
