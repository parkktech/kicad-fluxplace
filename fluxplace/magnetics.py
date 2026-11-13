"""LAN magnetics wiring check (release gate; nothing board- or part-specific).

A magnetics module (or magjack) is a set of 1:1 transformer channels. The
datasheet pin table says which pins of a channel are the chip-side pair and
centre tap and which are the line-side pair and centre tap. A part whose spec
entry declares them is checked against the board's pad nets:

    "channels": [{"chip_p": "2", "chip_n": "3", "chip_ct": "1",
                  "line_p": "23", "line_n": "22", "line_ct": "24"}, ...],
    "pinmap_source": "<datasheet>.pdf#pN"          (the pin table's page)

Why it exists: a part swap kept the old module's cable-side pin order for two
of four channels. DRC, ERC and the spec-vs-board check all read 0 because the
board matched its own netlist; only the datasheet knows which pin is a centre
tap. Result: no gigabit link (100 Mb uses two pairs and worked).

Codes: MAG_LINE_ON_CT, MAG_CHANNEL_SPLIT, MAG_POLARITY, MAG_CT_WIRING,
MAG_NO_CHANNELS, MAG_CHANNELS_UNSOURCED, MAG_PIN_UNRESOLVED.
Constraints (all optional) in `[magnetics]`:
    polarity    = "warn" | "strict"      (strict: swapped P/N is a FAIL)
    chip_ct     = "common" | "any"       (chip centre taps on one net)
    line_ct     = "resistor" | "any"     (each line CT through its own resistor
                                          to one shared net)
"""
import re

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"
CODES = ("MAG_LINE_ON_CT", "MAG_CHANNEL_SPLIT", "MAG_POLARITY", "MAG_CT_WIRING",
         "MAG_NO_CHANNELS", "MAG_CHANNELS_UNSOURCED", "MAG_PIN_UNRESOLVED")

_ROLES = ("chip_p", "chip_n", "chip_ct", "line_p", "line_n", "line_ct")
_CONN_REF = re.compile(r"^(J|P|X|CN|CON|RJ)\d", re.I)
_CONN_FP = re.compile(r"Connector|RJ45|Jack|Header|USB|SMA|Socket|Receptacle|Terminal", re.I)
_RES_REF = re.compile(r"^R\d", re.I)
_LOOKS_MAG = re.compile(
    r"magnetic|magjack|mag-?jack|lan transformer|ethernet transformer|ethernet magnet|"
    r"1000\s*base|100\s*base|10/100|gigabit|\bh[x]?5\d{3}|749020|\bhr\d{6}|\bsi-\d{5}|"
    r"\bg24\d|\bpm-?\d{4}|lan\s*pulse", re.I)
_POL = re.compile(r"(?:[_.\-](P|N|POS|NEG)|([+-]))$", re.I)


def _f(level, code, msg, refs=()):
    return {"level": level, "code": code, "msg": msg, "refs": sorted(set(refs))}


def _pol(net):
    """'P' / 'N' from a trailing polarity token (_P, _N, +, -), else None."""
    m = _POL.search(net or "")
    if not m:
        return None
    t = (m.group(1) or m.group(2)).upper()
    return "P" if t in ("P", "POS", "+") else "N"


def _base(net):
    m = _POL.search(net or "")
    return net[:m.start()] if m else None


def _is_conn(ref, part):
    return bool(_CONN_REF.match(ref) or _CONN_FP.search((part or {}).get("footprint", "") or ""))


def _spec_comps(spec):
    return [c for c in (spec or {}).get("components", []) if isinstance(c, dict)]


def _two_pad_res(ref, part):
    pads = (part or {}).get("pads") or {}
    return bool(_RES_REF.match(ref)) and len(pads) == 2


