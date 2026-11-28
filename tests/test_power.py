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
    from fluxplace import opengate as OG              # pour_rails refills through opengate's helper process; other test modules set
    monkeypatch.setattr(OG, "refill", lambda b, *a, **k: None)   # routecheck.PY to a missing interpreter at import (3 order-dependent failures)


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


# ---- PHASE3G G4: rail rungs (widen in place, vias) -----------------------------------------------------------------
G4_CONS = {"power": {"VBAT": {"max_current_ma": 2000}}}


def _g4_need():
    r = PW.plan(G4_CONS)["VBAT"]
    return PW._need_on(r, "F.Cu")


def _g4_tracks(path, net):
    import pcbnew as P
    b = P.LoadBoard(path)
    return sorted([(P.ToMM(t.GetWidth()), P.ToMM(t.GetStart().x), P.ToMM(t.GetStart().y), P.ToMM(t.GetEnd().x), P.ToMM(t.GetEnd().y))
                   for t in b.GetTracks() if t.GetNetname() == net and t.GetClass() == "PCB_TRACK"])


def test_widen_lone_narrow_rail_track_to_need(tmp_path):
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 6, "VBAT")])
    out = str(tmp_path / "out.kicad_pcb")
    rec = PW.widen(src, G4_CONS, out)
    need = _g4_need()
    assert rec["widened_mm"] == pytest.approx(16.0, abs=0.01) and rec["left_mm"] == 0
    ws = [w for w, *_ in _g4_tracks(out, "VBAT")]
    assert ws and min(ws) == pytest.approx(round(need, 4), abs=1e-3)


def test_widen_beside_foreign_track_takes_the_reduced_width(tmp_path):
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    need = _g4_need()
    d = 0.375 * need + 0.075 + 0.2 + 0.05                     # foreign track: 0.75 need clears it, the full need does not
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 6, "VBAT"), (2, 6 + d, 18, 6 + d, "SIG")])
    out = str(tmp_path / "out.kicad_pcb")
    PW.widen(src, G4_CONS, out)
    w = min(w for w, *_ in _g4_tracks(out, "VBAT"))
    assert 0.5 * need - 1e-3 <= w < need - 1e-6


def test_widen_necks_a_narrow_pad_end_and_widens_only_the_far_part(tmp_path):
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    need = _g4_need()
    src = _board(str(tmp_path / "in.kicad_pcb"), pads=[("U1", 2, 6, "VBAT")], tracks=[(2, 6, 18, 6, "VBAT")])
    out = str(tmp_path / "out.kicad_pcb")
    PW.widen(src, G4_CONS, out)
    trs = _g4_tracks(out, "VBAT")
    pad_piece = [t for t in trs if abs(t[0] - 0.25) < 1e-3]     # the piece at the pad takes the pad's width (0.25 mm, probe ok)
    far_piece = [t for t in trs if abs(t[0] - round(need, 4)) < 1e-3]
    assert len(pad_piece) == 1 and len(far_piece) == 1
    assert abs(pad_piece[0][1] - 2) < 1e-3 and abs(pad_piece[0][3] - (2 + max(need, 0.5))) < 1e-3   # split max(W, 0.5) from the pad
    assert abs(far_piece[0][3] - 18) < 1e-3


def _lock_all(src):
    import pcbnew as P
    b = P.LoadBoard(src)
    for t in b.GetTracks():
        t.SetLocked(True)
    P.SaveBoard(src, b)


def _locked_flags(path, net):
    import pcbnew as P
    return [t.IsLocked() for t in P.LoadBoard(path).GetTracks() if t.GetNetname() == net and t.GetClass() == "PCB_TRACK"]


def test_locked_rail_track_is_not_widened_by_default_protect_empty_keeps_lock(tmp_path):
    """W37: a locked rail track of an unprotected net gets a wider UNLOCKED track over it; the locked original is kept as it was."""
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 6, "VBAT")])
    _lock_all(src)
    out = str(tmp_path / "out.kicad_pcb")
    rec = PW.widen(src, G4_CONS, out)
    need = _g4_need()
    assert rec["widened_mm"] == pytest.approx(16.0, abs=0.01) and rec["skipped"]["locked_mm"] == 0
    assert sorted(round(w, 4) for w, *_ in _g4_tracks(out, "VBAT"))[-1] == round(need, 4)
    assert sorted(_locked_flags(out, "VBAT")) == [False, True]
    assert any(abs(w - _g4_tracks(src, "VBAT")[0][0]) < 1e-6 for w, *_ in _g4_tracks(out, "VBAT"))      # the locked original width is intact
    rec2 = PW.widen(out, G4_CONS, str(tmp_path / "out2.kicad_pcb"))                                     # idempotent: no second overlay
    assert rec2["widened_mm"] == 0


