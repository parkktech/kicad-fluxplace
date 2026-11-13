"""Placement engine "cypress": NVIDIA Cypress (GPU, DREAMPlace-based) does the global placement, fluxplace does
everything Cypress has no notion of.

    board model -> cypress_io.build_model -> Bookshelf -> Cypress (its own Python, a subprocess) -> .pl
    -> targets -> legalise.legalise (courtyards, outline, keep-outs, escape allowances) -> clusters.reseat
    -> verify (0 overlaps, inside the outline, fixed parts untouched) -> moves

With an `[intent]` table (docs/INTENT.md) the flow is intent-aware: `intent.prepare` seats and holds the hard items first
(fixed cells), the zones become blockages, `near` / support / pair relations become weighted nets, an IC and its support
parts (and every rigid group) travel as one macro cell, and each legal trial is judged by the gate (INTENT rows, fan-out
check, plan-route) whose findings inflate the crowded parts for the next trial. A trial with an unmet hard intent is rejected.

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
from . import intent as IT
from . import legalise as LG
from . import legality as LGL
from . import modules as MD
from . import netclass as NC
from . import preroute as PR

ENGINES = ("builtin", "cypress")
DEFAULTS = {"unit_mm": 0.1, "gap_mm": 0.2, "cluster_weight": 8.0, "rail_weight": 0.0, "max_degree": 40,
            "timeout_s": 900, "threads": 8, "reseat": True, "escape": True, "trials": 4,
            # intent-aware flow (an [intent] table, or feedback = true); `trials` defaults to INTENT_TRIALS there
            "feedback": None,          # None = on with an [intent] table: the gate scores each trial and inflates what is crowded
            "macros": "auto",          # an IC + its support parts as one cell: true | false | "auto" (try both, keep the better)
            "near_weight": 1.0, "pair_weight": 2.0, "block_share": 0.5, "reserve_gain": 1.0,
            "inflate_step_mm": 0.4, "inflate_max_mm": 1.6, "blocked_pin_cost": 40.0, "soft_intent_cost": 20.0}
INTENT_TRIALS = 6
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


def _intent(cons):
    return bool(IT.section(cons)) or IT.enabled(cons)


class Unavailable(RuntimeError):
    """Cypress is not installed / not usable here; the message says what is missing."""


class Rejected(LG.PlacementError):
    """A legal trial the gate refuses (an unmet hard intent)."""


class NotConverged(Unavailable):
    """The run ended above its overflow target (`overflow` = where it ended); Cypress then keeps the start positions."""
    overflow = None


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
        e = NotConverged(f"Cypress did not converge (overflow {ovf}; {logp})")
        e.overflow = ovf
        raise e
    return pl, {"seconds": round(dt, 2), "place_s": num(r"non-linear placement takes ([0-9.]+) seconds"),
                "hpwl": num(r"'hpwl': ([0-9.eE+]+)"), "overflow": ovf, "iterations": num(r"'iteration': ([0-9]+)"), "log": logp}


def _keepouts(M):
    out = [(rect, set(sides)) for rect, sides, allow, max_h, _n in M.ko if not allow and max_h is None]
    for x0, y0, x1, y1, z in M.zones:
        sides = {"F" if l == "F.Cu" else "B" for l in z["layers"] if l in ("F.Cu", "B.Cu")}
        if sides and not z.get("owner"):
            out.append(((x0, y0, x1, y1), sides))
    return out


def _legalise(B, tg, fixed, cons, escape, log, units=None, rigid=(), loose=True):
    for kw in ([{"escape": True}] if escape else []) + [{"escape": False}] + ([{"escape": False, "strict": False}] if loose else []):
        try:
            return LG.legalise(B, tg, fixed=fixed, cons=cons, units=units, rigid=rigid, **kw)[0], kw
        except LG.PlacementError as e:
            log(f"legalise {kw} failed: {str(e)[:100]}")
    raise LG.PlacementError("cypress: the legaliser found no legal placement")


def repair_support(B, cons, spec, fixed, escape, log):
    """Support parts the gate still finds beyond their limit (no legal seat among today's neighbours): their IC is laid
    out with ALL its support parts on its own (clusters.seat, isolate), that cell is one rigid unit, and the legaliser
    runs again from the present poses so the neighbours make room. Around a fixed IC the support parts are seated the
    same way and held while the others are pushed away. Mutates B; reverts when the result is not legal. -> parts repaired."""
    parts = B["parts"]
    snap = lambda: {r: (round(p["pos"][0], 4), round(p["pos"][1], 4), p["angle"]) for r, p in parts.items()}
    bad = lambda: {r for row in IT.support_rows(B, cons, spec) if row["met"] is False and row["hard"] for r in row["refs"]}
    todo = bad()
    n0 = len(todo)
    for _ in range(4):
        if not todo:
            break
        before, units, hold, taken = snap(), {}, set(), set()
        stat = [(r, parts[r]) for r in fixed if r in parts]
        for c in CL.build(B, cons, spec)[0]:
            ic = c["ic"]
            if not any(s["ref"] in todo for s in c["supports"]):
                # a neighbour that is pushed aside keeps the support parts it has at its pins
                ok = [s["ref"] for s in c["supports"] if s["sure"] and s["dist"] <= s["limit"] and s["ref"] not in fixed
                      and s["ref"] not in taken and ic not in taken]
                if ic in fixed:
                    hold |= set(ok)
                elif ok:
                    units[ic] = [ic] + sorted(ok)
                taken |= set(ok) | {ic}
                continue
            sup = [s for s in c["supports"] if (s["sure"] or s["how"] == "spread") and s["ref"] not in fixed
                   and s["ref"] not in taken and not parts[s["ref"]].get("locked") and (ic not in fixed or s["ref"] in todo)]
            taken |= {s["ref"] for s in sup} | {ic}
            mv = CL.seat(B, {"ic": ic, "supports": sup}, isolate=True)
            for r, (x, y, a) in mv.items():
                GM.place(parts[r], x, y, a)
            if ic in fixed:                 # a seat on another fixed part is no seat
                for r in mv:
                    q = parts[r]
                    if any(o is not parts[ic] and (o["side"] == q["side"] or o["tht"]) and GM.overlap(q["court"], o["court"])
                           for _n, o in stat) or not GM.inside(q["court"], B["bbox"]):
                        GM.place(q, *before[r])
                    else:
                        hold.add(r)
            elif mv:
                units[ic] = [ic] + sorted(mv)
        tg = {r: v for r, v in snap().items() if r not in fixed and r not in hold}
        try:
            res, _kw = _legalise(B, tg, set(fixed) | hold, cons, escape, lambda m: None, units, list(units), loose=False)
        except LG.PlacementError:
            res = None
        if res is not None:
            for r, (x, y, a) in res.items():
                GM.place(parts[r], x, y, a)
        left = bad() if res is not None else todo
        if os.environ.get("FLUXPLACE_CYPRESS_DEBUG"):
            log(f"  repair round: todo {sorted(todo)}, {len(units)} units, held {sorted(hold)}, legal {res is not None}, left {sorted(left)}")
            for row in IT.support_rows(B, cons, spec):
                log("   " + str(row.get("detail"))[:300])
        if res is None or len(left) >= len(todo):
            for r, (x, y, a) in before.items():
                GM.place(parts[r], x, y, a)
            break
        todo = left
    if n0:
        log(f"support repair: {n0 - len(todo)} of {n0} part(s) beyond their limit brought to their pins")
    return n0 - len(todo)


def _finish(B, cons, spec, fixed, tg, S, log, units=None, rigid=()):
    """Cypress targets -> a legal board model: legalise (`units` = macros kept as one body), re-seat support parts, verify.
    Mutates B. -> (final poses, facts)."""
    x0, y0, x1, y1 = B["bbox"]
    M = LG._Model(B, cons, fixed, (x0, y0, x1, y1), escape=bool(S["escape"]))
    origin = {r: (round(p["pos"][0], 4), round(p["pos"][1], 4), p["angle"]) for r, p in B["parts"].items()}
    res, how = _legalise(B, tg, fixed, cons, bool(S["escape"]), log, units, rigid)
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
        if _intent(cons):                # as the intent seating does: rail caps near no IC pin go to the pin they are assigned to
            cl = CL.build(B, cons, spec)[0]
            spread = {s["ref"] for c in cl for s in c["supports"] if s["how"] == "spread" and s["dist"] > s["limit"]
                      and s["ref"] not in fixed}
            if spread:
                reseated += len(CL.reseat(B, cl, guess=True, only=spread, bounds=(x0 + em, y0 + em, x1 - em, y1 - em),
                                          legal=legal, strict=True))
        if _intent(cons):
            reseated += repair_support(B, cons, spec, fixed, bool(S["escape"]), log)
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
    if os.environ.get("FLUXPLACE_CYPRESS_DEBUG"):
        log(f"  finish: legalise {how}, in keep-out {v['in_keepout']}, reseated {reseated}")
    if v["overlaps"] or v["outside"] or broken:
        raise LG.PlacementError(f"cypress: result not legal after legalisation: {len(v['overlaps'])} overlap(s) "
                                f"{v['overlaps'][:4]}, {len(v['outside'])} outside {v['outside'][:4]}, fixed moved {broken[:4]}")
    return final, facts


# ---------------------------------------------------------------- intent-aware pieces
def _area(c):
    return max(0.0, c[2] - c[0]) * max(0.0, c[3] - c[1])


def hpwl(B):
    """Half-perimeter wire length over every net with two or more pads, mm."""
    by = {}
    for q in B["pads"]:
        if q["net"]:
            by.setdefault(q["net"], []).append(q)
    return sum(max(q["x"] for q in v) - min(q["x"] for q in v) + max(q["y"] for q in v) - min(q["y"] for q in v)
               for v in by.values() if len(v) > 1)


def zone_blocks(B, M, fixed, share=0.5):
    """Intent zones (regions, swept card areas, the clear depth in front of an edge part, `away` halos of held parts)
    -> ([(rect, {side})] blockages for Cypress, [names] it cannot express). Cypress has one blockage for every cell while
    a zone concerns some parts only (height, `refs`, `allow`): a zone is a blockage on a side when it applies to at least
    `share` of the movable courtyard area there; the rest is left to the legaliser (it knows the zones per part) and the gate."""
    Z, parts = M.intent, B["parts"]
    mov = [r for r in parts if r not in fixed and _area(parts[r]["court"]) > 0]
    tot = {s: sum(_area(parts[r]["court"]) for r in mov if parts[r]["side"][0] == s) for s in "FB"}
    blocks, post = [], []
    for z in Z.z:
        done = False
        for s in sorted(z["sides"]):
            hit = sum(_area(parts[r]["court"]) for r in mov if parts[r]["side"][0] == s and Z.applies(z, r))
            if tot[s] > 0 and hit >= share * tot[s]:
                blocks.append((tuple(z["rect"]), {s}))
                done = True
        if not done:
            post.append(z["name"])
    return blocks, post


def relation_links(B, cons, cls, S):
    """-> (links [{name, pads [(ref, pad)], weight}], {net: weight}). `near` items: one two-pin net per link of the chain,
    weight scaled from the budget (the tighter the budget against the board, the stronger the pull). Pairs: both nets
    weigh `pair_weight` times their class weight, and the two-pad parts in series on the two halves (same footprint) are
    tied pad to pad so the pair's endpoints stay side by side."""
    links, diag = [], B["diag"]
    pads, info = IT.plan_links(B, cons)
    for name in sorted(pads):
        pa, pb = pads[name]
        i = info[name]
        w = float(S["near_weight"]) * i["weight"] * min(8.0, max(1.0, diag / (4.0 * max(i["budget"], 0.5))))
        links.append({"name": name, "pads": [(pa["ref"], pa["num"]), (pb["ref"], pb["num"])], "weight": round(w, 2)})
    parts, by, pw, used, weights = B["parts"], NC.net_pads(B), float(S["pair_weight"]), set(), {}
    for a in sorted(cls):
        b = cls[a].get("partner")
        if not b or b not in cls:
            continue
        weights[a] = cls[a]["weight"] * pw
        if b < a:
            continue
        two = lambda n: {q["ref"]: q for q in by[n] if parts[q["ref"]]["n_pads"] == 2}
        A, Bn = two(a), two(b)
        for ra in sorted(set(A) - set(Bn)):
            cand = sorted(r for r in set(Bn) - set(A) if r not in used and parts[r].get("fp") == parts[ra].get("fp"))
            if cand:
                used.add(cand[0])
                links.append({"name": f"pair/{a}/{ra}-{cand[0]}", "pads": [(ra, A[ra]["num"]), (cand[0], Bn[cand[0]]["num"])],
                              "weight": round(max(1.0, cls[a]["weight"]) * pw, 2)})
    return links, weights


def escape_reserve(B, cons, spec, cls, gain=1.0, cap=1.0):
    """{ref: mm} for fine-pitch parts and ICs: the margin whose ring around the courtyard holds one via site per pin that
    must own an escape (escres.needy), times `gain`, at most `cap`."""
    from . import escapeplan as EP, escres as ER, routerules as RR
    if not ER.params(cons)["enabled"]:
        return {}
    R = RR.rules(cons, None, B=B)
    site = max(float(R["via"]), 0.3) + max(float(R["clearance"]), float(B["clearance"]))
    pr = EP.plan_params(cons)
    need = {}
    for t in ER.needy(B, cons, spec, cls):
        need[t["ref"]] = need.get(t["ref"], 0) + 1
    out = {}
    for ref, k in need.items():
        p = B["parts"][ref]
        if not (EP.is_fine(p, pr) or NC.is_ic(ref, p, 3)):
            continue
        wh = (p["court"][2] - p["court"][0]) + (p["court"][3] - p["court"][1])
        out[ref] = round(min(cap, gain * (-wh + math.sqrt(wh * wh + 4.0 * k * site * site)) / 4.0), 3)
    return out


def _compact(parts, refs):
    cs = [parts[r]["court"] for r in refs]
    box = (max(c[2] for c in cs) - min(c[0] for c in cs)) * (max(c[3] for c in cs) - min(c[1] for c in cs))
    return box <= LG.SPRAWL * sum(_area(c) for c in cs)


def build_macros(B, cons, fixed, clusters, support=True, skip=()):
    """-> ({key: [refs]}, [rigid keys]): the parts Cypress moves as ONE cell. Rigid `[placement.group]`s of movable parts
    (the intent's rigid bodies among them) as they stand; with `support` every movable IC is first laid out with its sure
    same-side support parts on their own (clusters.seat, isolate: the existing support seating) - that mutates B."""
    parts = B["parts"]
    macros, rigid, taken = {}, [], set(fixed)
    for name, g in sorted((((cons or {}).get("placement") or {}).get("group") or {}).items()):
        refs = [r for r in (g.get("refs") or []) if r in parts] if isinstance(g, dict) else []
        if len(refs) > 1 and not g.get("centre") and not set(refs) & taken and _compact(parts, refs):
            macros["group:" + name] = refs
            rigid.append("group:" + name)
            taken |= set(refs)
    for c in clusters if support else ():
        ic = c["ic"]
        if ic in taken:
            continue
        sup = [s for s in c["supports"] if s["sure"] and s["ref"] not in taken and s["ref"] not in skip
               and not parts[s["ref"]].get("locked") and not parts[s["ref"]]["tht"] and parts[s["ref"]]["side"] == parts[ic]["side"]]
        if not sup:
            continue
        mv = CL.seat(B, {"ic": ic, "supports": sup}, isolate=True)
        for r, (x, y, a) in mv.items():
            GM.place(parts[r], x, y, a)
        mem = [ic] + sorted(mv, key=lambda r: (GM.rect_gap(parts[ic]["court"], parts[r]["court"]), r))
        while len(mem) > 1 and not _compact(parts, mem):
            mem.pop()
        if len(mem) > 1:
            macros[ic] = mem
            taken |= set(mem)
    return macros, rigid


def _fanargs():
    import argparse
    return argparse.Namespace(track=None, clearance=None, via=None, drill=None, via_pad_gap=0.2, reach=2.2, lane=1.0,
                              max_dom=60, nodes=200000, repairs=12, check_only=True)


def assess(B, cons, spec, src, moves, wd, S, log):
    """The gate on one legal trial (B = the board model as placed). -> facts with `score` (lower is better):
    plan-route PLAN_SCORE + `blocked_pin_cost` per blocked fan-out pin + `soft_intent_cost` per unmet soft intent row.
    Raises Rejected when a hard intent row is not met. `src` (a board file) + `moves` are needed for the fan-out check."""
    from . import planroute as PLR
    out = {"blocked": None, "required": None}
    soft = 0
    if _intent(cons):
        rows = IT.check(B, cons, spec)
        bad = [r for r in rows if r["met"] is False]
        hard = [r for r in bad if r["hard"]]
        soft = len(bad) - len(hard)
        out["intent"] = {"met": sum(1 for r in rows if r["met"] is True), "soft_unmet": soft, "hard_unmet": len(hard)}
        if hard and IT.enabled(cons):
            for r in hard:
                log(f"  NOT MET hard {r['kind']} {r['name']}: {r['value']} (limit {r['limit']}) {str(r.get('detail') or '')[:300]}")
            raise Rejected("hard intent unmet: " + "; ".join(f"{r['kind']} {r['name']}"[:70] for r in hard[:4])
                           + (f" (+{len(hard) - 4})" if len(hard) > 4 else ""))
    R = PLR.run(B, cons, spec, ignore_routing=True, open_method="text")
    c = R["comp"]
    out.update({"plan": R["score"], "overflow": c["overflow"], "risk_nets": c.get("risk_nets", 0), "sealed": c["sealed"],
                "over_budget_mm": c["over_budget_mm"], "copper_mm": c["copper_mm"], "pair_split": c["pair_split"], "hpwl": round(hpwl(B)), "_findings": R["findings"], "_blocked": []})
    if src:
        try:
            from . import boardwrite as BW, fanout as FO
            path = os.path.join(wd, "trial.kicad_pcb")
            BW.move_file(src, path, moves, post=False)
            pro = os.path.splitext(src)[0] + ".kicad_pro"
            if os.path.exists(pro):
                import shutil
                shutil.copy(pro, os.path.splitext(path)[0] + ".kicad_pro")
            Bn, fres = FO.run(path, cons, spec, None, _fanargs(), None)
            per = FO.summarise(Bn, fres)
            out["_blocked"] = [p for v in per.values() for p in v["blocked"]]
            out["required"], out["blocked"] = sum(v["req"] for v in per.values()), len(out["_blocked"])
        except (Exception, SystemExit) as e:         # the fan-out check must never lose a legal trial
            log(f"fan-out check unavailable: {str(e).strip().splitlines()[0][:120] if str(e).strip() else type(e).__name__}")
    out["score"] = round(R["score"] + float(S["blocked_pin_cost"]) * (out["blocked"] or 0) + float(S["soft_intent_cost"]) * soft, 1)
    return out


def feedback(B, fixed, findings, blocked, infl, blocks, S):
    """Congestion-driven inflation for the next trial. A part with a blocked fan-out pin, the parts blamed for it and the
    parts bordering a plan-route overflow get `inflate_step_mm` more room (up to `inflate_max_mm`); an overflow that
    borders a fixed part becomes a blockage there (a fixed part does not move between trials). Mutates `infl` {ref: mm}
    and `blocks` [(rect, sides)]. -> (parts given more room, blockages added)."""
    parts, hot = B["parts"], {}
    add = lambda r, v: hot.__setitem__(r, hot.get(r, 0.0) + v) if r in parts else None
    for p in blocked or ():
        add(p["ref"], 1.0)
        for name, _n in (p.get("blame") or [])[:3]:
            if str(name).split(".")[0] != p["ref"]:
                add(str(name).split(".")[0], 0.5)
        m = re.search(r"([A-Za-z]+\d+\w*)\.", str(p.get("blocker") or ""))
        if m and m.group(1) != p["ref"]:
            add(m.group(1), 0.5)
    nb, seen = 0, {tuple(round(v, 1) for v in r) for r, _s in blocks}
    for f in findings or ():
        e = float(f.get("eff", f.get("overflow", 0)) or 0) if f.get("code") == "PLAN_OVERFLOW" else 0.0
        if e <= 0:
            continue
        near = [r for r in f.get("parts") or () if r in parts]
        for r in near:
            add(r, min(1.0, e))
        box = f.get("box")
        if box and any(r in fixed for r in near) and len(blocks) < 24 and tuple(round(v, 1) for v in box) not in seen:
            blocks.append((tuple(float(v) for v in box), {"F.Cu": {"F"}, "B.Cu": {"B"}}.get(f.get("layer"), {"F", "B"})))
            seen.add(tuple(round(v, 1) for v in box))
            nb += 1
    step, cap, n = float(S["inflate_step_mm"]), float(S["inflate_max_mm"]), 0
    for r, v in hot.items():
        new = round(min(cap, infl.get(r, 0.0) + step * min(2.0, max(0.5, v))), 3)
        if r not in fixed and new > infl.get(r, 0.0):
            infl[r] = new
            n += 1
    return n, nb


def place(B, cons=None, spec=None, workdir=".", home=None, python=None, log=print, params=None, board=None):
    """-> ({ref: (x, y, angle)} for every part that changes, report). Mutates B. Cypress is run `trials` times
    (TRIALS: its result varies a lot with a few parameters and with the seed); every result is legalised and judged,
    the best legal one is kept. Classic flow: judged by the gate score (preroute.score). Intent-aware flow (an `[intent]`
    table, or `feedback = true`): zones are blockages, relations are weighted nets, ICs travel with their support parts
    as macros, every trial is judged by `assess` and its congestion inflates the next (`feedback`); `board` = the file B
    was loaded from (the fan-out check needs one). Raises Unavailable when the tool is missing or no trial gave a legal,
    accepted placement (the caller falls back to the built-in placer)."""
    cons = cons or {}
    tool = locate(home, python)
    S = settings(cons)
    intent = _intent(cons)
    fb = intent if S["feedback"] is None else bool(S["feedback"])
    aware = intent or fb
    own = (cons.get("placement") or {}).get("cypress") or {}
    n_trials = max(1, int(own.get("trials", INTENT_TRIALS if aware else S["trials"])))
    os.makedirs(workdir, exist_ok=True)
    cls = NC.classify(B, cons, spec)
    x0, y0, x1, y1 = B["bbox"]
    # with an intent the owner's list decides what is held (as in the floorplan); without one every detected anchor is
    M = LG._Model(B, cons, MD.fixed_set(B, cons) if intent else PR.fixed_parts(B, cons), (x0, y0, x1, y1), escape=bool(S["escape"]))
    fixed = set(M.fixed)
    origin = {r: (p["pos"][0], p["pos"][1], p["angle"]) for r, p in B["parts"].items()}
    norm = lambda a: (a + 180) % 360 - 180
    changed = lambda final: {r: f for r, f in final.items() if abs(f[0] - origin[r][0]) > 1e-4 or abs(f[1] - origin[r][1]) > 1e-4
                             or abs(norm(f[2] - origin[r][2])) > 1e-6}
    reserve = escape_reserve(B, cons, spec, cls, float(S["reserve_gain"])) if aware and S["escape"] else {}
    infl, fb_blocks = {}, []

    def allowance(ref, part):
        big, court = M.rel(ref, part["angle"])
        e, g = reserve.get(ref, 0.0), infl.get(ref, 0.0)
        return tuple(max(v, e) + g for v in (court[0] - big[0], court[1] - big[1], big[2] - court[2], big[3] - court[3]))

    zb, post = zone_blocks(B, M, fixed, float(S["block_share"])) if intent else ([], [])
    links, weights = relation_links(B, cons, cls, S) if aware else ([], None)
    near_refs = {r for l in links if l["name"].startswith("intent/") for r, _p in l["pads"]}
    variants = {}

    def variant(mac):
        if mac not in variants:
            Bv = copy.deepcopy(B) if mac else B
            cl = CL.build(Bv, cons, spec)[0]
            macros, rigid = build_macros(Bv, cons, fixed, cl, support=mac, skip=near_refs) if aware else ({}, [])
            if mac:
                cl = CL.build(Bv, cons, spec)[0]
            for c in cl if aware else ():          # support nets: weight scaled from the part's distance limit
                for s in c["supports"]:
                    s["link_weight"] = round(max(float(S["cluster_weight"]), s.get("weight") or 0.0)
                                             * min(4.0, max(1.0, 5.0 / max(s["limit"], 0.5))), 2)
            variants[mac] = (Bv, cl, macros, rigid)
        return variants[mac]

    user = dict(params or {})
    user.update(own.get("params") or {})
    best, why, runs, gpu, t_all = None, "", [], 0.0, time.time()
    for k in range(n_trials):
        over = TRIALS[k % len(TRIALS)]
        mac = False
        if aware and S["macros"] == "auto":       # trial 0 with macros, trial 1 without, then the mode of the best so far
            mac = (k % 2 == 0) if best is None or k < 2 else bool(best[2].get("macro_mode"))
        elif aware:
            mac = bool(S["macros"])
        Bv, cl, macros, rigid = variant(mac)
        model = IO.build_model(Bv, fixed, cls, clusters=cl, keepouts=_keepouts(M) + zb + fb_blocks, allowance=allowance,
                               unit=float(S["unit_mm"]), gap=float(S["gap_mm"]), cluster_weight=float(S["cluster_weight"]),
                               rail_weight=float(S["rail_weight"]), max_degree=int(S["max_degree"]), macros=macros,
                               links=links, net_weights=weights)
        mov = [c for c in model["cells"] if not c["fixed"]]
        if not mov:
            return {}, {"engine": "cypress", "version": tool["commit"], "note": "nothing movable"}
        tdir = os.path.join(workdir, f"trial{k}")
        aux = IO.write_bookshelf(model, os.path.join(tdir if aware else workdir, "bookshelf"))
        util = IO.utilisation(model)
        prm = dict(PARAMS, **over)
        prm["target_density"] = round(min(1.0, max(prm["target_density"], util + 0.15)), 3)   # must exceed the fill
        prm.update(user)
        Bk = copy.deepcopy(Bv)
        rec = {"trial": k}
        if aware:
            rec.update({"macros": len(macros), "inflated": len(infl), "blocks": len(fb_blocks)})
        try:
            try:
                pl, st = run_tool(tool, aux, tdir, prm, timeout=float(S["timeout_s"]), threads=int(S["threads"]))
            except NotConverged as e:
                # a dense board settles above the overflow target: accept that level once (the legaliser does the rest)
                if e.overflow is None or not 0 < e.overflow < 0.3 or "stop_overflow" in user:
                    raise
                prm["stop_overflow"] = round(e.overflow + 0.02, 3)
                log(f"cypress trial {k}: settled at overflow {e.overflow:.3f}; once more with stop_overflow {prm['stop_overflow']}")
                pl, st = run_tool(tool, aux, os.path.join(workdir, f"trial{k}r"), prm, timeout=float(S["timeout_s"]),
                                  threads=int(S["threads"]))
            gpu += st["place_s"] or 0.0
            final, facts = _finish(Bk, cons, spec, fixed, IO.targets(model, Bk, IO.read_pl(pl)), S, log,
                                   units=macros or None, rigid=rigid)
            if aware:
                A = assess(Bk, cons, spec, board, changed(final), tdir, S, log)
                score = A["score"]
                rec.update({q: v for q, v in A.items() if not q.startswith("_")})
            else:
                score = PR.run(Bk, cons, spec)[1]["score"]
        except (Unavailable, LG.PlacementError) as e:
            why = str(e)
            rec["rejected" if isinstance(e, Rejected) else "failed"] = why[:160]
            runs.append(rec)
            log(f"cypress trial {k}: {'REJECTED: ' if isinstance(e, Rejected) else ''}{why[:160]}")
            continue
        rec.update({"score": score, "place_s": st["place_s"], "iterations": st["iterations"]})
        runs.append(rec)
        if aware:
            log(f"cypress trial {k}: score {score:.0f} = plan {A['plan']:.0f} (overflow {A['overflow']}, risk nets {A['risk_nets']}), "
                f"fan-out blocked {A['blocked']} of {A['required']}, intent {A.get('intent')}, HPWL {A['hpwl']} mm; "
                f"{len(macros)} macro(s), {len(infl)} part(s) inflated, GPU {st['place_s']} s, legaliser mean {facts['legaliser_mean_mm']} mm")
            if fb:
                n, nb = feedback(Bk, fixed, A["_findings"], A["_blocked"], infl, fb_blocks, S)
                if n or nb:
                    log(f"  feedback: {n} part(s) get more room, {nb} blockage(s) added for the next trial")
        else:
            log(f"cypress trial {k}: gate score {score:.0f} (GPU {st['place_s']} s, legaliser moved {facts['legaliser_moved']}, "
                f"mean {facts['legaliser_mean_mm']} mm)")
        if best is None or score < best[0]:
            best = (score, final, dict(facts, trial=k, seconds=st["seconds"], place_s=st["place_s"], tool_hpwl=st["hpwl"],
                                       overflow=st["overflow"], iterations=st["iterations"], log=st["log"], macro_mode=mac,
                                       cells=len(mov), fixed_cells=len(model["cells"]) - len(mov), nets=len(model["nets"]),
                                       utilisation=round(util, 3),
                                       params=dict(over, target_density=prm["target_density"], stop_overflow=prm["stop_overflow"])))
    if best is None:
        raise Unavailable(f"no Cypress trial gave a legal placement ({why[:200]})")
    score, final, rep = best
    for r, (x, y, a) in final.items():
        GM.place(B["parts"][r], x, y, a)
    ok = [t for t in runs if "score" in t]
    rep.update({"engine": "cypress", "version": tool["commit"], "score": score, "trials": runs, "gpu_s_all_trials": round(gpu, 2),
                "flow": "intent" if intent else "feedback" if fb else "classic", "wall_s": round(time.time() - t_all, 1)})
    if aware:
        rep.update({"rejected": sum(1 for t in runs if "rejected" in t), "failed": sum(1 for t in runs if "failed" in t),
                    "zone_blockages": len(zb), "zones_left_to_legaliser": post, "feedback_blockages": len(fb_blocks),
                    "links": len(links), "escape_reserved": len(reserve), "inflated": dict(sorted(infl.items())),
                    "gate": {q: v for q, v in next(t for t in ok if t["trial"] == rep["trial"]).items()
                             if q in ("plan", "overflow", "risk_nets", "blocked", "required", "hpwl", "intent", "macros")}})
    rep["summary"] = (f"trial {rep['trial']} of {len(runs)} kept ({len(ok)} legal), score {score:.0f}, GPU {rep['gpu_s_all_trials']} s"
                      + (f", {rep['rejected']} rejected by the intent gate" if aware and rep["rejected"] else ""))
    return changed(final), rep


def format_report(rep):
    if rep.get("note"):
        return f"cypress {rep['version']}: {rep['note']}"
    g = rep.get("gate")
    return (f"engine cypress {rep['version']}: {rep['cells']} cells, {rep['nets']} nets, best of {len(rep['trials'])} trial(s) "
            f"(GPU {rep['gpu_s_all_trials']} s), gate score {rep['score']:.0f}; legaliser moved {rep['legaliser_moved']} "
            f"(max {rep['legaliser_max_mm']} mm, mean {rep['legaliser_mean_mm']} mm); overlaps {rep['overlaps']}, "
            f"outside {rep['outside']}, in keep-out {rep['in_keepout']}, fixed moved {len(rep['fixed_moved'])}"
            + (f"\n  {rep['flow']} flow: {rep['summary']}; {rep['zone_blockages']} zone blockage(s), {len(rep['zones_left_to_legaliser'])} "
               f"zone(s) left to the legaliser, {rep['links']} relation net(s), {g.get('macros', 0)} macro(s), "
               f"{len(rep['inflated'])} part(s) inflated; plan {g['plan']:.0f}, overflow {g['overflow']}, risk nets {g['risk_nets']}, "
               f"fan-out blocked {g['blocked']} of {g['required']}, HPWL {g['hpwl']} mm" if g else ""))


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
    (logged; the caller runs the built-in placer). With an `[intent]` table the hard items are seated and held first
    (intent.prepare) and the INTENT gate runs on the written board. Raises legalise.PlacementError when the result is not
    legal or the gate finds a hard intent unmet on the file."""
    from . import boardwrite as BW, routediag as RD
    wd = workdir or (os.path.splitext(out)[0] + ".cypress")
    src, run_cons, seated = board, cons, None
    try:
        locate(home, python)
        src, run_cons, seated = IT.prepare(board, cons, spec, wd, log=log)       # nothing without an [intent] table
        B = RD.load(src)
        moves, rep = place(B, run_cons, spec, wd, home, python, log, board=src)
    except Unavailable as e:
        log(f"engine cypress unavailable: {e}; falling back to the built-in placer")
        return str(e)
    BW.move_file(src, out, moves)
    rep["moved"] = len(set(moves) | set(seated.moves if seated else ()))
    if seated:
        said = []
        rc = IT.gate(out, run_cons, spec, out=said.append)
        for line in said:
            log(line)
        rep["intent"] = {"held": len(seated.held), "bodies": len(seated.bodies), "gate": said[0] if said else ""}
        if rc:
            record(out, rep)
            raise LG.PlacementError("the INTENT gate fails on the written board: " + (said[0] if said else ""))
    record(out, rep)
    return rep
