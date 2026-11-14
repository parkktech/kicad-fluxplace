"""Design-completeness LINT: catch missing wiring and connector-choice smells
before placement/routing effort is spent on a board that isn't finished.

Checks (v1):
  no-power-entry        no connector carries a power-family net
  no-io-connector       board has no connectors at all
  unwired-connector     a connector whose pads carry no nets
  dead-end-net          net with exactly one pad (a wire to nowhere)
  no-gnd-on-part        part (>=4 pads) powered but with no ground pin
  no-net-pads           parts with unconnected pads (info: may be NC by design)
  barrel-jack           barrel/DC jack found — prefer a LATCHING connector
                        (JST VH/SM, Molex Micro-Fit/Mini-Fit, screw terminal):
                        barrel plugs walk out under vibration
  power-on-friction-header
                        power on a bare 0.1" pin header — same latching advice

Control-pin default levels (`control_pins(spec)`, `lint --spec`), from the spec netlist:
  control-pin-default   info: an enable / reset / gate / chip-select / power-down input has a pull: the
                        resulting level, the rail, active-low notes, its drivers
  control-pin-floating  warning: the only drivers can be high-impedance at power-up (GPIO-expander / SoC
                        GPIO outputs, off-board connector pins) and nothing pulls the line
  control-pin-intent    warning: the pull gives a level other than `[lint.default_level] NET = "high"|"low"`
  control-pin-dnp       warning: the line's only pull is DNP
  open-drain-no-pullup  warning: an open-drain / open-collector line (pin type or name) with no pull-up
  input-undriven        warning: an input with no driver and no pull at all
Pin roles come from pin names (the spec `pinmap`, or the pin id the net uses), optional datasheet pin types
(spec component `pin_types = {pin: KiCad electrical type}`) and the symbols of a schematic (`--sch`); the name
patterns are regexes overridable in `[lint.control_pins]` (control, gpio, open_drain, output, active_low).
Nothing here names a part, a reference or a net.

Pure python: operates on a flat pad list so it is testable without pcbnew.
`pads_from_board(board)` adapts a pcbnew BOARD. Every finding is a dict
{severity, code, msg, refs}; severities: error > warning > info.
"""
import re

__all__ = ["run", "pads_from_board", "POWER_RE", "GND_RE"]

POWER_RE = re.compile(
    r"^(\+?\d+(\.\d+)?V\d*(_\w+)?|VCC\w*|VDD\w*|VIN\w*|VBUS\w*|VBAT\w*|"
    r"PWR\w*|V_?SYS\w*|P\d+V\d*(_\w+)?|\w*_\d+V\d*)$", re.I)
GND_RE = re.compile(r"^(GND\w*|AGND|DGND|PGND|VSS\w*|EARTH|0V)$", re.I)


def _basename(net):
    """KiCad hierarchical nets ('/POWER_ENTRY/FAN_12V') -> leaf name."""
    return net.rsplit("/", 1)[-1]


def _is_power(net):
    return bool(POWER_RE.match(_basename(net)))

CONN_REF_RE = re.compile(r"^(J|CN|X|P)\d+", re.I)
CONN_FP_RE = re.compile(
    r"conn|header|jack|socket|terminal|usb|xlr|sma|u\.fl|jst|molex|phoenix|"
    r"df\d+|receptacle|plug", re.I)
BARREL_RE = re.compile(r"barrel|dc.?jack|jack.?dc|\bpj-?\d|5\.5x2\.[15]", re.I)
FRICTION_HDR_RE = re.compile(r"pinheader|pin_header|pinsocket|pin_socket", re.I)
RF_FRICTION_RE = re.compile(r"u\.?fl|umcc|\bmhf\b|ipex|i-pex", re.I)
MECH_RE = re.compile(r"mount|fiducial|logo|hole|testpoint|tp_|solderjumper", re.I)


def pads_from_board(board):
    """pcbnew BOARD -> flat pad list for run()."""
    out = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        fpname = str(fp.GetFPID().GetLibItemName())
        val = fp.GetValue()
        for pad in fp.Pads():
            out.append(dict(
                ref=ref, footprint=fpname, value=val,
                pad=pad.GetNumber(), net=pad.GetNetname() or "",
                drill=pad.GetDrillSize().x > 0))
    return out


