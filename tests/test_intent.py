"""Placement intent (intent.py, connectors.py, intentdraft.py): every key on a synthetic board, the gate, the ask path,
stage-fail propagation. Nothing here names a real board."""
import os, sys, tomllib
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cli
from fluxplace import (connectors as CN, constraints as C, geom as GM, intent as IT, intentdraft as ITD, legalise as LG,
                       legality as LGL, questions as QS, routediag as RD)
import pcbgen as g


def module(ref, x, y, side="F", name="RadioModule", **kw):
    """8-pad module, pads on the -x half, body reaching 4 mm to +x (its antenna end)."""
    pads = [g.pad(str(i + 1), -3.0, -1.5 + i, f"{ref}_N{i}", 0.6, 0.6) for i in range(4)]
    pads += [g.pad(str(i + 5), -1.0, -1.5 + i, f"{ref}_M{i}", 0.6, 0.6) for i in range(4)]
    return g.fp(ref, x, y, pads, name=name, side=side, court=(-4, -3, 5, 3), **kw)


def conn(ref, x, y, name, court=(-2, -2, 2, 2), n=3, typ="smd", **kw):
    return g.fp(ref, x, y, [g.pad(str(i + 1), 0, (i - (n - 1) / 2) * 1.0, f"{ref}_S{i}", 0.6, 0.6, typ=typ) for i in range(n)],
                name=name, court=court, **kw)


def cap(ref, x, y, n1, n2="GND", name="C_0402_1005Metric", **kw):
    return g.fp(ref, x, y, [g.pad("1", -0.5, 0, n1, 0.5, 0.5), g.pad("2", 0.5, 0, n2, 0.5, 0.5)], name=name,
                court=(-1.0, -0.5, 1.0, 0.5), **kw)


def load(tmp_path, body, **kw):
    return RD.load(g.board(tmp_path, body, **kw))


def rows(B, cons, kind=None):
    return [r for r in IT.check(B, cons) if kind is None or r["kind"] == kind]


# ------------------------------------------------------------------ edge + facing
def test_edge_and_facing_are_seated_and_gated(tmp_path):
    cons = {"intent": {"edge": [{"refs": ["U1"], "side": "not:top", "facing": "out", "front": "+x"}]}}
    B = load(tmp_path, module("U1", 50, 40) + cap("C1", 20, 20, "A"))
    r = rows(B, cons, "edge")[0]
    assert r["met"] is False and r["hard"] and IT.verdict(IT.check(B, cons), cons)[0] == 1
    S = IT.Seater(B, cons).run()
    r = rows(B, S.cons_out(), "edge")[0]
    assert r["met"] is True and "U1" in S.held and "U1" in S.cons_out()["placement"]["fixed"]
    side, gap, on, face, _z = IT.edge_state(B, S.cons_out(), "U1", IT.items(B, S.cons_out())["edge"][0])
    assert side != "top" and on and face and 0 <= gap <= 1.0
    assert LGL.count(LGL.Legal(B, S.cons_out()).issues()) == 0
    # the same check without `front`: the gate cannot judge facing and says what to give
    c2 = {"intent": {"edge": [{"refs": ["U1"], "side": "any", "facing": "out"}]}}
    r2 = rows(B, c2, "edge")[0]
    assert r2["met"] is False and "front unknown" in r2["detail"]


def test_edge_named_side_and_clear_depth(tmp_path):
    # U1 sits 2 mm inside the right edge facing out, with a part in the 2 mm in front of it
    body = module("U1", 93, 40) + cap("C1", 99.0, 40, "A")
    cons = {"intent": {"edge": [{"refs": ["U1"], "side": "right", "facing": "out", "front": "+x", "gap_mm": 3, "clear_mm": 2}]}}
    B = load(tmp_path, body)
    r = rows(B, cons, "edge")[0]
    assert r["met"] is False and "blocked by C1" in r["detail"]
    # the clear depth is a forbidden rectangle for everyone else once the part is held (placers and legaliser)
    L = LGL.Legal(B, cons, fixed={"U1"})
    assert L.issues()["forbidden"] == ["C1 in front of U1"]
    assert LGL.Legal(B, {}).issues()["forbidden"] == []
    S = IT.Seater(B, dict(cons, placement={"fixed": ["U1"]})).run()       # the engine moves the blocker out
    assert rows(B, S.cons_out(), "edge")[0]["met"] is True and "C1" in S.moves


