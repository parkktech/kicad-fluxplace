"""krt-whole route mode (fluxplace/krtwhole.py): flags from constraints, flow order, plane and rail assignment,
leftover ladder, rail neck rule, and the whole stage with every router, DRC and tie call stubbed."""
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
    R = RR.rules(cons, None, track=0.15, clearance=0.15, via=0.45, drill=0.2)
    assert R["min_track"] == 0.127 and R["min_clearance"] == 0.127
    assert R["escalation"] == "off" and R["strict_sizes"] is True
    krt = fake_checkout(tmp_path)
    flags = RR.flags(R, krt, str(tmp_path / "w"), script="route.py")
    fo = flags[flags.index("--fab-overrides") + 1]
    text = open(fo).read()
    assert "clearance = 0.127" in text and "track_width = 0.127" in text
    assert "--escalation" in flags and flags[flags.index("--escalation") + 1] == "off"
    assert "--strict-sizes" in flags


def test_rail_widths_use_the_netclass_rule_and_skip_pours():
    cons = {"power": {"+5V": {"max_current_ma": 4000, "pour": True},
                      "VBATT_IN": {"width_mm": 2.0},
                      "V3": {"max_current_ma": 900}}}
    got = KW.rail_widths(cons, {"+5V", "VBATT_IN", "V3", "GND"})
    assert got == {"VBATT_IN": 2.0, "V3": C.rail_width_mm(900)}


def test_plane_plan_puts_ground_first_then_pour_rails_in_stack_order():
    cons = {"power": {"+5V": {"max_current_ma": 4000, "pour": True}}}
    pads = {"GND": [1] * 5, "GND_ISO": [1], "+5V": [1] * 3, "+3V3": [1] * 9}
    plan = KW.plane_plan(cons, ["In1.Cu", "In4.Cu"], pads)
    assert plan == [("GND", "In1.Cu"), ("+5V", "In4.Cu")]
    assert KW.plane_plan(cons, [], pads) == []


def test_leftover_ladder_follows_the_checkout_flags(tmp_path):
    with_swap = types.SimpleNamespace(router_dir=fake_checkout(tmp_path, route_text=ROUTE_PY + '"--max-ripup", "--mps-layer-swap"\n'))
    assert [l for l, _ in KW.leftover_ladder(with_swap)] == ["ripup10", "ripup10+swap"]
    bare = types.SimpleNamespace(router_dir=fake_checkout(tmp_path / "b"))
    assert [l for l, _ in KW.leftover_ladder(bare)] == ["ripup10"]


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


def _stub_stage(monkeypatch, tmp_path, leftover_adds_key=False):
    """the whole stage with the router, kicad-cli and the tie modules replaced; file copies stay real."""
    krt = fake_checkout(tmp_path, route_text=ROUTE_PY + '"--max-ripup", "--mps-layer-swap"\n')
    calls = []

    def fake_router(cmd, cwd, log):
        calls.append(os.path.basename(cmd[1]) + " " + " ".join(cmd[cmd.index("--nets") + 1:cmd.index("--nets") + 2]))
        dst = cmd[3]
        KW.copy_board(cmd[2], dst)
        return 0, 0.1

    def fake_drc(board):
        extra = Counter({("clearance", ("new",)): 1}) if (leftover_adds_key and "leftover" in os.path.basename(board)) else Counter()
        return Counter() + extra, Counter(), 0

    monkeypatch.setattr(KW, "run_router", fake_router)
    monkeypatch.setattr(KW, "drc", fake_drc)
    monkeypatch.setattr(KW, "copper_names", lambda b: ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"])
    monkeypatch.setattr(KW.RD, "load", lambda b: {"pads": []})
    monkeypatch.setattr(KW.NC, "net_pads", lambda B: {"GND": [1] * 5, "+5V": [1] * 3, "SIG": [1, 1]})
    monkeypatch.setattr(KW, "rf_nets", lambda B, cons: set())
    monkeypatch.setattr(KW, "measure", lambda b, r: ({"SIG": {"len": 5.0, "min_w": 0.15, "thin_runs_mm": []}}, 3))
    monkeypatch.setattr(KW, "pair_report", lambda b, c, cons, seg: {"total": 0, "complete": 0, "uncomplete": [], "over_limit": 0,
                                                                    "rows": [], "uncoupled": []})
    monkeypatch.setattr(KW, "owners", lambda d, b, c, cons: {"rows": [], "unowned": []})
    monkeypatch.setattr(KW, "refill_save", lambda s, t: KW.copy_board(s, t))
    from fluxplace import snapvias as SVM
    monkeypatch.setattr(SVM, "snap_file", lambda s, t: (KW.copy_board(s, t), (0, 0))[1])
    monkeypatch.setattr(PT, "run", lambda s, t, nets, conn, **kw: (KW.copy_board(s, t), {"placed": 0})[1])
    monkeypatch.setattr(PTI, "run", lambda s, t, w, nets=None: (KW.copy_board(s, t), {"nets": {}, "untied": []})[1])
    monkeypatch.setattr(C, "inject_netclasses", lambda *a, **k: 0)
    work = tmp_path / "W" / "4-route"
    board = tmp_path / "in" / "board.kicad_pcb"
    board.parent.mkdir(parents=True)
    board.write_text("(kicad_pcb)\n")
    board.with_suffix(".kicad_pro").write_text("{}")
    a = types.SimpleNamespace(track=0.15, clearance=0.15, layers=["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"], router_dir=krt,
                              router_py=sys.executable)
    cons = {"route": {"min_track_mm": 0.127, "min_clearance_mm": 0.127},
            "power": {"+5V": {"max_current_ma": 4000, "pour": True}}}
    c = types.SimpleNamespace(a=a, cons=cons,
                              dir=lambda n, name: str(work))
    return c, str(board), calls


def test_stage_runs_passes_in_order_and_accepts_a_leftover_rung(monkeypatch, tmp_path):
    c, board, calls = _stub_stage(monkeypatch, tmp_path)
    res = KW.run(c, 4, board, _conn)
    names = [s["step"] for s in json.load(open(str(tmp_path / "W" / "4-route" / "krtwhole.json")))["steps"]]
    assert names[:2] == ["planes GND->In1.Cu", "planes +5V->In4.Cu"]
    assert names[2:4] == ["pairs route_diff *", "rest route.py *"]
    assert names[4] == "leftovers ripup10"
    assert calls[0].startswith("route_planes.py") and calls[2].startswith("route_diff.py") and calls[3].startswith("route.py")
    assert res["verdict"] == "pass"


def test_stage_rejects_a_leftover_rung_that_adds_a_drc_key(monkeypatch, tmp_path):
    c, board, _ = _stub_stage(monkeypatch, tmp_path, leftover_adds_key=True)
    KW.run(c, 4, board, _conn)
    steps = json.load(open(str(tmp_path / "W" / "4-route" / "krtwhole.json")))["steps"]
    lo = [s for s in steps if s["step"].startswith("leftovers")][0]
    assert lo["accepted"] is False and "DRC" in lo["why"]


def _conn(path):
    """open connections of a stub board: a leftover rung (or the final board) closes the net, every other step leaves it open."""
    if "leftover" in os.path.basename(path) or path.endswith(os.path.join("4-route", "board.kicad_pcb")):
        return {"SIG": 0}
    return {"SIG": 1}


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
