"""Placement INTENT: what the owner wants from the placement, stated in the constraints, honoured by the placers and the
legaliser, and graded by the INTENT gate (`fluxplace intent BOARD`, and at the end of `floorplan` / `place`).
Full reference with examples: docs/INTENT.md. Keys (all under `[intent]`; every list of refs takes globs):

  [[intent.edge]]    refs, side = "any"|"left"|"right"|"top"|"bottom"|"not:top"|[...], facing = "out", front = "+x" (opening
                     in the footprint frame, when it cannot be derived), clear_mm (nothing in front inside this depth),
                     overhang_mm (courtyard may pass the outline by this much), gap_mm (still "on the edge"), hard = true
  [[intent.near]]    chain = [refs] (pad-to-pad along the chain, summed) or refs + to (each ref to one part), max_mm,
                     hard = false, weight
  [[intent.away]]    a = [refs], b = [refs], min_mm (courtyard to courtyard), hard = true, weight
  [[intent.region]]  name, rect = [x0, y0, x1, y1] or under = "REF" (+ margin_mm), side = "F"|"B"|"both", keep = "out"|"in",
                     max_height_mm (keep-out: only parts taller than this are forbidden; absent = nothing at all),
                     refs (keep-in: these must be inside; keep-out: only these are concerned), allow = [refs]
  [[intent.group]]   name, refs (move and turn as ONE body), rel = {REF = [dx, dy, rot]} (verified poses in the frame of the
                     first ref), swept = [refs] | "all" (their bounding box + swept_margin_mm is occupied on swept_side;
                     parts up to swept_max_height_mm may sit under it; absent = nothing at all, holes included;
                     swept_allow = [refs] may sit in it regardless)
  [intent.support]   follow = true (an IC's support parts move with it), decap_hard = true (a decoupling cap beyond its
                     limit fails the gate), hard = false (same for the other support parts), waive = [refs]
  [intent.connectors] REF = {entry = "side"|"top", opening = "+x", edge = false}; see connectors.py
  [intent]           edge_gap_mm = 1.0, plug_mm = 5.0 (default clear depth of a facing part), enabled = true,
                     lead_protrusion_mm = 1.5 (how far through-hole leads reach into a region on the other side)

Sides are board axes on screen: left = min x, top = min y. Part heights: `[placement.heights]` (ref or footprint glob -> mm), else an
estimate for chip passives from the footprint size code, else unknown = treated as tall.

How it is honoured: HARD intent is seated first and then HELD (`prepare`): rigid groups, edge parts (with their support
parts), hard `near` partners of a held part; hard `near` between movable parts makes them one rigid body. Every zone that
follows (regions, swept areas, the clear depth in front of an edge part, hard `away` distances from held parts) is a forbidden
rectangle in `legalise._Model` = one source for legality.Legal (placers) and the legaliser. SOFT intent is a cost term in the
floorplan (`near` = a two-pin net with a budget, `away` = a penalty). The gate lists every item as met / NOT MET with the
measured value; an unmet hard item fails the stage.
"""
import copy
import fnmatch
import math
import os
import re

from . import connectors as CN
from . import geom as GM

SIDES = ("left", "right", "top", "bottom")
NORMAL = {"left": (-1.0, 0.0), "right": (1.0, 0.0), "top": (0.0, -1.0), "bottom": (0.0, 1.0)}
ATTACH_MM = 15.0
_MECH = re.compile(r"^(MH|MK|FID|TP|H)\d")
_CHIP = re.compile(r"(?:^|[:_])(R|C|L|FB|LED|D)_(0201|0402|0603|0805|1206|1210|1812|2010|2512)(?:_|$)")
_CHIP_H = {"R": {"0201": 0.26, "0402": 0.4, "0603": 0.5, "0805": 0.6, "1206": 0.65, "1210": 0.65, "1812": 0.7,
                 "2010": 0.65, "2512": 0.65},
           "C": {"0201": 0.33, "0402": 0.55, "0603": 0.9, "0805": 1.4, "1206": 1.8, "1210": 2.7, "1812": 2.8}}
_CHIP_H["FB"] = _CHIP_H["LED"] = _CHIP_H["D"] = _CHIP_H["R"]
_CHIP_H["L"] = _CHIP_H["C"]


# ---------------------------------------------------------------- reading the constraints
def section(cons):
    return (cons or {}).get("intent") or {}


def enabled(cons):
    """the gate FAILS a stage only when the constraints carry an `[intent]` table (and not `enabled = false`)."""
    return "intent" in (cons or {}) and bool(section(cons).get("enabled", True))


def _list(v):
    return [v] if isinstance(v, str) else list(v or [])


def match(parts, globs):
    gl = _list(globs)
    return sorted(r for r in parts if any(fnmatch.fnmatch(r, g) for g in gl))


def height(B, cons, ref):
    """part height in mm, or None: [placement.heights] (key = ref glob or footprint-name glob, first match wins), else
    the chip-size estimate."""
    fp = str(B["parts"][ref].get("fp", ""))
    for g, v in (((cons or {}).get("placement") or {}).get("heights") or {}).items():
        if fnmatch.fnmatch(ref, g) or fnmatch.fnmatch(fp.split(":")[-1], g):
            return float(v)
    m = _CHIP.search(fp)
    if m:
        return _CHIP_H.get(m.group(1), {}).get(m.group(2))
    return None


def _sides(spec):
    """allowed outline sides from "any" | "left" | "not:top" | [..]."""
    out = set()
    for s in _list(spec or "any"):
        s = str(s).lower().replace(" ", "")
        if s in ("any", ""):
            out |= set(SIDES)
        elif s.startswith("not:"):
            out |= set(SIDES) - {s[4:]}
        elif s in SIDES:
            out.add(s)
        else:
            raise SystemExit(f"[intent.edge] side = {s!r}: use any, left, right, top, bottom or not:<side>")
    return [s for s in SIDES if s in out]


