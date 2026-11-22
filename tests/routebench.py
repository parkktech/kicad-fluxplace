"""routebench: routing benchmark harness (ROUTER-C0). Dev script, not a user command.

Part 1 (B2): argparse, stage (copy + optional strip), metrics. Parts 2-3 (B3, B4) add engines, rows, output, baseline.
Interpreter: /usr/bin/python3. Never run a router from pytest: the engines are stubbed in tests.
"""
import argparse, datetime, glob, json, os, re, shutil, subprocess, sys, time
from collections import Counter

from fluxplace import boardwrite, constraints, corpus as C, drcguard, gaps, routefr, routerloc
from fluxplace.pipeline import state_path

KEYS = ("opens", "shorts", "errors", "new_err_keys", "err_types", "completion", "pairs", "rails", "score",
        "vias", "tracks", "track_mm", "tool", "board_sha")
SIDECARS = (".kicad_pro", ".kicad_prl", ".kicad_dru")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSONL = os.path.join(ROOT, "docs", "corpus", "routebench.jsonl")
ENGINE_ROWS = {"krt": ["krt"], "freerouting": ["freerouting", "freerouting+krt"], "kernel": ["kernel"]}
ENGINES = {}
_NET_DECL = re.compile(r'\(net\s+(?:\d+\s+)?"([^"]*)"\)')


def _try(fn, *a, **kw):
    """One optional helper: its failure gives None for its keys, never a lost row."""
    try:
        return fn(*a, **kw)
    except Exception:
        return None


def net_names(text):
    return sorted({n for n in _NET_DECL.findall(text) if n})


def stage(ent, work):
    """Copy the board and its project sidecars into work/<name>/. The source is never written.
    When ent["bench"]["strip"] is true, the copy loses every track, arc and via of its nets (zones stay)."""
    d = os.path.join(work, ent["name"])
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    base = os.path.splitext(ent["board"])[0]
    board = os.path.join(d, os.path.basename(ent["board"]))
    shutil.copy2(ent["board"], board)
    for ext in SIDECARS:
        if os.path.isfile(base + ext):
            shutil.copy2(base + ext, os.path.join(d, os.path.basename(base) + ext))
    if ent.get("bench", {}).get("strip"):
        with open(board, encoding="utf-8") as f:
            text = f.read()
        text, _n = boardwrite.strip_nets(text, net_names(text))
        with open(board, "w", encoding="utf-8") as f:
            f.write(text)
    return board


def _pairs(board, cons):
    rows = gaps._pair_audit(board, cons)[2]
    out = {"complete": 0, "mismatch": 0, "half": 0, "open": 0}
    for r in rows:
        if r.get("status") in out:
            out[r["status"]] += 1
    return out


def _rails(gl):
    narrow = [g for g in gl if g["kind"] == "power-width"]
    vias = [g for g in gl if g["kind"] == "power-vias"]
    return {"narrow": len(narrow), "vias": len(vias),
            "mag": round(sum(float(g.get("mag", 0) or 0) for g in narrow + vias), 3)}


def metrics(board, cons=None):
    """Metrics of one board (table in ROUTER-C0 'Metrics'). Every optional part is in its own try: a failing
    helper gives null for its keys. Without constraints pairs, rails and score are null."""
    out = {k: None for k in KEYS}
    m = _try(gaps.measure, board)
    if m is not None:
        out.update(opens=m["opens"], shorts=m["shorts"], errors=m["errors"],
                   err_types=[[str(k), n] for k, n in Counter(
                       str(v.get("type")) for v in (m["drc"].get("violations") or [])
                       if v.get("severity") == "error").most_common(5)],
                   _err_keys={str(k): n for k, n in m["err_keys"].items()})
    if cons:
        if m is not None:
            gl = _try(gaps.classify, m["drc"], cons, board=board) or []
            out["pairs"] = _try(_pairs, board, cons)
            out["rails"] = _rails(gl)
            out["score"] = _try(gaps.score, m, gl)
    pcb = _try(C._pcb_metrics, board) or {}
    out["vias"] = pcb.get("board.vias")
    out["tracks"] = pcb.get("board.tracks")
    out["track_mm"] = pcb.get("board.track_mm")
    return out


def _reg(name):
    def deco(fn):
        ENGINES[name] = fn
        return fn
    return deco


def _pipeline_cmd(copy, work, ent, router):
    """The baseline invocation of ROUTER-C0 (as W14/scripts/run-w14-build.sh does). No --constraints without constraints."""
    budget = int(ent["bench"].get("budget_s", 1800))
    cmd = ["/usr/bin/python3", os.path.join(ROOT, "cli.py"), "pipeline", copy, "--workdir", work]
    if "constraints" in ent:
        cmd += ["--constraints", ent["constraints"]]
    if "spec" in ent:
        cmd += ["--spec", ent["spec"]]
    return cmd + ["--flow", "constructive", "--from", "route", "--to", "route", "--router", router,
                  "--route-budget", str(budget), "--assume-defaults", "--allow-incomplete", "--allow-undocumented",
                  "--no-api", "--report-dir", os.path.join(work, "reports")]


