"""routebench part 1: stage (strip), metrics (helpers monkeypatched; no router, no kicad-cli)."""
import pytest

import pcbgen
import routebench as RB
from fluxplace import corpus as C, gaps

BODY = ('(segment (start 10 10) (end 20 10) (width 0.2) (layer "F.Cu") (net "A") (uuid "11111111-1111-4111-8111-111111111111"))\n'
        '(segment (start 10 12) (end 20 12) (width 0.2) (layer "F.Cu") (net "B") (uuid "22222222-2222-4222-8222-222222222222"))\n'
        '(via (at 15 15) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "A") (uuid "33333333-3333-4333-8333-333333333333"))\n'
        '(zone (net "A") (net_name "A") (layer "F.Cu") (uuid "44444444-4444-4444-8444-444444444444") (hatch edge 0.5)\n'
        '  (polygon (pts (xy 0 0) (xy 50 0) (xy 50 50) (xy 0 50))))\n')


def _ent(path, strip):
    return {"name": "t", "board": path, "bench": {"strip": strip, "task": "whole", "budget_s": 60}}


def test_strip_removes_net_copper_and_keeps_zones(tmp_path):
    src = pcbgen.board(tmp_path, BODY)
    before = open(src).read()
    out = RB.stage(_ent(src, True), str(tmp_path / "work"))
    text = open(out).read()
    assert "(segment" not in text and "(via" not in text
    assert text.count("(zone") == before.count("(zone") == 1
    assert open(src).read() == before        # source board untouched


def test_stage_without_strip_keeps_copper(tmp_path):
    src = pcbgen.board(tmp_path, BODY)
    out = RB.stage(_ent(src, False), str(tmp_path / "work"))
    assert open(out).read().count("(segment") == 2


def test_metrics_fills_every_key_with_constraints(tmp_path, monkeypatch):
    src = pcbgen.board(tmp_path, BODY)
    drc = {"violations": [{"type": "clearance", "severity": "error"}], "unconnected_items": []}
    m = {"opens": 2, "shorts": 0, "errors": 1, "err_keys": {("clearance", "x"): 1}, "drc": drc}
    monkeypatch.setattr(gaps, "measure", lambda b, *a, **k: m)
    monkeypatch.setattr(gaps, "_pair_audit", lambda b, c, o=None: (None, None, [
        {"status": "complete"}, {"status": "half"}, {"status": "open"}]))
    gl = [{"kind": "power-width", "class": "plain", "mag": 0.05}, {"kind": "power-vias", "class": "plain", "mag": 1}, {"kind": "pair-open", "class": "pair", "sev": 3}]
    monkeypatch.setattr(gaps, "classify", lambda d, c, board=None, **k: gl)
    monkeypatch.setattr(C, "_pcb_metrics", lambda p: {"board.vias": 4, "board.tracks": 9, "board.track_mm": 12.5})
    r = RB.metrics(src, {"rf": {}})
    for k in ("opens", "shorts", "errors", "err_types", "pairs", "rails", "score", "vias", "tracks", "track_mm"):
        assert r[k] is not None, k
    assert r["pairs"] == {"complete": 1, "mismatch": 0, "half": 1, "open": 1}
    assert r["rails"] == {"narrow": 1, "vias": 1, "mag": 1.05}
    assert r["score"] == gaps.score(m, gl)
    assert r["err_types"] == [["clearance", 1]]


def test_metrics_without_constraints_nulls_pairs_rails_score(tmp_path, monkeypatch):
    src = pcbgen.board(tmp_path, BODY)
    m = {"opens": 0, "shorts": 0, "errors": 0, "err_keys": {}, "drc": {"violations": [], "unconnected_items": []}}
    monkeypatch.setattr(gaps, "measure", lambda b, *a, **k: m)
    monkeypatch.setattr(C, "_pcb_metrics", lambda p: {})
    r = RB.metrics(src, None)
    assert r["pairs"] is None and r["rails"] is None and r["score"] is None
    assert r["opens"] == 0 and r["vias"] is None


def test_failing_helper_gives_null_not_lost_row(tmp_path, monkeypatch):
    src = pcbgen.board(tmp_path, BODY)
    m = {"opens": 1, "shorts": 0, "errors": 0, "err_keys": {}, "drc": {"violations": [], "unconnected_items": []}}
    monkeypatch.setattr(gaps, "measure", lambda b, *a, **k: m)
    monkeypatch.setattr(gaps, "_pair_audit", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(C, "_pcb_metrics", lambda p: {"board.vias": 0})
    r = RB.metrics(src, {"x": 1})
    assert r["pairs"] is None and r["opens"] == 1 and r["vias"] == 0
