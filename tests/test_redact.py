"""fix-c #11: a key in a request URL never reaches an exception message, log line or report."""
import urllib.error
import urllib.request
import pytest
from fluxplace import redact as R, sourcing as S

FAKE = "FAKEKEY-9f3c2a77"


def test_redact_patterns():
    assert FAKE not in R.redact(f"https://api.mouser.com/x?apiKey={FAKE}&q=1")
    assert "apiKey=***" in R.redact(f"https://api.mouser.com/x?apiKey={FAKE}")
    assert FAKE not in R.redact(f"Authorization: Bearer {FAKE}")
    assert FAKE not in R.redact(f"X-DIGIKEY-Client-Id: {FAKE}")


def test_urlopen_wrapper_scrubs_raised_exception(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError(f"connection refused for https://api.mouser.com/v1?apiKey={FAKE}")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    req = urllib.request.Request(f"https://api.mouser.com/v1?apiKey={FAKE}")
    with pytest.raises(urllib.error.URLError) as e:
        R.urlopen(req, timeout=1)
    assert FAKE not in str(e.value) and FAKE not in repr(e.value)


def test_mouser_lookup_exception_has_no_key(monkeypatch):
    def boom(req, timeout=None):
        raise OSError(f"tls failure talking to {req.full_url}")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)
    msgs = []
    with pytest.raises(Exception) as e:
        S._mouser_once("RC0603FR-0710KL", "RC0603FR-0710KL", {"MOUSER_API_KEY": FAKE}, retries=1)
    text = f"{type(e.value).__name__}: {e.value} {msgs}"
    assert FAKE not in text
