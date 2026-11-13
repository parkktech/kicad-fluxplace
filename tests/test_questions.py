"""Questions facility: ambiguous -> stops before writing (exit 3); answered -> proceeds and is recorded; --assume-defaults marks
outputs; a board with full records raises none; spec-vs-board contradiction raises one."""
import argparse, json, os, sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
from fluxplace import questions as QS, routediag as RD

FLOORS = {"fab": {"min_track_mm": 0.127, "min_clearance_mm": 0.127, "min_via_dia_mm": 0.4, "min_drill_mm": 0.2}}


def args(**kw):
    d = dict(answer=[], assume_defaults=False, qjson=False)
    d.update(kw)
    return argparse.Namespace(**d)


def bare_board(tmp_path, extra=""):
    body = G.two("R1", 20, 20, "A", "B") + G.two("R2", 40, 20, "A", "B") + extra
    return G.board(tmp_path, body, outline=(0, 0, 100, 80))


def test_ambiguous_stops_before_writing_and_lists_the_questions(tmp_path, capsys):
    b = bare_board(tmp_path)
    with pytest.raises(SystemExit) as e:
        QS.guard(args(), "route", b, {}, None)
    assert e.value.code == 3
    out = capsys.readouterr().out
    assert "rule-floors" in out and "default if forced" in out and "exit 3" in out
    doc = json.load(open(QS.questions_path(b)))
    q = {x["id"]: x for x in doc["questions"]}
    assert set(q["rule-floors"]) >= {"id", "topic", "found", "options", "default", "settle"}
    assert {o["choice"] for o in q["rule-floors"]["options"]} == {"standard", "relaxed", "advanced"}
    assert not os.path.exists(os.path.splitext(b)[0] + ".fluxmap.json")        # nothing but the questions file was written
    with pytest.raises(SystemExit) as e:
        QS.guard(args(qjson=True), "route", b, {}, None)
    assert e.value.code == 3 and '"questions"' in capsys.readouterr().out


def test_answered_proceeds_is_recorded_and_not_asked_again(tmp_path):
    b = bare_board(tmp_path)
    S, cons, spec = QS.guard(args(answer=["rule-floors=standard"]), "route", b, {}, None)
    assert cons["fab"]["min_track_mm"] == 0.127                                  # the answer is applied
    assert json.load(open(os.path.splitext(b)[0] + ".fluxmap.json"))["answers"] == {"rule-floors": "standard"}
    S2, cons2, _ = QS.guard(args(), "route", b, {}, None)                      # no flag now: found in the map
    assert cons2["fab"]["min_track_mm"] == 0.127
    S3, cons3, _ = QS.guard(args(), "route", bare_board(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path),
                            {"answers": {"rule-floors": "advanced"}}, None)     # constraints [answers]
    assert cons3["fab"]["min_track_mm"] == 0.1
    with pytest.raises(SystemExit) as e:
        QS.guard(args(answer=["rule-floors=bogus"]), "route", b, {}, None)
    assert "choose one of" in str(e.value)


def test_assume_defaults_marks_outputs_and_fab_refuses(tmp_path, capsys):
    b = bare_board(tmp_path)
    S, cons, _ = QS.guard(args(assume_defaults=True), "route", b, {}, None)
    assert "ASSUMED: rule-floors" in capsys.readouterr().out and cons["fab"]["min_track_mm"] == 0.2     # the forced default
    out = str(tmp_path / "out.kicad_pcb")
    open(out, "w").write(open(b).read())
    S.mark(out)
    assert QS.assumed_ids(out) == ["rule-floors"]
    out2 = str(tmp_path / "out2.kicad_pcb")
    open(out2, "w").write("x")
    QS.inherit(out, out2)
    assert QS.assumed_ids(out2) == ["rule-floors"]
    with pytest.raises(SystemExit) as e:
        QS.refuse_assumed(out2, "fab")
    assert "ASSUMED: rule-floors" in str(e.value)
    QS.refuse_assumed(b, "fab")                                                  # a board without marks passes


