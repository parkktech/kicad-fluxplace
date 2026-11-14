"""Schematic model + block inventory for `fluxplace schematic --layout blocks`.

Model (from a spec or a board): {name, comps: {ref: {ref, value, fp, dnp, pins: {pad: {name, net}}, nc: [(pad, name)]}},
nets: {net: [(ref, pad)]}}. A spec net lists `[ref, pin]`; when the part has a `pinmap` (pin name -> pad) the pin is a NAME
and the pad is `pinmap[name]`, so the schematic pin numbers are the real footprint pads and the pin names are the functions.
A pinmap pad that no net uses is a declared no-connect (the only no_connect flags the drawing carries).

Block inventory (board-agnostic, no geometry):
  1. `[schematic.blocks] NAME = ["U3", "C3*", ...]` (refs / fnmatch globs) claims parts first, in TOML order;
  2. the rest form modules the way `modules.build` does (every part that is not a small 2-pad passive is an anchor;
     `clusters.ownership` gives an IC its own supports; the rest join the module they share the most signal nets with,
     else the anchor whose reference number is nearest);
  3. modules merge greedily by shared SIGNAL nets (ground, rails and nets that reach more than `bus_modules` modules
     do not glue), normalised by size, with a bonus for the same reference-designator series (U3xx with C3xx), up to
     `max_parts` parts per block; a block left with fewer than `min_parts` parts joins its strongest neighbour.
  Parts that are not passives and whose (at most two) pins are all ground or unconnected (mounting holes, fiducials,
  shield tabs) form one "Mechanical" block. A small block with no signal in common joins the block it shares a rail with.
Tunables live in `[schematic]` (max_parts, min_parts, bus_modules); nothing here names a board's refs or nets.
"""
import fnmatch
import json
import math
import os
import re
from collections import Counter, defaultdict

SMALL = re.compile(r"^(C|R|L|FB|Y|X|XO|D|LED|F)\d")
_RAILX = re.compile(r"^[A-Z0-9]*V(DD|CC|IN|BAT|BATT|BUS)[A-Z0-9_]*$", re.I)


def natkey(s):
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", str(s)) if t]


def refnum(ref):
    m = re.search(r"(\d+)$", ref)
    return int(m.group(1)) if m else 0


def series(ref):
    return refnum(ref) // 100


# ------------------------------------------------------------------ model
def from_spec(path):
    with open(path) as fh:
        spec = json.load(fh)
    comps = {}
    for c in spec["components"]:
        pm = c.get("pinmap") or {}
        if isinstance(pm, str):
            import ast
            pm = ast.literal_eval(pm)
        comps[c["ref"]] = {"ref": c["ref"], "value": str(c.get("value", "")),
                           "fp": "%s:%s" % (c.get("lib", ""), c.get("fp", "")) if c.get("lib") or c.get("fp") else "",
                           "dnp": bool(c.get("dnp")), "pins": {}, "nc": [], "pinmap": {str(k): str(v) for k, v in pm.items()},
                           "errors": []}
    nets = {}
    for net, conns in spec["nets"].items():
        out = []
        for ref, pin in conns:
            pin = str(pin)
            c = comps.get(ref)
            if c is None:
                comps[ref] = c = {"ref": ref, "value": "", "fp": "", "dnp": False, "pins": {}, "nc": [], "pinmap": {},
                                  "errors": ["not in components"]}
            pm = c["pinmap"]
            if pin in pm:
                pad, name = pm[pin], pin
            else:
                pad = pin
                name = next((k for k, v in pm.items() if v == pin), "")
            old = c["pins"].get(pad)
            if old and old["net"] != net:
                c["errors"].append("pad %s on %s and %s" % (pad, old["net"], net))
            c["pins"][pad] = {"name": name or (old or {}).get("name", ""), "net": net}
            out.append((ref, pad))
        nets[net] = out
    for c in comps.values():
        used = set(c["pins"])
        seen = set()
        for name, pad in sorted(c["pinmap"].items(), key=lambda kv: natkey(kv[1])):
            if pad not in used and pad not in seen:
                c["nc"].append((pad, name))
                seen.add(pad)
    return {"name": spec.get("name", os.path.splitext(os.path.basename(path))[0]), "comps": comps, "nets": nets,
            "source": os.path.basename(path)}


def from_board(path):
    from . import schematic as SC
    name, comps, nets = SC.from_board(path)
    M = {"name": name, "comps": {}, "nets": {}, "source": os.path.basename(path)}
    for c in comps:
        M["comps"][c["ref"]] = {"ref": c["ref"], "value": c["value"], "fp": c["fp"], "dnp": False, "pinmap": {},
                                "pins": {str(p): {"name": "", "net": n} for p, n in c["pins"]}, "nc": [], "errors": []}
    for n, conns in nets.items():
        M["nets"][n] = sorted({(r, str(p)) for r, p in conns})
    # a pad KiCad gave its own "unconnected-(...)" net is a no-connect, not a one-pin signal
    for n in [n for n, c in M["nets"].items() if n.startswith("unconnected-") and len(c) == 1]:
        r, p = M["nets"].pop(n)[0]
        c = M["comps"][r]
        c["pins"].pop(p, None)
        c["nc"].append((p, ""))
    return M


