"""fabconform: make a routed board build FIRST TIME at every selected fab house, and say what is left per house.

For every copper item under any selected house's published minimum (track width, via annular ring, PTH ring, drill, clearance)
a guarded FIX is tried on a text copy of the board:

  track   widen the segment to the strictest house track minimum (ladder: strictest, then each lower house minimum). A
          controlled-impedance segment (a [pairs] leg, or an [rf] net) is checked with the project's own impedance model
          (impedance.py on the board stackup) at the old and the new width, the pair gap shrinking by the width growth
          (centreline fixed); accepted only inside the net class tolerance, else it stays and the impedance-limited width is reported.
  via     smaller standard drill and/or bigger pad so ring >= the strictest house via ring (power/zone nets keep their drill).
          Candidates are ordered by pad growth.

All edits are applied as a batch, DRC'd (drcguard: no key may occur more often than on the base, shorts and opens too), the
edits sitting next to a NEW violation go back one rung down their ladder, and the loop repeats. What cannot be fixed becomes a
per-house ORDER-OPTION (the cheapest published tier / process of that house that accepts the value, with its URL and date) or
IMPOSSIBLE (no published option). Nothing here names a board, a net or a number: limits come from fabcaps + data/fabconform_options.json.
"""
import json
import math
import os
import re
import shutil
import tempfile
from collections import Counter, defaultdict

from . import boardwrite as BW
from . import drcguard as DG
from . import fabcaps as FC

EPS = 1e-6
DRILL_STEP = 0.05                          # standard drill grid (0.2 / 0.25 / 0.3 ...)
PAD_STEP = 0.005
UNCOUPLED_GAP = 5.0                        # mm: a pair leg with no partner beside it is a lone microstrip (coupling term ~ 0)
HOUSE_DEFAULT_TIER = "standard"
_OPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fabconform_options.json")

KIND_LABEL = {"track": "track width", "via_ring": "via annular ring", "pth_ring": "PTH annular ring", "drill": "drill",
              "space": "clearance"}
LIMIT_KEY = {"track": "track_min", "via_ring": "via_annular_min", "pth_ring": "pth_annular_min", "drill": "drill_min",
             "space": "space_min"}


def options_data():
    with open(_OPT) as f:
        return json.load(f)


# ---------------------------------------------------------------- houses
def resolve_houses(spec):
    """'jlcpcb,pcbway' or 'pcbway-fine-drill,jlcpcb' -> [(house, profile name)]. A bare vendor means its standard tier."""
    out = []
    for h in [x.strip() for x in str(spec).split(",") if x.strip()]:
        name = f"{h}-{HOUSE_DEFAULT_TIER}" if h in FC.data()["vendors"] else h
        v, _t = FC.split(name)                           # FabProfileError, loud, on an unknown name
        out.append((v, name))
    return out


def house_limits(houses, layers):
    """{house: limits dict of its profile row at `layers` copper layers}."""
    return {h: FC.limits(p, layers)["limits"] for h, p in houses}


def strictest(lims, key):
    vals = [l[key] for l in lims.values() if l.get(key) is not None]
    return max(vals) if vals else None


# ---------------------------------------------------------------- board text
_NUM = r"(-?[\d.]+)"
_SEG = re.compile(rf'\(start {_NUM} {_NUM}\)\s*\(end {_NUM} {_NUM}\)\s*\(width ([\d.]+)\)\s*\(layer "([^"]+)"\)')
_VIA_AT = re.compile(rf"\(at {_NUM} {_NUM}\)")
_NETN = re.compile(r'\(net "([^"]*)"\)')


