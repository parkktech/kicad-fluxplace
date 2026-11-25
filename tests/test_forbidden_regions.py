"""Forbidden regions: rule-area keep-outs (radius aware) and solder-mask openings (net aware), in the board model and in fanout."""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import escapeplan as EP, routediag as RD

HAVE = os.path.exists("/usr/bin/python3") and subprocess.run(
    ["/usr/bin/python3", "-c", "import pcbnew"], capture_output=True).returncode == 0

OPEN = ('(gr_rect (start 20 20) (end 40 40) (stroke (width 0) (type default)) (fill yes) (layer "B.Mask") '
        '(uuid "00000000-0000-0000-0000-0000000000a1"))\n')
GND_ZONE = ('(zone (net "GND") (net_name "GND") (layer "B.Cu") (name "") (hatch edge 0.5) (polygon (pts '
            '(xy 10 10) (xy 50 10) (xy 50 50) (xy 10 50))))\n')
NOVIA = ('(zone (net 0) (net_name "") (layer "F.Cu") (name "NOVIA") (hatch edge 0.5) (keepout (tracks allowed) (vias not_allowed) '
         '(pads allowed) (copperpour allowed) (footprints allowed)) (polygon (pts (xy 20 20) (xy 40 20) (xy 40 40) (xy 20 40))))\n')


def load(tmp_path, body):
    return RD.load(PB.write(tmp_path, body))


def test_mask_opening_is_a_forbidden_region_for_other_nets(tmp_path):
    B = load(tmp_path, OPEN + GND_ZONE)
    assert [(m["owner"], m["layers"]) for m in B["mask_open"]] == [("GND", {"B.Cu"})]
    ob = EP.Obstacles(B, EP.plan_params({}), True)
    allc = frozenset(B["copper"])
    assert ob.keepout_hit(30, 30, allc, vias=True, net="VX", r=0.225)          # a via of another net: exposed
    assert not ob.keepout_hit(30, 30, allc, vias=True, net="GND", r=0.225)     # the owner net's via is fine
    assert ob.keepout_hit(30, 30, allc, vias=True, r=0.225)                    # unknown net: blocked
    assert not ob.keepout_hit(45, 45, allc, vias=True, net="VX", r=0.225)      # outside
    assert ob.keepout_hit(19.8, 30, allc, vias=True, net="VX", r=0.225)        # centre outside, copper (+ mask margin) inside
    assert ob.keepout_hit(30, 30, frozenset({"B.Cu"}), net="VX", r=0.075)      # a track on the opening's side
    assert not ob.keepout_hit(30, 30, frozenset({"F.Cu"}), net="VX", r=0.075)  # front copper is under mask


def test_no_via_rule_area_blocks_a_via_by_its_copper_not_its_centre(tmp_path):
    B = load(tmp_path, NOVIA)
    ob = EP.Obstacles(B, EP.plan_params({}), True)
    allc = frozenset(B["copper"])
    assert ob.keepout_hit(30, 30, allc, vias=True, net="VX", r=0.225)
    assert ob.keepout_hit(40.2, 30, allc, vias=True, net="VX", r=0.225)        # centre 0.2 outside, annulus inside
    assert not ob.keepout_hit(40.5, 30, allc, vias=True, net="VX", r=0.225)
    assert not ob.keepout_hit(30, 30, frozenset({"F.Cu"}), net="VX", r=0.075)  # tracks are allowed there


def test_unfilled_mask_graphic_is_no_opening(tmp_path):
    B = load(tmp_path, OPEN.replace("(fill yes)", "(fill no)"))
    assert B["mask_open"] == []


def test_pocket_field_blocks_vias_in_a_foreign_opening(tmp_path):
    from fluxplace import pocket as PK
    B = load(tmp_path, OPEN + GND_ZONE)
    for own, blocked in (({"VX"}, True), ({"GND"}, False)):
        F = PK.Field(B, (30, 30), 3.0, 0.1, own=own, clearance=0.127)
        j, i = F.cell(30, 30)
        assert bool(F.via_ok(0.45)[j, i]) is (not blocked)


def _fan_gnd_via(tmp_path, region):
    import test_fanout as TF
    from fluxplace import fanout as FO
    caps = PB.fp("C0", 30, 30, [PB.pad("1", -0.5, 0, "+3V3", 0.5, 0.5), PB.pad("2", 0.5, 0, "GND", 0.5, 0.5)])
    caps += PB.fp("J1", 60, 60, [PB.pad("1", 0, 0, "+3V3", 1, 1), PB.pad("2", 3, 0, "GND", 1, 1)])
    path = PB.write(tmp_path, region + caps)
    B, res = FO.run(path, {}, None, str(tmp_path / "out.kicad_pcb"), TF.args(), None)
    return [p for p in res["pins"] if p["ref"] == "C0" and p["net"] == "GND"][0]


def _inside(v):
    return 19.7 < v[0] < 40.3 and 19.7 < v[1] < 40.3


@pytest.mark.skipif(not HAVE, reason="pcbnew not available")
@pytest.mark.parametrize("region", [OPEN + GND_ZONE.replace('"GND"', '"+5V"'), NOVIA])
def test_fanout_puts_no_via_in_a_forbidden_region(tmp_path, region):
    pin = _fan_gnd_via(tmp_path, region)
    assert not (pin.get("via") and _inside(pin["via"]))
    assert pin["ok"] is False or pin["via"] is not None or pin.get("kind")      # reported, never silently placed inside


@pytest.mark.skipif(not HAVE, reason="pcbnew not available")
def test_fanout_may_put_the_owner_nets_via_in_its_mask_opening(tmp_path):
    pin = _fan_gnd_via(tmp_path, OPEN + GND_ZONE)
    assert pin["ok"] and pin["via"] and _inside(pin["via"])


@pytest.mark.skipif(not HAVE, reason="pcbnew not available")
def test_plane_via_drop_honours_mask_openings(tmp_path):
    import pcbnew
    from fluxplace import planes as PL
    b = pcbnew.LoadBoard(PB.write(tmp_path, OPEN + GND_ZONE + PB.fp("C0", 60, 60, [PB.pad("1", 0, 0, "VX", 1, 1), PB.pad("2", 3, 0, "GND", 1, 1)])))
    assert PL.via_at_points(b, "VX", [(30, 30)]) == 0          # another net's via inside the opening
    assert PL.via_at_points(b, "VX", [(45, 45)]) == 1
    assert PL.via_at_points(b, "GND", [(30, 30)]) == 1         # the owner net's via is fine