def _is_connector(ref, fpname, value):
    if MECH_RE.search(fpname):
        return False
    return bool(CONN_REF_RE.match(ref)) and bool(
        CONN_FP_RE.search(fpname + " " + value))


def run(pads, waivers=None):
    """pads: [{ref, footprint, value, pad, net, drill}] -> findings list.

    waivers: ["code:regex", ...] — drop findings whose code matches and whose
    msg or any ref matches the regex (for de-scoped features, spare footprints,
    split-board interconnect stubs)."""
    findings = []
    by_part = {}
    net_pads = {}
    for p in pads:
        by_part.setdefault(p["ref"], []).append(p)
        if p["net"]:
            net_pads.setdefault(p["net"], []).append(p)

    connectors = {r for r, ps in by_part.items()
                  if _is_connector(r, ps[0]["footprint"], ps[0]["value"])}

    def add(sev, code, msg, refs):
        findings.append(dict(severity=sev, code=code, msg=msg,
                             refs=sorted(refs)))

    # --- power entry / io presence ---------------------------------------
    if not connectors:
        add("warning", "no-io-connector",
            "no connectors found — how do power and signals get on/off "
            "this board?", [])
    else:
        entry = {r for r in connectors
                 if any(_is_power(p["net"]) for p in by_part[r])}
        if not entry:
            add("warning", "no-power-entry",
                "no connector carries a power-family net (VCC/VIN/+NV...) — "
                "missing power input wiring?", sorted(connectors))

    # --- unwired connectors ----------------------------------------------
    for r in sorted(connectors):
        ps = [p for p in by_part[r] if p["pad"]]
        if len(ps) >= 2 and not any(p["net"] for p in ps):
            add("error", "unwired-connector",
                f"{r} ({by_part[r][0]['footprint']}) has no nets on any pad",
                [r])

    # --- dead-end nets ----------------------------------------------------
    dead = [n for n, ps in net_pads.items()
            if len(ps) == 1 and not n.lower().startswith("unconnected")]
    for n in sorted(dead):
        p = net_pads[n][0]
        add("warning", "dead-end-net",
            f"net '{n}' reaches only {p['ref']} pad {p['pad']} — "
            "a wire to nowhere", [p["ref"]])

    # --- powered part without ground -------------------------------------
    for r, ps in sorted(by_part.items()):
        numbered = [p for p in ps if p["pad"]]
        if len(numbered) < 4 or r in connectors:
            continue
        if MECH_RE.search(ps[0]["footprint"]):
            continue
        has_pwr = any(_is_power(p["net"]) for p in numbered)
        has_gnd = any(GND_RE.match(_basename(p["net"])) for p in numbered)
        if has_pwr and not has_gnd:
            add("warning", "no-gnd-on-part",
                f"{r} ({ps[0]['value']}) has power but no ground pin — "
                "missing return path?", [r])

    # --- unconnected pads (informational: NC pins are legitimate) --------
    loose = []
    for r, ps in sorted(by_part.items()):
        numbered = [p for p in ps if p["pad"]]
        if len(numbered) < 2 or MECH_RE.search(ps[0]["footprint"]):
            continue
        n_open = sum(1 for p in numbered if not p["net"])
        if n_open:
            loose.append((r, n_open, len(numbered)))
    if loose:
        add("info", "no-net-pads",
            "unconnected pads (verify each is NC by design): " +
            ", ".join(f"{r} {k}/{n}" for r, k, n in loose),
            [r for r, _, _ in loose])

    # --- connector style: latch beats friction ---------------------------
    for r, ps in sorted(by_part.items()):
        blob = ps[0]["footprint"] + " " + ps[0]["value"]
        if BARREL_RE.search(blob):
            add("warning", "barrel-jack",
                f"{r} is a barrel/DC jack — prefer a LATCHING connector "
                "(JST VH/SM, Molex Micro-Fit/Mini-Fit, screw terminal); "
                "barrel plugs walk out under vibration", [r])
    for r in sorted(connectors):
        ps = by_part[r]
        blob = ps[0]["footprint"]
        if FRICTION_HDR_RE.search(blob) and any(
                _is_power(p["net"]) for p in ps):
            add("warning", "power-on-friction-header",
                f"{r} feeds power through a friction-fit pin header — "
                "use a latching connector for anything that must survive "
                "vibration", [r])

    # --- RF friction coax: U.FL/MHF snap-fits walk off under vibration ---
    for r, ps in sorted(by_part.items()):
        blob = (ps[0]["footprint"] + " " + ps[0]["value"])
        if RF_FRICTION_RE.search(blob) and not re.search(r"lk|lock", blob, re.I):
            add("warning", "rf-friction-coax",
                f"{r} is a snap-fit micro-coax (U.FL/MHF class, ~30 cycles, "
                "a few N retention) — for vibration environments use a "
                "locking variant (I-PEX MHF LK), a threaded connector "
                "(SMA/MMCX), or adhesive-stake the mated pair with strain "
                "relief", [r])

    if waivers:
        rules = []
        for w in waivers:
            code, _, pat = w.partition(":")
            rules.append((code, re.compile(pat or ".")))
        findings = [f for f in findings if not any(
            f["code"] == c and (rx.search(f["msg"]) or
                                any(rx.search(r) for r in f["refs"]))
            for c, rx in rules)]
    return findings


