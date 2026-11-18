"""`fluxplace corpus`: standing multi-board regression scorecard.

Runs the existing analysis commands (lint, review, route-ready, plan-route, schematic,
fanout, pairs, kicad-cli DRC, fab) over every board in a user manifest, one subprocess per
step, and records exit code, runtime, a crash flag (traceback) and metrics parsed from each
command's --json output. `--baseline` / `--baseline-ref` compare against a previous scorecard
or against the same manifest run with another git ref. Inputs are copied into a fresh work
dir per run; the originals are hashed before and after and never written.

The manifest is user data (`~/.config/fluxplace/corpus.toml` by default); nothing in this
module names a board, ref, net or path.
"""
import hashlib, json, os, shutil, subprocess, sys, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "cli.py")
DEFAULT_MANIFEST = "~/.config/fluxplace/corpus.toml"
DEFAULT_OUT = "~/.cache/fluxplace/corpus"
RULES = {"drc_noise": 3, "runtime_factor": 1.5, "runtime_floor_s": 2.0}
OK_EXIT = {0, 1, 3, 5}          # 1 = FAIL verdict, 3 = open questions, 5 = kicad-cli DRC violations
PASS_WORDS = {"PASS", "READY"}
VERDICT_KEYS = {"sch.netlist", "fab.gerber"}
LEVEL_ALIAS = {"WARNING": "WARN", "ERROR": "FAIL"}
_PCB_LOCK = threading.Lock()    # pcbnew is not thread-safe


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_sha(root=ROOT):
    try:
        return subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=20).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# ----------------------------------------------------------------------------- manifest
def load_manifest(path):
    import tomllib
    path = os.path.abspath(os.path.expanduser(path))
    with open(path, "rb") as f:
        m = tomllib.load(f)
    base = os.path.dirname(path)
    boards, names = [], set()
    for e in m.get("board", []):
        name = e.get("name")
        if not name or name in names:
            raise ValueError(f"manifest: board name missing or duplicate: {name!r}")
        names.add(name)
        tiers = list(e.get("tier", ["fast"]))
        if set(tiers) - {"fast", "full"}:
            raise ValueError(f"manifest: {name}: tier must be fast and/or full, got {tiers}")
        ent = {"name": name, "tier": tiers, "notes": e.get("notes", ""),
               "pipeline_args": list(e.get("pipeline_args", [])), "timeout_s": int(e.get("timeout_s", 900))}
        for key in ("board", "spec", "constraints"):
            if key not in e:
                if key == "board":
                    raise ValueError(f"manifest: {name}: board is required")
                continue
            p = os.path.expanduser(e[key])
            p = p if os.path.isabs(p) else os.path.join(base, p)
            if not os.path.isfile(p):
                raise FileNotFoundError(f"manifest: {name}: {key} not found: {p}")
            ent[key] = os.path.abspath(p)
        boards.append(ent)
    return boards, {**RULES, **m.get("rules", {})}


