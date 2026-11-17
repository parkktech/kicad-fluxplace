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
    c, calls, runner = _ctx(tmp_path, monkeypatch, [0, 0, 3])
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert [os.path.basename(x[1]) for x in calls] == ["route_planes.py", "route_diff.py", "route.py"]
    assert calls[1][calls[1].index("--nets") + 1:calls[1].index("--layers")] == ["USB_DM", "USB_DP"]
    assert r["verdict"] == "warn" and "USB_DP" in r["line"]
    assert "--json-out" in calls[1] and "--json-out" in calls[2]


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
