"""Compact-by-default output for analysis commands.

Every byte a command prints lands in an AI agent's context and stays there.
This layer wraps a command (one place: ``cli.main``), captures what it prints
and re-emits it as

    <cmd>: <VERDICT> (exit N) - <the command's own summary line>
    counts: <per-code histogram, worst first>
    worst <N> of <M>: <the N highest-ranked items, one line each>
    full report: <board stem>.<cmd>.txt (+ .json)   --full for everything

at most ~40 lines / 3 KB.  The full text is always written to disk first, so
nothing is lost, only moved.  Exit codes are never touched.

The command bodies are not edited: this module parses their printed text.
Recognised shapes (all existing outputs fall into one of them):

  * finding lines   ``  FAIL CODE  message`` / ``  WARNING code: message``
  * section lines   ``CODE (n)`` followed by ``  INFO message``       (bom-opt)
  * net blocks      ``NET  (n pads: ...)`` / ``  CODE: msg`` / ``-> fix``  (route-diagnose)
  * move lists      ``  12. C54 from (x, y) toward U5.32  fixes 3: NET``
  * count tables    ``  CODE      27``                                (route-ready)
  * grade lines     ``    NONE  MPN ...``                                (sourcing)

Anything else falls back to head + tail of the raw text.
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile

MAX_LINES = 40
MAX_BYTES = 3000
DEFAULT_TOP = 10
MSG_WIDTH = 150
MAX_CODE_ROWS = 12
HEAD_KEEP = {"bom-opt": 5, "route-ready": 5}     # fact lines worth keeping verbatim

# Commands that print analysis (never a long-running pipeline: those must
# stream progress and are not buffered here).
COMPACT_CMDS = (
    "review", "route-ready", "route-diagnose", "clusters", "anchor-check",
    "floorplan-check", "templates", "bom-opt", "lint", "verify-models", "sourcing",
    "datasheets", "spec-check", "drc-scope", "eval", "silk", "magnetics",
)

_LEVELS = {"FAIL": 0, "ERROR": 0, "WARN": 1, "WARNING": 1, "INFO": 2}
_GRADES = {"NONE": "FAIL", "RISK": "FAIL", "CN_ABSENT": "FAIL", "CN_NONE": "FAIL",
           "LEAD": "WARN", "LOW": "WARN", "CN_LOW": "WARN", "ERR": "WARN",
           "OK": "INFO", "CN_OK": "INFO"}
_LOW_PRIORITY = {"CONGESTION"}      # "no single geometric cause found" fallbacks

_NOISE = re.compile(r"^\S*property\.h\(\d+\): assert ")
_RE_FIND = re.compile(
    r"^\s*(FAIL|ERROR|WARNING|WARN|INFO)\s+"
    r"([A-Z][A-Z0-9_]{2,}|[a-z][a-z0-9-]+):?\s+(.*\S)\s*$")
_RE_SECTION = re.compile(r"^([A-Z][A-Z0-9_]{2,}) \((\d+)\)\s*$")
_RE_SECT_ITEM = re.compile(r"^\s+(FAIL|WARN|INFO)\s+(.*\S)\s*$")
_RE_NET = re.compile(r"^(\S.*?)\s+\((\d+) pads?\b")
_RE_ISSUE = re.compile(r"^\s{2}([A-Z][A-Z0-9_]{2,}): (.*\S)\s*$")
_RE_FIX = re.compile(r"^\s+-> (.*\S)\s*$")
_RE_MOVE = re.compile(r"^\s+(\d+)\. (\S+) (from \(.*?\) toward .*?)\s+fixes (\d+): (.*\S)\s*$")
_RE_COUNT = re.compile(r"^\s{2}([A-Z][A-Z0-9_]{2,})\s+(\d+)\s*$")
_RE_WORST = re.compile(r"^\s*worst \d+:")
_RE_GRADE = re.compile(r"^\s+(OK|LOW|LEAD|RISK|NONE|CN_[A-Z]+|ERR)\s+(\S+)\s+(.*\S)\s*$")
_RE_FAR = re.compile(r"^\s+FAR\s+(\S+)\s+(.*\S)\s*$")
_RE_ANCHOR = re.compile(r"^(\S+)\s+\(.*?\) about .*?(\d+)% worse\s*$")
_RE_SUMMARY = re.compile(r"^([\w-]+): .*\d")
_RE_PATH = re.compile(r"(?:/[\w.\-+]+)*/([\w.\-+]+)")
_RE_MM = re.compile(r"(\d+(?:\.\d+)?)\s*mm")


def strip_noise(text):
    return "\n".join(l for l in text.splitlines() if not _NOISE.match(l))


def parse(text):
    """-> dict(items, counts, moves, summary). Items: level, code, msg, net, fix."""
    items, moves, counts = [], [], {}
    summary = None
    section = None
    net = None
    in_count_table = True
    for line in text.splitlines():
        m = _RE_SECTION.match(line)
        if m:
            section = m.group(1)
            continue
        if _RE_WORST.match(line):
            in_count_table = False
            continue
        m = _RE_MOVE.match(line)
        if m:
            moves.append({"ref": m.group(2), "move": m.group(3),
                          "fixes": int(m.group(4)), "nets": m.group(5)})
            continue
        m = _RE_COUNT.match(line)
        if m and in_count_table:
            counts.setdefault("_table", {})[m.group(1)] = int(m.group(2))
            continue
        m = _RE_FIND.match(line)
        if m:
            lvl = m.group(1)
            items.append({"level": "WARN" if lvl == "WARNING" else
                          ("FAIL" if lvl == "ERROR" else lvl),
                          "code": m.group(2), "msg": m.group(3), "net": None, "fix": None})
            continue
        m = _RE_FAR.match(line)
        if m:
            items.append({"level": "WARN", "code": "SUPPORT_FAR", "msg": m.group(1) + " " + m.group(2),
                          "net": None, "fix": None})
            continue
        m = _RE_ANCHOR.match(line)
        if m:
            if int(m.group(2)) > 0:
                items.append({"level": "WARN", "code": "ANCHOR_WORSE", "msg": line.strip(),
                              "net": None, "fix": None})
            continue
        m = _RE_GRADE.match(line)
        if m:
            items.append({"level": _GRADES.get(m.group(1), "INFO"), "code": m.group(1),
                          "msg": m.group(2) + " " + m.group(3), "net": None, "fix": None})
            continue
        if section:
            m = _RE_SECT_ITEM.match(line)
            if m:
                items.append({"level": m.group(1), "code": section, "msg": m.group(2),
                              "net": None, "fix": None})
                continue
        m = _RE_NET.match(line)
        if m and not line.startswith(" "):
            net = m.group(1)
            section = None
            continue
        m = _RE_ISSUE.match(line)
        if m and net is not None:
            items.append({"level": "ISSUE", "code": m.group(1), "msg": m.group(2),
                          "net": net, "fix": None})
            continue
        m = _RE_FIX.match(line)
        if m and items and items[-1].get("net") is not None and not items[-1]["fix"]:
            items[-1]["fix"] = m.group(1)
            continue
        m = _RE_SUMMARY.match(line)
        if m:
            summary = line.strip()
    for it in items:
        counts.setdefault((it["level"], it["code"]), 0)
        counts[(it["level"], it["code"])] += 1
    return {"items": items, "moves": moves, "counts": counts, "summary": summary}


def _mag(msg):
    m = _RE_MM.search(msg) or re.search(r"(\d+)% worse", msg)
    return float(m.group(1)) if m else 0.0


def _rank(it, i):
    return (_LEVELS.get(it["level"], 1), 1 if it["code"] in _LOW_PRIORITY else 0,
            -_mag(it["msg"]), i)


def _clip(s, n=MSG_WIDTH):
    return s if len(s) <= n else s[: n - 3] + "..."


def pick_summary(cmd, raw, parsed):
    """The command's own most informative summary line (verbatim)."""
    summ = None
    for l in raw.splitlines():
        if l.strip().lower().startswith(cmd) and re.search(r"\d", l):
            summ = l.strip()
    res = [l.strip() for l in raw.splitlines() if l.strip().startswith("RESULT:")]
    summ = (res[-1] if res else None) or summ or parsed["summary"]
    if not summ:
        summ = next((l.strip() for l in raw.splitlines() if l.strip()), "")
    return summ


