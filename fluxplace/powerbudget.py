"""The one power budget (PLAN-POWER-PREPLAN section 1; D71dc/D71dd). Nothing here is computed twice: widths and via
counts come from power.rail, resistance and the drop verdict from channel (dc_resistance_mohm, dcdrop_row), copper thickness
from the stackup rows, support ownership from clusters.ownership.

  build(cons, B=None, spec=None, rows=None, facts=None, mpn_map=None) -> {"rails": {net: Rail}, "supports": [], "findings": [],
                                                                         "hot_c", "crowding", "detour"}

Rail: net, current_a, sizing (power.rail), drop {target_mv, max_mv, source, klass} | None, r_allow_mohm {target, max},
source / loads {spec, pads, how}, copper [{layer, kind, width_ipc_mm, mohm_sq, vias_end, r_vias_mohm, reach_mm {target, max}}],
choice {kind, layers, width_mm, why}, edges [{load, dist_mm, len_mm, width_req_mm, est}] (edges need a board; [] without).

Reach (L_max) is the drop equation solved for length: L_max = (R_allow - R_vias) x W / (Rsq(T_hot, t_layer) x DETOUR x CROWDING).
Rsq comes from the stackup copper of THAT layer at the hot COPPER temperature: ambient ([env] temp_max_c, default 85) plus the
trace rise the rail's IPC width was sized for (power.rail rise_c), so the same edge on 0.5 oz copper has half the reach of 1 oz.
A [[dcdrop]] temp_c states the copper temperature itself and wins. DETOUR (channel, 1.15) is Manhattan -> routed; CROWDING (1.25, INF until a routed
board measures it; [route] power_crowding overrides) is current crowding at the pads. Vias: a layer change at each end
costs `vias_needed` parallel barrels (current / rated amps per via, power.rail).
A rail whose series drop is the design (a sense resistor, a bias inductor, a return net) carries
`[power."NET"] dcdrop_exempt = "reason"`: no budget, no reach, listed as INFO POWER_PLAN_EXEMPT (the reason is required).
Nothing here names a board, a reference or an MPN.
"""
import math

from . import channel as CH
from . import power as PW

CROWDING = 1.25                 # INF: current crowding at the pads; replaced by the measured ratio after the first routed board (P8)
HOT_C = 85.0                    # INF: used when [env] temp_max_c is absent (the product's hot corner, D71c)
FLOOR_MM = 0.127                # never plan below this (ready.min_feature_mm default)
TIERS = ("target", "max")
BULK_UF = 4.7                   # clusters.BULK_UF: above this a capacitor is bulk storage, not per-pin bypass
BYPASS_MAX_MM = 5.0             # the pre-P3 gate limit of every support part (clusters.LIMIT_DEFAULT): FAIL stays where it was
BULK_MM = (10.0, 15.0)          # INF, unverified: path inductance at pour width (0.3-0.5 nH/mm) below the ESL of a bulk part (2-5 nH)
SENSE_MM = 5.0                  # feedback divider / sense resistor / any other support part (pre-P3 default)


# ------------------------------------------------------------------ the quantities
def hot_c(cons):
    return float((((cons or {}).get("env") or {}).get("temp_max_c")) or HOT_C)


def crowding(cons):
    return float((((cons or {}).get("route") or {}).get("power_crowding")) or CROWDING)


def exempt_reason(cons, net):
    """The stated reason a rail has no [[dcdrop]] budget by design (`[power."NET"] dcdrop_exempt = "reason"`), else None."""
    p = ((cons or {}).get("power") or {}).get(net)
    v = p.get("dcdrop_exempt") if isinstance(p, dict) else None
    return v.strip() if isinstance(v, str) and v.strip() else None


def floor_mm(cons):
    c = cons or {}
    fab = float((c.get("fab") or {}).get("min_track_mm", (c.get("route") or {}).get("min_track_mm", 0.0)))
    return max(fab, float((c.get("ready") or {}).get("min_feature_mm", FLOOR_MM)))


