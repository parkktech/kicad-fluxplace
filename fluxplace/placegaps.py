"""PLACEGAPS: the one placement dispatcher (docs/PLAN-PLACE-GAPS.md), the placement twin of `gaps.close`.

A gap is a measured shortfall of a placement ({id, kind, class, mag, refs, nets, at, row, detail}); DETECT[kind] finds them from
data the stages already have; DISPATCH[kind] lists rung names, RUNGS[name](gap, ctx) -> [candidate] proposes moves (a rung never
writes a board and never judges itself); an evaluator applies and judges a candidate; `_accept` is the one accept rule;
`close` is the loop. Phase 0: detectors for every kind, the loop and the accept rule (tested with fake evaluators), memo,
report files and narration; DISPATCH is empty, so `detect_stage` (the only thing the stages call) is detect-only: it writes
report files and prints a table, never a board, never raises. Board-agnostic: no reference, net or distance of a board here.
"""
import copy
import json
import math
import os
import shutil
import time
import zlib

from . import events as EV

ORDER = ("ghost-open", "row-cin-side", "row-template", "row-pin-bypass", "row-away", "row-flight", "row-edge", "row-rail",
         "row-other", "support-far", "support-starved", "net-budget", "side-illegal", "ghost-costly", "ghost-hot", "part-datasheet", "part-stock")
CLASS_RANK = {"hs-pair": 0, "rf": 1, "power": 2, "pair": 3, "plain": 4}
SCOPE = {"ready": ("part-datasheet", "part-stock"),
         "floorplan": tuple(k for k in ORDER if not k.startswith(("part-", "ghost-"))),
         "placetour": tuple(k for k in ORDER if not k.startswith("part-"))}
TERMS = ("hs_open", "rf_open", "power_open", "illegal", "hard_rows", "hard_mag", "decap_far", "support_far", "far_mm",
         "open", "hot", "hot_occ", "vias", "detour")
R6_TERMS = ("hs_open", "rf_open", "power_open", "illegal", "hard_rows")        # never worse, whatever the target gap gained
DIAG_RUNGS = {"feasible"}                     # diagnosis-only rungs: no candidate by design, no "no-candidate" entry
TARGET_MUST_GAIN = ("row-cin-side:", "row-template:", "row-pin-bypass:")
MAX_ROUNDS = 6
CANDS_PER_RUNG = 8
WINDOW_MM = 3.0
DETECT = {}                      # kind -> fn(ctx) -> [gap]
DISPATCH = {k: [] for k in ORDER}  # kind -> [rung name]; rungs arrive with the later phases
RUNGS = {}                       # rung name -> fn(gap, ctx) -> [candidate]


def detector(*kinds):
    def deco(fn):
        for k in kinds:
            DETECT[k] = fn
        return fn
    return deco


def gap(kind, name, cls="plain", mag=0.0, refs=(), nets=(), at=None, row=None, detail=""):
    return {"id": f"{kind}:{name}", "kind": kind, "class": cls, "mag": round(float(mag), 3), "refs": sorted(set(refs)),
            "nets": sorted(set(nets)), "at": [round(at[0], 2), round(at[1], 2)] if at else None, "row": row, "detail": str(detail)[:200]}


def _sort(gs):
    return sorted(gs, key=lambda g: (CLASS_RANK.get(g["class"], 9), -g["mag"], g["id"]))


# ------------------------------------------------------------------ context (lazy, cached; a source that fails is recorded, not raised)
class Ctx(dict):
    """stage, board, cons, spec, ds_dir, plus optional blame (placetour judge), rows (budget rows), parts_gate (partsgate.gate)."""

    def need(self, key, make):
        if key not in self:
            try:
                self[key] = make()
            except Exception as e:                          # noqa: BLE001 - recorded by the detector that asked
                self[key] = None
                self.setdefault("errors", {})[key] = f"{type(e).__name__}: {e}"
        return self[key]

    def B(self):
        from . import routediag as RD
        return self.need("B", lambda: RD.load(self["board"]))

    def rows(self):
        def make():
            p = os.path.splitext(self["board"])[0] + ".budgets.json"          # written by ready.stage_honoured: the full table
            if os.path.exists(p):
                r = json.load(open(p)).get("rows")
                if isinstance(r, list) and r and "metric" in r[0]:
                    return r
            from . import ready as RDY
            return RDY.measure_rows(self["spec"], self["cons"], self.B(), self["board"], ds_dir=self.get("ds_dir"))[0]
        return self["rows"] if self.get("rows") is not None else self.need("_rows", make)

    def intent_rows(self):
        from . import intent as IT
        return self.need("_intent", lambda: IT.check(self.B(), self["cons"], self["spec"]))

    def clusters(self):
        from . import clusters as CL
        return self.need("_cl", lambda: CL.build(self.B(), self["cons"], self["spec"])[0])


# ------------------------------------------------------------------ detectors
def _net_class(ctx):
    """-> fn(net) -> (class, partner or None): rf (constraints), power ([power] rails), pair / hs-pair (naming + families), plain."""
    from . import constraints as C, graph, power as PWR, review as RV
    cons = ctx["cons"]
    rf = set((cons.get("rf") or {}).get("nets") or [])
    pw = set(PWR.route_widths(cons or {}, track_mm=0.15))
    names = sorted({p["net"] for p in ctx.B()["pads"] if p.get("net")}) if isinstance(ctx.B()["pads"], list) else []
    mates = {}
    for slave, master in graph.diff_pairs(names, exclude=C.not_pairs(cons)).items():
        mates[slave], mates[master] = master, slave
    masters = set(graph.diff_pairs(names, exclude=C.not_pairs(cons)).values())

    nets_pins = (ctx.get("spec") or {}).get("nets") or {}
    pins = lambda n: [p[1] for p in nets_pins.get(n, ()) if isinstance(p, (list, tuple)) and len(p) > 1]

    def of(net):
        mate = mates.get(net)
        if net in rf or mate in rf or RV.is_rf(net, pins(net)) or (mate and RV.is_rf(mate, pins(mate))):
            return "rf", mate
        if mate:
            master = net if net in masters else mate
            return ("hs-pair" if RV.pair_gated(cons, master) else "pair"), mate
        return ("power" if net in pw else "plain"), None
    return of


