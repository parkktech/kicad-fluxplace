"""Part-entry gate: is every part of a spec COMPLETE and consistent BEFORE layout starts?

Found on a real board AFTER placement and routing: 69 parts whose manufacturer part number only sat in the VALUE text, an
0805 capacitor MPN on an 0603 footprint, an 0603 resistor MPN on a 1206 footprint, a 2-pin connector MPN on a 3-pin footprint.
None of that is a layout problem; all of it was knowable from the spec on day one.

A part is COMPLETE when it has an MPN in a real field (spec `mpn` or the mpn_map, not just value text), a manufacturer, a
datasheet file on disk, a `pinmap_source` (file + page) when it has more than 2 pins, a package and a footprint.  Cross-checks:
package (distributor / declared / read off the vendor series code in the MPN) vs the footprint family, MPN pin count vs footprint
pads (an exposed pad is tolerated), live stock grade.  Offline (`--no-api`, no credentials) the distributor-backed checks are
"not checked", never a pass.  DNP parts and mechanical parts (holes, fiducials, test points, logos) are skipped by rule and
counted.  Nothing here is board-specific.

Codes (FAIL unless noted): PART_MPN_MISSING, PART_MPN_IN_VALUE, PART_MFR_MISSING, PART_DATASHEET_MISSING (WARN for a 2-pin passive
when `[parts] passive_datasheet = "warn"`), PART_PINMAP_UNSOURCED, PART_PACKAGE_MISMATCH, PART_PINCOUNT_MISMATCH, PART_NO_STOCK
(NONE/RISK/LEAD; LOW is WARN), PART_GENERIC (a passive given by value only: FAIL unless `[parts] generic_passives = true` or the
answer `generic-passives=accept`), PART_FOOTPRINT_MISSING, PART_PACKAGE_MISSING (WARN; FAIL with `[parts] package = "required"`),
PART_MPN_VALUE_CONFLICT (WARN: the part number written in the value text is not the MPN on record), PART_UNKNOWABLE (the part's
component dossier found no source for a REQUIRED fact: a hard blocker, no waiver; the message names the proposed knowable replacement).

`--fix --out SPEC2` writes a COPY with only what is unambiguous (a value that a distributor lookup confirms as exactly that MPN
gets its `mpn`; `mfr` and `package` come from an exact lookup).  Everything else is a QUESTION (`part-mpn:<value>`), never a guess.
"""
import copy
import json
import os
import re

from . import questions as QS

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"
CODES = ("PART_MPN_MISSING", "PART_MPN_IN_VALUE", "PART_MFR_MISSING", "PART_DATASHEET_MISSING", "PART_PINMAP_UNSOURCED",
         "PART_PACKAGE_MISMATCH", "PART_PINCOUNT_MISMATCH", "PART_NO_STOCK", "PART_GENERIC", "PART_FOOTPRINT_MISSING",
         "PART_PACKAGE_MISSING", "PART_MPN_VALUE_CONFLICT", "PART_UNKNOWABLE")
_MECH_REF = re.compile(r"^(MH|MK|H|TP|FID|NT|LOGO|G|MP|SH)\d", re.I)
_MECH_FP = re.compile(r"MountingHole|Fiducial|TestPoint|Logo|Symbol:|NetTie|Mounting_|Standoff", re.I)
_PASSIVE_REF = re.compile(r"^(R|C|L|FB|RN)\d", re.I)

# Vendor series codes that embed the chip size: (regex on the MPN, group -> imperial size or a metric/other code, table)
_METRIC = {"1005": "0402", "1608": "0603", "2012": "0805", "2520": "1008", "3216": "1206",
           "3225": "1210", "4532": "1812", "4520": "1808", "5025": "2010", "6332": "2512", "1005M": "0402"}
