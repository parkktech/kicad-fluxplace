"""IC support clusters: which 2-pin parts belong to which IC, and where they
should sit. Pure text (routediag.load).

Ownership of a 2-pin passive/crystal/ferrite (C R L FB Y X):
  * a pad on a net that carries pins of exactly ONE IC  -> that IC owns the part,
    and the pin it serves is that IC's nearest pad on the net;
  * a pad on a shared rail (ground, or a net with pins of several ICs) -> only
    when the other pad gives no owner: the rail's ICs are narrowed to the part's
    reference-designator block (C416 -> 4xx), then to the numerically nearest
    reference (U410), and any remaining tie is broken so caps spread evenly over
    the rail's ICs by supply-pin count. A rail capacitor is never given to an
    arbitrary far IC;
  * a part whose two pads reach two DIFFERENT ICs is a chain link (series R,
    ferrite), not support.
The spec JSON adds nothing here beyond pin names; it carries no grouping.
"""
import math
import re
from collections import defaultdict

from . import geom as GM
from . import netclass as NC

SUPPORT = re.compile(r"^(C|R|L|FB|Y|X|XO)\d")
LANE_MM = {True: 1.0, False: 0.5}
DECIDED = ("block", "refdes", "group")      # owner fixed by the netlist's blocks / references / the rail's source-load groups (not a guess)


def block(ref):
    m = re.search(r"(\d+)$", ref)
    return int(m.group(1)) // 100 if m else 0


def number(ref):
    m = re.search(r"(\d+)$", ref)
    return int(m.group(1)) if m else 0


def _is_support(ref, part):
    return bool(SUPPORT.match(ref)) and part["n_pads"] == 2


def ownership(B, cls=None, cons=None, spec=None, min_pads=5):
    """({support_ref: {ic, net, pad (IC pad number), how}}, {link refs})."""
    cls = cls or NC.classify(B, cons, spec)
    parts, pads = B["parts"], NC.net_pads(B)
    ic_refs = {r for r, p in parts.items() if NC.is_ic(r, p, min_pads)}
    ics_on = {n: sorted({p["ref"] for p in ps if p["ref"] in ic_refs}) for n, ps in pads.items()}
    own, links, rail_caps = {}, set(), []

    def nearest_pad(ic, net, near):
        ps = [p for p in pads[net] if p["ref"] == ic]
        return min(ps, key=lambda p: math.hypot(p["x"] - near["x"], p["y"] - near["y"]))

    for ref in sorted(parts):
        part = parts[ref]
        if not _is_support(ref, part) or ref in ic_refs:
            continue
        sides = []
        for p in part["pads"]:
            n = p["net"]
            if not n or NC.is_ground(n):
                sides.append((p, None, "gnd"))
            elif len(ics_on.get(n, ())) == 1 and not (
                    ref.startswith("C") and any(
                        q["ref"] not in ic_refs and not SUPPORT.match(q["ref"]) for q in pads[n])):
                # (a capacitor on a net that also reaches a connector/module is
                # decoupling of a distribution rail, not of the one IC: block rule)
                sides.append((p, ics_on[n][0], "own"))
            elif len(ics_on.get(n, ())) >= 1 and ref.startswith("C"):
                sides.append((p, None, "rail"))
            elif len(ics_on.get(n, ())) > 1:
                sides.append((p, None, "rail"))
            else:
                sides.append((p, None, "none"))
        rf = [s for s in sides if (cls.get(s[0]["net"]) or {}).get("class") == "rf"]
        if rf and not any(s[1] for s in rf):
            continue        # in line with an RF net that reaches no IC (bias inductor, DC block, matching part at the antenna):
            #                 the part belongs to the RF line and its net budget, not to the IC on its DC side
        owners = {s[1] for s in sides if s[1]}
        if len(owners) > 1:
            links.add(ref)
            continue
        if owners:
            ic = next(iter(owners))
            cand = [s for s in sides if s[1]]
            # serve the pad on the highest-weight class net
            p = max((s[0] for s in cand), key=lambda q: cls.get(q["net"], {}).get("weight", 0))
            tgt = nearest_pad(ic, p["net"], p)
            own[ref] = {"ic": ic, "net": p["net"], "pad": tgt["num"], "via": p["num"], "how": "net"}
            continue
        rails = [s[0] for s in sides if s[2] == "rail"]
        if rails:
            rail_caps.append((ref, rails[0]))
    # shared-rail parts: block, then nearest reference, then spread by pin count
    load = defaultdict(int)
    for ref, p in rail_caps:
        net = p["net"]
        cand = ics_on[net]
        same = [c for c in cand if block(c) == block(ref)] if block(ref) else []
        how = "block"
        if not same:
            same, how = cand, "spread"      # flat numbering / no IC in the block: guess
        cand = same
        if len(cand) > 1 and how != "spread":
            d = {c: abs(number(c) - number(ref)) for c in cand}
            m = min(d.values())
            near = [c for c in cand if d[c] == m]
            how = "refdes"
            cand = near
        if len(cand) > 1:
            cand = sorted(cand, key=lambda c: (load[(net, c)] / max(1, sum(
                1 for q in pads[net] if q["ref"] == c)), c))
            how = "spread"
        ic = cand[0]
        load[(net, ic)] += 1
        tgt = nearest_pad(ic, net, p)
        own[ref] = {"ic": ic, "net": net, "pad": tgt["num"], "via": p["num"], "how": how}
    _by_group(B, cons, spec, own)
    _interchange(B, cons, own)
    _explicit(B, cons, own, links)
    return own, links


