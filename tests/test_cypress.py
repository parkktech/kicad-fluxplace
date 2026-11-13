"""Placement engine "cypress": Bookshelf conversion, a fake Cypress tool, legality after the legaliser,
fallback to the built-in placer when the tool is absent, engine recorded by the pipeline."""
import argparse, json, math, os, subprocess, sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as g
from fluxplace import (cypress_io as IO, placer_cypress as PC, routediag as RD, netclass as NC, geom as GM,
                       legalise as LG, pipeline as PL)

ROOT = os.path.join(os.path.dirname(__file__), "..")

# stands in for dreamplace/Placer.py: piles every movable cell on the middle of the grid, turns the first one 90 degrees
FAKE = '''
import json, os, sys
cfg = json.load(open(sys.argv[1]))
aux = cfg["aux_input"]
d, files = os.path.dirname(aux), open(aux).read().split(":")[1].split()
name = os.path.splitext(os.path.basename(aux))[0]
rows = sum(1 for l in open(os.path.join(d, name + ".scl")) if l.startswith("CoreRow"))
out, n = ["UCLA pl 1.0", ""], 0
for l in open(os.path.join(d, name + ".pl")):
    t = l.split()
    if len(t) >= 5 and t[3] == ":":
        if "/FIXED" in l:
            out.append(l.strip())
        else:
            side = "F" if t[4].startswith("F") else ""
            out.append("%s %d %d : %s%s" % (t[0], 200 + n % 3, rows // 2 + n % 2, side, "W" if n == 0 else "N"))
            n += 1
res = os.path.join(cfg["result_dir"], name)
os.makedirs(res, exist_ok=True)
open(os.path.join(res, name + ".gp.pl"), "w").write("\\n".join(out) + "\\n")
print("non-linear placement takes 0.01 seconds")
print("Final PPA: {'rsmt': 1.0, 'hpwl': 123.0, 'overflow': 0.05}")
'''


def fake_home(tmp_path):
    h = tmp_path / "Cypress"
    (h / "dreamplace" / "ops" / "hpwl").mkdir(parents=True)
    (h / "dreamplace" / "ops" / "hpwl" / "hpwl_cpp.so").write_text("")
    (h / "dreamplace" / "Placer.py").write_text(FAKE)
    return str(h)


def val(ref, x, y, a, b, **kw):
    return g.two(ref, x, y, a, b, court=(-1.0, -0.5, 1.0, 0.5), **kw)


def mk(tmp_path, **kw):
    b = g.fp("J1", 5, 40, [g.pad(str(i + 1), 0, (i - 2.5) * 2, n, 0.8, 0.6, typ="thru_hole")
                           for i, n in enumerate(["A", "B", "C", "D", "E", "F"])], court=(-1, -7, 1, 7))
    b += g.ic("U1", 80, 70, ["A", "B", "C", "VDD", "GND", "Z1"], pitch=1.27, court=(-3, -3, 3, 3))
    b += g.ic("U2", 20, 10, ["D", "E", "F", "VDD", "GND", "Z2"], pitch=1.27, court=(-3, -3, 3, 3))
    b += val("C1", 90, 10, "VDD", "GND") + val("C2", 70, 15, "Z1", "GND") + val("R1", 15, 70, "Z2", "Z1")
    b += val("R2", 60, 60, "A", "Z2", side="B")
    b += g.fp("MH1", 50, 5, [g.pad("1", 0, 0, "", typ="np_thru_hole")], court=(-1, -1, 1, 1), locked=True)
    return g.board(tmp_path, b, **kw)


CONS = {"placement": {"fixed": ["J1"]}}


def model(B, cons=CONS, **kw):
    from fluxplace import preroute as PR
    return IO.build_model(B, PR.fixed_parts(B, cons), NC.classify(B, cons, None), **kw)


