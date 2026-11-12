import json, os, shutil, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import stall as ST, routediag as RD, boardwrite as BW, netclass as NC
import pcbgen as g


def mk(tmp_path):
    b = g.ic("U1", 20, 20, ["N1", "a", "b", "c", "d"], pitch=1.27, court=(-3, -3, 3, 3))
    b += g.two("C1", 60, 20, "N1", "GND", court=(-1, -.5, 1, .5))          # far support
    b += g.two("C2", 60, 40, "N1", "GND", locked=True, court=(-1, -.5, 1, .5))
    seg = '(segment (start 10 10) (end 12 10) (width 0.15) (layer "F.Cu") (net "KEEP"))\n'
    seg += '(segment (start 60 20) (end 40 20) (width 0.15) (layer "F.Cu") (net "N1"))\n'
    b += g.two("R9", 10, 10, "KEEP", "KEEP2") + seg
    return g.board(tmp_path, b)


def test_stage_timeout_default_and_override():
    assert ST.stage_timeout(None, {}) == 900
    assert ST.stage_timeout(None, {"route": {"stage_timeout_s": 120}}) == 120
    assert ST.stage_timeout(30, {"route": {"stage_timeout_s": 120}}) == 30


def test_strip_nets_only_removes_named_copper(tmp_path):
    p = mk(tmp_path)
    t, n = BW.strip_nets(open(p).read(), ["N1"])
    assert n == 1 and '(net "KEEP")' in t and '(net "N1")' not in t.split("(footprint")[-1].split("segment")[-1]
    assert t.count("(segment") == 1


def test_escape_first_orders_fine_pitch_nets_first(tmp_path):
    b = g.ic("U1", 20, 20, ["F1", "a", "b", "c", "d"], pitch=0.4) + g.two("R1", 40, 20, "F1", "X")
    b += g.ic("U2", 60, 60, ["C1", "a2", "b2", "c2", "d2"], pitch=1.27) + g.two("R2", 80, 60, "C1", "Y")
    B = RD.load(g.board(tmp_path, b))
    first, rest = ST.escape_first(B, NC.classify(B))
    assert "F1" in first and "C1" in rest and "F1" not in rest


def test_stall_loop_moves_unlocked_only_and_reroutes_only_affected(tmp_path):
    p = mk(tmp_path)
    calls = []

    def route_fn(b_in, b_out, nets, timeout):
        calls.append((nets, timeout))
        shutil.copy(b_in, b_out)               # a fake router that connects nothing new
        return False
    out = ST.stall_loop(p, str(tmp_path), route_fn, {}, None, rounds=2, replace=True,
                        failed0=["N1"], log=lambda m: None)
    assert "C1" in out["moved"] and "C2" not in out["moved"]       # C2 is locked
    assert calls and "KEEP" not in calls[0][0] and "N1" in calls[0][0]
    # the diagnosis is always written
    assert os.path.exists(out["diagnosis_files"][0]) and os.path.exists(str(tmp_path / "route-diagnose-0.json"))
    # copper of the untouched net survived the rip-up, the affected net's was removed
    t = open(str(tmp_path / "stall-1.kicad_pcb")).read()
    assert '(net "KEEP")' in t and t.count("(segment") == 1
    B = RD.load(out["board"])
    assert abs(B["parts"]["C1"]["pos"][0] - 60) > 5                  # moved next to U1


def test_no_replace_only_diagnoses(tmp_path):
    p = mk(tmp_path)
    out = ST.stall_loop(p, str(tmp_path), lambda *a: False, {}, None, replace=False, failed0=["N1"],
                        log=lambda m: None)
    assert out["moved"] == {} and out["board"] == p and len(out["diagnosis_files"]) == 1


def test_timeout_triggers_diagnosis_even_with_no_failed_nets(tmp_path):
    p = mk(tmp_path)
    out = ST.stall_loop(p, str(tmp_path), lambda *a: False, {}, None, failed0=[], timed_out0=True,
                        log=lambda m: None)
    assert len(out["diagnosis_files"]) == 1
