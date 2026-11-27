"""PLACEGAPS phase 1: class-ranked judge key, verdict fail on an hs / rf open, ghost-open diagnosis (confirm, corridor, blockers),
review.is_rf port tokens, the bounded stock lookups (per-request cap, host negative cache, failed-lookup cache)."""
import os
import sys
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fluxplace import placegaps as PG, placetour as PT, redact as R, review as RV  # noqa: E402
from test_placegaps_core import ctx, fake_B, part  # noqa: E402


def test_class_key_ranks_hs_above_any_plain_opens(monkeypatch):
    monkeypatch.setattr(PT, "judge_key", lambda routed, blame, cons=None: (len(blame["open"]), 0, 0.0, 0, 0.0))
    monkeypatch.setattr(PG, "open_terms", lambda board, blame, cons, spec: (blame["hs"], 0, 0))
    one_hs = PT.class_key("r", {"open": [1], "hs": 1})
    five_plain = PT.class_key("r", {"open": [1] * 5, "hs": 0})
    assert five_plain < one_hs and one_hs[0] == 1 and len(one_hs) == len(PT.KEY_NAMES)
    assert PT.class_key(None, {"open": [], "hs": 0})[0] == PT.WORST


def test_class_key_unclassable_ranks_worst(monkeypatch, capsys):
    monkeypatch.setattr(PT, "judge_key", lambda routed, blame, cons=None: (2, 0, 0.0, 0, 0.0))
    monkeypatch.setattr(PG, "open_terms", lambda *a: 1 / 0)
    assert PT.class_key("r", {"open": []})[:3] == (PT.WORST,) * 3 and "not classed" in capsys.readouterr().out


@pytest.mark.parametrize("cause,want", [("blocked", "fail"), ("order", "warn"), ("unconfirmed", "fail"), (None, "fail")])
def test_stage_fails_on_an_hs_open_unless_the_router_alone_routed_it(monkeypatch, tmp_path, cause, want):
    from test_placetour import _script_stage, _stage_ctx
    keys = {"input": (5, 1, 0.5, 0, 1.0), "A": (3, 0, 0.0, 0, 0.5)}
    board = _script_stage(monkeypatch, tmp_path, [["A"], []], keys)
    g = PG.gap("ghost-open", "P/N", "hs-pair", 1, nets=["P", "N"])
    g.update(blockers=["R1"], **({"cause": cause} if cause else {}))
    monkeypatch.setattr(PG, "detect_stage", lambda *a, **k: {"gaps": [g], "line": "gaps"})
    r = PT.stage(_stage_ctx(tmp_path), 3, board)
    assert r["verdict"] == want and "N/P ghost-unrouted" in r["line"]
    assert any(x.startswith("FAIL: hs-pair N/P" if want == "fail" else "WARN: hs-pair N/P") for x in r["next"])


def test_stage_without_critical_gap_passes(monkeypatch, tmp_path):
    from test_placetour import _script_stage, _stage_ctx
    board = _script_stage(monkeypatch, tmp_path, [["A"], []], {"input": (5, 1, 0.5, 0, 1.0), "A": (3, 0, 0.0, 0, 0.5)})
    monkeypatch.setattr(PG, "detect_stage", lambda *a, **k: {"gaps": [PG.gap("ghost-open", "S", "plain", 1)], "line": "gaps"})
    assert PT.stage(_stage_ctx(tmp_path), 3, board)["verdict"] == "pass"


def test_report_labels_match_the_class_key(tmp_path):
    key0, key1 = (1, 1, 0, 9, 7, 3.9, 810, 92.6), (0, 1, 0, 5, 7, 3.9, 800, 90.0)
    PT._report(str(tmp_path / "r.md"), PT.params({}), key0, key1, [], set(), [], [])
    md = open(tmp_path / "r.md").read()
    assert "| hs-pair opens | 1 | 0 |" in md and "| judge opens (signal) | 9 | 5 |" in md and "| detour sum | 92.6 | 90.0 |" in md