def test_model_cells_sides_fixed_and_nets(tmp_path):
    B = RD.load(mk(tmp_path))
    m = model(B, clusters=[{"ic": "U1", "supports": [{"ref": "C2", "via": "1", "pad": "6", "weight": 1.0}]}])
    by = {c["ref"]: c for c in m["cells"] if c["ref"]}
    assert by["J1"]["fixed"] and by["MH1"]["fixed"] and not by["U1"]["fixed"]
    assert by["R2"]["side"] == "B" and by["U1"]["side"] == "F"
    # the through-hole connector occupies both sides: a second pinless fixed cell
    assert sum(1 for c in m["cells"] if c["fixed"] and c["w"] == by["J1"]["w"] and c["h"] == by["J1"]["h"]) == 2
    nets = {n["net"]: n for n in m["nets"]}
    assert "GND" not in nets and "VDD" not in nets                   # rails: weight 0, dropped
    assert nets["support:C2"]["weight"] == 8.0 and len(nets["support:C2"]["pins"]) == 2
    assert nets["A"]["weight"] > 0 and len(nets["A"]["pins"]) == 3
    # courtyard 6 x 6 mm + 0.2 mm spacing on a 0.1 mm grid
    assert (by["U1"]["w"], by["U1"]["h"]) == (62, 62)


def test_bookshelf_files_and_round_trip(tmp_path):
    B = RD.load(mk(tmp_path))
    m = model(B)
    aux = IO.write_bookshelf(m, str(tmp_path / "bs"))
    d = os.path.dirname(aux)
    nodes = open(os.path.join(d, "board.nodes")).read()
    assert f"NumNodes : {len(m['cells'])}" in nodes and nodes.count(" terminal") == sum(c["fixed"] for c in m["cells"])
    pl = IO.read_pl(os.path.join(d, "board.pl"))
    assert any(o == "FN" for _x, _y, o in pl.values())               # the bottom-side part
    assert open(os.path.join(d, "board.wts")).read().count("\n") == len(m["nets"]) + 2
    tg = IO.targets(m, B, pl)
    assert set(tg) == {"U1", "U2", "C1", "C2", "R1", "R2"}
    for r, (x, y, a) in tg.items():
        assert math.hypot(x - B["parts"][r]["pos"][0], y - B["parts"][r]["pos"][1]) <= 0.08 and a == B["parts"][r]["angle"]


def test_rotation_turns_the_cell_about_its_centre(tmp_path):
    body = g.fp("U1", 30, 30, [g.pad("1", -2, 0, "A"), g.pad("2", 2, 1, "B")], court=(-3, -1, 5, 2)) + val("R1", 60, 30, "A", "B")
    B = RD.load(g.board(tmp_path, body))
    m = model(B, cons={}, gap=0.0)
    c = next(c for c in m["cells"] if c["ref"] == "U1")
    for o, th in (("N", 0), ("W", 90), ("S", 180), ("E", 270)):
        x, y, a = IO.targets(m, B, {c["name"]: (c["x"], c["y"], o)})["U1"]
        court = GM.moved(B["parts"]["U1"], x, y, a)[1]
        assert a == th
        # the courtyard centre stays where the cell centre is
        assert abs((court[0] + court[2]) / 2 - 31) < 1e-6 and abs((court[1] + court[3]) / 2 - 30.5) < 1e-6


def test_outline_blocks_cover_what_is_outside_a_notched_outline(tmp_path):
    pts = [(0, 0), (100, 0), (100, 80), (40, 80), (40, 50), (0, 50)]
    edge = "".join(f'(gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (layer "Edge.Cuts"))\n'
                   for a, b in zip(pts, pts[1:] + pts[:1]))
    B = RD.load(mk(tmp_path, edge=edge))
    blocks = IO.outline_blocks(B)
    assert blocks and abs(sum((r[2] - r[0]) * (r[3] - r[1]) for r in blocks) - 40 * 30) < 1e-6
    assert all(r[0] >= 0 and r[2] <= 40 and r[1] >= 50 for r in blocks)
    assert sum(1 for c in model(B)["cells"] if c["name"].startswith("k")) == 2 * len(blocks)