def items(B, cons):
    """the intent, normalised against the board: {edge, near, away, region, group, support, connectors}."""
    sec = section(cons)
    parts = B["parts"]
    gap0, plug0 = float(sec.get("edge_gap_mm", 1.0)), float(sec.get("plug_mm", 5.0))
    em0 = float(((cons or {}).get("placement") or {}).get("edge_clearance_mm", 0.3))
    edge, covered = [], set()
    for i, e in enumerate(sec.get("edge", []) or []):
        refs = match(parts, e.get("refs"))
        covered |= set(refs)
        edge.append({"name": e.get("name") or f"edge{i + 1}", "refs": refs, "sides": _sides(e.get("side")),
                     "facing": str(e.get("facing", "")).lower() == "out", "front": e.get("front"),
                     "clear": float(e.get("clear_mm", plug0 if str(e.get("facing", "")).lower() == "out" else 0.0)),
                     "overhang": float(e.get("overhang_mm", 0.0)), "gap": float(e.get("gap_mm", gap0)),
                     "hard": bool(e.get("hard", True)), "src": "declared"})
    conn = CN.table(B, cons, CN.answers_of(cons))
    for row in conn:
        d = CN.declared(cons, row["ref"])
        if row["entry"] == "side" and row["ref"] not in covered and d.get("edge", True) is not False:
            edge.append({"name": f"connector {row['ref']}", "refs": [row["ref"]], "sides": list(SIDES), "facing": True,
                         "front": row["key"], "clear": plug0, "gap": gap0, "hard": True, "src": "connector",
                         "overhang": float(d["overhang_mm"]) if "overhang_mm" in d else CN.overhang(parts[row["ref"]], row["key"], em0)})
    near = []
    for i, e in enumerate(sec.get("near", []) or []):
        n = {"name": e.get("name") or f"near{i + 1}", "max": float(e["max_mm"]), "hard": bool(e.get("hard", False)),
             "weight": float(e.get("weight", 20.0 if e.get("hard") else 3.0))}
        if e.get("chain"):
            n["chain"] = [r for r in e["chain"] if r in parts]
        else:
            n["refs"], n["to"] = match(parts, e.get("refs")), e.get("to")
            if n["to"] not in parts:
                continue
        near.append(n)
    away = [{"name": e.get("name") or f"away{i + 1}", "a": match(parts, e.get("a")), "b": match(parts, e.get("b")),
             "min": float(e["min_mm"]), "hard": bool(e.get("hard", True)), "weight": float(e.get("weight", 5.0))}
            for i, e in enumerate(sec.get("away", []) or [])]
    region = []
    for i, e in enumerate(sec.get("region", []) or []):
        side = str(e.get("side", "both")).upper()
        region.append({"name": e.get("name") or f"region{i + 1}", "rect": e.get("rect"), "under": e.get("under"),
                       "margin": float(e.get("margin_mm", 0.0)), "sides": {"F", "B"} if side in ("BOTH", "") else {side[0]},
                       "keep": str(e.get("keep", "out")).lower(), "max_h": e.get("max_height_mm"),
                       "refs": match(parts, e["refs"]) if e.get("refs") else None, "allow": set(match(parts, e.get("allow"))),
                       "hard": bool(e.get("hard", True))})
    group = []
    for i, e in enumerate(sec.get("group", []) or []):
        refs = [r for r in _list(e.get("refs")) if r in parts]
        sw = e.get("swept")
        side = str(e.get("swept_side", "")).upper()
        group.append({"name": e.get("name") or f"group{i + 1}", "refs": refs, "rel": e.get("rel") or {},
                      "swept": (refs if sw == "all" else [r for r in _list(sw) if r in parts]) if sw else [],
                      "margin": float(e.get("swept_margin_mm", 0.0)), "side": side[:1] if side else None,
                      "max_h": e.get("swept_max_height_mm"), "hard": bool(e.get("hard", True)),
                      "allow": set(match(parts, e.get("swept_allow")))})
    sup = sec.get("support") or {}
    support = {"follow": bool(sup.get("follow", True)), "decap_hard": bool(sup.get("decap_hard", True)),
               "hard": bool(sup.get("hard", False)), "waive": set(match(parts, sup.get("waive")))}
    return {"edge": edge, "near": near, "away": away, "region": region, "group": group, "support": support, "connectors": conn}


def effective(cons, B=None):
    """constraints with the intent folded into the keys the rest of the tool already reads (a copy): rigid groups become
    `[placement.group."intent:NAME"]`, parts with an overhang join `[placement] edge_mounted`."""
    sec = section(cons)
    if not sec:
        return cons
    c = copy.deepcopy(cons)
    pl = c.setdefault("placement", {})
    for i, e in enumerate(sec.get("group", []) or []):
        if e.get("refs"):
            pl.setdefault("group", {}).setdefault("intent:" + str(e.get("name") or f"group{i + 1}"), {"refs": list(e["refs"])})
    return c


def overhang_refs(B, cons):
    """refs an `[[intent.edge]]` lets pass the outline (`overhang_mm > 0`)."""
    out = set()
    for e in section(cons).get("edge", []) or []:
        if float(e.get("overhang_mm", 0.0)) > 0:
            out |= set(match(B["parts"], e.get("refs")))
    for ref, d in (section(cons).get("connectors") or {}).items():
        if isinstance(d, dict) and float(d.get("overhang_mm", 0.0)) > 0 and ref in B["parts"]:
            out.add(ref)
    if section(cons):
        for row in CN.table(B, cons, CN.answers_of(cons)):
            if row["entry"] == "side" and CN.overhang(B["parts"][row["ref"]], row["key"]) > 0:
                out.add(row["ref"])
    return out


# ---------------------------------------------------------------- geometry of the intent
def front_vec(B, cons, ref, item=None):
    """world unit vector of a part's opening / antenna end: the item's `front`, the connector table, else the part's own
    rule area (an antenna keep-out lies in front of the module). None when nothing says."""
    part = B["parts"][ref]
    key = (section(cons).get("_front") or {}).get(ref) or (item or {}).get("front")
    if key in CN.DIRS:
        return CN.world(part, key)
    if CN.is_connector(ref, part):
        row = CN.classify(B, ref, cons, CN.answers_of(cons))
        if row["opening"]:
            return row["opening"]
    zs = [z for z in B["zones"] if z.get("owner") == ref and z["keepout"] and z["pts"]]
    if zs:
        zx = sum(p[0] for z in zs for p in z["pts"]) / sum(len(z["pts"]) for z in zs)
        zy = sum(p[1] for z in zs for p in z["pts"]) / sum(len(z["pts"]) for z in zs)
        px = sum(q["x"] for q in part["pads"]) / max(1, len(part["pads"]))
        py = sum(q["y"] for q in part["pads"]) / max(1, len(part["pads"]))
        dx, dy = zx - px, zy - py
        if max(abs(dx), abs(dy)) > 0.5 and min(abs(dx), abs(dy)) < 0.5 * max(abs(dx), abs(dy)):
            return ((1.0 if dx > 0 else -1.0), 0.0) if abs(dx) > abs(dy) else (0.0, (1.0 if dy > 0 else -1.0))
    return None


def edge_gaps(B, court):
    bx0, by0, bx1, by1 = B["bbox"]
    return {"left": court[0] - bx0, "right": bx1 - court[2], "top": court[1] - by0, "bottom": by1 - court[3]}


def front_zone(B, court, side, depth):
    """rectangle in front of a courtyard across `side`, `depth` deep, clipped to the board box (None when it is all outside)."""
    bx0, by0, bx1, by1 = B["bbox"]
    r = {"left": (court[0] - depth, court[1], court[0], court[3]), "right": (court[2], court[1], court[2] + depth, court[3]),
         "top": (court[0], court[1] - depth, court[2], court[1]), "bottom": (court[0], court[3], court[2], court[3] + depth)}[side]
    r = (max(r[0], bx0), max(r[1], by0), min(r[2], bx1), min(r[3], by1))
    return r if r[2] - r[0] > 0.05 and r[3] - r[1] > 0.05 else None


def edge_state(B, cons, ref, item):
    """(side, gap, on_edge, facing_ok | None, zone) of one edge part as it sits."""
    c = B["parts"][ref]["court"]
    g = edge_gaps(B, c)
    f = front_vec(B, cons, ref, item) if item["facing"] else None
    cands = []
    for s in item["sides"]:
        on = -item["overhang"] - 0.02 <= g[s] <= item["gap"] + 1e-6
        face = None if not item["facing"] else (f is not None and f[0] * NORMAL[s][0] + f[1] * NORMAL[s][1] >= 0.7)
        cands.append((0 if on and face is not False else 1 if on else 2, abs(g[s]), s, on, face))
    if not cands:
        return None, 0.0, False, None, None
    _k, _a, s, on, face = min(cands)
    zone = front_zone(B, c, s, item["clear"]) if item["facing"] and item["clear"] > 0 and on else None
    return s, g[s], on, face, zone


