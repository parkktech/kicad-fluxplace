"""Review findings 2 and 3 through the pipeline seam, on real boards (pcbnew): protect existing items not nets, --from runs are protected,
tune releases only the pairs it tuned, in-place outputs are checked, a stale manifest is re-seeded at the start."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pytest.importorskip("pcbnew")
from fluxplace import boardwrite as BW, pipeline as PL
from test_boardwrite import BASE, board, seg


def test_adding_copper_to_a_locked_net_passes_the_seam(tmp_path):
    prev = board(tmp_path, "prev.kicad_pcb", BASE())
    BW.seed(prev)
    new = board(tmp_path, "new.kicad_pcb", BASE() + seg("D_P", (30, 20), (35, 22)))
    r = PL._commit_point("route", prev, {"verdict": "pass", "board": new})
    assert r["verdict"] == "pass" and set(BW.load_manifest(new)["nets"]) == {"D_P", "D_N"}


def test_ripping_a_locked_item_is_refused_and_the_last_good_board_stays(tmp_path):
    prev = board(tmp_path, "prev.kicad_pcb", BASE())
    BW.seed(prev)
    new = board(tmp_path, "new.kicad_pcb", seg("D_N", (20, 21), (30, 21), True) + seg("SIG", (20, 23), (30, 23)))
    r = PL._commit_point("route", prev, {"verdict": "pass", "board": new, "line": "routed"})
    assert r["verdict"] == "fail" and r["board"] == prev and "PROTECTED_CHANGED D_P" in r["line"]


def test_a_stage_output_that_is_its_input_path_is_still_checked(tmp_path):
    prev = board(tmp_path, "prev.kicad_pcb", BASE())
    BW.seed(prev)
    txt = open(prev).read().replace("(end 30 20)", "(end 30 22)", 1)
    open(prev, "w").write(txt)
    r = PL._commit_point("route", prev, {"verdict": "pass", "board": prev})
    assert r["verdict"] == "fail" and "PROTECTED_CHANGED D_P" in r["line"]


def test_tune_releases_only_the_pairs_it_tuned(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fluxplace import tune as TU
    prev = board(tmp_path, "prev.kicad_pcb", BASE())
    BW.seed(prev)
    moved_dn = board(tmp_path, "moved.kicad_pcb", seg("D_P", (20, 20), (30, 20.6), True) + seg("D_N", (20, 21), (30, 21.7), True) + seg("SIG", (20, 23), (30, 23)))
    # tune reports having worked on D_P/D_N only: both released, so the change passes; a net it did not name is not released
    r = PL._commit_point("tune", prev, {"verdict": "pass", "board": moved_dn, "released": ["D_P", "D_N"], "complete_pairs": [("D_P", "D_N")]})
    assert r["verdict"] == "pass" and set(BW.load_manifest(moved_dn)["nets"]) == {"D_P", "D_N"}
    prev2 = board(tmp_path, "prev2.kicad_pcb", BASE())
    BW.seed(prev2)
    r2 = PL._commit_point("tune", prev2, {"verdict": "pass", "board": moved_dn, "released": ["D_N"]})
    assert r2["verdict"] == "fail" and "D_P" in r2["line"]                      # D_P changed but tune did not say it tuned it


def test_st_tune_reports_the_pairs_it_worked_on(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fluxplace import tune as TU
    b = tmp_path / "in.kicad_pcb"; b.write_text("(kicad_pcb)\n")
    d = tmp_path
    c = SimpleNamespace(dir=lambda n, name: str(d), cons_path=None)
    out = str(d / "board.kicad_pcb")

    def fake(argv, log):
        json.dump({"pairs": [{"pair": "A_P/A_N", "status": "ok"}, {"pair": "B_P/B_N", "status": "needs_reroute"},
                             {"pair": "C_P/C_N", "status": "over"}]}, open(out + ".tune.json", "w"))
        return 0, ""
    monkeypatch.setattr(PL, "_run_cli", fake)
    monkeypatch.setattr(TU, "lock_tuned", lambda *a, **k: None)
    r = PL.st_tune(c, 1, str(b))
    assert sorted(r["released"]) == ["A_N", "A_P", "C_N", "C_P"] and r["complete_pairs"] == [("A_P", "A_N")]


def test_a_from_run_past_pairs_seeds_a_board_that_has_no_manifest(tmp_path, monkeypatch):
    prev = board(tmp_path, "in.kicad_pcb", BASE())
    assert not BW.load_manifest(prev)["nets"]
    man = BW.seed_from_board(prev, {}, log=print)
    assert set(man["nets"]) == {"D_P", "D_N"} and not BW.is_stale(prev)


# ---- gaps.close on a real board: the real kicad-cli measure, the real guard and the real manifest; only the rung is a stand-in
def _open_board(tmp_path, name="in.kicad_pcb", rip=False):
    import planboards as PB
    EDGE = '(gr_rect (start 0 0) (end 100 80) (stroke (width 0.1) (type default)) (fill no) (layer "Edge.Cuts"))\n'
    body = PB.fp("U1", 30, 30, [PB.pad("1", 0, 0, "A"), PB.pad("2", 0, 5, "L")]) + PB.fp("U2", 30, 30, [PB.pad("1", 20, 0, "A"), PB.pad("2", 20, 5, "L")])
    if rip:                                                    # the same connection as two locked segments: connectivity equal, copper not
        body += seg("L", (30, 35), (40, 35), True) + seg("L", (40, 35), (50, 35), True) + seg("A", (30, 30), (50, 30))
    else:
        body += seg("L", (30, 35), (50, 35), True)
        if name == "fixed.kicad_pcb":
            body += seg("A", (30, 30), (50, 30))
    p = tmp_path / name
    p.write_text('(kicad_pcb (version 20260206) (generator "pcbnew")\n(layers (0 "F.Cu" signal) (2 "B.Cu" signal))\n' + EDGE + body + ")\n")
    return str(p)


@pytest.mark.parametrize("rip,expect", [(False, "clean"), (True, "flat")])
def test_close_adopts_a_real_candidate_only_if_locked_copper_is_untouched(tmp_path, monkeypatch, rip, expect):
    import shutil
    from fluxplace import gaps as GP
    base = _open_board(tmp_path)
    BW.seed(base)
    assert "L" in BW.load_manifest(base)["nets"]
    cand = _open_board(tmp_path, "fixed.kicad_pcb") if not rip else _open_board(tmp_path, "rip.kicad_pcb", rip=True)

    def rung(b, out, gaps, ctx):
        shutil.copy(cand, out)
        return {"mode": "stand-in"}
    monkeypatch.setitem(GP.DISPATCH, "open-free", ["patch"])
    monkeypatch.setitem(GP.RUNGS, "patch", rung)
    res = GP.close(base, str(tmp_path / "work"), cons={}, attempts=str(tmp_path / "a.jsonl"), runs=str(tmp_path / "r.jsonl"), log=lambda m: None, inproc=True)
    assert res["stop"] == expect, res
    if rip:
        why = [json.loads(x)["why"] for x in open(tmp_path / "a.jsonl")]
        assert any("PROTECTED_CHANGED L" in w for w in why)
    else:
        assert BW.verify(res["best"], res["best"])["ok"] and "L" in BW.load_manifest(res["best"])["nets"]
