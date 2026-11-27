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


def _close_stage(tmp_path, monkeypatch, prev, cleaned_content):
    """st_close with the fresh-process calls faked: `clean` writes `cleaned_content` as postroute.kicad_pcb, `close` records its board."""
    from types import SimpleNamespace
    d = tmp_path / "w"
    d.mkdir(exist_ok=True)
    c = SimpleNamespace(dir=lambda n, name: str(d), cons_path=None, cons={}, board_in=prev)
    seen = {}

    def fake(module, func, *a, **k):
        if func == "clean":
            open(a[1], "w").write(cleaned_content)
            return {"snap": {"moved": 2}}
        seen["closed"] = a[0]
        man = BW.load_manifest(a[0])
        return {"start": {"opens": 1, "errors": 1}, "end": {"opens": 0, "errors": 0}, "attempts": 1, "stop": "clean", "best": a[0],
                "manifest_nets": sorted(man["nets"])}
    monkeypatch.setattr(PL, "in_fresh_process", fake)
    return PL.st_close(c, 8, prev), seen, str(d / "postroute.kicad_pcb")


def test_st_close_carries_the_manifest_to_the_post_route_board(tmp_path, monkeypatch):
    prev = board(tmp_path, "prev.kicad_pcb", BASE())
    BW.seed(prev)
    r, seen, cleaned = _close_stage(tmp_path, monkeypatch, prev, open(prev).read() + "")
    assert os.path.isfile(cleaned + ".done.json") or os.path.isfile(BW.done_path(cleaned))
    assert seen["closed"] == cleaned and set(BW.load_manifest(cleaned)["nets"]) == {"D_P", "D_N"} and not BW.is_stale(cleaned)
    assert "snapped 2" in r["line"]


def test_st_close_closes_the_original_board_when_the_snap_breaks_a_protected_item(tmp_path, monkeypatch):
    prev = board(tmp_path, "prev.kicad_pcb", BASE())
    BW.seed(prev)
    ripped = board(tmp_path, "rip.kicad_pcb", seg("D_N", (20, 21), (30, 21), True) + seg("SIG", (20, 23), (30, 23)))
    r, seen, cleaned = _close_stage(tmp_path, monkeypatch, prev, open(ripped).read())
    assert seen["closed"] == prev and "post-route refused" in r["line"] and "PROTECTED_CHANGED" in r["line"]


def test_a_pair_rung_in_close_releases_only_its_targets_on_the_carried_manifest(tmp_path, monkeypatch):
    """What gaps.close does per rung (BW.commit(best, final, best, release=targets, manifest=man)), on the manifest st_close carried."""
    def carried(sub):
        d = tmp_path / sub
        d.mkdir()
        prev = board(d, "prev.kicad_pcb", BASE())
        BW.seed(prev)
        return _close_stage(d, monkeypatch, prev, open(prev).read())[2]
    both = lambda d: board(d, "m.kicad_pcb", seg("D_N", (20, 21), (30, 24), True) + seg("D_P", (20, 20), (30, 22), True))
    c1 = carried("a")
    assert BW.commit(c1, both(tmp_path / "a"), c1, release={"D_P", "D_N"}, manifest=BW.load_manifest(c1), reason="pair-skew pairgroup")["written"]
    c2 = carried("b")
    v = BW.commit(c2, both(tmp_path / "b"), c2, release={"D_P"}, manifest=BW.load_manifest(c2), reason="pair-skew pairgroup")
    assert not v["written"] and "PROTECTED_CHANGED D_N" in v["why"]


def test_close_on_a_stale_manifest_returns_a_stop_dict_and_leaves_the_incumbent(tmp_path, monkeypatch):
    from fluxplace import gaps as GP
    monkeypatch.setenv("FLUXPLACE_VERSIONS", str(tmp_path / "none.json"))
    base = _open_board(tmp_path)
    BW.seed(base)
    txt = open(base).read().replace("(end 50 35)", "(end 50 36)", 1)                                # edited outside the flow
    open(base, "w").write(txt)
    before = open(base).read()
    msgs = []
    res = GP.close(base, str(tmp_path / "work"), cons={}, attempts=str(tmp_path / "a.jsonl"), runs=str(tmp_path / "r.jsonl"), log=msgs.append, inproc=True)
    assert res["stop"] == "manifest_stale" and res["verdict"] == "fail" and any(x.startswith("L:") for x in res["diff"])
    assert any(x.startswith("  L:") for x in msgs) and open(base).read() == before and BW.is_stale(base)
    assert not os.path.exists(tmp_path / "work" / "best.kicad_pcb") and os.path.exists(tmp_path / "work" / "stop.json")

def test_route_post_centre_fallback_never_moves_protected_copper(tmp_path, monkeypatch):
    """W46: the route stage's post ran centre_run with protect=[] and moved locked copper of the manifest nets (25 nets: PROTECTED_CHANGED at the commit)."""
    from fluxplace import krtchain as K, snapiter as SI, planetie as PT, opengate as OG
    prev = board(tmp_path, "prev.kicad_pcb", BASE())
    BW.seed(prev)
    best = str(tmp_path / "best.kicad_pcb")
    open(best, "w").write(open(prev).read())
    open(str(tmp_path / "best.kicad_pro"), "w").write("{}")
    seen = {}

    def centre(src, dst, iters=3, log=print, protect=()):
        seen["protect"] = set(protect)
        if "D_P" not in protect:                                       # the real move: a locked D_P end moves onto the via centre
            open(dst, "w").write(open(src).read().replace("(end 30 20)", "(end 30 20.05)", 1))
        return {"centred_left": 0}
    monkeypatch.setattr(SI, "run", lambda *a, **k: {"centred_left": 3, "moved": 0})
    monkeypatch.setattr(SI, "via_run", lambda *a, **k: {"centred_left": 3})
    monkeypatch.setattr(SI, "joiner_run", lambda *a, **k: {"centred_left": 3})
    monkeypatch.setattr(SI, "centre_run", centre)
    monkeypatch.setattr(PT, "run_guarded", lambda *a, **k: {"placed": 0})
    monkeypatch.setattr(OG, "refill", lambda *a, **k: None)
    R = {"track": 0.15, "clearance": 0.127, "via": 0.45, "drill": 0.2, "clearance_class": 0.0, "min_clearance": 0.127}
    K._post(best, str(tmp_path), [], R, protect=sorted(BW.load_manifest(prev)["nets"]))
    assert {"D_P", "D_N"} <= seen["protect"]
    r = PL._commit_point("route", prev, {"verdict": "pass", "board": best})
    assert r["verdict"] == "pass"
