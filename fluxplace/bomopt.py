"""Part-count optimization: fewer parts, fewer distinct part numbers.

Pure Python (no pcbnew, no network). Every finding is a *candidate* — the
analysis never claims two parts are interchangeable, only that a human should
check ratings and tolerances before merging them.

    analyze(components, nets, mpn_map=None, cons=None) -> {"summary", "findings"}

Constraints, all optional, in a `[bom]` table:

    max_parts, max_unique, max_dnp   budgets (FAIL, FAIL, WARN)
    array_min = 4                    resistors on a shared net before an array is suggested
    merge_tolerance = 0.1            value spread that counts as "the same value"
    exempt = ["MH*"]                 refs ignored entirely
    keep_values = ["R12"]            refs whose value must not be merged
    keep_links = ["R40"]             0-ohm links that must stay
"""
import fnmatch
import math
import re

__all__ = ["analyze", "parse_value", "kind_of", "package_of", "SAVINGS_CODES"]

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"

_MECH_REF = re.compile(r"^(MH|MK|H|FID|TP)\d", re.I)
_MECH_FP = re.compile(r"MountingHole|Fiducial|TestPoint|Logo", re.I)
_KIND_RE = re.compile(r"^([A-Za-z]+)\d")
_CHIP_RE = re.compile(r"(?<!\d)(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)(?!\d)")
_PASSIVE = ("R", "C", "L", "FB")
_KINDS = ("R", "C", "L", "FB", "D", "Q", "U", "J")

_MAG = {
    "R": {"R": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "m": 1e-3, "G": 1e9},
    "C": {"p": 1e-12, "P": 1e-12, "n": 1e-9, "N": 1e-9, "u": 1e-6, "U": 1e-6,
          "m": 1e-3},
    "L": {"p": 1e-12, "n": 1e-9, "N": 1e-9, "u": 1e-6, "U": 1e-6, "m": 1e-3},
}
_UNIT = {"R": ("", "ohm", "\u03c9"), "C": ("", "f"), "L": ("", "h")}
_VAL_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?|\.\d+)\s*([A-Za-z]?)(\d*)\s*(ohm|Ω|[FfHh])?(?=$|[\s%,/(@±+])",
    re.I)


def parse_value(value, kind):
    """Component value string -> float in base units (ohm/F/H), or None.

    Handles "10k", "10 k", "4.7k", "4k7", "100R", "0R", "1M", "47k 1%",
    "100nF", "4u7", "2.2uH". kind is "R", "C" or "L"; anything else, or
    anything unparseable, gives None."""
    kind = (kind or "").upper()
    if kind not in _MAG or not isinstance(value, str):
        return None
    t = value.replace("µ", "u").replace("μ", "u")
    m = _VAL_RE.match(t)
    if not m:
        return None
    num, mag, frac, unit = m.groups()
    unit = (unit or "")
    # the magnitude letter is optional; a letter that is really a unit
    # ("100F", "10H") is re-read as the unit
    if mag and mag not in _MAG[kind]:
        if frac or unit or mag.lower() not in _UNIT[kind]:
            return None
        unit, mag = mag, ""
    if unit.lower() not in _UNIT[kind]:
        return None
    scale = _MAG[kind][mag] if mag else 1.0
    if frac:
        if "." in num:
            return None
        num = f"{num}.{frac}"
    return float(num) * scale


def kind_of(ref):
    m = _KIND_RE.match(ref or "")
    if not m:
        return "other"
    p = m.group(1).upper()
    if p in _KINDS:
        return p
    return "other"


def package_of(fp):
    """Chip size ("0402") when the footprint name carries one, an array tag
    for resistor arrays, else the footprint name itself."""
    fp = fp or ""
    if re.search(r"Array|_Convex|_Concave", fp, re.I):
        return fp
    m = _CHIP_RE.search(fp)
    return m.group(1) if m else fp


def _is_chip(pkg):
    return bool(_CHIP_RE.fullmatch(pkg or ""))


def _glob_any(ref, patterns):
    return any(fnmatch.fnmatchcase(ref, p) for p in patterns or ())


