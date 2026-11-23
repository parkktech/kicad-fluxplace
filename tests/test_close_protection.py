"""Close vs the boardwrite manifest: a pair-skew rung's release reaches the pipeline's commit point, a pair inside its maximum is not tuned or
released, and a final board that fails protection falls back to the newest accepted state that verifies (synthetic: measure/rungs stubbed)."""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pytest.importorskip("pcbnew")
from fluxplace import boardwrite as BW, gaps as G, pipeline as PL
from test_boardwrite import BASE, board, seg

SKEW = {"id": "pair-skew:D_P/D_N", "base": "pair:D_P/D_N", "kind": "pair-skew", "sev": 1, "class": "pair", "nets": ["D_P", "D_N"],
        "at": None, "layer": None, "skew": 2.5, "limit": 0.5}
OPEN = {"id": "o1", "kind": "open-gap", "class": "plain", "nets": ["SIG"], "at": [10.0, 10.0], "layer": None}


def _m(gaps, opens):
    return {"opens": opens, "shorts": 0, "errors": 0, "gaps": [dict(g) for g in gaps], "opens_by_net": {}}


def _setup(tmp_path, monkeypatch, tuned, broken=None):
    """tuned: board text written by the tune rung; broken: written by the later open-gap rung (changes SIG)."""
    inp = board(tmp_path, "in.kicad_pcb", BASE())
    BW.seed(inp, why_by_net={"D_P": "pair D_P/D_N", "D_N": "pair D_P/D_N", "SIG": "kept"})
    pending = {"gaps": [SKEW, OPEN]}

    def remeasure(path, cli="kicad-cli"):
        return _m(pending["gaps"] if "r0" not in os.path.basename(path) and "best" in os.path.basename(path) else
                  ([OPEN] if "tune" in path else []), 2 if "best" in path else 1)
    monkeypatch.setattr(G, "_remeasure", remeasure)
    monkeypatch.setattr(G, "_classify_into", lambda *a, **k: None)
    monkeypatch.setattr(G, "_protect", lambda *a, **k: set())
    monkeypatch.setattr(G, "_accept", lambda *a, **k: (True, ""))

    def rung(name, base, out, gaps, ctx):
        if name == "tune" and gaps and gaps[0]["kind"] == "pair-skew":
            shutil.copy(tuned, out)
            return {"status": "ran"}
        if name == "patch" and broken:
            shutil.copy(broken, out)
            return {"status": "ran"}
        return {}
    monkeypatch.setattr(G, "_call_rung", rung)
    return inp


def _moved(tmp_path):
    return board(tmp_path, "tuned.kicad_pcb", seg("D_P", (20, 20), (30, 20.6), True) + seg("D_N", (20, 21), (30, 21.7), True) + seg("SIG", (20, 23), (30, 23)))


def test_pair_skew_rung_release_is_reported_and_the_commit_point_accepts_it(tmp_path, monkeypatch):
    tuned = _moved(tmp_path)
    inp = _setup(tmp_path, monkeypatch, tuned)
    seen = {}
    monkeypatch.setattr(G, "_remeasure", lambda p, cli="kicad-cli": _m([OPEN] if "best" not in os.path.basename(p) or seen.get("go") else [SKEW, OPEN], 1))
    res = G.close(inp, str(tmp_path / "W"), cons={}, attempts=str(tmp_path / "a.jsonl"), log=lambda *a: None, max_rounds=1)
    assert set(res["released"]) == {"D_P", "D_N"} and res["protection_broken_by"] is None
    r = PL._commit_point("close", inp, {"verdict": "pass", "board": res["best"], "released": sorted(res["released"])})
    assert r["verdict"] == "pass"
    assert PL._commit_point("close", inp, {"verdict": "pass", "board": res["best"]})["verdict"] == "fail"      # no release reported: the old end-of-run refusal
    assert {"D_P", "D_N"} <= set(BW.load_manifest(res["best"])["nets"])                                         # re-sealed: protected at the new copper


