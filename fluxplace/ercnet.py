"""Schematic-level electrical checks on the netlist (ERC-style, no pcbnew needed).

Codes (severity in brackets):
  DECAP_MISSING        [warning] IC power pin on a rail with no capacitor rail->ground
  DECAP_FAR            [warning] nearest such capacitor is farther than [lint] decap_mm
                                  (needs placed pads: board_facts)
  I2C_PULLUP_MISSING   [warning] SDA/SCL-style net with no resistor to a rail
  PASSIVE_VALUE        [warning] R/C/L value does not parse to number+unit;
                       [info]    pull resistor outside [erc] pull_min_ohm..pull_max_ohm
  NET_NO_DRIVER        [info]    every pin on the net is an input by symbol pin type

Roles come from the official KiCad symbol (pin type and pin name via review.pin_role),
the net names via lint's GND/power patterns, and values from the part's value string.
No board-specific reference, net name or constant lives here; thresholds come from the
constraints TOML with the defaults stated below. A rating (voltage) is not in the data,
so it is not checked.

Input: a spec dict {"components": [...], "nets": {name: [[ref, pin], ...]} or
[[name, [[ref, pin], ...]], ...]}, or a lint-style flat pad list (then only the
pin-type-free checks run). board_facts: review facts (uses facts["parts"][ref]["pad_abs"])
or {"parts": {...}} built by the caller.

Every finding is a lint-style dict {severity, code, msg, refs}.
"""
import re

from . import lint as LT
from . import review as R

__all__ = ["run", "lib_pin_types", "parse_ohm", "DEFAULT_DECAP_MM", "DEFAULT_PULL_MIN_OHM",
           "DEFAULT_PULL_MAX_OHM"]

DEFAULT_DECAP_MM = 5.0
DEFAULT_PULL_MIN_OHM = 1e3
DEFAULT_PULL_MAX_OHM = 1e5

_PASSIVE_REF = re.compile(r"^(C|R|L|D|FB|F|LED|Q|J|P|CN|X|Y|SW|TP|MH|H|FID|BT|K|RV|TH|SP|LS|BZ|"
                          r"ANT|CR|T|FL|JP|SJ)_?\d", re.I)
_R_TOK = re.compile(r"(\d+)(?:\.(\d+))?([RKkM])?(\d*)")
_R_VAL = re.compile(r"\d+(?:\.\d+)?(?:[RKkM]\d*)?")
_C_VAL = re.compile(r"\d+(?:\.\d+)?(?:[pnuµ]\d*F?|mF|F)")
_L_VAL = re.compile(r"\d+(?:\.\d+)?(?:[pnuµm]\d*H|H)")
_I2C = re.compile(r"(?:^|[^A-Z])(SDA|SCL)\d*$", re.I)
_CAP_REF = re.compile(r"^C_?\d+", re.I)
_LED_HINT = re.compile(r"(?:^|[^A-Z])LEDS?(?:[^A-Z]|$)", re.I)
_PULL_LOAD_EXCL = re.compile(r"^(R|C|L|D|LED|FB|F|TP|MH|H|FID|JP|SJ)_?\d", re.I)
_RES_REF = re.compile(r"^R_?\d+", re.I)

_LIB_CACHE = {}
_IDX = {}


def lib_pin_types(path, name):
    """{pin number: (pin name, electrical type)} of one official symbol (all units),
    following 'extends'. Types are KiCad's: input, output, power_in, passive, ..."""
    key = (path, name)
    if key in _LIB_CACHE:
        return _LIB_CACHE[key]
    with open(path, encoding="utf-8", errors="replace") as fh:
        txt = fh.read()
    m = re.search(r'^\t\(symbol "' + re.escape(name) + r'"(.*?)(?=^\t\(symbol "|\Z)',
                  txt, re.M | re.S)
    out = {}
    if m:
        block = m.group(1)
        ext = re.search(r'\(extends "([^"]+)"\)', block)
        if ext:
            out = lib_pin_types(path, ext.group(1))
        else:
            types = re.findall(r"\(pin\s+(\S+)\s", block)
            names = re.findall(r'\(name\s+"([^"]*)"', block)
            nums = re.findall(r'\(number\s+"([^"]*)"', block)
            out = {n: (nm, t) for t, nm, n in zip(types, names, nums)}
    _LIB_CACHE[key] = out
    return out