def _key(v):
    """Hashable identity for a parsed value (6 significant digits)."""
    return float(f"{v:.6g}")


def _fmt(kind, v):
    if v is None:
        return "?"
    unit = {"R": "ohm", "C": "F", "L": "H"}[kind]
    for scale, pre in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"),
                       (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
        if abs(v) >= scale * 0.9999 or scale == 1e-12:
            return f"{v / scale:.4g}{pre}{unit}"
    return f"{v:g}{unit}"


def _f(level, code, msg, refs=(), saves=None):
    d = {"level": level, "code": code, "msg": msg, "refs": sorted(set(refs))}
    if saves:
        d["saves"] = saves
    return d


def _nsort(refs):
    return sorted(refs, key=lambda r: (re.sub(r"\d+$", "", r), int(re.search(r"(\d+)$", r).group(1))
                                       if re.search(r"(\d+)$", r) else 0, r))


def _names(refs, n=8):
    refs = _nsort(refs)
    return ", ".join(refs[:n]) + (f" +{len(refs) - n} more" if len(refs) > n else "")


SAVINGS_CODES = ("BOM_ARRAY_CANDIDATE", "BOM_VALUE_MERGE", "BOM_PACKAGE_SPLIT",
                 "BOM_PARALLEL", "BOM_SERIES_ZERO")


def analyze(components, nets, mpn_map=None, cons=None):
    bom = (cons or {}).get("bom") or {}
    mpn_map = mpn_map or {}
    exempt = bom.get("exempt") or []
    array_min = int(bom.get("array_min", 4))
    tol = float(bom.get("merge_tolerance", 0.1))
    keep_values = bom.get("keep_values") or []
    keep_links = bom.get("keep_links") or []

    # ------------------------------------------------------------ parts
    parts = {}
    for c in components or []:
        ref = c.get("ref")
        if not ref or ref in parts:
            continue
        fp = c.get("fp", "") or ""
        if _MECH_REF.match(ref) or _MECH_FP.search(fp) or _glob_any(ref, exempt):
            continue
        kind = kind_of(ref)
        pkg = package_of(fp)
        val = str(c.get("value", "") or "")
        num = parse_value(val, kind) if kind in ("R", "C", "L") else None
        vkey = _key(num) if num is not None else val
        mpn = mpn_map.get(ref)
        parts[ref] = {"ref": ref, "kind": kind, "pkg": pkg, "value": val,
                      "num": num, "vkey": vkey, "dnp": bool(c.get("dnp")),
                      "mpn": mpn,
                      "ident": mpn or f"{kind}|{vkey}|{pkg}|{val if kind not in _PASSIVE else ''}"}
    fitted = {r: p for r, p in parts.items() if not p["dnp"]}
    dnp = [r for r, p in parts.items() if p["dnp"]]
    passives = {r: p for r, p in fitted.items() if p["kind"] in _PASSIVE}

    pinnet = {}
    for net, pins in (nets or {}).items():
        for pin in pins:
            pinnet[(str(pin[0]), str(pin[1]))] = net

    def terms(ref):
        a, b = pinnet.get((ref, "1")), pinnet.get((ref, "2"))
        return (a, b) if a and b and a != b else None

    # ---------------------------------------------------------- summary
    idents = {}
    for r, p in fitted.items():
        idents.setdefault(p["ident"], []).append(r)
    by_kind = {k: 0 for k in _KINDS}
    by_kind["other"] = 0
    for p in fitted.values():
        by_kind[p["kind"]] += 1
    summary = {
        "total_parts": len(parts),
        "fitted_parts": len(fitted),
        "dnp_parts": len(dnp),
        "unique_values": len({(p["kind"], p["vkey"], p["pkg"]) for p in passives.values()}),
        "unique_mpns": len({p["mpn"] for p in fitted.values() if p["mpn"]}),
        "unique_parts": len(idents),
        "by_kind": by_kind,
        "passive_ratio": round(len(passives) / len(fitted), 3) if fitted else 0.0,
        "single_use_mpns": sum(1 for v in idents.values() if len(v) == 1),
    }
    out = []

    # ---------------------------------------------------------- a. budgets
    if "max_parts" in bom and summary["fitted_parts"] > int(bom["max_parts"]):
        out.append(_f(FAIL, "BOM_BUDGET_PARTS",
                      f"{summary['fitted_parts']} fitted parts exceeds the budget of "
                      f"{bom['max_parts']} ([bom] max_parts)"))
    if "max_unique" in bom and summary["unique_parts"] > int(bom["max_unique"]):
        what = "part numbers" if mpn_map else "distinct values"
        out.append(_f(FAIL, "BOM_BUDGET_UNIQUE",
                      f"{summary['unique_parts']} {what} exceeds the budget of "
                      f"{bom['max_unique']} ([bom] max_unique)"))
    if "max_dnp" in bom and summary["dnp_parts"] > int(bom["max_dnp"]):
        out.append(_f(WARN, "BOM_BUDGET_DNP",
                      f"{summary['dnp_parts']} do-not-populate parts exceeds the budget "
                      f"of {bom['max_dnp']} ([bom] max_dnp)", refs=dnp))

    # ---------------------------------------------------------- b. arrays
    cand = {}
    for r, p in passives.items():
        if p["kind"] != "R" or p["num"] in (None, 0.0) or not _is_chip(p["pkg"]):
            continue
        t = terms(r)
        if not t:
            continue
        for net in t:
            cand.setdefault((p["vkey"], p["pkg"], net), []).append(r)
    placed = set()
    groups = sorted(cand.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
    for (vkey, pkg, net), refs in groups:
        refs = [r for r in refs if r not in placed]
        n = len(refs)
        if n < array_min:
            continue
        placed.update(refs)
        arrays = math.ceil(n / 4)
        saved = n - arrays
        out.append(_f(INFO, "BOM_ARRAY_CANDIDATE",
                      f"{n} x {_fmt('R', vkey)} {pkg} share net {net}: candidate for "
                      f"{arrays} four-resistor array(s), saves {saved} placement(s) "
                      f"(check array power/tolerance/footprint)",
                      refs=refs, saves={"placements": saved, "lines": 0}))

    # ---------------------------------------------------- c. value merging
    counts = {}
    for r, p in passives.items():
        if p["num"] is not None and p["num"] != 0:
            counts.setdefault((p["kind"], p["pkg"]), {}).setdefault(p["vkey"], []).append(r)
    for (kind, pkg), vals in sorted(counts.items()):
        for vkey, refs in sorted(vals.items()):
            if _glob_any_list(refs, keep_values):
                continue
            best = None
            for ok, orefs in vals.items():
                if ok == vkey or len(orefs) <= len(refs):
                    continue
                if abs(vkey - ok) / ok <= tol + 1e-9 and (
                        best is None or len(orefs) > len(vals[best])):
                    best = ok
            if best is not None:
                out.append(_f(INFO, "BOM_VALUE_MERGE",
                              f"{kind} {pkg}: {len(refs)} x {_fmt(kind, vkey)} vs "
                              f"{len(vals[best])} x {_fmt(kind, best)} "
                              f"({abs(vkey - best) / best * 100:.1f}% apart) — candidate to "
                              f"standardise on {_fmt(kind, best)} and drop a line item "
                              f"(check ratings and tolerance)",
                              refs=refs, saves={"placements": 0, "lines": 1}))

    # --------------------------------------------------- d. package split
    byval = {}
    for r, p in passives.items():
        if _is_chip(p["pkg"]) and p["vkey"] != "":
            byval.setdefault((p["kind"], p["vkey"]), {}).setdefault(p["pkg"], []).append(r)
    for (kind, vkey), pk in sorted(byval.items(), key=lambda kv: str(kv[0])):
        if len(pk) < 2:
            continue
        major = max(pk, key=lambda k: (len(pk[k]), k))
        minority = [r for k, v in pk.items() if k != major for r in v]
        shown = _fmt(kind, vkey) if isinstance(vkey, float) else vkey
        out.append(_f(INFO, "BOM_PACKAGE_SPLIT",
                      f"{kind} {shown} appears in {len(pk)} packages "
                      f"({', '.join(f'{len(v)} x {k}' for k, v in sorted(pk.items()))}) — "
                      f"candidate to use {major} throughout (check voltage/power rating)",
                      refs=minority, saves={"placements": 0, "lines": 1}))

    # ------------------------------------------------ e. single-use passives
    use = {}
    for r, p in passives.items():
        use.setdefault(p["mpn"] or (p["kind"], p["vkey"], p["pkg"]), []).append(r)
    single = [(k, v[0]) for k, v in use.items() if len(v) == 1]
    if single:
        what = "part numbers" if mpn_map else "values"
        ex = [str(k) if isinstance(k, str) else
              f"{k[0]} {_fmt(k[0], k[1]) if isinstance(k[1], float) else k[1]} {k[2]}"
              for k, _ in single]
        out.append(_f(INFO, "BOM_SINGLE_USE",
                      f"{len(single)} passive {what} used exactly once (each is a "
                      f"line item and a reel): " + "; ".join(sorted(ex)[:15])
                      + (" ..." if len(ex) > 15 else ""),
                      refs=[r for _, r in single]))

    # ------------------------------------------------------- f. parallel
    par = {}
    for r, p in passives.items():
        if p["kind"] not in ("R", "C"):
            continue
        t = terms(r)
        if t:
            par.setdefault((p["kind"], frozenset(t)), []).append(r)
    for (kind, nn), refs in sorted(par.items(), key=lambda kv: (kv[0][0], sorted(kv[0][1]))):
        a, b = sorted(nn)
        if kind == "R" and len(refs) >= 2:
            out.append(_f(INFO, "BOM_PARALLEL",
                          f"{len(refs)} resistors in parallel between {a} and {b} "
                          f"({_names(refs)}) — candidate for a single part if not a "
                          f"tuning/power-sharing choice",
                          refs=refs, saves={"placements": len(refs) - 1, "lines": 0}))
        elif kind == "C":
            for vk, grp in _by(refs, lambda r: passives[r]["vkey"]).items():
                # bypass-size caps (< 1 uF) are per-pin decoupling by
                # design; only bulk capacitance is a merge candidate
                if len(grp) >= 3 and isinstance(vk, float) and vk >= 1e-6:
                    out.append(_f(INFO, "BOM_PARALLEL",
                                  f"{len(grp)} x {_fmt('C', vk) if isinstance(vk, float) else vk} "
                                  f"capacitors in parallel between {a} and {b} "
                                  f"({_names(grp)}) — candidate for one larger part "
                                  f"(check ESR, voltage derating and placement per IC pin)",
                                  refs=grp, saves={"placements": len(grp) - 1, "lines": 0}))

    # ---------------------------------------------------- g. 0-ohm links
    zeros = [r for r, p in passives.items()
             if p["kind"] == "R" and p["num"] == 0 and not _glob_any(r, keep_links)]
    if zeros:
        out.append(_f(INFO, "BOM_SERIES_ZERO",
                      f"{len(zeros)} fitted 0-ohm link(s): {_names(zeros, 15)} — each is a "
                      f"candidate to delete or to make a solder jumper",
                      refs=zeros, saves={"placements": len(zeros), "lines": 0}))

    # ------------------------------------------------------------ h. DNP
    if dnp:
        out.append(_f(INFO, "BOM_DNP_COUNT",
                      f"{len(dnp)} do-not-populate part(s): {_names(dnp, 15)}", refs=dnp))

    order = {FAIL: 0, WARN: 1, INFO: 2}
    out.sort(key=lambda f: (order[f["level"]], f["code"], f["msg"]))
    return {"summary": summary, "findings": out}


def _by(items, fn):
    d = {}
    for i in items:
        d.setdefault(fn(i), []).append(i)
    return d


def _glob_any_list(refs, patterns):
    return any(_glob_any(r, patterns) for r in refs)
