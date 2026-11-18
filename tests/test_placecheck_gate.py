"""placement-check and pairs run the route stage's collision gate, rule areas included (W11 lesson)."""
import json, os, sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
from test_collisions import P, FP, _load
from fluxplace import legality as LGL
import cli
from fluxplace import placementcheck as PCK, routediag as RD

TRACKS_OUT = ('(zone (net 0) (net_name "") (layers "F.Cu") (name "TRAP") (hatch edge 0.5) '
              '(keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour allowed) '
              '(footprints allowed)) (polygon (pts (xy 29 29) (xy 31 29) (xy 31 31) (xy 29 31))))')


def _trapped_board(tmp_path):
    owner = FP("X1", 60, 60, [P("1", 0, 0, "GND")])            # any part; the rule area is board-level
    pad = FP("R1", 30.2, 30.2, [P("1", 0, 0, "GND")])          # connected pad inside the track-forbidding area
    return owner + pad + TRACKS_OUT


def test_placement_check_fails_on_track_forbidding_rule_area(tmp_path, capsys):
    B, _ = _load(tmp_path, _trapped_board(tmp_path))
    fails = PCK.gate_failures(B, {})
    assert fails and fails[0]["level"] == "FAIL" and "tracks not allowed" in fails[0]["msg"]
    cons = tmp_path / "c.toml"
    cons.write_text("[placement]\n")
    brd = str(tmp_path / "b.kicad_pcb")
    with pytest.raises(SystemExit) as e:
        cli.main(["placement-check", brd, "--constraints", str(cons), "--json"])
    res = json.loads(capsys.readouterr().out)
    assert e.value.code == 1 and not res["ok"]
    assert any("tracks not allowed" in c["msg"] for c in res["collisions"])


def test_pairs_refuses_with_the_gate_message(tmp_path, capsys):
    _load(tmp_path, _trapped_board(tmp_path), name="pp.kicad_pcb")
    cons = tmp_path / "c.toml"
    cons.write_text("[placement]\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["pairs", str(tmp_path / "pp.kicad_pcb"), "--constraints", str(cons),
                  "--out", str(tmp_path / "out.kicad_pcb")])
    assert str(e.value).startswith("pairs: refused") and "tracks not allowed" in str(e.value)
    assert not (tmp_path / "out.kicad_pcb").exists()


def _pour(net, x0, y0, x1, y1, layer="F.Cu"):
    return (f'(zone (net "{net}") (net_name "{net}") (layers "{layer}") (hatch edge 0.5) (fill yes) '
            f'(polygon (pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1}))) '
            f'(filled_polygon (layer "{layer}") (pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1}))))')


def _trapped_gnd(tmp_path, pour, allow_pours=True, name="pk.kicad_pcb"):
    area = TRACKS_OUT if allow_pours else TRACKS_OUT.replace("(copperpour allowed)", "(copperpour not_allowed)")
    body = FP("X1", 60, 60, [P("1", 0, 0, "GND")]) + FP("R1", 30.2, 30.2, [P("1", 0, 0, "GND")]) + area + pour
    return _load(tmp_path, body, name=name)


def test_pour_connected_pad_is_not_trapped(tmp_path):
    # the pad sits in a no-tracks area, but a GND pour on its layer that the area allows connects it: no FAIL
    B, _ = _trapped_gnd(tmp_path, _pour("GND", 0, 0, 60, 60))
    assert LGL.collision_gate(B)[0] is True
    assert not [x for x in LGL.collisions(B) if "keepout_trapped" in x["kinds"]]


def test_trapped_stays_fail_without_a_pour_or_when_pours_are_not_allowed(tmp_path):
    B, _ = _trapped_gnd(tmp_path, "", name="a.kicad_pcb")
    assert LGL.collision_gate(B)[0] is False
    B, _ = _trapped_gnd(tmp_path, _pour("GND", 0, 0, 60, 60), allow_pours=False, name="b.kicad_pcb")
    assert LGL.collision_gate(B)[0] is False
    B, _ = _trapped_gnd(tmp_path, _pour("VIN", 0, 0, 60, 60), name="c.kicad_pcb")   # another net's pour does not count
    assert LGL.collision_gate(B)[0] is False
