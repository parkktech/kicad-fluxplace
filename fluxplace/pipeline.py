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

STAGES = ["ready", "floorplan-check", "anchor-check", "place", "route-ready", "route", "tune", "silk", "review"]
# the constructive flow (docs/FLOW.md): modules -> floorplan -> fan-out (gate FANOUT_OK) -> plan-route (channels, WARN) -> route -> audit -> silk -> review
CONSTRUCTIVE = ["ready", "modules", "floorplan", "fanout", "pairs", "plan-route", "route", "tune", "silk", "review"]
FLOWS = {"classic": STAGES, "constructive": CONSTRUCTIVE}
ALL_STAGES = list(dict.fromkeys(STAGES + CONSTRUCTIVE))
STATE_MAX = 2400   # router + engine records joined the stage entries
VERDICTS = ("pass", "warn", "fail", "blocked", "na", "skipped")   # warn: ran, its own gate line says FAIL, does not block


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
            (v.get("fr") or {}).pop("curve", None)
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
    if st.get("not_ready"):
        out.append("  NOT READY: " + _clip(st["not_ready"], 120))
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


def _questions_result(rc, text, board):
    """exit code 3 from a stage = open questions: the pipeline stops, the agent relays them to the user."""
    if rc != 3:
        return None
    lines = [l for l in text.splitlines() if l.strip()]
    return {"verdict": "questions", "line": next((l for l in lines if "question" in l), lines[0] if lines else "questions open")[:160],
            "board": board, "next": lines[:40]}


def _copy(src, dst):
    """shutil.copy that tolerates src == dst (a stage re-run on its own output)."""
    if os.path.exists(dst) and os.path.samefile(src, dst):
        return
    shutil.copy(src, dst)


def _sidecar(src, dst):
    if os.path.exists(dst) and os.path.exists(src) and os.path.samefile(src, dst):
        return
    s = os.path.splitext(src)[0] + ".kicad_pro"
    if os.path.exists(s):
        shutil.copy(s, os.path.splitext(dst)[0] + ".kicad_pro")
    m = os.path.splitext(src)[0] + ".fluxmap.json"        # the map (answers, pours, fan-out record) travels with the board
    if os.path.exists(m) and not os.path.exists(os.path.splitext(dst)[0] + ".fluxmap.json"):
        shutil.copy(m, os.path.splitext(dst)[0] + ".fluxmap.json")


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
        from . import questions as QS
        self.answers = dict((state or {}).get("answers") or {})
        self.answers.update(QS.parse_answers(getattr(a, "answer", None)))
        self.assume = bool(getattr(a, "assume_defaults", False))
        if state is not None:
            state["answers"] = self.answers
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

    def qs(self):
        """question flags for the stages that can raise questions (fanout, modules, floorplan)."""
        v = []
        for k, x in sorted(self.answers.items()):
            v += ["--answer", f"{k}={x}"]
        return v + (["--assume-defaults"] if self.assume else [])


# ------------------------------------------------------------------ stages
def st_ready(c, n, board):
    """readiness gate (docs/commands/ready.md): parts, stackup-computed impedance geometry, rails, anchors, outline, floors."""
    if not c.spec_path:
        return {"verdict": "na", "line": "no --spec: readiness (parts, impedance geometry) not checked", "board": board}
    d = c.dir(n, "ready")
    argv = ["ready", c.spec_path, "--board", board] + (["--constraints", c.cons_path] if c.cons_path else []) + c.qs()
    argv += [] if getattr(c.a, "api", False) else ["--no-api"]
    argv += ["--allow-incomplete"] if getattr(c.a, "allow_incomplete", False) else []
    rc, text = _run_cli(argv, os.path.join(d, "stage.log"))
    q = _questions_result(rc, text, board)
    if q:
        return q
    line = next((l for l in text.splitlines() if l.startswith(("ready:", "NOT READY"))), _last_line(text))
    if rc:
        return {"verdict": "blocked", "line": line, "board": board,
                "next": [l.strip()[2:] for l in text.splitlines() if l.startswith("  - ")][:5] + [os.path.join(d, "stage.log")]}
    return {"verdict": "pass", "line": next((l for l in text.splitlines() if l.startswith("NOT READY")), line), "board": board}


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
    rc, text = _run_cli(["place", "--board", mid, "--strategy", "routable", "--out", out] + _engine(c) + c.cs(), log)
    if rc != 0 or not os.path.exists(out):
        return {"verdict": "fail", "line": "place routable: " + (_last_line(text) or f"exit {rc}"),
                "board": mid, "next": ["routable place refused/failed: " + log]}
    _sidecar(board, out)
    moved = next((l.strip() for l in text.splitlines() if l.startswith(("routable:", "cypress:"))), "placed")
    return dict({"verdict": "pass", "line": moved, "board": out, "engine": _engine_record(out)}, **_wl_record(out))