@detector("ghost-open", "ghost-costly", "ghost-hot")
def detect_ghost(ctx):
    """From the judge blame (route_review on the KRT ghost copy): one gap per open net, a pair is one gap (either leg open)."""
    bl = ctx.get("blame")
    if bl is None:
        return None                                             # not measured at this stage (no judge ran)
    of = _net_class(ctx)
    out, seen = [], set()
    for o in bl.get("open", []):
        cls, mate = of(o["net"])
        legs = sorted({o["net"], mate} - {None}) if cls in ("hs-pair", "pair") else [o["net"]]
        name = "/".join(legs)
        if name in seen:
            continue
        seen.add(name)
        out.append(gap("ghost-open", name, cls, len(o.get("refs") or []) or 1, o.get("refs") or (), legs,
                       detail=f"ghost-unrouted, parts {','.join(o.get('refs') or [])}"))
    for c in bl.get("costly", []):
        out.append(gap("ghost-costly", c["net"], of(c["net"])[0], c.get("detour", 0.0), c.get("refs") or (), [c["net"]],
                       detail=f"{c.get('vias', 0)} vias, detour {c.get('detour', 0.0):.1f}"))
    for h in bl.get("hot", []):
        bb = h.get("bbox") or [0, 0, 0, 0]
        out.append(gap("ghost-hot", "{:.0f},{:.0f}".format(bb[0], bb[1]), "plain", h.get("occ", 0.0), h.get("refs") or (), h.get("nets") or (),
                       at=((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2), detail=f"occupancy {h.get('occ', 0.0):.2f}, {h.get('cells', 0)} cells"))
    return out


def row_kind(r, intent=False):
    """Row -> gap kind (plan section 5). Budget rows: kind / metric / name; intent rows: kind."""
    k, name, metric = r.get("kind"), str(r.get("name")), r.get("metric")
    if intent:
        return {"edge": "row-edge", "away": "row-away", "region": "row-away"}.get(k, "row-other")
    if k == "support" and metric == "template":
        return "row-cin-side" if "cin_side" in name else "row-template"
    if metric == "pin-bypass":
        return "row-pin-bypass"
    return {"separation": "row-away", "keepout-flight": "row-flight", "rail": "row-rail"}.get(k, "row-other")


SOFT_BUDGET_MIN = 10.0                 # [placegaps] soft_budget_min: soft rows are only worked while the loop has run less than this
RAIL_TOLERANCE = 0.10                  # a rail reach overage within the model's error is a soft row


def rail_tolerance(cons):
    try:
        return float(((cons or {}).get("placegaps") or {}).get("rail_tolerance", RAIL_TOLERANCE))
    except (TypeError, ValueError):
        return RAIL_TOLERANCE


def _excess(r):
    v, lim = r.get("value"), r.get("limit") if "limit" in r else (r.get("max") if r.get("max") is not None else r.get("target"))
    try:
        v, lim = float(v), float(lim)
        return abs(v - lim) / abs(lim) if lim else abs(v - lim)
    except (TypeError, ValueError):
        return 1.0


@detector(*(k for k in ORDER if k.startswith("row-")))
def detect_rows(ctx):
    """A gap per hard row not met: budget rows (verdict fail) and intent rows (met False). Other-kind rows keep their own kind."""
    out, sep_b, sep_i = [], {}, {}                         # separation rows by name: budget table / intent check (one gap per named row)
    far = {f["ref"] for f in ctx.need("_far", lambda: far_supports(ctx)) or []}
    for r in ctx.rows() or []:
        if r.get("hard") and r.get("verdict") == "fail":
            if r.get("kind") == "support" and r.get("metric") == "distance" and (r.get("refs") or [None])[0] in far:
                continue                                        # graded as a support-far gap (which carries this row)
            soft = r.get("kind") == "rail" and _excess(r) <= rail_tolerance(ctx.get("cons"))      # inside the model's error: soft, never outranks a hard row
            out.append(gap(row_kind(r), f"{r['name']}|{r.get('metric')}", "plain", _excess(r), r.get("refs") or (), r.get("nets") or (),
                           row=None if soft else f"{r['kind']}:{r['name']}",
                           detail=f"{r.get('metric')} {r.get('value')} limit {r.get('max') if r.get('max') is not None else r.get('target')}"
                                  + (" (soft: within the rail model error)" if soft else "")))
            if soft:
                out[-1]["soft"] = True
            if r.get("kind") == "separation":
                sep_b[str(r["name"])] = out[-1]
    for r in ctx.intent_rows() or []:
        if r.get("hard") and r.get("met") is False and r.get("kind") != "support":      # support rows: the support-far gaps carry them
            out.append(gap(row_kind(r, True), r["name"], "plain", _excess(r), r.get("refs") or (), (), row=f"intent:{r['kind']}:{r['name']}",
                           detail=f"{r['kind']} {r.get('value')} limit {r.get('limit')}; {r.get('detail', '')}"))
            if r.get("kind") == "away" and str(r["name"]).startswith("sep:"):
                sep_i[str(r["name"])[4:]] = out[-1]
    drop = set()
    for n, gi in sep_i.items():                      # the same [[separation]] seen twice: keep the copy that names the offender (ties: the budget row)
        gb = sep_b.get(n)
        if gb is not None:
            drop.add(id(gi) if gb["refs"] or not gi["refs"] else id(gb))
    return [g for g in out if id(g) not in drop]


def far_supports(ctx):
    """[{ref, ic, pad, dist, limit, kind}]: the rule of intent.support_rows, one entry per graded support beyond its limit
    (phase 4 moves this into clusters.far_supports; one source then)."""
    from . import fluxmap as FM, intent as IT
    it = IT.items(ctx.B(), ctx["cons"])
    near = {r for n in it["near"] for r in (n.get("chain") or n.get("refs") or ())}
    out = []
    for c in ctx.clusters():
        for s in c["supports"]:
            if s["ref"] in it["support"]["waive"] or s["ref"] in near or (not s["sure"] and s["how"] != "spread"):
                continue
            if s["dist"] > s["limit"] + 1e-6:
                out.append({"ref": s["ref"], "ic": c["ic"], "pad": s["pad"], "dist": s["dist"], "limit": s["limit"],
                            "kind": "decap" if FM._kind(s["ref"], s["cls"]) == "decap" else "other"})
    return out


@detector("support-far")
def detect_support_far(ctx):
    fs = ctx.need("_far", lambda: far_supports(ctx))
    if fs is None:
        return None
    B = ctx.B()
    hard = {(r.get("refs") or [None])[0]: f"support:{r['name']}" for r in ctx.rows() or []
            if r.get("hard") and r.get("verdict") == "fail" and r.get("kind") == "support" and r.get("metric") == "distance"}
    return [gap("support-far", f["ref"], f["kind"], f["dist"] - f["limit"], (f["ref"], f["ic"]), (), at=B["parts"][f["ref"]]["pos"],
                row=hard.get(f["ref"]), detail=f"{f['kind']} {f['dist']:.1f} mm of {f['ic']}.{f['pad']}, limit {f['limit']:g}") for f in fs]


def _free_area(B, ic, ring, skip):
    """Free area (mm2) of the square ring around `ic`'s courtyard, grown by `ring` mm, inside the board bbox, not under another
    part's courtyard on the IC's side (0.5 mm raster)."""
    import numpy as np
    x0, y0, x1, y1 = B["parts"][ic]["court"]
    x0, y0, x1, y1 = x0 - ring, y0 - ring, x1 + ring, y1 + ring
    bx0, by0, bx1, by1 = B["bbox"]
    step = 0.5
    nx, ny = max(1, int((x1 - x0) / step)), max(1, int((y1 - y0) / step))
    xs, ys = x0 + (np.arange(nx) + 0.5) * step, y0 + (np.arange(ny) + 0.5) * step
    free = ((xs >= bx0) & (xs <= bx1))[None, :] & ((ys >= by0) & (ys <= by1))[:, None]
    ix0, iy0, ix1, iy1 = B["parts"][ic]["court"]
    free &= ~(((xs >= ix0) & (xs <= ix1))[None, :] & ((ys >= iy0) & (ys <= iy1))[:, None])
    side = B["parts"][ic]["side"]
    for ref, p in B["parts"].items():
        if ref in skip or ref == ic or p["side"] != side:
            continue
        a, b, c, d = p["court"]
        free &= ~(((xs >= a) & (xs <= c))[None, :] & ((ys >= b) & (ys <= d))[:, None])
    return float(free.sum()) * step * step


@detector("support-starved")
def detect_support_starved(ctx):
    """An IC whose ring (courtyard grown by the largest limit among its far supports) has less free area than the far supports'
    courtyards add up to (packing factor 1.0 in phase 0: the measured factor arrives with the rung that needs it)."""
    fs = ctx.need("_far", lambda: far_supports(ctx))
    if fs is None:
        return None
    B, out, by = ctx.B(), [], {}
    for f in fs:
        by.setdefault(f["ic"], []).append(f)
    for ic, lst in sorted(by.items()):
        need = sum((lambda c: (c[2] - c[0]) * (c[3] - c[1]))(B["parts"][f["ref"]]["court"]) for f in lst)
        have = _free_area(B, ic, max(f["limit"] for f in lst), {f["ref"] for f in lst})
        if have < need:
            out.append(gap("support-starved", ic, "plain", need - have, [ic] + [f["ref"] for f in lst], (), at=B["parts"][ic]["pos"],
                           detail=f"needs {need:.0f} mm2 within {max(f['limit'] for f in lst):g} mm, free {have:.0f} mm2, {len(lst)} far"))
    return out


@detector("net-budget")
def detect_net_budget(ctx):
    """The pre-route gate's NET_OVER_BUDGET FAIL as a placement gap (B10): a net whose pad MST is over its class budget while moving
    parts could still fix it (a floor made of fixed parts alone is ANCHOR_SPACING, an owner question, not a gap). mag = mm over."""
    from . import netclass as NC, preroute as PR
    B = ctx.B()
    cls = NC.classify(B, ctx["cons"], ctx.get("spec"))
    of = _net_class(ctx)
    out = []
    for f in PR.net_over_budget(B, cls, ctx["cons"]):
        if f["level"] == "FAIL":
            out.append(gap("net-budget", f["net"], of(f["net"])[0], f["value"] - f["limit"], f["refs"], (f["net"],), detail=f["msg"]))
    return out


@detector("side-illegal")
def detect_side_illegal(ctx):
    """A back part taller than [placement] back_max_height_mm, or standing in a back-closed keep-out. (back_ok eligibility by
    policy arrives with phase 9; a back part of unknown height is counted in the detail, not as a gap.)"""
    from . import geom as GM, intent as IT, legalise as LGS
    B, cons = ctx.B(), ctx["cons"]
    lim = ((cons.get("placement") or {}).get("back_max_height_mm"))
    kos = [k for k in LGS.keepout_rects(B, cons) if "B" in k[1]]
    out, unknown = [], 0
    for ref, p in sorted(B["parts"].items()):
        if p["side"] == "F.Cu":
            continue
        why = []
        h = IT.height(B, cons, ref)
        if lim is not None and h is not None and h > float(lim) + 1e-9:
            why.append(f"{h:g} mm over back_max_height_mm {float(lim):g}")
        unknown += lim is not None and h is None
        for rect, _s, allow, _mh, name in kos:
            if ref not in allow and GM.overlap(rect, p["court"], -0.02):
                why.append(f"in back-closed area {name}")
        if why:
            out.append(gap("side-illegal", ref, "plain", 1.0 + (h or 0), (ref,), (), at=p["pos"], detail="; ".join(why)))
    if unknown and out:
        out[0]["detail"] += f"; {unknown} back part(s) of unknown height"
    return out


@detector("part-datasheet", "part-stock")
def detect_parts(ctx):
    """From partsgate.gate: needs datasheet -> part-datasheet; stock code PART_NO_STOCK -> part-stock (STOCK_UNCHECKED is a
    distributor-offline state, counted in `unchecked`, not a gap)."""
    g = ctx.get("parts_gate")
    if g is None:
        return None
    out = []
    if any("stock" in u for u in g.get("unchecked") or ()) and not any(
            i.get("code") == "PART_NO_STOCK" for v in (g.get("parts") or {}).values() for i in v):
        ctx.setdefault("errors", {})["part-stock"] = "stock NOT CHECKED (offline / no distributor data): not counted as zero"
        ctx["_stock_unchecked"] = True
    for ref, items in sorted((g.get("parts") or {}).items()):
        for i in items:
            if i["need"] == "datasheet":
                out.append(gap("part-datasheet", ref, "plain", 1, (ref,), (), detail=i.get("detail", "")))
            elif i["need"] == "stock" and i.get("code") == "PART_NO_STOCK":
                out.append(gap("part-stock", ref, "plain", 1, (ref,), (), detail=i.get("detail", "")))
    return out


# ------------------------------------------------------------------ detect (every kind in scope; a failed detector is reported, never a silent zero)
def detect(ctx, kinds=None):
    """-> {"gaps": [...], "counts": {kind: n | None}, "classes": {kind: {class: n}}, "errors": {kind: text}}.
    None = not measured here (no input for it); an error is a detector that raised."""
    kinds = [k for k in ORDER if kinds is None or k in kinds]
    gaps, counts, errors, done = [], {}, {}, {}
    for k in kinds:
        fn = DETECT[k]
        if fn not in done:
            try:
                got = fn(ctx)
                done[fn] = None if got is None else list(got)
            except Exception as e:                          # noqa: BLE001
                done[fn] = None
                errors[k] = f"{type(e).__name__}: {e}"
        mine = None if done[fn] is None or (k == "part-stock" and ctx.get("_stock_unchecked")) else [g for g in done[fn] if g["kind"] == k]
        counts[k] = None if mine is None else len(mine)
        gaps += mine or []
        if k not in errors and mine is None:
            src = [e for e in ((ctx.get("errors") or {}).values())]
            if src:
                errors[k] = src[0]
    cls = {}
    for g in gaps:
        cls.setdefault(g["kind"], {}).setdefault(g["class"], 0)
        cls[g["kind"]][g["class"]] += 1
    return {"gaps": _sort(gaps), "counts": counts, "classes": cls, "errors": errors}


def merge(*results):
    """Union of detect results (kinds measured by different stage calls); a kind keeps the first measured count."""
    out = {"gaps": [], "counts": {}, "classes": {}, "errors": {}}
    for r in results:
        for k, n in (r.get("counts") or {}).items():
            if out["counts"].get(k) is None and n is not None:
                out["counts"][k] = n
                out["gaps"] += [g for g in r["gaps"] if g["kind"] == k]
                out["classes"][k] = (r.get("classes") or {}).get(k, {})
            else:
                out["counts"].setdefault(k, n)
        out["errors"].update(r.get("errors") or {})
    out["gaps"] = _sort(out["gaps"])
    return out


# ------------------------------------------------------------------ score, accept, memo
def score_terms(gaps, state=None):
    """Named score terms (smaller is better, lexicographic in TERMS order). `state` carries what gaps cannot: illegal (new legality
    issues vs the input baseline) and the judge key terms open / hot / hot_occ / vias / detour (carried unchanged by PlanEval)."""
    state = state or {}
    n = lambda pred: sum(1 for g in gaps if pred(g))
    rows = [g for g in gaps if g["row"]]                       # hard rows not met, whatever the kind that carries them
    far = [g for g in gaps if g["kind"] == "support-far"]
    t = {"hs_open": n(lambda g: g["kind"] == "ghost-open" and g["class"] == "hs-pair"),
         "rf_open": n(lambda g: g["kind"] == "ghost-open" and g["class"] == "rf"),
         "power_open": n(lambda g: g["kind"] == "ghost-open" and g["class"] == "power"),
         "illegal": int(state.get("illegal", 0)), "hard_rows": len(rows), "hard_mag": round(sum(g["mag"] for g in rows if g["kind"].startswith("row-")), 3),
         "decap_far": sum(1 for g in far if g["class"] == "decap"), "support_far": sum(1 for g in far if g["class"] != "decap"),
         "far_mm": round(sum(g["mag"] for g in far), 3)}
    for k in ("open", "hot", "hot_occ", "vias", "detour"):
        t[k] = state.get(k, 0)
    return t


def score(gaps, state=None):
    return [score_terms(gaps, state)[k] for k in TERMS]


def _accept(base, cand, target=None):
    """The one accept rule, every kind. base / cand: {"gaps": [...], "state": {...}, "refusal": None | "legal:..."}.
    -> (ok, check, why). check names the refusing check: legal:<..> | drc:<..> | row:<kind>:<name> | score:<term> | judge:<class> open."""
    if cand.get("refusal"):
        r = str(cand["refusal"])
        return False, r.split(" ", 1)[0] if r.startswith(("legal", "drc")) else r, r
    tb, tc = score_terms(base["gaps"], base.get("state")), score_terms(cand["gaps"], cand.get("state"))
    if tc["illegal"] > 0:
        return False, "legal:new", f"{tc['illegal']} new legality issue(s) against the input"
    for t in R6_TERMS:
        if tc[t] > tb[t]:
            check = {"hs_open": "judge:hs-pair open", "rf_open": "judge:rf open", "power_open": "judge:power open"}.get(t, f"score:{t}")
            if t == "hard_rows":
                lost = sorted({g["row"] for g in base["gaps"] if g["row"]} - {g["row"] for g in cand["gaps"] if g["row"]})
                new = sorted({g["row"] for g in cand["gaps"] if g["row"]} - {g["row"] for g in base["gaps"] if g["row"]})
                check = "row:" + (new[0] if new else "?") if new else check
            return False, check, f"{t} worse {tb[t]:g}->{tc[t]:g}"
    if target and str(target).startswith("ghost-open:") and target in {g["id"] for g in cand["gaps"]}:
        return False, "judge:target open", f"{target} is still open in the judge (a ghost-open repair counts only when its target routes)"
    if target and str(target).startswith(TARGET_MUST_GAIN):       # a template / bypass repair counts only when its own row got closer (served pairs, loop, bare pin)
        m0 = next((g["mag"] for g in base["gaps"] if g["id"] == target), None)
        m1 = next((g["mag"] for g in cand["gaps"] if g["id"] == target), None)
        if m0 is not None and m1 is not None and m1 >= m0 - 1e-9:
            return False, f"score:{target.split(':', 1)[0]}", f"{target} not closer: mag {m0:g}->{m1:g}"
    mb0 = {g["id"]: g["mag"] for g in base["gaps"] if g["row"]}
    worse = sorted(g["id"] for g in cand["gaps"] if g["row"] and g["id"] != target and g["id"] in mb0 and g["mag"] > mb0[g["id"]] + 1e-9)
    if worse:                                        # a row that already fails must not fail harder because another was repaired
        r = next(g for g in cand["gaps"] if g["id"] == worse[0])
        return False, f"row:{r['row']}", f"{r['row']} worse: mag {mb0[worse[0]]:g}->{r['mag']:g}"
    newrow = sorted({g["row"] for g in cand["gaps"] if g["row"]} - {g["row"] for g in base["gaps"] if g["row"]})
    if newrow:                                       # a repair that breaks another hard row is refused whatever it gained
        return False, f"row:{newrow[0]}", f"hard row {newrow[0]} newly not honoured"
    sb, sc = score(base["gaps"], base.get("state")), score(cand["gaps"], cand.get("state"))
    if sc < sb:
        return True, None, "score lower"
    if sc == sb and target is not None:
        mb = {g["id"]: g["mag"] for g in base["gaps"]}
        mc = {g["id"]: g["mag"] for g in cand["gaps"]}
        if target in mb and mc.get(target, 0.0) < mb[target]:
            return True, None, f"target {target} mag {mb[target]:g}->{mc.get(target, 0.0):g}"
    first = next((t for t, a, b in zip(TERMS, sb, sc) if b > a), "no-gain")
    return False, f"score:{first}", "no gain"


def pose_hash(B, g, r=WINDOW_MM):
    """crc32 of the poses (ref, x, y, angle, side) of every part within `r` mm of the gap: its refs, else its position."""
    parts = B["parts"]
    pts = [parts[x]["pos"] for x in g.get("refs") or () if x in parts] or ([tuple(g["at"])] if g.get("at") else [])
    near = sorted(ref for ref, p in parts.items() if any(math.hypot(p["pos"][0] - q[0], p["pos"][1] - q[1]) <= r + 5.0 for q in pts)
                  or ref in (g.get("refs") or ()))
    s = ";".join(f"{ref},{parts[ref]['pos'][0]:.3f},{parts[ref]['pos'][1]:.3f},{parts[ref]['angle']:.1f},{parts[ref]['side']}" for ref in near)
    return zlib.crc32(s.encode())


def seed(g, stage_seed=0):
    return zlib.crc32(g["id"].encode()) ^ int(stage_seed)


# ------------------------------------------------------------------ close loop
def _narr(stage, text, phase="doing"):
    try:
        from . import liveview as LV
        LV.narrate(stage, text, phase)
    except Exception:                                   # noqa: BLE001 - narration never fails a stage
        pass


def narration(res, stage):
    """One plain line per call from the data (never a literal list of kinds)."""
    c = {k: n for k, n in res["counts"].items() if n}
    if not c:
        return f"{stage}: no placement gaps"
    return f"{stage}: placement gaps " + ", ".join(f"{n} {k}" for k, n in c.items())


class Evaluator:
    """What `close` needs from a stage (PlanEval / JudgeEval / PartsEval, phases 2-4). Phase 0 ships the interface only."""
    def detect(self, kinds):                            # -> [gap] on the current state
        raise NotImplementedError

    def state(self):                                    # -> {"illegal": n, "open": .., ...} carried terms
        return {}

    def trial(self, cand):                              # -> {"gaps": [...], "state": {...}, "refusal": None | str, "token": any}
        raise NotImplementedError

    def commit(self, trial):                            # make the trial the base
        raise NotImplementedError

    def trials(self, cands):                            # a batch of candidates (JudgeEval judges them in parallel) -> [trial] in order
        return [self.trial(c) for c in cands]

    def pose_key(self, g):                              # -> hashable of the poses around the gap (memo)
        return pose_hash(self.board(), g)

    def board(self):
        raise NotImplementedError


def close(ctx, kinds, ev, stage="placegaps", clock=time.time, budget_min=None):
    """rounds x kinds (ORDER) x rungs (yield order) x gaps (worst first). -> {stop, accepted, refused, gaps_in, gaps_out, score_in,
    score_out, rounds}. Stops: no gap in scope, no rung, no gain N rounds, stalled, budget, rung cap, max rounds (a time stop is
    named; it is the one non-deterministic outcome)."""
    from . import gaps as G
    cons = ctx.get("cons") or {}
    flow = cons.get("flow") or {}
    no_gain = int(flow.get("no_gain_rounds", G.NO_GAIN_ROUNDS))
    stall_s = float(flow.get("stall_min", G.STALL_MIN)) * 60
    budget_s = float(flow.get("budget_min") or 0) * 60
    own_s = float(budget_min) * 60 if budget_min else 0.0                 # this loop's own wall budget (placetour: [placetour] ladder_budget_min)
    max_rounds = int((cons.get("placegaps") or {}).get("max_rounds", MAX_ROUNDS))
    soft_s = float((cons.get("placegaps") or {}).get("soft_budget_min", SOFT_BUDGET_MIN)) * 60      # soft rows are only worked while elapsed < this
    cap_s = G._rung_cap(cons)
    t0 = last = clock()
    allow = (cons.get("placegaps") or {}).get("kinds")
    kinds = [k for k in ORDER if k in kinds and (allow is None or k in allow)]
    base = {"gaps": _sort(ev.detect(kinds)), "state": ev.state(), "refusal": None}
    out = {"stop": "", "accepted": [], "refused": [], "gaps_in": list(base["gaps"]), "score_in": score(base["gaps"], base["state"]), "rounds": 0}
    memo, yields, stale = set(), {}, 0
    _narr(stage, narration({"counts": {k: sum(1 for g in base["gaps"] if g["kind"] == k) for k in kinds}}, stage), "start")
    for r in range(1, max_rounds + 1):
        if not base["gaps"]:
            out["stop"] = "no gap in scope"
            break
        if not any(DISPATCH.get(g["kind"]) for g in base["gaps"]):
            out["stop"] = "no rung for the gaps in scope"
            break
        out["rounds"] = r
        gain, tried = False, 0
        for kind in kinds:
            rungs = [x for x in DISPATCH.get(kind, []) if x in RUNGS]
            for rung in G._rank(kind, rungs, yields):
                if own_s and clock() - t0 > own_s:
                    out["stop"] = "ladder budget (time stop)"
                    break
                t_rung = clock()
                for g in [x for x in base["gaps"] if x["kind"] == kind]:
                    if g["id"] not in {x["id"] for x in base["gaps"]}:
                        continue                        # an earlier accept of this rung closed it
                    if g.get("soft") and soft_s and clock() - t0 > soft_s:
                        out["soft_skips"] = out.get("soft_skips", 0) + 1       # a soft row (inside the model error) never spends hard-row time
                        continue
                    if own_s and clock() - t0 > own_s:
                        out["stop"] = "ladder budget (time stop)"
                    elif budget_s and clock() - t0 > budget_s:
                        out["stop"] = "flow budget_min (time stop)"
                    elif clock() - last > stall_s:
                        out["stop"] = "stalled (time stop)"
                    elif clock() - t_rung > cap_s:
                        out["refused"].append({"gap": g["id"], "rung": rung, "check": "cap", "why": "rung cap (time stop)"})
                        break
                    if out["stop"]:
                        break
                    key = (rung, kind, g["id"], ev.pose_key(g))
                    if key in memo:
                        out["memo_skips"] = out.get("memo_skips", 0) + 1       # counted, not listed: it adds no information
                        continue
                    memo.add(key)
                    tried += 1
                    cands = sorted(RUNGS[rung](g, ctx) or [], key=lambda c: c["id"])[:CANDS_PER_RUNG]
                    if not cands and kind.startswith("row-") and rung not in DIAG_RUNGS:           # a leftover row is never silent: the rung says why it had nothing to try
                        out["refused"].append({"gap": g["id"], "rung": rung, "check": "no-candidate",
                                               "why": (ctx.get("why_none") or {}).get((rung, g["id"])) or "the rung proposed no move"})
                    winner = None
                    for cand, tr in zip(cands, ev.trials(cands) if cands else []):
                        ok, check, why = _accept(base, tr, g["id"])
                        yields.setdefault(kind, {}).setdefault(rung, [0, 0])
                        yields[kind][rung][0] += 1
                        if ok:
                            if winner is None or score(tr["gaps"], tr.get("state")) < score(winner[1]["gaps"], winner[1].get("state")):
                                winner = (cand, tr, why)
                            continue
                        out["refused"].append({"gap": g["id"], "rung": rung, "candidate": cand["id"], "check": check, "why": why})
                    if winner:                          # the best acceptable candidate of this rung (ties: lowest id) becomes the base
                        cand, tr, why = winner
                        yields[kind][rung][1] += 1
                        n_before = len(base["gaps"])
                        ev.commit(tr)
                        base = {"gaps": _sort(tr["gaps"]), "state": tr.get("state") or {}, "refusal": None}
                        out["accepted"].append({"gap": g["id"], "rung": rung, "candidate": cand["id"], "why": why,
                                                "refs": sorted(cand.get("moves") or {}), "gaps_before": n_before, "gaps_after": len(base["gaps"]),
                                                "moves": {r: [round(float(x), 3) for x in v[:3]] for r, v in (cand.get("moves") or {}).items()},
                                                "removed_refs": sorted(tr.get("removed_refs") or [])})
                        EV.emit("parts", stage=stage, i=r, refs=sorted(cand.get("moves") or {}), why=f"{kind}/{rung}")
                        _narr(stage, f"{stage}: {kind} {g['id'].split(':', 1)[1]} repaired by {rung}")
                        gain, last = True, clock()
                if out["stop"]:
                    break
            if out["stop"]:
                break
        sc = score(base["gaps"], base["state"])
        EV.iteration("place", r, n=max_rounds, metrics=dict(zip(TERMS, sc)), note="placegaps")
        stale = 0 if gain else stale + 1
        if out["stop"]:
            break
        if not tried and not gain:
            out["stop"] = "nothing new to try (every rung ran on these poses)"
            break
        if stale >= no_gain:
            out["stop"] = f"no gain for {stale} round(s)"
            break
    out["stop"] = out["stop"] or "max_rounds"
    out.update(gaps_out=list(base["gaps"]), score_out=score(base["gaps"], base["state"]))
    try:
        cin_questions(ctx, out)
    except Exception as e:                              # noqa: BLE001 - a question that cannot be asked never fails the loop
        ctx.setdefault("errors", {})["cin_questions"] = f"{type(e).__name__}: {e}"
    _narr(stage, f"{stage}: placement gaps {len(out['gaps_in'])} -> {len(out['gaps_out'])}, {len(out['accepted'])} repaired ({out['stop']})", "end")
    return out


# ------------------------------------------------------------------ report files, table, stage entry
def table(res, title="placegaps"):
    """Text table: every kind in ORDER with its count (- = not measured in this stage call, ERR = detector raised) and class split."""
    w = max(len(k) for k in ORDER)
    lines = [f"{title}: placement gaps (detect-only; rungs arrive with later phases)", f"  {'kind'.ljust(w)}  count  classes / detail"]
    for k in ORDER:
        n = (res.get("counts") or {}).get(k, None)
        txt = "ERR" if k in (res.get("errors") or {}) and n is None else "-" if n is None else str(n)
        cl = ", ".join(f"{c} {v}" for c, v in sorted((res.get("classes") or {}).get(k, {}).items(), key=lambda t: CLASS_RANK.get(t[0], 9)))
        err = (res.get("errors") or {}).get(k)
        lines.append(f"  {k.ljust(w)}  {txt.rjust(5)}  {cl}{'  ' + err[:90] if err and n is None else ''}")
    return "\n".join(lines)


def stage_line(res):
    c = [(k, n) for k, n in (res.get("counts") or {}).items() if n]
    return f"gaps {sum(n for _k, n in c)} in {len(c)} kinds: " + " ".join(f"{k} {n}" for k, n in c) if c else "gaps 0"


def _ledger(res, stage):
    rows = []
    for k in ORDER:
        n = (res.get("counts") or {}).get(k)
        if n:
            ex = "; ".join(g["id"].split(":", 1)[1] for g in res["gaps"] if g["kind"] == k)[:160]
            rows.append(f"| {stage}: {n} x {k} ({ex}) | measured from the stage data | none yet (placegaps phase 0 is detect-only) | not repaired |")
    for a in res.get("accepted") or []:
        rows.append(f"| {stage}: {a['gap']} | {a.get('why', '')} | rung {a['rung']} ({a['candidate']}; moved {','.join(a.get('refs') or [])}) | accepted: gaps {a.get('gaps_before')} -> {a.get('gaps_after')} |")
    for r in (res.get("refused") or [])[:40]:
        rows.append(f"| {stage}: {r['gap']} | refused by {r['check']} | rung {r['rung']} {r.get('candidate', '')} | {str(r.get('why', ''))[:90]} |")
    return "| issue | cause | fix | measured result |\n|---|---|---|---|\n" + "\n".join(rows) + "\n"


def write_reports(res, outdir, stage, base="placegaps"):
    """<base>.json (gaps, counts, attempts), <base>.md (table + attempts), ledger rows. Never raises. -> json path or None."""
    try:
        os.makedirs(outdir, exist_ok=True)
        pj = os.path.join(outdir, base + ".json")
        body = {"stage": stage, "counts": res["counts"], "classes": res.get("classes"), "errors": res.get("errors"), "gaps": res["gaps"],
                "accepted": res.get("accepted", []), "refused": res.get("refused", [])}
        with open(pj + ".tmp", "w") as fh:
            json.dump(body, fh, indent=1, default=list)
        os.replace(pj + ".tmp", pj)
        md = os.path.join(outdir, base + ".md")
        with open(md, "w") as fh:
            fh.write("```\n" + table(res, stage) + "\n" + diagnosis_table(res["gaps"]) + "\n```\n\n| gap | class | mag | detail |\n|---|---|---|---|\n")
            fh.write("\n".join(f"| {g['id']} | {g['class']} | {g['mag']:g} | {g['detail']} |" for g in res["gaps"]) + "\n")
        with open(os.path.join(outdir, "ledger-rows.md" if base == "placegaps" else base + ".ledger-rows.md"), "w") as fh:
            fh.write(_ledger(res, stage))
        EV.artifact("report", md, "placegaps")
        return pj
    except Exception:                                   # noqa: BLE001
        return None


def detect_stage(stage, board, cons, spec, outdir, base="placegaps", ds_dir=None, blame=None, parts_gate=None, kinds=None, printer=print,
                 confirm=None, attempts=None):
    """The one call the stages make. Detect-only: reads the board and stage data, writes report files into `outdir`, prints the
    table. Never writes a board, never raises. A later step that fails (diagnosis, reports, printing) is recorded in `errors`
    and keeps the gaps already found. -> detect result (+ "line", "report")."""
    try:
        ctx = Ctx(stage=stage, board=board, cons=cons or {}, spec=spec, ds_dir=ds_dir, blame=blame, parts_gate=parts_gate)
        _narr(stage, f"{stage}: measuring placement gaps", "start")
        res = detect(ctx, kinds or SCOPE.get(stage))
    except Exception as e:                              # noqa: BLE001 - detect-only must never fail a stage
        return {"gaps": [], "counts": {}, "classes": {}, "errors": {"*": f"{type(e).__name__}: {e}"}, "line": "gaps not measured", "report": None}
    if blame is not None:
        try:
            diagnose(ctx, res["gaps"], confirm)         # reads the board, may run `confirm` (a router on a scratch copy); writes no board
        except Exception as e:                          # noqa: BLE001 - the gaps stay; an undiagnosed critical gap reads "unconfirmed"
            res["errors"]["diagnose"] = f"{type(e).__name__}: {e}"
            for g in res["gaps"]:
                if g["kind"] == "ghost-open":
                    g.setdefault("cause", "unconfirmed")
                    g.setdefault("blockers", [])
        res["errors"].update({k: v for k, v in (ctx.get("errors") or {}).items() if k in ("confirm", "routediag", "corridor")})
    if attempts:                                        # what the close loop did before this measurement: into placegaps.json / the ledger rows
        res.update(accepted=attempts.get("accepted", []), refused=attempts.get("refused", []))
    for step in (lambda: res.update(line=stage_line(res)),
                 lambda: res.update(report=write_reports(res, outdir, stage, base)),
                 lambda: _narr(stage, narration(res, stage), "end"),
                 lambda: printer and printer(table(res, stage) + ("\n" + diagnosis_table(res["gaps"]) if blame is not None else ""))):
        try:
            step()
        except Exception as e:                          # noqa: BLE001
            res["errors"]["report"] = f"{type(e).__name__}: {e}"
    res.setdefault("line", "gaps ?")
    res.setdefault("report", None)
    return res


# ------------------------------------------------------------------ phase 1: class terms of a judge, diagnosis of a ghost-open gap
def open_terms(board, blame, cons, spec):
    """(hs_open, rf_open, power_open) of a judge blame on `board`: the same gaps `detect_ghost` makes (a pair is one gap)."""
    ctx = Ctx(stage="placetour", board=board, cons=cons or {}, spec=spec, blame=blame)
    t = score_terms(detect_ghost(ctx) or [])
    return t["hs_open"], t["rf_open"], t["power_open"]


def critical(gaps):
    """The ghost-open gaps that fail a placement stage: an hs pair or an rf net unrouted."""
    return [g for g in gaps if g["kind"] == "ghost-open" and g["class"] in ("hs-pair", "rf")]


def _rules(ctx, g):
    """-> (track mm, clearance mm, width mm of the corridor): 2w + gap + 2 clr for a pair (family numbers from [pairs.*]), else w + 2 clr."""
    from . import budget as BG, routerules as RR
    R = RR.rules(ctx["cons"], None, B=ctx.B())
    w, clr = float(R["track"]), float(R["clearance"])
    if g["class"] in ("hs-pair", "pair"):
        fams = ctx["cons"].get("pairs") or {}
        fam = BG.family_of(fams, g["nets"][0]) if g["nets"] else None
        f = fams.get(fam) or {}
        w, gap_mm = float(f.get("width_mm", w)), float(f.get("gap_mm", clr))
        return w, clr, 2 * w + gap_mm + 2 * clr
    return w, clr, w + 2 * clr


def corridor(ctx, g):
    """Flight lines (MST over the pads of the gap's nets, only the edges that reach a disconnected part) as rects of the derived
    width -> {"rects": [...], "len_mm": float, "width_mm": float}."""
    from . import budget as BG, escreserve as ES
    B = ctx.B()
    _w, _c, width = _rules(ctx, g)
    rects, total, edges = [], 0.0, []
    for net in g["nets"]:
        pts = {(p["ref"], p["num"]): (p["x"], p["y"]) for p in B["pads"] if p["net"] == net}
        for a, b, d in BG.mst_edges(pts):
            if g["refs"] and a[0] not in g["refs"] and b[0] not in g["refs"]:
                continue
            total += d
            edges.append((pts[a], pts[b]))
            rects += ES.seg_rects(pts[a], pts[b], width)
    return {"rects": rects, "len_mm": round(total, 2), "width_mm": round(width, 3), "edges": edges}


def blockers(ctx, g, corr):
    """Parts whose courtyard cuts a corridor rect on a side an endpoint pad is on (or through-hole), endpoints excluded, smallest
    courtyard first -> [{"ref", "movable", "area"}]."""
    from . import geom as GM, placetour as PT
    B = ctx.B()
    ends = {p["ref"] for p in B["pads"] if p["net"] in g["nets"]}
    sides = {B["parts"][r]["side"] for r in ends if r in B["parts"]}
    mov = set(PT._movable(B, ctx["cons"], PT.params(ctx["cons"])))
    out = {}
    for ref, p in B["parts"].items():
        if ref in ends or not (p["side"] in sides or p.get("tht")):
            continue
        if any(GM.overlap(r, p["court"], -0.02) for r in corr["rects"]):
            c = p["court"]
            out[ref] = {"ref": ref, "movable": ref in mov, "area": round((c[2] - c[0]) * (c[3] - c[1]), 2)}
    return sorted(out.values(), key=lambda x: (x["area"], x["ref"]))


def diagnose(ctx, gaps, confirm=None):
    """Fills cause / blockers / routediag causes / corridor on every ghost-open gap; no board change.
    confirm(nets) -> set of those nets still open when routed ALONE on the placed board (None: not run).
      routed alone but open in the judge  -> cause "order" (room exists; the competition for it lost; ladder starts at `reserve`)
      open alone too                       -> cause "blocked" (the placement leaves no way; ladder starts at `unblock`)"""
    from . import routediag as RDG
    todo = [g for g in gaps if g["kind"] == "ghost-open"]
    crit = [g for g in todo if g["class"] in ("hs-pair", "rf", "power")]
    still = None
    if confirm is not None and crit:
        try:
            still = confirm(sorted({n for g in crit for n in g["nets"]}))
        except Exception as e:                              # noqa: BLE001
            ctx.setdefault("errors", {})["confirm"] = f"{type(e).__name__}: {e}"
    try:
        B = ctx.B()
        rd = {n["net"]: n["causes"] for n in RDG.diagnose(B, sorted({n for g in todo for n in g["nets"]}), ctx["cons"], ctx["spec"])["nets"]}
    except Exception as e:                                  # noqa: BLE001
        rd = {}
        ctx.setdefault("errors", {})["routediag"] = f"{type(e).__name__}: {e}"
    for g in todo:
        try:
            c = corridor(ctx, g)
            bl = blockers(ctx, g, c)
            g["corridor_mm"], g["width_mm"] = c["len_mm"], c["width_mm"]
            g["blockers"] = [b["ref"] for b in bl]
            g["blockers_movable"] = [b["ref"] for b in bl if b["movable"]]
        except Exception as e:                              # noqa: BLE001 - this gap keeps its class; its corridor is reported missing
            ctx.setdefault("errors", {})["corridor"] = f"{type(e).__name__}: {e}"
            g.update(corridor_mm=None, width_mm=None, blockers=[], blockers_movable=[], causes=[], cause="unconfirmed")
            continue
        g["causes"] = sorted({x["cause"] for n in g["nets"] for x in rd.get(n, [])})
        if g in crit and still is not None:
            g["cause"] = "blocked" if set(g["nets"]) & set(still) else "order"
        elif g in crit:
            g["cause"] = "unconfirmed"
        else:
            g["cause"] = "not-confirmed"                     # plain nets: corridor and blockers reported, no router run for them
    return todo


def diagnosis_table(gaps):
    rows = [g for g in gaps if g["kind"] == "ghost-open" and "cause" in g]
    if not rows:
        return ""
    out = ["  ghost-open diagnosis (net: class, cause, blocking parts, routediag causes):"]
    for g in rows:
        out.append(f"    {'/'.join(g['nets'])}: {g['class']}, {g['cause']}, blockers [{', '.join(g['blockers'][:8]) or '-'}]"
                   f" (movable {len(g['blockers_movable'])}), corridor {g['corridor_mm'] if g['corridor_mm'] is not None else '?'} mm x {g['width_mm'] if g['width_mm'] is not None else '?'}, causes [{', '.join(g.get('causes') or []) or '-'}]")
    return "\n".join(out)


# ------------------------------------------------------------------ phase 2: ghost-open rungs (unblock, seal, endpoint) and the JudgeEval
RUNGABLE = ("blocked", "unconfirmed")        # `order` gaps start at `reserve` (phase 3); a plain net has no diagnosis to act on
CLEAR_MM = 0.2                               # courtyard clearance added to the minimum translation


def _nearest_normal(edges, p):
    """Unit normal (both signs) of the corridor edge nearest to point `p` -> [(nx, ny), (-nx, -ny)]."""
    best = None
    for a, b in edges:
        L = math.hypot(b[0] - a[0], b[1] - a[1]) or 1e-9
        t = max(0.0, min(1.0, ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / (L * L)))
        d = math.hypot(p[0] - (a[0] + t * (b[0] - a[0])), p[1] - (a[1] + t * (b[1] - a[1])))
        if best is None or d < best[0]:
            best = (d, (-(b[1] - a[1]) / L, (b[0] - a[0]) / L))
    n = best[1] if best else (1.0, 0.0)
    return [n, (-n[0], -n[1])]


def clear_shift(court, rects, vec, step=0.1, maxd=12.0):
    """Smallest translation along unit `vec` (mm, step 0.1) after which `court` no longer overlaps any rect, + CLEAR_MM; None if none <= maxd."""
    from . import geom as GM
    k = 0
    while k * step <= maxd:
        d = k * step
        c = (court[0] + vec[0] * d, court[1] + vec[1] * d, court[2] + vec[0] * d, court[3] + vec[1] * d)
        if not any(GM.overlap(r, c, -0.02) for r in rects):
            return round(d + CLEAR_MM, 2)
        k += 1
    return None


def _P(ctx):
    from . import placetour as PT
    return PT.params(ctx["cons"])


def _with_supports(B, ctx, mov, tg):
    """Targets -> legal moves, and the support parts of every IC among them follow it (intent.move_with_support: near ones rigidly, far ones
    re-seated at their pin), then everything is legalised against the non-movable parts (placetour._legal). None when nothing is legal."""
    from . import intent as IT, placetour as PT
    full = dict(tg)
    try:
        ics = {c["ic"] for c in ctx.clusters() or []}
        for ref, (x, y, a) in list(tg.items()):
            if ref in ics:
                work = copy.deepcopy(B)                         # move_with_support relocates parts on the model it is given: never the shared one
                for r, v in (IT.move_with_support(work, ctx["cons"], ctx["spec"], ref, x, y, a) or {}).items():
                    if r in mov:
                        full[r] = tuple(v) if r != ref else (x, y, a)
    except Exception as e:                                  # noqa: BLE001 - the IC alone, as before
        ctx.setdefault("errors", {})["supports"] = f"{type(e).__name__}: {e}"
        full = dict(tg)
    return PT._legal(B, ctx["cons"], full, mov)


def _r_unblock(g, ctx):
    """Per blocker (movable, <= 16 pads, smallest first): the minimum-translation nudge out of the corridor along either normal of the
    nearest flight line; candidates move the first 1, 2, 3 blockers together (the 1-blocker one also on the other normal); legalised
    against every non-movable part (placetour._legal)."""
    from . import placetour as PT
    if g["kind"] != "ghost-open" or g.get("cause") not in RUNGABLE:
        return []
    B = ctx.B()
    mov = set(PT._movable(B, ctx["cons"], _P(ctx)))
    corr = corridor(ctx, g)
    bl = [b for b in blockers(ctx, g, corr) if b["movable"] and len(B["parts"][b["ref"]]["pads"]) <= 16]
    plan = []                                               # per blocker: [(dx, dy) via normal 0, via normal 1]
    for b in bl[:3]:
        p = B["parts"][b["ref"]]
        shifts = []
        for vec in _nearest_normal(corr["edges"], p["pos"]):
            d = clear_shift(p["court"], corr["rects"], vec)
            shifts.append(None if d is None else (vec[0] * d, vec[1] * d))
        plan.append((b["ref"], shifts))
    out, seen = [], set()

    def add(tag, picks):
        tg = {}
        for ref, sh in picks:
            x, y, a = B["parts"][ref]["pos"][0], B["parts"][ref]["pos"][1], B["parts"][ref]["angle"]
            tg[ref] = (x + sh[0], y + sh[1], a)
        mv = _with_supports(B, ctx, mov, tg) if tg else None
        k = PT._key(mv) if mv else None
        if mv and k not in seen:
            seen.add(k)
            out.append({"id": f"unblock-{tag}", "rung": "unblock", "moves": mv, "claims": [], "notes": f"clear {','.join(sorted(tg))} of the corridor"})

    best = lambda sh: min((x for x in sh if x), key=lambda v: math.hypot(*v), default=None)
    for n in (1, 2, 3):
        picks = [(r, best(sh)) for r, sh in plan[:n] if best(sh)]
        if len(picks) == n:
            add(f"n{n}", picks)
    if plan and len([x for x in plan[0][1] if x]) == 2:                 # the 1-blocker move on the other normal
        sh = plan[0][1]
        i = min((0, 1), key=lambda k: (math.hypot(*sh[k]), k))
        add("n1-other", [(plan[0][0], sh[1 - i])])
    return out


def _r_seal(g, ctx):
    """When routediag found a SEALED_PAD: breakout.repair on a scratch copy of the board (it evicts what walls a dense part in); the
    parts it moved are the candidate. No sealed pad in the diagnosis: nothing to propose."""
    import shutil
    import tempfile
    from . import breakout as BO, routediag as RDG
    if g["kind"] != "ghost-open" or g.get("cause") not in RUNGABLE or "SEALED_PAD" not in (g.get("causes") or []):
        return []
    B = ctx.B()
    d = tempfile.mkdtemp(prefix="seal-", dir=os.path.dirname(os.path.abspath(ctx["board"])))
    try:
        tmp = os.path.join(d, "seal.kicad_pcb")
        shutil.copy(ctx["board"], tmp)
        rep = BO.repair(tmp, ctx["cons"], ctx["spec"], log=lambda *a, **k: None)
        B2 = RDG.load(tmp)
        mv = {r: (p["pos"][0], p["pos"][1], p["angle"]) for r, p in B2["parts"].items()
              if r in B["parts"] and (abs(p["pos"][0] - B["parts"][r]["pos"][0]) > 1e-3 or abs(p["pos"][1] - B["parts"][r]["pos"][1]) > 1e-3
                                      or abs(p["angle"] - B["parts"][r]["angle"]) > 1e-3)}
        return [{"id": "seal-breakout", "rung": "seal", "moves": mv, "claims": [], "notes": f"breakout repair, failed pins {rep.get('before')}->{rep.get('after')}"}] if mv else []
    finally:
        shutil.rmtree(d, ignore_errors=True)


def endpoint_budget(g, ctx=None):
    """Length budget (mm) of the gap's own net class: a pair's own kind (usb2, eth, pcie ... from netclass.classify, owner overrides in
    [placement.budget] included), the rf budget (owner's [placement.rf] / [placement.budget] rf included); None -> the span is the reference."""
    from . import netclass as NC
    try:
        cons = ctx["cons"]
        pl = cons.get("placement") or {}
        if g["class"] == "rf":
            user = (pl.get("budget") or {}).get("rf")
            rfc = pl.get("rf") or {}
            if user is not None and not isinstance(user, dict):
                return float(user)
            return NC.rf_budget_mm(float(rfc.get("freq_ghz", 2.4)), float(rfc.get("loss_db", 0.5))) if rfc else NC.BUDGET.get("rf")
        info = NC.classify(ctx.B(), cons, ctx["spec"]).get(g["nets"][0]) or {}
        return info.get("budget") if info.get("class") == "pair" else None
    except Exception:                                       # noqa: BLE001 - no table, no budget: the span
        return None


def _r_endpoint(g, ctx):
    """The smaller movable endpoint part steps toward the other end along the straight line by 25 / 50 / 100 % of the excess over the
    class budget (of the whole span when the class has none); legalised (placetour._legal)."""
    from . import placetour as PT
    if g["kind"] != "ghost-open" or g.get("cause") not in RUNGABLE or not g["nets"]:
        return []
    B = ctx.B()
    mov = set(PT._movable(B, ctx["cons"], _P(ctx)))
    pads = [q for q in B["pads"] if q["net"] in g["nets"]]
    refs = sorted({q["ref"] for q in pads})
    if len(refs) < 2:
        return []
    area = lambda r: (lambda c: (c[2] - c[0]) * (c[3] - c[1]))(B["parts"][r]["court"])
    movers = sorted([r for r in refs if r in mov], key=lambda r: (area(r), r))
    if not movers:
        return []
    me = movers[0]
    mine = [q for q in pads if q["ref"] == me]
    other = [q for q in pads if q["ref"] != me]
    mx, my = sum(q["x"] for q in mine) / len(mine), sum(q["y"] for q in mine) / len(mine)
    ox, oy = sum(q["x"] for q in other) / len(other), sum(q["y"] for q in other) / len(other)
    span = math.hypot(ox - mx, oy - my)
    if span < 1e-6:
        return []
    bud = endpoint_budget(g, ctx)
    excess = span - bud if bud and span > bud else span
    out, p = [], B["parts"][me]
    for frac in (0.25, 0.5, 1.0):
        d = min(span, excess * frac)
        tg = {me: (p["pos"][0] + (ox - mx) / span * d, p["pos"][1] + (oy - my) / span * d, p["angle"])}
        mv = _with_supports(B, ctx, mov, tg)
        if mv:
            out.append({"id": f"endpoint-{me}-{int(frac * 100)}", "rung": "endpoint", "moves": mv, "claims": [],
                        "notes": f"{me} {d:.1f} mm toward the other end (span {span:.1f}, budget {bud if bud else '-'})"})
    return out


DISPATCH["ghost-open"] = ["unblock", "seal", "endpoint"]
RUNGS.update({"unblock": _r_unblock, "seal": _r_seal, "endpoint": _r_endpoint})


# ------------------------------------------------------------------ phase 8: row-rail (pull-load, pour-need)
def _ref(s):
    return str(s).split(".")[0]


def _rail_row(ctx, g):
    for r in ctx.rows() or []:
        if r.get("kind") == "rail" and g.get("id") == f"row-rail:{r.get('name')}|{r.get('metric')}":
            return r
    return None


def _rail_xy(B, ref, net):
    p = B["parts"].get(ref)
    return [(q["x"], q["y"]) for q in (p["pads"] if p else []) if q.get("net") == net]


def rail_reach(B, src, load, net, shift=(0.0, 0.0)):
    """(Manhattan mm, source pad, worst load pad): the load pad farthest from its nearest source pad (powerbudget._ends), with the
    source pads translated by `shift`. The plane is on an inner layer: the side of a part does not enter (pad x, y are absolute)."""
    s = [(x + shift[0], y + shift[1]) for x, y in _rail_xy(B, src, net)]
    ld = _rail_xy(B, load, net)
    if not s or not ld:
        return None
    d = lambda a, b: abs(a[0] - b[0]) + abs(a[1] - b[1])
    w = max(ld, key=lambda b: (min(d(a, b) for a in s), b))
    a = min(s, key=lambda a: (d(a, w), a))
    return d(a, w), a, w


def _rail_siblings(ctx, row):
    """Every rail row of the same net and source: [(load ref, limit mm, value mm)]."""
    src = _ref(row["refs"][0])
    return [(_ref(r["refs"][1]), r.get("max"), r.get("value")) for r in ctx.rows() or []
            if r.get("kind") == "rail" and r.get("nets") == row.get("nets") and _ref(r["refs"][0]) == src and len(r.get("refs") or []) > 1]


def _r_pull_load(g, ctx):
    """The load steps toward the source, or the source (with its regulator's supports) toward the load, by the Manhattan excess x 1.1 + 0.2 mm
    (+ 0, 1, 2.5 mm), along the line and along each axis; legalised against every non-movable part (placetour._legal), supports carried
    (intent.move_with_support). A source move is proposed only when no other load of the rail gets newly beyond its limit by it."""
    from . import placetour as PT
    row = _rail_row(ctx, g)
    if g["kind"] != "row-rail" or not row or row.get("max") is None or len(row.get("refs") or []) < 2:
        return []
    B, net = ctx.B(), (row.get("nets") or [None])[0]
    src, load = _ref(row["refs"][0]), _ref(row["refs"][1])
    got = rail_reach(B, src, load, net)
    if got is None:
        return []
    dist, a, w = got
    need = dist - float(row["max"])
    if need <= 0:
        return []
    mov = set(PT._movable(B, ctx["cons"], _P(ctx)))
    dx, dy = w[0] - a[0], w[1] - a[1]                       # source pad -> load pad
    ax, ay = abs(dx), abs(dy)
    sib = [(r, mx, v) for r, mx, v in _rail_siblings(ctx, row) if r != load and mx is not None]
    ic = next((c["ic"] for c in ctx.clusters() or [] if c["ic"] == src or any(s["ref"] == src for s in c["supports"])), src)
    out, seen = [], set()
    for k, extra in enumerate((0.0, 1.0, 2.5)):
        s = need * 1.1 + 0.2 + extra
        vecs = {"line": (dx / dist * s, dy / dist * s)}
        if ax >= s:
            vecs["x"] = (math.copysign(s, dx), 0.0)
        if ay >= s:
            vecs["y"] = (0.0, math.copysign(s, dy))
        for tag, (vx, vy) in sorted(vecs.items()):
            for who, ref, sg in (("load", load, -1.0), ("src", ic, 1.0)):
                if ref not in mov:
                    continue
                if who == "src":                            # the source steps toward the load: no sibling load may get newly over its limit
                    bad = False
                    for r, mx, v in sib:
                        n = rail_reach(B, src, r, net, (vx, vy))
                        if n is not None and n[0] > max(float(mx), float(v if v is not None else 0.0)) + 1e-6:
                            bad = True
                            break
                    if bad:
                        continue
                p = B["parts"][ref]
                tg = {ref: (p["pos"][0] + sg * vx, p["pos"][1] + sg * vy, p["angle"])} if who == "src" else \
                     {ref: (p["pos"][0] - vx, p["pos"][1] - vy, p["angle"])}
                mv = _with_supports(B, ctx, mov, tg)
                key = PT._key(mv) if mv else None
                if mv and key not in seen:
                    seen.add(key)
                    out.append({"id": f"pull-{k}-{tag}-{who}", "rung": "pull-load", "moves": mv, "claims": [],
                                "notes": f"{ref} {s:.1f} mm toward {'the load' if who == 'src' else 'the source'} ({tag})"})
    return out


def pour_need(ctx, g):
    """What a rail row that no placement move closes needs: the rail copper wider by value / limit (reach is linear in the copper width at
    a fixed drop budget) and the load spread."""
    row = _rail_row(ctx, g)
    if not row or row.get("max") is None:
        return None
    B, net = ctx.B(), (row.get("nets") or [None])[0]
    src = _ref(row["refs"][0])
    sib = _rail_siblings(ctx, row)
    over = sorted(r for r, mx, v in sib if mx is not None and v is not None and v > mx)
    xy = [q for r, _m, _v in sib for q in _rail_xy(B, r, net)]
    spread = (round(max(x for x, _ in xy) - min(x for x, _ in xy), 1), round(max(y for _, y in xy) - min(y for _, y in xy), 1)) if xy else None
    rec = _pour_rec(row, sib, spread, over, src, net)
    ctx.setdefault("pour_need", {}).setdefault(net, rec)           # the first record of a net stands; pour_questions refreshes it from the final state
    return ctx["pour_need"][net]


def _pour_rec(row, sib, spread, over, src, net):
    vals = [v for _r, _m, v in sib if v is not None] or [row["value"]]
    worst = max(vals)
    return {"net": net, "source": src, "limit_mm": row["max"], "worst_mm": worst, "loads": len(sib), "loads_over": over, "spread_mm": spread,
            "copper_factor": round(float(worst) / float(row["max"]), 3), "layers": row.get("layers")}


def pour_questions(ctx, res):
    """After `close`: the rails whose row is still not honoured (gaps_out) and for which no move was found get the owner question
    `rail-pour:<net>` (ctx["session"], a questions.Session); a rail a later round closed is dropped from the record. -> [record]."""
    left = {g["nets"][0] for g in res.get("gaps_out") or [] if g["kind"] == "row-rail" and g["nets"]}
    need = ctx.setdefault("pour_need", {})
    for net in [n for n in need if n not in left]:
        del need[net]
    for net, rec in sorted(need.items()):                           # the final state: loads over, worst reach and copper factor of the rail now
        row = next((r for r in ctx.rows() or [] if r.get("kind") == "rail" and (r.get("nets") or [None])[0] == net and r.get("refs")), None)
        if row and row.get("max") is not None:
            sib = _rail_siblings(ctx, row)
            over = sorted(r for r, mx, v in sib if mx is not None and v is not None and v > mx)
            need[net] = dict(_pour_rec(row, sib, rec["spread_mm"], over, rec["source"], net))
    S = ctx.get("session")
    for net, rec in sorted(need.items()):
        if S is None:
            continue
        over, f, row_max = rec["loads_over"], rec["copper_factor"], rec["limit_mm"]
        sp = rec["spread_mm"]
        found = (f"rail {net} from {rec['source']}: {len(over)} of {rec['loads']} loads beyond the reach limit {row_max:g} mm ({', '.join(over)}); worst "
                 f"{rec['worst_mm']:g} mm; loads spread {sp[0]:g} x {sp[1]:g} mm; no legal move of a load or of the source closes it" if sp else
                 f"rail {net}: loads beyond the reach limit and no legal move closes it")
        S.ask(f"rail-pour:{net}", "rail copper", found,
              {"pour": f"plan the rail as a pour / plane copper on {', '.join(rec.get('layers') or ['an inner layer'])}, x{f:g} the copper of the budget "
                       f"(the row then reads 'by pour')",
               "waive": "accept the reach as it is, with the reason on record",
               "move": "unfix or move the source / loads yourself, then run again"},
              "pour", "the route stage (rail copper) and the next floorplan run")
    return [need[n] for n in sorted(need)]


def _r_pour_need(g, ctx):
    """No candidate: the rail needs copper, not a move. Records the need (ctx["pour_need"]); `pour_questions` asks after the loop."""
    if g["kind"] == "row-rail":
        pour_need(ctx, g)
    return []


DISPATCH["row-rail"] = ["pull-load", "pour-need"]
RUNGS.update({"pull-load": _r_pull_load, "pour-need": _r_pour_need})


# ------------------------------------------------------------------ phase 6: row-away (feasible, push-out, re-seat), row-flight (push-out)
AWAY_STEP_MM = 1.0                      # grid pitch of the feasibility / re-seat scan (the module's bounding box is the footprint: conservative)
AWAY_DIRS = 8


def _sep_entry(cons, name):
    from . import budget as BG
    n = name[4:] if name.startswith("sep:") else name
    return next((e for i, e in enumerate((cons or {}).get("separation") or []) if BG.sep_name(e, i) == n), None)


def _away_info(ctx, g):
    """The row behind a row-away gap: {name, limit, value, refs, a {ref|keepout: rect}, b {...}} or None (a region row, a row not found)."""
    from . import budget as BG, intent as IT
    B, cons = ctx.B(), ctx["cons"]
    row = next((r for r in ctx.rows() or [] if r.get("kind") == "separation" and g["id"] == f"row-away:{r['name']}|{r.get('metric')}"), None)
    irow = None if row else next((r for r in ctx.intent_rows() or [] if r.get("kind") == "away" and g["id"] == f"row-away:{r['name']}"), None)
    if row is None and irow is None:
        return None
    r = row or irow
    name = str(r["name"])
    e = _sep_entry(cons, name) if row or name.startswith("sep:") else None
    if e is not None:
        ko = {BG.KO + k["name"]: k["rect"] for k in BG.keepouts(B, cons)}

        def group(items):
            items = [items] if isinstance(items, str) else list(items or [])
            rects = {x: ko[x] for x in items if x in ko}
            rects.update({x: B["parts"][x]["court"] for x in IT.match(B["parts"], [x for x in items if not str(x).startswith(BG.KO)])})
            return rects
        ga, gb = group(e.get("a")), group(e.get("b"))
        gb = {k: v for k, v in gb.items() if k not in ga}
        limit = e.get("min_mm")
    else:
        a = next((x for x in IT.items(B, cons)["away"] if x["name"] == name), None)
        if a is None:
            return None
        ga, gb = {x: B["parts"][x]["court"] for x in a["a"]}, {x: B["parts"][x]["court"] for x in a["b"] if x not in a["a"]}
        limit = a["min"]
    if limit is None:
        return None
    return {"name": name[4:] if name.startswith("sep:") else name, "limit": float(limit), "value": r.get("value"),
            "refs": list(r.get("refs") or []), "a": ga, "b": gb}


def _module(ctx, ref, mov):
    """The offender's unit: the IC and its supports that may move (the same unit `_with_supports` carries)."""
    sup = [s["ref"] for c in ctx.clusters() or [] if c["ic"] == ref for s in c["supports"]]
    return [ref] + sorted(s for s in set(sup) if s in mov and s != ref and s in ctx.B()["parts"])


def _away_scan(ctx, g, info):
    """Feasibility of the offender's module at the row's limit: scans module-bbox positions on the grid (board minus fixed parts and
    keep-outs, at least `limit` from the other group). -> dict {off, module, bbox, other, free: [(disp, cx, cy)], best, wanted, refusing, ...};
    the verdict `impossible` is recorded in ctx["away_impossible"][gap id] with limit, best reachable value and the refusing part."""
    from . import budget as BG, geom as GM, placetour as PT
    B, cons = ctx.B(), ctx["cons"]
    mov = set(PT._movable(B, cons, _P(ctx)))
    imp = ctx.setdefault("away_impossible", {})
    imp.pop(g["id"], None)
    side = lambda x: "a" if x in info["a"] else "b" if x in info["b"] else None             # noqa: E731
    off = next((r for r in info["refs"] if r in B["parts"] and r in mov and side(r)), None)
    if off is None:                                          # both sides fixed (or the row names no part): nothing may move
        val = info["value"]
        imp[g["id"]] = {"name": info["name"], "limit": info["limit"], "best": val, "value": val, "refusing": ", ".join(info["refs"]) or "?",
                        "why": "both ends are fixed"}
        return {"off": None}
    other = info["b" if side(off) == "a" else "a"]
    module = _module(ctx, off, mov)
    cs = [B["parts"][m]["court"] for m in module]
    bb = (min(c[0] for c in cs), min(c[1] for c in cs), max(c[2] for c in cs), max(c[3] for c in cs))
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    c0 = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)
    key = (g["id"], tuple((m, *B["parts"][m]["pos"], B["parts"][m]["angle"]) for m in module), tuple(sorted(tuple(r) for r in other.values())))
    cache = ctx.setdefault("_away_cache", {})
    if key in cache:
        sc = cache[key]
        if sc["verdict"]:
            imp[g["id"]] = sc["verdict"]
        return sc
    board = B.get("bbox") or (min(p["court"][0] for p in B["parts"].values()), min(p["court"][1] for p in B["parts"].values()),
                              max(p["court"][2] for p in B["parts"].values()), max(p["court"][3] for p in B["parts"].values()))
    obst = [(r, B["parts"][r]["court"]) for r in sorted(B["parts"]) if r not in mov and r not in module and r not in other and r not in info["a"]
            and r not in info["b"]]
    obst += [(r, B["parts"][r]["court"]) for r in sorted(B["parts"]) if r not in mov and r not in module and (r in info["a"] or r in info["b"])
             and r not in other]
    for k in BG.keepouts(B, cons):                           # a keep-out the module already stands in, or owns, is not an obstacle
        if k["owner"] in module or k["owner"] in other or (BG.KO + k["name"]) in other or (BG.KO + k["name"]) in info["a"] \
                or (BG.KO + k["name"]) in info["b"] or GM.overlap(bb, k["rect"]):
            continue
        obst.append(("keep-out " + k["name"], tuple(k["rect"])))
    orects = list(other.values())
    pos, x = [], board[0] + w / 2
    while x <= board[2] - w / 2 + 1e-9:
        y = board[1] + h / 2
        while y <= board[3] - h / 2 + 1e-9:
            r = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
            pos.append((min((GM.rect_gap(r, o) for o in orects), default=1e9), x, y, r))
            y += AWAY_STEP_MM
        x += AWAY_STEP_MM
    blocker = lambda r: next((n for n, o in obst if GM.overlap(r, o)), None)                # noqa: E731
    pos.sort(key=lambda t: (-t[0], t[1], t[2]))
    best = next(((v, x, y) for v, x, y, r in pos if blocker(r) is None), None)
    wanted = [t for t in pos if t[0] >= info["limit"] + CLEAR_MM - 1e-9]
    free = sorted(((math.hypot(x - c0[0], y - c0[1]), x, y) for v, x, y, r in wanted[:4000] if blocker(r) is None))
    sc = {"off": off, "module": module, "bbox": bb, "other": other, "free": free, "best": best[0] if best else None, "c0": c0, "verdict": None,
          "obst": obst, "wanted": len(wanted)}
    if not free:
        tally = {}
        for v, x, y, r in wanted[:400]:
            n = blocker(r) or "?"
            tally[n] = tally.get(n, 0) + 1
        sc["verdict"] = {"name": info["name"], "limit": info["limit"], "value": info["value"],
                         "best": round(min(best[0], 1e6), 2) if best else None,
                         "refusing": max(sorted(tally), key=lambda n: tally[n]) if tally else "the board outline (it cannot hold the unit that far away)",
                         "why": f"{off} (unit of {len(module)} part(s), {w:.1f} x {h:.1f} mm) has no place on the board at {info['limit']:g} mm"}
        imp[g["id"]] = sc["verdict"]
    cache[key] = sc
    return sc