def test_locate_reports_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("CYPRESS_HOME", raising=False)
    monkeypatch.delenv("CYPRESS_PYTHON", raising=False)
    with pytest.raises(PC.Unavailable, match="no built dreamplace"):
        PC.locate(str(tmp_path))
    with pytest.raises(PC.Unavailable, match="no Python"):
        PC.locate(fake_home(tmp_path))
    t = PC.locate(fake_home(tmp_path / "x"), sys.executable)
    assert t["placer"].endswith("Placer.py") and t["python"] == sys.executable
    monkeypatch.setenv("CYPRESS_HOME", t["home"])
    monkeypatch.setenv("CYPRESS_PYTHON", sys.executable)
    assert PC.locate()["home"] == t["home"]
    with pytest.raises(ValueError):
        PC.engine_of({"placement": {"engine": "nope"}})
    assert PC.engine_of({"placement": {"engine": "cypress"}}) == "cypress" and PC.engine_of({}) == "builtin"
    assert PC.engine_of({"placement": {"engine": "cypress"}}, "builtin") == "builtin"


def test_place_with_fake_tool_is_legal_and_keeps_fixed_parts(tmp_path):
    p = mk(tmp_path)
    B = RD.load(p)
    mv, rep = PC.place(B, CONS, None, str(tmp_path / "w"), fake_home(tmp_path), sys.executable, log=lambda m: None)
    assert rep["engine"] == "cypress" and rep["version"] and rep["tool_hpwl"] == 123.0
    assert len(rep["trials"]) == 4 and rep["score"] == min(t["score"] for t in rep["trials"])
    assert rep["overlaps"] == 0 and rep["outside"] == 0 and rep["fixed_moved"] == []
    assert rep["legaliser_moved"] >= 1                                # the pile had to be spread
    assert "J1" not in mv and "MH1" not in mv and {"U1", "U2"} <= set(mv)
    P = B["parts"]
    refs = sorted(P)
    for i, a in enumerate(refs):
        assert GM.inside(P[a]["court"], B["bbox"])
        for b in refs[i + 1:]:
            if P[a]["side"] == P[b]["side"] or P[a]["tht"] or P[b]["tht"]:
                assert not GM.overlap(P[a]["court"], P[b]["court"], -0.001), (a, b)


def _cli(*argv, env=None):
    return subprocess.run([sys.executable, os.path.join(ROOT, "cli.py"), *argv], capture_output=True, text=True,
                          timeout=120, env=dict(os.environ, **(env or {})))


def test_cli_engine_cypress_and_fallback_when_absent(tmp_path):
    p, out = mk(tmp_path), str(tmp_path / "o.kicad_pcb")
    cons = tmp_path / "c.toml"
    cons.write_text('[placement]\nfixed = ["J1"]\n')
    r = _cli("place", "--board", p, "--strategy", "routable", "--engine", "cypress", "--cypress-home",
             str(tmp_path / "none"), "--constraints", str(cons), "--out", out)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "engine cypress unavailable" in r.stdout and "falling back to the built-in placer" in r.stdout
    assert "routable:" in r.stdout and PC.read_record(out)["engine"] == "builtin"
    r = _cli("place", "--board", p, "--strategy", "routable", "--engine", "cypress", "--cypress-home", fake_home(tmp_path),
             "--cypress-python", sys.executable, "--constraints", str(cons), "--out", out)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "engine cypress" in r.stdout and "overlaps 0" in r.stdout
    rec = PC.read_record(out)
    assert rec["engine"] == "cypress" and rec["version"] and rec["overlaps"] == 0
    B = RD.load(out)
    assert B["parts"]["J1"]["pos"] == (5, 40) and B["parts"]["U1"]["pos"] != (80, 70)