def sheet_mohm_sq(rows, layer, temp_c=20.0):
    """Sheet resistance of `layer` in mohm per square at temp_c: rho(T) / t."""
    rho = CH.RHO_CU_20C * (1 + CH.ALPHA_CU * (temp_c - 20.0))
    return rho / (CH.copper_t_mm(rows, layer) * 1e-3) * 1e3


def synth_path(layer, len_mm, width_mm, vias=()):
    """A planned path in the shape a measured one has (channel.measure_src_load): found, layer_sq, via_list."""
    return {"found": True, "layer_sq": {layer: len_mm / width_mm}, "via_list": [dict(v) for v in vias], "planned": True}


def _copper(rows):
    return [r["name"] for r in rows if r.get("type") == "copper" and r.get("name")]


def _layer_vias(rail, rows, layer):
    """Via barrels one end of the path needs to reach `layer` from the outer pad layer: `vias_needed` parallel barrels in one entry."""
    cu = _copper(rows)
    if not cu or layer in (cu[0], cu[-1]):
        return []
    return [{"top": cu[0], "bottom": layer, "drill": rail.get("via_drill_mm") or 0.2, "n": int(rail.get("vias_needed") or 1)}]


def _r_vias(rail, rows, layer):
    """Both ends' via resistance (mohm) for a path on `layer`."""
    v = _layer_vias(rail, rows, layer) * 2
    return CH.dc_resistance_mohm({"layer_sq": {}, "via_list": v}, rows, rail["temp_c"]) if v else 0.0


def _entry(rail, amps=None):
    """The [[dcdrop]] entry a planned path is judged against (same keys the review reads)."""
    d = dict(rail.get("d") or {"net": rail["net"], "source": "planned"})
    d.update(net=rail["net"], current_a=float(amps if amps is not None else rail["current_a"] or 0.0), temp_c=rail["temp_c"])
    return d


def estimate(rail, a_xy, b_xy, layer, width_mm, rows, amps=None):
    """A planned edge's drop row (channel.dcdrop_row on a synthetic path): Manhattan x DETOUR x CROWDING squares on `layer`."""
    dist = abs(a_xy[0] - b_xy[0]) + abs(a_xy[1] - b_xy[1])
    sq_len = dist * rail["detour"] * rail["crowding"]
    p = synth_path(layer, sq_len, width_mm, _layer_vias(rail, rows, layer) * 2)
    return CH.dcdrop_row(_entry(rail, amps), p, rows)


def reach_mm(rail, layer, width_mm, tier, rows, amps=None):
    """Longest Manhattan distance the rail's budget (`tier`: target | max) allows on `layer` at `width_mm`: the inverse of estimate
    (None without a budget; 0.0 when the via barrels alone exceed it)."""
    d = rail.get("d") or {}
    mv = d.get("drop_mv_" + tier)
    a = float(amps if amps is not None else rail["current_a"] or 0.0)
    if mv is None or not a:
        return None
    room = float(mv) / a - _r_vias(rail, rows, layer)
    if room <= 0:
        return 0.0
    return room * width_mm / sheet_mohm_sq(rows, layer, rail["temp_c"]) / (rail["detour"] * rail["crowding"])


def width_req_mm(rail, dist_mm, layer, tier, rows, amps=None):
    """Narrowest width that carries `dist_mm` (Manhattan) inside `tier`: max(IPC width on the layer, drop width, fab floor).
    None when the tier has no budget; inf when the vias alone exceed it."""
    d = rail.get("d") or {}
    mv = d.get("drop_mv_" + tier)
    a = float(amps if amps is not None else rail["current_a"] or 0.0)
    ipc = _ipc_width(rail, rows, layer)
    if mv is None or not a:
        return None
    room = float(mv) / a - _r_vias(rail, rows, layer)
    if room <= 0:
        return math.inf
    drop_w = sheet_mohm_sq(rows, layer, rail["temp_c"]) * rail["detour"] * rail["crowding"] * dist_mm / room
    return max(ipc, drop_w, rail["floor_mm"])


def _ipc_width(rail, rows, layer):
    s = rail["sizing"]
    cu = _copper(rows)
    outer = layer in (cu[:1] + cu[-1:])
    w = s.get("width_outer" if outer else "width_inner")
    return max(float(w if w is not None else (s.get("track_mm") or 0.0)), rail["floor_mm"])      # never plan below the floor


