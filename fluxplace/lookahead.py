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
            "max_area_frac": 0.25, "ghost_budget_min": 40, "jobs": 4, "max_rounds": 3, "no_gain_rounds": 1,
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
    gm = dict(e.get("ghost_min") or {})
    P["ghost_min"] = {"krt": float(gm.get("krt", 8)), "fr": float(gm.get("fr", 8)), "verify": float(gm.get("verify", 10))}
    P["ghost_passes_fr"] = int(dict(e.get("ghost_passes") or {}).get("fr", 4))
    P["verify_top"] = int(e.get("verify_top", 2))
    P["global_ghost"] = e.get("global_ghost") is not False
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
        "print('OK', len(items), flush=True)",
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
        "print('OK', n, flush=True)",
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


def _adopt_code(board_in, ghost_in, board_out, nets):
    """Script text for a fresh pcbnew process: copy the tracks, arcs and vias of `nets` from the ghost board onto the
    candidate as new locked items (new uuids), net matched by name. Prints the count last."""
    return "\n".join([
        "import pcbnew, os",
        f"b = pcbnew.LoadBoard({board_in!r})",
        f"g = pcbnew.LoadBoard({ghost_in!r})",
        f"nets = set({sorted(nets)!r})",
        "n = 0",
        "for t in g.GetTracks():",
        "    nm = t.GetNetname()",
        "    if nm not in nets or b.FindNet(nm) is None:",
        "        continue",
        "    tgt = b.FindNet(nm)",
        "    cls = t.GetClass()",
        "    if cls == 'PCB_VIA':",
        "        v = pcbnew.PCB_VIA(b)",
        "        v.SetPosition(t.GetPosition())",
        "        v.SetWidth(t.GetWidth())",
        "        v.SetDrill(t.GetDrill())",
        "        v.SetViaType(t.GetViaType())",
        "        v.SetLayerPair(t.TopLayer(), t.BottomLayer())",
        "        v.SetNet(tgt)",
        "        v.SetLocked(True)",
        "        b.Add(v)",
        "    elif cls == 'PCB_ARC':",
        "        a = pcbnew.PCB_ARC(b)",
        "        a.SetStart(t.GetStart()); a.SetMid(t.GetMid()); a.SetEnd(t.GetEnd())",
        "        a.SetWidth(t.GetWidth()); a.SetLayer(t.GetLayer()); a.SetNet(tgt); a.SetLocked(True)",
        "        b.Add(a)",
        "    else:",
        "        tr = pcbnew.PCB_TRACK(b)",
        "        tr.SetStart(t.GetStart()); tr.SetEnd(t.GetEnd())",
        "        tr.SetWidth(t.GetWidth()); tr.SetLayer(t.GetLayer()); tr.SetNet(tgt); tr.SetLocked(True)",
        "        b.Add(tr)",
        "    n += 1",
        f"pcbnew.SaveBoard({board_out!r}, b)",
        "print('OK', n, flush=True)",
        "os._exit(0)",
    ]) + "\n"


def adopt(board_in, ghost_in, board_out, nets):
    """Copy the ghost's copper of `nets` onto board_in -> board_out (locked); -> number of items copied."""
    from . import pipeline as PL
    last = _run(_adopt_code(board_in, ghost_in, board_out, nets))
    PL._sidecar(board_in, board_out)
    return int(last.split()[1])


def adopt_pairs(ghost_board, pairs, cons):
    """Pairs the ghost lays complete and in tolerance (pairaudit over the ghost's text) -> [(p, n)] to adopt."""
    from . import pairaudit as PA, routeconn as RC, routediag as RD
    B = RD.load(ghost_board)
    res = PA.audit(B, pairs, cons or {}, set(RC.text_open(B)))
    return [pr for pr in pairs if res.get(pr, {}).get("status") == "complete"]


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