def test_pipeline_records_the_engine(tmp_path, monkeypatch):
    b = g.board(tmp_path, g.ic("U1", 20, 20, ["A", "B", "C", "D"], pitch=1.27) +
                g.ic("U2", 60, 40, ["A", "B", "C", "E"], pitch=1.27) + g.two("R1", 40, 30, "D", "E"))
    monkeypatch.setenv("CYPRESS_HOME", fake_home(tmp_path))
    monkeypatch.setenv("CYPRESS_PYTHON", sys.executable)
    cons = tmp_path / "c.toml"
    cons.write_text('[placement]\nengine = "cypress"\n')
    d = dict(board=b, workdir=str(tmp_path / "W"), constraints=str(cons), spec=None, from_stage=None, to_stage="place",
             status=False, waive=[], force=True, api=False)
    assert PL.run(argparse.Namespace(**d)) == 0
    st = PL.load_state(d["workdir"])
    assert st["stages"]["place"]["verdict"] == "pass" and st["stages"]["place"]["engine"].startswith("cypress ")
    # the default engine is the built-in placer, and it is recorded too
    d2 = dict(d, workdir=str(tmp_path / "W2"), constraints=None)
    assert PL.run(argparse.Namespace(**d2)) == 0
    assert PL.load_state(d2["workdir"])["stages"]["place"]["engine"].startswith("builtin ")


def test_a_run_that_did_not_converge_is_not_used(tmp_path):
    # Cypress writes the START positions when it diverges: that must never be taken for a placement
    h = fake_home(tmp_path)
    pl = os.path.join(h, "dreamplace", "Placer.py")
    open(pl, "w").write(FAKE.replace("'hpwl': 123.0", "'hpwl': inf") + 'print("placement failed")\n')
    B = RD.load(mk(tmp_path))
    said = []
    with pytest.raises(PC.Unavailable, match="no Cypress trial gave a legal placement.*did not converge"):
        PC.place(B, CONS, None, str(tmp_path / "w"), h, sys.executable, log=said.append)
    assert B["parts"]["U1"]["pos"] == (80, 70)
    # it settled at overflow 0.05: each trial was repeated once with that level accepted
    assert sum("once more with stop_overflow 0.07" in m for m in said) == 4
    assert json.load(open(tmp_path / "w" / "trial0r" / "cypress.json"))["stop_overflow"] == 0.07


def test_constructive_flow_uses_the_engine_in_the_floorplan_stage(tmp_path, monkeypatch):
    b = mk(tmp_path)
    monkeypatch.setenv("CYPRESS_HOME", fake_home(tmp_path))
    monkeypatch.setenv("CYPRESS_PYTHON", sys.executable)
    cons = tmp_path / "c.toml"
    cons.write_text('[placement]\nengine = "cypress"\nfixed = ["J1"]\n')
    d = dict(board=b, workdir=str(tmp_path / "W"), constraints=str(cons), spec=None, from_stage="floorplan",
             to_stage="floorplan", flow="constructive", status=False, waive=[], force=True, api=False)
    PL.run(argparse.Namespace(**d))
    st = PL.load_state(d["workdir"])["stages"]["floorplan"]
    assert st["verdict"] == "pass" and st["engine"].startswith("cypress ") and st["line"].startswith("cypress:")
    assert RD.load(st["board"])["parts"]["J1"]["pos"] == (5, 40)


# ------------------------------------------------------------------ intent-aware flow (docs/CYPRESS.md "With an intent")
from fluxplace import intent as IT, modules as MD, clusters as CL, constraints as C


def _fixed(B, cons):
    M = LG._Model(B, cons, MD.fixed_set(B, cons), B["bbox"])
    return M, set(M.fixed)


def test_a_macro_is_one_cell_with_every_member_pin_and_turns_as_one_body(tmp_path):
    B = RD.load(mk(tmp_path))
    cl = [{"ic": "U1", "supports": [{"ref": "C2", "via": "1", "pad": "6", "weight": 1.0}, {"ref": "C1", "via": "1", "pad": "4", "weight": 1.0}]}]
    m = model(B, clusters=cl, macros={"U1": ["U1", "C2"]}, gap=0.0)
    cell = next(c for c in m["cells"] if c.get("macro") == "U1")
    assert set(cell["members"]) == {"U1", "C2"} and not cell["fixed"] and cell["side"] == "F"
    assert not [c for c in m["cells"] if c["ref"] == "C2" and c is not cell]            # no cell of its own
    u, c2 = B["parts"]["U1"]["court"], B["parts"]["C2"]["court"]
    assert cell["w"] == round((max(u[2], c2[2]) - min(u[0], c2[0])) / 0.1) and cell["h"] == round((max(u[3], c2[3]) - min(u[1], c2[1])) / 0.1)
    nets = {n["net"]: n for n in m["nets"]}
    assert "support:C2" not in nets and "support:C1" in nets                            # inside the macro there is nothing to pull
    assert "Z1" in nets and {t[0] for t in nets["Z1"]["pins"]} == {cell["name"], next(c["name"] for c in m["cells"] if c["ref"] == "R1")}
    d0 = (B["parts"]["C2"]["pos"][0] - B["parts"]["U1"]["pos"][0], B["parts"]["C2"]["pos"][1] - B["parts"]["U1"]["pos"][1])
    for o, th in (("N", 0), ("W", 90), ("S", 180)):
        tg = IO.targets(m, B, {cell["name"]: (cell["x"] + 50, cell["y"], o)})
        rx, ry = GM.rot(d0[0], d0[1], th)
        assert tg["U1"][2] == th and tg["C2"][2] == th
        assert abs(tg["C2"][0] - tg["U1"][0] - rx) < 1e-3 and abs(tg["C2"][1] - tg["U1"][1] - ry) < 1e-3