def summarize(findings, log=print):
    order = {"error": 0, "warning": 1, "info": 2}
    n = {"error": 0, "warning": 0, "info": 0}
    for f in sorted(findings, key=lambda f: (order[f["severity"]], f["code"])):
        n[f["severity"]] += 1
        log(f"  {f['severity'].upper():7s} {f['code']}: {f['msg']}")
    log(f"lint: {n['error']} errors, {n['warning']} warnings, "
        f"{n['info']} info")
    return n


# ---------------------------------------------------------------- control-pin default levels (spec netlist)
CONTROL_PINS = {
    # enable / reset / gate / chip-select / power-down style INPUTS whose level at power-up matters
    "control": r"^[nNxX/]?_?(EN|ENA|ENABLE|EN\d+|[A-Z0-9]+_EN|EN_[A-Z0-9_]+|SHDN|SHUTDOWN|PD|PDN|PWDN|STBY|STANDBY|SLEEP|"
               r"RST|RESET|[A-Z0-9]+_RST|RST_[A-Z0-9]+|[A-Z0-9]*RESET[A-Z0-9_]*|MR|CS\d*|[A-Z0-9]+_CS|CE|G|GATE|OE|ON|ON_OFF|"
               r"PWRKEY|PWR_ON|POWER_ON|INH|INHIBIT|DISABLE|DIS)(_?N|_?B|#|_L)?$",
    # outputs that are high-impedance until firmware configures them
    "gpio": r"^(GPIO|GP|GPIO_|PIO|P[A-K]|P)_?\d+(_\d+)?$",
    # open-drain / open-collector outputs (need a pull-up somewhere)
    "open_drain": r"^[nN]?_?(PGOOD|PG|PGD|PWRGD|PWR_GOOD|POK|PWROK|INT|IRQ|ALERT|ALRT|FAULT|FLT|OCS|OC|SDA|SCL|SMBDAT|SMBCLK|"
                  r"READY|RDY|STAT|CHG)(\d*)(_?N|_?B|#)?$",
    # push-pull outputs
    "output": r"^(OUT|OUTPUT|Y\d*|Q\d*|DOUT|SDO|MISO|TXD?|VOUT_LOGIC)$",
    "active_low": r"(^[nN/][A-Z]|(_N|_B|#|_L)$)",
}
RAIL_RE = (r"^(\+?\d+(\.\d+)?V\d*(_\w+)?|[+-]?V(CC|DD|EE|IN|BAT|BATT|BUS|SYS|MAIN|OUT|REF|IO|CORE|MODEM)\w*|"
           r"P\d+V\d*\w*|\w*_\d+V\d*|[AD]VDD\w*|VDDIO\w*)$")