def ghost(a, board, outdir, budget_min, router="krt-whole", passes=None):
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
    cons_path = getattr(a, "constraints", None)
    if cons_path and os.path.exists(cons_path):   # a ghost is a sketch: stage gates off (Fable fix: the pairs gate stopped every ghost, exit 5)
        text = open(cons_path).read()
        if "[flow.gates]" not in text:
            text += "\n[flow.gates]\npairs = []\nroute = []\n"
        if passes and router == "freerouting" and "[route.freerouting]" not in text:
            text += f"\n[route.freerouting]\npasses = {int(passes)}\n"
        cons_path = os.path.join(outdir, "constraints.ghost.toml")
        with open(cons_path, "w") as fh:
            fh.write(text)
    if cons_path:
        cmd += ["--constraints", cons_path]
    if getattr(a, "spec", None):
        cmd += ["--spec", a.spec]
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
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=budget_min * 60 + 120)
        with open(os.path.join(outdir, "ghost.out"), "w") as fh:
            fh.write("\n".join(l for l in (r.stdout + r.stderr).splitlines() if "PROPERTY_ENUM" not in l)[-20000:])
    except subprocess.TimeoutExpired:
        pass                      # hard cap: whatever copper the ghost has is the ghost (partial is fine)
    found = sorted(glob.glob(os.path.join(outdir, "run", "*-route", "board.kicad_pcb")))
    return found[0] if found else None


def fitness(board, critical, cons, reserved_area=0.0, escape_mm=3.0):
    """Board quality, lexicographic key (-pairs_complete, shorts, opens, sealed, drc_errors, reserved_area) (smaller is better).
    sealed = opens of non-critical nets with a pad-side unconnected item within escape_mm of locked critical copper.
    blockers = {"P|N": {"count": sealed opens next to that pair's locked copper, "nets": those open nets}}."""
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
                e = blockers.setdefault(f"{p}|{n}", {"count": 0, "nets": []})
                e["count"] += 1
                e["nets"] = sorted(set(e["nets"]) | set(nets))
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
    PR.run(applied, cons, spec, out, only=list(pair), fallback=False)
    if not os.path.exists(out):
        raise RuntimeError(f"repair of {pair[0]}/{pair[1]}: pairs.run wrote no board")
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


