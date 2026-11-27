"""Datasheet-driven electrical checks on the SPEC netlist (no board needed, so they run in the parts-first stage, before placement).

Per-part facts live in the datasheet manifest (`<datasheets>/datasheets.json`), under the part's existing entry:
    {"TPS2051CDBVR": {"file": ..., "sha256": ..., "facts": {"vin_min": {"value": 4.5, "source": "TPS2051CDBVR.pdf#p5"}, ...}}}
Add one with `fluxplace parts-fact MPN vin_min=4.5 vin_max=5.5 supply_pins='["IN"]' --source TPS2051CDBVR.pdf#p5`.
Every fact carries the page it came from; a fact with no source is ignored (reported as such), never trusted.

Fact keys
  vin_min, vin_max   V   recommended supply range of the pins in `supply_pins` (list of pin names or numbers); a number, or {pin: V} when pins differ
  no_supply          bool  true: the part has no supply pin (TVS array, passive divider); the datasheet page says what it is
  supply_pins        []  which pins the range / `i_max_ma` apply to
  i_max_ma           mA  maximum supply current (a number for the first supply pin, or {pin: mA})
  i_out_max_a        A   rated output current of a regulator / switch (the source limit)
  decap_mm bulk_mm   mm  datasheet limit, pad of the bypass / bulk capacitor to the supply pin it serves (number, or {pin: mm} as i_max_ma;
                         powerbudget.supports: a datasheet figure replaces the derived and default limits; REQ, never printed as a default)
  contact_a          A   per-contact current rating of a connector or socket
  v_rated            V   voltage rating of a capacitor / device where the value text has none
  pin_rules          []  [{"pin": "ILIM", "rule": "needs_resistor"|"must_float", "allow_net_of": "IN", "level": "WARN",
                           "note": "..."}]  a configuration pin that must not sit directly on a rail / ground

Constraint keys read (all optional; a check without its inputs reports "not evaluated", never a silent pass)
  [power."NET"]  nominal_v min_v max_v surge_v   rail voltage (SUPPLY_RANGE, CAP_VOLTAGE_MARGIN)
                 source = "U102"  source_a = 2.0  load_extra_ma = 120   (SOURCE_OVERLOAD; source_a overrides the part's i_out_max_a)
                 input_eff = 0.93   regulator efficiency, with the rail's current and nominal_v this is its input power (FUSE_SIZING)
  [fuse]         rating_a v_min also_v = [6.0] continuous_pct = 0.75 rerate = 1.0 (rerate: fraction of rating usable at [env] temp_max_c) extra_w
  [elec]         cap_margin = 1.25  source_warn_pct = 10
  [nets.NAME]    role = "signal"    declares a net a signal, not a power rail (power.unknown_rails skips it)

SUPPLY_RANGE_UNKNOWN (WARN): an IC (U*/Y*) with a pin on a declared [power] rail and no vin_min/vin_max fact; add one with parts-fact.
Codes: SUPPLY_RANGE SUPPLY_RANGE_UNKNOWN SOURCE_OVERLOAD CONTACT_CURRENT FUSE_SIZING PIN_STRAP CAP_VOLTAGE_MARGIN (FAIL/WARN), ELEC_NOT_EVALUATED (INFO).
Nothing here names a board, a reference or an MPN.
"""
import json
import math
import os
import re

CODES = ("SUPPLY_RANGE", "SOURCE_OVERLOAD", "CONTACT_CURRENT", "FUSE_SIZING", "PIN_STRAP", "CAP_VOLTAGE_MARGIN", "SUPPLY_RANGE_UNKNOWN", "ELEC_NOT_EVALUATED")
FACT_KEYS = ("no_supply", "vin_min", "vin_max", "supply_pins", "i_max_ma", "i_out_max_a", "contact_a", "v_rated", "pin_rules", "decap_mm", "bulk_mm", "layout")
_GND = re.compile(r"^(GND\w*|AGND|DGND|PGND|VSS\w*|EARTH|0V)$", re.I)
_POWER = re.compile(r"^(\+?\d+V\d*\w*|V(CC|DD|BAT|BATT|IN|BUS)\w*)$", re.I)
_VRATE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*V(?![\w])", re.I)


def _f(level, code, msg, refs=()):
    return {"level": level, "code": code, "msg": msg, "refs": sorted(set(refs))}