# ----------------------------------------------------------------------------- steps
def _run(argv, cwd, log, timeout):
    """One fluxplace/kicad-cli step as a subprocess, niced, output captured to `log`."""
    t0 = time.time()
    nice = ["nice", "-n", "10"] if shutil.which("nice") else []
    try:
        p = subprocess.run(nice + argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        code, text = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        code, text = None, f"TIMEOUT after {timeout}s\n"
    from . import quietlog
    text = quietlog.filter_text(text)
    with open(log, "w") as f:
        f.write(text)
    return {"exit": code, "runtime_s": round(time.time() - t0, 2),
            "crashed": "Traceback (most recent call last)" in text, "log": log}


def _fluxplace(*args):
    return [sys.executable, CLI, "--no-sourcing", *args]


def _j(path):
    with open(path) as f:
        return json.load(f)


def _count(prefix, rows, key="level", code="code"):
    """Counts by level and by level.code, e.g. review.FAIL and review.FAIL.SILK_TOO_SMALL."""
    m = {}
    for r in rows:
        lv = LEVEL_ALIAS.get(str(r.get(key)).upper(), str(r.get(key)).upper())
        for k in (f"{prefix}.{lv}", f"{prefix}.{lv}.{r.get(code)}"):
            m[k] = m.get(k, 0) + 1
    return m


def _p_lint(r, out):
    rows = _j(out)
    m = _count("lint", rows, key="severity")
    m["lint.total"] = len(rows)
    return m


def _p_review(r, out):
    rows = _j(out)
    m = _count("review", rows)
    m["review.verdict"] = "FAIL" if any(x["level"] == "FAIL" for x in rows) else (
        "WARN" if any(x["level"] == "WARN" for x in rows) else "PASS")
    return m


def _p_route_ready(r, out):
    d = _j(out)
    m = _count("rr", d["findings"])
    m["rr.verdict"] = "NOT READY" if any(x["level"] == "FAIL" for x in d["findings"]) else "READY"
    sc = d.get("scorecard", {})
    m["rr.crossings"] = sc.get("crossings")
    m["rr.weighted_ratsnest_mm"] = sc.get("weighted_ratsnest_mm")
    return m


def _p_collisions(r, out):
    d = _j(out)
    rows = d.get("findings", []) if isinstance(d, dict) else d
    m = {"col.COLLISION": len(rows)}
    for x in rows:
        k = f"col.kind.{x.get('kind') or x.get('cls') or 'other'}"
        m[k] = m.get(k, 0) + 1
    return m


def _p_plan_route(r, out):
    d = _j(out)
    v = d["verdict"]
    c = v.get("counts", {})
    m = {"plan.verdict": v.get("result"), "plan.score": v.get("score"), "plan.confidence": v.get("confidence"),
         "plan.predicted_routable": v.get("predicted_routable"), "plan.sealed": c.get("PLAN_PIN_SEALED", 0),
         "plan.overflow": c.get("PLAN_OVERFLOW", 0), "plan.overflow_mm": v.get("overflow_total")}
    for code, n in c.items():
        m[f"plan.code.{code}"] = n
    return m


def _p_schematic(r, rep):
    d = _j(rep)
    g = d.get("gate", {})
    nl = g.get("netlist", {})
    return {"sch.verdict": d.get("verdict"), "sch.netlist": nl.get("verdict"),
            "sch.nets": nl.get("nets_sch"), "sch.erc_total": (g.get("erc") or {}).get("total")}


def _p_fanout(r, rep):
    if r["exit"] == 3 and not os.path.exists(rep):
        return {"fanout.verdict": "QUESTIONS"}
    d = _j(rep)
    req, ok = d.get("required") or 0, d.get("escaped") or 0
    return {"fanout.verdict": d.get("verdict"), "fanout.required": req, "fanout.escaped": ok,
            "fanout.escaped_frac": round(ok / req, 3) if req else None}


def _p_pairs(r, rep):
    d = _j(rep)
    m = {"pairs.complete": d.get("complete"), "pairs.total": d.get("total")}
    q = rep.replace(".pairs.plan.json", ".questions.json")
    if os.path.exists(q):
        m["pairs.questions"] = len(_j(q).get("questions", []))
    return m


def _p_drc(r, rep):
    d = _j(rep)
    viol = d.get("violations", [])
    errs = [v for v in viol if v.get("severity") == "error"]
    m = {"drc.errors": len(errs), "drc.warnings": sum(1 for v in viol if v.get("severity") == "warning"),
         "drc.unconnected": len(d.get("unconnected_items", [])) +
                            sum(1 for v in viol if v.get("type") == "unconnected_items"),
         "drc.parity": len(d.get("schematic_parity", []))}
    for v in errs:
        k = f"drc.error.{v.get('type')}"
        m[k] = m.get(k, 0) + 1
    return m


def _pcb_metrics(path):
    import pcbnew
    with _PCB_LOCK:
        b = pcbnew.LoadBoard(path)
        vias = tracks = 0
        length = 0.0
        for t in b.GetTracks():
            if isinstance(t, pcbnew.PCB_VIA):
                vias += 1
            else:
                tracks += 1
                length += t.GetLength() / 1e6
        return {"board.footprints": len(b.GetFootprints()), "board.nets": b.GetNetCount(),
                "board.tracks": tracks, "board.vias": vias, "board.track_mm": round(length, 1)}


def _kicad_cli():
    return shutil.which("kicad-cli") or "/usr/bin/kicad-cli"


# ----------------------------------------------------------------------------- per board
def _stage(ent, work):
    """Copy the board (+ sibling .kicad_pro, spec, constraints) into work/<name>/; hash the originals."""
    d = os.path.join(work, ent["name"])
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    src = {"board": ent["board"]}
    pro = os.path.splitext(ent["board"])[0] + ".kicad_pro"
    if os.path.exists(pro):
        src["pro"] = pro
    for k in ("spec", "constraints"):
        if k in ent:
            src[k] = ent[k]
    before = {k: sha256(p) for k, p in src.items()}
    paths = {}
    for k, p in src.items():
        paths[k] = os.path.join(d, os.path.basename(p))
        shutil.copy2(p, paths[k])
    return d, paths, src, before


def run_board(ent, work, tier, rules):
    name = ent["name"]
    d, paths, src, before = _stage(ent, work)
    logs, out = os.path.join(d, "logs"), os.path.join(d, "out")
    os.makedirs(logs)
    os.makedirs(out)
    board = paths["board"]
    stem = os.path.splitext(os.path.basename(board))[0]
    cin = (["--spec", paths["spec"]] if "spec" in paths else []) + \
          (["--constraints", paths["constraints"]] if "constraints" in paths else [])
    res = {"name": name, "tier": tier, "steps": {}, "metrics": {}, "notes": ent.get("notes", "")}
    to = ent["timeout_s"]

    def step(sname, argv, parse=None, ok=OK_EXIT):
        r = _run(argv, d, os.path.join(logs, sname + ".log"), to)
        rec = {"exit": r["exit"], "runtime_s": r["runtime_s"], "crashed": r["crashed"]}
        if r["crashed"]:
            res["metrics"][f"crash.{sname}"] = 1
        elif r["exit"] not in ok:
            res["metrics"][f"error.{sname}"] = 1
        elif parse:
            try:
                res["metrics"].update(parse(r))
            except Exception as e:  # a missing/garbled --json is a parse error, not a crash
                rec["parse_error"] = f"{type(e).__name__}: {e}"[:200]
                res["metrics"][f"error.parse.{sname}"] = 1
        res["steps"][sname] = rec
        return r

    rep = lambda suffix: os.path.join(d, stem + suffix)     # step-output path next to the work board
    step("lint", _fluxplace("lint", "--board", board, *cin, "--json", os.path.join(out, "lint.json")),
         lambda r: _p_lint(r, os.path.join(out, "lint.json")))
    step("review", _fluxplace("review", "--board", board, *cin, "--no-api",
                              "--json", os.path.join(out, "review.json")),
         lambda r: _p_review(r, os.path.join(out, "review.json")))
    step("route-ready", _fluxplace("route-ready", board, *cin, "--json", os.path.join(out, "route-ready.json")),
         lambda r: _p_route_ready(r, os.path.join(out, "route-ready.json")))
    step("collisions", _fluxplace("route-ready", board, "--collisions-only",
                                  *(["--constraints", paths["constraints"]] if "constraints" in paths else []),
                                  "--json", os.path.join(out, "collisions.json")),
         lambda r: _p_collisions(r, os.path.join(out, "collisions.json")))
    step("plan-route", _fluxplace("plan-route", board, *cin, "--json", os.path.join(out, "plan-route.json")),
         lambda r: _p_plan_route(r, os.path.join(out, "plan-route.json")))
    if "spec" in paths:
        sch_dir = os.path.join(d, "sch")
        os.makedirs(sch_dir)
        sch_out = os.path.join(sch_dir, stem + ".kicad_sch")
        step("schematic", _fluxplace("schematic", "--spec", paths["spec"], "--out", sch_out, "--no-render",
                                     *(["--constraints", paths["constraints"]] if "constraints" in paths else [])),
             lambda r: _p_schematic(r, os.path.join(sch_dir, stem + ".schematic.json")))
    step("fanout", _fluxplace("fanout", board, *cin, "--check-only"),
         lambda r: _p_fanout(r, rep(".fanout.json")))
    step("pairs", _fluxplace("pairs", board, *cin, "--plan-only"),
         lambda r: _p_pairs(r, rep(".pairs.plan.json")))
    drc_json = os.path.join(out, "drc.json")
    step("drc", [_kicad_cli(), "pcb", "drc", "--format", "json", "--output", drc_json, board],
         lambda r: _p_drc(r, drc_json))
    routed = res["metrics"].get("drc.unconnected") == 0 and "drc.unconnected" in res["metrics"]
    try:
        pm = _pcb_metrics(board)
        res["metrics"].update(pm)
        res["steps"]["pcb-metrics"] = {"exit": 0, "runtime_s": 0.0, "crashed": False}
    except Exception as e:  # pcbnew failure: record it as a crash of this step
        import traceback
        with open(os.path.join(logs, "pcb-metrics.log"), "w") as f:
            f.write(traceback.format_exc())
        res["steps"]["pcb-metrics"] = {"exit": 1, "runtime_s": 0.0, "crashed": True}
        res["metrics"]["crash.pcb-metrics"] = 1
    if routed:
        fab_dir = os.path.join(d, "fab")
        step("fab", _fluxplace("fab", "--board", board, "--out", fab_dir, "--no-api", "--no-review", *cin),
             ok={0, 1})
        if res["steps"]["fab"]["exit"] in (0, 1) and os.path.isdir(fab_dir):
            step("gerber-check", _fluxplace("fab", "--check-only", fab_dir, *cin),
                 lambda r: {"fab.gerber": "PASS" if r["exit"] == 0 else ("QUESTIONS" if r["exit"] == 3 else "FAIL")},
                 ok={0, 1, 3})
    if tier == "full":
        wd = os.path.join(d, "pipeline")
        os.makedirs(wd)
        step("pipeline", _fluxplace("pipeline", board, "--workdir", wd, *cin, *ent["pipeline_args"]),
             ok={0, 1, 2, 3})
        outs = sorted((os.path.join(dp, f) for dp, _, fs in os.walk(wd) for f in fs
                       if f.endswith(".kicad_pcb")), key=os.path.getmtime)
        if outs:   # newest board the pipeline wrote: DRC + copper metrics under the routed. prefix
            drc_r = os.path.join(out, "drc-routed.json")
            step("drc-routed", [_kicad_cli(), "pcb", "drc", "--format", "json", "--output", drc_r, outs[-1]],
                 lambda r: {f"routed.{k}": v for k, v in _p_drc(r, drc_r).items()})
            res["metrics"].update({f"routed.{k}": v for k, v in _pcb_metrics(outs[-1]).items()})

    # inputs must be untouched: a changed original is an error, never a silent pass
    after = {k: sha256(p) for k, p in src.items()}
    res["inputs_unchanged"] = before == after
    if not res["inputs_unchanged"]:
        res["metrics"]["error.inputs_modified"] = 1
    res["runtime_s"] = round(sum(s["runtime_s"] for s in res["steps"].values()), 2)
    res["runtime"] = {k: v["runtime_s"] for k, v in res["steps"].items()}
    return res


def run_all(boards, tier, work, out, jobs, rules):
    os.makedirs(work, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    workers = max(1, min(jobs, 4, len(boards) or 1))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda e: run_board(e, work, tier, rules), boards))
    return {"schema": 1, "tool": {"git_sha": git_sha(), "tier": tier, "python": sys.version.split()[0]},
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "boards": {r["name"]: r for r in results}}


