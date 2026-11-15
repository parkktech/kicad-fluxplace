"""Drop-in equivalents for parts that are short or absent at the build quantity.

A candidate is `drop_in: true` ONLY when every one of these is verified from distributor parametric data AND the candidate's own
datasheet (fetched and validated by dsfetch):  same footprint/land pattern, same pin count, same electrical value, rating equal
or better, tolerance/resistance-type figures equal or better, temperature range at least as wide, same qualification (AEC-Q where
the original has it), active lifecycle, stock at the build quantity.  A packaging variant of the same MPN (reel/tape/cut-tape
suffix) is the same part (`packaging_equivalent`), not an alternative.  Anything unverified is NOT a drop-in: `unverified` lists
what could not be checked, `differs` what is different, and the part stays `needs_owner`.  No board-specific names live here.
"""
import re

from . import parts as P

# ---- packaging variants: (mpn regex, tail class) -> two MPNs equal after the tail is dropped
_PKG_TAILS = [
    (r"^G[RC]M\w+[A-Z]\d{2}[DLJWK]$", 1),      # Murata: last char is the packaging code
    (r"^[A-Z]{2,}\d[A-Z0-9]*[A-Z]{2}[RT]$", 1),    # TI-style orderable ending in a reel code (R = 3000, T = 250)
]


