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


_DOWN = {}            # host -> [consecutive network failures, down-until epoch]: the per-process negative cache of a dead host


class HostSkipped(urllib.error.URLError):
    """Raised without touching the network: the host failed `http_down_after` times in a row and is skipped for `http_down_s`.
    A transient state of this process, never a fact about the part: callers must not cache it."""


# [parts] keys (constraints) with their defaults; env FLUXPLACE_<NAME> overrides; a bad value falls back to the next level
DEFAULTS = {"http_timeout_s": 15.0, "http_down_after": 2, "http_down_s": 300.0, "lookup_budget_s": 120.0}
_ENV = {"http_timeout_s": "FLUXPLACE_HTTP_TIMEOUT", "http_down_after": "FLUXPLACE_HTTP_DOWN_AFTER", "http_down_s": "FLUXPLACE_HTTP_DOWN_S",
        "lookup_budget_s": "FLUXPLACE_LOOKUP_BUDGET_S"}
_CFG = {}


def configure(cons):
    """Take the lookup limits from the constraints' [parts] table (called where a stage loads them). Never raises."""
    _CFG.clear()
    try:
        _CFG.update({k: v for k, v in ((cons or {}).get("parts") or {}).items() if k in DEFAULTS})
    except Exception:                                   # noqa: BLE001
        pass


def setting(key):
    """env override > [parts] key > default; a value that is not a number (or is negative) is skipped, never raised."""
    import os
    for raw in (os.environ.get(_ENV[key]), _CFG.get(key)):
        try:
            v = float(raw)
            if raw is not None and v >= 0:
                return int(v) if isinstance(DEFAULTS[key], int) else v
        except (TypeError, ValueError):
            continue
    return DEFAULTS[key]


def _limits():
    return setting("http_timeout_s"), setting("http_down_after"), setting("http_down_s")


def _host(req):
    import urllib.parse
    return urllib.parse.urlsplit(getattr(req, "full_url", None) or str(req)).hostname or ""


def urlopen(req, timeout=30, slow=False):
    """urllib.request.urlopen whose failures carry no credentials. HTTPError keeps its type (callers branch on
    .code); other exceptions are re-raised with the same type when constructible, else as RuntimeError. The
    replacement is raised with no __cause__ and no __context__, so the original (unredacted) exception and its
    URL are not reachable from the traceback.
    Bounded: one request waits at most `http_timeout_s` (default 15); a host that fails `http_down_after` (2)
    times in a row at the network level (timeout, DNS, refused; an HTTP status is an answer) is skipped for
    `http_down_s` (300) s, so a dead distributor costs two timeouts, not one per part.
    slow: a manufacturer document host (large PDFs, slow CDNs): the caller's timeout is not capped and the host gets two more
    failures of grace before it is skipped."""
    import time
    cap, after, down_s = _limits()
    if slow:
        cap, after = max(cap, timeout), after + 2
    host = _host(req)
    st = _DOWN.get(host)
    if st and st[1] > time.time():
        raise HostSkipped(f"{host} skipped: {st[0]} network failures in a row (negative cache, {int(st[1] - time.time())} s left)")
    try:
        r = urllib.request.urlopen(req, timeout=min(timeout, cap))
        _DOWN.pop(host, None)
        return r
    except urllib.error.HTTPError as e:
        _DOWN.pop(host, None)
        if e.url:
            e.url = redact(e.url)
        e.msg = redact(e.msg)
        raise
    except Exception as e:
        n = (_DOWN.get(host) or [0, 0])[0] + 1
        _DOWN[host] = [n, time.time() + down_s if n >= after else 0]
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