def _interchange(B, cons, own):
    """Identical parts are interchangeable: bulk capacitors (> powerbudget.BULK_UF) of a planned rail with the same net, value and
    footprint and a decided owner (block / refdes / group) are re-dealt among their owners' pins so the summed squared distance
    is least (each owner keeps its count of each value). Two equal parts that swapped places are two served pins; a part parked
    at a pin that owns no such part stays far. Deterministic: nearest pair first, then pairwise exchange until no gain.
    (Bypass parts need none of this: build's `near` rule already lets any pin of the rail take them.)"""
    from . import powerbudget as PB
    parts, pads = B["parts"], NC.net_pads(B)
    groups = defaultdict(list)
    for ref, o in own.items():
        if o["how"] in DECIDED and ref.startswith("C") and (cap_uf(parts[ref].get("value")) or 0) > PB.BULK_UF and PB._planned(cons, o["net"]):
            groups[(o["net"], str(parts[ref].get("value")), str(parts[ref].get("fp")))].append(ref)
    for (net, _v, _f), refs in sorted(groups.items()):
        refs.sort()
        slots = [own[r]["ic"] for r in refs]
        if len(set(slots)) < 2:
            continue
        via = {r: next(q for q in parts[r]["pads"] if q["num"] == own[r]["via"]) for r in refs}
        near = {}
        for r in refs:
            for ic in set(slots):
                near[(r, ic)] = min(((q["x"] - via[r]["x"]) ** 2 + (q["y"] - via[r]["y"]) ** 2, str(q["num"]), q) for q in pads[net] if q["ref"] == ic)
        cost = lambda r, ic: near[(r, ic)][0]
        free, got = list(range(len(slots))), {}
        for _c, r, i in sorted((cost(r, slots[i]), r, i) for r in refs for i in range(len(slots))):
            if r not in got and i in free:
                got[r] = i
                free.remove(i)
        better = True
        while better:
            better = False
            for a in refs:
                for b in refs:
                    if a < b and cost(a, slots[got[b]]) + cost(b, slots[got[a]]) < cost(a, slots[got[a]]) + cost(b, slots[got[b]]) - 1e-9:
                        got[a], got[b] = got[b], got[a]
                        better = True
        for r in refs:
            ic = slots[got[r]]
            if ic != own[r]["ic"]:
                own[r] = dict(own[r], ic=ic, pad=near[(r, ic)][2]["num"], swapped=own[r]["ic"])


COVER_DECADE = 10.0         # a pin's bypass is replaced only by a capacitor within this factor of its value (INF, first guess)


def _supply_pads(ic, pins, spec, facts, mpn_map):
    """(pads, kind): the IC's pads on a rail that its `supply_pins` fact names -> kind "supply". No fact, or none of
    these pads is named (an unlisted supply pin stays graded) -> every pad, kind "unlisted"."""
    from . import elec as EL
    comp = next((c for c in (spec or {}).get("components", []) or [] if isinstance(c, dict) and c.get("ref") == ic), None)
    sp = set(((facts or {}).get(EL._mpn(comp, mpn_map)) or {}).get("supply_pins") or []) if comp else set()
    keep = [q for q in pins if sp & set(EL._names(comp, q["num"]))] if sp else []
    return (keep, "supply") if keep else (pins, "unlisted")