_IN_TYPES = {"input"}
_WEAK_TYPES = {"bidirectional", "tri_state"}
_STRONG_TYPES = {"output", "power_out"}
_OD_TYPES = {"open_collector", "open_emitter"}
_RVAL = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([rRkKmM]?)(\d*)\s*(?:ohm|Ω)?", re.I)


def _ohms(v):
    m = _RVAL.match(v or "")
    if not m:
        return None
    a, u, b = m.groups()
    mult = {"": 1, "r": 1, "k": 1e3, "m": 1e6}[u.lower()]
    return float(a + ("." + b if b else "")) * mult


def _is_resistor(c, n_pins):
    t = f"{c.get('lib', '')} {c.get('fp', '')}"
    return n_pins == 2 and (re.search(r"resistor|(^|[^A-Z])R_\d", t, re.I) is not None or
                            (re.match(r"^R\d", c["ref"]) is not None and _ohms(c.get("value")) is not None))


def sch_pin_types(sch_path):
    """{ref: {pin number: (name, electrical type)}} from a .kicad_sch's embedded lib_symbols (pins of every unit)."""
    from . import boardfile as BF
    root = BF.parse(open(sch_path, errors="replace").read())
    libs = {}
    for ls in BF._kids(root, "lib_symbols"):
        for sym in BF._kids(ls, "symbol"):
            pins = {}

            def walk(n):
                for k in n:
                    if isinstance(k, list) and k and k[0] == "pin":
                        a = BF._atoms(k)
                        num = (BF._atoms(BF._first(k, "number")) or [""])[0]
                        name = (BF._atoms(BF._first(k, "name")) or [""])[0]
                        if num:
                            pins[num] = (name, a[0] if a else "")
                    elif isinstance(k, list) and k and k[0] == "symbol":
                        walk(k)
            walk(sym)
            libs[(BF._atoms(sym) or [""])[0]] = pins
    out = {}
    for sym in BF._kids(root, "symbol"):
        lid = (BF._atoms(BF._first(sym, "lib_id")) or [""])[0]
        ref = next((BF._atoms(p)[1] for p in BF._kids(sym, "property") if BF._atoms(p)[:1] == ["Reference"]), None)
        if ref and lid in libs:
            out[ref] = libs[lid]
    return out


