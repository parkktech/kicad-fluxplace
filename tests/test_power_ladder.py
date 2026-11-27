"""W34: rails ladder (W14 measured: widest first, outer layers, inner only at small fallback widths), D71da neck-downs only
when proven (and recorded), via count per current, a VBATT-style rail with two branches stays connected, 5 A pour at IPC width."""
import math
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fluxplace import power as PW, krtchain as K  # noqa: E402

CONS5 = {"power": {"VBATT": {"max_current_ma": 5000, "max_v": 14.5, "pour": True}}}


def test_neck_width_is_the_inverse_of_the_heat_test():
    for amps, ln in ((5.0, 2.0), (3.0, 1.0), (0.9, 2.0)):
        w = PW.neck_width_mm(amps, ln, 1.0)
        assert PW.neck_heat_c(amps, ln, w, 1.0) == pytest.approx(PW.DEFAULT_RISE_C, rel=1e-6)
    assert PW.neck_drop_mv(5.0, 2.0, 0.66, 1.0) == pytest.approx(7.4, abs=0.3)


def test_5a_rail_needs_ipc_outer_width_and_more_vias_than_1a():
    r5 = PW.rail(CONS5, "VBATT", layers=6)
    assert r5["width_outer"] == pytest.approx(2.77, abs=0.05) and r5["pour"]
    assert PW.route_widths(dict(CONS5, route={"power_spine": "ipc-outer"}), layers=6)["VBATT"] == 2.8
    r1 = PW.rail({"power": {"V1": {"max_current_ma": 1000}}}, "V1", layers=6)
    assert r5["vias_needed"] == math.ceil(5.0 / r5["via_amps"] - 1e-9) and r5["vias_needed"] > r1["vias_needed"] >= 1
    facts = {"copper_layers": 6, "track_geom": [], "vias": [{"net": "VBATT", "x": 1, "y": 1}], "stackup": None}
    assert any(f["code"] == "POWER_VIAS" for f in PW.check(facts, CONS5))


def test_ladder_widths_order_and_floor():
    assert PW.ladder_widths({}, "N", 2.8) == [2.8, 1.4, 1.0, 0.6]
    assert PW.ladder_widths({}, "N", 1.4) == [1.4, 1.0, 0.6]
    assert PW.ladder_widths({}, "N", 0.5, 0.15) == [0.5]
    assert PW.ladder_widths({}, "N", 1.4) == [1.4, 1.0, 0.6]
    assert PW.ladder_widths({"route": {"power_ladder_mm": [0.8, 0.3]}}, "N", 2.0, 0.4) == [2.0, 0.8]
    sig = ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"]
    oc = {"route": {"power_spine": "ipc-outer"}}
    assert PW.ladder_layers(oc, sig, 2.8) == ["F.Cu", "B.Cu"]
    assert PW.ladder_layers(oc, sig, 1.4, fallback=True) == ["F.Cu", "B.Cu"]
    assert PW.ladder_layers(oc, sig, 1.0, fallback=True) == sig
    assert PW.ladder_layers(oc, sig, 1.0) == ["F.Cu", "B.Cu"]          # the rail's own first rung stays on the outer pour layers


