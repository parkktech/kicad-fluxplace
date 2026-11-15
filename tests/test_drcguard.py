"""Shared DRC guard (fluxplace/drcguard.py): multiset-of-keys acceptance, position-free
identity, zone-violation exclusion, and guard_board accepting a count rise with no new key."""
import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import drcguard as DG
from fluxplace import tune as TU

pcbnew = pytest.importorskip("pcbnew")
MM = pcbnew.FromMM


def _v(typ, *descs, x=0.0, y=0.0):
    return {"type": typ, "items": [{"description": d, "pos": {"x": x, "y": y}} for d in descs]}


def test_identity_ignores_position_and_decimal_measurements():
    a = DG.key_of(_v("track_dangling", "Track [N1] on F.Cu, length 10.0000 mm", x=1, y=1))
    b = DG.key_of(_v("track_dangling", "Track [N1] on F.Cu, length 9.7000 mm", x=10.3, y=10))
    assert a == b


def test_identity_keeps_integers_that_name_the_violation():
    assert DG.key_of(_v("clearance", "Pad 1 of U3")) != DG.key_of(_v("clearance", "Pad 2 of U3"))


def test_second_instance_of_an_existing_key_is_new():
    base = Counter({("track_dangling", ("Track [N1]",)): 1})
    cand = Counter({("track_dangling", ("Track [N1]",)): 2})
    assert DG.new_keys(base, cand) == Counter({("track_dangling", ("Track [N1]",)): 1})


def test_removed_keys_and_moved_keys_are_not_new():
    base = Counter({("a", ("x",)): 1, ("b", ("y",)): 1})
    assert not DG.new_keys(base, Counter({("a", ("x",)): 1}))
    assert not DG.new_keys(base, Counter({("a", ("x",)): 1, ("b", ("y",)): 1}))


def test_a_different_violation_is_new_even_when_the_count_is_equal():
    base = Counter({("drc_gap", ("a",)): 1})
    assert DG.new_keys(base, Counter({("drc_width", ("b",)): 1})) == Counter({("drc_width", ("b",)): 1})


def test_zone_violations_are_excluded_from_keys_only():
    d = {"violations": [_v("clearance", "Zone GND [GND]"), _v("clearance", "Pad 1 of U3")],
         "unconnected_items": []}
    keys, counts = DG.keys_from_report(d)
    assert sum(keys.values()) == 1 and counts == (2, 0)


def _board(path):
    b = pcbnew.BOARD()
    b.SetCopperLayerCount(2)
    n = pcbnew.NETINFO_ITEM(b, "A")
    b.Add(n)
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(pcbnew.VECTOR2I(MM(0), MM(0)))
    t.SetEnd(pcbnew.VECTOR2I(MM(40), MM(0)))
    t.SetWidth(MM(0.2))
    t.SetLayer(pcbnew.F_Cu)
    t.SetNet(n)
    b.Add(t)
    b.Save(path)
    return path


def test_guard_board_accepts_a_count_rise_with_no_new_key(tmp_path):
    src = _board(str(tmp_path / "w.kicad_pcb"))
    base = (Counter({("drc_gap", ("a",)): 1}), (1, 0))
    accepted, cur = DG.guard_board(src, src, lambda bd: True, base, "kicad-cli", lambda m: None,
                                   report=lambda p, c: (Counter({("drc_gap", ("a",)): 1}), (3, 0)))
    assert accepted is True and cur[1] == (3, 0)


def test_guard_board_rejects_a_new_key_and_leaves_the_work_file(tmp_path):
    src = _board(str(tmp_path / "w.kicad_pcb"))
    before = open(src, "rb").read()
    base = (Counter({("drc_gap", ("a",)): 1}), (1, 0))
    accepted, _ = DG.guard_board(src, src, lambda bd: True, base, "kicad-cli", lambda m: None,
                                 report=lambda p, c: (Counter({("drc_width", ("b",)): 1}), (1, 0)))
    assert accepted is False and open(src, "rb").read() == before


def test_tune_pairs_guard_uses_the_shared_rule(tmp_path, monkeypatch):
    src = _board(str(tmp_path / "w.kicad_pcb"))
    monkeypatch.setattr(TU, "drc_report", lambda p, kicad_cli="kicad-cli": (Counter({("drc_gap", ("a",)): 1}), (5, 0)))
    ok, _ = TU._guarded(src, src, lambda bd: True, "kicad-cli", (Counter({("drc_gap", ("a",)): 1}), (1, 0)),
                        lambda m: None)
    assert ok is True
