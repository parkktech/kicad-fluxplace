"""close: a rung child logs nothing to the run, so its supervisor reports start, end and (while the child's files move) one
event per interval (fable-pairs full-4: 237 s inside one rung child without an event)."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import gaps as GP


def _steps(ev):
    return [json.loads(x).get("step", "") for x in open(ev).read().splitlines() if x.strip()]


def test_a_rung_child_reports_start_end_and_each_interval_its_files_move(tmp_path, monkeypatch):
    work, ev = str(tmp_path / "w"), str(tmp_path / "events.jsonl")
    os.makedirs(work)
    monkeypatch.setenv("FLUXPLACE_EVENTS", ev)

    def child():
        for i in range(6):
            time.sleep(0.1)
            open(os.path.join(work, f"f{i}"), "w").write("x")
        return "done"
    assert GP._rung_watched("pourisland", work, child, every=0.15) == "done"
    steps = _steps(ev)
    assert steps[0] == "rung pourisland: start" and steps[-1].startswith("rung pourisland: end after")
    assert any("files moving" in s for s in steps[1:-1])


def test_a_rung_child_that_writes_nothing_stays_silent_and_a_failure_is_named(tmp_path, monkeypatch):
    work, ev = str(tmp_path / "w"), str(tmp_path / "events.jsonl")
    os.makedirs(work)
    monkeypatch.setenv("FLUXPLACE_EVENTS", ev)

    def child():
        time.sleep(0.5)                                   # hung: no file moves, so no event (the stall watcher's case)
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        GP._rung_watched("krt", work, child, every=0.1)
    steps = _steps(ev)
    assert len(steps) == 2 and steps[1].startswith("rung krt: failed after")


def test_pourisland_allowance_has_a_floor_and_the_rung_starts_no_call_it_cannot_finish(tmp_path, monkeypatch):
    """c1 smoke: a 12 min close gave the rung 5 % = 36 s, the child was killed inside its first edit."""
    assert GP._rung_cap({}, "pourisland", 720) == 240 and GP._rung_cap({}, "pourisland", 7200) == 360
    from fluxplace import pourisland as P
    calls = []
    monkeypatch.setattr(P, "repair", lambda *a, **k: calls.append(k.get("budget")) or {})
    ctx = {"work": str(tmp_path), "cons_path": None, "budget_s": 50.0}
    assert GP._r_pourisland(str(tmp_path / "b.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [{"net": "GND", "nets": ["GND"]}], ctx)["wrote"] is False
    assert calls == []                                    # 50 s left: less than one guarded edit, nothing starts
    ctx["budget_s"] = 200.0
    GP._r_pourisland(str(tmp_path / "b.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [{"net": "GND", "nets": ["GND"]}], ctx)
    assert calls == [125.0]                               # the call may start edits until EDIT_S before the deadline