# ------------------------------------------------------------------ source and loads
def _pads_of(B):
    from . import netclass as NC
    return NC.net_pads(B)


def _group_pads(pads, spec):
    spec = str(spec)
    ref, _, num = spec.partition(".")
    return [p for p in pads if p["ref"] == ref and (not num or str(p["num"]) == num)]


def _fact(facts, mpn_map, spec, ref, key):
    from . import elec as EL
    comp = next((c for c in (spec or {}).get("components", []) or [] if isinstance(c, dict) and c.get("ref") == ref), {"ref": ref})
    return ((facts or {}).get(EL._mpn(comp, mpn_map)) or {}).get(key)


def resolve_ends(B, cons, net, spec=None, facts=None, mpn_map=None):
    """-> (source, loads): source {spec, pads, how} | None, loads [{spec, pads, how}].
    Order: [[dcdrop]] from/to, `[power.NET] source`, a part with an i_out_max_a fact on the net. A source part with no pad on
    the net (a regulator behind its inductor) resolves to the net pad of the two-pad part whose other pad's net carries a pin
    of the source. Loads without `to`: every part on the net with more than two pads except the source."""
    pads = _pads_of(B).get(net, [])
    parts = B["parts"]
    c = cons or {}
    d = next((x for x in c.get("dcdrop", []) or [] if x.get("net") == net and x.get("from")), None) or \
        next((x for x in c.get("dcdrop", []) or [] if x.get("net") == net), None)
    src, how = None, None
    if d and d.get("from"):
        src, how = str(d["from"]), "dcdrop.from"
    elif isinstance(((c.get("power") or {}).get(net)), dict) and c["power"][net].get("source"):
        src, how = str(c["power"][net]["source"]), "[power].source"
    else:
        for ref in sorted({p["ref"] for p in pads}):
            if _fact(facts, mpn_map, spec, ref, "i_out_max_a") is not None:
                src, how = ref, "fact i_out_max_a"
                break
    source = None
    if src is not None:
        g = _group_pads(pads, src)
        if not g and how != "dcdrop.from":
            link = _series_link(B, net, src.split(".")[0], pads)
            if link:
                src, g, how = link[0], link[1], how + " via series part"
        source = {"spec": src, "pads": [(p["ref"], p["num"]) for p in g], "how": how}
    loads = []
    if d and d.get("to"):
        to = [d["to"]] if isinstance(d["to"], str) else list(d["to"])
        loads = [{"spec": str(t), "pads": [(p["ref"], p["num"]) for p in _group_pads(pads, t)], "how": "dcdrop.to"} for t in to]
    else:
        sref = (source or {}).get("spec", "").split(".")[0]
        for ref in sorted({p["ref"] for p in pads}):
            if ref != sref and parts.get(ref, {}).get("n_pads", 0) > 2:
                loads.append({"spec": ref, "pads": [(p["ref"], p["num"]) for p in pads if p["ref"] == ref], "how": "inferred"})
    return source, loads


def _series_link(B, net, sref, pads):
    parts = B["parts"]
    if sref not in parts:
        return None
    from . import netclass as NC
    sref_nets = {q["net"] for q in parts[sref]["pads"] if q["net"] and not NC.is_ground(q["net"])}
    for ref in sorted({p["ref"] for p in pads}):
        pt = parts.get(ref)
        if not pt or ref == sref or pt.get("n_pads") != 2:
            continue
        mine = [q for q in pt["pads"] if q["net"] == net]
        other = [q for q in pt["pads"] if q["net"] != net]
        if mine and other and other[0]["net"] in sref_nets:
            return f"{ref}.{mine[0]['num']}", [{"ref": ref, "num": mine[0]["num"], "x": mine[0]["x"], "y": mine[0]["y"]}]
    return None


# ------------------------------------------------------------------ the budget
def _drop(d):
    if not d:
        return None
    return {"target_mv": d.get("drop_mv_target"), "max_mv": d.get("drop_mv_max"), "source": d.get("source", "NO SOURCE"),
            "klass": "REQ" if d.get("source") and "INF" not in str(d.get("source")) else "INF"}