def verdict_line(cmd, rc, parsed, raw):
    ok = "ok" if rc == 0 else "FAIL"
    summ = _clip(_RE_PATH.sub(r"\1", pick_summary(cmd, raw, parsed)), 160)
    lv = {}
    for it in parsed["items"]:
        lv[it["level"]] = lv.get(it["level"], 0) + 1
    tally = " ".join(f"{n} {k}" for k, n in lv.items() if k != "ISSUE")
    tbl = sorted(((c, n) for c, n in parsed["counts"].get("_table", {}).items() if n),
                 key=lambda kv: -kv[1])
    if tbl:
        tally = ", ".join(f"{c} {n}" for c, n in tbl[:4]) + (", ..." if len(tbl) > 4 else "")
    has = any(w in summ.upper() for w in ("FAIL", "WARN", "INFO"))
    extra = f" [{tally}]" if tally and not has else ""
    return f"{cmd}: {ok} (exit {rc}) - {summ}{extra}"


def _fmt_item(it):
    head = it["code"] if it["level"] == "ISSUE" else f"{it['level']} {it['code']}"
    body = it["msg"]
    if it.get("net"):
        body = f"{it['net']}: {body}"
    if it.get("fix"):
        body += f"  -> {it['fix']}"
    return _clip(re.sub(r"\s{2,}", " ", f"  {head} {body}"))