_IMPERIAL = {"0201", "0402", "0603", "0805", "1206", "1210", "1812", "2220"}
_MURATA = {"03": "0201", "15": "0402", "18": "0603", "21": "0805", "31": "1206", "32": "1210", "43": "1812", "55": "2220"}
_KOA = {"1E": "0402", "1J": "0603", "2A": "0805", "2B": "1206", "2E": "1210", "3A": "2010", "3B": "2512"}
_PANA = {"2": "0402", "3": "0603", "6": "0805", "8": "1206", "14": "1210", "12": "2010", "1": "2512"}
_SAMSUNG = {"03": "0201", "05": "0402", "10": "0603", "21": "0805", "31": "1206", "32": "1210"}
CHIP_SERIES = [
    (re.compile(r"^(?:RC|AC|CC|RT|RV|RL|RM|YC|YR|PE)(\d{4})", re.I), lambda m: m.group(1) if m.group(1) in
     _METRIC.values() else None, "Yageo/Vishay series code"),
    (re.compile(r"^(?:CRCW|TNPW|CRG|CRHV|MCS|MCT|MCW|ERA-?|RGH)(\d{4})", re.I), lambda m: m.group(1), "Vishay/Panasonic series code"),
    (re.compile(r"^G[RCJ][MJQ]?(\d{2})\d", re.I), lambda m: _MURATA.get(m.group(1)), "Murata size code"),
    (re.compile(r"^CL(\d{2})[A-Z]", re.I), lambda m: _SAMSUNG.get(m.group(1)), "Samsung size code"),
    (re.compile(r"^C(\d{4})(?:X\d[A-Z]|C0G|NP0|JB|CH)", re.I), lambda m: _METRIC.get(m.group(1)), "TDK metric size code"),
    (re.compile(r"^C(\d{4})[CPXUZ]\d{3}", re.I), lambda m: m.group(1) if m.group(1) in _IMPERIAL else None, "KEMET size code"),
    (re.compile(r"^RK73[A-Z]\d([A-Z]{1,2})", re.I), lambda m: _KOA.get(m.group(1).upper()[:2]), "KOA size code"),
    (re.compile(r"^ERJ-?(\d{1,2})", re.I), lambda m: _PANA.get(m.group(1)), "Panasonic size code"),
]


def _f(level, code, msg, refs=()):
    return {"level": level, "code": code, "msg": msg, "refs": sorted(set(refs))}


# ------------------------------------------------------------------ settings
def settings(cons):
    p = dict((cons or {}).get("parts") or {})
    return {"generic": p.get("generic_passives"), "passive_datasheet": str(p.get("passive_datasheet", "fail")).lower(),
            "package": str(p.get("package", "warn")).lower(), "need": int(p.get("stock_need", 10)),
            "pinmap_exempt": tuple(p.get("pinmap_exempt", ())), "waive": list(p.get("waive", ())),
            "skip_variants": set(p.get("skip_variants", ()))}


# ------------------------------------------------------------------ value / MPN text
_ELEC = re.compile(r"""^(?:[<>~±]?\d+(?:[.,]\d+)?[a-zA-Zµμ]{0,4}\d{0,3}(?:/\d+(?:\.\d+)?[a-zA-Z]*)?%?
                       |\d+[RrKkMmGgUuNnPp]\d*|\d+[Vv]\d+|\d+(?:[.,]\d+)?%|\d+(?:[.,]\d+)?[a-zA-Zµμ]{1,3}(?:/\d+[a-zA-Z]+)?)$""", re.X)
_MPN_CH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/+#]*$")


def is_electrical_value(tok):
    """'10k' '4.7uH' '100nF' '0R' '1%' '3V3' '0603' — a value, not a part number."""
    t = (tok or "").strip().strip("(),;")
    return bool(t) and bool(_ELEC.match(t))


def mpn_like(tok):
    """True when one token reads like a manufacturer part number (letters+digits, or a long hyphenated numeric one)."""
    t = (tok or "").strip().strip("(),;:")
    if len(t) < 5 or not _MPN_CH.match(t) or is_electrical_value(t):
        return False
    digits = sum(c.isdigit() for c in t)
    letters = sum(c.isalpha() for c in t)
    if letters and digits >= 2:
        return True
    return not letters and digits >= 8 and "-" in t


def value_tokens(value):
    return [t for t in re.split(r"[\s,;]+", re.sub(r"[()\[\]]", " ", value or "")) if t]


def mpn_in_value(value):
    """The first token of `value` that reads like an MPN, or None."""
    for t in value_tokens(value):
        if mpn_like(t):
            return t.strip(":,;")
    return None


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _value_conflict(tok, mpn):
    """True when the part number written in the value text is a different part from the MPN on record.  A suffix variant
    ('TPS628502-Q1' for TPS628502QDRLRQ1) is the same part: the leading stem of an alphanumeric token may prefix the MPN."""
    t, m = _norm(tok), _norm(mpn)
    if t in m or m in t:
        return False
    if re.search(r"[A-Za-z]", tok):
        stem = _norm(re.split(r"-", tok)[0])
        if len(stem) >= 5 and (m.startswith(stem) or stem.startswith(m)):
            return False
    return True


def _ref_prefix(ref):
    m = re.match(r"^[A-Za-z]+", ref or "")
    return m.group(0).upper() if m else ""


# ------------------------------------------------------------------ footprint facts
def fp_name(c):
    return (c.get("fp") or "").split(":")[-1]


