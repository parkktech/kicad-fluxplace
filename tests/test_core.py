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


def test_quad_hub_central():
    """Quadratic placement must land the hub INSIDE its connectors' hull (the whole
    point: force-directed exiles the hub to the periphery, quad must not)."""
    parts, nets = _synthetic()
    cg = G.build(parts, nets)
    topo = T.analyze(cg)
    pos, rot = P.place(parts, cg, topo, strategy="quad", rotate="ortho")
    assert len(pos) == len(parts), "not every part placed"
    assert P.count_overlaps(parts, pos, 0.15, angles=rot) == 0, "quad left overlaps"
    lo_x = min(pos["J1"][0], pos["J2"][0]); hi_x = max(pos["J1"][0], pos["J2"][0])
    assert lo_x < pos["U1"][0] < hi_x, "hub exiled outside the connector hull"
    print("ok  quad: hub central, no overlaps")


def test_router_gate():
    """Global router on the quad layout: everything must route with zero overflow on
    a tiny board, and the score must expose the gate fields."""
    from fluxplace import route as R
    parts, nets = _synthetic()
    cg = G.build(parts, nets)
    topo = T.analyze(cg)
    pos, rot = P.place(parts, cg, topo, strategy="quad", rotate="ortho")
    rep = R.score(parts, pos, cg, angles=rot)
    assert rep["overflow"] == 0, f"tiny board must be routable, overflow={rep['overflow']}"
    assert rep["nets"] >= 3, "expected several routed nets"
    for n, d in rep["detour"].items():
        assert d < 4.0, f"net {n} detours x{d:.1f} — routing is pathological"
    print(f"ok  router: {rep['nets']} nets, overflow 0, wl {rep['wirelength']:.0f} mm")


def test_place_routed_pipeline():
    """The full route-aware pipeline: legal, all parts, and the routability gate."""
    parts, nets = _synthetic()
    cg = G.build(parts, nets)
    topo = T.analyze(cg)
    pos, rot, rep = P.place_routed(parts, cg, topo)
    assert len(pos) == len(parts), "not every part placed"
    assert P.count_overlaps(parts, pos, 0.15, angles=rot) == 0, "pipeline left overlaps"
    assert rep["overflow"] == 0, "pipeline must deliver a routable placement here"
    print("ok  place_routed: legal + routable")


def test_power_traces_and_pairs():
    """graph v2: small-fanout power rails become routed traces (never GND); diff
    pairs are detected by naming convention with the P side as master."""
    from fluxplace.graph import classify, diff_pairs, power_width
    power, signal, ptraces = classify({
        "+28V_IN": ["J1", "F1"], "GND": ["a", "b", "c"],
        "+5V": [chr(97 + i) for i in range(15)], "+3V3_M2": ["U2", "J3"],
        "SIG": ["a", "b"]})
    assert "+28V_IN" in ptraces and "+3V3_M2" in ptraces
    assert "GND" not in ptraces and "+5V" not in ptraces, "planes must stay planes"
    assert power_width("+28V_IN") == 3 and power_width("+3V3_M2") == 2
    pairs = diff_pairs({"PCIE_TX_P_AC": 1, "PCIE_TX_N_AC": 1, "USB_DP": 1, "USB_DM": 1,
                        "LONE_P": 1, "X": 1})
    assert pairs == {"PCIE_TX_N_AC": "PCIE_TX_P_AC", "USB_DM": "USB_DP"}, pairs
    print("ok  power traces + diff pairs classified")


def test_pin_rotation():
    """pin_at must match KiCad's empirical rotation: +90 deg maps (x, y) -> (y, -x)."""
    parts = {"X": {"pins": {"N": (0.95, 0.0)}, "angle0": 0.0}}
    x, y = P.pin_at(parts, "X", "N", 90)
    assert abs(x) < 1e-9 and abs(y + 0.95) < 1e-9, (x, y)
    x, y = P.pin_at(parts, "X", "N", 180)
    assert abs(x + 0.95) < 1e-9 and abs(y) < 1e-9, (x, y)
    # offsets read at a non-zero drawn angle rotate by the DELTA only
    parts["X"]["angle0"] = 90.0
    assert P.pin_at(parts, "X", "N", 90) == (0.95, 0.0)
    print("ok  pin rotation matches KiCad convention")