def compact_text(cmd, raw, rc, top=DEFAULT_TOP, paths=()):
    """The compact report for ``raw`` (pure function; tests call this)."""
    parsed = parse(raw)
    out = [verdict_line(cmd, rc, parsed, raw)]
    items, moves = parsed["items"], parsed["moves"]
    keep = HEAD_KEEP.get(cmd, 0)
    if keep:
        out += [l.rstrip() for l in raw.splitlines()[1:keep] if l.strip()]
    if items or moves or parsed["counts"].get("_table"):
        tbl = parsed["counts"].get("_table")
        if tbl:
            rows = sorted(tbl.items(), key=lambda kv: -kv[1])
            out.append("counts: " + "  ".join(f"{c}={n}" for c, n in rows if n))
        elif items:
            rows = sorted(((k, n) for k, n in parsed["counts"].items() if k != "_table"),
                          key=lambda kv: (_LEVELS.get(kv[0][0], 1), -kv[1], kv[0][1]))
            out.append("counts by code (%d codes):" % len(rows))
            for (lvl, code), n in rows[:MAX_CODE_ROWS]:
                out.append(f"  {n:5d} {lvl if lvl != 'ISSUE' else '':4s} {code}")
            if len(rows) > MAX_CODE_ROWS:
                out.append(f"  ... {len(rows) - MAX_CODE_ROWS} more code(s)")
        if items:
            order = sorted(range(len(items)), key=lambda i: _rank(items[i], i))
            cap = max(2, -(-top // 3))              # keep one code from filling the list
            seen, pick = {}, []
            for i in order:
                c = items[i]["code"]
                if seen.get(c, 0) < cap:
                    seen[c] = seen.get(c, 0) + 1
                    pick.append(i)
            pick += [i for i in order if i not in pick]
            pick = pick[:top]
            pick.sort(key=lambda i: _rank(items[i], i))
            out.append(f"worst {len(pick)} of {len(items)} (max {cap} per code):")
            out += [_fmt_item(items[i]) for i in pick]
        if moves:
            mv = moves                      # already ranked by the command
            out.append(f"moves: {len(moves)} (top {min(top, len(mv))} by nets fixed):")
            out += [_clip(f"  {m['ref']} {m['move']}  fixes {m['fixes']}: {m['nets']}", 130)
                    for m in mv[:top]]
    else:
        lines = [l for l in raw.splitlines() if l.strip()]
        keep_h, keep_t = 12, 6
        if len(lines) <= keep_h + keep_t + 1:
            out += [_clip(l, 200) for l in lines[1:]]
        else:
            out += [_clip(l, 200) for l in lines[1:keep_h]]
            out.append(f"  ... {len(lines) - keep_h - keep_t} line(s) omitted")
            out += [_clip(l, 200) for l in lines[-keep_t:]]
    if paths:
        out.append("full report: " + "  ".join(paths) + "   (--full: everything; --top N)")
    txt = "\n".join(out)
    # hard budget: shrink message width until it fits
    w = MSG_WIDTH
    while (len(txt.encode()) > MAX_BYTES or len(out) > MAX_LINES) and w > 60:
        w -= 20
        out = [(_clip(l, w) if i else l) for i, l in enumerate(out)]
        if len(out) > MAX_LINES:
            keep = out[:MAX_LINES - 1] + [out[-1]]
            out = keep
        txt = "\n".join(out)
    return txt, parsed


def _report_base(a, cmd):
    for k in ("board", "spec", "mpn_map", "out", "target", "project"):
        v = getattr(a, k, None)
        if isinstance(v, str) and v and not v.startswith("-"):
            d = os.path.dirname(os.path.abspath(v))
            if os.path.isdir(d):
                stem = os.path.splitext(os.path.basename(v))[0]
                return os.path.join(d, f"{stem}.{cmd}")
    return os.path.join(os.getcwd(), f"fluxplace.{cmd}")


def _write(base, ext, text):
    for b in (base, os.path.join(tempfile.gettempdir(), os.path.basename(base))):
        try:
            with open(f"{b}.{ext}", "w") as fh:
                fh.write(text)
            return f"{b}.{ext}"
        except OSError:
            continue
    return None


@contextlib.contextmanager
def _quiet_stderr_noise():
    """pcbnew asserts straight to fd 2; capture it and replay minus the noise."""
    try:
        sys.stderr.flush()
        saved = os.dup(2)
        tmp = tempfile.TemporaryFile(mode="w+b")
        os.dup2(tmp.fileno(), 2)
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            sys.stderr.flush()
            os.dup2(saved, 2)
            os.close(saved)
            tmp.seek(0)
            data = tmp.read().decode(errors="replace")
            tmp.close()
            keep = [l for l in data.splitlines() if not _NOISE.match(l)]
            if keep:
                sys.stderr.write("\n".join(keep) + "\n")
        except Exception:
            pass


def run_compact(cmd, a, fn):
    """Run ``fn(a)`` capturing stdout; print the compact form; keep exit semantics."""
    full = getattr(a, "full_output", False) or getattr(a, "raw", False)
    if full:
        return fn(a)
    if cmd in ("drc-scope", "silk") and getattr(a, "json", False):
        return fn(a)                          # machine JSON requested
    quiet = getattr(a, "quiet", False)
    top = getattr(a, "top", None) or DEFAULT_TOP
    buf = io.StringIO()
    rc, err_msg = 0, None
    with _quiet_stderr_noise(), contextlib.redirect_stdout(buf):
        try:
            r = fn(a)
            if type(r) is int and r:              # a command that returns its verdict code
                rc = r
        except SystemExit as e:
            if e.code is None or e.code == 0:
                rc = 0
            elif isinstance(e.code, int):
                rc = e.code
            else:
                rc, err_msg = 1, str(e.code)
    raw = strip_noise(buf.getvalue())
    if err_msg:
        raw += "\n" + err_msg
    small = len(raw.encode()) <= MAX_BYTES - 300 and raw.count("\n") < MAX_LINES - 4
    base = _report_base(a, cmd)
    paths = []
    if not small:
        t = _write(base, "txt", raw + "\n")
        parsed_for_json = parse(raw)
        if t:
            paths.append(t)
        if not getattr(a, "json", None):
            j = _write(base, "json", json.dumps({
                "cmd": cmd, "exit": rc, "summary": parsed_for_json["summary"],
                "counts": {f"{k[0]} {k[1]}": n for k, n in parsed_for_json["counts"].items()
                           if k != "_table"} | parsed_for_json["counts"].get("_table", {}),
                "items": parsed_for_json["items"], "moves": parsed_for_json["moves"],
            }, indent=1))
            if j:
                paths.append(j)
    if quiet:
        text = verdict_line(cmd, rc, parse(raw), raw)
    elif small:
        p = parse(raw)
        body = raw.splitlines()
        if body and body[0].strip().lower().startswith(cmd):
            body = body[1:]                         # the verdict line carries it
        sm = pick_summary(cmd, raw, p)
        body = [l for l in body if not sm or l.strip() != sm]
        text = verdict_line(cmd, rc, p, raw) + ("\n" + "\n".join(body) if body else "")
    else:
        text, _ = compact_text(cmd, raw, rc, top=top, paths=paths)
    print(text)
    if rc:
        raise SystemExit(err_msg and 1 or rc)
    return None


def add_flags(ap, sub):
    """Register --full / -q / --top once on the top parser and each compact
    subcommand (subparser defaults are SUPPRESSed so they never clobber)."""
    import argparse
    def _add(p, dflt):
        kw = {} if dflt else {"default": argparse.SUPPRESS}
        p.add_argument("--full", dest="full_output", action="store_true",
                       help="print the command's complete output (default: compact)", **kw)
        p.add_argument("--raw", dest="raw", action="store_true", help=argparse.SUPPRESS, **kw)
        p.add_argument("-q", "--quiet", dest="quiet", action="store_true",
                       help="print only the one-line verdict", **kw)
        p.add_argument("--top", dest="top", type=int,
                       default=DEFAULT_TOP if dflt else argparse.SUPPRESS,
                       help="worst N items in the compact report (default %d)" % DEFAULT_TOP)
    _add(ap, True)
    for name in COMPACT_CMDS:
        p = sub.choices.get(name)
        if p is None:
            continue
        have = {s for act in p._actions for s in act.option_strings}
        if "--full" in have:                    # drc-scope: --full means something else
            p.add_argument("--raw", dest="raw", action="store_true",
                           default=argparse.SUPPRESS,
                           help="print the command's complete output (default: compact)")
            p.add_argument("-q", "--quiet", dest="quiet", action="store_true",
                           default=argparse.SUPPRESS, help="only the one-line verdict")
            p.add_argument("--top", dest="top", type=int, default=argparse.SUPPRESS,
                           help="worst N items (default %d)" % DEFAULT_TOP)
        else:
            _add(p, False)