def _r_feasible(g, ctx):
    """Diagnosis only: no candidate. Records the `impossible` verdict (limit, best reachable value, refusing part) for `away_questions`."""
    if g["kind"] == "row-away":
        info = _away_info(ctx, g)
        if info:
            _away_scan(ctx, g, info)
        else:                                               # a region row (or a row not found): no rung moves it yet; say so, never silently
            ctx.setdefault("row_reports", {})[g["id"]] = {"name": g["id"].split(":", 1)[1].split("|")[0], "why": "region row: no placement rung repairs it yet (listed, not relaxed)"}
    return []


def _reached(mv, ref, tg, tol=0.05):
    """True when the legalised move `mv` puts `ref` on its target pose `tg[ref]` (within `tol` mm): the push was not undone by the legaliser."""
    if not mv:
        return False
    x, y, _a = tg[ref]
    got = mv.get(ref)
    return bool(got) and math.hypot(got[0] - x, got[1] - y) <= tol


def _carry_blockers(B, ctx, mov, sc, tg, info):
    """The legaliser treats every part that is not a target as fixed, so a unit pushed into a gap that small movable parts fill is put back
    where it was (or short of it). Those parts (movable, same side, not part of the unit nor of either group of the row) whose courtyard
    meets the unit's pushed courtyards step out by the least translation (8 ways, `clear_shift`) as targets of the same move; every move is
    then legalised together. -> moves, or None when nothing blocks / the pushed unit is still held back."""
    from . import geom as GM
    off = sc["off"]
    dx, dy = tg[off][0] - B["parts"][off]["pos"][0], tg[off][1] - B["parts"][off]["pos"][1]
    unit = set(sc["module"])
    new = [(m, tuple(v + s_ for v, s_ in zip(B["parts"][m]["court"], (dx, dy, dx, dy)))) for m in sc["module"]]
    rects = [c for _m, c in new]
    skip = unit | set(info["a"]) | set(info["b"])
    side = B["parts"][off]["side"]
    blk = sorted((r for r in mov if r not in skip and (B["parts"][r]["side"] == side or B["parts"][r].get("tht"))
                  and any(GM.overlap(c, B["parts"][r]["court"], -0.02) for c in rects)),
                 key=lambda r: (GM.rect_gap(B["parts"][r]["court"], B["parts"][off]["court"]), r))[:6]
    if not blk:
        return None
    c45 = math.sqrt(0.5)
    full = dict(tg)
    for r in blk:
        q = B["parts"][r]
        best = None
        for vec in ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0), (c45, c45), (-c45, c45), (c45, -c45), (-c45, -c45)):
            t = clear_shift(q["court"], rects, vec)
            if t is not None and (best is None or t < best[0]):
                best = (t, vec)
        if best is None:
            return None
        full[r] = (q["pos"][0] + best[1][0] * best[0], q["pos"][1] + best[1][1] * best[0], q["angle"])
    mv = _with_supports(B, ctx, mov, full)
    return mv if _reached(mv, off, tg) else None