def test_overhang_is_allowed_per_footprint(tmp_path):
    c = conn("J1", 97.0, 40, "Conn_Thing_Horizontal", court=(-1, -2, 4, 2))        # courtyard 1 mm past the outline
    B = load(tmp_path, c)
    cons = {"intent": {"edge": [{"refs": ["J1"], "side": "right", "facing": "out", "overhang_mm": 1.5}]}}
    assert rows(B, cons, "edge")[0]["met"] is True and "J1" in LGL.Legal(B, cons).edge
    assert LGL.Legal(B, cons).issues()["outside"] == []
    cons0 = {"intent": {"edge": [{"refs": ["J1"], "side": "right", "facing": "out"}]}}
    assert rows(B, cons0, "edge")[0]["met"] is False


# ------------------------------------------------------------------ near
def test_near_chain_hard_is_seated_from_the_held_end(tmp_path):
    body = (conn("J1", 10, 40, "Jack_Vertical", locked=True) + cap("C5", 60, 60, "J1_S1", "RFA") +
            g.ic("U2", 80, 20, ["RFA", "X1", "X2", "GND", "X3", "X4"]))
    cons = {"intent": {"near": [{"name": "rf", "chain": ["J1", "C5", "U2"], "max_mm": 10, "hard": True}]}}
    B = load(tmp_path, body)
    r = rows(B, cons, "near")[0]
    assert r["met"] is False and r["value"] > 60
    S = IT.Seater(B, cons).run()
    r = rows(B, cons, "near")[0]
    assert r["met"] is True and r["value"] <= 10 and {"C5", "U2"} <= S.held and "J1" not in S.moves
    assert LGL.count(LGL.Legal(B, cons).issues()) == 0


def test_near_soft_is_a_two_pin_net_with_a_budget_and_no_base_makes_a_body(tmp_path):
    body = g.ic("U1", 20, 20, ["A", "B", "GND", "C"]) + g.ic("U2", 80, 60, ["A", "D", "GND", "E"]) + cap("R1", 50, 70, "E", "F")
    cons = {"intent": {"near": [{"name": "n", "refs": ["R1"], "to": "U2", "max_mm": 6, "weight": 7},
                                {"name": "pair", "chain": ["U1", "U2"], "max_mm": 12, "hard": True}]}}
    B = load(tmp_path, body)
    pads, info = IT.plan_links(B, cons)
    assert set(pads) == {"intent/n/0", "intent/pair/0"} and info["intent/n/0"] == {"weight": 7.0, "budget": 6.0, "class": "intent"}
    assert {q["ref"] for q in pads["intent/n/0"]} == {"R1", "U2"}
    S = IT.Seater(B, cons).run()
    assert S.held == set() and S.bodies and set(S.bodies[0][1]) >= {"U1", "U2"}       # nobody is held: one rigid body
    assert S.cons_out()["placement"]["group"]["intent:pair"]["refs"] == S.bodies[0][1]
    assert rows(B, cons, "near")[-1]["met"] is True


