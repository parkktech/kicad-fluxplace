"""pairbase: a re-laid pair keeps its own width (baseline / input board above the DRC minimum), never the DRC minimum; PAIR_NARROWED."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import pairbase as PB


def seg(net, w, a=(0, 0), b=(10, 0), layer="F.Cu", y=0.0):
    return {"net": net, "w": w, "a": (a[0], a[1] + y), "b": (b[0], b[1] + y), "layer": layer}


BASE = [seg("E_P", 0.15), seg("E_N", 0.15, y=0.3), seg("E_P", 0.15, a=(10, 0), b=(20, 0))]


def test_modal_width_is_length_weighted_and_ignores_widths_at_or_below_the_floor():
    segs = [seg("E_P", 0.088, b=(30, 0)), seg("E_P", 0.15, b=(5, 0))]
    assert PB.modal_width(segs, {"E_P"}) == 0.088
    assert PB.modal_width(segs, {"E_P"}, above=0.088) == 0.15
    assert PB.modal_width(segs, {"X"}) is None


def test_modal_gap_measures_parallel_legs_edge_to_edge():
    assert abs(PB.modal_gap(BASE, ("E_P", "E_N")) - 0.15) < 1e-6


def test_with_widths_states_the_baseline_width_and_keeps_an_explicit_one(monkeypatch, tmp_path):
    cur = [seg("E_P", 0.088), seg("E_N", 0.088, y=0.3)]
    monkeypatch.setattr(PB, "_segs", lambda p: ((BASE if "base" in p else cur), {}))
    cons = {"pairs": {"E_": {"impedance_diff": 100}}}
    c, used = PB.with_widths(cons, str(tmp_path / "cur.kicad_pcb"), str(tmp_path / "base.kicad_pcb"), [("E_P", "E_N")])
    assert c["pairs"]["E_P"]["width_mm"] == 0.15 and c["pairs"]["E_N"]["width_mm"] == 0.15 and c["pairs"]["E_P"]["impedance_diff"] == 100
    assert "width_mm" not in cons["pairs"]["E_"]                                  # the caller's constraints are not edited
    c2, used2 = PB.with_widths({"pairs": {"E_": {"width_mm": 0.2}}}, "cur", "base", [("E_P", "E_N")])
    assert used2 == {} and c2["pairs"]["E_"]["width_mm"] == 0.2


def test_without_a_baseline_only_widths_above_the_drc_minimum_count(monkeypatch, tmp_path):
    (tmp_path / "cur.kicad_pro").write_text('{"board": {"design_settings": {"rules": {"min_track_width": 0.088}}}}')
    cur = [seg("E_P", 0.088, b=(30, 0)), seg("E_P", 0.15, b=(5, 0))]
    monkeypatch.setattr(PB, "_segs", lambda p: (cur, {}))
    assert PB.widths_for(str(tmp_path / "cur.kicad_pcb"), None, [("E_P", "E_N")])[("E_P", "E_N")][0] == 0.15
    monkeypatch.setattr(PB, "_segs", lambda p: ([seg("E_P", 0.088)], {}))
    assert PB.widths_for(str(tmp_path / "cur.kicad_pcb"), None, [("E_P", "E_N")]) == {}


def test_narrowed_flags_only_update_laid_tracks_thinner_than_the_baseline(monkeypatch):
    cur = BASE + [seg("E_P", 0.088, a=(0, 5), b=(8, 5)), seg("E_N", 0.088, a=(0, 6), b=(8, 6))]
    monkeypatch.setattr(PB, "_segs", lambda p: ((BASE if p == "base" else cur), {}))
    out = PB.narrowed("cur", "base")
    assert out == [("E_P/E_N", 0.088, 0.15, 2)]
    assert PB.findings("cur", "base")[0]["code"] == "PAIR_NARROWED" and PB.findings("cur", "base")[0]["level"] == "FAIL"
    monkeypatch.setattr(PB, "_segs", lambda p: (BASE, {}))
    assert PB.narrowed("cur", "base") == []                                      # nothing new: carried copper is not judged