def swept_rect(B, g):
    cs = [B["parts"][r]["court"] for r in g["swept"]]
    m = g["margin"]
    return (min(c[0] for c in cs) - m, min(c[1] for c in cs) - m, max(c[2] for c in cs) + m, max(c[3] for c in cs) + m)


def region_rect(B, r):
    if r.get("rect"):
        return tuple(float(v) for v in r["rect"])
    if r.get("under") in B["parts"]:
        c, m = B["parts"][r["under"]]["court"], r["margin"]
        return (c[0] - m, c[1] - m, c[2] + m, c[3] + m)
    return None


class Zones:
    """every forbidden rectangle the intent implies, as the board stands. `held` = the refs whose position may be relied on
    (zones that depend on a part are built only from held parts; None = every part)."""

    def __init__(self, B, cons, held=None):
        self.B, self.cons = B, cons
        self.z = []
        self._h, self._for = {}, {}
        self.lead = float(section(cons).get("lead_protrusion_mm", 1.5))
        if not section(cons):
            self.top = set()
            return
        it = items(B, cons)
        parts = B["parts"]
        ok = (lambda r: True) if held is None else (lambda r: r in held)
        self.top = {r["ref"] for r in it["connectors"] if r["entry"] == "top"}
        big = B["bbox"]
        for r in it["region"]:
            rect = region_rect(B, r)
            if rect is None or (r["under"] and not ok(r["under"])):
                continue
            allow = set(r["allow"]) | ({r["under"]} if r["under"] else set())
            if r["keep"] == "in":
                x0, y0, x1, y1 = rect
                e = 1e4
                for k, strip in enumerate(((big[0] - e, big[1] - e, x0, big[3] + e), (x1, big[1] - e, big[2] + e, big[3] + e),
                                           (big[0] - e, big[1] - e, big[2] + e, y0), (big[0] - e, y1, big[2] + e, big[3] + e))):
                    self.z.append({"name": r["name"], "kind": "keepin", "rect": strip, "sides": {"F", "B"}, "max_h": None,
                                   "allow": allow, "only": set(r["refs"] or ()), "holes": False})
            else:
                self.z.append({"name": r["name"], "kind": "region", "rect": rect, "sides": r["sides"], "max_h": r["max_h"],
                               "allow": allow, "only": set(r["refs"]) if r["refs"] is not None else None, "holes": False})
        for g in it["group"]:
            if g["swept"] and all(ok(r) for r in g["swept"]):
                side = g["side"] or parts[g["refs"][0]]["side"][0]
                self.z.append({"name": f"swept area of {g['name']}", "kind": "swept", "rect": swept_rect(B, g), "sides": {side},
                               "max_h": g["max_h"], "allow": set(g["refs"]) | g["allow"], "only": None, "holes": True})
        for e in it["edge"]:
            for ref in e["refs"]:
                if not ok(ref):
                    continue
                s, _gap, on, face, zone = edge_state(B, cons, ref, e)
                if zone and face:
                    self.z.append({"name": f"front of {ref}", "kind": "front", "rect": zone, "sides": {"F", "B"} if parts[ref]["tht"]
                                   else {parts[ref]["side"][0]}, "max_h": None, "allow": {ref}, "only": None, "holes": False})
        for a in it["away"]:
            if not a["hard"]:
                continue
            for src, dst in ((a["a"], a["b"]), (a["b"], a["a"])):
                for ref in src:
                    if ok(ref):
                        c, m = parts[ref]["court"], a["min"]
                        self.z.append({"name": f"{a['name']}: {a['min']:g} mm from {ref}", "kind": "away",
                                       "rect": (c[0] - m, c[1] - m, c[2] + m, c[3] + m), "sides": {"F", "B"}, "max_h": None,
                                       "allow": set(src), "only": set(dst), "holes": False})

    def h(self, ref):
        if ref not in self._h:
            self._h[ref] = height(self.B, self.cons, ref)
        return self._h[ref]

    def applies(self, z, ref):
        part = self.B["parts"][ref]
        if ref in z["allow"] or (z["only"] is not None and ref not in z["only"]):
            return False
        body = any(not q.get("np") for q in part["pads"])
        if (not body or _MECH.match(ref)) and (not z["holes"] or z["max_h"] is not None):
            return False                           # mounting hardware has no height: it counts only where nothing may be
        if part["side"][0] not in z["sides"]:
            # from the other side only through-hole leads reach in, by `[intent] lead_protrusion_mm` (1.5)
            if not part["tht"] or (body and z["max_h"] is not None and self.lead <= float(z["max_h"])):
                return False
            return True
        if z["max_h"] is not None and ref not in self.top and body:
            hh = self.h(ref)
            if hh is not None and hh <= float(z["max_h"]) and part["side"][0] in z["sides"]:
                return False
        return True

    def for_ref(self, ref):
        """[(name, rect)] the part must stay out of."""
        if ref not in self._for:
            self._for[ref] = [(z["name"], z["rect"]) for z in self.z if self.applies(z, ref)]
        return self._for[ref]


# ---------------------------------------------------------------- measuring
def _signal_nets(B):
    """{net: pads} without ground and the big rails (more than 12 pads)."""
    from . import netclass as NC
    return {n: ps for n, ps in NC.net_pads(B).items() if not NC.is_ground(n) and len(ps) <= 12}


def link(B, a, b, nets=None):
    """(distance mm, pad of a, pad of b, how) between two parts: the closest pads on a net they share, else the closest pads."""
    pa, pb = B["parts"][a]["pads"], B["parts"][b]["pads"]
    nets = _signal_nets(B) if nets is None else nets
    shared = {q["net"] for q in pa if q["net"] in nets} & {q["net"] for q in pb if q["net"] in nets}
    best = None
    for p in pa:
        for q in pb:
            if shared and (p["net"] != q["net"] or p["net"] not in shared):
                continue
            if p.get("np") or q.get("np"):
                continue
            d = math.hypot(p["x"] - q["x"], p["y"] - q["y"])
            if best is None or d < best[0]:
                best = (d, p, q)
    if best is None:
        ca, cb = B["parts"][a]["court"], B["parts"][b]["court"]
        return GM.rect_gap(ca, cb), None, None, "courtyards"
    return best[0], best[1], best[2], "net" if shared else "pads"


def _row(kind, name, hard, met, value, limit, detail="", refs=()):
    return {"kind": kind, "name": name, "hard": bool(hard), "met": met, "value": value, "limit": limit, "detail": detail,
            "refs": list(refs)}