# ------------------------------------------------------------------ away
def test_away_hard_gate_zone_and_engine(tmp_path):
    body = g.ic("U1", 30, 30, ["SW", "B", "GND", "C"]) + g.ic("U9", 38, 30, ["RX", "D", "GND", "E"], locked=True)
    cons = {"intent": {"away": [{"name": "noise", "a": ["U1"], "b": ["U9"], "min_mm": 15}]}}
    B = load(tmp_path, body)
    r = rows(B, cons, "away")[0]
    assert r["met"] is False and r["hard"] and r["value"] < 15
    assert any("noise" in x for x in LGL.Legal(B, cons, fixed={"U9"}).issues()["forbidden"])
    S = IT.Seater(B, cons).run()
    r = rows(B, cons, "away")[0]
    assert r["met"] is True and r["value"] >= 15 and "U1" in S.moves and "U9" not in S.moves
    soft = {"intent": {"away": [{"a": ["U1"], "b": ["U9"], "min_mm": 80, "hard": False}]}}
    assert IT.verdict(IT.check(B, soft), soft)[0] == 0 and rows(B, soft, "away")[0]["met"] is False


# ------------------------------------------------------------------ regions
def test_region_height_side_keepin_and_legaliser(tmp_path):
    body = (cap("C1", 20, 20, "A", side="B") + cap("C2", 24, 20, "B", side="B", name="C_1210_3225Metric") +
            cap("C3", 28, 20, "D") + g.ic("U5", 22, 26, ["A", "B", "GND", "D"], side="B", name="QFN") + cap("C4", 70, 60, "E"))
    reg = {"name": "under", "rect": [10, 10, 40, 40], "side": "B", "max_height_mm": 1.0}
    cons = {"intent": {"region": [reg]}}
    B = load(tmp_path, body)
    r = rows(B, cons, "region")[0]
    assert r["met"] is False and r["value"] == 2 and "C2 (2.7 mm)" in r["detail"] and "U5 (height unknown)" in r["detail"]
    assert LGL.Legal(B, cons).issues()["forbidden"] == ["C2 in under", "U5 in under"]          # C1 is low, C3 is on F
    c2 = {"intent": {"region": [reg]}, "placement": {"heights": {"U*": 0.9}}}
    assert rows(B, c2, "region")[0]["value"] == 1
    nothing = {"intent": {"region": [dict(reg, max_height_mm=None, allow=["C1"])]}}
    nothing["intent"]["region"][0].pop("max_height_mm")
    assert LGL.Legal(B, nothing).issues()["forbidden"] == ["C2 in under", "U5 in under"]
    # the legaliser takes a part out of the region; the engine does too
    res, rep = LG.legalise(B, {"C2": (24, 20, 0)}, cons=cons)
    assert rep["in_keepout"] == [] and not GM.overlap(GM.moved(B["parts"]["C2"], *res["C2"])[1], (10, 10, 40, 40))
    S = IT.Seater(B, cons).run()
    assert rows(B, cons, "region")[0]["met"] is True and {"C2", "U5"} <= set(S.moves) and "C1" not in S.moves
    # keep-in: the listed refs must be inside
    kin = {"intent": {"region": [{"name": "zone", "rect": [60, 50, 90, 75], "keep": "in", "refs": ["C3", "C4"]}]}}
    r = rows(B, kin, "region")[0]
    assert r["met"] is False and r["refs"] == ["C3"]


