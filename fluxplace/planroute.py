"""plan-route: map -> escapes -> global route -> one verdict (PLAN_SCORE).  See docs/commands/plan-route.md.

PLAN_SCORE = 100 x sealed pins + 40 x plane pins without a via site + 5 x overflow (track-cell sum, vias
included) + 20 x mm over budget on rf/switch_node/bypass/pair nets, 3 x on the others (as preroute.score) + 30 x pair splits + 30 x RF layer changes + 200 x unreached
connections + 0.05 x class-weighted estimated copper mm (pair 4, rf 6, clock 5, bypass 8, switch 10, signal 1, slow 1, rails 0.25: long
high-speed spans cost, and a placer keeps moving once nothing is wrong).
Lower is better; 0 violations -> the score is just the weighted copper length term.  The over_budget component counts hard-class mm (others scaled by 3/20).  Weights: `[plan] w_*`.
"""
import json
import math
from collections import defaultdict
import os
import time
from collections import Counter, defaultdict

from . import escapeplan as EP
from . import globalroute as GR
from . import netclass as NC

CODES = ["PLAN_PIN_SEALED", "PLAN_ESCAPE_OVERSUBSCRIBED", "PLAN_POWER_NO_PLANE", "PLAN_NO_VIA_SITE", "PLAN_OVERFLOW", "PLAN_NET_OVER_BUDGET", "PLAN_PAIR_SPLIT",
         "PLAN_RF_LAYER_CHANGES", "PLAN_UNREACHABLE", "PLAN_BUS_WEAVE", "PLAN_PART_IN_CORRIDOR", "PLAN_RETURN_PATH", "PLAN_ZONING"]
FAIL_CODES = ("PLAN_PIN_SEALED", "PLAN_ESCAPE_OVERSUBSCRIBED", "PLAN_OVERFLOW")   # kept for importers; the predicate is is_hard_failure


def open_state(B, board_path=None, method="text"):
    """({net: open connections}, {net: [pad clusters]}, routed set) of the board as it is."""
    from . import routeconn as RC
    groups = {}
    if method == "pcbnew" and board_path:
        nets = RC.open_by_net(board_path, fill=True)
        text = RC.text_open(B, groups)
        groups = {n: g for n, g in groups.items() if n in nets}
    else:
        nets = RC.text_open(B, groups)
    pads = NC.net_pads(B)
    cu = {s["net"] for s in B["segs"]} | {v["net"] for v in B["vias"]}
    routed = {n for n, ps in pads.items() if len(ps) >= 2 and n not in nets and n in cu}
    return nets, groups, routed


def score_of(pr, findings, ns, cls=None):
    c = Counter(f["code"] for f in findings)
    over = sum(f.get("eff", f.get("overflow", 0)) for f in findings if f["code"] == "PLAN_OVERFLOW")
    ob = sum(max(0.0, f["value"] - f["limit"]) * (1.0 if f.get("cls") in GR.HARD else pr["w_overbudget"] / pr["w_overbudget_hard"])
             for f in findings if f["code"] == "PLAN_NET_OVER_BUDGET")
    unr = sum(f["value"] for f in findings if f["code"] == "PLAN_UNREACHABLE")
    ln = sum(v["len"] for v in ns.values())
    wl = sum(v["len"] * max(0.25, (cls or {}).get(n, {}).get("weight", 1.0)) for n, v in ns.items())
    comp = {"sealed": c["PLAN_PIN_SEALED"], "oversub": c["PLAN_ESCAPE_OVERSUBSCRIBED"], "power_no_plane": c["PLAN_POWER_NO_PLANE"], "no_via_site": c["PLAN_NO_VIA_SITE"], "overflow": round(over, 1),
            "over_budget_mm": round(ob, 1), "pair_split": c["PLAN_PAIR_SPLIT"], "rf_changes": c["PLAN_RF_LAYER_CHANGES"], "weave": c["PLAN_BUS_WEAVE"], "corridor": c["PLAN_PART_IN_CORRIDOR"], "return_path": c["PLAN_RETURN_PATH"], "zoning": c["PLAN_ZONING"],
            "unreached": unr, "copper_mm": round(ln, 1), "weighted_copper": round(wl)}
    s = (pr["w_sealed"] * (comp["sealed"] + comp["oversub"]) + pr["w_nosite"] * comp["power_no_plane"] + pr["w_nosite"] * comp["no_via_site"] + pr["w_overflow"] * over +
         pr["w_overbudget_hard"] * ob + pr["w_pair"] * comp["pair_split"] + 4.0 * comp["weave"] + 3.0 * comp["corridor"] + 2.0 * comp["return_path"] + 5.0 * comp["zoning"] + pr["w_rf"] * comp["rf_changes"] +
         pr["w_unreach"] * unr + pr["w_len"] * wl)
    return round(s, 1), comp