def _pxy(B, refnum):
    p = next((q for q in B["parts"][refnum[0]]["pads"] if q["num"] == refnum[1]), None)
    return (p["x"], p["y"]) if p else None


def _ends(B, source, loads):
    """[(load, source xy, worst load pad xy, Manhattan mm)]: nearest source pad to the load's farthest pad."""
    sxy = [_pxy(B, q) for q in (source or {}).get("pads", [])]
    out = []
    for ld in loads:
        lxy = [_pxy(B, q) for q in ld["pads"]]
        if not sxy or not lxy:
            continue
        worst = max(lxy, key=lambda b: min(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a in sxy))
        a = min(sxy, key=lambda a: abs(a[0] - worst[0]) + abs(a[1] - worst[1]))
        out.append((ld, a, worst, abs(a[0] - worst[0]) + abs(a[1] - worst[1])))
    return out


def _edges(rail, ends, rows):
    """One row per load: Manhattan distance and the estimated drop on the choice."""
    ch = rail["choice"]
    out = []
    for ld, a, worst, dist in ends:
        amps = (rail["d"] or {}).get("to_a", {}).get(ld["spec"]) if isinstance((rail["d"] or {}).get("to_a"), dict) else None
        lay, w = ch["layers"][0], ch["width_mm"]
        out.append({"load": ld["spec"], "dist_mm": round(dist, 2), "len_mm": round(dist * rail["detour"], 2),
                    "amps": float(amps) if amps is not None else rail["current_a"], "amps_how": "dcdrop.to_a" if amps is not None else "full rail current",
                    "width_req_mm": {t: _r(width_req_mm(rail, dist, lay, t, rows, amps)) for t in TIERS},
                    "reach_mm": {t: _r(reach_mm(rail, lay, w, t, rows, amps)) for t in TIERS},
                    "est": estimate(rail, a, worst, lay, w, rows, amps)})
    return out


def _r(x):
    return None if x is None or x == math.inf else round(x, 2)


def _choose(rail, ends):
    """Outer copper first (D71cd: no power plane): the first outer layer whose max reach holds the longest edge; else the layer
    with the longest reach; with no budget or no placement, the first outer layer at its IPC width."""
    cs, d = rail["copper"], rail["drop"]
    if not cs:
        return {"kind": "none", "layers": [], "width_mm": 0.0, "why": "no routable copper layer"}
    kind = "corridor" if rail["sizing"].get("pour") else "track"
    far = max([e[3] for e in ends], default=0.0)
    reach = lambda c: c["reach_mm"]["max"] or 0.0
    ok = [c for c in cs if d and reach(c) >= far]
    if ok:
        c = sorted(ok, key=lambda c: c["kind"] != "outer")[0]
        why = "outer copper at its IPC width" if not far else ("first outer layer holding the longest edge" if c["kind"] == "outer" else
                                                               "no outer layer holds the longest edge at its IPC width: first inner layer that does")
    elif d:
        c, why = max(cs, key=reach), "no layer holds the longest edge: longest reach at the max budget"
    else:
        c, why = sorted(cs, key=lambda c: c["kind"] != "outer")[0], "no [[dcdrop]] row: outer copper at its IPC width"
    return {"kind": kind, "layers": [c["layer"]], "width_mm": c["width_ipc_mm"], "why": why}