# ------------------------------------------------------------------ rigid group + swept area
def test_rigid_group_with_swept_area(tmp_path):
    hole = lambda ref, x, y: g.fp(ref, x, y, [g.pad("", 0, 0, typ="np_thru_hole")], name="Standoff", court=(-1.5, -1.5, 1.5, 1.5))
    body = (conn("J9", 30, 40, "CardSocket_M2", court=(-2, -6, 2, 6), n=8) + hole("H1", 60, 40) +
            cap("C1", 45, 40, "A", name="C_1210_3225Metric") + cap("C2", 45, 43, "B") + hole("H7", 50, 37))
    grp = {"name": "card", "refs": ["J9", "H1"], "swept": "all", "swept_max_height_mm": 1.0}
    cons = {"intent": {"group": [grp]}}
    B = load(tmp_path, body)
    r = [x for x in rows(B, cons, "group") if x["name"].startswith("swept")][0]
    assert r["met"] is False and r["refs"][:1] == ["C1"]                    # C2 is low; a bare hole has no height
    c0 = {"intent": {"group": [dict(grp, swept_max_height_mm=None)]}}
    c0["intent"]["group"][0].pop("swept_max_height_mm")
    r0 = [x for x in rows(B, c0, "group") if x["name"].startswith("swept")][0]
    assert r0["value"] == 3 and {"C1", "C2", "H7"} <= set(r0["refs"])       # nothing at all: another part's hole counts
    before = (B["parts"]["H1"]["pos"][0] - B["parts"]["J9"]["pos"][0], B["parts"]["H1"]["pos"][1] - B["parts"]["J9"]["pos"][1])
    S = IT.Seater(B, cons).run()
    after = (B["parts"]["H1"]["pos"][0] - B["parts"]["J9"]["pos"][0], B["parts"]["H1"]["pos"][1] - B["parts"]["J9"]["pos"][1])
    assert {"J9", "H1"} <= S.held and abs(before[0] ** 2 + before[1] ** 2 - after[0] ** 2 - after[1] ** 2) < 1e-6
    assert all(x["met"] is not False for x in rows(B, cons, "group")) and LGL.count(LGL.Legal(B, cons).issues()) == 0
    assert IT.effective(cons)["placement"]["group"]["intent:card"]["refs"] == ["J9", "H1"]       # one module in the floorplan
    # `rel` poses are verified
    rel = {"intent": {"group": [dict(grp, rel={"H1": [after[0], after[1], 0]})]}}
    assert [x for x in rows(B, rel, "group") if "rigid" in x["name"]][0]["met"] in (True, False)
    GM.place(B["parts"]["H1"], B["parts"]["H1"]["pos"][0] + 3, B["parts"]["H1"]["pos"][1], 0)
    a = B["parts"]["J9"]
    lx, ly = GM.rot(after[0], after[1], -a["angle"])
    rel = {"intent": {"group": [dict(grp, rel={"H1": [lx, ly, 0]})]}}
    assert [x for x in rows(B, rel, "group") if "rigid" in x["name"]][0]["met"] is False


def test_top_entry_connector_may_not_sit_under_a_swept_area(tmp_path):
    body = (conn("J9", 30, 40, "CardSocket_M2", court=(-2, -6, 2, 6), n=8) + conn("J2", 45, 40, "Header_Vertical", court=(-1, -1, 1, 1))
            + cap("C2", 45, 44, "B") + conn("J3", 60, 40, "Header_Vertical", court=(-1, -1, 1, 1)))
    cons = {"intent": {"group": [{"name": "card", "refs": ["J9", "J3"], "swept": "all", "swept_max_height_mm": 5.0}]},
            "placement": {"heights": {"J*": 2.0}}}
    B = load(tmp_path, body)
    r = [x for x in rows(B, cons, "group") if x["name"].startswith("swept")][0]
    assert r["met"] is False and r["refs"][0] == "J2"                       # low enough, but its plug comes from above


# ------------------------------------------------------------------ support parts
def _ic_with_caps(far=False):
    b = g.ic("U410", 40, 40, ["VDD", "A1", "D410", "GND", "A2", "A3"], pitch=0.5)
    b += cap("C411", 60 if far else 35.9, 62 if far else 40.9, "D410")
    b += cap("C412", 35.9, 39.1, "VDD")
    return b


