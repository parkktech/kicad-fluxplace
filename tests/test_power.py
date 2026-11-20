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


# --- rail corridor pour (D71cj D, PHASE3B items 2-4) ---
import pytest  # noqa: E402


def test_rail_priorities_above_existing_distinct_largest_highest():
    p = PW.rail_priorities([0, 1, 3], ["VBAT", "V5"])          # nets in descending current
    assert p == {"VBAT": 5, "V5": 4} and min(p.values()) > 3
    assert PW.rail_priorities([], ["A"]) == {"A": 1}


def test_rail_outline_two_segments_one_polygon():
    rings = PW.rail_outline([(0, 0, 5, 0, 1.0), (5, 0, 5, 4, 1.0)], over=2.0)
    assert len(rings) == 1                                       # joined at (5, 0): one union, not two rectangles
    xs = [x for x, _y in rings[0]]
    ys = [y for _x, y in rings[0]]
    assert min(xs) == pytest.approx(-2.5, abs=2e-3) and max(xs) == pytest.approx(7.5, abs=2e-3)   # round caps: pcbnew arc error 1 um
    assert min(ys) == pytest.approx(-2.5, abs=2e-3) and max(ys) == pytest.approx(6.5, abs=2e-3)


def test_rail_outline_disjoint_is_two_rings():
    assert len(PW.rail_outline([(0, 0, 1, 0, 0.2), (10, 0, 11, 0, 0.2)], over=0.5)) == 2
    assert PW.rail_outline([], over=1.0) == []


def test_pour_needs_no_shapely_and_missing_board_is_not_a_crash(monkeypatch, tmp_path):
    pytest.importorskip("pcbnew")
    import sys
    monkeypatch.setitem(sys.modules, "shapely", None)           # PHASE3C E: the outline is pcbnew's, shapely is not imported
    cons = {"power": {"VBAT": {"max_current_ma": 2000, "max_v": 4.2, "pour": True}}}
    with pytest.raises(Exception):                              # the board is missing: an error, not a skip for shapely
        PW.pour_rails(str(tmp_path / "missing.kicad_pcb"), cons, str(tmp_path / "out.kicad_pcb"))


def _rail_board(path, zone_net=None):
    """VBAT spine on F.Cu (4 mm from the outline edge), optional existing zone of `zone_net` at priority 2."""
    from test_routerules_syn import _board
    import pcbnew as P
    _board(path, tracks=[(2, 6, 18, 6, "VBAT")])
    if zone_net:
        b = P.LoadBoard(path)
        b.Add(P.NETINFO_ITEM(b, zone_net))
        z = P.ZONE(b)
        z.SetLayer(P.F_Cu)
        z.SetNetCode(b.FindNet(zone_net).GetNetCode())
        z.SetAssignedPriority(2)
        poly = P.SHAPE_POLY_SET()
        poly.NewOutline()
        for x, y in ((1, 1), (19, 1), (19, 11), (1, 11)):
            poly.Append(P.FromMM(x), P.FromMM(y))
        z.SetOutline(poly)
        b.Add(z)
        b.Save(path)
    return path


def _stub_dg(monkeypatch, before, after):
    from fluxplace import drcguard as DG, planes as PL
    seq = iter([before, after])
    monkeypatch.setattr(DG, "measure", lambda p, **k: dict(next(seq)))
    monkeypatch.setattr(PL, "refill", lambda b: None)


CLEAN = {"opens": 0, "shorts": 0, "errors": 0}
CONS_POUR = {"power": {"VBAT": {"max_current_ma": 2000, "max_v": 4.2, "pour": True}}}


def test_pour_writes_named_zone_and_keeps_when_guard_holds(monkeypatch, tmp_path):
    pcbnew = pytest.importorskip("pcbnew")
    from fluxplace import gaps as GP
    monkeypatch.setattr(GP, "_zone_nets", lambda p: set())
    _stub_dg(monkeypatch, CLEAN, CLEAN)
    src = _rail_board(str(tmp_path / "in.kicad_pcb"))
    out = str(tmp_path / "out.kicad_pcb")
    rec = PW.pour_rails(src, CONS_POUR, out, layers=4)
    assert rec["added"] == ["VBAT/F.Cu"] and rec["kept"] == rec["added"] and not rec["dropped"]
    z = [z for z in pcbnew.LoadBoard(out).Zones() if z.GetZoneName() == "fluxplace-rail:VBAT"]
    assert len(z) == 1 and z[0].GetAssignedPriority() == 1       # no existing zone: base 0 + 1


