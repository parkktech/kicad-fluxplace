"""patch._new_violation_points: bad (x, y) mm points from a kicad-cli DRC dict for the new violation keys."""
import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("pcbnew")
from fluxplace import drcguard as DG
from fluxplace.patch import _new_violation_points


def _drc():
    return {
        "violations": [
            {"type": "clearance", "severity": "error", "items": [
                {"description": "Track [GND] on F.Cu", "pos": {"x": 1.5, "y": 2.5}},
                {"description": "Pad 1 [VCC]", "pos": {"x": 3.0, "y": 4.0}}]},
            {"type": "hole_clearance", "severity": "error", "items": [
                {"description": "Via [X]", "pos": {"x": 9.0, "y": 9.0}}]},
        ],
        "unconnected_items": [
            {"type": "unconnected_items", "items": [
                {"description": "Pad [A]", "pos": {"x": 7.0, "y": 8.0}}]},
        ],
    }


def test_only_new_violation_items_return_points():
    d = _drc()
    new_v = Counter({DG.key_of(d["violations"][0]): 1})
    assert _new_violation_points(d, new_v) == [(1.5, 2.5), (3.0, 4.0)]


def test_unconnected_key_uses_u_prefix_and_is_matched():
    d = _drc()
    new_v = Counter({DG.key_of(d["unconnected_items"][0], "U"): 1})
    assert _new_violation_points(d, new_v) == [(7.0, 8.0)]


def test_no_new_keys_gives_no_points():
    assert _new_violation_points(_drc(), Counter()) == []