def parse(text):
    """-> (segments, vias): dicts with span, geometry, net. Only plain segments and through vias are touched later."""
    root = text.index("(")
    segs, vias = [], []
    for s, e in BW._children(text, root, BW._end(text, root)):
        if text.startswith("(segment", s):
            node = text[s:e]
            m, n = _SEG.search(node), _NETN.search(node)
            if m and n:
                x1, y1, x2, y2, w = (float(m.group(i)) for i in range(1, 6))
                segs.append({"span": (s, e), "x1": x1, "y1": y1, "x2": x2, "y2": y2, "w": w, "layer": m.group(6),
                             "net": n.group(1), "locked": "(locked" in node})
        elif text.startswith("(via", s) and not text[s + 4].isalnum():
            node = text[s:e]
            a, sz, dr, n = _VIA_AT.search(node), re.search(r"\(size ([\d.]+)\)", node), re.search(r"\(drill ([\d.]+)\)", node), _NETN.search(node)
            if a and sz and dr and n:
                through = not re.search(r"\((blind|micro|buried)\b", node)
                vias.append({"span": (s, e), "x": float(a.group(1)), "y": float(a.group(2)), "size": float(sz.group(1)),
                             "drill": float(dr.group(1)), "net": n.group(1), "through": through, "locked": "(locked" in node})
    return segs, vias


def apply_edits(text, edits):
    """edits: [(span, {"width": w} | {"size": s, "drill": d})] -> new text (spans from `parse(text)`, applied back to front)."""
    for (s, e), ch in sorted(edits, key=lambda x: -x[0][0]):
        node = text[s:e]
        for key, val in ch.items():
            node = re.sub(rf"\({key} [\d.]+\)", f"({key} {BW._num(val)})", node, count=1)
        text = text[:s] + node + text[e:]
    return text


# ---------------------------------------------------------------- impedance
class ZModel:
    """Impedance of a segment at an old / new width, from the project stackup (impedance.py), tolerance from the net class."""

    def __init__(self, cons, board, facts):
        from . import zgeom as ZG, impedance as IM, graph, constraints as C
        self.ZG, self.IM = ZG, IM
        self.cons = cons or {}
        self.rows, self.planes, self.src = ZG.resolve_stack(self.cons, facts=facts)
        self.rf = self.cons.get("rf") or {}
        self.rf_nets = set(self.rf.get("nets") or [])
        names = sorted({s["net"] for s in facts.get("_segs", [])})
        self.partner = {}
        if self.cons.get("pairs"):
            for slave, master in graph.diff_pairs(names, exclude=C.not_pairs(self.cons)).items():
                self.partner[slave], self.partner[master] = master, slave
        self.geo = {}

    def geom(self, layer):
        if layer not in self.geo:
            self.geo[layer] = self.IM.geometry(self.rows, self.planes, layer) if self.rows else None
        g = self.geo[layer]
        return g if g and g.get("model") else None

    def kind(self, net):
        if net in self.rf_nets:
            return "rf"
        if net in self.partner and self.ZG.family_of(self.cons, net):
            return "pair"
        return None

    def target(self, net):
        if net in self.rf_nets:
            return float(self.rf.get("target_z", 50)), float(self.rf.get("tolerance_pct", 10)), "single"
        fam = self.ZG.family_of(self.cons, net)
        fv = (self.cons.get("pairs") or {}).get(fam) or {}
        return float(fv.get("impedance_diff") or 0), float(fv.get("tolerance_pct", 10)), "diff"

    def z(self, net, layer, w, centre):
        """Impedance at width `w` (both legs) and centreline distance `centre` (None for a single-ended net)."""
        g = self.geom(layer)
        if g is None:
            return None
        if net in self.rf_nets:
            return self.IM.z_single(w, g)
        gap = UNCOUPLED_GAP if centre is None else max(centre - w, 0.02)
        return self.IM.z_diff(w, gap, g)

    def limit_width(self, net, layer, centre, lo, hi):
        """Widest width in [lo, hi] still inside tolerance (None when even `lo` is out)."""
        tz, tol, _m = self.target(net)
        ok = lambda w: abs(self.z(net, layer, w, centre) - tz) <= tz * tol / 100.0 + 1e-9
        if not ok(lo):
            return None
        if ok(hi):
            return hi
        a, b = lo, hi
        for _ in range(40):
            m = (a + b) / 2
            a, b = (m, b) if ok(m) else (a, m)
        return a


