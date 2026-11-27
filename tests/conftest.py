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
)


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.nodeid.startswith(SERIAL_NODEID_PREFIXES):
            item.add_marker(pytest.mark.xdist_group("serial"))