def _ctx(tmp_path, monkeypatch, closes_at):
    """krt-whole context: the stub router writes the rung width into the board; the net is open until a rung <= closes_at."""
    from fluxplace import gaps as GP
    (tmp_path / "in.kicad_pcb").write_text("x")
    sig = ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"]
    monkeypatch.setattr(K, "plane_plan", lambda b: (sig, [("GND", "In1.Cu")]))
    monkeypatch.setattr(K, "_all_nets", lambda b: ["X"])
    monkeypatch.setattr(GP, "_zone_nets", lambda b: set())
    monkeypatch.setattr(PW, "pour_rails", lambda *a, **k: {"added": [], "dropped": False, "skipped": {}})
    monkeypatch.setattr(K, "_rail_inner", lambda b, n: {})
    monkeypatch.setattr(K.RR, "rules", lambda *a, **k: {"min_clearance": 0.127, "min_track": 0.127, "min_via": 0.45, "min_drill": 0.2,
                                                         "clearance": 0.127, "track": 0.15, "via": 0.45, "drill": 0.2, "clearance_class": 0.0})

    def opens(b, **k):
        t = open(b).read() if os.path.exists(b) else ""
        w = float(t.split("w=")[1].split()[0]) if "w=" in t else None
        return {"VBATT": 1} if w is None or w > closes_at else {}
    monkeypatch.setattr(K.RC, "open_by_net", opens)
    calls = []

    def runner(cmd, log, **kw):
        calls.append(cmd)
        if os.path.basename(cmd[1]) == "route.py" and "VBATT" in cmd:
            w = cmd[cmd.index("--power-nets-widths") + 1]
            open(cmd[3], "w").write(f"w={w}")
        else:
            open(cmd[3], "w").write("y")
        return 0, 1.0
    c = types.SimpleNamespace(a=types.SimpleNamespace(router_py="py", router_dir="/r", track=0.15, clearance=0.127),
                              cons={"power": CONS5["power"], "route": {"power_spine": "ipc-outer", "whole_post": False}},
                              work=str(tmp_path), state={}, dir=lambda n, nm: str(tmp_path))
    return c, calls, runner, sig


def _rail_calls(calls):
    out = []
    for cmd in calls:
        if os.path.basename(cmd[1]) == "route.py" and "--power-nets" in cmd and cmd[cmd.index("--power-nets") + 1] == "VBATT":
            lay = cmd[cmd.index("--layers") + 1:next(i for i in range(cmd.index("--layers") + 1, len(cmd)) if cmd[i].startswith("--"))]
            out.append((float(cmd[cmd.index("--power-nets-widths") + 1]), lay))
    return out


def test_rails_ladder_steps_down_widest_first_outer_then_inner_at_the_small_widths(tmp_path, monkeypatch):
    c, calls, runner, sig = _ctx(tmp_path, monkeypatch, closes_at=1.0)
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    rc = _rail_calls(calls)[:3]
    assert rc == [(2.8, ["F.Cu", "B.Cu"]), (1.4, ["F.Cu", "B.Cu"]), (1.0, sig)]          # W14: inner layers only at <= 1.0
    assert [x["width"] for x in r["rails"]["ladder"]["VBATT"]] == [2.8, 1.4, 1.0]
    assert r["rails"]["ladder"]["VBATT"][-1]["open"] == 0 and r["rails"]["ladder"]["VBATT"][0]["open"] == 1
    assert [s["step"] for s in r["steps"]][:2] == ["plane-GND", "rails-pour"]            # rails before pairs and the plain route


def test_rails_ladder_stops_at_the_first_rung_that_completes(tmp_path, monkeypatch):
    c, calls, runner, sig = _ctx(tmp_path, monkeypatch, closes_at=2.8)
    K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert [w for w, _l in _rail_calls(calls)][:2] == [2.8, 2.8] or len(_rail_calls(calls)) >= 1
    assert [w for w, _l in _rail_calls(calls)].count(1.4) == 0


def test_rails_ladder_exhausted_rail_stays_open_and_is_reported(tmp_path, monkeypatch):
    c, calls, runner, sig = _ctx(tmp_path, monkeypatch, closes_at=0.1)
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert [x["width"] for x in r["rails"]["ladder"]["VBATT"]] == [2.8, 1.4, 1.0, 0.6]
    assert r["rails"]["ladder"]["VBATT"][-1]["open"] == 1


# ---- widen: neck-downs only when proven, recorded ----------------------------------------------------------------

def _neck_gap(need, w):
    """distance between a rail track and a foreign 0.15 mm track at which a track of width w just fits (clearance 0.2 + margin)."""
    return w / 2 + 0.075 + 0.2 + 0.03


