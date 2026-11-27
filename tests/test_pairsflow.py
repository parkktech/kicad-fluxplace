"""W29: the pairs gate defers findings a later stage owns (tune: skew; routers/close: open pairs) instead of dead-ending at exit 5;
coupled-preferred pair classification; heartbeat elapsed counts from the stage start. Review stays the final hard gate."""
import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from fluxplace import pipeline as PL, gaps as GP, pairs as PR, pairaudit as PA, netclass as NC, events, liveview as LV, opengate as OG, review as R
from test_pipeline import _board, _args


def _row(kind, p="ETH_L0_P", n="ETH_L0_N"):
    return {"id": f"{kind}:{p}", "kind": kind, "class": "hs-pair", "nets": [p, n], "at": None}


def _close(left, stop="packets"):
    return lambda board, work, *a, **k: {"stop": stop, "start": {"opens": 3}, "end": {"opens": 3}, "best": board, "left": left, "packets": []}


def _flow(tmp_path, monkeypatch, left, to_stage="tune", stop="packets"):
    b = _board(tmp_path)
    seen = []
    monkeypatch.setitem(PL.FUNCS, "pairs", lambda c, n, board: {"verdict": "pass", "line": "pairs ok", "board": board})
    monkeypatch.setitem(PL.FUNCS, "tune", lambda c, n, board: seen.append("tune") or {"verdict": "pass", "board": board})
    monkeypatch.setitem(PL.FUNCS, "critroute", lambda c, n, board: seen.append("critroute") or {"verdict": "pass", "board": board})
    monkeypatch.setattr(GP, "close", _close(left, stop))
    a = _args(tmp_path, b, flow="constructive", from_stage="pairs", to_stage=to_stage)
    return PL.run(a), PL.load_state(a.workdir), seen


def test_skew_only_findings_continue_to_tune(tmp_path, monkeypatch):
    rc, st, seen = _flow(tmp_path, monkeypatch, [_row("pair-skew"), _row("pair-skew", "ETH_L1_P", "ETH_L1_N")])
    assert rc == 0 and seen == ["tune"]
    g = st["stages"]["pairs"]["gate"]
    assert st["stages"]["pairs"]["verdict"] == "warn" and g["stop"] == "deferred" and len(g["deferred"]) == 2


def test_pair_open_only_findings_continue_when_a_router_follows(tmp_path, monkeypatch):
    rc, st, seen = _flow(tmp_path, monkeypatch, [_row("pair-open", "PCIE_TX_P", "PCIE_TX_N")], to_stage="critroute")
    assert rc == 0 and st["stages"]["pairs"]["gate"]["stop"] == "deferred" and seen == ["tune", "critroute"]


def test_owner_stage_beyond_the_run_range_still_defers(tmp_path, monkeypatch):
    rc, st, seen = _flow(tmp_path, monkeypatch, [_row("pair-open")], to_stage="tune")      # route/close follow in the flow, after this run
    assert rc == 0 and seen == ["tune"] and st["stages"]["pairs"]["gate"]["stop"] == "deferred"


def test_unowned_kind_keeps_the_gate_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(GP, "ai_answer_required", lambda cons: True)        # [ai] answer = required keeps exit 5
    rc, st, seen = _flow(tmp_path, monkeypatch, [_row("pair-skew"), _row("short")])
    assert rc == 5 and seen == [] and st["stages"]["pairs"]["verdict"] == "packets"


def test_deferred_left_rules():
    assert GP.deferred_left([_row("pair-skew")], ["tune"]) and GP.deferred_left([_row("pair-open")], ["close"])
    assert GP.deferred_left([], ["tune"]) is None
    assert GP.deferred_left([_row("pair-skew"), _row("short")], ["tune"]) is None


def test_review_still_fails_skew_and_open_pairs(tmp_path):
    from test_review import facts
    f = facts(net_pads={"ETH_L0_P": [], "ETH_L0_N": []},
              net_tracks={"ETH_L0_P": {"length": 35.1, "layers": {"F.Cu": 35.1}, "vias": 0, "segments": []},
                          "ETH_L0_N": {"length": 80.4, "layers": {"F.Cu": 80.4}, "vias": 0, "segments": []}})
    assert any(x["code"] == "PAIR_SKEW" and x["level"] == "FAIL" for x in R.check_pairs(f, {}))
    p = lambda ref, x: {"ref": ref, "num": "1", "x": x, "y": 0, "layers": ["F.Cu"]}
    m = {"fresh": 1, "saved": 1, "cli_fresh": 1, "stale": [], "connections": OG.connections({"ETH_L0_P": [[p("A", 0)], [p("B", 9)]]})}
    assert any(x["code"] == "UNROUTED" and x["level"] == "FAIL" for x in OG.findings(m))


# ---------------------------------------------------------------- B
def _pad(ref, num, net, x, y=0.0):
    return {"ref": ref, "num": num, "net": net, "x": x, "y": y}


def test_coupled_preferred_classifier():
    two = {"pads": [_pad("U1", "1", "MIC_P", 0), _pad("U1", "2", "MIC_N", 1), _pad("J1", "1", "MIC_P", 20), _pad("J1", "2", "MIC_N", 21)]}
    assert PR.coupled_preferred(two, {}, "MIC_P", "MIC_N") is None
    lone = {"pads": two["pads"] + [_pad("R1", "1", "MIC_P", 10)]}                    # a series passive on one leg only
    assert "unpaired pad(s) R1.1" in PR.coupled_preferred(lone, {}, "MIC_P", "MIC_N")
    one = {"pads": [_pad("T1", "1", "MIC_P", 0), _pad("T1", "2", "MIC_N", 1), _pad("C1", "1", "MIC_P", 50), _pad("C2", "1", "MIC_N", 90)]}
    assert "no two terminals" in PR.coupled_preferred(one, {}, "MIC_P", "MIC_N")
    assert PR.coupled_preferred(lone, {"pairs": {"MIC_": {"impedance_diff": 100}}}, "MIC_P", "MIC_N") is None   # controlled impedance: stays a pair
    eth = {"pads": [dict(q, net=q["net"].replace("MIC", "ETH_L0")) for q in lone["pads"]]}
    assert PR.coupled_preferred(eth, {}, "ETH_L0_P", "ETH_L0_N") is None            # high-speed family: stays a pair


def test_pair_list_skips_coupled_preferred():
    cls = {"A_P": {"class": "pair", "partner": "A_N"}, "A_N": {"class": "pair", "partner": "A_P"},
           "B_P": {"class": "pair", "partner": "B_N", "coupled_pref": "x"}, "B_N": {"class": "pair", "partner": "B_P", "coupled_pref": "x"}}
    assert PA.pair_list(cls) == [("A_N", "A_P")]


# ---------------------------------------------------------------- C
def test_heartbeat_elapsed_grows(tmp_path, monkeypatch):
    for k in (events.ENV_FILE, events.ENV_RUN, events.ENV_STAGE, LV.ENV_OPS, "FLUXPLACE_NESTED"):
        monkeypatch.delenv(k, raising=False)
    LV._narr.clear()
    events.init(tmp_path / "W", run="t")
    monkeypatch.setattr(LV, "NARR_S", 0.5)
    with LV.watching("pairs"):
        time.sleep(2.7)
    import json
    rows = [json.loads(l) for l in open(os.path.join(tmp_path / "W", "events.jsonl"))]
    secs = [int(r["text"].split(", ")[1].split(" s")[0]) for r in rows if r.get("kind") == "narration" and "so far" in r.get("text", "")]
    assert len(secs) >= 3 and secs[-1] > secs[0] and secs == sorted(secs), secs