def _wl_record(board):
    """{"wl": before>after numbers} when the wire-length pass ran on the stage board (wlrefine.py; full record beside it)."""
    from . import wlrefine as WL
    p = os.path.splitext(board)[0] + ".wl-refine.json"
    try:
        return {"wl": WL.short(json.load(open(p)))} if os.path.exists(p) else {}
    except Exception:
        return {}


def _engine(c):
    """Placement engine hook (docs/CYPRESS.md): --engine, else `[placement] engine`; [] = the built-in placer."""
    from . import placer_cypress as PC
    e = PC.engine_of(c.cons, getattr(c.a, "engine", None))
    return ["--engine", e] if e != "builtin" else []


def _engine_record(board):
    from . import placer_cypress as PC
    r = PC.read_record(board)
    return f"{r.get('engine', 'builtin')} {r.get('version', '')}".strip()


def _engine_floorplan(c, d, board, out):
    """Constructive flow with an external engine: it places the board instead of `floorplan`. -> result or None
    (engine not selected, not installed, or it refused: the floorplan stage runs as usual)."""
    if not _engine(c):
        return None
    rc, text = _run_cli(["place", "--board", board, "--strategy", "routable", "--out", out] + _engine(c) + c.cs(),
                        os.path.join(d, "stage.log"))
    line = next((l.strip() for l in text.splitlines() if l.startswith("cypress:")), None)
    if rc != 0 or not line or not os.path.exists(out):
        return None
    _sidecar(board, out)
    return {"verdict": "pass", "line": line, "board": out, "engine": _engine_record(out)}


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


def _route_cmd(a, src, dst, only, power_nets=None, limited=False, max_ripup=3, R=None, rflags=(), pair=False, gap=None,
               dflags=()):
    """One py_router call. `R` (routerules.rules) and `rflags` (routerules.flags) carry the project's rules:
    fab-overrides floors, --same-net-pad-clearance (no vias in pads), grid step.  `pair`: the two nets are the legs of a
    differential pair and go to route_diff.py (coupled P and N); `dflags` = the rule flags that script accepts."""
    from . import adaptive
    from . import routerules as RR
    R = R or RR.rules(None, src, a.track, a.clearance)
    script = "route_diff.py" if pair else "route.py"
    cmd = [a.router_py, os.path.join(a.router_dir, "py_router", script), src,
           "--output", dst, "--keep-input-copper", "--layers", *a.layers,
           *RR.size_args(R), *adaptive.krt_extra(a.router_dir, script), *(dflags if pair else rflags)]
    if pair and gap:
        cmd += ["--diff-pair-gap", f"{gap:g}"]
    if limited:
        cmd += ["--max-ripup", str(max_ripup)]       # batches: limited rip-up; the leftovers pass uses the default
    from . import batchroute as BR
    if BR.has_fanout(src):
        cmd += ["--no-stub-layer-swap"]              # the router may not re-layer the locked fan-out stubs
    return cmd + ["--nets", *only]


def diff_named(p, n):
    """route_diff.py finds pairs by suffix only: _P/_N, -P/-N, P/N, +/-."""
    for a, b in (("_P", "_N"), ("-P", "-N"), ("P", "N"), ("+", "-")):
        for x, y in ((p, n), (n, p)):
            if x.endswith(a) and y == x[:-len(a)] + b:
                return True
    return False


def pair_gap(cons, p):
    """Gap P-N the constraints imply for pair `p` ([pairs.FAMILY] impedance_diff via PAIR_GEOM_BY_Z); None = the router's own default."""
    from . import constraints as C
    best = None
    for fam, v in ((cons or {}).get("pairs") or {}).items():
        if p.startswith(fam) and "impedance_diff" in v and (best is None or len(fam) > best[0]):
            best = (len(fam), C.PAIR_GEOM_BY_Z.get(int(v["impedance_diff"]), C.PAIR_GEOM_BY_Z[100])[1])
    return best[1] if best else None


