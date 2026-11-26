"""Golden microboards: one per ledger mechanism, built in code, run through the production stage seam.

A fixture is a board + .kicad_pro + constraints written into tmp_path, a Ctx built from the real `pipeline` argument parser, and
`stage()` = pipeline._exec_stage (stage function -> commit point -> pcbnew state guard -> stage gate: the loop body `fluxplace pipeline`
runs).  No --force, no lock bypass, no seeded state: whatever a stage needs it creates itself, as in a run.
`_exec_stage` does not exist before the A-series refactor of 2026-11-26; `stage()` then composes the same calls from the pieces that
do (used only to check a fixture against the commit before a fix)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(HERE))
import pcbgen as g  # noqa: E402

PRO = {"board": {"design_settings": {"rules": {"min_track_width": 0.1, "min_clearance": 0.1, "min_via_diameter": 0.4,
                                               "min_through_hole_diameter": 0.2, "min_hole_to_hole": 0.25}}}}


def write_board(d, body, name="in.kicad_pcb", outline=(0, 0, 40, 30), pro=True):
    """4-layer board (F / In1 / In2 GND plane / B) + its .kicad_pro; -> path."""
    d.mkdir(parents=True, exist_ok=True)
    x0, y0, x1, y1 = outline
    edge = f'(gr_rect (start {x0} {y0}) (end {x1} {y1}) (stroke (width 0.1) (type solid)) (fill none) (layer "Edge.Cuts"))\n'
    p = g.board(d, body, name=name, outline=outline, edge=edge)            # In2.Cu GND plane inset 1 mm from the outline (no edge-clearance noise)
    x0, y0, x1, y1 = outline
    txt = open(p).read().replace('(31 "B.Cu" signal)', '(6 "In2.Cu" signal) (31 "B.Cu" signal)')
    plane = (f'(zone (net 1) (net_name "GND") (layer "In2.Cu") (hatch edge 0.5) (polygon (pts (xy {x0 + 1} {y0 + 1}) (xy {x1 - 1} {y0 + 1}) '
             f'(xy {x1 - 1} {y1 - 1}) (xy {x0 + 1} {y1 - 1}))))\n')
    open(p, "w").write(txt.rstrip().rstrip(")") + plane + ")\n")
    if pro:
        with open(os.path.splitext(p)[0] + ".kicad_pro", "w") as fh:
            json.dump(PRO, fh)
    return p


def context(tmp, board, cons="", spec=None, argv=()):
    """-> pipeline.Ctx for `board` with the CLI's own argument defaults and constraints text `cons`."""
    import cli
    from fluxplace import pipeline as P
    cp = tmp / "constraints.toml"
    cp.write_text(cons)
    argv = ["pipeline", board, "--workdir", str(tmp / "W"), "--constraints", str(cp)] + list(argv)
    a = cli.build_parser().parse_args(argv)
    work = os.path.abspath(a.workdir)
    os.makedirs(work, exist_ok=True)
    return P.Ctx(a, work, board, {"stages": {}})


def use_krt(c):
    """What `pipeline._run` sets on the arguments before a `route` stage in krt-whole mode (router located the way the run locates it)."""
    from fluxplace import routerloc as RL
    a = c.a
    a.route_mode, a.router_name, a.fr_loc = "krt-whole", "krt", None
    r = RL.resolve(getattr(a, "router_dir", None), getattr(a, "router_py", None))
    a.router_dir, a.router_py = r["dir"], r["py"]
    return c


def stage(c, n, name, board, later=()):
    from fluxplace import pipeline as P
    if hasattr(P, "_exec_stage"):
        return P._exec_stage(c, n, name, board, later)
    res = P.FUNCS[name](c, n, board)                       # pre-refactor commits: the same calls, same order
    res.setdefault("verdict", "pass")
    if res["verdict"] in ("pass", "warn"):
        res = P._commit_point(name, board, res)
    guard = getattr(P, "_pcbnew_state_guard", None)
    res = guard(name, res) if guard else res
    from fluxplace import gaps as GP
    gk = GP.gate_kinds(c.cons, name)
    if res["verdict"] in ("pass", "warn") and gk:
        g = P._gate_check(c, n, name, res.get("board") or board, gk, src=board, later=later)
        res.update(verdict=g["verdict"], line=g["line"], next=g["next"], board=g["board"], gate=g["gate"])
    return res


def chain(c, board, names, first=1):
    """Run `names` in order, each from the previous stage's board; -> [result]; stops after a result that is not pass/warn."""
    out = []
    for i, nm in enumerate(names, first):
        r = stage(c, i, nm, board, later=names[i - first + 1:])
        out.append(r)
        if r["verdict"] not in ("pass", "warn"):
            break
        board = r.get("board") or board
    return out