def test_diagnosis_crash_keeps_the_gaps(monkeypatch, tmp_path):
    bl = {"open": [{"net": "S1", "refs": ["U1"]}, {"net": "RFX", "refs": ["C1"]}], "costly": [], "hot": [], "turned": []}
    monkeypatch.setattr(PG, "_net_class", lambda c: lambda n: ("rf", None) if n == "RFX" else ("plain", None))
    monkeypatch.setattr(PG, "diagnose", lambda *a, **k: 1 / 0)
    r = PG.detect_stage("placetour", "/nonexistent.kicad_pcb", {}, {}, str(tmp_path), blame=bl, printer=None, kinds=["ghost-open"])
    assert r["counts"]["ghost-open"] == 2 and "diagnose" in r["errors"]
    rf = PG.critical(r["gaps"])
    assert [g["class"] for g in rf] == ["rf"] and rf[0]["cause"] == "unconfirmed"          # warn, never a silent pass or a crash


def test_corridor_failure_of_one_gap_is_unconfirmed(monkeypatch):
    from fluxplace import routediag as RDG
    monkeypatch.setattr(RDG, "diagnose", lambda B, nets, cons, spec: {"nets": []})
    monkeypatch.setattr(PG, "corridor", lambda c, g: 1 / 0)
    g = PG.gap("ghost-open", "N1", "hs-pair", 1, nets=["N1"])
    c = gctx(board_with_cut())
    PG.diagnose(c, [g], confirm=lambda n: set())
    assert g["cause"] == "unconfirmed" and g["blockers"] == [] and "corridor" in c["errors"]
    assert "corridor ? mm" in PG.diagnosis_table([g])


@pytest.mark.parametrize("net,pins,rf", [
    ("BT_SW_RFC", (), True), ("BT_SW_RF2", (), True), ("RF_GNSS", (), True), ("RF_INT_SW", (), True), ("BT1_RF", (), True),
    ("/RF/ANT_OUT", (), True), ("RF_EXT", (), True),
    ("RFU_PIN", (), False), ("RFID", (), False), ("ANT_SEL", (), False), ("ANT_DET", (), False), ("PWRFAIL", (), False),
    ("RF_EN", (), False), ("RFC_CTRL", (), False), ("SW_RF1_SEL", (), False), ("RF_3V3", (), False), ("VCC_RF", (), False),
    ("RF_GND", (), False), ("RF_1V8", (), False),
    ("X_1", ("RFC",), True), ("X_1", ("RFIO",), True), ("X_1", ("GNSS_ANT",), True), ("X_1", ("RF_IN",), True), ("X_1", ("RF",), True),
    ("X_1", ("GND",), False), ("X_1", ("RF_EN",), False), ("X_1", ("ANT_SEL",), False), ("X_1", ("RFC_CTRL",), False),
    ("X_1", ("VCC_RF",), False), ("X_1", ("RFU",), False)])
def test_is_rf_port_tokens_pin_context_and_control_exclusions(net, pins, rf):
    assert RV.is_rf(net, pins) is rf


def board_with_cut():
    B = fake_B()
    B["pads"] = [{"ref": "U1", "num": "1", "x": 10.0, "y": 10.0, "net": "N1"}, {"ref": "J1", "num": "1", "x": 40.0, "y": 10.0, "net": "N1"}]
    B["parts"]["J1"] = part(40, 10)
    B["parts"]["X1"] = part(25, 10, court=(24, 9, 26, 11))          # small part on the flight line
    B["parts"]["X2"] = part(30, 10, court=(28, 8, 33, 12))          # bigger part on the flight line
    B["parts"]["X3"] = part(25, 30)                                  # off the line
    B["parts"]["X4"] = part(20, 10, side="B.Cu", court=(19, 9, 21, 11))   # other side
    return B


def gctx(B):
    return ctx(B=B, cons={}, spec={})


