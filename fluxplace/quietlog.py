"""Drop known-noise lines (the pcbnew property.h assert) from stderr and from captured subprocess logs.

The assert is printed by C code straight to fd 2, thousands of times; one log reached 146 MB of it. Three entry points:
`LineFilter` (the pure filter), `install_stderr()` (the process's own fd 2, restored at exit), `filter_file()` /
`filter_text()` (a log a subprocess wrote). Off with FLUXPLACE_RAW_STDERR=1. Non-matching lines are never lost.
"""
import atexit
import os
import re
import sys
import threading

# known-noise patterns (matched against one line, no newline); extend this list
NOISE = [re.compile(r"property\.h\(\d+\): assert .*m_choices")]
LABEL = "KiCad assert"


def is_noise(line):
    s = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
    return any(p.search(s) for p in NOISE)


def summary_line(n):
    return f"[fluxplace] dropped {n} repeated {LABEL} lines"


class LineFilter:
    """feed(bytes) -> bytes of the kept complete lines; finish() -> the unfinished tail + the one count line."""

    def __init__(self):
        self.dropped = 0
        self._buf = b""

    def feed(self, data):
        self._buf += data
        if b"\n" not in self._buf:
            return b""
        head, _, self._buf = self._buf.rpartition(b"\n")
        out = []
        for ln in head.split(b"\n"):
            if is_noise(ln):
                self.dropped += 1
            else:
                out.append(ln)
        return b"\n".join(out) + b"\n" if out else b""

    def finish(self):
        tail, self._buf = self._buf, b""
        out = b""
        if tail:
            if is_noise(tail):
                self.dropped += 1
            else:
                out = tail + b"\n"
        if self.dropped:
            out += (summary_line(self.dropped) + "\n").encode()
        return out


def raw_requested():
    return os.environ.get("FLUXPLACE_RAW_STDERR") == "1"


def filter_text(text):
    """Text minus noise lines, plus the count line when any were dropped."""
    f = LineFilter()
    return (f.feed(text.encode("utf-8", "surrogateescape")) + f.finish()).decode("utf-8", "replace")


def filter_file(path, chunk=1 << 20):
    """Rewrite a captured log in place without its noise lines (streamed; bounded memory). Returns the number dropped.
    A failure leaves the log untouched and returns 0."""
    if raw_requested():
        return 0
    tmp = path + ".qlog.tmp"
    try:
        f = LineFilter()
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            while True:
                b = src.read(chunk)
                if not b:
                    break
                dst.write(f.feed(b))
            dst.write(f.finish())
        if f.dropped:
            os.replace(tmp, path)
        else:
            os.remove(tmp)
        return f.dropped
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 0


_active = None


class _Stderr:
    def __init__(self):
        self.saved = os.dup(2)
        self.r, self.w = os.pipe()
        self.filt = LineFilter()
        self.done = False
        sys.stderr.flush()
        os.dup2(self.w, 2)
        self.t = threading.Thread(target=self._pump, daemon=True)
        self.t.start()

    def _write(self, b):
        while b:
            try:
                n = os.write(self.saved, b)
            except InterruptedError:
                continue
            b = b[n:]

    def _pump(self):
        try:
            while True:
                b = os.read(self.r, 65536)
                if not b:
                    break
                self._write(self.filt.feed(b))
        except OSError:
            pass

    def stop(self):
        if self.done:
            return
        self.done = True
        try:
            sys.stderr.flush()
        except Exception:                                  # noqa: BLE001
            pass
        try:
            os.dup2(self.saved, 2)                          # fd 2 is the real stderr again
            os.close(self.w)                                # a child still holding the pipe cannot block us below
            self.t.join(5)
            self._write(self.filt.finish())
        except OSError:
            pass
        for fd in (self.r, self.saved):
            try:
                os.close(fd)
            except OSError:
                pass


def install_stderr(force=False):
    """Filter this process's fd 2 until exit. Idempotent; no-op when FLUXPLACE_RAW_STDERR=1, under pytest unless
    force=True, or when fd 2 is unusable. Returns the handle (call .stop() to restore early) or None."""
    global _active
    if _active is not None and not _active.done:
        return _active
    if raw_requested() or (not force and ("PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules)):
        return None
    try:
        _active = _Stderr()
    except OSError:
        return None
    atexit.register(_active.stop)
    return _active
