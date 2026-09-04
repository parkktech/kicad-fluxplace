"""Part DOCUMENTATION as a build requirement.

A component is on the board only when three things are true and can be
shown from files in the project:

  1. it has a manufacturer part number (MPN) — a value like "10k" or
     "SP0504" is a description, not a part;
  2. its datasheet is ON DISK in the project (fetched through the DigiKey /
     Mouser APIs or dropped in by hand) and recorded in a manifest with a
     hash, so the document reviewed is the document that ships with the
     package — a URL that answered once is not documentation;
  3. every pin the design uses is backed by that document: the spec's
     `pinmap` names must be found on the cited datasheet page(s)
     (`pinmap_source: "datasheets/<file>.pdf#p3"`), or the part must be a
     plain 2-terminal passive whose pins carry no names. Connectors (J*)
     need the MPN and the datasheet; their pins are positions, not names.

`review` turns each miss into a FAIL (MPN_MISSING, DATASHEET_MISSING,
PINMAP_MISSING, PINMAP_EVIDENCE_WEAK); `schematic` refuses to generate from
a spec that fails `spec_check`. Motivation: utv-comms V1.4 shipped to an
external reviewer with an ESD array whose ground was on the wrong pin — the
pinmap had been typed from memory, and nothing in the toolchain asked for
the page it came from.

pdftotext (poppler-utils) is used for page text; when it is missing, page
evidence cannot be checked and is reported as such (never silently passed).
Fetching uses curl_cffi (Chrome TLS impersonation) when installed: the
manufacturer hosts that refuse scripts accept a browser fingerprint, so no
human has to click through for a public document.
"""
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.request

__all__ = ["datasheet_temp", "fetch", "load_manifest", "page_text", "evidence", "spec_check",
           "PASSIVE_PREFIXES", "manifest_path"]

MANIFEST = "datasheets.json"
PASSIVE_PREFIXES = ("R", "C", "L", "FB", "MH", "MK", "H", "TP", "JP", "NT")
_PIN_TOKEN = re.compile(r"[A-Z0-9_+\-/#~.]+", re.I)


def manifest_path(ds_dir):
    return os.path.join(ds_dir, MANIFEST)


def load_manifest(ds_dir):
    p = manifest_path(ds_dir)
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            return {}
    return {}


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(mpn):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", mpn).strip("_") or "part"


def _get(url, timeout=60):
    """(status, content_type, bytes). Uses curl_cffi impersonating Chrome
    when installed — manufacturer CDNs (Amphenol, Littelfuse, onsemi, C&K,
    Molex, measured) answer 403 to a plain client's TLS fingerprint and 200
    to a browser's; the document is public either way. Falls back to urllib."""
    try:
        from curl_cffi import requests as cffi
        r = cffi.get(url, impersonate="chrome", timeout=timeout, allow_redirects=True,
                     headers={"Accept": "application/pdf,*/*"})
        return r.status_code, r.headers.get("content-type", ""), r.content
    except ImportError:
        pass
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0",
        "Accept": "application/pdf,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def _download(url, dest, timeout=60):
    """-> (ok, detail). A 200 that is not a PDF is a bot page, not a document."""
    if not url:
        return False, "no URL"
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    try:
        status, ctype, data = _get(url, timeout)
    except Exception as e:
        return False, type(e).__name__ + (" %s" % getattr(e, "code", "") if hasattr(e, "code") else "")
    if status != 200:
        return False, "HTTP %s" % status
    if not data.startswith(b"%PDF"):
        return False, "served %s, not a PDF" % (data[:15].decode("latin1", "replace").strip() or "nothing")
    with open(dest, "wb") as fh:
        fh.write(data)
    return True, "%d bytes" % len(data)


