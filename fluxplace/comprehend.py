"""Circuit COMPREHENSION: auto-detect physics constraints from the netlist,
Quilter-style (docs/QUILTER-DOCS-DIGEST.md §5) — then exceed the published gaps.

Pure python on the same flat pad list lint uses, so it's testable without
pcbnew: [{ref, footprint, value, pad, net, pin?, drill?}]. `pin` is the
schematic pin function name when available (pcbnew PAD.GetPinFunction()).

Detected constraint classes (all overridable by the caller):
  power_nets     name/pattern detection; conservative default currents
                 (<3 V -> 200 mA, >=3 V -> 500 mA, Quilter's exact floors);
                 IPC-2221 trace width @ 20 degC rise for the assigned current.
  diff_pairs     suffix conventions +/-, P/N, P/M, A/B, t/c, DP/DM, D+/D-;
                 nets whose basename starts with 'V' never pair (VDDP/VDDN
                 guard); inline series R/C segments merged into one logical
                 pair.
  bypass_caps    cap bridging a parent's power net and ground. Pin assignment
                 priority: explicit pin-function match > voltage-named pin >
                 shared across same-named pins. Every cap carries its farads
                 and an ascending-capacitance rank per (parent, net): the
                 smallest cap belongs CLOSEST to the pin.
  crystals       refdes X/Y with both pins direct to one parent — plus the
                 topologies Quilter documents missing: series-R paths (one
                 2-pin R hop) and the load caps, folded into the cluster.
  converters     refdes U driving an inductor L: hot loop {U, L, Cin, Cout}.
                 Deterministic cap choice (largest-value bulk per side, ties
                 by refdes) instead of Quilter's "somewhat arbitrary" pick;
                 FB/sense nets flagged for keepaway.

Everything not matched stays a generic low-speed digital signal (Quilter's
default too). Output is JSON-serializable; `summarize()` prints the report.
"""
import re
from collections import defaultdict

from .lint import POWER_RE, GND_RE, _basename

__all__ = ["comprehend", "summarize", "parse_value", "ipc2221_width_mm",
           "rail_voltage", "pads_from_board"]


# ---------------------------------------------------------------- value parse
_VAL_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?|\.\d+)\s*(?P<mag>[pnumkMGµ]?)(?P<unit>[FfHhΩRK]?)")
_MAG = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
        "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}


def parse_value(text, unit="F"):
    """'100n' / '4.7uF' / '0.1u' / '10k' -> float in base units, or None.
    Also handles the R-as-decimal style ('4R7' -> 4.7) for resistors."""
    if not text:
        return None
    t = text.strip()
    if unit == "R":
        m = re.match(r"^(\d+)[Rr](\d+)$", t)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
    m = _VAL_RE.match(t)
    if not m:
        return None
    num = float(m.group("num"))
    mag = m.group("mag")
    # bare "10u"/"100n" style: magnitude implies the unit for C/L
    return num * _MAG.get(mag, 1.0)


# ------------------------------------------------------------------ power nets
_VOLT_RE = re.compile(
    r"(?:^|[^A-Z0-9])\+?(\d+)V(\d*)(?:[^A-Z0-9]|$)|(?:^|[^A-Z0-9])(\d+)\.(\d+)V", re.I)


def rail_voltage(net):
    """Best-effort volts from a rail name: 3V3->3.3, +5V->5, 1V8->1.8,
    28V_IN->28, P3V3->3.3. None when the name carries no number."""
    n = _basename(net)
    m = _VOLT_RE.search(n)
    if not m:
        return None
    if m.group(1) is not None:
        whole, frac = m.group(1), m.group(2)
        return float(f"{whole}.{frac}" if frac else whole)
    return float(f"{m.group(3)}.{m.group(4)}")


def ipc2221_width_mm(current_a, temp_rise_c=20.0, oz=1.0, internal=False):
    """IPC-2221 conductor width for a current. k=0.048 external, 0.024 internal;
    A[mil^2] = (I / (k * dT^0.44))^(1/0.725); width = A / (1.378 * oz)."""
    if current_a <= 0:
        return 0.0
    k = 0.024 if internal else 0.048
    area_mil2 = (current_a / (k * temp_rise_c ** 0.44)) ** (1 / 0.725)
    width_mil = area_mil2 / (1.378 * oz)
    return round(width_mil * 0.0254, 3)


