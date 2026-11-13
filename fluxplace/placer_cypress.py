"""Placement engine "cypress": NVIDIA Cypress (GPU, DREAMPlace-based) does the global placement, fluxplace does
everything Cypress has no notion of.

    board model -> cypress_io.build_model -> Bookshelf -> Cypress (its own Python, a subprocess) -> .pl
    -> targets -> legalise.legalise (courtyards, outline, keep-outs, escape allowances) -> clusters.reseat
    -> verify (0 overlaps, inside the outline, fixed parts untouched) -> moves

The tool is found from, in order: the `home` argument (`--cypress-home`), $CYPRESS_HOME, `~/tools/Cypress*`.
Its interpreter: `python` argument, $CYPRESS_PYTHON, then `<home>/env`, `<home>/.venv`, `<home>/../cypress-env`.
Not found -> `Unavailable` with the reason; the caller falls back to the built-in placer.
Settings: `[placement.cypress]` in the constraints (see DEFAULTS and PARAMS; `params = {...}` overrides PARAMS).
"""
import copy
import glob
import json
import math
import os
import re
import subprocess
import time

from . import clusters as CL
from . import cypress_io as IO
from . import geom as GM
from . import legalise as LG
from . import legality as LGL
from . import netclass as NC
from . import preroute as PR

ENGINES = ("builtin", "cypress")
DEFAULTS = {"unit_mm": 0.1, "gap_mm": 0.2, "cluster_weight": 8.0, "rail_weight": 0.0, "max_degree": 40,
            "timeout_s": 900, "threads": 8, "reseat": True, "escape": True, "trials": 4}
# Cypress/DREAMPlace parameters; the Cypress authors tune these per board with a Bayesian search.
PARAMS = {
    "gpu": 1, "num_bins_x": 256, "num_bins_y": 256,
    "global_place_stages": [{"num_bins_x": 256, "num_bins_y": 256, "iteration": 2000, "learning_rate": 0.002,
                             "wirelength": "logsumexp", "optimizer": "nesterov", "Llambda_density_weight_iteration": 10,
                             "Lsub_iteration": 2, "learning_rate_decay": 0.995}],
    "target_density": 0.6, "density_weight": 1.0, "net_crossing_flag": 1, "net_crossing_weight": 0.1,
    "random_seed": 1000, "scale_factor": 1.0, "shift_factor": [0, 0], "ignore_net_degree": 100, "gp_noise_ratio": 0.025,
    "enable_fillers": 1, "global_place_flag": 1, "legalize_flag": 0, "detailed_place_flag": 0, "stop_overflow": 0.07,
    "dtype": "float32", "plot_flag": 0, "RePlAce_LOWER_PCOF": 0.94, "RePlAce_UPPER_PCOF": 1.11, "gamma": 0.4,
    "random_center_init_flag": 1, "routability_opt_flag": 0, "deterministic_flag": 1, "macro_overlap_flag": 1,
    "macro_overlap_weight": 8e-06, "macro_halo_x": 0, "macro_halo_y": 0, "enable_rotation": 1, "create_gif": 0,
}


# one Cypress run per entry (`[placement.cypress] trials = N` runs the first N); the best legal result is kept.
# Measured on a 337-part board: about 4 runs in 10 do not converge, and converged gate scores differ by +-10 %.
_FINE = {"num_bins_x": 512, "num_bins_y": 512, "global_place_stages": [dict(PARAMS["global_place_stages"][0],
                                                                         num_bins_x=512, num_bins_y=512)]}
TRIALS = [{"net_crossing_weight": 0.03}, _FINE, {}, {"net_crossing_weight": 0.03, "random_seed": 7}, {"random_seed": 7},
          {"stop_overflow": 0.1}, {"net_crossing_weight": 0.01}, {"random_seed": 11}]


class Unavailable(RuntimeError):
    """Cypress is not installed / not usable here; the message says what is missing."""


def engine_of(cons, flag=None):
    """The selected engine: the flag, else `[placement] engine`, else builtin."""
    e = flag or ((cons or {}).get("placement") or {}).get("engine") or "builtin"
    if e not in ENGINES:
        raise ValueError(f"placement engine {e!r}: expected one of {', '.join(ENGINES)}")
    return e