def fetch(mpns, ds_dir, creds=None, refresh=False, log=print, urls=None):
    """Fetch datasheets for `mpns` into ds_dir and update the manifest.
    urls: optional {mpn: url} overrides (a URL the engineer found by hand).
    Returns {mpn: {"file", "sha256", "source", "fetched"} | None}."""
    from .models import credentials
    from . import sourcing as S
    os.makedirs(ds_dir, exist_ok=True)
    man = load_manifest(ds_dir)
    creds = creds if creds is not None else credentials()
    out = {}
    for mpn in sorted(set(m for m in mpns if m)):
        ent = man.get(mpn)
        if ent and not refresh:
            f = os.path.join(ds_dir, ent.get("file", ""))
            if os.path.exists(f) and _sha(f) == ent.get("sha256"):
                out[mpn] = ent
                continue
            log(f"    {mpn}: manifest entry stale (file missing or changed) — refetching")
        cands = [(urls or {}).get(mpn)] if (urls or {}).get(mpn) else []
        cands += S.datasheet_urls(mpn, creds)
        # TI hides the PDF behind a distributor redirect page; its own
        # symlink host serves it plainly
        for u in list(cands):
            m = re.search(r"gotoUrl=([^&]+)", u or "")
            if m and "ti.com" in u:
                import urllib.parse
                cands.append(urllib.parse.unquote(m.group(1)))
        if not cands:
            log(f"    {mpn}: no datasheet URL from DigiKey or Mouser")
            out[mpn] = None
            continue
        dest = os.path.join(ds_dir, _safe_name(mpn) + ".pdf")
        ok, detail, url = False, "", None
        for url in cands:
            ok, detail = _download(url, dest)
            if ok:
                break
        if not ok:
            log(f"    {mpn}: {detail} ({(url or '')[:60]}) — fetch it in a browser and drop "
                f"it in as {os.path.basename(dest)}")
            out[mpn] = None
            continue
        ent = {"file": os.path.basename(dest), "sha256": _sha(dest), "source": url,
               "fetched": time.strftime("%Y-%m-%d")}
        man[mpn] = ent
        out[mpn] = ent
        log(f"    {mpn}: {ent['file']} ({detail})")
    with open(manifest_path(ds_dir), "w") as fh:
        json.dump(man, fh, indent=1, sort_keys=True)
    return out


def adopt(ds_dir, files, log=print):
    """Register hand-dropped PDFs: files = {mpn: filename-in-ds_dir}."""
    man = load_manifest(ds_dir)
    for mpn, name in files.items():
        p = os.path.join(ds_dir, name)
        if not os.path.exists(p):
            log(f"    {mpn}: {name} not found in {ds_dir}")
            continue
        man[mpn] = {"file": name, "sha256": _sha(p), "source": "manual",
                    "fetched": time.strftime("%Y-%m-%d")}
        log(f"    {mpn}: registered {name}")
    with open(manifest_path(ds_dir), "w") as fh:
        json.dump(man, fh, indent=1, sort_keys=True)
    return man


