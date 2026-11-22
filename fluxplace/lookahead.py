"""Lookahead: ghost-route demand map and reserved channels (pure text, no pcbnew, no router).

A ghost route is the board routed unlocked as a throw-away. Its non-critical copper shows where the other nets run:
cells with many distinct nets are bus channels, cells next to a net's own pads are escape channels. The channels are
reserved (rule areas) before the critical pairs are laid around them. See docs/recipes/LOOKAHEAD.md.
"""
import json
import math
import os
import shutil
import time

from . import routediag as RD

DEFAULTS = {"enabled": False, "cell_mm": 1.0, "escape_mm": 3.0, "min_nets": 2, "pair_clear_mm": 2.0,
            "max_area_frac": 0.25, "ghost_budget_min": 40, "jobs": 3, "max_rounds": 4, "no_gain_rounds": 2,
            "variants": [[2, 3.0], [1, 3.0], [3, 2.0]], "res_cost": 200.0, "off_cost": 20.0, "corridor_mm": 1.0,
            "hard": False, "ghost_routers": ["krt-whole", "freerouting"], "consensus_any_layer": False}
_FLOAT = ("cell_mm", "escape_mm", "pair_clear_mm", "max_area_frac", "res_cost", "off_cost", "corridor_mm")
_INT = ("min_nets", "ghost_budget_min", "jobs", "max_rounds", "no_gain_rounds")


def params(cons):
    """[lookahead] table of constraints.toml -> the stage parameters (defaults from the recipe). Stage is off unless enabled = true."""
    e = dict((cons or {}).get("lookahead") or {})
    P = dict(DEFAULTS)
    P.update(e)
    P["enabled"] = e.get("enabled") is True
    P["hard"] = e.get("hard") is True
    P["consensus_any_layer"] = e.get("consensus_any_layer") is True
    P["ghost_routers"] = [str(x) for x in P["ghost_routers"]]
    for k in _FLOAT:
        P[k] = float(P[k])
    for k in _INT:
        P[k] = int(P[k])
    P["variants"] = [(int(a), float(b)) for a, b in P["variants"]]
    return P


def critical_pairs(board_path, cons, spec=None):
    """[(p, n)] the pairs pairs.stage lays: pairaudit.pair_list over netclass.classify."""
    from . import netclass as NC, pairaudit as PA
    return PA.pair_list(NC.classify(RD.load(board_path), cons or {}, spec))


def critical_nets(board_path, cons, spec=None):
    """Legs of every pair pairs.stage lays (critical_pairs) plus [power] rails (the same helper krtchain uses:
    power.route_widths)."""
    from . import power as PWR
    legs = {n for pr in critical_pairs(board_path, cons, spec) for n in pr}
    return legs | set(PWR.route_widths(cons or {}, track_mm=0.15))


def _cell_of(x, y, x0, y0, c):
    return int(math.floor((x - x0) / c)), int(math.floor((y - y0) / c))


def demand(board_path, critical, P):
    """Per-cell demand map of a (ghost) board -> {"cells": {(layer, ix, iy): {"nets", "crit", "escape", "pad_dist"}},
    "origin": (x0, y0), "cell": c}.  `pad_dist` = {net: distance of the cell centre to the nearest pad of that net}, kept so
    reservations() can apply any escape_mm; `escape` uses P["escape_mm"]."""
    B = RD.load(board_path)
    crit = set(critical)
    plane = {z["net"] for z in B["zones"] if z.get("net")}
    cu = list(B["copper"])
    x0, y0, _x1, _y1 = B["bbox"]
    c = float(P["cell_mm"])
    cells = {}

    def mark(layer, x, y, net):
        ix, iy = _cell_of(x, y, x0, y0, c)
        cl = cells.setdefault((layer, ix, iy), {"nets": set(), "crit": set(), "escape": False, "pad_dist": {}})
        if net in crit:
            cl["crit"].add(net)
        elif net and net not in plane:
            cl["nets"].add(net)

    def raster(layer, pts, net):
        # step cell/2 along each polyline leg; a zero-length leg marks its single cell
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            n = max(1, int(math.ceil(math.hypot(bx - ax, by - ay) / (c / 2))))
            for i in range(n + 1):
                t = i / n
                mark(layer, ax + (bx - ax) * t, ay + (by - ay) * t, net)

    for s in B["segs"]:
        net = s["net"]
        if s["layer"] not in cu or (net not in crit and (not net or net in plane)):
            continue
        raster(s["layer"], [s["a"], s["mid"], s["b"]] if s.get("mid") else [s["a"], s["b"]], net)
    for v in B["vias"]:
        idx = sorted(cu.index(l) for l in v["layers"] if l in cu) or [0, len(cu) - 1]
        for layer in cu[idx[0]:idx[-1] + 1]:
            raster(layer, [(v["x"], v["y"]), (v["x"], v["y"])], v["net"])

    pads = {}
    for p in B["pads"]:
        if p["net"]:
            pads.setdefault(p["net"], []).append((p["x"], p["y"]))
    for (layer, ix, iy), cl in cells.items():
        cx, cy = x0 + (ix + 0.5) * c, y0 + (iy + 0.5) * c
        for n in cl["nets"]:
            if n in pads:
                cl["pad_dist"][n] = min(math.hypot(cx - px, cy - py) for px, py in pads[n])
        cl["escape"] = any(d <= P["escape_mm"] for d in cl["pad_dist"].values())
    return {"cells": cells, "origin": (x0, y0), "cell": c}


