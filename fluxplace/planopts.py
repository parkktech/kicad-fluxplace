"""plan-options: when plan-route fails, find the SMALLEST change that makes it pass and rank the options for the owner.

Never touches the project board or constraints: every option writes <outdir>/<name>.kicad_pcb, .fluxmap.json and .constraints.toml.

Search (docs/commands/plan-options.md): baseline `place --objective plan` in the current outline; if that passes, "fits as is".
Otherwise, in parallel worker processes, each option = scratch outline/rules -> short `--objective plan` re-place -> plan-route:
  growth per edge (probe at --max-grow, then bisection in 1 mm steps), then pairs of the two nearest-miss edges if no single edge passes;
  local neck-down around the fine-pitch fan-outs, global 0.127, sub-floor 0.10 (labelled, never recommended), smaller via, via-in-pad,
  layer roles (dedicated +3V3 plane, direction swap, 8 layers); combinations of at most two small relaxations.
Rules, not advice: a relaxation thins only low-speed single-ended nets. Controlled-impedance nets (pair/rf/clock classes, ETH_/USB/PCIE/audio
names, `[nets.X] impedance`) and rails keep their designed width in the capacity model (globalroute.mult); an option whose neck-down region
overflows on such a net is invalid. Rank: valid and performance-neutral first, then smallest area, cheapest relative cost, most margin.
"""
import concurrent.futures as cf
import copy
import fnmatch
import json
import math
import multiprocessing
import os
import re
import time

from . import boardwrite as BW
from . import boardfile as BF
from . import escapeplan as EP
from . import globalroute as GR
from . import netclass as NC

COST = {"per_cm2": 1.0, "layer_pair": 0.40, "track_tight": 0.08, "track_subfloor": 0.30, "via_small": 0.06, "via_in_pad": 0.20,
        "neck_local": 0.04}
BASE_TRACK = 0.15
FLOOR = 0.127
EDGES = ("L", "R", "T", "B")


def cost_model(cons):
    c = dict(COST)
    c.update({k: float(v) for k, v in (((cons.get("plan") or {}).get("cost")) or {}).items() if k in COST})
    return c


# ------------------------------------------------------------------ board growth (text edit)
_PT = re.compile(r"\((start|end|mid|center|xy)\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\)")


def _shift(node, fx, fy):
    def f(m):
        x, y = float(m.group(2)), float(m.group(3))
        return f"({m.group(1)} {BW._num(fx(x))} {BW._num(fy(y))})"
    return _PT.sub(f, node)


def edge_parts(B, cons=None):
    """{ref: (left, right, top, bottom)}: the parts that travel with an edge when it grows.  Explicit list first: `[plan] edge_parts = [refs]`
    (each goes with its nearest edge, however far it is set back); plus the edge-mounted connectors of legality.edge_mounted; plus, as a
    heuristic fallback, parts whose courtyard is within `[plan] edge_margin_mm` (1.5) of an edge."""
    from . import legality as LGL
    pl = (cons or {}).get("plan") or {}
    margin = float(pl.get("edge_margin_mm", 1.5))
    x0, y0, x1, y1 = B["bbox"]
    out = {}
    explicit = set(pl.get("edge_parts", [])) | set(LGL.edge_mounted(B, cons or {}))
    for ref, p in B["parts"].items():
        c = p["court"]
        d = (c[0] - x0, x1 - c[2], c[1] - y0, y1 - c[3])
        if ref in explicit:
            k = min(range(4), key=lambda i: d[i])
            out[ref] = tuple(i == k for i in range(4))
            continue
        e = tuple(v <= margin for v in d)
        if any(e) and not (e[0] and e[1]) and not (e[2] and e[3]):
            out[ref] = e
    return out


