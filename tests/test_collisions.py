"""Copper-collision gate (legality.collisions): true pad shapes, grouping by footprint pair, keep-outs, holes,
pour islands, project severities, route-ready --collisions-only and the pipeline route-stage block."""
import json, os, sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
from fluxplace import routediag as RD, legality as LGL, preroute as PR
import cli


def P(num, dx, dy, net="", shape="rect", w=0.6, h=0.6, typ="smd", layers='"F.Cu"', drill=None):
    n = f' (net "{net}")' if net else ""
    d = f" (drill {drill})" if drill else ""
    return f'(pad "{num}" {typ} {shape} (at {dx} {dy} 0) (size {w} {h}){d} (layers {layers}){n})'


def FP(ref, x, y, pads, court=None, extra=""):
    return G.fp(ref, x, y, pads, court=court).replace(")\n", f" {extra})\n", 1) if extra else G.fp(ref, x, y, pads, court=court)


def _load(tmp_path, body, severities=None, name="b.kicad_pcb"):
    p = G.board(tmp_path, body, name=name)
    if severities is not None:
        (tmp_path / name.replace(".kicad_pcb", ".kicad_pro")).write_text(json.dumps(
            {"board": {"design_settings": {"rule_severities": severities, "rules": {"min_hole_clearance": 0.25}}}}))
    return RD.load(p), p


def _kinds(f, pair):
    return next((x["kinds"] for x in f if x["pair"] == list(pair)), {})


def test_overlap_grouped_per_footprint_pair(tmp_path):
    # J1 sits over U1: three overlapping pad pairs of different nets -> ONE finding for the pair, FAIL, electrical
    u = FP("U1", 20, 20, [P("1", 0, 0, "A"), P("2", 1, 0, "B"), P("3", 2, 0, "C")])
    j = FP("J1", 20, 20, [P("1", 0, 0, "X"), P("2", 1, 0, "Y"), P("3", 2, 0, "Z")])
    B, _ = _load(tmp_path, u + j)
    f = LGL.collisions(B)
    assert len([x for x in f if x["pair"] == ["J1", "U1"]]) == 1
    x = f[0]
    assert x["level"] == "FAIL" and x["kinds"]["copper_overlap"] == 3 and "electrical" in x["relevance"]


def test_same_net_and_same_part_multi_piece_pins_are_not_collisions(tmp_path):
    # a QFN-style part with two copper pieces both numbered "1" (overlapping each other) and a GND neighbour of
    # the same net touching it: nothing to report
    u = FP("U1", 20, 20, [P("1", 0, 0, "GND", w=2, h=2), P("1", 0.8, 0, "GND", w=2, h=2), P("2", 3, 0, "SW")])
    c = FP("C1", 21.6, 20, [P("1", 0.0, 0, "GND")])
    B, _ = _load(tmp_path, u + c)
    assert not any(x["kinds"].keys() & {"copper_overlap", "copper_clearance"} for x in LGL.collisions(B))


def test_true_shapes_round_pads_diagonal_do_not_overlap(tmp_path):
    # two 1 mm circles 0.9 mm apart on each axis: their bounding boxes overlap, the copper does not (gap 0.27)
    a = FP("H1", 20, 20, [P("1", 0, 0, "A", shape="circle", w=1, h=1)])
    b = FP("H2", 20.9, 20.9, [P("1", 0, 0, "B", shape="circle", w=1, h=1)])
    B, _ = _load(tmp_path, a + b)
    assert not any("copper_overlap" in x["kinds"] or "copper_clearance" in x["kinds"] for x in LGL.collisions(B))
    b2 = FP("H2", 20.6, 20.6, [P("1", 0, 0, "B", shape="circle", w=1, h=1)])
    B2, _ = _load(tmp_path, a + b2, name="c.kicad_pcb")
    assert _kinds(LGL.collisions(B2), ("H1", "H2")).get("copper_overlap") == 1


def test_clearance_follows_project_severity_overlap_always_fails(tmp_path):
    a = FP("R1", 20, 20, [P("1", 0, 0, "A")])
    b = FP("R2", 20.65, 20, [P("1", 0, 0, "B")])            # 0.05 mm gap, inside the 0.14 clearance
    B, _ = _load(tmp_path, a + b, severities={"clearance": "warning", "shorting_items": "ignore"})
    x = LGL.collisions(B)[0]
    assert x["kinds"]["copper_clearance"] == 1 and x["levels"]["copper_clearance"] == "WARN"
    assert "copper_overlap" not in x["kinds"] and "manufacturing" in x["relevance"]
    c = FP("R2", 20.3, 20, [P("1", 0, 0, "B")])
    B, _ = _load(tmp_path, a + c, severities={"shorting_items": "ignore"}, name="c.kicad_pcb")
    assert LGL.collisions(B)[0]["level"] == "FAIL"         # overlap is hard whatever the project says


KEEPOUT = ('(zone (net 0) (net_name "") (layers "F.Cu") (name "SIMKO") (hatch edge 0.5) '
           '(keepout (tracks not_allowed) (vias not_allowed) (pads not_allowed) (copperpour not_allowed) '
           '(footprints allowed)) (polygon (pts (xy 29 29) (xy 31 29) (xy 31 31) (xy 29 31))))')