# ----------------------------------------------------------------------------- compare
CODE_PREFIXES = ("review.", "rr.", "lint.", "plan.code.", "col.kind.", "drc.error.")


def _is_new_check_key(k):
    """A per-code count (review.WARN.CODE, drc.error.TYPE, ...): new when its code is absent in the baseline."""
    return k.startswith(CODE_PREFIXES) and k.count(".") >= 2


def _ran_ok(board, step):
    """A step counts for runtime only when it ran to a normal exit with no error/crash metric."""
    st = board["steps"].get(step, {})
    return st.get("exit") in OK_EXIT and not st.get("crashed") and not board["metrics"].get(f"error.{step}")


def compare(cur, base, rules):
    """Per-metric deltas plus regressions (judged against rules) and new checks (codes absent in base)."""
    rows, regs, new, notes = [], [], [], []
    for name in sorted(set(cur["boards"]) | set(base["boards"])):
        c, b = cur["boards"].get(name), base["boards"].get(name)
        if c is None:
            regs.append(f"{name}: board missing from current run")
            continue
        if b is None:
            notes.append(f"{name}: board not in baseline")
            continue
        cm, bm = c["metrics"], b["metrics"]
        for k in sorted(set(cm) | set(bm)):
            cv, bv = cm.get(k), bm.get(k)
            if cv == bv:
                continue
            rows.append({"board": name, "metric": k, "base": bv, "cur": cv})
            if k.startswith("crash."):
                regs.append(f"{name}: {k} (traceback)")
            elif k.startswith("error.") and (cv or 0) > (bv or 0):
                regs.append(f"{name}: {k} {bv or 0} -> {cv}")
            elif (k.endswith(".verdict") or k in VERDICT_KEYS) and bv in PASS_WORDS and cv not in PASS_WORDS:
                regs.append(f"{name}: {k} {bv} -> {cv}")
            elif k == "drc.unconnected" and (cv or 0) > (bv or 0):
                regs.append(f"{name}: open connections {bv or 0} -> {cv}")
            elif k == "drc.errors" and (cv or 0) - (bv or 0) > rules["drc_noise"]:
                regs.append(f"{name}: DRC errors {bv or 0} -> {cv} (noise margin {rules['drc_noise']})")
            elif k.startswith(("review.FAIL.", "rr.FAIL.")):
                if bv is None:
                    new.append(f"{name}: {k}={cv}")
                elif (cv or 0) > bv:
                    regs.append(f"{name}: {k} {bv} -> {cv}")
            elif bv is None and cv and _is_new_check_key(k):
                new.append(f"{name}: {k}={cv}")
        for s in sorted(set(c["runtime"]) & set(b["runtime"])):
            if not (_ran_ok(c, s) and _ran_ok(b, s)):
                if _ran_ok(c, s) != _ran_ok(b, s):   # e.g. a baseline step that errored at once: not comparable
                    notes.append(f"{name}: {s} not comparable (baseline ran ok={_ran_ok(b, s)}, "
                                 f"current ok={_ran_ok(c, s)})")
                continue
            ct, bt = c["runtime"][s], b["runtime"][s]
            if ct > bt * rules["runtime_factor"] and ct - bt > rules["runtime_floor_s"]:
                regs.append(f"{name}: runtime {s} {bt}s -> {ct}s")
    return {"verdict": "REGRESSION" if regs else "OK", "regressions": regs, "new_checks": new,
            "notes": notes, "deltas": rows}