def test_links_and_net_weight_overrides_reach_the_weights_file(tmp_path):
    B = RD.load(mk(tmp_path))
    links = [{"name": "intent/x/0", "pads": [("R1", "1"), ("U2", "1")], "weight": 24.0},
             {"name": "intent/held/0", "pads": [("J1", "1"), ("MH1", "1")], "weight": 9.0}]
    m = model(B, links=links, net_weights={"A": 7.5})
    nets = {n["net"]: n for n in m["nets"]}
    assert nets["intent/x/0"]["weight"] == 24.0 and len(nets["intent/x/0"]["pins"]) == 2
    assert "intent/held/0" not in nets and nets["A"]["weight"] == 7.5
    aux = IO.write_bookshelf(m, str(tmp_path / "bs"))
    assert f"{nets['intent/x/0']['name']} 24\n" in open(os.path.join(os.path.dirname(aux), "board.wts")).read()


def test_held_group_swept_area_region_and_front_become_fixed_cells_and_blockages(tmp_path):
    hole = lambda ref, x, y: g.fp(ref, x, y, [g.pad("", 0, 0, typ="np_thru_hole")], name="Standoff", court=(-1.5, -1.5, 1.5, 1.5))
    body = (g.fp("J9", 30, 40, [g.pad(str(i + 1), 0, i - 3.5, f"S{i}", 0.6, 0.6) for i in range(8)], name="CardSocket_M2", court=(-2, -6, 2, 6))
            + hole("H1", 60, 40) + g.ic("U1", 80, 20, ["S0", "S1", "S2", "S3"], pitch=1.27, court=(-3, -3, 3, 3))
            + val("R1", 70, 60, "S4", "S5") + val("R2", 75, 60, "S6", "S7") + val("R3", 20, 70, "S0", "S7", side="B"))
    cons = {"intent": {"group": [{"name": "card", "refs": ["J9", "H1"], "swept": "all", "swept_side": "F"}],
                       "region": [{"name": "all-out", "rect": [0, 0, 20, 20], "side": "both"},
                                  {"name": "one-part", "rect": [80, 60, 100, 80], "refs": ["R1"]}]}}
    B = RD.load(g.board(tmp_path, body))
    S = IT.Seater(B, cons).run()
    run = S.cons_out()
    M, fixed = _fixed(B, run)
    assert {"J9", "H1"} <= fixed                                       # the rigid group is held: fixed cells
    blocks, post = PC.zone_blocks(B, M, fixed)
    swept = IT.swept_rect(B, IT.items(B, run)["group"][0])
    assert (swept, {"F"}) in blocks                                    # the card area, on its side only
    assert ((0.0, 0.0, 20.0, 20.0), {"F"}) in blocks and ((0.0, 0.0, 20.0, 20.0), {"B"}) in blocks
    assert post == ["one-part"]                                        # concerns one part: the legaliser and the gate keep it
    m = IO.build_model(B, fixed, NC.classify(B, run, None), keepouts=blocks)
    by = {c["ref"]: c for c in m["cells"] if c["ref"]}
    assert by["J9"]["fixed"] and by["H1"]["fixed"] and not by["U1"]["fixed"]
    k = [c for c in m["cells"] if c["name"].startswith("k")]
    assert len(k) == 3 and all(c["fixed"] and not c["ref"] for c in k)
    assert not any(c["name"] in {x["name"] for x in k} for n in m["nets"] for c in [{"name": t[0]} for t in n["pins"]])   # pinless
    # an `away` halo concerns the offending set only: left to the legaliser, unless that set is most of what moves on the side
    for a, blocked in ((["R1"], False), (["U1"], True)):
        away = {"intent": {"away": [{"name": "noise", "a": a, "b": ["J9"], "min_mm": 10}]}, "placement": {"fixed": ["J9"]}}
        M2, f2 = _fixed(B, away)
        b2, p2 = PC.zone_blocks(B, M2, f2)
        c = B["parts"]["J9"]["court"]
        assert (b2 == [((c[0] - 10, c[1] - 10, c[2] + 10, c[3] + 10), {"F"})]) is blocked and any("noise" in n for n in p2) is not blocked
        assert any("noise" in n for n, _r in M2.forbidden_named(a[0]))