def test_locked_rail_track_of_protected_net_is_skipped_and_counted(tmp_path):
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    from fluxplace import drcguard as DG
    cons = dict(G4_CONS, rf={"nets": ["VBAT"]})                 # RF net: protected_nets keeps its copper locked
    names = ["VBAT", "SIG"]
    assert "VBAT" in DG.protected_nets(cons, names)
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 6, "VBAT")])
    _lock_all(src)
    out = tmp_path / "out.kicad_pcb"
    rec = PW.widen(src, cons, str(out), protect=DG.protected_nets(cons, names))
    assert rec["widened_mm"] == 0 and not out.exists()
    assert rec["skipped"]["locked_mm"] == pytest.approx(16.0, abs=0.01)
    assert rec["skipped"]["by_net"]["VBAT"] == {"locked_mm": pytest.approx(16.0, abs=0.01), "pour_zone_mm": 0.0}


def test_protected_pair_leg_named_net_is_left_alone_when_passed_in_protect(tmp_path):
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    cons = dict(G4_CONS, pairs=[{"p": "VBAT", "n": "VBAT_N"}])
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 6, "VBAT"), (2, 8, 18, 8, "VBAT_N")])
    _lock_all(src)
    out = tmp_path / "out.kicad_pcb"
    rec = PW.widen(src, cons, str(out), protect={"VBAT"})
    assert rec["widened_mm"] == 0 and rec["skipped"]["by_net"]["VBAT"]["locked_mm"] > 0


def test_necked_locked_rail_track_stays_whole_and_the_far_part_is_overlaid_unlocked(tmp_path):
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    need = _g4_need()
    src = _board(str(tmp_path / "in.kicad_pcb"), pads=[("U1", 2, 6, "VBAT")], tracks=[(2, 6, 18, 6, "VBAT")])
    _lock_all(src)
    out = str(tmp_path / "out.kicad_pcb")
    PW.widen(src, G4_CONS, out)
    assert sorted(_locked_flags(out, "VBAT")) == [False, True]
    assert len(_g4_tracks(out, "VBAT")) == 2 and any(abs(t[0] - round(need, 4)) < 1e-3 for t in _g4_tracks(out, "VBAT"))


def test_copper_inside_a_same_net_pour_is_widened_too(tmp_path, monkeypatch):
    """W34: check() (D71da) gives a zone no carve-out, so the rung must not skip the narrow copper inside one (it refused every rail
    that had a zone: ledger "route gate (W16 route-4)")."""
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    from fluxplace import gaps as GP
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 6, "VBAT")])
    rail = dict(PW.plan(G4_CONS)["VBAT"], pour=True)
    monkeypatch.setattr(PW, "_rails_on", lambda board, cons, nets=None: {"VBAT": rail})
    monkeypatch.setattr(GP, "_zone_nets", lambda board: {"VBAT"})
    out = tmp_path / "out.kicad_pcb"
    rec = PW.widen(src, G4_CONS, str(out))
    assert rec["widened_mm"] == pytest.approx(16.0, abs=0.01) and out.exists()
    assert rec["skipped"]["pour_zone_mm"] == 0

def test_add_vias_places_the_missing_via_beside_the_existing_one(tmp_path, monkeypatch):
    from fluxplace import handroute as HR, routediag as RD
    src = tmp_path / "in.kicad_pcb"
    src.write_text("(kicad_pcb)\n")
    out = str(tmp_path / "out.kicad_pcb")
    monkeypatch.setattr(PW, "_rails_on", lambda board, cons, nets=None: {"VBAT": {"vias_needed": 2}})
    monkeypatch.setattr(RD, "load", lambda p: {"vias": [{"net": "VBAT", "x": 10.0, "y": 6.0}], "copper": ["F.Cu", "B.Cu"]})
    monkeypatch.setattr(HR, "window", lambda board, at=None, **k: {"via_sites": {"sites": [[30.0, 1.0, 0.5], [12.0, 8.0, 0.1]]}})
    laid = []

    def lay(board, net, path, out=None, constraints=None, **k):
        laid.append((net, path))
        if path.startswith("via 30"):                    # the best site is refused by its probe: the next one is tried
            return {"accepted": False, "reason": "probe failed"}
        open(out, "w").write("(kicad_pcb)\n")
        return {"accepted": True}
    monkeypatch.setattr(HR, "lay", lay)
    rec = PW.add_vias(str(src), G4_CONS, out)
    assert rec["added"] == {"VBAT": 1} and rec["refused"] == {"VBAT": 1}
    assert [p for _n, p in laid] == ["via 30.000,1.000", "via 12.000,8.000"]
    assert os.path.exists(out)


