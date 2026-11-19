"""Board-version registry: `fluxplace versions` answers "which board are we on".

The registry (default docs/resume/boards.json; env FLUXPLACE_VERSIONS, relative to the root) holds every board version:
status, parent, decisions, the CURRENT board and its accepted history, rejected boards. Exactly one version is active.
<version>/CURRENT.kicad_pcb links to the active version's current board. Every other .kicad_pcb in a version folder is
scratch until `accept` names it. One resume file per version (RESUME-<version>.md, in the registry's folder) carries a
NOW block, rewritten before and after every step.

  versions status [--brief]                 active version, CURRENT, metrics, consistency warnings
  versions step "what starts now"           write NOW "Step in progress" (before a step), commit
  versions accept BOARD --note N [--next X] [--allow-worse]
                                            measure (DRC), refuse if opens/shorts/errors rose, set CURRENT, NOW, commit
                                            exit 4: "no change" (same copper as CURRENT); nothing is written
  versions reject BOARD --note N            record a tried-and-rejected board, commit
  versions new VERSION --from BOARD --reason R [--decisions a,b]
                                            new version folder (+ config links), start board, RESUME file, old one closed
  versions init --version V --board B [--title T] [--decisions a,b]
                                            create the registry, CURRENT and RESUME-V (refuses if a registry exists)
  versions start-context [--hook-json]      start rule, status, NOW block, warnings; never fails (exit 0)

Registry key "config" (every field optional): folder_pattern ("W{n}"), links ({name: relative target} created in a new
version folder), commit (true: git add + commit the registry and resume dir after writes), start_rule (text printed by
start-context).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import datetime

from fluxplace import progress as PG

REG_DEFAULT = os.path.join("docs", "resume", "boards.json")
START_RULE = ("before any other action: (1) read the status and NOW block below; if there is any WARNING, resolve it "
              "first; (2) bump the session counter in NOW; (3) work only on the CURRENT board (<version>/CURRENT.kicad_pcb). "
              "Every step: `fluxplace versions step \"...\"` before it, then `fluxplace versions accept BOARD --note` or "
              "`fluxplace versions reject BOARD --note` after it. A new board version only via `fluxplace versions new`. "
              "Never pick a board by file date or name.")
CONFIG_DEFAULT = {"folder_pattern": "W{n}", "links": {}, "commit": True, "start_rule": START_RULE}
CTX_MAX = 2000
ROOT = REG = RES = None


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _git_top():
    try:
        p = subprocess.run(["git", "-C", os.getcwd(), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    except OSError:
        return None
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def _setup(explicit):
    """Set ROOT / REG / RES: --root, else the git toplevel of the cwd, else the cwd."""
    global ROOT, REG, RES
    ROOT = os.path.abspath(explicit) if explicit else (_git_top() or os.getcwd())
    REG = os.path.join(ROOT, os.environ.get("FLUXPLACE_VERSIONS") or REG_DEFAULT)
    RES = os.path.dirname(REG)


def rel(p):
    return os.path.relpath(os.path.abspath(p), ROOT)


def config(r):
    c = json.loads(json.dumps(CONFIG_DEFAULT))
    c.update(r.get("config") or {})
    return c


def load():
    if not os.path.exists(REG):
        sys.exit("no registry at %s: create one with `fluxplace versions init --version V --board B`" % rel(REG))
    return json.load(open(REG))


def save(r):
    os.makedirs(RES, exist_ok=True)
    json.dump(r, open(REG, "w"), indent=1)
    open(REG, "a").write("\n")


def active(r):
    return r["active"], r["versions"][r["active"]]


def _xy(p):
    return (int(p.x), int(p.y))


def copper_fingerprint(path):
    """sha1 of a board's copper (pcbnew, nm): tracks and arcs (class, net, layer, start, end, width, mid),
    vias (net, pos, diameter, drill), footprints (ref, pos, rotation, layer), zones (net, layers, priority, keepout,
    outline points rounded to 0.001 mm). Zone fill is not hashed: it varies run to run."""
    import pcbnew
    b = pcbnew.LoadBoard(path)
    rows = []
    for t in b.GetTracks():
        cls = t.GetClass()
        if cls == "PCB_VIA":
            rows.append(("via", t.GetNetname(), _xy(t.GetPosition()), t.GetWidth(pcbnew.F_Cu),
                         t.GetWidth(pcbnew.B_Cu), t.GetDrillValue()))
        else:
            mid = _xy(t.GetMid()) if cls == "PCB_ARC" else None
            rows.append((cls, t.GetNetname(), t.GetLayer(), _xy(t.GetStart()), _xy(t.GetEnd()), t.GetWidth(), mid))
    for f in b.GetFootprints():
        rows.append(("fp", f.GetReference(), _xy(f.GetPosition()), round(f.GetOrientationDegrees(), 6), f.GetLayer()))
    for z in b.Zones():
        ol = z.Outline()
        pts = sorted((round(c.x / 1e6, 3), round(c.y / 1e6, 3))
                     for i in range(ol.OutlineCount())
                     for c in (ol.Outline(i).CPoint(j) for j in range(ol.Outline(i).PointCount())))
        rows.append(("zone", z.GetNetname(), tuple(z.GetLayerSet().Seq()), z.GetAssignedPriority(),
                     bool(z.GetIsRuleArea()), pts))
    # board drawings on fabricated layers (copper, mask, paste, silk, edge): e.g. a mask opening added after routing is a
    # real change. Documentation layers (User.*, Cmts, Dwgs, Eco, Margin, Fab, CrtYd) are ignored.
    doc = {"Dwgs.User", "Cmts.User", "Eco1.User", "Eco2.User", "Margin", "F.Fab", "B.Fab", "F.CrtYd", "B.CrtYd"}
    for d in b.GetDrawings():
        if d.GetClass() != "PCB_SHAPE" or b.GetLayerName(d.GetLayer()) in doc or b.GetLayerName(d.GetLayer()).startswith("User."):
            continue
        poly = []
        if d.GetShape() == pcbnew.SHAPE_T_POLY:
            ps = d.GetPolyShape()
            poly = sorted((round(c.x / 1e6, 3), round(c.y / 1e6, 3))
                          for i in range(ps.OutlineCount()) for c in (ps.Outline(i).CPoint(j) for j in range(ps.Outline(i).PointCount())))
        rows.append(("shape", b.GetLayerName(d.GetLayer()), int(d.GetShape()), _xy(d.GetStart()), _xy(d.GetEnd()),
                     d.GetWidth(), bool(d.IsAnyFill()), poly))
    return hashlib.sha1(json.dumps(sorted(repr(r) for r in rows)).encode()).hexdigest()


def measure(board):
    """Error-level DRC of a temp copy (zones refilled): unconnected, shorts, errors, ... (progress.measure)."""
    return PG.measure(board)


def fmt(m):
    return "%d opens, %d shorts, %d errors" % (m["unconnected"], m["shorts"], m["errors"])


def resume_path(v):
    return os.path.join(RES, "RESUME-%s.md" % v)


def set_now(v, **lines):
    """Rewrite named lines of the NOW block in RESUME-<v>.md; the header carries the update time."""
    p = resume_path(v)
    if not os.path.exists(p):
        sys.exit("refused: no resume file %s" % rel(p))
    t = open(p).read()
    a, b = t.index("<!-- now -->"), t.index("<!-- /now -->")
    blk = t[a:b]
    blk = re.sub(r"updated [0-9-]+ [0-9:]+", "updated " + now(), blk, count=1)
    for key, val in lines.items():
        label = {"board": "Board version", "state": "Board state", "step": "Step in progress", "next": "Next step"}[key]
        pat = re.compile(r"^- %s:.*?(?=^- |\Z)" % re.escape(label), re.S | re.M)
        new = "- %s: %s\n" % (label, val)
        blk = pat.sub(lambda _m: new, blk, count=1) if pat.search(blk) else blk.rstrip("\n") + "\n" + new
    open(p, "w").write(t[:a] + blk + t[b:])


def link_current(v, board):
    d = os.path.join(ROOT, v)
    os.makedirs(d, exist_ok=True)
    for ext in (".kicad_pcb", ".kicad_pro"):
        ln = os.path.join(d, "CURRENT" + ext)
        src = os.path.splitext(board)[0] + ext
        if os.path.lexists(ln):
            os.unlink(ln)
        if os.path.exists(src):
            os.symlink(os.path.relpath(os.path.abspath(src), d), ln)


def _folder_re(cfg):
    """folder_pattern as a regex: literal text, {n} = one or more digits."""
    return re.compile(r"(\d+)".join(re.escape(p) for p in cfg["folder_pattern"].split("{n}")))


def _natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def commit(msg, cfg):
    """git add + commit of the registry and the resume dir; quiet no-op when not a git repo or commit = false."""
    if not cfg.get("commit", True):
        return
    try:
        if subprocess.run(["git", "-C", ROOT, "rev-parse", "--is-inside-work-tree"], capture_output=True).returncode != 0:
            return
        files = [REG] + [os.path.join(RES, f) for f in os.listdir(RES)]
        subprocess.run(["git", "-C", ROOT, "add", "--"] + files, capture_output=True)
        # only these paths: never sweep in whatever else the user has staged
        subprocess.run(["git", "-C", ROOT, "commit", "-q", "-m", msg, "--"] + files, capture_output=True)
    except OSError:
        return


def warnings(r):
    cfg = config(r)
    v, e = active(r)
    w = []
    cur = os.path.join(ROOT, e["current"])
    if not os.path.exists(cur):
        w.append("CURRENT board missing on disk: " + e["current"])
    ln = os.path.join(ROOT, v, "CURRENT.kicad_pcb")
    if not os.path.islink(ln) or os.path.realpath(ln) != os.path.realpath(cur):
        w.append("%s/CURRENT.kicad_pcb does not point at the registry's current board" % v)
    pat = _folder_re(cfg)
    folders = [d for d in os.listdir(ROOT) if pat.fullmatch(d) and os.path.isdir(os.path.join(ROOT, d))]
    for d in sorted(set(folders) - set(r["versions"]), key=_natkey):
        w.append("%s exists but is not in the registry: register it (fluxplace versions new) or delete it" % d)
    if not os.path.exists(resume_path(v)):
        w.append("no resume file for the active version: %s" % rel(resume_path(v)))
    else:
        t = open(resume_path(v)).read()
        if v not in t[t.find("<!-- now -->"):t.find("<!-- /now -->")]:
            w.append("NOW block does not name the active version " + v)
    if os.path.exists(cur):
        mt = os.path.getmtime(cur)
        newer = [rel(os.path.join(dp, f)) for dp, _, fs in os.walk(os.path.join(ROOT, v)) for f in fs
                 if f.endswith(".kicad_pcb") and not f.startswith("CURRENT") and ".session" not in dp
                 and "/undo" not in dp and os.path.getmtime(os.path.join(dp, f)) > mt + 60]
        tried = {h["board"] for h in e.get("rejected", [])} | {h["board"] for h in e.get("history", [])}
        newer = [n for n in newer if n not in tried and not re.search(r"\.(i\d+|guard|try)\.kicad_pcb$|\.kicad_pcb\.", n)]
        if newer:
            w.append("%d board file(s) newer than CURRENT, neither accepted nor rejected (scratch?): %s"
                     % (len(newer), ", ".join(sorted(newer)[:4])))
    return w


def _status_lines(r, brief):
    v, e = active(r)
    h = e["history"][-1] if e.get("history") else {}
    lines = ["ACTIVE BOARD VERSION: %s (%s)  decisions: %s" % (v, e.get("title", ""), ",".join(e.get("decisions", []))),
             "CURRENT: %s  [%s/CURRENT.kicad_pcb]" % (e["current"], v),
             "state: %s  (accepted %s: %s)" % (fmt(h["metrics"]) if h else "unmeasured", h.get("time", "-"), h.get("note", "-"))]
    if not brief:
        for k, x in sorted(r["versions"].items(), key=lambda kv: _natkey(kv[0])):
            if k != v:
                lines.append("  %s %s: %s" % (k, x["status"], x.get("reason", "")))
    return lines


def cmd_status(a):
    r = load()
    print("\n".join(_status_lines(r, a.brief) + ["WARNING: " + w for w in warnings(r)]))


def cmd_step(a):
    r = load()
    v, _ = active(r)
    set_now(v, step="%s (started %s)" % (a.text, now()))
    commit("resume %s: step started: %s" % (v, a.text), config(r))
    print("NOW: step in progress = " + a.text)


def cmd_accept(a):
    r = load()
    cfg = config(r)
    v, e = active(r)
    b = os.path.abspath(a.board)
    if not rel(b).startswith(v + os.sep):
        sys.exit("refused: %s is not inside the active version %s/" % (rel(b), v))
    cur = e.get("current")
    if cur and os.path.exists(os.path.join(ROOT, cur)) and \
            copper_fingerprint(b) == copper_fingerprint(os.path.join(ROOT, cur)):
        print("no change: %s has the same copper as CURRENT (%s)" % (rel(b), cur))
        sys.exit(4)
    m = measure(b)
    islands = m.pop("islands", None)
    prev = e["history"][-1]["metrics"] if e.get("history") else None
    if prev and not a.allow_worse:
        worse = [k for k in ("shorts", "unconnected", "errors") if m[k] > prev[k]]
        if worse:
            sys.exit("refused: %s rose (%s -> %s). Use reject, or --allow-worse with a note saying why."
                     % (",".join(worse), fmt(prev), fmt(m)))
    try:
        PG.checkpoint(b, workdir=os.path.join(ROOT, v), note=a.note, metrics=dict(m),
                      measure_fn=lambda _b: dict(m, islands=islands), allow_new_shorts=a.allow_worse)
    except ValueError as ex:
        sys.exit("refused: %s" % ex)
    e["current"] = rel(b)
    e.setdefault("history", []).append({"time": now(), "board": rel(b), "metrics": m, "note": a.note})
    save(r)
    link_current(v, b)
    st = "%s. CURRENT = `%s` (%s/CURRENT.kicad_pcb)" % (fmt(m), rel(b), v)
    kw = {"state": st, "step": "none (last: %s)" % a.note}
    if a.next:
        kw["next"] = a.next
    set_now(v, **kw)
    commit("board %s: accept %s (%s): %s" % (v, os.path.basename(b), fmt(m), a.note), cfg)
    print("accepted %s: %s (was %s)" % (rel(b), fmt(m), fmt(prev) if prev else "-"))


def cmd_reject(a):
    r = load()
    v, e = active(r)
    e.setdefault("rejected", []).append({"time": now(), "board": rel(a.board), "note": a.note})
    save(r)
    set_now(v, step="none (last rejected: %s: %s)" % (os.path.basename(a.board), a.note))
    commit("board %s: reject %s: %s" % (v, os.path.basename(a.board), a.note), config(r))
    print("rejected " + rel(a.board))


def _decisions(s):
    return [x for x in (s or "").split(",") if x]


def _resume_text(v, intro, m, cur, session=1):
    return ("# Resume — board version %s\n\n%s\n\n<!-- now -->\n## NOW (%s session %d, updated %s; rewrite this block "
            "BEFORE each step and after its result)\n- Board version: **%s**. Run `fluxplace versions status` for CURRENT.\n"
            "- Board state: %s. CURRENT = `%s`\n- Step in progress: none.\n- Next step: (set it)\n<!-- /now -->\n\n"
            "## STATUS (%s)\n\n" % (v, intro, v, session, now(), v, fmt(m), cur, v))


def cmd_init(a):
    if os.path.exists(REG):
        sys.exit("refused: registry %s already exists" % rel(REG))
    v = a.version
    b = os.path.abspath(a.board)
    if not os.path.isfile(b):
        sys.exit("refused: no board file " + a.board)
    if not rel(b).startswith(v + os.sep):
        sys.exit("refused: %s is not inside %s/" % (rel(b), v))
    m = measure(b)
    cfg = json.loads(json.dumps(CONFIG_DEFAULT))
    r = {"config": cfg, "active": v,
         "versions": {v: {"status": "active", "title": a.title or "", "decisions": _decisions(a.decisions),
                          "current": rel(b), "history": [{"time": now(), "board": rel(b), "metrics": m, "note": "init"}]}}}
    save(r)
    link_current(v, b)
    open(resume_path(v), "w").write(_resume_text(v, "Started from `%s`. Title: %s" % (rel(b), a.title or "-"), m, rel(b)))
    commit("board %s: init registry (%s)" % (v, fmt(m)), cfg)
    print("initialised %s; CURRENT = %s (%s)" % (v, rel(b), fmt(m)))


def cmd_new(a):
    r = load()
    cfg = config(r)
    old, oe = active(r)
    v = a.version
    if not _folder_re(cfg).fullmatch(v):
        sys.exit("refused: %s does not match folder_pattern %s" % (v, cfg["folder_pattern"]))
    if v in r["versions"]:
        sys.exit("refused: %s already registered" % v)
    src = os.path.abspath(a.src)
    if not os.path.isfile(src):
        sys.exit("refused: no board file " + a.src)
    d = os.path.join(ROOT, v)
    os.makedirs(d, exist_ok=True)
    for name, target in cfg["links"].items():
        if not os.path.lexists(os.path.join(d, name)):
            os.symlink(target, os.path.join(d, name))
    start = os.path.join(d, "start.kicad_pcb")
    shutil.copy(src, start)
    pro = os.path.splitext(src)[0] + ".kicad_pro"
    if os.path.exists(pro):
        shutil.copy(pro, os.path.join(d, "start.kicad_pro"))
    m = measure(start)
    oe["status"] = "superseded"
    oe["reason"] = "by %s: %s" % (v, a.reason)
    r["versions"][v] = {"status": "active", "parent": old, "from": rel(src), "title": a.reason,
                        "decisions": _decisions(a.decisions or ",".join(oe.get("decisions", []))),
                        "current": rel(start), "history": [{"time": now(), "board": rel(start), "metrics": m,
                                                            "note": "start, from " + rel(src)}]}
    r["active"] = v
    save(r)
    link_current(v, start)
    if os.path.exists(resume_path(old)):
        t = open(resume_path(old)).read()
        t = t.replace("<!-- now -->", "<!-- closed -->\n**CLOSED %s: superseded by `%s` (%s).**\n<!-- was-now -->"
                      % (now(), rel(resume_path(v)), a.reason), 1).replace("<!-- /now -->", "<!-- /was-now -->", 1)
        open(resume_path(old), "w").write(t)
    intro = "Parent: %s (`%s`). Started from `%s`. Reason: %s" % (old, rel(resume_path(old)), rel(src), a.reason)
    open(resume_path(v), "w").write(_resume_text(v, intro, m, rel(start)))
    commit("board %s: new version from %s: %s" % (v, rel(src), a.reason), cfg)
    print("active %s; start %s (%s); %s superseded" % (v, rel(start), fmt(m), old))


def _now_block(v):
    p = resume_path(v)
    if not os.path.exists(p):
        return "(no resume file: %s)" % rel(p)
    t = open(p).read()
    a, b = t.find("<!-- now -->"), t.find("<!-- /now -->")
    if a < 0 or b < 0:
        return "(no NOW block in %s)" % rel(p)
    return t[a + len("<!-- now -->"):b].strip()


def _start_text():
    if not os.path.exists(REG):
        return "WARNING: no board registry at %s (create one: fluxplace versions init --version V --board B)" % rel(REG)
    r = load()
    cfg = config(r)
    v, _ = active(r)
    head = "\n".join([cfg["start_rule"], ""] + _status_lines(r, brief=True)) + "\n"
    warns = "\n".join("WARNING: " + w for w in warnings(r))
    now_txt = _now_block(v)
    fixed = len(head) + len("\n") + len(warns) + len("\n") + len("\n...(NOW truncated)")
    if fixed + len(now_txt) > CTX_MAX:
        now_txt = now_txt[:max(0, CTX_MAX - fixed - 20)] + "\n...(NOW truncated)"
    text = head + "\n" + now_txt + ("\n" + warns if warns else "")
    if len(text) > CTX_MAX:
        text = text[:CTX_MAX - 20] + "\n...(truncated)"
    return text


def _emit(text, hook):
    if hook:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    else:
        print(text)


def cmd_start_context(a):
    """Never raises: any error becomes a one-line warning, exit 0 (it runs as a SessionStart hook)."""
    try:
        text = _start_text()
    except (Exception, SystemExit) as ex:                            # noqa: BLE001 - the hook must not fail
        text = "WARNING: start-context: %s: %s" % (type(ex).__name__, ex)
    _emit(text, a.hook_json)


def run(a):
    _setup(getattr(a, "root", None))
    return a.f(a)


def add_subparsers(pv):
    """The `versions ACTION` subcommands (shared by cli.py and main)."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=None, help="repo root (default: git toplevel of cwd, else cwd)")
    vs = pv.add_subparsers(dest="vcmd", required=True, metavar="ACTION")
    p = vs.add_parser("status", parents=[common], help="active version, CURRENT, metrics, warnings")
    p.add_argument("--brief", action="store_true"); p.set_defaults(f=cmd_status)
    p = vs.add_parser("step", parents=[common], help="write NOW 'Step in progress' before a step")
    p.add_argument("text"); p.set_defaults(f=cmd_step)
    p = vs.add_parser("accept", parents=[common], help="measure, refuse if worse, set CURRENT, NOW, commit")
    p.add_argument("board"); p.add_argument("--note", required=True); p.add_argument("--next")
    p.add_argument("--allow-worse", action="store_true"); p.set_defaults(f=cmd_accept)
    p.epilog = "exit 4: no change (the board has the same copper as CURRENT); nothing is recorded or moved"
    p = vs.add_parser("reject", parents=[common], help="record a rejected board")
    p.add_argument("board"); p.add_argument("--note", required=True); p.set_defaults(f=cmd_reject)
    p = vs.add_parser("new", parents=[common], help="new board version from a board file")
    p.add_argument("version"); p.add_argument("--from", dest="src", required=True)
    p.add_argument("--reason", required=True); p.add_argument("--decisions"); p.set_defaults(f=cmd_new)
    p = vs.add_parser("init", parents=[common], help="create the registry with the first CURRENT board")
    p.add_argument("--version", required=True); p.add_argument("--board", required=True)
    p.add_argument("--title"); p.add_argument("--decisions"); p.set_defaults(f=cmd_init)
    p = vs.add_parser("start-context", parents=[common], help="SessionStart text: rule, status, NOW, warnings")
    p.add_argument("--hook-json", action="store_true", help="print the SessionStart hookSpecificOutput JSON")
    p.set_defaults(f=cmd_start_context)
    return vs


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fluxplace versions", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_subparsers(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
