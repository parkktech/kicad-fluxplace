import json
import os
import time

import pytest

from fluxplace import events


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(events.ENV_FILE, raising=False)
    monkeypatch.delenv(events.ENV_RUN, raising=False)
    monkeypatch.delenv(events.ENV_STAGE, raising=False)
    events._buckets.clear()
    yield


def _lines(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def test_env_unset_is_noop(tmp_path):
    events.emit("log", text="x")
    events.emit_limited("k", 2, "log", text="x")
    assert not any(p.name == "events.jsonl" for p in tmp_path.iterdir())
    assert events.ENV_FILE not in os.environ


def test_init_sets_env_and_run(tmp_path, monkeypatch):
    run = events.init(tmp_path, run="R1")
    assert run == "R1"
    assert os.environ[events.ENV_FILE] == os.path.join(str(tmp_path), "events.jsonl")
    assert os.environ[events.ENV_RUN] == "R1"


def test_lines_parse_and_common_fields(tmp_path, monkeypatch):
    events.init(tmp_path, run="R2")
    events.emit("stage_start", stage="place", n=1, name="place")
    events.emit("progress", stage="route", step="pass 1", pct=12.5, state="running")
    rows = _lines(os.path.join(str(tmp_path), "events.jsonl"))
    assert [r["kind"] for r in rows] == ["stage_start", "progress"]
    for r in rows:
        assert r["v"] == 1 and r["run"] == "R2" and r["pid"] == os.getpid()
        assert isinstance(r["ts"], float)
    assert rows[0]["seq"] < rows[1]["seq"]
    assert rows[0]["stage"] == "place" and rows[0]["n"] == 1
    assert rows[1]["pct"] == 12.5


def test_stage_defaults_to_env(tmp_path, monkeypatch):
    events.init(tmp_path, run="R3")
    monkeypatch.setenv(events.ENV_STAGE, "pairs")
    events.emit("log", level="info", text="hi", src="pairs")
    events.emit("log", stage=None, level="info", text="hi", src="x")
    rows = _lines(os.path.join(str(tmp_path), "events.jsonl"))
    assert rows[0]["stage"] == "pairs"


def test_truncation(tmp_path):
    events.init(tmp_path, run="R4")
    events.emit("log", level="info", text="a" * 5000, src="krt")
    row = _lines(os.path.join(str(tmp_path), "events.jsonl"))[0]
    assert len(row["text"]) <= events.MAX_STR
    assert row["text"].endswith("...")


def test_oversized_line_is_dropped_to_marker(tmp_path):
    events.init(tmp_path, run="R5")
    events.emit("parts", frame=[[f"R{i}", 1.0, 2.0, 0, "top"] for i in range(20000)])
    path = os.path.join(str(tmp_path), "events.jsonl")
    raw = open(path, "rb").read()
    assert len(raw) < 64 * 1024
    row = json.loads(raw)
    assert row["kind"] == "parts" and row["truncated"] is True


def test_never_raises_on_bad_path(monkeypatch, tmp_path):
    monkeypatch.setenv(events.ENV_FILE, str(tmp_path / "no" / "such" / "dir" / "e.jsonl"))
    events.emit("log", text="x")  # must not raise
    events.emit_limited("k", 2, "log", text="x")


def test_unserialisable_field_does_not_raise(tmp_path):
    events.init(tmp_path, run="R6")
    events.emit("log", obj=object(), text="ok")
    row = _lines(os.path.join(str(tmp_path), "events.jsonl"))[0]
    assert row["text"] == "ok" and "object" in row["obj"]


def test_limiter_burst_and_refill(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(events.time, "monotonic", lambda: clock[0])
    allowed = [events.limited("p", 2) for _ in range(5)]
    assert allowed == [True, True, False, False, False]
    clock[0] += 0.5  # one token refilled at 2/s
    assert events.limited("p", 2) is True
    assert events.limited("p", 2) is False
    clock[0] += 100.0  # refill capped at burst
    assert [events.limited("p", 2) for _ in range(3)] == [True, True, False]


def test_emit_limited_drops_excess(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(events.time, "monotonic", lambda: clock[0])
    events.init(tmp_path, run="R7")
    for _ in range(50):
        events.emit_limited("log", 20, "log", text="line")
    rows = _lines(os.path.join(str(tmp_path), "events.jsonl"))
    assert len(rows) == 20