def check(B, cons, spec=None, among=None, only_kinds=None):
    """the INTENT gate: one row per item, `met` True / False / None (cannot be judged), with the measured value.
    `among` limits the parts that count as obstacles (the seating engine uses it)."""
    it = items(B, cons)
    parts = B["parts"]
    seen = (lambda r: True) if among is None else (lambda r: r in among)
    rows = []
    want = (lambda k: True) if only_kinds is None else (lambda k: k in only_kinds)
    if want("edge"):
        for e in it["edge"]:
            for ref in e["refs"]:
                s, gap, on, face, zone = edge_state(B, cons, ref, e)
                hit = []
                if zone:
                    p0 = parts[ref]
                    hit = sorted(r for r, p in parts.items() if r != ref and seen(r) and not _MECH.match(r)
                                 and (p["tht"] or p0["tht"] or p["side"] == p0["side"]) and GM.overlap(zone, p["court"], -0.02))
                met = bool(on and face is not False and not hit)
                d = f"{s} edge, gap {gap:.2f} mm"
                if e["facing"]:
                    d += ", facing out" if face else ", front unknown: give front = \"+x\"..." if front_vec(B, cons, ref, e) is None \
                        else ", NOT facing out"
                if hit:
                    d += f"; {e['clear']:g} mm in front blocked by {', '.join(hit[:5])}"
                rows.append(_row("edge", f"{e['name']}: {ref}", e["hard"], met, round(gap, 2), e["gap"], d, [ref] + hit[:5]))
    nets = _signal_nets(B)
    if want("near"):
        for n in it["near"]:
            if n.get("chain"):
                ch = n["chain"]
                ds = [link(B, a, b, nets)[0] for a, b in zip(ch, ch[1:])]
                tot = sum(ds)
                rows.append(_row("near", f"{n['name']}: {' - '.join(ch)}", n["hard"], tot <= n["max"] + 1e-6, round(tot, 2), n["max"],
                                 "links " + " + ".join(f"{d:.1f}" for d in ds), ch))
            else:
                for r in n["refs"]:
                    d = link(B, r, n["to"], nets)[0]
                    rows.append(_row("near", f"{n['name']}: {r} - {n['to']}", n["hard"], d <= n["max"] + 1e-6, round(d, 2), n["max"],
                                     "", [r, n["to"]]))
    if want("away"):
        for a in it["away"]:
            best = None
            for x in a["a"]:
                for y in a["b"]:
                    if x == y or not (seen(x) and seen(y)):
                        continue
                    d = GM.rect_gap(parts[x]["court"], parts[y]["court"])
                    if best is None or d < best[0]:
                        best = (d, x, y)
            if best:
                rows.append(_row("away", f"{a['name']}", a["hard"], best[0] >= a["min"] - 1e-6, round(best[0], 2), a["min"],
                                 f"closest {best[1]} - {best[2]}", [best[1], best[2]]))
    if want("region") or want("group") or want("connectors"):
        Z = Zones(B, cons)
        by = {}
        for z in Z.z:
            if z["kind"] in ("region", "keepin", "swept"):
                by.setdefault((z["kind"], z["name"]), []).append(z)
        hard_of = {r["name"]: r["hard"] for r in it["region"]}
        hard_of.update({f"swept area of {g['name']}": g["hard"] for g in it["group"]})
        for (kind, name), zs in sorted(by.items()):
            if not want("group" if kind == "swept" else "region"):
                continue
            bad = sorted({r for z in zs for r, p in parts.items() if seen(r) and Z.applies(z, r)
                          and GM.overlap(p["court"], z["rect"], -0.02)})
            lim = zs[0]["max_h"]
            what = "must be inside" if kind == "keepin" else ("nothing" if lim is None else f"nothing taller than {float(lim):g} mm")
            det = what + (": " + ", ".join(f"{r}" + (f" ({Z.h(r):g} mm)" if Z.h(r) is not None else " (height unknown)"
                                                   if lim is not None else "") for r in bad[:8]) if bad else "")
            rows.append(_row("group" if kind == "swept" else "region", name, hard_of.get(name, True), not bad, len(bad), 0, det,
                             bad[:8] + sorted(zs[0]["allow"] if kind == "swept" else ())))
        if want("connectors"):
            for c in it["connectors"]:
                if c["entry"] == "unknown":
                    rows.append(_row("connector", f"{c['ref']} entry direction", False, None, "unknown", "side|top",
                                     f"footprint {c['fp']}: answer connector-entry:{c['fp']} or [intent.connectors]", [c["ref"]]))
    if want("group"):
        for g in it["group"]:
            if not g["refs"]:
                continue
            a = parts[g["refs"][0]]
            worst, n = 0.0, 0
            for r, want_rel in sorted(g["rel"].items()):
                if r not in parts:
                    continue
                p = parts[r]
                lx, ly = GM.rot(p["pos"][0] - a["pos"][0], p["pos"][1] - a["pos"][1], -a["angle"])
                da = ((p["angle"] - a["angle"] - float(want_rel[2]) + 180.0) % 360.0) - 180.0 if len(want_rel) > 2 else 0.0
                worst = max(worst, math.hypot(lx - float(want_rel[0]), ly - float(want_rel[1])), abs(da) / 10.0)
                n += 1
            if n:
                rows.append(_row("group", f"{g['name']}: rigid ({', '.join(g['refs'][:6])})", g["hard"], worst <= 0.05, round(worst, 3),
                                 0.05, f"{n} relative pose(s) checked", g["refs"]))
            else:
                rows.append(_row("group", f"{g['name']}: rigid ({', '.join(g['refs'][:6])})", g["hard"], None, "-", "-",
                                 "moved as one body by the placers; no `rel` poses to verify against", g["refs"]))
    if want("support"):
        rows += support_rows(B, cons, spec, it)
    return rows


def support_rows(B, cons, spec, it=None):
    """decoupling caps and the other support parts against their distance limits: offenders listed."""
    from . import clusters as CL, fluxmap as FM
    it = it or items(B, cons)
    cl, _links = CL.build(B, cons, spec)
    dec, oth, guess = [], [], 0
    n_dec = n_oth = 0
    near_refs = {r for n in it["near"] for r in (n.get("chain") or n.get("refs") or ())}
    for c in cl:
        for s in c["supports"]:
            k = FM._kind(s["ref"], s["cls"])
            if s["ref"] in it["support"]["waive"] or s["ref"] in near_refs:
                continue                           # a part the owner placed by a `near` item is graded there
            if not s["sure"] and s["how"] != "spread":       # bulk storage is not a per-pin bypass: not graded
                guess += k == "decap" and s["dist"] > s["limit"]
                continue
            if k == "decap":
                n_dec += 1
            else:
                n_oth += 1
            if s["dist"] > s["limit"] + 1e-6:
                (dec if k == "decap" else oth).append((s["ref"], c["ic"], s["pad"], s["dist"], s["limit"]))
    fmt = lambda v: ", ".join(f"{r} {d:.1f}>{l:g} mm of {ic}.{pad}" for r, ic, pad, d, l in sorted(v, key=lambda t: -t[3] / t[4])[:12])
    rows = [_row("support", f"decoupling caps within their limit ({n_dec} owned)", it["support"]["decap_hard"], not dec, len(dec), 0,
                 fmt(dec) + (f"; {guess} bulk cap(s) beyond the per-pin limit are not graded" if guess else ""), [t[0] for t in dec])]
    rows.append(_row("support", f"other support parts within their limit ({n_oth} owned)", it["support"]["hard"], not oth, len(oth), 0,
                     fmt(oth), [t[0] for t in oth]))
    return rows


def verdict(rows, cons):
    """(exit code, one line)."""
    met = sum(1 for r in rows if r["met"] is True)
    bad = [r for r in rows if r["met"] is False]
    unk = sum(1 for r in rows if r["met"] is None)
    hard = [r for r in bad if r["hard"]]
    fail = bool(hard) and enabled(cons)
    word = "FAIL" if fail else "WARN" if bad or unk else "OK"
    line = (f"INTENT: {word} - {len(rows)} item(s): {met} met, {len(bad)} not met ({len(hard)} hard), {unk} not judged"
            + ("" if enabled(cons) or not rows else "; no [intent] table in the constraints: reported, not gated"))
    return (1 if fail else 0), line


