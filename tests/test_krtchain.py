"""krt-whole chain: flag construction, step chaining, exit-3 acceptance (router stubbed)."""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import krtchain as K  # noqa: E402

R = {"min_clearance": 0.127, "min_track": 0.127, "min_via": 0.45, "min_drill": 0.2, "clearance": 0.127, "track": 0.15,
     "via": 0.45, "drill": 0.2, "clearance_class": 0.0}


def test_flags(tmp_path):
    f = K.base_flags(R, str(tmp_path))
    assert f[:8] == ["--track-width", "0.15", "--clearance", "0.127", "--via-size", "0.45", "--via-drill", "0.2"]
    assert "--strict-sizes" in f and f[f.index("--escalation") + 1] == "off"
    assert "clearance = 0.127" in open(f[f.index("--fab-overrides") + 1]).read()
    c = K.cmd_route("py", "/r", "a", "b", ["F.Cu", "B.Cu"], f, {"GND": 0.3})
    assert c[c.index("--power-nets") + 1] == "GND" and c[c.index("--power-nets-widths") + 1] == "0.3"
    assert "--keep-input-copper" in c and "--keep-input-copper" not in K.cmd_planes("py", "/r", "a", "b", "GND", "In1.Cu", f)


def _ctx(tmp_path, monkeypatch, rcs):
    (tmp_path / "in.kicad_pcb").write_text("x")
    monkeypatch.setattr(K, "plane_plan", lambda b: (["F.Cu", "B.Cu"], [("GND", "In1.Cu")]))
    monkeypatch.setattr(K, "_all_nets", lambda b: ["USB_DP", "USB_DM", "X"])
    monkeypatch.setattr(K.RC, "open_by_net", lambda b, **k: {"USB_DP": 1})
    monkeypatch.setattr(K.RR, "rules", lambda *a, **k: dict(R))
    c = types.SimpleNamespace(a=types.SimpleNamespace(router_py="py", router_dir="/r", track=0.15, clearance=0.127),
                              cons={"pairs": {"USB_": {}}, "route": {"whole_post": False}}, work=str(tmp_path), state={}, dir=lambda n, nm: str(tmp_path))
    calls = []

    def runner(cmd, log, **kw):
        calls.append(cmd)
        rc = rcs.pop(0)
        if rc in (0, 3):
            open(cmd[3], "w").write("y")
        return rc, 1.0
    return c, calls, runner


def test_chain_accepts_exit3(tmp_path, monkeypatch):
    c, calls, runner = _ctx(tmp_path, monkeypatch, [0, 3])
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert [os.path.basename(x[1]) for x in calls] == ["route_planes.py", "route.py"]    # gated USB pair: no KRT diff step (F1)
    assert calls[1][calls[1].index("--nets") + 1:calls[1].index("--power-nets")] == ["X"]   # both legs out (open + pairgroups failure)
    assert r["verdict"] == "warn" and "USB_DP" in r["line"] and r["hs_excluded"] == ["USB_DP"]
    assert "--json-out" in calls[1]


def test_router_relaxed_project_is_discarded_after_every_step(tmp_path, monkeypatch):
    import json
    approved = {"board": {"design_settings": {"rules": {"min_via_diameter": 0.46, "min_hole_to_hole": 0.3}}},
                "net_settings": {"classes": [{"name": "Default", "clearance": 0.2}]}}
    (tmp_path / "in.kicad_pro").write_text(json.dumps(approved))
    c, calls, base_runner = _ctx(tmp_path, monkeypatch, [0, 0, 0])
    monkeypatch.setattr(K.RR, "rules", lambda *a, **k: dict(R, min_via=0.46, via=0.46))

    def relaxed_runner(cmd, log, **kw):
        rc, secs = base_runner(cmd, log, **kw)
        relaxed = {"board": {"design_settings": {"rules": {"min_via_diameter": 0.45, "min_hole_to_hole": 0.1}}},
                   "net_settings": {"classes": [{"name": "Default", "clearance": 0.01}]}}
        open(cmd[3][:-10] + ".kicad_pro", "w").write(json.dumps(relaxed))
        return rc, secs

    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), relaxed_runner)
    assert json.load(open(tmp_path / "board.kicad_pro")) == approved
    assert json.load(open(tmp_path / "3-route.kicad_pro")) == approved
    assert "router project rules discarded:" in r["line"]
    routed = [s for s in r["steps"] if s["step"] != "pairgroups"]           # pairgroups is a Python step, no router project
    assert len(routed) == 2 and all(s["router_project_discarded"] for s in routed)
    assert all(cmd[cmd.index("--via-size") + 1] == "0.46" for cmd in calls)


