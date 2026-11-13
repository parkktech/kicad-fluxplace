"""Drawing-sheet title block of a .kicad_pcb: read, check, fill.

The `(title_block (title ..) (date ..) (rev ..) (company ..) (comment N ..))` token is text in the board
file (pcbnew 10 cannot be trusted with it through SWIG here, same as the paper), so it is read and
written as text: a fill changes ONLY that block, nothing else in the file.

  TITLEBLOCK_EMPTY      title or revision missing                  WARN (FAIL at fab / deliver / pcbway)
  TITLEBLOCK_REV_STALE  revision disagrees with the board version  FAIL

The board version is silk's resolution (`silk.resolve_version`: `[silk.title] version`, `[project] version`,
spec version, file-name suffix) WITHOUT the title block itself as a source. `fill` writes title (from
`[project] name`, else the spec's `name`; neither -> a question, never invented), revision (the resolved
version), date (ISO date of the run, injectable) and company (ONLY `[project] company` or
`[silk.title] company` of the constraints; never invented)."""
import datetime
import os
import re

HARD_STAGES = ("fab", "deliver", "pcbway")
FIELDS = ("title", "date", "rev", "company")


def block_span(text):
    """(start, end) of the `(title_block ...)` token (balanced, string aware) or None."""
    m = re.search(r"\(title_block\b", text)
    if not m:
        return None
    depth, i, n = 0, m.start(), len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return m.start(), i + 1
        i += 1
    return None


def _unq(s):
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_block(text):
    """{"present", "title", "date", "rev", "company", "comments": {n: text}} from board file text."""
    sp = block_span(text)
    out = {"present": sp is not None, "title": "", "date": "", "rev": "", "company": "", "comments": {}}
    if not sp:
        return out
    blk = text[sp[0]:sp[1]]
    for f in FIELDS:
        m = re.search(r'\(%s\s+"((?:[^"\\]|\\.)*)"\s*\)' % f, blk)
        if m:
            out[f] = _unq(m.group(1))
    for m in re.finditer(r'\(comment\s+(\d+)\s+"((?:[^"\\]|\\.)*)"\s*\)', blk):
        out["comments"][int(m.group(1))] = _unq(m.group(2))
    return out


def read(path):
    with open(path, errors="replace") as fh:
        return parse_block(fh.read())


def project_name(cons, spec):
    """(name, source) — `[project] name`, else the spec JSON's `name`; (None, None) when neither declares one."""
    n = ((cons or {}).get("project") or {}).get("name")
    if n and str(n).strip():
        return str(n).strip(), "[project] name"
    n = (spec or {}).get("name") if isinstance(spec, dict) else None
    if n and str(n).strip():
        return str(n).strip(), "spec name"
    return None, None


def company(cons):
    """The company string the constraints declare (`[project] company`, else `[silk.title] company`) or None."""
    for tbl in (((cons or {}).get("project") or {}), (((cons or {}).get("silk") or {}).get("title") or {})):
        c = tbl.get("company")
        if c and str(c).strip():
            return str(c).strip()
    return None


def board_version(board, cons, spec):
    """(version tuple, source) — silk's resolution, minus the title block (it is what is being checked)."""
    from . import silk as SK
    cfg = {"title": SK.title_settings(cons)}
    return SK.resolve_version(cfg, cons, spec, {}, board)


def _parse_rev(rev):
    from . import silk as SK
    m = re.fullmatch(r"\s*(?:[vV]|[rR]ev\.?\s?)?(\d+(?:\.\d+)*)\s*", rev or "")
    return SK.norm_version(m.group(1)) if m else None


def findings(tb, board, cons=None, spec=None, stage="review"):
    """[{level, code, msg, refs}] for a parsed title block `tb`."""
    out = []
    hard = stage in HARD_STAGES
    missing = [n for n, v in (("title", tb.get("title")), ("revision", tb.get("rev"))) if not (v or "").strip()]
    if missing:
        out.append({"level": "FAIL" if hard else "WARN", "code": "TITLEBLOCK_EMPTY", "refs": [],
                    "msg": f"drawing-sheet title block has no {' and no '.join(missing)}"
                           + ("" if tb.get("present") else " (no title block at all)")
                           + " — run `fluxplace title-block BOARD --out O` (or fit-sheet --fill-title-block)"})
    rev = (tb.get("rev") or "").strip()
    if rev:
        ver = board_version(board, cons, spec)
        if ver:
            from . import silk as SK
            have = _parse_rev(rev)
            if have != ver[0]:
                out.append({"level": "FAIL", "code": "TITLEBLOCK_REV_STALE", "refs": [],
                            "msg": f"title-block revision {rev!r} disagrees with the board version "
                                   f"{SK._vshow(ver[0])} (from {ver[1]}) — run `fluxplace title-block`"})
    return out