def test_support_parts_follow_an_ic_move_and_are_reseated(tmp_path):
    B = load(tmp_path, _ic_with_caps())
    assert [r for r in IT.support_rows(B, {"intent": {}}, None) if "decoupling" in r["name"]][0]["met"] is True
    d0 = {r: (B["parts"][r]["pos"][0] - 40, B["parts"][r]["pos"][1] - 40) for r in ("C411", "C412")}
    mv = IT.move_with_support(B, {}, None, "U410", 70, 55, 90)
    assert set(mv) >= {"U410", "C411", "C412"}
    for r, (dx, dy) in d0.items():                                          # rigid: same place relative to the turned IC
        rx, ry = GM.rot(dx, dy, 90)
        assert abs(B["parts"][r]["pos"][0] - (70 + rx)) < 1e-6 and abs(B["parts"][r]["pos"][1] - (55 + ry)) < 1e-6
    sup = [r for r in IT.support_rows(B, {"intent": {}}, None) if "decoupling" in r["name"]][0]
    assert sup["met"] is True
    # an edge seat carries the support parts too
    B = load(tmp_path, _ic_with_caps())
    cons = {"intent": {"edge": [{"refs": ["U410"], "side": "left"}]}}
    S = IT.Seater(B, cons).run()
    assert {"U410", "C411", "C412"} <= S.held and rows(B, cons, "edge")[0]["met"] is True
    assert [r for r in IT.support_rows(B, cons, None) if "decoupling" in r["name"]][0]["met"] is True
    off = {"intent": {"edge": [{"refs": ["U410"], "side": "left"}], "support": {"follow": False}}}
    B = load(tmp_path, _ic_with_caps())
    assert IT.Seater(B, off).run().held == {"U410"}


def test_decap_limit_is_a_hard_gate_with_offenders_and_the_engine_reseats(tmp_path):
    B = load(tmp_path, _ic_with_caps(far=True))
    cons = {"intent": {}}
    r = [x for x in IT.check(B, cons) if "decoupling" in x["name"]][0]
    assert r["met"] is False and r["hard"] and r["refs"] == ["C411"] and "C411" in r["detail"] and "U410" in r["detail"]
    assert IT.verdict(IT.check(B, cons), cons)[0] == 1
    assert IT.verdict(IT.check(B, {}), {})[0] == 0                          # no [intent] table: reported, not gated
    waived = {"intent": {"support": {"waive": ["C411"]}}}
    assert [x for x in IT.check(B, waived) if "decoupling" in x["name"]][0]["met"] is True
    S = IT.Seater(B, cons).run()
    assert "C411" in S.moves and [x for x in IT.check(B, cons) if "decoupling" in x["name"]][0]["met"] is True


# ------------------------------------------------------------------ connectors
def test_connector_entry_from_name_geometry_and_declaration(tmp_path):
    body = (conn("J1", 20, 20, "USB_C_Receptacle_Horizontal", court=(-2, -2, 5, 2)) + conn("J2", 40, 20, "PinHeader_1x03_Vertical") +
            conn("J3", 60, 20, "Jack_XYZ", court=(-2, -2, 6, 2), typ="thru_hole") + conn("J4", 80, 20, "Thing_ABC") +
            conn("J5", 20, 50, "Socket_M.2_KeyB") + conn("J6", 40, 50, "Thing_ABC", angle=90.0, court=(-2, -2, 2, 2)))
    B = load(tmp_path, body)
    cons = {"intent": {"connectors": {"J6": {"entry": "side", "opening": "-y"}}}}
    t = {r["ref"]: r for r in CN.table(B, cons)}
    assert (t["J1"]["entry"], t["J1"]["how"], t["J1"]["key"], t["J1"]["opening"]) == ("side", "name", "+x", (1.0, 0.0))
    assert (t["J2"]["entry"], t["J2"]["how"]) == ("top", "name")
    assert (t["J3"]["entry"], t["J3"]["how"], t["J3"]["key"]) == ("side", "geometry", "+x")
    assert (t["J4"]["entry"], t["J4"]["how"]) == ("unknown", "none")
    assert t["J5"]["entry"] == "card"
    assert (t["J6"]["entry"], t["J6"]["how"]) == ("side", "declared") and t["J6"]["opening"] == pytest.approx(GM.rot(0.0, -1.0, 90.0), abs=1e-6)
    assert "J1 " in CN.format_table(list(t.values()))
    # a side-entry connector is an implicit hard edge item, facing out; `edge = false` switches it off
    names = [e["name"] for e in IT.items(B, cons)["edge"]]
    assert names == ["connector J1", "connector J3", "connector J6"]
    r = [x for x in rows(B, cons, "edge") if "J1" in x["name"]][0]
    assert r["met"] is False and r["hard"]
    off = {"intent": {"connectors": {"J1": {"edge": False}, "J6": {"entry": "top"}}}}
    assert [e["name"] for e in IT.items(B, off)["edge"]] == ["connector J3"]
    S = IT.Seater(B, cons).run()
    e = [x for x in rows(B, S.cons_out(), "edge")]
    assert all(x["met"] for x in e) and {"J1", "J3", "J6"} <= S.held
    st = IT.edge_state(B, S.cons_out(), "J1", IT.items(B, S.cons_out())["edge"][0])
    assert st[2] and st[3]                                                  # on an edge, opening pointing out