def test_pour_rerun_on_a_board_with_rail_zones_does_not_touch_decayed_bindings_and_nets_scopes_it(monkeypatch, tmp_path):
    """W15 close: `railpour` died with "'SwigPyObject' object has no attribute 'GetNetname'" on every board that already had rail zones
    (board.Remove, then Zones()).  The old zones are stripped in a fresh process; the real zone-net read then sees the rail as unpoured.
    `nets` (the rung's gap nets) limits the rails: a rail not named costs nothing and writes nothing."""
    pcbnew = pytest.importorskip("pcbnew")
    _stub_dg(monkeypatch, CLEAN, CLEAN)
    src = _rail_board(str(tmp_path / "in.kicad_pcb"), zone_net="GND")
    out = str(tmp_path / "out.kicad_pcb")
    assert PW.pour_rails(src, CONS_POUR, out, layers=4)["added"] == ["VBAT/F.Cu"]
    removed = []
    real = pcbnew.BOARD.Remove
    monkeypatch.setattr(pcbnew.BOARD, "Remove", lambda self, it: removed.append(it) or real(self, it))
    _stub_dg(monkeypatch, CLEAN, CLEAN)
    out2 = str(tmp_path / "out2.kicad_pcb")
    rec = PW.pour_rails(out, CONS_POUR, out2, layers=4)
    assert rec["added"] == ["VBAT/F.Cu"] and removed == []           # nothing removed in this process
    named = [z for z in pcbnew.LoadBoard(out2).Zones() if (z.GetZoneName() or "").startswith("fluxplace-rail:")]
    assert len(named) == 1
    assert not [f for f in os.listdir(tmp_path) if ".strip." in f]
    rec = PW.pour_rails(out, CONS_POUR, str(tmp_path / "out3.kicad_pcb"), layers=4, nets=["+3V3"])
    assert rec["added"] == [] and rec["before"] is None and not (tmp_path / "out3.kicad_pcb").exists()


def test_widened_locked_rail_stub_passes_the_protection_check_and_a_pair_change_is_refused(tmp_path):
    """W37 (ledger "route (W16 finish-2, 1f163d6)"): the widen added copper over the locked stub, it did not change it; the manifest
    commit check accepts that and still refuses a modified protected item."""
    pytest.importorskip("pcbnew")
    import pcbnew as P
    from test_routerules_syn import _board
    from fluxplace import boardwrite as BW
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 6, "VBAT"), (2, 9, 18, 9, "SIG")])
    _lock_all(src)
    man = BW.seed(src)
    out = str(tmp_path / "out.kicad_pcb")
    PW.widen(src, G4_CONS, out)
    assert BW.verify(src, out, manifest=man)["ok"]
    b = P.LoadBoard(out)                                                    # a modified protected (locked) item is refused
    for t in b.GetTracks():
        if t.GetNetname() == "SIG":
            t.SetWidth(t.GetWidth() + 100000)
    bad = str(tmp_path / "bad.kicad_pcb")
    P.SaveBoard(bad, b)
    assert BW.verify(src, bad, manifest=man)["changed"] == ["SIG"]


# ---- W42: widen to each run's own need; power_mag weighted by shortfall -----------------------------------------------
def test_widen_targets_the_runs_own_solved_need_not_the_rail_need(tmp_path, monkeypatch):
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 6, "VBAT")])
    need = _g4_need()
    run = {"layers": ["F.Cu"], "len": 16.0, "min_w": 0.15, "need": need, "n": 1, "members": [0], "layer": "F.Cu"}
    seg = {"layer": "F.Cu", "x1": 2.0, "y1": 6.0, "x2": 18.0, "y2": 6.0, "width": 0.15, "need": need, "len": 16.0}
    real = PW.check

    def fake(facts, cons, dc=None, board=None, detail=None):
        detail["VBAT"] = [{"run": run, "segs": [seg], "need_run": 0.32, "amps": 0.3, "cleared": False}]
        return []
    monkeypatch.setattr(PW, "check", fake)
    out = str(tmp_path / "out.kicad_pcb")
    PW.widen(src, G4_CONS, out)
    monkeypatch.setattr(PW, "check", real)
    ws = [w for w, *_ in _g4_tracks(out, "VBAT")]
    assert ws and min(ws) == pytest.approx(0.35, abs=1e-3) and need > 0.7       # ceil05(0.32), not the rail's need