def repeat_diff(runs):
    """Metrics that differ between repeated runs of the same boards (determinism probe)."""
    diffs = {}
    for name in runs[0]["boards"]:
        keys = set()
        for r in runs:
            keys |= set(r["boards"].get(name, {}).get("metrics", {}))
        for k in sorted(keys):
            vals = [r["boards"].get(name, {}).get("metrics", {}).get(k) for r in runs]
            if len(set(map(repr, vals))) > 1:
                diffs.setdefault(name, {})[k] = vals
    return diffs


# ----------------------------------------------------------------------------- reports
def scorecard_md(sc):
    names = list(sc["boards"])
    lines = [f"# corpus scorecard ({sc['tool']['tier']}, fluxplace {sc['tool']['git_sha']}, {sc['generated']})", "",
             "| board | runtime s | crashes | errors | inputs unchanged |", "|---|---|---|---|---|"]
    for n in names:
        b = sc["boards"][n]
        crashes = sum(v for k, v in b["metrics"].items() if k.startswith("crash."))
        errs = sum(v for k, v in b["metrics"].items() if k.startswith("error."))
        lines.append(f"| {n} | {b['runtime_s']} | {crashes} | {errs} | {b['inputs_unchanged']} |")
    keys = sorted({k for n in names for k in sc["boards"][n]["metrics"]})
    lines += ["", "| metric | " + " | ".join(names) + " |", "|---" * (len(names) + 1) + "|"]
    for k in keys:
        lines.append(f"| {k} | " + " | ".join(str(sc["boards"][n]["metrics"].get(k, "")) for n in names) + " |")
    return "\n".join(lines) + "\n"