def test_layer_router_via_and_taper():
    """Layer-aware A* pays for turns; wide-net reservation tapers at terminals."""
    from fluxplace.route import Grid
    g = Grid(0, 0, 40, 40, cell=2.0)
    p = g.astar((0, 0), (8, 6))
    assert p is not None and p[0] == (0, 0) and p[-1] == (8, 6)
    turns = sum(1 for i in range(1, len(p) - 1)
                if (p[i][0] - p[i - 1][0] != p[i + 1][0] - p[i][0]))
    assert turns <= 2, f"free grid should route as an L/Z, got {turns} turns"
    g.reserve(p, 3)
    e_end = Grid._edge(p[0], p[1])
    mid = len(p) // 2
    e_mid = Grid._edge(p[mid], p[mid + 1])
    assert g.usage[e_end] == 1, "terminal edges must taper to width 1"
    assert g.usage[e_mid] == 3, "mid-route edges carry the full width"
    g.release(p, 3)
    assert all(u == 0 for u in g.usage.values()), "release must mirror reserve exactly"
    print("ok  layer router: turns bounded, tapered width symmetric")


def test_determinism():
    """Same input -> byte-identical placement (salted-hash order must not leak)."""
    parts1, nets1 = _synthetic()
    cg1 = G.build(parts1, nets1)
    t1 = T.analyze(cg1)
    a = P.place_routed(parts1, cg1, t1)[0]
    parts2, nets2 = _synthetic()
    cg2 = G.build(parts2, nets2)
    t2 = T.analyze(cg2)
    b = P.place_routed(parts2, cg2, t2)[0]
    assert a == b, "pipeline must be deterministic"
    print("ok  pipeline deterministic")


def test_locked_anchor_respected():
    """A locked part is a hard mechanical anchor: placement must return it EXACTLY at
    its real coord (never at some phantom perimeter spot), and the movable cloud must
    sit in the real board region around it, not off in origin-centered space."""
    parts, nets = _synthetic()
    # weld J1 far out at (200, 0); everything the pipeline does must keep it there.
    parts["J1"]["x"], parts["J1"]["y"] = 200.0, 0.0
    parts["J1"]["locked"] = True
    parts["J1"]["angle0"] = 0.0
    cg = G.build(parts, nets)
    topo = T.analyze(cg)
    # quad alone must pin the locked anchor as an RHS constant
    from fluxplace.quadratic import quad
    qpos = quad(parts, cg, topo)
    assert abs(qpos["J1"][0] - 200.0) < 1e-6 and abs(qpos["J1"][1]) < 1e-6, \
        f"quad moved a locked anchor to {qpos['J1']}"
    # full place() must return it welded, and its net-mate R1/U2 must be pulled toward
    # the anchor region (not stranded near the origin where the strategy 'wanted' J1)
    pos, ang = P.place(parts, cg, topo, strategy="quad")
    assert abs(pos["J1"][0] - 200.0) < 1e-6 and abs(pos["J1"][1]) < 1e-6, \
        f"place moved locked J1 to {pos['J1']}"
    assert pos["R1"][0] > 60.0, f"R1 (on J1's net) should follow the anchor out, got {pos['R1']}"
    print("ok  locked anchor welded + movable cloud follows it")


def test_side_aware_overlap():
    """Opposite-side SMD parts share the 2D projection but never collide; a THT part
    pierces both sides and still collides with anything."""
    parts = {
        "C1": dict(value="x", w=2, h=2, x=0, y=0, side="F", pins={}),
        "C2": dict(value="x", w=2, h=2, x=0, y=0, side="B", pins={}),   # dead-on, other side
        "C3": dict(value="x", w=2, h=2, x=0, y=0, side="F", pins={}),   # dead-on, same side
    }
    # F vs B at the same spot: not an overlap
    assert P.count_overlaps({"C1": parts["C1"], "C2": parts["C2"]},
                            {"C1": (0, 0), "C2": (0, 0)}, 0.0) == 0, "F/B SMD must not collide"
    # F vs F at the same spot: a real overlap
    assert P.count_overlaps({"C1": parts["C1"], "C3": parts["C3"]},
                            {"C1": (0, 0), "C3": (0, 0)}, 0.0) == 1, "same-side must collide"
    # a THT part on the back still collides with a front SMD (drilled field spans both)
    tht = dict(value="x", w=2, h=2, x=0, y=0, side="B", tht=True, pins={})
    assert P.count_overlaps({"C1": parts["C1"], "T1": tht},
                            {"C1": (0, 0), "T1": (0, 0)}, 0.0) == 1, "THT must collide across sides"
    print("ok  side-aware overlap (F/B pass, same-side + THT collide)")