def fp_pads_from_name(name):
    """Pad count a footprint name states: 'SOT-23-5' 5, 'SOIC-8_...' 8, '..._1x03_P3.00mm' 3, '2x05' 10, 'xx-24pin'.  None if silent."""
    from . import review as R
    if not name:
        return None
    m = re.search(r"(?<![0-9.])(\d{1,2})x(\d{1,2})(?![0-9.])", name)
    if m and "mm" in name:
        return int(m.group(1)) * int(m.group(2))
    m = re.search(r"[_\-](\d{1,3})\s*(?:pin|pos|p|pad)s?(?![a-z0-9])", name, re.I)
    if m:
        return int(m.group(1))
    pins = {p for _, p in R.package_key(name) if isinstance(p, int)}
    return pins.pop() if len(pins) == 1 else None


def chip_from_fp(name):
    from . import review as R
    return {s for f, s in R.package_key(name) if f == "CHIP"}


def chip_from_mpn(mpn):
    """(imperial size, why) from a vendor series code in the MPN, or (None, None)."""
    for rx, fn, why in CHIP_SERIES:
        m = rx.match(mpn or "")
        if m:
            s = fn(m)
            if s:
                return s, why
    return None, None


# ------------------------------------------------------------------ datasheets
def project_dir(ref):
    """The directory holding the .kicad_pro of `ref` (a board / project file), looked for upward; None when there is none."""
    d = os.path.dirname(os.path.abspath(ref)) if ref else None
    while d and d != os.path.dirname(d):
        if any(f.endswith(".kicad_pro") for f in os.listdir(d)):
            return d
        d = os.path.dirname(d)
    return None


def resolve_dir(path, anchors=(), strict=False, project=None):
    """A relative datasheet dir: beside/above each anchor file first (constraints dir, board dir, project root: the project's own,
    whatever the cwd), then as given from the cwd. `strict`: SystemExit when it resolves nowhere (the owner set it, so it must exist)."""
    if not path:
        return path
    if os.path.isabs(path):
        if strict and not os.path.isdir(path):
            raise SystemExit(f"[docs] datasheets = {path!r} is not a directory")
        return path
    for a in anchors:
        d = os.path.dirname(os.path.abspath(a)) if a else None
        while d and d != os.path.dirname(d):
            if os.path.isdir(os.path.join(d, path)):
                return os.path.join(d, path)
            d = os.path.dirname(d)
    pd = project_dir(project)
    if pd and os.path.isdir(os.path.join(pd, path)):
        return os.path.join(pd, path)
    if os.path.isdir(path):
        return os.path.abspath(path)
    if strict:
        tried = [os.path.join(os.path.dirname(os.path.abspath(a)), path) for a in anchors if a] + ([os.path.join(pd, path)] if pd else [])
        raise SystemExit(f"[docs] datasheets = {path!r} not found; tried {', '.join(tried) or 'no anchor'} (and above them), nor from {os.getcwd()}")
    return path


def datasheet_file(mpn, ds_dir, comp=None, man=None):
    """Path of the datasheet on disk for `mpn`, or None."""
    from . import partdocs as PD
    man = man if man is not None else (PD.load_manifest(ds_dir) if ds_dir else {})
    cands = []
    if comp and comp.get("datasheet"):
        cands.append(comp["datasheet"])
    ent = man.get(mpn)
    if ent and ent.get("file"):
        cands.append(ent["file"])
    if ds_dir and mpn:
        cands += [PD._safe_name(mpn) + ".pdf", mpn + ".pdf"]
    for c in cands:
        for p in ([c] if os.path.isabs(c) else [os.path.join(ds_dir or "", c), c]):
            if os.path.isfile(p):
                return p
    return None


def parse_source(src):
    """'X.pdf#p3', 'X.pdf#p16 (ratings), #p14-15' -> (file, [pages]); 'kicad:LIB:SYM' -> ('kicad', [])."""
    if not isinstance(src, str) or not src.strip():
        return None, []
    s = src.strip()
    if s.lower().startswith("kicad:"):
        return "kicad", []
    m = re.search(r"(\S+?\.pdf)", s, re.I)
    if not m:
        return None, []
    pages = []
    for a, b in re.findall(r"#?p(\d+)(?:\s*-\s*(\d+))?", s[m.end():]):
        pages += list(range(int(a), int(b or a) + 1))
    return m.group(1), pages


# ------------------------------------------------------------------ the check
def pin_count(c, facts, spec_pins):
    """(count, source): the board pad count, else the pads the spec uses/maps, else the footprint name."""
    parts = (facts or {}).get("parts") or {}
    p = parts.get(c.get("ref"))
    if p:
        n = p.get("pads_total") or p.get("pads")
        n = len(n) if isinstance(n, (dict, list)) else n
        if n:
            return int(n), "board"
    n = max(len(spec_pins.get(c.get("ref"), ())), len({str(v) for v in (c.get("pinmap") or {}).values()}))
    nm = fp_pads_from_name(fp_name(c))
    if nm:
        return nm, "footprint name"
    return (n, "spec nets") if n else (0, None)


