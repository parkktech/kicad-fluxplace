"""D71cj: rail widths for the router (power.route_widths), the pipeline's --power-nets flags, and batch rail order."""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import power as PW  # noqa: E402
from fluxplace import pipeline as PL  # noqa: E402
from fluxplace import batchroute as BR  # noqa: E402

CONS = {"power": {"BIG": {"width_mm": 7.2, "pour": True}, "MID": {"width_mm": 0.7}, "TINY": {"width_mm": 0.05}},
        "route": {"track_mm": 0.15}}


def test_route_widths_pour_spine_track_floor():
    w = PW.route_widths(CONS, layers=6, track_mm=0.15)
    assert w == {"BIG": 1.0, "MID": 0.7, "TINY": 0.15}


def test_route_widths_spine_override_and_exclude():
    cons = {**CONS, "route": {"power_spine_mm": 2.0}}
    assert PW.route_widths(cons, layers=6, track_mm=0.15, exclude={"MID"}) == {"BIG": 2.0, "TINY": 0.15}


def test_route_widths_no_current_left_out():
    assert PW.route_widths({"power": {"X": {"max_v": 3.3}}}, layers=6, track_mm=0.15) == {}


def _route(only, power_nets, pair=False):
    a = types.SimpleNamespace(router_py="python3", router_dir="/nonexistent", layers=["F.Cu", "B.Cu"])
    R = {"min_clearance": 0.127, "min_track": 0.127, "min_via": 0.45, "min_drill": 0.2, "clearance": 0.127, "track": 0.15,
         "via": 0.45, "drill": 0.2, "clearance_class": 0.0}
    old = BR.has_fanout
    BR.has_fanout = lambda src: False
    try:
        return PL._route_cmd(a, "in.kicad_pcb", "out.kicad_pcb", only, power_nets=power_nets, R=R, pair=pair)
    finally:
        BR.has_fanout = old


def test_power_nets_flags_only_for_nets_in_batch():
    cmd = _route(["+5V", "SIG"], {"+5V": 1.0, "VBATT": 1.0})
    i = cmd.index("--power-nets")
    assert cmd[i:i + 4] == ["--power-nets", "+5V", "--power-nets-widths", "1"]
    assert cmd[-3:] == ["--nets", "+5V", "SIG"]


def test_no_power_flags_when_no_rail_in_batch_or_pair():
    assert "--power-nets" not in _route(["SIG"], {"+5V": 1.0})
    assert "--power-nets" not in _route(["+5V", "X"], {"+5V": 1.0}, pair=True)
    assert "--power-nets" not in _route(["+5V"], None)


def test_batch_order_puts_rails_first_widest_first():
    assert BR.rails_first(["SIG1", "A", "B", "SIG2"], {"A": 0.7, "B": 1.0}) == ["B", "A", "SIG1", "SIG2"]
    assert BR.rails_first(["SIG1", "A"], None) == ["SIG1", "A"]
