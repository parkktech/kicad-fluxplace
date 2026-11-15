"""krt-whole route mode (fluxplace/krtwhole.py): flags from constraints (floors pinned, protected widths), flow order, pair
hand-over, rc 3 acceptance, via centring, and the whole stage with every router, DRC and tie call stubbed; plus a small real
run on a pcbnew-built board when a router checkout is present."""
import json
import os
import sys
import types
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import constraints as C
from fluxplace import krtwhole as KW
from fluxplace import netclass as NC
from fluxplace import pipeline as PL
from fluxplace import planetie as PT
from fluxplace import pourtie as PTI
from fluxplace import routediag as RD
from fluxplace import routerules as RR

# krt_flags reads option names as quoted strings from the package sources, so the fake checkout quotes them
ROUTE_PY = '"--fab-overrides", "--escalation", "--strict-sizes", "--keep-input-copper", "--nets", "--layers"\n'


def fake_checkout(tmp_path, route_text=ROUTE_PY, planes_text='"--nets", "--plane-layers", "--fab-overrides"\n'):
    root = tmp_path / "krt"
    (root / "py_router").mkdir(parents=True)
    (root / "py_router" / "route.py").write_text(route_text)
    (root / "py_router" / "route_planes.py").write_text(planes_text)
    (root / "py_router" / "route_diff.py").write_text('"--nets", "--fab-overrides"\n')
    return str(root)


def test_direct_flow_order_and_no_fanout_or_pairs():
    assert PL.FLOWS["direct"] == ["ready", "modules", "floorplan", "route", "tune", "silk", "review"]
    for gone in ("fanout", "pairs", "pour-tie", "plan-route"):
        assert gone not in PL.FLOWS["direct"]
    assert PL.FLOWS["constructive"] == PL.CONSTRUCTIVE          # existing flows unchanged


def test_floors_come_from_constraints_and_are_pinned(tmp_path):
    cons = {"route": {"min_track_mm": 0.127, "min_clearance_mm": 0.127},
            "fab": {"min_via_dia_mm": 0.4, "min_drill_mm": 0.2}}
    R = RR.rules(cons, None, track=0.2, clearance=0.2, via=0.45, drill=0.2)
    assert R["min_track"] == 0.127 and R["min_clearance"] == 0.127
    a = types.SimpleNamespace(router_dir=fake_checkout(tmp_path), router_py="py", layers=["F.Cu", "B.Cu"])
    src = str(tmp_path / "in.kicad_pcb")
    off = KW.router_cmd(a, R, str(tmp_path / "w"), "route.py", src, "o.kicad_pcb", esc="off")
    fab = KW.router_cmd(a, R, str(tmp_path / "w"), "route.py", src, "o.kicad_pcb", esc="fab")
    assert off[off.index("--escalation") + 1] == "off" and fab[fab.index("--escalation") + 1] == "fab"
    for cmd in (off, fab):                                      # preferred sizes requested; the floor file pins the bottom
        assert cmd[cmd.index("--track-width") + 1] == "0.2" and cmd[cmd.index("--clearance") + 1] == "0.2"
        text = open(cmd[cmd.index("--fab-overrides") + 1]).read()
        assert "clearance = 0.127" in text and "track_width = 0.127" in text and "via_diameter = 0.4" in text
        assert "--strict-sizes" in cmd and "--keep-input-copper" in cmd


def test_rail_widths_use_the_netclass_rule_and_pours_only_on_request():
    cons = {"power": {"+5V": {"max_current_ma": 4000, "pour": True},
                      "VBATT_IN": {"width_mm": 2.0},
                      "V3": {"max_current_ma": 900}}}
    nets = {"+5V", "VBATT_IN", "V3", "GND"}
    assert KW.rail_widths(cons, nets) == {"VBATT_IN": 2.0, "V3": C.rail_width_mm(900)}
    assert KW.rail_widths(cons, nets, pour=True)["+5V"] == C.rail_width_mm(4000)