def test_widen_leaves_a_run_that_is_already_wide_enough_for_its_own_current(tmp_path, monkeypatch):
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 6, "VBAT")])
    need = _g4_need()
    run = {"layers": ["F.Cu"], "len": 16.0, "min_w": 0.15, "need": need, "n": 1, "members": [0], "layer": "F.Cu"}
    seg = {"layer": "F.Cu", "x1": 2.0, "y1": 6.0, "x2": 18.0, "y2": 6.0, "width": 0.15, "need": need, "len": 16.0}
    monkeypatch.setattr(PW, "check", lambda f, c, dc=None, board=None, detail=None: detail.update(
        VBAT=[{"run": run, "segs": [seg], "need_run": 0.15, "amps": 0.05, "cleared": True}]) or [])
    out = str(tmp_path / "out.kicad_pcb")
    assert PW.widen(src, G4_CONS, out)["widened_mm"] == 0 and not os.path.exists(out)


def test_power_width_value_is_a_shortfall_so_a_partial_widen_scores_lower():
    from test_power_neck import seg, cons, STACK
    def val(w):
        f = {"copper_layers": 2, "stackup": STACK, "track_geom": [seg(0, 0, 30, 0, w)]}
        return [x for x in PW.check(f, cons(2.0, False)) if x["code"] == "POWER_WIDTH"][0]["value"]
    a, b, c = val(0.15), val(0.4), val(0.8)
    assert a > b > 0 and a < 30.0 + 1e-9 and 0 < b < a
    assert c == 0 or c < b


def _dups(path):
    from fluxplace import dedupe
    return len(dedupe.duplicates(open(path, errors="surrogateescape").read()))


def test_widen_never_stacks_an_exact_duplicate_under_a_wide_twin(tmp_path):
    """W68: two unlocked narrow tracks with the same ends (the second reversed): widening both made two identical wide tracks
    (DUPLICATE_COPPER at the route gate's close commit). The second is now removed as covered by the first."""
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 6, "VBAT"), (18, 6, 2, 6, "VBAT")])
    out = str(tmp_path / "out.kicad_pcb")
    PW.widen(src, G4_CONS, out)
    assert _dups(out) == 0 and len(_g4_tracks(out, "VBAT")) == 1, _g4_tracks(out, "VBAT")


def test_widen_widens_a_stub_at_an_odd_angle_the_angle_finding_is_not_a_width_finding(tmp_path):
    """K14 (route-9 VBATT_PROT/VMODEM): a router pad stub at 15 deg carries angle_not_45_90 at every width; widening must still pass."""
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 9.2, "VBAT")])
    out = str(tmp_path / "out.kicad_pcb")
    rec = PW.widen(src, G4_CONS, out)
    ws = [w for w, *_ in _g4_tracks(out, "VBAT")]
    assert rec["widened_mm"] > 0 and ws and min(ws) >= 0.5 * _g4_need() - 1e-3


def test_widen_repeats_until_the_per_run_targets_stop_moving(monkeypatch, tmp_path):
    pytest.importorskip("pcbnew")
    calls = []

    def once(board, cons, out, nets=None, protect=()):
        calls.append(board)
        open(out, "w").write("x")
        return {"widened_mm": 1.0 if len(calls) < 3 else 0.0, "left_mm": 0, "by_net": {}, "necks": [], "unreached": {}}
    monkeypatch.setattr(PW, "_widen_once", once)
    out = str(tmp_path / "o.kicad_pcb")
    rec = PW.widen("in", {}, out)
    assert calls == ["in", out, out], calls
    assert rec["passes"] == 2 and rec["widened_mm"] == 2.0, rec


# ---- ledger 2026-11-28 (W16 VBATT_IN): widths between the ladder rungs, displaced neighbours ----------------------------------
def test_widest_between_finds_the_grid_width_under_the_refused_rung():
    assert PW._widest_between(lambda w: w <= 1.37, 1.0, 1.3825) == pytest.approx(1.35)
    assert PW._widest_between(lambda w: False, 1.0, 1.3825) is None
    assert PW._widest_between(lambda w: True, 1.0, 1.04) is None          # no grid width strictly between