def _seg_dist(p, a, b):
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def centre_distance(seg, by_net_layer, partner_net):
    """Centreline distance from `seg` to the nearest near-parallel partner segment on the same layer (None when none runs beside it)."""
    best = None
    a1 = math.atan2(seg["y2"] - seg["y1"], seg["x2"] - seg["x1"])
    mid = ((seg["x1"] + seg["x2"]) / 2, (seg["y1"] + seg["y2"]) / 2)
    for o in by_net_layer.get((partner_net, seg["layer"]), ()):
        a2 = math.atan2(o["y2"] - o["y1"], o["x2"] - o["x1"])
        d = abs((a1 - a2 + math.pi / 2) % math.pi - math.pi / 2)
        if d > math.radians(8):
            continue
        dist = _seg_dist(mid, (o["x1"], o["y1"]), (o["x2"], o["y2"]))
        if dist < 1.0 and (best is None or dist < best):
            best = dist
    return best


# ---------------------------------------------------------------- planning
def _std_down(d, dmin):
    out, x = [], d
    while x - DRILL_STEP >= dmin - EPS:
        x = round(x - DRILL_STEP, 4)
        out.append(x)
    return out


def _pad_for(drill, ring, dia_min):
    return round(max(dia_min or 0.0, math.ceil((drill + 2 * ring) / PAD_STEP - 1e-9) * PAD_STEP), 4)


def via_candidates(v, ring_t, drill_min, dia_min, keep_drill):
    """Ordered [(size, drill, label)] that reach ring >= ring_t, smallest pad growth first."""
    out = []
    for d in [v["drill"]] + ([] if keep_drill else _std_down(v["drill"], drill_min)):
        for pad in {max(v["size"], _pad_for(d, ring_t, dia_min)), _pad_for(d, ring_t, dia_min)}:
            if (pad - d) / 2 >= ring_t - EPS and pad >= v["size"] - EPS and (pad, d) != (v["size"], v["drill"]):
                out.append((pad, d))
    out = sorted(set(out), key=lambda c: (round(c[0] - v["size"], 4), round(v["drill"] - c[1], 4)))
    return [(p, d, ("drill %.2f->%.2f" % (v["drill"], d) if d != v["drill"] else "") +
             (" pad %.3f->%.3f" % (v["size"], p) if p > v["size"] + EPS else "")) for p, d in out]


def track_ladder(w, lims, key="track_min"):
    """Widths to try, strictest house minimum first, only those above the current width."""
    return sorted({l[key] for l in lims.values() if l.get(key) and l[key] > w + EPS}, reverse=True)


class Plan:
    """Per-item ladders and the current rung. rung -1 = not edited (ladder exhausted / refused)."""

    def __init__(self):
        self.items = []

    def add(self, **kw):
        kw.setdefault("rung", 0)
        kw.setdefault("why", "")
        self.items.append(kw)
        return kw

    def edits(self):
        out = []
        for it in self.items:
            if 0 <= it["rung"] < len(it["ladder"]):
                out.append((it["span"], it["ladder"][it["rung"]]["edit"]))
        return out

    def active(self):
        return [it for it in self.items if 0 <= it["rung"] < len(it["ladder"])]


