"""PLACEGAPS phase 7: row-cin-side / row-template / row-pin-bypass rungs (template-turn, unit-turn, clear-side, re-deal) on a synthetic
buck unit (real clusters / seater / budget rows on a generated board; no router, no KiCad)."""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
import pcbgen as g  # noqa: E402
import powerboard as PW  # noqa: E402
import test_layout_template as T  # noqa: E402
from fluxplace import budget as BU, geom as GM, placegaps as PG, placetour as PT, routediag as RD  # noqa: E402

FIXED = set()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FIXED.clear()
    monkeypatch.setattr(PT, "_movable", lambda B, cons, P: sorted(set(B["parts"]) - FIXED))


def unit(tmp_path, extra="", c1=(30.0, 25.0), cons_extra=None):
    """U1 (30, 30), pins VIN 1, GND 2 on the left package side; C1 starts above the IC (off the datasheet side)."""
    b = g.ic("U1", 30, 30, ["VIN", "GND", "SW", "BOOT", "FB", "EN"], pitch=1.0)
    b += PW.cap("C1", c1[0], c1[1], "VIN", "GND", "10uF") + PW.cap("C2", 27, 34, "BOOT", "SW", "100nF")
    b += PW.cap("L1", 36, 31, "SW", "VOUT", "") + PW.cap("R1", 27, 38, "FB", "VOUT", "") + extra
    board = g.board(tmp_path, b, outline=(0, 0, 80, 60))
    return board, T._datasheets(tmp_path, T.LAY)


def rows_of(B, cons):
    return BU.template_rows(B, cons, T.SPEC)


def mkctx(board, cons, B=None):
    B = B or RD.load(board)
    return PG.Ctx(stage="floorplan", board=board, cons=cons, spec=T.SPEC, ds_dir=None, B=B, rows=rows_of(B, cons))


def served(B, cons):
    return next(r["value"] for r in rows_of(B, cons) if r["name"].endswith("cin_side"))


def applied(B, moves):
    B2 = copy.deepcopy(B)
    for r, (x, y, a) in moves.items():
        GM.place(B2["parts"][r], x, y, a)
    return B2


class ModelEval(PG.Evaluator):
    """Applies a candidate on the board model and measures every row again (what FileEval does on files)."""
    def __init__(self, ctx):
        self.ctx, self.B = ctx, ctx.B()

    def _gaps(self, B):
        c = PG.Ctx(stage="floorplan", board=self.ctx["board"], cons=self.ctx["cons"], spec=T.SPEC, ds_dir=None, B=B, rows=rows_of(B, self.ctx["cons"]))
        return PG.detect(c, PG.SCOPE["floorplan"])["gaps"]

    def detect(self, kinds):
        return self._gaps(self.B)

    def state(self):
        return {"illegal": 0}

    def board(self):
        return self.B

    def trial(self, cand):
        B2 = applied(self.B, cand["moves"])
        return {"gaps": self._gaps(B2), "state": {"illegal": 0}, "refusal": None, "B": B2}

    def commit(self, tr):
        self.B = tr["B"]
        self.ctx.pop("_cl", None)
        self.ctx["B"], self.ctx["rows"] = self.B, rows_of(self.B, self.ctx["cons"])


def cin_gap(c):
    return next(x for x in PG.detect(c, ["row-cin-side"])["gaps"])


def test_rows_map_to_the_three_kinds_and_are_gaps(tmp_path):
    board, cons = unit(tmp_path)
    c = mkctx(board, cons)
    c["rows"].append({"kind": "support", "name": "U1.3 own bypass (C2)", "metric": "pin-bypass", "value": 5.0, "limit_mm": 3.0, "max": 3.0, "target": 3.0,
                      "hard": True, "verdict": "fail", "refs": ["C2", "U1"], "nets": ["BOOT"]})
    gs = {g_["kind"]: g_ for g_ in PG.detect(c, ["row-cin-side", "row-template", "row-pin-bypass"])["gaps"]}
    assert set(gs) == {"row-cin-side", "row-template", "row-pin-bypass"}
    assert gs["row-cin-side"]["row"] == "support:U1 template cin_side" and gs["row-pin-bypass"]["id"].endswith("|pin-bypass")