def _power_nets(net_pads):
    """Quilter's floors: <3 V -> 200 mA, >=3 V (or unknown) -> 500 mA."""
    out = []
    for net in sorted(net_pads):
        base = _basename(net)
        if GND_RE.match(base):
            continue
        if not POWER_RE.match(base) and rail_voltage(net) is None:
            continue
        volts = rail_voltage(net)
        ma = 200 if (volts is not None and volts < 3.0) else 500
        out.append(dict(net=net, volts=volts, current_ma=ma, source="default",
                        width_mm=ipc2221_width_mm(ma / 1000.0)))
    return out


# ------------------------------------------------------------------ diff pairs
# (pattern on the P-side basename, replacement producing the N-side). Quilter's
# published conventions; longest/most specific first so USB_DP -> USB_DM wins
# over the bare P->N rule.
_PAIR_RULES = (
    (re.compile(r"\+$"), "-"),
    (re.compile(r"_DP$"), "_DM"), (re.compile(r"DP$"), "DM"),
    (re.compile(r"D\+$"), "D-"),
    (re.compile(r"_P$"), "_N"), (re.compile(r"_P(_[A-Za-z0-9]+)$"), r"_N\1"),
    (re.compile(r"P$"), "N"), (re.compile(r"P$"), "M"),
    (re.compile(r"A$"), "B"),
    (re.compile(r"_t$"), "_c"), (re.compile(r"_T$"), "_C"),
)
_TWO_PIN_PASSIVE = re.compile(r"^[RC]\d+$", re.I)


def _diff_pairs(net_pads, by_part):
    names = {n: _basename(n) for n in net_pads}
    by_base = defaultdict(list)
    for n, b in names.items():
        by_base[b].append(n)
    pairs = []
    used = set()
    for net in sorted(net_pads):
        base = names[net]
        if base.upper().startswith("V"):        # VDDP/VDDN guard (Quilter rule)
            continue
        if net in used:
            continue
        for pat, rep in _PAIR_RULES:
            if not pat.search(base):
                continue
            partner_base = pat.sub(rep, base)
            if partner_base == base or partner_base.upper().startswith("V"):
                continue
            partners = [m for m in by_base.get(partner_base, ())
                        if m != net and m not in used]
            if partners:
                partner = sorted(partners)[0]
                pairs.append(dict(p=net, n=partner, rule=pat.pattern))
                used.update((net, partner))
                break
    # merge inline series segments: a pair leg that dead-ends into a 2-pin R/C
    # whose far side is another leg of a DIFFERENT detected pair with a matching
    # name stem is the same logical pair (AC-coupling caps, series terminators).
    for pr in pairs:
        pr["segments"] = _series_extension(pr, net_pads, by_part)
    return pairs


def _series_extension(pair, net_pads, by_part):
    """Follow each leg through 2-pin R/C passives; return every net name that
    belongs to this logical pair (the P/N legs plus their far-side segments)."""
    segs = {pair["p"], pair["n"]}
    frontier = [pair["p"], pair["n"]]
    while frontier:
        net = frontier.pop()
        for p in net_pads.get(net, ()):
            if not _TWO_PIN_PASSIVE.match(p["ref"]):
                continue
            other = [q["net"] for q in by_part[p["ref"]]
                     if q["net"] and q["net"] != net]
            for o in other:
                if o not in segs and not GND_RE.match(_basename(o)) \
                        and not POWER_RE.match(_basename(o)):
                    segs.add(o)
                    frontier.append(o)
    return sorted(segs)


# ----------------------------------------------------------------- bypass caps
_VPIN_RE = re.compile(r"^(V|VDD|VCC|VIN|VBAT|AVDD|DVDD|PVDD|VDDA|VDDIO|"
                      r"\+?\d+V\d*)", re.I)


