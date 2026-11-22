"""Lookahead: ghost-route demand map and reserved channels (pure text, no pcbnew, no router).

A ghost route is the board routed unlocked as a throw-away. Its non-critical copper shows where the other nets run:
cells with many distinct nets are bus channels, cells next to a net's own pads are escape channels. The channels are
reserved (rule areas) before the critical pairs are laid around them. See docs/recipes/LOOKAHEAD.md.
"""
import math
import os

from . import routediag as RD

DEFAULTS = {"enabled": False, "cell_mm": 1.0, "escape_mm": 3.0, "min_nets": 2, "pair_clear_mm": 2.0,
            "max_area_frac": 0.25, "ghost_budget_min": 40, "jobs": 3, "max_rounds": 4, "no_gain_rounds": 2,
            "variants": [[2, 3.0], [1, 3.0], [3, 2.0]]}
_FLOAT = ("cell_mm", "escape_mm", "pair_clear_mm", "max_area_frac")
_INT = ("min_nets", "ghost_budget_min", "jobs", "max_rounds", "no_gain_rounds")


def params(cons):
    """[lookahead] table of constraints.toml -> the stage parameters (defaults from the recipe). Stage is off unless enabled = true."""
    e = dict((cons or {}).get("lookahead") or {})
    P = dict(DEFAULTS)
    P.update(e)
    P["enabled"] = e.get("enabled") is True
    for k in _FLOAT:
        P[k] = float(P[k])
    for k in _INT:
        P[k] = int(P[k])
    P["variants"] = [(int(a), float(b)) for a, b in P["variants"]]
    return P


def critical_nets(board_path, cons, spec=None):
    """Legs of every pair pairs.stage lays (the same list: pairaudit.pair_list over netclass.classify) plus [power] rails
    (the same helper krtchain uses: power.route_widths)."""
    from . import netclass as NC, pairaudit as PA, power as PWR
    B = RD.load(board_path)
    cls = NC.classify(B, cons or {}, spec)
    legs = {n for pr in PA.pair_list(cls) for n in pr}
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


def ghost(a, board, outdir, budget_min):
    """Route a copy of `board` unlocked, throw-away: `pipeline ... --from route --to route --router krt-whole`
    (flags as W15/scripts-route.sh, passed through from `a`). -> path of the routed board, or None."""
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
    cmd += ["--flow", "constructive", "--from", "route", "--to", "route", "--router", "krt-whole",
            "--router-dir", a.router_dir, "--router-py", a.router_py, "--route-budget", str(int(budget_min * 60)),
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
    return {"pairs_complete": complete, "pairs_total": len(pairs), "shorts": shorts, "opens": opens, "sealed": sealed,
            "drc_errors": errors, "reserved_area": float(reserved_area), "blockers": blockers, "key": key}