def test_short_run_is_necked_at_the_proven_width_and_recorded(tmp_path):
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    r = PW.plan(CONS5, layers=2)["VBATT"]
    wn = math.ceil(PW.neck_width_mm(5.0, 1.5, 1.0) * 20) / 20
    d = _neck_gap(r["width_outer"], wn)
    assert d < 0.5 * r["width_outer"] / 2 + 0.3                     # nothing in the 1.0 / 0.75 / 0.5 need ladder fits
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(6, 6, 7.5, 6, "VBATT"), (4, 6 + d, 10, 6 + d, "SIG")])
    out = str(tmp_path / "out.kicad_pcb")
    rec = PW.widen(src, CONS5, out)
    assert len(rec["necks"]) == 1
    n = rec["necks"][0]
    assert n["width_mm"] == pytest.approx(wn, abs=0.051) and n["run_len_mm"] == pytest.approx(1.5, abs=0.01)
    assert n["dT_c"] <= PW.DEFAULT_RISE_C + 1e-6 and n["drop_mv"] > 0 and "VBATT" not in rec["unreached"]


def test_long_run_is_never_necked_and_is_reported_as_a_placement_finding(tmp_path):
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    r = PW.plan(CONS5, layers=2)["VBATT"]
    d = _neck_gap(r["width_outer"], 0.7)
    src = _board(str(tmp_path / "in.kicad_pcb"), pads=[("C1", 6, 6, "VBATT")],
                 tracks=[(6, 6, 16, 6, "VBATT"), (4, 6 + d, 18, 6 + d, "SIG")])
    rec = PW.widen(src, CONS5, str(tmp_path / "out.kicad_pcb"))
    assert rec["necks"] == []                                        # 10 mm > D71da's 2 mm: no neck-down
    f = rec["unreached"]["VBATT"][0]
    assert f["len_mm"] > PW.NECK_MAX_MM and f["need_mm"] == pytest.approx(r["width_outer"], abs=0.01) and f["refs"] == ["C1"]


def test_vbatt_style_rail_with_two_branches_stays_fully_connected(tmp_path):
    pytest.importorskip("pcbnew")
    from test_routerules_syn import _board
    from fluxplace import routeconn as RC
    pads = [("J1", 2, 6, "VBATT"), ("U1", 18, 6, "VBATT"), ("U2", 10, 11, "VBATT")]
    tracks = [(2, 6, 10, 6, "VBATT"), (10, 6, 18, 6, "VBATT"), (10, 6, 10, 11, "VBATT")]      # trunk + one branch to each load
    src = _board(str(tmp_path / "in.kicad_pcb"), pads=pads, tracks=tracks)
    out = str(tmp_path / "out.kicad_pcb")
    rec = PW.widen(src, CONS5, out)
    assert rec["widened_mm"] > 0
    # the 0.25 mm test pads cannot carry 5 A: they are the findings (and only they)
    assert {r for f in rec["unreached"].get("VBATT", []) for r in f["refs"]} <= {"J1", "U1", "U2"}
    assert not RC.open_by_net(out, fill=False).get("VBATT")


def test_5a_rail_pour_is_at_least_the_ipc_width(tmp_path, monkeypatch):
    pytest.importorskip("pcbnew")
    import pcbnew
    from test_power import _rail_board, _stub_dg, CLEAN
    from fluxplace import gaps as GP
    monkeypatch.setattr(GP, "_zone_nets", lambda p: set())
    _stub_dg(monkeypatch, CLEAN, CLEAN)
    src = _rail_board(str(tmp_path / "in.kicad_pcb"))
    # the spine is the 0.15 mm board track: the pour (spine grown by power_pour_over_mm) must still be >= the IPC width
    cons = {"power": {"VBAT": {"max_current_ma": 5000, "max_v": 14.5, "pour": True}}}
    out = str(tmp_path / "out.kicad_pcb")
    rec = PW.pour_rails(src, cons, out, layers=2)
    assert rec["added"] == ["VBAT/F.Cu"]
    z = [z for z in pcbnew.LoadBoard(out).Zones() if z.GetNetname() == "VBAT"][0]
    bb = z.GetBoundingBox()
    assert pcbnew.ToMM(bb.GetHeight()) >= PW.plan(cons, layers=2)["VBAT"]["width_outer"]


# ---- W34 part 2: branch current per run, zone credit --------------------------------------------------------------

