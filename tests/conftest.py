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
def _drc_gate_off_for_synthetic_boards(monkeypatch, request):
    """The floorplan's KiCad DRC gate fails closed; the synthetic fixtures are not boards kicad-cli loads. Only test_legalgate runs it for real."""
    if request.module.__name__.endswith("test_legalgate"):
        yield
        return
    from fluxplace import legalgate as LG
    monkeypatch.setattr(LG, "gate", lambda *a, **k: {"n": {}, "items": [], "pairs": [], "reverted": {}})
    yield