def pin_demand(B, cons=None, spec=None, own=None, facts=None, mpn_map=None):
    """Per-pin demand (EVAL-PLACER-STEP1 D4): [{ic, pad, net, parts, uf, dist_mm, limit_mm, cover, verdict}], one row per IC and
    planned rail whose own bypass is missing at the pin. Demand: the netlist gives the IC a bypass capacitor on the rail
    (a capacitor to ground of at most powerbudget.BULK_UF whose owner is decided: block / refdes / group). Supply: every
    capacitor to ground on the rail within powerbudget.BYPASS_MAX_MM of one of the IC's pads on the rail, whoever owns it.
      a capacitor within COVER_DECADE of the demanded value is there  -> no row (the pin is served; `near` did its job)
      only a larger capacitor is there (bulk parked at the pin)        -> verdict "over-target-pass" (named, not failed)
      no capacitor of at least value / COVER_DECADE is there           -> verdict "fail" (the pin is bare)
    dist_mm = distance of the nearest of the IC's own parts to the pad; cover = {ref, uf, dist_mm} of the nearest larger
    capacitor or None. Ownership and the `near` rule are not changed: this is a second reading of the same table."""
    from . import powerbudget as PB
    if own is None:
        own, _links = ownership(B, None, cons, spec)
    if facts is None:
        facts, mpn_map = PB.facts_for(cons, spec)
    parts, pads = B["parts"], NC.net_pads(B)

    def cap_of(ref):
        part = parts[ref]
        if not (ref.startswith("C") and part["n_pads"] == 2 and any(NC.is_ground(q["net"] or "") for q in part["pads"])):
            return None
        return cap_uf(part.get("value"))
    caps = defaultdict(list)                             # net -> [(ref, uF, pad on the net)]
    for ref in sorted(parts):
        uf = cap_of(ref)
        for q in parts[ref]["pads"] if uf else ():
            if q["net"] and not NC.is_ground(q["net"]):
                caps[q["net"]].append((ref, uf, q))
    want = defaultdict(list)                             # (ic, net) -> [decided bypass refs]
    for ref in sorted(own):
        o, uf = own[ref], cap_of(ref)
        if uf and uf <= PB.BULK_UF and o["how"] in DECIDED and PB._planned(cons, o["net"]):
            want[(o["ic"], o["net"])].append(ref)
    out = []
    for (ic, net), refs in sorted(want.items()):
        pins, kind = _supply_pads(ic, [q for q in pads[net] if q["ref"] == ic], spec, facts, mpn_map)
        d = lambda c: min(math.hypot(c["x"] - q["x"], c["y"] - q["y"]) for q in pins)      # noqa: E731
        uf = min(cap_of(r) for r in refs)
        near = sorted((round(d(c), 2), r, u) for r, u, c in caps[net] if d(c) <= PB.BYPASS_MAX_MM and u >= uf / COVER_DECADE)
        if any(u <= uf * COVER_DECADE for _d, _r, u in near):
            continue
        dist, ref = min((d(next(q for q in parts[r]["pads"] if q["net"] == net)), r) for r in refs)
        pad = min(pins, key=lambda q: math.hypot(q["x"] - parts[ref]["pos"][0], q["y"] - parts[ref]["pos"][1]))["num"]
        out.append({"ic": ic, "pad": pad, "net": net, "parts": refs, "uf": uf, "dist_mm": round(dist, 2), "limit_mm": PB.BYPASS_MAX_MM,
                    "cover": {"ref": near[0][1], "uf": near[0][2], "dist_mm": near[0][0]} if near else None, "pin_kind": kind,
                    "verdict": "over-target-pass" if near else "fail"})
    return out


