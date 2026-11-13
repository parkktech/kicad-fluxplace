"""Escape reservations for the placer / legaliser: room a part needs to get its pins out is kept free while its
neighbours are placed or legalised.

  bands      fine-pitch parts (pad pitch <= `[escape] fine_pitch_mm`, 0.65) and multi-row parts (>= 2 pad rows on a side):
             per pad side a HARD band (the first escape row: stub + via + clearance) and a SOFT band behind it (one via row
             per further pad row / per 12 pins).  Hard = no other part body there; soft = a part is moved out when a legal
             seat exists within the band's `cost` (mm of displacement the reservation is worth)
  pin sites  escres.plan on the parts that stay put (via site + lane of power pins and of passives beside a controlled
             pair), as soft rectangles
  corridors  pair centre lines of `pairs --plan-only` (pairs.plan), as soft rectangles on their outer layer

`for_legalise(B, cons, spec, movable, pins=False)` (bands only: every pass of the placer; pin sites and, with
`[escape] reserve_pairs = true`, pair corridors are added on its final pass) -> {"rects": [{rect, sides, hard, cost, owner, why}], "inflate": {ref: (l, t, r, b)}}:
bands of parts that stay put are absolute rectangles; a movable owner carries its hard band as inflation.
legalise.legalise(reserve=...) applies it; `[escape] reserve = false` turns it off."""
import math

from . import escapeplan as EP
from . import geom as GM

SOFT_COST = 3.0


def params(cons):
    e = dict((cons or {}).get("escape") or {})
    return {"on": e.get("reserve", True) is not False, "fine": float(e.get("fine_pitch_mm", 0.65)), "pins": e.get("reserve_pins", True) is not False,
            "pairs": bool(e.get("reserve_pairs", False)), "cost": float(e.get("reserve_cost_mm", SOFT_COST))}


def _rows(vals, tol=0.15):
    out = []
    for v in sorted(vals):
        if not out or v - out[-1] > tol:
            out.append(v)
    return len(out)