def _pin_map(spec):
    out = {}
    for conns in (spec.get("nets") or {}).values():
        for ref, pin in conns:
            out.setdefault(ref, set()).add(str(pin))
    return out


def is_skipped(c, facts, st):
    """-> reason or None: parts the gate skips by RULE."""
    if c.get("dnp"):
        return "dnp"
    if c.get("variant") and c.get("variant") in st["skip_variants"]:
        return "variant"
    if _MECH_REF.match(c.get("ref", "")) or _MECH_FP.search(fp_name(c)) or _MECH_FP.search(c.get("lib") or ""):
        return "mechanical"
    return None


def _dossier(mpn, man):
    """The part's component dossier (dossier.py) for the datasheet on record, or None (not made: the gate cannot say it is unknowable)."""
    from . import dossier as DO
    ent = man.get(mpn)
    return DO.load(mpn, ent.get("sha256")) if isinstance(ent, dict) and ent.get("sha256") else None


def run(spec, cons=None, facts=None, mpn_map=None, ds_dir=None, data=None, stock=None, answers=None, session=None):
    """-> {"findings": [...], "rows": {ref: {...}}, "skipped": {reason: [refs]}, "unchecked": {what: n}, "generic": [refs],
    "values": {token: [refs]}}.  `data` = {mpn: distributor row|None} (partdata.fetch), `stock` = {mpn: {verdict, stock}}
    (sourcing.check) — None means offline: those checks are counted as not checked."""
    from . import partdocs as PD, review as R
    st = settings(cons)
    mpn_map = mpn_map or (facts or {}).get("mpn_map") or {}
    man = PD.load_manifest(ds_dir) if ds_dir else {}
    spec_pins = _pin_map(spec)
    parts = (facts or {}).get("parts") or {}
    out, rows, skipped = [], {}, {}
    unchecked = {"distributor package/pins": 0, "stock": 0}
    generic, values = [], {}
    lookup_on = data is not None
    for c in spec.get("components", []):
        ref = c.get("ref", "?")
        why = is_skipped(c, facts, st)
        if why:
            skipped.setdefault(why, []).append(ref)
            continue
        value, fp = c.get("value", ""), fp_name(c)
        mpn = c.get("mpn") or mpn_map.get(ref)
        msrc = "spec" if c.get("mpn") else ("mpn_map" if mpn_map.get(ref) else None)
        passive = bool(_PASSIVE_REF.match(ref))
        npins, nsrc = pin_count(c, facts, spec_pins)
        row = rows[ref] = {"ref": ref, "value": value, "mpn": mpn, "mpn_source": msrc, "fp": fp, "pins": npins}
        # ---- 1. MPN in a real field
        if not mpn:
            tok = mpn_in_value(value)
            if tok:
                d = (data or {}).get(tok) if lookup_on else None
                note = (f"lookup says {d.get('manufacturer') or '?'}, {d.get('package') or '?'}" if d
                        else ("lookup found nothing" if lookup_on else "not looked up"))
                out.append(_f(FAIL, "PART_MPN_IN_VALUE", f"{ref}: value '{value}' looks like an MPN ({tok}) with no MPN record; {note}",
                              refs=[ref]))
                values.setdefault(tok, []).append(ref)
                row["value_mpn"] = tok
            elif passive and is_electrical_value(next(iter(value_tokens(value)), "")):
                generic.append(ref)
            else:
                out.append(_f(FAIL, "PART_MPN_MISSING", f"{ref} ({value}): no manufacturer part number", refs=[ref]))
        else:
            # value text names a different part number than the MPN on record
            for t in value_tokens(value):
                if mpn_like(t) and _value_conflict(t, mpn):
                    out.append(_f(WARN, "PART_MPN_VALUE_CONFLICT",
                                  f"{ref}: value text names {t} but the MPN on record is {mpn}", refs=[ref]))
                    break
        d = (data or {}).get(mpn) if (lookup_on and mpn) else None
        exact = bool(d) and _norm(d.get("matched")) == _norm(mpn)
        # ---- 2. manufacturer
        mfr = c.get("mfr") or c.get("manufacturer")
        row["mfr"] = mfr
        if mpn and not mfr:
            hint = f" (lookup: {d['manufacturer']})" if d and d.get("manufacturer") else ""
            out.append(_f(FAIL, "PART_MFR_MISSING", f"{ref} {mpn}: no manufacturer recorded{hint}", refs=[ref]))
        # ---- 3. datasheet on disk
        if mpn:
            f = datasheet_file(mpn, ds_dir, c, man)
            row["datasheet"] = f
            if not f:
                lvl = WARN if (passive and npins <= 2 and st["passive_datasheet"] == "warn") else FAIL
                out.append(_f(lvl, "PART_DATASHEET_MISSING", f"{ref} {mpn}: no datasheet file on disk", refs=[ref]))
            dos = _dossier(mpn, man)
            if dos and not dos.get("knowable", True):
                pick = (dos.get("replacement_choice") or {}).get("choice")
                rep = next((r for r in dos.get("replacement") or [] if r.get("knowable") and (r["mpn"] == pick or pick is None)), None)
                out.append(_f(FAIL, "PART_UNKNOWABLE", f"{ref} {mpn}: no source gives {', '.join(dos['missing'])} (datasheet, page pictures, "
                              f"the family's other documents, distributor) - a part that cannot be fully known is not used; "
                              + (f"proposed replacement {rep['mpn']} ({'drop-in' if rep['drop_in'] else 'differs: ' + '; '.join(rep['differs'][:2])})"
                                 if rep else "no knowable replacement found yet (fluxplace datasheets proposes one)"), refs=[ref]))
        # ---- 4. pinmap source for > 2 pins
        if npins > 2 and not any(re.fullmatch(p.replace("*", ".*"), ref) for p in st["pinmap_exempt"]):
            sfile, pages = parse_source(c.get("pinmap_source"))
            if not sfile:
                out.append(_f(FAIL, "PART_PINMAP_UNSOURCED",
                              f"{ref} {mpn or value}: {npins} pins and no pinmap_source (datasheet file#page of the pin table)",
                              refs=[ref]))
            elif sfile != "kicad":
                here = datasheet_file(os.path.basename(sfile)[:-4], ds_dir) or (os.path.join(ds_dir or "", os.path.basename(sfile))
                                                                               if os.path.isfile(os.path.join(ds_dir or "", os.path.basename(sfile))) else None)
                if not here and not os.path.isfile(sfile):
                    out.append(_f(FAIL, "PART_PINMAP_UNSOURCED", f"{ref}: pinmap_source file {sfile} is not on disk", refs=[ref]))
                elif not pages:
                    out.append(_f(FAIL, "PART_PINMAP_UNSOURCED", f"{ref}: pinmap_source {sfile} names no page (#pN)", refs=[ref]))
        # ---- 5. footprint, package
        if not fp:
            out.append(_f(FAIL, "PART_FOOTPRINT_MISSING", f"{ref} ({value}): no footprint", refs=[ref]))
        elif ref in parts and (parts[ref].get("footprint") or "") != fp:
            out.append(_f(WARN, "PART_FOOTPRINT_MISMATCH", f"{ref}: spec footprint {fp} but the board has {parts[ref].get('footprint')}",
                          refs=[ref]))
        pkg = c.get("package") or (d or {}).get("package")
        row["package"] = pkg
        if not c.get("package") and not (d or {}).get("package"):
            out.append(_f(FAIL if st["package"] == "required" else WARN, "PART_PACKAGE_MISSING",
                          f"{ref}: no package recorded", refs=[ref]))
        # ---- 6. cross-checks
        if fp and mpn:
            if pkg:
                verdict, detail = R.packages_agree(pkg, fp)
                if verdict in ("family", "pins"):
                    out.append(_f(FAIL, "PART_PACKAGE_MISMATCH",
                                  f"{ref} {mpn}: package '{pkg}' but footprint '{fp}' ({detail})", refs=[ref]))
                elif verdict == "unknown" and lookup_on:
                    unchecked["distributor package/pins"] += 1
            if passive:
                size, how = chip_from_mpn(mpn)
                fs = chip_from_fp(fp)
                if size and fs and size not in fs and not any(x.get("code") == "PART_PACKAGE_MISMATCH" and ref in x["refs"] for x in out):
                    out.append(_f(FAIL, "PART_PACKAGE_MISMATCH",
                                  f"{ref} {mpn}: the MPN's {how} says {size} but the footprint is {'/'.join(sorted(fs))} ({fp})",
                                  refs=[ref]))
            want = None
            if d and d.get("pins"):
                want = (int(d["pins"]), "distributor pin/position count")
            elif d and R._pins_from_pkg(d.get("package") or ""):
                want = (R._pins_from_pkg(d["package"]), "distributor package")
            elif c.get("package") and R._pins_from_pkg(c["package"]):
                want = (R._pins_from_pkg(c["package"]), "declared package")
            if want and npins:
                n, src = want
                ep = re.search(r"(?<![A-Za-z])EP(?![a-z])|PAD|THERMAL|EXPOSED|QFN|DFN|SON|LGA|MLP|MLF|LFCSP|PowerPAK|HSOP|HTSSOP|HVSSOP", fp, re.I)
                pads = ((facts or {}).get("parts") or {}).get(ref, {}).get("pads")
                names = len(pads) if isinstance(pads, (dict, list)) and pads else None   # a pin drawn as several copper pieces is one pad NAME
                ok_counts = (n, n + 1) if ep else (n,)
                if npins not in ok_counts and names not in ok_counts:   # an exposed pad is one extra pad on the IC families that have one
                    out.append(_f(FAIL, "PART_PINCOUNT_MISMATCH",
                                  f"{ref} {mpn}: {src} says {n} but the footprint {fp} has {npins} pads ({nsrc})", refs=[ref]))
            elif npins and not want and lookup_on:
                unchecked["distributor package/pins"] += 1
        elif mpn and not lookup_on:
            unchecked["distributor package/pins"] += 1
        # ---- 7. stock
        if mpn:
            s = (stock or {}).get(mpn) if stock is not None else None
            if s:
                v = s.get("verdict")
                row["stock"] = f"{v} {s.get('stock', 0)}"
                if v in ("NONE", "RISK", "LEAD"):
                    out.append(_f(FAIL, "PART_NO_STOCK", f"{ref} {mpn}: stock grade {v} ({s.get('stock', 0)} on hand)", refs=[ref]))
                elif v == "LOW":
                    out.append(_f(WARN, "PART_NO_STOCK", f"{ref} {mpn}: low stock ({s.get('stock', 0)})", refs=[ref]))
                elif v == "ERR":
                    unchecked["stock"] += 1
            else:
                unchecked["stock"] += 1
    # ---- generic passives: a decision, not a default
    if generic:
        acc = st["generic"]
        if session is not None and acc is None:
            acc = session.ask("generic-passives", "parts",
                              f"{len(generic)} passive(s) are given by value only, no MPN ({', '.join(sorted(generic)[:6])}"
                              f"{'...' if len(generic) > 6 else ''})",
                              {"require-mpn": "each needs a manufacturer part number before layout (FAIL PART_GENERIC)",
                               "accept": "a generic passive by value/tolerance/package is acceptable here (INFO)"},
                              "require-mpn", "constraints [parts] generic_passives = true|false, or --answer generic-passives=accept"
                              ) == "accept"
        for ref in generic:
            out.append(_f(INFO if acc else FAIL, "PART_GENERIC",
                          f"{ref} ({rows[ref]['value']}): generic passive, no MPN" + ("" if acc else " (not accepted: constraints [parts] generic_passives)"),
                          refs=[ref]))
    from . import elec as EL
    out += EL.check_spec(spec, cons, ds_dir, mpn_map)          # datasheet-driven electrical checks, before placement
    order = {FAIL: 0, WARN: 1, INFO: 2}
    out = [x for x in out if x["code"] == "PART_UNKNOWABLE" or not _waived(x, st["waive"])]     # an unknowable part has no waiver
    out.sort(key=lambda x: (order[x["level"]], x["code"], x["msg"]))
    return {"findings": out, "rows": rows, "skipped": skipped, "unchecked": unchecked, "generic": sorted(generic),
            "values": values, "lookup": lookup_on, "stock_on": stock is not None}