def _r_push_out(g, ctx):
    """row-away: the offender's unit steps along the separating direction (and 7 more at 45 deg) by limit - value + clearance.
    row-flight: the movable end steps (8 directions) by the least distance at which its flight line clears the keep-out + clearance."""
    from . import placetour as PT
    if g["kind"] == "row-flight":
        return _flight_push(g, ctx)
    if g["kind"] != "row-away":
        return []
    info = _away_info(ctx, g)
    if not info or info["value"] is None:
        return []
    sc = _away_scan(ctx, g, info)
    if not sc["off"] or sc["verdict"]:
        return []
    B = ctx.B()
    mov = set(PT._movable(B, ctx["cons"], _P(ctx)))
    from . import geom as GM
    near = min(sc["other"].values(), key=lambda o: (GM.rect_gap(sc["bbox"], o), o))
    ux, uy = sc["c0"][0] - (near[0] + near[2]) / 2, sc["c0"][1] - (near[1] + near[3]) / 2
    base = math.atan2(uy, ux) if (ux or uy) else 0.0
    d = info["limit"] - float(info["value"]) + CLEAR_MM
    p = B["parts"][sc["off"]]
    out, seen = [], set()
    for k in range(AWAY_DIRS):
        a = base + k * 2 * math.pi / AWAY_DIRS
        tg = {sc["off"]: (p["pos"][0] + d * math.cos(a), p["pos"][1] + d * math.sin(a), p["angle"])}
        mv = _with_supports(B, ctx, mov, tg)
        if not _reached(mv, sc["off"], tg):                    # the legaliser held the unit back: small parts stand in its way, they travel with it
            mv = _carry_blockers(B, ctx, mov, sc, tg, info) or mv
        key = PT._key(mv) if mv else None
        if mv and key not in seen:
            seen.add(key)
            out.append({"id": f"push-{k}", "rung": "push-out", "moves": mv, "claims": [], "notes": f"{sc['off']} {d:.1f} mm away from the other group ({k * 45} deg)"})
    return out