def reservations(D, B, critical, P, min_nets=None, escape_mm=None):
    """Channels to reserve from a demand map -> [{"layer", "rect": (x0, y0, x1, y1), "nets", "demand"}]."""
    mn = P["min_nets"] if min_nets is None else min_nets
    em = P["escape_mm"] if escape_mm is None else escape_mm
    x0, y0 = D["origin"]
    c = D["cell"]
    crit = set(critical)
    cpads = [(p["x"], p["y"]) for p in B["pads"] if p["net"] in crit]
    clear = P["pair_clear_mm"]
    cand = []
    for (layer, ix, iy), cl in D["cells"].items():
        if cl["crit"]:
            continue
        esc = any(d <= em for d in cl["pad_dist"].values())
        if not ((esc and cl["nets"]) or len(cl["nets"]) >= mn):
            continue
        cx, cy = x0 + (ix + 0.5) * c, y0 + (iy + 0.5) * c
        if any(math.hypot(cx - px, cy - py) <= clear for px, py in cpads):
            continue
        cand.append((layer, ix, iy, cl, esc))
    cand.sort(key=lambda t: (-len(t[3]["nets"]), not t[4], t[0], t[2], t[1]))

    bx0, by0, bx1, by1 = B["bbox"]
    cap = P["max_area_frac"] * max(bx1 - bx0, 0.0) * max(by1 - by0, 0.0)
    used, by_layer = {}, {}
    for layer, ix, iy, cl, _esc in cand:
        if used.get(layer, 0.0) + c * c > cap:
            continue
        used[layer] = used.get(layer, 0.0) + c * c
        by_layer.setdefault(layer, {})[(ix, iy)] = cl

    out = []
    for layer in sorted(by_layer):
        cells = by_layer[layer]
        rows = {}
        for (ix, iy) in cells:
            rows.setdefault(iy, []).append(ix)
        runs_at = {}                                       # row -> [(first ix, last ix)] of adjacent cells
        for iy, xs in rows.items():
            xs = sorted(xs)
            s = p = xs[0]
            runs = runs_at.setdefault(iy, [])
            for ix in xs[1:]:
                if ix == p + 1:
                    p = ix
                    continue
                runs.append((s, p))
                s = p = ix
            runs.append((s, p))
        active, done = {}, []                              # equal runs in consecutive rows stack into one rectangle
        for iy in sorted(runs_at):
            nxt = {}
            for key in runs_at[iy]:
                span = active.pop(key, None)
                if span is not None and span[1] == iy - 1:
                    span[1] = iy
                    nxt[key] = span
                else:
                    if span is not None:
                        done.append((key, span))
                    nxt[key] = [iy, iy]
            done += list(active.items())
            active = nxt
        done += list(active.items())
        for (s, e), (r0, r1) in done:
            members = [cells[(ix, iy)] for iy in range(r0, r1 + 1) for ix in range(s, e + 1)]
            nets = sorted({n for cl in members for n in cl["nets"]})
            out.append({"layer": layer,
                        "rect": (x0 + s * c, y0 + r0 * c, min(x0 + (e + 1) * c, bx1), min(y0 + (r1 + 1) * c, by1)),
                        "nets": nets, "demand": sum(len(cl["nets"]) for cl in members)})
    return out


PY = "/usr/bin/python3"                 # pcbnew lives in the system interpreter; a fresh process per load (SWIG safety)
ZONE_PREFIX = "LOOKAHEAD-"


def _apply_code(board_in, board_out, rects):
    """Script text for a fresh pcbnew process: one rule-area zone per rect (no tracks, no vias; pads, pours and
    footprints allowed), named LOOKAHEAD-<i>, saved to board_out."""
    items = [(i, r["layer"], tuple(float(v) for v in r["rect"])) for i, r in enumerate(rects)]
    return "\n".join([
        "import pcbnew, os",
        f"b = pcbnew.LoadBoard({board_in!r})",
        f"items = {items!r}",
        "for i, layer, (x0, y0, x1, y1) in items:",
        "    z = pcbnew.ZONE(b)",
        "    z.SetIsRuleArea(True)",
        "    for setter, val in (('SetDoNotAllowTracks', True), ('SetDoNotAllowVias', True), ('SetDoNotAllowPads', False),",
        "                        ('SetDoNotAllowCopperPour', False), ('SetDoNotAllowFootprints', False)):",
        "        if hasattr(z, setter):",
        "            getattr(z, setter)(val)",
        "    ls = pcbnew.LSET()",
        "    ls.AddLayer(b.GetLayerID(layer))",
        "    z.SetLayerSet(ls)",
        f"    z.SetZoneName('{ZONE_PREFIX}' + str(i))",
        "    pts = pcbnew.SHAPE_LINE_CHAIN()",
        "    for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):",
        "        pts.Append(pcbnew.FromMM(px), pcbnew.FromMM(py))",
        "    pts.SetClosed(True)",
        "    z.AddPolygon(pts)",
        "    b.Add(z)",
        f"pcbnew.SaveBoard({board_out!r}, b)",
        "print('OK', len(items))",
        "os._exit(0)",
    ]) + "\n"