def rehome(B, cl, cons=None, spec=None, demand=None):
    """Per-pin demand applied to the cluster table (P5): for every pin_demand row the IC's own bypass part nearest to the pin
    leaves the cluster that holds it (the `near` rule parked it at another IC's pin, or it is far) and joins its own IC's
    cluster: served pad = the row's pad, how = "pin-demand", sure, limit from the one support table. Mutates `cl`.
    -> {ref: {from, to, pad, dist}}. A part already in its own IC's cluster is not listed (nothing to do)."""
    from . import powerbudget as PB
    parts = B["parts"]
    rows = pin_demand(B, cons, spec) if demand is None else demand
    where = {s["ref"]: c for c in cl for s in c["supports"]}
    by_ic = {c["ic"]: c for c in cl}
    out = {}
    for p in rows:
        pin = next(q for q in parts[p["ic"]]["pads"] if str(q["num"]) == str(p["pad"]) and q["net"] == p["net"])
        d = {}
        for r in p["parts"]:
            via = next(q for q in parts[r]["pads"] if q["net"] == p["net"])
            d[r] = math.hypot(via["x"] - pin["x"], via["y"] - pin["y"])
        ref = min(sorted(d), key=lambda r: d[r])
        src = where.get(ref)
        if src is None or src["ic"] == p["ic"]:
            continue
        s = next(x for x in src["supports"] if x["ref"] == ref)
        row = PB.support_row(B, cons, spec, ref, p["net"], None, ic=p["ic"], pad=pin["num"])
        dist = max(0.0, d[ref] - HALO_MM) if _pitch(parts[p["ic"]]) <= 0.5 else d[ref]
        src["supports"].remove(s)
        s.update(ic=p["ic"], pad=pin["num"], dist=dist, how="pin-demand", sure=True, limit=row["max"], target=row["target"], rank=9)
        dst = by_ic.get(p["ic"])
        if dst is None:
            dst = by_ic[p["ic"]] = {"ic": p["ic"], "supports": [], "regulator": False}
            cl.append(dst)
            cl.sort(key=lambda c: c["ic"])
        dst["supports"].append(s)
        dst["supports"].sort(key=lambda x: (x["rank"], -x["weight"], x["dist"], x["ref"]))
        where[ref] = dst
        out[ref] = {"from": src["ic"], "to": p["ic"], "pad": pin["num"], "dist": round(d[ref], 2)}
    return out


def unseated(cl, fixed=()):
    """Confidently-owned support parts beyond their limit after a seating pass: [{ref, ic, pad, dist, limit, why}], why =
    "fixed" (the part may not move) or "no legal seat" (the seater found none). Sorted by ref."""
    return sorted(({"ref": s["ref"], "ic": c["ic"], "pad": s["pad"], "dist": round(s["dist"], 2), "limit": s["limit"],
                    "why": "fixed" if s["ref"] in fixed else "no legal seat"} for c in cl for s in c["supports"] if s["sure"] and s["dist"] > s["limit"]),
                  key=lambda x: x["ref"])


def loop_metrics(B, c, names=None):
    """The three lengths a switching regulator's datasheet layout fixes, measured on the board model for cluster `c`
    (regulator = it has a switch-node part): {"cin_loop_mm": input-capacitor loop (input pin -> capacitor -> its ground pad ->
    nearest ground pad of the IC; the shortest over the rank-1 parts), "sw_mm": IC switch pin to the farthest switch-node pad
    (the extent of the hot node), "boot_mm": bootstrap part to its pin, "fb_mm": the farthest rank-3 two-pad resistor to its
    pin}. A value is None when the cluster has no such part. Rigid moves of the whole module leave every value unchanged."""
    parts = B["parts"]
    ic = parts[c["ic"]]
    gnd = [q for q in ic["pads"] if NC.is_ground(q["net"] or "")]
    out = {"cin_loop_mm": None, "sw_mm": None, "boot_mm": None, "fb_mm": None}

    def dist(a, b):
        return math.hypot(a["x"] - b["x"], a["y"] - b["y"])
    for s in c["supports"]:
        part = parts[s["ref"]]
        via = next((q for q in part["pads"] if q["num"] == s["via"]), None)
        pin = next((q for q in ic["pads"] if q["num"] == s["pad"]), None)
        if via is None or pin is None:
            continue
        if s["rank"] == 1 and s["ref"].startswith("C") and gnd:
            g = next((q for q in part["pads"] if NC.is_ground(q["net"] or "")), None)
            if g is not None:
                v = dist(pin, via) + dist(via, g) + min(dist(g, q) for q in gnd)
                out["cin_loop_mm"] = v if out["cin_loop_mm"] is None else min(out["cin_loop_mm"], v)
        elif s["rank"] == 0:
            v = max(dist(pin, q) for q in part["pads"] if q["net"] == s["net"])
            out["sw_mm"] = v if out["sw_mm"] is None else max(out["sw_mm"], v)
        elif s["rank"] == 2:
            v = dist(pin, via)
            out["boot_mm"] = v if out["boot_mm"] is None else max(out["boot_mm"], v)
        elif s["rank"] == 3 and s["ref"].startswith("R") and part["n_pads"] == 2:
            v = dist(pin, via)
            out["fb_mm"] = v if out["fb_mm"] is None else max(out["fb_mm"], v)
    return {k: (round(v, 3) if v is not None else None) for k, v in out.items()}


_BLOCKS = {}