def _bypass_caps(pads, by_part, net_pads):
    """cap (refdes C*, exactly 2 distinct nets: one ground-family, one power
    net shared with a non-passive parent) -> constraint rows with capacitance
    rank per (parent, net): ascending farads, smallest belongs closest."""
    rows = []
    for ref in sorted(by_part):
        if not re.match(r"^C\d+$", ref, re.I):
            continue
        nets = sorted({p["net"] for p in by_part[ref] if p["net"]})
        if len(nets) != 2:
            continue
        gnd = [n for n in nets if GND_RE.match(_basename(n))]
        rail = [n for n in nets if n not in gnd]
        if len(gnd) != 1 or len(rail) != 1:
            continue
        rail = rail[0]
        base = _basename(rail)
        if not (POWER_RE.match(base) or rail_voltage(rail) is not None):
            continue
        # parents: non-passive parts on the SAME rail that also touch ground
        parents = []
        for q in net_pads.get(rail, ()):
            pr = q["ref"]
            if pr == ref or re.match(r"^[RCLDY]\d+$|^X\d+$|^FB\d+$", pr, re.I):
                continue
            if any(GND_RE.match(_basename(z["net"])) for z in by_part[pr]
                   if z["net"]):
                parents.append((pr, q))
        if not parents:
            continue
        # pin assignment priority (Quilter's order): explicit voltage-style pin
        # FUNCTION name first, then any pad on the rail; same-named pins share.
        best = {}
        for pr, q in parents:
            score = 1 if _VPIN_RE.match((q.get("pin") or "")) else 0
            cur = best.get(pr)
            if cur is None or score > cur[0]:
                best[pr] = (score, q.get("pin") or q.get("pad") or "")
        farads = parse_value(by_part[ref][0].get("value", ""), "F")
        for pr in sorted(best):
            rows.append(dict(cap=ref, parent=pr, net=rail,
                             pin=best[pr][1], farads=farads))
    # ascending-capacitance rank per (parent, net); unknown values rank last
    groups = defaultdict(list)
    for r in rows:
        groups[(r["parent"], r["net"])].append(r)
    for g in groups.values():
        g.sort(key=lambda r: (r["farads"] is None, r["farads"] or 0.0, r["cap"]))
        for i, r in enumerate(g):
            r["rank"] = i          # 0 = smallest = closest to the pin
    return rows


# -------------------------------------------------------------------- crystals
def _crystals(by_part, net_pads):
    """X*/Y* refdes. Direct: both crystal nets land on one parent. Series-R:
    a leg reaches the parent through exactly one 2-pin R. Load caps: caps from
    crystal nets to ground join the cluster. (Quilter documents missing the
    series-R and load-cap cases — we take them.)"""
    out = []
    for ref in sorted(by_part):
        if not re.match(r"^[XY]\d+$", ref, re.I):
            continue
        xnets = sorted({p["net"] for p in by_part[ref]
                        if p["net"] and not GND_RE.match(_basename(p["net"]))})
        if not (1 <= len(xnets) <= 2):
            continue
        parent_hits = defaultdict(list)     # parent -> [(net_at_parent, via)]
        series = []
        for net in xnets:
            for q in net_pads.get(net, ()):
                pr = q["ref"]
                if pr == ref:
                    continue
                if re.match(r"^R\d+$", pr, re.I) and len(
                        {z["net"] for z in by_part[pr] if z["net"]}) == 2:
                    far = [z["net"] for z in by_part[pr]
                           if z["net"] and z["net"] != net][0]
                    for q2 in net_pads.get(far, ()):
                        if q2["ref"] not in (ref, pr) and not re.match(
                                r"^[RCLDY]\d+$", q2["ref"], re.I):
                            parent_hits[q2["ref"]].append((far, pr))
                            series.append(pr)
                elif not re.match(r"^[RCLDY]\d+$|^X\d+$", pr, re.I):
                    parent_hits[pr].append((net, None))
        if not parent_hits:
            continue
        parent = max(sorted(parent_hits), key=lambda k: len(parent_hits[k]))
        load = sorted({q["ref"] for net in xnets for q in net_pads.get(net, ())
                       if re.match(r"^C\d+$", q["ref"], re.I)
                       and any(GND_RE.match(_basename(z["net"]))
                               for z in by_part[q["ref"]] if z["net"])})
        out.append(dict(crystal=ref, parent=parent, nets=xnets,
                        series_r=sorted(set(series)), load_caps=load))
    return out


# ------------------------------------------------------------------ converters
_FB_RE = re.compile(r"(^|_)(FB|VFB|SNS|SENSE|COMP)($|_)", re.I)