def compare_md(cmp):
    lines = [f"# corpus compare: {cmp['verdict']}", ""]
    lines += [f"- REGRESSION {x}" for x in cmp["regressions"]] or ["- no regressions"]
    lines += ["", "## new checks (not in baseline, not regressions)"]
    lines += [f"- {x}" for x in cmp["new_checks"]] or ["- none"]
    lines += ["", "| board | metric | baseline | current | delta |", "|---|---|---|---|---|"]
    for r in cmp["deltas"]:
        d = (r["cur"] - r["base"]) if isinstance(r["cur"], (int, float)) and isinstance(r["base"], (int, float)) \
            else ""
        lines.append(f"| {r['board']} | {r['metric']} | {r['base']} | {r['cur']} | {d} |")
    return "\n".join(lines) + "\n"


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


def _load_sc(path):
    return _j(os.path.expanduser(path))


# ----------------------------------------------------------------------------- driver
def _select(boards, tier, names):
    return [b for b in boards if tier in b["tier"] and (not names or b["name"] in names)]


_SHIM = """import argparse, sys
sys.path.insert(0, sys.argv[1])
from fluxplace import corpus as C
p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="cmd"); C.add_parsers(sub)
a = p.parse_args(["corpus"] + sys.argv[2:]); sys.exit(a.fn(a))
"""