def run(B, cons=None, spec=None, ignore_routing=False, cell=None, board_path=None, log=None, iterations=None,
        open_method="text", cls=None, variant=None):
    """-> result dict {pr, cls, plan, gr, ns, findings, score, comp, state, timing, mode}."""
    variant = variant or ((cons or {}).get("plan") or {}).get("variant")
    log = log or (lambda m: None)
    cons = cons or {}
    t0 = time.time()
    cls = cls if cls is not None else NC.classify(B, cons, spec)
    pr = EP.plan_params(cons, cell_mm=cell, iterations=iterations)
    state = {"open": {}, "groups": {}, "routed": set()}
    if not ignore_routing:
        o, g, r = open_state(B, board_path, open_method)
        state = {"open": o, "groups": g, "routed": r}
    t1 = time.time()
    plan = EP.plan_escapes(B, cons, cls, pr, ignore_routing=ignore_routing, routed_nets=state["routed"])
    t2 = time.time()
    gr = GR.GlobalRoute(B, cons, cls, pr, plan, ignore_routing, routed_nets=state["routed"], groups=state["groups"], log=log, variant=variant)
    gr.build()
    gr.route_all()
    t3 = time.time()
    ns = gr.net_stats()
    f = list(plan.findings) + GR.findings(gr, ns)
    from . import planrules as PRL
    f += PRL.part_in_corridor(gr, B) + PRL.return_path(gr, plan, B, cons) + PRL.zoning(B, cls, cons)
    sc, comp = score_of(pr, f, ns, cls)
    from . import planrules as PRL
    cz, kind = PRL.overflow_causes(gr, B, pr)
    risk = PRL.risk_nets(gr, kind)
    comp["risk_nets"] = len(risk)
    nplan = max(1, len(gr.nets))
    return {"causes": cz, "risk": sorted(risk), "risk_frac": round(len(risk) / nplan, 3), "pr": pr, "cls": cls, "plan": plan, "gr": gr, "ns": ns, "findings": f, "score": sc, "comp": comp, "state": state,
            "mode": "ignore-routing" if ignore_routing else "default",
            "timing": {"state_s": round(t1 - t0, 1), "escapes_s": round(t2 - t1, 1), "route_s": round(t3 - t2, 1),
                       "total_s": round(time.time() - t0, 1)}}


HARD_CODES = ("PLAN_PIN_SEALED", "PLAN_ESCAPE_OVERSUBSCRIBED", "PLAN_UNREACHABLE", "PLAN_NO_VIA_SITE", "PLAN_RF_LAYER_CHANGES")


def is_hard_failure(x, pr=None):
    """One finding that fails the verdict by itself (the single PASS predicate, see `predicate`)."""
    c = x["code"]
    if c in HARD_CODES:
        return True
    if c == "PLAN_POWER_NO_PLANE":
        return bool((pr or {}).get("fail_on_power_no_plane", True))
    if c == "PLAN_NET_OVER_BUDGET":
        return x.get("cls") in GR.HARD
    if c == "PLAN_OVERFLOW":
        return bool(x.get("risky"))        # over capacity somewhere outside the part courtyards: counted through risk_frac / risk_nets below
    return False