def build_plan(text, segs, vias, lims, zm, cons, facts, log=print):
    strict_ring = strictest(lims, "via_annular_min") or 0.0
    drill_min = strictest(lims, "drill_min") or 0.0
    dia_min = strictest(lims, "via_dia_min") or 0.0
    ring_targets = sorted({l["via_annular_min"] for l in lims.values() if l.get("via_annular_min")} |
                          {o["limit_mm"] for h in lims for o in options_data().get(h, []) if o["kind"] == "via_ring"}, reverse=True)
    zone_nets = set(facts.get("zone_nets") or []) | set((cons.get("power") or {}))
    plan = Plan()
    by_nl = defaultdict(list)
    for s in segs:
        by_nl[(s["net"], s["layer"])].append(s)
    centre = {}
    for s in segs:
        pn = zm.partner.get(s["net"]) if zm else None
        centre[id(s)] = centre_distance(s, by_nl, pn) if pn else None
    for s in segs:
        lad = track_ladder(s["w"], lims)
        if not lad or s["locked"]:
            continue
        kind = zm.kind(s["net"]) if zm else None
        rungs, info = [], {}
        c = centre[id(s)]
        for w in lad:
            if kind and zm.geom(s["layer"]):
                tz, tol, mode = zm.target(s["net"])
                cc = c if mode == "diff" else None            # None: no partner beside the segment -> uncoupled microstrip
                z_old, z_new = zm.z(s["net"], s["layer"], s["w"], cc), zm.z(s["net"], s["layer"], w, cc)
                inside = abs(z_new - tz) <= tz * tol / 100.0 + 1e-9
                if not inside and abs(z_new - tz) > abs(z_old - tz):      # already out of tolerance: accept only a move toward the target
                    lim = zm.limit_width(s["net"], s["layer"], cc, s["w"], w)
                    info["z_blocked"] = {"want": w, "z_new": round(z_new, 1), "limit_w": None if lim is None else round(lim, 4)}
                    continue
                rungs.append({"edit": {"width": w}, "to": w, "z_old": round(z_old, 1), "z_new": round(z_new, 1), "target": tz,
                              "tol_pct": tol, "coupled": cc is not None, "inside_tol": inside,
                              "centre": None if cc is None else round(cc, 4)})
            elif kind:
                info["z_blocked"] = {"want": w, "z_new": None, "limit_w": None, "note": "no reference plane / stackup: impedance not computable"}
            else:
                rungs.append({"edit": {"width": w}, "to": w})
        plan.add(kind="track", span=s["span"], net=s["net"], layer=s["layer"], x=(s["x1"] + s["x2"]) / 2, y=(s["y1"] + s["y2"]) / 2,
                 geom=((s["x1"], s["y1"]), (s["x2"], s["y2"])), old=s["w"], ladder=rungs, z=kind, info=info,
                 rung=0 if rungs else -1, why="" if rungs else "impedance-blocked" if info.get("z_blocked") else "no rung")
    for v in vias:
        ring = (v["size"] - v["drill"]) / 2
        if ring >= strict_ring - EPS or not v["through"] or v["locked"]:
            continue
        keep = v["net"] in zone_nets
        cands, seen = [], set()
        for rt in ring_targets:
            if rt <= ring + EPS:
                continue
            for c in via_candidates(v, rt, drill_min, dia_min, keep):
                if c[:2] not in seen:
                    seen.add(c[:2])
                    cands.append(c)
        rungs = [{"edit": {"size": p, "drill": d}, "to": (p, d), "label": lab.strip()} for p, d, lab in cands]
        plan.add(kind="via_ring", span=v["span"], net=v["net"], layer="F.Cu-B.Cu", x=v["x"], y=v["y"], geom=((v["x"], v["y"]),),
                 old=ring, old_size=v["size"], old_drill=v["drill"], ladder=rungs, z=None, info={"keep_drill": keep},
                 rung=0 if rungs else -1, why="" if rungs else "no candidate")
    return plan, centre


# ---------------------------------------------------------------- guard loop
def _new_instances(base_m, drc):
    """Error violations of `drc` the base does not already have (per key, the count above the base's), plus the unconnected items
    of every net whose open count rose. The key set is drcguard's own (error severity), so it matches the base measure."""
    cm = DG.measure_from_report(drc)
    extra = DG.new_keys(base_m["keys"], cm["keys"])
    out, left = [], Counter(extra)
    for v in drc.get("violations", []):
        if v.get("severity") != "error":
            continue
        k = DG.key_of(v)
        if left[k] > 0:
            left[k] -= 1
            out.append(v)
    rose = {n for n, c in cm["opens_by_net"].items() if c > base_m["opens_by_net"].get(n, 0)}
    from . import drcfull
    out += [u for u in drc.get("unconnected_items", []) if drcfull.net_of(u) in rose]
    return out, extra


def _near(it, pos, r=0.6):
    p = (pos["x"], pos["y"])
    if len(it["geom"]) == 2:
        return _seg_dist(p, it["geom"][0], it["geom"][1]) <= r + it["ladder"][max(it["rung"], 0)]["to"] / 2 if it["kind"] == "track" else False
    return math.hypot(p[0] - it["geom"][0][0], p[1] - it["geom"][0][1]) <= r + 0.5


