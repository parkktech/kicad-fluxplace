"""Call log: one JSON line per CLI invocation in .fluxplace/calls.jsonl, readable with `fluxplace calls`.

Location: beside the board / directory named in the arguments, else ~/.cache/fluxplace/. Capped (~5 MB, one old file
kept as calls.jsonl.1). A logging failure never fails the command. Off with FLUXPLACE_NO_CALL_LOG=1, and under pytest
unless FLUXPLACE_CALL_LOG=1.
"""
import datetime
import json
import os
import re
import sys
import time

from fluxplace.redact import redact

MAX_BYTES = 5_000_000
SUMMARY_CHARS = 500
_SECRET_FLAG = re.compile(r"^--?[a-z-]*(api[-_]?key|token|password|passwd|secret)$", re.I)
_depth = 0


def enabled():
    if os.environ.get("FLUXPLACE_NO_CALL_LOG") == "1":
        return False
    if "PYTEST_CURRENT_TEST" in os.environ and os.environ.get("FLUXPLACE_CALL_LOG") != "1":
        return False
    return True


def safe_argv(argv):
    out, mask = [], False
    for t in argv:
        t = str(t)
        out.append("***" if mask else redact(t))
        mask = bool(_SECRET_FLAG.match(t)) and "=" not in t
    return out


def default_path():
    return os.path.join(os.path.expanduser("~/.cache/fluxplace"), "calls.jsonl")


def log_path(argv, cwd=None):
    """.fluxplace/calls.jsonl beside the first board/project, else the first existing file/dir in argv, else the cache."""
    cwd = cwd or os.getcwd()
    toks = [str(t) for t in argv if not str(t).startswith("-")]
    cands = [os.path.join(cwd, t) if not os.path.isabs(t) else t for t in toks]
    for pick in (lambda p: p.endswith((".kicad_pcb", ".kicad_pro")) and os.path.isfile(p),
                 lambda p: os.path.isdir(p), lambda p: os.path.isfile(p)):
        for p in cands:
            try:
                if pick(p):
                    d = p if os.path.isdir(p) else os.path.dirname(p)
                    return os.path.join(d, ".fluxplace", "calls.jsonl")
            except OSError:
                pass
    return default_path()


def attempts_path(board):
    """.fluxplace/attempts.jsonl beside the board (same location rule as calls.jsonl)."""
    return os.path.join(os.path.dirname(log_path([board])), "attempts.jsonl")


def append(rec, path, max_bytes=None):
    max_bytes = max_bytes or int(os.environ.get("FLUXPLACE_CALL_LOG_MAX", MAX_BYTES))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass
    with open(path, "a") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


class _Tee:
    """Proxy for sys.stdout that keeps the first SUMMARY_CHARS written."""

    def __init__(self, inner):
        self._inner, self.head = inner, ""

    def write(self, s):
        if len(self.head) < SUMMARY_CHARS and isinstance(s, str):
            self.head += s
        return self._inner.write(s)

    def __getattr__(self, k):
        return getattr(self._inner, k)


def run(fn, argv=None):
    """Run fn(argv) (the CLI main) and append one call-log line. Only the outermost call logs; the result is unchanged."""
    global _depth
    if _depth or not enabled():
        return fn(argv)
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "calls":
        return fn(argv)
    _depth += 1
    t0, rc, tee = time.time(), 0, None
    real = sys.stdout
    try:
        tee = sys.stdout = _Tee(real)
    except Exception:                                      # noqa: BLE001
        tee = None
    try:
        r = fn(argv)
        rc = r if type(r) is int else 0
        return r
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        raise
    except BaseException as e:
        rc = 1
        if tee is not None:
            tee.head = (tee.head + f"\nraised {type(e).__name__}: {e}")[:SUMMARY_CHARS]
        raise
    finally:
        _depth -= 1
        if sys.stdout is tee:
            sys.stdout = real
        try:
            rec = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                   "cmd": args[0] if args else "", "argv": safe_argv(args), "cwd": os.getcwd(),
                   "dur_s": round(time.time() - t0, 2), "rc": rc}
            if tee is not None and tee.head.strip():
                rec["summary"] = redact(tee.head.strip())[:SUMMARY_CHARS]
            append(rec, log_path(args))
        except Exception:                                  # noqa: BLE001 - never fail the command over its log
            pass


def read(path, tail=20, failed=False):
    """-> list of records (oldest first), the last `tail` after the filter. Bad lines are skipped."""
    recs = []
    for p in (path + ".1", path):
        try:
            with open(p) as fh:
                for ln in fh:
                    try:
                        recs.append(json.loads(ln))
                    except ValueError:
                        pass
        except OSError:
            pass
    if failed:
        recs = [r for r in recs if r.get("rc")]
    return recs[-tail:] if tail else recs


def resolve(target, cwd=None):
    """PATH of `fluxplace calls`: a .jsonl, a board, a directory; none -> ./.fluxplace if present, else the cache."""
    cwd = cwd or os.getcwd()
    if target:
        if target.endswith(".jsonl"):
            return target
        d = target if os.path.isdir(target) else os.path.dirname(os.path.abspath(target))
        return os.path.join(d, ".fluxplace", "calls.jsonl")
    p = os.path.join(cwd, ".fluxplace", "calls.jsonl")
    return p if os.path.exists(p) else default_path()


def line(r):
    a = " ".join(r.get("argv", []))[:90]
    s = (r.get("summary") or "").splitlines()
    return f"{r.get('ts', '?')[:19]} rc={r.get('rc')} {r.get('dur_s', 0):>6}s  {a}" + (f"  | {s[0][:60]}" if s else "")