def parse_ohm(tok):
    """'10k' -> 10000, '4k7' -> 4700, '100R' -> 100, '1M' -> 1e6; None when unparseable."""
    m = _R_TOK.fullmatch((tok or "").strip())
    if not m:
        return None
    whole, frac, letter, tail = m.groups()
    if letter is None and tail:
        return None
    mult = {"R": 1.0, "K": 1e3, "k": 1e3, "M": 1e6, None: 1.0}[letter]
    if tail:                                  # '4k7' = 4.7k: digits after the letter are the fraction
        return float(f"{whole}.{tail}") * mult
    return float(f"{whole}.{frac or '0'}") * mult


def _first_token(value):
    parts = (value or "").strip().split()
    return parts[0] if parts else ""


def _value_ok(ref, value):
    tok = _first_token(value)
    if not tok:
        return False
    if re.match(r"^R", ref, re.I):
        return bool(_R_VAL.fullmatch(tok))
    if re.match(r"^C", ref, re.I):
        return bool(_C_VAL.fullmatch(tok))
    return bool(_L_VAL.fullmatch(tok))


def _index(idx):
    if idx is not None:
        return idx
    if "idx" not in _IDX:
        _IDX["idx"] = R.lib_index()
    return _IDX["idx"]


def _from_pads(pads):
    comps, nets = {}, {}
    for p in pads:
        comps.setdefault(p["ref"], {"ref": p["ref"], "value": p.get("value", ""),
                                    "fp": p.get("footprint", "")})
        if p.get("net"):
            nets.setdefault(p["net"], []).append([p["ref"], str(p["pad"])])
    return {"components": list(comps.values()), "nets": nets}


def _pad_of(comp, pin):
    """A spec net pin is a pad number or a pinmap pin name; returns its pad numbers."""
    pm = (comp or {}).get("pinmap") or {}
    v = pm.get(pin, pin)
    return [str(x) for x in (v if isinstance(v, (list, tuple)) else [v])]


def _nets_of(spec, comps):
    nets = spec.get("nets") or {}
    items = nets.items() if isinstance(nets, dict) else nets
    out = {}
    for name, pins in items:
        for r, p in pins:
            out.setdefault(name, []).extend((str(r), pad) for pad in _pad_of(comps.get(str(r)), str(p)))
    return out


class _Model:
    def __init__(self, spec, idx, use_lib):
        self.comps = {c["ref"]: c for c in spec.get("components", [])}
        self.net_pins = _nets_of(spec, self.comps)
        self.pin_info = {}          # (ref, pad) -> (pin name, etype or None, role)
        self.known_types = use_lib
        # names from the spec's pinmap (name -> pad), the fallback when no official symbol exists
        for ref, c in self.comps.items():
            for name, pads in ((c.get("pinmap") or {}).items()):
                for pad in (pads if isinstance(pads, (list, tuple)) else [pads]):
                    self.pin_info[(ref, str(pad))] = (name, None, R.pin_role(name))
        if use_lib and idx:
            for ref, c in self.comps.items():
                sym = R.lib_symbol_for(c.get("mpn") or c.get("value") or "", c.get("value") or "", idx)
                if not sym:
                    continue
                types = lib_pin_types(*sym)
                for pin, (nm, et) in types.items():
                    self.pin_info[(ref, pin)] = (nm, et, R.pin_role(nm))  # symbol beats pinmap
        self.ref_pins = {}
        for net, pins in self.net_pins.items():
            for ref, pin in pins:
                self.ref_pins.setdefault(ref, set()).add(pin)
        # pad count: the pinmap's pads when given (a part with 2 wired pins is still a 5-pad IC)
        self.pad_count = {}
        for ref, c in self.comps.items():
            pads = set()
            for v in (c.get("pinmap") or {}).values():
                pads.update(str(x) for x in (v if isinstance(v, (list, tuple)) else [v]))
            self.pad_count[ref] = max(len(pads), len(self.ref_pins.get(ref, ())))
        self.known_types = bool(use_lib and any(e for _, e, _ in self.pin_info.values()))

    def info(self, ref, pin):
        return self.pin_info.get((ref, pin), ("", None, None))

    def is_gnd(self, net):
        if LT.GND_RE.match(LT._basename(net)):
            return True
        return any(self.info(r, p)[2] == "GND" for r, p in self.net_pins.get(net, []))

    def is_rail(self, net):
        if self.is_gnd(net):
            return False
        if LT._is_power(net):
            return True
        return any(self._power_pin(r, p) for r, p in self.net_pins.get(net, []))

    def _power_pin(self, ref, pin):
        _, et, role = self.info(ref, pin)
        if role == "PWR":
            return True
        return et == "power_in" and role != "GND"

    def caps_between(self, rail):
        """[(cap ref, rail pad, ground pad)] for capacitors with one pad on `rail` and one on ground."""
        out = []
        for ref in self.comps:
            if not _CAP_REF.match(ref):
                continue
            nets = self.nets_of_ref(ref)
            rail_pads = nets.get(rail, [])
            gnd_pads = [p for n, ps in nets.items() if n != rail and self.is_gnd(n) for p in ps]
            if rail_pads and gnd_pads:
                out.append((ref, rail_pads[0], gnd_pads[0]))
        return out

    def nets_of_ref(self, ref):
        return {n: [p for r, p in ps if r == ref] for n, ps in self.net_pins.items()
                if any(r == ref for r, _ in ps)}


