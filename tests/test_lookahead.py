"""LOOKAHEAD stage: demand map, reservations, ghost/fitness/render helpers and the round loop (no pcbnew, no router)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import lookahead as LA
from fluxplace import routediag as RD


def board(segs=(), vias=(), pads=(), zones=(), copper=("F.Cu", "B.Cu"), bbox=(0.0, 0.0, 10.0, 10.0)):
    return {"segs": list(segs), "vias": list(vias), "pads": list(pads), "zones": list(zones),
            "copper": list(copper), "bbox": bbox}


def seg(net, a, b, layer="F.Cu", mid=None):
    return {"net": net, "a": a, "b": b, "w": 0.2, "mid": mid, "layer": layer, "locked": False}


def pad(net, x, y):
    return {"net": net, "x": x, "y": y, "layers": {"F.Cu"}, "ref": "U1", "num": "1"}


@pytest.fixture
def load(monkeypatch):
    def install(B):
        monkeypatch.setattr(RD, "load", lambda path, **kw: B)
        return B
    return install


P0 = LA.params({"lookahead": {"enabled": True}})


def test_params_defaults_and_overrides():
    P = LA.params(None)
    assert P["enabled"] is False and P["cell_mm"] == 1.0 and P["min_nets"] == 2 and P["max_rounds"] == 4
    assert P["variants"] == [(2, 3.0), (1, 3.0), (3, 2.0)]
    Q = LA.params({"lookahead": {"enabled": True, "cell_mm": "0.5", "variants": [[1, 2]]}})
    assert Q["enabled"] is True and Q["cell_mm"] == 0.5 and Q["variants"] == [(1, 2.0)]
    assert LA.params({"lookahead": {"enabled": "yes"}})["enabled"] is False


def test_demand_rasterises_non_critical_segment(load):
    load(board(segs=[seg("N1", (0.5, 0.5), (5.5, 0.5))]))
    D = LA.demand("x.kicad_pcb", set(), P0)
    row = sorted(k for k in D["cells"] if k[0] == "F.Cu" and k[2] == 0)
    assert [k[1] for k in row] == [0, 1, 2, 3, 4, 5]
    assert all(D["cells"][k]["nets"] == {"N1"} and not D["cells"][k]["crit"] for k in row)
    assert D["origin"] == (0.0, 0.0) and D["cell"] == 1.0


def test_critical_copper_goes_to_crit_not_nets(load):
    load(board(segs=[seg("P", (0.5, 2.5), (3.5, 2.5))]))
    D = LA.demand("x", {"P"}, P0)
    cl = D["cells"][("F.Cu", 1, 2)]
    assert cl["crit"] == {"P"} and cl["nets"] == set()


def test_plane_and_unnamed_nets_are_skipped(load):
    zone = {"net": "GND", "layers": {"F.Cu"}, "pts": [], "bbox": (0, 0, 1, 1)}
    load(board(segs=[seg("GND", (0.5, 0.5), (3.5, 0.5)), seg("", (0.5, 5.5), (3.5, 5.5))], zones=[zone]))
    D = LA.demand("x", set(), P0)
    assert D["cells"] == {} or all(not cl["nets"] for cl in D["cells"].values())


def test_via_marks_every_spanned_copper_layer(load):
    cu = ["F.Cu", "In1.Cu", "B.Cu"]
    via = {"net": "N2", "x": 4.5, "y": 4.5, "d": 0.6, "drill": 0.3, "layers": {"F.Cu", "B.Cu"}, "locked": False}
    load(board(vias=[via], copper=cu))
    D = LA.demand("x", set(), P0)
    assert {k[0] for k in D["cells"]} == set(cu)
    assert all(cl["nets"] == {"N2"} for cl in D["cells"].values())


def test_escape_flag_from_own_pad_distance(load):
    load(board(segs=[seg("N1", (0.5, 0.5), (0.5, 4.5))], pads=[pad("N1", 0.5, 0.5)]))
    D = LA.demand("x", set(), P0)
    near = D["cells"][("F.Cu", 0, 0)]
    far = D["cells"][("F.Cu", 0, 4)]
    assert near["escape"] is True and near["pad_dist"]["N1"] < 1.0
    assert far["escape"] is False and far["pad_dist"]["N1"] > 3.0


def test_reservations_drop_crit_cells_and_pad_clear_cells(load):
    # N1 runs along row 0 from x=0.5 to 9.5; a critical pad of P sits at (9.5, 0.5); a crit copper cell at (2,0)
    B = board(segs=[seg("N1", (0.5, 0.5), (9.5, 0.5))], pads=[pad("P", 9.5, 0.5)], bbox=(0.0, 0.0, 10.0, 10.0))
    load(B)
    D = LA.demand("x", {"P"}, P0)
    D["cells"][("F.Cu", 2, 0)]["crit"].add("P")
    out = LA.reservations(D, B, {"P"}, dict(P0, pair_clear_mm=0.6, max_area_frac=1.0, min_nets=1))
    assert out
    assert all(not (r["rect"][0] <= 2.5 <= r["rect"][2]) for r in out)    # crit cell (2,0) dropped
    assert all(not (r["rect"][0] <= 9.5 <= r["rect"][2]) for r in out)    # pad-clear cell (9,0) dropped
    assert any(r["rect"][0] <= 8.5 <= r["rect"][2] for r in out)          # cell 1.0 mm from the pad kept


def test_reservations_area_cap_keeps_highest_demand(load):
    B = board(segs=[seg("A", (0.5, 0.5), (3.5, 0.5)), seg("B", (0.5, 0.5), (3.5, 0.5)), seg("C", (0.5, 0.5), (1.5, 0.5))],
              bbox=(0.0, 0.0, 10.0, 10.0))
    load(B)
    D = LA.demand("x", set(), P0)
    out = LA.reservations(D, B, set(), dict(P0, max_area_frac=0.02))   # cap 2 mm2 = 2 cells
    kept = {(r["rect"][0], r["rect"][1]) for r in out}
    assert sum(r["rect"][2] - r["rect"][0] for r in out) <= 2.0 + 1e-9
    assert (0.0, 0.0) in kept                              # highest demand (3 nets) kept first


def test_reservations_merge_rows_into_rectangles(load):
    B = board(segs=[seg("A", (0.5, 0.5), (2.5, 0.5)), seg("A", (0.5, 1.5), (2.5, 1.5))], bbox=(0.0, 0.0, 10.0, 10.0))
    load(B)
    D = LA.demand("x", set(), dict(P0, min_nets=1))
    out = LA.reservations(D, B, set(), dict(P0, min_nets=1, max_area_frac=1.0))
    assert len(out) == 1
    r = out[0]
    assert r["layer"] == "F.Cu" and r["rect"] == (0.0, 0.0, 3.0, 2.0)
    assert r["nets"] == ["A"] and r["demand"] == 6


def test_min_nets_and_escape_thresholds(load):
    B = board(segs=[seg("A", (0.5, 0.5), (0.5, 0.5))], pads=[pad("A", 1.5, 0.5)], bbox=(0.0, 0.0, 10.0, 10.0))
    load(B)
    D = LA.demand("x", set(), P0)
    # one net on the cell, own pad 1.0 mm from the cell centre: reserved with min_nets=2 through the escape rule
    assert LA.reservations(D, B, set(), dict(P0, max_area_frac=1.0))
    # with escape_mm = 0.5 the pad is out of reach and min_nets=2 is not met: nothing reserved
    assert LA.reservations(D, B, set(), dict(P0, max_area_frac=1.0), escape_mm=0.5) == []


def test_critical_nets_uses_pair_list_and_rails(monkeypatch):
    from fluxplace import netclass as NC, pairaudit as PA, power as PWR
    monkeypatch.setattr(RD, "load", lambda path, **kw: {})
    monkeypatch.setattr(NC, "classify", lambda B, cons, spec=None: {"DP": {}, "DN": {}})
    monkeypatch.setattr(PA, "pair_list", lambda cls: [("DN", "DP")])
    monkeypatch.setattr(PWR, "route_widths", lambda cons, **kw: {"VBAT": 1.0})
    assert LA.critical_nets("x", {}) == {"DP", "DN", "VBAT"}


def test_apply_code_is_valid_python_and_sets_rule_area_flags():
    rects = [{"layer": "F.Cu", "rect": (1.0, 2.0, 3.0, 4.0)}, {"layer": "B.Cu", "rect": (0.0, 0.0, 5.0, 5.0)}]
    code = LA._apply_code("/in.kicad_pcb", "/out.kicad_pcb", rects)
    compile(code, "<apply>", "exec")
    for needle in ("SetIsRuleArea(True)", "SetDoNotAllowTracks", "SetDoNotAllowVias", "'F.Cu'", "/out.kicad_pcb",
                   "LOOKAHEAD-", "(1.0, 2.0, 3.0, 4.0)"):
        assert needle in code


def test_strip_code_targets_only_lookahead_zones():
    code = LA._strip_code("/in.kicad_pcb", "/out.kicad_pcb")
    compile(code, "<strip>", "exec")
    assert "startswith('LOOKAHEAD-')" in code and "b.Remove(z)" in code


def test_apply_and_strip_run_fresh_process_and_copy_sidecar(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(LA, "_run", lambda code, timeout=900: calls.append(code) or "OK 1")
    src = tmp_path / "in.kicad_pcb"
    src.write_text("(kicad_pcb)")
    (tmp_path / "in.kicad_pro").write_text("{}")
    dst = str(tmp_path / "out.kicad_pcb")
    LA.apply(str(src), dst, [{"layer": "F.Cu", "rect": (0, 0, 1, 1)}])
    LA.strip(str(src), str(tmp_path / "s.kicad_pcb"))
    assert "SetIsRuleArea" in calls[0] and "startswith" in calls[1]
    assert (tmp_path / "out.kicad_pro").exists() and (tmp_path / "s.kicad_pro").exists()


def test_run_raises_when_pcbnew_step_does_not_print_ok(monkeypatch):
    import subprocess
    class R:
        returncode, stdout, stderr = 1, "", "boom"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="pcbnew step failed"):
        LA._run("x")