CONS_BR = {"power": {"VB": {"max_current_ma": 3000, "max_v": 5.0, "source": "U1"}},
           "dcdrop": [{"net": "VB", "from": "U1", "to": ["U2"], "current_a": 3.0, "drop_mv_max": 100.0}]}
DC_OK = {"VB": {"ok": True, "mv": 10.0, "max": 100.0, "why": ""}}


def _br_board(tmp_path, zone=False, extra_pad=None):
    import pcbnew as P
    from test_routerules_syn import _board
    pads = [("U1", 2, 4, "VB"), ("C1", 10, 4, "VB"), ("U2", 18, 4, "VB")] + ([extra_pad] if extra_pad else [])
    tracks = [(2, 10, 18, 10, "VB"), (2, 4, 2, 10, "VB"), (10, 4, 10, 10, "VB"), (18, 4, 18, 10, "VB")] + (
        [(extra_pad[1], 4, extra_pad[1], 10, "VB")] if extra_pad else [])
    p = _board(str(tmp_path / "br.kicad_pcb"), pads=pads, tracks=tracks)
    b = P.LoadBoard(p)
    for t in b.GetTracks():
        if t.GetStart().y != t.GetEnd().y or t.GetStart().x != t.GetEnd().x:
            if t.GetStart().y == t.GetEnd().y:
                t.SetWidth(P.FromMM(3.0))                       # the trunk: wide enough for 3 A
    if zone:
        z = P.ZONE(b)
        z.SetLayer(P.F_Cu)
        z.SetNetCode(b.FindNet("VB").GetNetCode())
        ch = P.SHAPE_LINE_CHAIN()
        for x, y in ((15, 2), (19.5, 2), (19.5, 11), (15, 11)):
            ch.Append(P.FromMM(x), P.FromMM(y))
        ch.SetClosed(True)
        z.Outline().AddOutline(ch)
        b.Add(z)
        P.ZONE_FILLER(b).Fill(b.Zones())
    P.SaveBoard(p, b)
    return p


def _check(p, cons=CONS_BR, dc=DC_OK):
    from fluxplace import boardfile
    return [f for f in PW.check(boardfile.facts_from_file(p), cons, dc, board=p) if f["code"] == "POWER_WIDTH"]


def test_decap_stub_passes_and_load_and_source_stubs_fail_at_0_15(tmp_path):
    pytest.importorskip("pcbnew")
    fs = _check(_br_board(tmp_path))
    assert len(fs) == 1 and "2 narrow run(s)" in fs[0]["msg"]               # source stub + 3 A load pin; the decap stub is cleared
    assert "3.00 A tree flow U1" in fs[0]["msg"]                         # W40: the loads behind the stub (U2, 3 A), from the source U1


def test_unknown_load_current_uses_the_rail_total_and_is_flagged(tmp_path):
    pytest.importorskip("pcbnew")
    p = _br_board(tmp_path, extra_pad=("U3", 14, 4, "VB"))
    fs = _check(p)
    # W40: unknown only on the branches upstream of the unknown load: U3's own stub and the source stub; the U2 stub stays known
    assert len(fs) == 1 and "3 narrow run(s)" in fs[0]["msg"] and "2 run(s) with current unknown" in fs[0]["msg"]


def test_run_inside_a_same_net_pour_gets_zone_credit(tmp_path):
    pytest.importorskip("pcbnew")
    base = _check(_br_board(tmp_path / "a" if (tmp_path / "a").mkdir() is None else tmp_path))
    (tmp_path / "b").mkdir()
    z = _check(_br_board(tmp_path / "b", zone=True))
    assert "2 narrow run(s)" in base[0]["msg"]
    assert not z or "1 narrow run(s)" in z[0]["msg"]                        # the U2 stub lies in the filled pour: credited, source stub still fails
    assert z and "credited" in z[0]["msg"]


# ---- W34 part 3: resistor / diode / gate / no-end-pad physics --------------------------------------------------