def _is_ic(ref, pad_count):
    return (pad_count >= 5 and not _PASSIVE_REF.match(ref)
            and not LT.CONN_REF_RE.match(ref))


def _check_decaps(m, board_facts, decap_mm, out):
    pos = {}
    if board_facts:
        for ref, part in (board_facts.get("parts") or {}).items():
            for num, xy in (part.get("pad_abs") or {}).items():
                pos[(ref, str(num))] = xy
    for ref in sorted(m.comps):
        pins = m.ref_pins.get(ref, set())
        if not _is_ic(ref, m.pad_count.get(ref, 0)):
            continue
        by_rail = {}
        for net, npins in m.nets_of_ref(ref).items():
            if not m.is_rail(net):
                continue
            hits = sorted(p for p in npins if m._power_pin(ref, p))
            if hits:
                by_rail[net] = hits
        for rail, rpins in sorted(by_rail.items()):
            caps = m.caps_between(rail)
            if not caps:
                out.append(dict(severity="warning", code="DECAP_MISSING",
                                msg=f"{ref} power pin(s) {','.join(rpins)} on {rail}: "
                                    f"no capacitor from {rail} to ground",
                                refs=[ref]))
                continue
            if not pos:
                continue
            worst = None
            for pin in rpins:
                p0 = pos.get((ref, pin))
                if p0 is None:
                    continue
                best = None
                for cref, cpin, _ in caps:
                    c0 = pos.get((cref, cpin))
                    if c0 is None:
                        continue
                    d = ((p0[0] - c0[0]) ** 2 + (p0[1] - c0[1]) ** 2) ** 0.5
                    if best is None or d < best[0]:
                        best = (d, cref)
                if best and (worst is None or best[0] > worst[0]):
                    worst = (best[0], best[1], pin)
            if worst and worst[0] > decap_mm:
                out.append(dict(severity="warning", code="DECAP_FAR",
                                msg=f"{ref} pin {worst[2]} on {rail}: nearest decap {worst[1]} is "
                                    f"{worst[0]:.1f} mm away (limit {decap_mm:g} mm)",
                                refs=[ref, worst[1]]))


def _check_i2c(m, out):
    for net, pins in sorted(m.net_pins.items()):
        if len(pins) < 2:
            continue
        names = [m.info(r, p)[0] for r, p in pins]
        if not (any(_I2C.search(n or "") for n in names) or _I2C.search(LT._basename(net))):
            continue
        ok = False
        for ref, _ in pins:
            if not _RES_REF.match(ref):
                continue
            for other in m.nets_of_ref(ref):
                if other != net and m.is_rail(other):
                    ok = True
        if not ok:
            out.append(dict(severity="warning", code="I2C_PULLUP_MISSING",
                            msg=f"net {net}: SDA/SCL-style signal with no resistor to a rail",
                            refs=sorted({r for r, _ in pins})))