def test_only_the_target_pairs_the_candidate_re_laid_are_released(tmp_path, monkeypatch):
    tuned = _moved(tmp_path)                                           # D_P/D_N re-laid, SIG as it was
    inp = _setup(tmp_path, monkeypatch, tuned)
    other = dict(SKEW, id="pair-skew:SIG/SIGB", base="pair:SIG/SIGB", nets=["SIG", "SIGB"])       # a second target pair, protected, untouched
    monkeypatch.setattr(G, "_remeasure", lambda p, cli="kicad-cli": _m([OPEN] if "best" not in os.path.basename(p) else [SKEW, other, OPEN], 1))
    res = G.close(inp, str(tmp_path / "W"), cons={}, attempts=str(tmp_path / "a.jsonl"), log=lambda *a: None, max_rounds=1)
    assert set(res["released"]) == {"D_P", "D_N"}
    assert not os.path.exists(str(tmp_path / "W" / "accepted"))        # the final board verified: no snapshot left on disk


def test_a_pair_inside_its_maximum_is_not_a_tune_target_and_releases_nothing():
    ok = dict(SKEW, skew=0.4, limit=0.5)
    assert not G._skew_fails(ok) and G._skew_fails(SKEW)
    man = {"nets": {"D_P": {}, "D_N": {}}}
    assert G._release_nets("pair-skew", [ok], man) == set()
    assert G._release_nets("pair-skew", [SKEW], man) == {"D_P", "D_N"}
    assert G._release_nets("open-gap", [SKEW], man) == set()


def test_tune_rung_skips_a_pair_that_passes_its_maximum(monkeypatch, tmp_path):
    from fluxplace import tune as TU
    monkeypatch.setattr(G, "_pair_block", lambda b, c, g: {"legs": {"D_P": 10.0, "D_N": 10.3}, "skew_limit_mm": 0.2})
    monkeypatch.setattr(TU, "tune_pairs", lambda *a, **k: pytest.fail("tuned a pair inside its maximum"))
    r = G._r_tune("x", str(tmp_path / "o"), [dict(SKEW, skew=0.4, limit=0.5)], {"cons": {}, "budget_s": 1})
    assert r == {"status": "no pair"}


def test_final_commit_falls_back_to_the_newest_verified_accepted_state(tmp_path, monkeypatch):
    tuned = _moved(tmp_path)
    broken = board(tmp_path, "broken.kicad_pcb", seg("D_P", (20, 20), (30, 20.6), True) + seg("D_N", (20, 21), (30, 21.7), True) + seg("SIG", (20, 23), (30, 29)))
    inp = _setup(tmp_path, monkeypatch, tuned, broken)
    state = {"n": 0}

    def rm(p, cli="kicad-cli"):
        b = os.path.basename(p)
        if "tune" in b:
            return _m([OPEN], 1)
        if "patch" in b:
            return _m([], 0)
        return _m([SKEW, OPEN] if state["n"] == 0 else [OPEN], 2 if state["n"] == 0 else 1)
    monkeypatch.setattr(G, "_remeasure", rm)
    real = BW.verify
    monkeypatch.setattr(BW, "verify", lambda base, cand, release=(), manifest=None: (
        {"ok": True, "why": "", "changed": [], "duplicates": 0, "uuid_collisions": 0} if "patch" in os.path.basename(cand) and state.update(n=1) is None
        else real(base, cand, release, manifest)))
    res = G.close(inp, str(tmp_path / "W"), cons={}, attempts=str(tmp_path / "a.jsonl"), log=lambda *a: None, max_rounds=3)
    assert res["protection_broken_by"] and "patch" in res["protection_broken_by"]
    assert res["stop"] == "protection" and res["exit"] != 0           # the patch state was clean; the state handed on has its gap back
    assert os.path.isdir(str(tmp_path / "W" / "accepted"))            # kept as evidence after a fallback
    assert real(inp, res["best"], release=set(res["released"]), manifest=BW.load_manifest(inp))["ok"]
    assert set(res["released"]) == {"D_P", "D_N"}
