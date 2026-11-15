"""Keep credentials out of logs, exception text and reports.

Distributor and Nexar calls carry keys in query strings (`apiKey=`) and auth headers. `urlopen` is the one
place those leak: an exception's text (URLError reason, a wrapped message) can echo the full URL. Every HTTP
call site in sourcing/nexar/partdata goes through `urlopen` here, which re-raises with the text scrubbed.
"""
import re
import urllib.error
import urllib.request

_PATS = [
    re.compile(r"((?:apiKey|apikey|api_key|access_token|client_secret|client_id|password|token)=)[^&\s'\"<>]+", re.I),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"((?:Authorization|X-DIGIKEY-Client-(?:Id|Secret))[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+", re.I),
]


def redact(text):
    """Text with credential values replaced by ***. Safe on any string, including None."""
    out = "" if text is None else str(text)
    for p in _PATS:
        out = p.sub(lambda m: m.group(1) + "***", out)
    return out


def urlopen(req, timeout=30):
    """urllib.request.urlopen whose failures carry no credentials. HTTPError keeps its type (callers branch on
    .code); other exceptions are re-raised with the same type when constructible, else as RuntimeError."""
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
        raise new from None


__all__ = ["redact", "urlopen"]