# ------------------------------------------------------------------ the fact store (inside datasheets.json)
def load_facts(ds_dir):
    """{mpn: {key: value}} - only facts that cite a source."""
    from . import partdocs as PD
    out = {}
    for mpn, ent in (PD.load_manifest(ds_dir) if ds_dir else {}).items():
        fx = (ent or {}).get("facts") if isinstance(ent, dict) else None
        if not fx:
            continue
        ok = {k: v["value"] for k, v in fx.items() if isinstance(v, dict) and v.get("source") and "value" in v}
        if ok:
            ok["_src"] = {k: v["source"] for k, v in fx.items() if isinstance(v, dict) and v.get("source") and "value" in v}
            out[mpn] = ok
    return out


def _coerce(s):
    try:
        return json.loads(s)
    except ValueError:
        return s


_NOT_DIGITS = ("page", "derived", "derivation")      # keys of a dict fact that are bookkeeping, not datasheet figures


def _numbers(key, v):
    """[(name, number)] a fact claims the cited page states: a scalar, or the numbers of a dict fact (nested too). A dict fact's
    `derived` (list of its keys, or true for all) marks values the page does not state (D71do): they are not digit-checked."""
    if isinstance(v, bool):
        return []
    if isinstance(v, (int, float)):
        return [(key, v)]
    if not isinstance(v, dict):
        return []
    d = v.get("derived")
    skip = set(_NOT_DIGITS) | (set(v) if d is True else {str(x) for x in d} if isinstance(d, (list, tuple)) else set())
    out = []
    for k2, v2 in v.items():
        if k2 not in skip:
            out += _numbers(f"{key}.{k2}", v2)
    return out


def set_facts(ds_dir, mpn, kv, source, check_page=True, force=False):
    """Write facts (key -> value) for `mpn` with a `file.pdf#pN` citation. -> (written, warnings). Raises ValueError on a bad cite.
    A numeric value whose digits are not on the cited page is refused unless force (the page is the evidence)."""
    from . import partdocs as PD
    f, pages = PD.parse_source(source)
    if not f or not pages:
        raise ValueError("source must name a file and a page: file.pdf#p7")
    path = f if os.path.isabs(f) else os.path.join(ds_dir, f)
    if not os.path.isfile(path):
        raise ValueError(f"cited file not found: {path}")
    man = PD.load_manifest(ds_dir)
    if mpn not in man:
        raise ValueError(f"{mpn}: not in {PD.MANIFEST} (fetch/adopt its datasheet first)")
    warn = []
    text = PD.page_text(path, pages) if check_page else None
    for k, v in kv.items():
        if k not in FACT_KEYS:
            raise ValueError(f"unknown fact '{k}' (known: {', '.join(FACT_KEYS)})")
        if text is not None:
            for name, n in _numbers(k, v):
                if not re.search(r"(?<![\d.])" + re.escape(f"{n:g}") + r"(?![\d])", text):
                    msg = f"{mpn} {name}={n:g}: that number is not on {f} page(s) {pages}"
                    if not force:
                        raise ValueError(msg + " (use --force if the page states it another way; a value the page does not state is `derived`)")
                    warn.append(msg)
        man[mpn].setdefault("facts", {})[k] = {"value": v, "source": source}
    tmp = PD.manifest_path(ds_dir) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(man, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, PD.manifest_path(ds_dir))
    return sorted(kv), warn


# ------------------------------------------------------------------ spec helpers
def _nets(spec):
    n = (spec or {}).get("nets") or {}
    if isinstance(n, dict):
        return {k: [tuple(p) for p in v] for k, v in n.items()}
    return {x[0] if not isinstance(x, dict) else x.get("name"): [tuple(p) for p in (x[1] if not isinstance(x, dict) else x.get("pins", []))] for x in n}


def _names(comp, pin):
    """The pin's names: as written, its pinmap pad, and every pinmap name that maps to the same pad."""
    pm = (comp or {}).get("pinmap") or {}
    pin = str(pin)
    pad = pm.get(pin, pin)
    pad = str(pad[0] if isinstance(pad, (list, tuple)) and pad else pad)
    return {pin, pad} | {k for k, v in pm.items() if str(v) == pad}


def _pin_net(comp, nets_by_ref, pinname):
    for n, pins in nets_by_ref.get(comp["ref"], {}).items():
        for p in pins:
            if str(pinname) in _names(comp, p):
                return n
    return None


def _index(spec):
    nets = _nets(spec)
    by_ref = {}
    for n, pins in nets.items():
        for r, p in pins:
            by_ref.setdefault(r, {}).setdefault(n, []).append(p)
    return nets, by_ref


def _mpn(c, mpn_map):
    return c.get("mpn") or (mpn_map or {}).get(c.get("ref"))