def _r_re_seat(g, ctx):
    """row-away: grid seats of the unit (nearest first, >= 2 mm apart, at least limit + clearance from the other group, clear of fixed parts and keep-outs)."""
    from . import placetour as PT
    if g["kind"] != "row-away":
        return []
    info = _away_info(ctx, g)
    if not info:
        return []
    sc = _away_scan(ctx, g, info)
    if not sc["off"] or sc["verdict"]:
        return []
    B = ctx.B()
    mov = set(PT._movable(B, ctx["cons"], _P(ctx)))
    p = B["parts"][sc["off"]]
    picks = []
    for _d, x, y in sc["free"]:
        if all(math.hypot(x - a, y - b) >= 2.0 for a, b in picks):
            picks.append((x, y))
        if len(picks) >= CANDS_PER_RUNG:
            break
    out, seen = [], set()
    for k, (x, y) in enumerate(picks):
        tg = {sc["off"]: (p["pos"][0] + x - sc["c0"][0], p["pos"][1] + y - sc["c0"][1], p["angle"])}
        mv = _with_supports(B, ctx, mov, tg)
        key = PT._key(mv) if mv else None
        if mv and key not in seen:
            seen.add(key)
            out.append({"id": f"seat-{k}", "rung": "re-seat", "moves": mv, "claims": [], "notes": f"{sc['off']} seated {math.hypot(x - sc['c0'][0], y - sc['c0'][1]):.1f} mm away"})
    return out


def _flight_push(g, ctx):
    from . import budget as BG, geom as GM, placetour as PT
    B, cons = ctx.B(), ctx["cons"]
    row = next((r for r in ctx.rows() or [] if r.get("kind") == "keepout-flight" and g["id"] == f"row-flight:{r['name']}|{r.get('metric')}"), None)
    if not row or len(row.get("refs") or []) < 2 or not row.get("nets"):
        return []
    ko = next((k for k in BG.keepouts(B, cons) if str(row["name"]).endswith(" x " + k["name"])), None)
    if ko is None:
        return []
    mov = set(PT._movable(B, cons, _P(ctx)))
    a, b = row["refs"][:2]
    ctr = BG._centres(B, row["nets"][0])
    if a not in ctr or b not in ctr:
        return []
    ends = [r for r in (a, b) if r in mov]
    rep = ctx.setdefault("row_reports", {})
    rep.pop(g["id"], None)
    if not ends:
        rep[g["id"]] = {"name": row["name"], "why": f"both ends {a}, {b} are fixed: a layer change is the route stage's job"}
        return []
    r0 = (ko["rect"][0] - CLEAR_MM, ko["rect"][1] - CLEAR_MM, ko["rect"][2] + CLEAR_MM, ko["rect"][3] + CLEAR_MM)
    out, seen = [], set()
    for k in range(AWAY_DIRS):
        ang = k * 2 * math.pi / AWAY_DIRS
        ux, uy = math.cos(ang), math.sin(ang)
        best = None
        for e in ends:
            o = b if e == a else a
            d = 0.25
            while d <= 40.0:
                c = (ctr[e][0] + ux * d, ctr[e][1] + uy * d)
                if not GM.seg_hits_rect(c, ctr[o], r0):
                    break
                d += 0.25
            else:
                continue
            if best is None or (d, e) < best:
                best = (d, e)
        if best is None:
            continue
        d, e = best
        p = B["parts"][e]
        mv = _with_supports(B, ctx, mov, {e: (p["pos"][0] + ux * d, p["pos"][1] + uy * d, p["angle"])})
        key = PT._key(mv) if mv else None
        if mv and key not in seen:
            seen.add(key)
            out.append({"id": f"flight-{k}", "rung": "push-out", "moves": mv, "claims": [], "notes": f"{e} {d:.1f} mm ({k * 45} deg): the flight line clears {ko['name']}"})
    return out