def test_template_turn_mirror_and_quarter_turns_serve_the_input_pair(tmp_path):
    board, cons = unit(tmp_path)
    c = mkctx(board, cons)
    B = c.B()
    assert served(B, cons) == 0
    g_ = cin_gap(c)
    cands = PG.RUNGS["template-turn"](g_, c)
    assert len({x["id"] for x in cands}) == len(cands) > 1 and any("-m" in x["id"] for x in cands)      # mirrored poses are proposed
    assert all("U1" not in x["moves"] for x in cands)                                                  # the regulator stays
    assert any(served(applied(B, x["moves"]), cons) == 1 for x in cands)
    res = PG.close(c, ["row-cin-side"], ModelEval(c), stage="t")
    assert res["accepted"] and res["accepted"][0]["rung"] == "template-turn"
    assert not [x for x in res["gaps_out"] if x["kind"] == "row-cin-side"] and any(x["kind"] == "row-cin-side" for x in res["gaps_in"])
    hr = PG.TERMS.index("hard_rows")
    assert res["score_out"][hr] < res["score_in"][hr]


def test_determinism(tmp_path):
    board, cons = unit(tmp_path)
    a = PG.RUNGS["template-turn"](cin_gap(mkctx(board, cons)), mkctx(board, cons))
    b = PG.RUNGS["template-turn"](cin_gap(mkctx(board, cons)), mkctx(board, cons))
    assert [(x["id"], x["moves"]) for x in a] == [(x["id"], x["moves"]) for x in b]


def walled(tmp_path):
    """Fixed foreign parts wall the left package side (where the pair is); the right side is free."""
    extra = "".join(PW.cap(f"X{i}", x, y, f"XA{i}", f"XB{i}", "100nF") for i, (x, y) in enumerate(
        [(26.85, 29.5), (26.85, 31.4), (26.85, 27.6), (24.6, 29.5), (24.6, 31.4), (24.6, 27.6), (28.4, 25.4), (22.4, 29.5), (22.4, 31.4), (22.4, 27.6),
         (26.85, 33.3), (26.85, 25.7), (24.6, 33.3), (24.6, 25.7), (22.4, 33.3), (22.4, 25.7)]))
    board, cons = unit(tmp_path, extra)
    FIXED.update(f"X{i}" for i in range(16))
    return board, cons


def test_unit_turn_faces_the_pair_side_to_free_board_when_a_fixed_wall_holds_the_side(tmp_path):
    board, cons = walled(tmp_path)
    c = mkctx(board, cons)
    B = c.B()
    g_ = cin_gap(c)
    assert not any(served(applied(B, x["moves"]), cons) == 1 for x in PG.RUNGS["template-turn"](g_, c) + PG.RUNGS["clear-side"](g_, c))
    cands = PG.RUNGS["unit-turn"](g_, c)
    assert cands and all("U1" in x["moves"] for x in cands)
    win = [x for x in cands if served(applied(B, x["moves"]), cons) == 1]
    assert win and not (set(win[0]["moves"]) & FIXED)
    res = PG.close(c, ["row-cin-side"], ModelEval(c), stage="t")
    assert any(a["rung"] == "unit-turn" for a in res["accepted"]) and not [x for x in res["gaps_out"] if x["kind"] == "row-cin-side"]


def test_clear_side_moves_the_part_in_the_strip_out_and_seats_the_capacitor(tmp_path):
    x = PW.cap("X9", 26.85, 29.5, "XA", "XB", "100nF")
    board, cons = unit(tmp_path, x)
    c = mkctx(board, cons)
    B = c.B()
    g_ = cin_gap(c)
    cands = PG.RUNGS["clear-side"](g_, c)
    assert cands and all("X9" in x_["moves"] for x_ in cands)
    strip_hits = lambda B2: GM.overlap(B2["parts"]["X9"]["court"], (24.0, 28.0, 27.35, 31.0), -0.02)
    assert strip_hits(B)
    ok = [x_ for x_ in cands if served(applied(B, x_["moves"]), cons) == 1 and not strip_hits(applied(B, x_["moves"]))]
    assert ok


