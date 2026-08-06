"""Core tests — run with plain python3 (no pcbnew needed; that's the point of the layering).
    python3 tests/test_core.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace import graph as G, topology as T, placement as P


def _synthetic():
    """A tiny hub board: U1 hub, two ICs each behind a connector, decaps on power only."""
    parts = {
        "U1": dict(value="HUB", w=20, h=20, x=0, y=0, sheet="cpu", pins={"D1": (10, 0), "D2": (-10, 0)}),
        "U2": dict(value="IC", w=4, h=4, x=30, y=0, sheet="a", pins={"D1": (0, 0), "SIGA": (0, 2)}),
        "U3": dict(value="IC", w=4, h=4, x=-30, y=0, sheet="b", pins={"D2": (0, 0), "SIGB": (0, 2)}),
        "J1": dict(value="CONN", w=6, h=6, x=50, y=0, sheet="a", pins={"SIGA": (0, 0)}),
        "J2": dict(value="CONN", w=6, h=6, x=-50, y=0, sheet="b", pins={"SIGB": (0, 0)}),
        "R1": dict(value="1k", w=1, h=1, x=40, y=0, sheet="a", pins={"SIGA": (0, 0)}),
        "C1": dict(value="100nF", w=1, h=1, x=28, y=4, sheet="a", pins={"+3V3": (0, 0), "GND": (0, 1)}),
        "C2": dict(value="100nF", w=1, h=1, x=-28, y=4, sheet="b", pins={"+3V3": (0, 0), "GND": (0, 1)}),
    }
    nets = {
        "D1": ["U1", "U2"], "D2": ["U1", "U3"],
        "SIGA": ["U2", "R1", "J1"], "SIGB": ["U3", "J2"],
        "+3V3": ["U1", "U2", "U3", "C1", "C2"], "GND": ["U1", "U2", "U3", "C1", "C2", "J1", "J2"],
    }
    return parts, nets


def test_graph_power_split():
    parts, nets = _synthetic()
    cg = G.build(parts, nets)
    assert "+3V3" in cg.power_nets and "GND" in cg.power_nets, "power nets not classified"
    assert "SIGA" in cg.signal_nets and "D1" in cg.signal_nets, "signal nets missing"
    print("ok  power/signal split")


def test_hub_and_branches():
    parts, nets = _synthetic()
    cg = G.build(parts, nets)
    topo = T.analyze(cg)
    assert topo.hub == "U1", f"hub should be U1, got {topo.hub}"
    assert len(topo.branches) >= 2, "expected at least two branches"
    print("ok  hub + branches")


def test_placement_no_overlap():
    parts, nets = _synthetic()
    cg = G.build(parts, nets)
    topo = T.analyze(cg)
    for strat in ("radial", "pack", "flux"):
        pos, rot = P.place(parts, cg, topo, strategy=strat, rotate="ortho", iters=120)
        assert len(pos) == len(parts), "not every part placed"
        ov = P.count_overlaps(parts, pos, 0.15, angles=rot)
        assert ov == 0, f"[{strat}] overlaps after placement: {ov}"
    print("ok  placement covers all parts, no overlaps (all strategies)")


def test_hpwl_improves():
    parts, nets = _synthetic()
    cg = G.build(parts, nets)
    topo = T.analyze(cg)
    before = P.hpwl(parts, cg, {r: (parts[r]["x"], parts[r]["y"]) for r in parts})
    after = P.hpwl(parts, cg, P.flux(parts, cg, topo, iters=200))
    assert after <= before * 1.5, "flux should not blow up wirelength on a hub graph"
    print(f"ok  hpwl before={before:.0f} after={after:.0f}")


if __name__ == "__main__":
    test_graph_power_split()
    test_hub_and_branches()
    test_placement_no_overlap()
    test_hpwl_improves()
    print("\nALL CORE TESTS PASSED")