def predicate(R):
    """THE verdict predicate used by plan-route (exit code), the route-ready gate and plan-options: FAIL when any of: sealed pin, oversubscribed
    pin group, unreachable connection, plane pin without via site, RF net over its layer-change limit, rail without plane/pour
    (`[plan] fail_on_power_no_plane`, default true), a hard-class net over budget (rf, switch_node, bypass, pair), the share of planned nets at risk above
    `max_risk_frac` (0.06), or more than `max_risk_nets` (10) nets at risk.  -> (fail, [reasons], hard failure count)."""
    pr = R["pr"]
    hard = [x for x in R["findings"] if x["code"] != "PLAN_OVERFLOW" and is_hard_failure(x, pr)]
    why = sorted({x["code"] for x in hard})
    nrisk = R["comp"]["risk_nets"]
    if R["risk_frac"] > pr["max_risk_frac"]:
        why.append(f"risk_frac {R['risk_frac']:.3f} > {pr['max_risk_frac']:g}")
    if nrisk > pr["max_risk_nets"]:
        why.append(f"{nrisk} nets at risk > {pr['max_risk_nets']:g}")
    return bool(why), why, len(hard)


def rank_key(R):
    """Lexicographic quality of a plan, lower is better: (FAIL flag, hard failure count, risk_frac, PLAN_SCORE)."""
    fail, _why, hard = predicate(R)
    return (int(fail), hard, R["risk_frac"], R["score"])


def confidence(R):
    """Routability confidence 0-100 (docs/commands/plan-route.md): 100 x (0.6 A + 0.2 H + 0.2 S) - 15 x (sealed + oversubscribed pins, at most 45),
       A = 1 - risk_frac / 0.20 (risk_frac = share of planned nets whose path crosses an over-capacity cell outside part courtyards),
       H = min over fine-pitch parts of free via slots / (2 x signal pins), S = min over budgeted nets of slack / (20 % of budget); each clipped 0..1.
       Scale: the V1.6 board routed 94 % of its nets (risk_frac 0.06) -> A = 0.7."""
    A = min(1.0, max(0.0, 1.0 - R["risk_frac"] / 0.20))
    hs = [h["free_total"] / max(1.0, 2.0 * sum(v for k, v in h["pins"].items() if k in ("direct", "via", "sealed")))
          for h in R["plan"].headroom.values() if sum(h["pins"].values()) >= 6]
    H = min(1.0, max(0.0, min(hs))) if hs else 1.0
    sl = []
    for n, nt in R["gr"].nets.items():
        lim = NC.effective_budget(nt["info"])
        if lim:
            sl.append((lim - R["ns"][n]["len"]) / (0.2 * lim))
    S = min(1.0, max(0.0, min(sl))) if sl else 1.0
    c = Counter(f["code"] for f in R["findings"])
    v = 100 * (0.6 * A + 0.2 * H + 0.2 * S) - min(45, 15 * (c["PLAN_PIN_SEALED"] + c["PLAN_ESCAPE_OVERSUBSCRIBED"]))
    v -= min(20, 10 * c["PLAN_UNREACHABLE"]) + min(20, 5 * c["PLAN_NO_VIA_SITE"]) + min(20, 5 * sum(
        1 for f in R["findings"] if f["code"] == "PLAN_NET_OVER_BUDGET" and f.get("cls") in GR.HARD))
    if predicate(R)[0]:
        v = min(v, 49)                                  # a failing verdict never reads as confident
    return round(min(100.0, max(0.0, v))), {"routable_share": A, "headroom": round(H, 2), "slack": round(S, 2), "risk_frac": R["risk_frac"]}


def verdict(R):
    f = R["findings"]
    c = Counter(x["code"] for x in f)
    thr = float(R["pr"]["max_risk_frac"])
    fail, why, _hard = predicate(R)
    conf, parts = confidence(R)
    return {"risk_frac": R["risk_frac"], "predicted_routable": round(1 - R["risk_frac"], 3), "causes": R["causes"], "max_risk_frac": thr,
            "score": R["score"], "confidence": conf, "conf_terms": parts, "result": "FAIL" if fail else "ok", "reasons": why, "counts": {k: c[k] for k in CODES if c[k]},
            "components": R["comp"], "mode": R["mode"], "nets_planned": len(R["gr"].nets),
            "overflow_total": R["gr"].summary()["overflow"], "timing": R["timing"]}