def test_corridor_and_blockers_smallest_first_endpoints_and_other_side_excluded(monkeypatch):
    from fluxplace import placetour as PTm, routediag as RDG
    monkeypatch.setattr(PTm, "_movable", lambda B, cons, P: ["X1", "X2", "X3"])
    monkeypatch.setattr(PG, "_rules", lambda c, g: (0.15, 0.15, 0.45))
    c = gctx(board_with_cut())
    g = PG.gap("ghost-open", "N1", "plain", 2, refs=["J1", "U1"], nets=["N1"])
    corr = PG.corridor(c, g)
    assert corr["len_mm"] == 30.0 and corr["width_mm"] == 0.45
    bl = PG.blockers(c, g, corr)
    assert [b["ref"] for b in bl] == ["C1", "X1", "X2"] and [b["movable"] for b in bl] == [False, True, True]


def test_diagnose_cause_from_confirm(monkeypatch):
    from fluxplace import placetour as PTm, routediag as RDG
    monkeypatch.setattr(PTm, "_movable", lambda B, cons, P: ["X1"])
    monkeypatch.setattr(PG, "_rules", lambda c, g: (0.15, 0.15, 0.45))
    monkeypatch.setattr(RDG, "diagnose", lambda B, nets, cons, spec: {"nets": [{"net": n, "causes": [{"cause": "LONG_HAUL"}]} for n in nets]})
    mk = lambda cls: PG.gap("ghost-open", "N1", cls, 2, refs=["J1", "U1"], nets=["N1"])
    g1 = mk("hs-pair"); PG.diagnose(gctx(board_with_cut()), [g1], confirm=lambda nets: set())
    assert g1["cause"] == "order" and g1["causes"] == ["LONG_HAUL"] and g1["blockers"] == ["C1", "X1", "X2"] and g1["blockers_movable"] == ["X1"]
    g2 = mk("rf"); PG.diagnose(gctx(board_with_cut()), [g2], confirm=lambda nets: set(nets))
    assert g2["cause"] == "blocked"
    g3 = mk("hs-pair"); PG.diagnose(gctx(board_with_cut()), [g3], confirm=None)
    assert g3["cause"] == "unconfirmed"
    g4 = mk("plain"); calls = []
    PG.diagnose(gctx(board_with_cut()), [g4], confirm=lambda nets: calls.append(nets) or set())
    assert g4["cause"] == "not-confirmed" and calls == []                       # plain nets never cost a router run
    c = gctx(board_with_cut()); g5 = mk("hs-pair")
    PG.diagnose(c, [g5], confirm=lambda nets: 1 / 0)
    assert g5["cause"] == "unconfirmed" and "confirm" in c["errors"]
    assert "N1: hs-pair, unconfirmed" in PG.diagnosis_table([g5])


