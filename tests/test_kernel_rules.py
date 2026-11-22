"""K7: kernel net_rule against the legacy model (pcbnew effective net class), with and without a project."""
import json
import os

import pytest

from fluxplace import kernel as K

pytestmark = pytest.mark.skipif(not K.available(), reason="run kernel/build.sh")

HEAD4 = ('(kicad_pcb (version 20260206) (generator "pcbnew")\n'
         '(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
         '(gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts") (uuid "12121212-1212-4121-8121-121212121212"))\n'
         '(segment (start 10 10) (end 20 10) (width 0.2) (layer "F.Cu") (net "SIG_1") (uuid "a1111111-1111-4111-8111-111111111111"))\n'
         '(segment (start 10 20) (end 20 20) (width 0.2) (layer "F.Cu") (net "A") (uuid "a2222222-2222-4222-8222-222222222222"))\n'
         '(segment (start 10 30) (end 20 30) (width 0.2) (layer "F.Cu") (net "GND") (uuid "a3333333-3333-4333-8333-333333333333"))\n'
         '(segment (start 10 40) (end 20 40) (width 0.2) (layer "F.Cu") (net "OTHER") (uuid "a4444444-4444-4444-8444-444444444444"))\n)\n')

PROJECT = {
    "board": {"design_settings": {"rules": {"min_clearance": 0.1, "min_track_width": 0.15,
                                           "min_via_diameter": 0.5, "min_through_hole_diameter": 0.3}}},
    "net_settings": {
        "classes": [
            {"name": "Default", "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "HS", "priority": 1, "clearance": 0.15, "track_width": 0.3, "via_diameter": 0.7, "via_drill": 0.35},
            {"name": "PWR", "priority": 2, "track_width": 0.5},
        ],
        "netclass_patterns": [{"netclass": "HS", "pattern": "SIG*"}],
        "netclass_assignments": {"A": ["PWR"]},
    },
}
KEYS = ("width", "clearance", "via_dia", "via_drill")


def _legacy(path):
    from fluxplace import hrgeom
    return hrgeom.Model(path, refill=False)


def _same(kern, leg):
    if kern["class"] != leg["class"]:
        return False
    return all(abs(kern[k] - leg[k]) < 1e-6 for k in KEYS)


def test_net_rule_with_project_matches_pcbnew(tmp_path):
    path = tmp_path / "p.kicad_pcb"
    path.write_text(HEAD4)
    (tmp_path / "p.kicad_pro").write_text(json.dumps(PROJECT))
    m = _legacy(str(path))
    with K.Kernel(str(path)) as k:
        assert k.call("info")["rules"]["source"] == "project"
        for net in ["SIG_1", "A", "GND", "OTHER", ""]:
            kern = k.call("net_rule", net=net)
            leg = m.net_rule(net)
            assert _same(kern, leg), (net, kern, leg)


def test_net_rule_without_project_matches_pcbnew_defaults(tmp_path):
    path = tmp_path / "n.kicad_pcb"
    path.write_text(HEAD4)
    m = _legacy(str(path))
    with K.Kernel(str(path)) as k:
        assert k.call("info")["rules"]["source"] == "fallback"
        for net in ["SIG_1", "GND"]:
            assert _same(k.call("net_rule", net=net), m.net_rule(net))


def test_set_floors_raises_and_never_lowers(tmp_path):
    path = tmp_path / "f.kicad_pcb"
    path.write_text(HEAD4)
    with K.Kernel(str(path)) as k:
        f = k.call("set_floors", clr=0.25, track=0.0, via_dia=0.0, via_drill=0.0)
        assert f["clr"] == 0.25
        assert k.call("net_rule", net="GND")["clearance"] == 0.25


@pytest.mark.skipif(not os.path.isfile("/var/kicad/utv-comms-bridge/W14/stageF/hand/r5.kicad_pcb"),
                    reason="W14 r5 not present")
def test_net_rule_on_w14_r5_every_net():
    path = "/var/kicad/utv-comms-bridge/W14/stageF/hand/r5.kicad_pcb"
    with K.Kernel(path) as k:
        nets = sorted({it["net_name"] for it in k.call("items")["items"]} - {""})
    m = _legacy(path)
    bad = [n for n in nets if not _same(K.Kernel(path).call("net_rule", net=n), m.net_rule(n))]
    assert not bad, bad[:5]
