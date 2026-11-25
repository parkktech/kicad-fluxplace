"""PLACEGAPS phase 8: row-rail (pull-load, pour-need) and FileEval (fakes: no router, no KiCad)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fluxplace import placegaps as PG, placetour as PT  # noqa: E402
from test_placegaps_core import ctx, part  # noqa: E402

NET = "+3V3"


def rail_board(load_x=150.0, side="F.Cu"):
    """source L1 at x=10, loads U1 (x=load_x), U2 (x=60) on one net, one row each; limit 122 mm."""
    B = {"parts": {"L1": part(10, 50), "U1": part(load_x, 50, side), "U2": part(60, 50)}, "bbox": (0, 0, 200, 100), "pads": []}
    for r, p in B["parts"].items():
        p["pads"] = [{"ref": r, "num": "1", "x": p["pos"][0], "y": p["pos"][1], "net": NET}]
    return B


def rows(B):
    out = []
    for ld in ("U1", "U2"):
        v = PG.rail_reach(B, "L1", ld, NET)[0]
        out.append({"kind": "rail", "name": f"{NET} L1->{ld}", "metric": "reach", "unit": "mm", "value": round(v, 2), "target": 28.0, "max": 122.0,
                    "hard": True, "verdict": "fail" if v > 122.0 else "ok", "refs": ["L1", ld], "nets": [NET], "layers": ["In2.Cu"]})
    return out


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(PT, "_movable", lambda B, cons, P: sorted(B["parts"]))
    monkeypatch.setattr(PT, "_legal", lambda B, cons, tg, mov: {r: tuple(v) for r, v in tg.items()})


def mkctx(B, **kw):
    return ctx(B=B, cons={}, spec={}, rows=rows(B), _cl=[], **kw)


def test_rail_rows_map_to_row_rail_and_are_gaps():
    B = rail_board()
    c = mkctx(B)
    gs = [g for g in PG.detect(c, ["row-rail"])["gaps"]]
    assert [g["id"] for g in gs] == [f"row-rail:{NET} L1->U1|reach"] and gs[0]["row"] == f"rail:{NET} L1->U1"


def test_reach_is_manhattan_on_pad_xy_whatever_the_side():
    F, Bk = rail_board(150.0, "F.Cu"), rail_board(150.0, "B.Cu")
    assert PG.rail_reach(F, "L1", "U1", NET)[0] == PG.rail_reach(Bk, "L1", "U1", NET)[0] == 140.0
    assert PG.rail_reach(F, "L1", "U1", NET, (20.0, 0.0))[0] == 120.0           # the source shifted toward the load


def test_pull_load_moves_the_load_toward_the_source_by_more_than_the_excess(env):
    B = rail_board()
    c = mkctx(B)
    g = PG.detect(c, ["row-rail"])["gaps"][0]
    cands = PG.RUNGS["pull-load"](g, c)
    loads = [x for x in cands if x["id"].endswith("-load")]
    assert loads and all(list(x["moves"]) == ["U1"] and x["moves"]["U1"][0] < 150.0 for x in loads)
    assert min(150.0 - x["moves"]["U1"][0] for x in loads) >= 140.0 - 122.0 + 1.0 - 1e-6
    assert len({x["id"] for x in cands}) == len(cands)


def test_pull_source_is_proposed_only_when_no_sibling_load_gets_newly_over(env):
    B = rail_board()
    c = mkctx(B)
    g = PG.detect(c, ["row-rail"])["gaps"][0]
    src = [x for x in PG.RUNGS["pull-load"](g, c) if x["id"].endswith("-src")]
    assert src and all(x["moves"]["L1"][0] > 10.0 for x in src)                 # toward U1, U2 (x=60) stays within its limit
    B2 = rail_board()
    B2["parts"]["U2"] = part(10, 50)                                                # a sibling load right at the source
    B2["parts"]["U2"]["pads"] = [{"ref": "U2", "num": "1", "x": 10.0, "y": 50.0, "net": NET}]
    c2 = mkctx(B2)
    c2["rows"][1].update(value=0.0, max=0.5)                                       # moving the source away would push it over its 0.5 mm limit
    g2 = PG.detect(c2, ["row-rail"])["gaps"][0]
    assert not [x for x in PG.RUNGS["pull-load"](g2, c2) if x["id"].endswith("-src")]


def test_fixed_load_and_source_give_no_candidate_and_pour_need_asks(env, monkeypatch):
    monkeypatch.setattr(PT, "_movable", lambda B, cons, P: [])
    B = rail_board()

    class S:
        asked = []

        def ask(self, qid, topic, found, options, default, settle, apply=None):
            self.asked.append((qid, found, sorted(options), default))
            return default
    s = S()
    c = mkctx(B, session=s)
    g = PG.detect(c, ["row-rail"])["gaps"][0]
    assert PG.RUNGS["pull-load"](g, c) == []
    assert PG.RUNGS["pour-need"](g, c) == []
    rec = c["pour_need"][NET]
    assert rec["loads_over"] == ["U1"] and rec["loads"] == 2 and rec["copper_factor"] == round(140.0 / 122.0, 3) and rec["spread_mm"] == (90.0, 0.0)
    assert not s.asked                                                              # asked after the loop, not by the rung
    PG.pour_questions(c, {"gaps_out": [g]})
    assert s.asked and s.asked[0][0] == f"rail-pour:{NET}" and "140" in s.asked[0][1] and "122" in s.asked[0][1]
    PG.RUNGS["pour-need"](g, c)
    PG.pour_questions(c, {"gaps_out": [g]})
    assert len(s.asked) == 2 and s.asked[0][0] == s.asked[1][0]                    # a net per record: asked again only by a new call
    PG.pour_questions(c, {"gaps_out": []})                                          # a rail a later round closed is dropped and not asked
    assert len(s.asked) == 2 and not c["pour_need"]


class Fake(PG.Evaluator):
    """base = the rail gap; a candidate's gaps come from `table` (cand id -> gap list)."""
    def __init__(self, base, table):
        self.base, self.table, self.B = base, table, rail_board()

    def detect(self, kinds):
        return list(self.base)

    def board(self):
        return self.B

    def trial(self, cand):
        return {"gaps": self.table[cand["id"]], "state": {}, "refusal": None}

    def commit(self, tr):
        self.base = tr["gaps"]