def _strip_code(board_in, board_out):
    """Script text: remove every zone whose name starts with LOOKAHEAD-, save to board_out."""
    return "\n".join([
        "import pcbnew, os",
        f"b = pcbnew.LoadBoard({board_in!r})",
        "n = 0",
        "for z in list(b.Zones()):",
        f"    if z.GetZoneName().startswith('{ZONE_PREFIX}'):",
        "        b.Remove(z)",
        "        n += 1",
        f"pcbnew.SaveBoard({board_out!r}, b)",
        "print('OK', n)",
        "os._exit(0)",
    ]) + "\n"


def _run(code, timeout=900):
    """Run a pcbnew script in a fresh interpreter -> its stdout's last line; raises when it did not print OK."""
    import subprocess
    rc = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=timeout)
    last = (rc.stdout.strip().splitlines() or [""])[-1]
    if rc.returncode != 0 or not last.startswith("OK"):
        raise RuntimeError(f"pcbnew step failed (rc {rc.returncode}): {(rc.stderr or rc.stdout)[-300:]}")
    return last


def apply(board_in, board_out, rects):
    """board_in + LOOKAHEAD-<i> rule areas (one per rect) -> board_out; the .kicad_pro travels with it."""
    from . import pipeline as PL
    _run(_apply_code(board_in, board_out, rects))
    PL._sidecar(board_in, board_out)
    return board_out


def strip(board_in, board_out):
    """board_in without any LOOKAHEAD-* zone -> board_out; the .kicad_pro travels with it."""
    from . import pipeline as PL
    _run(_strip_code(board_in, board_out))
    PL._sidecar(board_in, board_out)
    return board_out


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ghost(a, board, outdir, budget_min, router="krt-whole"):
    """Route a copy of `board` unlocked, throw-away: `pipeline ... --from route --to route --router <router>`
    (krt-whole or freerouting; flags as W15/scripts-route.sh, passed through from `a`). -> routed board path, or None."""
    import glob
    import shutil
    import subprocess
    from . import pipeline as PL
    os.makedirs(outdir, exist_ok=True)
    copy = os.path.join(outdir, "in.kicad_pcb")
    shutil.copy(board, copy)
    PL._sidecar(board, copy)
    cmd = [PY, os.path.join(REPO, "cli.py"), "pipeline", copy, "--workdir", os.path.join(outdir, "run")]
    for key, flag in (("constraints", "--constraints"), ("spec", "--spec")):
        if getattr(a, key, None):
            cmd += [flag, getattr(a, key)]
    cmd += ["--flow", "constructive", "--from", "route", "--to", "route", "--router", router]
    if router == "freerouting":
        for key, flag in (("freerouting_jar", "--freerouting-jar"), ("java", "--java")):
            if getattr(a, key, None):
                cmd += [flag, getattr(a, key)]
    else:
        cmd += ["--router-dir", a.router_dir, "--router-py", a.router_py]
    cmd += ["--route-budget", str(int(budget_min * 60)),
            "--assume-defaults", "--allow-incomplete", "--allow-undocumented", "--no-api"]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=budget_min * 60 + 600)
    except subprocess.TimeoutExpired:
        return None
    found = sorted(glob.glob(os.path.join(outdir, "run", "*-route", "board.kicad_pcb")))
    return found[0] if found else None


def fitness(board, critical, cons, reserved_area=0.0, escape_mm=3.0):
    """Board quality, lexicographic key (-pairs_complete, shorts, opens, sealed, drc_errors, reserved_area) (smaller is better).
    sealed = opens of non-critical nets with a pad-side unconnected item within escape_mm of locked critical copper.
    blockers = {"P/N": sealed opens next to that pair's locked copper}."""
    from . import gaps as GP, netclass as NC, pairaudit as PA, routediag as RD
    crit = set(critical)
    m = GP.measure(board)
    B = RD.load(board)
    cls = NC.classify(B, cons or {})
    pairs = PA.pair_list(cls)
    res = PA.audit(B, pairs, cons or {}, set(m.get("opens_by_net") or {}))
    complete = sum(1 for e in res.values() if e.get("status") == "complete")
    locked = [s for s in B["segs"] if s["locked"] and s["net"] in crit]
    locked += [v for v in B["vias"] if v["locked"] and v["net"] in crit]

    def near_pair_copper(x, y):
        hits = set()
        for s in locked:
            d = (RD._seg_dist(x, y, s["a"][0], s["a"][1], s["b"][0], s["b"][1]) if "a" in s
                 else math.hypot(x - s["x"], y - s["y"]))
            if d <= escape_mm:
                hits.add(s["net"])
        return hits

    sealed, blockers = 0, {}
    for it in (m.get("drc") or {}).get("unconnected_items") or []:
        nets = set(GP._nets_of([it]))
        pos = GP._pos(it)
        if pos is None or not nets or nets & crit:
            continue
        near = near_pair_copper(*pos)
        if not near:
            continue
        sealed += 1
        for p, n in pairs:
            if p in near or n in near:
                k = f"{p}/{n}"
                blockers[k] = blockers.get(k, 0) + 1
    shorts, opens, errors = int(m.get("shorts", 0)), int(m.get("opens", 0)), int(m.get("errors", 0))
    key = (-complete, shorts, opens, sealed, errors, float(reserved_area))
    unconnected = [{"net": ",".join(sorted(GP._nets_of([it]))), "pos": GP._pos(it)}
                   for it in (m.get("drc") or {}).get("unconnected_items") or []]
    return {"pairs_complete": complete, "pairs_total": len(pairs), "shorts": shorts, "opens": opens, "sealed": sealed,
            "drc_errors": errors, "reserved_area": float(reserved_area), "blockers": blockers, "key": key,
            "critical": sorted(crit), "unconnected": unconnected}