def build(cons, B=None, spec=None, rows=None, facts=None, mpn_map=None, board_path=None, planes=None):
    """-> {"rails": {net: Rail}, "supports": [], "findings": [...], ...}. `rows` = stackup rows (channel.stack_rows)."""
    cons = cons or {}
    if rows is None:
        rows, _planes, _src = CH.stack_rows(cons, board_path)
    sizing = PW.plan(cons, board=board_path)
    planes = set(planes if planes is not None else ((cons.get("stackup") or {}).get("planes")) or ())
    hot, crowd, flo = hot_c(cons), crowding(cons), floor_mm(cons)
    out = {"rails": {}, "supports": [], "findings": [], "hot_c": hot, "crowding": crowd, "detour": CH.DETOUR}
    dc_by = {}
    for d in cons.get("dcdrop", []) or []:
        dc_by.setdefault(d["net"], d)
    cu = _copper(rows)
    for net, s in sorted(sizing.items()):
        if not s or not s.get("current_a"):
            continue
        d = dc_by.get(net)
        rise = float(s.get("rise_c") or 0.0)
        rail = {"net": net, "current_a": s["current_a"], "sizing": s, "d": d, "drop": _drop(d), "rise_c": rise,
                "temp_c": float((d or {}).get("temp_c", hot + rise)), "exempt": None if d else exempt_reason(cons, net),
                "detour": CH.DETOUR, "crowding": crowd, "floor_mm": flo, "via_drill_mm": s.get("via_drill_mm"), "vias_needed": s.get("vias_needed")}
        amps = float(s["current_a"])
        rail["r_allow_mohm"] = {t: (float(d["drop_mv_" + t]) / amps if d and d.get("drop_mv_" + t) is not None else None) for t in TIERS}
        rail["copper"] = []
        for lyr in cu:
            if lyr in planes:
                continue
            outer = lyr in (cu[0], cu[-1])
            w = _ipc_width(rail, rows, lyr)
            rv = _r_vias(rail, rows, lyr)
            rail["copper"].append({"layer": lyr, "kind": "outer" if outer else "inner", "width_ipc_mm": round(w, 3),
                                   "mohm_sq": round(sheet_mohm_sq(rows, lyr, rail["temp_c"]), 4),
                                   "vias_end": 0 if outer else int(s.get("vias_needed") or 1), "r_vias_mohm": round(rv, 3),
                                   "reach_mm": {t: _r(reach_mm(rail, lyr, w, t, rows)) for t in TIERS}})
        ends = []
        if B is not None:
            rail["source"], rail["loads"] = resolve_ends(B, cons, net, spec, facts, mpn_map)
            ends = _ends(B, rail["source"], rail["loads"])
        else:
            rail["source"], rail["loads"] = None, []
        rail["choice"] = _choose(rail, ends)
        rail["edges"] = _edges(rail, ends, rows) if ends and rail["choice"]["layers"] else []
        out["rails"][net] = rail
        out["findings"] += _findings(rail, B is not None)
    if B is not None:
        out["supports"] = supports(B, cons, spec, facts, mpn_map)
        from . import clusters as CL
        out["pin_demand"] = CL.pin_demand(B, cons, spec, facts=facts, mpn_map=mpn_map)
    return out


def _findings(rail, have_board):
    n, f = rail["net"], []
    if rail["drop"] is None and rail.get("exempt"):
        return [{"code": "POWER_PLAN_EXEMPT", "level": "INFO", "net": n,
                 "msg": f"{n}: {rail['current_a']:g} A, no drop budget by design ([power] dcdrop_exempt: {rail['exempt']})"}]
    if rail["drop"] is None:
        f.append({"code": "POWER_PLAN_NO_BUDGET", "level": "FAIL", "net": n,
                  "msg": f"{n}: {rail['current_a']:g} A in [power] and no [[dcdrop]] row (D71dd); add one with drop_mv_max/target, from, to and a cited source, or state why the rail has none: [power.\"{n}\"] dcdrop_exempt = \"reason\""})
    if have_board and (rail["source"] is None or not rail["source"]["pads"]):
        f.append({"code": "POWER_SOURCE_UNKNOWN", "level": "OPEN", "net": n,
                  "msg": f"{n}: no source resolved (set [[dcdrop]] from or [power.\"{n}\"] source)" if rail["source"] is None else
                         f"{n}: source {rail['source']['spec']} has no pad on the net"})
    for ld in rail["loads"]:
        if not ld["pads"]:
            f.append({"code": "POWER_SOURCE_UNKNOWN", "level": "OPEN", "net": n, "msg": f"{n}: load {ld['spec']} has no pad on the net"})
    if rail["drop"] and rail["choice"]["layers"]:
        lay = rail["choice"]["layers"][0]
        c = next(x for x in rail["copper"] if x["layer"] == lay)
        if c["reach_mm"]["max"] == 0.0:
            f.append({"code": "POWER_COPPER_IMPOSSIBLE", "level": "FAIL", "net": n,
                      "msg": f"{n}: the via barrels alone ({c['r_vias_mohm']:g} mohm) exceed the allowed {rail['r_allow_mohm']['max']:g} mohm"})
    return f