def check(parts, net_pads, spec, cons=None):
    """parts: {ref: {"pads": {num: net}, "footprint", "value"}}; net_pads:
    {net: [(ref, pad)]}. Returns finding dicts (same shape as review)."""
    cfg = (cons or {}).get("magnetics", {}) or {}
    strict_pol = str(cfg.get("polarity", "warn")).lower() == "strict"
    chip_mode = str(cfg.get("chip_ct", "common")).lower()
    line_mode = str(cfg.get("line_ct", "resistor")).lower()
    out = []

    def net_of(ref, pad):
        return ((parts.get(ref) or {}).get("pads") or {}).get(str(pad)) or ""

    def others(net, ref):
        return [(r, p) for r, p in {tuple(x) for x in net_pads.get(net, [])} if r != ref]

    def conn_pads(net, ref):
        return [f"{r}.{p}" for r, p in others(net, ref) if _is_conn(r, parts.get(r))]

    for comp in _spec_comps(spec):
        ref = comp.get("ref")
        chans = comp.get("channels")
        desc = " ".join(str(comp.get(k, "")) for k in ("value", "mpn", "fp", "description", "note"))
        if not chans:
            if ref in parts and _LOOKS_MAG.search(desc + " " + (parts[ref].get("footprint") or "")):
                out.append(_f(WARN, "MAG_NO_CHANNELS",
                              f"{ref} ({comp.get('value')}) looks like LAN magnetics/magjack but declares no "
                              f"`channels`: read its datasheet pin table (cite the page in pinmap_source) and "
                              f"declare chip_p/chip_n/chip_ct/line_p/line_n/line_ct per channel", refs=[ref]))
            continue
        if ref not in parts:
            continue
        if not comp.get("pinmap_source"):
            out.append(_f(WARN, "MAG_CHANNELS_UNSOURCED",
                          f"{ref}: channels declared without pinmap_source (datasheet file#page of the pin table)",
                          refs=[ref]))
        pads = parts[ref].get("pads") or {}
        resolved = []
        for i, ch in enumerate(chans, 1):
            row = {}
            for role in _ROLES:
                v = ch.get(role)
                if v in (None, ""):
                    continue
                if str(v) not in pads:
                    out.append(_f(WARN, "MAG_PIN_UNRESOLVED",
                                  f"{ref} channel {i} {role}={v}: no such pad on the board part", refs=[ref]))
                    continue
                row[role] = str(v)
            if "chip_p" in row and "chip_n" in row and "line_p" in row and "line_n" in row:
                resolved.append((i, row))
            else:
                out.append(_f(WARN, "MAG_PIN_UNRESOLVED",
                              f"{ref} channel {i}: needs chip_p, chip_n, line_p and line_n pads to be checked",
                              refs=[ref]))
        line_ct_nets = {}
        # ---- line side: only line nets on line pins, only termination on CT
        for i, row in resolved:
            for role in ("line_p", "line_n"):
                pad, net = row[role], net_of(ref, row[role])
                if not net:
                    continue
                oth = others(net, ref)
                if not conn_pads(net, ref) and oth and all(_two_pad_res(r, parts.get(r)) or
                                                          bool(_RES_REF.match(r)) for r, _ in oth):
                    out.append(_f(FAIL, "MAG_LINE_ON_CT",
                                  f"{ref}.{pad} (channel {i} {role}) is on {net}, a termination net "
                                  f"({', '.join(sorted(f'{r}.{p}' for r, p in oth))}, no connector): "
                                  f"the datasheet has the line pair here, not a centre tap", refs=[ref]))
            if "line_ct" in row:
                pad, net = row["line_ct"], net_of(ref, row["line_ct"])
                line_ct_nets[i] = net
                cp = conn_pads(net, ref) if net else []
                if cp:
                    out.append(_f(FAIL, "MAG_LINE_ON_CT",
                                  f"{ref}.{pad} (channel {i} line_ct) is on {net}, a line net that reaches "
                                  f"{', '.join(sorted(cp))}: the datasheet has the centre tap here", refs=[ref]))
        # ---- channel split / polarity
        for i, row in resolved:
            cp_, cn_ = net_of(ref, row["chip_p"]), net_of(ref, row["chip_n"])
            lp_, ln_ = net_of(ref, row["line_p"]), net_of(ref, row["line_n"])
            lpol, lnpol = _pol(lp_), _pol(ln_)
            if lp_ and ln_:
                bp, bn = _base(lp_), _base(ln_)
                if (bp is not None or bn is not None) and bp != bn:
                    out.append(_f(FAIL, "MAG_CHANNEL_SPLIT",
                                  f"{ref} channel {i}: chip pair {cp_}/{cn_} comes out on line pads "
                                  f"{row['line_p']}/{row['line_n']} = {lp_}/{ln_}, which are not one pair "
                                  f"(a channel's two line pads must carry the same pair)", refs=[ref]))
            cpol, cnpol = _pol(cp_), _pol(cn_)
            if cpol and cnpol and cpol != cnpol and lpol and lnpol and lpol != lnpol and cpol == lnpol:
                out.append(_f(FAIL if strict_pol else WARN, "MAG_POLARITY",
                              f"{ref} channel {i}: chip {row['chip_p']}={cp_} / {row['chip_n']}={cn_} reach line "
                              f"{row['line_p']}={lp_} / {row['line_n']}={ln_} with P and N swapped "
                              f"(auto-polarity PHYs tolerate it)", refs=[ref]))
        # ---- centre taps
        if chip_mode == "common":
            cts = {i: (row["chip_ct"], net_of(ref, row["chip_ct"])) for i, row in resolved if "chip_ct" in row}
            nets = {n for _, n in cts.values()}
            if "" in nets or len(nets) > 1:
                out.append(_f(FAIL, "MAG_CT_WIRING",
                              f"{ref} chip-side centre taps are not on one common net: "
                              + ", ".join(f"{p}={n or 'NO NET'}" for p, n in cts.values()), refs=[ref]))
            for i, row in resolved:
                for role in ("chip_ct",):
                    if role in row:
                        n = net_of(ref, row[role])
                        sig = {net_of(ref, r2[x]) for _, r2 in resolved for x in ("chip_p", "chip_n")}
                        if n and (n in sig or conn_pads(n, ref)):
                            out.append(_f(FAIL, "MAG_CT_WIRING",
                                          f"{ref}.{row[role]} (channel {i} chip_ct) is on signal net {n}", refs=[ref]))
        if line_mode == "resistor":
            far = {}
            seen = {}
            for i, net in line_ct_nets.items():
                pad = next(r["line_ct"] for j, r in resolved if j == i)
                if not net:
                    out.append(_f(FAIL, "MAG_CT_WIRING", f"{ref}.{pad} (channel {i} line_ct) has no net", refs=[ref]))
                    continue
                if net in seen:
                    out.append(_f(FAIL, "MAG_CT_WIRING",
                                  f"{ref} channels {seen[net]} and {i} line centre taps share net {net} "
                                  f"with no resistor of their own", refs=[ref]))
                seen[net] = i
                if conn_pads(net, ref):
                    continue                      # already a MAG_LINE_ON_CT FAIL
                res = [(r, p) for r, p in others(net, ref) if _two_pad_res(r, parts.get(r))]
                if len(res) != 1:
                    out.append(_f(FAIL, "MAG_CT_WIRING",
                                  f"{ref}.{pad} (channel {i} line_ct) on {net}: needs exactly one own series "
                                  f"resistor, found {len(res)}", refs=[ref] + [r for r, _ in res]))
                    continue
                r, p = res[0]
                far[i] = next((n for q, n in parts[r]["pads"].items() if q != p), "")
            if len(set(far.values())) > 1 or "" in far.values():
                out.append(_f(FAIL, "MAG_CT_WIRING",
                              f"{ref} line-side centre-tap resistors do not end on one shared net: "
                              + ", ".join(f"channel {i} -> {n or 'NO NET'}" for i, n in sorted(far.items())),
                              refs=[ref]))
    return out


def summary(findings):
    n = {FAIL: 0, WARN: 0, INFO: 0}
    for f in findings:
        n[f["level"]] += 1
    return n