MM_PER_PX = 0.05


def render(board, rects, fit, path):
    """PNG of one candidate (matplotlib Agg, 1 px = 0.05 mm, one column per copper layer, max 4 columns).
    Grey outline; non-critical copper light and thin; critical copper saturated and bold; reserved rects hatched;
    unconnected items of the fitness dict as red stubs with the net name. Title: fit["id"] and fit["key"]."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    B = RD.load(board)
    crit = set(fit.get("critical") or ())
    x0, y0, x1, y1 = B["bbox"]
    w_mm, h_mm = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
    layers = [l for l in B["copper"] if any(s["layer"] == l for s in B["segs"]) or any(l in v["layers"] for v in B["vias"])]
    layers = layers[:4] or B["copper"][:1]
    ncols = max(1, min(4, len(layers)))
    nrows = (len(layers) + ncols - 1) // ncols
    dpi = 25.4 / MM_PER_PX                                   # 508 dpi: 1 px = 0.05 mm
    fig, axes = plt.subplots(nrows, ncols, figsize=(ncols * (w_mm / 25.4 + 0.3), nrows * (h_mm / 25.4 + 0.4)),
                             squeeze=False)
    for ax in axes.flat:
        ax.set_visible(False)
    for i, layer in enumerate(layers):
        ax = axes.flat[i]
        ax.set_visible(True)
        ax.set_aspect("equal")
        ax.set_title(layer, fontsize=8)
        for (a, b) in B["edge"] or [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]:
            ax.plot([a[0], b[0]], [a[1], b[1]], color="grey", lw=0.8)
        for s in B["segs"]:
            if s["layer"] != layer:
                continue
            is_crit = s["net"] in crit
            ax.plot([s["a"][0], s["b"][0]], [s["a"][1], s["b"][1]], color="#d62728" if is_crit else "#c8c8c8",
                    lw=1.6 if is_crit else 0.4, solid_capstyle="round", zorder=3 if is_crit else 2)
        for v in B["vias"]:
            if layer in v["layers"]:
                ax.add_patch(plt.Circle((v["x"], v["y"]), v["d"] / 2, color="#d62728" if v["net"] in crit else "#999999",
                                        zorder=4))
        for r in rects:
            if r["layer"] != layer:
                continue
            rx0, ry0, rx1, ry1 = r["rect"]
            ax.add_patch(Rectangle((rx0, ry0), rx1 - rx0, ry1 - ry0, facecolor="orange", alpha=0.18,
                                   hatch="//", edgecolor="orange", lw=0.5, zorder=1))
        for u in fit.get("unconnected") or []:
            if not u.get("pos"):
                continue
            ux, uy = u["pos"]
            ax.plot([ux, ux + 1.0], [uy, uy], color="red", lw=1.0, zorder=5)
            ax.text(ux + 1.05, uy, u.get("net", ""), color="red", fontsize=5, va="center")
        ax.set_xlim(x0 - 0.5, x1 + 0.5)
        ax.set_ylim(y1 + 0.5, y0 - 0.5)
        ax.tick_params(labelsize=5)
    key = fit.get("key")
    fig.suptitle(f"{fit.get('id', '')} {tuple(key) if key is not None else ''}", fontsize=9)
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
    return path


def _mk(p):
    os.makedirs(p, exist_ok=True)
    return p


def _progress(path, state, message="", **fields):
    from . import routestatus as RS
    st = {"stage": "lookahead", "state": state, "pid": os.getpid(), "current_step": str(message)[:240], "updated": time.time()}
    st.update(fields)
    RS._atomic(path, st)


def _vtxt(kind, variant, pair):
    mn, em = variant
    return f"{kind} min_nets={mn} esc={em:g}" + (f" {pair[0]}/{pair[1]}" if pair else "")


def _table(path, rows):
    lines = ["| round | id | variant | fitness | png | verdict |", "|---|---|---|---|---|---|"]
    for r in rows:
        fit = "-" if r["fit"] is None else str(tuple(r["fit"]))
        lines.append(f"| {r['round']} | {r['id']} | {r['variant']} | {fit} | {r['png']} | {r['verdict']} |")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def _pairs_stage(sub, n, board):
    """pairs.stage on a sub-context -> (laid board, complete, total) from its board.pairs.json report."""
    from . import pairs as PR
    r = PR.stage(sub, n, board)
    if r.get("verdict") == "fail":
        raise RuntimeError(r.get("line", "pairs stage failed"))
    rep_path = os.path.join(sub.dir(n, "pairs"), "board.pairs.json")
    rep = json.load(open(rep_path)) if os.path.exists(rep_path) else {}
    return r.get("board", board), int(rep.get("complete", 0)), int(rep.get("total", 0))


def _lay_fresh(sub, cand, board, rects, hard=True):
    """hard: apply the reserved rects as rule areas first; soft: no zones, the reservations are the pairs guide (sub.guide).
    Then lay the pairs (pairs.stage); incomplete -> strip and lay once more; strip -> locked board."""
    applied = apply(board, os.path.join(cand, "applied.kicad_pcb"), rects) if hard else board
    cur, comp, total = _pairs_stage(sub, 1, applied)
    if comp < total:
        cur, _c, _t = _pairs_stage(sub, 2, strip(cur, os.path.join(cand, "stripped.kicad_pcb")))
    return strip(cur, os.path.join(cand, "locked.kicad_pcb"))


def _lay_repair(sub, cand, board, rects, pair, cons, spec):
    """the locked board with one pair's copper unlocked and removed (pairsonly.unlock_items + pairaudit.strip_legs), the
    rects applied, and only that pair laid again (pairs.run only=)."""
    from . import pairs as PR, pairsonly as PO, pairaudit as PA
    text = open(board, errors="surrogateescape").read()
    text, _nu = PO.unlock_items(text, list(pair))
    text, _nr = PA.strip_legs(text, list(pair))
    ripped = os.path.join(cand, "ripped.kicad_pcb")
    with open(ripped, "w", errors="surrogateescape") as fh:
        fh.write(text)
    _copy_sidecar(board, ripped)
    applied = apply(ripped, os.path.join(cand, "applied.kicad_pcb"), rects)
    out = os.path.join(cand, "relaid.kicad_pcb")
    PR.run(applied, cons, spec, out, only=list(pair))
    return strip(out, os.path.join(cand, "locked.kicad_pcb"))


def _copy_sidecar(src, dst):
    from . import pipeline as PL
    PL._sidecar(src, dst)


def _verify(a, locked, cand, crit, cons, P, rects, tag):
    """ghost of the locked board (verify) -> (ghost board, fitness dict with id, png) or (None, None, None)."""
    g = ghost(a, locked, os.path.join(cand, "ghost"), P["ghost_budget_min"])
    if not g:
        return None, None, None
    area = sum((r["rect"][2] - r["rect"][0]) * (r["rect"][3] - r["rect"][1]) for r in rects)
    fit = fitness(g, crit, cons, reserved_area=area, escape_mm=P["escape_mm"])
    fit["id"] = tag
    png = render(g, rects, fit, os.path.join(cand, "ghost.png"))
    return g, fit, png


def _job(d, tag, kind, variant, base, rects, pair, acfg, crit, guide=None):
    cand = _mk(os.path.join(d, tag))
    cs = (["--constraints", acfg["constraints"]] if acfg.get("constraints") else []) + \
         (["--spec", acfg["spec"]] if acfg.get("spec") else [])
    job = {"tag": tag, "kind": kind, "variant": list(variant), "board": base, "dir": cand,
           "rects": [{"layer": r["layer"], "rect": list(r["rect"]), "nets": r["nets"], "demand": r["demand"]} for r in rects],
           "pair": list(pair) if pair else None, "a": acfg, "cs": cs, "critical": sorted(crit), "guide": guide,
           "result": os.path.join(cand, "result.json")}
    jobfile = os.path.join(_mk(os.path.join(d, "jobs")), f"{tag}.json")
    job["jobfile"] = jobfile
    with open(jobfile, "w") as fh:
        json.dump(job, fh)
    return job


def _spawn(job, timeout):
    import subprocess
    code = (f"import sys; sys.path.insert(0, {REPO!r}); "
            f"from fluxplace import lookahead as LA; LA._worker({job['jobfile']!r})")
    try:
        subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    try:
        return json.load(open(job["result"]))
    except (OSError, ValueError):
        return {"tag": job["tag"], "error": "worker produced no result"}


def _run_jobs(jobs, P, timeout):
    """candidates of one round, up to [lookahead] jobs at a time; each candidate is its own interpreter (threads only wait)."""
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=max(1, P["jobs"])) as ex:
        return list(ex.map(lambda j: _spawn(j, timeout), jobs))


def _worker(jobfile):
    """one candidate, in a fresh interpreter: lay (fresh or repair), verify by ghost, fitness, render -> result.json."""
    from types import SimpleNamespace
    from . import constraints as C
    job = json.load(open(jobfile))
    out = {"tag": job["tag"], "variant": job["variant"], "kind": job["kind"], "locked": None, "ghost": None,
           "png": None, "fit": None}
    try:
        acfg = job["a"]
        cons = C.load(acfg["constraints"]) if acfg.get("constraints") else {}
        spec = json.load(open(acfg["spec"])) if acfg.get("spec") else None
        P = params(cons)
        cand = _mk(job["dir"])
        rects = [{"layer": r["layer"], "rect": tuple(r["rect"]), "nets": r["nets"], "demand": r["demand"]}
                 for r in job["rects"]]
        sub = SimpleNamespace(cons=cons, spec=spec, dir=lambda n, name: _mk(os.path.join(cand, f"{n}-{name}")),
                              cs=lambda: list(job["cs"]), qs=lambda: [])
        if job["kind"] == "repair":
            locked = _lay_repair(sub, cand, job["board"], rects, job["pair"], cons, spec)
        else:
            if job["kind"] == "soft":
                sub.guide = make_guide(job["guide"])
            locked = _lay_fresh(sub, cand, job["board"], rects, hard=job["kind"] != "soft")
        a = SimpleNamespace(**acfg)
        g, fit, png = _verify(a, locked, cand, set(job["critical"]), cons, P, rects, job["tag"])
        out.update(locked=locked, ghost=g, png=png, fit=fit)
    except Exception as e:                                       # noqa: BLE001 - one candidate failing is a row, not a stage crash
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    with open(job["result"], "w") as fh:
        json.dump(out, fh)


def merge_demand(Ds, mode, P):
    """Mixed demand maps of two or more ghosts. consensus: cells where both ghosts used the same non-critical net (same
    layer, or any layer with consensus_any_layer); union: cells either ghost used. Same shape as demand()."""
    def cp(cl):
        return {"nets": set(cl["nets"]), "crit": set(), "pad_dist": dict(cl["pad_dist"])}

    def merged(a, b, keep=None):
        nets = (a["nets"] | b["nets"]) if keep is None else set(keep)
        pd = {}
        for n in nets:
            ds = [x["pad_dist"][n] for x in (a, b) if n in x["pad_dist"]]
            if ds:
                pd[n] = min(ds)
        return {"nets": nets, "crit": set(), "pad_dist": pd, "escape": any(d <= P["escape_mm"] for d in pd.values())}

    cells = {}
    A = Ds[0]
    if mode == "union":
        for D in Ds:
            for k, cl in D["cells"].items():
                if cl["nets"]:
                    cells[k] = merged(cells[k], cl) if k in cells else merged(cl, cl)
    elif P["consensus_any_layer"]:
        idx = {}
        for (l, ix, iy), cl in Ds[1]["cells"].items():
            if cl["nets"]:
                idx.setdefault((ix, iy), []).append((l, cl))
        for (l, ix, iy), cl in A["cells"].items():
            for _lb, clb in idx.get((ix, iy), []):
                common = cl["nets"] & clb["nets"]
                if cl["nets"] and common:
                    cells[(l, ix, iy)] = merged(cl, clb, keep=common)
    else:
        for k, cl in A["cells"].items():
            clb = Ds[1]["cells"].get(k)
            if cl["nets"] and clb and clb["nets"] & cl["nets"]:
                cells[k] = merged(cl, clb, keep=cl["nets"] & clb["nets"])
    return {"cells": cells, "origin": A["origin"], "cell": A["cell"]}


def _corr_for(src, sources, order):
    """Corridors a candidate of source `src` uses: its own ghost's, or for mixed sources the first ghost (router order) that has
    the pair."""
    if src in order:
        return sources[src]["corr"]
    out = {}
    for g in order:
        for k, v in sources[g]["corr"].items():
            out.setdefault(k, v)
    return out


def _specs(r, best, best_variant, P, ghosts, mixed):
    """(kind, (min_nets, escape_mm), pair, src) per candidate of round r. Round 1: soft variants per ghost, one hard variant
    (first ghost; every variant when hard = true), one soft candidate per mixed source (consensus, union)."""
    if r == 1:
        out = []
        for gi, g in enumerate(ghosts):
            for v in P["variants"]:
                out.append(("hard" if P["hard"] else "soft", tuple(v), None, g))
            if gi == 0 and not P["hard"]:
                out.append(("hard", tuple(P["variants"][0]), None, g))
        for m in mixed:
            out.append(("soft", tuple(P["variants"][0]), None, m))
        return out
    if best is None:
        return []
    out = []
    for name, _n in sorted(best["fit"].get("blockers", {}).items(), key=lambda kv: (-kv[1], kv[0]))[:2]:
        p, q = name.split("|")
        out.append(("repair", (1, P["escape_mm"] + 1.0), (p, q), best["src"]))
    if best_variant:
        mn, em = best_variant
        out.append(("soft", (max(mn - 1, 1), em), None, best["src"]))
    return out


def _vtxt2(src, kind, variant, pair):
    return f"{src} " + _vtxt(kind, variant, pair)


def _row(r, tag, variant, fit, png, verdict):
    return {"round": r, "id": tag, "variant": variant, "fit": tuple(fit["key"]) if fit else None,
            "png": png or "-", "verdict": verdict}


def _ghost_round0(a, board, d, P, routers):
    """all ghosts of round 0 in parallel (one router process each; threads only wait) -> {router: path or None}."""
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=max(1, min(P["jobs"], len(routers)))) as ex:
        futs = {r: ex.submit(ghost, a, board, os.path.join(d, f"ghost0-{r}"), P["ghost_budget_min"], r) for r in routers}
        return {r: f.result() for r, f in futs.items()}


def stage(c, n, board):
    """lookahead stage (constructive flow, before pairs): ghost demand per router (KRT and Freerouting both as throw-away
    ideas, plus consensus and union) -> reserved channels and soft pair guides -> pairs laid around them -> ghost verify ->
    ranked tournament (TOURNAMENT.md, one PNG per candidate). Output = the winner's locked board."""
    P = params(c.cons)
    if not P["enabled"]:
        return {"verdict": "skipped", "line": "[lookahead] enabled = false", "board": board}
    d = c.dir(n, "lookahead")
    spath, table = os.path.join(d, "progress.json"), os.path.join(d, "TOURNAMENT.md")
    a = c.a
    acfg = {k: getattr(a, k, None) for k in ("constraints", "spec", "router_dir", "router_py", "freerouting_jar", "java")}
    crit = critical_nets(board, c.cons, c.spec)
    pl = critical_pairs(board, c.cons, c.spec)
    rows, sources, ok_fit = [], {}, {}
    routers = list(P["ghost_routers"])
    _progress(spath, "running", "round 0: ghosts " + ", ".join(routers))
    gouts = _ghost_round0(a, board, d, P, routers)
    for r in routers:
        g = gouts.get(r)
        if not g:
            rows.append(_row(0, f"ghost-{r}", "unlocked", None, None, "fail: no routed board"))
            continue
        fit = fitness(g, crit, c.cons, escape_mm=P["escape_mm"])
        fit["id"] = f"ghost-{r}"
        png = render(g, [], fit, os.path.join(d, f"ghost-{r}.png"))
        B = RD.load(g)
        corr, why = corridors(B, pl, P)
        sources[r] = {"D": demand(g, crit, P), "corr": corr, "fit": fit, "B": B}
        ok_fit[r] = fit
        rows.append(_row(0, f"ghost-{r}", "unlocked", fit, png, "reference"))
    ghosts = [r for r in routers if r in sources]
    if not ghosts:
        _progress(spath, "failed", "no round-0 ghost produced a board")
        return {"verdict": "fail", "line": "lookahead: no round-0 ghost produced a routed board", "board": board}
    mixed = []
    if len(ghosts) >= 2:
        Ds = [sources[g]["D"] for g in ghosts]
        sources["consensus"] = {"D": merge_demand(Ds, "consensus", P), "corr": _corr_for("consensus", sources, ghosts)}
        sources["union"] = {"D": merge_demand(Ds, "union", P), "corr": _corr_for("union", sources, ghosts)}
        mixed = ["consensus", "union"]
    _table(table, rows)
    B0 = sources[ghosts[0]]["B"]
    ref = min(ok_fit.values(), key=lambda f: tuple(f["key"]))
    best, best_variant, stale, rounds, cands = None, None, 0, 0, 0
    for r in range(1, P["max_rounds"] + 1):
        rounds = r
        specs = _specs(r, best, best_variant, P, ghosts, mixed)
        if not specs:
            break
        jobs = []
        for i, (kind, variant, pair, src) in enumerate(specs):
            mn, em = variant
            S = sources[src]
            rects = reservations(S["D"], B0, crit, P, min_nets=mn, escape_mm=em)
            base = best["locked"] if kind == "repair" else board
            guide = ({"origin": S["D"]["origin"], "cell": S["D"]["cell"], "res": rects,
                      "corr": _corr_for(src, sources, ghosts), "res_cost": P["res_cost"], "off_cost": P["off_cost"]}
                     if kind == "soft" else None)
            jobs.append(_job(d, f"r{r}-{i}", kind, variant, base, rects, pair, acfg, crit, guide=guide))
            jobs[-1]["src"] = src
        _progress(spath, "running", f"round {r}: {len(jobs)} candidates")
        results = _run_jobs(jobs, P, P["ghost_budget_min"] * 60 + 1800)
        cands += len(results)
        okr = [x for x in results if x.get("fit")]
        rb = min(okr, key=lambda x: tuple(x["fit"]["key"]), default=None)
        improved = rb is not None and (best is None or tuple(rb["fit"]["key"]) < tuple(best["fit"]["key"]))
        for job, x in zip(jobs, results):
            vt = _vtxt2(job["src"], job["kind"], tuple(job["variant"]), job["pair"])
            if x.get("fit") is None:
                verdict = "fail: " + str(x.get("error", "no ghost"))[:80]
            else:
                verdict = "winner" if (x is rb and improved) else "kept"
            rows.append(_row(r, job["tag"], vt, x.get("fit"), x.get("png"), verdict))
        if improved:
            best = {"fit": rb["fit"], "locked": rb["locked"], "png": rb["png"], "id": rb["tag"],
                    "variant": rb["variant"], "src": next(j["src"] for j in jobs if j["tag"] == rb["tag"])}
            stale = 0
        else:
            stale += 1
        if r == 1 and best is not None:
            best_variant = tuple(best["variant"])
        _table(table, rows)
        _progress(spath, "running", f"round {r} done: best {best['id'] if best else '-'}", rounds=r, candidates=cands)
        if best is None:
            break
        done = best["fit"]["opens"] == 0 and best["fit"]["pairs_complete"] == best["fit"]["pairs_total"]
        if done or stale >= P["no_gain_rounds"]:
            break
    if best is None:
        _progress(spath, "failed", "no candidate produced a board")
        return {"verdict": "fail", "line": "lookahead: no candidate produced a routed board", "board": board,
                "next": [f"TOURNAMENT.md: {table}"]}
    out = os.path.join(d, "board.kicad_pcb")
    shutil.copy(best["locked"], out)
    _copy_sidecar(best["locked"], out)
    f = best["fit"]
    line = (f"lookahead: {rounds} rounds, {cands} candidates, winner {best['id']} ({best['src']}): "
            f"pairs {f['pairs_complete']}/{f['pairs_total']}, ghost opens {f['opens']} "
            f"(round-0 ghost opens {ref['opens']})")
    _progress(spath, "done", line, board=out, rounds=rounds, candidates=cands)
    return {"verdict": "pass", "line": line, "board": out,
            "next": [f"TOURNAMENT.md: {table}", f"winner png: {best['png']}"]}


