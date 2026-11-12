"""Staged pipeline with small file handoffs.

    fluxplace pipeline BOARD --constraints C --spec S --workdir W [--from STAGE] [--to STAGE] [--status]

Stages, in order:  floorplan-check -> anchor-check -> place -> route-ready ->
route (fanout + time-boxed route + stall loop) -> tune -> silk -> review.

Each stage reads ONLY the previous stage's board file and ``W/state.json`` (a
handoff under 2 KB: stage, verdict, scorecard numbers, paths, what to fix
next), writes its own outputs under ``W/<n>-<stage>/`` (board.kicad_pcb,
stage.log, result.json) and never needs conversation history, so each stage
can run in its own agent context.  A stage whose command does not exist in
this build reports ``na`` ("not available") and passes the board through; the
pipeline definition never fails because of it.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import time

from . import routeconn as RC

STAGES = ["floorplan-check", "anchor-check", "place", "route-ready", "route", "tune", "silk", "review"]
# the constructive flow (docs/FLOW.md): modules -> floorplan -> fan-out (gate FANOUT_OK) -> plan-route (channels, WARN) -> route -> audit -> silk -> review
CONSTRUCTIVE = ["modules", "floorplan", "fanout", "plan-route", "route", "tune", "silk", "review"]
FLOWS = {"classic": STAGES, "constructive": CONSTRUCTIVE}
ALL_STAGES = list(dict.fromkeys(STAGES + CONSTRUCTIVE))
STATE_MAX = 2000
VERDICTS = ("pass", "fail", "blocked", "na", "skipped")


# ------------------------------------------------------------------ state
def state_path(work):
    return os.path.join(work, "state.json")


def load_state(work):
    try:
        with open(state_path(work)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _clip(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "~"


def save_state(work, st):
    """Write state.json, shrinking free text until it is under STATE_MAX bytes."""
    st["updated"] = int(time.time())
    for width in (110, 80, 60, 40):
        for k, v in st.get("stages", {}).items():
            v["line"] = _clip(v.get("line", ""), width)
        st["next"] = [_clip(x, width) for x in st.get("next", [])][:5]
        txt = json.dumps(st, separators=(",", ":"))
        if len(txt) <= STATE_MAX:
            break
    else:                                   # pathological paths: keep only what handoff needs
        for k, v in st.get("stages", {}).items():
            v.pop("board", None)
            v["line"] = _clip(v.get("line", ""), 24)
        st["next"] = st["next"][:2]
        txt = json.dumps(st, separators=(",", ":"))
    with open(state_path(work), "w") as fh:
        fh.write(txt)
    return txt


def format_status(st):
    """<= 15 lines."""
    if not st:
        return "pipeline: no state.json (run `fluxplace pipeline BOARD --workdir W ...`)"
    out = [f"pipeline {st.get('verdict', '?')} at {st.get('stage', '?')}  board {st.get('board', '?')}"]
    for i, name in enumerate(FLOWS.get(st.get("flow", "classic"), STAGES), 1):
        s = st.get("stages", {}).get(name)
        out.append(f"  {i} {name:16s} {s['verdict'] if s else '-':8s} {s['line'] if s else ''}".rstrip())
    sc = st.get("scorecard")
    if sc:
        out.append("  scorecard " + " ".join(f"{k}={v}" for k, v in sc.items()))
    nx = st.get("next") or []
    if nx:
        out.append("  next: " + _clip(nx[0], 100) + (f"  (+{len(nx) - 1} more in state.json)" if len(nx) > 1 else ""))
    return "\n".join(out[:15])


# ------------------------------------------------------------------ helpers
def _scorecard(board, cons, spec, waivers=()):
    from . import routediag as RD, preroute as PR
    B = RD.load(board)
    f, card = PR.run(B, cons, spec, waivers=list(waivers), plan=PR.use_plan(cons))
    codes = {}
    for x in f:
        if x["level"] == "FAIL":
            codes[x["code"]] = codes.get(x["code"], 0) + 1
    sc = {"score": card.get("score"), "crossings": card.get("crossings"),
          "ratsnest_mm": card.get("weighted_ratsnest_mm"),
          "overflow": (card.get("congestion") or {}).get("overflow"),
          "fail": card.get("fail"),
          "open": sum(RC.text_open(B).values())}
    sc = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in sc.items() if v is not None}
    return sc, f, codes


def _run_cli(argv, logpath):
    """Run a fluxplace command in-process; full output -> logpath. -> (rc, text)."""
    import cli
    buf = io.StringIO()
    rc = 0
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            cli.main(argv)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except Exception as e:                              # report, never crash the pipeline
            buf.write(f"\n{type(e).__name__}: {e}\n")
            rc = 99
    text = buf.getvalue()
    with open(logpath, "a") as fh:
        fh.write("$ fluxplace " + " ".join(map(str, argv)) + "\n" + text + "\n")
    return rc, text


def _sidecar(src, dst):
    s = os.path.splitext(src)[0] + ".kicad_pro"
    if os.path.exists(s):
        shutil.copy(s, os.path.splitext(dst)[0] + ".kicad_pro")


def _last_line(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[0] if lines else ""


def _subcommands():
    import cli
    ap = cli.build_parser()
    for a in ap._actions:
        if hasattr(a, "choices") and isinstance(a.choices, dict) and "analyze" in a.choices:
            return a.choices
    return {}


class Ctx:
    def __init__(self, a, work, board_in, state):
        self.a, self.work, self.board_in, self.state = a, work, board_in, state
        from . import constraints as C
        self.cons_path, self.spec_path = a.constraints, a.spec
        self.cons = C.load(a.constraints)
        self.spec = json.load(open(a.spec)) if a.spec else None

    def dir(self, n, name):
        d = os.path.join(self.work, f"{n}-{name}")
        os.makedirs(d, exist_ok=True)
        return d

    def cs(self):
        v = []
        if self.cons_path:
            v += ["--constraints", self.cons_path]
        if self.spec_path:
            v += ["--spec", self.spec_path]
        return v


# ------------------------------------------------------------------ stages
def st_floorplan_check(c, n, board):
    d = c.dir(n, "floorplan-check")
    rc, text = _run_cli(["floorplan-check", board] + c.cs(), os.path.join(d, "stage.log"))
    return {"verdict": "pass" if rc == 0 else "fail", "line": _last_line(text), "board": board,
            "next": [] if rc == 0 else ["floorplan-check failed: see " + os.path.join(d, "stage.log")]}


def st_anchor_check(c, n, board):
    d = c.dir(n, "anchor-check")
    out = os.path.join(d, "board.kicad_pcb")
    rc, text = _run_cli(["anchor-check", board] + c.cs() + ["--apply", "--out", out],
                        os.path.join(d, "stage.log"))
    if rc != 0 or not os.path.exists(out):
        return {"verdict": "fail", "line": _last_line(text) or f"exit {rc}", "board": board,
                "next": ["anchor-check --apply failed: " + os.path.join(d, "stage.log")]}
    _sidecar(board, out)
    line = next((l.strip() for l in text.splitlines() if "--apply" in l), _last_line(text))
    return {"verdict": "pass", "line": line, "board": out}


def st_place(c, n, board):
    d = c.dir(n, "place")
    log = os.path.join(d, "stage.log")
    mid, out = os.path.join(d, "clusters.kicad_pcb"), os.path.join(d, "board.kicad_pcb")
    rc, text = _run_cli(["clusters", board] + c.cs() + ["--apply", "--out", mid], log)
    if rc != 0 or not os.path.exists(mid):
        return {"verdict": "fail", "line": "clusters --apply: " + (_last_line(text) or f"exit {rc}"),
                "board": board, "next": ["clusters failed: " + log]}
    _sidecar(board, mid)
    rc, text = _run_cli(["place", "--board", mid, "--strategy", "routable", "--out", out] + c.cs(), log)
    if rc != 0 or not os.path.exists(out):
        return {"verdict": "fail", "line": "place routable: " + (_last_line(text) or f"exit {rc}"),
                "board": mid, "next": ["routable place refused/failed: " + log]}
    _sidecar(board, out)
    moved = next((l.strip() for l in text.splitlines() if l.startswith("routable:")), "placed")
    return {"verdict": "pass", "line": moved, "board": out}


def st_route_ready(c, n, board):
    d = c.dir(n, "route-ready")
    waive = []
    for w in c.a.waive or []:
        waive += ["--waive", w]
    rj = os.path.join(d, "route-ready.json")
    rc, text = _run_cli(["route-ready", board] + c.cs() + waive + ["--json", rj],
                        os.path.join(d, "stage.log"))
    sc, f, codes = _scorecard(board, c.cons, c.spec, c.a.waive or [])
    if rc == 0:
        return {"verdict": "pass", "line": f"gate passed, {sc.get('fail', 0)} FAIL", "board": board,
                "scorecard": sc}
    worst = [x for x in f if x["level"] == "FAIL"][:5]
    nxt = [f"{x['code']}: {x['msg']}" for x in worst]
    verdict = "pass" if c.a.force else "blocked"
    return {"verdict": verdict, "scorecard": sc, "board": board,
            "line": ("forced past " if c.a.force else "route-ready FAIL ") +
                    ", ".join(f"{k} {v}" for k, v in sorted(codes.items(), key=lambda kv: -kv[1])[:4]),
            "next": nxt + [f"full list: {rj}"]}


def _route_cmd(a, src, dst, only, power_nets=None, limited=False, max_ripup=3, R=None, rflags=()):
    """One py_router call. `R` (routerules.rules) and `rflags` (routerules.flags) carry the project's rules:
    fab-overrides floors, --same-net-pad-clearance (no vias in pads), grid step."""
    from . import adaptive
    from . import routerules as RR
    R = R or RR.rules(None, src, a.track, a.clearance)
    cmd = [a.router_py, os.path.join(a.router_dir, "py_router", "route.py"), src,
           "--output", dst, "--keep-input-copper", "--layers", *a.layers,
           *RR.size_args(R), *adaptive.krt_extra(a.router_dir), *rflags]
    if limited:
        cmd += ["--max-ripup", str(max_ripup)]       # batches: limited rip-up; the leftovers pass uses the default
    from . import batchroute as BR
    if BR.has_fanout(src):
        cmd += ["--no-stub-layer-swap"]              # the router may not re-layer the locked fan-out stubs
    return cmd + ["--nets", *only]


def st_route(c, n, board):
    """Batch routing with checkpoints (batchroute.py): fan-out first, then power, critical, rest, local nets
    first; one time-boxed router call per batch, a checkpoint board after each, a leftovers pass, then the
    stall loop. Time-boxed by --route-budget; the result is never less routed than the input."""
    from . import routediag as RD, routestatus as RS, stall as ST, netclass as NC, batchroute as BR
    from . import routerules as RR, routepar as RP
    a = c.a
    if not (os.path.exists(a.router_py) and os.path.exists(os.path.join(a.router_dir, "py_router", "route.py"))):
        return {"verdict": "na", "line": f"router not found ({a.router_dir}); pass --router-py/--router-dir",
                "board": board}
    d = c.dir(n, "route")
    best = os.path.join(d, "board.kicad_pcb")
    shutil.copy(board, best)
    _sidecar(board, best)
    t0 = time.time()
    budget = float(a.route_budget)
    spath = os.path.join(d, "routed.kicad_pcb.status.json")
    S = BR.settings(c.cons)
    R = RR.rules(c.cons, board, a.track, a.clearance)
    rflags = RR.flags(R, a.router_dir, d)
    conn = BR.default_conn(S)
    extra = {}
    build = lambda s, o, only, lim: _route_cmd(a, s, o, only, limited=lim, max_ripup=S["max_ripup"], R=R,
                                               rflags=rflags)
    run = BR.make_runner(build, a.router_dir, extra, spath, S)

    def left():
        return max(1.0, budget - (time.time() - t0))

    def nfail(path):
        return sum(conn(path).values())            # open connections (real connectivity), not "nets without copper"

    state = {"best": nfail(best), "timeouts": 0, "halvings": 0, "ckpts": 0}
    state["start"] = state["best"]

    def keep(path):
        """keep-best: only a board with fewer unrouted nets replaces the best-so-far."""
        if path and os.path.exists(path):
            k = nfail(path)
            if k < state["best"]:
                state["best"] = k
                shutil.copy(path, best)
                _sidecar(board, best)
        return state["best"]

    B = RD.load(board)
    todo = sorted(conn(board))
    cls = NC.classify(B, c.cons, c.spec)
    first, _rest = ST.escape_first(B, cls)
    first = [x for x in first if x in todo]
    if R["mode"] == "parallel":
        res = RP.route_parallel(board, d, todo, build, run, cons=c.cons, spec=c.spec, B=B, cls=cls, first=first,
                                rules=R, total_budget=budget, status_path=spath, extra=extra,
                                log=lambda m: None, conn=conn, cwd=a.router_dir)
    else:
        res = BR.route_batches(board, d, todo, run, cons=c.cons, spec=c.spec, B=B, cls=cls, first=first,
                               total_budget=budget, status_path=spath, extra=extra, log=lambda m: None,
                               rules=R, conn=conn)
    for k in ("timeouts", "halvings"):
        state[k] += res[k]
    state["ckpts"] += len(res["checkpoints"])
    keep(res["board"])
    left_nets = sorted(conn(best))
    timed = bool(res["timeouts"]) and bool(left_nets)
    stall = None
    if (left_nets or timed) and a.stall_rounds > 0 and left() > 5:
        k = [0]

        def again(b_in, b_out, only, timeout):
            k[0] += 1
            r = BR.route_batches(b_in, os.path.join(d, f"stall-batches-{k[0]}"), only, run, cons=c.cons,
                                 spec=c.spec, total_budget=min(timeout, left()), status_path=spath,
                                 extra=extra, log=lambda m: None, rules=R, conn=conn)
            shutil.copy(r["board"], b_out)
            _sidecar(b_in, b_out)
            state["timeouts"] += r["timeouts"]
            state["halvings"] += r["halvings"]
            state["ckpts"] += len(r["checkpoints"])
            keep(b_out)
            return bool(r["timeouts"]) and bool(r["leftovers"])
        stall = ST.stall_loop(best, d, again, c.cons, c.spec, rounds=a.stall_rounds,
                              replace=a.replace_on_stall, timeout=min(a.route_timeout, left()),
                              failed0=left_nets, timed_out0=timed, log=lambda m: None)
        keep(stall["board"])
    final_open = conn(best)
    final = sorted(final_open)
    diag = None
    if final:
        from . import routestatus as RS2
        diag, _ = RS2.diagnose_result({"out": best}, c.cons or None, c.spec)
    el = round(time.time() - t0)
    line = (f"{len(todo)} open net(s), open connections {state['start']} -> {sum(final_open.values())} "
            f"({len(final)} net(s)) in {el}s, "
            f"{state['ckpts']} checkpoint(s)"
            + (f", {state['halvings']} batch halving(s)" if state["halvings"] else "")
            + (f", {state['timeouts']} batch timeout(s)" if state["timeouts"] else "")
            + (f", {res['violations']} RULE_VIOLATION discarded" if res.get("violations") else ""))
    out = {"verdict": "pass" if not final else "fail", "line": line, "board": best}
    if final:
        out["next"] = [f"{sum(final_open.values())} open connection(s) in {len(final)} net(s); first: {', '.join(final[:6])}",
                       f"diagnosis (what to move): {diag}",
                       f"leftovers list: {os.path.join(d, 'leftovers.json')}"]
    return out


def st_tune(c, n, board):
    d = c.dir(n, "tune")
    out = os.path.join(d, "board.kicad_pcb")
    shutil.copy(board, out)
    _sidecar(board, out)
    rc, text = _run_cli(["tune", "--board", out, "--out", out] + (["--constraints", c.cons_path] if c.cons_path else []),
                        os.path.join(d, "stage.log"))
    over = [l for l in text.splitlines() if "still over" in l]
    if rc != 0:
        return {"verdict": "fail", "line": _last_line(text) or f"exit {rc}", "board": board,
                "next": ["tune failed: " + os.path.join(d, "stage.log")]}
    return {"verdict": "fail" if over else "pass", "board": out,
            "line": f"{len(over)} pair(s) still over skew" if over else "pairs within skew",
            "next": [_clip(l.strip(), 100) for l in over[:3]]}


def st_silk(c, n, board):
    """Silkscreen check + reference-text fit after routing (copper is never touched), before review/fab."""
    d = c.dir(n, "silk")
    out = os.path.join(d, "board.kicad_pcb")
    rc, text = _run_cli(["silk", board, "--fix", "--out", out] + (["--constraints", c.cons_path] if c.cons_path else []),
                        os.path.join(d, "stage.log"))
    if not os.path.exists(out):
        return {"verdict": "fail", "line": _last_line(text) or f"exit {rc}", "board": board,
                "next": ["silk failed: " + os.path.join(d, "stage.log")]}
    _sidecar(board, out)
    summ = next((l.strip() for l in text.splitlines() if l.strip().startswith("silk:")), _last_line(text))
    return {"verdict": "pass" if rc == 0 else "fail", "line": summ, "board": out,
            "next": [] if rc == 0 else ["FAIL-level silk findings left (footprint graphics / board texts): "
                                        + os.path.join(d, "stage.log")]}


def st_review(c, n, board):
    d = c.dir(n, "review")
    argv = ["review", "--board", board] + c.cs() + ["--json", os.path.join(d, "review.json")]
    if not c.a.api:
        argv += ["--no-api"]
    rc, text = _run_cli(["--no-sourcing"] + argv, os.path.join(d, "stage.log"))
    summ = next((l.strip() for l in text.splitlines() if l.strip().startswith("review:")), _last_line(text))
    res = {"verdict": "pass" if rc == 0 else "fail", "line": summ, "board": board}
    try:
        items = json.load(open(os.path.join(d, "review.json")))
        codes = {}
        for f in items:
            if f["level"] == "FAIL":
                codes[f["code"]] = codes.get(f["code"], 0) + 1
        res["next"] = [f"{k} x{v}" for k, v in sorted(codes.items(), key=lambda kv: -kv[1])[:5]]
    except (OSError, ValueError):
        pass
    return res


def st_modules(c, n, board):
    d = c.dir(n, "modules")
    rc, text = _run_cli(["modules", board] + c.cs(), os.path.join(d, "stage.log"))
    return {"verdict": "pass" if rc == 0 else "fail", "line": _last_line(text) if rc else next(
        (l for l in text.splitlines() if l.startswith("modules:")), _last_line(text))[:160], "board": board}


def st_floorplan(c, n, board):
    d = c.dir(n, "floorplan")
    out = os.path.join(d, "board.kicad_pcb")
    argv = ["floorplan", board, "--out", out] + c.cs() + (["--size"] if getattr(c.a, "size", False) else [])
    rc, text = _run_cli(argv, os.path.join(d, "stage.log"))
    line = next((l for l in reversed(text.splitlines()) if l.startswith("floorplan:")), _last_line(text))
    return {"verdict": "pass" if rc == 0 and os.path.exists(out) else "fail", "line": line, "board": out if os.path.exists(out) else board}


def st_fanout(c, n, board):
    d = c.dir(n, "fanout")
    out = os.path.join(d, "board.kicad_pcb")
    rc, text = _run_cli(["fanout", board, "--out", out] + c.cs(), os.path.join(d, "stage.log"))
    line = next((l for l in text.splitlines() if l.startswith("fanout:")), _last_line(text))
    res = {"verdict": "pass" if rc == 0 else "fail", "line": line, "board": out if os.path.exists(out) else board}
    if rc:
        res["next"] = [l for l in text.splitlines() if l.startswith("FANOUT_PIN_BLOCKED")][:5]
    return res


def st_plan_route(c, n, board):
    """channels only: sealed pins cannot exist (the fan-out gate passed), so overflow is a WARN and never blocks."""
    d = c.dir(n, "plan-route")
    rc, text = _run_cli(["plan-route", board] + c.cs(), os.path.join(d, "stage.log"))
    return {"verdict": "pass", "line": ("WARN " if rc else "") + _last_line(text), "board": board}


FUNCS = {"modules": st_modules, "floorplan": st_floorplan, "fanout": st_fanout, "plan-route": st_plan_route,
         "floorplan-check": st_floorplan_check, "anchor-check": st_anchor_check, "place": st_place,
         "route-ready": st_route_ready, "route": st_route, "tune": st_tune, "silk": st_silk, "review": st_review}


# ------------------------------------------------------------------ driver
def run(a):
    """Execute the stage range. -> exit code (0 ok, 1 stopped on fail/blocked)."""
    work = os.path.abspath(a.workdir)
    os.makedirs(work, exist_ok=True)
    st = load_state(work)
    if a.status:
        print(format_status(st))
        return 0
    flow = getattr(a, "flow", None) or (st or {}).get("flow") or "classic"
    STAGES_ = FLOWS[flow]
    first = a.from_stage or STAGES_[0]
    last = a.to_stage or STAGES_[-1]
    i0, i1 = STAGES_.index(first), STAGES_.index(last)
    if i0 > i1:
        raise SystemExit(f"pipeline: --from {first} is after --to {last}")
    if i0 == 0:
        if not a.board:
            raise SystemExit("pipeline: BOARD is required when starting at the first stage")
        st = {"schema": 1, "flow": flow, "board0": os.path.abspath(a.board), "stages": {}, "paths": {
            "workdir": work, "constraints": a.constraints, "spec": a.spec}}
        board = os.path.abspath(a.board)
    else:
        if not st:
            if not a.board:
                raise SystemExit(f"pipeline: --from {first} needs BOARD (or {state_path(work)} from the previous stage)")
            # no state yet: start the pipeline at this stage on BOARD (no hand-written state.json needed)
            st = {"schema": 1, "flow": flow, "board0": os.path.abspath(a.board), "board": os.path.abspath(a.board), "stages": {},
                  "paths": {"workdir": work, "constraints": a.constraints, "spec": a.spec}}
        board = st["board"]
        if a.board:
            board = os.path.abspath(a.board)
        for k in STAGES_[i0:]:
            st["stages"].pop(k, None)
    c = Ctx(a, work, board, st)
    rc = 0
    for i in range(i0, i1 + 1):
        name = STAGES_[i]
        t = time.time()
        res = FUNCS[name](c, i + 1, board)
        res.setdefault("verdict", "pass")
        board = res.get("board", board)
        st["stages"][name] = {"verdict": res["verdict"], "line": res.get("line", ""),
                              "dir": f"{i + 1}-{name}", "board": board}
        st["stage"], st["verdict"], st["board"] = name, res["verdict"], board
        st["next"] = res.get("next", [])
        if res.get("scorecard"):
            st["scorecard"] = res["scorecard"]
        elif res["verdict"] in ("pass",) and name in ("anchor-check", "place", "route", "tune"):
            try:
                st["scorecard"] = _scorecard(board, c.cons, c.spec, a.waive or [])[0]
            except Exception:
                pass
        save_state(work, st)
        print(f"[{i + 1}/{len(STAGES_)}] {name}: {res['verdict']} - {_clip(res.get('line', ''), 120)} "
              f"({time.time() - t:.0f}s)", flush=True)
        if res["verdict"] in ("fail", "blocked"):
            rc = 1
            break
    print(f"pipeline: {st['verdict']} at {st['stage']}; state {state_path(work)}")
    return rc