def test_widen_takes_a_width_between_the_rungs_when_half_the_need_does_not_fit(tmp_path):
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    need = _g4_need()
    d = 0.2 * need + 0.075 + 0.2                             # a foreign track that leaves room for 0.4 need: under the 0.5 rung
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 6, "VBAT"), (2, 6 + d, 18, 6 + d, "SIG")])
    out = str(tmp_path / "out.kicad_pcb")
    PW.widen(src, G4_CONS, out)
    w = min(w for w, *_ in _g4_tracks(out, "VBAT"))
    assert 0.15 + 1e-6 < 0.4 * need - 0.06 <= w <= 0.4 * need + 1e-6


def test_displace_removes_only_unlocked_foreign_copper_that_blocks_the_rail(tmp_path):
    pytest.importorskip("pcbnew")
    import pcbnew as P
    from test_routerules_syn import _board
    need = _g4_need()
    d = 0.2 * need + 0.075 + 0.2
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 6, "VBAT"), (2, 6 + d, 18, 6 + d, "SIG"), (2, 11, 18, 11, "FAR")])
    out = str(tmp_path / "out.kicad_pcb")
    rec = PW.displace(src, G4_CONS, out, nets=["VBAT"])
    assert rec["ripped"] == {"SIG": 1} and rec["segs"] == 1
    assert not _g4_tracks(out, "SIG") and _g4_tracks(out, "FAR") and _g4_tracks(out, "VBAT")
    PW.widen(out, G4_CONS, str(tmp_path / "w.kicad_pcb"))
    assert min(w for w, *_ in _g4_tracks(str(tmp_path / "w.kicad_pcb"), "VBAT")) == pytest.approx(round(need, 4), abs=1e-3)
    out2 = str(tmp_path / "out2.kicad_pcb")                  # a net the caller keeps (protected / rail / zone net) is never removed
    assert PW.displace(src, G4_CONS, out2, nets=["VBAT"], keep={"SIG"})["ripped"] == {} and not os.path.exists(out2)
    b = P.LoadBoard(src)                                     # nor is locked copper
    [t.SetLocked(True) for t in b.GetTracks() if t.GetNetname() == "SIG"]
    lk = str(tmp_path / "lk.kicad_pcb"); P.SaveBoard(lk, b)
    import shutil; shutil.copy(src[:-10] + ".kicad_pro", lk[:-10] + ".kicad_pro")
    out3 = str(tmp_path / "out3.kicad_pcb")
    assert PW.displace(lk, G4_CONS, out3, nets=["VBAT"])["ripped"] == {} and not os.path.exists(out3)


def test_widen_splits_a_long_track_at_a_blocker_near_one_end(tmp_path):
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    need = _g4_need()
    d = 0.2 * need + 0.075 + 0.2                             # a short foreign track beside the first millimetre only
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 6, "VBAT"), (2, 6 + d, 3, 6 + d, "SIG")])
    out = str(tmp_path / "out.kicad_pcb")
    PW.widen(src, G4_CONS, out)
    trs = _g4_tracks(out, "VBAT")
    wide = [t for t in trs if abs(t[0] - round(need, 4)) < 1e-3]
    assert len(trs) >= 2 and len(wide) == 1 and abs(wide[0][3] - wide[0][1]) >= 8.0      # at least half the track at the full width
    assert min(t[0] for t in trs) < need - 1e-6
    assert PW._clear_span([{"at": [6.0, 6.0]}, {"at": [12.0, 6.0]}], (2, 6), (18, 6), need) is None     # blocked along its length
    assert PW._clear_span([{"at": [3.0, 6.0]}, {"kind": "angle_not_45_90"}], (2, 6), (18, 6), need) is None
    d0, d1 = PW._clear_span([{"at": [2.5, 6.0]}, {"at": [17.5, 6.0]}], (2, 6), (18, 6), need)          # a blocker at each end: the middle
    assert 0.5 < d0 < 3 and 13 < d1 < 15.5


def test_widen_gives_a_track_shorter_than_the_pad_neck_the_pad_width(tmp_path):
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    src = _board(str(tmp_path / "in.kicad_pcb"), pads=[("U1", 2, 6, "VBAT")], tracks=[(2, 6, 2.7, 6, "VBAT")])
    out = str(tmp_path / "out.kicad_pcb")
    PW.widen(src, G4_CONS, out)
    assert [t[0] for t in _g4_tracks(out, "VBAT")] == [pytest.approx(0.25)]              # pad 0.25 x 0.25: the stub takes 0.25, not 0.15