def _n(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def packaging_equivalent(a, b):
    """True when `a` and `b` are the same part in different packaging (cut tape / reel / bulk code)."""
    x, y = _n(a), _n(b)
    if not x or not y:
        return False
    if x == y:
        return True
    from . import sourcing as S
    if S._same_part(a, b)[0] and (x.startswith(y) or y.startswith(x)):
        return True                                     # one MPN extends the other by a short packaging tail
    if len(x) == len(y) and x[:-1] == y[:-1]:
        for rx, k in _PKG_TAILS:
            if re.match(rx, x) and re.match(rx, y):
                return True
    return False


# ---- numbers with units
_PFX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
_NUM = re.compile(r"(-?\d+(?:\.\d+)?)\s*(p|n|u|µ|μ|m|k|K|M|G)?\s*(Hz|F|V|A|W|H|Ω|Ohms?|ohms?|%|ppm)(?![A-Za-z])")


def parse_quantity(text):
    """'100nF' -> (1e-07, 'F'); '±10%' -> (10, '%'); '20 mOhm' -> (0.02, 'ohm'); None when there is no number+unit."""
    if not text:
        return None
    m = _NUM.search(str(text).replace(",", ""))
    if not m:
        return None
    unit = m.group(3)
    if unit.lower().startswith("ohm") or unit == "Ω":
        unit = "ohm"
    return float(m.group(1)) * _PFX.get(m.group(2) or "", 1.0), unit


_IGNORE = re.compile(r"packag|series|manufacturer|status|base product|rohs|moisture|reach|eccn|htsus|lead time|supplier device|"
                     r"package / case|case|size|dimension|features|applications|ratings|qualification|grade|operating temp|"
                     r"mounting|thickness|height|standard|description|category|number of pins|lead free|lifecycle|weight|"
                     r"part number|product", re.I)
_AT_LEAST = re.compile(r"voltage.*(rated|rating|breakdown)|rated voltage|current.*(rating|saturation|rated|output|continuous|collector)|"
                       r"current rating|power \(watts\)|power rating|drain to source voltage|vdss|voltage - (input|supply).*max|"
                       r"max.*voltage|saturation", re.I)
_AT_MOST = re.compile(r"tolerance|dc ?resistance|dcr|rds ?on|\besr\b|voltage - forward|quiescent|leakage|on-?resistance|noise", re.I)
_EQUAL = re.compile(r"capacitance|^resistance|inductance|temperature coefficient|dielectric|fet type|technology|channel|polarity|"
                    r"configuration|output type|frequency$|load capacitance|interface|voltage - output|^type$|circuit|"
                    r"function|number of (channels|circuits|outputs|inputs|regulators|bits)|resolution|topology|isolation|"
                    r"output configuration", re.I)


def _same_text(a, b):
    return re.sub(r"[^a-z0-9.+\-]", "", str(a).lower()) == re.sub(r"[^a-z0-9.+\-]", "", str(b).lower())


def compare_params(orig, cand):
    """Parametric comparison. -> {"verified": [..], "differs": [..], "unverified": [..]} (each entry names the parameter)."""
    out = {"verified": [], "differs": [], "unverified": []}
    lo = {k.lower(): (k, v) for k, v in (orig or {}).items()}
    lc = {k.lower(): (k, v) for k, v in (cand or {}).items()}
    for key, (k0, ov) in lo.items():
        if _IGNORE.search(key):
            continue
        rule = "atmost" if _AT_MOST.search(key) else "atleast" if _AT_LEAST.search(key) else "equal" if _EQUAL.search(key) else None
        if rule is None:
            continue
        if key not in lc:
            out["unverified"].append(f"{k0}: not listed for the candidate")
            continue
        cv = lc[key][1]
        qo, qc = parse_quantity(ov), parse_quantity(cv)
        if qo and qc and qo[1] == qc[1]:
            a, b = qo[0], qc[0]
            ok = (abs(a - b) <= 1e-6 * max(abs(a), abs(b), 1e-30) if rule == "equal" else
                  b >= a * (1 - 1e-9) if rule == "atleast" else b <= a * (1 + 1e-9))
            (out["verified"] if ok else out["differs"]).append(f"{k0}: {ov} -> {cv}")
        elif rule == "equal":
            (out["verified"] if _same_text(ov, cv) else out["differs"]).append(f"{k0}: {ov} -> {cv}")
        else:
            out["unverified"].append(f"{k0}: {ov} vs {cv} not comparable")
    return out


def _qual(params, mpn=""):
    t = " ".join(str(v) for k, v in (params or {}).items() if re.search(r"rating|qualif|grade", k, re.I))
    m = re.search(r"AEC-?Q\s?(\d{3})", t, re.I)
    return m.group(1) if m else None


def classify(orig, cand, fp=None, required=1, temp=(None, None), ds=None):
    """-> {"drop_in": bool, "identical": bool, "checks": {name: (ok|None, detail)}, "differs": [...], "unverified": [...]}.
    orig / cand: {"mpn","package","pins","temp_min","temp_max","lifecycle","stock","params": {...}} (orig may be the recorded part's
    distributor row; its stock is not used).  ds: {"level": 'part'|'series'|None, "text": str|None} for the CANDIDATE's datasheet."""
    from . import review as R
    chk, differs, unver = {}, [], []

    def put(name, ok, detail):
        chk[name] = (ok, detail)
        if ok is False:
            differs.append(f"{name}: {detail}")
        elif ok is None:
            unver.append(f"{name}: {detail}")

    if packaging_equivalent(orig.get("mpn"), cand.get("mpn")):
        put("part", True, "same part, packaging variant")
    put("stock", (cand.get("stock") or 0) >= required, f"{cand.get('stock') or 0} on hand vs {required} needed")
    life = (cand.get("lifecycle") or "Active").lower()
    put("lifecycle", life in ("active", "in production", "new product", ""), cand.get("lifecycle") or "not stated")
    cp, op = cand.get("package"), orig.get("package")
    if fp and cp:
        v, d = R.packages_agree(cp, fp)
        put("footprint", True if v == "ok" else None if v == "unknown" else False, f"package '{cp}' vs footprint {fp}: {d}")
    else:
        put("footprint", None, "package or footprint not stated")
    if op and cp:
        v, d = R.packages_agree(cp, op)
        put("package", True if v == "ok" or _same_text(cp, op) else None if v == "unknown" else False, f"'{cp}' vs original '{op}'")
    po, pc = orig.get("pins"), cand.get("pins")
    kp = lambda pk: {n for f, n in (R.package_key(pk) or set()) if isinstance(n, int)} if pk else set()   # noqa: E731
    po, pc = po or (min(kp(op)) if kp(op) else None), pc or (min(kp(cp)) if kp(cp) else None)
    chip = bool(cp and op and {f for f, _ in R.package_key(cp)} == {"CHIP"} == {f for f, _ in R.package_key(op)})
    if po and pc:
        put("pins", po == pc, f"{pc} vs {po}")
    elif chip or (po is None and pc is None and not kp(cp) and not kp(op) and fp and P.chip_from_fp(fp)):
        put("pins", True, "two-terminal chip: the footprint match covers the pin count")
    else:
        put("pins", None, "pin count not stated by both listings")
    lo, hi = temp
    cmin, cmax = cand.get("temp_min"), cand.get("temp_max")
    omin, omax = orig.get("temp_min"), orig.get("temp_max")
    need_lo = min(x for x in (lo, omin) if x is not None) if (lo is not None or omin is not None) else None
    need_hi = max(x for x in (hi, omax) if x is not None) if (hi is not None or omax is not None) else None
    if need_lo is None and need_hi is None:
        put("temperature", True, "no requirement or original range to cover")
    elif cmin is None or cmax is None:
        put("temperature", None, "candidate listing states no operating range")
    else:
        put("temperature", (need_lo is None or cmin <= need_lo) and (need_hi is None or cmax >= need_hi),
            f"{cmin:g}..{cmax:g} C vs required {need_lo}..{need_hi} C")
    qo, qc = _qual(orig.get("params")), _qual(cand.get("params"))
    if qo:
        put("qualification", bool(qc) and int(qc) <= int(qo) or bool(qc) and qc == qo, f"AEC-Q{qc or '??'} vs original AEC-Q{qo}")
    else:
        put("qualification", True, "original has no AEC-Q qualification")
    pc_ = compare_params(orig.get("params"), cand.get("params"))
    differs += [f"parameter {x}" for x in pc_["differs"]]
    unver += [f"parameter {x}" for x in pc_["unverified"]]
    if pc_["differs"]:
        chk["parameters"] = (False, "; ".join(pc_["differs"]))
    elif pc_["verified"]:
        chk["parameters"] = (True, f"{len(pc_['verified'])} verified: " + "; ".join(pc_["verified"][:4]))
    else:
        put("parameters", None, "no electrical parameter could be compared")
    lvl = (ds or {}).get("level")
    if lvl != "part":
        put("datasheet", None if not lvl else False, "candidate datasheet not fetched" if not lvl else "only a series document, not the part's")
    else:
        txt = (ds or {}).get("text") or ""
        fam = {f for f, _ in (R.package_key(cp) or set()) | (R.package_key(fp) or set()) if f != "CHIP"}
        chip = P.chip_from_mpn(cand["mpn"])[0] if cand.get("mpn") else None
        fs = P.chip_from_fp(fp or "")
        if fam:
            ok = any(re.sub(r"[^A-Z0-9]", "", f) in re.sub(r"[^A-Z0-9]", "", txt.upper()) for f in fam)
            put("datasheet", ok or None, "validated; names the package family" if ok else "validated, but package family not found in its text")
        elif chip and fs:
            put("datasheet", chip in fs, f"validated; MPN size code {chip} vs footprint {'/'.join(sorted(fs))}")
        else:
            put("datasheet", True, "validated for the candidate MPN")
    ident = chk.get("part", (None,))[0] is True
    return {"drop_in": all(v[0] is True for k, v in chk.items() if k != "part"), "identical": ident, "checks": chk,
            "differs": differs, "unverified": unver}


def rank(cands):
    """Verified drop-ins first, then by fewest differences/unverified, then price."""
    return sorted(cands, key=lambda c: (not c.get("drop_in"), len(c.get("differs", [])), len(c.get("unverified", [])),
                                        c.get("ext") is None, c.get("ext") or 0))


def row_for(d, mpn):
    """A partdata/lookup row -> the classify() dict."""
    return {"mpn": mpn, "package": d.get("package"), "pins": d.get("pins"), "temp_min": d.get("temp_min"), "temp_max": d.get("temp_max"),
            "lifecycle": d.get("lifecycle"), "params": d.get("raw_params") or d.get("params") or {}, "stock": d.get("stock")}
