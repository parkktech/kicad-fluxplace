import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # tests/ is a package: test helpers (pcbgen, ...) import from here under any xdist order
import pytest


@pytest.fixture(autouse=True)
def _events_isolated(monkeypatch, tmp_path):
    """Keep the event stream and ETA history out of the user's home during tests."""
    from fluxplace import eta
    for k in ("FLUXPLACE_EVENTS", "FLUXPLACE_RUN", "FLUXPLACE_STAGE", "FLUXPLACE_NESTED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(eta, "TIMINGS", str(tmp_path / "_timings" / "timings.jsonl"))
    yield


@pytest.fixture(autouse=True)
def _shared_cache_isolated(monkeypatch, tmp_path):
    """The shared per-user lookup cache (~/.cache/fluxplace) is a tmp dir in tests."""
    monkeypatch.setenv("FLUXPLACE_CACHE_HOME", str(tmp_path / "_shared_cache"))
    yield


@pytest.fixture(autouse=True)
def _pair_rows_cache_reset():
    """gaps._PAIR_ROWS is keyed by copper content: a test that mocks a failing measure must not see a row cached by another test."""
    from fluxplace import gaps
    gaps._PAIR_ROWS.clear()
    yield


@pytest.fixture(autouse=True)
def _http_breaker_reset():
    """redact.urlopen remembers a dead host per process; a test that fails a host must not poison the next one."""
    from fluxplace import redact
    redact._DOWN.clear()
    redact._CFG.clear()
    yield
    redact._DOWN.clear()
    redact._CFG.clear()


@pytest.fixture(autouse=True)
def _drc_gate_off_for_synthetic_boards(monkeypatch, request):
    """The floorplan's KiCad DRC gate fails closed; the synthetic fixtures are not boards kicad-cli loads. Only test_legalgate runs it for real."""
    if request.module.__name__.endswith("test_legalgate"):
        yield
        return
    from fluxplace import legalgate as LG
    monkeypatch.setattr(LG, "gate", lambda *a, **k: {"n": {}, "items": [], "pairs": [], "reverted": {}})
    yield


# B9 (merge gate speed): the gate runs `pytest -n N --dist loadgroup`. Tests that share a fixed path/port or
# process-global state must run in ONE worker, one after another: list them here (node-id prefix) and they get
# xdist_group("serial"). Everything else is free to spread. Plain `pytest` (no xdist) ignores the mark.
SERIAL_NODEID_PREFIXES = (
    "tests/test_core.py::test_adaptive_fanout_priority",   # default arg tmpdir=/tmp/fluxtest_ff
    "tests/test_core.py::test_constraints_ingest",         # default arg path=/tmp/fluxtest_cons.toml
    "tests/test_routefr.py::",
    "tests/test_handroute_ends.py::test_via_min_budget_writes_verified_prefix_and_marks_remainder",   # time budget, fails under load (85), 1.4 s alone
    "tests/test_lookahead.py::test_stage_runs_each_ghost_and_drops_a_failed_one",
    "tests/test_gate_script.py::",                        # queue timing (GATE_POLL 0.3 s) fails under -n 8 at load 40+ (batch5b gate), passes alone   # load flake (ledger row 219), fails under -n 8                             # stall/timing assertions fail under -n 8 load (2026-11-26 main-all run), pass serially
)


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.nodeid.startswith(SERIAL_NODEID_PREFIXES):
            item.add_marker(pytest.mark.xdist_group("serial"))