def away_questions(ctx, res):
    """After `close`: a row-away gap still open and impossible (both ends fixed, or no place at the limit) is one owner question
    `row:<name>` (ctx["session"]) with limit, best reachable value, refusing part and the four ways out. Returns the records asked; a row
    a later round closed is dropped. row-flight rows with both ends fixed come back in ctx["row_reports"] (the route stage's job)."""
    left = {g["id"] for g in res.get("gaps_out") or []}
    imp = ctx.setdefault("away_impossible", {})
    for gid in [k for k in imp if k not in left]:
        del imp[gid]
    rep = ctx.setdefault("row_reports", {})
    for gid in [k for k in rep if k not in left]:
        del rep[gid]
    S, out = ctx.get("session"), []
    for gid, rec in sorted(imp.items()):
        out.append(dict(rec, gap=gid))
        if S is None:
            continue
        best = "none (nothing can be reached)" if rec["best"] is None else f"{rec['best']:g} mm"
        S.ask(f"row:{rec['name']}", "separation", f"row {rec['name']}: {rec['value'] if rec['value'] is None else format(float(rec['value']), 'g')} mm now, limit {rec['limit']:g} mm; best reachable {best}; "
              f"refused by {rec['refusing']} ({rec['why']})",
              {"waive": "accept the separation as it is, with the reason on record",
               "lower": f"lower min_mm of the row to the best reachable value ({best})",
               "unfix": f"unfix the part that stops the move ({rec['refusing']}) so it can be placed",
               "grow": "grow the board so the unit fits at the limit"},
              "waive", "the next floorplan run")
    return out


DISPATCH["row-away"] = ["feasible", "push-out", "re-seat"]
DISPATCH["row-flight"] = ["push-out"]
RUNGS.update({"feasible": _r_feasible, "push-out": _r_push_out, "re-seat": _r_re_seat})


class FileEval(Evaluator):
    """Floorplan's evaluator after the search: a candidate is applied to a copy of the written board (placetour._apply, connectivity guard),
    every gap kind of the stage is measured again on it, and it is refused for: a new legality issue against the input (model), a
    support beyond its limit that was not, wire length or crossings more than +2 % (floorplan2.measured), a `guards` value that rose
    (breakout / fan-out), and, when it would improve the score, a new KiCad DRC item against the input (`drc_new`). The base moves only on commit."""
    TOL = 0.02

    def __init__(self, ctx, board, input_board, workdir, legal_base=None, measure=None, guards=None, drc_ref_board=None):
        self.ctx, self.input_board, self.work = ctx, input_board, workdir
        self.drc_board = drc_ref_board or input_board
        self.legal_base, self.measure, self.guards = legal_base, measure, dict(guards or {})
        os.makedirs(workdir, exist_ok=True)
        self.board_path = os.path.join(workdir, "base.kicad_pcb")
        shutil.copyfile(board, self.board_path)
        pro = os.path.splitext(input_board)[0] + ".kicad_pro"
        self.pro = pro if os.path.exists(pro) else None
        if self.pro:
            shutil.copyfile(self.pro, os.path.splitext(self.board_path)[0] + ".kicad_pro")
        self.n, self.drc_ref, self.gaps = 0, None, None
        self.m = self._cost(self.board_path)
        self.g0 = {k: f(self.board_path) for k, f in self.guards.items()}
        self._sync()

    def _cost(self, path):
        return self.measure(path) if self.measure else {}

    def _sync(self):
        self.ctx["board"] = self.board_path
        for k in ("B", "_rows", "_intent", "_cl", "_far", "errors", "rows"):
            self.ctx.pop(k, None)

    def _gaps(self, path):
        c = Ctx(stage="floorplan", board=path, cons=self.ctx["cons"], spec=self.ctx["spec"], ds_dir=self.ctx.get("ds_dir"))
        return detect(c, SCOPE["floorplan"])["gaps"]

    def detect(self, kinds):
        if self.gaps is None:
            self.gaps = self._gaps(self.board_path)
        return list(self.gaps)

    def state(self):
        return {"illegal": 0}

    def board(self):
        return self.ctx.B()

    def trial(self, cand):
        from . import legalgate as LG, placetour as PT, routediag as RD
        self.n += 1
        p = os.path.join(self.work, f"t{self.n}-{cand['id']}.kicad_pcb")
        ok, probs, _rem = PT._apply(self.board_path, cand["moves"], p, False)
        if not ok:
            return {"gaps": [], "state": {}, "refusal": "legal:guard " + "; ".join(probs[:2])}
        if self.pro:
            shutil.copyfile(self.pro, os.path.splitext(p)[0] + ".kicad_pro")
        B1 = RD.load(p)
        ill = LG.count_new(LG.new_issues(LG.baseline(B1, self.ctx["cons"]), self.legal_base)) if self.legal_base is not None else 0
        gaps = self._gaps(p)
        tr = {"gaps": gaps, "state": {"illegal": ill}, "refusal": None, "board": p}
        if ill:
            tr["refusal"] = f"legal:model {ill} new issue(s) against the input"
            return tr
        before, after = score_terms(self.gaps or [], self.state()), score_terms(gaps, tr["state"])
        for t in ("decap_far", "support_far", "far_mm"):
            if after[t] > before[t] + 1e-9:
                tr["refusal"] = f"score:{t} {before[t]}->{after[t]}"
                return tr
        m = self._cost(p)
        for k in ("wl", "cross"):
            if self.m.get(k) and m.get(k, 0) > self.m[k] * (1 + self.TOL):
                tr["refusal"] = f"score:{k} {self.m[k]:.0f}->{m[k]:.0f} (+{100 * (m[k] / self.m[k] - 1):.1f} % > 2 %)"
                return tr
        tr["m"] = m
        for k, f in sorted(self.guards.items()):
            v = f(p)
            if v > self.g0[k]:
                tr["refusal"] = f"score:{k} {self.g0[k]}->{v}"
                return tr
        if score(gaps, tr["state"]) <= score(self.gaps or [], self.state()):
            try:
                if self.drc_ref is None:
                    self.drc_ref = LG._drc(self.drc_board, pro_from=self.drc_board) or []
                d = LG.drc_new(p, self.drc_board, ref=self.drc_ref)
            except Exception:                                   # noqa: BLE001
                d = None
            if d is None:
                tr["refusal"] = "drc:not-run (kicad-cli gave no answer: fails closed)"
            elif d["items"]:
                tr["refusal"] = "drc:" + ",".join(f"{t} {n}" for t, n in sorted(d["n"].items())) + " " + ";".join("/".join(q) for q in d["pairs"][:3])
        return tr

    def commit(self, tr):
        self.board_path, self.gaps, self.m = tr["board"], tr["gaps"], tr.get("m") or self.m
        self._sync()


# ------------------------------------------------------------------ phase 7: row-cin-side, row-template, row-pin-bypass
def _row_of(ctx, g):
    for r in ctx.rows() or []:
        if r.get("kind") == "support" and r.get("metric") in ("template", "pin-bypass") and g.get("id") == f"{g['kind']}:{r.get('name')}|{r.get('metric')}":
            return r
    return None


def _unit(ctx, B, ic):
    """The cluster of regulator `ic` measured on the model `B` (fresh distances: rungs move parts on copies)."""
    from . import clusters as CL
    return next((c for c in CL.build(B, ctx["cons"], ctx["spec"])[0] if c["ic"] == ic), None)


def _on_copy(B, tg):
    from . import geom as GM
    B2 = copy.deepcopy(B)
    for r, (x, y, a) in tg.items():
        GM.place(B2["parts"][r], x, y, a)
    return B2


def _reseat_on(ctx, B2, c, only=None, everything=False, refused=None):
    """The seater (clusters.reseat, strict: a seat beyond the part's limit is refused) on the model copy, for the parts of cluster `c`
    named in `only` that are far or off the template (all of them with `everything`); mutates B2."""
    from . import clusters as CL, legality as LGL
    x0, y0, x1, y1 = B2["bbox"]
    CL.reseat(B2, [c], everything=everything, only=only, bounds=(x0 + 0.2, y0 + 0.2, x1 - 0.2, y1 - 0.2), legal=LGL.Legal(B2, ctx["cons"]), strict=True, refused=refused)


def _moves_between(B0, B2):
    out = {}
    for r, p in B2["parts"].items():
        q = B0["parts"].get(r)
        if q is not None and (abs(p["pos"][0] - q["pos"][0]) > 1e-3 or abs(p["pos"][1] - q["pos"][1]) > 1e-3 or abs(p["angle"] - q["angle"]) > 1e-3):
            out[r] = (round(p["pos"][0], 4), round(p["pos"][1], 4), p["angle"])
    return out


def _unit_gap(g, ctx, kinds=("row-cin-side", "row-template")):
    """-> (board, regulator ref, its cluster, movable set, the unit's movable confidently-owned supports, input caps) or None."""
    from . import placetour as PT
    row = _row_of(ctx, g)
    if g["kind"] not in kinds or not row or not row.get("refs"):
        return None
    B, ic = ctx.B(), row["refs"][0]
    c = next((x for x in ctx.clusters() or [] if x["ic"] == ic), None)
    if c is None or ic not in B["parts"]:
        return None
    mov = set(PT._movable(B, ctx["cons"], _P(ctx)))
    sup = sorted(s["ref"] for s in c["supports"] if s["sure"] and s["ref"] in mov and not B["parts"][s["ref"]].get("locked"))
    caps = sorted(s["ref"] for s in c["supports"] if s["sure"] and s["rank"] == 1 and s["ref"].startswith("C") and s["ref"] in sup)
    return B, ic, c, mov, sup, caps


def _r_template_turn(g, ctx):
    """The regulator stays; its movable supports are laid around it in the 8 poses of the template (4 quarter turns x mirrored or not,
    about the regulator's origin), legalised against every non-movable part, then the parts that are far or off the template are re-seated
    at their pins (an input capacitor on the named pin pair's side first). The identity pose is the plain re-seat."""
    from . import geom as GM, placetour as PT
    u = _unit_gap(g, ctx)
    if u is None:
        return []
    B, ic, c, mov, sup, caps = u
    only = set(caps if g["kind"] == "row-cin-side" else sup)
    if not only:
        return []
    cx, cy = B["parts"][ic]["pos"]
    out, seen = [], set()
    for mirror in (False, True):
        for k in range(4):
            tg = {}
            if k or mirror:
                for r in sup:
                    p = B["parts"][r]
                    dx, dy = p["pos"][0] - cx, p["pos"][1] - cy
                    rx, ry = GM.rot(-dx if mirror else dx, dy, 90.0 * k)
                    tg[r] = (cx + rx, cy + ry, ((180.0 - p["angle"]) if mirror else p["angle"]) + 90.0 * k)
                tg = {r: (x, y, a % 360.0) for r, (x, y, a) in tg.items()}
                mv = PT._legal(B, ctx["cons"], tg, mov)
                if not mv:
                    continue
            else:
                mv = {}
            B2 = _on_copy(B, mv)
            c2 = _unit(ctx, B2, ic)
            if c2 is None:
                continue
            _reseat_on(ctx, B2, c2, only=only)
            moves = _moves_between(B, B2)
            key = PT._key(moves) if moves else None
            if moves and key not in seen:
                seen.add(key)
                out.append({"id": f"template-turn-{'m' if mirror else 'r'}{90 * k}", "rung": "template-turn", "moves": moves, "claims": [],
                            "notes": f"{ic} unit laid {'mirrored, ' if mirror else ''}turned {90 * k} deg, then re-seated"})
    return out


def _r_unit_turn(g, ctx):
    """The whole module (regulator + its supports, intent.move_with_support) turned 90 / 180 / 270 deg about the regulator's origin so the
    named side faces other board; the parts far or off the template are re-seated at their pins in the new pose."""
    from . import placetour as PT
    u = _unit_gap(g, ctx, ("row-cin-side",))
    if u is None:
        return []
    B, ic, c, mov, sup, caps = u
    if ic not in mov:
        return []
    p = B["parts"][ic]
    out, seen = [], set()
    for k in (1, 2, 3):
        mv = _with_supports(B, ctx, mov, {ic: (p["pos"][0], p["pos"][1], (p["angle"] + 90.0 * k) % 360.0)})
        if not mv:
            continue
        B2 = _on_copy(B, mv)
        c2 = _unit(ctx, B2, ic)
        if c2 is not None:
            _reseat_on(ctx, B2, c2, only=set(sup))
        moves = _moves_between(B, B2)
        key = PT._key(moves) if moves else None
        if moves and key not in seen:
            seen.add(key)
            out.append({"id": f"unit-turn-{90 * k}", "rung": "unit-turn", "moves": moves, "claims": [], "notes": f"module of {ic} turned {90 * k} deg"})
    return out


def _strips(B, c, g, row, pairs=None):
    """Where the parts served by the row must stand: [(rect, depth)] beyond the regulator's courtyard, in front of the pin pair's span
    +- CIN_TOL_MM (cin_side, cin_loop_mm) or of the served pin (sw_mm, fb_mm); depth = the serving parts' courtyard + CLEAR_MM."""
    from . import clusters as CL
    icp = B["parts"][c["ic"]]
    ic_c = icp["court"]
    key = str(row.get("name", "")).rsplit(" ", 1)[-1]
    if g["kind"] == "row-cin-side" or key == "cin_loop_mm":
        rank, spans = 1, [(a, gnd) for a, gnd in (pairs if pairs is not None else ((c.get("layout") or {}).get("pairs") or []))]
    else:
        rank = 0 if key == "sw_mm" else 3
        spans = [(s["pad"], s["pad"]) for s in c["supports"] if s["sure"] and s["rank"] == rank]
    out = []
    for a, gnd in spans:
        pa = next((q for q in icp["pads"] if q["num"] == a), None)
        pg = next((q for q in icp["pads"] if q["num"] == gnd), None)
        if pa is None or pg is None:
            continue
        ox, oy = CL._outward(icp, pa)
        ext = [B["parts"][s["ref"]]["court"] for s in c["supports"] if s["sure"] and s["rank"] == rank]
        depth = max(((e[2] - e[0]) if ox else (e[3] - e[1])) for e in ext) + CLEAR_MM if ext else 1.0
        if ox:
            lo, hi = min(pa["y"], pg["y"]) - CL.CIN_TOL_MM, max(pa["y"], pg["y"]) + CL.CIN_TOL_MM
            rect = (ic_c[2], lo, ic_c[2] + depth, hi) if ox > 0 else (ic_c[0] - depth, lo, ic_c[0], hi)
        else:
            lo, hi = min(pa["x"], pg["x"]) - CL.CIN_TOL_MM, max(pa["x"], pg["x"]) + CL.CIN_TOL_MM
            rect = (lo, ic_c[3], hi, ic_c[3] + depth) if oy > 0 else (lo, ic_c[1] - depth, hi, ic_c[1])
        out.append((tuple(rect), (ox, oy)))
    return out


def _clear_picks(B, c, g, row, mov, serve, ic, pairs=None, together=3):
    """[(tag, [(blocker ref, (dx, dy))])]: each of the first 2 movable blockers standing in the row's strip alone (2 smallest ways out), and
    the first `together` (3) together. `serve` = the parts that belong in the strip (never moved out of it)."""
    ics = B["parts"][ic]
    plan = {}                                                     # blocker -> [(shift mm, (dx, dy))] smallest first
    for rect, (ox, oy) in _strips(B, c, g, row, pairs):
        for ref in sorted(mov - serve - {ic}):
            p = B["parts"][ref]
            if (p["side"] != ics["side"] and not p.get("tht")) or not _hit(rect, p["court"]):
                continue
            for vec in ((0.0, 1.0), (0.0, -1.0)) if ox else ((1.0, 0.0), (-1.0, 0.0)):
                d = clear_shift(p["court"], [rect], vec)
                if d is not None:
                    plan.setdefault(ref, []).append((d, (vec[0] * d, vec[1] * d)))
            d = clear_shift(p["court"], [rect], (ox, oy))
            if d is not None:
                plan.setdefault(ref, []).append((d, (ox * d, oy * d)))
    order = sorted(plan, key=lambda r: (min(d for d, _ in plan[r]), r))
    for r in order:
        plan[r] = sorted(plan[r], key=lambda t: (t[0], t[1]))[:2]
    picks = []
    for r in order[:2]:
        picks.extend([(f"{r}-{i}", [(r, v)]) for i, (_d, v) in enumerate(plan[r])])
    if len(order) > 1:
        picks.append(("all", [(r, plan[r][0][1]) for r in order[:together]]))
    return picks