def _waived(f, waive):
    for w in waive:
        code, _, rx = w.partition(":")
        if f["code"] == code and (not rx or re.search(rx, f["msg"]) or any(re.search(rx, r) for r in f["refs"])):
            return True
    return False


def counts(findings):
    n = {}
    for f in findings:
        n.setdefault(f["code"], {FAIL: 0, WARN: 0, INFO: 0})[f["level"]] += 1
    return n


def totals(findings):
    t = {FAIL: 0, WARN: 0, INFO: 0}
    for f in findings:
        t[f["level"]] += 1
    return t


# ------------------------------------------------------------------ lookups (network; mocked in tests)
def fetch_data(mpns, cache_dir=None, refresh=False, log=lambda m: None):
    """{mpn: distributor row|None} via partdata (DigiKey, then Mouser).  No credentials -> None (offline)."""
    from . import partdata as PDA
    from .models import credentials
    creds = credentials()
    if not ("DIGIKEY_CLIENT_ID" in creds or "MOUSER_API_KEY" in creds):
        return None
    return PDA.fetch(sorted(set(mpns)), cache_dir=cache_dir, refresh=refresh, log=log, creds=creds)


def fetch_stock(mpns, need=10, cache_dir=None, refresh=False, log=lambda m: None, refs=None, qty=None):
    """{mpn: {verdict, stock, required, per_board, chosen, ext}} via sourcing.check; None when offline.  With `qty` (the build
    quantity) and `refs` ({mpn: [ref, ...]}) each MPN is graded against per-board count x qty, not the flat `need`."""
    from . import sourcing as S
    by = {m: list((refs or {}).get(m) or []) for m in sorted(set(mpns))}
    if qty:
        by = {m: (r or ["?"]) for m, r in by.items()}
    rep, _ = S.check(by, need=need, cache_dir=cache_dir, refresh=refresh, log=log, qty=int(qty) if qty else None)
    return ({m: {"verdict": r["verdict"], "stock": r["stock"], "required": r.get("required"), "per_board": r.get("per_board"),
                 "chosen": r.get("chosen"), "ext": r.get("chosen_ext")} for m, r in rep.items()} if rep else None)