# ------------------------------------------------------------------ the one support table
_FACTS = {}


def facts_for(cons, spec=None, anchors=()):
    """({mpn: facts}, mpn_map) from the datasheet dir of [docs] datasheets (cited facts only); ({}, {}) when there is none."""
    import os
    from . import elec as EL, parts as P, partdocs as PD
    ds = P.resolve_dir((((cons or {}).get("docs") or {}).get("datasheets")), anchors)
    facts = {}
    try:
        if ds and os.path.isdir(ds):
            mp = PD.manifest_path(ds)
            key = (os.path.abspath(mp), os.stat(mp).st_mtime_ns if os.path.exists(mp) else None)
            if key not in _FACTS:                # one read per manifest version: clusters.build runs inside placer loops
                _FACTS.clear()
                _FACTS[key] = EL.load_facts(ds)
            facts = _FACTS[key]
    except (OSError, ValueError):
        facts = {}
    return facts, {c["ref"]: c["mpn"] for c in (spec or {}).get("components", []) if isinstance(c, dict) and c.get("mpn")}


def _planned(cons, net):
    p = ((cons or {}).get("power") or {}).get(net)
    return isinstance(p, dict) and PW.current_a(p) is not None


def role_of(part, ref, net, info, cons):
    """Role of a support part: loop (switch node), clock (crystal), bulk (> BULK_UF to ground on a planned rail), bypass (capacitor
    to ground), sense (everything else: divider, sense resistor, series capacitor)."""
    from . import clusters as CL
    if info and info["class"] == "switch_node" and info.get("budget"):
        return "loop"
    if info and info["class"] == "clock" and info.get("reason") == "crystal/oscillator":
        return "clock"
    from . import netclass as NC
    if ref.startswith("C") and any(q["net"] != net and NC.is_ground(q["net"] or "") for q in part["pads"]):     # rail / pin to ground
        uf = CL.cap_uf(part.get("value"))
        return "bulk" if (uf or 0) > BULK_UF and _planned(cons, net) else "bypass"
    return "sense"


def _fact_mm(fx, key, names):
    v = (fx or {}).get(key)
    if isinstance(v, dict):
        v = next((v[n] for n in names if n in v), None)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def support_row(B, cons, spec, ref, net, info, ic=None, pad=None, facts=None, mpn_map=None):
    """The limit of one support part: {role, target, max, unit, source, klass}. Source order: datasheet fact (decap_mm for
    bypass, bulk_mm for bulk, REQ) -> derived (netclass budget and its basis, INF) -> default (INF). Target warns, max fails."""
    from . import netclass as NC
    role = role_of(B["parts"][ref], ref, net, info, cons)
    if role in ("loop", "clock"):
        b = float(info["budget"])
        row = {"target": b, "max": b, "source": f"derived: netclass {info['class']} budget ({NC.BASIS.get(info['class'], 'class budget')})", "klass": "INF"}
    elif role == "bypass" and info and info["class"] == "bypass" and info.get("budget"):
        b = float(info["budget"])                # a pin's own capacitor (IC pin to cap to ground): the class budget is the limit, as before P3
        row = {"target": b, "max": b, "source": f"derived: netclass bypass budget ({NC.BASIS['bypass']})", "klass": "INF"}
    elif role == "bypass":
        row = {"target": NC.BUDGET["bypass"], "max": BYPASS_MAX_MM, "klass": "INF",
               "source": f"target derived: netclass bypass ({NC.BASIS['bypass']}); max default {BYPASS_MAX_MM:g} mm (the pre-budget gate limit)"}
    elif role == "bulk":
        row = {"target": BULK_MM[0], "max": BULK_MM[1], "klass": "INF",
               "source": f"default {BULK_MM[0]:g}/{BULK_MM[1]:g} mm, unverified: path inductance at pour width below the ESL of a bulk part (a datasheet bulk_mm replaces it)"}
    elif info and info["class"] == "rf" and info.get("budget"):
        b = float(info["budget"])                # a series part on an RF net (DC block, matching): the RF length budget, not the 5 mm default
        row = {"target": b, "max": b, "source": f"derived: netclass rf budget ({NC.BASIS['rf']})", "klass": "INF"}
    else:
        row = {"target": SENSE_MM, "max": SENSE_MM, "klass": "INF", "source": f"default {SENSE_MM:g} mm (sense / feedback / other support part)"}
    key = {"bypass": "decap_mm", "bulk": "bulk_mm"}.get(role)
    if key and ic and facts is None:
        facts, mpn_map = facts_for(cons, spec)
    if key and ic and facts:
        from . import elec as EL
        comp = next((c for c in (spec or {}).get("components", []) or [] if isinstance(c, dict) and c.get("ref") == ic), {"ref": ic})
        mpn = EL._mpn(comp, mpn_map)
        names = {str(pad)} | set(EL._names(comp, pad)) if pad is not None else set()
        mm = _fact_mm((facts or {}).get(mpn), key, names)
        if mm is not None:
            src = ((facts.get(mpn) or {}).get("_src") or {}).get(key, "datasheet")
            row = {"target": mm, "max": mm, "klass": "REQ", "source": f"datasheet fact {key} of {mpn} ({src})"}
    row.update(role=role, unit="mm")
    return row