def corridors(B, pairs, P):
    """Per critical pair, the ghost copper of both legs on the demand grid, widened by corridor_mm ->
    ({"p|n": sorted [(layer, ix, iy)]}, {"p|n": reason} for the pairs with no corridor). A pair whose legs are not both
    connected on the ghost gets no corridor."""
    from . import routeconn as RC
    c, x0, y0 = P["cell_mm"], B["bbox"][0], B["bbox"][1]
    halo = max(0, int(math.ceil(P["corridor_mm"] / c)))
    open_nets = set(RC.text_open(B))
    cu = set(B["copper"])
    out, why = {}, {}
    for p, n in pairs:
        key = "|".join(sorted((p, n)))
        if p in open_nets or n in open_nets:
            why[key] = "legs not both connected on the ghost"
            continue
        legs, cells = {p, n}, set()
        for s in B["segs"]:
            if s["net"] in legs and s["layer"] in cu:
                pts = [s["a"], s["mid"], s["b"]] if s.get("mid") else [s["a"], s["b"]]
                for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                    k = max(1, int(math.ceil(math.hypot(bx - ax, by - ay) / (c / 2))))
                    for t in range(k + 1):
                        cells.add((s["layer"],) + _cell_of(ax + (bx - ax) * t / k, ay + (by - ay) * t / k, x0, y0, c))
        for v in B["vias"]:
            if v["net"] in legs:
                for layer in v["layers"] & cu:
                    cells.add((layer,) + _cell_of(v["x"], v["y"], x0, y0, c))
        grown = {(l, ix + dx, iy + dy) for (l, ix, iy) in cells
                 for dx in range(-halo, halo + 1) for dy in range(-halo, halo + 1)}
        out[key] = sorted(grown)
    return out, why