def test_confirm_runs_router_on_the_gap_nets_only(monkeypatch, tmp_path):
    from fluxplace import gaps as G
    seen = {}
    done = {"complete": True}
    monkeypatch.setattr(PT.LA, "lean_krt", lambda a, b, d, m, extra=(), only=None, info=None: (seen.update(only=only), info.update(done), str(tmp_path / "r.kicad_pcb"))[2])
    monkeypatch.setattr(G, "_krt_open", lambda board, cp: {"A", "OTHER"})
    from types import SimpleNamespace
    run = PT._confirm(SimpleNamespace(constraints=None), "b", str(tmp_path), PT.params({}))
    assert run(["A", "B"]) == {"A"} and seen["only"] == {"A", "B"}
    monkeypatch.setattr(PT.LA, "lean_krt", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        run(["A"])
    done["complete"] = False                                                     # the router hit its cap: a checkpoint is not an answer
    monkeypatch.setattr(PT.LA, "lean_krt", lambda a, b, d, m, extra=(), only=None, info=None: info.update(done) or "ckpt.kicad_pcb")
    with pytest.raises(RuntimeError, match="cap"):
        run(["A"])


# ------------------------------------------------------------------ bounded lookups
def test_http_timeout_is_capped_and_dead_host_is_skipped(monkeypatch):
    calls, seen = [], []
    def boom(req, timeout=None):
        calls.append(1); seen.append(timeout)
        raise urllib.error.URLError("timed out")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    req = urllib.request.Request("https://api.example.com/v1")
    for _ in range(2):
        with pytest.raises(urllib.error.URLError):
            R.urlopen(req, timeout=300)
    assert seen == [15, 15]                                                      # the per-request cap, whatever the caller asked
    with pytest.raises(urllib.error.URLError, match="negative cache"):
        R.urlopen(req, timeout=30)
    assert len(calls) == 2                                                       # the third did not touch the network
    other = urllib.request.Request("https://api.other.com/v1")
    with pytest.raises(urllib.error.URLError, match="timed out"):
        R.urlopen(other)
    assert len(calls) == 3                                                       # another host is unaffected


def test_http_status_is_an_answer_not_an_outage(monkeypatch):
    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "slow down", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    req = urllib.request.Request("https://api.example.com/v1")
    for _ in range(5):
        with pytest.raises(urllib.error.HTTPError):
            R.urlopen(req)


def test_down_host_comes_back_after_the_window(monkeypatch):
    monkeypatch.setenv("FLUXPLACE_HTTP_DOWN_S", "0.05")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("x")))
    req = urllib.request.Request("https://api.example.com/v1")
    for _ in range(2):
        with pytest.raises(urllib.error.URLError):
            R.urlopen(req)
    time.sleep(0.08)
    with pytest.raises(urllib.error.URLError, match="^<urlopen error x>$|x"):
        R.urlopen(req)                                                           # tried again (not "negative cache")


def test_partdata_failed_lookup_is_remembered_briefly(monkeypatch, tmp_path):
    from fluxplace import partdata as PD
    n = []
    monkeypatch.setattr(PD, "_mouser_details", lambda mpn, creds: n.append(mpn) or (_ for _ in ()).throw(RuntimeError("down")))
    out = PD.fetch(["P1", "P2"], cache_dir=str(tmp_path), log=lambda *a: None, creds={"MOUSER_API_KEY": "k"})
    assert out == {"P1": None, "P2": None} and len(n) == 2
    PD.fetch(["P1", "P2"], cache_dir=str(tmp_path), log=lambda *a: None, creds={"MOUSER_API_KEY": "k"})
    assert len(n) == 2                                                           # second run: no network
    import json
    c = json.load(open(tmp_path / PD.CACHE_NAME))
    c["P1"]["_t"] -= PD.NEG_TTL + 1
    json.dump(c, open(tmp_path / PD.CACHE_NAME, "w"))
    PD.fetch(["P1"], cache_dir=str(tmp_path), log=lambda *a: None, creds={"MOUSER_API_KEY": "k"})
    assert len(n) == 3                                                           # expired: tried again


def test_sourcing_failed_lookup_is_unchecked_not_a_pass_and_not_repeated(monkeypatch, tmp_path):
    from fluxplace import sourcing as S
    n = []
    monkeypatch.setattr(S, "credentials", lambda: {"MOUSER_API_KEY": "k"})
    monkeypatch.setattr(S, "_mouser", lambda mpn, creds: n.append(mpn) or (_ for _ in ()).throw(RuntimeError("down")))
    kw = dict(cache_dir=str(tmp_path), log=lambda *a: None, qty=10)
    rep, _c = S.check({"P1": ["U1"]}, **kw)
    rep2, _c = S.check({"P1": ["U1"]}, **kw)
    assert len(n) == 1 and rep["P1"]["verdict"] == rep2["P1"]["verdict"] != "OK"


