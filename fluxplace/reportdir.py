"""Where default report files (`<board stem>.<cmd>.txt/.json`) go. One rule for every command:

  a. `--report-dir DIR` (global) or env FLUXPLACE_REPORT_DIR sends every default report there;
  b. else a command given an explicit `--json FILE` / `--out FILE` writes its companion reports in
     that file's directory;
  c. else beside the input, as before; the command then names each file it wrote (`announce`, stderr).
"""
import os
import sys

_DIR = None


def set_dir(d):
    global _DIR
    _DIR = d or None


def _explicit_dir(a):
    for k in ("json", "out"):
        v = getattr(a, k, None)
        if isinstance(v, str) and v and not v.startswith("-"):
            return os.path.dirname(os.path.abspath(v))
    return None


def redirect(a, stem):
    """`stem` (path minus extension, beside the input) -> the stem to write report files at."""
    d = _DIR or os.environ.get("FLUXPLACE_REPORT_DIR") or _explicit_dir(a)
    if not d:
        return stem
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return stem
    return os.path.join(d, os.path.basename(stem))


def announce(*paths):
    """One `report: PATH` line per file written, on stderr (stdout may be machine JSON)."""
    for p in paths:
        if p:
            print(f"report: {p}", file=sys.stderr)
