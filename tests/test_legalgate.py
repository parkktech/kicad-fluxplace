"""Legality against the INPUT board: the baseline hides nothing the seating added, an illegal arranged part is legalised or reported,
breakout refuses an illegal move, the written board is gated by KiCad DRC and repaired."""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import pcbgen as G
from fluxplace import breakout as BO
from fluxplace import geom as GM
from fluxplace import legalgate as LG
from fluxplace import legality as LGL
from fluxplace import modules as MD
from fluxplace import routediag as RD

CAP = dict(court=(-0.9, -0.45, 0.9, 0.45))


def _two_caps(tmp_path, x2=40.0, name="b.kicad_pcb"):
    body = G.two("C1", 20, 20, "A", "B", **CAP) + G.two("C2", x2, 20, "C", "D", **CAP)
    return G.four_layers(G.board(tmp_path, body, name=name))


def test_baseline_of_the_input_hides_what_the_seating_adds(tmp_path):
    B = RD.load(_two_caps(tmp_path))
    base = LG.baseline(B, {})
    assert LG.count_new(LG.new_issues(LGL.Legal(B, {}, ()).issues(), base)) == 0
    GM.place(B["parts"]["C2"], 20.3, 20, 0.0)                         # the seating puts C2 onto C1
    arranged = LGL.Legal(B, {}, ()).issues()
    assert arranged["pad_short"] and arranged["overlap"]
    new = LG.new_issues(arranged, base)                              # against the input: named
    assert new["pad_short"] and new["overlap"] == ["C1/C2"]
    assert LG.count_new(LG.new_issues(arranged, arranged)) == 0     # against the arranged board (the old baseline): hidden


def test_an_illegal_arranged_support_goes_back_to_its_input_pose(tmp_path):
    B = RD.load(_two_caps(tmp_path))
    base, orig = LG.baseline(B, {}), LG.poses(B)
    GM.place(B["parts"]["C2"], 20.3, 20, 0.0)
    of = {"C1": "C1", "C2": "C1"}                                    # one module: the floorplan cannot move the pair apart
    flips = set()
    out = MD._legalise_arranged(B, {}, set(), [], of, base, orig, flips, {}, set(), lambda o: False)
    assert list(out) and set(out) <= {"C1", "C2"}
    assert LG.count_new(LG.new_issues(LGL.Legal(B, {}, ()).issues(), base)) == 0
    assert any(tuple(B["parts"][r]["pos"]) == orig[r][:2] for r in out)


def test_a_conflict_between_two_modules_is_left_to_the_floorplan(tmp_path):
    B = RD.load(_two_caps(tmp_path))
    base, orig = LG.baseline(B, {}), LG.poses(B)
    GM.place(B["parts"]["C2"], 20.3, 20, 0.0)
    out = MD._legalise_arranged(B, {}, set(), [], {"C1": "C1", "C2": "C2"}, base, orig, set(), {}, set(), lambda o: False)
    assert out == {} and B["parts"]["C2"]["pos"] == (20.3, 20)


def _ic(ref, x, y):
    return G.fp(ref, x, y, [G.pad(str(k + 1), 0, k * 1.0, f"Q{k}", 0.6, 0.6) for k in range(6)], court=(-1, -1, 1, 6))


def _qfn_wall(tmp_path, extra=""):
    from test_breakout import qfn, wall
    return G.board(tmp_path, qfn("U1", 50, 40) + wall() + extra)


def test_breakout_refuses_a_move_that_adds_an_issue_the_input_did_not_have(tmp_path):
    big = G.fp("D1", 46.5, 40.25, [G.pad("1", 6.5, 6.5, "Z", 0.5, 0.5)], court=(-7, -7, 7, 7))     # every pose within reach of C1 is inside D1's courtyard
    path = _qfn_wall(tmp_path, big)
    r = BO.repair(path, {}, None, baseline={k: [] for k in ("overlap", "outside", "forbidden", "no_tht", "pad_short", "hole", "hardware")})
    assert "C1" not in r["moved"]                                    # no legal pose: the part stays


def test_breakout_default_baseline_is_the_board_as_it_stands(tmp_path):
    big = G.fp("D1", 46.5, 40.25, [G.pad("1", 6.5, 6.5, "Z", 0.5, 0.5)], court=(-7, -7, 7, 7))
    r = BO.repair(_qfn_wall(tmp_path, big), {}, None)                # no baseline: an overlap C1 already had is not new
    assert "C1" in r["moved"]


@pytest.mark.skipif(not shutil.which("kicad-cli"), reason="kicad-cli")
def test_the_output_gate_flags_an_injected_short_and_repairs_it(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    src = _two_caps(tmp_path / "in")
    out = _two_caps(tmp_path / "out", x2=20.2)                       # the written board has C2 on top of C1
    d = LG.drc_new(out, src)
    assert d is not None and d["n"].get("shorting_items", 0) >= 1 and d["n"].get("courtyards_overlap", 0) >= 1
    assert "ILLEGAL" in LG.drc_line(d)
    base = LG.baseline(RD.load(src), {})
    (tmp_path / "out2").mkdir()
    out2 = shutil.copy(out, str(tmp_path / "out2" / "b.kicad_pcb"))
    shutil.copy(out[:-10] + ".kicad_pro", out2[:-10] + ".kicad_pro") if os.path.exists(out[:-10] + ".kicad_pro") else None
    rp = LG.repair_file(out, src, {}, base, log=lambda *a: None)    # Legal sees it too: C2 or C1 goes back
    assert rp["reverted"] and not rp["left"] and LG.drc_new(out, src)["items"] == []
    g = LG.gate(out2, src, {}, base, log=lambda *a: None)          # the DRC alone finds it and repairs it
    assert g is not None and not g["items"] and g["reverted"]
    assert LG.drc_new(out2, src)["items"] == []
