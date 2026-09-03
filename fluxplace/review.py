"""The DESIGN REVIEW gate — the checks an outside reviewer runs that DRC,
ERC, lint and the sourcing gate all miss.

Every rule here is a failure class that reached an external review of a
finished, DRC-clean, fully packaged board (utv-comms V1.4, 2026-09-03) without
any tool objecting. The reviewer found nine issues; the tools had found none,
because each tool checked the board against ITSELF (schematic vs copper,
netclass vs track) and none checked it against the SPEC, the DATASHEET or the
ENVIRONMENT. This module does that:

  NET_STRAIGHT_COPPER    a spec-protected net has a component on it
  PAIR_SKEW / PAIR_LAYER_MISMATCH / PAIR_VIA_MISMATCH
                         diff pair P and N routed differently
  RF_IMPEDANCE_OFF       RF copper graded on the LAYER it is actually on
  RF_VIA_COUNT           layer transitions on an RF net
  FOOTPRINT_PACKAGE_MISMATCH / PIN_COUNT_MISMATCH
                         distributor says PowerDI5060-8, board says SOIC-8
  PINMAP_ROLE_MISMATCH / PINMAP_PIN_ABSENT / PINMAP_UNVERIFIED
                         spec pinmap vs the KiCad official symbol for the MPN
  TEMP_RATING            part rated narrower than the product environment
  SPEC_SIZE_MISMATCH / SPEC_LAYER_MISMATCH / SPEC_COMPONENT_MISMATCH /
  SPEC_FOOTPRINT_MISMATCH
                         the spec JSON and the copper have drifted apart
  HOLDUP_SHORT           bulk capacitance cannot deliver the stated ride-through
  TVS_MARGIN             clamp voltage vs the downstream device rating
  ENV_UNDEFINED          nobody answered the environment questions

Pure logic runs on a `facts` dict so it is testable without pcbnew;
`facts_from_board()` at the bottom is the only pcbnew consumer. Findings are
{"level": FAIL|WARN|INFO, "code", "msg", "refs"} — FAIL is a stop-ship, the
same contract as preflight and lint.
"""
import glob
import math
import os
import re

from . import constraints as C
from . import graph as G
from . import si as SI
from . import stackup as ST
from .comprehend import parse_value

__all__ = ["run", "summarize", "facts_from_board", "package_key",
           "packages_agree", "lib_index", "lib_pins", "pin_role"]

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"


def _f(level, code, msg, refs=()):
    return {"level": level, "code": code, "msg": msg, "refs": sorted(set(refs))}


# ==========================================================================
# 1. Net rules from the spec/constraints
# ==========================================================================
def check_net_rules(facts, cons):
    """[nets."PTT_THRU"] straight_copper = ["J1A:1", "J2A:1"] — the net may
    touch ONLY those pads. Anything else on it is a component in a path the
    spec says must survive the board being dead."""
    out = []
    rules = (cons or {}).get("nets", {})
    for net, rule in rules.items():
        allowed = rule.get("straight_copper")
        if allowed is None:
            continue
        allowed = {tuple(a.split(":", 1)) if ":" in a else (a, None)
                   for a in allowed}
        allowed_refs = {a[0] for a in allowed}
        pads = facts["net_pads"].get(net)
        if pads is None:
            out.append(_f(WARN, "NET_RULE_UNKNOWN_NET",
                          f"{net}: straight_copper rule but no such net on the board"))
            continue
        bad = [(r, p) for r, p in pads
               if (r, p) not in allowed and (r, None) not in allowed]
        if bad:
            out.append(_f(FAIL, "NET_STRAIGHT_COPPER",
                          f"{net} must be straight copper between "
                          f"{', '.join(sorted(allowed_refs))} but also lands on "
                          f"{', '.join(f'{r}.{p}' for r, p in sorted(bad))} — "
                          f"a component in the fail-safe path",
                          refs=[r for r, _ in bad]))
        elif len(pads) < len(allowed):
            out.append(_f(FAIL, "NET_STRAIGHT_COPPER",
                          f"{net}: not all required pads are on the net "
                          f"({len(pads)} of {len(allowed)})"))
    return out


# ==========================================================================
# 2. Differential pairs — P and N must be the same route
# ==========================================================================
_HS_FAMILIES = {"ETH", "USB", "PCIE", "HDMI", "MIPI", "LVDS", "CSI", "DSI",
                "SATA", "SDI", "TMDS", "CLK", "DP", "SGMII", "RGMII", "MDI"}


def _real_layers(track, min_mm=2.0):
    return {l for l, mm in track.get("layers", {}).items() if mm >= min_mm}