def rgap(name, mag=0.1):
    return PG.gap("row-rail", name, "plain", mag, refs=["L1", "U1"], nets=[NET], row=f"rail:{name}")


def test_close_accepts_the_pull_that_honours_the_row_and_refuses_one_that_breaks_another_hard_row(env):
    B = rail_board()
    c = mkctx(B)
    tgt = PG.detect(c, ["row-rail"])["gaps"][0]
    cands = {x["id"]: x for x in PG.RUNGS["pull-load"](tgt, c)}
    ids = sorted(cands)
    other = rgap(f"{NET} L1->U2", 0.05)
    table = {i: [] for i in ids}
    table[ids[0]] = [other]                                    # closes the target, opens another hard row: same count, refused by name
    ev = Fake([tgt], table)
    res = PG.close(c, ["row-rail"], ev, stage="t")
    assert res["accepted"] and res["accepted"][0]["candidate"] == ids[1]
    assert any(r["candidate"] == ids[0] and r["check"] == f"row:rail:{NET} L1->U2" for r in res["refused"])
    assert res["score_out"][PG.TERMS.index("hard_rows")] == 0


def test_file_eval_refuses_wire_length_over_two_percent_support_far_and_new_legality(env, monkeypatch, tmp_path):
    from fluxplace import legalgate as LG, routediag as RD
    src = tmp_path / "in.kicad_pcb"
    src.write_text("x")
    cost = {"wl": 1000.0, "cross": 100}
    seen = {"cost": dict(cost), "gaps": [], "ill": 0}
    ev = PG.FileEval(mkctx(rail_board()), str(src), str(src), str(tmp_path / "w"), legal_base={}, measure=lambda p: dict(seen["cost"]),
                     guards={"fan": lambda p: 0}, drc_ref_board=str(src))
    ev.gaps = [rgap(f"{NET} L1->U1")]
    monkeypatch.setattr(PT, "_apply", lambda b, mv, out, mr: (open(out, "w").write("y"), (True, [], {}))[1])
    monkeypatch.setattr(RD, "load", lambda p: rail_board())
    monkeypatch.setattr(LG, "baseline", lambda B, cons: {})
    monkeypatch.setattr(LG, "new_issues", lambda i, b: {"overlap": ["a/b"] * seen["ill"]})
    monkeypatch.setattr(ev, "_gaps", lambda p: list(seen["gaps"]))
    monkeypatch.setattr(LG, "_drc", lambda *a, **k: [])
    monkeypatch.setattr(LG, "drc_new", lambda *a, **k: {"n": {}, "items": [], "pairs": []})
    cand = {"id": "c", "moves": {"U1": (1, 2, 0)}}
    ok = ev.trial(cand)
    assert ok["refusal"] is None and ok["m"]["wl"] == 1000.0
    seen["cost"]["wl"] = 1021.0
    assert ev.trial(cand)["refusal"].startswith("score:wl")
    seen["cost"]["wl"] = 1019.0
    assert ev.trial(cand)["refusal"] is None
    seen["gaps"] = [PG.gap("support-far", "C1", "decap", 3.0, refs=["C1"])]
    assert ev.trial(cand)["refusal"].startswith("score:decap_far")
    seen["gaps"], seen["ill"] = [], 1
    assert ev.trial(cand)["refusal"].startswith("legal:model")