def _rail(cons, net):
    p = ((cons or {}).get("power") or {}).get(net)
    return p if isinstance(p, dict) else {}


def _vrange(p):
    lo = p.get("min_v", p.get("nominal_v"))
    hi = p.get("max_v", p.get("nominal_v"))
    return (float(lo) if lo is not None else None, float(hi) if hi is not None else None)


def _is_gnd(n):
    return bool(n and _GND.match(n.split("/")[-1]))


def _short(refs, n=10):
    refs = sorted(refs)
    return ", ".join(refs[:n]) + (f" (+{len(refs) - n})" if len(refs) > n else "")


# ------------------------------------------------------------------ the checks
def _lim(v, pin):
    """A limit given as one number for all supply pins, or {pin: number}."""
    if isinstance(v, dict):
        v = v.get(pin)
    return float(v) if v is not None else None


def check_supply_range(spec, cons, facts, mpn_map, nets, by_ref, noeval):
    out = []
    unknown = []
    for c in spec.get("components", []):
        mpn = _mpn(c, mpn_map)
        fx = facts.get(mpn) or {}
        if fx.get("no_supply"):
            continue                    # a passive / protection part with no supply pin, stated by its datasheet: nothing to range-check
        if "vin_min" not in fx and "vin_max" not in fx:
            if c["ref"][:1] in ("U", "Y"):
                on_rail = [n for n in by_ref.get(c["ref"], {}) if not _is_gnd(n) and any(v is not None for v in _vrange(_rail(cons, n)))]
                if on_rail:
                    unknown.append(f"{c['ref']} {mpn or c.get('value', '')} ({', '.join(sorted(on_rail)[:3])})")
                else:
                    noeval.setdefault("SUPPLY_RANGE", {}).setdefault("no supply-range fact", []).append(f"{c['ref']} {mpn or c.get('value', '')}")
            continue
        for sp in fx.get("supply_pins") or []:
            net = _pin_net(c, by_ref, sp)
            if net is None:
                continue
            p = _rail(cons, net)
            lo, hi = _vrange(p)
            if lo is None and hi is None:
                noeval.setdefault("SUPPLY_RANGE", {}).setdefault("rail voltage not declared in [power.*]", []).append(f"{c['ref']}.{sp} on {net}")
                continue
            vmin, vmax = _lim(fx.get("vin_min"), sp), _lim(fx.get("vin_max"), sp)
            bad = []
            if lo is not None and vmin is not None and lo < vmin - 1e-9:
                bad.append(f"rail goes down to {lo:g} V, part needs >= {vmin:g} V")
            if hi is not None and vmax is not None and hi > vmax + 1e-9:
                bad.append(f"rail goes up to {hi:g} V, part allows <= {vmax:g} V")
            if bad:
                srcs = (facts.get(mpn) or {}).get("_src", {})
                src = srcs.get("vin_min") or srcs.get("vin_max") or ""
                out.append(_f("FAIL", "SUPPLY_RANGE", f"{c['ref']} ({mpn}) pin {sp} on {net} (rail {_fmt(lo)}..{_fmt(hi)} V, part {_fmt(vmin)}..{_fmt(vmax)} V"
                              + (f", {src}" if src else "") + "): " + "; ".join(bad), refs=[c["ref"]]))
    if unknown:
        out.append(_f("WARN", "SUPPLY_RANGE_UNKNOWN", f"{len(unknown)} IC(s) on a declared rail have no datasheet supply-range fact "
                      f"(parts-fact MPN vin_min= vin_max= supply_pins=): {_short(unknown)}", refs=[u.split(" ")[0] for u in unknown]))
    return out


def _fmt(v):
    return "?" if v is None else f"{v:g}"


def per_pin(fx, key):
    """{pin: value} of a per-pin fact: a number applies to the first supply pin, a dict is already per pin ({} when absent)."""
    v = fx.get(key)
    if v is None:
        return {}
    sps = fx.get("supply_pins") or []
    return dict(v) if isinstance(v, dict) else ({sps[0]: v} if sps else {})


def _loads(spec, facts, mpn_map, by_ref):
    """{net: [(ref, mA)]} from i_max_ma facts."""
    out = {}
    for c in spec.get("components", []):
        fx = facts.get(_mpn(c, mpn_map)) or {}
        for pin, ma in per_pin(fx, "i_max_ma").items():
            net = _pin_net(c, by_ref, pin)
            if net:
                out.setdefault(net, []).append((c["ref"], float(ma)))
    return out