def _baseline_ref(a, ref, out, work):
    """Run the same manifest against `ref`'s fluxplace code in a temporary worktree and return its scorecard.
    The ref may predate this runner, so the current corpus.py is overlaid into the worktree; the steps
    (cli.py and the analysis modules) are the ref's own."""
    tree = os.path.join(out, "baseline-tree")
    shutil.rmtree(tree, ignore_errors=True)
    subprocess.run(["git", "-C", ROOT, "worktree", "add", "--detach", tree, ref], check=True,
                   capture_output=True, text=True)
    try:
        shutil.copy2(os.path.join(ROOT, "fluxplace", "corpus.py"), os.path.join(tree, "fluxplace", "corpus.py"))
        cmd = [sys.executable, "-c", _SHIM, tree, "--manifest", a.manifest, "--tier", a.tier,
               "--work", os.path.join(work, "baseline-ref"), "--out", os.path.join(out, "baseline-ref"),
               "--jobs", str(a.jobs)]
        for n in a.board or []:
            cmd += ["--board", n]
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=tree)
        if p.returncode not in (0, 1):
            raise RuntimeError(f"baseline run at {ref} failed ({p.returncode}):\n{p.stdout[-800:]}{p.stderr[-800:]}")
        return _load_sc(os.path.join(out, "baseline-ref", a.tier, "scorecard.json"))
    finally:
        subprocess.run(["git", "-C", ROOT, "worktree", "remove", "--force", tree], capture_output=True)
        subprocess.run(["git", "-C", ROOT, "worktree", "prune"], capture_output=True)