def check_pairs(facts, cons):
    tracks = facts.get("net_tracks", {})
    pairs = G.diff_pairs(list(facts["net_pads"]))
    if not pairs:
        return []
    lengths = {n: t["length"] for n, t in tracks.items()}
    lim = lambda master: C.skew_limit_mm(cons or {}, master)
    finds, _ = SI.pair_skew_findings(lengths, pairs, warn_mm=lim)
    out = []
    fams = tuple((cons or {}).get("pairs", {}))
    def gated(master):
        # skew is a stop-ship for high-speed pairs and for any family the
        # engineer wrote a [pairs.*] rule for; on an audio pair it is advice
        return master.startswith(fams) or master.upper().split("_")[0] in _HS_FAMILIES
    for level, code, msg in finds:
        master = msg.split("/")[0]
        hard = code == "PAIR_SKEW" and gated(master)
        out.append(_f(FAIL if hard else WARN, code, msg))
    for slave, master in sorted(pairs.items()):
        tm, ts = tracks.get(master), tracks.get(slave)
        if not tm or not ts or not gated(master):
            continue
        lm, ls = _real_layers(tm), _real_layers(ts)
        if lm != ls:
            out.append(_f(WARN, "PAIR_LAYER_MISMATCH",
                          f"{master}/{slave}: P on {'/'.join(sorted(lm)) or '-'}, "
                          f"N on {'/'.join(sorted(ls)) or '-'} — the pair does not "
                          f"share a reference plane"))
        if tm.get("vias", 0) != ts.get("vias", 0):
            out.append(_f(WARN, "PAIR_VIA_MISMATCH",
                          f"{master}/{slave}: {tm.get('vias', 0)} vs "
                          f"{ts.get('vias', 0)} vias"))
    return out


# ==========================================================================
# 3. RF impedance on the layer the copper is actually on
# ==========================================================================
def _dielectrics(stackup):
    """[(name|None, type, thickness, er)] in order; copper rows carry name."""
    return [(l.get("name"), l["type"], float(l.get("thickness", 0) or 0),
             float(l.get("epsilon_r", 4.4) or 4.4)) for l in stackup]


def layer_geometry(stackup, plane_layers, layer):
    """(h_up, er_up, h_dn, er_dn, t) — dielectric distance from `layer` to the
    nearest reference plane above and below (None when there is none), and
    the copper thickness. Thickness-weighted Er across mixed dielectrics."""
    rows = _dielectrics(stackup)
    idx = next((i for i, r in enumerate(rows) if r[0] == layer), None)
    if idx is None:
        return None
    t = rows[idx][2] or 0.035

    def walk(step):
        h, er_w = 0.0, 0.0
        i = idx + step
        while 0 <= i < len(rows):
            name, typ, th, er = rows[i]
            if typ == "copper":
                if name in plane_layers:
                    return (h, er_w / h if h else 4.4)
                i += step
                continue
            h += th
            er_w += th * er
            i += step
        return (None, None)
    hu, eu = walk(-1)
    hd, ed = walk(+1)
    return hu, eu, hd, ed, t


def z0_on_layer(w, geom):
    """Characteristic impedance of a `w` mm trace with layer_geometry `geom`.
    One plane -> microstrip; two planes -> asymmetric stripline (parallel
    combination of the two symmetric striplines, an accepted closed form).
    Returns (z, model) or (None, 'unreferenced')."""
    hu, eu, hd, ed, t = geom
    if hu is None and hd is None:
        return None, "unreferenced"
    if hu is None or hd is None:
        h, er = (hd, ed) if hu is None else (hu, eu)
        return ST.microstrip_z0(w, h, t, er), "microstrip"
    z1 = ST.stripline_z0(w, 2 * hu + t, t, eu)
    z2 = ST.stripline_z0(w, 2 * hd + t, t, ed)
    return 2 * z1 * z2 / (z1 + z2), "stripline"


_RF_TOKENS = {"RF", "RFIN", "RFOUT", "COAX", "UFL", "LNA", "ANTENNA"}


def is_rf(net):
    """A net is RF when a whole underscore-token says so: RF_GNSS, RF_INT_SW,
    ANT1_RF. ANT_SEL / ANT_DET / PWRFAIL are DC lines whose NAMES merely
    contain the letters (stackup.looks_rf matched those — measured on V1.4)."""
    toks = set(re.split(r"[_\-.]", net.upper()))
    return bool(toks & _RF_TOKENS)