def test_full_records_raise_none(tmp_path, capsys):
    b = bare_board(tmp_path)
    cons = dict(FLOORS, route={"allow_rip": "never"}, placement={"fixed": []}, lock={"at": {}},
                pairs={"X": {"skew_mm": 0.1}}, preroute={"signal_layers": ["F.Cu", "B.Cu", "In1.Cu"]})
    for cmd in ("fanout", "modules", "floorplan", "route"):
        S, c2, _ = QS.guard(args(), cmd, b, cons, None)
        assert not S.open
    assert not os.path.exists(QS.questions_path(b))


def test_spec_board_contradiction_raises_one(tmp_path):
    b = bare_board(tmp_path)
    spec = {"components": [{"ref": "R1"}, {"ref": "R2"}, {"ref": "U9"}], "nets": {"A": {}, "GONE": {}}}
    cons = dict(FLOORS, route={"allow_rip": "never"}, preroute={"signal_layers": ["F.Cu", "B.Cu", "In1.Cu"]})
    with pytest.raises(SystemExit):
        QS.guard(args(), "fanout", b, cons, spec)
    q = json.load(open(QS.questions_path(b)))["questions"]
    assert [x["id"] for x in q] == ["spec-board"] and "U9" in q[0]["found"] and "GONE" in q[0]["found"]
    S, c2, s2 = QS.guard(args(answer=["spec-board=trust-board"]), "fanout", b, cons, spec)
    assert [c["ref"] for c in s2["components"]] == ["R1", "R2"] and "GONE" not in s2["nets"]
    spec_ok = {"components": [{"ref": "R1"}, {"ref": "R2"}], "nets": {"A": {}, "B": {}}}
    assert not QS.guard(args(), "fanout", b, cons, spec_ok)[0].open


def test_controlled_fixed_and_rails_and_layers(tmp_path):
    inner = "".join(G.fp(f"C{i}", 10 + 4 * i, 50, [G.pad("1", 0, 0, "RF_IN")]) for i in range(2)) + \
        "".join(G.fp(f"P{i}", 10 + 4 * i, 60, [G.pad("1", 0, 0, "VX")]) for i in range(9)) + \
        G.fp("H1", 5, 5, [G.pad("1", 0, 0, "", typ="np_thru_hole")])
    b = bare_board(tmp_path, inner)
    with pytest.raises(SystemExit):
        QS.guard(args(), "route", b, FLOORS, None)
    ids = {x["id"] for x in json.load(open(QS.questions_path(b)))["questions"]}
    assert {"controlled:rf", "rail-pour:VX", "rip-existing"} - ids == {"rip-existing"}      # no tracks yet: no rip question
    assert "layer-roles:inner" not in ids                                                   # 3-layer test board: one inner layer
    S, cons, _ = QS.guard(args(answer=["controlled:rf=ordinary", "rail-pour:VX=pour"]), "route", b, FLOORS, None)
    assert cons["placement"]["net"]["RF_IN"]["class"] == "signal" and cons["plan"]["pour"][0]["net"] == "VX"
    with pytest.raises(SystemExit):
        QS.guard(args(), "floorplan", b, FLOORS, None)
    ids = {x["id"] for x in json.load(open(QS.questions_path(b)))["questions"]}
    assert "fixed-parts" in ids


def test_rip_existing_and_ambiguous_layer_role(tmp_path):
    seg = '(segment (start 20 20) (end 40 20) (width 0.15) (layer "F.Cu") (net "A"))\n'
    zone = '(zone (net "GND") (layer "In1.Cu") (polygon (pts (xy 0 0) (xy 60 0) (xy 60 40) (xy 0 40))))\n'
    b = bare_board(tmp_path, seg + zone)
    with pytest.raises(SystemExit):
        QS.guard(args(), "route", b, FLOORS, None)
    q = {x["id"]: x for x in json.load(open(QS.questions_path(b)))["questions"]}
    assert "rip-existing" in q and "layer-role:In1.Cu" in q and "30 %" in q["layer-role:In1.Cu"]["found"]
    S, cons, _ = QS.guard(args(answer=["rip-existing=low-speed", "layer-role:In1.Cu=plane"]), "route", b, FLOORS, None)
    assert cons["route"]["allow_rip"] == "low-speed" and cons["preroute"]["plane_layers"] == ["In1.Cu"]
