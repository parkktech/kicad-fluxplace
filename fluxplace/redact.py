"""Keep credentials out of logs, exception text, caches and reports.

Distributor and Nexar calls carry keys in query strings (`apiKey=`), JSON bodies and auth headers. Every HTTP call
site in the package goes through `urlopen` here (tests/test_redact.py walks the AST to enforce it), and any text
written to a log, exception message, cache or failure file goes through `redact`.
"""
import re
import urllib.error
import urllib.request

# Credential key names. client_id is deliberately absent (public identifier); the DigiKey client-id header is
# covered by the header pattern below because the header is sent with the secret-bearing requests.
_SEP_IN = r"(?:[_-]|%5F|%5f|%2D|%2d)?"   # word separator inside a key name, plain or URL-encoded
_KEY = (r"(?:api" + _SEP_IN + r"key|access" + _SEP_IN + r"token|refresh" + _SEP_IN + r"token|id" + _SEP_IN + r"token|"
        r"client" + _SEP_IN + r"secret|password|passwd|secret|token|key)")
# separator: "=" or ":" (plain, or URL-encoded as %3D / %3A), with optional quoting around the value
_SEP = r"(?:=|:|%3D|%3A|%3d|%3a)"
_OPEN_Q = r"(?:\"|'|%22|%27)?"

_PATS = [
    # key=value, key: value, "key": "value", key%3Dvalue (query strings, JSON, form bodies, URL-encoded)
    re.compile(r"((?<![A-Za-z0-9])" + _KEY + r"(?![A-Za-z0-9])[\"']?\s*" + _SEP + r"\s*" + _OPEN_Q + r"\s*)"
               r"[^&\s\"',}<>]+", re.I),
    # Authorization / Proxy-Authorization header, plain or URL-encoded, with optional scheme
    re.compile(r"((?:Proxy-)?Authorization[\"']?\s*" + _SEP + r"\s*" + _OPEN_Q + r"\s*"
               r"(?:(?:%20|\s|\+)*(?:Bearer|Basic|Digest|Token)(?:\s|%20|\+)+)?)[^\s\"',}&<>]+", re.I),
    # bare bearer credentials in free text (Basic is only recognised inside an Authorization header above)
    re.compile(r"(\bBearer(?:\s|%20|\+)+)[A-Za-z0-9._~+/=-]+", re.I),
    # DigiKey headers (client id is sent as a header; both are treated as secret, as before)
    re.compile(r"(X-DIGIKEY-Client-(?:Id|Secret)[\"']?\s*" + _SEP + r"\s*" + _OPEN_Q + r"\s*)[^\s\"',}&<>]+", re.I),
]


def redact(text):
    """Text with credential values replaced by ***. Safe on any string, including None. Idempotent."""
    out = "" if text is None else str(text)
    for p in _PATS:
        out = p.sub(lambda m: m.group(1) + "***", out)
    return out


def urlopen(req, timeout=30):
    """urllib.request.urlopen whose failures carry no credentials. HTTPError keeps its type (callers branch on
    .code); other exceptions are re-raised with the same type when constructible, else as RuntimeError. The
    replacement is raised with no __cause__ and no __context__, so the original (unredacted) exception and its
    URL are not reachable from the traceback."""
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.url:
            e.url = redact(e.url)
        e.msg = redact(e.msg)
        raise
    except Exception as e:
        text = redact(f"{type(e).__name__}: {e}")
        try:
            new = type(e)(redact(str(e)))
        except Exception:
            new = RuntimeError(text)
        err = new
    # raised outside the handler: raising inside it would set __context__ to the original exception
    err.__cause__ = None
    err.__context__ = None
    err.__suppress_context__ = True
    raise err


__all__ = ["redact", "urlopen"]
