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


# ---- fixes-d #6: table-driven redaction of key=value, JSON, URL-encoded and header forms
SECRET = "s3cr3t-Zq81"
REDACT_CASES = [
    f'{{"client_secret": "{SECRET}"}}',
    f"{{'client_secret': '{SECRET}'}}",
    f'{{"access_token":"{SECRET}","x":1}}',
    f"password: {SECRET}",
    f"password={SECRET}&user=bob",
    f"https://api.mouser.com/v1?apiKey={SECRET}",
    f"https://api.digikey.com/x?API_KEY={SECRET}",
    f"https://x.example/?apikey={SECRET}",
    f"apiKey: '{SECRET}'",
    f"refresh_token={SECRET}",
    f"token%3D{SECRET}%26q%3D1",
    f"client%5Fsecret%3D{SECRET}",
    f"password%3A%20{SECRET}",
    f"Authorization: Bearer {SECRET}",
    f"Authorization: Basic {SECRET}",
    f"Authorization%3A%20Bearer%20{SECRET}",
    f"Proxy-Authorization: Basic {SECRET}",
    f"Bearer {SECRET}",
    f"secret={SECRET}",
    f"key={SECRET}",
    f'"key": "{SECRET}"',
    f"X-DIGIKEY-Client-Secret: {SECRET}",
]
KEEP_CASES = [
    "client_id=PUBLIC-123",
    '{"client_id": "PUBLIC-123"}',
    "https://example.com/a?q=resistor&page=2",
    "secretary: Jim",
    "Basic usage of the tool",
    "keyword=0603",
]


@pytest.mark.parametrize("text", REDACT_CASES)
def test_redact_table_removes_secret(text):
    out = R.redact(text)
    assert SECRET not in out, out
    assert R.redact(out) == out            # idempotent


@pytest.mark.parametrize("text", KEEP_CASES)
def test_redact_table_keeps_non_secrets(text):
    assert R.redact(text) == text


def test_redact_none_and_non_string():
    assert R.redact(None) == ""
    assert R.redact(12) == "12"


# ---- fixes-d #8: the replacement exception carries no original exception and no unredacted URL
def test_urlopen_replacement_has_no_context_or_cause(monkeypatch):
    import traceback
    url = f"https://api.mouser.com/v1?apiKey={SECRET}"

    def boom(req, timeout=None):
        raise OSError(f"tls failure talking to {url}")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(OSError) as e:
        R.urlopen(urllib.request.Request(url), timeout=1)
    assert e.value.__context__ is None and e.value.__cause__ is None
    text = "".join(traceback.format_exception(e.type, e.value, e.tb))
    assert SECRET not in text and SECRET not in repr(e.value)


# ---- fixes-d #7: every HTTP call in the package goes through the redacting wrapper
def test_only_redact_calls_urlopen_or_imports_http_clients():
    import ast
    import pathlib
    pkg = pathlib.Path(R.__file__).parent
    bad = []
    for path in sorted(pkg.rglob("*.py")):
        if path.name == "redact.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
                if name == "urlopen":
                    bad.append(f"{path.name}:{node.lineno} urlopen")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for m in mods:
                    if m.split(".")[0] in ("requests", "httpx", "urllib3") or m.startswith("http.client"):
                        bad.append(f"{path.name}:{node.lineno} import {m}")
    assert not bad, "direct HTTP calls outside redact.py: " + "; ".join(bad)


def test_models_fetch_failure_is_redacted(tmp_path, monkeypatch):
    from fluxplace import models as M
    import json as _json

    def boom(req, timeout=None):
        raise OSError(f"reset for {req.full_url}")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(M, "dk_media", lambda *a, **k: (_ for _ in ()).throw(OSError(f"x https://api.digikey.com/y?apiKey={SECRET}")))
    monkeypatch.setattr(M, "mouser_lookup", lambda *a, **k: None)
    M.fetch_step("RC0603", tmp_path, {}, "tok", log=lambda *a: None)
    cache = _json.loads((tmp_path / ".fetch_failures.json").read_text())
    assert SECRET not in (tmp_path / ".fetch_failures.json").read_text()
    assert cache
