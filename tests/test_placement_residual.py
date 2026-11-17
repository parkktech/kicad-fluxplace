import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import placementcheck as PC, pipeline as P


def test_residual_groups_all_placement_kinds_without_truncation(monkeypatch):
    import fluxplace.routediag as RD
    import fluxplace.legality as LE
    monkeypatch.setattr(RD, "load", lambda board: {})
    monkeypatch.setattr(LE, "Legal", lambda B, cons: types.SimpleNamespace(issues=lambda: {
        "outside": ["U12", "U13"], "overlap": ["U1/U2"], "pad_short": ["U3.1"], "hole": ["H1"]}))
    checked = {"rules": [
        {"kind": "keepout", "name": "module-region", "violations": [{"ref": f"U{i}"} for i in range(14)]},
        {"kind": "no_tht", "name": "card", "violations": [{"ref": "J1", "pad": "1"}]},
        {"kind": "hardware", "name": "H1", "violations": [{"ref": "U4"}]},
        {"kind": "keepout", "name": "empty", "violations": []}],
        "under": [{"module": "card", "side": "F", "parts": [{"ref": "U5", "verdict": "too_tall"}, {"ref": "U6", "verdict": "ok"}]}],
        "quadrants": {}}
    monkeypatch.setattr(PC, "compute", lambda B, cons: checked)
    r = PC.residual("synthetic", {})
    assert len(r["violations"]["keep-out"][0]["items"]) == 14
    assert r["violations"]["outside"] == ["U12", "U13"]
    assert r["violations"]["no_tht"][0]["items"][0]["pad"] == "1"
    assert r["violations"]["module"][0]["items"] == [{"ref": "U5", "verdict": "too_tall"}]
    assert not r["ok"] and len(PC.residual_lines(r)) > 15


def test_failed_stage_result_and_log_share_complete_residual_lists(tmp_path, monkeypatch):
    board = tmp_path / "board.kicad_pcb"
    board.write_text("synthetic")
    rows = {"outside": ["U1", "U2"], "keep-out": [{"rule": "r", "items": [{"ref": "U3"}]}], "no_tht": [],
            "hardware": [], "module": [], "overlap": [], "pad_short": [], "hole": []}
    monkeypatch.setattr(PC, "residual", lambda board, cons: {"violations": rows})
    (tmp_path / "stage.log").write_text("original failure\n")
    c = types.SimpleNamespace(cons={})
    res = {"verdict": "fail", "line": "floorplan: FAIL", "board": str(board)}
    out = P._residual_fail(res, c, str(tmp_path))
    assert out["residual_violations"] == rows
    text = (tmp_path / "stage.log").read_text()
    assert '"U1"' in text and '"U2"' in text and '"U3"' in text
    saved = json.load(open(tmp_path / "placement-residual.json"))
    assert saved["violations"]["outside"] == ["U1", "U2"]
