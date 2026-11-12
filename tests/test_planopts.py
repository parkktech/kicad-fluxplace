"""plan-options (planopts.py) on a synthetic board.  Pure text; worker processes are forked."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import planopts as PO, planroute as PL, routediag as RD

N = 20


def strip_board(tmp, n=N, h=4.0, eth=False):
    """A strip y 30..30+h carrying n nets from a left pad block to a right pad block (nets cross the whole board)."""
    pads_l = [PB.pad("1", 0, 0, f"S{i}", 0.2, 0.2) for i in range(n)]
    body = ""
    for i in range(n):
        row = i % 2
        x = 4 + 0.25 * (i // 2)
        y = 31.5 + 3.0 * row
        body += PB.fp(f"L{i}", x, y, [PB.pad("1", 0, 0, f"S{i}", 0.2, 0.2)])
        body += PB.fp(f"R{i}", 100 - x, y, [PB.pad("1", 0, 0, f"S{i}", 0.2, 0.2)])
    edge = f'(gr_rect (start 0 30) (end 100 {30 + h}) (layer "Edge.Cuts"))\n'
    p = tmp / "s.kicad_pcb"
    p.write_text(PB.HEAD + edge + body + ")\n")
    return str(p)


def test_growth_text_moves_the_edge(tmp_path):
    p = strip_board(tmp_path, n=4)
    B = RD.load(p)
    t = PO.grow_text(open(p).read(), B, (0, 0, 2, 0))
    q = tmp_path / "g.kicad_pcb"
    q.write_text(t)
    assert PO.board_size(str(q)) == (100.0, 6.0)
    assert PO.board_size(p) == (100.0, 4.0)


def test_narrow_board_passes_after_growing_one_edge(tmp_path):
    p = strip_board(tmp_path)
    cons = {}
    r = PO.search(p, cons, None, str(tmp_path / "out"), max_grow=6, workers=2, place_time=0.0, base_place_time=0.0, log=lambda m: None)
    assert not r["baseline"]["pass"] and not r["fits"]
    grows = [o for o in r["ok"] if any(o["grow"]) and not o["rules"]]
    assert grows, r["prompt"]
    best = r["ok"][0]
    assert best["size"][1] > 4.0 and best["size"][0] == 100.0 and best["pass"]
    assert os.path.exists(best["board"]) and os.path.exists(best["board"].replace(".kicad_pcb", ".fluxmap.json"))
    assert "Fails at 100 x 4 mm" in r["prompt"] and "Recommended" in r["prompt"]
    assert r["caveat"].startswith("verdict unproven")


def test_fits_as_is(tmp_path):
    p = strip_board(tmp_path, n=6, h=20.0)
    r = PO.search(p, {}, None, str(tmp_path / "out2"), max_grow=2, workers=1, place_time=0.0, base_place_time=0.0, log=lambda m: None)
    assert r["fits"] and r["prompt"].startswith("Fits as is")


def test_sub_floor_option_is_labelled_and_never_recommended():
    base = {"controlled": {}, "return_path": 0}
    mk = lambda name, area, rules, sub, ok=True: {"name": name, "pass": ok, "rules": rules, "area_cm2": area, "cost": area, "confidence": 50,
                                                  "score": 1, "overflow": 0, "sealed": 0, "no_via_site": 0, "subfloor": sub, "neck_violations": [],
                                                  "controlled": {}, "return_path": 0, "grow": [0, 0, 0, 0], "size": [1, 1]}
    a = mk("neck100", 50.0, {"neck": {"f": {"refs": ["U1"], "track_mm": 0.10, "clearance_mm": 0.10}}}, True)
    b = mk("grow", 55.0, {}, False)
    ok, sub, miss = PO.rank([a, b], base)
    assert [r["name"] for r in ok] == ["grow"] and [r["name"] for r in sub] == ["neck100"]
    assert PO.subfloor(a["rules"]) and "sub-floor" in a["impact"]


def test_local_neck_down_costs_less_than_global():
    cons = {}
    c_loc, _ = PO.relative_cost(100.0, {"neck": {"f": {"refs": ["U1"], "track_mm": 0.127, "clearance_mm": 0.127}}}, 4, cons, 4)
    c_glo, _ = PO.relative_cost(100.0, {"track_mm": 0.127, "clearance_mm": 0.127}, 4, cons, 4)
    c_std, _ = PO.relative_cost(100.0, {}, 4, cons, 4)
    c_8, _ = PO.relative_cost(100.0, {"variant": "8layer"}, 8, cons, 4)
    assert c_std < c_loc < c_glo < c_8


def test_neck_down_never_thins_an_ethernet_pair(tmp_path):
    """The neck-down narrows capacity for low-speed nets only: an ETH pair keeps 3 pitches of its designed width inside it, so a neck-down
    through a channel the pair cannot fit is reported (neck_violations) and the option is never recommended."""
    body = ""
    for i, n in enumerate(("ETH_P0_P", "ETH_P0_N")):
        body += PB.fp(f"A{i}", 10, 40 + i * 0.8, [PB.pad("1", 0, 0, n, 0.5, 0.5)]) + PB.fp(f"B{i}", 90, 40 + i * 0.8, [PB.pad("1", 0, 0, n, 0.5, 0.5)])
    body += PB.keepout(40, 0, 60, 39.2, "wallN") + PB.keepout(40, 41.5, 60, 80, "wallS")
    p = PB.write(tmp_path, body)
    cons = PO.scratch_cons({}, {"neck": {"f": {"refs": ["A0", "B0"], "track_mm": 0.127, "clearance_mm": 0.127, "margin_mm": 60.0}}})
    R = PL.run(RD.load(p), cons, None, ignore_routing=True)
    gr = R["gr"]
    assert gr.nets["ETH_P0_N"]["fixedw"] or gr.nets["ETH_P0_P"]["fixedw"]
    assert max(gr.mult) > 1.0                     # the pair's demand grows where the pitch shrinks
    from fluxplace.globalroute import is_controlled
    assert is_controlled("ETH_P0_P", R["cls"]["ETH_P0_P"], {})
    # an option record that overflows inside the neck is invalid and never recommended
    bad = {"neck_violations": ["ETH_P0_P"], "subfloor": False, "rules": {}, "controlled": {}, "return_path": 0, "pass": True,
           "area_cm2": 1.0, "cost": 1.0, "confidence": 1, "score": 1, "overflow": 0, "sealed": 0, "no_via_site": 0, "name": "x"}
    ok, sub, miss = PO.rank([bad], {"controlled": {}, "return_path": 0})
    assert not ok and sub and "never recommended" in sub[0]["impact"]


def test_option_that_lengthens_a_controlled_pair_ranks_below_an_equal_one():
    base = {"controlled": {"ETH_P0_P": [50.0, 2]}, "return_path": 0}
    mk = lambda name, ln, rp=0: {"name": name, "pass": True, "rules": {}, "area_cm2": 50.0, "cost": 50.0, "confidence": 50, "score": 1, "overflow": 0,
                                 "sealed": 0, "no_via_site": 0, "subfloor": False, "neck_violations": [], "grow": [0] * 4, "size": [1, 1],
                                 "controlled": {"ETH_P0_P": [ln, 2]}, "return_path": rp}
    longer, same = mk("longer", 62.0), mk("same", 50.5)
    ok, _s, _m = PO.rank([longer, same], base)
    assert [r["name"] for r in ok] == ["same", "longer"] and "worse" in ok[1]["impact"]
    ok, _s, _m = PO.rank([mk("rp", 50.0, rp=3), same], base)
    assert ok[0]["name"] == "same"
