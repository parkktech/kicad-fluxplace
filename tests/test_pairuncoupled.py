"""PAIR_ROUTED_UNCOUPLED: a pair the router closed as two ordinary nets is connected, flagged, and measured."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import pairuncoupled as PU


def board(dy):
    pad = lambda net, x, y: {"net": net, "x": x, "y": y, "w": 0.4, "h": 0.4, "layers": ["F.Cu"], "ref": "U", "num": "1"}
    seg = lambda net, y0, y1: {"net": net, "a": (0.0, y0), "b": (20.0, y1), "layer": "F.Cu", "w": 0.15}
    return {"copper": ["F.Cu", "B.Cu"], "pads": [pad("D_P", 0, 0), pad("D_P", 20, 0), pad("D_N", 0, dy), pad("D_N", 20, dy)],
            "segs": [seg("D_P", 0, 0), seg("D_N", dy, dy)], "vias": []}


def test_far_apart_legs_are_uncoupled_and_flagged():
    m = PU.measure(board(5.0), ("D_P", "D_N"), 0.15, 0.2, 5.0)
    assert m["connected"] and m["uncoupled_mm"] == [20.0, 20.0] and m["skew"] == 0.0 and m["length"] == [20.0, 20.0]
    assert PU.flagged([m]) == [m] and "PAIR_ROUTED_UNCOUPLED D_P/D_N" in PU.line(m) and "uncoupled P 20.0" in PU.line(m)


def test_coupled_legs_are_not_flagged():
    m = PU.measure(board(0.35), ("D_P", "D_N"), 0.15, 0.2, 5.0)
    assert m["uncoupled_mm"] == [0.0, 0.0] and PU.flagged([m]) == []


def test_a_bounded_uncoupled_end_is_within_the_limit():
    m = PU.measure(board(0.35), ("D_P", "D_N"), 0.15, 0.2, 5.0)
    m["uncoupled_mm"] = [6.0, 4.0]                                        # two ends of at most 5 mm each
    assert PU.flagged([m]) == []