def _job(d, tag, kind, variant, base, rects, pair, acfg, crit, guide=None, adopt=None, jobs=None):
    cand = _mk(os.path.join(d, tag))
    cs = (["--constraints", acfg["constraints"]] if acfg.get("constraints") else []) + \
         (["--spec", acfg["spec"]] if acfg.get("spec") else [])
    job = {"tag": tag, "kind": kind, "variant": list(variant), "board": base, "dir": cand,
           "rects": [{"layer": r["layer"], "rect": list(r["rect"]), "nets": r["nets"], "demand": r["demand"]} for r in rects],
           "pair": list(pair) if pair else None, "a": acfg, "cs": cs, "critical": sorted(crit), "guide": guide,
           "adopt": adopt, "jobs": jobs,
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
    """one candidate, in a fresh interpreter: lay (fresh or repair) with fallback off and the pool cap, tier-1 fitness (text)
    -> result.json; kind "verify": tier-2 KRT ghost of the locked board, full fitness, render."""
    import time
    from types import SimpleNamespace
    from . import constraints as C
    job = json.load(open(jobfile))
    out = {"tag": job["tag"], "variant": job["variant"], "kind": job["kind"], "locked": None, "ghost": None,
           "png": None, "fit": None, "secs": {}}
    t0 = time.time()
    try:
        acfg = job["a"]
        cons = C.load(acfg["constraints"]) if acfg.get("constraints") else {}
        spec = json.load(open(acfg["spec"])) if acfg.get("spec") else None
        P = params(cons)
        cand = _mk(job["dir"])
        crit = set(job["critical"])
        rects = [{"layer": r["layer"], "rect": tuple(r["rect"]), "nets": r["nets"], "demand": r["demand"]}
                 for r in job["rects"]]
        area = sum((r["rect"][2] - r["rect"][0]) * (r["rect"][3] - r["rect"][1]) for r in rects)
        a = SimpleNamespace(**acfg)
        if job["kind"] == "verify":
            g, fit, png = _verify_full(a, job["board"], cand, crit, cons, P, job["tag"], area, rects)
            out.update(locked=job["board"], ghost=g, png=png, fit=fit)
        else:
            cons.setdefault("pairlay", {})
            cons["pairlay"]["fallback"] = False                 # candidates: no fallback search (W14: 36-77 min)
            cons["pairlay"]["workers"] = max(1, 32 // max(1, int(job.get("jobs", 4))))
            sub = SimpleNamespace(cons=cons, spec=spec, dir=lambda n, name: _mk(os.path.join(cand, f"{n}-{name}")),
                                  cs=lambda: list(job["cs"]), qs=lambda: [])
            if job["kind"] == "repair":
                locked = _lay_repair(sub, cand, job["board"], rects, job["pair"], cons, spec)
            else:
                base = job["board"]
                if job["kind"] == "soft":
                    sub.guide = make_guide(job["guide"])
                    for k, ad in enumerate(job.get("adopt") or []):
                        nxt = os.path.join(cand, f"adopt-{k}.kicad_pcb")
                        adopt(base, ad["board"], nxt, [n for pr in ad["pairs"] for n in pr])
                        base = nxt
                    out["adopted"] = sum(len(ad["pairs"]) for ad in job.get("adopt") or [])
                locked = _lay_fresh(sub, cand, base, rects, hard=job["kind"] != "soft")
            out["secs"]["lay"] = round(time.time() - t0, 1)
            fast = fitness_fast(locked, crit, cons, reserved_area=area, escape_mm=P["escape_mm"])
            out.update(locked=locked, fit=fast)
            out["secs"]["tier1"] = round(time.time() - t0, 1)
    except Exception as e:                                       # noqa: BLE001 - one candidate failing is a row, not a stage crash
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    with open(job["result"], "w") as fh:
        json.dump(out, fh)


def _verify_full(a, locked, cand, crit, cons, P, tag, area, rects):
    """tier 2: KRT verify ghost of the locked board (ghost_min.verify) -> (ghost board, full fitness with opens, png)."""
    g = ghost(a, locked, os.path.join(cand, "verify"), P["ghost_min"]["verify"], "krt-whole")
    if not g:
        return None, None, None
    fit = fitness(g, crit, cons, reserved_area=area, escape_mm=P["escape_mm"])
    fit["id"] = tag
    png = render(g, rects, fit, os.path.join(cand, "verify.png"))
    return g, fit, png


def merge_demand(Ds, mode, P):
    """Mixed demand maps of two or more ghosts. consensus: cells where both ghosts used the same non-critical net (same
    layer, or any layer with consensus_any_layer); union: cells either ghost used. Same shape as demand()."""
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


def global_demand(board, cons, spec, P, crit):
    """The coarse PathFinder ghost (planroute.run, text only, seconds): its per-cell routes give the demand map directly.
    -> demand map of the same shape as demand(); no corridors (no board copper to take them from)."""
    from . import planroute as PRR, routediag as RD
    B = RD.load(board)
    R = PRR.run(B, cons or {}, spec, ignore_routing=False)
    gr = R["gr"]
    N, c = gr.N, P["cell_mm"]
    cu = set(B["copper"])
    cells = {}
    for name, rt in gr.routes.items():
        if not name or name in crit:
            continue
        for cn in rt.get("conns") or []:
            for nd in cn.get("path") or []:
                layer = gr.layers[nd // N]
                if layer not in cu:
                    continue
                gx, gy = gr.xy_of(nd % N)
                ix, iy = _cell_of(gx, gy, B["bbox"][0], B["bbox"][1], c)
                cl = cells.setdefault((layer, ix, iy), {"nets": set(), "crit": set(), "escape": False, "pad_dist": {}})
                cl["nets"].add(name)
    pads = {}
    for pd in B["pads"]:
        if pd["net"]:
            pads.setdefault(pd["net"], []).append((pd["x"], pd["y"]))
    for (layer, ix, iy), cl in cells.items():
        cx, cy = B["bbox"][0] + (ix + 0.5) * c, B["bbox"][1] + (iy + 0.5) * c
        for n in cl["nets"]:
            if n in pads:
                cl["pad_dist"][n] = min(math.hypot(cx - px, cy - py) for px, py in pads[n])
        cl["escape"] = any(d <= P["escape_mm"] for d in cl["pad_dist"].values())
    return {"cells": cells, "origin": (B["bbox"][0], B["bbox"][1]), "cell": c}


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


def fitness_fast(board, critical, cons, reserved_area=0.0, escape_mm=3.0):
    """Tier 1, text only (no DRC, no router): pairs complete (pairaudit over routeconn open nets), sealed = PLAN_PIN_SEALED
    pins of non-critical nets, overflow = planroute overflow of the non-critical nets around the locked copper.
    key = (-pairs_complete, shorts, sealed, overflow, reserved_area); shorts are 0 here (tier 2 measures them)."""
    import re
    from . import netclass as NC, pairaudit as PA, planroute as PRR, routeconn as RC, routediag as RD
    crit = set(critical)
    cons = cons or {}
    B = RD.load(board)
    cls = NC.classify(B, cons)
    pairs = PA.pair_list(cls)
    res = PA.audit(B, pairs, cons, set(RC.text_open(B)))
    complete = sum(1 for e in res.values() if e.get("status") == "complete")
    R = PRR.run(B, cons, None, ignore_routing=False, cls=cls)
    sealed, blockers = 0, {}
    for f in R["findings"]:
        if f.get("code") != "PLAN_PIN_SEALED" or f.get("net") in crit:
            continue
        sealed += 1
        near = set(re.findall(r"(?:track|via|pad) (\S+)", " ".join(str(x) for x in f.get("blockers") or [])))
        for p, q in pairs:
            if p in near or q in near:
                e = blockers.setdefault(f"{p}|{q}", {"count": 0, "nets": []})
                e["count"] += 1
                if f.get("net"):
                    e["nets"] = sorted(set(e["nets"]) | {f["net"]})
    overflow = float(R["comp"].get("overflow", 0.0))
    key = (-complete, 0, sealed, overflow, float(reserved_area))
    return {"pairs_complete": complete, "pairs_total": len(pairs), "shorts": 0, "opens": None, "sealed": sealed,
            "overflow": overflow, "drc_errors": None, "reserved_area": float(reserved_area), "blockers": blockers,
            "key": key, "critical": sorted(crit), "unconnected": []}


def restrict_demand(D, nets):
    """D with each cell's nets cut to `nets` (cells left empty are dropped): the channels of those nets only."""
    nets = set(nets)
    cells = {}
    for k, cl in D["cells"].items():
        keep = cl["nets"] & nets
        if keep:
            cells[k] = {"nets": keep, "crit": set(cl["crit"]), "escape": cl["escape"],
                        "pad_dist": {n: d for n, d in cl["pad_dist"].items() if n in keep}}
    return {"cells": cells, "origin": D["origin"], "cell": D["cell"]}


def _specs(r, best, best_variant, P, ghosts, mixed, globals_=()):
    """(kind, (min_nets, escape_mm), pair, src) per candidate of round r. Round 1: soft variants per router ghost, one hard
    variant (first ghost; every variant when hard = true), one soft per global / mixed source. Later rounds: repairs of the
    winner's top blocker pairs and the best variant with min_nets - 1, both on the winner's source."""
    if r == 1:
        out = []
        for gi, g in enumerate(ghosts):
            for v in P["variants"]:
                out.append(("hard" if P["hard"] else "soft", tuple(v), None, g))
            if gi == 0 and not P["hard"]:
                out.append(("hard", tuple(P["variants"][0]), None, g))
        for m in list(globals_) + list(mixed):
            out.append(("soft", tuple(P["variants"][0]), None, m))
        return out
    if best is None:
        return []
    out = []
    for name, _e in sorted(best["fit"].get("blockers", {}).items(), key=lambda kv: (-kv[1]["count"], kv[0]))[:2]:
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
    """the router ghosts of round 0 in parallel (one router process each; threads only wait) -> {router: path or None}."""
    import concurrent.futures as cf

    def one(r):
        if r == "freerouting":
            return ghost(a, board, os.path.join(d, "ghost0-freerouting"), P["ghost_min"]["fr"], r, passes=P["ghost_passes_fr"])
        return ghost(a, board, os.path.join(d, f"ghost0-{r}"), P["ghost_min"]["krt"], r)
    with cf.ThreadPoolExecutor(max_workers=max(1, min(P["jobs"], len(routers)))) as ex:
        futs = {r: ex.submit(one, r) for r in routers}
        return {r: f.result() for r, f in futs.items()}


def stage(c, n, board):
    """lookahead stage (constructive flow, before pairs). Round 0: the KRT and Freerouting ghosts (capped, in parallel) and the
    global ghost (seconds) are three idea sources; consensus and union are mixed sources. Rounds: soft-guided and hard
    candidates per source, tier-1 text fitness for all, KRT verify for the top verify_top; ranked, TOURNAMENT.md, PNG each."""
    import time
    P = params(c.cons)
    if not P["enabled"]:
        return {"verdict": "skipped", "line": "[lookahead] enabled = false", "board": board}
    t_stage = time.time()
    d = c.dir(n, "lookahead")
    spath, table = os.path.join(d, "progress.json"), os.path.join(d, "TOURNAMENT.md")
    a = c.a
    acfg = {k: getattr(a, k, None) for k in ("constraints", "spec", "router_dir", "router_py", "freerouting_jar", "java")}
    crit = critical_nets(board, c.cons, c.spec)
    pl = critical_pairs(board, c.cons, c.spec)
    rows, sources, ok_fit, timing = [], {}, {}, {}
    stop = "no round ran"
    routers = list(P["ghost_routers"])
    _progress(spath, "running", "round 0: ghosts " + ", ".join(routers), timing=timing)
    t0 = time.time()
    gouts = _ghost_round0(a, board, d, P, routers)
    timing["router_ghosts_s"] = round(time.time() - t0, 1)
    for r in routers:
        g = gouts.get(r)
        if not g:
            rows.append(_row(0, f"ghost-{r}", "unlocked", None, None, "fail: no routed board"))
            continue
        t1 = time.time()
        fit = fitness(g, crit, c.cons, escape_mm=P["escape_mm"])
        fit["id"] = f"ghost-{r}"
        png = render(g, [], fit, os.path.join(d, f"ghost-{r}.png"))
        B = RD.load(g)
        corr, _why = corridors(B, pl, P)
        sources[r] = {"D": demand(g, crit, P), "corr": corr, "B": B, "path": g}
        ok_fit[r] = fit
        timing[f"ghost-{r}_s"] = round(time.time() - t0, 1)
        rows.append(_row(0, f"ghost-{r}", "unlocked", fit, png, "reference"))
    ghosts = [r for r in routers if r in sources]
    globals_ = []
    if P["global_ghost"]:
        t1 = time.time()
        try:
            sources["global"] = {"D": global_demand(board, c.cons, c.spec, P, crit), "corr": {}}
            globals_ = ["global"]
            rows.append(_row(0, "ghost-global", "planroute, demand only", None, None, "reference (no board)"))
        except Exception as e:                                   # noqa: BLE001 - the global ghost is an idea, not a must
            rows.append(_row(0, "ghost-global", "planroute", None, None, f"fail: {type(e).__name__}"))
        timing["ghost-global_s"] = round(time.time() - t1, 1)
    if not ghosts and not globals_:
        _progress(spath, "failed", "no round-0 ghost produced a board", timing=timing)
        return {"verdict": "fail", "line": "lookahead: no round-0 ghost produced a routed board", "board": board}
    if not ghosts:
        B0 = RD.load(board)
    else:
        B0 = sources[ghosts[0]]["B"]
    mixed = []
    if len(ghosts) >= 2:
        Ds = [sources[g]["D"] for g in ghosts]
        sources["consensus"] = {"D": merge_demand(Ds, "consensus", P), "corr": _corr_for("consensus", sources, ghosts)}
        sources["union"] = {"D": merge_demand(Ds, "union", P), "corr": _corr_for("union", sources, ghosts)}
        mixed = ["consensus", "union"]
    order = ghosts + globals_
    adopt_from = {}                                   # pair -> ghost board it is copied from (router order, first wins)
    for g in ghosts:
        for pr in adopt_pairs(sources[g]["path"], pl, c.cons):
            adopt_from.setdefault(tuple(pr), g)
    adopt_lists = {}
    for pr, g in adopt_from.items():
        adopt_lists.setdefault(sources[g]["path"], []).append(list(pr))
    adopt_list = [{"board": b, "pairs": prs} for b, prs in sorted(adopt_lists.items())]
    _table(table, rows)
    ref = min(ok_fit.values(), key=lambda f: tuple(f["key"])) if ok_fit else None
    best, best_variant, stale, rounds, cands = None, None, 0, 0, 0
    for r in range(1, P["max_rounds"] + 1):
        rounds = r
        specs = _specs(r, best, best_variant, P, ghosts, mixed, globals_)
        if not specs:
            break
        t_round = time.time()
        jobs = []
        for i, (kind, variant, pair, src) in enumerate(specs):
            mn, em = variant
            S = sources[src]
            DD = S["D"]
            if kind == "repair":
                DD = restrict_demand(S["D"], best["fit"]["blockers"]["|".join(pair)]["nets"])
            rects = reservations(DD, B0, crit, P, min_nets=mn, escape_mm=em)
            base = best["locked"] if kind == "repair" else board
            guide = ({"origin": S["D"]["origin"], "cell": S["D"]["cell"], "res": rects,
                      "corr": _corr_for(src, sources, order), "res_cost": P["res_cost"], "off_cost": P["off_cost"]}
                     if kind == "soft" else None)
            job = _job(d, f"r{r}-{i}", kind, variant, base, rects, pair, acfg, crit, guide=guide,
                       adopt=adopt_list if kind == "soft" else None, jobs=P["jobs"])
            job["src"] = src
            jobs.append(job)
        _progress(spath, "running", f"round {r}: {len(jobs)} candidates (tier 1)", timing=timing)
        results = _run_jobs(jobs, P, P["ghost_min"]["krt"] * 60 + 1800)
        cands += len(results)
        t_verify = time.time()
        okc = [(j, x) for j, x in zip(jobs, results) if x.get("fit")]
        okc.sort(key=lambda jx: tuple(jx[1]["fit"]["key"]))
        top = okc[:P["verify_top"]]
        vjobs = []
        for j, x in top:
            vj = _job(d, f"{j['tag']}-v", "verify", tuple(j["variant"]), x["locked"], [], None, acfg, crit)
            vj["board"], vj["src"] = x["locked"], j["src"]
            vjobs.append(vj)
        verified = _run_jobs(vjobs, P, P["ghost_min"]["verify"] * 60 + 300) if vjobs else []
        timing[f"{r}_verify_s"] = round(time.time() - t_verify, 1)
        by_tag = {}
        for (j, x), v in zip(top, verified):
            by_tag[j["tag"]] = (x, v)
        vrank = []
        for tag, (x, v) in by_tag.items():
            if v.get("fit"):
                vrank.append((tuple(x["fit"]["key"]) + (v["fit"]["opens"],), tag, x, v))
        vrank.sort(key=lambda t: t[0])
        rb = vrank[0] if vrank else None
        improved = rb is not None and (best is None or rb[0] < best["rank"])
        for job, x in zip(jobs, results):
            vt = _vtxt2(job["src"], job["kind"], tuple(job["variant"]), job["pair"])
            if x.get("fit") is not None and "adopted" in x:
                vt += f" adopt {x['adopted']}"
            if x.get("fit") is None:
                rows.append(_row(r, job["tag"], vt, None, None, "fail: " + str(x.get("error", "no lay"))[:80]))
                continue
            vv = by_tag.get(job["tag"], (None, None))[1]
            if vv is None:
                verdict = "tier1 only"
            elif vv.get("fit") is None:
                verdict = "verify failed"
            else:
                verdict = ("winner" if (rb and rb[1] == job["tag"] and improved) else "kept") + f" (opens {vv['fit']['opens']})"
            rows.append(_row(r, job["tag"], vt, x["fit"], (vv or {}).get("png"), verdict))
        if improved:
            _tag, x, v = rb[1], rb[2], rb[3]
            src = next(j["src"] for j in jobs if j["tag"] == _tag)
            best = {"fit": v["fit"], "locked": x["locked"], "png": v.get("png"), "id": _tag,
                    "variant": x["variant"], "src": src, "rank": rb[0]}
            stale = 0
        else:
            stale += 1
        if r == 1 and best is not None:
            best_variant = tuple(best["variant"])
        timing[f"round{r}_s"] = round(time.time() - t_round, 1)
        _table(table, rows)
        _progress(spath, "running", f"round {r} done: best {best['id'] if best else '-'}", rounds=r, candidates=cands,
                  timing=timing)
        if best is None:
            stop = f"round {r}: no candidate passed tier 1 and verify; nothing to keep"
            break
        done = best["fit"]["opens"] == 0 and best["fit"]["pairs_complete"] == best["fit"]["pairs_total"]
        if done or stale >= P["no_gain_rounds"]:
            stop = "clean winner" if done else f"no gain for {stale} round(s)"
            break
        stop = "max_rounds"
    timing["stage_s"] = round(time.time() - t_stage, 1)
    if best is None:
        _progress(spath, "failed", "no candidate produced a board", timing=timing, reason=stop)
        return {"verdict": "fail", "line": "lookahead: no candidate produced a routed board", "board": board,
                "next": [f"TOURNAMENT.md: {table}"]}
    out = os.path.join(d, "board.kicad_pcb")
    shutil.copy(best["locked"], out)
    _copy_sidecar(best["locked"], out)
    f = best["fit"]
    line = (f"lookahead: {rounds} rounds, {cands} candidates, winner {best['id']} ({best['src']}): "
            f"pairs {f['pairs_complete']}/{f['pairs_total']}, ghost opens {f['opens']} "
            f"(round-0 ghost opens {ref['opens'] if ref else '-'}), {timing['stage_s']} s")
    _progress(spath, "done", line, board=out, rounds=rounds, candidates=cands, timing=timing, reason=stop)
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


def _span(lo, hi, g0, st, n):
    """Grid indices [i0, i1) whose cell centre (g0 + (k + 0.5) * st) lies in [lo, hi), clamped to 0..n."""
    import math as _m
    i0 = _m.ceil((lo - g0) / st - 0.5)
    i1 = _m.ceil((hi - g0) / st - 0.5)
    return max(0, min(n, i0)), max(0, min(n, i1))


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
        # numpy rasterisation (Fable fix: the per-cell Python loop over a 0.1 mm whole-board grid x rects took hours)
        import numpy as np
        key = ("|".join(sorted(pr)), grid.nx, grid.ny, tuple(sig))
        if key in cache:
            return cache[key]
        cs = corr.get("|".join(sorted(pr)))
        F = grid.F
        nx, ny, st, gx0, gy0 = F.nx, F.ny, F.step, F.x0, F.y0
        out = []
        for layer in sig:
            v = np.zeros((ny, nx), dtype=np.float32)
            if cs is not None:
                own = np.zeros((ny, nx), dtype=bool)
                for (l, ix, iy) in cs:
                    if l != layer:
                        continue
                    i0, i1 = _span(x0 + ix * c, x0 + (ix + 1) * c, gx0, st, nx)
                    j0, j1 = _span(y0 + iy * c, y0 + (iy + 1) * c, gy0, st, ny)
                    own[j0:j1, i0:i1] = True
                v[~own] += off_cost
            for r in rects:
                if r["layer"] != layer:
                    continue
                a_, d_, b_, e_ = r["rect"]
                i0, i1 = _span(a_, b_, gx0, st, nx)
                j0, j1 = _span(d_, e_, gy0, st, ny)
                v[j0:j1, i0:i1] += res_cost
            out.append(np.minimum(v, 255).astype(np.uint8).ravel().tobytes())
        cache[key] = out
        return out
    return guide