def st_route(c, n, board):
    """Batch routing with checkpoints (batchroute.py): fan-out first, then power, critical, rest, local nets
    first; one time-boxed router call per batch, a checkpoint board after each, a leftovers pass, then the
    stall loop. Time-boxed by --route-budget; the result is never less routed than the input."""
    from . import routediag as RD, routestatus as RS, stall as ST, netclass as NC, batchroute as BR
    from . import routerules as RR, routepar as RP
    a = c.a
    rname = getattr(a, "router_name", None) or "krt"
    fr_loc = getattr(a, "fr_loc", None)
    have_krt = bool(a.router_py and a.router_dir and os.path.exists(a.router_py)
                    and os.path.exists(os.path.join(a.router_dir, "py_router", "route.py")))
    if rname == "freerouting" and not fr_loc:
        return {"verdict": "fail", "line": "Freerouting not located; pass --freerouting-jar/--java", "board": board}
    if not have_krt and rname != "freerouting":
        return {"verdict": "fail", "line": f"router not found ({a.router_dir}); pass --router-py/--router-dir",
                "board": board}
    d = c.dir(n, "route")
    from . import questions as QS
    import types
    qa = types.SimpleNamespace(answer=[f"{k}={v}" for k, v in c.answers.items()], assume_defaults=c.assume, qjson=False)
    try:
        qsess, c_cons, c_spec = QS.guard(qa, "route", board, c.cons, c.spec)
    except SystemExit as e:
        if e.code == QS.EXIT:
            return {"verdict": "questions", "line": f"questions open: {QS.questions_path(board)}", "board": board,
                    "next": [QS.questions_path(board)]}
        raise
    c.cons, c.spec = c_cons, c_spec
    best = os.path.join(d, "board.kicad_pcb")
    _copy(board, best)
    _sidecar(board, best)
    t0 = time.time()
    budget = float(a.route_budget)
    spath = os.path.join(d, "routed.kicad_pcb.status.json")
    S = BR.settings(c.cons)
    R = RR.rules(c.cons, board, a.track, a.clearance)
    rflags = RR.flags(R, a.router_dir, d) if have_krt else []
    conn = BR.default_conn(S)
    extra = {}
    dflags = RR.flags(R, a.router_dir, d, script="route_diff.py") if have_krt else []

    def build(s, o, only, lim, pair=False):
        pair = pair and len(only) == 2 and diff_named(*only)       # other pair naming (DP/DM...): route_diff.py cannot see the pair
        return _route_cmd(a, s, o, only, limited=lim, max_ripup=S["max_ripup"], R=R, rflags=rflags, pair=pair,
                          gap=pair_gap(c.cons, only[0]) if pair else None, dflags=dflags)
    run = BR.make_runner(build, a.router_dir, extra, spath, S)

    def left():
        return max(1.0, budget - (time.time() - t0))
    # the rounds and the stall loop stop at (1 - frac) of the budget so the escalation ladder always gets its share;
    # `[route] ladder_reserve_frac = 0` hands everything to the rounds (the ladder then only runs when they end early)
    rc_ = (c.cons or {}).get("route") or {}
    reserve = budget * min(0.9, max(0.0, float(rc_.get("ladder_reserve_frac", 0.25))))
    rounds_budget = budget - reserve
    # the ladder's guaranteed slice: whatever the rounds, their audits and post-steps overran, it still gets this long
    ladder_min = float(rc_.get("ladder_min_s", min(reserve, max(60.0, 0.1 * budget))))

    def nfail(path):
        return sum(conn(path).values())            # open connections (real connectivity), not "nets without copper"

    tm = time.time()
    state = {"best": nfail(best), "timeouts": 0, "halvings": 0, "ckpts": 0}
    state["start"] = state["best"]
    # what one audit of this board costs: a round is followed by a merge, a post-step and a connectivity count that
    # no router timeout covers, so the rounds stop that much earlier (charged to their budget, not to the ladder's)
    overhead = min(0.25 * rounds_budget, 3.0 * (time.time() - tm))

    def left_rounds():
        return max(1.0, rounds_budget - overhead - (time.time() - t0))

    def keep(path):
        """keep-best: only a board with fewer unrouted nets replaces the best-so-far."""
        if path and os.path.exists(path):
            k = nfail(path)
            if k < state["best"]:
                state["best"] = k
                shutil.copy(path, best)
                _sidecar(board, best)
        return state["best"]

    from . import routepost as RPO
    jgap = float(((c.cons or {}).get("route") or {}).get("join_gap_mm", 0.3))
    post_info = {"snapped": 0, "joined": 0, "off_centre": []}

    def post_round(path):
        """after every round: snap track ends into via centres, join small same-net gaps (never worse than before)."""
        r = RPO.post_round(path, conn, jgap)
        post_info["snapped"] += r["snapped"]
        post_info["joined"] += r["joined"]
        post_info["off_centre"] = r["off_centre"]
        return r
    B = RD.load(board)
    todo = sorted(conn(board))
    cls = NC.classify(B, c.cons, c.spec)
    first, _rest = ST.escape_first(B, cls)
    first = [x for x in first if x in todo]
    frrec = None
    if rname == "freerouting" and todo:
        frrec = _freerouting(c, d, board, B, cls, todo, R, conn, post_round, extra, spath,
                             lambda: budget - ladder_min - (time.time() - t0), budget)
        if frrec.get("ok"):
            b0 = state["best"]
            frrec["adopted"] = keep(frrec["out"]) < b0  # every later board is built on it, sub-floor copper included
    start_board = best if frrec else board             # hybrid finish: what Freerouting left goes to the per-net router
    rtodo = sorted(conn(best)) if frrec else todo
    res = {"timeouts": 0, "halvings": 0, "checkpoints": [], "board": None, "leftovers": rtodo}
    if not have_krt or not rtodo:
        pass
    elif R["mode"] == "parallel":
        res = RP.route_parallel(start_board, d, rtodo, build, run, cons=c.cons, spec=c.spec, B=B, cls=cls, first=first,
                                rules=R, total_budget=left_rounds(), status_path=spath, extra=extra,
                                log=lambda m: None, conn=conn, cwd=a.router_dir, post=post_round)
    else:
        res = BR.route_batches(start_board, d, rtodo, run, cons=c.cons, spec=c.spec, B=B, cls=cls, first=first,
                               total_budget=left_rounds(), status_path=spath, extra=extra, log=lambda m: None,
                               rules=R, conn=conn)
    for k in ("timeouts", "halvings"):
        state[k] += res[k]
    state["ckpts"] += len(res["checkpoints"])
    keep(res["board"])
    left_nets = sorted(conn(best))
    timed = bool(res["timeouts"]) and bool(left_nets)
    stall = None
    if have_krt and (left_nets or timed) and a.stall_rounds > 0 and left_rounds() > 5:
        k = [0]

        def again(b_in, b_out, only, timeout):
            k[0] += 1
            r = BR.route_batches(b_in, os.path.join(d, f"stall-batches-{k[0]}"), only, run, cons=c.cons,
                                 spec=c.spec, total_budget=min(timeout, left_rounds()), status_path=spath,
                                 extra=extra, log=lambda m: None, rules=R, conn=conn)
            shutil.copy(r["board"], b_out)
            _sidecar(b_in, b_out)
            state["timeouts"] += r["timeouts"]
            state["halvings"] += r["halvings"]
            state["ckpts"] += len(r["checkpoints"])
            keep(b_out)
            return bool(r["timeouts"]) and bool(r["leftovers"])
        stall = ST.stall_loop(best, d, again, c.cons, c.spec, rounds=a.stall_rounds,
                              replace=a.replace_on_stall, timeout=min(a.route_timeout, left_rounds()),
                              failed0=left_nets, timed_out0=timed, log=lambda m: None)
        keep(stall["board"])
    ladder = None
    from . import routeladder as RLD
    lleft = RLD.clock(budget, t0, ladder_min)         # budget left, never less than the guaranteed slice from now
    times = {"budget": round(budget), "reserve": round(reserve), "ladder_min": round(ladder_min), "overhead": round(overhead, 1),
             "rounds_end": round(time.time() - t0), "ladder_left": round(lleft())}
    if have_krt and conn(best) and lleft() > 15:      # a flat round is not the end: escalate (docs/commands/route.md)
        import copy

        def run_for(layers):
            a2 = copy.copy(a)
            a2.layers = list(layers)

            def build2(s_, o_, only, lim, pair=False):
                pair = pair and len(only) == 2 and diff_named(*only)
                return _route_cmd(a2, s_, o_, only, limited=lim, max_ripup=S["max_ripup"], R=R, rflags=rflags, pair=pair,
                                  gap=pair_gap(c.cons, only[0]) if pair else None, dflags=dflags)
            return BR.make_runner(build2, a.router_dir, extra, spath, S)
        lad = RLD.Ladder(best, os.path.join(d, "ladder"), run, run_for, conn, keep, lleft, B, c.cons, c.spec, cls, a.layers,
                         trial_s=min(a.route_timeout, 180), post=post_round, log=lambda m: None)
        ladder = lad.climb()
        ladder["line"] = RLD.summary(ladder)
    from . import routefr as FR_
    times["ladder_end"] = round(time.time() - t0)
    post_round(best)                                   # the board as it ends: snapped, gaps joined
    final_open = conn(best)
    final = sorted(final_open)
    diag = None
    if final:
        from . import routestatus as RS2
        diag, _ = RS2.diagnose_result({"out": best}, c.cons or None, c.spec)
    el = round(time.time() - t0)
    line = ((FR_.summary(frrec) + "; " if frrec else "") + f"{len(todo)} open net(s), open connections {state['start']} -> {sum(final_open.values())} "
            f"({len(final)} net(s)) in {el}s, "
            f"{state['ckpts']} checkpoint(s)"
            + (f", {state['halvings']} batch halving(s)" if state["halvings"] else "")
            + (f", {state['timeouts']} batch timeout(s)" if state["timeouts"] else "")
            + (f", {res['violations']} RULE_VIOLATION discarded" if res.get("violations") else ""))
    if ladder:
        line += ", " + ladder["line"]
    if post_info["joined"] or post_info["snapped"]:
        line += f", post-step: {post_info['joined']} gap(s) joined, {post_info['snapped']} via end(s) snapped"
    if res.get("dropped"):
        line += ", dropped " + ", ".join(f"{k} {v}" for k, v in res["dropped"].items())
    if res.get("rails"):
        line += f", rails {res['rails']['complete']}/{res['rails']['total']} complete"
    if res.get("pairs"):
        line += ", pairs " + " ".join(f"{k} {len(v)}" for k, v in res["pairs"].items() if v)
    tuned = None
    try:
        from . import qualaudit as QA
        tuned = QA.autotune(best, c.cons, conn)        # pairs failing ONLY on skew: bounded tune, re-audit
    except Exception as e:
        tuned = {"error": str(e)[:80]}
    from . import opengate as OG
    OG.refill(best)                                    # a routed board is saved with fresh zone fills
    try:
        from . import qualaudit as QA2
        Bq = RD.load(best)
        qf = QA2.status_fields(Bq, cls, c.cons, set(conn(best)), set(conn(board)), board=best)
        extra.update(qf, tuned=tuned)
        BR._status_write(spath, extra, "done")
        if qf["not_done"]["count"]:
            line += f", NOT DONE {qf['not_done']['count']} pair/RF net(s) out of tolerance (routed {qf['nets_routed']}/{qf['nets_open0']})"
    except Exception:
        pass
    qsess.mark(best)
    from . import routepost as RPO2
    warn = RPO2.gate_line(post_info)
    if warn:
        line += "; " + warn.split(" (")[0]
    out = {"verdict": "pass" if not final else "fail", "line": line, "board": best, "times": times}
    with open(os.path.join(d, "result.json"), "w") as fh:
        json.dump({"line": line, "router": rname, "times": times, "open_start": state["start"], "open_end": sum(final_open.values()),
                   "freerouting": {k: v for k, v in (frrec or {}).items() if k != "passes"} or None,
                   "ladder": ladder["rungs"] if ladder else None}, fh, indent=1)
    if not have_krt:
        out.setdefault("next", []).append("per-net router (KiCadRoutingTools) not found: no hybrid finish, no ladder; pass --router-dir")
    if frrec:
        out["fr"] = FR_.state_record(frrec)
        if not frrec.get("ok"):
            out.setdefault("next", []).append("Freerouting: " + frrec.get("error", "?"))
        sf = (frrec.get("subfloor") or {}).get("failed") or []
        if sf and frrec.get("adopted"):
            out["verdict"] = "fail"                     # never deliver copper below the floor
            out.setdefault("next", []).insert(0, f"SUB-FLOOR COPPER: {len(sf)} track(s) below {R['min_track']:g} mm could not be widened: " +
                                               "; ".join(f"{x['net']} {x['layer']} {x['at'][0]:g},{x['at'][1]:g} w {x['from']:g} ({x['blocked_by']})"
                                                         for x in sf[:6]) + f" (all: {os.path.join(d, 'freerouting.json')})")
        if frrec.get("adopted") and (not frrec.get("locked_same", True) or frrec.get("plane_tracks_added")):
            out["verdict"] = "fail"
            out.setdefault("next", []).insert(0, "Freerouting changed locked copper or routed on a plane layer; see freerouting.json")
    if ladder:
        out["ladder"] = ladder["rungs"]
        with open(os.path.join(d, "ladder.json"), "w") as fh:
            json.dump(ladder, fh, indent=1)
    if final:
        out["next"] = [f"{sum(final_open.values())} open connection(s) in {len(final)} net(s); first: {', '.join(final[:6])}",
                       f"diagnosis (what to move): {diag}",
                       f"leftovers list: {os.path.join(d, 'leftovers.json')}"]
    if warn:
        out.setdefault("next", []).append(warn)
    if ladder:
        out.setdefault("next", []).append(f"ladder record (which rung closed what): {os.path.join(d, 'ladder.json')}")
    return out


