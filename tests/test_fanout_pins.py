"""Multi-piece pins (one pin = every pad of a footprint sharing its number and net, touching or not), the same-net
link track for a piece that has no escape of its own, and the `[fanout] waive` / `[floorplan] waive_fail` waiver.
Synthetic boards built with tests/planboards.py; needs /usr/bin/python3 with pcbnew (the helper runs there)."""
import argparse
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import fanout as FO
from fluxplace import preroute as PR

HAVE = os.path.exists("/usr/bin/python3") and subprocess.run(
    ["/usr/bin/python3", "-c", "import pcbnew"], capture_output=True).returncode == 0
pytestmark = pytest.mark.skipif(not HAVE, reason="pcbnew not available")


def args(**kw):
    d = dict(track=None, clearance=None, via=None, drill=None, via_pad_gap=0.2, reach=2.2, lane=1.0, max_dom=60, nodes=200000,
             repairs=12, check_only=False)
    d.update(kw)
    return argparse.Namespace(**d)


def go(tmp, body, cons=None, **kw):
    path = PB.write(tmp, body)
    out = str(tmp / "out.kicad_pcb")
    B, res = FO.run(path, cons or {}, None, out, args(**kw), None)
    return B, res, FO.summarise(B, res)


def two_piece_ic(ref, x, y):
    """6-pin fine-pitch part. Pin 2 (GND) is two pieces: an edge pad between pins 1 and 3 on the top row, and a long thin
    strip in the gap between the rows (it touches neither row, so it is a second copper component of the same pin)."""
    pads = [PB.pad("1", -0.35, -1.0, "S1", 0.15, 0.65),
            PB.pad("2", 0.0, -1.0, "GND", 0.15, 0.65),
            PB.pad("3", 0.35, -1.0, "S3", 0.15, 0.65),
            PB.pad("4", -0.35, 1.0, "S4", 0.15, 0.65),
            PB.pad("5", 0.0, 1.0, "S5", 0.15, 0.65),
            PB.pad("6", 0.35, 1.0, "S6", 0.15, 0.65),
            PB.pad("2", 0.0, 0.0, "GND", 1.1, 0.13)]
    return PB.fp(ref, x, y, pads, court=(-1.2, -1.6, 1.2, 1.6))


def far(tmp_nets):
    return PB.fp("J1", 60, 20, [PB.pad(str(i + 1), 0, i * 2.0, n, 1.2, 1.2) for i, n in enumerate(tmp_nets)])


NETS = ["S1", "S3", "S4", "S5", "S6"]


def test_two_piece_pin_is_one_pin_and_both_pieces_escape(tmp_path):
    body = two_piece_ic("U1", 40, 40) + far(NETS + ["GND"])
    _, res, per = go(tmp_path, body)
    assert per["U1"]["req"] == 6                       # six pin NUMBERS, not seven copper components
    assert per["U1"]["ok"] == 6 and not per["U1"]["blocked"], per["U1"]
    pin2 = [p for p in res["pins"] if p["ref"] == "U1" and p["pin"] == "2"]
    assert len(pin2) == 2 and all(p["ok"] for p in pin2)   # both copper pieces of pin 2 have an escape


def test_unnamed_blocked_pin_still_fails(tmp_path):
    # a keep-out over the whole part: nothing of U1 can leave, every pin blocked
    body = two_piece_ic("U1", 40, 40) + PB.keepout(36, 35, 44, 45, "block") + far(NETS + ["GND"])
    _, _res, per = go(tmp_path, body)
    assert per["U1"]["ok"] == 0 and len(per["U1"]["blocked"]) == 6
    assert not per["U1"]["waived"]


def test_waiver_turns_named_blocked_pins_into_waived(tmp_path):
    body = two_piece_ic("U1", 40, 40) + PB.keepout(36, 35, 44, 45, "block") + far(NETS + ["GND"])
    cons = {"fanout": {"waive": ["U1\\.1: pad 1 under the keep-out, accepted", "U1\\.2: GND, accepted", "U1\\.3: accepted",
                                  "U1\\.4: accepted", "U1\\.5: accepted", "U1\\.6: accepted"]}}
    _, res, per = go(tmp_path, body, cons)
    assert len(per["U1"]["waived"]) == 6 and not per["U1"]["blocked"]
    assert all(p["waived"] for p in per["U1"]["waived"])


def test_waiver_names_only_the_pins_it_lists(tmp_path):
    body = two_piece_ic("U1", 40, 40) + PB.keepout(36, 35, 44, 45, "block") + far(NETS + ["GND"])
    cons = {"fanout": {"waive": ["U1\\.1: accepted"]}}
    _, _res, per = go(tmp_path, body, cons)
    assert [p["pin"] for p in per["U1"]["waived"]] == ["1"]
    assert sorted(p["pin"] for p in per["U1"]["blocked"]) == ["2", "3", "4", "5", "6"]


def test_floorplan_waive_fail_names_a_pin_for_the_fanout_stage(tmp_path):
    body = two_piece_ic("U1", 40, 40) + PB.keepout(36, 35, 44, 45, "block") + far(NETS + ["GND"])
    cons = {"floorplan": {"waive_fail": "run-local: U1.2 GND fan-out 1/6 (accepted)"}}
    pats, notes = FO.waivers(cons)
    assert notes and "U1.2" in notes[0]
    _, _res, per = go(tmp_path, body, cons)
    assert [p["pin"] for p in per["U1"]["waived"]] == ["2"]


def test_preroute_warns_on_waived_pin_until_its_net_is_connected(tmp_path):
    body = two_piece_ic("U1", 40, 40) + PB.keepout(36, 35, 44, 45, "block") + far(NETS + ["GND"])
    cons = {"fanout": {"waive": ["U1\\.1: accepted"]}}
    path = PB.write(tmp_path, body)
    from fluxplace import routediag as RD
    fnd = PR.waived_pin_findings(RD.load(path), cons)
    assert [f["code"] for f in fnd] == ["FANOUT_PIN_WAIVED"] and fnd[0]["level"] == "WARN"
    # the same board with the pin's net routed to it: no finding
    seg = (f'(segment (start 39.65 39.0) (end 39.65 37.0) (width 0.15) (layer "F.Cu") (net "S1"))\n')
    B2 = RD.load(PB.write(tmp_path, two_piece_ic("U1", 40, 40) + PB.keepout(36, 35, 44, 45, "block") + far(NETS + ["GND"]) + seg,
                          name="routed.kicad_pcb"))
    assert PR.waived_pin_findings(B2, cons) == []