def test_accept_refuses_a_non_target_failing_row_that_gets_worse():
    tgt = rgap(f"{NET} L1->U1", 0.2)
    sep = PG.gap("row-away", "gnss|min", "plain", 0.644, refs=["U3"], row="intent:away:gnss")
    sep2 = dict(sep, mag=0.996)
    base = {"gaps": [tgt, sep], "state": {}}
    ok, check, why = PG._accept(base, {"gaps": [sep2], "state": {}, "refusal": None}, tgt["id"])
    assert not ok and check == "row:intent:away:gnss" and "0.644->0.996" in why
    ok, _c, _w = PG._accept(base, {"gaps": [dict(sep, mag=0.5)], "state": {}, "refusal": None}, tgt["id"])
    assert ok


def test_rail_overage_inside_the_model_error_is_a_soft_row_and_the_tolerance_is_a_key():
    B = rail_board(135.0)                                                  # 125 mm vs 122: 2.5 %
    c = mkctx(B)
    g = PG.detect(c, ["row-rail"])["gaps"][0]
    assert g["row"] is None and "soft" in g["detail"] and PG.score_terms([g])["hard_rows"] == 0
    c2 = mkctx(B)
    c2["cons"] = {"placegaps": {"rail_tolerance": 0.01}}
    assert PG.detect(c2, ["row-rail"])["gaps"][0]["row"] == f"rail:{NET} L1->U1"