def _converters(by_part, net_pads):
    """U* whose pad net also lands on a 2-pin L*: switching converter.
    Hot loop = {U, L, Cin, Cout}; caps chosen DETERMINISTICALLY: the
    largest-farad bulk cap per side (ties by refdes). FB nets flagged."""
    out = []
    for ref in sorted(by_part):
        if not re.match(r"^U\d+$", ref, re.I):
            continue
        unets = {p["net"] for p in by_part[ref] if p["net"]}
        for lref in sorted({q["ref"] for n in unets
                            for q in net_pads.get(n, ())
                            if re.match(r"^L\d+$", q["ref"], re.I)}):
            lnets = sorted({p["net"] for p in by_part[lref] if p["net"]})
            if len(lnets) != 2:
                continue
            sw = [n for n in lnets if n in unets]
            if not sw:
                continue
            sw = sw[0]
            vout = [n for n in lnets if n != sw][0]
            # input rail: a power-family net on U that isn't SW/VOUT
            vins = sorted(n for n in unets
                          if n not in (sw, vout)
                          and (POWER_RE.match(_basename(n))
                               or rail_voltage(n) is not None)
                          and not GND_RE.match(_basename(n)))
            cin = _bulk_cap(vins[0], by_part, net_pads) if vins else None
            cout = _bulk_cap(vout, by_part, net_pads)
            fb = sorted(n for n in unets if _FB_RE.search(_basename(n)))
            out.append(dict(u=ref, l=lref, sw=sw, vin=vins[0] if vins else None,
                            vout=vout, cin=cin, cout=cout, fb_nets=fb,
                            hot_loop=[r for r in (ref, lref, cin, cout) if r]))
    return out


def _bulk_cap(net, by_part, net_pads):
    """Largest-farad grounded cap on `net` (deterministic: ties by refdes)."""
    if not net:
        return None
    cands = []
    for q in net_pads.get(net, ()):
        r = q["ref"]
        if not re.match(r"^C\d+$", r, re.I):
            continue
        if not any(GND_RE.match(_basename(z["net"])) for z in by_part[r]
                   if z["net"]):
            continue
        cands.append((-(parse_value(by_part[r][0].get("value", ""), "F") or 0.0), r))
    return min(cands)[1] if cands else None


# ------------------------------------------------------------------- top level
def comprehend(pads):
    """Flat pad list -> constraint dict (JSON-serializable)."""
    by_part = defaultdict(list)
    net_pads = defaultdict(list)
    for p in pads:
        by_part[p["ref"]].append(p)
        if p.get("net"):
            net_pads[p["net"]].append(p)
    return dict(
        power_nets=_power_nets(net_pads),
        diff_pairs=_diff_pairs(net_pads, by_part),
        bypass_caps=_bypass_caps(pads, by_part, net_pads),
        crystals=_crystals(by_part, net_pads),
        converters=_converters(by_part, net_pads),
    )


def pads_from_board(board):
    """pcbnew BOARD -> flat pad list with pin functions and positions (a
    superset of lint's adapter; extra keys are harmless there)."""
    out = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        fpname = str(fp.GetFPID().GetLibItemName())
        val = fp.GetValue()
        for pad in fp.Pads():
            try:
                pin = pad.GetPinFunction() or ""
            except Exception:
                pin = ""
            pp = pad.GetPosition()
            out.append(dict(
                ref=ref, footprint=fpname, value=val,
                pad=pad.GetNumber(), pin=pin, net=pad.GetNetname() or "",
                drill=pad.GetDrillSize().x > 0,
                x=pp.x / 1e6, y=pp.y / 1e6))
    return out


def summarize(c, log=print):
    log(f"power nets: {len(c['power_nets'])}  "
        f"diff pairs: {len(c['diff_pairs'])}  "
        f"bypass caps: {len(c['bypass_caps'])}  "
        f"crystals: {len(c['crystals'])}  "
        f"converters: {len(c['converters'])}")
    for p in c["power_nets"]:
        v = f"{p['volts']}V" if p["volts"] is not None else "?V"
        log(f"  PWR  {p['net']}: {v} {p['current_ma']}mA -> {p['width_mm']}mm")
    for d in c["diff_pairs"]:
        seg = f" (+{len(d['segments']) - 2} series segs)" \
            if len(d.get("segments", ())) > 2 else ""
        log(f"  PAIR {d['p']} / {d['n']}{seg}")
    for b in c["bypass_caps"]:
        f = "?" if b["farads"] is None else f"{b['farads']:.2e}F"
        log(f"  BYP  {b['cap']} -> {b['parent']}.{b['pin'] or '?'} "
            f"({b['net']}, {f}, rank {b['rank']})")
    for x in c["crystals"]:
        extra = (f" series {','.join(x['series_r'])}" if x["series_r"] else "") + \
                (f" load {','.join(x['load_caps'])}" if x["load_caps"] else "")
        log(f"  XTAL {x['crystal']} -> {x['parent']}{extra}")
    for u in c["converters"]:
        log(f"  CONV {u['u']}: SW={u['sw']} L={u['l']} "
            f"Cin={u['cin']} Cout={u['cout']} loop={','.join(u['hot_loop'])}")
    return c