def guard_loop(text, base_path, plan, base_measure, workdir, max_rounds=8, log=print, pro_edit=None):
    """Apply the plan, DRC, step the items next to a NEW violation one rung down; repeat. -> (final text, rounds log)."""
    rounds = []
    for rnd in range(1, max_rounds + 1):
        act = plan.active()
        if not act:
            break
        cand = apply_edits(text, plan.edits())
        p = os.path.join(workdir, f"cand-{rnd}.kicad_pcb")
        open(p, "w").write(cand)
        for ext in (".kicad_pro", ".kicad_dru"):
            src = os.path.splitext(base_path)[0] + ext
            if os.path.isfile(src):
                shutil.copy(src, os.path.splitext(p)[0] + ext)
        drc = DG.drc_json(p, pro_edit=pro_edit)
        new, extra = _new_instances(base_measure, drc)
        rounds.append({"round": rnd, "edited": len(act), "new_violations": len(new)})
        log(f"  fabconform round {rnd}: {len(act)} edits, {len(new)} new violations")
        if not new:
            return cand, rounds, drc
        hit = set()
        for v in new:
            for itm in v.get("items", []):
                pos = itm.get("pos")
                if not pos:
                    continue
                for i, it in enumerate(act):
                    if _near(it, pos):
                        hit.add(i)
        if not hit:                                    # nothing attributable: refuse the whole batch loudly
            for it in act:
                it["rung"], it["why"] = -1, "new DRC violation not attributable to an edit: batch refused"
            return text, rounds, None
        for i in hit:
            act[i]["rung"] += 1
            near = [v for v in new if any(it.get("pos") and _near(act[i], it["pos"]) for it in v.get("items", []))]
            act[i]["why"] = "DRC at rung %d: %s" % (act[i]["rung"] - 1, "; ".join(sorted({(v.get("description") or v.get("type", "unconnected"))[:80] for v in near})[:2]))
            if act[i]["rung"] >= len(act[i]["ladder"]):
                act[i]["rung"] = -1
    return None, rounds, None


# ---------------------------------------------------------------- per-house verdicts
def house_verdict(house, profile, kind, value, layers, lims_by_house):
    """-> dict(status ok|option|impossible, tier, text, source, retrieved) for one item value at one house."""
    key = LIMIT_KEY[kind]
    need = lims_by_house[house].get(key)
    if need is None or value >= need - EPS:
        return {"status": "ok"}
    v, t0 = FC.split(profile)
    for (vv, t) in FC.all_tiers():
        if vv != v or t == t0 or not FC.offers(vv, t, layers):
            continue
        lim, _u = FC.row_for(vv, t, layers)
        lv = lim.get(key)
        if lv is not None and value >= lv - EPS:
            info = FC.tier_info(vv, t)
            return {"status": "option", "tier": info["name"], "source": info["source"], "retrieved": info["retrieved"],
                    "text": f"{info['name']}: {key} {lv:g} mm ({info['price_step']})"}
    for o in options_data().get(house, []):
        if o["kind"] == kind and value >= o["limit_mm"] - EPS:
            return {"status": "option", "tier": o["option"], "source": o["source"], "retrieved": o["retrieved"],
                    "text": f"{o['option']}: {kind} >= {o['limit_mm']:g} mm ({o['cost']})"}
    return {"status": "impossible", "text": f"{value:g} mm is below every published {house} option for {KIND_LABEL[kind]} (standard {need:g} mm)",
            "source": FC.tier_info(v, t0)["source"], "retrieved": FC.tier_info(v, t0)["retrieved"]}


def classify(kind, value, houses, layers, lims_by_house):
    return {h: house_verdict(h, p, kind, value, layers, lims_by_house) for h, p in houses}


def action_of(verdicts, changed):
    st = {v["status"] for v in verdicts.values()}
    if "impossible" in st:
        return "IMPOSSIBLE"
    if "option" in st:
        return "ORDER-OPTION"
    return "FIXED" if changed else "OK"