def test_protected_nets_keep_their_rule_width_and_the_rest_take_the_preferred_track():
    cons = {"pairs": {"USB_": {"skew_mm": 3.0}}, "rf": {"nets": ["ANT"]}}
    cls = {"GND": {"class": "rail"}, "ANT": {"class": "rf"}, "CLK": {"class": "clock"}, "USB_DP": {"class": "pair"},
           "SIG": {"class": "signal"}}
    prot = KW.protected_nets(cls, cons)
    assert prot == {"GND", "ANT", "CLK", "USB_DP"}
    w = KW.protected_widths(prot, {"V5": 0.5, "GND": 0.4}, {"ANT"}, 0.31, {"track": 0.2})
    assert w == {"ANT": 0.31, "CLK": 0.2, "GND": 0.4, "USB_DP": 0.2}
    assert KW.power_args({"A[1]": 0.3}) == ["--power-nets", "A[[]1]", "--power-nets-widths", "0.3"]


def test_only_pairs_with_an_open_leg_go_to_route_diff():
    cons = {"pairs": {"ETH_": {"skew_mm": 1.0}, "AUD_": {"skew_mm": 1.0}}}
    names = ["ETH_N", "ETH_P", "ETH_TX_N", "ETH_TX_P", "AUD_DP", "AUD_DM", "OTHER_N", "OTHER_P"]
    diff, plain = KW.open_pairs(names, {"ETH_TX_P": 1}, cons)
    assert diff == ["ETH_TX_N", "ETH_TX_P"] and plain == []         # ETH_ is complete (locked); OTHER_ has no family
    diff, plain = KW.open_pairs(names, {"AUD_DM": 2}, cons)
    assert "AUD_DP" in diff + plain and "ETH_N" not in diff
    diff, _ = KW.open_pairs(names, {}, cons, handoff={"ETH_N"})    # a pair the pairs stage handed over (state c)
    assert diff == ["ETH_N", "ETH_P"]
    assert KW.routed_by_diff("X_P", "X_N") and not KW.routed_by_diff("X_P", "Y_N")


def test_exit_code_three_is_accepted_not_a_failure():
    assert KW.accept_rc(0) and KW.accept_rc(3)
    assert not KW.accept_rc(1) and not KW.accept_rc(124)


def test_pair_family_limits_come_from_constraints():
    cons = {"pairs": {"ETH_": {"impedance_diff": 100, "skew_mm": 1.0}, "USB_": {"impedance_diff": 90, "skew_mm": 3.0}},
            "si": {"default_skew_mm": 2.5}}
    fams = dict(KW.pair_families(cons))
    assert fams == {"ETH_": 1.0, "USB_": 3.0}
    assert KW.pair_families({}) == []


def test_rail_report_fails_only_a_long_thin_run():
    seg = {"+3V3": {"len": 40.0, "min_w": 0.15, "thin_runs_mm": [0.5, 1.0]},
           "VBATT_IN": {"len": 60.0, "min_w": 0.2, "thin_runs_mm": [3.0]}}
    rep = {r["net"]: r for r in KW.rail_report(seg, {"+3V3": 0.3, "VBATT_IN": 0.3}, set(), None)}
    assert rep["+3V3"]["fail"] is False          # two pad-entry necks, each shorter than the allowed run
    assert rep["VBATT_IN"]["fail"] is True and rep["VBATT_IN"]["longest_thin_mm"] == 3.0


def test_net_classes_for_ground_and_pads():
    assert NC.is_ground("GND") and not NC.is_ground("+5V")


def test_new_keys_is_the_multiset_difference(monkeypatch):
    base = Counter({("clearance", ("a",)): 1})
    monkeypatch.setattr(KW, "drc", lambda board: (Counter({("clearance", ("a",)): 1, ("clearance", ("b",)): 1}), Counter(), 0))
    got = KW.new_keys("x.kicad_pcb", base)
    assert got == Counter({("clearance", ("b",)): 1})