def test_escape_detection_and_ladder():
    """Adaptive escape: cluster unrouted pads by part, flag the fine-pitch bottleneck,
    step the local rule down the ladder, emit valid .kicad_dru."""
    from fluxplace import escape as E
    parts = {
        "U10": dict(w=20, h=20, x=50, y=50),   # the stuck LQFP
        "J20": dict(w=28, h=8, x=50, y=90),    # the stuck mezzanine
        "R1":  dict(w=1, h=1, x=10, y=10),     # one stray unrouted pad — not a zone
    }
    def item(ref):
        return {"items": [{"description": f"Pad 1 [NET] of {ref} on F.Cu"},
                          {"description": "Track [NET] on B.Cu"}]}
    drc = {"unconnected_items": [item("U10")] * 9 + [item("J20")] * 12 + [item("R1")] * 1}
    zones = E.detect_escape_zones(parts, drc, min_unrouted=5)
    refs = [z["ref"] for z in zones]
    assert refs == ["J20", "U10"], f"worst-first fine-pitch zones expected, got {refs}"
    assert "R1" not in refs, "a single stray unrouted pad must not become a zone"
    z0 = zones[0]
    assert z0["bbox"][2] - z0["bbox"][0] > 28, "zone must cover the part + margin"
    # ladder steps down and stops at the floor
    assert E.ladder_step(0.20) == 0.15 and E.ladder_step(0.125) == 0.10
    assert E.ladder_step(0.10) is None, "must stop at the JLCPCB fine-pitch floor"
    dru = E.dru_text(zones, 0.10, 0.10)
    assert "escape_U10" in dru and "escape_J20" in dru and "track_width (min 0.1mm)" in dru
    assert dru.startswith("(version 1)")
    # no zones -> empty (valid) ruleset, never a crash
    assert E.dru_text([], 0.1, 0.1).strip() == "(version 1)"
    print("ok  adaptive escape: detect zones + step-down ladder + dru emit")


def test_escape_net_aware_floor():
    """The step-down floor is per-net, read from the schematic: a signal net may thin to
    the fab floor; a power/current rail keeps its ampacity width and must never be necked."""
    from fluxplace import escape as E, graph as G
    parts, nets = _synthetic()
    cg = G.build(parts, nets)
    # GND / +3V3 are power (kept wide); SIGA is signal (may thin)
    assert E.net_floor_mm(cg, "SIGA") == 0.10, "signal net thins to the fab floor"
    assert E.net_floor_mm(cg, "GND") >= 0.20, "a power rail keeps >= its ampacity width"
    drc = {"unconnected_items": [
        {"items": [{"description": "Pad 1 [SIGA] of U2 on F.Cu"}]},
        {"items": [{"description": "Pad 2 [GND] of U2 on F.Cu"}]}]}
    cls = E.classify_stalled_nets(cg, drc)
    assert "SIGA" in cls["thin"] and "GND" in cls["keep"], cls
    print("ok  escape: net-aware floor (signal thins, rail keeps width)")


def test_channel_cut_and_open():
    """Channel-aware relief: a congestion WALL (overflow concentrated on one straight
    cut) is detected by cut_overflow, and _open_channels shifts everything past the
    cut by the lane width the router is short — locked parts and everything before
    the cut hold position."""
    from fluxplace import route as R
    g = R.Grid(0, 0, 20, 20, cell=2.0, layers=2, pitch=0.35)
    for iy in range(g.ny):                       # wall between columns 3|4
        e = R.Grid._edge((3, iy), (4, iy))
        g.usage[e] = g.cap((3, iy), (4, iy)) + 4.0
    cuts = R.cut_overflow(g)
    assert cuts[0][0] == "v" and cuts[0][1] == 3, cuts[0]
    assert abs(cuts[0][3] - 4.0) < 1e-6, "need_tracks = worst single-edge deficit"

    parts = {f"P{i}": dict(w=2.0, h=2.0) for i in range(4)}
    parts["P3"]["locked"] = True                 # locked holds even past the cut
    pos = {"P0": [2.0, 5.0], "P1": [2.0, 9.0], "P2": [12.0, 5.0], "P3": [12.0, 9.0]}

    class StubR:                                 # score: the wall is fixed after one lane
        cut_overflow = staticmethod(R.cut_overflow)
        @staticmethod
        def score(parts, p, graph, angles):
            return dict(overflow=0.0, grid=g)

    p2, rep2 = P._open_channels(parts, None, {r: list(v) for r, v in pos.items()},
                                {}, 0.4, StubR, dict(overflow=10.0, grid=g))
    assert rep2["overflow"] == 0.0
    lane = min(3.0, max(0.6, 4.0 * 0.35))        # 1.4mm lane from the 4-track deficit
    assert abs(p2["P2"][0] - (12.0 + lane)) < 0.5, "part past the cut rides the shift"
    assert abs(p2["P0"][0] - 2.0) < 0.5, "part before the cut holds"
    assert abs(p2["P3"][0] - 12.0) < 1e-9, "locked part holds its mate coords"
    print("ok  channel: cut_overflow wall detect + lane opening (locked holds)")