def part_bands(part, pads, court, d1, drow, fine_mm):
    """[(side index 0 left / 1 top / 2 right / 3 bottom, hard depth, soft depth, rect span lo, hi)] for one part pose."""
    ps = [p for p in pads if not p.get("np") and p["w"] * p["h"] <= 4.0]
    if len({p["num"] for p in ps}) < 6:
        return []
    fine = EP.part_pitch({"pads": ps}) <= fine_mm
    cx, cy = (court[0] + court[2]) / 2, (court[1] + court[3]) / 2
    hw, hh = max((court[2] - court[0]) / 2, 1e-6), max((court[3] - court[1]) / 2, 1e-6)
    side = [[], [], [], []]
    for p in ps:
        dx, dy = p["x"] - cx, p["y"] - cy
        if abs(dx) / hw >= abs(dy) / hh:
            side[2 if dx > 0 else 0].append(p)
        else:
            side[3 if dy > 0 else 1].append(p)
    out = []
    for k, sp in enumerate(side):
        if len(sp) < 3:
            continue
        rows = _rows([p["x"] if k in (0, 2) else p["y"] for p in sp])
        if not fine and rows < 2:
            continue
        soft = (rows - 1) + (len(sp) // 12 if fine else 0)
        along = [(p["y"], p["ey"]) if k in (0, 2) else (p["x"], p["ex"]) for p in sp]
        out.append((k, d1, soft * drow, min(a - e for a, e in along), max(a + e for a, e in along)))
    return out


def _band_rect(court, k, lo, hi, d0, d1):
    """rectangle from d0 to d1 outside court side k, spanning lo..hi along it."""
    if k == 0:
        return (court[0] - d1, lo, court[0] - d0, hi)
    if k == 2:
        return (court[2] + d0, lo, court[2] + d1, hi)
    if k == 1:
        return (lo, court[1] - d1, hi, court[1] - d0)
    return (lo, court[3] + d0, hi, court[3] + d1)


def _sides(part):
    return {"F", "B"} if part["tht"] else {"F" if part["side"] == "F.Cu" else "B"}


def seg_rects(a, b, width, piece=1.0):
    """axis-aligned rectangles covering a track corridor (a diagonal is cut into `piece` mm steps)."""
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    n = 1 if (abs(a[0] - b[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6) else max(1, int(math.ceil(L / piece)))
    out = []
    for k in range(n):
        p = (a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n)
        q = (a[0] + (b[0] - a[0]) * (k + 1) / n, a[1] + (b[1] - a[1]) * (k + 1) / n)
        out.append((min(p[0], q[0]) - width / 2, min(p[1], q[1]) - width / 2, max(p[0], q[0]) + width / 2, max(p[1], q[1]) + width / 2))
    return out


def corridor_rects(corridors, copper, cost=SOFT_COST):
    """pair centre-line corridors [{layer, pts, width}] -> soft reservations on the outer layers."""
    out = []
    for c in corridors:
        side = "F" if c["layer"] == copper[0] else ("B" if c["layer"] == copper[-1] else None)
        if not side:
            continue
        for a, b in zip(c["pts"], c["pts"][1:]):
            out += [{"rect": r, "sides": {side}, "hard": False, "cost": cost, "owner": None, "why": "pair corridor"} for r in seg_rects(a, b, c["width"])]
    return out


def pair_corridors(board, B, cons, spec):
    """centre-line corridors of every pair that can be planned on the current placement (pairs.plan, plan-only)."""
    from . import pairs as PR
    res = PR.plan(board, cons, spec, plan_only=True, B=B)
    return [c for e in res["pairs"] for s in e["sections"] for c in s.get("corridor", [])]


def for_legalise(B, cons=None, spec=None, movable=(), board=None, corridors=None, pins=None):
    P = params(cons)
    out = {"rects": [], "inflate": {}}
    if not P["on"]:
        return out
    from . import routerules as RR
    R = RR.rules(cons, None, B=B)
    track, clr, via = max(R["track"], B["track"]), max(R["clearance"], B["clearance"]), R["via"]
    d1, drow = via + 2 * clr + track, via + clr
    movable = set(movable)
    cache = B.setdefault("_escreserve", {})               # bands per part pose: the placer calls this in a loop
    for ref, part in B["parts"].items():
        if EP.is_mech(ref) or part["n_pads"] < 6:
            continue
        key = (ref, round(part["pos"][0], 3), round(part["pos"][1], 3), part["angle"], d1, drow, P["fine"])
        if key not in cache:
            cache[key] = part_bands(part, part["pads"], part["court"], d1, drow, P["fine"])
        bands = cache[key]
        if not bands:
            continue
        if ref in movable:
            a = [0.0, 0.0, 0.0, 0.0]
            for k, hard, _soft, _lo, _hi in bands:
                a[k] = hard
            out["inflate"][ref] = tuple(a)
            continue
        sd = _sides(part)
        for k, hard, soft, lo, hi in bands:
            out["rects"].append({"rect": _band_rect(part["court"], k, lo, hi, 0.0, hard), "sides": sd, "hard": True, "cost": None, "owner": ref,
                                 "why": f"first escape row of {ref}"})
            if soft > 0:
                out["rects"].append({"rect": _band_rect(part["court"], k, lo, hi, hard, hard + soft), "sides": sd, "hard": False, "cost": P["cost"],
                                     "owner": ref, "why": f"escape rows of {ref}"})
    if P["pins"] and pins is not False:
        out["rects"] += pin_sites(B, cons, spec, movable, P["cost"])
    if corridors is None and P["pairs"] and pins is not False:
        try:
            corridors = pair_corridors(board, B, cons, spec)
        except Exception:                                # noqa: BLE001 - advisory
            corridors = None
    if corridors:
        out["rects"] += corridor_rects(corridors, B["copper"], P["cost"])
    return out


def pin_sites(B, cons, spec, movable, cost=SOFT_COST):
    """escres.plan on the parts that stay put (the movable ones taken off the board): each reserved via site + lanes as a soft rectangle."""
    from . import escres as ER
    keep = {r: p for r, p in B["parts"].items() if r not in movable}
    if not keep or len(keep) == len(B["parts"]) and not movable:
        return []
    Bs = dict(B, parts=keep, pads=[p for p in B["pads"] if p["ref"] in keep])
    try:
        res = ER.plan(Bs, cons, spec)
    except Exception:                                    # noqa: BLE001 - a reservation is advisory; the legaliser must still run
        return []
    out = []
    for r in res["reserved"]:
        pts = [r["pad_xy"]] + r["lane"] + [r["exit"][1]]
        m = r["via"] / 2 + 0.15
        xs, ys = [q[0] for q in pts], [q[1] for q in pts]
        part = B["parts"].get(r["ref"])
        out.append({"rect": (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m), "sides": _sides(part) if part else {"F", "B"}, "hard": False,
                    "cost": cost, "owner": r["ref"], "why": f"escape of {r['ref']}.{r['pin']} ({r['why']})"})
    return out


def crowding(B, reserve):
    """[(ref, why)] parts whose courtyard sits in a reservation that is not theirs (a report; the legaliser acts on it)."""
    out = []
    for ref, part in B["parts"].items():
        if EP.is_mech(ref):
            continue
        sd = _sides(part)
        for r in reserve["rects"]:
            if r["owner"] != ref and sd & r["sides"] and GM.overlap(part["court"], r["rect"]):
                out.append((ref, ("HARD " if r["hard"] else "soft ") + r["why"]))
                break
    return out