def test_footprint_rule_area_flags_other_parts_and_own_npth(tmp_path):
    hole = '(pad "" np_thru_hole circle (at 0 0) (size 1 1) (drill 1) (layers "*.Cu" "*.Mask"))'
    owner = FP("J1", 30, 30, [P("1", 3, 0, "A"), hole], extra=KEEPOUT)
    r = FP("R1", 30.5, 30.5, [P("1", 0, 0, "B")])           # pad inside the J1 rule area
    far = FP("R2", 50, 50, [P("1", 0, 0, "B")])
    B, _ = _load(tmp_path, owner + r + far)
    f = LGL.collisions(B)
    ko = [x for x in f if "keepout" in x["kinds"]]
    assert len(ko) == 1 and ko[0]["refs"] == ["R1"] and ko[0]["level"] == "FAIL" and ko[0]["relevance"] == ["placement"]
    own = [x for x in f if "keepout_own" in x["kinds"]]
    assert own and own[0]["refs"] == ["J1"]
    assert own[0]["level"] == "WARN" and own[0]["relevance"] == ["footprint"]      # a footprint detail: reported, never a FAIL
    assert not any("R2" in x["refs"] for x in f)


def test_hole_in_courtyard_and_hole_clearance(tmp_path):
    mh = FP("MH1", 40, 40, [P("1", 0, 0, "GND", shape="circle", w=3, h=3, typ="thru_hole", layers='"*.Cu"', drill=2)])
    c = FP("C1", 41.0, 40, [P("1", 0.5, 0, "VIN")], court=(-0.5, -1, 2, 1))   # pad edge 0.2 mm from the hole
    B, _ = _load(tmp_path, mh + c, severities={"pth_inside_courtyard": "warning"})
    k = _kinds(LGL.collisions(B), ("C1", "MH1"))
    f = LGL.collisions(B)
    assert k.get("hole_in_courtyard") == 1 and k.get("hole_clearance") == 1
    assert f[0]["levels"]["hole_in_courtyard"] == "WARN" and f[0]["levels"]["hole_clearance"] == "FAIL"


def test_pour_island_needs_a_touching_item_of_its_net(tmp_path):
    zone = ('(zone (net "GND") (layer "F.Cu") (hatch edge 0.5) (polygon (pts (xy 0 0) (xy 90 0) (xy 90 70) (xy 0 70)))'
            ' (filled_polygon (layer "F.Cu") (pts (xy 10 10) (xy 20 10) (xy 20 20) (xy 10 20)))'
            ' (filled_polygon (layer "F.Cu") (pts (xy 60 60) (xy 70 60) (xy 70 65) (xy 60 65))))\n')
    c = FP("C1", 15, 15, [P("1", 0, 0, "GND"), P("2", 1, 0, "X")])
    B, _ = _load(tmp_path, c + zone)
    isl = [x for x in LGL.collisions(B) if "pour_island" in x["kinds"]]
    assert len(isl) == 1 and isl[0]["pair"] == ["GND@F.Cu"] and "(65.0, 62.5)" in isl[0]["msg"]


def test_relevance_filter_and_route_ready_collisions_only(tmp_path, capsys):
    u = FP("U1", 20, 20, [P("1", 0, 0, "A")])
    j = FP("J1", 20, 20, [P("1", 0, 0, "X")])
    B, p = _load(tmp_path, u + j)
    assert LGL.collisions(B, relevance=["routing"]) == []
    assert LGL.collisions(B, relevance=["electrical"])
    with pytest.raises(SystemExit) as e:
        cli.main(["--full", "route-ready", p, "--collisions-only"])
    assert e.value.code == 1 and "J1 x U1" in capsys.readouterr().out
    cons = tmp_path / "c.toml"
    cons.write_text('[review]\nwaive = ["COLLISION:J1 x U1"]\n')
    cli.main(["--full", "route-ready", p, "--collisions-only", "--constraints", str(cons)])      # waived: exit 0
    f, card = PR.run(RD.load(p), {}, collide=True)
    assert card["counts"]["COLLISION"] == 1 and card["fail"] >= 1


def test_pipeline_route_stage_blocks_on_collision(tmp_path):
    from test_pipeline import _args
    from fluxplace import pipeline as PL
    u = FP("U1", 20, 20, [P("1", 0, 0, "A"), P("2", 5, 0, "B")])
    j = FP("J1", 20, 20, [P("1", 0, 0, "X"), P("2", 9, 9, "A")])
    p = G.board(tmp_path, u + j)
    a = _args(tmp_path, p, force=False, from_stage="route", to_stage="route", answer=["rule-floors=standard"])
    assert PL.run(a) == 1
    st = PL.load_state(a.workdir)
    assert st["stages"]["route"]["verdict"] == "blocked" and "collision gate" in st["stages"]["route"]["line"]
    assert os.path.exists(os.path.join(a.workdir, "7-route", "collisions.json"))