def check_rf(facts, cons):
    rf = (cons or {}).get("rf", {})
    target = float(rf.get("target_z", 50.0))
    tol = float(rf.get("tolerance_pct", 10.0))
    max_vias = int(rf.get("max_vias", 1))
    min_seg = float(rf.get("min_segment_mm", 1.0))
    names = set(rf.get("nets", [])) or {n for n in facts["net_pads"] if is_rf(n)}
    stackup = facts.get("stackup") or []
    planes = set(facts.get("plane_layers", []))
    out = []
    if not names:
        return out
    if not stackup:
        out.append(_f(WARN, "RF_NO_STACKUP",
                      "RF nets present but the board defines no stackup — "
                      "impedance cannot be verified"))
        return out
    per_net_rules = (cons or {}).get("nets", {})
    for net in sorted(names):
        tr = facts["net_tracks"].get(net)
        if not tr:
            continue
        lim = int(per_net_rules.get(net, {}).get("max_vias", max_vias))
        if tr.get("vias", 0) > lim:
            out.append(_f(FAIL if net in per_net_rules else WARN, "RF_VIA_COUNT",
                          f"{net}: {tr['vias']} vias (limit {lim}) — every layer "
                          f"transition is an impedance discontinuity"))
        worst = None
        for layer, w, mm in tr.get("segments", []):
            if mm < min_seg:
                continue
            geom = layer_geometry(stackup, planes, layer)
            if geom is None:
                continue
            z, model = z0_on_layer(w, geom)
            if z is None:
                out.append(_f(WARN, "RF_NO_REFERENCE",
                              f"{net}: {mm:.1f}mm on {layer} with no reference "
                              f"plane on either side"))
                continue
            err = 100.0 * (z - target) / target
            if abs(err) > tol and (worst is None or abs(err) > abs(worst[0])):
                worst = (err, layer, w, mm, z, model)
        if worst:
            err, layer, w, mm, z, model = worst
            out.append(_f(FAIL, "RF_IMPEDANCE_OFF",
                          f"{net}: {mm:.1f}mm of {w:.3f}mm trace on {layer} is "
                          f"~{z:.0f} ohm ({model}, {err:+.0f}% of {target:.0f}) — "
                          f"the width is right for an outer layer, not this one"))
    return out


# ==========================================================================
# 4. Package / pin count / pinmap / temperature — against the manufacturer
# ==========================================================================
_CHIP = ("01005", "0201", "0402", "0504", "0603", "0805", "1008", "1206",
         "1210", "1806", "1812", "2010", "2220", "2512", "2920")

# (regex, family, pins-from-group-or-fixed)
_PKG_RULES = [
    (r"\bSOT-?23-?(\d)\b", "SOT23", "g"), (r"\bTSOT-?23-?(\d)\b", "SOT23", "g"),
    (r"\bSOT-?23\b(?!-)", "SOT23", 3), (r"\bSOT-?25\b", "SOT23", 5),
    (r"\bSOT-?26\b", "SOT23", 6), (r"\bSOT-?753\b", "SOT23", 5),
    (r"\bSOT-?457\b", "SOT23", 6), (r"\bTSOP-?6\b", "SOT23", 6),
    (r"\bSC-?59\b", "SOT23", 3), (r"\bTO-?236\b", "SOT23", 3),
    (r"\bSOT-?353\b", "SC70", 5), (r"\bSC-?70-?(\d)\b", "SC70", "g"),
    (r"\bSOT-?363\b", "SC70", 6), (r"\bSC-?88A?\b", "SC70", None),
    (r"\bSOT-?89-?(\d)?", "SOT89", "g"), (r"\bSOT-?223-?(\d)?", "SOT223", "g"),
    (r"\bPOWERDI-?(\d{4})-?(\d+)?", "POWERDI", "g2"),
    (r"\b(\d+)-?POWERDI-?(\d{4})", "POWERDI", "g1"),
    (r"\b(\d+)-(?:SOIC|SO|SOP|POWERSOIC|HSOP|SO-POWERPAD|EXPOSED)\b", "SOIC", "g1"),
    (r"\b(?:SOIC|HSOP|POWERSOIC)-?(\d+)", "SOIC", "g"),
    (r"\bSO-?(\d+)\b(?![.\d])", "SOIC", "g"), (r"\bSOP-?(\d+)\b", "SOIC", "g"),
    (r"\b(\d+)-(?:TSSOP|HTSSOP)\b", "TSSOP", "g1"), (r"\b(?:H)?TSSOP-?(\d+)", "TSSOP", "g"),
    (r"\b(\d+)-(?:MSOP|VSSOP|HVSSOP)\b", "MSOP", "g1"), (r"\b(?:MSOP|VSSOP|HVSSOP)-?(\d+)", "MSOP", "g"),
    (r"\b(\d+)-(?:SSOP)\b", "SSOP", "g1"), (r"\bSSOP-?(\d+)", "SSOP", "g"),
    (r"\b(\d+)-(?:W|V|U|T|X2)?(?:QFN|VQFN|WQFN|UQFN|TQFN|MLF|MLPQ|MLPD|LFCSP)\b", "QFN", "g1"),
    (r"\b(?:W|V|U|T|X2)?(?:QFN|MLF|MLPQ|MLPD|LFCSP)-?(\d+)", "QFN", "g"),
    (r"\b(\d+)-(?:W|V|U|T|X2)?(?:DFN|SON|WSON|VSON|USON|WDFN|UDFN|TDFN|VDFN|X2SON)\b", "DFN", "g1"),
    (r"\b(?:W|V|U|T|X2)?(?:DFN|SON|WSON|VSON|USON|WDFN|UDFN|TDFN|VDFN|X2SON)-?(\d+)", "DFN", "g"),
    (r"\bTO-?252(?:-(\d))?", "TO252", "g"), (r"\bD-?PAK\b", "TO252", None),
    (r"\bTO-?263(?:-(\d))?", "TO263", "g"), (r"\bD2-?PAK\b", "TO263", None),
    (r"\bTO-?220(?:-(\d))?", "TO220", "g"), (r"\bTO-?92(?:-(\d))?", "TO92", "g"),
    (r"\bTO-?247(?:-(\d))?", "TO247", "g"),
    (r"\bDO-?214AB\b", "SMC", 2), (r"\bSMC\b", "SMC", 2),
    (r"\bDO-?214AA\b", "SMB", 2), (r"\bSMB\b", "SMB", 2),
    (r"\bDO-?214AC\b", "SMA", 2), (r"\bSMA(?:F|J)?\b(?!\s*(?:JACK|CONN|EDGE|BULK))", "SMA", 2),
    (r"\bSOD-?123(?:F|FL|W)?\b", "SOD123", 2), (r"\bSOD-?323(?:F)?\b", "SOD323", 2),
    (r"\bSOD-?523\b", "SOD523", 2), (r"\bSOD-?882\b", "SOD882", 2),
    (r"\bSOD-?80\b", "SOD80", 2), (r"\bMINIMELF\b", "SOD80", 2),
    (r"\b(\d+)-(?:BGA|LFBGA|TFBGA|FBGA)\b", "BGA", "g1"), (r"\bBGA-?(\d+)", "BGA", "g"),
    (r"\b(\d+)-(?:LQFP|TQFP|QFP)\b", "QFP", "g1"), (r"\b(?:LQFP|TQFP|QFP)-?(\d+)", "QFP", "g"),
    (r"\b(\d+)-(?:DIP|PDIP)\b", "DIP", "g1"), (r"\b(?:P)?DIP-?(\d+)", "DIP", "g"),
]