def load(spec=None, board=None):
    return from_spec(spec) if spec else from_board(board)


# ------------------------------------------------------------------ net kinds
def net_kinds(M, cons=None):
    """{net: 'gnd' | 'rail' | 'sig'}: ground and power nets become power symbols."""
    from . import netclass as NC
    pw = set((cons or {}).get("power") or {})
    out = {}
    for n in M["nets"]:
        if NC.is_ground(n):
            out[n] = "gnd"
        elif n in pw or NC._RAILNAME.search(n) or _RAILX.match(n):
            out[n] = "rail"
        else:
            out[n] = "sig"
    return out


def two_pin(c):
    """A drawn-as-passive part: exactly two connected pads, no declared NC, small-part reference."""
    return len(c["pins"]) == 2 and not c["nc"] and bool(SMALL.match(c["ref"]))


# ------------------------------------------------------------------ inventory
def _pseudo_board(M):
    parts, pads = {}, []
    for r, c in M["comps"].items():
        ps = [{"num": p, "net": i["net"], "ref": r, "x": 0.0, "y": 0.0} for p, i in c["pins"].items()]
        parts[r] = {"n_pads": len(ps), "pads": ps, "court": (0, 0, 0, 0), "pos": (0, 0), "angle": 0, "side": "F"}
        pads += ps
    return {"parts": parts, "pads": pads, "bbox": (0, 0, 1, 1)}


def _modules(M, refs, kinds):
    from . import clusters as CL
    comps = M["comps"]
    B = _pseudo_board({"comps": {r: comps[r] for r in refs}, "nets": M["nets"]})
    try:
        own, _links = CL.ownership(B)
    except Exception:
        own = {}
    anchors = [r for r in sorted(refs, key=natkey) if not two_pin(comps[r])]
    of, mods = {}, {}
    for r in anchors:
        of[r] = r
        mods[r] = [r]
    left = []
    for r in sorted(refs, key=natkey):
        if r in of:
            continue
        o = own.get(r, {}).get("ic")
        if o in of:
            of[r] = of[o]
            mods[of[o]].append(r)
        else:
            left.append(r)
    pend = list(left)
    for _ in range(4):                      # supports of supports (RC chains) settle in a few passes
        nxt = []
        for r in pend:
            sc = Counter()
            for p in comps[r]["pins"].values():
                if kinds.get(p["net"]) != "sig":
                    continue
                for q, _pad in M["nets"][p["net"]]:
                    if q != r and q in of:
                        sc[of[q]] += 1
            if sc:
                best = sorted(sc.items(), key=lambda kv: (-kv[1], natkey(kv[0])))[0][0]
                of[r] = best
                mods[best].append(r)
            else:
                nxt.append(r)
        if len(nxt) == len(pend):
            break
        pend = nxt
    for r in pend:
        if not mods:
            mods[r] = [r]
            of[r] = r
            continue
        best = min(mods, key=lambda a: (series(a) != series(r), abs(refnum(a) - refnum(r)), natkey(a)))
        of[r] = best
        mods[best].append(r)
    return mods, of