def _route_stage(work):
    try:
        with open(state_path(work), encoding="utf-8") as fh:
            return ((json.load(fh).get("stages") or {}).get("route")) or {}
    except (OSError, ValueError):
        return {}


def _pipeline_run(copy, work, ent, router, boards_of):
    """Run one pipeline route (budget_s + 120 s wall limit) and return the runner record. Never raises on rc."""
    budget = int(ent["bench"].get("budget_s", 1800))
    os.makedirs(work, exist_ok=True)
    log = os.path.join(work, "run.log")
    t0 = time.monotonic()
    with open(log, "w", encoding="utf-8") as fh:
        p = subprocess.run(_pipeline_cmd(copy, work, ent, router), cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                           timeout=budget + 120)
    wall = round(time.monotonic() - t0, 1)
    rs = _route_stage(work)
    return {"rc": p.returncode, "wall_s": wall, "log": log, "boards": boards_of(work, rs),
            "version": _try(lambda: str(routefr.jar_version(routefr.locate()["jar"])))}


@_reg("krt")
def _krt(copy, work, ent):
    def boards(w, rs):
        return {"krt": os.path.join(w, rs["board"])} if rs.get("board") else {}
    r = _pipeline_run(copy, work, ent, "krt-whole", boards)
    r["version"] = _try(lambda: "{version}@{commit}".format(**routerloc.resolve()))
    r["wall"] = {"krt": r["wall_s"]}
    return r


@_reg("freerouting")
def _freerouting(copy, work, ent):
    def boards(w, rs):
        out = {}
        if rs.get("dir"):
            out["freerouting"] = os.path.join(w, rs["dir"], "freerouting.kicad_pcb")
        if rs.get("board"):
            out["freerouting+krt"] = os.path.join(w, rs["board"])
        return {k: v for k, v in out.items() if os.path.isfile(v)}
    r = _pipeline_run(copy, work, ent, "freerouting", boards)
    r["wall"] = {k: r["wall_s"] for k in r["boards"]}
    return r


@_reg("kernel")
def _kernel(copy, work, ent):
    """n/a until fluxplace.kmodel exposes route_board (C2)."""
    try:
        from fluxplace import kmodel
        kmodel.route_board
    except (ImportError, AttributeError):
        return {"na": True, "rc": None, "wall_s": 0.0, "boards": {}, "log": None, "version": None, "wall": {}}
    raise NotImplementedError("kernel engine is C2")


def _newest(work):
    files = glob.glob(os.path.join(work, "**", "*.kicad_pcb"), recursive=True)
    return {"newest": max(files, key=os.path.getmtime)} if files else {}


def _tail(path, n=300):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()[-n:]
    except (OSError, TypeError):
        return None


def _start(m):
    return {k: m.get(k) for k in ("opens", "shorts", "errors", "vias", "tracks", "track_mm", "score")}


def _end(start, m, end_keys):
    e = {k: m.get(k) for k in ("opens", "shorts", "errors", "err_types", "pairs", "rails", "score", "vias",
                                "tracks", "track_mm")}
    base = Counter(start.get("_err_keys") or {})
    e["new_err_keys"] = sum(drcguard.new_keys(base, Counter(end_keys or {})).values())
    e["d_vias"] = (m.get("vias") or 0) - (start.get("vias") or 0)
    e["d_track_mm"] = round((m.get("track_mm") or 0.0) - (start.get("track_mm") or 0.0), 1)
    return e