def make_guide(G):
    """Pairs guide (pairs.py ctx["guide"]) from a dict {origin, cell, res: reserved rects, corr: {"p|n": cells}, res_cost,
    off_cost}: -> guide(pair, grid, sig) -> one cost list per signal layer over the pair's grid (nx*ny, 0..255):
    + res_cost on reserved cells, + off_cost outside the pair's own corridor (only for pairs that have one)."""
    x0, y0 = G["origin"]
    c = G["cell"]
    res_cost, off_cost = float(G.get("res_cost", 200.0)), float(G.get("off_cost", 20.0))
    corr = {k: {tuple(x) for x in v} for k, v in (G.get("corr") or {}).items()}
    rects = list(G.get("res") or [])
    cache = {}

    def guide(pr, grid, sig):
        key = ("|".join(sorted(pr)), grid.nx, grid.ny, tuple(sig))
        if key in cache:
            return cache[key]
        cs = corr.get("|".join(sorted(pr)))
        nx, ny = grid.nx, grid.ny
        out = []
        for layer in sig:
            rr = [r["rect"] for r in rects if r["layer"] == layer]
            arr = [0] * (nx * ny)
            for j in range(ny):
                for i in range(nx):
                    x, y = grid.xy(j, i)
                    v = 0.0
                    if cs is not None and (layer,) + _cell_of(x, y, x0, y0, c) not in cs:
                        v += off_cost
                    if any(a <= x <= b and d <= y <= e for a, d, b, e in rr):
                        v += res_cost
                    if v:
                        arr[j * nx + i] = min(255, int(v))
            out.append(arr)
        cache[key] = out
        return out
    return guide