def render_block(tb):
    """KiCad-style multi-line `(title_block ...)`, tab indented to sit inside a top-level token."""
    rows = ["\t(title_block"]
    for f in ("title", "date", "rev", "company"):
        if tb.get(f):
            rows.append(f"\t\t({f} {_q(tb[f])})")
    for n in sorted(tb.get("comments", {})):
        rows.append(f"\t\t(comment {n} {_q(tb['comments'][n])})")
    rows.append("\t)")
    return "\n".join(rows)


def plan_fill(board, cons=None, spec=None, date=None):
    """-> (new field dict, notes, missing) ; `missing` lists what could not be filled from the records."""
    cur = read(board)
    new = dict(cur)
    new["comments"] = dict(cur["comments"])
    missing = []
    name, _src = project_name(cons, spec)
    if not cur["title"].strip():
        if name:
            new["title"] = name
        else:
            missing.append("title")
    ver = board_version(board, cons, spec)
    if ver:
        from . import silk as SK
        new["rev"] = SK._vshow(ver[0])
    elif not cur["rev"].strip():
        missing.append("revision")
    new["date"] = date or datetime.date.today().isoformat()
    co = company(cons)
    if co and not cur["company"].strip():
        new["company"] = co
    return new, missing


def write_block(text, tb):
    """Text with the title block replaced (or inserted after `(paper ..)`, else before `(layers`)."""
    blk = render_block(tb)
    sp = block_span(text)
    if sp:
        a = text.rfind("\n", 0, sp[0]) + 1                      # swallow the token's own indent
        return text[:a] + blk + text[sp[1]:]
    m = re.search(r'\n[ \t]*\(paper\b[^\n]*', text)
    if m:
        return text[:m.end()] + "\n" + blk + text[m.end():]
    m = re.search(r"\n[ \t]*\(layers", text)
    if not m:
        raise ValueError("no (paper ...) or (layers ...) token to place the title block before")
    return text[:m.start()] + "\n" + blk + text[m.start():]


def fill(board, out=None, cons=None, spec=None, date=None, session=None):
    """Fill the title block of `board`, writing to `out` (default: in place). `session` is a
    questions.Session: a title the records do not name is ASKED (the facility's exit code 3, nothing
    written). -> {"before", "after", "missing", "path"}."""
    new, missing = plan_fill(board, cons, spec, date)
    if "title" in missing and session is not None:
        ch = session.ask(
            "title-block-name", "title-block",
            "the board's title block has no title and neither `[project] name` (constraints) nor `name` (spec) "
            "declares the project's name",
            {"declare-name": "add `[project] name = \"...\"` to the constraints (or `name` to the spec JSON) and re-run",
             "leave-title-empty": "write revision, date and company only; TITLEBLOCK_EMPTY stays until a title is set"},
            "leave-title-empty", "[project] name in the constraints, or the spec JSON `name`")
        code = session.gate()
        if code:
            raise SystemExit(code)
        if ch == "declare-name":
            raise SystemExit("title-block: answer was declare-name — declare it, then re-run")
    with open(board) as fh:
        text = fh.read()
    before = parse_block(text)
    res = write_block(text, new)
    dst = out or board
    with open(dst, "w") as fh:
        fh.write(res)
    return {"before": before, "after": parse_block(res), "missing": missing, "path": dst}


def summary_line(path, cons=None, spec=None, stage="review"):
    """One line for fit-sheet's summary: the title block state."""
    tb = read(path)
    f = findings(tb, path, cons, spec, stage)
    if not f:
        return f"title block: {tb['title']!r} rev {tb['rev']!r}"
    return "title block: " + "; ".join(f"{x['level']} {x['code']}" for x in f)