def build_quantity(cons):
    """[bom] quantity_target (boards to build) as an int >= 1, else None (legacy flat stock_need)."""
    q = ((cons or {}).get("bom") or {}).get("quantity_target")
    try:
        return int(q) if q and int(q) >= 1 else None
    except (TypeError, ValueError):
        return None


def evaluate(spec, cons=None, facts=None, mpn_map=None, ds_dir=None, api=True, cache_dir=None, session=None, data=None, stock=None,
             extra_mpns=(), qty=None):
    """Look up (unless offline), then run().  `data`/`stock` inject results (tests)."""
    st = settings(cons)
    from . import redact as _RD
    _RD.configure(cons)
    mm = mpn_map or (facts or {}).get("mpn_map") or {}
    mpns = {c.get("mpn") or mm.get(c.get("ref")) for c in spec.get("components", []) if not c.get("dnp")}
    mpns |= {mpn_in_value(c.get("value", "")) for c in spec.get("components", [])
             if not (c.get("mpn") or mm.get(c.get("ref"))) and not c.get("dnp")}
    mpns = {m for m in mpns | set(extra_mpns) if m}
    if api and data is None and mpns:
        data = fetch_data(mpns, cache_dir=cache_dir)
        if data is not None and stock is None:
            refs = {}
            for c in spec.get("components", []):
                m = c.get("mpn") or mm.get(c.get("ref"))
                if m and not c.get("dnp"):
                    refs.setdefault(m, []).append(c.get("ref"))
            stock = fetch_stock(mpns, need=st["need"], cache_dir=cache_dir, refs=refs,
                                qty=qty if qty is not None else build_quantity(cons))
    elif not api:
        data = stock = None
    res = run(spec, cons, facts=facts, mpn_map=mm, ds_dir=ds_dir, data=data, stock=stock, session=session)
    res["data"] = data
    res["stock_data"] = stock
    return res