def test_pour_skips_rail_with_existing_zone(monkeypatch, tmp_path):
    pytest.importorskip("pcbnew")
    from fluxplace import gaps as GP
    monkeypatch.setattr(GP, "_zone_nets", lambda p: {"VBAT"})
    _stub_dg(monkeypatch, CLEAN, CLEAN)
    src = _rail_board(str(tmp_path / "in.kicad_pcb"), zone_net="VBAT")
    rec = PW.pour_rails(src, CONS_POUR, str(tmp_path / "out.kicad_pcb"), layers=4)
    assert rec["added"] == [] and rec["skipped"] == {"VBAT": "board already has a zone of this net"}


def test_pour_priority_above_existing_and_rerun_removes_named(monkeypatch, tmp_path):
    pcbnew = pytest.importorskip("pcbnew")
    from fluxplace import gaps as GP
    monkeypatch.setattr(GP, "_zone_nets", lambda p: set())
    _stub_dg(monkeypatch, CLEAN, CLEAN)
    src = _rail_board(str(tmp_path / "in.kicad_pcb"), zone_net="GND")
    out = str(tmp_path / "out.kicad_pcb")
    PW.pour_rails(src, CONS_POUR, out, layers=4)
    _stub_dg(monkeypatch, CLEAN, CLEAN)
    rec = PW.pour_rails(out, CONS_POUR, str(tmp_path / "out2.kicad_pcb"), layers=4)   # re-run on its own output
    zs = pcbnew.LoadBoard(str(tmp_path / "out2.kicad_pcb")).Zones()
    named = [z for z in zs if (z.GetZoneName() or "").startswith("fluxplace-rail:")]
    assert len(named) == 1 and rec["added"] == ["VBAT/F.Cu"]
    gnd = [z for z in zs if z.GetNetname() == "GND"]
    assert gnd and named[0].GetAssignedPriority() > max(z.GetAssignedPriority() for z in gnd)


def test_pour_guard_drops_when_shorts_rise(monkeypatch, tmp_path):
    pytest.importorskip("pcbnew")
    from fluxplace import gaps as GP
    monkeypatch.setattr(GP, "_zone_nets", lambda p: set())
    _stub_dg(monkeypatch, CLEAN, dict(CLEAN, shorts=1))
    src = _rail_board(str(tmp_path / "in.kicad_pcb"))
    out = str(tmp_path / "out.kicad_pcb")
    rec = PW.pour_rails(src, CONS_POUR, out, layers=4)
    assert rec["dropped"] and rec["added"] == [] and "shorts 0 -> 1" in rec["dropped_reason"]
    assert not (tmp_path / "out.kicad_pcb").exists()


def test_route_widths_ipc_outer_and_rail_layers():
    from fluxplace import power
    cons = {"power": {"V5A": {"max_current_ma": 5000, "pour": True}}, "route": {"power_spine": "ipc-outer"}}
    w = power.route_widths(cons, layers=6, track_mm=0.15)["V5A"]
    r = power.rail(cons, "V5A", layers=6)
    assert w >= r["width_outer"] and w < r["width_outer"] + 0.051 and w > power.SPINE_MM
    assert power.rail_layers(cons, ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"]) == ["F.Cu", "B.Cu"]
    assert power.rail_layers({}, ["F.Cu", "In2.Cu"]) == ["F.Cu", "In2.Cu"]


def test_pour_guard_one_zone_at_a_time(monkeypatch):
    """PHASE3E 2: a zone that adds a track open is dropped; a zone that adds only a zone-island open is kept."""
    from fluxplace import power as P
    base = {"opens": 0, "shorts": 0, "errors": 0, "track_opens": 0}
    added, dropped = [], []

    def trial(z):
        added.append(z)
        return {"A": dict(base, track_opens=1, opens=1), "B": dict(base, opens=1)}[z]

    kept, why, cur = P.pour_guard([("A/F.Cu", "A"), ("B/B.Cu", "B")], base, trial, dropped.append)
    assert kept == ["B/B.Cu"] and [lab for lab, _ in why] == ["A/F.Cu"] and "track_opens 0 -> 1" in why[0][1]
    assert dropped == ["A"] and added == ["A", "B"]
    assert cur["opens"] == 1 and cur["track_opens"] == 0


def test_pour_guard_measure_splits_zone_islands():
    from fluxplace import power as P, drcguard as DG
    d = {"unconnected_items": [
        {"items": [{"description": "Zone [VBAT] on F.Cu"}]},
        {"items": [{"description": "Track [VBAT]"}]}]}
    m = P._guard_measure({"opens": 2, "shorts": 0, "errors": 0, "drc": d}, DG)
    assert m["track_opens"] == 1 and m["opens"] == 2