def _block_of(B, cons, spec):
    """{ref: functional block} (blocks.of_board), kept per netlist: the grouping does not depend on the placement."""
    from . import blocks as BK
    key = (hash(tuple(sorted((q["ref"], str(q["num"]), q["net"] or "") for q in B["pads"]))),
           repr(sorted(((cons or {}).get("blocks") or {}).items())), repr(sorted((((cons or {}).get("schematic") or {}).get("blocks") or {}).items())),
           spec if isinstance(spec, str) else None)
    if key not in _BLOCKS:
        _BLOCKS.clear()
        try:
            _BLOCKS[key] = BK.of_board(B, cons, spec)["of"]
        except Exception:               # noqa: BLE001  (a block clash is ready's finding, not ours: no block rule then)
            _BLOCKS[key] = {}
    return _BLOCKS[key]


def _by_group(B, cons, spec, own):
    """A guessed (`spread`) support part of a planned rail that sits in the same functional block as exactly ONE source or load
    group of that rail (powerbudget.resolve_ends) serves that group: its nearest pad of the group on the net. how = "group".
    Two groups in the block, none, or a group that is itself a two-pad part: the guess stays and `ready` asks."""
    from . import powerbudget as PB
    guess = sorted(r for r, o in own.items() if o["how"] == "spread" and PB._planned(cons, o["net"]))
    if not guess:
        return
    parts, of, ends = B["parts"], _block_of(B, cons, spec), {}
    facts, mpn_map = PB.facts_for(cons, spec)
    for ref in guess:
        o = own[ref]
        net = o["net"]
        if net not in ends:
            src, loads = PB.resolve_ends(B, cons, net, spec, facts, mpn_map)
            g = defaultdict(set)
            for e in ([src] if src else []) + loads:
                for r, num in e["pads"]:
                    g[r].add(str(num))
            ends[net] = g
        same = [r for r in sorted(ends[net]) if of.get(r) is not None and of.get(r) == of.get(ref) and not _is_support(r, parts[r])]
        if len(same) != 1:
            continue
        via = next(q for q in parts[ref]["pads"] if q["num"] == o["via"])
        pin = min((q for q in parts[same[0]]["pads"] if q["net"] == net and str(q["num"]) in ends[net][same[0]]),
                  key=lambda q: math.hypot(q["x"] - via["x"], q["y"] - via["y"]))
        own[ref] = {"ic": same[0], "net": net, "pad": pin["num"], "via": o["via"], "how": "group"}


def _explicit(B, cons, own, links):
    """`[power."NET"] support = {REF = "IC.PAD"}`: the owner decided which pin the part serves; it wins over every guess."""
    parts = B["parts"]
    for net, pw in ((cons or {}).get("power") or {}).items():
        for ref, tgt in ((pw or {}).get("support") or {}).items() if isinstance(pw, dict) else ():
            ic, _, pad = str(tgt).partition(".")
            part = parts.get(ref)
            pin = next((q for q in parts.get(ic, {}).get("pads", ()) if str(q["num"]) == pad and q["net"] == net), None)
            via = next((q for q in part["pads"] if q["net"] == net), None) if part else None
            if pin and via:
                own[ref] = {"ic": ic, "net": net, "pad": pin["num"], "via": via["num"], "how": "explicit"}
                links.discard(ref)


_CAPV = re.compile(r"([\d.]+)\s*([pnuµm]?)F", re.I)
BULK_UF = 4.7


def cap_uf(value):
    m = _CAPV.search(value or "")
    if not m:
        return None
    return float(m.group(1)) * {"p": 1e-6, "n": 1e-3, "u": 1.0, "µ": 1.0, "m": 1e3, "": 1.0}[m.group(2).lower()]


HALO_MM = 0.6           # legalise.ESC_MIN: the mandatory escape halo of a fine-pitch IC
_BOOT = re.compile(r"BOOT|BST|CBOOT", re.I)
_VIN = re.compile(r"^(VIN|VBAT|VSUP|PVIN|VI)\d*$|VIN|VBATT", re.I)


def reg_rank(s, parts, pinname, cls):
    """Regulator template priority: 0 switch node, 1 input-capacitor loop, 2 bootstrap,
    3 feedback/output/everything else (conflicts are resolved in this order)."""
    pn = pinname.get(s["pad"], "")
    # a bootstrap cap spans BOOT and SW: it is the bootstrap, not the switch-node part
    for n in s.get("nets", ()):
        if _BOOT.search(n or ""):
            return 2
    for q in parts[s["ic"]]["pads"]:
        if q["net"] in s.get("nets", ()) and _BOOT.search(pinname.get(q["num"], "")):
            return 2
    if s["cls"] == "switch_node":
        return 0
    if _BOOT.search(pn) or _BOOT.search(s["net"]):
        return 2
    if _VIN.search(pn) or _VIN.search(s["net"]):
        return 1
    return 3


