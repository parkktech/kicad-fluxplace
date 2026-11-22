"""PLACETOUR stage: judge + blame (P1), move generator (P2), tier-1 gates (P3), tournament stage (P4), report (P5). No pcbnew, no router."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import placetour as PT  # noqa: E402

REVIEW = {
    "open_nets": [{"net": "GND", "poured": True, "disconnected_pad_parts": ["C1"], "parts": ["C1"]},
                  {"net": "SIG1", "poured": False, "disconnected_pad_parts": ["U1", "R2"], "parts": ["U1", "R2"]},
                  {"net": "SIG2", "poured": False, "disconnected_pad_parts": [], "parts": ["R3"]}],
    "most_vias": [{"net": "SIG1", "pads": 3, "vias": 4, "detour": 1.2, "parts": ["U1", "R2"]}],
    "longest_detours": [{"net": "SIG1", "pads": 3, "vias": 1, "detour": 2.0, "parts": ["R2", "R9"]},
                        {"net": "SIG3", "pads": 2, "vias": 0, "detour": 1.6, "parts": ["R4"]}],
    "congested_areas": [{"box": [1.0, 2.0, 3.0, 4.0], "cells": 6, "peak": 0.8, "mean": 0.6, "vias": 2,
                         "parts": ["U1", "C5"], "nets": ["SIG1"]}],
    "turned_parts": [{"ref": "R7", "x": 10.0, "y": 5.0, "rot": 90, "saves_mm": 1.5}],
    "counts": {},
}


def test_blame_parses_review_fixture():
    b = PT.blame_of(REVIEW)
    assert b["source"] == "route_review"
    assert b["open"] == [{"net": "SIG1", "refs": ["R2", "U1"]}, {"net": "SIG2", "refs": ["R3"]}]    # poured GND left out
    nets = {c["net"]: c for c in b["costly"]}
    assert nets["SIG1"]["vias"] == 4 and nets["SIG1"]["detour"] == 2.0 and nets["SIG1"]["refs"] == ["R2", "R9", "U1"]
    assert [c["net"] for c in b["costly"]] == ["SIG1", "SIG3"]                                      # sorted by detour
    assert b["hot"] == [{"cells": 6, "occ": 0.8, "refs": ["C5", "U1"], "nets": ["SIG1"], "bbox": [1.0, 2.0, 3.0, 4.0], "vias": 2}]
    assert b["turned"] == [{"ref": "R7", "x": 10.0, "y": 5.0, "rot": 90, "saves": 1.5}]


def test_judge_key_orders_by_opens_then_hot_then_vias(monkeypatch):
    vias = {"a": 5, "b": 2, "c": 2}
    monkeypatch.setattr(PT.RD, "load", lambda p: {"vias": [0] * vias[p]})
    base = {"open": [{"net": "N", "refs": []}], "costly": [{"detour": 1.0}], "hot": [{"occ": 0.5}], "turned": []}
    more_open = dict(base, open=base["open"] * 2)
    more_hot = dict(base, hot=[{"occ": 0.5}, {"occ": 0.6}])
    assert PT.judge_key("a", base) < PT.judge_key("a", more_open)                                   # opens dominate
    assert PT.judge_key("b", base) < PT.judge_key("b", more_hot)                                    # then hot areas
    assert PT.judge_key("b", base) < PT.judge_key("a", base)                                        # then vias
    assert PT.judge_key(None, base) > PT.judge_key("a", more_open)                                  # no routed copy: worst


def test_judge_falls_back_to_predictor_without_routed_copy(monkeypatch, tmp_path):
    class A:
        router_dir, router_py = "/nowhere", "/usr/bin/python3"
    monkeypatch.setattr(PT.LA, "lean_krt", lambda a, board, outdir, budget: None)
    monkeypatch.setattr(PT, "_predictor_blame", lambda board, cons, spec: {"open": [], "costly": [], "hot": [], "turned": [],
                                                                         "source": "planroute"})
    j = PT.judge(A(), "in.kicad_pcb", str(tmp_path / "unused"), PT.params({}))
    assert j["routed"] is None and j["source"] == "planroute"


def test_judge_uses_route_review_blame_when_routed(monkeypatch, tmp_path):
    class A:
        router_dir, router_py = "/nowhere", "/usr/bin/python3"
    monkeypatch.setattr(PT.LA, "lean_krt", lambda a, board, outdir, budget: "/x/routed.kicad_pcb")
    monkeypatch.setattr(PT, "_review", lambda a, routed, rev: PT.blame_of(REVIEW))
    j = PT.judge(A(), "in.kicad_pcb", str(tmp_path / "r1"), PT.params({"placetour": {"judge_min": 2}}))
    assert j["routed"] == "/x/routed.kicad_pcb" and j["source"] == "route_review"


def test_params_defaults_and_enable():
    P = PT.params({})
    assert P["enabled"] is False and P["max_rounds"] == 10 and P["judge_top"] == 4 and P["move_routed"] is True
    assert PT.params({"placetour": {"enabled": True, "max_rounds": "3"}})["max_rounds"] == 3