def inventory(M, cons=None):
    """-> {"blocks": [{name, refs, anchor, how}], "of": {ref: block name}, "modules": n}"""
    cons = cons or {}
    sc = cons.get("schematic") or {}
    kinds = net_kinds(M, cons)
    comps = M["comps"]
    allrefs = sorted(comps, key=natkey)
    of, blocks = {}, []
    for name, pats in (sc.get("blocks") or {}).items():
        pats = [pats] if isinstance(pats, str) else list(pats)
        got = [r for r in allrefs if r not in of and any(fnmatch.fnmatchcase(r, p) for p in pats)]
        if got:
            blocks.append({"name": str(name), "refs": got, "how": "constraints"})
            for r in got:
                of[r] = str(name)
    mech = [r for r in allrefs if r not in of and not two_pin(comps[r]) and len(comps[r]["pins"]) <= 2
            and all(kinds[i["net"]] == "gnd" for i in comps[r]["pins"].values())]
    rest = [r for r in allrefs if r not in of and r not in mech]
    n_auto = len(rest)
    max_parts = int(sc.get("max_parts", max(24, int(math.ceil(n_auto / 10.0)) if n_auto else 24)))
    min_parts = int(sc.get("min_parts", 6))
    bus = int(sc.get("bus_modules", 6))
    nmod = 0
    if rest:
        mods, mof = _modules(M, rest, kinds)
        nmod = len(mods)
        # signal-net incidence per module (nets that reach too many modules are buses: no glue)
        inc = defaultdict(set)
        for n, conns in M["nets"].items():
            if kinds[n] != "sig":
                continue
            ms = {mof[r] for r, _p in conns if r in mof}
            if 2 <= len(ms) <= bus:
                for m in ms:
                    inc[m].add(n)
        groups = {m: {"refs": list(rs), "nets": set(inc[m]), "series": Counter(series(r) for r in rs)}
                  for m, rs in mods.items()}

        def anchor_of(rs):
            return sorted(rs, key=lambda r: (-len(comps[r]["pins"]) - len(comps[r]["nc"]), natkey(r)))[0]

        def score(a, b):
            ga, gb = groups[a], groups[b]
            sh = len(ga["nets"] & gb["nets"])
            sa, sb = series(anchor_of(ga["refs"])), series(anchor_of(gb["refs"]))
            bonus = 1.5 if sa and sa == sb else 0.0
            if not sh and not bonus:
                return 0.0
            na, nb = len(ga["refs"]), len(gb["refs"])
            return (sh + bonus) / math.sqrt(na * nb)

        rinc = defaultdict(set)
        for n, conns in M["nets"].items():
            if kinds[n] == "rail":
                for r, _p in conns:
                    if r in mof:
                        rinc[mof[r]].add(n)
        for m in groups:
            groups[m]["rails"] = set(rinc[m])

        def rail_score(a, b):
            sh = len(groups[a]["rails"] & groups[b]["rails"])
            return sh / math.sqrt(len(groups[a]["refs"]) * len(groups[b]["refs"])) if sh else 0.0

        def merge(a, b):
            ga, gb = groups[a], groups.pop(b)
            ga["refs"] += gb["refs"]
            ga["nets"] |= gb["nets"]
            ga["series"] += gb["series"]
            ga["rails"] |= gb["rails"]

        while True:
            best, pair = 0.0, None
            keys = sorted(groups, key=natkey)
            for i, a in enumerate(keys):
                for b in keys[i + 1:]:
                    if len(groups[a]["refs"]) + len(groups[b]["refs"]) > max_parts:
                        continue
                    s = score(a, b)
                    if s > best + 1e-12:
                        best, pair = s, (a, b)
            if not pair or best < 0.08:
                break
            a, b = pair
            if len(groups[b]["refs"]) > len(groups[a]["refs"]):
                a, b = b, a
            merge(a, b)
        # stragglers join their strongest neighbour (cap relaxed by half)
        for k in sorted(groups, key=lambda k: (len(groups[k]["refs"]), natkey(k))):
            if k not in groups or len(groups[k]["refs"]) >= min_parts:
                continue
            cand = [(score(k, o), o) for o in groups if o != k
                    and (len(groups[k]["refs"]) <= 3 or len(groups[o]["refs"]) + len(groups[k]["refs"]) <= max_parts * 1.5)]
            cand = [c for c in cand if c[0] > 0]
            if not cand:                    # no signal in common: the rail it shares most
                cand = [(rail_score(k, o), o) for o in groups if o != k
                        and (len(groups[k]["refs"]) <= 3 or len(groups[o]["refs"]) + len(groups[k]["refs"]) <= max_parts * 1.5)]
                cand = [c for c in cand if c[0] > 0]
            if cand:
                o = sorted(cand, key=lambda c: (-c[0], natkey(c[1])))[0][1]
                merge(o, k)
        for k in sorted(groups, key=natkey):
            rs = sorted(groups[k]["refs"], key=natkey)
            blocks.append({"name": None, "refs": rs, "how": "auto"})
    if mech:
        blocks.append({"name": "Mechanical", "refs": mech, "how": "mechanical"})
    used = set()
    for b in blocks:
        rs = b["refs"]
        b["anchor"] = sorted(rs, key=lambda r: (-len(comps[r]["pins"]) - len(comps[r]["nc"]), natkey(r)))[0]
        if not b["name"]:
            b["name"] = block_title(comps[b["anchor"]])
        base, i = b["name"], 2
        while b["name"].lower() in used:
            b["name"] = "%s %d" % (base, i)
            i += 1
        used.add(b["name"].lower())
        for r in rs:
            of[r] = b["name"]
    order = {b["name"]: i for i, b in enumerate(blocks)}
    blocks.sort(key=lambda b: (b["how"] == "mechanical", b["how"] != "constraints" and 1 or 0,
                               -len(b["refs"]) if b["how"] == "auto" else order[b["name"]]))
    return {"blocks": blocks, "of": of, "modules": nmod, "max_parts": max_parts}


def block_title(c):
    v = re.sub(r"[^A-Za-z0-9+._ -]", " ", c["value"]).split()
    v = [w for w in v if not re.match(r"^\d+(\.\d+)?$", w)][:2]
    return " ".join([c["ref"]] + v)[:32]


def slug(name):
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return s or "block"