def build(B, cons=None, spec=None, facts=None, mpn_map=None):
    """[{ic, supports: [{ref, net, pad, cls, weight, dist, limit, role, target, source, klass}]}], ordered:
    switch-node parts first, then by class weight, then by distance. `limit` is the max (FAIL), `target` warns:
    both come from powerbudget.support_row, the one support table."""
    from . import powerbudget as PB
    cls = NC.classify(B, cons, spec)
    own, links = ownership(B, cls, cons, spec)
    parts = B["parts"]
    if facts is None:
        facts, mpn_map = PB.facts_for(cons, spec)          # once per build (cached per manifest), never per part
    by = defaultdict(list)
    for ref, o in own.items():
        part = parts[ref]
        sp = next(p for p in part["pads"] if p["num"] == o["via"])
        pin = next(p for p in parts[o["ic"]]["pads"] if p["num"] == o["pad"])
        info = cls.get(o["net"])
        row = PB.support_row(B, cons, spec, ref, o["net"], info, facts=facts, mpn_map=mpn_map)    # default limit (no served pin yet)
        lim = row["max"]
        planned_bulk = row["role"] == "bulk"
        sole = len({q["ref"] for q in NC.net_pads(B)[o["net"]] if NC.is_ic(q["ref"], parts[q["ref"]])}) == 1
        guess = o["how"] == "spread" and not (planned_bulk and sole)     # one IC on a planned rail: nothing to guess between
        sure, how, ic_, pad_, dist = not guess, o["how"], o["ic"], o["pad"], math.hypot(
            sp["x"] - pin["x"], sp["y"] - pin["y"])
        if o["how"] not in ("net", "explicit") and not (planned_bulk and o["how"] in DECIDED):
            # shared rail: a cap already within its limit of ANY IC pin on the rail serves that pin. Not a planned rail's bulk
            # part with a decided owner: storage belongs to the regulator or load it was sized for, wherever it sits (a bulk part
            # parked at another IC's pin is a far part, not a served pin; identical bulk parts are re-dealt in _interchange).
            best = min(((math.hypot(q["x"] - sp["x"], q["y"] - sp["y"]), q) for q in NC.net_pads(B)[o["net"]]
                        if NC.is_ic(q["ref"], parts[q["ref"]])), key=lambda t: t[0], default=None)
            if best and best[0] <= lim:
                ic_, pad_, dist, sure, how = best[1]["ref"], best[1]["num"], best[0], True, "near"
            elif (cap_uf(part["value"]) or 0) > BULK_UF and (guess or not planned_bulk):
                sure, how = False, "bulk"          # bulk storage on an unplanned rail is not per-pin bypass
            elif planned_bulk:
                how = "bulk"                       # a planned rail's bulk part is graded against its owner's pin
        row = PB.support_row(B, cons, spec, ref, o["net"], info, ic=ic_, pad=pad_, facts=facts, mpn_map=mpn_map)
        by[ic_].append({
            "ref": ref, "net": o["net"], "pad": pad_, "via": o["via"], "how": how,
            "sure": sure,
            "cls": info["class"] if info else "signal", "weight": info["weight"] if info else 1.0,
            "dist": dist, "limit": row["max"], "target": row["target"], "role": row["role"], "unit": "mm",
            "source": row["source"], "klass": row["klass"]})
    out = []
    names = NC.pin_names(spec)
    for ic in sorted(by):
        reg = any(s["cls"] == "switch_node" for s in by[ic])
        for s in by[ic]:
            s["ic"] = ic
            s["nets"] = [q["net"] for q in parts[s["ref"]]["pads"]]
            s["rank"] = reg_rank(s, parts, names.get(ic, {}), cls) if reg else 9
            if _pitch(parts[ic]) <= 0.5:
                s["dist"] = max(0.0, s["dist"] - HALO_MM)   # measured from the edge of the keep-clear halo
        sup = sorted(by[ic], key=lambda s: (s["rank"], -s["weight"], s["dist"], s["ref"]))
        out.append({"ic": ic, "supports": sup, "regulator": reg})
    return out, links


def far_parts(cl):
    """Confidently-owned support parts beyond their limit (guessed ownership,
    `how == "spread"`, is never graded)."""
    return [(c["ic"], s) for c in cl for s in c["supports"] if s["sure"] and s["dist"] > s["limit"]]