def test_close_trials_a_source_move_inside_the_cap_and_carries_the_supports(env, monkeypatch):
    from fluxplace import intent as IT
    B = rail_board()
    B["parts"]["C1"], B["parts"]["R1"] = part(11, 50), part(9, 52)

    def mws(B_, cons, spec, ref, x, y, a, legal=None, log=None):                  # the supports ride rigidly with the IC
        p = B_["parts"][ref]
        dx, dy = x - p["pos"][0], y - p["pos"][1]
        out = {r: (B_["parts"][r]["pos"][0] + dx, B_["parts"][r]["pos"][1] + dy, B_["parts"][r]["angle"]) for r in (ref, "C1", "R1")}
        for r, v in out.items():                                                    # the real one relocates parts on the model it is handed
            B_["parts"][r]["pos"] = (v[0], v[1])
        return out
    monkeypatch.setattr(IT, "move_with_support", mws)
    c = mkctx(B)
    c["_cl"] = [{"ic": "L1", "supports": [{"ref": "C1"}, {"ref": "R1"}]}]
    g = PG.detect(c, ["row-rail"])["gaps"][0]
    snap = {r: p["pos"] for r, p in B["parts"].items()}
    cands = sorted(PG.RUNGS["pull-load"](g, c), key=lambda x: x["id"])[:PG.CANDS_PER_RUNG]
    assert {r: p["pos"] for r, p in B["parts"].items()} == snap                      # candidate generation leaves the shared model alone
    assert any(x["id"].endswith("-src") for x in cands) and any(x["id"].endswith("-load") for x in cands)
    src = next(x for x in cands if x["id"].endswith("-src"))
    d = src["moves"]["L1"][0] - 10.0
    assert d > 0 and src["moves"]["C1"][0] - 11 == pytest.approx(d) and src["moves"]["R1"][0] - 9 == pytest.approx(d)
    # a template row that was met and is no longer is refused by name (cin_side / sw rows hold or the move does not stay)
    base = [g]
    bad = PG.gap("row-template", "U102 template sw_mm|template", "plain", 0.3, refs=["U102"], row="support:U102 template sw_mm")
    ok, check, _w = PG._accept({"gaps": base, "state": {}}, {"gaps": [bad], "state": {}, "refusal": None}, g["id"])
    assert not ok and check == "row:support:U102 template sw_mm"


def test_pour_need_uses_the_worst_load_over_the_limit_and_is_refreshed_from_the_final_state(env):
    B = rail_board()
    c = mkctx(B, session=None)
    g = PG.detect(c, ["row-rail"])["gaps"][0]
    PG.RUNGS["pour-need"](g, c)
    assert c["pour_need"][NET]["copper_factor"] == round(140.0 / 122.0, 3)
    c["pour_need"][NET]["loads_over"] = ["stale"]
    B["parts"]["U1"]["pads"][0].update(x=140.0)                                      # a later round brought the load in: 130 mm, still over
    c["rows"] = rows(B)
    PG.pour_questions(c, {"gaps_out": [g]})
    rec = c["pour_need"][NET]
    assert rec["loads_over"] == ["U1"] and rec["worst_mm"] == 130.0 and rec["copper_factor"] == round(130.0 / 122.0, 3)


def test_file_eval_runs_the_drc_gate_on_an_equal_score_and_refuses_far_mm_rise(env, monkeypatch, tmp_path):
    from fluxplace import legalgate as LG, routediag as RD
    src = tmp_path / "in.kicad_pcb"
    src.write_text("x")
    ev = PG.FileEval(mkctx(rail_board()), str(src), str(src), str(tmp_path / "w"), legal_base={}, measure=lambda p: {"wl": 1.0, "cross": 1},
                     drc_ref_board=str(src))
    far = PG.gap("support-far", "C1", "decap", 3.0, refs=["C1"])
    ev.gaps = [far]
    seen = {"gaps": [dict(far)], "drc": 0}
    monkeypatch.setattr(PT, "_apply", lambda b, mv, out, mr: (open(out, "w").write("y"), (True, [], {}))[1])
    monkeypatch.setattr(RD, "load", lambda p: rail_board())
    monkeypatch.setattr(LG, "baseline", lambda B, cons: {})
    monkeypatch.setattr(LG, "new_issues", lambda i, b: {})
    monkeypatch.setattr(ev, "_gaps", lambda p: list(seen["gaps"]))
    monkeypatch.setattr(LG, "_drc", lambda *a, **k: [])

    def drc(*a, **k):
        seen["drc"] += 1
        return {"n": {"clearance": 1}, "items": [1], "pairs": [["C1", "C2"]]}
    monkeypatch.setattr(LG, "drc_new", drc)
    cand = {"id": "c", "moves": {"U1": (1, 2, 0)}}
    tr = ev.trial(cand)                                                               # equal score: DRC still runs and refuses
    assert seen["drc"] == 1 and tr["refusal"].startswith("drc:")
    seen["gaps"] = [dict(far, mag=3.5)]
    assert ev.trial(cand)["refusal"].startswith("score:far_mm")