def format_rows(rows, top=None, only_bad=False):
    out = []
    sel = [r for r in rows if not only_bad or r["met"] is not True]
    sel.sort(key=lambda r: ({False: 0, None: 1, True: 2}[r["met"]], not r["hard"]))
    for r in sel[:top] if top else sel:
        st = {True: "met    ", False: "NOT MET", None: "?      "}[r["met"]]
        out.append(f"  {st} {'hard' if r['hard'] else 'soft'} {r['kind']:9s} {r['name'][:70]}: {r['value']} (limit {r['limit']})"
                   + (f"; {r['detail'][:200]}" if r["detail"] else ""))
    if top and len(sel) > top:
        out.append(f"  ... {len(sel) - top} more (--full)")
    return "\n".join(out)


# ---------------------------------------------------------------- seating engine (hard intent first, then held)
def _spiral(radius, step):
    n = int(radius / step)
    pts = [(math.hypot(i * step, j * step), i * step, j * step) for i in range(-n, n + 1) for j in range(-n, n + 1)]
    return [(dx, dy) for d, dx, dy in sorted(pts) if d <= radius + 1e-9]


class Seater:
    """Seats the hard intent on the board model `B` (mutated). After `run()`: `held` (refs to hold from now on), `bodies`
    (rigid groups of movable parts), `moves` ({ref: (x, y, angle)} that changed), `notes`."""

    def __init__(self, B, cons, spec=None, log=None):
        from . import clusters as CL, modules as MD, netclass as NC
        self.B, self.cons, self.spec = B, effective(cons, B), spec
        self.log = log or (lambda s: None)
        self.parts = B["parts"]
        self.it = items(B, self.cons)
        from . import preroute as PR
        grouped = {r for g in self.it["group"] for r in g["refs"]}
        # mounting hardware is held by default, but a stand-off that belongs to an intent group moves with its group
        self.fixed0 = {r for r in MD.fixed_set(B, self.cons) if r not in grouped or r in PR.explicit_fixed(B, self.cons)}
        self.held, self.bodies, self.notes = set(), [], []
        self.orig = {r: (p["pos"][0], p["pos"][1], p["angle"]) for r, p in self.parts.items()}
        cl, _ = CL.build(B, self.cons, spec)
        self.entry = {s["ref"]: s for c in cl for s in c["supports"] if s["sure"]}
        self.sup = {c["ic"]: [s["ref"] for s in c["supports"] if s["sure"]] for c in cl}
        self.owner = {s: ic for ic, v in self.sup.items() for s in v}
        self.group_of = {r: g["refs"] for g in self.it["group"] for r in g["refs"]}
        self.nets = _signal_nets(B)
        self.cls = NC.classify(B, self.cons, spec)
        self.near_refs = {r for n in self.it["near"] for r in (n.get("chain") or n.get("refs") or ())}
        self.front = {}
        for e in self.it["edge"]:
            for r in e["refs"]:
                f = front_vec(B, self.cons, r, e)
                if f:
                    self.front[r] = CN.local_key(self.parts[r], f)
        self.cons.setdefault("intent", {})["_front"] = dict(self.front)
        self.it = items(B, self.cons)
        self.refresh()

    # -- model
    def solid(self):
        return self.fixed0 | self.held

    def claimed(self):
        return self.solid() | {r for _n, refs in self.bodies for r in refs}

    def refresh(self):
        from . import legality as LGL
        self.L = LGL.Legal(self.B, self.cons, self.solid())

    def unit(self, ref, alone=False):
        refs = [ref] if alone else list(self.group_of.get(ref, [ref]))
        if self.it["support"]["follow"] and not alone:
            sol = self.solid()
            for r in list(refs):
                c0 = self.parts[r]["court"]
                for s in self.sup.get(r, ()):
                    if s in refs or s in sol or s in self.group_of or s in self.near_refs:
                        continue
                    if GM.rect_gap(c0, self.parts[s]["court"]) <= ATTACH_MM:
                        refs.append(s)
        return refs

    def poses(self, unit, anchor, x, y, dth):
        ax, ay = self.parts[anchor]["pos"]
        out = {}
        for r in unit:
            p = self.parts[r]
            rx, ry = GM.rot(p["pos"][0] - ax, p["pos"][1] - ay, dth)
            out[r] = (x + rx, y + ry, (p["angle"] + dth) % 360.0)
        return out

    def legal(self, unit, poses, ghosts=True):
        mem, cl = set(unit), self.claimed()
        saved = {}
        for r, (x, y, a) in poses.items():
            if r in self.L.edge:                   # the overhang side turns with the part (Legal read it at the old angle)
                saved[r] = self.L.edge[r]
                k = self.front.get(r)
                self.L.edge[r] = tuple(round(v, 6) for v in GM.rot(*CN.DIRS[k], a)) if k else None
        ok = True
        for r, (x, y, a) in poses.items():
            pads, court = GM.moved(self.parts[r], x, y, a)
            for kind, d in self.L.part_issues(r, court, pads):
                o = d if kind == "overlap" else d.split("/")[1].rsplit(".", 1)[0] if kind == "pad_short" else None
                if o is not None and (o in mem or (ghosts and o not in cl)):
                    continue
                ok = False
                break
            if not ok:
                break
        if not ok:
            self.L.edge.update(saved)
        return ok

    def apply(self, poses):
        old = {r: (self.parts[r]["pos"][0], self.parts[r]["pos"][1], self.parts[r]["angle"]) for r in poses}
        for r, (x, y, a) in poses.items():
            GM.place(self.parts[r], x, y, a)
            self.L.refresh(r)
        return old

    def intent_ok(self, unit, kinds=("edge", "away", "region", "group")):
        """no unmet hard row that involves the unit, counting only claimed parts and the unit as obstacles."""
        mem = set(unit)
        rows = check(self.B, self.cons, self.spec, among=self.claimed() | mem, only_kinds=set(kinds))
        return not any(r["met"] is False and r["hard"] and mem & set(r["refs"]) for r in rows)

    def wires(self, unit, anchor):
        """[(local dx, dy of a pad about the anchor, target x, y, weight)] for the signal nets the unit shares with others."""
        mem = set(unit)
        ax, ay = self.parts[anchor]["pos"]
        out = []
        for n, ps in self.nets.items():
            mine = [q for q in ps if q["ref"] in mem]
            oth = [q for q in ps if q["ref"] not in mem]
            if not mine or not oth:
                continue
            cx, cy = sum(q["x"] for q in oth) / len(oth), sum(q["y"] for q in oth) / len(oth)
            w = float(self.cls.get(n, {}).get("weight", 1.0))
            for q in mine:
                out.append((q["x"] - ax, q["y"] - ay, cx, cy, w / len(mine)))
        for n in self.it["near"]:                  # the owner's `near` items pull harder than plain nets
            pairs = list(zip(n["chain"], n["chain"][1:])) if n.get("chain") else [(r, n["to"]) for r in n["refs"]]
            for a, b in pairs:
                if (a in mem) == (b in mem):
                    continue
                me, oth = (a, b) if a in mem else (b, a)
                _d, pm, po, _h = link(self.B, me, oth, self.nets)
                if pm is not None:
                    out.append((pm["x"] - ax, pm["y"] - ay, po["x"], po["y"], 3.0 * n["weight"]))
        return out

    @staticmethod
    def wire_cost(wires, x, y, dth):
        c = 0.0
        for dx, dy, cx, cy, w in wires:
            rx, ry = GM.rot(dx, dy, dth)
            c += w * (abs(x + rx - cx) + abs(y + ry - cy))
        return c

    def take(self, unit, anchor, cands, kinds=("edge", "away", "region", "group"), limit=6000, ghosts=True):
        """first candidate (cost, x, y, dth), cheapest first, that is legal and leaves no unmet hard intent; applied."""
        for n, (_c, x, y, dth) in enumerate(sorted(cands)):
            if n >= limit:
                break
            poses = self.poses(unit, anchor, x, y, dth)
            if not self.legal(unit, poses, ghosts):
                continue
            old = self.apply(poses)
            if self.intent_ok(unit, kinds):
                return True
            self.apply(old)
        return False

    # -- the seats
    def seat_edge(self, ref, e):
        if ref in self.solid():
            return
        unit = self.unit(ref)
        if any(r in self.fixed0 for r in unit):
            self.notes.append(f"{ref}: in a fixed group, not moved")
            return
        a = self.parts[ref]
        s, gap, on, face, _z = edge_state(self.B, self.cons, ref, e)
        if on and face is not False and self.legal(unit, self.poses(unit, ref, a["pos"][0], a["pos"][1], 0)) and self.intent_ok(unit):
            self.hold(unit, f"{ref}: already on the {s} edge")
            return
        bx0, by0, bx1, by1 = self.B["bbox"]
        em = self.L.M.em + 0.02
        key = self.front.get(ref)
        wires = self.wires(unit, ref)
        cands = []
        for dth in (0, 90, 180, 270):
            ang = (a["angle"] + dth) % 360.0
            _pads, c = GM.moved(a, 0.0, 0.0, ang)
            f = GM.rot(*CN.DIRS[key], ang) if key else None
            for sd in e["sides"]:
                nx, ny = NORMAL[sd]
                if e["facing"] and f is not None and f[0] * nx + f[1] * ny < 0.7:
                    continue
                off = -e["overhang"] if e["overhang"] > 0 else em
                if sd in ("left", "right"):
                    x = bx0 + off - c[0] if sd == "left" else bx1 - off - c[2]
                    t, hi = by0 + em - c[1], by1 - em - c[3]
                    while t <= hi + 1e-9:
                        cands.append((self.wire_cost(wires, x, t, dth), x, t, dth))
                        t += 0.5
                else:
                    y = by0 + off - c[1] if sd == "top" else by1 - off - c[3]
                    t, hi = bx0 + em - c[0], bx1 - em - c[2]
                    while t <= hi + 1e-9:
                        cands.append((self.wire_cost(wires, t, y, dth), t, y, dth))
                        t += 0.5
        if self.take(unit, ref, cands) or self.take([ref], ref, cands):
            s2 = edge_state(self.B, self.cons, ref, e)[0]
            self.hold(unit, f"{ref}: seated on the {s2} edge" + (f" with {len(unit) - 1} support part(s)" if len(unit) > 1 else ""))
        else:
            self.notes.append(f"{ref}: NO legal seat on the {'/'.join(e['sides'])} edge (left where it is)")

    def settle(self, unit, anchor, why, radius=None, hold=True):
        """keep the unit where it is when that is legal among the claimed parts, else the nearest legal pose."""
        a = self.parts[anchor]
        if self.legal(unit, self.poses(unit, anchor, a["pos"][0], a["pos"][1], 0)) and self.intent_ok(unit):
            ok = True
        else:
            R = radius or self.B["diag"]
            x0, y0 = a["pos"]
            wires = self.wires(unit, anchor)
            nw = max(1.0, sum(w[4] for w in wires))
            cands = [(math.hypot(dx, dy) + (2.0 if dth else 0.0) + 0.5 * self.wire_cost(wires, x0 + dx, y0 + dy, dth) / nw,
                      x0 + dx, y0 + dy, dth) for dth in (0, 90, 180, 270) for dx, dy in _spiral(R, 1.0)]
            ok = self.take(unit, anchor, cands, limit=40000)
            why += ": moved to the nearest legal pose" if ok else ": NO legal pose found (left where it is)"
        if ok and hold:
            self.hold(unit, why)
        else:
            self.notes.append(why)
        return ok

    def pull(self, b, a, budget):
        """seat the unit of `b` next to `a` (closest pads on their shared net); -> link length after."""
        unit = self.unit(b)
        if b in self.solid() or any(r in self.solid() for r in unit):
            return link(self.B, b, a, self.nets)[0]
        d0, pb, pa, _how = link(self.B, b, a, self.nets)
        if pb is None:
            return d0
        p = self.parts[b]
        lx, ly = pb["x"] - p["pos"][0], pb["y"] - p["pos"][1]
        wires = self.wires(unit, b)
        R = max(6.0, min(budget, 25.0))
        cands = []
        for dth in (0, 90, 180, 270):
            rx, ry = GM.rot(lx, ly, dth)
            for dx, dy in _spiral(R, 0.5):
                cands.append((math.hypot(dx, dy) + 0.02 * self.wire_cost(wires, pa["x"] + dx - rx, pa["y"] + dy - ry, dth) / max(1, len(wires)),
                              pa["x"] + dx - rx, pa["y"] + dy - ry, dth))
        if not self.take(unit, b, cands, limit=20000):
            self.take([b], b, cands, limit=20000)
        return link(self.B, b, a, self.nets)[0]

    def hold(self, unit, why):
        self.held |= set(unit)
        self.notes.append(why)
        self.refresh()

    def seat_near(self, n):
        sol = self.solid()
        if n.get("chain"):
            ch = n["chain"]
            if len(ch) < 2:
                return
            base = next((i for i, r in enumerate(ch) if r in sol), None)
            order = ([(ch[i], ch[i - 1]) for i in range(1, len(ch))] if base is None else
                     [(ch[i], ch[i - 1]) for i in range(base + 1, len(ch))] + [(ch[i], ch[i + 1]) for i in range(base - 1, -1, -1)])
            share = n["max"] / max(1, len(ch) - 1)
            for b, a in order:
                d = self.pull(b, a, share)
                if base is not None:
                    self.hold(self.unit(b), f"{n['name']}: {b} seated {d:.1f} mm from {a}")
            if base is None:
                refs = sorted({r for c in ch for r in self.unit(c)})
                self.bodies.append((n["name"], refs))
                self.notes.append(f"{n['name']}: {' - '.join(ch)} made one rigid body ({len(refs)} parts), no member is held")
        else:
            to = n["to"]
            for r in n["refs"]:
                d = self.pull(r, to, n["max"])
                if to in sol:
                    self.hold(self.unit(r), f"{n['name']}: {r} seated {d:.1f} mm from {to}")
            if to not in sol:
                refs = sorted({x for c in [to] + n["refs"] for x in self.unit(c)})
                self.bodies.append((n["name"], refs))
                self.notes.append(f"{n['name']}: {', '.join(n['refs'])} + {to} made one rigid body, not held")

    def evict(self):
        """movable parts that now sit on a held part, in a forbidden zone or too close to a held part move to the nearest
        legal pose (an IC with its support parts)."""
        cl = self.claimed()
        todo = []
        for r in sorted(self.parts):
            if r in cl or _MECH.match(r):
                continue
            bad = False
            for kind, d in self.L.part_issues(r):
                o = d if kind == "overlap" else d.split("/")[1].rsplit(".", 1)[0] if kind == "pad_short" else None
                if o is None and kind != "outside" or (o is not None and o in cl):
                    bad = True
                    break
            if bad:
                todo.append(r)
        done, left = 0, []
        moved = set()
        for r in sorted(todo, key=lambda r: (-(self.parts[r]["court"][2] - self.parts[r]["court"][0]) *
                                             (self.parts[r]["court"][3] - self.parts[r]["court"][1]), r)):
            if r in moved:
                continue
            ic = self.owner.get(r)
            if ic and ic not in cl and r not in self.near_refs:
                # a support part in the way: first another seat at its own pin, else its IC moves with all its support parts
                from . import clusters as CL
                bx = self.B["bbox"]
                mv = CL.seat(self.B, {"ic": ic, "supports": [self.entry[r]]}, legal=self.L, strict=True,
                             bounds=(bx[0] + 0.2, bx[1] + 0.2, bx[2] - 0.2, bx[3] - 0.2))
                if r in mv:
                    self.apply({r: mv[r]})
                    done += 1
                    moved.add(r)
                    continue
                r = ic
            unit = [u for u in self.unit(r, alone=r in self.owner) if u not in cl]
            x0, y0 = self.parts[r]["pos"]
            cands = [(math.hypot(dx, dy) + (1.0 if dth else 0.0), x0 + dx, y0 + dy, dth) for dth in (0, 90)
                     for dx, dy in _spiral(min(self.B["diag"], 60.0), 0.5 if len(unit) == 1 else 1.0)]
            if self.take(unit, r, cands, kinds=("away", "region", "group"), limit=30000, ghosts=False) or \
                    self.take(unit, r, cands, kinds=("away", "region", "group"), limit=30000, ghosts=True):
                done += 1
                moved |= set(unit)
            else:
                left.append(r)
        if todo:
            self.notes.append(f"{done} movable part(s) moved out of held parts / forbidden zones"
                              + (f"; no room for {', '.join(left[:8])}" if left else ""))

    def run(self):
        it = self.it
        e_of = {r: e for e in it["edge"] if e["hard"] for r in e["refs"]}
        for g in sorted(it["group"], key=lambda g: -len(g["refs"])):
            if not g["refs"] or not g["hard"]:
                continue
            er = next((r for r in g["refs"] if r in e_of), None)
            if any(r in self.fixed0 for r in g["refs"]):
                self.notes.append(f"group {g['name']}: has fixed members, held as it is")
                self.held |= set(g["refs"]) - self.fixed0
                self.refresh()
            elif er:
                self.seat_edge(er, e_of[er])
            elif g["swept"]:
                self.settle(self.unit(g["refs"][0]), g["refs"][0], f"group {g['name']}")
        for reg in it["region"]:                       # a region under a part needs that part to stay
            if reg["under"] and reg["under"] not in self.solid():
                self.settle(self.unit(reg["under"]), reg["under"], f"{reg['under']} (region {reg['name']} lies under it)")
        # hard `near` that starts at a held part goes first (it fixes where the chain is); edge parts then see it
        later = []
        for n in it["near"]:
            if not n["hard"]:
                continue
            based = any(r in self.solid() for r in (n.get("chain") or [n.get("to")]))
            if based:
                self.seat_near(n)
            else:
                later.append(n)
        area = lambda r: (self.parts[r]["court"][2] - self.parts[r]["court"][0]) * (self.parts[r]["court"][3] - self.parts[r]["court"][1])
        for r in sorted(e_of, key=lambda r: (-area(r), r)):
            if r not in self.held:
                self.seat_edge(r, e_of[r])
        for n in later:
            self.seat_near(n)
        self.refresh()
        self.evict()
        if it["support"]["follow"]:
            from . import clusters as CL
            x0, y0, x1, y1 = self.B["bbox"]
            cl, _ = CL.build(self.B, self.cons, self.spec)
            self.refresh()
            bounds = (x0 + 0.2, y0 + 0.2, x1 - 0.2, y1 - 0.2)
            mv = CL.reseat(self.B, cl, bounds=bounds, legal=self.L, strict=True)
            spread = {s["ref"] for c in cl for s in c["supports"] if s["how"] == "spread" and s["dist"] > s["limit"]
                      and s["ref"] not in self.solid()}
            if spread:                              # rail caps near no IC pin at all: seated at the pin they are assigned to
                mv.update(CL.reseat(self.B, cl, guess=True, only=spread, bounds=bounds, legal=self.L, strict=True))
            if mv:
                self.notes.append(f"{len(mv)} support part(s) re-seated at their pins")
        return self

    @property
    def moves(self):
        out = {}
        for r, p in self.parts.items():
            now = (round(p["pos"][0], 4), round(p["pos"][1], 4), round(p["angle"], 4))
            o = self.orig[r]
            if abs(now[0] - o[0]) > 1e-4 or abs(now[1] - o[1]) > 1e-4 or abs((now[2] - o[2] + 180) % 360 - 180) > 1e-3:
                out[r] = (now[0], now[1], (now[2] + 180.0) % 360.0 - 180.0)
        return out

    def cons_out(self):
        """the constraints the placer runs with: held parts are fixed, rigid bodies are placement groups."""
        c = copy.deepcopy(self.cons)
        pl = c.setdefault("placement", {})
        pl["fixed"] = sorted(set(pl.get("fixed", []) or []) | self.held)
        for name, refs in self.bodies:
            pl.setdefault("group", {})["intent:" + name] = {"refs": list(refs)}
        return c


