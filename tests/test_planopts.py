"""plan-options (planopts.py) on synthetic boards.  Pure text; worker processes are forked."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import escapeplan as EP, planopts as PO, planroute as PL, routediag as RD

N = 18


def strip_board(tmp, n=N, h=4.0, extra=""):
    """A strip y 30..30+h carrying n nets from a left pad block to a right pad block (nets cross the whole board)."""
    body = ""
    for i in range(n):
        x, y = 4 + 0.9 * (i // 2), 31.5 + 3.0 * (i % 2)
        body += PB.fp(f"L{i}", x, y, [PB.pad("1", 0, 0, f"S{i}", 0.2, 0.2)])
        body += PB.fp(f"R{i}", 100 - x, y, [PB.pad("1", 0, 0, f"S{i}", 0.2, 0.2)])
    edge = f'(gr_rect (start 0 30) (end 100 {30 + h}) (layer "Edge.Cuts"))\n'
    p = tmp / "s.kicad_pcb"
    p.write_text(PB.HEAD + edge + body + extra + ")\n")
    return str(p)


def test_growth_moves_only_the_outer_outline(tmp_path):
    cut = '(gr_rect (start 40 31) (end 44 33) (layer "Edge.Cuts"))\n'           # an interior cut-out
    p = strip_board(tmp_path, n=4, extra=cut)
    B = RD.load(p)
    t = PO.grow_text(open(p).read(), B, (0, 0, 2, 0))
    assert "(start 40 31) (end 44 33)" in t                                        # the cut-out did not travel with the top edge
    q = tmp_path / "g.kicad_pcb"
    q.write_text(t)
    assert PO.board_size(str(q)) == (100.0, 6.0) and PO.board_size(p) == (100.0, 4.0)


def test_listed_edge_connector_travels_even_when_set_back(tmp_path):
    conn = PB.fp("J9", 50, 33.5, [PB.pad("1", 0, 0, "X", 1, 1), PB.pad("2", 1.5, 0, "X", 1, 1)])      # 3.5 mm from the top edge
    p = strip_board(tmp_path, n=4, h=8.0, extra=conn)
    B = RD.load(p)
    t0 = PO.grow_text(open(p).read(), B, (0, 0, 3, 0))
    t1 = PO.grow_text(open(p).read(), B, (0, 0, 3, 0), {"plan": {"edge_parts": ["J9"]}})
    y = lambda t: RD.load(_write(tmp_path, t))["parts"]["J9"]["pos"][1]
    assert y(t0) == 33.5 and y(t1) == 30.5


def _write(tmp, t):
    f = tmp / "t.kicad_pcb"
    f.write_text(t)
    return str(f)


def test_narrow_board_passes_after_growing_one_edge(tmp_path):
    p = strip_board(tmp_path)
    r = PO.search(p, {}, None, str(tmp_path / "out"), max_grow=6, workers=2, place_evals=0, base_place_evals=0, log=lambda m: None)
    assert not r["baseline"]["pass"] and not r["fits"]
    grows = [o for o in r["ok"] if any(o["grow"]) and not o["rules"]]
    assert grows and all(o["pass"] for o in r["ok"])
    best = grows[0]
    assert best["size"][1] > 4.0 and best["size"][0] == 100.0
    assert r["ok"][0]["area_cm2"] <= best["area_cm2"]                       # ranked by area, then cost
    assert os.path.exists(best["board"]) and os.path.exists(best["board"].replace(".kicad_pcb", ".fluxmap.json"))
    assert "Fails at 100 x 4 mm" in r["prompt"] and "Recommended" in r["prompt"]
    assert r["caveat"].startswith("verdict unproven")


def test_fits_as_is(tmp_path):
    p = strip_board(tmp_path, n=6, h=20.0)
    r = PO.search(p, {}, None, str(tmp_path / "out2"), max_grow=2, workers=1, place_evals=0, base_place_evals=0, log=lambda m: None)
    assert r["fits"] and r["prompt"].startswith("Fits as is")


def test_sub_floor_plan_value_is_raised_to_the_floor_and_recorded():
    pr = EP.plan_params({"plan": {"track_mm": 0.10, "clearance_mm": 0.10}})
    assert pr["track_mm"] == 0.127 and pr["clearance_mm"] == 0.127 and len(pr["floor_clamped"]) == 2
    pr = EP.plan_params({"plan": {"track_mm": 0.10, "clearance_mm": 0.10, "allow_subfloor": True}})
    assert pr["track_mm"] == 0.10 and not pr["floor_clamped"]
    assert EP.plan_params({"plan": {"floor_mm": 0.15, "track_mm": 0.127}})["track_mm"] == 0.15


def _opt(name, area=50.0, rules=None, sub=False, ok=True, ctl=None, rp=0, neck=()):
    return {"name": name, "pass": ok, "rules": rules or {}, "area_cm2": area, "cost": area, "confidence": 50, "score": 1, "overflow": 0,
            "sealed": 0, "no_via_site": 0, "subfloor": sub, "neck_violations": list(neck), "controlled": ctl if ctl is not None else {},
            "return_path": rp, "grow": [0] * 4, "size": [1, 1], "routable_pct": 95}


def test_sub_floor_options_are_not_offered_in_the_prompt():
    base = dict(_opt("baseline", ok=False), controlled={}, size=[86.5, 98.6], cost=100.0, score=2000.0, overflow=9.0, sealed=0)
    neck = {"neck": {"f": {"refs": ["U1"], "track_mm": 0.10, "clearance_mm": 0.10}}}
    ok, sub, miss = PO.rank([_opt("neck100", rules=neck, sub=True), _opt("grow", area=60.0)], base)
    txt = PO.prompt_text(base, ok, sub, miss)
    assert [r["name"] for r in ok] == ["grow"] and "Recommended: +" not in txt.split("Not offered")[0].replace("Recommended: as is", "")
    assert "Not offered (below the owner floor" in txt and txt.index("Recommended") < txt.index("Not offered")
    table = PO.format_table(dict(base, size=[86.5, 98.6]), ok, sub, miss)
    assert "not offered: below owner floor" in table and "[PASS-NOT-RECOMMENDED]" not in table


def test_controlled_guard_is_per_net_zero_vias_and_fails_closed():
    base = {"controlled": {"ETH_P0_P": [50.0, 2, 40.0], "ETH_P0_N": [50.0, 2, 40.0]}, "return_path": 0}
    good = _opt("good", ctl={"ETH_P0_P": [50.2, 2, 40.0], "ETH_P0_N": [50.1, 2, 40.0]})
    one_via = _opt("one_via", ctl={"ETH_P0_P": [50.0, 3, 40.0], "ETH_P0_N": [50.0, 2, 40.0]})
    partner_long = _opt("partner_long", ctl={"ETH_P0_P": [50.0, 2, 40.0], "ETH_P0_N": [58.0, 2, 40.0]})
    missing = _opt("missing", ctl={"ETH_P0_P": [50.0, 2, 40.0]})
    grown = _opt("grown", ctl={"ETH_P0_P": [56.0, 2, 46.0], "ETH_P0_N": [56.0, 2, 46.0]})        # the pads moved 6 mm apart: +6 forced
    ok, sub, _m = PO.rank([good, one_via, partner_long, missing, grown], base)
    assert [r["name"] for r in ok] == ["good", "grown"]
    assert {r["name"] for r in sub} == {"one_via", "partner_long", "missing"}
    assert any("not planned in the option" in w for w in missing["perf_why"]) and any("ETH_P0_N" in w for w in partner_long["perf_why"])
    assert not PO.rank([_opt("rp", ctl=good["controlled"], rp=2)], base)[0]


def test_controlled_metrics_count_both_legs_of_a_pair(tmp_path):
    body = ""
    for i, n in enumerate(("ETH_P0_P", "ETH_P0_N")):
        body += PB.fp(f"A{i}", 10, 40 + i * 1.5, [PB.pad("1", 0, 0, n, 0.5, 0.5)]) + PB.fp(f"B{i}", 90, 40 + i * 1.5, [PB.pad("1", 0, 0, n, 0.5, 0.5)])
    R = PL.run(RD.load(PB.write(tmp_path, body)), {}, None, ignore_routing=True)
    R["cons"] = {}
    m = PO.controlled_metrics(R)
    assert set(m) == {"ETH_P0_P", "ETH_P0_N"} and m["ETH_P0_P"][:2] == m["ETH_P0_N"][:2] and m["ETH_P0_P"][0] > 70


def test_local_neck_down_costs_less_than_global():
    cons = {}
    c_loc, _ = PO.relative_cost(100.0, {"neck": {"f": {"refs": ["U1"], "track_mm": 0.127, "clearance_mm": 0.127}}}, 4, cons, 4)
    c_glo, _ = PO.relative_cost(100.0, {"track_mm": 0.127, "clearance_mm": 0.127}, 4, cons, 4)
    c_std, _ = PO.relative_cost(100.0, {}, 4, cons, 4)
    c_8, _ = PO.relative_cost(100.0, {"variant": "8layer"}, 8, cons, 4)
    assert c_std < c_loc < c_glo < c_8


def test_neck_down_overflow_on_a_controlled_net_makes_the_option_invalid():
    ok, sub, _m = PO.rank([_opt("bad", neck=["ETH_P0_P"])], {"controlled": {}, "return_path": 0})
    assert not ok and sub and "never recommended" in sub[0]["impact"]