def locate(home=None, python=None):
    """-> {home, placer, python, commit}; raises Unavailable."""
    cands = [home] if home else ([os.environ["CYPRESS_HOME"]] if os.environ.get("CYPRESS_HOME") else
                                 sorted(glob.glob(os.path.join(os.path.expanduser("~"), "tools", "[Cc]ypress*"))))
    tried = []
    for h in cands:
        for sub in ("install", ""):
            pl = os.path.join(h, sub, "dreamplace", "Placer.py")
            if os.path.exists(pl) and glob.glob(os.path.join(os.path.dirname(pl), "ops", "*", "*.so")):
                py = python or os.environ.get("CYPRESS_PYTHON") or next(
                    (p for p in (os.path.join(h, "env", "bin", "python"), os.path.join(h, ".venv", "bin", "python"),
                                 os.path.join(os.path.dirname(os.path.abspath(h)), "cypress-env", "bin", "python"))
                     if os.path.exists(p)), None)
                if not py or not os.path.exists(py):
                    raise Unavailable(f"Cypress at {h} but no Python for it (set CYPRESS_PYTHON or --cypress-python)")
                commit = ""
                try:
                    commit = subprocess.run(["git", "-C", h, "rev-parse", "--short", "HEAD"], capture_output=True,
                                            text=True, timeout=10).stdout.strip()
                except (OSError, subprocess.SubprocessError):
                    pass
                return {"home": h, "placer": pl, "python": py, "commit": commit or "unknown"}
        tried.append(h)
    raise Unavailable("Cypress not found (" + (", ".join(tried) + ": no built dreamplace/Placer.py" if tried else
                      "no --cypress-home, no $CYPRESS_HOME, nothing under ~/tools/Cypress*") + "); see docs/CYPRESS.md")


def settings(cons):
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (((cons or {}).get("placement") or {}).get("cypress") or {}).items() if k != "params"})
    return s