# ---------------------------------------------------------------- seating
def _lanes(B, ic, side_ref=None):
    """Escape-lane rectangles of every pad of `ic`: from the pad's outer edge
    outward (away from the part centre), as wide as the pad's narrow side."""
    part = B["parts"][ic]
    cx = sum(p["x"] for p in part["pads"]) / len(part["pads"])
    cy = sum(p["y"] for p in part["pads"]) / len(part["pads"])
    fine = _pitch(part) <= 0.5
    L = LANE_MM[fine]
    out = []
    for p in part["pads"]:
        dx, dy = p["x"] - cx, p["y"] - cy
        horiz = abs(dx) * p["ey"] >= abs(dy) * p["ex"] if (p["ex"] and p["ey"]) else abs(dx) >= abs(dy)
        if abs(p["ex"] - p["ey"]) > 0.25 * max(p["ex"], p["ey"]):
            horiz = p["ex"] > p["ey"]
        if horiz:
            s = 1 if dx >= 0 else -1
            out.append((p["x"] + s * p["ex"] if s > 0 else p["x"] - p["ex"] - L, p["y"] - p["ey"],
                        p["x"] + p["ex"] + L if s > 0 else p["x"] - p["ex"], p["y"] + p["ey"]))
        else:
            s = 1 if dy >= 0 else -1
            out.append((p["x"] - p["ex"], p["y"] + p["ey"] if s > 0 else p["y"] - p["ey"] - L,
                        p["x"] + p["ex"], p["y"] + p["ey"] + L if s > 0 else p["y"] - p["ey"]))
    return out


def _pitch(part):
    pts = [(p["x"], p["y"]) for p in part["pads"]]
    best = 999.0
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
            if 0.01 < d < best:
                best = d
    return best


def _outward(part, pin):
    cx = sum(p["x"] for p in part["pads"]) / len(part["pads"])
    cy = sum(p["y"] for p in part["pads"]) / len(part["pads"])
    dx, dy = pin["x"] - cx, pin["y"] - cy
    n = math.hypot(dx, dy) or 1.0
    if abs(dx) >= abs(dy):
        return (1 if dx >= 0 else -1, 0)
    return (0, 1 if dy >= 0 else -1)


def seat(B, cl, bounds=None, step=0.25, reach=8.0, log=None, keepouts=True, isolate=False, legal=None,
         strict=False, left=None):
    """Re-seat the movable support parts of cluster `cl` around its (fixed) IC.
    Returns {ref: (x, y, angle)}. Rigid: each part keeps its footprint; only its
    origin and rotation change. Candidate positions are tried in order of
    served-pad-to-pin distance (plus a ground-return term for caps); with `isolate` only
    the IC counts as an obstacle, so the unit is laid out as a compact macro cell and
    placed among the other parts afterwards; the first
    one that stays out of every other courtyard, the IC's escape lanes, the
    board edge and track-forbidding rule areas wins. With `legal` (legality.Legal) a seat must
    also add no violation there (same-side courtyards of through-hole parts, the real outline with its
    rounded corners, named keep-outs and side regions, no-through-hole regions). With `strict` a seat
    farther than the part's limit from its pin is refused: the part stays and is appended to `left`
    as (ref, reason)."""
    parts = B["parts"]
    ic = cl["ic"]
    icp = parts[ic]
    occ = GM.Hash()
    halo = _pitch(icp) <= 0.5
    for r, p in parts.items():
        if not isolate or r == ic:          # isolate: lay the unit out on its own (macro cell)
            occ.add(r, p["court"], p["side"])
    lanes = _lanes(B, ic)
    zones = [z for z in B["zones"] if z["keepout"] and z["no_tracks"] and z["pts"]] if keepouts else []
    moves = {}
    for s in cl["supports"]:
        ref = s["ref"]
        part = parts[ref]
        if part.get("locked"):
            continue
        pin = next(p for p in icp["pads"] if p["num"] == s["pad"])
        ox, oy = _outward(icp, pin)
        gnd = [p for p in icp["pads"] if NC.is_ground(p["net"])]
        other = [p for p in part["pads"] if p["num"] != s["via"]]
        is_gnd_cap = bool(other and NC.is_ground(other[0]["net"]) and gnd)
        n_d = int(reach / step)
        base = (pin["x"] + ox * pin["ex"], pin["y"] + oy * pin["ey"])
        grid = sorted(((math.hypot(ia * step, it * step), ia * step, it * step)
                       for ia in range(0, n_d + 1) for it in range(-n_d, n_d + 1)))
        pick, best = None, math.inf
        for approx, d, t in grid:
            if approx > best + 2.5:
                break
            cx = base[0] + ox * d + (-oy) * t
            cy = base[1] + oy * d + ox * t
            for ang in (0.0, 90.0, 180.0, 270.0):
                pads, court = GM.moved(part, cx, cy, ang)
                sp = next(p for p in pads if p["num"] == s["via"])
                cost = math.hypot(sp["x"] - pin["x"], sp["y"] - pin["y"])
                if is_gnd_cap:
                    op = next(p for p in pads if p["num"] != s["via"])
                    cost += 0.3 * min(math.hypot(op["x"] - g["x"], op["y"] - g["y"]) for g in gnd)
                if cost >= best:
                    continue
                if bounds and not GM.inside(court, bounds):
                    continue
                if any(GM.overlap(court, ln) for ln in lanes):
                    continue
                if any(True for _ in occ.hits(court, part["side"], 0.0, ignore=(ref,))):
                    continue
                if zones and _in_zone(zones, part, court):
                    continue
                if strict and math.hypot(sp["x"] - pin["x"], sp["y"] - pin["y"]) > s["limit"] + (
                        HALO_MM if halo else 0.0):
                    continue
                if legal is not None and legal.part_issues(ref, court, pads):
                    continue
                pick, best = (cx, cy, ang, court, cost), cost
        if pick:
            cx, cy, ang, court, cost = pick
            moves[ref] = (round(cx, 4), round(cy, 4), ang)
            occ.add(ref, court, part["side"])
            if legal is not None:
                legal.hash.add(ref, court, "x")
            if log:
                log(f"  {ref} -> ({cx:.2f}, {cy:.2f}) {ang:g} deg  "
                    f"{s['dist']:.1f} -> {cost:.1f} mm (pin {ic}.{s['pad']})")
        else:
            why = (f"no legal seat within its {s['limit']:g} mm limit of {ic}.{s['pad']}" if strict
                   else f"no free seat within {reach:g} mm of {ic}.{s['pad']}")
            if left is not None:
                left.append((ref, why))
            if log:
                log(f"  {ref}: {why} - left in place")
    return moves