def bench_engine(ent, copy, start_m, engine, work, cons, tool, sha):
    """Rows of one engine run on one board. Every failure becomes a row (status error|timeout), never a crash."""
    names = ENGINE_ROWS[engine]
    budget = int(ent["bench"].get("budget_s", 1800))
    timed_out, res = False, {}
    t0 = time.monotonic()
    try:
        res = ENGINES[engine](copy, work, ent)
    except subprocess.TimeoutExpired:
        timed_out = True
        newest = _newest(work).get("newest")
        res = {"rc": None, "wall_s": round(time.monotonic() - t0, 1), "log": os.path.join(work, "run.log"),
               "boards": {names[0]: newest} if newest else {}, "version": None}
    except Exception as exc:                                      # a runner crash is an error row
        res = {"rc": None, "wall_s": round(time.monotonic() - t0, 1), "log": os.path.join(work, "run.log"),
               "boards": {}, "version": None, "exc": repr(exc)}
    rows = []
    for name in names:
        bpath = (res.get("boards") or {}).get(name)
        wall = (res.get("wall") or {}).get(name, res.get("wall_s"))
        status, err, end = "error", None, None
        if res.get("na"):
            status = "n/a"
        elif res.get("exc"):
            err = res["exc"]
        elif timed_out:
            status = "timeout"
        elif res.get("rc") not in (0, 1) or not bpath or not os.path.isfile(bpath):
            err = res.get("exc") or _tail(res.get("log")) or f"rc {res.get('rc')}, no board"
        else:
            status = "ok"
        if status in ("ok", "timeout") and bpath and os.path.isfile(bpath):
            m = metrics(bpath, cons)
            end = _end(start_m, m, m.get("_err_keys"))
        comp = None
        if end and start_m.get("opens") and end.get("opens") is not None:
            comp = round(1 - end["opens"] / start_m["opens"], 4)
        rows.append({"board": ent["name"], "task": ent["bench"].get("task"), "engine": name, "status": status,
                     "start": _start(start_m), "end": end, "completion": comp,
                     "wall_s": wall, "budget_s": budget, "rc": res.get("rc"), "engine_version": res.get("version"),
                     "board_sha": sha, "tool": tool, "out_board": bpath, "log": res.get("log"), "error": err})
    return rows


def write_status(out, state, done, total, current):
    with open(os.path.join(out, "routebench.status.json"), "w", encoding="utf-8") as fh:
        json.dump({"state": state, "done": done, "total": total, "current": current}, fh)


def write_outputs(rows, out, meta, jsonl=JSONL):
    """routebench.json (+ md table) in out; one jsonl line per row appended to the committed corpus file."""
    os.makedirs(out, exist_ok=True)
    doc = {"schema": 1, **meta, "rows": rows}
    with open(os.path.join(out, "routebench.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, default=str)
    lines = [json.dumps({**r, "ts": meta["ts"], "tool": meta["tool"]}, default=str) for r in rows]
    if jsonl:
        os.makedirs(os.path.dirname(jsonl), exist_ok=True)
        with open(jsonl, "a", encoding="utf-8") as fh:
            fh.write("".join(l + "\n" for l in lines))
    md = md_table(rows)
    with open(os.path.join(out, "routebench.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    return md


def md_table(rows):
    cols = ["board", "engine", "status", "opens", "errors", "pairs", "rails narrow", "d_vias", "d_track_mm", "wall_s"]
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        s, e = r["start"], r["end"] or {}
        pr = e.get("pairs") or {}
        ptot = sum(pr.values()) if pr else None
        out.append("| " + " | ".join(str(x) for x in [
            r["board"], r["engine"], r["status"], f"{s.get('opens')} -> {e.get('opens')}", e.get("errors"),
            f"{pr.get('complete')}/{ptot}" if pr else "-", (e.get("rails") or {}).get("narrow"),
            e.get("d_vias"), e.get("d_track_mm"), r["wall_s"]]) + " |")
    return "\n".join(out) + "\n"


def run_all(boards, engines, work, out, tool, ts, jsonl=JSONL):
    total = sum(len(ENGINE_ROWS[e]) for _ in boards for e in engines)
    rows, done = [], 0
    os.makedirs(out, exist_ok=True)
    write_status(out, "running", 0, total, None)
    for ent in boards:
        copy = stage(ent, work)
        cons = constraints.load(ent["constraints"]) if "constraints" in ent else None
        start_m = metrics(copy, cons)
        start_m["_err_keys"] = {str(k): v for k, v in (start_m.get("_err_keys") or {}).items()}
        sha = C.sha256(ent["board"])
        for eng in engines:
            write_status(out, "running", done, total, f"{ent['name']}/{eng}")
            new = bench_engine(ent, copy, start_m, eng, os.path.join(work, ent["name"], eng), cons, C.git_sha(), sha)
            rows += new
            done += len(new)
            write_status(out, "running", done, total, f"{ent['name']}/{eng}")
    meta = {"ts": ts, "tool": C.git_sha(), "kicad": "10.0.5", "cpus": os.cpu_count()}
    md = write_outputs(rows, out, meta, jsonl)
    write_status(out, "done", done, total, None)
    return rows, md


def main(argv=None):
    ap = argparse.ArgumentParser(description="routing benchmark (ROUTER-C0)")
    ap.add_argument("--manifest", default=os.path.expanduser("~/.config/fluxplace/routebench.toml"))
    ap.add_argument("--board", action="append", default=[])
    ap.add_argument("--engine", action="append", default=[])
    ap.add_argument("--out", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--baseline")
    args = ap.parse_args(argv)
    boards, _rules = C.load_manifest(args.manifest)
    boards = [b for b in boards if b.get("bench") and (not args.board or b["name"] in args.board)]
    engines = args.engine or ["krt", "freerouting"]
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    rows, md = run_all(boards, engines, args.work, args.out, C.git_sha(), ts)
    print(md, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
