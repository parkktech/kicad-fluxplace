import pytest


@pytest.fixture(autouse=True)
def _events_isolated(monkeypatch, tmp_path):
    """Keep the event stream and ETA history out of the user's home during tests."""
    from fluxplace import eta
    for k in ("FLUXPLACE_EVENTS", "FLUXPLACE_RUN", "FLUXPLACE_STAGE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(eta, "TIMINGS", str(tmp_path / "_timings" / "timings.jsonl"))
    yield
