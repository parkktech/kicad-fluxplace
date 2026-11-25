"""PLACEGAPS: the one placement dispatcher (docs/PLAN-PLACE-GAPS.md), the placement twin of `gaps.close`.

A gap is a measured shortfall of a placement ({id, kind, class, mag, refs, nets, at, row, detail}); DETECT[kind] finds them from
data the stages already have; DISPATCH[kind] lists rung names, RUNGS[name](gap, ctx) -> [candidate] proposes moves (a rung never
writes a board and never judges itself); an evaluator applies and judges a candidate; `_accept` is the one accept rule;
`close` is the loop. Phase 0: detectors for every kind, the loop and the accept rule (tested with fake evaluators), memo,
report files and narration; DISPATCH is empty, so `detect_stage` (the only thing the stages call) is detect-only: it writes
report files and prints a table, never a board, never raises. Board-agnostic: no reference, net or distance of a board here.
"""
import json
import math
import os
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
            out.append(gap(row_kind(r), f"{r['name']}|{r.get('metric')}", "plain", _excess(r), r.get("refs") or (), r.get("nets") or (),
                           row=f"{r['kind']}:{r['name']}", detail=f"{r.get('metric')} {r.get('value')} limit {r.get('max') if r.get('max') is not None else r.get('target')}"))
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

    def pose_key(self, g):                              # -> hashable of the poses around the gap (memo)
        return pose_hash(self.board(), g)

    def board(self):
        raise NotImplementedError


def close(ctx, kinds, ev, stage="placegaps", clock=time.time):
    """rounds x kinds (ORDER) x rungs (yield order) x gaps (worst first). -> {stop, accepted, refused, gaps_in, gaps_out, score_in,
    score_out, rounds}. Stops: no gap in scope, no rung, no gain N rounds, stalled, budget, rung cap, max rounds (a time stop is
    named; it is the one non-deterministic outcome)."""
    from . import gaps as G
    cons = ctx.get("cons") or {}
    flow = cons.get("flow") or {}
    no_gain = int(flow.get("no_gain_rounds", G.NO_GAIN_ROUNDS))
    stall_s = float(flow.get("stall_min", G.STALL_MIN)) * 60
    budget_s = float(flow.get("budget_min") or 0) * 60
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
        gain = False
        for kind in kinds:
            rungs = [x for x in DISPATCH.get(kind, []) if x in RUNGS]
            for rung in G._rank(kind, rungs, yields):
                t_rung = clock()
                for g in [x for x in base["gaps"] if x["kind"] == kind]:
                    if budget_s and clock() - t0 > budget_s:
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
                        out["refused"].append({"gap": g["id"], "rung": rung, "check": "memo", "why": "same poses tried"})
                        continue
                    memo.add(key)
                    for cand in sorted(RUNGS[rung](g, ctx) or [], key=lambda c: c["id"])[:CANDS_PER_RUNG]:
                        tr = ev.trial(cand)
                        ok, check, why = _accept(base, tr, g["id"])
                        yields.setdefault(kind, {}).setdefault(rung, [0, 0])
                        yields[kind][rung][0] += 1
                        if ok:
                            yields[kind][rung][1] += 1
                            before = [x["id"] for x in base["gaps"]]
                            ev.commit(tr)
                            base = {"gaps": _sort(tr["gaps"]), "state": tr.get("state") or {}, "refusal": None}
                            out["accepted"].append({"gap": g["id"], "rung": rung, "candidate": cand["id"], "why": why,
                                                    "refs": sorted(cand.get("moves") or {}), "gaps_before": len(before), "gaps_after": len(base["gaps"])})
                            EV.emit("parts", stage=stage, i=r, refs=sorted(cand.get("moves") or {}), why=f"{kind}/{rung}")
                            _narr(stage, f"{stage}: {kind} {g['id'].split(':', 1)[1]} repaired by {rung}")
                            gain, last = True, clock()
                            break
                        out["refused"].append({"gap": g["id"], "rung": rung, "candidate": cand["id"], "check": check, "why": why})
                if out["stop"]:
                    break
            if out["stop"]:
                break
        sc = score(base["gaps"], base["state"])
        EV.iteration("place", r, n=max_rounds, metrics=dict(zip(TERMS, sc)), note="placegaps")
        stale = 0 if gain else stale + 1
        if out["stop"]:
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
                 confirm=None):
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
    rects, total = [], 0.0
    for net in g["nets"]:
        pts = {(p["ref"], p["num"]): (p["x"], p["y"]) for p in B["pads"] if p["net"] == net}
        for a, b, d in BG.mst_edges(pts):
            if g["refs"] and a[0] not in g["refs"] and b[0] not in g["refs"]:
                continue
            total += d
            rects += ES.seg_rects(pts[a], pts[b], width)
    return {"rects": rects, "len_mm": round(total, 2), "width_mm": round(width, 3)}


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