def test_a_fixed_blocker_gives_no_clear_side_candidate(tmp_path):
    x = PW.cap("X9", 26.85, 29.5, "XA", "XB", "100nF")
    board, cons = unit(tmp_path, x)
    FIXED.add("X9")
    c = mkctx(board, cons)
    assert PG.RUNGS["clear-side"](cin_gap(c), c) == []


def test_fix_that_worsens_a_template_row_or_breaks_another_hard_row_is_refused():
    cin = PG.gap("row-cin-side", "U1 template cin_side|template", "plain", 1.0, refs=["U1", "C1"], row="support:U1 template cin_side")
    sw = PG.gap("row-template", "U1 template sw_mm|template", "plain", 0.5, refs=["U1"], row="support:U1 template sw_mm")
    fb = PG.gap("row-template", "U1 template fb_mm|template", "plain", 0.5, refs=["U1"], row="support:U1 template fb_mm")
    base = {"gaps": [cin, sw], "state": {}}
    ok, check, _ = PG._accept(base, {"gaps": [dict(sw, mag=0.9)], "state": {}, "refusal": None}, cin["id"])        # cin served, sw_mm worse
    assert not ok and check == "row:support:U1 template sw_mm"
    ok, check, _ = PG._accept(base, {"gaps": [sw, fb], "state": {}, "refusal": None}, cin["id"])                    # cin served, fb_mm newly over
    assert not ok and check == "row:support:U1 template fb_mm"
    ok, _c, _w = PG._accept(base, {"gaps": [dict(sw, mag=0.4)], "state": {}, "refusal": None}, cin["id"])
    assert ok


def test_a_row_template_gap_is_repaired_when_the_measured_loop_drops(tmp_path):
    board, cons = unit(tmp_path)
    c = mkctx(board, cons)
    gs = {x["id"]: x for x in PG.detect(c, ["row-template"])["gaps"]}
    g_ = gs["row-template:U1 template cin_loop_mm|template"]
    cands = PG.RUNGS["template-turn"](g_, c)
    assert cands
    res = PG.close(c, ["row-template"], ModelEval(c), stage="t")
    assert res["accepted"] and res["score_out"][PG.TERMS.index("hard_mag")] < res["score_in"][PG.TERMS.index("hard_mag")]


def bypass_board(tmp_path):
    """U1 with three VOUT pads; its bypass cap C9 stands far from pad 4 (a bare pin), no other VOUT cap near."""
    b = g.ic("U1", 30, 30, ["VOUT", "GND", "VOUT", "VOUT", "EN", "FB"], pitch=1.0)
    b += PW.cap("C9", 30, 44, "VOUT", "GND", "100nF")
    return g.board(tmp_path, b, outline=(0, 0, 80, 60))