def test_unknown_connector_is_asked_not_assumed(tmp_path, capsys):
    path = g.board(tmp_path, conn("J4", 50, 40, "Thing_ABC") + conn("J8", 60, 40, "Thing_ABC"))
    B = RD.load(path)
    cons = {"intent": {}}
    S = QS.Session(path, cons, {}, False, "intent")
    QS.CHECKS["connector-entry"](S, B, cons)
    assert [q["id"] for q in S.open] == ["connector-entry:Thing_ABC"] and S.open[0]["default"] == "top"
    assert {o["choice"] for o in S.open[0]["options"]} == {"side", "top"}
    S0 = QS.Session(path, {}, {}, False, "intent")                          # no [intent] table: nothing is asked
    QS.CHECKS["connector-entry"](S0, B, {})
    assert S0.open == []
    S2 = QS.Session(path, cons, {"connector-entry:Thing_ABC": "side"}, False, "intent")
    QS.CHECKS["connector-entry"](S2, B, cons)
    c2, _ = S2.apply(cons, None)
    assert c2["intent"]["connectors"] == {"J4": {"entry": "side"}, "J8": {"entry": "side"}} and not S2.open
    assert {r["ref"]: r["entry"] for r in CN.table(B, c2)} == {"J4": "side", "J8": "side"}
    # the command stops with exit 3 and writes the questions file; --assume-defaults goes on and says so
    cf = tmp_path / "c.toml"
    cf.write_text("[intent]\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["intent", path, "--constraints", str(cf)])
    assert e.value.code == 3 and os.path.exists(QS.questions_path(path))
    assert "connector-entry:Thing_ABC" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["intent", path, "--constraints", str(cf), "--assume-defaults"])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "ASSUMED: connector-entry:Thing_ABC" in out and "INTENT:" in out


# ------------------------------------------------------------------ gate, command, draft, registry
def test_intent_command_gate_exit_codes_and_report(tmp_path, capsys):
    path = g.board(tmp_path, module("U1", 50, 40) + cap("C1", 20, 20, "A"))
    cf = tmp_path / "c.toml"
    cf.write_text('[[intent.edge]]\nrefs = ["U1"]\nside = "left"\nfacing = "out"\nfront = "+x"\n')
    with pytest.raises(SystemExit) as e:
        cli.main(["intent", path, "--constraints", str(cf)])
    out = capsys.readouterr().out
    assert e.value.code == 1 and out.startswith("INTENT: FAIL") and "NOT MET hard edge" in out
    assert os.path.exists(os.path.splitext(path)[0] + ".intent.json")
    seated = str(tmp_path / "seated.kicad_pcb")
    with pytest.raises(SystemExit) as e:
        cli.main(["intent", path, "--constraints", str(cf), "--apply", "--out", seated])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "INTENT: OK" in out and "left edge" in out
    B = RD.load(seated)
    assert B["parts"]["U1"]["court"][0] - B["bbox"][0] <= 1.0
    with pytest.raises(SystemExit):
        cli.main(["intent", path, "--constraints", str(cf), "--apply", "--out", path])     # never the input board