def _run_adaptive(drcs, parts, cg, tmpdir):
    """Drive route_adaptive with stubbed router/DRC; returns (fanned, widths, summ)."""
    import os
    import shutil
    import fluxplace.adaptive as AD
    shutil.rmtree(tmpdir, ignore_errors=True)
    os.makedirs(tmpdir)
    placed = os.path.join(tmpdir, "placed.kicad_pcb")
    open(placed, "w").write("board")
    seq = list(drcs)
    real_drc = AD.drc_unrouted
    AD.drc_unrouted = lambda cur, cli: seq.pop(0)
    fanned = []

    def route_fresh(src, outb, fine, log=print):
        open(outb, "w").write("routed")
        return outb

    def fanout(board, outb, ref, nets=None, log=print):
        fanned.append((ref, tuple(nets or ())))
        open(outb, "w").write("fanned")
        return outb
    try:
        _, summ = AD.route_adaptive(placed, tmpdir, route_fresh, cg, parts,
                                    fanout=fanout, log=lambda m: None)
    finally:
        AD.drc_unrouted = real_drc
    return fanned, [r["width"] for r in summ["rounds"]], summ


def test_adaptive_fanout_priority(tmpdir="/tmp/fluxtest_ff"):
    """Residue CONCENTRATED at a fine-pitch part -> fanout gets the router time
    FIRST (no clearance ladder burned); residue SPREAD -> the ladder runs and
    fanout is never called. (dig: two ladder rungs bought 69->69->72 = nothing;
    the fanout rung bought 69->33.)"""
    parts, nets = _synthetic()
    parts["U9"] = dict(w=8, h=8, x=0, y=0, pins={"SIGA": (0, 0)})
    cg = G.build(parts, nets)

    def item(net, ref):
        return {"items": [{"description": f"Pad 1 [{net}] of {ref} on F.Cu"}]}
    ok = {"unconnected_items": []}

    # concentrated: 10 stuck endpoints, all at U9 -> fanout first, then closed
    bad = {"unconnected_items": [item("SIGA", "U9")] * 10}
    fanned, widths, summ = _run_adaptive(
        [(bad, {"SIGA"}), (ok, set()), (ok, set())], parts, cg, tmpdir)
    assert fanned and fanned[0][0] == "U9", f"concentrated residue must fan first: {fanned}"
    assert "SIGA" in fanned[0][1], "fanout must target the stuck net"
    assert widths == [0.2, 0.2], f"no ladder rung may run before fanout: {widths}"
    assert summ["closed"] and summ["diagnosis"].startswith("CLOSED"), summ["diagnosis"]

    # spread: stuck endpoints scattered over many parts (no zone reaches
    # min_unrouted=5) -> ladder steps down, fanout never called
    spread = {"unconnected_items": [item("SIGA", r) for r in
                                    ("U2", "U3", "J1", "C1", "R1")]
              + [item("SIGB", r) for r in ("U3", "J2", "C2")]}
    fanned2, widths2, summ2 = _run_adaptive(
        [(spread, {"SIGA", "SIGB"}), (ok, set()), (ok, set())], parts, cg, tmpdir)
    assert not fanned2, f"spread residue must not trigger fanout: {fanned2}"
    assert 0.15 in widths2, f"spread residue must walk the ladder: {widths2}"
    assert summ2["closed"], summ2
    print("ok  adaptive: fanout-priority on concentrated residue, ladder on spread")


def test_candidate_selection():
    """Population search: fewest unrouted wins; DRC violations break ties; the
    un-jittered base (earliest) breaks the rest."""
    from fluxplace.adaptive import pick_best
    assert pick_best([("c0", 42, 900), ("c1", 33, 1200), ("c2", 40, 100)]) == 1
    assert pick_best([("c0", 33, 900), ("c1", 33, 700)]) == 1, "violations tie-break"
    assert pick_best([("c0", 33, 700), ("c1", 33, 700)]) == 0, "base wins pure ties"
    print("ok  candidates: DRC-best selection (unrouted, violations, base)")


if __name__ == "__main__":
    test_graph_power_split()
    test_hub_and_branches()
    test_placement_no_overlap()
    test_hpwl_improves()
    test_quad_hub_central()
    test_router_gate()
    test_place_routed_pipeline()
    test_power_traces_and_pairs()
    test_pin_rotation()
    test_layer_router_via_and_taper()
    test_determinism()
    test_locked_anchor_respected()
    test_side_aware_overlap()
    test_escape_detection_and_ladder()
    test_escape_net_aware_floor()
    test_channel_cut_and_open()
    test_adaptive_fanout_priority()
    test_candidate_selection()
    print("\nALL CORE TESTS PASSED")
