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


def test_class_key_unclassable_ranks_as_plain(monkeypatch, capsys):
    monkeypatch.setattr(PT, "judge_key", lambda routed, blame, cons=None: (2, 0, 0.0, 0, 0.0))
    monkeypatch.setattr(PG, "open_terms", lambda *a: 1 / 0)
    assert PT.class_key("r", {"open": []})[:3] == (0, 0, 0) and "not classed" in capsys.readouterr().out


@pytest.mark.parametrize("hs,want", [(True, "fail"), (False, "pass")])
def test_stage_fails_on_hs_open_and_passes_without(monkeypatch, tmp_path, hs, want):
    from test_placetour import _script_stage, _stage_ctx
    keys = {"input": (5, 1, 0.5, 0, 1.0), "A": (3, 0, 0.0, 0, 0.5)}
    board = _script_stage(monkeypatch, tmp_path, [["A"], []], keys)
    g = PG.gap("ghost-open", "P/N" if hs else "S", "hs-pair" if hs else "plain", 1, nets=["P", "N"] if hs else ["S"])
    g.update(cause="blocked", blockers=["R1"])
    monkeypatch.setattr(PG, "detect_stage", lambda *a, **k: {"gaps": [g], "line": "gaps"})
    r = PT.stage(_stage_ctx(tmp_path), 3, board)
    assert r["verdict"] == want
    if hs:
        assert "N/P ghost-unrouted" in r["line"] and any(x.startswith("FAIL: hs-pair N/P") for x in r["next"])


@pytest.mark.parametrize("net,pins,rf", [("BT_SW_RFC", (), True), ("BT_SW_RF2", (), True), ("RF_GNSS", (), True), ("RFU_PIN", (), False),
                                         ("RFID", (), False), ("ANT_SEL", (), False), ("PWRFAIL", (), False),
                                         ("X_1", ("RFC",), True), ("X_1", ("GND",), False), ("X_1", ("RF_IN",), True)])
def test_is_rf_port_tokens_and_pin_context(net, pins, rf):
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
    monkeypatch.setattr(PT.LA, "lean_krt", lambda a, b, d, m, extra=(), only=None: seen.update(only=only) or str(tmp_path / "r.kicad_pcb"))
    monkeypatch.setattr(G, "_krt_open", lambda board, cp: {"A", "OTHER"})
    from types import SimpleNamespace
    run = PT._confirm(SimpleNamespace(constraints=None), "b", str(tmp_path), PT.params({}))
    assert run(["A", "B"]) == {"A"} and seen["only"] == {"A", "B"}
    monkeypatch.setattr(PT.LA, "lean_krt", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
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