def test_partdata_budget_leaves_the_rest_unchecked_and_uncached(monkeypatch, tmp_path):
    from fluxplace import partdata as PD
    import json
    monkeypatch.setenv("FLUXPLACE_LOOKUP_BUDGET_S", "0.01")
    seen = []
    def slow(mpn, creds):
        seen.append(mpn); time.sleep(0.03)
        return {"package": "P", "temp_min": 0, "temp_max": 1, "source": "mouser"}
    monkeypatch.setattr(PD, "_mouser_details", slow)
    logs = []
    out = PD.fetch(["A", "B", "C"], cache_dir=str(tmp_path), log=logs.append, creds={"MOUSER_API_KEY": "k"})
    assert seen == ["A"] and out["A"] and out["B"] is None and out["C"] is None
    assert any("UNCHECKED" in str(x) for x in logs)
    assert sorted(json.load(open(tmp_path / PD.CACHE_NAME))) == ["A"]            # B, C were not asked: not cached as failures


def test_sourcing_budget_is_unchecked_not_a_pass(monkeypatch, tmp_path):
    from fluxplace import sourcing as S
    monkeypatch.setenv("FLUXPLACE_LOOKUP_BUDGET_S", "0.01")
    monkeypatch.setattr(S, "credentials", lambda: {"MOUSER_API_KEY": "k"})
    def slow(mpn, creds):
        time.sleep(0.03)
        return (500, "in stock", [], "")
    monkeypatch.setattr(S, "_mouser", slow)
    rep, _c = S.check({"A1": ["U1"], "B1": ["U2"]}, cache_dir=str(tmp_path), log=lambda *a: None, qty=1)
    assert rep["B1"]["verdict"] not in ("OK", "LOW")
    import json
    assert "B1" not in json.load(open(tmp_path / S.CACHE_NAME))                   # not asked: not remembered as a failure


def test_negative_entry_keeps_the_good_half_and_skips_transient_errors(monkeypatch, tmp_path):
    from fluxplace import sourcing as S
    import json
    monkeypatch.setattr(S, "credentials", lambda: {"MOUSER_API_KEY": "k", "DIGIKEY_CLIENT_ID": "i", "DIGIKEY_CLIENT_SECRET": "s"})
    monkeypatch.setattr(S, "_dk_token", lambda c: "t")
    monkeypatch.setattr(S, "_dk", lambda mpn, creds, tok, on_token=None: (500, "in stock", [], ""))
    err = {"e": RuntimeError("down")}
    def mouser(mpn, creds):
        raise err["e"]
    monkeypatch.setattr(S, "_mouser", mouser)
    kw = dict(cache_dir=str(tmp_path), log=lambda *a: None, qty=1)
    S.check({"P1": ["U1"]}, **kw)
    ent = json.load(open(tmp_path / S.CACHE_NAME))["P1"]
    assert ent["_neg"] and ent["errs"] == ["Mouser"] and ent["dk"][0] == 500          # the DigiKey half survives
    for e, name in ((R.HostSkipped("api.mouser.com skipped: negative cache"), "P2"),
                    (urllib.error.HTTPError("https://x", 429, "slow", {}, None), "P3")):
        err["e"] = e
        S.check({name: ["U1"]}, **kw)
        assert name not in json.load(open(tmp_path / S.CACHE_NAME))                  # a skipped host / a 429 is never cached


def test_partdata_does_not_cache_a_skipped_host(monkeypatch, tmp_path):
    from fluxplace import partdata as PD
    import json
    monkeypatch.setattr(PD, "_mouser_details", lambda mpn, creds: (_ for _ in ()).throw(R.HostSkipped("skipped")))
    PD.fetch(["P1"], cache_dir=str(tmp_path), log=lambda *a: None, creds={"MOUSER_API_KEY": "k"})
    assert "P1" not in json.load(open(tmp_path / PD.CACHE_NAME))