# ---------------------------------------------------------------- main
def _house_pro_edit(lims):
    """pro_edit(dict): the strictest house rules into the temp project, so kicad-cli itself reports what is under them."""
    sp, tr, ring = strictest(lims, "space_min"), strictest(lims, "track_min"), strictest(lims, "via_annular_min")
    dm, dia = strictest(lims, "drill_min"), strictest(lims, "via_dia_min")

    def edit(pd):
        r = pd.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})
        for k, v in (("min_clearance", sp), ("min_track_width", tr), ("min_via_annular_width", ring),
                     ("min_through_hole_diameter", dm), ("min_via_diameter", dia)):
            if v:
                r[k] = v
        for c in (pd.get("net_settings") or {}).get("classes", []):
            if sp and c.get("clearance") is not None:
                c["clearance"] = sp                   # house space only: the project's larger preference is not a fab limit
    return edit


def _relax_edit(lims):
    """pro_edit(dict): the project's clearance (every net class and the board minimum) lowered to the strictest HOUSE space, so a
    gap that is legal at every fab but under the project's own preferred clearance is not a guard failure (`--relax-clearance`)."""
    sp = strictest(lims, "space_min")

    def edit(pd):
        r = pd.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})
        r["min_clearance"] = min(float(r.get("min_clearance") or sp), sp)
        for c in (pd.get("net_settings") or {}).get("classes", []):
            if c.get("clearance") is not None:
                c["clearance"] = min(float(c["clearance"]), sp)
    return edit


def _loc(x, y):
    return f"({x:.3f}, {y:.3f})"


def run(board, cons=None, houses="jlcpcb,pcbway", out=None, log=print, kicad_cli="kicad-cli", relax_clearance=False):
    """-> report dict (rows, counts, impedance, order lines). Writes `out` (+ sidecars) only when the guard accepts."""
    from . import review as R, opengate as OG
    hs = resolve_houses(houses)
    layers = FC.board_layer_count(board) or 6
    lims = house_limits(hs, layers)
    facts, _stk = R.apply_constraint_stackup(R.facts_from_board(board), cons or {})
    text = open(board, errors="replace").read()
    segs, vias = parse(text)
    facts["_segs"] = segs
    zm = ZModel(cons or {}, board, facts)
    plan, centre = build_plan(text, segs, vias, lims, zm, cons or {}, facts, log)
    log(f"fabconform: {len(segs)} segments, {len(vias)} vias; houses {', '.join(p for _h, p in hs)} ({layers} layers); "
        f"{len(plan.items)} items under a house minimum")
    work = tempfile.mkdtemp(prefix="fxconform-")
    try:
        pe = _relax_edit(lims) if relax_clearance else None
        measure = (lambda path: DG.measure_from_report(DG.drc_json(path, kicad_cli, pro_edit=pe))) if pe else (lambda path: DG.measure(path, kicad_cli))
        base_m = measure(board)
        new_text, rounds, drc = guard_loop(text, board, plan, base_m, work, log=log, pro_edit=pe) if plan.active() else (text, [], None)
        accepted, why = True, ""
        if new_text is None:
            new_text, accepted, why = text, False, "guard loop did not converge in max rounds"
        elif plan.active():
            cand_path = os.path.join(work, "final.kicad_pcb")
            open(cand_path, "w").write(new_text)
            for ext in (".kicad_pro", ".kicad_dru"):
                src = os.path.splitext(board)[0] + ext
                if os.path.isfile(src):
                    shutil.copy(src, os.path.splitext(cand_path)[0] + ext)
            cand_m = measure(cand_path)
            accepted, why = DG.accept(base_m, cand_m, mode="edit", base_board=board, cand_board=cand_path)
            why = why or ""
        if not accepted:
            log(f"fabconform: REFUSED - {why}; board left unchanged")
            for it in plan.items:
                it["rung"], it["why"] = -1, it["why"] or f"guard refused: {why}"
            new_text = text
        final_path = out or os.path.join(work, "final.kicad_pcb")
        open(final_path, "w").write(new_text)
        for ext in (".kicad_pro", ".kicad_prl", ".kicad_dru"):
            src = os.path.splitext(board)[0] + ext
            dst = os.path.splitext(final_path)[0] + ext
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.copy(src, dst)
        if out and accepted and plan.active():
            try:
                OG.refill(final_path)                 # zone fill follows the widened copper (pcbnew refill + save)
            except Exception as e:                    # noqa: BLE001 - reported, the board is still the DRC-accepted text
                log(f"fabconform: zone refill skipped ({e})")
        house_drc = DG.drc_json(final_path, pro_edit=_house_pro_edit(lims))
        rows = build_rows(plan, hs, layers, lims, zm, facts, house_drc, centre, segs, vias, new_text)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"board": board, "out": out if out else None, "accepted": accepted, "why": why, "layers": layers,
            "houses": [p for _h, p in hs], "rounds": rounds, "rows": rows, "counts": count(rows),
            "limits": {h: {k: lims[h].get(k) for k in LIMIT_KEY.values()} for h, _p in hs},
            "residual_drc": dict(Counter(v["type"] for v in house_drc.get("violations", []))),
            "impedance": impedance_summary(plan, zm), "relax_clearance": relax_clearance}