def check_source_overload(spec, cons, facts, mpn_map, nets, by_ref, noeval):
    out = []
    comps = {c["ref"]: c for c in spec.get("components", [])}
    loads = _loads(spec, facts, mpn_map, by_ref)
    warn_pct = float(((cons or {}).get("elec") or {}).get("source_warn_pct", 10))
    for net, p in sorted(((cons or {}).get("power") or {}).items()):
        if not isinstance(p, dict) or not (p.get("source") or p.get("source_a")):
            continue
        src = p.get("source")
        sa = p.get("source_a")
        if sa is None and src in comps:
            sa = (facts.get(_mpn(comps[src], mpn_map)) or {}).get("i_out_max_a")
        if sa is None:
            noeval.setdefault("SOURCE_OVERLOAD", {}).setdefault("source rating unknown", []).append(net)
            continue
        ls = loads.get(net, [])
        tot = sum(m for _r, m in ls) + float(p.get("load_extra_ma", 0) or 0)
        lim = float(sa) * 1000.0
        if tot > lim:
            lvl = "FAIL"
        elif tot > lim * (1 - warn_pct / 100.0):
            lvl = "WARN"
        else:
            continue
        top = ", ".join(f"{r} {m:g}" for r, m in sorted(ls, key=lambda x: -x[1])[:5])
        out.append(_f(lvl, "SOURCE_OVERLOAD", f"{net}: loads sum {tot:g} mA ({len(ls)} part(s) from facts"
                      + (f" + {float(p['load_extra_ma']):g} declared" if p.get("load_extra_ma") else "") + f": {top}) vs source "
                      f"{src or ''} {lim:g} mA ({'over' if lvl == 'FAIL' else 'within ' + format(warn_pct, 'g') + ' % of'} its limit)",
                      refs=[x for x in [src] if x] + [r for r, _m in ls[:6]]))
    return out


def current_a(p):
    from . import power as PW
    return PW.current_a(p)


def check_contact_current(spec, cons, facts, mpn_map, nets, by_ref, noeval):
    out = []
    for c in spec.get("components", []):
        mpn = _mpn(c, mpn_map)
        ca = (facts.get(mpn) or {}).get("contact_a")
        if ca is None:
            continue
        for net, pins in by_ref.get(c["ref"], {}).items():
            p = _rail(cons, net)
            need = current_a(p) if p else None
            if not need:
                continue
            n = len(pins)
            cap = n * float(ca) * float(p.get("contact_derate", 1.0))
            if cap < need - 1e-9:
                out.append(_f("FAIL", "CONTACT_CURRENT", f"{c['ref']} ({mpn}) carries {net}: {n} contact(s) x {float(ca):g} A = {cap:g} A "
                              f"< {need:g} A the rail's max_current/load", refs=[c["ref"]]))
    return out


def check_fuse(spec, cons, facts, mpn_map, nets, by_ref, noeval):
    fu = (cons or {}).get("fuse")
    if not isinstance(fu, dict) or not fu.get("rating_a"):
        return []
    pw = 0.0
    parts = []
    for net, p in ((cons or {}).get("power") or {}).items():
        if isinstance(p, dict) and p.get("input_eff") and current_a(p) and p.get("nominal_v"):
            w = current_a(p) * float(p["nominal_v"]) / float(p["input_eff"])
            pw += w
            parts.append(f"{net} {current_a(p):g} A x {float(p['nominal_v']):g} V / {float(p['input_eff']):g} = {w:.1f} W")
    pw += float(fu.get("extra_w", 0) or 0)
    if not parts:
        noeval.setdefault("FUSE_SIZING", {}).setdefault("no rail has input_eff", []).append("[fuse]")
        return []
    env = (cons or {}).get("env") or {}
    rating = float(fu["rating_a"]) * float(fu.get("rerate", 1.0))
    cont = float(fu.get("continuous_pct", 0.75))
    out = []
    vs = [(float(fu["v_min"]), True)] if fu.get("v_min") else []
    vs += [(float(v), False) for v in fu.get("also_v", [])]
    if not vs:
        noeval.setdefault("FUSE_SIZING", {}).setdefault("no [fuse] v_min", []).append("[fuse]")
    for v, hard in vs:
        i = pw / v
        if i > rating:
            lvl = "FAIL" if hard else "WARN"
        elif i > rating * cont:
            lvl = "WARN"
        else:
            continue
        out.append(_f(lvl, "FUSE_SIZING", f"input {pw:.1f} W = {i:.2f} A at {v:g} V ({'stated minimum' if hard else 'sensitivity, no stated minimum'}) vs "
                      f"{float(fu['rating_a']):g} A fuse x rerate {float(fu.get('rerate', 1.0)):g} (at {env.get('temp_max_c', '?')} C) = {rating:g} A "
                      f"usable, {cont * 100:g} % continuous = {rating * cont:.2f} A [{'; '.join(parts)}]"))
    return out