def main(a):
    boards, rules = load_manifest(a.manifest)
    sel = _select(boards, a.tier, a.board)
    if not sel:
        print(f"corpus: no boards in {a.manifest} for tier {a.tier}" + (f" matching {a.board}" if a.board else ""))
        return 2
    out = os.path.expanduser(a.out)
    work = os.path.expanduser(a.work)
    if a.repeat > 1:
        runs = []
        for i in range(a.repeat):
            sc = run_all(sel, a.tier, os.path.join(work, f"repeat{i}"), os.path.join(out, f"repeat{i}"),
                         a.jobs, rules)
            runs.append(sc)
        diffs = repeat_diff(runs)
        _write(os.path.join(out, "repeat.json"), json.dumps({"runs": a.repeat, "diffs": diffs}, indent=1))
        n = sum(len(v) for v in diffs.values())
        print(f"corpus repeat: {a.repeat} runs x {len(sel)} board(s): "
              f"{'DETERMINISTIC' if not n else f'{n} metric(s) differ between runs'} -> {out}/repeat.json")
        return 1 if n else 0

    tdir = os.path.join(out, a.tier)
    os.makedirs(tdir, exist_ok=True)
    sc = run_all(sel, a.tier, os.path.join(work, a.tier), tdir, a.jobs, rules)
    sc["tool"]["tier"] = a.tier
    sc["manifest"] = os.path.abspath(os.path.expanduser(a.manifest))
    _write(os.path.join(tdir, "scorecard.json"), json.dumps(sc, indent=1, default=str))
    _write(os.path.join(tdir, "scorecard.md"), scorecard_md(sc))
    crashes = sum(1 for b in sc["boards"].values() for k in b["metrics"] if k.startswith("crash."))
    print(f"corpus {a.tier}: {len(sel)} board(s), {crashes} crash(es) -> {tdir}/scorecard.md")

    base = None
    if a.baseline_ref:
        base = _baseline_ref(a, a.baseline_ref, out, work)
    elif a.baseline:
        base = _load_sc(a.baseline)
    rc = 0
    if base is not None:
        cmp = compare(sc, base, rules)
        _write(os.path.join(tdir, "compare.json"), json.dumps(cmp, indent=1, default=str))
        _write(os.path.join(tdir, "compare.md"), compare_md(cmp))
        print(f"corpus compare: {cmp['verdict']} - {len(cmp['regressions'])} regression(s), "
              f"{len(cmp['new_checks'])} new check(s) -> {tdir}/compare.md")
        rc = 1 if cmp["regressions"] else 0
    if a.update_baseline:
        if not a.baseline:
            print("corpus: --update-baseline needs --baseline FILE to write to")
            return 2
        _write(os.path.expanduser(a.baseline), json.dumps(sc, indent=1, default=str))
        print(f"corpus: baseline written -> {a.baseline}")
    return rc


def add_parsers(sub):
    p = sub.add_parser("corpus", help="regression scorecard; "
                                      "vs baseline or git ref")
    p.add_argument("--manifest", default=DEFAULT_MANIFEST, help="corpus TOML (default ~/.config/fluxplace/corpus.toml)")
    p.add_argument("--tier", choices=["fast", "full"], default="fast")
    p.add_argument("--board", action="append", default=None, help="only this manifest board name (repeatable)")
    p.add_argument("--work", default=DEFAULT_OUT + "/work", help="work dir: input copies live here")
    p.add_argument("--out", default=DEFAULT_OUT, help="scorecard, compare and log output dir")
    p.add_argument("--baseline", default=None, help="scorecard JSON to compare against (or to write with --update-baseline)")
    p.add_argument("--baseline-ref", default=None, help="git ref: run the same manifest with that code and compare")
    p.add_argument("--update-baseline", action="store_true", help="write the current scorecard to --baseline")
    p.add_argument("--repeat", type=int, default=1, help="run the fast tier N times; report metrics that differ")
    p.add_argument("--jobs", type=int, default=4, help="boards in parallel (max 4)")
    p.set_defaults(fn=lambda a: sys.exit(main(a)))
    return p