def build_rows(plan, hs, layers, lims, zm, facts, house_drc, centre, segs, vias, final_text):
    """Group the per-item outcomes into table rows: (kind, net, layer, old, new) with a count and the first location."""
    groups = {}
    for it in plan.items:
        final = None
        if 0 <= it["rung"] < len(it["ladder"]):
            r = it["ladder"][it["rung"]]
            final = r["to"][0] - r["to"][1] if it["kind"] == "via_ring" and isinstance(r["to"], tuple) else r["to"]
            if it["kind"] == "via_ring":
                final = (r["to"][0] - r["to"][1]) / 2
        value = final if final is not None else it["old"]
        v = classify(it["kind"], value, hs, layers, lims)
        act = action_of(v, final is not None)
        k = (it["kind"], it["net"], it["layer"], round(it["old"], 4), None if final is None else round(final, 4), act)
        g = groups.setdefault(k, {"kind": it["kind"], "net": it["net"], "layer": it["layer"], "old": it["old"], "new": final, "n": 0,
                                  "loc": _loc(it["x"], it["y"]), "action": act, "verdicts": v, "why": it["why"], "info": it["info"],
                                  "detail": ""})
        g["n"] += 1
        if final is not None and it["ladder"]:
            r = it["ladder"][it["rung"]]
            g["detail"] = r.get("label") or (f"Z {r['z_old']} -> {r['z_new']} ohm (target {r['target']:g} +-{r['tol_pct']:g}%)" if "z_new" in r else "")
    # house-rule DRC residue that is not a track / via item: clearance, PTH and drill
    for vio in house_drc.get("violations", []):
        t = vio.get("type")
        if t not in ("clearance", "drill_out_of_range", "hole_clearance", "hole_to_hole", "via_diameter"):
            continue
        m = re.search(r"actual ([\d.]+)", vio.get("description", ""))
        it = (vio.get("items") or [{}])[0]
        pos = it.get("pos") or {"x": 0, "y": 0}
        net = (re.search(r"\[([^\]]*)\]", it.get("description", "")) or [None, ""])[1]
        kind = "space" if t in ("clearance", "hole_clearance", "hole_to_hole") else "drill"
        value = float(m.group(1)) if m else None
        if value is None:
            continue
        v = classify(kind, value, hs, layers, lims)
        act = action_of(v, False)
        k = (kind + ":" + t, net, "", round(value, 3), None, act)
        g = groups.setdefault(k, {"kind": kind, "net": net, "layer": "", "old": value, "new": None, "n": 0, "loc": _loc(pos["x"], pos["y"]),
                                  "action": act, "verdicts": v, "why": t, "info": {}, "detail": vio.get("description", "")[:90]})
        g["n"] += 1
    return sorted(groups.values(), key=lambda g: (g["action"], g["kind"], g["net"]))


def count(rows):
    c = Counter()
    for g in rows:
        c[g["action"]] += g["n"]
    return dict(c)