def test_re_deal_seats_the_owned_bypass_at_the_bare_pin(tmp_path, monkeypatch):
    from fluxplace import clusters as CL
    board = bypass_board(tmp_path)
    B = RD.load(board)
    pin = next(q for q in B["parts"]["U1"]["pads"] if q["num"] == "1")
    cap = lambda B_: next(q for q in B_["parts"]["C9"]["pads"] if q["net"] == "VOUT")
    far = lambda B_: ((cap(B_)["x"] - pin["x"]) ** 2 + (cap(B_)["y"] - pin["y"]) ** 2) ** 0.5
    dem = {"ic": "U1", "pad": "1", "net": "VOUT", "parts": ["C9"], "uf": 0.1, "dist_mm": round(far(B), 2), "limit_mm": 3.0, "cover": None, "verdict": "fail"}
    monkeypatch.setattr(CL, "pin_demand", lambda B_, cons=None, spec=None, **k: [dem])
    row = {"kind": "support", "name": "U1.1 own bypass (C9)", "metric": "pin-bypass", "value": dem["dist_mm"], "max": 3.0, "target": 3.0, "hard": True,
           "verdict": "fail", "refs": ["C9", "U1"], "nets": ["VOUT"]}
    c = PG.Ctx(stage="floorplan", board=board, cons={}, spec={}, ds_dir=None, B=B, rows=[row])
    c["_cl"] = [{"ic": "U1", "regulator": False, "supports": [{"ref": "C9", "ic": "U1", "pad": "3", "via": next(q["num"] for q in B["parts"]["C9"]["pads"] if q["net"] == "VOUT"),
                                                              "net": "VOUT", "cls": "decap", "rank": 9, "weight": 1, "sure": True, "how": "refdes", "dist": 14.0, "limit": 3.0,
                                                              "target": 3.0}]}]
    g_ = PG.detect(c, ["row-pin-bypass"])["gaps"][0]
    cands = PG.RUNGS["re-deal"](g_, c)
    assert cands and list(cands[0]["moves"]) == ["C9"] and far(applied(B, cands[0]["moves"])) < far(B)
    assert far(applied(B, cands[0]["moves"])) <= 3.0 + 1.0


def test_a_cin_side_repair_that_does_not_serve_a_pair_is_refused_even_if_the_score_falls():
    cin = PG.gap("row-cin-side", "U1 template cin_side|template", "plain", 0.5, refs=["U1", "C1"], row="support:U1 template cin_side")
    base = {"gaps": [cin], "state": {"far_mm": 0}}
    ok, check, why = PG._accept(base, {"gaps": [cin], "state": {}, "refusal": None}, cin["id"])
    assert not ok and check == "score:row-cin-side" and "not closer" in why
    assert PG._accept(base, {"gaps": [dict(cin, mag=0.2)], "state": {}, "refusal": None}, cin["id"])[0]
    assert PG._accept(base, {"gaps": [], "state": {}, "refusal": None}, cin["id"])[0]


# ---- cin-re-deal: more named pin pairs than input caps
LAY2 = dict(T.LAY, cin_side=[["VIN", "GND"], ["VIN2", "GND2"]])
SPEC2 = {"components": [{"ref": "U1", "mpn": "REG1", "pinmap": {"VIN": "1", "GND": "2", "SW": "3", "BOOT": "4", "FB": "5", "EN": "6", "VIN2": "7", "GND2": "8"}}]}


def two_pairs(tmp_path, c3_value="10uF"):
    """U1 with a pair on pads 1/2 (C1 serves it) and a second pair on pads 7/8; C3 is a free capacitor of the same net far away."""
    b = g.ic("U1", 30, 30, ["VIN", "GND", "SW", "BOOT", "FB", "EN", "VIN", "GND"], pitch=1.0)
    b += PW.cap("C1", 26, 30, "VIN", "GND", "10uF") + PW.cap("C3", 60, 50, "VIN", "GND", c3_value)
    b += PW.cap("C2", 27, 34, "BOOT", "SW", "100nF") + PW.cap("L1", 36, 36, "SW", "VOUT", "") + PW.cap("R1", 27, 38, "FB", "VOUT", "")
    board = g.board(tmp_path, b, outline=(0, 0, 80, 60))
    return board, T._datasheets(tmp_path, LAY2)


def mk2(board, cons, owners=None, extra=()):
    from fluxplace import clusters as CL
    B = RD.load(board)
    rows = BU.template_rows(B, cons, SPEC2)
    c = PG.Ctx(stage="floorplan", board=board, cons=cons, spec=SPEC2, ds_dir=None, B=B, rows=rows)
    cl, _ = CL.build(B, cons, SPEC2)
    for k in cl:
        k["supports"] = [s for s in k["supports"] if s["ref"] != "C3"]                 # C3 is nobody's (no cluster owns it)
    c["_cl"] = cl + list(extra)
    return c


