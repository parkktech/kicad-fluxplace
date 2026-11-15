"""Sealed vs surface-only blocked pins, the fan-out verdict for each, and --allow-incomplete past a sealed-pin FAIL.
Synthetic boards (tests/planboards.py); fan-out needs /usr/bin/python3 with pcbnew."""
import argparse
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import fanout as FO
from fluxplace import pipeline as PL
from test_pipeline import _args, _board

HAVE = os.path.exists("/usr/bin/python3") and subprocess.run(
    ["/usr/bin/python3", "-c", "import pcbnew"], capture_output=True).returncode == 0
pytestmark = pytest.mark.skipif(not HAVE, reason="pcbnew not available")


def args(**kw):
    d = dict(track=None, clearance=None, via=None, drill=None, via_pad_gap=0.2, reach=2.2, lane=1.0, max_dom=60, nodes=200000,
             repairs=12, check_only=False, no_breakout=True)   # these tests exercise the generic solver; breakout: test_breakout.py
    d.update(kw)
    return argparse.Namespace(**d)


def sealed_body():
    """6-pin fine-pitch part under a keep-out that forbids tracks and vias over the whole part and its surroundings."""
    nets = ["S1", "S2", "S3", "S4", "S5", "S6"]
    pads = [PB.pad(str(i + 1), (i % 3 - 1) * 0.35, (1 if i >= 3 else -1), n, 0.15, 0.65) for i, n in enumerate(nets)]
    return (PB.fp("U1", 40, 40, pads, court=(-1.2, -1.6, 1.2, 1.6)) + PB.keepout(36, 35, 44, 45, "block")
            + PB.fp("J1", 60, 20, [PB.pad(str(i + 1), 0, i * 2.0, n, 1.2, 1.2) for i, n in enumerate(nets)]))


def run_case(tmp, cons):
    path = PB.write(tmp, sealed_body())
    B, res = FO.run(path, cons, None, str(tmp / "out.kicad_pcb"), args(), None)
    return res, FO.summarise(B, res)


def test_sealed_pins_are_classified_and_fail(tmp_path):
    res, per = run_case(tmp_path, {})
    assert all(p["class"] == "SEALED" and p["free_mm"] == 0.0 for p in res["pins"] if not p["ok"])
    assert len(per["U1"]["blocked"]) == 6 and not per["U1"]["surface"]
    assert FO.is_blocked_only("fanout: FANOUT_FAIL - 0/6\nFANOUT_PIN_BLOCKED U1.1 S1 keepout\n")


def test_surface_only_pins_warn_not_fail(tmp_path):
    # the same pins with an exit threshold of 0 mm: any free surface run counts as a surface exit
    res, per = run_case(tmp_path, {"escape": {"surface_exit_mm": 0.0}})
    assert all(p["class"] == "SURFACE_ONLY" for p in res["pins"] if not p["ok"])
    assert len(per["U1"]["surface"]) == 6 and not per["U1"]["blocked"]


def test_fail_text_with_drc_or_crash_is_not_blocked_only():
    assert not FO.is_blocked_only("fanout: FANOUT_DRC - x\n")
    assert not FO.is_blocked_only("Traceback (most recent call last):\nfanout: FANOUT_FAIL - x\n")


def test_stage_reports_blocked_only_and_warn_for_surface(tmp_path, monkeypatch):
    b = _board(tmp_path)
    a = _args(tmp_path, b)
    c = PL.Ctx(a, a.workdir, b, {})
    out = os.path.join(c.dir(4, "fanout"), "board.kicad_pcb")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w").write(open(b).read())
    monkeypatch.setattr(PL, "_run_cli", lambda argv, log: (1, "fanout: FANOUT_FAIL - 1/2 required pins escaped\n"
                                                              "FANOUT_PIN_BLOCKED U1.1 N x [SEALED, free run 0 mm]\n"))
    r = PL.st_fanout(c, 4, b)
    assert r["verdict"] == "fail" and r.get("blocked_only") is True
    monkeypatch.setattr(PL, "_run_cli", lambda argv, log: (0, "fanout: FANOUT_WARN - 1/1\nFANOUT_PIN_SURFACE_ONLY U1.1 N free run 1.2 mm\n"))
    r = PL.st_fanout(c, 4, b)
    assert r["verdict"] == "warn" and not r.get("blocked_only")


def test_allow_incomplete_continues_past_sealed_fail_and_records_it(tmp_path, monkeypatch):
    b = _board(tmp_path)
    a = _args(tmp_path, b, allow_incomplete=True, from_stage="fanout", to_stage="pairs", flow="constructive")
    seen = []

    def fake_fanout(c, n, board):
        out = os.path.join(c.dir(n, "fanout"), "board.kicad_pcb")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "w").write(open(board).read())
        return {"verdict": "fail", "line": "fanout: FANOUT_FAIL - 1/2", "board": out, "blocked_only": True}

    def fake_pairs(c, n, board):
        seen.append(board)
        return {"verdict": "pass", "line": "pairs ok", "board": board}
    monkeypatch.setitem(PL.FUNCS, "fanout", fake_fanout)
    monkeypatch.setitem(PL.FUNCS, "pairs", fake_pairs)
    rc = PL.run(a)
    assert seen, "pairs must run after a sealed-pin fanout FAIL under --allow-incomplete"
    ledger = open(os.path.join(a.workdir, "LEDGER.txt")).read()
    assert "passed by --allow-incomplete: fanout" in ledger
    assert rc == 0