def impedance_summary(plan, zm):
    """Per pair/RF family: widened segments with Z before/after, and the impedance-limited width where widening was refused."""
    out = {}
    for it in plan.items:
        if not it.get("z"):
            continue
        d = out.setdefault((zm.ZG.family_of(zm.cons, it["net"]) or "rf") + "@" + it["layer"], {"segments": 0, "widened": 0, "z_old": [], "z_new": [],
                                                                                           "blocked": 0, "limit_w": []})
        d["segments"] += 1
        if 0 <= it["rung"] < len(it["ladder"]):
            r = it["ladder"][it["rung"]]
            d["widened"] += 1
            d["z_old"].append(r["z_old"])
            d["z_new"].append(r["z_new"])
            d["to"] = r["to"]
            d["tol_pct"], d["target"] = r["tol_pct"], r["target"]
            d["coupled_segments"] = d.get("coupled_segments", 0) + (1 if r.get("coupled") else 0)
            d["in_tol_after"] = d.get("in_tol_after", 0) + (1 if r.get("inside_tol") else 0)
            if "w_tol" not in d and not r.get("coupled"):
                g = zm.geom(it["layer"])
                lo, hi = r["target"] * (1 - r["tol_pct"] / 100.0), r["target"] * (1 + r["tol_pct"] / 100.0)
                wt = zm.IM._bisect(lambda w: zm.IM.z_diff(w, UNCOUPLED_GAP, g), hi, 0.04, 2.0) if g else None
                d["w_tol"] = None if wt is None else round(wt, 3)       # narrowest uncoupled width whose 2 x Z0 reaches the +tol edge
        elif it["info"].get("z_blocked"):
            d["blocked"] += 1
            d["limit_w"].append(it["info"]["z_blocked"].get("limit_w"))
    for d in out.values():
        for k in ("z_old", "z_new"):
            v = d.pop(k)
            d[k] = [min(v), max(v)] if v else None
        lw = [x for x in d.pop("limit_w") if x]
        d["limit_w"] = min(lw) if lw else None
    return out


# ---------------------------------------------------------------- output
def table(rep):
    """Plain-text table: item, net, location, house limits, action."""
    hl = rep["limits"]
    lines = [f"{'item':<18} {'net':<16} {'n':>3} {'was':>7} {'now':>7}  {'first location':<18} {'action':<13} detail"]
    for g in rep["rows"]:
        now = "" if g["new"] is None else f"{g['new']:.3f}"
        det = g["detail"]
        if g["action"] in ("ORDER-OPTION", "IMPOSSIBLE"):
            det = "; ".join(f"{h}: {v['text']}" for h, v in g["verdicts"].items() if v["status"] != "ok")
            det += f" [fix blocked: {g['why']}]" if g["why"] and g["kind"] in ("track", "via_ring") else ""
        elif g["info"].get("z_blocked"):
            det = f"impedance-limited: {g['info']['z_blocked']}"
        lines.append(f"{KIND_LABEL.get(g['kind'], g['kind']):<18} {g['net'][:16]:<16} {g['n']:>3} {g['old']:>7.3f} {now:>7}  {g['loc']:<18} {g['action']:<13} {det}")
    lim = "; ".join(f"{h}: track {l['track_min']:g} space {l['space_min']:g} via ring {l['via_annular_min']:g} PTH ring {l['pth_annular_min']:g} drill {l['drill_min']:g}"
                    for h, l in hl.items())
    lines.append(f"house limits (mm, {rep['layers']} layers): {lim}")
    lines.append("counts: " + ", ".join(f"{k} {v}" for k, v in sorted(rep["counts"].items())))
    return "\n".join(lines)


def order_notes(rep):
    """{house: markdown} - the order options each house needs for what remains."""
    res = {}
    for h, p in [(x.split("-")[0], x) for x in rep["houses"]]:
        L = [f"# Order options - {h.upper()} ({p}), generated by fluxplace fabconform", ""]
        any_ = False
        for g in rep["rows"]:
            v = g["verdicts"].get(h)
            if not v or v["status"] == "ok":
                continue
            any_ = True
            lab = f"{g['n']} x {KIND_LABEL.get(g['kind'], g['kind'])} {g['old']:.3f} mm" + (f" on {g['net']}" if g["net"] else "") + f" at {g['loc']}"
            L.append(f"- {v['status'].upper()}: {lab}: {v['text']}  [{v.get('source', '')}, retrieved {v.get('retrieved', '')}]")
        if not any_:
            L.append("None: every copper feature meets the standard table of this house.")
        res[h] = "\n".join(L) + "\n"
    return res