def prepare(board, cons, spec, workdir, log=print, name="intent-seated"):
    """seat the hard intent of `board` into a copy in `workdir`. -> (board path, constraints to run with, Seater | None).
    Without an `[intent]` table nothing happens."""
    if not section(cons):
        return board, cons, None
    from . import boardwrite as BW, routediag as RD
    B = RD.load(board)
    S = Seater(B, cons, spec, log=log).run()
    out = os.path.join(workdir, name + ".kicad_pcb")
    os.makedirs(workdir, exist_ok=True)
    BW.move_file(board, out, S.moves, post=False)
    import shutil
    for ext in (".kicad_pro", ".fluxmap.json", ".assumed.json"):
        src = os.path.splitext(board)[0] + ext
        if os.path.exists(src):
            shutil.copy(src, os.path.splitext(out)[0] + ext)
    if log:
        log(f"intent: {len(S.held)} part(s) seated and held, {len(S.bodies)} rigid bod(ies), {len(S.moves)} part(s) moved; {out}")
        for n in S.notes:
            log("  " + n)
    return out, S.cons_out(), S


def move_with_support(B, cons, spec, ref, x, y, angle, legal=None, log=None):
    """relocate `ref` to (x, y, angle) on the model and bring its support parts along: the near ones rigidly, then every
    support beyond its limit is re-seated at its pin. -> {ref: (x, y, angle)} of everything that moved."""
    from . import clusters as CL, legality as LGL
    parts = B["parts"]
    cl, _ = CL.build(B, cons, spec)
    sup = [s["ref"] for c in cl if c["ic"] == ref for s in c["supports"] if s["sure"]]
    p = parts[ref]
    ax, ay, a0 = p["pos"][0], p["pos"][1], p["angle"]
    dth = angle - a0
    near = [s for s in sup if GM.rect_gap(p["court"], parts[s]["court"]) <= ATTACH_MM and not parts[s].get("locked")]
    out = {}
    for r in [ref] + near:
        q = parts[r]
        rx, ry = GM.rot(q["pos"][0] - ax, q["pos"][1] - ay, dth)
        GM.place(q, x + rx, y + ry, (q["angle"] + dth) % 360.0)
        out[r] = (round(q["pos"][0], 4), round(q["pos"][1], 4), q["angle"])
    L = legal or LGL.Legal(B, cons)
    for r in out:
        L.refresh(r)
    cl2, _ = CL.build(B, cons, spec)
    x0, y0, x1, y1 = B["bbox"]
    mv = CL.reseat(B, [c for c in cl2 if c["ic"] == ref], everything=False, bounds=(x0 + 0.2, y0 + 0.2, x1 - 0.2, y1 - 0.2),
                   legal=L, strict=True, log=log)
    out.update({r: v for r, v in mv.items()})
    return out