def test_prepare_writes_a_seated_copy_and_holds(tmp_path):
    path = g.board(tmp_path, module("U1", 50, 40) + cap("C1", 20, 20, "A"))
    cons = {"intent": {"edge": [{"refs": ["U1"], "side": "bottom", "facing": "out", "front": "+x"}]}}
    out, c2, S = IT.prepare(path, cons, None, str(tmp_path / "w"), log=None)
    assert out != path and "U1" in c2["placement"]["fixed"] and c2["intent"]["_front"] == {"U1": "+x"}
    assert rows(RD.load(out), c2, "edge")[0]["met"] is True
    assert IT.prepare(path, {}, None, str(tmp_path / "w2"), log=None)[0] == path          # no intent: untouched


def test_draft_marks_derived_and_guess_and_never_applies_a_guess(tmp_path):
    hole = g.fp("H1", 60, 60, [g.pad("", 0, 0, typ="np_thru_hole")], name="Standoff", court=(-1.5, -1.5, 1.5, 1.5))
    body = (conn("J1", 20, 20, "USB_C_Receptacle_Horizontal", court=(-2, -2, 5, 2)) + conn("J4", 80, 20, "Thing_ABC") +
            conn("J5", 30, 60, "Socket_M.2_KeyB", n=8, court=(-2, -6, 2, 6)) + hole + _ic_with_caps())
    B = load(tmp_path, body)
    text, st = ITD.draft(B)
    assert st["derived"] >= 2 and st["guess"] >= 1 and st["ask"] == 1
    live = tomllib.loads(text)                                              # guesses are comments: the draft parses without them
    assert [e["refs"] for e in live["intent"]["edge"]] == [["J1"]] and live["intent"]["support"]["follow"] is True
    assert "group" not in live["intent"] and '#? [[intent.group]]' in text and "# guess:" in text and "# derived:" in text
    assert "# ask: J4" in text and "connector-entry:Thing_ABC" in text
    for line in text.splitlines():
        assert not line.strip() or line.startswith(("#", "[", "refs", "side", "facing", "front", "overhang", "follow", "decap",
                                                    "chain", "hard", "max_mm"))
    out = str(tmp_path / "d.toml")
    cli.main(["intent-draft", g.board(tmp_path, body, name="d.kicad_pcb"), "--out", out])
    assert tomllib.load(open(out, "rb"))["intent"]["edge"]


def test_region_rect_is_a_registered_coordinate():
    assert C.coord_kind(("intent", "region", "rect")) == "xy"
    cons = {"intent": {"region": [{"name": "a", "rect": [1, 2, 3, 4]}]}}
    assert C.coord_items(cons) == [("intent.region.rect", (1.0, 2.0)), ("intent.region.rect", (3.0, 4.0))]
    assert C.unregistered_coords(cons) == []


