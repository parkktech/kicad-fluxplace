"""N12/N13 (F16 narration): plain-words narration lines per stage, the silence guard, parked and why on placement parts."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import events, liveview as LV, modules as MD, pipeline as PL, routediag as RD


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (events.ENV_FILE, events.ENV_RUN, events.ENV_STAGE, LV.ENV_OPS, "FLUXPLACE_NESTED"):
        monkeypatch.delenv(k, raising=False)
    LV._last.clear()
    LV._narr.clear()
    yield


def _on(tmp_path, monkeypatch):
    W = tmp_path / "W"
    events.init(W, run="t")
    return W


def _narr(W):
    if not os.path.exists(os.path.join(W, "events.jsonl")):
        return []
    with open(os.path.join(W, "events.jsonl"), encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip() and json.loads(x)["kind"] == "narration"]


def test_narrate_line_has_the_contract_fields(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    LV.narrate("ready", "Checking the parts list before any part is placed.", "start")
    (row,) = _narr(W)
    assert {k: row[k] for k in ("v", "kind", "stage", "phase", "text")} == {
        "v": 1, "kind": "narration", "stage": "ready", "phase": "start", "text": "Checking the parts list before any part is placed."}
    assert "eta_s" in row and row["eta_s"] is None


def test_narrate_stream_off_is_a_no_op(tmp_path):
    assert LV.narrate("ready", "x", "start") is None
    assert not os.path.exists(tmp_path / "events.jsonl")


def test_stage_wrapper_gives_start_and_end_lines(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    PL._ev_stage("stage_start", 1, "modules")
    PL._ev_stage_end(1, "modules", {"verdict": "pass", "line": "modules: 3 groups"}, None, 1.2)
    rows = _narr(W)
    assert [r["phase"] for r in rows] == ["start", "end"]
    assert all(r["stage"] == "modules" for r in rows)
    assert "modules: 3 groups" in rows[1]["text"] and "1.2" in rows[1]["text"]


def test_stage_wrapper_start_line_is_in_words_for_every_stage(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    for name in ("ready", "floorplan", "route", "review"):
        PL._ev_stage("stage_start", 1, name)
    rows = _narr(W)
    assert [r["stage"] for r in rows] == ["ready", "floorplan", "route", "review"]
    assert all(len(r["text"].split()) >= 6 for r in rows)


def test_progress_round_gives_a_plain_doing_line(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    LV.progress("floorplan", "anneal", always=True, generation=2, generations=8, key=[["hard rows", 1]], verdict="kept")
    (row,) = _narr(W)
    assert row["phase"] == "doing" and "round 2 of 8" in row["text"] and "hard rows 1" in row["text"]


def test_no_silence_over_the_limit(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    monkeypatch.setattr(LV, "NARR_S", 0.05)
    with LV.watching("place"):
        time.sleep(0.5)
    beats = [r for r in _narr(W) if r["phase"] == "doing"]
    assert len(beats) >= 4 and all(b["stage"] == "place" for b in beats)


def test_watching_is_not_nested(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    monkeypatch.setattr(LV, "NARR_S", 0.05)
    with LV.watching("floorplan"):
        with LV.watching("floorplan"):
            time.sleep(0.2)
    assert len(_narr(W)) <= 3
    assert LV._watch["stage"] is None


def _board(tmp):
    body = PB.column_ic("U1", 40, 40, [f"S{i}" for i in range(8)])
    body += PB.fp("C1", 44, 40, [PB.pad("1", -0.5, 0, "S0", 0.5, 0.5), PB.pad("2", 0.5, 0, "GND", 0.5, 0.5)])
    path = PB.write(tmp, body)
    B = RD.load(path)
    return B, MD.build(B, {}, None, arrange=False)


def test_snapshot_marks_parked_and_why(tmp_path):
    B, M = _board(tmp_path)
    crit = [{"id": "C1 -> U1.1", "ok": False, "value": 6.2, "limit": 5.0, "text": "support C1 -> U1.1", "hard": True},
            {"id": "U9 decoupling", "ok": True, "value": 1, "limit": 2, "text": "other"}]
    d = LV.snapshot_dict(B, 1, M, meta={"criteria": crit})
    by = {p["ref"]: p for p in d["parts"]}
    assert by["U1"]["parked"] is False and by["C1"]["parked"] is False
    assert set(by["U1"]["why"]) == {"owner", "module", "fixed", "rows"}
    assert by["C1"]["why"]["rows"] == [{"id": "C1 -> U1.1", "ok": False, "hard": True}]
    assert by["U1"]["why"]["rows"] == [{"id": "C1 -> U1.1", "ok": False, "hard": True}]
    assert by["U1"]["why"]["module"] == by["U1"]["mod"]
    # a part whose courtyard is outside the outline is parked
    B["parts"]["C1"]["pos"] = (B["bbox"][2] + 50.0, B["bbox"][3] + 50.0)
    B["parts"]["C1"]["court"] = (B["bbox"][2] + 49.0, B["bbox"][3] + 49.0, B["bbox"][2] + 51.0, B["bbox"][3] + 51.0)
    d = LV.snapshot_dict(B, 2, M, meta={"criteria": crit})
    assert {p["ref"]: p["parked"] for p in d["parts"]}["C1"] is True


def test_direct_command_gets_start_heartbeat_and_end(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    monkeypatch.setattr(LV, "NARR_S", 0.05)

    def fake(a):
        time.sleep(0.3)
        return 0

    assert LV.run_command("ready", fake, None) == 0
    rows = _narr(W)
    assert rows[0]["phase"] == "start" and rows[-1]["phase"] == "end" and rows[-1]["stage"] == "ready"
    assert "done" in rows[-1]["text"] and any(r["phase"] == "doing" for r in rows)


def test_direct_command_under_pipeline_emits_no_own_start_or_end(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    monkeypatch.setenv(events.ENV_STAGE, "ready")
    LV.run_command("ready", lambda a: 0, None)
    assert [r["phase"] for r in _narr(W)] == []