def test_lookup_limits_come_from_parts_table_env_overrides_bad_values_fall_back(monkeypatch):
    assert R.setting("http_timeout_s") == 15.0 and R.setting("http_down_after") == 2 and R.setting("lookup_budget_s") == 120.0
    R.configure({"parts": {"http_timeout_s": 7, "http_down_after": 3, "lookup_budget_s": "90", "other": 1}})
    assert R.setting("http_timeout_s") == 7.0 and R.setting("http_down_after") == 3 and R.setting("lookup_budget_s") == 90.0
    monkeypatch.setenv("FLUXPLACE_HTTP_TIMEOUT", "4")
    assert R.setting("http_timeout_s") == 4.0                                        # env is the override
    monkeypatch.setenv("FLUXPLACE_HTTP_TIMEOUT", "fast")
    assert R.setting("http_timeout_s") == 7.0                                        # bad env: the [parts] value
    R.configure({"parts": {"http_timeout_s": "slow"}})
    assert R.setting("http_timeout_s") == 15.0                                       # bad everywhere: the default, no raise
    monkeypatch.setenv("FLUXPLACE_HTTP_DOWN_AFTER", "-3")
    assert R.setting("http_down_after") == 2


def test_checkpoint_is_atomic_and_cache_never_falls_back_to_cwd(monkeypatch, tmp_path):
    from fluxplace import sourcing as S
    import json
    path = tmp_path / "c.json"
    S.write_json(str(path), {"a": 1})
    S.write_json(str(path), {"a": 2})
    assert json.load(open(path)) == {"a": 2} and sorted(os.listdir(tmp_path)) == ["c.json"]      # no .tmp left behind
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FLUXPLACE_CACHE_HOME", raising=False)      # the conftest fixture's shared cache; this test is about the HOME default
    monkeypatch.chdir(tmp_path)
    d = S.cache_home(None)
    assert d == str(tmp_path / "home" / ".cache" / "fluxplace") and os.path.isdir(d)
    assert S.cache_home(str(tmp_path)) == str(tmp_path)


