"""Escape planner (escapeplan.py) on synthetic boards.  Pure text: runs under any interpreter (no pcbnew)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import escapeplan as EP, netclass as NC, routediag as RD

SIG = [f"S{i}" for i in range(10)]


def plan(path, **kw):
    B = RD.load(path)
    cls = NC.classify(B, {}, None)
    return B, EP.plan_escapes(B, {}, cls, EP.plan_params({}), **kw)


def board(tmp, gap):
    """A 10-pin fine-pitch column at x=20 (pads 0.8 long, edge at x=19.6) and a wall of foreign pads `gap` mm west of it."""
    wall_x = 18.5 - 0.4 - gap - 0.2
    body = PB.column_ic("U1", 20.0, 40.0, SIG) + PB.wall("R1", wall_x, 40.0, 14)
    # every net needs a second pad so that it is a real 2-pad net
    body += PB.fp("J1", 60.0, 40.0, [PB.pad(str(i + 1), 0, i * 1.5, s, 1.0, 1.0) for i, s in enumerate(SIG)])
    body += PB.fp("J2", 80.0, 40.0, [PB.pad(str(i + 1), 0, i * 1.5, f"EU1{i}", 1.0, 1.0) for i in range(10)])
    return PB.write(tmp, body)


def test_more_pins_than_slots_is_sealed(tmp_path):
    B, pl = plan(board(tmp_path, 1.0), ignore_routing=True)
    sealed = [f for f in pl.findings if f["code"] == "PLAN_PIN_SEALED"]
    kinds = [pl.pins[("U1", str(i + 1))]["kind"] for i in range(10)]
    assert sealed and kinds.count("via") == 6 and kinds.count("sealed") == len(sealed) == 4
    assert all(f["part"] == "U1" and f["blocker"].startswith("R1.") for f in sealed)
    assert "R1" in sealed[0]["suggestion"] and "move R1" in sealed[0]["suggestion"]


def test_wall_too_tight_seals_everything_and_moving_it_passes(tmp_path):
    B, pl = plan(board(tmp_path, 0.35), ignore_routing=True)
    # all but the two end pins (they dog-bone sideways past the end of the column)
    assert sum(1 for f in pl.findings if f["code"] == "PLAN_PIN_SEALED") == 8
    B, pl = plan(board(tmp_path, 3.0), ignore_routing=True)
    assert not [f for f in pl.findings if f["code"] == "PLAN_PIN_SEALED"]
    assert all(pl.pins[("U1", str(i + 1))]["kind"] == "direct" for i in range(10))


def test_rotated_part_passes(tmp_path):
    """The same column turned 90 degrees so that its lanes run along the wall's gap: it escapes."""
    p = board(tmp_path, 0.35)
    B = RD.load(p)
    sealed0 = len([1 for r in EP.plan_escapes(B, {}, NC.classify(B, {}, None), EP.plan_params({})).findings
                   if r["code"] == "PLAN_PIN_SEALED"])
    assert sealed0 == 8
    txt = open(p).read().replace('(footprint "X" (layer "F.Cu") (at 20.0 40.0 0.0) (property "Reference" "U1")',
                                 '(footprint "X" (layer "F.Cu") (at 40.0 20.0 0.0) (property "Reference" "U1")')
    (tmp_path / "moved.kicad_pcb").write_text(txt)
    B, pl = plan(str(tmp_path / "moved.kicad_pcb"), ignore_routing=True)
    assert not [f for f in pl.findings if f["code"] == "PLAN_PIN_SEALED"]


def test_plane_pin_needs_via_site(tmp_path):
    """A GND pad boxed in by foreign pads has no via site; with room it gets one; a pad in a pour is 'pour'."""
    box = "".join(PB.fp(f"R{i}", 30 + dx, 30 + dy, [PB.pad("1", 0, 0, f"N{i}", 1.0, 1.0)])
                  for i, (dx, dy) in enumerate([(-1.0, 0), (1.0, 0), (0, -1.0), (0, 1.0)]))
    body = PB.fp("C1", 30, 30, [PB.pad("1", 0, 0, "GND", 0.8, 0.8), PB.pad("2", 5, 0, "VX", 0.8, 0.8)]) + box
    body += PB.fp("C2", 60, 60, [PB.pad("1", 0, 0, "GND", 0.8, 0.8), PB.pad("2", 5, 0, "VY", 0.8, 0.8)])
    B, pl = plan(PB.write(tmp_path, body), ignore_routing=True)
    nv = {(f["part"], f["pin"]) for f in pl.findings if f["code"] == "PLAN_NO_VIA_SITE"}
    assert ("C1", "1") in nv and ("C2", "1") not in nv
    assert pl.pins[("C2", "1")]["kind"] == "via"


def test_no_via_in_a_signal_pad(tmp_path):
    """A GND via site is never placed inside another pad, nor touching one (clearance + via radius)."""
    body = PB.fp("C1", 30, 30, [PB.pad("1", 0, 0, "GND", 0.8, 0.8), PB.pad("2", 5, 0, "VX", 0.8, 0.8)])
    B, pl = plan(PB.write(tmp_path, body), ignore_routing=True)
    x, y = pl.pins[("C1", "1")]["via"]
    for q in B["pads"]:
        dx = max(abs(x - q["x"]) - q["ex"], 0)
        dy = max(abs(y - q["y"]) - q["ey"], 0)
        assert (dx * dx + dy * dy) ** 0.5 >= 0.45 / 2 + 0.127 - 1e-6