# ------------------------------------------------------------------ a stage FAIL is never a silent pass
def test_floorplan_stage_fails_on_fail_lines_and_nonzero_exit(tmp_path, monkeypatch):
    from fluxplace import pipeline as PL

    class Ctx:
        a = type("A", (), {"size": False})()
        cons = {}

        def dir(self, n, name):
            d = tmp_path / f"{n}-{name}"
            d.mkdir(exist_ok=True)
            return str(d)

        def cs(self):
            return []

        def qs(self):
            return []

    def fake(rc, text, write=True):
        def run(argv, log):
            if write:
                open(argv[argv.index("--out") + 1], "w").write("x")
            return rc, text
        return run

    monkeypatch.setattr(PL, "_run_cli", fake(0, "floorplan: PASS at grow [0, 0, 0, 0]; wrote x\n"))
    assert PL.st_floorplan(Ctx(), 2, "b.kicad_pcb")["verdict"] == "pass"
    monkeypatch.setattr(PL, "_run_cli", fake(0, "floorplan: FAIL at grow [0, 0, 0, 0]; wrote x\n"))
    assert PL.st_floorplan(Ctx(), 2, "b.kicad_pcb")["verdict"] == "fail"          # the old silent pass
    monkeypatch.setattr(PL, "_run_cli", fake(1, "floorplan: FAIL at grow [0, 0, 0, 0]; wrote x\n"))
    assert PL.st_floorplan(Ctx(), 2, "b.kicad_pcb")["verdict"] == "fail"
    monkeypatch.setattr(PL, "_run_cli", fake(0, "floorplan: PASS at grow [0, 0, 0, 0]\nINTENT: FAIL - 3 item(s)\n"))
    assert PL.st_floorplan(Ctx(), 2, "b.kicad_pcb")["verdict"] == "fail"
    monkeypatch.setattr(PL, "_run_cli", fake(0, "floorplan: FAIL at grow [0, 0, 0, 0]\nfloorplan: WAIVED - the FAIL above is accepted\n"))
    r = PL.st_floorplan(Ctx(), 2, "b.kicad_pcb")
    assert r["verdict"] == "pass" and "WAIVED" in r["line"]                       # an explicit, recorded waiver


def _fake_floorplan(monkeypatch, tmp_path, fanout_ok):
    from fluxplace import floorplan2 as FP2
    board = g.board(tmp_path, module("U1", 50, 40) + cap("C1", 20, 20, "A"))

    def once(b, cons, spec, grow, args, text_in, wd, tag, log=print):
        res = {"tag": tag, "grow": list(grow), "escaped": 5, "required": 5 if fanout_ok else 6, "moved_modules": 1, "movable": 2,
               "source": {"cross": 0, "wl": 1.0}, "annealed": {"cross": 0, "wl": 1.0}, "secs": 0.0, "construct_failed": [],
               "legality_new": {}, "fanout_ok": fanout_ok, "rounds": [{"why": ["U1.3 blocked"]}], "board": b, "blocked": [],
               "metrics": {"RF_EXT": None, "pair_spans": []}}
        return res, None, RD.load(b)
    monkeypatch.setattr(FP2, "floorplan_once", once)
    monkeypatch.setattr(FP2, "edge_order", lambda B, cons: ["L", "R", "T", "B"])
    monkeypatch.setattr(FP2, "span_summary", lambda s: "-")
    monkeypatch.setattr(FP2, "plan_summary", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("skipped")))
    return board


def test_floorplan_command_exits_nonzero_on_fail_and_records_a_waiver(tmp_path, monkeypatch, capsys):
    board = _fake_floorplan(monkeypatch, tmp_path, fanout_ok=False)
    out = str(tmp_path / "o.kicad_pcb")
    with pytest.raises(SystemExit) as e:
        cli.main(["floorplan", board, "--out", out, "--assume-defaults"])
    assert e.value.code == 1 and "floorplan: FAIL at grow" in capsys.readouterr().out
    cf = tmp_path / "w.toml"
    cf.write_text('[floorplan]\nwaive_fail = "3 pins accepted by the owner"\n')
    cli.main(["floorplan", board, "--out", out, "--constraints", str(cf), "--assume-defaults"])
    assert "floorplan: WAIVED" in capsys.readouterr().out
    # a floorplan that passes the fan-out but leaves hard intent unmet fails too
    board = _fake_floorplan(monkeypatch, tmp_path, fanout_ok=True)
    ci = tmp_path / "i.toml"
    ci.write_text('[[intent.away]]\na = ["U1"]\nb = ["C1"]\nmin_mm = 200\n')
    monkeypatch.setattr(IT, "prepare", lambda b, cons, spec, wd, **k: (b, cons, None))
    with pytest.raises(SystemExit) as e:
        cli.main(["floorplan", board, "--out", out, "--constraints", str(ci), "--assume-defaults"])
    o = capsys.readouterr().out
    assert e.value.code == 1 and "floorplan: PASS" in o and "INTENT: FAIL" in o