def rank(findings):
    """Worst first: sealed pins, overflow by size, unreachable, no via site, budget excess, rest."""
    w = {"PLAN_PIN_SEALED": 0, "PLAN_ESCAPE_OVERSUBSCRIBED": 0, "PLAN_POWER_NO_PLANE": 3, "PLAN_UNREACHABLE": 1, "PLAN_OVERFLOW": 2, "PLAN_NO_VIA_SITE": 3,
         "PLAN_NET_OVER_BUDGET": 4, "PLAN_BUS_WEAVE": 6, "PLAN_PART_IN_CORRIDOR": 5, "PLAN_RETURN_PATH": 5, "PLAN_ZONING": 5, "PLAN_RF_LAYER_CHANGES": 5, "PLAN_PAIR_SPLIT": 6}

    def key(f):
        mag = f.get("overflow") or ((f.get("value", 0) - f.get("limit", 0)) if f.get("limit") is not None else 0) or 0
        return (w.get(f["code"], 9), -float(mag), f["msg"])
    return sorted(findings, key=key)


def format_text(R, top=10, title=""):
    v = verdict(R)
    L = [f"PLAN-ROUTE {title} [{R['mode']}]".rstrip(),
         f"  PLAN_SCORE {v['score']:g}   confidence {v['confidence']}   {v['result']}   ({v['timing']['total_s']:g} s: state {v['timing']['state_s']:g}, "
         f"escapes {v['timing']['escapes_s']:g}, route {v['timing']['route_s']:g})",
         "  components: " + ", ".join(f"{k} {c:g}" for k, c in v["components"].items()),
         f"  nets planned {v['nets_planned']}, overflow {v['overflow_total']:g} (courtyard {v['causes']['units']['courtyard']:g}, fan-out "
         f"{v['causes']['units']['fanout']:g}, channel {v['causes']['units']['channel']:g}); at-risk nets {R['comp']['risk_nets']} = "
         f"{100 * v['risk_frac']:.0f}% -> predicted routable {100 * v['predicted_routable']:.0f}% (limit {100 * v['max_risk_frac']:.0f}% at risk)"]
    for k in CODES:
        if k in v["counts"]:
            L.append(f"  {k:22s} {v['counts'][k]}")
    L.append(f"  worst {top}:")
    for x in rank(R["findings"])[:top]:
        L.append(f"    {x['code']:20s} {x['msg']}")
        if x.get("suggestion"):
            L.append(f"      -> {x['suggestion']}")
    L.append(f"  RESULT: {v['result']} ({len(R['findings'])} finding(s))")
    return "\n".join(L)