def check_pin_strap(spec, cons, facts, mpn_map, nets, by_ref, noeval):
    out = []
    for c in spec.get("components", []):
        fx = facts.get(_mpn(c, mpn_map)) or {}
        for r in fx.get("pin_rules") or []:
            pin = r.get("pin")
            net = _pin_net(c, by_ref, pin)
            if net is None:
                continue
            direct = _is_gnd(net) or bool(_rail(cons, net)) or bool(_POWER.match(net))
            lvl = r.get("level", "WARN")
            if r.get("rule") == "must_float":
                if len(nets.get(net, [])) > 1:
                    out.append(_f(lvl, "PIN_STRAP", f"{c['ref']}.{pin} on {net}: datasheet says leave it open. {r.get('note', '')}".strip(), refs=[c["ref"]]))
                continue
            if r.get("rule") == "needs_resistor" and direct:
                ok_net = None
                if r.get("allow_net_of"):
                    ok_net = _pin_net(c, by_ref, r["allow_net_of"])
                if ok_net and net == ok_net:
                    out.append(_f("INFO", "PIN_STRAP", f"{c['ref']}.{pin} tied to {net} (same net as {r['allow_net_of']}): documented special "
                                  f"mode. {r.get('note', '')}".strip(), refs=[c["ref"]]))
                else:
                    out.append(_f(lvl, "PIN_STRAP", f"{c['ref']}.{pin} sits directly on {net}; the datasheet wants a resistor here. "
                                  f"{r.get('note', '')}".strip(), refs=[c["ref"]]))
    return out


def _cap_rating(c, facts, mpn_map):
    fx = facts.get(_mpn(c, mpn_map)) or {}
    if fx.get("v_rated") is not None:
        return float(fx["v_rated"]), "fact"
    m = _VRATE.search(str(c.get("value", "")))
    return (float(m.group(1)), "value text") if m else (None, None)


def check_cap_margin(spec, cons, facts, mpn_map, nets, by_ref, noeval):
    out = []
    margin = float(((cons or {}).get("elec") or {}).get("cap_margin", 1.25))
    for c in spec.get("components", []):
        if not re.match(r"^C\d", c["ref"]):
            continue
        rv, src = _cap_rating(c, facts, mpn_map)
        for net in by_ref.get(c["ref"], {}):
            if _is_gnd(net):
                continue
            p = _rail(cons, net)
            _lo, hi = _vrange(p)
            if hi is None:
                continue
            if rv is None:
                noeval.setdefault("CAP_VOLTAGE_MARGIN", {}).setdefault("capacitor has no voltage rating", []).append(c["ref"])
                continue
            surge = p.get("surge_v")
            if rv < hi or (surge and rv < float(surge)):
                out.append(_f("FAIL", "CAP_VOLTAGE_MARGIN", f"{c['ref']} {c.get('value', '')} rated {rv:g} V ({src}) on {net} "
                              f"{'max ' + format(hi, 'g') + ' V' if rv < hi else 'surge ' + format(float(surge), 'g') + ' V'}", refs=[c["ref"]]))
            elif rv < hi * margin:
                out.append(_f("WARN", "CAP_VOLTAGE_MARGIN", f"{c['ref']} {c.get('value', '')} rated {rv:g} V ({src}) on {net} max {hi:g} V: "
                              f"{rv / hi:.2f}x, want >= {margin:g}x", refs=[c["ref"]]))
    return out


def check_spec(spec, cons, ds_dir=None, mpn_map=None, facts=None):
    """All checks on the spec netlist -> findings (level, code, msg, refs). Needs no board."""
    if not spec or not spec.get("components"):
        return []
    facts = facts if facts is not None else load_facts(ds_dir)
    nets, by_ref = _index(spec)
    noeval = {}
    out = []
    for fn in (check_supply_range, check_source_overload, check_contact_current, check_fuse, check_pin_strap, check_cap_margin):
        out += fn(spec, cons or {}, facts, mpn_map or {}, nets, by_ref, noeval)
    for code, why in sorted(noeval.items()):
        for reason, items in sorted(why.items()):
            out.append(_f("INFO", "ELEC_NOT_EVALUATED", f"{code} not evaluated, {reason}: {_short(set(items))}",
                          refs=[i.split(".")[0].split(" ")[0] for i in items if re.match(r"^[A-Z]+\d", i)][:20]))
    return out