def _in_zone(zones, part, court):
    pts = [((court[0] + court[2]) / 2, (court[1] + court[3]) / 2), (court[0], court[1]),
           (court[2], court[1]), (court[2], court[3]), (court[0], court[3])]
    return any(part["side"] in z["layers"] and any(GM.poly_has(z["pts"], x, y) for x, y in pts)
               for z in zones)


def reseat(B, cl, everything=False, guess=False, bounds=None, log=None, isolate=False, only=None,
           legal=None, strict=False, left=None):
    """Seat the far support parts of every cluster around their ICs (ICs never
    move). Mutates the board model B as it goes so later clusters see earlier
    moves. Parts already within their limit stay put unless `everything`;
    guessed ownership (`spread`) is skipped unless `guess`. -> {ref: (x, y, angle)}"""
    allm = {}
    for c in cl:
        sup = [s for s in c["supports"]
               if (everything or s["dist"] > s["limit"]) and (guess or s["sure"])
               and (only is None or s["ref"] in only)]
        if not sup:
            continue
        if log:
            log(f"{c['ic']}: re-seating {len(sup)} part(s)")
        m = seat(B, {"ic": c["ic"], "supports": sup}, bounds=bounds, log=log, isolate=isolate,
                 legal=legal, strict=strict, left=left)
        for ref, (x, y, a) in m.items():
            GM.place(B["parts"][ref], x, y, a)
            if legal is not None:
                legal.refresh(ref)
        allm.update(m)
    return allm


def format_text(cl, links=()):
    L = []
    for c in cl:
        L.append(f"{c['ic']}  ({len(c['supports'])} support part(s))")
        if c.get("regulator"):
            tag = {0: "switch node", 1: "input cap", 2: "bootstrap", 3: "feedback/output"}
            L.append("  regulator template: " + "; ".join(
                f"{tag[s['rank']]} {s['ref']} {s['dist']:.1f} mm" for s in c["supports"] if s["rank"] < 9))
        for s in c["supports"]:
            flag = "FAR " if s["dist"] > s["limit"] else "    "
            g = "" if s["sure"] else " (guessed owner)"
            L.append(f"  {flag}{s['ref']:6s} pin {s['pad']:>4s} {s['net']:20s} {s['cls']:11s} "
                     f"{s['dist']:6.1f} mm (limit {s['limit']:g}){g}")
    if links:
        L.append(f"chain links (series parts between two ICs, not support): {', '.join(sorted(links))}")
    return "\n".join(L)