# ------------------------------------------------------------------ fix
def apply_fix(spec, res, data, session):
    """-> (spec copy, [what was written]).  Only what is unambiguous; the rest is a question."""
    sp = copy.deepcopy(spec)
    done = []
    by_ref = {c.get("ref"): c for c in sp.get("components", [])}
    for tok, refs in sorted(res["values"].items()):
        d = (data or {}).get(tok)
        exact = bool(d) and _norm(d.get("matched")) == _norm(tok)
        found = (f"value '{tok}' (on {', '.join(sorted(refs)[:6])}{'...' if len(refs) > 6 else ''}) looks like an MPN; "
                 + (f"lookup says {d.get('manufacturer') or '?'}, {d.get('package') or '?'}"
                    + ("" if exact else f" (matched {d.get('matched')}, not exactly {tok})") if d else "no lookup result"))
        qid = f"part-mpn:{tok}"
        if exact and not session.cli.get(qid):
            choice = "record"
        else:
            choice = session.ask(qid, "parts", found,
                                 {"record": f"{tok} becomes the MPN of those parts", "skip": "leave them without an MPN (still FAIL)"},
                                 "skip", "the owner confirms the value is the manufacturer part number")
        if choice == "record":
            for r in refs:
                by_ref[r]["mpn"] = tok
                done.append(f"{r}.mpn={tok}")
    for ref, row in res["rows"].items():
        c = by_ref.get(ref)
        mp = c.get("mpn") or row.get("mpn")
        d = (data or {}).get(mp or "")
        if not c or not d or _norm(d.get("matched")) != _norm(mp):
            continue
        if d.get("manufacturer") and not (c.get("mfr") or c.get("manufacturer")):
            c["mfr"] = d["manufacturer"]
            done.append(f"{ref}.mfr")
        if d.get("package") and not c.get("package"):
            c["package"] = d["package"]
            done.append(f"{ref}.package")
    return sp, done