def _r_clear_side(g, ctx):
    """A movable part standing in the strip the row's parts need (pin-pair span +- CIN_TOL_MM, depth = their courtyard + clearance) steps out
    by the minimum translation (clear_shift along either way of the package side, or outward); its supports follow; then the parts of the
    row are re-seated at their pins. Candidates: each of the first 2 blockers alone, and the first 3 together."""
    from . import placetour as PT
    u = _unit_gap(g, ctx)
    if u is None:
        return []
    B, ic, c, mov, sup, caps = u
    row = _row_of(ctx, g)
    serve = set(caps if g["kind"] == "row-cin-side" else sup)
    if not serve:
        return []
    picks = _clear_picks(B, c, g, row, mov, serve, ic)
    out, seen = [], set()
    for tag, mvs in picks:
        tg = {r: (B["parts"][r]["pos"][0] + v[0], B["parts"][r]["pos"][1] + v[1], B["parts"][r]["angle"]) for r, v in mvs}
        mv = _with_supports(B, ctx, mov, tg)
        if not mv:
            continue
        B2 = _on_copy(B, mv)
        c2 = _unit(ctx, B2, ic)
        if c2 is not None:
            _reseat_on(ctx, B2, c2, only=serve)
        moves = _moves_between(B, B2)
        key = PT._key(moves) if moves else None
        if moves and key not in seen:
            seen.add(key)
            out.append({"id": f"clear-side-{tag}", "rung": "clear-side", "moves": moves, "claims": [],
                        "notes": f"{', '.join(r for r, _v in mvs)} step out of the strip in front of {ic}"})
    return out


def _hit(a, b):
    from . import geom as GM
    return GM.overlap(a, b, -0.02)


def _r_re_deal(g, ctx):
    """Minimal `re-deal` (plan section 4 rung 5; the section's machinery is not in yet): for the bare pin, each of the regulator's own
    bypass parts of the net (nearest to the pin first, 2 at most) is served to that pin (clusters.pin_demand's row: ic, pad, net, parts)
    and re-seated by the seater (strict: within the support limit of the pin). A part owned by another cluster is dealt away from it."""
    from . import clusters as CL, powerbudget as PB
    row = _row_of(ctx, g)
    if g["kind"] != "row-pin-bypass" or not row or len(row.get("refs") or []) < 2 or not row.get("nets"):
        return []
    from . import placetour as PT
    B, cons, spec = ctx.B(), ctx["cons"], ctx["spec"]
    ic, net, parts = row["refs"][-1], row["nets"][0], set(row["refs"][:-1])
    dem = next((d for d in CL.pin_demand(B, cons, spec) if d["ic"] == ic and d["net"] == net and set(d["parts"]) == parts), None)
    if dem is None or ic not in B["parts"]:
        return []
    mov = set(PT._movable(B, cons, _P(ctx)))
    pin = next((q for q in B["parts"][ic]["pads"] if str(q["num"]) == str(dem["pad"]) and q["net"] == net), None)
    if pin is None:
        return []
    own = {s["ref"]: s for c in ctx.clusters() or [] for s in c["supports"]}
    dist = lambda r: min(math.hypot(q["x"] - pin["x"], q["y"] - pin["y"]) for q in B["parts"][r]["pads"] if q["net"] == net)
    out, seen = [], set()
    for ref in sorted([r for r in parts if r in mov and r in own], key=lambda r: (dist(r), r))[:2]:
        s = dict(own[ref], ic=ic, pad=pin["num"], how="pin-demand", sure=True, rank=9, dist=dist(ref))
        try:
            lim = PB.support_row(B, cons, spec, ref, net, None, ic=ic, pad=pin["num"])
            s.update(limit=lim["max"], target=lim["target"])
        except Exception:                                   # noqa: BLE001 - the part keeps the limit it had
            pass
        B2 = copy.deepcopy(B)
        _reseat_on(ctx, B2, {"ic": ic, "supports": [s]}, only={ref}, everything=True)
        moves = _moves_between(B, B2)
        key = PT._key(moves) if moves else None
        if moves and key not in seen:
            seen.add(key)
            out.append({"id": f"re-deal-{ref}-{pin['num']}", "rung": "re-deal", "moves": moves, "claims": [],
                        "notes": f"{ref} served to {ic}.{pin['num']} ({net}) and seated at the pin"})
    return out


def _cap_net_uf(part, net):
    from . import clusters as CL, netclass as NC
    if not (part["n_pads"] == 2 and any(q["net"] == net for q in part["pads"]) and any(NC.is_ground(q["net"] or "") for q in part["pads"])):
        return None
    return CL.cap_uf(part.get("value"))


def _cin_pool(ctx, B, ic, c, mov, owned, pin, net, rec):
    """The capacitors of `net` that can serve another input pair: [(owned by a cluster, dist to the pin, ref)] sorted; every capacitor of the net
    (and why it is not usable) is appended to rec["caps"], sorted usable first, nearest first."""
    from . import clusters as CL
    parts = B["parts"]
    need_uf = rec["need_uf"]
    owner = {s["ref"]: cc for cc in ctx.clusters() or [] for s in cc["supports"]}
    pool = []
    for ref in sorted(r for r, p in parts.items() if r.startswith("C") and r != ic):
        uf = _cap_net_uf(parts[ref], net)
        if uf is None or ref in owned:
            continue
        d = min(math.hypot(q["x"] - pin["x"], q["y"] - pin["y"]) for q in parts[ref]["pads"] if q["net"] == net)
        why = None
        if uf < need_uf - 1e-9:
            why = f"{uf:g} uF is below the input cap value {need_uf:g} uF"
        elif ref not in mov or parts[ref].get("locked"):
            why = "fixed (may not move)"
        else:
            oc = owner.get(ref)
            if oc is not None and oc["ic"] != ic:
                left = sum(1 for s in oc["supports"] if s["ref"] != ref and s["sure"] and s["net"] == net and s["ref"].startswith("C"))
                need = max(1, len((oc.get("layout") or {}).get("pairs") or []))
                if left < need:
                    why = f"owned by {oc['ic']}, which would keep {left} of the {need} cap(s) its rows need"
        rec["caps"].append({"ref": ref, "uf": uf, "dist_mm": round(d, 1), "why": why})
        if why is None:
            pool.append((owner.get(ref) is not None, round(d, 3), ref))
    rec["caps"].sort(key=lambda x: (x["why"] is not None, x["dist_mm"], x["ref"]))
    return sorted(pool)


def _cin_support(ctx, B, c, owned, ref, net, pad):
    """The support entry of capacitor `ref` served to input pad `pad` of the cluster `c` (limit from the one support table)."""
    from . import powerbudget as PB
    ic, cap = c["ic"], B["parts"][ref]
    via = next(q for q in cap["pads"] if q["net"] == net)
    s0 = next((x for cc in ctx.clusters() or [] for x in cc["supports"] if x["ref"] == ref), None)
    s = dict(s0 or {"cls": "power", "weight": 1.0, "role": "bypass", "nets": [q["net"] for q in cap["pads"]]}, ref=ref, ic=ic, pad=pad["num"], via=via["num"],
             net=net, how="pin-demand", sure=True, rank=1, dist=math.hypot(via["x"] - pad["x"], via["y"] - pad["y"]))
    try:
        lim = PB.support_row(B, ctx["cons"], ctx["spec"], ref, net, None, ic=ic, pad=pad["num"])
        s.update(limit=lim["max"], target=lim["target"])
    except Exception:                                   # noqa: BLE001 - the limit of the cluster's own input cap
        s.update(limit=next((x["limit"] for x in c["supports"] if x["ref"] in owned), 5.0), target=s.get("target", 5.0))
    return s


def _fixed_walls(B, ic, mov, side):
    """Courtyard rects that cannot move: fixed parts on the regulator's side (or through-hole) and the bounding boxes of the track-forbidding
    keep-out zones that close that side."""
    out = [p["court"] for r, p in B["parts"].items() if r != ic and r not in mov and (p["side"] == side or p.get("tht"))]
    for z in B.get("zones") or []:
        if z.get("keepout") and z.get("no_tracks") and z.get("pts") and side in (z.get("layers") or ()):
            xs, ys = [q[0] for q in z["pts"]], [q[1] for q in z["pts"]]
            out.append((min(xs), min(ys), max(xs), max(ys)))
    return out


def _depth_deficit(B, ic, rect, normal, depth, walls):
    """How much shorter than `depth` the free run in front of the regulator's package edge is along `normal` (a strip `rect` wide), because a wall
    stands in it: depth - free, 0 when nothing in the strip's width is nearer than depth. Walls behind the package edge do not count."""
    ox, oy = normal
    ic_c = B["parts"][ic]["court"]
    free = depth
    for w in walls:
        if ox:
            if not (w[1] < rect[3] and w[3] > rect[1]):
                continue
            edge = (w[0] - ic_c[2]) if ox > 0 else (ic_c[0] - w[2])
            near = (w[2] > ic_c[2]) if ox > 0 else (w[0] < ic_c[0])
        else:
            if not (w[0] < rect[2] and w[2] > rect[0]):
                continue
            edge = (w[1] - ic_c[3]) if oy > 0 else (ic_c[1] - w[3])
            near = (w[3] > ic_c[3]) if oy > 0 else (w[1] < ic_c[1])
        if near:
            free = min(free, max(0.0, edge))
    return round(depth - free, 3)


SHIFT_LADDER_MM = (0.0, 0.5, 1.0, 1.5, 2.5)     # unit-shift: travel beyond the depth deficit (a flat cap needs the strip's depth, the supports in it need room)


def _strip_block(ctx, B2, g, row, ic, pr, ref, sup, owned, seat_cap, refused):
    """The module's own supports (`sup`, movable and confidently owned) whose courtyards stand in the strip of pin pair `pr` leave it as one
    block: they are lifted off the board model, `seat_cap` seats the input cap `ref` in the emptied strip, then the lifted parts re-seat at
    their pins (clusters.reseat, strict) wherever a legal seat is free: the strip is taken now, so that is another side of the IC.
    -> the model copy, or None (no strip part, the cap does not seat, or a lifted part has no seat within its limit)."""
    from . import geom as GM
    c2 = _unit(ctx, B2, ic)
    strips = _strips(B2, c2, g, row, [pr]) if c2 is not None else []
    if not strips:
        return None
    rect = strips[0][0]
    blk = [r for r in sup if r != ref and r not in owned and r in B2["parts"] and _hit(rect, B2["parts"][r]["court"])]
    if not blk:
        return None
    B3 = copy.deepcopy(B2)
    x0, y0 = B3["bbox"][0], B3["bbox"][1]
    for i, r in enumerate(blk):                                # parked clear of the board and of one another
        GM.place(B3["parts"][r], x0 - 30.0 - 10.0 * i, y0 - 30.0, B3["parts"][r]["angle"])
    c3 = _unit(ctx, B3, ic)
    if c3 is None or not seat_cap(B3, c3, next(q for q in B3["parts"][ic]["pads"] if q["num"] == pr[0])):
        return None
    c4, left = _unit(ctx, B3, ic), {}
    if c4 is None:
        return None
    _reseat_on(ctx, B3, c4, only=set(blk), everything=True, refused=left)
    if any(B3["parts"][r]["pos"][0] < x0 - 5.0 for r in blk):   # one that found no seat is still parked: the block does not fit
        refused.update({r: f"no free seat after the strip emptied ({left.get(r, '?')})" for r in blk if B3["parts"][r]["pos"][0] < x0 - 5.0})
        return None
    return B3


def _r_unit_shift(g, ctx):
    """A fixed part (or keep-out) stands in the strip an unserved input pin pair needs, closer than the capacitor's courtyard depth + clearance:
    the whole module (regulator + supports, `_with_supports`) moves away from the blocker, against the pair side's outward normal, by the depth
    deficit + SHIFT_LADDER_MM (0 / 0.5 / 1.0 / 1.5 / 2.5), and by (deficit + 0.5) mm of normal travel at either 45 deg; then the input cap of that
    side is seated there (an owned cap that serves no pair, else the cin-re-deal pick: a free cap of the net with a value >= the template's input cap).
    The module's own supports that stand in that strip travel as a block (`_strip_block`): they are lifted out, the cap takes the strip, and they
    re-seat at their pins on whatever side is free; only when that fails do they step out by the least translation (into the neighbours)."""
    from . import clusters as CL
    u = _unit_gap(g, ctx, ("row-cin-side",))
    if u is None:
        return []
    B, ic, c, mov, sup, _caps = u
    if ic not in mov:
        return []
    icp, parts = B["parts"][ic], B["parts"]
    pairs = (c.get("layout") or {}).get("pairs") or []
    owned = sorted(s["ref"] for s in c["supports"] if s["sure"] and s["rank"] == 1 and s["ref"].startswith("C"))
    bare = [pr for pr in pairs if not any(CL._on_cin_side(icp, parts[r]["court"], [pr], pr[0], parts[r]["pads"]) for r in owned)]
    if not bare:
        return []
    free_owned = [r for r in owned if r in sup and not any(CL._on_cin_side(icp, parts[r]["court"], [pr], pr[0], parts[r]["pads"]) for pr in pairs)]
    walls = _fixed_walls(B, ic, mov, icp["side"])
    row = _row_of(ctx, g)
    out, seen, why = [], set(), []
    for pr in bare[:2]:
        pin = next((q for q in icp["pads"] if q["num"] == pr[0]), None)
        strips = _strips(B, c, g, row, [pr])
        if pin is None or not pin["net"] or not strips:
            continue
        rect, (ox, oy) = strips[0]
        depth = (rect[2] - rect[0]) if ox else (rect[3] - rect[1])
        net = pin["net"]
        need_uf = min([CL.cap_uf(parts[r].get("value")) or 0.0 for r in owned] or [0.0])
        rec = {"need_uf": need_uf, "caps": []}
        pool = [r for _o, _d, r in _cin_pool(ctx, B, ic, c, mov, owned, pin, net, rec)]
        cap_order = (free_owned[:1] + pool[:2])
        if cap_order:                                          # the capacitor that will stand there sets the depth
            cc = parts[cap_order[0]]["court"]
            depth = max(depth, ((cc[2] - cc[0]) if ox else (cc[3] - cc[1])) + CLEAR_MM)
        deficit = _depth_deficit(B, ic, rect, (ox, oy), depth, walls)
        if deficit <= 0:
            why.append(f"{pr[0]}/{pr[1]}: no fixed part or keep-out shortens the strip")
            continue
        c45 = math.sqrt(0.5)
        moves_to = [(f"{e:g}", (-ox * (deficit + e), -oy * (deficit + e))) for e in SHIFT_LADDER_MM]
        for sg in (1.0, -1.0):                                 # the two 45 deg variants: normal travel deficit + 0.5 mm
            t = (deficit + 0.5) / c45
            vx, vy = (-ox * c45 - sg * oy * c45, -oy * c45 + sg * ox * c45)
            moves_to.append((f"d{'p' if sg > 0 else 'n'}", (vx * t, vy * t)))
        for tag, (vx, vy) in moves_to:
            mv = _with_supports(B, ctx, mov, {ic: (icp["pos"][0] + vx, icp["pos"][1] + vy, icp["angle"])})
            if not mv:
                why.append(f"{pr[0]}/{pr[1]}: the module cannot move {math.hypot(vx, vy):.1f} mm away from the blocker legally")
                continue
            for ref in cap_order:
                B2 = _on_copy(B, mv)
                ic2 = B2["parts"][ic]
                pad2 = next(q for q in ic2["pads"] if q["num"] == pr[0])
                c2 = _unit(ctx, B2, ic)
                if c2 is None:
                    continue
                refused = {}

                def seat_cap(B2, c2, pad2):
                    if ref in free_owned:
                        _reseat_on(ctx, B2, c2, only={ref}, everything=True, refused=refused)
                    else:
                        s_ = _cin_support(ctx, B2, c2, owned, ref, net, pad2)
                        _reseat_on(ctx, B2, {"ic": ic, "supports": [s_], "layout": c["layout"]}, only={ref}, everything=True, refused=refused)
                    return CL._on_cin_side(B2["parts"][ic], B2["parts"][ref]["court"], [pr], pr[0], B2["parts"][ref]["pads"])
                ok = seat_cap(B2, c2, pad2)
                if not ok:                                         # the strip's own supports travel as a block: lifted, the cap seats, they re-seat on a free side
                    B3 = _strip_block(ctx, B2, g, row, ic, pr, ref, sup, owned, seat_cap, refused)
                    if B3 is not None:
                        B2, ok = B3, True
                if not ok:                                         # the module's own parts that came along stand in the strip: they step out, then the cap seats
                    pk = _clear_picks(B2, c2, g, row, mov, set(owned) | {ref}, ic, [pr], together=8)
                    pre = _with_supports(B2, ctx, mov, {r: (B2["parts"][r]["pos"][0] + v[0], B2["parts"][r]["pos"][1] + v[1], B2["parts"][r]["angle"]) for r, v in pk[-1][1]}) if pk else None
                    if pre:
                        B3 = _on_copy(B2, pre)
                        c3 = _unit(ctx, B3, ic)
                        if c3 is not None and seat_cap(B3, c3, next(q for q in B3["parts"][ic]["pads"] if q["num"] == pr[0])):
                            B2, ok = B3, True
                if not ok:
                    why.append(f"{pr[0]}/{pr[1]}: shifted {math.hypot(vx, vy):.1f} mm, {ref} still has no seat on the side ({refused.get(ref, 'off the side')})")
                    continue
                moves = _moves_between(B, B2)
                key = _pt_key(moves)
                if key not in seen:
                    seen.add(key)
                    out.append({"id": f"unit-shift-{pr[0]}-{tag}-{ref}", "rung": "unit-shift", "moves": moves, "claims": [],
                                "notes": f"module of {ic} {math.hypot(vx, vy):.1f} mm away from the blocker (deficit {deficit:g} mm), {ref} seated on the {pr[0]}/{pr[1]} side"})
                break
    if not out:
        ctx.setdefault("why_none", {})[("unit-shift", g["id"])] = "; ".join(dict.fromkeys(why))[:300] or "no unserved pair"
    return out


