"""Pair terminals whose own neighbourhood is in the way of the coupled exit (W16 USB_DM_J / USB_DP_J, refused every run):
a SPLIT terminal (foreign pad + its fan-out via between the P and N pads), a twin receptacle whose tap via stands in the exit,
and a tap via on a leg in the audit."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import pairaudit as PA
from fluxplace import pairgeo as G
from fluxplace import pairlay as PL
from fluxplace import pairs as PR
from fluxplace import routediag as RD


def seg(a, b, net, layer="F.Cu", w=0.15):
    return f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) (layer "{layer}") (net "{net}"))\n'


def via(x, y, net, d=0.46):
    return f'(via (at {x} {y}) (size {d}) (drill 0.2) (layers "F.Cu" "B.Cu") (net "{net}"))\n'


def split_body(dx=9.6, dy=2.0):
    """a flow-through protection array (P, GND, N pads in one row, the GND pin's fan-out stub and via in front of the row) and a
    plain two-pad part `dx` to the side and `dy` in front: the nearest way in is from the side, across the GND copper."""
    row = [PB.pad("1", 0, 0, "DP", 0.6, 1.3), PB.pad("2", 0.95, 0, "GND", 0.6, 1.3), PB.pad("3", 1.9, 0, "DM", 0.6, 1.3)]
    far = [PB.pad("1", 0, 0, "DP", 0.3, 1.1), PB.pad("2", 0.5, 0, "DM", 0.3, 1.1)]
    return (PB.fp("U8", 40, 40, row) + PB.fp("J1", 40.7 + dx, 40 + dy, far)
            + seg((40.95, 40), (40.95, 41.17), "GND", w=0.127) + via(40.95, 41.17, "GND"))


def test_split_terminal_is_detected_by_geometry(tmp_path):
    B = RD.load(PB.write(tmp_path, split_body()))
    items = G.Items.of_board(B)
    terms, _lone = PR.terminals(B, "DP", "DM", 16.0)
    u8, j1 = (next(t for t in terms if t["name"] == r) for r in ("U8", "J1"))
    assert PR._split(items, u8, ["F.Cu"], ("DP", "DM"), 0.13, ())
    assert not PR._split(items, j1, ["F.Cu"], ("DP", "DM"), 0.13, ())


def test_pair_parts_round_the_foreign_via_between_the_pads_of_a_split_terminal(tmp_path):
    b = PB.write(tmp_path, split_body())
    out = str(tmp_path / "o.kicad_pcb")
    rep = PR.run(b, {"pairlay": {"workers": 1}}, None, out, fallback=False)
    row = rep["pairs"][0]
    assert row["open"] == [0, 0] and row["status"] == "complete", row
    B = RD.load(out)
    lens = [PA.profile(B, n)["length"] for n in ("DP", "DM")]
    assert abs(lens[0] - lens[1]) < 0.15, lens                        # the legs part symmetrically: no long way round for one of them


def test_exact_check_refusal_shuts_the_centre_line_cells_of_the_fouling_leg(tmp_path):
    B = RD.load(PB.write(tmp_path, split_body()))
    items = G.Items.of_board(B)
    leg = [("seg", "F.Cu", (39.0, 41.17), (43.0, 41.17), 0.15)]        # straight through the GND via
    bad = PL.fouls(items, leg, "DM", 0.13, (), 0.05)
    assert bad and all(l == "F.Cu" and abs(x - 40.95) < 0.5 and it["label"] == "via GND" for l, x, _y, it in bad)
    grid = PL.Grid(B, ["F.Cu", "B.Cu"], 0.13, 0.3, 0.1, {"DP", "DM"})
    D, O = grid.view()
    j, i = grid.cell(40.95, 41.6)
    assert D["F.Cu"][j, i] > 0.13
    for l, x, y, it in bad:
        grid.shut(D, O, l, x, y, 0.5, it["label"] + " (exact check)")
    assert D["F.Cu"][j, i] < 0 and D["B.Cu"][j, i] > 0 and "exact check" in grid.label(int(O["F.Cu"][j, i]))


def test_a_via_the_path_runs_through_on_one_layer_is_a_tap_not_a_layer_change(tmp_path):
    two = PB.fp("A", 40, 40, [PB.pad("1", 0, 0, "N1", 0.3, 0.8)]) + PB.fp("B", 50, 40, [PB.pad("1", 0, 0, "N1", 0.3, 0.8)])
    tap = seg((40, 40), (45, 40), "N1") + via(45, 40, "N1") + seg((45, 40), (50, 40), "N1")
    assert PA.profile(RD.load(PB.write(tmp_path, two + tap)), "N1")["vias"] == 0
    hop = seg((40, 40), (45, 40), "N1") + via(45, 40, "N1") + seg((45, 40), (50, 40), "N1", "B.Cu") + via(50, 40, "N1")
    p = PA.profile(RD.load(PB.write(tmp_path, two + hop, name="c.kicad_pcb")), "N1")
    assert p["vias"] >= 1 and p["layers"] == ["F.Cu", "B.Cu"]


def test_twin_receptacle_with_long_pads_keeps_the_pair_whose_leg_runs_over_its_own_tap_via(tmp_path):
    # pads 1.15 long (a real USB-C row): the tap via sits right at the pad end, the leg passes over it on F.Cu and the audit
    # called that "vias 0 vs 1" and took the pair back (open 2 / 2)
    row = [PB.pad(str(i + 1), 0.5 * i, 0, net, 0.3, 1.15) for i, net in enumerate(("USB_DM", "USB_DP", "USB_DM", "USB_DP"))]
    duo = [PB.pad("1", 0, 0, "USB_DP", 0.3, 0.8), PB.pad("2", 0.8, 0, "USB_DM", 0.3, 0.8)]
    b = PB.write(tmp_path, PB.fp("J5", 40, 60, row) + PB.fp("U8", 40.35, 45, duo))
    out = str(tmp_path / "o.kicad_pcb")
    row = PR.run(b, {"pairlay": {"workers": 1}}, None, out, fallback=False)["pairs"][0]
    assert row["open"] == [0, 0], row
    a = PA.audit(RD.load(out), [("USB_DP", "USB_DM")], {}, set())[("USB_DP", "USB_DM")]
    assert set(a["causes"]) <= {"skew"}, a