def test_chain_stops_on_failure(tmp_path, monkeypatch):
    c, calls, runner = _ctx(tmp_path, monkeypatch, [0, 1, 0])
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert r["verdict"] == "fail" and len(calls) == 2


def test_chain_rejects_stale_output(tmp_path, monkeypatch):
    c, _calls, _unused = _ctx(tmp_path, monkeypatch, [])
    (tmp_path / "2-diff.kicad_pcb").write_text("stale")
    calls = []

    def runner(cmd, log, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            open(cmd[3], "w").write("fresh")
        return 0, 1.0

    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert r["verdict"] == "fail" and len(calls) == 2


def test_chain_uses_one_total_budget(tmp_path, monkeypatch):
    c, _calls, _unused = _ctx(tmp_path, monkeypatch, [])
    c.a.route_budget = 42
    caps = []

    def runner(cmd, log, **kw):
        caps.append(kw["cap_s"])
        open(cmd[3], "w").write("y")
        return 0, 1.0

    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert r["verdict"] == "warn" and all(0 < cap <= 42 for cap in caps)
    assert sum(r["steps"][i]["s"] for i in range(len(caps))) <= 42
    import json
    st = json.load(open(tmp_path / "routed.kicad_pcb.status.json"))
    assert st["state"] == "warn" and st["open"] == {"USB_DP": 1}


def test_zero_open_with_pair_or_rf_failures_is_warn_not_done(tmp_path, monkeypatch):
    c, _calls, runner = _ctx(tmp_path, monkeypatch, [0, 0, 0])
    monkeypatch.setattr(K.RC, "open_by_net", lambda b, **k: {})
    from fluxplace import routediag as RD, netclass as NC, qualaudit as QA
    monkeypatch.setattr(RD, "load", lambda b: {})
    monkeypatch.setattr(NC, "classify", lambda *a: {})
    monkeypatch.setattr(QA, "status_fields", lambda *a, **k: {
        "not_done": {"count": 2, "pairs": [{"pair": "USB_DP/USB_DM", "cause": "skew"}], "rf": []},
        "nets_routed": 0, "nets_open0": 2})
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert r["verdict"] == "warn" and "NOT DONE 2" in r["line"]
    import json
    st = json.load(open(tmp_path / "routed.kicad_pcb.status.json"))
    assert st["state"] == "warn" and st["not_done"]["count"] == 2


def test_chain_runs_guarded_cleanup_when_enabled(tmp_path, monkeypatch):
    c, calls, runner = _ctx(tmp_path, monkeypatch, [0, 0, 0])
    c.cons["route"]["whole_post"] = True
    seen = []

    def post(best, d, planes, rules):
        seen.append((best, planes, rules["via"]))
        return {"snap": {"moved": 40, "centred_left": 7}, "ties": {"placed": 2}}

    monkeypatch.setattr(K, "_post", post)
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert len(seen) == 1 and seen[0][1] == [("GND", "In1.Cu")]
    assert r["post"]["snap"]["moved"] == 40 and "guarded plane ties 2" in r["line"]


def test_stub_ok_keeps_clearance(tmp_path):
    pcbnew = __import__("pytest").importorskip("pcbnew")
    from fluxplace import planetie as PT
    from test_routerules_syn import _board
    b = pcbnew.LoadBoard(_board(str(tmp_path / "s.kicad_pcb"), pads=[("U1", 5, 6, "A"), ("U2", 5.5, 6, "B")]))
    nc = b.FindNet("A").GetNetCode()
    f = pcbnew.F_Cu
    assert not PT.stub_ok(b, nc, f, (5, 6), (6, 6), 0.15, 0.127)        # runs into the B pad
    assert PT.stub_ok(b, nc, f, (5, 6), (5, 7), 0.15, 0.127)


def _sized(tmp_path, cons, track=None, clr=None):
    from fluxplace import routerules as RR
    a = types.SimpleNamespace(track=track, clearance=clr)
    r = RR.rules_for(cons, None, a, B={"parts": {}})
    return K.base_flags(r, str(tmp_path))


FLOOR = {"min_track_mm": 0.127, "min_clearance_mm": 0.127}


def test_preferred_size_from_constraints(tmp_path):
    f = _sized(tmp_path, {"route": {**FLOOR, "track_mm": 0.15}})
    assert " ".join(f[:8]) == "--track-width 0.15 --clearance 0.127 --via-size 0.45 --via-drill 0.2"
    assert f[8:11] == ["--escalation", "off", "--strict-sizes"] and f[11] == "--fab-overrides"


def test_cli_size_beats_constraints(tmp_path):
    f = _sized(tmp_path, {"route": {**FLOOR, "track_mm": 0.15, "clearance_mm": 0.14}}, track=0.18, clr=0.13)
    assert f[:2] == ["--track-width", "0.18"]


def test_legacy_default_lifted_by_floor(tmp_path):
    assert _sized(tmp_path, {"route": FLOOR})[:4] == ["--track-width", "0.2", "--clearance", "0.127"]   # clearance on the wire = the floor (request_clearance)
    assert _sized(tmp_path, {"route": {"min_track_mm": 0.25}})[:2] == ["--track-width", "0.25"]


def test_post_exception_keeps_board_and_warns(tmp_path, monkeypatch):
    import json
    c, calls, runner = _ctx(tmp_path, monkeypatch, [0, 0, 0])
    c.cons["route"]["whole_post"] = True
    monkeypatch.setattr(K.RC, "open_by_net", lambda b, **k: {})

    def post(best, d, planes, rules):
        open(best, "w").write("half written")
        raise RuntimeError("tie failed")

    monkeypatch.setattr(K, "_post", post)
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert r["verdict"] == "warn" and r["post"] is None
    assert r["post_error"] == {"type": "RuntimeError", "message": "tie failed"} and "POST ERROR RuntimeError" in r["line"]
    assert open(r["board"]).read() != "half written"
    assert json.load(open(tmp_path / "routed.kicad_pcb.status.json"))["post_error"]["type"] == "RuntimeError"
    assert json.load(open(tmp_path / "result.json"))["post_error"]["message"] == "tie failed"


def test_pairs_missed_by_external_router_are_reported(tmp_path, monkeypatch):
    c, _calls, runner = _ctx(tmp_path, monkeypatch, [0, 0, 0])
    monkeypatch.setattr(K.RC, "open_by_net", lambda b, **k: {})
    from fluxplace import routediag as RD, netclass as NC, qualaudit as QA, pairs as PRS, pairaudit as PA
    monkeypatch.setattr(RD, "load", lambda b: {})
    monkeypatch.setattr(NC, "classify", lambda *a: {})
    monkeypatch.setattr(QA, "status_fields", lambda *a, **k: {"not_done": {"count": 0, "pairs": [], "rf": []}, "nets_routed": 0, "nets_open0": 0})
    monkeypatch.setattr(PA, "pair_list", lambda cls: [("A_P", "A_N")])
    monkeypatch.setattr(PRS, "crosscheck_external", lambda pairs, summary=None, log=None: [{"pair": "A_P/A_N", "reason": "not_recognised"}])
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    had_diff = any(s["step"] == "diff" for s in r["steps"])
    assert ("pair router missed 1 pair" in r["line"]) == had_diff


def test_route_reads_completed_pairs_report_beside_router_input(tmp_path, monkeypatch):
    import json
    c, _calls, runner = _ctx(tmp_path, monkeypatch, [0, 0, 0])
    opens = iter(({"LED_P": 1}, {}, {}, {}, {}))
    monkeypatch.setattr(K.RC, "open_by_net", lambda b, **k: next(opens, {}))
    monkeypatch.setattr(K, "_all_nets", lambda b: ["LED_P", "LED_N", "X"])
    (tmp_path / "in.pairs.json").write_text(json.dumps({"pairs": [{"pair": ["LED_P", "LED_N"], "status": "complete"}]}))
    from fluxplace import routediag as RD, netclass as NC, qualaudit as QA, pairs as PRS, pairaudit as PA
    monkeypatch.setattr(RD, "load", lambda b: {})
    monkeypatch.setattr(NC, "classify", lambda *a: {})
    monkeypatch.setattr(QA, "status_fields", lambda *a, **k: {"not_done": {"count": 0, "pairs": [], "rf": []}, "nets_routed": 0, "nets_open0": 0})
    monkeypatch.setattr(PA, "pair_list", lambda cls: [("LED_P", "LED_N")])
    monkeypatch.setattr(PRS, "crosscheck_external", lambda pairs, summary=None, log=None: [
        {"pair": "LED_P/LED_N", "p": "LED_P", "n": "LED_N", "reason": "not_recognised"}])
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    result = json.load(open(tmp_path / "result.json"))
    assert result["pairs_missed"] == []
    assert result["pairs_already_done"] == [{"pair": "LED_P/LED_N", "p": "LED_P", "n": "LED_N",
                                            "reason": "pairs_stage_complete"}]
    assert "pairs already done 1" in r["line"] and "pair router missed" not in r["line"]


def test_pairs_missed_excludes_stage_complete_and_locked_connected(monkeypatch):
    rows = [{"pair": f"{stem}_P/{stem}_N", "p": f"{stem}_P", "n": f"{stem}_N", "reason": "not_recognised"}
            for stem in ("A", "B", "C")]
    report = {"pairs": [{"pair": ["A_P", "A_N"], "status": "complete"},
                        {"pair": ["C_P", "C_N"], "status": "handoff"}]}
    monkeypatch.setattr(K, "_input_locked_connected", lambda board, opens, pairs: {frozenset(("B_P", "B_N"))})
    missed, done = K._partition_missed(rows, report, "input.kicad_pcb", {"C_N": 1})
    assert [row["pair"] for row in missed] == ["C_P/C_N"]
    assert [(row["pair"], row["reason"]) for row in done] == [("A_P/A_N", "pairs_stage_complete"),
                                                               ("B_P/B_N", "input_connected_locked")]


def test_input_locked_connected_requires_copper_locks_and_no_open_leg(monkeypatch):
    class Track:
        def __init__(self, net, locked=True):
            self.net, self.locked = net, locked

        def GetNetname(self):
            return self.net

        def IsLocked(self):
            return self.locked

    tracks = [Track("A_P"), Track("A_N"), Track("B_P"), Track("B_N", False), Track("C_P"), Track("C_N"), Track("D_P")]
    board = types.SimpleNamespace(GetTracks=lambda: tracks)
    monkeypatch.setattr(K, "_pcb", lambda: types.SimpleNamespace(LoadBoard=lambda path: board))
    pairs = {frozenset((f"{stem}_P", f"{stem}_N")) for stem in ("A", "B", "C", "D")}
    done = K._input_locked_connected("synthetic.kicad_pcb", {"C_N": 1}, pairs)
    assert done == {frozenset(("A_P", "A_N"))}


def test_rails_step_before_diff_and_zone_nets_excluded(tmp_path, monkeypatch):
    import fluxplace.gaps as GP
    c, calls, runner = _ctx(tmp_path, monkeypatch, [0, 0, 0, 0])
    c.cons["power"] = {"VBAT": {"max_current_ma": 2000, "max_v": 4.2}, "GND": {"max_current_ma": 500}}
    monkeypatch.setattr(GP, "_zone_nets", lambda b: {"GND"})        # GND has its plane zone: not a rail
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    names = [s["step"] for s in r["steps"]]
    assert names == ["plane-GND", "rails-pour", "pairgroups", "route"]      # VBAT is a pour rail (PHASE3E 5)
    rails_cmd = calls[1]
    assert os.path.basename(rails_cmd[1]) == "route.py"
    assert rails_cmd[rails_cmd.index("--nets") + 1:rails_cmd.index("--power-nets")] == ["VBAT"]
    assert rails_cmd[rails_cmd.index("--power-nets") + 1] == "VBAT"
    assert r["rails"]["routed"] == ["VBAT"] and r["rails"]["widths"]["VBAT"] > 0.15
    # the same rail on a board with a zone of its net: no rails step, no rail width
    c2, calls2, runner2 = _ctx(tmp_path, monkeypatch, [0, 0, 0])
    c2.cons["power"] = c.cons["power"]
    monkeypatch.setattr(GP, "_zone_nets", lambda b: {"GND", "VBAT"})
    r2 = K.run(c2, 5, str(tmp_path / "in.kicad_pcb"), runner2)
    assert [s["step"] for s in r2["steps"]] == ["plane-GND", "pairgroups", "route"] and r2["rails"]["routed"] == []


def test_no_rails_no_step(tmp_path, monkeypatch):
    import fluxplace.gaps as GP
    c, calls, runner = _ctx(tmp_path, monkeypatch, [0, 0, 0])
    monkeypatch.setattr(GP, "_zone_nets", lambda b: {"GND"})
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert [s["step"] for s in r["steps"]] == ["plane-GND", "pairgroups", "route"]


def test_pour_runs_before_final_measurement(tmp_path, monkeypatch):
    from fluxplace import power as PWR
    c, calls, runner = _ctx(tmp_path, monkeypatch, [0, 0, 0])
    events = []
    monkeypatch.setattr(PWR, "pour_rails", lambda b, cons, out, layers=None: events.append("pour") or {"added": [], "dropped": False, "skipped": {}})
    real = K.RC.open_by_net
    monkeypatch.setattr(K.RC, "open_by_net", lambda b, **k: events.append("measure") or real(b, **k))
    K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert events[-2:] == ["pour", "measure"]


def test_rail_refused_and_inner_report(tmp_path, monkeypatch):
    import json
    summ = tmp_path / "5-rails.json"
    summ.write_text(json.dumps({"failed_single": [{"net": "VBAT", "pad_entry_refused": {"span_mm": 1.0}},
                                                  {"net": "SIG", "pad_entry_refused": {}}]}))
    rows = [{"step": "rails", "summary": str(summ)}]
    assert K._rail_refused(["VBAT", "V5"], rows) == ["VBAT"]
    assert K._rail_refused(["VBAT"], [{"step": "rails", "summary": None}]) == []
    monkeypatch.setattr(K, "_rail_inner", K._rail_inner)
    from fluxplace import power as PWR
    monkeypatch.setattr(PWR, "rail_inner_mm", lambda b, nets: {"VBAT": 3.2})
    assert K._rail_inner("x", ["VBAT"]) == {"VBAT": 3.2}


def test_ipc_outer_routes_non_pour_rail_on_all_signal_layers(tmp_path, monkeypatch):
    """PHASE3E 5: outer-only (ipc-outer) is for pour rails; a non-pour 0.15 mm rail is routed on every signal layer."""
    from fluxplace import power as PWR, gaps as GP
    c, calls, runner = _ctx(tmp_path, monkeypatch, [0, 0, 0, 0, 0])
    c.cons["route"] = {"power_spine": "ipc-outer", "whole_post": False}
    c.cons["power"] = {"V5A": {"pour": True}, "SIG5": {}}
    sig = ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"]
    monkeypatch.setattr(K, "plane_plan", lambda b: (sig, [("GND", "In1.Cu")]))
    monkeypatch.setattr(GP, "_zone_nets", lambda b: set())
    monkeypatch.setattr(PWR, "route_widths", lambda cons, **k: {"V5A": 1.0, "SIG5": 0.15})
    monkeypatch.setattr(PWR, "is_pour", lambda cons, n: n == "V5A")
    monkeypatch.setattr(PWR, "pour_rails", lambda *a, **k: {"added": [], "dropped": False, "skipped": {}})
    monkeypatch.setattr(K, "_rail_inner", lambda b, n: {})
    monkeypatch.setattr(K, "_rail_refused", lambda n, rows: [])
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    names = [s["step"] for s in r["steps"]]
    assert "rails-pour" in names and "rails" in names
    rails_cmds = {n: calls[names.index(n)] for n in ("rails-pour", "rails")}
    layers = lambda cmd: cmd[cmd.index("--layers") + 1:next(i for i in range(cmd.index("--layers") + 1, len(cmd)) if cmd[i].startswith("--"))]
    assert layers(rails_cmds["rails-pour"]) == ["F.Cu", "B.Cu"]
    assert layers(rails_cmds["rails"]) == sig
    assert "V5A" in rails_cmds["rails-pour"] and "SIG5" in rails_cmds["rails"]


def test_pairgroups_budget_and_failed_legs_on_exception(tmp_path, monkeypatch):
    """PHASE3E 4: budget = PER_PAIR_S x incomplete pairs (capped); a crash puts every pair leg in failed_legs."""
    import types as T
    from fluxplace import pairsonly as PO, pairaudit as PA, netclass as NC, routediag as RD, constraints as C
    src, dst = str(tmp_path / "s.kicad_pcb"), str(tmp_path / "d.kicad_pcb")
    open(src, "w").write("x")
    c = T.SimpleNamespace(cons={}, spec=None)
    monkeypatch.setattr(PA, "pair_list", lambda _c: [("A_P", "A_N"), ("B_P", "B_N")])
    monkeypatch.setattr(NC, "classify", lambda *a, **k: None)
    monkeypatch.setattr(C, "not_pairs", lambda cons: set())
    monkeypatch.setattr(K.RC, "text_open", lambda B: {})
    monkeypatch.setattr(PA, "audit", lambda B, pairs, cons, opn: {p: {"status": "open"} for p in pairs})
    seen = []
    monkeypatch.setattr(PO, "regroup", lambda *a, log=None, budget_s=None, **k: seen.append(budget_s) or {"failed": [], "groups": {}})
    monkeypatch.setattr(RD, "load", lambda p: None)
    failed = set()
    K._run_pairgroups(c, str(tmp_path), src, dst, failed, 10_000.0)
    assert seen == [2 * PO.PER_PAIR_S]
    K._run_pairgroups(c, str(tmp_path), src, dst, failed, 30.0)
    assert seen[-1] == 30.0
    monkeypatch.setattr(RD, "load", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    failed = set()
    K._run_pairgroups(c, str(tmp_path), src, dst, failed, 100.0, pair_legs={"A_P", "A_N", "B_P", "B_N"})
    assert failed == {"A_P", "A_N", "B_P", "B_N"} and os.path.exists(dst)


def test_diff_step_carries_family_width_on_outer_layers_only(tmp_path, monkeypatch):
    # PHASE3G G3b: a controlled family is routed at its table width on the layers whose width and gap equal the
    # outer layer's; a net with no family keeps the signal width (no --track-width override) on all signal layers
    import pytest
    from fluxplace import zgeom as Z, pairs as PS, routediag as RD, review as RV
    from test_zgeom import ROWS, SIG, PLANES, CONS
    board = tmp_path / "b.kicad_pcb"
    board.write_text("")
    monkeypatch.setattr(RV, "parse_stackup_text", lambda raw: ROWS)
    monkeypatch.setattr(PS, "pair_layers", lambda B, cons: (SIG, PLANES))
    monkeypatch.setattr(RD, "load", lambda p: None)
    groups = K.diff_groups(CONS, str(board), ["ETH_TX_P", "ETH_TX_N", "FOO_1"], SIG)
    by = {tuple(sorted(ns)): (lay, w, g) for ns, lay, w, g in groups}
    lay, w, g = by[("ETH_TX_N", "ETH_TX_P")]
    tab = Z.family_table(CONS, ROWS, PLANES, SIG, "ETH_", board=str(board))
    assert "In2.Cu" not in lay and "In3.Cu" not in lay and set(lay) == {"F.Cu", "B.Cu"}
    assert w == pytest.approx(tab["F.Cu"]["w"], abs=1e-4) and g == pytest.approx(tab["F.Cu"]["gap"], abs=1e-4)
    assert by[("FOO_1",)] == (SIG, None, None)
    base = ["--track-width", "0.15", "--escalation", "off"]
    cmd = K.cmd_diff("py", "/r", "s", "o", ["ETH_TX_P", "ETH_TX_N"], lay, base, None, width=w, gap=g)
    assert cmd.count("--track-width") == 1 and cmd[cmd.index("--track-width") + 1] == f"{w:g}"
    assert cmd[cmd.index("--diff-pair-gap") + 1] == f"{g:g}"
    assert cmd[cmd.index("--layers") + 1:cmd.index("--layers") + 1 + len(lay)] == lay
    plain = K.cmd_diff("py", "/r", "s", "o", ["FOO_1"], SIG, base, None)
    assert plain.count("--track-width") == 1 and plain[plain.index("--track-width") + 1] == "0.15"
    assert "--diff-pair-gap" not in plain


def test_rf_nets_route_before_the_rest(tmp_path, monkeypatch):
    import fluxplace.gaps as GP
    c, calls, runner = _ctx(tmp_path, monkeypatch, [0, 0, 0, 0])
    monkeypatch.setattr(K, "_all_nets", lambda b: ["USB_DP", "USB_DM", "X", "BT_SW_RFC", "RF_EN"])
    monkeypatch.setattr(GP, "_zone_nets", lambda b: {"GND"})
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert [s["step"] for s in r["steps"]] == ["plane-GND", "pairgroups", "rf", "route"]    # RF_EN is a control line, not RF
    rf = calls[1]
    assert rf[rf.index("--nets") + 1:rf.index("--layers")] == ["BT_SW_RFC"]
