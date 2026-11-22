"""Event stream for the dashboard (contract: docs/EVENTS-CONTRACT.md).

fluxplace only WRITES events; the dashboard tails them. Every public function here is
fail-safe: emitting never raises and never blocks a stage. No-op unless init() or the env
var FLUXPLACE_EVENTS is set, so subprocesses inherit the stream through the environment.
"""
import json
import os
import time

ENV_FILE = "FLUXPLACE_EVENTS"
ENV_RUN = "FLUXPLACE_RUN"
ENV_STAGE = "FLUXPLACE_STAGE"
VERSION = 1
MAX_STR = 400          # longest string kept in any field (log text contract)
MAX_LINE = 60_000      # keep each line well under the 64 KiB contract limit

_seq = 0
_buckets = {}          # key -> [tokens, last_ts]


def init(work, run=None):
    """Point the stream at W/events.jsonl and set the run id for this process and its children."""
    try:
        os.makedirs(os.fspath(work), exist_ok=True)
        os.environ[ENV_FILE] = os.path.join(os.fspath(work), "events.jsonl")
        os.environ[ENV_RUN] = run or os.environ.get(ENV_RUN) or time.strftime("%Y%m%d-%H%M%S") + "-" + str(os.getpid())
        return os.environ[ENV_RUN]
    except Exception:
        return None


def _trim(v):
    if isinstance(v, str):
        return v if len(v) <= MAX_STR else v[:MAX_STR - 3] + "..."
    if isinstance(v, dict):
        return {str(k): _trim(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_trim(x) for x in v]
    return v


def emit(kind, stage=None, **fields):
    """Append one event line. No-op when the stream is off. Never raises."""
    global _seq
    try:
        path = os.environ.get(ENV_FILE)
        if not path:
            return
        _seq += 1
        obj = {"v": VERSION, "ts": round(time.time(), 3), "run": os.environ.get(ENV_RUN, ""),
               "seq": _seq, "pid": os.getpid(),
               "stage": stage if stage is not None else (os.environ.get(ENV_STAGE) or None),
               "kind": kind}
        obj.update(_trim(fields))
        line = json.dumps(obj, default=str, separators=(",", ":"))
        if len(line) > MAX_LINE:
            obj = {k: obj[k] for k in ("v", "ts", "run", "seq", "pid", "stage", "kind")}
            obj["truncated"] = True
            line = json.dumps(obj, separators=(",", ":"))
        data = (line + "\n").encode("utf-8", "replace")
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except Exception:
        pass


def limited(key, per_s):
    """Token bucket per key: at most `per_s` events per second sustained, burst of per_s. Never raises."""
    try:
        now = time.monotonic()
        tokens, last = _buckets.get(key, [float(per_s), now])
        tokens = min(float(per_s), tokens + (now - last) * per_s)
        if tokens >= 1.0:
            _buckets[key] = [tokens - 1.0, now]
            return True
        _buckets[key] = [tokens, now]
        return False
    except Exception:
        return False


def emit_limited(key, per_s, kind, stage=None, **fields):
    """emit() rate-limited by limited(key, per_s). Never raises."""
    try:
        if enabled() and limited(key, per_s):
            emit(kind, stage=stage, **fields)
    except Exception:
        pass


def enabled():
    """True when the stream is on (env FLUXPLACE_EVENTS set). Lets callers skip building frames."""
    return bool(os.environ.get(ENV_FILE))


def _workdir():
    p = os.environ.get(ENV_FILE)
    return os.path.dirname(p) if p else ""


def _rel(path):
    """Artifact paths are relative to W (contract)."""
    base = _workdir()
    p = os.fspath(path)
    if base and os.path.isabs(p) and p.startswith(base + os.sep):
        return os.path.relpath(p, base).replace(os.sep, "/")
    return p


def iteration(loop, i, n=None, metrics=None, note=""):
    """iteration event (loop place/pairs/route/close/lookahead/ghost/tune). Never raises."""
    try:
        if os.environ.get(ENV_FILE):
            clean = {k: v for k, v in (metrics or {}).items() if isinstance(v, (int, float, str, bool, list, type(None)))}
            emit("iteration", loop=loop, i=i, n=n, metrics=clean, note=str(note)[:MAX_STR])
    except Exception:
        pass


def artifact(type_, path, label=""):
    """artifact event (type png/report/board/table). Never raises."""
    try:
        if os.environ.get(ENV_FILE):
            emit("artifact", type=type_, path=_rel(path), label=str(label)[:MAX_STR])
    except Exception:
        pass


if __name__ == "__main__":                       # python3 -m fluxplace.events --sample OUT | --replay W [--speed N] OUT
    import sys
    from .events_sample import main as _main
    sys.exit(_main(sys.argv[1:]))
