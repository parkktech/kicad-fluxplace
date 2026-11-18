"""placement-check and pairs run the route stage's collision gate, rule areas included (W11 lesson)."""
import json, os, sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
from test_collisions import P, FP, _load
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