def package_key(text):
    """Every (family, pins) reading of a package string. Distributors list
    aliases ('SC-74A, SOT-753'); footprints carry one name plus decoration
    ('SOT-23-5', 'SOIC-8_3.9x4.9mm_P1.27mm'). pins is None when the string
    does not say."""
    if not text:
        return set()
    u = text.upper().replace("_", " ").replace("−", "-")
    u = u.replace("(", " ").replace(")", " ").replace(",", " ")
    keys = set()
    m = re.search(r"\b(" + "|".join(_CHIP) + r")\b", u)
    if m and not re.search(r"\b(SOT|SOIC|QFN|DFN|SOD|TO-?\d)", u):
        keys.add(("CHIP", m.group(1)))
    for pat, fam, pins in _PKG_RULES:
        for mm in re.finditer(pat, u):
            p = None
            if pins == "g":
                p = int(mm.group(1)) if mm.lastindex and mm.group(1) else None
            elif pins == "g1":
                p = int(mm.group(1))
            elif pins == "g2":
                p = int(mm.group(2)) if mm.lastindex and mm.lastindex >= 2 and mm.group(2) else None
            elif isinstance(pins, int):
                p = pins
            keys.add((fam, p))
    return keys


def _pins_from_pkg(text):
    """Pin count implied by a package string when every reading agrees."""
    pins = {p for _, p in package_key(text) if isinstance(p, int)}
    return pins.pop() if len(pins) == 1 else None


def packages_agree(distributor, footprint):
    """-> (verdict, detail): 'ok' | 'family' | 'pins' | 'unknown'."""
    a, b = package_key(distributor), package_key(footprint)
    if not a or not b:
        return "unknown", f"{distributor!r} vs {footprint!r}"
    fam_a, fam_b = {f for f, _ in a}, {f for f, _ in b}
    shared = fam_a & fam_b
    if not shared:
        return "family", f"{'/'.join(sorted(fam_a))} vs {'/'.join(sorted(fam_b))}"
    if shared == {"CHIP"}:
        ca = {p for f, p in a if f == "CHIP"}
        cb = {p for f, p in b if f == "CHIP"}
        if ca & cb:
            return "ok", "CHIP"
        return "family", f"chip {'/'.join(sorted(ca))} vs {'/'.join(sorted(cb))}"
    for fam in shared:
        pa = {p for f, p in a if f == fam}
        pb = {p for f, p in b if f == fam}
        if None in pa or None in pb or pa & pb:
            return "ok", fam
    pa = sorted(p for f, p in a if f in shared and p)
    pb = sorted(p for f, p in b if f in shared and p)
    return "pins", f"{'/'.join(sorted(shared))} {pa} vs {pb}"


# ---- pinmap vs the KiCad official symbol ----------------------------------
_GND_RE = re.compile(r"^(?:[ADP]?GND\d*|VSS\d*|GND_?\w*|VEE\d*|V-|V−|VNEG|0V)$", re.I)
_PWR_RE = re.compile(r"^(?:V(?:CC|DD|IN|BUS|BAT|BATT|\+|S|DDA|DDD|IO)\w*|\+?\d+V\d*|AV(?:CC|DD)\w*|DV(?:CC|DD)\w*|VCC\w*)$", re.I)
_COMMON = {"A", "ANODE", "COM", "COMMON", "C", "K_COM", "A_COM"}
_NC_RE = re.compile(r"^(?:NC|N/C|DNC)\d*$", re.I)