def test_relations_become_weighted_nets_scaled_from_the_budget(tmp_path):
    body = (g.ic("U1", 20, 20, ["D_P", "D_N", "A", "B"], pitch=1.27) + g.ic("U2", 70, 60, ["E_P", "E_N", "A", "B"], pitch=1.27)
            + val("C1", 40, 20, "D_P", "E_P", name="C_0402") + val("C2", 40, 60, "D_N", "E_N", name="C_0402")
            + val("R1", 60, 20, "A", "X") + val("R2", 80, 20, "B", "Y"))
    B = RD.load(g.board(tmp_path, body))
    cons = {"intent": {"near": [{"name": "tight", "refs": ["R1"], "to": "U1", "max_mm": 4.0},
                                {"name": "loose", "refs": ["R2"], "to": "U1", "max_mm": 60.0}]}}
    cls = NC.classify(B, cons, None)
    assert cls["D_P"]["partner"] == "D_N"
    links, weights = PC.relation_links(B, cons, cls, PC.settings(cons))
    by = {l["name"]: l for l in links}
    assert by["intent/tight/0"]["weight"] > by["intent/loose/0"]["weight"] >= 3.0        # the tighter budget pulls harder
    assert by["intent/tight/0"]["pads"][0][0] == "R1" and by["intent/tight/0"]["pads"][1][0] == "U1"
    pair = [l for l in links if l["name"].startswith("pair/")]
    assert len(pair) == 1 and {r for r, _p in pair[0]["pads"]} == {"C1", "C2"}          # series parts of the two halves, side by side
    assert weights["D_P"] == cls["D_P"]["weight"] * 2.0 and weights["E_N"] == cls["E_N"]["weight"] * 2.0 and "A" not in weights
    m = IO.build_model(B, set(), cls, links=links, net_weights=weights)
    nets = {n["net"]: n for n in m["nets"]}
    assert nets["D_P"]["weight"] == weights["D_P"] and nets[pair[0]["name"]]["weight"] == pair[0]["weight"]


