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
         "row-other", "support-far", "support-starved", "side-illegal", "ghost-costly", "ghost-hot", "part-datasheet", "part-stock")
CLASS_RANK = {"hs-pair": 0, "rf": 1, "power": 2, "pair": 3, "plain": 4}
SCOPE = {"ready": ("part-datasheet", "part-stock"),
         "floorplan": tuple(k for k in ORDER if not k.startswith(("part-", "ghost-"))),
         "placetour": tuple(k for k in ORDER if not k.startswith("part-"))}
TERMS = ("hs_open", "rf_open", "power_open", "illegal", "hard_rows", "hard_mag", "decap_far", "support_far", "far_mm",
         "open", "hot", "hot_occ", "vias", "detour")
R6_TERMS = ("hs_open", "rf_open", "power_open", "illegal", "hard_rows")        # never worse, whatever the target gap gained
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
    out = []
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
    for r in ctx.intent_rows() or []:
        if r.get("hard") and r.get("met") is False and r.get("kind") != "support":      # support rows: the support-far gaps carry them
            out.append(gap(row_kind(r, True), r["name"], "plain", _excess(r), r.get("refs") or (), (), row=f"intent:{r['kind']}:{r['name']}",
                           detail=f"{r['kind']} {r.get('value')} limit {r.get('limit')}; {r.get('detail', '')}"))
    return out


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
