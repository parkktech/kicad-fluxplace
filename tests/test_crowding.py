import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import json
import pytest
from fluxplace import density as DN, questions as QS, routediag as RD
import pcbgen as g

BOTH = {"placement": {"sides": "both", "back_max_height_mm": 2.2}}
BIG = [(25, 15), (75, 15), (25, 40), (75, 40), (25, 65), (75, 65)]


def _big(n=6, area_court=(-20, -12.5, 20, 12.5), side="F"):
    return "".join(g.two(f"U{i + 1}", x, y, f"X{i}", f"Y{i}", name="Big", court=area_court, side=side) for i, (x, y) in enumerate(BIG[:n]))


def _small(n=120):
    out = ""
    for i in range(n):
        out += g.two(f"R{i + 1}", 4 + (i % 30) * 3.0, 4 + (i // 30) * 3.0, f"N{i}", f"M{i}", name="R_0603_1608Metric", court=(-1.5, -1.5, 1.5, 1.5))
    return out


def _B(tmp_path, body, name="b.kicad_pcb"):
    return RD.load(g.board(tmp_path, body, name=name))


def test_usage_known_areas(tmp_path):
    B = _B(tmp_path, _big(3))                                     # 3 x 1000 mm2 free on an 8000 mm2 board
    u = DN.usage(B, {})
    assert u["outline_mm2"] == pytest.approx(8000, rel=1e-3)
    f = u["sides"]["F"]
    assert f["load_mm2"] == pytest.approx(3000) and f["usable_mm2"] == pytest.approx(8000, rel=1e-3) and f["util"] == pytest.approx(0.375, rel=1e-2)
    assert u["sides"]["B"]["load_mm2"] == 0


def test_closed_area_and_fixed_parts_reduce_usable(tmp_path):
    cons = {"placement": {"fixed": ["U1"], "keepout": {"m": {"rect": [50, 0, 100, 80], "side": "B"}}}}
    u = DN.usage(_B(tmp_path, _big(2)), cons)
    assert u["sides"]["F"]["usable_mm2"] == pytest.approx(8000 - 1000, rel=1e-2)            # U1 is fixed: its courtyard is not usable
    assert u["sides"]["F"]["load_mm2"] == pytest.approx(1000)
    assert u["sides"]["B"]["usable_mm2"] == pytest.approx(4000, rel=1e-2) and u["sides"]["B"]["closed_mm2"] == pytest.approx(4000, rel=1e-2)


def test_density_line_and_verdicts(tmp_path):
    B = _B(tmp_path, _big(5) + _small(20))
    rep = DN.crowding_report(B, BOTH)
    ln = rep["line"]
    assert ln.startswith("density: F ") and "mm2 (warn)" in ln and ", B 0 % of 8000 mm2" in ln and "hot cells (>= 85 %)" in ln
    assert "movable small parts F->B eligible: 20 (180 mm2)" in ln
    assert rep["verdicts"]["F"]["verdict"] in ("warn", "fail") and rep["verdicts"]["B"]["verdict"] == "ok"
    # defaults live in one place and are overridable in [placement]
    assert DN.limits({})["warn"] == 0.50 and DN.limits({"placement": {"density_warn": 0.4}})["warn"] == 0.4
    gp = DN.gaps(B, BOTH, None, rep)
    assert {x["kind"] for x in gp} <= {"density-hot", "side-load"} and any(x["id"] == "side-load:F" for x in gp)
    assert all(set(x) >= {"id", "kind", "class", "mag", "refs", "nets", "at", "row", "detail"} for x in gp)


def test_ready_line_fails_only_above_density_fail(tmp_path):
    from fluxplace import ready as RDY
    ok = RDY.density_line(None, {}, _B(tmp_path, _small(5)))
    assert ok["status"] == "PASS" and not ok["detail"].startswith("WARN")
    warn = RDY.density_line(None, {}, _B(tmp_path, _big(4), name="w.kicad_pcb"))           # 50 %
    assert warn["status"] == "PASS" and warn["detail"].startswith("WARN")
    bad = RDY.density_line(None, {}, _B(tmp_path, _big(6), name="f.kicad_pcb"))            # 75 %
    assert bad["status"] == "FAIL" and "over 65 %" in bad["blockers"][0]


def test_size_suggestion_known_dims(tmp_path):
    B = _B(tmp_path, _big(6))                                     # 6000 mm2 free on the front, nothing else
    rep = DN.crowding_report(B, {})
    s = rep["suggestion"]
    assert s["required_mm2"]["F"] == pytest.approx(12000, rel=1e-2) and s["needed_outline_mm2"] == pytest.approx(12000, rel=1e-2)
    assert not s["fits"] and s["gain_mm2"] == pytest.approx(4000, rel=2e-2) and s["limit"]["source"] == "no enclosure limit declared"
    o = {x["key"].split("-")[1][0]: x for x in s["options"]}
    right = next(x for x in s["options"] if x["grow"][1] > 0 and not x["grow"][3])
    assert right["size_mm"][0] == pytest.approx(150, abs=0.3) and right["size_mm"][1] == pytest.approx(80)       # 4000 / 80 = 50 mm
    bottom = next(x for x in s["options"] if x["grow"][3] > 0 and not x["grow"][1])
    assert bottom["size_mm"] == pytest.approx([100, 120], abs=0.3)                                                # 4000 / 100 = 40 mm
    both = next(x for x in s["options"] if x["grow"][1] > 0 and x["grow"][3] > 0)
    assert (both["size_mm"][0] * both["size_mm"][1]) == pytest.approx(12000, rel=1e-2)
    assert all(x["predicted_util"]["F"] == pytest.approx(0.5, abs=0.01) for x in s["options"])
    # a declared enclosure limit is honoured and named
    lim = DN.suggest(B, {"placement": {"max_board_mm": [120, 100]}}, None, rep["usage"], rep["shortfall"])
    assert lim["limit"]["max_mm"] == [120.0, 100.0] and [x["within_limit"] for x in lim["options"]].count(False) >= 1
    assert DN.size_lines({**s})[0].startswith("size: needs 12000") and "no enclosure limit declared" in DN.size_lines(s)[0]
    fits = DN.crowding_report(_B(tmp_path, _big(2), name="f.kicad_pcb"), {})["suggestion"]
    assert fits["fits"] and DN.size_lines(fits)[0].startswith("size: fits")


def test_grow_key_roundtrip():
    assert DN.parse_grow(DN.grow_key((0, 8.5, 0, 0))) == (0.0, 8.5, 0.0, 0.0) and DN.parse_grow("keep") is None and DN.parse_grow(None) is None


def _crowded(tmp_path):
    return _B(tmp_path, _big(5) + _small(120))


def test_options_ranked_by_relief_per_cost_and_computed_from_the_board(tmp_path):
    B = _crowded(tmp_path)
    rep = DN.crowding_report(B, BOTH)
    ops, default = DN.options(B, BOTH, None, rep)
    keys = [o["key"] for o in ops]
    assert "a-fill" in keys and any(k.startswith("grow-") for k in keys)
    score = [o["relief"] / o["cost"] for o in ops]
    assert score == sorted(score, reverse=True)
    fill = next(o for o in ops if o["key"] == "a-fill")
    assert fill["text"].startswith("120 part(s) / 1080 mm2")                         # this board's eligible parts, not generic text
    assert fill["predicted"]["F"]["util"] == pytest.approx((5 * 1000 - 0 + 0) / 8000 + 0 - 0.0, abs=0.2)
    assert fill["predicted"]["B"]["util"] == pytest.approx(1080 / 8000, abs=0.01)
    assert fill["fixes"]["util"] in ("yes", "helps")
    ok = [o for o in ops if o["all_ok"]]
    assert default == (min(ok, key=lambda o: (o["cost"], o["key"]))["key"] if ok else "keep")
    if "e-layers" in keys:                                                          # last resort: no area relief
        e = next(o for o in ops if o["key"] == "e-layers")
        assert e["predicted"]["F"]["util"] == pytest.approx(rep["usage"]["sides"]["F"]["util"], abs=1e-3) and e["cost"] == max(o["cost"] for o in ops)


def test_height_raise_option_names_what_it_unlocks(tmp_path):
    tall = g.two("C900", 90, 70, "P", "Q", name="C_1210_3225Metric", court=(-1.5, -1.2, 1.5, 1.2))      # 2.7 mm: over the 2.2 mm limit
    B = _B(tmp_path, _big(5) + _small(100) + tall)
    light = {"placement": {"sides": "both-light-back", "back_max_height_mm": 2.2}}
    rep = DN.crowding_report(B, light)
    ops, _d = DN.options(B, light, None, rep)
    a = [o for o in ops if o["key"].startswith("a-height-")]
    assert a and a[0]["key"] == "a-height-2.7" and "unlocks 1 part(s)" in a[0]["text"] and a[0]["parts"] == ["C900"]


def test_crowding_question_lists_keep_and_every_option_and_never_applies(tmp_path):
    B = _crowded(tmp_path)
    cons = dict(BOTH)
    path = str(tmp_path / "b.kicad_pcb")
    S = QS.Session(path, cons, {}, False, "floorplan")
    lines = []
    ch, rep, ops = QS.ask_crowding(S, B, cons, None, out=lines.append)
    q = next(q for q in S.open if q["id"] == "crowding")
    assert [o["choice"] for o in q["options"]][0] == "keep" and {o["choice"] for o in q["options"]} >= {"keep", "a-fill"} | {o["key"] for o in ops}
    assert q["default"] == ch and any("predicted F" in o["consequence"] for o in q["options"])
    assert lines[0].startswith("crowding:") and "default" in lines[0] and any(l.startswith("  0. keep") for l in lines)
    assert cons == BOTH and S.apply(cons, None)[0] == BOTH                         # nothing applied by asking
    # assumed defaults are recorded as assumed, not applied
    S2 = QS.Session(path, cons, {}, True, "floorplan")
    QS.ask_crowding(S2, B, cons, None, out=lambda *a: None)
    assert "crowding" in S2.assumed and S2.apply(cons, None)[0] == BOTH


def test_crowding_question_not_asked_when_ok_and_answers_apply(tmp_path):
    B = _B(tmp_path, _small(5))
    S = QS.Session(str(tmp_path / "b.kicad_pcb"), BOTH, {}, False, "floorplan")
    assert QS.ask_crowding(S, B, BOTH, None, out=lambda *a: None)[0] is None and not S.open
    S = QS.Session("x", BOTH, {"crowding": "a-height-2.8"}, False, "floorplan")
    QS.apply_crowding_answer(S)
    assert S.apply(BOTH, None)[0]["placement"]["back_max_height_mm"] == 2.8 and BOTH["placement"]["back_max_height_mm"] == 2.2
    S = QS.Session("x", BOTH, {"crowding": "a-heavy:L1+L2"}, False, "floorplan")
    QS.apply_crowding_answer(S)
    assert S.apply(BOTH, None)[0]["placement"]["back_allow"] == ["L1", "L2"]
    S = QS.Session("x", {}, {"crowding": "grow-L0R8T0B0"}, False, "floorplan")
    assert DN.parse_grow(QS.board_grow_answer(S)) == (0.0, 8.0, 0.0, 0.0)


def test_check_crowding_in_ready_opens_the_question(tmp_path):
    B = _crowded(tmp_path)
    S = QS.Session(str(tmp_path / "b.kicad_pcb"), BOTH, {}, False, "ready")
    QS.check_crowding(S, B, BOTH, spec=None)
    assert [q["id"] for q in S.open] == ["crowding"]