def control_pins(spec, cons=None, sch_types=None, waivers=None):
    """Control-pin default-level lint over the spec netlist (see module doc). -> [finding] (lint format)."""
    cons = cons or {}
    lc = (cons.get("lint") or {})
    pat = dict(CONTROL_PINS)
    pat.update({k: v for k, v in (lc.get("control_pins") or {}).items() if k in CONTROL_PINS and isinstance(v, str)})
    rx = {k: re.compile(v, re.I if k in ("gpio", "output") else 0) for k, v in pat.items()}
    intent = {k: str(v).lower() for k, v in (lc.get("default_level") or {}).items()}
    comps = {c["ref"]: c for c in spec.get("components", [])}
    nets = spec.get("nets") or {}
    if isinstance(nets, list):
        nets = {n: p for n, p in nets}
    rails = set((cons.get("power") or {}).keys()) | set(lc.get("rails", []) or [])

    rail_rx = re.compile(lc.get("rail_regex") or RAIL_RE, re.I)

    def is_rail(n):                      # stricter than POWER_RE: PWR_HOLD / PWR_EN are control lines, not rails
        return n in rails or bool(GND_RE.match(_basename(n))) or bool(rail_rx.match(_basename(n)))

    def level_of(n):
        return "low" if GND_RE.match(_basename(n)) else "high"

    net_of, pins_of = {}, {}
    for n, pl in nets.items():
        for r, pin in pl:
            net_of[(r, str(pin))] = n
            pins_of.setdefault(r, []).append(str(pin))
    sch_types = sch_types or {}

    def info(r, pin):
        """(name, type) of a pin: names from the pinmap (number -> name) or the id itself; type from the spec
        `pin_types` (by name or number), else the schematic symbol."""
        c = comps.get(r, {})
        pm = c.get("pinmap") or {}
        rev = {str(v): k for k, v in pm.items()}
        name = rev.get(pin, pin)
        num = str(pm.get(pin, pin))
        pt = c.get("pin_types") or {}
        typ = pt.get(name) or pt.get(num) or ""
        if not typ and r in sch_types:
            sn, st = sch_types[r].get(num, ("", ""))
            typ = st if st not in ("passive", "unspecified") else ""
            name = name if name != num or not sn or sn == "~" else sn
        return name, typ

    def tokens(name):
        return [t for t in re.split(r"[/|]", name) if t]

    def role(r, pin):
        c = comps.get(r, {})
        name, typ = info(r, pin)
        if c.get("dnp"):
            return "dnp", name
        if _is_resistor(c, len(set(pins_of.get(r, ())))):
            return "resistor", name
        if typ in _OD_TYPES or any(rx["open_drain"].match(t) for t in tokens(name)):
            return "open_drain", name
        if typ in _IN_TYPES and any(rx["control"].match(t) for t in tokens(name)):
            return "control", name
        if not typ and any(rx["control"].match(t) for t in tokens(name)) and not _is_connector(r, c.get("fp", ""), c.get("value", "")):
            return "control", name
        if typ in _STRONG_TYPES or any(rx["output"].match(t) for t in tokens(name)):
            return "strong", name
        if typ in _WEAK_TYPES or any(rx["gpio"].match(t) for t in tokens(name)):
            return "gpio", name
        if _is_connector(r, c.get("fp", ""), c.get("value", "")) or re.search(r"connector|conn_", c.get("lib", ""), re.I):
            return "connector", name
        if typ in _IN_TYPES:
            return "input", name
        return "other", name

    def other_end(r, pin):
        ps = [p for p in set(pins_of.get(r, ())) if p != pin]
        return net_of.get((r, ps[0])) if len(ps) == 1 else None

    def describe(r, pin, name, kind):
        what = {"gpio": "GPIO output, Hi-Z until configured", "connector": "off-board / module pin, state unknown at power-up",
                "strong": "push-pull output", "open_drain": "open-drain", "other": "pin"}.get(kind, kind)
        return f"{r}.{name} ({what})"

    out = []

    def add(sev, code, msg, refs):
        out.append(dict(severity=sev, code=code, msg=msg, refs=sorted(set(refs))))

    def scan(n, via="", skip=None, hop=True):
        """-> (pulls [(R, rail, via)], dnp pulls, drivers [(ref, name, kind, via)], controls, open-drains, inputs) of net n;
        one hop through a series resistor brings the far side's pulls and drivers (marked `via R`)."""
        P, D, DRV, C, OD, IN = [], [], [], [], [], []
        for r, p in nets.get(n, []):
            p = str(p)
            if r == skip:
                continue
            kind, name = role(r, p)
            if kind == "resistor":
                far = other_end(r, p)
                if far and is_rail(far):
                    P.append((r, far, via))
                elif far and hop and far != n:
                    P2, D2, DRV2, _c, _o, _i = scan(far, f" via {r}", skip=r, hop=False)
                    P += P2
                    D += D2
                    DRV += DRV2
            elif kind == "dnp":
                far = other_end(r, p)
                if _is_resistor(comps[r], len(set(pins_of.get(r, ())))) and far and is_rail(far):
                    D.append((r, far))
            elif kind == "control":
                if not via:
                    C.append((r, name))
            elif kind == "open_drain":
                OD.append((r, name)) if not via else None
                DRV.append((r, name, kind, via))
            elif kind in ("gpio", "connector", "strong", "other"):
                DRV.append((r, name, kind, via))
            elif kind == "input" and not via:
                IN.append((r, name))
        return P, D, DRV, C, OD, IN

    for n, pl in sorted(nets.items()):
        if is_rail(n):
            continue
        pulls, dnp_pulls, drivers, ctrls, ods, inputs = scan(n)
        # passives (caps, other 2-pin parts) are "other" with numeric pin names: not drivers
        drivers = [d for d in drivers if not (d[2] == "other" and len(set(pins_of.get(d[0], ()))) <= 2)]
        prefs = [r for r, _f, _v in pulls]
        drv_txt = ", ".join(describe(r, nm, nm, k) + via for r, nm, k, via in drivers) or "none"
        up = [x for x in pulls if level_of(x[1]) == "high"]
        down = [x for x in pulls if level_of(x[1]) == "low"]
        ptxt = "; ".join(f"pull-{'up' if level_of(f) == 'high' else 'down'} {r} {comps[r].get('value', '')} to {f}{v}"
                         for r, f, v in pulls)
        if ctrls:
            names = ", ".join(f"{r}.{nm}" for r, nm in ctrls)
            al = any(rx["active_low"].search(nm) for _r, nm in ctrls) or bool(rx["active_low"].search(_basename(n)))
            if pulls:
                lvl = "high" if up and not down else "low" if down and not up else "divider"
                note = ""
                if al and lvl == "low":
                    note = " (active-low: ASSERTED at power-up)"
                elif al and lvl == "high":
                    note = " (active-low: released at power-up)"
                add("info", "control-pin-default",
                    f"{names} on {n}: {ptxt} -> default {lvl.upper()}{note}; drivers: {drv_txt}",
                    [r for r, _ in ctrls] + prefs + [d[0] for d in drivers])
                want = intent.get(n)
                if want in ("high", "low") and want != lvl:
                    add("warning", "control-pin-intent",
                        f"{names} on {n}: default {lvl.upper()} from {ptxt}, but [lint.default_level] declares {want.upper()}",
                        [r for r, _ in ctrls] + prefs)
            elif dnp_pulls:
                add("warning", "control-pin-dnp",
                    f"{names} on {n}: the only pull ({', '.join(r for r, _ in dnp_pulls)}) is DNP; drivers: {drv_txt}",
                    [r for r, _ in ctrls] + [r for r, _ in dnp_pulls])
            elif n in intent and intent[n].startswith("external"):
                add("info", "control-pin-default", f"{names} on {n}: declared {intent[n]} (pull outside this board)",
                    [r for r, _ in ctrls])
            elif any(k == "strong" for _r, _n, k, _v in drivers):
                add("info", "control-pin-default", f"{names} on {n}: no pull, driven by {drv_txt}",
                    [r for r, _ in ctrls] + [d[0] for d in drivers])
            elif any(k == "other" for _r, _n, k, _v in drivers):
                add("info", "control-pin-default",
                    f"{names} on {n}: no pull; driver direction unknown: {drv_txt} (declare spec pin_types to check it)",
                    [r for r, _ in ctrls] + [d[0] for d in drivers])
            elif drivers:
                add("warning", "control-pin-floating",
                    f"{names} on {n}: no pull resistor; the only driver(s) {drv_txt} can be high-impedance at power-up"
                    f"{' (active-low)' if al else ''} - add a pull to the safe level", [r for r, _ in ctrls] + [d[0] for d in drivers])
            else:
                add("warning", "input-undriven", f"{names} on {n}: no driver and no pull", [r for r, _ in ctrls])
        if ods and not up and not (n in intent and intent[n] in ("external-high", "external")):
            add("warning", "open-drain-no-pullup",
                f"{n}: open-drain {', '.join(f'{r}.{nm}' for r, nm in ods)} with no pull-up on the board"
                f"{' (pull-down only: ' + ptxt + ')' if down else ''}", [r for r, _ in ods])
        if inputs and not ctrls and not drivers and not pulls:
            add("warning", "input-undriven", f"{', '.join(f'{r}.{nm}' for r, nm in inputs)} on {n}: no driver and no pull",
                [r for r, _ in inputs])
    if waivers:
        rules = [(c, re.compile(p or ".")) for c, _, p in (w.partition(":") for w in waivers)]
        out = [f for f in out if not any(f["code"] == c and (x.search(f["msg"]) or any(x.search(r) for r in f["refs"]))
                                          for c, x in rules)]
    return out