def plan_section(R):
    """The planner's sections of the map (escapes, grid, channels, verdict)."""
    gr, plan = R["gr"], R["plan"]
    esc = defaultdict(dict)
    for (ref, pad), rec in sorted(plan.pins.items()):
        k = rec.get("kind")
        if not rec.get("fine") and k in ("direct", "pour"):
            continue
        if k == "direct":
            esc[ref][pad] = ["d", rec.get("dir"), rec.get("lane")]
        elif k == "via":
            esc[ref][pad] = ["v"] + rec["via"] + [rec.get("stub")]
        elif k == "pour":
            esc[ref][pad] = ["p"]
        elif k == "sealed":
            esc[ref][pad] = ["s", rec.get("blocker")]
        elif k == "no_site":
            esc[ref][pad] = ["n"]
    N = gr.N
    dig = lambda v: str(min(int(v), 9)) if v < 10 else "x"
    cap = {"capH": {}, "capV": {}, "useH": {}, "useV": {}}
    for l, name in enumerate(gr.layers):
        cap["capH"][name] = "".join(dig(gr.capH[l][c]) for c in range(N))
        cap["capV"][name] = "".join(dig(gr.capV[l][c]) for c in range(N))
        cap["useH"][name] = "".join(dig(gr.uH[l][c]) for c in range(N))
        cap["useV"][name] = "".join(dig(gr.uV[l][c]) for c in range(N))
    cap["via_cap"] = "".join(dig(gr.vcap[c]) for c in range(N))
    cap["via_use"] = "".join(dig(gr.vu[c]) for c in range(N))
    grid = {"origin": [round(gr.x0, 2), round(gr.y0, 2)], "cell": gr.cell, "nx": gr.nx, "ny": gr.ny,
            "layers": gr.layers, "rows": "row-major, j*nx+i, digits 0-9 ('x' = 10 or more)"}
    grid.update(cap)
    nets = {}
    ns = R["ns"]
    for name, nt in sorted(gr.nets.items()):
        r = gr.routes.get(name)
        st = ns[name]
        lim = NC.effective_budget(nt["info"])
        ent = {"len": st["len"], "budget": None if lim is None else round(lim, 1), "vias": st["vias"],
               "layers": [l.replace(".Cu", "") for l in st["layers"]]}
        if nt["pair"]:
            ent["pair"] = nt["members"]
        if r:
            runs, cur, curl = [], [], None
            for cn in r["conns"]:
                p = cn.get("path")
                if not p:
                    continue
                for nd in p:
                    l, c = divmod(nd, N)
                    if l != curl:
                        if cur:
                            runs.append(f"{curl}:" + ",".join(cur))
                        cur, curl = [], l
                    cur.append(_b36(c))
                if cur:
                    runs.append(f"{curl}:" + ",".join(cur))
                cur, curl = [], None
            ent["path"] = ";".join(runs)
        nets[name] = ent
    ov = [f for f in R["findings"] if f["code"] == "PLAN_OVERFLOW"]
    channels = [{"box": f["box"], "layer": f["layer"], "demand": f["demand"], "cap": f["capacity"],
                 "overflow": f["overflow"], "nets": f["nets"][:6], "parts": f["parts"]} for f in ov if "box" in f]
    v = verdict(R)
    from . import planrules as PRL
    corr = [[round(gr.x0 + (c % gr.nx) * gr.cell, 2), round(gr.y0 + (c // gr.nx) * gr.cell, 2),
             round(gr.x0 + (c % gr.nx + 1) * gr.cell, 2), round(gr.y0 + (c // gr.nx + 1) * gr.cell, 2)] for c in sorted(PRL.corridor_cells(gr))]
    return {"corridors": corr, "escapes": {"pins": dict(esc), "headroom": plan.headroom,
                        "legend": "d=direct [dir,lane mm], v=via slot [x,y,stub], p=pour, s=sealed [blocker], n=no via site"},
            "grid": grid, "route": nets, "channels": channels,
            "verdict": {"risk_frac": v["risk_frac"], "predicted_routable": v["predicted_routable"], "causes": v["causes"], "score": v["score"], "confidence": v["confidence"], "conf_terms": v["conf_terms"], "result": v["result"], "mode": v["mode"], "components": v["components"],
                        "counts": v["counts"], "reasons": v["reasons"],
                        "findings": rank(R["findings"])[:60], "floors": {k: R["pr"][k] for k in (
                            "track_mm", "clearance_mm", "via_mm", "drill_mm", "cell_mm")}}}


def _b36(n):
    s = ""
    while True:
        n, r = divmod(n, 36)
        s = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + s
        if not n:
            return s


def route_hint(map_path):
    """{net: rank} for the batch router's ordering: nets whose planned path crosses a PLAN_OVERFLOW channel = 0 (route them
    first, while the channel is empty), differential pairs = 1.  `[route] plan_map = "<board>.fluxmap.json"`.  The router has no
    interface for per-net layers or corridors, so the plan can only order the nets (docs/commands/plan-route.md)."""
    m = json.load(open(map_path))
    out = {}
    for ch in m.get("channels", []):
        for n in ch.get("nets", []):
            out[n] = 0
    for n, r in (m.get("route") or {}).items():
        if r.get("pair"):
            for mem in r["pair"]:
                out.setdefault(mem, 1)
    return out


def run_variants(B, cons, spec, ignore_routing, cell=None, board_path=None, base=None):
    """Same plan, alternative layer roles: mixed (as the board), plane3v3 (one inner signal layer becomes a dedicated plane),
    8layer (two more signal layers).  -> [{variant, layers, score, confidence, sealed, overflow, vias, copper}]"""
    rows = []
    for v in ("mixed", "plane3v3", "8layer"):
        R = base if (v == "mixed" and base) else run(B, cons, spec, ignore_routing, cell=cell, board_path=board_path,
                                                     variant=None if v == "mixed" else v)
        ver = verdict(R)
        rows.append({"variant": v, "layers": len(R["gr"].layers), "score": ver["score"], "confidence": ver["confidence"],
                     "sealed": ver["components"]["sealed"] + ver["components"]["oversub"], "overflow": ver["components"]["overflow"],
                     "vias": sum(x["vias"] for x in R["ns"].values()), "copper_mm": ver["components"]["copper_mm"]})
    return rows


def calibrate(B, cons, spec, board_path, grid_scan=True):
    """Predicted vs actual on a routed board: (a) open nets the plan flagged / missed (default mode), (b) the capacity constants:
    for a grid of (util, jog_frac) the share of the board's real track cell-directions that exceed the modelled capacity.
    -> dict with hit/miss/false lists, the errors (open but predicted clear) and the best constants."""
    from . import routeconn as RC
    import math as _m
    R = run(B, cons, spec, ignore_routing=False, board_path=board_path)
    groups = {}
    actual = set(RC.text_open(B, groups))
    pred = set()
    for f in R["findings"]:
        if f["code"] in ("PLAN_PIN_SEALED", "PLAN_NO_VIA_SITE", "PLAN_ESCAPE_OVERSUBSCRIBED", "PLAN_UNREACHABLE", "PLAN_NET_OVER_BUDGET") and f.get("net"):
            pred.add(f["net"])
        elif f["code"] == "PLAN_OVERFLOW":
            pred |= set(f.get("nets", []))
    planned = set(R["gr"].nets)
    grid = []
    padc = defaultdict(set)
    for util in ((0.6, 0.7, 0.8, 0.9, 1.0) if grid_scan else ()):
        for jog in (0.3, 0.4, 0.5, 0.6):
            pr = EP.plan_params(cons, util=util, jog_frac=jog)
            g = GR.GlobalRoute(B, cons, R["cls"], pr, None, True)
            g.build()
            if not padc:
                for q in B["pads"]:
                    if q["net"]:
                        for c in g._cells_of_rect(EP.prect(q)):
                            padc[q["net"]].add(c)
            use, over = defaultdict(set), 0
            for sg in B["segs"]:
                l = g.lid.get(sg["layer"])
                if l is None:
                    continue
                (ax, ay), (bx, by) = sg["a"], sg["b"]
                n = max(1, int(_m.hypot(bx - ax, by - ay) / 0.25))
                for k in range(n + 1):
                    c = g.cell_of(ax + (bx - ax) * k / n, ay + (by - ay) * k / n)
                    if c in padc[sg["net"]]:
                        continue
                    use[(l, c, 0 if abs(bx - ax) >= abs(by - ay) else 1)].add(sg["net"])
            for (l, c, d), nets in use.items():
                if len(nets) > (g.capH if d == 0 else g.capV)[l][c]:
                    over += 1
            grid.append({"util": util, "jog_frac": jog, "over_share": round(over / max(1, len(use)), 3)})
    ok = [g for g in grid if g["over_share"] <= 0.05]
    if not grid:
        return {"actual_open": sorted(actual), "hit": sorted(actual & pred), "miss": sorted(actual - pred), "planned": len(planned),
                "false_alarm": sorted((pred & planned) - actual), "grid": [], "best": None, "score": R["score"], "result": R}
    best = max(ok, key=lambda g: (-g["util"] * 0 + (1.0 - g["util"]) * -1, -g["jog_frac"])) if ok else min(grid, key=lambda g: g["over_share"])
    # tightest model that still holds 95 % of the real usage: smallest util, then largest jog
    best = min(ok, key=lambda g: (g["util"], -g["jog_frac"])) if ok else best
    return {"actual_open": sorted(actual), "predicted": sorted(pred & (planned | actual)), "hit": sorted(actual & pred),
            "miss": sorted(actual - pred), "false_alarm": sorted((pred & planned) - actual), "planned": len(planned),
            "grid": grid, "best": best, "score": R["score"], "result": R}


def write_constants(path, best):
    """Replace/append util and jog_frac in the `[plan]` table of a constraints TOML (text edit, other keys kept)."""
    import re as _re
    import shutil
    shutil.copyfile(path, path + ".bak")                       # never rewrite a constraints file without a backup
    txt = open(path).read()
    kv = {"util": best["util"], "jog_frac": best["jog_frac"]}
    m = _re.search(r"^\[plan\][^\n]*\n", txt, _re.M)
    if not m:
        txt = txt.rstrip("\n") + "\n\n[plan]   # tuned by plan-route --calibrate\n" + "".join(f"{k} = {v}\n" for k, v in kv.items())
    else:
        end = _re.search(r"^\[", txt[m.end():], _re.M)
        stop = m.end() + (end.start() if end else len(txt) - m.end())
        body = txt[m.end():stop]
        for k, v in kv.items():
            if _re.search(rf"^{k}\s*=", body, _re.M):
                body = _re.sub(rf"^{k}\s*=.*$", f"{k} = {v}", body, flags=_re.M)
            else:
                body = f"{k} = {v}\n" + body
        txt = txt[:m.end()] + body + txt[stop:]
    open(path, "w").write(txt)


def swap_candidates(B, cons, spec, max_eval=24):
    """`[swap.group.NAME] pads = ["J10.25", "J10.45", ...]`: pins whose functions may be exchanged (a schematic change, never applied
    here).  Every pair of pads in a group is tried (net names exchanged on the model), the plan re-run; -> [{group, a, b, nets, gain}]
    best first.  Pairs are ordered by how far each net's other pins lie from the other pad (most promising first)."""
    sw = ((cons.get("swap") or {}).get("group") or {})
    base = run(B, cons, spec, ignore_routing=True, iterations=4)
    out, n_eval = [], 0
    pads = {f"{q['ref']}.{q['num']}": q for q in B["pads"]}
    pn = NC.net_pads(B)
    cand = []
    for gname, g in sorted(sw.items()):
        ps = [p for p in g.get("pads", []) if p in pads and pads[p]["net"]]
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                a, b = pads[ps[i]], pads[ps[j]]
                if a["net"] == b["net"]:
                    continue
                def far(q, other):
                    o = [x for x in pn[q["net"]] if x is not q]
                    return min((math.hypot(x["x"] - other["x"], x["y"] - other["y"]) for x in o), default=0) - \
                        min((math.hypot(x["x"] - q["x"], x["y"] - q["y"]) for x in o), default=0)
                cand.append((-(far(a, b) + far(b, a)), gname, ps[i], ps[j]))
    for _k, gname, pa, pb in sorted(cand)[:max_eval]:
        a, b = pads[pa], pads[pb]
        a["net"], b["net"] = b["net"], a["net"]
        try:
            R = run(B, cons, spec, ignore_routing=True, iterations=4)
            out.append({"group": gname, "a": pa, "b": pb, "nets": [b["net"], a["net"]], "gain": round(base["score"] - R["score"], 1),
                        "score": R["score"]})
        finally:
            a["net"], b["net"] = b["net"], a["net"]
        n_eval += 1
    return sorted(out, key=lambda r: -r["gain"]), base["score"]


def calibrate_placement(B, cons, spec, radius=1.5):
    """Placement-only hit rate: plan the routed board's placement with all copper ignored and ask what share of the pads it left
    unjoined (text connectivity) lies within `radius` mm of a risky finding (sealed pin, oversubscribed group, over-capacity cell outside
    courtyards).  -> (rate, failed pads, hits)."""
    from . import routeconn as RC
    groups = {}
    RC.text_open(B, groups)
    failed = []
    for n, cl in groups.items():
        for c in sorted(cl, key=lambda c: -len(c))[1:]:
            failed += [(p["x"], p["y"]) for p in c]
    R = run(B, cons, spec, ignore_routing=True)
    pts = []
    for f in R["findings"]:
        if f["code"] in ("PLAN_PIN_SEALED", "PLAN_ESCAPE_OVERSUBSCRIBED", "PLAN_NO_VIA_SITE"):
            pts.append(tuple(f["at"]))
        elif f["code"] == "PLAN_OVERFLOW" and f.get("risky"):
            pts += [tuple(c) for c in f.get("cells_xy", [])]
    hit = sum(1 for x, y in failed if any(abs(x - px) <= radius + 1.0 and abs(y - py) <= radius + 1.0 and math.hypot(x - px, y - py) <= radius + 1.0
                                          for px, py in pts))
    return (hit / len(failed) if failed else 1.0), len(failed), hit