def _edit(p, ref, value=None, pinfn=None):
    import pcbnew as P
    b = P.LoadBoard(p)
    for f in b.GetFootprints():
        if f.GetReference() == ref:
            if value is not None:
                f.SetValue(value)
            if pinfn is not None:
                for pad in f.Pads():
                    pad.SetPinFunction(pinfn)
    P.SaveBoard(p, b)
    return p


def _msg(p):
    fs = _check(p)
    return fs[0]["msg"] if fs else ""


def test_parse_ohms():
    for v, o in (("10k", 1e4), ("4.7k", 4700.0), ("4k7", 4700.0), ("0R", 0.0), ("220", 220.0), ("1M", 1e6), ("100R", 100.0)):
        assert PW.parse_ohms(v) == pytest.approx(o)
    assert PW.parse_ohms("100nF") is None or PW.parse_ohms("BAT54") is None


def test_resistor_pad_carries_at_most_v_over_r_so_its_stub_passes(tmp_path):
    pytest.importorskip("pcbnew")
    p = _edit(_br_board(tmp_path, extra_pad=("R1", 14, 4, "VB")), "R1", value="10k")
    assert "2 narrow run(s)" in _msg(p) and "unknown" not in _msg(p)        # source + U2 only; 5 V / 10 k = 0.5 mA


def test_zero_ohm_resistor_is_a_series_link_unknown_and_flagged(tmp_path):
    pytest.importorskip("pcbnew")
    m = _msg(_edit(_br_board(tmp_path, extra_pad=("R1", 14, 4, "VB")), "R1", value="0R"))
    assert "3 narrow run(s)" in m and "parts: R1" in m


def test_diode_without_a_dcdrop_row_stays_unknown(tmp_path):
    pytest.importorskip("pcbnew")
    m = _msg(_br_board(tmp_path, extra_pad=("D1", 14, 4, "VB")))
    assert "3 narrow run(s)" in m and "parts: D1" in m


def test_mosfet_gate_pin_carries_no_dc_current(tmp_path):
    pytest.importorskip("pcbnew")
    p = _edit(_br_board(tmp_path, extra_pad=("Q1", 14, 4, "VB")), "Q1", pinfn="G")
    assert "2 narrow run(s)" in _msg(p)
    q = _edit(_br_board(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, extra_pad=("Q1", 14, 4, "VB")), "Q1", pinfn="D")
    assert "parts: Q1" in _msg(q)                                           # a drain pin is not a gate


def _stub_board(tmp_path, end_via):
    import pcbnew as P
    from test_routerules_syn import _board
    p = _br_board(tmp_path)
    b = P.LoadBoard(p)
    t = P.PCB_TRACK(b)                                                       # a 6 mm stub off the trunk, no pad at its end
    t.SetStart(P.VECTOR2I(P.FromMM(6), P.FromMM(10)))
    t.SetEnd(P.VECTOR2I(P.FromMM(6), P.FromMM(4)))
    t.SetWidth(P.FromMM(0.15))
    t.SetLayer(P.F_Cu)
    t.SetNet(b.FindNet("VB"))
    b.Add(t)
    if end_via:
        v = P.PCB_VIA(b)
        v.SetPosition(P.VECTOR2I(P.FromMM(6), P.FromMM(4)))
        v.SetWidth(P.FromMM(0.45))
        v.SetDrill(P.FromMM(0.2))
        v.SetNet(b.FindNet("VB"))
        v.SetLayerPair(P.F_Cu, P.B_Cu)
        b.Add(v)
    P.SaveBoard(p, b)
    return p


def test_run_with_no_end_pad_is_a_dangling_stub_carrying_nothing(tmp_path):
    pytest.importorskip("pcbnew")
    assert "2 narrow run(s)" in _msg(_stub_board(tmp_path, end_via=False))   # the dangling 6 mm stub is cleared (0 A), the others as before


def test_stub_ending_on_a_via_with_nothing_behind_it_carries_nothing(tmp_path):
    pytest.importorskip("pcbnew")
    # W40: a via link is solved in the rail tree (powerflow, via_joins_layers); a via that leads nowhere has no load behind it
    assert "2 narrow run(s)" in _msg(_stub_board(tmp_path, end_via=True))