def test_support_macro_is_laid_out_by_the_support_seating_and_escape_room_is_reserved(tmp_path):
    body = (g.ic("U410", 40, 40, ["VDD", "A1", "D410", "GND", "A2", "A3"], pitch=0.5, court=(-3, -2, 3, 2))
            + g.fp("C411", 80, 70, [g.pad("1", -0.5, 0, "D410"), g.pad("2", 0.5, 0, "GND")], name="C_0402_1005Metric", court=(-1, -0.5, 1, 0.5))
            + g.fp("C412", 10, 10, [g.pad("1", -0.5, 0, "VDD"), g.pad("2", 0.5, 0, "GND")], name="C_0402_1005Metric", court=(-1, -0.5, 1, 0.5))
            + g.ic("U2", 70, 20, ["A1", "A2", "A3", "GND"], pitch=1.27, court=(-3, -3, 3, 3)))
    B = RD.load(g.board(tmp_path, body))
    cl = CL.build(B, {}, None)[0]
    own = {s["ref"] for c in cl if c["ic"] == "U410" for s in c["supports"] if s["sure"]}
    assert own
    macros, rigid = PC.build_macros(B, {}, set(), cl)
    assert macros["U410"][0] == "U410" and set(macros["U410"][1:]) == own and rigid == []
    for r in own:                                                      # laid out at the IC, no overlap inside the cell
        assert GM.rect_gap(B["parts"]["U410"]["court"], B["parts"][r]["court"]) < 3.0
        assert not GM.overlap(B["parts"]["U410"]["court"], B["parts"][r]["court"], -0.001)
    assert PC.build_macros(B, {}, {"U410"}, CL.build(B, {}, None)[0])[0] == {}           # a held IC is no macro
    grp = {"placement": {"group": {"intent:body": {"refs": ["U2", "C411"]}}}}
    GM.place(B["parts"]["C411"], 74.5, 20, 0)
    macros, rigid = PC.build_macros(B, grp, set(), [], support=False)
    assert macros == {"group:intent:body": ["U2", "C411"]} and rigid == ["group:intent:body"]
    pw = {"power": {"VDD": {}}}
    res = PC.escape_reserve(B, pw, None, NC.classify(B, pw, None))
    assert 0 < res["U410"] <= 1.0 and "C412" not in res              # the IC's supply pin must own a via site; passives get none
    assert PC.escape_reserve(B, dict(pw, escape={"enabled": False}), None, NC.classify(B, pw, None)) == {}


def test_feedback_inflates_crowded_parts_and_blocks_beside_fixed_ones(tmp_path):
    B = RD.load(mk(tmp_path))
    S = PC.settings({})
    infl, blocks = {}, []
    blocked = [{"ref": "U1", "pin": "3", "blocker": "escape of C2.1 (GND)", "blame": [["R1.1", 90], ["U1.2", 50]]}]
    find = [{"code": "PLAN_OVERFLOW", "eff": 2.0, "parts": ["U2", "J1"], "box": [6, 38, 8, 40], "layer": "F.Cu"},
            {"code": "PLAN_OVERFLOW", "eff": 1.0, "parts": ["U2", "C1"], "box": [30, 30, 32, 32], "layer": "In1.Cu"},
            {"code": "PLAN_PIN_SEALED", "parts": ["R2"]}]
    n, nb = PC.feedback(B, {"J1", "MH1"}, find, blocked, infl, blocks, S)
    assert n == len(infl) and set(infl) == {"U1", "C2", "R1", "U2", "C1"} and "J1" not in infl and "R2" not in infl
    assert infl["U1"] == 0.4 and infl["C2"] == 0.2 and infl["U2"] == 0.8
    assert nb == 1 and blocks == [((6.0, 38.0, 8.0, 40.0), {"F"})]     # only the overflow that borders a fixed part
    for _ in range(9):
        PC.feedback(B, {"J1", "MH1"}, find, blocked, infl, blocks, S)
    assert max(infl.values()) == S["inflate_max_mm"] and len(blocks) == 1


INTENT = {"placement": {"fixed": ["J1"], "cypress": {"trials": 3}},
          "intent": {"near": [{"name": "r1-u2", "refs": ["R1"], "to": "U2", "max_mm": 30.0}],
                     "region": [{"name": "corner", "rect": [80, 60, 100, 80]}], "support": {"decap_hard": False}}}