def test_without_allow_incomplete_a_sealed_fail_still_stops(tmp_path, monkeypatch):
    b = _board(tmp_path)
    a = _args(tmp_path, b, allow_incomplete=False, from_stage="fanout", to_stage="pairs", flow="constructive")
    seen = []

    def fake_fanout(c, n, board):
        out = os.path.join(c.dir(n, "fanout"), "board.kicad_pcb")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "w").write(open(board).read())
        return {"verdict": "fail", "line": "fanout: FANOUT_FAIL - 1/2", "board": out, "blocked_only": True}
    monkeypatch.setitem(PL.FUNCS, "fanout", fake_fanout)
    monkeypatch.setitem(PL.FUNCS, "pairs", lambda c, n, board: seen.append(board) or {"verdict": "pass", "board": board})
    rc = PL.run(a)
    assert not seen and rc == 1


# ---- fixes-d #5: SURFACE_ONLY stays WARN by default, is strict on request, and is checked after routing
def test_surface_only_strict_config_restores_fail(tmp_path):
    cons = {"escape": {"surface_exit_mm": 0.0}, "fanout": {"surface_only": "fail"}}
    path = PB.write(tmp_path, sealed_body())
    B, res = FO.run(path, cons, None, str(tmp_path / "out.kicad_pcb"), args(), None)
    per = FO.summarise(B, res, FO.strict_surface(cons))
    assert len(per["U1"]["blocked"]) == 6 and not per["U1"]["surface"]
    assert FO.strict_surface({}) is False and FO.strict_surface({"fanout": {"surface_only": "warn"}}) is False


def _pad_board(segs=(), vias=()):
    """minimal routed-board model for preroute.surface_findings: one pad U1.1 on net N at (10,10), 1x1 mm."""
    return {"pads": [{"ref": "U1", "num": "1", "net": "N", "x": 10.0, "y": 10.0, "ex": 0.5, "ey": 0.5, "w": 1.0, "h": 1.0, "shape": "rect", "angle": 0.0}],
            "segs": list(segs), "vias": list(vias), "parts": {}}


def test_surface_pin_unrouted_after_routing_is_fail(tmp_path):
    from fluxplace import preroute as PR
    pins = [{"ref": "U1", "pin": "1", "net": "N"}]
    f = PR.surface_findings(_pad_board(), pins, routed=True)
    assert [x["code"] for x in f] == ["FANOUT_PIN_UNROUTED"] and f[0]["level"] == "FAIL"
    # the same pin with a track of its net ending on the pad is connected: INFO, not FAIL
    seg = {"net": "N", "w": 0.2, "a": (10.0, 10.0), "b": (14.0, 10.0), "layer": "F.Cu"}
    f = PR.surface_findings(_pad_board([seg]), pins, routed=True)
    assert [x["level"] for x in f] == ["INFO"]


def test_route_ready_lists_surface_pins_as_warn(tmp_path):
    from fluxplace import preroute as PR
    pins = [{"ref": "U1", "pin": "1", "net": "N"}]
    f = PR.surface_findings(_pad_board(), pins, routed=False)
    assert [x["level"] for x in f] == ["WARN"] and "route pending" in f[0]["msg"]


def test_surface_pins_read_from_fanout_sidecar(tmp_path):
    import json
    from fluxplace import preroute as PR
    board = str(tmp_path / "b.kicad_pcb")
    open(board, "w").write("")
    json.dump({"surface": [{"ref": "U1", "pin": 1, "net": "N", "free_mm": 1.2}]}, open(tmp_path / "b.fanout.json", "w"))
    assert PR.surface_pins(board) == ([{"ref": "U1", "pin": "1", "net": "N"}], False)
    json.dump({"pins": [{"ref": "U1", "pin": "1", "net": "N"}]}, open(tmp_path / "b.surface.json", "w"))
    assert PR.surface_pins(board) == ([{"ref": "U1", "pin": "1", "net": "N"}], True)


# ---- fixes-d #9: a track that crosses a pad (no end on it) counts as on the pad
def test_track_crossing_a_pad_without_ending_in_it_is_on_pad():
    from fluxplace import preroute as PR
    pad = {"ref": "U1", "num": "1", "net": "N", "x": 10.0, "y": 10.0, "ex": 0.5, "ey": 0.5, "w": 1.0, "h": 1.0, "shape": "rect", "angle": 0.0}
    B = {"pads": [pad], "vias": [], "segs": [{"net": "N", "w": 0.2, "a": (8.0, 10.0), "b": (12.0, 10.0), "layer": "F.Cu"}]}
    assert PR._on_pad(B, pad, "N"), "a track through the pad body must count"
    B["segs"][0].update(a=(8.0, 12.0), b=(12.0, 12.0))           # clear of the pad by 1.5 mm: no
    assert not PR._on_pad(B, pad, "N")
    B["segs"][0].update(a=(8.0, 10.0), b=(12.0, 10.0), net="OTHER")  # another net's copper never counts
    assert not PR._on_pad(B, pad, "N")


def test_track_crossing_a_pad_clears_its_waived_finding(tmp_path):
    from fluxplace import preroute as PR
    pad = {"ref": "U1", "num": "1", "net": "N", "x": 10.0, "y": 10.0, "ex": 0.5, "ey": 0.5, "w": 1.0, "h": 1.0, "shape": "rect", "angle": 0.0}
    B = {"pads": [pad], "vias": [], "segs": [{"net": "N", "w": 0.2, "a": (8.0, 10.0), "b": (12.0, 10.0), "layer": "F.Cu"}]}
    cons = {"fanout": {"waive": ["U1\\.1: test"]}}
    assert PR.waived_pin_findings(B, cons) == []
