"""What a stage may not hand on (ledger 2026-11-27, fable-incplace): copper on a layer the board does not have (refused at the
commit point of every stage) and gap rows of gaps.NO_NEW_DEFAULTS kinds the stage input did not have (route: clearance)."""
import os
import shutil
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pcbnew = pytest.importorskip("pcbnew")
from fluxplace import pipeline as PL, boardwrite as BW, gaps as GP, constraints as C  # noqa: E402
from test_incplace import routed  # noqa: E402

OFF = '(segment (start 12 14) (end 16 14) (width 0.2) (layer "In3.Cu") (net "A"))\n'      # the fixture has F / In1 / In2 / B
NEAR = '(segment (start 8 10.4) (end 12 10.4) (width 0.2) (layer "F.Cu") (net "C"))\n'    # 0.05 mm under the pads of R1 (nets A, B)


def _ctx(tmp, cp):
    return types.SimpleNamespace(dir=lambda n, nm: os.makedirs(tmp / f"{n}-{nm}", exist_ok=True) or str(tmp / f"{n}-{nm}"),
                                 cons_path=cp, cons=C.load(cp) if cp else {})


def test_copper_off_the_board_stack_is_found_and_refused_at_the_commit_point(tmp_path):
    prev, _cp = routed(tmp_path / "a")
    new, _ = routed(tmp_path / "b", extra=OFF)
    assert BW.offstack_copper(prev) == [] and BW.offstack_copper(new) == [("track", "A", "In3.Cu")]
    r = PL._commit_point("route", prev, {"verdict": "pass", "board": new, "line": "routed"})
    assert r["verdict"] == "fail" and r["board"] == prev and "In3.Cu" in r["line"] and "A on" in r["line"]
    assert PL._commit_point("pairs", prev, {"verdict": "pass", "board": new})["verdict"] == "fail"       # any stage, not only route
    assert PL._commit_point("route", new, {"verdict": "pass", "board": new})["verdict"] == "pass"         # nothing the stage added


def test_no_new_kinds_default_and_override():
    assert GP.no_new_kinds({}, "route") == ["clearance"] and GP.no_new_kinds({}, "pairs") == []
    assert GP.no_new_kinds({"flow": {"no_new": {"route": []}}}, "route") == []


def test_only_rows_the_input_did_not_have_fail_the_stage(tmp_path, monkeypatch):
    row = {"id": "clearance:x", "kind": "clearance", "evidence": "Clearance violation"}
    seen = []

    def measure(rows):
        def f(board, work, cons_path=None, cons=None, kinds=None):
            seen.append(os.path.basename(board))
            return {"left": rows[os.path.basename(board)]}
        return f
    c = _ctx(tmp_path, None)
    res = {"verdict": "pass", "board": "out", "line": "gate route: clean"}
    monkeypatch.setattr(GP, "gate_measure", measure({"in": [], "out": []}))
    assert PL._no_new_gaps(c, 12, "route", "in", dict(res))["verdict"] == "pass" and seen == ["out"]      # clean output: the input is not measured
    monkeypatch.setattr(GP, "gate_measure", measure({"in": [row], "out": [row]}))
    assert PL._no_new_gaps(c, 12, "route", "in", dict(res))["verdict"] == "pass"                           # the input had it: not this stage's
    monkeypatch.setattr(GP, "gate_measure", measure({"in": [], "out": [row]}))
    r = PL._no_new_gaps(c, 12, "route", "in", dict(res))
    assert r["verdict"] == "fail" and "1 new clearance" in r["line"] and os.path.exists(tmp_path / "12-gate" / "no-new.json")
    assert PL._no_new_gaps(c, 8, "pairs", "in", dict(res))["verdict"] == "pass"                            # a stage with no such kinds
    monkeypatch.setattr(GP, "gate_measure", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("drc died")))
    assert PL._no_new_gaps(c, 12, "route", "in", dict(res))["verdict"] == "fail"                           # unmeasured is never a pass


@pytest.mark.skipif(not shutil.which("kicad-cli"), reason="the gate measures with kicad-cli")
def test_a_real_new_clearance_error_fails_the_route_stage(tmp_path):
    prev, cp = routed(tmp_path / "a")
    new, _ = routed(tmp_path / "b", extra=NEAR)
    c = _ctx(tmp_path, cp)
    r = PL._no_new_gaps(c, 12, "route", prev, {"verdict": "pass", "board": new, "line": "gate route: clean"})
    assert r["verdict"] == "fail" and "new clearance" in r["line"], r
    same = shutil.copy(new, tmp_path / "b" / "copy.kicad_pcb")
    shutil.copy(os.path.splitext(new)[0] + ".kicad_pro", tmp_path / "b" / "copy.kicad_pro")
    assert PL._no_new_gaps(c, 14, "route", new, {"verdict": "pass", "board": str(same)})["verdict"] == "pass"   # same errors as its input