def pin_role(name):
    n = (name or "").strip().strip("~")
    if _GND_RE.match(n):
        return "GND"
    if _PWR_RE.match(n):
        return "PWR"
    if _NC_RE.match(n):
        return "NC"
    if n.upper() in _COMMON:
        return "COMMON"
    return "IO"


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def lib_index(lib_dirs=("/usr/share/kicad/symbols",)):
    """{normalized symbol name: (path, raw name)} over the official libraries.
    Only top-level symbols (unit symbols carry a _N_M suffix)."""
    idx = {}
    for d in lib_dirs:
        for path in glob.glob(os.path.join(d, "*.kicad_sym")):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
            except OSError:
                continue
            for m in re.finditer(r'^\t\(symbol "([^"]+)"', txt, re.M):
                name = m.group(1)
                if re.search(r"_\d+_\d+$", name):
                    continue
                idx.setdefault(_norm(name), (path, name))
    return idx


def lib_pins(path, name):
    """{number: pin name} of one symbol (all units), following 'extends'."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        txt = fh.read()
    m = re.search(r'^\t\(symbol "' + re.escape(name) + r'"(.*?)(?=^\t\(symbol "|\Z)',
                  txt, re.M | re.S)
    if not m:
        return {}
    block = m.group(1)
    ext = re.search(r'\(extends "([^"]+)"\)', block)
    if ext:
        return lib_pins(path, ext.group(1))
    names = re.findall(r'\(name\s+"([^"]*)"', block)
    nums = re.findall(r'\(number\s+"([^"]*)"', block)
    return {n: nm for nm, n in zip(names, nums)}


def lib_symbol_for(mpn, value, idx):
    """Longest official symbol whose name is a prefix of the MPN (or equals
    the value): SP0504BAHTG -> SP0504BAHT. Minimum 5 characters — 'LM' is
    not evidence."""
    best = None
    for cand in (_norm(mpn), _norm(value)):
        if not cand:
            continue
        for key, (path, name) in idx.items():
            if len(key) >= 5 and cand.startswith(key):
                if best is None or len(key) > len(best[0]):
                    best = (key, path, name)
    return (best[1], best[2]) if best else None


def check_parts(facts, spec, partdata, cons, idx=None):
    """Footprint vs distributor package, pin count vs pads, pinmap vs the
    official symbol, operating temperature vs the environment."""
    out = []
    env = C.env_profile(cons or {})
    parts = facts["parts"]
    spec_parts = {c["ref"]: c for c in (spec or {}).get("components", [])}
    mpn_of = facts.get("mpn_map", {})
    fetched = partdata is not None          # None = no lookup was attempted
    partdata = partdata or {}
    unchecked = []
    for ref in sorted(parts):
        p = parts[ref]
        fp = p.get("footprint", "")
        mpn = mpn_of.get(ref)
        data = partdata.get(mpn) if mpn else None
        sp = spec_parts.get(ref)

        if sp and sp.get("fp") and sp["fp"] != fp.split(":")[-1]:
            out.append(_f(FAIL, "SPEC_FOOTPRINT_MISMATCH",
                          f"{ref}: spec footprint {sp['fp']} but the board has "
                          f"{fp.split(':')[-1]}", refs=[ref]))

        if mpn and data is None and fetched:
            unchecked.append(ref)
        if data:
            pkg = data.get("package")
            if pkg:
                verdict, detail = packages_agree(pkg, fp.split(":")[-1])
                if verdict == "family":
                    out.append(_f(FAIL, "FOOTPRINT_PACKAGE_MISMATCH",
                                  f"{ref} {mpn}: distributor package '{pkg}' "
                                  f"but footprint '{fp.split(':')[-1]}' ({detail}) — "
                                  f"a land-pattern that fits is not the part",
                                  refs=[ref]))
                elif verdict == "pins":
                    out.append(_f(FAIL, "PIN_COUNT_MISMATCH",
                                  f"{ref} {mpn}: package '{pkg}' vs footprint "
                                  f"'{fp.split(':')[-1]}' disagree on pin count "
                                  f"({detail})", refs=[ref]))
                elif verdict == "unknown":
                    out.append(_f(INFO, "PACKAGE_UNCHECKED",
                                  f"{ref} {mpn}: cannot classify '{pkg}' vs "
                                  f"'{fp.split(':')[-1]}'", refs=[ref]))
            npins = data.get("pins") or _pins_from_pkg(pkg)
            npads = p.get("connectable_pads")
            if npins and npads and npins != npads and not p.get("tht_mech"):
                # distributors count leads; footprints may add an EP pad
                if not (npads == npins + 1 and "EP" in fp.upper()):
                    out.append(_f(FAIL, "PIN_COUNT_MISMATCH",
                                  f"{ref} {mpn}: {npins} pins per distributor, "
                                  f"{npads} connectable pads on the footprint",
                                  refs=[ref]))
            tmin, tmax = data.get("temp_min"), data.get("temp_max")
            if env and tmin is not None and tmax is not None:
                if tmin > env["temp_min_c"] or tmax < env["temp_max_c"]:
                    out.append(_f(FAIL, "TEMP_RATING",
                                  f"{ref} {mpn}: rated {tmin:+.0f}..{tmax:+.0f} C, "
                                  f"product environment needs "
                                  f"{env['temp_min_c']:+.0f}..{env['temp_max_c']:+.0f} C",
                                  refs=[ref]))
            elif env and (tmin is None or tmax is None) and ref[0] in "UQDTKJL":
                out.append(_f(INFO, "TEMP_UNRATED",
                              f"{ref} {mpn}: no operating-temperature data from "
                              f"the distributors — confirm from the datasheet",
                              refs=[ref]))
            lc = (data.get("lifecycle") or "").lower()
            if any(b in lc for b in ("obsolete", "discontinued", "nrnd",
                                     "not recommended", "last time")):
                out.append(_f(FAIL, "LIFECYCLE",
                              f"{ref} {mpn}: lifecycle '{data.get('lifecycle')}'",
                              refs=[ref]))

        # pinmap vs the official KiCad symbol for the same part
        pinmap = (sp or {}).get("pinmap")
        if pinmap:
            sym = lib_symbol_for(mpn, (sp or {}).get("value") or p.get("value"),
                                 idx) if idx else None
            if sym:
                lp = lib_pins(*sym)
                if lp:
                    absent = sorted(str(n) for n in pinmap.values()
                                    if str(n) not in lp)
                    if absent:
                        out.append(_f(FAIL, "PINMAP_PIN_ABSENT",
                                      f"{ref} {mpn}: spec pinmap uses pin(s) "
                                      f"{', '.join(absent)} that {sym[1]} "
                                      f"({os.path.basename(sym[0])}) does not have "
                                      f"— it has {len(lp)} pins", refs=[ref]))
                    for nm, num in sorted(pinmap.items()):
                        ln = lp.get(str(num))
                        if ln is None:
                            continue
                        rs, rl = pin_role(nm), pin_role(ln)
                        bad = ((rs == "GND" and rl not in ("GND", "COMMON")) or
                               (rl == "GND" and rs not in ("GND", "COMMON")) or
                               (rs == "PWR" and rl in ("GND", "COMMON")) or
                               (rl == "PWR" and rs == "GND") or
                               (rl == "COMMON" and rs == "IO"))
                        if bad:
                            out.append(_f(FAIL, "PINMAP_ROLE_MISMATCH",
                                          f"{ref} {mpn}: spec puts {nm} on pin "
                                          f"{num}, but {sym[1]} has '{ln}' there "
                                          f"— the pinmap does not match the part",
                                          refs=[ref]))
                    if len(lp) != p.get("connectable_pads", len(lp)):
                        out.append(_f(FAIL, "PIN_COUNT_MISMATCH",
                                      f"{ref} {mpn}: {sym[1]} has {len(lp)} pins, "
                                      f"footprint has {p.get('connectable_pads')} "
                                      f"connectable pads", refs=[ref]))
            elif not (sp or {}).get("pinmap_source"):
                out.append(_f(WARN, "PINMAP_UNVERIFIED",
                              f"{ref} {mpn or p.get('value')}: pinmap has no "
                              f"library match and no pinmap_source (datasheet "
                              f"page/URL) — verify against the pin table",
                              refs=[ref]))
    if unchecked:
        out.append(_f(WARN, "PART_DATA_UNAVAILABLE",
                      f"{len(unchecked)} part(s) with an MPN but no distributor "
                      f"data: {', '.join(unchecked[:12])}"
                      f"{'...' if len(unchecked) > 12 else ''}", refs=unchecked))
    return out


# ==========================================================================
# 5. Spec <-> board sync
# ==========================================================================
def check_spec_sync(facts, spec, size_tol_mm=2.0):
    out = []
    if not spec:
        return out
    b = spec.get("board", {})
    w, h = facts.get("size_mm", (None, None))
    if w and b.get("width_mm") and b.get("height_mm"):
        sw, sh = float(b["width_mm"]), float(b["height_mm"])
        same = (abs(sw - w) <= size_tol_mm and abs(sh - h) <= size_tol_mm)
        swapped = (abs(sh - w) <= size_tol_mm and abs(sw - h) <= size_tol_mm)
        if not (same or swapped):
            out.append(_f(FAIL, "SPEC_SIZE_MISMATCH",
                          f"spec says {sw:g} x {sh:g} mm, Edge.Cuts is "
                          f"{w:.1f} x {h:.1f} mm — update the spec or the outline"))
    if b.get("layer_count") and facts.get("copper_layers"):
        if int(b["layer_count"]) != int(facts["copper_layers"]):
            out.append(_f(FAIL, "SPEC_LAYER_MISMATCH",
                          f"spec says {b['layer_count']} layers, board has "
                          f"{facts['copper_layers']}"))
    srefs = {c["ref"] for c in spec.get("components", [])}
    brefs = {r for r, p in facts["parts"].items()
             if not p.get("mech") and not r.startswith("__")}
    only_spec, only_board = sorted(srefs - brefs), sorted(brefs - srefs)
    if only_spec or only_board:
        out.append(_f(FAIL, "SPEC_COMPONENT_MISMATCH",
                      f"components differ: only in spec {only_spec[:8]}, "
                      f"only on board {only_board[:8]}",
                      refs=only_spec + only_board))
    return out


# ==========================================================================
# 6. Electrical margins the schematic cannot show
# ==========================================================================
def _bulk_uF(facts, rail):
    total = 0.0
    refs = []
    for ref, p in facts["parts"].items():
        if not ref.startswith("C"):
            continue
        nets = set(p.get("pads", {}).values())
        if nets == {rail, "GND"}:
            v = parse_value(p.get("value", ""), "F")
            if v:
                total += v * 1e6
                refs.append(ref)
    return total, refs


def check_power(facts, cons):
    out = []
    for rail, rule in (cons or {}).get("power", {}).items():
        need_ms = rule.get("holdup_ms")
        if need_ms is None:
            continue
        v0 = float(rule.get("nominal_v", 0) or 0)
        vmin = float(rule.get("min_v", 0) or 0)
        load = float(rule.get("load_a", 0) or 0)
        if not (v0 and vmin and load) or vmin >= v0:
            out.append(_f(WARN, "HOLDUP_UNDERSPECIFIED",
                          f"{rail}: holdup_ms needs nominal_v, min_v and load_a"))
            continue
        uF, refs = _bulk_uF(facts, rail)
        t_ms = (uF * 1e-6) * (v0 - vmin) / load * 1e3
        if t_ms < float(need_ms):
            need_uF = float(need_ms) * 1e-3 * load / (v0 - vmin) * 1e6
            out.append(_f(FAIL, "HOLDUP_SHORT",
                          f"{rail}: {uF:.0f} uF ({', '.join(refs) or 'no bulk'}) "
                          f"holds {v0:g}->{vmin:g} V at {load:g} A for "
                          f"{t_ms:.2f} ms; spec asks {need_ms:g} ms "
                          f"(needs ~{need_uF:.0f} uF, or a different requirement)",
                          refs=refs))
    prot = (cons or {}).get("protection", {})
    if prot:
        clamp = prot.get("clamp_v")
        tvs = prot.get("tvs_ref")
        if clamp is None and tvs:
            mpn = facts.get("mpn_map", {}).get(tvs)
            clamp = ((facts.get("partdata") or {}).get(mpn) or {}).get("clamp_v")
        down = prot.get("downstream_max_v")
        if clamp and down:
            margin = 100.0 * (float(down) - float(clamp)) / float(down)
            if margin < 0:
                out.append(_f(FAIL, "TVS_MARGIN",
                              f"{tvs or 'TVS'} clamps at {clamp:g} V above the "
                              f"{down:g} V downstream rating", refs=[tvs] if tvs else []))
            elif margin < float(prot.get("min_margin_pct", 15)):
                out.append(_f(WARN, "TVS_MARGIN",
                              f"{tvs or 'TVS'} clamps at {clamp:g} V vs {down:g} V "
                              f"downstream — {margin:.0f}% margin (want "
                              f">= {prot.get('min_margin_pct', 15)}%)",
                              refs=[tvs] if tvs else []))
        elif tvs and not clamp:
            out.append(_f(WARN, "TVS_UNCHECKED",
                          f"{tvs}: no clamp voltage (set protection.clamp_v or "
                          f"let the distributor data supply it)", refs=[tvs]))
    return out


# ==========================================================================
# 7. Environment
# ==========================================================================
def check_env(facts, cons):
    env = C.env_profile(cons or {})
    if not env:
        return [_f(WARN, "ENV_UNDEFINED",
                   "no [env] profile — temperature, vibration, moisture and "
                   "transient class are unknown, so nothing can be derated. "
                   "Run `fluxplace intake` or add [env] to the constraints")]
    out = []
    if env.get("vibration") == "high":
        for ref, p in facts["parts"].items():
            fp = p.get("footprint", "").upper()
            if "PINHEADER" in fp or "PIN_HEADER" in fp:
                out.append(_f(WARN, "ENV_VIBRATION_HEADER",
                              f"{ref}: friction-fit header on a high-vibration "
                              f"product", refs=[ref]))
    if env.get("moisture") in ("condensing", "wet", "outdoor"):
        out.append(_f(INFO, "ENV_MOISTURE",
                      "moisture exposure declared — conformal coat, sealed "
                      "connectors and no exposed test points on the fab brief"))
    return out


# ==========================================================================
# run / summarize
# ==========================================================================
def _waived(fnd, waivers):
    for w in waivers or ():
        code, _, rx = w.partition(":")
        if fnd["code"] != code:
            continue
        if not rx or re.search(rx, fnd["msg"]) or any(
                re.search(rx, r) for r in fnd.get("refs", [])):
            return True
    return False


def run(facts, spec=None, cons=None, partdata=None, idx=None, waivers=()):
    facts = dict(facts)
    facts["partdata"] = partdata or {}
    out = []
    out += check_env(facts, cons)
    out += check_net_rules(facts, cons)
    out += check_pairs(facts, cons)
    out += check_rf(facts, cons)
    out += check_parts(facts, spec, partdata, cons, idx=idx)
    out += check_spec_sync(facts, spec)
    out += check_power(facts, cons)
    order = {FAIL: 0, WARN: 1, INFO: 2}
    out = [f for f in out if not _waived(f, waivers)]
    out.sort(key=lambda f: (order[f["level"]], f["code"], f["msg"]))
    return out


def summarize(findings, log=print):
    n = {FAIL: 0, WARN: 0, INFO: 0}
    for f in findings:
        n[f["level"]] += 1
        log(f"  {f['level']:4s} {f['code']:28s} {f['msg']}")
    log(f"review: {n[FAIL]} FAIL, {n[WARN]} WARN, {n[INFO]} INFO")
    return {"fail": n[FAIL], "warning": n[WARN], "info": n[INFO]}


# ==========================================================================
# pcbnew adapter
# ==========================================================================
def parse_stackup_text(raw):
    """[{type, name, thickness, epsilon_r}] from the (stackup ...) block."""
    m = re.search(r"\(stackup\b(.*)", raw, re.S)
    if not m:
        return []
    body = m.group(1)
    end = re.search(r"\n\t\t\)\n", body)
    if end:
        body = body[:end.start()]
    rows = []
    blocks = re.split(r'(?=\(layer\s+")', body)
    for blk in blocks:
        hm = re.match(r'\(layer\s+"([^"]+)"', blk)
        if not hm:
            continue
        tm = re.search(r'\(type\s+"([^"]+)"\)', blk)
        typ = tm.group(1) if tm else ""
        if typ not in ("copper", "core", "prepreg"):
            continue
        th = re.search(r"\(thickness\s+([\d.]+)", blk)
        er = re.search(r"\(epsilon_r\s+([\d.]+)\)", blk)
        rows.append({"name": hm.group(1) if typ == "copper" else None,
                     "type": typ,
                     "thickness": float(th.group(1)) if th else 0.0,
                     "epsilon_r": float(er.group(1)) if er else 4.4})
    return rows


def facts_from_board(board_path, mpn_map=None, plane_track_max_mm=200.0):
    """Everything the checks need, measured once."""
    import pcbnew
    from collections import defaultdict
    from . import sourcing as S
    board = pcbnew.LoadBoard(board_path)
    bb = board.GetBoardEdgesBoundingBox()
    parts, net_pads = {}, defaultdict(list)
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        pads, conn = {}, 0
        for pad in fp.Pads():
            num = pad.GetNumber()
            if not num:
                continue
            if num not in pads and num.isdigit():
                conn += 1
            pads[num] = pad.GetNetname()
            if pad.GetNetname():
                net_pads[pad.GetNetname()].append((ref, num))
        parts[ref] = {
            "value": fp.GetValue(),
            "footprint": fp.GetFPID().GetUniStringLibId(),
            "pads": pads, "connectable_pads": conn,
            "mech": not any(pads.values()),
            "tht": any(p.GetDrillSize().x > 0 for p in fp.Pads()),
        }
    tracks = {}
    layer_len = defaultdict(float)
    for t in board.GetTracks():
        n = t.GetNetname()
        if not n:
            continue
        d = tracks.setdefault(n, {"length": 0.0, "layers": defaultdict(float),
                                  "vias": 0, "segments": defaultdict(float)})
        if t.GetClass() == "PCB_VIA":
            d["vias"] += 1
            continue
        if t.GetClass() != "PCB_TRACK":
            continue
        ln = pcbnew.ToMM(t.GetLength())
        lname = board.GetLayerName(t.GetLayer())
        w = round(pcbnew.ToMM(t.GetWidth()), 4)
        d["length"] += ln
        d["layers"][lname] += ln
        d["segments"][(lname, w)] += ln
        layer_len[lname] += ln
    for n, d in tracks.items():
        d["layers"] = dict(d["layers"])
        d["segments"] = [(l, w, mm) for (l, w), mm in d["segments"].items()]
    zones = defaultdict(set)
    for z in board.Zones():
        seq = z.GetLayerSet().Seq()
        for i in range(len(seq)):
            zones[board.GetLayerName(seq[i])].add(z.GetNetname())
    planes = [l for l, nets in zones.items()
              if nets and layer_len.get(l, 0.0) <= plane_track_max_mm]
    with open(board_path) as fh:
        raw = fh.read()
    mp = {}
    path = mpn_map or S.find_map(board_path)
    if path and os.path.exists(path):
        import json
        for ref, mpn in json.load(open(path)).items():
            if not ref.startswith("_") and isinstance(mpn, str) and mpn:
                mp[ref] = mpn
    return {
        "board": board_path,
        "size_mm": (pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight())),
        "copper_layers": board.GetCopperLayerCount(),
        "parts": parts,
        "net_pads": dict(net_pads),
        "net_tracks": tracks,
        "stackup": parse_stackup_text(raw),
        "plane_layers": sorted(planes),
        "layer_track_mm": dict(layer_len),
        "mpn_map": mp,
        "mpn_map_path": path,
    }
