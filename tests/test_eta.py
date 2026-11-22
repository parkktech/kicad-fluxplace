import json
import os

import pytest

from fluxplace import eta as ETA
from fluxplace import events


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("FLUXPLACE_EVENTS", "FLUXPLACE_RUN", "FLUXPLACE_STAGE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ETA, "TIMINGS", str(tmp_path / "cfg" / "timings.jsonl"))
    events._buckets.clear()
    ETA._CTX.update(features={}, stages=[], current=None, t_stage=None, t_tick=0.0)
    yield


def test_estimate_table_without_history():
    est = ETA.estimate({}, ["ready", "place"], elapsed_in_stage=0.0)
    assert est["model"] == "table" and est["secs_left"] == 605.0
    est2 = ETA.estimate({}, ["ready", "place"], elapsed_in_stage=100.0)
    assert est2["secs_left"] == 505.0
    assert 0.0 <= est2["done_frac"] <= 1.0


def test_scale_per_stage_kind():
    f = {"components": 200, "nets": 100, "density": 0.5}
    assert ETA.scale_of("place", f) == 200.0
    assert ETA.scale_of("route", f) == 50.0
    assert ETA.scale_of("close", f) == 50.0
    assert ETA.scale_of("review", f) == 1.0


def test_history_scales_with_board_size():
    small = {"components": 100, "nets": 100, "density": 0.5}
    big = {"components": 400, "nets": 200, "density": 0.5}
    ETA.record(small, "route", 500.0, "pass")      # 500 s at scale 50 -> 10 s per unit
    ETA.record(small, "route", 500.0, "pass")
    est = ETA.estimate(big, ["route"], 0.0)
    assert est["model"] == "history" and abs(est["secs_left"] - 1000.0) < 1e-6
    assert "route:history(n=2)" in est["basis"]


def test_history_window_is_last_20():
    f = {"components": 10}
    for _ in range(25):
        ETA.record(f, "place", 100.0, "pass")
    assert len(ETA._ratios("place")) == 20


def test_record_never_raises(monkeypatch):
    monkeypatch.setattr(ETA, "TIMINGS", "/proc/definitely/not/writable/timings.jsonl")
    ETA.record({}, "route", 1.0, "pass")
    assert ETA.estimate({}, ["route"])["secs_left"] == 3600.0


def test_report_writes_eta_json_and_event(tmp_path, monkeypatch):
    events.init(tmp_path, run="E1")
    ETA.report({"secs_left": 42.0, "done_frac": 0.5, "model": "table", "basis": "x"}, stage="place")
    obj = json.load(open(tmp_path / "eta.json"))
    assert obj["secs_left"] == 42.0 and obj["stage"] == "place"
    row = json.loads(open(tmp_path / "events.jsonl").readline())
    assert row["kind"] == "eta" and row["done_frac"] == 0.5


def test_stage_end_records_and_reports(tmp_path):
    events.init(tmp_path, run="E2")
    ETA.set_run({"components": 10}, ["ready", "place", "route"])
    ETA.stage_end("ready", 4.0, "pass")
    assert os.path.exists(tmp_path / "eta.json")
    rows = [json.loads(x) for x in open(ETA.TIMINGS)]
    assert rows[-1]["stage"] == "ready" and rows[-1]["secs"] == 4.0


def test_tick_rate_limited(tmp_path):
    events.init(tmp_path, run="E3")
    ETA.set_run({}, ["place", "route"])
    ETA.stage_start("place")
    ETA.tick()                          # just started: no tick yet
    assert not os.path.exists(tmp_path / "eta.json")
    ETA._CTX["t_tick"] = 0.0            # 60 s have passed
    ETA.tick()
    assert os.path.exists(tmp_path / "eta.json")