# ---------------------------------------------------------------- floorplan hooks (soft intent = cost, movable hard away = legality)
def plan_links(B, cons):
    """`near` items as two-pin nets for the floorplan: ({name: [pad, pad]}, {name: {"weight", "budget"}})."""
    pads, info = {}, {}
    if not section(cons):
        return pads, info
    nets = _signal_nets(B)
    for n in items(B, cons)["near"]:
        pairs = list(zip(n["chain"], n["chain"][1:])) if n.get("chain") else [(r, n["to"]) for r in n["refs"]]
        for k, (a, b) in enumerate(pairs):
            _d, pa, pb, _how = link(B, a, b, nets)
            if pa is None:
                continue
            name = f"intent/{n['name']}/{k}"
            pads[name] = [pa, pb]
            info[name] = {"weight": n["weight"], "budget": n["max"] / max(1, len(pairs)), "class": "intent"}
    return pads, info


class PlanTerms:
    """`away` items for floorplan2.Plan: a penalty for the soft ones, a legality test for the hard ones between two
    movable modules (held sources are forbidden rectangles already)."""

    def __init__(self, P):
        self.P = P
        self.items = []
        self.by_mod = {}
        if not section(P.cons):
            return
        parts = P.B["parts"]
        for a in items(P.B, P.cons)["away"]:
            if not a["a"] or not a["b"]:
                continue
            rad = lambda r: 0.5 * max(parts[r]["court"][2] - parts[r]["court"][0], parts[r]["court"][3] - parts[r]["court"][1])
            self.items.append((a, [P.idx[P.of[r]] for r in a["a"]], [P.idx[P.of[r]] for r in a["b"]],
                               [rad(r) for r in a["a"]], [rad(r) for r in a["b"]]))
            if a["hard"]:
                for src, dst in ((a["a"], a["b"]), (a["b"], a["a"])):
                    for r in src:
                        self.by_mod.setdefault(P.idx[P.of[r]], []).append((r, dst, a["min"]))

    def penalty(self):
        """sum over away items of weight x shortfall (module anchors, minus the part radii): cheap, called every move."""
        P, pen = self.P, 0.0
        for a, ma, mb, ra, rb in self.items:
            for i, m in enumerate(ma):
                for j, k in enumerate(mb):
                    if m == k:
                        continue
                    d = math.hypot(P.X[m] - P.X[k], P.Y[m] - P.Y[k]) - ra[i] - rb[j]
                    if d < a["min"]:
                        pen += a["weight"] * (a["min"] - d) * (10.0 if a["hard"] else 1.0)
        return pen

    def ok(self, i):
        """hard `away` holds for the members of module i against the parts placed now."""
        parts = self.P.B["parts"]
        mem = self.P.mods[i]["members"]
        for r, dst, mn in self.by_mod.get(i, ()):
            c = parts[r]["court"]
            for o in dst:
                if o in mem or parts[o]["pos"][0] > 9e4:
                    continue
                if GM.rect_gap(c, parts[o]["court"]) < mn - 1e-6:
                    return False
        return True


