"""tune --match acceptance: DRC violation SET (not count), in-process geometric pre-check
before any DRC run, and per-net DRC run accounting."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import tune as TU

pcbnew = pytest.importorskip("pcbnew")
MM = pcbnew.FromMM


def _trk(b, net, x0, y0, x1, y1, w=0.2):
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(pcbnew.VECTOR2I(MM(x0), MM(y0)))
    t.SetEnd(pcbnew.VECTOR2I(MM(x1), MM(y1)))
    t.SetWidth(MM(w))
    t.SetLayer(pcbnew.F_Cu)
    t.SetNet(net)
    b.Add(t)
    return t


def _two_nets(path, other_y):
    b = pcbnew.BOARD()
    b.SetCopperLayerCount(2)
    a = pcbnew.NETINFO_ITEM(b, "A"); b.Add(a)
    o = pcbnew.NETINFO_ITEM(b, "B"); b.Add(o)
    _trk(b, a, 0, 0, 40, 0)
    _trk(b, o, 0, other_y, 40, other_y)
    b.Save(path)
    return path


def test_set_acceptance_rejects_a_swap_that_keeps_the_count(tmp_path, monkeypatch):
    src = _two_nets(str(tmp_path / "w.kicad_pcb"), 5.0)
    base = (frozenset({("drc_gap", ("a",), 1.0, 1.0)}), (1, 0))

    def after_swap(path, kicad_cli="kicad-cli"):              # one removed, a different one added: same count
        return frozenset({("drc_width", ("b",), 2.0, 2.0)}), (1, 0)
    monkeypatch.setattr(TU, "drc_report", after_swap)
    stats = {}
    ok, _ = TU._guarded_set(src, src, lambda bd: True, "kicad-cli", base, lambda m: None, stats)
    assert ok is False and stats["drc_runs"] == 1


def test_set_acceptance_accepts_when_no_member_is_new(tmp_path, monkeypatch):
    src = _two_nets(str(tmp_path / "w.kicad_pcb"), 5.0)
    base = (frozenset({("drc_gap", ("a",), 1.0, 1.0), ("drc_gap", ("c",), 3.0, 3.0)}), (2, 0))
    monkeypatch.setattr(TU, "drc_report",
                        lambda path, kicad_cli="kicad-cli": (frozenset({("drc_gap", ("a",), 1.0, 1.0)}), (1, 0)))
    ok, (keys, counts) = TU._guarded_set(src, src, lambda bd: True, "kicad-cli", base, lambda m: None, {})
    assert ok is True and counts == (1, 0)


def test_geometric_precheck_rejects_without_a_drc_run(tmp_path, monkeypatch):
    # net B runs 0.5 mm from net A: a 0.6 mm accordion on A must cross it
    src = _two_nets(str(tmp_path / "close.kicad_pcb"), 0.7)
    b = pcbnew.LoadBoard(src)
    stats = {}

    def no_drc(*a, **k):
        raise AssertionError("DRC must not run for a candidate the pre-check rejects")
    monkeypatch.setattr(TU, "drc_report", no_drc)
    got = TU.meander_to(b, "A", 2.0, amp_mm=0.6, period_mm=0.8, clearance_mm=0.127, stats=stats,
                        log=lambda m: None)
    assert got == 0.0 and stats["geom_rejected"] == 1 and stats["geom_checked"] == 1


def test_geometric_precheck_passes_clear_candidate(tmp_path):
    src = _two_nets(str(tmp_path / "far.kicad_pcb"), 5.0)
    b = pcbnew.LoadBoard(src)
    stats = {}
    got = TU.meander_to(b, "A", 2.0, amp_mm=0.6, period_mm=0.8, clearance_mm=0.127, stats=stats,
                        log=lambda m: None)
    assert got > 0 and stats.get("geom_rejected", 0) == 0