def test_lean_krt_routes_critical_nets_first_then_the_rest_on_that_copper(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from fluxplace import lookahead as LA, krtchain as KC, routediag as RDG
    board = tmp_path / "in.kicad_pcb"
    board.write_text("x")
    cmds = []
    monkeypatch.setattr(LA, "_ghost_copy", lambda b, d: (os.makedirs(d, exist_ok=True), str(tmp_path / "copy.kicad_pcb"), open(tmp_path / "copy.kicad_pcb", "w").write("c"))[1])
    monkeypatch.setattr(LA, "_signal_nets", lambda c: (["F.Cu", "B.Cu"], {"GND"}))
    monkeypatch.setattr(LA, "_ghost_stats", lambda *a, **k: None)
    monkeypatch.setattr(LA, "critical_first", lambda copy, cons, spec, nets: ["RFX", "P"])
    monkeypatch.setattr(KC, "base_flags", lambda R, d: [])
    monkeypatch.setattr(LA.RR if hasattr(LA, "RR") else __import__("fluxplace.routerules", fromlist=["x"]), "rules_for", lambda *a, **k: {})
    monkeypatch.setattr(RDG, "load", lambda p: {"pads": [{"net": n} for n in ("RFX", "RFX", "P", "P", "S", "S", "GND", "GND")]})
    monkeypatch.setattr(__import__("fluxplace.routeconn", fromlist=["x"]), "text_open", lambda B, *a, **k: {})
    envs = {}
    def fake_run(cmd, cwd, outdir, tag, out, cap, env=None):
        envs[tag] = env
        cmds.append((tag, cmd))
        open(out, "w").write(tag)
        return True, 1.0
    monkeypatch.setattr(LA, "_run_capped", fake_run)
    a = SimpleNamespace(constraints=None, spec=None, router_py="py", router_dir="/r", track=None, clearance=None)
    info = {}
    out = LA.lean_krt(a, str(board), str(tmp_path / "o"), 1.0, info=info)
    assert [t for t, _c in cmds] == ["krt-first-fwd", "krt"] and out.endswith("krt-lean.kicad_pcb")      # B11 / R124: fwd closed every critical net, no rev run
    nets = lambda c: c[c.index("--nets") + 1:c.index("--layers")]
    assert nets(cmds[0][1]) == ["RFX", "P"] and nets(cmds[1][1]) == ["S"]                  # critical first, then the rest (GND is a plane)
    assert dict(cmds)["krt-first-fwd"][2].endswith("in-fwd.kicad_pcb") and cmds[1][1][2].endswith("krt-first.kicad_pcb")   # the second pass starts from the first's copper
    assert envs["krt-first-fwd"] is None and envs["krt"]["KICAD_RIP_PREEXISTING"] == "0"                 # pass 2 may not rip pass 1
    assert "--keep-input-copper" in cmds[1][1] and info["complete"] and info["passes"] == 2 and info["first"] == ["RFX", "P"]
    assert info["first_pick"] == "fwd" and info["fragile"] == []
    cmds.clear()
    LA.lean_krt(a, str(board), str(tmp_path / "o2"), 1.0, only={"P"}, info=info)           # a diagnosis run stays one pass
    assert [t for t, _c in cmds] == ["krt"] and nets(cmds[0][1]) == ["P"] and info["passes"] == 1 and envs["krt"] is None


def test_critical_first_orders_hs_then_rf_then_power(monkeypatch):
    from fluxplace import lookahead as LA
    cls = {"A_P": "hs-pair", "RFX": "rf", "VBAT": "power", "S": "plain", "Q": "pair"}
    monkeypatch.setattr(PG, "_net_class", lambda c: lambda n: (cls[n], None))
    assert LA.critical_first("b", {}, {}, ["S", "VBAT", "RFX", "Q", "A_P"]) == ["A_P", "RFX", "VBAT"]
    monkeypatch.setattr(PG, "_net_class", lambda c: 1 / 0)
    assert LA.critical_first("b", {}, {}, ["S"]) == []                                      # unclassable: one pass as before


def test_target_must_be_gone_for_a_ghost_open_accept():
    hs = PG.gap("ghost-open", "P/N", "hs-pair", 1)
    other = PG.gap("ghost-open", "S1", "plain", 1)
    base = {"gaps": [hs, other], "state": {"illegal": 0, "open": 5}, "refusal": None}
    better_but_open = {"gaps": [hs], "state": {"illegal": 0, "open": 4}, "refusal": None}               # lower score, target still open
    ok, check, why = PG._accept(base, better_but_open, "ghost-open:P/N")
    assert (ok, check) == (False, "judge:target open") and "still open" in why
    routed = {"gaps": [other], "state": {"illegal": 0, "open": 4}, "refusal": None}
    assert PG._accept(base, routed, "ghost-open:P/N")[0] is True
    assert PG._accept(base, better_but_open, "support-far:C1")[0] is True                              # other kinds keep the score rule


def test_commit_keeps_every_carried_cause(monkeypatch, tmp_path):
    from test_placegaps_rungs import judge_world
    c, ev = judge_world(monkeypatch, tmp_path, {})
    ev.detect(["ghost-open"])
    for g in ev.gaps:
        g["cause"] = "order" if g["id"].endswith("HS_N") else "not-confirmed"
    tr = {"gaps": [PG.gap("ghost-open", "HS_N", "hs-pair", 1, nets=["HS_N"]), PG.gap("ghost-open", "S1", "plain", 1, nets=["S1"]),
                   PG.gap("ghost-open", "NEW", "plain", 1, nets=["NEW"])],
          "res": {"blame": {"open": []}, "key": [0] * 8}, "board": str(tmp_path / "base.kicad_pcb")}
    monkeypatch.setattr(PG, "diagnose", lambda ctx, gaps, conf: [g.update(cause="unconfirmed") for g in gaps])   # what a router-less diagnose says
    ev.commit(tr)
    by = {g["id"]: g["cause"] for g in ev.gaps}
    assert by == {"ghost-open:HS_N": "order", "ghost-open:S1": "not-confirmed", "ghost-open:NEW": "unconfirmed"}   # `order` is not turned into a rung target


def test_ladder_budget_stops_inside_the_rung_loop(monkeypatch):
    monkeypatch.setitem(PG.DISPATCH, "support-far", ["home", "home2"])
    calls = []
    def rung(g, c):
        calls.append(g["id"])
        return []
    monkeypatch.setitem(PG.RUNGS, "home", rung)
    monkeypatch.setitem(PG.RUNGS, "home2", rung)
    from test_placegaps_core import FakeEv
    t = iter(range(0, 10 ** 6, 100))
    out = PG.close({"cons": {}}, ["support-far"], FakeEv({"C1", "C2", "C3"}), clock=lambda: next(t), budget_min=1)
    assert out["stop"] == "ladder budget (time stop)" and len(calls) < 6


def test_ladder_budget_is_a_placetour_key(monkeypatch):
    assert PT.params({"placetour": {"enabled": True}})["ladder_budget_min"] == 15.0
    assert PT.params({"placetour": {"ladder_budget_min": 6}})["ladder_budget_min"] == 6.0


def test_close_ghost_gets_the_datasheet_dir_and_the_ladder_budget(monkeypatch, tmp_path):
    from types import SimpleNamespace
    seen = {}
    monkeypatch.setattr(PG, "JudgeEval", lambda ctx, *a, **k: seen.update(ds=ctx["ds_dir"]) or SimpleNamespace(board_path="x", blame={}, key=(0,) * 8))
    monkeypatch.setattr(PG, "close", lambda ctx, kinds, ev, stage="x", budget_min=None, **k: seen.update(b=budget_min) or {"accepted": [], "stop": "x"})
    PT._close_ghost(SimpleNamespace(), {}, {}, {"board": "b", "blame": {}, "key": (1,) * 8, "routed": "r"}, str(tmp_path),
                    PT.params({"placetour": {"ladder_budget_min": 7}}), {}, "b", str(tmp_path / "p.json"), "/ds")
    assert seen == {"ds": "/ds", "b": 7.0}


def test_ladder_moves_reach_the_report_and_removed_refs(monkeypatch, tmp_path):
    from test_placetour import _script_stage, _stage_ctx
    keys = {"input": (5, 1, 0.5, 0, 1.0), "A": (3, 0, 0.0, 0, 0.5)}
    board = _script_stage(monkeypatch, tmp_path, [["A"], []], keys)
    monkeypatch.setattr(PT, "class_key", lambda routed, blame, cons=None, spec=None: (1, 0, 0) + tuple(blame["k"]))
    orig_jobs = PT.LA._run_jobs
    monkeypatch.setattr(PT.LA, "_run_jobs", lambda *a, **k: [dict(r, key=[1, 0, 0] + r["key"][3:]) for r in orig_jobs(*a, **k)])   # an hs net stays open
    step = {"gap": "ghost-open:N1", "rung": "unblock", "candidate": "unblock-n1", "refs": ["R5"], "moves": {"R5": [2.0, 3.0, 90.0]}, "removed_refs": ["R5"]}
    best_after = {"board": str(tmp_path / "moved.kicad_pcb"), "blame": {"open": []}, "key": (0, 0, 0, 1, 0, 0.0, 0, 0.5), "routed": "/r"}
    open(best_after["board"], "w").write("ladder")
    monkeypatch.setattr(PT, "_close_ghost", lambda *a, **k: (best_after, {"accepted": [step], "refused": [], "stop": "done"}))
    monkeypatch.setattr(PG, "detect_stage", lambda *a, **k: {"gaps": [], "line": "gaps"})
    r = PT.stage(_stage_ctx(tmp_path), 3, board)
    md = open(os.path.join(tmp_path, "w", "3-placetour", "placetour.md")).read()
    assert "| ladder | unblock | R5 |" in md and "(2.0, 3.0, 90.0)" in md and "R5" in md.split("fan-out again)")[1]
    assert open(r["board"]).read() == "ladder"
