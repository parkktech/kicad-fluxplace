"""plan-route: map -> escapes -> global route -> one verdict (PLAN_SCORE).  See docs/commands/plan-route.md.

PLAN_SCORE = 100 x sealed pins + 40 x plane pins without a via site + 5 x overflow (track-cell sum, vias
included) + 1 x mm of copper over budget + 30 x pair splits + 30 x RF layer changes + 200 x unreached
connections + 0.05 x class-weighted estimated copper mm (pair 4, rf 6, clock 5, bypass 8, switch 10, signal 1, slow 1, rails 0.25: long
high-speed spans cost, and a placer keeps moving once nothing is wrong).
Lower is better; 0 violations -> the score is just 0.02 x copper length.  Weights: `[plan] w_*`.
"""
import json
import math
import os
import time
from collections import Counter, defaultdict

from . import escapeplan as EP
from . import globalroute as GR
from . import netclass as NC

CODES = ["PLAN_PIN_SEALED", "PLAN_NO_VIA_SITE", "PLAN_OVERFLOW", "PLAN_NET_OVER_BUDGET", "PLAN_PAIR_SPLIT",
         "PLAN_RF_LAYER_CHANGES", "PLAN_UNREACHABLE"]
FAIL_CODES = ("PLAN_PIN_SEALED", "PLAN_OVERFLOW")


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
    over = sum(f.get("overflow", 0) for f in findings if f["code"] == "PLAN_OVERFLOW")
    ob = sum(max(0.0, f["value"] - f["limit"]) for f in findings if f["code"] == "PLAN_NET_OVER_BUDGET")
    unr = sum(f["value"] for f in findings if f["code"] == "PLAN_UNREACHABLE")
    ln = sum(v["len"] for v in ns.values())
    wl = sum(v["len"] * max(0.25, (cls or {}).get(n, {}).get("weight", 1.0)) for n, v in ns.items())
    comp = {"sealed": c["PLAN_PIN_SEALED"], "no_via_site": c["PLAN_NO_VIA_SITE"], "overflow": round(over, 1),
            "over_budget_mm": round(ob, 1), "pair_split": c["PLAN_PAIR_SPLIT"], "rf_changes": c["PLAN_RF_LAYER_CHANGES"],
            "unreached": unr, "copper_mm": round(ln, 1), "weighted_copper": round(wl)}
    s = (pr["w_sealed"] * comp["sealed"] + pr["w_nosite"] * comp["no_via_site"] + pr["w_overflow"] * over +
         pr["w_overbudget"] * ob + pr["w_pair"] * comp["pair_split"] + pr["w_rf"] * comp["rf_changes"] +
         pr["w_unreach"] * unr + pr["w_len"] * wl)
    return round(s, 1), comp


def run(B, cons=None, spec=None, ignore_routing=False, cell=None, board_path=None, log=None, iterations=None,
        open_method="text", cls=None):
    """-> result dict {pr, cls, plan, gr, ns, findings, score, comp, state, timing, mode}."""
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
    gr = GR.GlobalRoute(B, cons, cls, pr, plan, ignore_routing, routed_nets=state["routed"], groups=state["groups"], log=log)
    gr.build()
    gr.route_all()
    t3 = time.time()
    ns = gr.net_stats()
    f = list(plan.findings) + GR.findings(gr, ns)
    sc, comp = score_of(pr, f, ns, cls)
    return {"pr": pr, "cls": cls, "plan": plan, "gr": gr, "ns": ns, "findings": f, "score": sc, "comp": comp, "state": state,
            "mode": "ignore-routing" if ignore_routing else "default",
            "timing": {"state_s": round(t1 - t0, 1), "escapes_s": round(t2 - t1, 1), "route_s": round(t3 - t2, 1),
                       "total_s": round(time.time() - t0, 1)}}


def verdict(R):
    f = R["findings"]
    c = Counter(x["code"] for x in f)
    fail = bool(c["PLAN_PIN_SEALED"] or c["PLAN_OVERFLOW"])
    return {"score": R["score"], "result": "FAIL" if fail else "ok", "counts": {k: c[k] for k in CODES if c[k]},
            "components": R["comp"], "mode": R["mode"], "nets_planned": len(R["gr"].nets),
            "overflow_total": R["gr"].summary()["overflow"], "timing": R["timing"]}


def rank(findings):
    """Worst first: sealed pins, overflow by size, unreachable, no via site, budget excess, rest."""
    w = {"PLAN_PIN_SEALED": 0, "PLAN_UNREACHABLE": 1, "PLAN_OVERFLOW": 2, "PLAN_NO_VIA_SITE": 3,
         "PLAN_NET_OVER_BUDGET": 4, "PLAN_RF_LAYER_CHANGES": 5, "PLAN_PAIR_SPLIT": 6}

    def key(f):
        mag = f.get("overflow") or ((f.get("value", 0) - f.get("limit", 0)) if f.get("limit") is not None else 0) or 0
        return (w.get(f["code"], 9), -float(mag), f["msg"])
    return sorted(findings, key=key)


def format_text(R, top=10, title=""):
    v = verdict(R)
    L = [f"PLAN-ROUTE {title} [{R['mode']}]".rstrip(),
         f"  PLAN_SCORE {v['score']:g}   {v['result']}   ({v['timing']['total_s']:g} s: state {v['timing']['state_s']:g}, "
         f"escapes {v['timing']['escapes_s']:g}, route {v['timing']['route_s']:g})",
         "  components: " + ", ".join(f"{k} {c:g}" for k, c in v["components"].items()),
         f"  nets planned {v['nets_planned']}, overflow {v['overflow_total']:g}"]
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
    return {"escapes": {"pins": dict(esc), "headroom": plan.headroom,
                        "legend": "d=direct [dir,lane mm], v=via slot [x,y,stub], p=pour, s=sealed [blocker], n=no via site"},
            "grid": grid, "route": nets, "channels": channels,
            "verdict": {"score": v["score"], "result": v["result"], "mode": v["mode"], "components": v["components"],
                        "counts": v["counts"], "timing": v["timing"],
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