def test_cin_re_deal_serves_the_second_pair_with_a_free_cap_of_the_same_net(tmp_path):
    board, cons = two_pairs(tmp_path)
    c = mk2(board, cons)
    row = next(r for r in c["rows"] if r["name"].endswith("cin_side"))
    assert (row["value"], row["max"]) == (1, 2)
    g_ = next(x for x in PG.detect(c, ["row-cin-side"])["gaps"])
    cands = PG.RUNGS["cin-re-deal"](g_, c)
    assert cands and all(list(x["moves"]) == ["C3"] for x in cands)
    B2 = applied(c.B(), cands[0]["moves"])
    assert next(r for r in BU.template_rows(B2, cons, SPEC2) if r["name"].endswith("cin_side"))["value"] == 2
    assert c["cin_need"][g_["id"]]["owned"] == ["C1"] and c["cin_need"][g_["id"]]["caps"][0]["ref"] == "C3"


def test_cin_re_deal_takes_a_cap_from_another_cluster_only_when_it_keeps_enough(tmp_path):
    board, cons = two_pairs(tmp_path)
    sole = {"ic": "U2", "supports": [{"ref": "C3", "net": "VIN", "sure": True, "rank": 9}]}
    c = mk2(board, cons, extra=[sole])
    g_ = next(x for x in PG.detect(c, ["row-cin-side"])["gaps"])
    assert PG.RUNGS["cin-re-deal"](g_, c) == []
    assert "owned by U2, which would keep 0 of the 1 cap(s)" in c["cin_need"][g_["id"]]["caps"][0]["why"]
    two = {"ic": "U2", "supports": [{"ref": "C3", "net": "VIN", "sure": True, "rank": 9}, {"ref": "C1", "net": "VIN", "sure": True, "rank": 9}]}
    c2 = mk2(board, cons, extra=[two])
    g2 = next(x for x in PG.detect(c2, ["row-cin-side"])["gaps"])
    assert PG.RUNGS["cin-re-deal"](g2, c2)


def test_no_usable_cap_is_a_refused_entry_and_one_owner_question_with_the_numbers(tmp_path):
    board, cons = two_pairs(tmp_path, c3_value="100nF")
    c = mk2(board, cons)

    class S:
        asked = []

        def ask(self, qid, topic, found, options, default, settle, apply=None):
            self.asked.append((qid, found, sorted(options), default))
            return default
    s = S()
    c["session"] = s
    g_ = next(x for x in PG.detect(c, ["row-cin-side"])["gaps"])

    class Same(PG.Evaluator):
        def detect(self, kinds):
            return [g_]

        def board(self):
            return c.B()

        def trial(self, cand):
            return {"gaps": [g_], "state": {}, "refusal": None}

        def commit(self, tr):
            pass
    res = PG.close(c, ["row-cin-side"], Same(), stage="t")
    none = [r for r in res["refused"] if r["rung"] == "cin-re-deal"]
    assert none and none[0]["check"] == "no-candidate" and "C3: 0.1 uF is below the input cap value 10 uF" in none[0]["why"]
    assert len(s.asked) == 1 and s.asked[0][0] == "row:U1 template cin_side"
    assert "2 input pin pair(s)" in s.asked[0][1] and "owns 1 input cap(s) (C1)" in s.asked[0][1] and "C3" in s.asked[0][1]


def test_a_leftover_row_gets_a_refused_entry_even_when_a_rung_proposes_nothing():
    cin = PG.gap("row-cin-side", "U1 template cin_side|template", "plain", 0.5, refs=["U1"], row="support:U1 template cin_side")

    class E(PG.Evaluator):
        def detect(self, kinds):
            return [cin]

        def board(self):
            return {"parts": {}}

        def trial(self, cand):
            raise AssertionError

        def commit(self, tr):
            pass
    old = PG.RUNGS.get("template-turn")
    PG.RUNGS["template-turn"] = lambda g_, c_: []
    try:
        res = PG.close(PG.Ctx(stage="floorplan", board="/x", cons={}, spec={}, ds_dir=None), ["row-cin-side"], E(), stage="t")
    finally:
        PG.RUNGS["template-turn"] = old
    assert any(r["rung"] == "template-turn" and r["check"] == "no-candidate" for r in res["refused"])