def _r_cin_re_deal(g, ctx):
    """Named input pin pairs outnumber the input caps the cluster owns: another capacitor of the pair's net, with a value >= the template's
    input cap, is served to the unserved pair and seated on its side (clusters.reseat only=, strict). An unowned one first (nearest to the
    pin), then one owned by another cluster, only when that regulator keeps at least the caps of its own rows (pairs, at least 1) on the net.
    Every candidate cap and why it was not usable goes to ctx["cin_need"][gap id] (the owner question after the loop)."""
    from . import clusters as CL, powerbudget as PB
    u = _unit_gap(g, ctx, ("row-cin-side",))
    if u is None:
        return []
    B, ic, c, mov, _sup, _caps = u
    pairs = (c.get("layout") or {}).get("pairs") or []
    icp, parts = B["parts"][ic], B["parts"]
    owned = sorted(s["ref"] for s in c["supports"] if s["sure"] and s["rank"] == 1 and s["ref"].startswith("C"))
    if len(pairs) <= len(owned):
        return []
    bare = [pr for pr in pairs if not any(CL._on_cin_side(icp, parts[r]["court"], [pr], pr[0], parts[r]["pads"]) for r in owned)]
    if not bare:
        return []
    pin = next((q for q in icp["pads"] if q["num"] == bare[0][0]), None)
    if pin is None or not pin["net"]:
        return []
    net = pin["net"]
    need_uf = min([CL.cap_uf(parts[r].get("value")) or 0.0 for r in owned] or [0.0])
    rec = {"ic": ic, "net": net, "pairs": len(pairs), "owned": owned, "need_uf": need_uf, "caps": []}
    ctx.setdefault("cin_need", {})[g["id"]] = rec
    pool = _cin_pool(ctx, B, ic, c, mov, owned, pin, net, rec)
    out, seen = [], set()
    for _o, _d, ref in sorted(pool)[:3]:
        cap = parts[ref]
        for pr in bare[:2]:
            pad = next(q for q in icp["pads"] if q["num"] == pr[0])
            s = _cin_support(ctx, B, c, owned, ref, net, pad)
            row = _row_of(ctx, g)
            variants = [("", None)] + [(f"-{tag}", mvs) for tag, mvs in _clear_picks(B, c, g, row, mov, set(owned) | {ref}, ic, [pr], together=8)[::-1][:3]]
            whys = []
            for vtag, mvs in variants:
                pre = {}
                if mvs:                                           # the blockers in the pair's strip step out first
                    tg = {r: (B["parts"][r]["pos"][0] + v[0], B["parts"][r]["pos"][1] + v[1], B["parts"][r]["angle"]) for r, v in mvs}
                    pre = _with_supports(B, ctx, mov, tg)
                    if not pre:
                        whys.append(f"{', '.join(r for r, _v in mvs)} cannot step out legally")
                        continue
                B2, refused = _on_copy(B, pre), {}
                _reseat_on(ctx, B2, {"ic": ic, "supports": [s], "layout": c["layout"]}, only={ref}, everything=True, refused=refused)
                moves = _moves_between(B, B2)
                if ref not in moves:
                    whys.append(f"no legal seat on the {pr[0]}/{pr[1]} side: {refused.get(ref, 'none')}" + (f" after {', '.join(r for r, _v in mvs)} step out" if mvs else ""))
                    continue
                if not CL._on_cin_side(B2["parts"][ic], B2["parts"][ref]["court"], [pr], pr[0], B2["parts"][ref]["pads"]):
                    whys.append(f"seat not on the {pr[0]}/{pr[1]} side" + (f" after {', '.join(r for r, _v in mvs)} step out" if mvs else ""))
                    continue
                key = _pt_key(moves)
                if key not in seen:
                    seen.add(key)
                    d0 = min(math.hypot(q["x"] - pad["x"], q["y"] - pad["y"]) for q in cap["pads"] if q["net"] == net)
                    out.append({"id": f"cin-re-deal-{ref}-{pr[0]}{vtag}", "rung": "cin-re-deal", "moves": moves, "claims": [],
                                "notes": f"{ref} {d0:.1f} mm from {ic}.{pr[0]}, served to the {pr[0]}/{pr[1]} pair" + (f"; {', '.join(r for r, _v in mvs)} step out of the strip" if mvs else "")})
                    whys = []
                    break
            if whys:
                next(q for q in rec["caps"] if q["ref"] == ref)["why"] = "; ".join(dict.fromkeys(whys))[:240]
    if not out:
        ctx.setdefault("why_none", {})[("cin-re-deal", g["id"])] = "; ".join(f"{x['ref']}: {x['why']}" for x in rec["caps"][:6]) or f"no capacitor of {net} is a candidate"
    return out


def _pt_key(moves):
    from . import placetour as PT
    return PT._key(moves)


def cin_questions(ctx, res):
    """After `close`: a cin_side row still not honoured gets one owner question `row:<name>` with the numbers (pairs, caps owned, each candidate
    cap and why it was refused). Nothing is applied here. -> [record]."""
    S = ctx.get("session")
    out = []
    for g in res.get("gaps_out") or []:
        rec = (ctx.get("cin_need") or {}).get(g["id"])
        if g["kind"] != "row-cin-side" or not rec:
            continue
        out.append(rec)
        if S is None:
            continue
        name = (g["row"] or g["id"]).split(":", 1)[-1]
        caps = "; ".join(f"{x['ref']} {x['uf']:g} uF {x['dist_mm']:g} mm: {x['why'] or 'seat refused'}" for x in rec["caps"][:6]) or "none on the net"
        S.ask(f"row:{name}", "input capacitor side",
              f"{rec['ic']} names {rec['pairs']} input pin pair(s) on separate sides, the cluster owns {len(rec['owned'])} input cap(s) ({', '.join(rec['owned']) or '-'}); "
              f"candidates on {rec['net']} (>= {rec['need_uf']:g} uF): {caps}",
              {"assign": f"name the cap yourself: [power.\"{rec['net']}\"] support = {{REF = \"{rec['ic']}.PAD\"}} (and unfix it if it is fixed)",
               "add": "add an input capacitor of at least that value on the unserved pair's side to the netlist",
               "waive": "accept one pair served, with the reason on record"},
              "assign", "the next floorplan run")
    return out


DISPATCH["row-cin-side"] = ["template-turn", "unit-turn", "clear-side", "unit-shift", "cin-re-deal"]
DISPATCH["row-template"] = ["template-turn", "clear-side"]
DISPATCH["row-pin-bypass"] = ["re-deal"]
RUNGS.update({"template-turn": _r_template_turn, "unit-turn": _r_unit_turn, "clear-side": _r_clear_side, "re-deal": _r_re_deal, "cin-re-deal": _r_cin_re_deal, "unit-shift": _r_unit_shift})


class JudgeEval(Evaluator):
    """Placetour's evaluator: a candidate is applied (placetour tier-1: guard, carried copper, gate state never worse than the base),
    judged by the class-first KRT ghost (placetour._worker, parallel), and its gaps are measured again on the candidate board. The
    base moves only on commit. Legality: tier-1 gates for every candidate, KiCad DRC against the input for each judged candidate
    that would improve the score (`drc_new`; any new item refuses it)."""

    def __init__(self, ctx, a_cfg, board, blame, key, workdir, P, input_board=None, confirm=None, jobs_fn=None):
        from . import placetour as PT
        self.ctx, self.acfg, self.P, self.work = ctx, a_cfg, P, workdir
        self.board_path, self.blame, self.key, self.n = board, blame, tuple(key), 0
        self.input_board = input_board or board
        self.confirm, self.jobs_fn = confirm, jobs_fn
        self.drc_ref = None
        self.tier1_state = None
        self.gaps = None
        self._sync()
        self.PT = PT

    def _sync(self):
        self.ctx["board"] = self.board_path
        for k in ("B", "_rows", "_intent", "_cl", "_far", "errors"):
            self.ctx.pop(k, None)
        self.ctx["blame"] = self.blame

    def _measure(self, board, blame, cons_ctx=None):
        """Ghost gaps from the judge blame + the placement gaps (rows, supports, side) on `board`."""
        c = Ctx(stage="placetour", board=board, cons=self.ctx["cons"], spec=self.ctx["spec"], ds_dir=self.ctx.get("ds_dir"), blame=blame)
        r = detect(c, [k for k in ORDER if k in SCOPE["placetour"]])
        return r["gaps"], c

    def detect(self, kinds):
        if self.gaps is None:
            self.gaps, c = self._measure(self.board_path, self.blame)
            if self.ctx.get("diagnosed"):
                prev = {g["id"]: g for g in self.ctx["diagnosed"]}
                for g in self.gaps:
                    if g["id"] in prev:
                        g.update({k: prev[g["id"]][k] for k in prev[g["id"]] if k in ("cause", "blockers", "blockers_movable", "causes", "corridor_mm", "width_mm")})
            else:
                diagnose(c, self.gaps, self.confirm)
        return list(self.gaps)                 # every kind measured: trials return the same set, so scores compare like with like

    def state(self):
        k = self.key
        return {"illegal": 0, "open": k[3], "hot": k[4], "hot_occ": k[5], "vias": k[6], "detour": k[7]}

    def board(self):
        return self.ctx.B()

    def trial(self, cand):
        return self.trials([cand])[0]

    def trials(self, cands):
        from . import lookahead as LA
        PT = self.PT
        self.n += 1
        rdir = os.path.join(self.work, f"close{self.n}")
        B0 = self.ctx.B()
        base_state = PT.gate_state(B0, self.ctx["cons"], self.ctx["spec"])
        surv, dropped = PT.tier1(self.board_path, cands, self.ctx["cons"], self.ctx["spec"], self.P, rdir, base_state)
        why = {c["id"]: r for c, r in dropped}
        pro = os.path.splitext(self.input_board)[0] + ".kicad_pro"           # the project (rules, netclasses) travels with every candidate
        for _k, _c, path, _r in surv:
            if os.path.exists(pro):
                shutil.copyfile(pro, os.path.splitext(path)[0] + ".kicad_pro")
        jobs = []
        for _k, cnd, path, _rem in surv:
            jd = LA._mk(os.path.join(rdir, "judge", cnd["id"]))
            job = {"tag": f"c{self.n}-{cnd['id']}", "board": path, "dir": jd, "result": os.path.join(jd, "result.json"), "a": self.acfg,
                   "judge_min": self.P["judge_min"], "critical": []}
            job["jobfile"] = os.path.join(LA._mk(os.path.join(rdir, "jobs")), f"{cnd['id']}.json")
            with open(job["jobfile"], "w") as fh:
                json.dump(job, fh, default=sorted)
            jobs.append(job)
        run = self.jobs_fn or (lambda js: LA._run_jobs(js, self.P, self.P["judge_min"] * 60 + 600, entry="placetour:_worker"))
        results = run(jobs) if jobs else []
        by = {}
        from . import relieve as RL
        try:
            A = RL.attached_copper(B0) if surv else {"segs": {}, "vias": {}}
        except Exception:                                   # noqa: BLE001 - no attached-copper map: no removed-copper refs
            A = {"segs": {}, "vias": {}}
        for (_k, cnd, path, rem), res in zip(surv, results):
            if not res.get("key"):
                by[cnd["id"]] = {"gaps": [], "state": {}, "refusal": "judge:no routed copy " + str(res.get("error") or "")[:60]}
                continue
            gaps, c = self._measure(path, res["blame"])
            diag = [g for g in gaps if g["kind"] == "ghost-open"]
            tr = {"gaps": gaps, "state": {"illegal": 0, "open": res["key"][3], "hot": res["key"][4], "hot_occ": res["key"][5],
                                          "vias": res["key"][6], "detour": res["key"][7]},
                  "refusal": None, "board": path, "res": res, "diag": diag,
                  "removed_refs": sorted({ref for i in rem["segs"] for _k2, ref, _n in A["segs"].get(i, []) if ref in cnd["moves"]}
                                         | {ref for j in rem["vias"] for ref, _n in A["vias"].get(j, []) if ref in cnd["moves"]})}
            by[cnd["id"]] = tr
        out = []
        for cnd in cands:
            tr = by.get(cnd["id"]) or {"gaps": [], "state": {}, "refusal": why.get(cnd["id"], "legal:tier1")}
            if not tr["refusal"] and str(why.get(cnd["id"], "")).startswith("worse"):
                tr["refusal"] = "legal:" + why[cnd["id"]]
            if tr["refusal"] and not str(tr["refusal"]).startswith(("legal", "drc", "judge")):
                tr["refusal"] = "legal:" + str(tr["refusal"])
            out.append(tr)
        self._drc_gate(out)
        return out

    def _drc_gate(self, trs):
        """KiCad DRC against the input board for each judged candidate that would improve the score; a new item refuses it (fails closed)."""
        from . import legalgate as LG
        base = {"gaps": self.gaps or [], "state": self.state()}
        for tr in trs:
            if tr.get("refusal") or tr.get("board") is None or not score(tr["gaps"], tr["state"]) < score(base["gaps"], base["state"]):
                continue
            try:
                if self.drc_ref is None:
                    self.drc_ref = LG._drc(self.input_board, pro_from=self.input_board) or []
                d = LG.drc_new(tr["board"], self.input_board, ref=self.drc_ref)
            except Exception as e:                                  # noqa: BLE001
                d = None
                tr["drc_error"] = f"{type(e).__name__}: {e}"
            tr["drc"] = None if d is None else {"n": d["n"], "pairs": d["pairs"][:6]}
            if d is None:
                tr["refusal"] = "drc:not-run (kicad-cli gave no answer: fails closed)"
            elif d["items"]:
                tr["refusal"] = "drc:" + ",".join(f"{t} {n}" for t, n in sorted(d["n"].items())) + " " + ";".join("/".join(p) for p in d["pairs"][:3])

    def commit(self, tr):
        self.board_path, self.blame, self.key = tr["board"], tr["res"]["blame"], tuple(tr["res"]["key"])
        self.routed = tr["res"].get("routed")
        prev = {g["id"]: g for g in (self.gaps or [])}
        self.gaps = tr["gaps"]
        self._sync()
        c = Ctx(stage="placetour", board=self.board_path, cons=self.ctx["cons"], spec=self.ctx["spec"], blame=self.blame)
        carry = [g for g in self.gaps if g["kind"] == "ghost-open"]
        for g in carry:
            if g["id"] in prev:                             # same net still open: it keeps its diagnosis class; blockers are re-derived
                g["cause"] = prev[g["id"]].get("cause", "unconfirmed")
        try:
            diagnose(c, carry, None)
        except Exception:                                   # noqa: BLE001
            pass
        for g in carry:                                     # diagnose without a router can only say "unconfirmed": a carried id keeps its own cause
            if g["id"] in prev and prev[g["id"]].get("cause"):
                g["cause"] = prev[g["id"]]["cause"]
        self.ctx["diagnosed"] = self.gaps