def run_tool(tool, aux, workdir, params, timeout=900, threads=8):
    """Run Cypress on a Bookshelf design. -> (.pl path, {seconds, place_s, hpwl, overflow, log})."""
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)
    cfg = dict(params, aux_input=os.path.abspath(aux), result_dir=os.path.join(os.path.abspath(workdir), "results"),
               num_threads=int(threads))
    cpath = os.path.join(workdir, "cypress.json")
    json.dump(cfg, open(cpath, "w"), indent=1)
    logp = os.path.join(workdir, "cypress.log")
    env = dict(os.environ, OMP_NUM_THREADS=str(int(threads)), PYTHONNOUSERSITE="1", MPLBACKEND="Agg")
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    t = time.time()
    try:
        with open(logp, "w") as fh:
            rc = subprocess.run([tool["python"], tool["placer"], cpath], cwd=workdir, stdout=fh, stderr=subprocess.STDOUT,
                                env=env, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        raise Unavailable(f"Cypress timed out after {timeout} s ({logp})")
    except OSError as e:
        raise Unavailable(f"Cypress could not start: {e}")
    dt = time.time() - t
    text = open(logp, errors="replace").read()
    own = os.path.join(workdir, "DREAMPlace.log")                 # the tool's Python log goes to its working directory
    if os.path.exists(own):
        text += open(own, errors="replace").read()
    name = os.path.splitext(os.path.basename(aux))[0]
    pl = os.path.join(cfg["result_dir"], name, f"{name}.gp.pl")
    if rc != 0 or not os.path.exists(pl):
        tail = " | ".join(l.strip() for l in text.strip().splitlines()[-3:])
        raise Unavailable(f"Cypress failed (exit {rc}): {tail[:300]} ({logp})")
    num = lambda rx: (lambda m: float(m[-1]) if m else None)(re.findall(rx, text))
    ovf = num(r"'overflow': ([0-9.eE+-]+)")
    if "placement failed" in text or num(r"'hpwl': ([0-9.eE+]+)") is None:       # it then writes the START positions
        raise Unavailable(f"Cypress did not converge (overflow {ovf}; {logp})")
    return pl, {"seconds": round(dt, 2), "place_s": num(r"non-linear placement takes ([0-9.]+) seconds"),
                "hpwl": num(r"'hpwl': ([0-9.eE+]+)"), "overflow": ovf, "iterations": num(r"'iteration': ([0-9]+)"), "log": logp}


def _keepouts(M):
    out = [(rect, set(sides)) for rect, sides, allow, max_h, _n in M.ko if not allow and max_h is None]
    for x0, y0, x1, y1, z in M.zones:
        sides = {"F" if l == "F.Cu" else "B" for l in z["layers"] if l in ("F.Cu", "B.Cu")}
        if sides and not z.get("owner"):
            out.append(((x0, y0, x1, y1), sides))
    return out


def _legalise(B, tg, fixed, cons, escape, log):
    for kw in ([{"escape": True}] if escape else []) + [{"escape": False}, {"escape": False, "strict": False}]:
        try:
            return LG.legalise(B, tg, fixed=fixed, cons=cons, **kw)[0], kw
        except LG.PlacementError as e:
            log(f"legalise {kw} failed: {str(e)[:100]}")
    raise LG.PlacementError("cypress: the legaliser found no legal placement")


def _finish(B, cons, spec, fixed, tg, S, log):
    """Cypress targets -> a legal board model: legalise, re-seat support parts, verify. Mutates B. -> (final poses, facts)."""
    x0, y0, x1, y1 = B["bbox"]
    M = LG._Model(B, cons, fixed, (x0, y0, x1, y1), escape=bool(S["escape"]))
    origin = {r: (round(p["pos"][0], 4), round(p["pos"][1], 4), p["angle"]) for r, p in B["parts"].items()}
    res, how = _legalise(B, tg, fixed, cons, bool(S["escape"]), log)
    for r, (x, y, a) in res.items():
        GM.place(B["parts"][r], x, y, a)
    d = [math.hypot(res[r][0] - tg[r][0], res[r][1] - tg[r][1]) for r in tg if r in res]
    snap = lambda: {r: (round(p["pos"][0], 4), round(p["pos"][1], 4), p["angle"]) for r, p in B["parts"].items()}
    final, reseated = snap(), 0
    if S["reseat"]:                      # support parts the legaliser pushed away go back beside their IC, legal seats only
        em = float((cons.get("placement") or {}).get("edge_clearance_mm", 0.3))
        legal = LGL.Legal(B, cons, fixed)
        for _ in range(3):
            cl = [c for c in CL.build(B, cons, spec)[0] if c["ic"] not in fixed]
            if not [1 for c in cl for s in c["supports"] if s["sure"] and s["dist"] > s["limit"]]:
                break
            reseated += len(CL.reseat(B, cl, bounds=(x0 + em, y0 + em, x1 - em, y1 - em), legal=legal, strict=True))
        after = snap()
        v = LG.verify(B, M, {r: after[r] for r in tg}, (x0, y0, x1, y1))
        if v["overlaps"] or v["outside"]:
            log(f"reseat left {len(v['overlaps'])} overlap(s): reverted to the legalised placement")
            for r, (x, y, a) in final.items():
                GM.place(B["parts"][r], x, y, a)
            reseated = 0
        else:
            final = after
    v = LG.verify(B, M, {r: final[r] for r in tg}, (x0, y0, x1, y1))
    broken = sorted(r for r in fixed if r in final and final[r] != origin[r])
    facts = {"turned": sum(1 for r in tg if abs((tg[r][2] - origin[r][2] + 180) % 360 - 180) > 1e-6), "legalise": how,
             "legaliser_moved": sum(1 for x in d if x > 0.01), "legaliser_max_mm": round(max(d), 3) if d else 0.0,
             "legaliser_mean_mm": round(sum(d) / len(d), 3) if d else 0.0, "reseated": reseated,
             "overlaps": len(v["overlaps"]), "outside": len(v["outside"]), "in_keepout": len(v["in_keepout"]),
             "fixed_moved": broken}
    if v["overlaps"] or v["outside"] or broken:
        raise LG.PlacementError(f"cypress: result not legal after legalisation: {len(v['overlaps'])} overlap(s) "
                                f"{v['overlaps'][:4]}, {len(v['outside'])} outside {v['outside'][:4]}, fixed moved {broken[:4]}")
    return final, facts


def place(B, cons=None, spec=None, workdir=".", home=None, python=None, log=print, params=None):
    """-> ({ref: (x, y, angle)} for every part that changes, report). Mutates B. Cypress is run `trials` times
    (TRIALS: its result varies a lot with a few parameters and with the seed); every result is legalised and judged
    by the gate score (preroute.score), the best legal one is kept. Raises Unavailable when the tool is missing or no
    trial gave a legal placement (the caller falls back to the built-in placer)."""
    cons = cons or {}
    tool = locate(home, python)
    S = settings(cons)
    os.makedirs(workdir, exist_ok=True)
    cls = NC.classify(B, cons, spec)
    x0, y0, x1, y1 = B["bbox"]
    M = LG._Model(B, cons, PR.fixed_parts(B, cons), (x0, y0, x1, y1), escape=bool(S["escape"]))
    fixed = set(M.fixed)
    origin = {r: (p["pos"][0], p["pos"][1], p["angle"]) for r, p in B["parts"].items()}

    def allowance(ref, part):
        infl, court = M.rel(ref, part["angle"])
        return (court[0] - infl[0], court[1] - infl[1], infl[2] - court[2], infl[3] - court[3])

    model = IO.build_model(B, fixed, cls, clusters=CL.build(B, cons, spec)[0], keepouts=_keepouts(M), allowance=allowance,
                           unit=float(S["unit_mm"]), gap=float(S["gap_mm"]), cluster_weight=float(S["cluster_weight"]),
                           rail_weight=float(S["rail_weight"]), max_degree=int(S["max_degree"]))
    mov = [c for c in model["cells"] if not c["fixed"]]
    if not mov:
        return {}, {"engine": "cypress", "version": tool["commit"], "note": "nothing movable"}
    aux = IO.write_bookshelf(model, os.path.join(workdir, "bookshelf"))
    util = IO.utilisation(model)
    base = dict(PARAMS)
    base["target_density"] = round(min(1.0, max(base["target_density"], util + 0.15)), 3)   # must exceed the fill
    user = dict(params or {})
    user.update((((cons.get("placement") or {}).get("cypress") or {}).get("params")) or {})
    best, why, runs, gpu = None, "", [], 0.0
    for k, over in enumerate(TRIALS[:max(1, int(S["trials"]))]):
        prm = dict(base, **over)
        prm.update(user)
        Bk = copy.deepcopy(B)
        try:
            pl, st = run_tool(tool, aux, os.path.join(workdir, f"trial{k}"), prm, timeout=float(S["timeout_s"]),
                              threads=int(S["threads"]))
            gpu += st["place_s"] or 0.0
            final, facts = _finish(Bk, cons, spec, fixed, IO.targets(model, Bk, IO.read_pl(pl)), S, log)
        except (Unavailable, LG.PlacementError) as e:
            why = str(e)
            runs.append({"trial": k, "failed": why[:160]})
            log(f"cypress trial {k}: {why[:160]}")
            continue
        score = PR.run(Bk, cons, spec)[1]["score"]
        runs.append({"trial": k, "score": score, "place_s": st["place_s"], "iterations": st["iterations"]})
        log(f"cypress trial {k}: gate score {score:.0f} (GPU {st['place_s']} s, legaliser moved {facts['legaliser_moved']}, "
            f"mean {facts['legaliser_mean_mm']} mm)")
        if best is None or score < best[0]:
            best = (score, final, dict(facts, trial=k, seconds=st["seconds"], place_s=st["place_s"], tool_hpwl=st["hpwl"],
                                       overflow=st["overflow"], iterations=st["iterations"], log=st["log"]))
    if best is None:
        raise Unavailable(f"no Cypress trial gave a legal placement ({why[:200]})")
    score, final, rep = best
    for r, (x, y, a) in final.items():
        GM.place(B["parts"][r], x, y, a)
    rep.update({"engine": "cypress", "version": tool["commit"], "cells": len(mov), "fixed_cells": len(model["cells"]) - len(mov),
                "nets": len(model["nets"]), "utilisation": round(util, 3), "score": score, "trials": runs,
                "gpu_s_all_trials": round(gpu, 2)})
    norm = lambda a: (a + 180) % 360 - 180
    moves = {r: f for r, f in final.items() if abs(f[0] - origin[r][0]) > 1e-4 or abs(f[1] - origin[r][1]) > 1e-4
             or abs(norm(f[2] - origin[r][2])) > 1e-6}
    return moves, rep


def format_report(rep):
    if rep.get("note"):
        return f"cypress {rep['version']}: {rep['note']}"
    return (f"engine cypress {rep['version']}: {rep['cells']} cells, {rep['nets']} nets, best of {len(rep['trials'])} trial(s) "
            f"(GPU {rep['gpu_s_all_trials']} s), gate score {rep['score']:.0f}; legaliser moved {rep['legaliser_moved']} "
            f"(max {rep['legaliser_max_mm']} mm, mean {rep['legaliser_mean_mm']} mm); overlaps {rep['overlaps']}, "
            f"outside {rep['outside']}, in keep-out {rep['in_keepout']}, fixed moved {len(rep['fixed_moved'])}")


def record(out, rep):
    """The placement record beside the board: <board stem>.place.json {engine, version, ...}."""
    path = os.path.splitext(out)[0] + ".place.json"
    json.dump(rep, open(path, "w"), indent=1, sort_keys=True)
    return path


def read_record(board):
    try:
        return json.load(open(os.path.splitext(board)[0] + ".place.json"))
    except (OSError, ValueError):
        return {}


def builtin_record(out, strategy, fallback=None):
    from . import __version__
    rep = {"engine": "builtin", "version": __version__, "strategy": strategy}
    if fallback:
        rep["fallback_from_cypress"] = fallback
    return record(out, rep)


def place_file(board, out, cons=None, spec=None, home=None, python=None, workdir=None, log=print):
    """`place --engine cypress` on files. -> report, or the reason (a string) when Cypress is unavailable or failed
    (logged; the caller runs the built-in placer). Raises legalise.PlacementError when the result is not legal."""
    from . import boardwrite as BW, routediag as RD
    try:
        locate(home, python)
        B = RD.load(board)
        moves, rep = place(B, cons, spec, workdir or (os.path.splitext(out)[0] + ".cypress"), home, python, log)
    except Unavailable as e:
        log(f"engine cypress unavailable: {e}; falling back to the built-in placer")
        return str(e)
    BW.move_file(board, out, moves)
    rep["moved"] = len(moves)
    record(out, rep)
    return rep