def _freerouting(c, d, board, B, cls, todo, R, conn, post_round, extra, spath, cap, budget):
    """The Freerouting phase of the route stage -> its record (routefr.route_board) + `out`; full record in freerouting.json."""
    from . import routefr as FR, ripolicy as RIP, preroute as PRE, batchroute as BR, pairaudit as PA, routediag as RD
    a = c.a
    try:
        FS = FR.settings(c.cons, threads=getattr(a, "freerouting_threads", None))
    except ValueError as e:
        return {"ok": False, "error": str(e), "version": a.fr_loc["version"]}
    # planes = the copper layers the route stage may not use (--layers lists the signal layers, as for the per-net
    # router); an explicit `[preroute] plane_layers / signal_layers` list wins.  A pour on a signal layer is no plane.
    pc = (c.cons or {}).get("preroute") or {}
    if isinstance(pc.get("plane_layers"), list) or isinstance(pc.get("signal_layers"), list):
        planes = PRE.layer_split(B, c.cons)[1]
    else:
        planes = [l for l in B["copper"] if l not in a.layers]
    ctl = RIP.controlled_nets(cls, c.cons, names=todo)
    skip = [n for n in todo if n in ctl] if FS["pairs"] == "exclude" else []
    want = getattr(a, "freerouting_budget", None) or FS["budget_s"]
    fb = max(10.0, min(float(want) if want else budget * FS["budget_frac"], cap() - 30.0))

    def tick(log, el):
        ps = log["passes"]
        extra.update(router="freerouting", fr_min=round(el / 60, 1), fr_pass=ps[-1]["pass"] if ps else 0,
                     fr_unrouted=ps[-1]["unrouted"] if ps else log["start_unrouted"])
        BR._status_write(spath, extra, "running")
    out = os.path.join(d, "freerouting.kicad_pcb")
    rec = FR.route_board(board, out, os.path.join(d, "freerouting"), loc=a.fr_loc, planes=planes, floor_track=R["min_track"],
                         floor_clearance=R["min_clearance"], budget_s=fb, S=FS, cons=c.cons, skip_nets=skip,
                         has_fanout=BR.has_fanout(board), conn=conn, post=post_round, tick=tick)
    rec["out"] = out
    rec["open_before"] = sum(conn(board).values())
    if rec.get("ok") and FS["pairs"] == "route":         # pairs Freerouting routed itself are never reported good unaudited
        try:
            now = set(conn(out))
            pairs = [p for p in PA.pair_list(cls) if (p[0] in todo or p[1] in todo)]
            au = PA.summary(PA.audit(RD.load(out), pairs, c.cons, now))
            rec["pairs_audit"] = {k: (v if k != "complete" else len(v)) for k, v in au.items() if v}
        except Exception as e:                           # noqa: BLE001
            rec["pairs_audit"] = {"error": str(e)[:100]}
    with open(os.path.join(d, "freerouting.json"), "w") as fh:
        json.dump(rec, fh, indent=1)
    return rec