def test_intent_flow_scores_every_trial_with_the_gate_and_records_it(tmp_path):
    p = mk(tmp_path)
    B = RD.load(p)
    said = []
    mv, rep = PC.place(B, INTENT, None, str(tmp_path / "w"), fake_home(tmp_path), sys.executable, log=said.append, board=p)
    assert rep["flow"] == "intent" and len(rep["trials"]) == 3 and rep["rejected"] == 0
    for t in rep["trials"]:
        assert {"plan", "overflow", "risk_nets", "blocked", "required", "hpwl", "intent", "macros", "inflated", "blocks"} <= set(t)
        assert t["intent"]["hard_unmet"] == 0 and t["score"] >= t["plan"]     # (pcbnew cannot open this synthetic file: no fan-out count)
    assert rep["score"] == min(t["score"] for t in rep["trials"]) and rep["zone_blockages"] == 2 and rep["links"] >= 1
    assert rep["trials"][0]["macros"] >= 1 and rep["trials"][1]["macros"] == 0          # "auto": with, then without
    assert rep["params"]["target_density"] and "trial" in rep["summary"] and rep["gate"]["plan"] is not None
    assert rep["overlaps"] == 0 and rep["fixed_moved"] == [] and "J1" not in mv
    corner = (80, 60, 100, 80)
    assert not any(GM.overlap(q["court"], corner) for q in B["parts"].values())
    assert os.path.exists(tmp_path / "w" / "trial0" / "bookshelf" / "board.aux")       # one design per trial (feedback changes it)
    assert "k" in open(tmp_path / "w" / "trial0" / "bookshelf" / "board.nodes").read()


def test_a_trial_with_unmet_hard_intent_is_rejected(tmp_path):
    p = mk(tmp_path)
    B = RD.load(p)
    cons = {"placement": {"fixed": ["J1"], "cypress": {"trials": 2}},
            "intent": {"away": [{"name": "apart", "a": ["U1"], "b": ["U2"], "min_mm": 60.0}], "support": {"decap_hard": False}}}
    said, h = [], fake_home(tmp_path)
    with pytest.raises(PC.Unavailable, match="hard intent unmet: away apart"):
        PC.place(B, cons, None, str(tmp_path / "w"), h, sys.executable, log=said.append, board=p)
    assert sum("REJECTED" in m for m in said) == 2 and any("NOT MET hard away apart" in m for m in said)
    assert B["parts"]["U1"]["pos"] == (80, 70)                         # nothing was taken from a rejected trial
    r = PC.place_file(p, str(tmp_path / "o.kicad_pcb"), cons, None, h, sys.executable, log=said.append)
    assert isinstance(r, str) and "hard intent unmet" in r and not os.path.exists(tmp_path / "o.kicad_pcb")


def test_cli_and_pipeline_run_the_intent_flow_and_finish_with_the_gate(tmp_path, monkeypatch):
    p, out = mk(tmp_path), str(tmp_path / "o.kicad_pcb")
    cons = tmp_path / "c.toml"
    cons.write_text('[placement]\nfixed = ["J1"]\n[placement.cypress]\ntrials = 3\n[intent.support]\ndecap_hard = false\n'
                    '[[intent.near]]\nname = "r1-u2"\nrefs = ["R1"]\nto = "U2"\nmax_mm = 30.0\n'
                    '[[intent.region]]\nname = "corner"\nrect = [80, 60, 100, 80]\n')
    assert C.load(str(cons))["intent"] == INTENT["intent"]
    h = fake_home(tmp_path)
    r = _cli("place", "--board", p, "--strategy", "routable", "--engine", "cypress", "--cypress-home", h,
             "--cypress-python", sys.executable, "--constraints", str(cons), "--out", out)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "intent flow: trial" in r.stdout and "INTENT: " in r.stdout and "INTENT: FAIL" not in r.stdout
    rec = PC.read_record(out)
    assert rec["engine"] == "cypress" and rec["flow"] == "intent" and rec["intent"]["gate"].startswith("INTENT:")
    assert [t["score"] for t in rec["trials"]] and rec["params"] and os.path.exists(os.path.splitext(out)[0] + ".intent.json")
    monkeypatch.setenv("CYPRESS_HOME", h)
    monkeypatch.setenv("CYPRESS_PYTHON", sys.executable)
    d = dict(board=p, workdir=str(tmp_path / "W"), constraints=str(cons), spec=None, from_stage="floorplan", engine="cypress",
             to_stage="floorplan", flow="constructive", status=False, waive=[], force=True, api=False)
    PL.run(argparse.Namespace(**d))
    st = PL.load_state(d["workdir"])["stages"]["floorplan"]
    assert st["verdict"] == "pass" and st["engine"].startswith("cypress ") and "kept" in st["engine"] and "score" in st["engine"]
