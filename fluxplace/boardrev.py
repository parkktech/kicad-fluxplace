"""Board revision id: short sha256 of the board file bytes. A plan (probe/suggest result) carries it; `--expect-rev` refuses a write when the file changed since."""
import hashlib


def rev(path, n=12):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:n]


def check(path, expect):
    """None when `expect` is falsy or matches; else the refusal message."""
    if not expect:
        return None
    now = rev(path)
    if now != expect:
        return f"stale plan: board changed since rev {expect} (now {now}); re-run suggest/probe"
    return None