# ------------------------------------------------------------------ reporting
def _spread(findings, top):
    """worst `top` findings, at most ceil(top/3) per code so one code cannot fill the list (FAIL first)."""
    cap = max(2, -(-top // 3))
    seen, pick = {}, []
    for x in findings:
        if seen.get(x["code"], 0) < cap:
            seen[x["code"]] = seen.get(x["code"], 0) + 1
            pick.append(x)
    return pick[:top]


def format_report(res, title, top=10, full=False, path=None):
    f = res["findings"]
    t = totals(f)
    lines = [f"{title}: {'FAIL' if t[FAIL] else 'ok'} - {t[FAIL]} FAIL, {t[WARN]} WARN, {t[INFO]} INFO over {len(res['rows'])} part(s)"
             + (f"; skipped {', '.join(f'{len(v)} {k}' for k, v in sorted(res['skipped'].items()))}" if res["skipped"] else "")]
    unc = [f"{k}: {n} not checked" for k, n in res["unchecked"].items() if n]
    if not res["lookup"]:
        unc = ["distributor data and stock: NOT CHECKED (offline / --no-api) - not a pass"]
    elif not res["stock_on"]:
        unc = [u for u in unc if not u.startswith("stock")] + ["stock: NOT CHECKED"]
    if unc:
        lines.append("  " + "; ".join(unc))
    cn = counts(f)
    if cn:
        lines.append("counts: " + "  ".join(f"{c}={sum(v.values())}" + ("" if v[FAIL] else "(w)") for c, v in
                                           sorted(cn.items(), key=lambda kv: (not kv[1][FAIL], -sum(kv[1].values())))))
    shown = f if full else _spread(f, top)
    lines += [f"  {x['level']:4s} {x['code']} {x['msg']}"[:230] for x in shown]
    if len(f) > len(shown):
        lines.append(f"  ... {len(f) - len(shown)} more")
    if path:
        lines.append(f"full report: {path}")
    return "\n".join(lines)


def write_report(res, stem, extra=None):
    txt, js = stem + ".parts.txt", stem + ".parts.json"
    try:
        with open(txt, "w") as fh:
            fh.write(format_report(res, "parts", full=True) + "\n")
        with open(js, "w") as fh:
            json.dump({"counts": counts(res["findings"]), "totals": totals(res["findings"]), "skipped": res["skipped"],
                       "unchecked": res["unchecked"], "findings": res["findings"], **(extra or {})}, fh, indent=1)
    except OSError:
        return None
    return txt


def review_findings(spec, cons, facts, ds_dir, partdata=None):
    """For `review`: the PART_* findings.  Distributor-backed checks run only on the review's own fetched rows (`partdata`); the
    codes the review raises itself (MPN_MISSING, DATASHEET_MISSING) and bare package-missing notes are left out."""
    res = run(spec, cons, facts=facts, ds_dir=ds_dir, data=partdata or None)
    from . import elec as EL
    skip = {"PART_MPN_MISSING", "PART_DATASHEET_MISSING", "PART_PACKAGE_MISSING"} | set(EL.CODES)     # elec codes: review's own check_elec
    return [x for x in res["findings"] if x["code"] not in skip and x["level"] != INFO]


def autogate(spec_path, cons=None, board=None, cmd="", ds_dir=None, mpn_map_path=None):
    """The gate other commands call at spec load: offline (no network), one summary line.  Stops (SystemExit) on a FAIL only when
    `[parts] enforce = true`; `pipeline` has its own hard stage (`ready`).  -> the findings."""
    try:
        spec = json.load(open(spec_path))
    except (OSError, ValueError):
        return []
    mm = {}
    if mpn_map_path and os.path.exists(mpn_map_path):
        mm = {k: v for k, v in json.load(open(mpn_map_path)).items() if not k.startswith("_") and isinstance(v, str) and v}
    facts = None
    if board and os.path.exists(board):
        try:
            from . import review as R
            facts = R.facts_from_board(board, mpn_map=mpn_map_path)
            mm = mm or facts.get("mpn_map") or {}
        except Exception:                                    # noqa: BLE001 - advisory
            facts = None
    ds = resolve_dir(ds_dir or ((cons or {}).get("docs") or {}).get("datasheets"), [spec_path, board])
    res = run(spec, cons, facts=facts, mpn_map=mm, ds_dir=ds)
    t = totals(res["findings"])
    cn = counts(res["findings"])
    top = ", ".join(f"{c}={sum(v.values())}" for c, v in sorted(cn.items(), key=lambda kv: -kv[1][FAIL])[:4] if v[FAIL])
    print(f"parts gate ({cmd}): {t[FAIL]} FAIL, {t[WARN]} WARN (offline; `fluxplace parts` for detail)" + (f" - {top}" if top else ""))
    if t[FAIL] and (cons or {}).get("parts", {}).get("enforce"):
        raise SystemExit(f"{cmd}: refusing to continue - {t[FAIL]} part-entry FAIL ([parts] enforce = true). Run `fluxplace parts`.")
    return res["findings"]