# ----------------------------------------------------------------- evidence
def page_text(pdf, pages=None):
    """Text of the given 1-based pages (all when None) via pdftotext, or
    None when pdftotext is unavailable."""
    cmd = ["pdftotext", "-layout"]
    if pages:
        cmd += ["-f", str(min(pages)), "-l", str(max(pages))]
    cmd += [pdf, "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def parse_source(src):
    """'datasheets/X.pdf#p3' / 'X.pdf#p3-4' / 'X.pdf' -> (file, [pages])."""
    if not src or not isinstance(src, str):
        return None, []
    m = re.match(r"^(.*?\.pdf)(?:#p(\d+)(?:-(\d+))?)?$", src.strip(), re.I)
    if not m:
        return None, []
    a, b = m.group(2), m.group(3)
    pages = list(range(int(a), int(b or a) + 1)) if a else []
    return m.group(1), pages


def _norm(x):
    """Compare pin names the way a reader does: case, slashes, dots and
    underscores are typography ('I/O1' == 'IO1', 'N.C.' == 'NC')."""
    x = (x or "").replace("\u2013", "-").replace("\u2212", "-").replace("\u2014", "-")
    return re.sub(r"[^A-Z0-9+\-]", "", x.upper())


def _tokens(text):
    return {_norm(t) for t in _PIN_TOKEN.findall(text or "")} - {""}


def _aliases(name):
    """Pin-name spellings a datasheet may use for a spec name."""
    n = name.upper().strip()
    out = {n, n.replace("_", ""), n.replace("_", "-"), n.replace("~", ""),
           n.rstrip("0123456789") if n[-1:].isdigit() and len(n) >= 2 else n}
    if "_" in n:                      # GND_8, NC_6, IO2_2: the base name
        out.add(n.split("_")[0])
    if n.endswith("_N") or n.endswith("N"):
        out.add(n[:-2] + "-" if n.endswith("_N") else n[:-1] + "-")
    if n.endswith("_P") or n.endswith("P"):
        out.add(n[:-2] + "+" if n.endswith("_P") else n[:-1] + "+")
    if n in ("GND", "GND1", "GND2", "GND10", "GND12", "PGND", "AGND", "DGND"):
        out |= {"GND", "VSS", "V-", "GROUND"}
    if n in ("VCC", "VDD", "VIN", "VBUS", "V+"):
        out |= {"VCC", "VDD", "V+", "VIN"}
    if n == "PAD":
        out |= {"EP", "PAD", "EXPOSED", "THERMAL"}
    if n in ("NC", "N.C."):
        out |= {"NC", "N.C.", "N/C"}
    if n in ("K", "C", "CATHODE"):
        out |= {"K", "CATHODE", "C"}
    if n in ("A", "ANODE"):
        out |= {"A", "ANODE"}
    if n.startswith("COIL"):
        out |= {"COIL", "COIL+", "COIL-", "+", "-"}
    if n in ("S", "S1", "S2", "S3", "SOURCE"):
        out |= {"S", "SOURCE"}
    if n in ("D", "D1", "D2", "DRAIN"):
        out |= {"D", "DRAIN"}
    if n in ("G", "GATE"):
        out |= {"G", "GATE"}
    return {_norm(a) for a in out if _norm(a)}


def evidence(pinmap, pdf_path, pages, min_ratio=0.8):
    """Does the cited page carry the pinmap's names? -> (ok, found, missing,
    detail). ok is None when the text could not be read."""
    if not pinmap:
        return True, [], [], "no pinmap"
    if not pdf_path or not os.path.exists(pdf_path):
        return False, [], sorted(pinmap), "datasheet file missing"
    txt = page_text(pdf_path, pages or None)
    if txt is None:
        return None, [], sorted(pinmap), "pdftotext unavailable or PDF unreadable"
    if len(txt.strip()) < 40:
        return None, [], sorted(pinmap), ("cited page(s) carry no text (drawing/scan) — "
                                          "verify by eye and cite a text page or the family sheet")
    toks = _tokens(txt)
    flat = _norm(txt)
    found, missing = [], []
    for name in pinmap:
        al = _aliases(name)
        if al & toks or any(len(a) >= 3 and a in flat for a in al):
            found.append(name)
        else:
            missing.append(name)
    ratio = len(found) / max(1, len(pinmap))
    return ratio >= min_ratio, sorted(found), sorted(missing), \
        "%d/%d pin names on page(s) %s" % (len(found), len(pinmap),
                                           ",".join(map(str, pages)) if pages else "all")


# ------------------------------------------------------------------- cite
def best_page(pinmap, pdf_path, max_pages=60):
    """(page, ratio, missing) for the page whose text carries the most of the
    pinmap's names — the pin-configuration page, in practice."""
    best = None
    for pg in range(1, max_pages + 1):
        txt = page_text(pdf_path, [pg])
        if txt is None:
            break
        if not txt.strip():
            continue
        ok, found, missing, _ = evidence(pinmap, pdf_path, [pg])
        ratio = len(found) / max(1, len(pinmap))
        if best is None or ratio > best[1]:
            best = (pg, ratio, missing)
        if ratio >= 1.0:
            break
    return best


def cite(spec, ds_dir, mpn_map=None, min_ratio=0.8, only_missing=True, log=print):
    """Fill `pinmap_source` with the datasheet page that backs each pinmap.
    Returns (spec, [(ref, page, ratio, missing)]) — a page below min_ratio
    is reported and NOT written, so a wrong pinmap stays visible."""
    man = load_manifest(ds_dir)
    mpn_map = mpn_map or {}
    report = []
    for c in spec.get("components", []):
        pm = c.get("pinmap")
        if not pm:
            continue
        if only_missing and parse_source(c.get("pinmap_source"))[0]:
            continue
        mpn = c.get("mpn") or mpn_map.get(c.get("ref"))
        ent = man.get(mpn) if mpn else None
        if not ent:
            continue
        pdf = os.path.join(ds_dir, ent["file"])
        bp = best_page(pm, pdf)
        if not bp:
            continue
        pg, ratio, missing = bp
        report.append((c.get("ref"), pg, ratio, missing))
        if ratio >= min_ratio:
            c["pinmap_source"] = f"{ent['file']}#p{pg}"
            log(f"    {c.get('ref')}: {ent['file']}#p{pg} ({ratio:.0%})")
        else:
            log(f"    {c.get('ref')}: best page {pg} carries only {ratio:.0%} of the "
                f"pin names — not cited; missing {', '.join(missing[:6])}")
    return spec, report


def kicad_evidence(pinmap, src, lib_dirs=("/usr/share/kicad/symbols",)):
    """'kicad:<Library>:<Symbol>' -> (ok, why). Pin numbers must exist on the
    symbol and ground/power roles must agree (review.pin_role rules)."""
    from . import review as R
    try:
        _, lib, sym = src.split(":", 2)
    except ValueError:
        return False, f"bad kicad: source {src!r}"
    path = None
    for d in lib_dirs:
        cand = os.path.join(d, lib + ".kicad_sym")
        if os.path.exists(cand):
            path = cand
            break
    if not path:
        return False, f"library {lib} not installed"
    lp = R.lib_pins(path, sym)
    if not lp:
        return False, f"symbol {sym} not in {lib}"
    absent = [str(n) for n in pinmap.values() if str(n) not in lp]
    if absent:
        return False, f"pins {', '.join(absent)} not on {lib}:{sym}"
    for nm, num in pinmap.items():
        rs, rl = R.pin_role(nm), R.pin_role(lp[str(num)])
        if (rs == "GND" and rl not in ("GND", "COMMON")) or (rl == "GND" and rs not in ("GND", "COMMON")) \
                or (rs == "PWR" and rl in ("GND", "COMMON")) or (rl == "COMMON" and rs == "IO"):
            return False, f"pin {num}: spec {nm} vs {lib}:{sym} '{lp[str(num)]}'"
    return True, f"matches {lib}:{sym} ({len(lp)} pins)"


# ---------------------------------------------------------------- spec check
def is_passive(ref, fp="", pins=None):
    """2-terminal passives carry no pin names to verify."""
    r = re.match(r"^[A-Za-z]+", ref or "")
    pfx = r.group(0).upper() if r else ""
    if pfx in ("R", "C", "L", "FB", "D", "BT", "F", "Y") and (pins is None or pins <= 2):
        return True
    return pfx in ("MH", "MK", "H", "TP", "JP", "NT", "LOGO", "G")


def spec_check(spec, ds_dir, mpn_map=None, strict=True, log=print):
    """Findings [(level, code, ref, msg)] for a netlist spec dict.
    mpn_map: {ref: mpn} (the spec may also carry 'mpn' per component)."""
    out = []
    man = load_manifest(ds_dir) if ds_dir else {}
    mpn_map = mpn_map or {}
    pins_of = {}
    for net, conns in (spec.get("nets") or {}).items():
        for ref, pin in conns:
            pins_of.setdefault(ref, set()).add(str(pin))
    for c in spec.get("components", []):
        ref = c.get("ref", "?")
        mpn = c.get("mpn") or mpn_map.get(ref)
        npins = len(pins_of.get(ref, ()))
        passive = is_passive(ref, c.get("fp", ""), npins)
        if c.get("dnp"):
            continue
        if not mpn:
            if not passive or c.get("value", "").lower() in ("", "?", "tbd"):
                out.append(("FAIL", "MPN_MISSING", ref,
                            f"{ref} ({c.get('value', '')}): no manufacturer part number"))
            continue
        ent = man.get(mpn)
        f = os.path.join(ds_dir, ent["file"]) if (ent and ds_dir) else None
        if not ent or not f or not os.path.exists(f):
            # a 2-terminal passive with an MPN is identified; its datasheet is
            # wanted (WARN), an active part's is required (FAIL)
            out.append(("FAIL" if (strict and not passive) else "WARN", "DATASHEET_MISSING", ref,
                        f"{ref} {mpn}: no datasheet on disk (fluxplace datasheets, "
                        f"or drop the PDF into {ds_dir or 'the datasheets dir'})"))
        pinmap = c.get("pinmap")
        is_conn = re.match(r"^J\d", ref or "") is not None
        if not pinmap and not passive and npins > 2 and not is_conn:
            out.append(("FAIL" if strict else "WARN", "PINMAP_MISSING", ref,
                        f"{ref} {mpn}: {npins} pins used and no pinmap — pin numbers "
                        f"without names cannot be checked against the datasheet"))
            continue
        if pinmap and passive and npins <= 2:
            continue                      # polarity of a 2-pin part: symbol convention
        if pinmap:
            src = c.get("pinmap_source")
            if isinstance(src, str) and src.startswith("kicad:"):
                # evidence = the KiCad official library symbol (datasheet-derived,
                # reviewed): every spec pin number must exist there with a
                # compatible role. The datasheet must still be on disk.
                okk, why = kicad_evidence(pinmap, src)
                if not okk:
                    out.append(("FAIL" if strict else "WARN", "PINMAP_EVIDENCE_WEAK", ref,
                                f"{ref} {mpn}: {why}"))
                continue
            sfile, pages = parse_source(src)
            if sfile:
                cand = [sfile, os.path.join(ds_dir or "", os.path.basename(sfile)),
                        os.path.join(os.path.dirname(ds_dir or ""), sfile)]
                sfile = next((p for p in cand if os.path.exists(p)), None)
            if not sfile and ent and f and os.path.exists(f):
                sfile, pages = f, []
                if src:
                    out.append(("INFO", "PINMAP_SOURCE_PROSE", ref,
                                f"{ref}: pinmap_source is prose, checked against the "
                                f"whole datasheet instead"))
            if not sfile:
                out.append(("FAIL" if strict else "WARN", "PINMAP_EVIDENCE_WEAK", ref,
                            f"{ref} {mpn}: pinmap has no datasheet to check against"))
                continue
            ok, found, missing, detail = evidence(pinmap, sfile, pages)
            if ok is None:
                out.append(("WARN", "PINMAP_EVIDENCE_UNREADABLE", ref,
                            f"{ref} {mpn}: {detail}"))
            elif not ok:
                out.append(("FAIL" if strict else "WARN", "PINMAP_EVIDENCE_WEAK", ref,
                            f"{ref} {mpn}: {detail}; not found: {', '.join(missing[:8])}"))
    return out

_TEMP_LINE = re.compile(
    r"(?:operating|ambient|working|functional)[^\n]{0,80}?temp(?:erature|\.)?[^\n]{0,140}?"
    r"(-\s?\d{1,3}|\b0)\s*(?:°\s*C|C\b|℃)?\s*(?:to|~|\.\.\.?|…|–|-|—|/)\s*\+?\s?(\d{1,3})\s*(?:°\s*C|C\b|℃)",
    re.I)


def datasheet_temp(pdf):
    """Operating-temperature range printed in the datasheet, as
    ((lo, hi), line) or None. The distributor's parametric field is a
    transcription; the sheet is the rating. DigiKey listed Omron's G6K
    relay at -40..+85 C while Omron's own sheet says 'Ambient operating
    temperature -40 to 70 C' — the review believed DigiKey (D70x)."""
    try:
        text = page_text(pdf) or ""
    except Exception:
        return None
    text = text.replace("−", "-").replace("\u2013", "-")
    best = None
    for m in _TEMP_LINE.finditer(text):
        try:
            lo = float(m.group(1).replace(" ", ""))
            hi = float(m.group(2))
        except ValueError:
            continue
        if lo >= hi or hi > 260 or lo < -100:
            continue
        line = text[max(0, m.start() - 10):m.end()].strip().replace("\n", " ")
        if "storage" in line.lower() or "junction" in line.lower():
            continue
        cand = ((lo, hi), line)
        # keep the NARROWEST range: the product's operating rating, not a
        # test-condition or a storage range that slipped past the filter
        if best is None or (hi - lo) < (best[0][1] - best[0][0]):
            best = cand
    return best