def _stub_stage(monkeypatch, tmp_path, rc=0, below_floor=False, thin_rail=False, pairs_open=True):
    """the whole stage with the router, kicad-cli and the tie modules replaced; file copies stay real."""
    krt = fake_checkout(tmp_path, route_text=ROUTE_PY + '"--max-ripup", "--mps-layer-swap", "--no-power-tap-neckdown"\n')
    calls = []

    def fake_router(cmd, cwd, log, cap_s=0):
        nets = cmd[cmd.index("--nets") + 1:] if "--nets" in cmd else []
        calls.append({"script": os.path.basename(cmd[1]), "src": cmd[2], "dst": cmd[3], "nets": nets, "cmd": cmd,
                      "esc": cmd[cmd.index("--escalation") + 1] if "--escalation" in cmd else None})
        KW.copy_board(cmd[2], cmd[3])
        return rc, 0.1

    monkeypatch.setattr(KW, "run_router", fake_router)
    monkeypatch.setattr(KW, "drc", lambda board: (Counter(), Counter(), 0))
    monkeypatch.setattr(KW, "drc_report", lambda board: {"violations": []})
    monkeypatch.setattr(KW, "copper_names", lambda b: ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"])
    monkeypatch.setattr(KW, "planes_of", lambda b, sig: [("GND", "In1.Cu"), ("+5V", "In4.Cu")])
    monkeypatch.setattr(KW, "floor_audit", lambda b, R: ["track width below floor x2"] if below_floor else [])
    monkeypatch.setattr(KW.RD, "load", lambda b: {"pads": []})
    monkeypatch.setattr(KW.NC, "net_pads", lambda B: {"GND": [1] * 5, "+5V": [1] * 3, "SIG": [1, 1], "USB_P": [1, 1], "USB_N": [1, 1]})
    monkeypatch.setattr(KW.NC, "classify", lambda B, cons, spec: {"GND": {"class": "rail"}, "+5V": {"class": "rail"},
                                                                 "SIG": {"class": "signal"}, "USB_P": {"class": "pair"}, "USB_N": {"class": "pair"}})
    monkeypatch.setattr(KW, "rf_nets", lambda B, cons: set())
    thin = {"+5V": {"len": 9.0, "min_w": 0.15, "thin_runs_mm": [5.0]}} if thin_rail else {}
    monkeypatch.setattr(KW, "measure", lambda b, r: (dict({"SIG": {"len": 5.0, "min_w": 0.15, "thin_runs_mm": []}}, **thin), 3))
    monkeypatch.setattr(KW, "pair_report", lambda b, c, cons, seg: {"total": 0, "complete": 0, "uncomplete": [], "over_limit": 0,
                                                                    "rows": [], "uncoupled": []})
    monkeypatch.setattr(KW, "owners", lambda d, b, c, cons: {"rows": [], "unowned": []})
    monkeypatch.setattr(KW, "refill_save", lambda s, t: KW.copy_board(s, t))
    from fluxplace import snapvias as SVM
    from fluxplace import batchroute as BRM
    monkeypatch.setattr(SVM, "snap_guarded", lambda s, t, rep, **k: {"moved": 0, "before": 0, "after": 0, "accepted": False, "why": "nothing to move"})
    monkeypatch.setattr(BRM, "route_batches", lambda board, wd, nets, run, **k: {"board": board, "start_open": 1, "open": 1, "attempts": 0})
    monkeypatch.setattr(PT, "run", lambda s, t, nets, conn, **kw: (KW.copy_board(s, t), {"placed": 0})[1])
    monkeypatch.setattr(PTI, "run", lambda s, t, w, nets=None, cons=None: (KW.copy_board(s, t), {"nets": {}, "untied": []})[1])
    monkeypatch.setattr(C, "inject_netclasses", lambda *a, **k: 0)
    work = tmp_path / "W" / "4-route"
    board = tmp_path / "in" / "board.kicad_pcb"
    board.parent.mkdir(parents=True)
    board.write_text("(kicad_pcb)\n")
    board.with_suffix(".kicad_pro").write_text("{}")
    a = types.SimpleNamespace(track=0.2, clearance=0.2, layers=["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"], router_dir=krt,
                              router_py=sys.executable, route_budget=100)
    cons = {"route": {"min_track_mm": 0.127, "min_clearance_mm": 0.127}, "pairs": {"USB_": {"skew_mm": 3.0}},
            "power": {"+5V": {"max_current_ma": 4000, "pour": True}}}
    c = types.SimpleNamespace(a=a, cons=cons, dir=lambda n, name: str(work))
    conn = _conn_factory(pairs_open)
    return c, str(board), calls, conn


def _conn_factory(pairs_open):
    """open connections of a stub board: the pairs step closes the USB pair, the rest step closes SIG."""
    def conn(path):
        b = os.path.basename(path)
        usb = 0 if (b.startswith(("02-pairs", "03-", "04-", "05-", "06-", "07-")) or b == "board.kicad_pcb" or not pairs_open) else 1
        sig = 0 if (b.startswith(("04-rest", "05-", "06-", "07-")) or b == "board.kicad_pcb") else 1
        return {"USB_P": usb, "USB_N": usb, "SIG": sig}
    return conn


def _steps(tmp_path):
    return json.load(open(str(tmp_path / "W" / "4-route" / "krtwhole.json")))["steps"]


def test_stage_chains_pairs_protected_then_rest_and_the_pairs_output_feeds_the_next_step(monkeypatch, tmp_path):
    c, board, calls, conn = _stub_stage(monkeypatch, tmp_path)
    res = KW.run(c, 4, board, conn)
    scripts = [x["script"] for x in calls]
    assert scripts == ["route_planes.py", "route_planes.py", "route_diff.py", "route.py"]        # no protected nets open: no call
    assert calls[2]["nets"] == ["USB_N", "USB_P"]                                                 # only the open pair
    assert calls[3]["src"] == calls[2]["dst"] and calls[2]["src"] == calls[1]["dst"]              # each step builds on the last
    assert calls[3]["esc"] == "fab" and "--escalation" in calls[3]["cmd"]
    assert "!USB_N" not in calls[3]["nets"]                                                       # the pair was closed
    names = [s["step"] for s in _steps(tmp_path)]
    assert names[:2] == ["planes GND->In1.Cu", "planes +5V->In4.Cu"] and names[2].startswith("pairs route_diff")
    assert res["verdict"] == "pass"


def test_open_protected_nets_get_an_exact_pass_and_stay_out_of_the_step_down_pass(monkeypatch, tmp_path):
    c, board, calls, conn = _stub_stage(monkeypatch, tmp_path, pairs_open=False)
    base = conn

    def conn2(path):
        r = dict(base(path))
        r["GND"] = 0 if os.path.basename(path).startswith("03-") or "05-" in os.path.basename(path) else 2
        return r
    KW.run(c, 4, board, conn2)
    prot = [x for x in calls if x["dst"].endswith("03-protected.kicad_pcb")][0]
    assert prot["esc"] == "off" and "GND" in prot["nets"] and "--power-nets-widths" in prot["nets"]


def test_exit_three_with_every_feature_at_the_floor_is_kept_with_a_note(monkeypatch, tmp_path):
    c, board, calls, conn = _stub_stage(monkeypatch, tmp_path, rc=3)
    KW.run(c, 4, board, conn)
    steps = _steps(tmp_path)
    rest = [s for s in steps if s["step"].startswith("rest")][0]
    assert rest["rc"] == 3 and rest["accepted"] is True and "at or above the floor" in rest["why"]


def test_a_step_delivering_below_a_floor_is_rejected_and_its_input_kept(monkeypatch, tmp_path):
    c, board, calls, conn = _stub_stage(monkeypatch, tmp_path, rc=3, below_floor=True)
    res = KW.run(c, 4, board, conn)
    steps = _steps(tmp_path)
    rest = [s for s in steps if s["step"].startswith("rest")][0]
    assert rest["accepted"] is False and "below floor" in rest["why"]
    assert res["verdict"] == "fail"              # the final audit also sees the below-floor copper


def test_a_thinned_protected_net_fails_the_step(monkeypatch, tmp_path):
    c, board, calls, conn = _stub_stage(monkeypatch, tmp_path, thin_rail=True)
    res = KW.run(c, 4, board, conn)
    assert [s for s in _steps(tmp_path) if s["step"].startswith("rest")][0]["accepted"] is False
    assert res["verdict"] == "fail"


def test_netclasses_are_raised_to_the_floors(tmp_path):
    """inject_netclasses writes 0.1 mm pair clearance and the impedance gap; the copy is raised to the floors, the
    values already above them are left alone, and every change is returned for the record."""
    pro = tmp_path / "b.kicad_pro"
    pro.write_text(json.dumps({"net_settings": {"classes": [
        {"name": "DP_ETH__100R", "clearance": 0.1, "track_width": 0.173, "diff_pair_gap": 0.107, "via_diameter": 0.45,
         "via_drill": 0.25},
        {"name": "PWR_X", "clearance": 0.15, "track_width": 1.5}]}}))
    R = {"min_clearance": 0.127, "min_track": 0.127, "min_via": 0.4, "min_drill": 0.2, "via": 0.45, "drill": 0.2}
    changed = KW.clamp_netclasses(str(pro), R)
    cls = {c["name"]: c for c in json.load(open(pro))["net_settings"]["classes"]}
    assert cls["DP_ETH__100R"]["clearance"] == 0.127 and cls["DP_ETH__100R"]["diff_pair_gap"] == 0.127
    assert cls["DP_ETH__100R"]["track_width"] == 0.173 and cls["PWR_X"]["track_width"] == 1.5
    assert ("DP_ETH__100R", "clearance", 0.1, 0.127) in changed
    assert all(c[0] != "PWR_X" for c in changed)


def test_netclass_vias_are_capped_at_the_requested_rule(tmp_path):
    pro = tmp_path / "c.kicad_pro"
    pro.write_text(json.dumps({"net_settings": {"classes": [{"name": "PWR_V", "via_diameter": 0.6, "via_drill": 0.3}]}}))
    R = {"min_clearance": 0.127, "min_track": 0.127, "min_via": 0.4, "min_drill": 0.2, "via": 0.45, "drill": 0.2}
    KW.clamp_netclasses(str(pro), R)
    cl = json.load(open(pro))["net_settings"]["classes"][0]
    assert cl["via_diameter"] == 0.45 and cl["via_drill"] == 0.2


# ----------------------------------------------------------------- centring acceptance (real pcbnew board, stubbed DRC)
try:
    import pcbnew as P
    HAVE_PCB = True
except ImportError:
    HAVE_PCB = False
need_pcbnew = pytest.mark.skipif(not HAVE_PCB, reason="pcbnew not available")


def _tiny(path, via_at=(10.0, 5.0), end_at=(10.1, 5.0), pads=None):
    """2-layer board: one via on net N, one F.Cu track on N ending `end_at` (inside the via copper, off its centre)."""
    b = P.BOARD()
    b.Add(P.NETINFO_ITEM(b, "N"))
    n = b.FindNet("N")
    v = P.PCB_VIA(b); v.SetPosition(P.VECTOR2I(P.FromMM(via_at[0]), P.FromMM(via_at[1]))); v.SetWidth(P.FromMM(0.45))
    v.SetDrill(P.FromMM(0.2)); v.SetNet(n); v.SetLayerPair(P.F_Cu, P.B_Cu); b.Add(v)
    t = P.PCB_TRACK(b); t.SetStart(P.VECTOR2I(P.FromMM(2), P.FromMM(5))); t.SetEnd(P.VECTOR2I(P.FromMM(end_at[0]), P.FromMM(end_at[1])))
    t.SetWidth(P.FromMM(0.2)); t.SetLayer(P.F_Cu); t.SetNet(n); b.Add(t)
    P.SaveBoard(path, b)
    open(path[:-len(".kicad_pcb")] + ".kicad_pro", "w").write("{}")
    return path, t.m_Uuid.AsString()


def _report_for(path, new_on=None):
    """stub kicad-cli report: one track_not_centered_on_via per unsnapped end; `new_on` (a track uuid) adds a clearance
    violation as soon as that track's end sits on the via centre."""
    b = P.LoadBoard(path)
    out = {"violations": []}
    vias = [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA"]
    for t in b.GetTracks():
        if t.GetClass() != "PCB_TRACK":
            continue
        for p in (t.GetStart(), t.GetEnd()):
            for v in vias:
                d = ((p.x - v.GetPosition().x) ** 2 + (p.y - v.GetPosition().y) ** 2) ** 0.5 / 1e6
                if 0 < d <= 0.225:
                    out["violations"].append({"type": "track_not_centered_on_via", "items": [{"pos": {"x": p.x / 1e6, "y": p.y / 1e6}}]})
                if d == 0 and new_on and t.m_Uuid.AsString() == new_on:
                    out["violations"].append({"type": "clearance", "items": [{"pos": {"x": p.x / 1e6, "y": p.y / 1e6},
                                                                              "uuid": t.m_Uuid.AsString(), "description": "Track [N] on F.Cu"}]})
    return out


@need_pcbnew
def test_centring_moves_the_track_end_onto_the_via_when_no_new_violation_appears(tmp_path):
    from fluxplace import snapvias as SV
    src, _u = _tiny(str(tmp_path / "a.kicad_pcb"))
    dst = str(tmp_path / "b.kicad_pcb")
    r = SV.snap_guarded(src, dst, lambda p: _report_for(p))
    assert r["accepted"] and r["before"] == 1 and r["after"] == 0 and r["moved"] == 1
    t = [x for x in P.LoadBoard(dst).GetTracks() if x.GetClass() == "PCB_TRACK"][0]
    assert (t.GetEnd().x, t.GetEnd().y) == (P.FromMM(10.0), P.FromMM(5.0))


@need_pcbnew
def test_centring_is_refused_when_the_move_creates_a_violation_and_the_track_is_excluded(tmp_path):
    from fluxplace import snapvias as SV
    src, uuid = _tiny(str(tmp_path / "a.kicad_pcb"))
    dst = str(tmp_path / "b.kicad_pcb")
    r = SV.snap_guarded(src, dst, lambda p: _report_for(p, new_on=uuid))
    assert not r["accepted"] and not os.path.exists(dst)           # the only move was the offender; it is excluded, nothing left to move
    assert r["after"] == r["before"] or "new violation" in r["why"] or r["why"]


@need_pcbnew
def test_centring_never_touches_locked_copper(tmp_path):
    from fluxplace import snapvias as SV
    src, _u = _tiny(str(tmp_path / "a.kicad_pcb"))
    b = P.LoadBoard(src)
    for t in b.GetTracks():
        t.SetLocked(True)
    P.SaveBoard(src, b)
    r = SV.snap_guarded(src, str(tmp_path / "b.kicad_pcb"), lambda p: _report_for(p))
    assert r["moved"] == 0 and not r["accepted"]


# ----------------------------------------------------------------- a small real run through the router
@need_pcbnew
def test_real_router_run_on_a_tiny_board_keeps_the_floor_and_exit_codes(tmp_path):
    from fluxplace import routerloc as RL
    from fluxplace import routeconn as RC
    try:
        loc = RL.resolve(None, None)
    except RuntimeError:
        pytest.skip("no KiCadRoutingTools checkout")
    b = P.BOARD()
    for nm in ("A", "B"):
        b.Add(P.NETINFO_ITEM(b, nm))
    e = [(0, 0), (20, 0), (20, 12), (0, 12)]
    for i in range(4):
        s = P.PCB_SHAPE(b); s.SetShape(P.SHAPE_T_SEGMENT); s.SetLayer(P.Edge_Cuts); s.SetWidth(P.FromMM(0.1))
        s.SetStart(P.VECTOR2I(P.FromMM(e[i][0]), P.FromMM(e[i][1]))); s.SetEnd(P.VECTOR2I(P.FromMM(e[(i + 1) % 4][0]), P.FromMM(e[(i + 1) % 4][1])))
        b.Add(s)
    for ref, x, y, net in (("R1", 3, 4, "A"), ("R2", 17, 4, "A"), ("R3", 3, 8, "B"), ("R4", 17, 8, "B")):
        f = P.FOOTPRINT(b); f.SetReference(ref); f.SetPosition(P.VECTOR2I(P.FromMM(x), P.FromMM(y)))
        p = P.PAD(f); p.SetNumber("1"); p.SetAttribute(P.PAD_ATTRIB_SMD); p.SetShape(P.PAD_SHAPE_RECT)
        p.SetSize(P.VECTOR2I(P.FromMM(0.5), P.FromMM(0.5))); p.SetPosition(P.VECTOR2I(P.FromMM(x), P.FromMM(y)))
        ls = P.LSET(); ls.AddLayer(P.F_Cu); p.SetLayerSet(ls); p.SetNet(b.FindNet(net)); f.Add(p); b.Add(f)
    src = str(tmp_path / "t.kicad_pcb")
    P.SaveBoard(src, b)
    json.dump({"meta": {"version": 1}, "net_settings": {"classes": [{"name": "Default", "clearance": 0.127, "track_width": 0.15,
              "via_diameter": 0.45, "via_drill": 0.2}], "netclass_patterns": []}}, open(src[:-10] + ".kicad_pro", "w"))
    cons = {"route": {"min_track_mm": 0.127, "min_clearance_mm": 0.127}, "fab": {"min_via_dia_mm": 0.4, "min_drill_mm": 0.2}}
    a = types.SimpleNamespace(router_dir=loc["dir"], router_py=loc["py"], layers=["F.Cu", "B.Cu"])
    R = RR.rules(cons, src, 0.2, 0.2)
    out = str(tmp_path / "o.kicad_pcb")
    rc, _s = KW.run_router(KW.router_cmd(a, R, str(tmp_path), "route.py", src, out, ["--nets", "*"], esc="fab"), loc["dir"], str(tmp_path / "r.log"), 300)
    assert KW.accept_rc(rc) and os.path.exists(out), open(str(tmp_path / "r.log")).read()[-800:]
    assert KW.floor_audit(out, R) == []
    assert sum(RC.open_by_net(out).values()) == 0


def test_preferred_sizes_are_the_default_class_unless_the_constraints_say_otherwise(tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)\n")
    board.with_suffix(".kicad_pro").write_text(json.dumps({"net_settings": {"classes": [
        {"name": "Default", "clearance": 0.12, "track_width": 0.15}]}}))
    cons = {"route": {"min_track_mm": 0.127, "min_clearance_mm": 0.127}}
    R = RR.rules(cons, str(board), 0.2, 0.2)
    got = KW.preferred_sizes(cons, str(board), R)
    assert got["track"] == 0.15 and got["clearance"] == 0.127          # the class clearance is under the floor: floored
    cons["route"]["preferred_track_mm"] = 0.2
    assert KW.preferred_sizes(cons, str(board), R)["track"] == 0.2
    board.with_suffix(".kicad_pro").unlink()
    assert KW.preferred_sizes({"route": {"min_track_mm": 0.127, "min_clearance_mm": 0.127}}, str(board), R)["track"] == 0.2   # the stage's own sizes
