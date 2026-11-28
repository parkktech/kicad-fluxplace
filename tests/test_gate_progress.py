"""stage gate: the gap loop logs nothing, so the gate reports what its own attempt files show - one event per change (W16 pairs
gate: 18 min without an event)."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import pipeline as PL


def phase(d, n, p):
    os.makedirs(os.path.join(d, "close"), exist_ok=True)
    with open(os.path.join(d, "close", f"attempt-{n:03d}.phases.jsonl"), "a") as f:
        f.write(json.dumps({"p": p, "w": time.time()}) + "\n")


def test_gate_state_names_the_newest_attempt_and_its_last_phase(tmp_path):
    d = str(tmp_path)
    assert PL._gate_state(d) == ""
    phase(d, 1, "launch")
    phase(d, 1, "router")
    assert PL._gate_state(d) == "attempt-001 router (phase 2)"


def test_the_gate_emits_one_progress_event_per_change_and_none_while_nothing_moves(tmp_path, monkeypatch):
    d, ev = str(tmp_path / "g"), str(tmp_path / "events.jsonl")
    os.makedirs(d)
    monkeypatch.setenv("FLUXPLACE_EVENTS", ev)

    def loop():
        phase(d, 1, "launch")
        time.sleep(0.15)
        phase(d, 1, "router")
        time.sleep(0.15)
        phase(d, 2, "launch")
        time.sleep(0.3)                                    # nothing moves: no event
        return "r"
    assert PL._gate_watched(d, "pairs", loop, poll=0.02) == "r"
    steps = [json.loads(x)["step"] for x in open(ev) if '"progress"' in x]
    assert steps[0] == "gate pairs: gap loop start" and steps[-1].startswith("gate pairs: gap loop end")
    mid = steps[1:-1]
    assert mid == ["gate pairs: gap loop attempt-001 launch (phase 1)", "gate pairs: gap loop attempt-001 router (phase 2)",
                   "gate pairs: gap loop attempt-002 launch (phase 1)"], mid
    assert all(json.loads(x).get("stage") == "pairs" for x in open(ev))