def st_tune(c, n, board):
    d = c.dir(n, "tune")
    out = os.path.join(d, "board.kicad_pcb")
    _copy(board, out)
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
    rc, text = _run_cli(["silk", board, "--fix", "--out", out] + (["--constraints", c.cons_path] if c.cons_path else [])
                        + (["--spec", c.spec_path] if c.spec_path else []),
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
    rc, text = _run_cli(["modules", board] + c.cs() + c.qs(), os.path.join(d, "stage.log"))
    q = _questions_result(rc, text, board)
    if q:
        return q
    return {"verdict": "pass" if rc == 0 else "fail", "line": _last_line(text) if rc else next(
        (l for l in text.splitlines() if l.startswith("modules:")), _last_line(text))[:160], "board": board}


def st_floorplan(c, n, board):
    d = c.dir(n, "floorplan")
    out = os.path.join(d, "board.kicad_pcb")
    res = _engine_floorplan(c, d, board, out)
    if res:
        return res
    argv = ["floorplan", board, "--out", out] + c.cs() + (["--size"] if getattr(c.a, "size", False) else [])
    rc, text = _run_cli(argv + c.qs(), os.path.join(d, "stage.log"))
    q = _questions_result(rc, text, board)
    if q:
        return q
    line = next((l for l in reversed(text.splitlines()) if l.startswith("floorplan:")), _last_line(text))
    # a floorplan / fan-out / INTENT FAIL fails the stage; `[floorplan] waive_fail = "reason"` is the only (recorded) way past it
    bad = rc != 0 or not os.path.exists(out) or line.startswith("floorplan: FAIL") or any(
        l.startswith("INTENT: FAIL") for l in text.splitlines())
    return dict({"verdict": "fail" if bad else "pass", "line": line, "board": out if os.path.exists(out) else board}, **_wl_record(out))


def st_fanout(c, n, board):
    d = c.dir(n, "fanout")
    out = os.path.join(d, "board.kicad_pcb")
    pours = bool(((c.cons or {}).get("plan") or {}).get("pour"))          # declared pours become real zones; only their pads are fanned out
    rc, text = _run_cli(["fanout", board, "--out", out] + (["--add-zones"] if pours else []) + c.cs() + c.qs(),
                        os.path.join(d, "stage.log"))
    q = _questions_result(rc, text, board)
    if q:
        return q
    line = next((l for l in text.splitlines() if l.startswith("fanout:")), _last_line(text))
    # the gate line decides too: `fanout` returns its verdict code without exiting, so rc alone reads 0 on FANOUT_FAIL
    bad = rc != 0 or any(l.startswith(("fanout: FANOUT_FAIL", "fanout: FANOUT_DRC")) for l in text.splitlines())
    res = {"verdict": "fail" if bad else "pass", "line": line, "board": out if os.path.exists(out) else board}
    if bad:
        res["next"] = [l for l in text.splitlines() if l.startswith("FANOUT_PIN_BLOCKED")][:5]
    return res


def st_plan_route(c, n, board):
    """channels only: sealed pins cannot exist (the fan-out gate passed), so overflow is a WARN and never blocks."""
    d = c.dir(n, "plan-route")
    rc, text = _run_cli(["plan-route", board] + c.cs(), os.path.join(d, "stage.log"))
    last = _last_line(text)
    bad = bool(rc) or "FAIL" in last
    return {"verdict": "warn" if bad else "pass", "line": ("WARN " if bad and not last.startswith("WARN") else "") + last, "board": board}


def st_pairs(c, n, board):
    """differential pairs laid by construction and locked (pairs.py); never blocks: an unlaid pair stays with the router."""
    from . import pairs as PR
    return PR.stage(c, n, board)


FUNCS = {"pairs": st_pairs, "ready": st_ready, "modules": st_modules, "floorplan": st_floorplan, "fanout": st_fanout, "plan-route": st_plan_route,
         "floorplan-check": st_floorplan_check, "anchor-check": st_anchor_check, "place": st_place,
         "route-ready": st_route_ready, "route": st_route, "tune": st_tune, "silk": st_silk, "review": st_review}


# ------------------------------------------------------------------ driver
def _silk_version_line(c, board):
    """A revision copy still carrying the old version on its silkscreen: say so before any stage runs."""
    try:
        from . import silk as SK
        from . import constraints as C
        cons = C.load(c.cons_path) if c.cons_path else {}
        if not SK.settings(cons)["enabled"]:
            return
        sc, cfg, f = SK.run_check(board, cons, spec=c.spec)
        for x in f:
            if x["code"] in ("SILK_VERSION_STALE", "SILK_TITLE_MISSING"):
                print(f"silk: {x['level']} {x['code']} {x['msg']}", flush=True)
    except Exception:                                    # noqa: BLE001 - advisory
        pass


def _stamp_not_ready(stage_dir, reasons):
    """every status file of a stage run on a NOT READY board says so."""
    import glob
    for p in glob.glob(os.path.join(stage_dir, "*.status.json")):
        try:
            d = json.load(open(p))
            d["not_ready"] = reasons
            json.dump(d, open(p, "w"))
        except (OSError, ValueError):
            pass


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
    router = None
    if "route" in STAGES_[i0:i1 + 1]:
        from . import routerloc as RL, routefr as FR, constraints as C_
        want = getattr(a, "router", None)
        if not want:
            try:
                want = ((C_.load(a.constraints) or {}).get("route") or {}).get("router") if a.constraints else None
            except Exception:                           # noqa: BLE001 - the stage reports a bad constraints file
                want = None
        try:
            a.router_name, a.fr_loc, note = FR.choose(want or "krt", getattr(a, "freerouting_jar", None), getattr(a, "java", None))
        except (RuntimeError, ValueError) as e:
            raise SystemExit(f"pipeline: {e}")
        if note:
            print("pipeline: " + note, flush=True)
        try:
            router = RL.resolve(getattr(a, "router_dir", None), getattr(a, "router_py", None))
            a.router_dir, a.router_py = router["dir"], router["py"]
        except RuntimeError as e:
            if a.router_name != "freerouting":
                raise SystemExit(f"pipeline: {e}")
            print(f"pipeline: {e} -- Freerouting only: no hybrid finish, no escalation ladder", flush=True)
            a.router_dir = a.router_py = None
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
    if i0 == 0 or a.board:
        _silk_version_line(c, board)
    rc = 0
    for i in range(i0, i1 + 1):
        name = STAGES_[i]
        t = time.time()
        prev = board
        res = FUNCS[name](c, i + 1, board)
        res.setdefault("verdict", "pass")
        if res.get("board") and res["board"] != prev:
            from . import questions as QS
            QS.inherit(prev, res["board"])
        board = res.get("board", board)
        st["stages"][name] = {"verdict": res["verdict"], "line": res.get("line", ""),
                              "dir": f"{i + 1}-{name}", "board": board}
        if name == "route" and router:
            st["stages"][name]["router"] = {k: router[k] for k in ("dir", "source", "version", "commit")}
        if name == "route":
            st["stages"][name].setdefault("router", {})["name"] = getattr(a, "router_name", "krt")
            if res.get("fr"):
                st["stages"][name]["fr"] = res["fr"]
        if res.get("engine"):
            st["stages"][name]["engine"] = res["engine"]
        st["stage"], st["verdict"], st["board"] = name, res["verdict"], board
        st["next"] = res.get("next", [])
        if res.get("scorecard"):
            st["scorecard"] = res["scorecard"]
        elif res["verdict"] in ("pass",) and name in ("anchor-check", "place", "route", "tune"):
            try:
                st["scorecard"] = _scorecard(board, c.cons, c.spec, a.waive or [])[0]
            except Exception:
                pass
        from . import questions as QS
        nr = QS.notready_reasons(board)
        if nr:
            st["not_ready"] = "; ".join(nr)
            _stamp_not_ready(os.path.join(work, f"{i + 1}-{name}"), st["not_ready"])
        else:
            st.pop("not_ready", None)
        save_state(work, st)
        print(("NOT READY " if nr else "") + f"[{i + 1}/{len(STAGES_)}] {name}: {res['verdict']} - {_clip(res.get('line', ''), 120)} "
              f"({time.time() - t:.0f}s)", flush=True)
        if res["verdict"] == "questions":
            rc = 3
            for l in res.get("next", [])[:40]:
                print(l)
            print("pipeline: questions are open; relay them to the user, then re-run with --answer ID=CHOICE (never answer them yourself)")
            break
        if res["verdict"] in ("fail", "blocked"):
            rc = 1
            break
    print(f"pipeline: {st['verdict']} at {st['stage']}; state {state_path(work)}")
    return rc