# ---------------------------------------------------------------- command
def report(board, cons, spec, rows, extra=None):
    import json
    path = os.path.splitext(board)[0] + ".intent.json"
    doc = {"board": board, "rows": rows, "enabled": enabled(cons)}
    doc.update(extra or {})
    try:
        with open(path, "w") as fh:
            json.dump(doc, fh, indent=1, default=str)
    except OSError:
        path = None
    return path


def gate(board, cons, spec, out=print, top=12, B=None):
    """run the gate on a board file and print it. -> exit code (1 = unmet hard intent and `[intent]` is present)."""
    from . import routediag as RD
    B = B or RD.load(board)
    rows = check(B, cons, spec)
    rc, line = verdict(rows, cons)
    path = report(board, cons, spec, rows, {"connectors": items(B, cons)["connectors"]})
    out(line + (f"; report {path}" if path else ""))
    txt = format_rows(rows, top=top, only_bad=True)
    if txt:
        out(txt)
    return rc


def cmd_intent(a):
    import json
    from . import constraints as C, questions as QS, routediag as RD, boardwrite as BW
    spec = json.load(open(a.spec)) if a.spec else None
    cons = C.load(a.constraints)
    S, cons, spec = QS.guard(a, "intent", a.board, cons, spec, topics=("connector-entry",))
    board = a.board
    if a.apply:
        if not a.out or os.path.abspath(a.out) == os.path.abspath(a.board):
            raise SystemExit("intent --apply needs --out (the input board is never overwritten)")
        B = RD.load(a.board)
        st = Seater(B, cons, spec).run()
        BW.move_file(a.board, a.out, st.moves, post=False)
        S.mark(a.out)
        print(f"intent --apply: {len(st.moves)} part(s) moved, {len(st.held)} held; wrote {a.out}")
        for n in st.notes:
            print("  " + n)
        if st.held:
            print("  hold them in the next step: [placement] fixed += " + json.dumps(sorted(st.held)))
        board, cons = a.out, st.cons_out()
    B = RD.load(board)
    rows = check(B, cons, spec)
    rc, line = verdict(rows, cons)
    conn = items(B, cons)["connectors"]
    path = report(board, cons, spec, rows, {"connectors": conn})
    print(line + (f"; report {path}" if path else ""))
    full = bool(getattr(a, "full_output", False))
    if not getattr(a, "quiet", False):
        txt = format_rows(rows, top=None if full else (a.top or 15), only_bad=not full)
        if txt:
            print(txt)
        if conn and (full or a.connectors):
            print("connectors:")
            print(CN.format_table(conn))
        elif conn:
            n = {k: sum(1 for c in conn if c["entry"] == k) for k in ("side", "top", "unknown")}
            print(f"connectors: {len(conn)} ({n['side']} side-entry, {n['top']} top-entry, {n['unknown']} unknown); --connectors lists them")
    raise SystemExit(rc)


def add_parser(sub):
    from . import questions as QS
    p = sub.add_parser("intent", help="INTENT gate: every placement-intent item of the constraints as met / NOT MET with the "
                                      "measured value; --apply seats the hard items (docs/INTENT.md)")
    p.add_argument("board")
    p.add_argument("--constraints", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--apply", action="store_true", help="seat the hard intent (edge parts, rigid groups, hard near) and write --out")
    p.add_argument("--out", default=None)
    p.add_argument("--connectors", action="store_true", help="print the connector table (entry direction, opening, how derived)")
    p.add_argument("--full", dest="full_output", action="store_true")
    p.add_argument("-q", "--quiet", dest="quiet", action="store_true")
    p.add_argument("--top", type=int, default=None)
    QS.add_args(p)
    p.set_defaults(fn=cmd_intent)