def _outer_loop_items(text, B, nodes):
    """Edge.Cuts items that belong to a loop touching the outline's bounding box (the outer outline); interior loops (cut-outs) are not."""
    pts = {i: [(round(float(a), 2), round(float(b), 2)) for _k, a, b in _PT.findall(text[s:e])] for i, (s, e) in enumerate(nodes)}
    par = list(range(len(nodes)))

    def find(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i
    where = {}
    for i, ps in pts.items():
        for q in ps:
            j = where.setdefault(q, i)
            par[find(i)] = find(j)
    x0, y0, x1, y1 = B["bbox"]
    outer = set()
    for i, ps in pts.items():
        if any(abs(x - x0) < 0.02 or abs(x - x1) < 0.02 or abs(y - y0) < 0.02 or abs(y - y1) < 0.02 for x, y in ps):
            outer.add(find(i))
    return {i for i in pts if find(i) in outer}


def grow_text(text, B, grow, cons=None):
    """Move the OUTER outline's left/right/top/bottom edges out by grow = (l, r, t, b) mm.  Only Edge.Cuts items of the loop that touches the
    bounding box move (half-plane rule for its points); interior cut-outs stay where they are.  Full-board pours follow the same rule; the parts of
    `edge_parts` (explicit list, edge-mounted connectors, margin heuristic) travel with their edge together with their own keep-outs and small
    keep-outs hugging an edge; everything else stays."""
    gl, gr_, gt, gb = grow
    x0, y0, x1, y1 = B["bbox"]
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half_x = lambda x: x - gl if x < cx else x + gr_
    half_y = lambda y: y - gt if y < cy else y + gb
    root_end = BW._end(text, text.index("("))
    edits = []
    ep = edge_parts(B, cons)
    kids = BW._children(text, 0, root_end)
    edge_nodes = [(s, e) for s, e in kids if text[s + 1:s + 12].split()[0].rstrip(")").startswith("gr_") and '"Edge.Cuts"' in text[s:e]]
    outer = _outer_loop_items(text, B, edge_nodes)
    for i, (s, e) in enumerate(edge_nodes):
        if i in outer:
            edits.append((s, e, _shift(text[s:e], half_x, half_y)))
    for s, e in kids:
        tag = text[s + 1:s + 12].split()[0].rstrip(")")
        node = text[s:e]
        if tag == "zone":
            pts = [(float(a), float(b)) for _k, a, b in _PT.findall(node)]
            if not pts:
                continue
            w = max(p[0] for p in pts) - min(p[0] for p in pts)
            h = max(p[1] for p in pts) - min(p[1] for p in pts)
            if w * h >= 0.6 * (x1 - x0) * (y1 - y0):
                edits.append((s, e, _shift(node, half_x, half_y)))
            else:
                dx = -gl if min(p[0] for p in pts) <= x0 + 2 else (gr_ if max(p[0] for p in pts) >= x1 - 2 else 0.0)
                dy = -gt if min(p[1] for p in pts) <= y0 + 2 else (gb if max(p[1] for p in pts) >= y1 - 2 else 0.0)
                if dx or dy:
                    edits.append((s, e, _shift(node, lambda x: x + dx, lambda y: y + dy)))
        elif tag == "footprint":
            m = BW._REF.search(text, s, e)
            if not m or m.group(1) not in ep:
                continue
            l, r, t, b = ep[m.group(1)]
            dx = (-gl if l else 0.0) + (gr_ if r else 0.0)
            dy = (-gt if t else 0.0) + (gb if b else 0.0)
            if not (dx or dy):
                continue
            for cs, ce in BW._children(text, s, e):
                ctag = text[cs + 1:cs + 12].split()[0].rstrip(")")
                if ctag == "at" and text[cs + 3].isspace():
                    a = BW._AT.match(text, cs)
                    edits.append((cs, ce, f"(at {BW._num(float(a.group(1)) + dx)} {BW._num(float(a.group(2)) + dy)}"
                                          + (f" {a.group(3)})" if a.group(3) else ")")))
                elif ctag == "zone":
                    edits.append((cs, ce, _shift(text[cs:ce], lambda x: x + dx, lambda y: y + dy)))
    for s, e, new in sorted(edits, reverse=True):
        text = text[:s] + new + text[e:]
    return text


def board_size(path):
    from . import routediag as RD
    B = RD.load(path)
    x0, y0, x1, y1 = B["bbox"]
    return round(x1 - x0, 2), round(y1 - y0, 2)


# ------------------------------------------------------------------ rules of an option
def scratch_cons(cons, rules, base_track=BASE_TRACK):
    """Constraints dict for an option: the standard class (0.15 / 0.15) unless the option changes it."""
    c = copy.deepcopy(cons)
    pl = c.setdefault("plan", {})
    if subfloor(rules, float(pl.get("floor_mm", FLOOR))):
        pl["allow_subfloor"] = True
    pl.setdefault("track_mm", base_track)
    pl.setdefault("clearance_mm", base_track)
    pl.setdefault("design_track_mm", pl["track_mm"])
    pl.setdefault("design_clearance_mm", pl["clearance_mm"])
    for k in ("track_mm", "clearance_mm", "via_mm", "drill_mm"):
        if k in rules:
            pl[k] = rules[k]
    if "neck" in rules:
        pl["neck"] = rules["neck"]
    for k in ("via_in_pad", "variant", "layer_dir"):
        if k in rules:
            pl[k] = rules[k]
    return c


def toml_text(rules):
    """The rule changes of an option as a `[plan]` fragment to append to a constraints file (documentation of the option)."""
    L = ["[plan]   # plan-options scratch rules"]
    for k in ("track_mm", "clearance_mm", "via_mm", "drill_mm"):
        if k in rules:
            L.append(f"{k} = {rules[k]}")
    for k in ("variant",):
        if k in rules:
            L.append(f'{k} = "{rules[k]}"')
    if "via_in_pad" in rules:
        L.append("via_in_pad = [" + ", ".join(f'"{g}"' for g in rules["via_in_pad"]) + "]")
    for name, n in sorted(rules.get("neck", {}).items()):
        L.append(f"[plan.neck.{name}]")
        L.append("refs = [" + ", ".join(f'"{r}"' for r in n["refs"]) + "]")
        L.append(f"track_mm = {n['track_mm']}")
        L.append(f"clearance_mm = {n['clearance_mm']}")
    return "\n".join(L) + "\n"


def subfloor(rules, floor=FLOOR):
    """Any track/clearance (global or neck-down) below the owner's floor (`[plan] floor_mm`, default 0.127)."""
    vals = [rules.get("track_mm"), rules.get("clearance_mm")] + [v for n in rules.get("neck", {}).values() for v in (n["track_mm"], n["clearance_mm"])]
    return any(v is not None and v < floor - 1e-9 for v in vals)


def relative_cost(area_cm2, rules, layers, cons, base_layers=4):
    c = cost_model(cons)
    pen = 0.0
    if layers > base_layers:
        pen += c["layer_pair"] * (layers - base_layers) / 2.0
    if subfloor(rules, float(((cons.get("plan") or {}).get("floor_mm", FLOOR)))):
        pen += c["track_subfloor"]
    elif rules.get("track_mm") is not None and rules["track_mm"] < BASE_TRACK - 1e-9:
        pen += c["track_tight"]
    if rules.get("neck"):
        pen += c["neck_local"] if not subfloor({"neck": rules["neck"]}) else c["track_subfloor"]
    if rules.get("via_mm") is not None and rules["via_mm"] < 0.45 - 1e-9:
        pen += c["via_small"]
    if rules.get("via_in_pad"):
        pen += c["via_in_pad"]
    return area_cm2 * c["per_cm2"] * (1.0 + pen), pen


# ------------------------------------------------------------------ one option (worker)
def passes(R):
    """PASS = the single plan predicate (planroute.predicate): no sealed/oversubscribed pin, no missing via site, nothing unreachable, no
    hard-class over-budget net, no rail without plane, and the at-risk nets within `max_risk_frac` and `max_risk_nets`."""
    from . import planroute as PL
    return not PL.predicate(R)[0]


def controlled_metrics(R):
    """{net: (planned length mm, vias, ratsnest MST mm)} for EVERY controlled net, both legs of a pair included."""
    gr, ns = R["gr"], R["ns"]
    pads = NC.net_pads(gr.B)
    m = {}
    for n, nt in gr.nets.items():
        for leg in nt["members"]:
            info = gr.cls.get(leg, nt["info"])
            if GR.is_controlled(leg, info, R.get("cons")):
                ps = [(p["x"], p["y"]) for p in pads.get(leg, [])]
                m[leg] = (ns[leg]["len"], ns[leg]["vias"], round(NC.mst_length(ps), 2))
    return m


def neck_violations(R):
    """Controlled/power nets routed through a neck-down cell that is over capacity: they cannot keep their geometry there."""
    gr = R["gr"]
    bad = []
    for n, r in gr.routes.items():
        if not gr.nets[n].get("fixedw"):
            continue
        for (l, c, d) in r["keys"]:
            if c in gr.neck_cells and (gr.uH if d == 0 else gr.uV)[l][c] > (gr.capH if d == 0 else gr.capV)[l][c]:
                bad.append(n)
                break
    return sorted(set(bad))


def evaluate(job):
    """Worker: build the scratch board and constraints of one option, re-place, plan.  -> result dict (JSON-able)."""
    from . import fluxmap as FM, routediag as RD, planroute as PL, planplace as PP
    t0 = time.time()
    name, rules, grow = job["name"], job["rules"], tuple(job["grow"])
    out = job["outdir"]
    cons = scratch_cons(job["cons"], rules)
    spec = job["spec"]
    text = open(job["base_board"], errors="surrogateescape").read()
    B0 = RD.load(job["base_board"])
    if any(grow):
        text = grow_text(text, B0, grow, cons)
    path = os.path.join(out, name + ".kicad_pcb")
    open(path, "w", errors="surrogateescape").write(text)
    B = RD.load(path)
    R = PL.run(B, cons, spec, ignore_routing=True)
    placed, moved, mean_d = False, 0, 0.0
    if job["place_evals"] > 0 and (any(grow) or not passes(R)):
        mv, st = PP.plan_place(B, cons, spec, log=lambda m: None, max_evals=job["place_evals"], rotation=job.get("rotation", False),
                               spread=grow if any(grow) else None)
        if mv:
            text2, _done = BW.apply_moves(text, {r: v for r, v in mv.items() if r not in st["flips"]})
            path2 = path + ".placed"
            open(path2, "w", errors="surrogateescape").write(text2)
            B2 = RD.load(path2)
            R2 = PL.run(B2, cons, spec, ignore_routing=True)
            os.remove(path2)
            if not (PL.rank_key(R) < PL.rank_key(R2)):         # confirmed by a full plan on the predicate key: never end worse than the input
                text, B, R, placed = text2, B2, R2, True
                moved = len(mv)
                mean_d = sum(math.hypot(x - B0["parts"][r]["pos"][0], y - B0["parts"][r]["pos"][1]) for r, (x, y, a) in mv.items()
                             if r in B0["parts"]) / max(1, len(mv))
                open(path, "w", errors="surrogateescape").write(text)
    R["cons"] = cons
    v = PL.verdict(R)
    W, H = board_size(path)
    layers = len(R["gr"].layers) + (2 if R["gr"].variant not in ("8layer",) and False else 0)
    nlay = len(B["copper"]) + (2 if rules.get("variant") == "8layer" else 0)
    cost, pen = relative_cost(W * H / 100.0, rules, nlay, cons, base_layers=len(B["copper"]))
    cm = controlled_metrics(R)
    nv = neck_violations(R)
    codes = v["counts"]
    res = {"name": name, "grow": list(grow), "rules": rules, "size": [W, H], "area_cm2": round(W * H / 100.0, 1),
           "score": v["score"], "confidence": v["confidence"], "conf_terms": v["conf_terms"], "pass": passes(R),
           "sealed": v["components"]["sealed"] + v["components"]["oversub"], "overflow": v["components"]["overflow"],
           "no_via_site": v["components"]["no_via_site"], "counts": codes, "cost": round(cost, 1), "cost_pen": round(pen, 2),
           "layers": nlay, "subfloor": subfloor(rules, float((cons.get("plan") or {}).get("floor_mm", FLOOR))), "neck_violations": nv, "controlled": {k: list(x) for k, x in cm.items()},
           "return_path": codes.get("PLAN_RETURN_PATH", 0), "placed": placed, "moved": moved, "mean_move_mm": round(mean_d, 2),
           "risk_frac": R["risk_frac"], "routable_pct": round(100 * (1 - R["risk_frac"])), "causes": R["causes"]["units"], "seconds": round(time.time() - t0, 1), "board": path}
    m = FM.build(B, cons, spec, path, cls=R["cls"])
    m.update(PL.plan_section(R))
    FM.save(m, os.path.join(out, name + ".fluxmap.json"))
    with open(os.path.join(out, name + ".constraints.toml"), "w") as fh:
        fh.write(f"# option {name}: grow L/R/T/B = {list(grow)} mm\n" + toml_text(rules))
    return res


# ------------------------------------------------------------------ search
def neck_rules(base, track, clr, margin=4.0):
    """A neck-down region around the fine-pitch parts that have findings in the baseline (else the largest connectors)."""
    refs = []
    fine_find = [(f.get("part"), f) for f in base["findings"] if f.get("part")]
    for f in base["findings"]:
        refs += [f["part"]] if f.get("part") else []
        if f["code"] == "PLAN_OVERFLOW":
            refs += [p for p in f.get("parts", []) if len(p) and True]
    P = base["gr"].B["parts"]
    refs = [r for r in dict.fromkeys(refs) if r in P and EP.is_fine(P[r], base["pr"])]
    if not refs:
        refs = [r for r, p in sorted(P.items(), key=lambda kv: -kv[1]["n_pads"])[:2] if EP.is_fine(p, base["pr"])]
    return {"neck": {"fanout": {"refs": refs[:12], "track_mm": track, "clearance_mm": clr, "margin_mm": margin}}}


def perf_worse(res, base):
    """The controlled-net rule, PER NET, fail closed:
       - every controlled net of the baseline must be present in the option (missing = violation);
       - zero added vias on any controlled net;
       - planned length may exceed the baseline's only by what the option's geometry forces: the growth of the net's own ratsnest
         (MST of its pads on the option board minus on the baseline board) plus 0.5 mm of grid rounding;
       - no added return-path finding; no controlled/power net overflowing inside a neck-down."""
    why = []
    for n, (bl, bv, bm) in sorted(base["controlled"].items()):
        o = res["controlled"].get(n)
        if o is None:
            why.append(f"{n}: not planned in the option (fail closed)")
            continue
        if o[1] > bv:
            why.append(f"{n}: vias {bv} -> {o[1]}")
        allow = max(0.0, o[2] - bm) + 0.5
        if o[0] > bl + allow:
            why.append(f"{n}: copper {bl:.1f} -> {o[0]:.1f} mm (allowed +{allow:.1f})")
    if res["return_path"] > base["return_path"]:
        why.append(f"return-path findings {base['return_path']} -> {res['return_path']}")
    if res["neck_violations"]:
        why.append("controlled/power nets overflow inside the neck-down: " + ", ".join(res["neck_violations"][:4]))
    return why


def impact(res, rules, why):
    if res["neck_violations"] or res["subfloor"]:
        lab = "touches controlled-impedance nets (never recommended)" if res["neck_violations"] else "none to controlled nets; sub-floor rule"
    elif rules.get("track_mm") is not None or rules.get("neck") or rules.get("via_mm") is not None or rules.get("via_in_pad"):
        lab = "low-speed only"
    else:
        lab = "none"
    return lab + (" - worse: " + "; ".join(why) if why else "")


def rank(results, base):
    rows = []
    for r in results:
        why = perf_worse(r, base)
        r["perf_why"] = why
        r["impact"] = impact(r, r["rules"], why)
        r["never"] = bool(r["neck_violations"]) or r["subfloor"]
        rows.append(r)
    ok = [r for r in rows if r["pass"] and not r["never"] and not r["perf_why"]]
    ok.sort(key=lambda r: (r["area_cm2"], r["cost"], -r["confidence"], r["score"]))
    worse = [r for r in rows if r["pass"] and not r["never"] and r["perf_why"]]
    for r in worse:
        r["never"] = True                                  # a pass that costs controlled-net performance is never recommended
    sub = [r for r in rows if (r["never"] or r["subfloor"])]              # never offered: perf-worse, invalid neck-down, below the owner floor
    sub.sort(key=lambda r: (not r["pass"], r["area_cm2"], r["cost"]))
    miss = [r for r in rows if not r["pass"] and not r["never"] and not r["subfloor"]]
    miss.sort(key=lambda r: (r["overflow"] + 100 * r["sealed"] + 40 * r["no_via_site"], bool(r["perf_why"]), r["area_cm2"]))
    return ok, sub, miss


def describe(r, base_size):
    parts = []
    g = r["grow"]
    names = ("left", "right", "top", "bottom")
    for k, v in zip(names, g):
        if v:
            parts.append(f"+{v:g} mm {k}")
    rl = r["rules"]
    if rl.get("neck"):
        n = next(iter(rl["neck"].values()))
        parts.append(f"local {n['track_mm']:g}/{n['clearance_mm']:g} neck-down at {','.join(n['refs'][:3])}{'...' if len(n['refs']) > 3 else ''}")
    if rl.get("track_mm") is not None:
        parts.append(f"global {rl['track_mm']:g}/{rl['clearance_mm']:g}")
    if rl.get("via_mm") is not None:
        parts.append(f"via {rl['via_mm']:g}/{rl['drill_mm']:g}")
    if rl.get("via_in_pad"):
        parts.append("via-in-pad " + ",".join(rl["via_in_pad"][:2]))
    if rl.get("variant"):
        parts.append({"plane3v3": "dedicated +3V3 plane layer", "dirswap": "layer directions swapped", "8layer": "8 layers"}.get(rl["variant"], rl["variant"]))
    return " + ".join(parts) or "as is"


def format_table(base, ok, sub, miss, limit=12):
    L = [f"{'option':44s} {'size mm':>13s} {'score':>7s} {'conf':>4s} {'sealed':>6s} {'ovfl':>5s} {'cost':>6s}  performance impact / what moves"]
    bs = base["size"]
    off = [r for r in sub if r["subfloor"]]
    for tag, rows in (("PASS", ok), ("PASS-NOT-RECOMMENDED", [r for r in sub if not r["subfloor"]]), ("NEAREST MISS", miss[:5])):
        for r in rows[:limit]:
            mv = f"{r['moved']} part(s) moved" if "moved" in r else ""
            L.append(f"{describe(r, bs)[:44]:44s} {r['size'][0]:6.1f}x{r['size'][1]:<6.1f} {r['score']:7.0f} {r['confidence']:4d} {r['sealed']:6d} "
                     f"{r['overflow']:5.0f} {100 * r['cost'] / base['cost']:5.0f}%  [{tag}] routable~{r['routable_pct']}% moved {r.get('moved', 0)}@{r.get('mean_move_mm', 0)}mm {r['impact']}")
    if off:
        L.append("not offered: below owner floor (needs an owner rule change): " + "; ".join(
            f"{describe(r, bs)} -> {'passes' if r['pass'] else 'still fails'}, score {r['score']:.0f}" for r in off[:3]))
    return "\n".join(L)


def prompt_text(base, ok, sub, miss):
    """The one-line owner prompt.  Options below the owner's floor are NOT offered here: they are listed on a separate line."""
    W, H = base["size"]
    if base["pass"]:
        return f"Fits as is at {W:g} x {H:g} mm (PLAN_SCORE {base['score']:g})."
    s = f"Fails at {W:g} x {H:g} mm (PLAN_SCORE {base['score']:g}, overflow {base['overflow']:g}, sealed {base['sealed']}). "
    if ok:
        best = ok[0]
        s += f"Passes with {describe(best, base['size'])} ({best['size'][0]:g} x {best['size'][1]:g}). "
        alt = [f"{describe(r, base['size'])} ({r['size'][0]:g} x {r['size'][1]:g}, cost {100 * r['cost'] / base['cost']:.0f}%)" for r in ok[1:4]]
        if alt:
            s += "Alternatives: " + "; ".join(alt) + ". "
        s += f"Recommended: {describe(best, base['size'])}."
    else:
        s += "No option within the limits passes. "
        if miss:
            m = miss[0]
            s += f"Nearest miss: {describe(m, base['size'])} (overflow {m['overflow']:g}, sealed {m['sealed']})."
    off = [r for r in sub if r["subfloor"] and not r["neck_violations"] and not r["perf_why"]]
    if off:
        s += f" Not offered (below the owner floor, would need an owner rule change): {'; '.join(describe(r, base['size']) for r in off[:2])}."
    return s


def search(board, cons, spec, outdir, max_grow=6, workers=None, place_evals=120, base_place_evals=250, log=print, calib=None, hit_min=0.6):
    """-> dict(baseline, ok, sub, miss, prompt, table, seconds, caveat)."""
    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    workers = workers or min(12, os.cpu_count() or 4)
    ctx = multiprocessing.get_context("fork")
    done = {}

    def run_jobs(jobs):
        for j in jobs:
            j.update(outdir=outdir, cons=cons, spec=spec, place_evals=j.get("place_evals", place_evals))
        with cf.ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            res = list(ex.map(evaluate, jobs))
        for r in res:
            done[r["name"]] = r
            log(f"  {r['name']:28s} {r['size'][0]:.1f}x{r['size'][1]:.1f} score {r['score']:7.1f} sealed {r['sealed']} overflow {r['overflow']:g} "
                f"{'PASS' if r['pass'] else 'fail'} ({r['seconds']:.0f} s)")
        return res

    base = run_jobs([{"name": "baseline", "grow": (0, 0, 0, 0), "rules": {}, "base_board": board, "place_evals": base_place_evals,
                      "rotation": True}])[0]
    from . import routediag as RD, planroute as PL
    if base["pass"]:
        return {"baseline": base, "ok": [], "sub": [], "miss": [], "prompt": prompt_text(base, [], [], []), "table": "", "fits": True,
                "seconds": round(time.time() - t0, 1)}
    bb = base["board"]
    B = RD.load(bb)
    c0 = scratch_cons(cons, {})
    R0 = PL.run(B, c0, spec, ignore_routing=True)
    base["findings_obj"] = None
    base_view = {"findings": R0["findings"], "gr": R0["gr"], "pr": R0["pr"]}
    bl = {"base_board": bb}
    fl = float(((cons.get("plan") or {}).get("floor_mm", FLOOR)))
    fab = (cons.get("fab") or {})
    sub_t = float(fab.get("min_track_mm", 0.10))
    sub_c = float(fab.get("min_clearance_mm", 0.10))
    sub_ok = min(sub_t, sub_c) < fl - 1e-9              # sub-floor options exist only when the fab minimum is below the owner floor
    via_min = float(fab.get("min_via_dia_mm", 0.40))
    drill_min = float(fab.get("min_drill_mm", 0.20))
    jobs = []
    for e, g in zip(EDGES, ((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1))):
        jobs.append(dict(bl, name=f"grow_{e}_{max_grow}", grow=tuple(v * max_grow for v in g), rules={}))
    nl = neck_rules(base_view, fl, fl)
    if sub_ok:
        jobs += [dict(bl, name="neck_100", grow=(0,) * 4, rules=neck_rules(base_view, sub_t, sub_c)),
                 dict(bl, name="global_100", grow=(0,) * 4, rules={"track_mm": sub_t, "clearance_mm": sub_c})]
    jobs += [dict(bl, name="neck_127", grow=(0,) * 4, rules=nl),
             dict(bl, name="global_127", grow=(0,) * 4, rules={"track_mm": fl, "clearance_mm": fl}),
             dict(bl, name="via_min", grow=(0,) * 4, rules={"via_mm": via_min, "drill_mm": drill_min}),
             dict(bl, name="via_in_pad", grow=(0,) * 4, rules={"via_in_pad": [r for r in nl["neck"]["fanout"]["refs"]] or ["U*"]}),
             dict(bl, name="plane3v3", grow=(0,) * 4, rules={"variant": "plane3v3"}),
             dict(bl, name="dirswap", grow=(0,) * 4, rules={"variant": "dirswap"}),
             dict(bl, name="layers8", grow=(0,) * 4, rules={"variant": "8layer"})]
    log(f"round 1: {len(jobs)} option(s) in parallel")
    r1 = run_jobs(jobs)
    # bisection per edge that passes at the probe size
    lo = {e: 0 for e in EDGES}
    hi = {e: max_grow for e in EDGES if done[f"grow_{e}_{max_grow}"]["pass"]}
    while any(hi[e] - lo[e] > 1 for e in hi):
        jobs = []
        for e in hi:
            if hi[e] - lo[e] > 1:
                mid = (lo[e] + hi[e]) // 2
                g = tuple(mid if k == e else 0 for k in EDGES)
                jobs.append(dict(bl, name=f"grow_{e}_{mid}", grow=g, rules={}))
        for r in run_jobs(jobs):
            e = r["name"].split("_")[1]
            mid = int(r["name"].split("_")[2])
            if r["pass"]:
                hi[e] = mid
            else:
                lo[e] = mid
    if not hi:       # no single edge passes: pairs of the two nearest-miss edges
        edges = sorted(EDGES, key=lambda e: done[f"grow_{e}_{max_grow}"]["overflow"] + 100 * done[f"grow_{e}_{max_grow}"]["sealed"])[:2]
        lo2, hi2 = 0, max_grow
        probe = run_jobs([dict(bl, name=f"pair_{edges[0]}{edges[1]}_{max_grow}", rules={},
                               grow=tuple(max_grow if k in edges else 0 for k in EDGES))])[0]
        if probe["pass"]:
            while hi2 - lo2 > 1:
                mid = (lo2 + hi2) // 2
                r = run_jobs([dict(bl, name=f"pair_{edges[0]}{edges[1]}_{mid}", rules={},
                                   grow=tuple(mid if k in edges else 0 for k in EDGES))])[0]
                if r["pass"]:
                    hi2 = mid
                else:
                    lo2 = mid
    # combinations of at most two small relaxations: +1/+2 mm on the best edge with a neck-down or a smaller via, 3V3 plane + neck-down
    best_e = min(EDGES, key=lambda e: done[f"grow_{e}_{max_grow}"]["overflow"] + 100 * done[f"grow_{e}_{max_grow}"]["sealed"])
    jobs = []
    for g in (1, 2):
        ge = tuple(g if k == best_e else 0 for k in EDGES)
        jobs.append(dict(bl, name=f"combo_{best_e}{g}_neck", grow=ge, rules=nl))
        jobs.append(dict(bl, name=f"combo_{best_e}{g}_via", grow=ge, rules={"via_mm": via_min, "drill_mm": drill_min}))
    jobs.append(dict(bl, name="combo_plane3v3_neck", grow=(0,) * 4, rules=dict(nl, variant="plane3v3")))
    jobs.append(dict(bl, name="combo_neck_via", grow=(0,) * 4, rules=dict(nl, via_mm=via_min, drill_mm=drill_min)))
    run_jobs(jobs)
    opts = [r for n, r in done.items() if n != "baseline"]
    ok, sub, miss = rank(opts, base)
    caveat = None
    if calib is not None:
        rate = calib["placement_rate"]
        if rate < hit_min:
            caveat = (f"verdict unproven (placement-only calibration {100 * rate:.0f}%: {calib['placement_hit']} of {calib['placement_failed']} "
                      f"pads that routing left open lie near a risky finding; threshold {100 * hit_min:.0f}%)")
    else:
        caveat = "verdict unproven (placement-only calibration: none, no --calibrate-board given)"
    out = {"baseline": base, "ok": ok, "sub": sub, "miss": miss, "prompt": prompt_text(base, ok, sub, miss),
           "table": format_table(base, ok, sub, miss), "fits": False, "caveat": caveat, "seconds": round(time.time() - t0, 1)}
    with open(os.path.join(outdir, "plan-options.json"), "w") as fh:
        json.dump({k: v for k, v in out.items() if k != "baseline"} | {"baseline": {k: v for k, v in base.items() if k != "findings_obj"}},
                  fh, indent=1, default=str)
    return out