def _check_passives(m, cons, out):
    erc = (cons or {}).get("erc", {}) or {}
    lo = float(erc.get("pull_min_ohm", DEFAULT_PULL_MIN_OHM))
    hi = float(erc.get("pull_max_ohm", DEFAULT_PULL_MAX_OHM))
    for ref, c in sorted(m.comps.items()):
        if not re.match(r"^[RCL]_?\d", ref, re.I):
            continue
        value = c.get("value", "")
        if not _value_ok(ref, value):
            out.append(dict(severity="warning", code="PASSIVE_VALUE",
                            msg=f"{ref} value '{value}' does not parse to a number + unit",
                            refs=[ref]))
            continue
        if not _RES_REF.match(ref):
            continue
        nets = m.nets_of_ref(ref)
        if len(nets) != 2:
            continue
        a, b = sorted(nets)
        rail_side = [n for n in (a, b) if m.is_rail(n) or m.is_gnd(n)]
        sig_side = [n for n in (a, b) if n not in rail_side]
        if len(rail_side) != 1 or len(sig_side) != 1:
            continue
        # a pull ends on an active pin, not on another passive, not on an LED (a series
        # current-limit resistor to an LED is not a pull), and not in a divider
        sig = sig_side[0]
        if not any(not _PULL_LOAD_EXCL.match(r) for r, _ in m.net_pins.get(sig, [])):
            continue
        # another resistor on the same net makes it a divider / feedback / set network, not a pull
        if sum(1 for r, _ in m.net_pins.get(sig, []) if _RES_REF.match(r)) > 1:
            continue
        if _LED_HINT.search(LT._basename(sig)) or any(
                _LED_HINT.search(m.comps.get(r, {}).get("value", "")) for r, _ in m.net_pins.get(sig, [])):
            continue
        ohm = parse_ohm(_first_token(value))
        if ohm is not None and not (lo <= ohm <= hi):
            out.append(dict(severity="info", code="PASSIVE_VALUE",
                            msg=f"{ref} pull {value} on {sig_side[0]} is outside "
                                f"{lo:g}..{hi:g} ohm", refs=[ref]))


def _check_drivers(m, out):
    if not m.known_types:
        return
    for net, pins in sorted(m.net_pins.items()):
        if len(pins) < 2 or net.lower().startswith("unconnected"):
            continue
        types = [m.info(r, p)[1] for r, p in pins]
        if all(t == "input" for t in types):
            out.append(dict(severity="info", code="NET_NO_DRIVER",
                            msg=f"net {net}: every pin is an input (no driver)",
                            refs=sorted({r for r, _ in pins})))


def waive(findings, waivers):
    """Same contract as lint.run's waivers: 'CODE:REGEX' drops matching findings."""
    if not waivers:
        return findings
    rules = []
    for w in waivers:
        code, _, pat = w.partition(":")
        rules.append((code, re.compile(pat or ".")))
    return [f for f in findings if not any(
        f["code"] == c and (rx.search(f["msg"]) or any(rx.search(r) for r in f["refs"]))
        for c, rx in rules)]


def run(spec_or_pads, cons=None, board_facts=None, idx=None):
    """See module doc. Returns lint-style findings (severity warning|info)."""
    if isinstance(spec_or_pads, list):
        spec, use_lib = _from_pads(spec_or_pads), False
    else:
        spec, use_lib = spec_or_pads, True
    lib_idx = _index(idx) if use_lib else None
    m = _Model(spec, lib_idx, use_lib)
    decap_mm = float(((cons or {}).get("lint", {}) or {}).get("decap_mm", DEFAULT_DECAP_MM))
    out = []
    _check_decaps(m, board_facts, decap_mm, out)
    _check_i2c(m, out)
    _check_passives(m, cons, out)
    _check_drivers(m, out)
    return out