def supports(B, cons, spec=None, facts=None, mpn_map=None, cl=None):
    """[Support]: one row per support part of every IC (clusters.build), graded with channel.verdict.
    {ref, role, serves "IC.PAD", net, limit {target, max, unit, source, klass}, dist_mm, verdict, how, sure}. The single table:
    clusters.far_parts, preroute.support_far and relocate read the same rows."""
    from . import clusters as CL
    if cl is None:
        cl, _ = CL.build(B, cons, spec, facts=facts, mpn_map=mpn_map)
    out = []
    for c in cl:
        for s in c["supports"]:
            lim = {"target": s["target"], "max": s["limit"], "unit": "mm", "source": s["source"], "klass": s["klass"]}
            v = CH.verdict(s["dist"], lim["target"], lim["max"])[0] if s["sure"] else "n/a"
            out.append({"ref": s["ref"], "role": s["role"], "serves": f"{c['ic']}.{s['pad']}", "net": s["net"], "limit": lim,
                        "dist_mm": round(s["dist"], 2), "verdict": v, "how": s["how"], "sure": s["sure"]})
    return sorted(out, key=lambda r: (r["ref"]))


# ------------------------------------------------------------------ the report (ready)
def summary(plan):
    """One line for the ready checklist: rails, shortest reach, edges over the max budget, supports over their limit."""
    rails = plan["rails"]
    reach = [e["reach_mm"]["max"] for r in rails.values() for e in r["edges"] if e["reach_mm"]["max"] is not None]
    over = [(n, e["load"]) for n, r in rails.items() for e in r["edges"]
            if e["est"].get("verdict") == "fail" or (e["reach_mm"]["max"] is not None and e["dist_mm"] > e["reach_mm"]["max"])]
    sup = [x for x in plan.get("supports", []) if x["verdict"] == "fail"]
    ex = sorted(n for n, r in rails.items() if r.get("exempt"))
    return (f"{len(rails) - len(ex)} rail(s) planned at {plan['hot_c']:g} C ambient + trace rise (crowding {plan['crowding']:g}); "
            + (f"{len(ex)} exempt by design ({', '.join(ex)}); " if ex else "")
            + (f"L_max {min(reach):g}..{max(reach):g} mm; " if reach else "")
            + f"{len(over)} edge(s) over the max budget, {len(sup)} support(s) over their limit"
            + (f", {len(plan['pin_demand'])} IC rail pin(s) without their own bypass" if plan.get("pin_demand") else ""))


def plan_json(plan):
    """JSON-safe copy of the budget for power-plan.json (tuples become lists)."""
    import json
    return json.loads(json.dumps(plan, default=list))
