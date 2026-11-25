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


# ---- construct, settle, gate fails closed, pipeline
def _plan(tmp_path, body, cons=None, arrange=False):
    from fluxplace import floorplan2 as F2
    B = RD.load(G.board(tmp_path, body, outline=(0, 0, 40, 30)))
    cons = cons or {}
    M = MD.build(B, cons, None, arrange=arrange)
    P = F2.Plan(B, cons, None, M, log=lambda *a, **k: None)
    P.freeze_baseline()
    return F2, B, P


def _big(ref, x, y, h=7.0):
    return G.fp(ref, x, y, [G.pad(str(k + 1), -1 + (k % 2) * 2, (k // 2) * 1.0, f"{ref}N{k}", 0.6, 0.6) for k in range(8)], court=(-h, -h, h, h))


def test_construct_places_the_largest_first_and_never_parks_a_module_illegally(tmp_path):
    # 40 x 30 board: three 14 x 14 courtyards leave no room for a fourth; the fourth is unplaced, not left on top of another
    body = "".join(_big(f"U{k}", 8 + 12 * (k % 3), 8 + 14 * (k // 3), 7.0) for k in range(4))
    F2, B, P = _plan(tmp_path, body, {"placement": {"fixed": []}})
    failed = F2.construct(P)
    assert failed and all(P.unplaced(P.idx[n]) for n in failed)
    assert not any(P.fixed[P.idx[n]] for n in failed)
    moved = F2.all_moves(P)
    assert not [r for r in moved if P.of[r] in failed]                       # a module with no pose writes nothing
    placed = [m["name"] for m in P.mods if m["name"] not in failed]
    kn = LG.known(P.base_issues)
    assert all(not LG.part_new(P.L, r, kn) for n in placed for r in P.mods[P.idx[n]]["members"])


def test_construct_orders_by_area_and_retries_the_failed_first(tmp_path):
    body = _big("U1", 5, 5, 3.0) + _big("U2", 30, 20, 3.0) + _big("U3", 20, 15, 9.0)
    F2, B, P = _plan(tmp_path, body)
    assert F2.construct(P) == []
    assert all(not P.unplaced(i) for i in range(P.n))


def test_settle_reseats_a_module_that_stands_on_another(tmp_path):
    body = _big("U1", 8, 8, 3.0) + _big("U2", 30, 20, 3.0)
    F2, B, P = _plan(tmp_path, body)
    orig = LG.poses(B)
    P.apply(P.idx["U2"], 8.5, 8.0, 0)                                          # the arrangement put U2 on U1
    kn = LG.known(P.base_issues)
    assert any(LG.part_new(P.L, r, kn) for r in P.mods[P.idx["U2"]]["members"])
    out = F2.settle(P, orig)
    assert out["reseated"] and not out["unplaced"]
    assert all(not LG.part_new(P.L, r, kn) for m in P.mods for r in m["members"])


def test_settle_parks_a_module_that_cannot_be_seated(tmp_path):
    body = _big("U1", 8, 8, 3.0) + _big("U2", 30, 20, 3.0)
    F2, B, P = _plan(tmp_path, body)
    orig = LG.poses(B)
    i = P.idx["U2"]
    P.unplace(i)
    out = F2.settle(P, orig)
    assert out["parked"] == ["U2"] and i in P.parked and "U2" in P.noseat
    assert B["parts"]["U2"]["pos"] == orig["U2"][:2] and "U2" not in F2.all_moves(P)


def _stage(monkeypatch, tmp_path, gate=None, left=None):
    import test_floorplan as TF
    from fluxplace import legalgate as LG0
    board, c, out = TF._fake_stage(monkeypatch, tmp_path)
    monkeypatch.setattr(LG0, "gate", lambda *a, **k: gate)
    if left is not None:
        monkeypatch.setattr(LG0, "repair_file", lambda *a, **k: {"reverted": {}, "left": left})
    return board, c, out


def test_the_gate_fails_closed_when_the_drc_did_not_run(monkeypatch, tmp_path, capsys):
    import cli
    board, c, out = _stage(monkeypatch, tmp_path, gate=None)
    with pytest.raises(SystemExit) as e:
        cli.main(["floorplan", board, "--out", out, "--constraints", c, "--assume-defaults"])
    assert e.value.code == 1 and "floorplan: ILLEGAL - legality gate did not run" in capsys.readouterr().out


def test_the_gate_honours_what_the_repair_could_not_put_right(monkeypatch, tmp_path, capsys):
    import cli
    clean = {"n": {}, "items": [], "pairs": [], "reverted": {}}
    board, c, out = _stage(monkeypatch, tmp_path, gate=clean, left=[("C1", "overlap", "U1")])
    with pytest.raises(SystemExit) as e:
        cli.main(["floorplan", board, "--out", out, "--constraints", c, "--assume-defaults"])
    assert e.value.code == 1 and "floorplan: ILLEGAL - 1 issue(s) no part could be moved out of: C1 overlap U1" in capsys.readouterr().out


def test_an_illegal_floorplan_is_not_the_stage_board(monkeypatch, tmp_path):
    from fluxplace import pipeline as PL
    out_dir = tmp_path / "st"
    out_dir.mkdir()
    board = tmp_path / "in.kicad_pcb"
    board.write_text("in")

    class C:
        def dir(self, n, name): return str(out_dir)
        a = type("A", (), {})()
        def cs(self): return []
        def qs(self): return []
    out = out_dir / "board.kicad_pcb"

    def fake_cli(argv, log):
        out.write_text("illegal")
        out.with_suffix(".kicad_pro").write_text("{}")
        return 1, "floorplan: ILLEGAL - 2 shorts, 1 overlaps, 0 clearance (new vs input): x\n"
    monkeypatch.setattr(PL, "_engine_floorplan", lambda *a: None)
    monkeypatch.setattr(PL, "_run_cli", fake_cli)
    monkeypatch.setattr(PL, "_questions_result", lambda *a: None)
    res = PL.st_floorplan(C(), 3, str(board))
    assert res["verdict"] == "fail" and res["board"] == str(board) and res["line"].startswith("floorplan: ILLEGAL")
    assert not out.exists() and (out_dir / "board.illegal.kicad_pcb").exists() and res["illegal_board"].endswith(".illegal.kicad_pcb")
