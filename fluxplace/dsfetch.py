"""Datasheet source chain for an AI agent: ordered fallbacks, every attempt recorded, every file validated.

Order (each step is recorded with its URL and outcome, so a failure names what was tried):
  1. distributor APIs     DigiKey / Mouser / Nexar datasheet URL fields (sourcing.datasheet_urls_by_source)
  2. element14 / Farnell  the free product API (needs ELEMENT14_API_KEY; recorded as skipped without one), PDFs from its CDN
  3. LCSC / JLCPCB        jlcsearch (community) gives the LCSC code, JLCPCB's part-detail endpoint gives the PDF link
  4. manufacturer         URL templates for manufacturers that publish stable, programmatic PDF links (MFR_TEMPLATES)
  5. series               any candidate that carries only the series/base code is kept as a last resort and flagged "series"
Conduct: official APIs and endpoints that answer an ordinary client.  The client says what it is (CLIENT_UA); there is no
browser impersonation, no cookies, no CAPTCHA handling.  A 403/challenge is recorded as the outcome and the chain moves on.

Validation (validate_pdf): `%PDF` header, a minimum size, pdftotext text, and the part number (or its base code with the
packaging suffix removed) in the text -> "part"; only the series prefix -> "series"; neither -> rejected with the reason.
Results are cached in the datasheet manifest (and a 24 h negative cache); a valid file on disk is never fetched again.
"""
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .redact import urlopen as _urlopen, redact

CLIENT_UA = "fluxplace-parts/1.0 (datasheet fetch for a PCB build; honest client)"
MIN_BYTES = 8 * 1024
NEG_TTL = 24 * 3600
ATTEMPTS = ".fetch_attempts.json"

# (mpn regex, [url templates]); {mpn} = full MPN, {base} = packaging suffix dropped, {lc} = lower-case MPN without a trailing
# packaging/grade suffix.  Only hosts that serve plain clients belong here.
MFR_TEMPLATES = [
    (r"^(LM|TPS|ADS|ISO|TLV|OPA|INA|TMP|SN7|CDC|DRV|BQ|TCA|TXB|TXS|ESD|LMR|TPD|THS|PCM|TLC|REF|ADC|DAC|UCC)[A-Z0-9-]+",
     ["https://www.ti.com/lit/ds/symlink/{ti}.pdf"]),
]


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for ch in iter(lambda: fh.read(1 << 16), b""):
            h.update(ch)
    return h.hexdigest()


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def part_tokens(mpn):
    """-> (part_tokens, series_tokens): normalised strings whose presence in a document means 'this part' / 'this series'.
    Part: the MPN, and the MPN with up to 3 trailing packaging characters dropped (never below 8 chars).  Series: the first
    ~60 % of the MPN (>= 6 chars)."""
    n = _norm(mpn)
    part = {n}
    for k in (1, 2, 3):
        if len(n) - k >= 8:
            part.add(n[:-k])
    ser = n[:max(6, int(len(n) * 0.6))]
    return part, ser


def pdf_text(path, first=None, last=None):
    cmd = ["pdftotext", "-layout"] + (["-f", str(first), "-l", str(last)] if first else []) + [path, "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else ""


def validate_pdf(path, mpn, min_bytes=MIN_BYTES):
    """-> (level, reason): level 'part' | 'series' | None (rejected, `reason` says why)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError as e:
        return None, f"unreadable: {e.__class__.__name__}"
    if not head.startswith(b"%PDF"):
        return None, f"not a PDF (starts {head[:6]!r})"
    if size < min_bytes:
        return None, f"too small ({size} bytes)"
    txt = pdf_text(path)
    if txt is None:
        return None, "pdftotext unavailable: cannot check the part number"
    if len(txt.strip()) < 200:
        return None, "no extractable text (scan or drawing-only): cannot confirm the part number"
    flat = _norm(txt)
    part, ser = part_tokens(mpn)
    hit = next((t for t in sorted(part, key=len, reverse=True) if t and t in flat), None)
    if hit:
        return "part", f"carries {hit}"
    if ser in flat:
        return "series", f"carries series code {ser}, not the part number"
    return None, f"neither {_norm(mpn)} nor its series code {ser} appears in the text (wrong or generic document)"


# --------------------------------------------------------------------- transport
def get(url, timeout=30, headers=None):
    """-> (status, content_type, bytes).  Plain client, honest user agent; one JavaScript `location.href=` hop is followed
    (the site's own redirect page), nothing else is worked around."""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    for _hop in range(2):
        req = urllib.request.Request(url, headers={"User-Agent": CLIENT_UA, "Accept": "application/pdf,application/json,*/*",
                                                   **(headers or {})})
        try:
            with _urlopen(req, timeout=timeout) as r:
                data, ct, st = r.read(), r.headers.get("Content-Type", ""), r.status
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", "") if e.headers else "", b""
        m = re.search(rb"location\.href\s*=\s*['\"]([^'\"]+)", data[:400]) if not data.startswith(b"%PDF") else None
        if m:
            url = m.group(1).decode("latin1")
            continue
        return st, ct, data
    return st, ct, data


def _json(url, timeout=30, headers=None):
    st, _ct, data = get(url, timeout, headers)
    if st != 200:
        raise RuntimeError(f"HTTP {st}")
    return json.loads(data)


# --------------------------------------------------------------------- sources
def packaging_variants(mpn):
    """The MPN plus the spellings a distributor may list the same part under (reel/packaging code dropped), tried in this order."""
    out = [mpn]
    if re.match(r"^G[RC]M|^CL\d|^C\d{4}[A-Z]", mpn.upper()) and len(mpn) >= 12:
        out.append(mpn[:-1])                       # Murata / Samsung / TDK: last character is the packaging code
    elif re.match(r"^CL\d\d[A-Z]\d{3}[A-Z]{2}[A-Z]{3}[EFG]$", mpn.upper()):
        out.append(mpn[:-1] + ("E" if mpn[-1].upper() != "E" else "G"))      # Samsung reel/carrier code
    elif re.search(r"[A-Z]{2,}(R|T)$", mpn.upper()):
        out.append(mpn[:-1] + ("T" if mpn.upper().endswith("R") else "R"))      # TI: R / T reel sizes
    return out


def src_distributors(mpn, creds):
    from . import sourcing as S
    out = []
    for v in packaging_variants(mpn):
        for src, u in S.datasheet_urls_by_source(v, {k: x for k, x in creds.items() if not k.startswith("NEXAR")}):
            out.append((src if v == mpn else src + "-pkgvariant", u))
            m = re.search(r"gotoUrl=([^&]+)", u or "")
            if m and "ti.com" in u:                  # TI wraps its own PDF link in a distributor redirect page
                out.append((src + "-unwrapped", urllib.parse.unquote(m.group(1))))
    return out


def src_nexar(mpn, creds):
    """Nexar (Octopart): metered part lookups, so it runs only after the free sources."""
    from . import nexar as NX
    if not NX.available(creds):
        raise LookupError("skipped: no Nexar credentials")
    return [("nexar", u) for u in [NX.datasheet_url(mpn, creds)] if u]


def src_element14(mpn, creds, store="uk.farnell.com"):
    key = creds.get("ELEMENT14_API_KEY")
    if not key:
        raise LookupError("skipped: no ELEMENT14_API_KEY (free key at partner.element14.com)")
    q = urllib.parse.quote(f"manuPartNum:{mpn}")
    d = _json("https://api.element14.com/catalog/products?term=" + q + "&storeInfo.id=" + store +
              "&resultsSettings.responseGroup=large&resultsSettings.offset=0&resultsSettings.numberOfResults=5"
              "&callInfo.responseDataFormat=JSON&callInfo.apiKey=" + key)
    out = []
    for p in (d.get("manufacturerPartNumberSearchReturn") or d.get("keywordSearchReturn") or {}).get("products", []) or []:
        for ds in p.get("datasheets") or []:
            if ds.get("url"):
                out.append(("element14", ds["url"]))
    return out


def _same(listed, mpn):
    from . import dropin as DI, sourcing as S
    return S._same_part(listed, mpn)[0] or DI.packaging_equivalent(listed, mpn)


def _lcsc_codes(mpn):
    """LCSC codes for an MPN: jlcsearch (community mirror) first, JLCPCB's parts-search endpoint second; the full MPN, then
    the MPN with trailing packaging characters dropped (a listing may carry another reel code)."""
    from . import sourcing as S
    n = re.sub(r"[^A-Za-z0-9]", "", mpn)
    seen, codes = set(), []
    for q in [mpn] + [n[:-k] for k in (1, 2) if len(n) - k >= 8]:
        try:
            for c in _json("https://jlcsearch.tscircuit.com/components/list.json?search=" + urllib.parse.quote(q)).get("components", [])[:8]:
                if c.get("lcsc") and _same(c.get("mfr") or "", mpn) and c["lcsc"] not in seen:
                    seen.add(c["lcsc"])
                    codes.append("C%s" % c["lcsc"])
        except Exception:                                              # noqa: BLE001 - mirror down: try the other endpoint
            pass
        if codes:
            return codes
    for q in (mpn, n[:-1]):
        body = json.dumps({"currentPage": 1, "pageSize": 10, "keyword": q, "searchSource": "search"}).encode()
        req = urllib.request.Request("https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList",
                                     data=body, headers={"User-Agent": CLIENT_UA, "Content-Type": "application/json"})
        try:
            with _urlopen(req, timeout=30) as r:
                lst = (((json.load(r).get("data") or {}).get("componentPageInfo") or {}).get("list")) or []
        except Exception:                                              # noqa: BLE001
            continue
        for c in lst:
            if _same(c.get("componentModelEn") or "", mpn) and c.get("componentCode") not in seen:
                seen.add(c["componentCode"])
                codes.append(c["componentCode"])
        if codes:
            break
    return codes


def src_lcsc(mpn, creds):
    out = []
    for code in _lcsc_codes(mpn)[:3]:
        det = _json("https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode=" + code).get("data") or {}
        for k in ("dataManualUrl", "dataManualOfficialLink", "dataManualFileAccessIdUrl"):
            u = det.get(k)
            if u:
                out.append(("lcsc", u))
                m = re.match(r"https?://www\.lcsc\.com/datasheet/lcsc_datasheet_(.+\.pdf)$", u)
                if m:       # older records name the page-style link; LCSC's file host serves the same file under the v2 path
                    out.append(("lcsc", "https://wmsc.lcsc.com/wmsc/upload/file/pdf/v2/lcsc/" + m.group(1)))
    return out


def ti_name(mpn):
    """TI's symlink name is the orderable family: LM61460AASQRJRRQ1 -> lm61460-q1 is not derivable from the MPN alone, so the
    candidates are the leading letters+digits, with and without a -q1 tail."""
    m = re.match(r"^([A-Z]+\d+[A-Z]?\d*)", mpn.upper())
    return m.group(1).lower() if m else mpn.lower()


def src_manufacturer(mpn, creds):
    out = []
    for rx, tpls in MFR_TEMPLATES:
        if re.match(rx, mpn.upper()):
            ti = ti_name(mpn)
            names = [ti] + ([ti + "-q1"] if re.search(r"Q1$|Q1[A-Z]*$", mpn.upper()) else [])
            for t in tpls:
                for nm in names:
                    out.append(("manufacturer", t.format(mpn=mpn, base=mpn[:-1], lc=mpn.lower(), ti=nm)))
    return out


SOURCES = [("distributor", src_distributors), ("element14", src_element14), ("lcsc", src_lcsc), ("manufacturer", src_manufacturer),
           ("nexar", src_nexar)]


# --------------------------------------------------------------------- manifest
def _manifest(ds_dir):
    p = os.path.join(ds_dir, "datasheets.json")
    try:
        return p, json.load(open(p))
    except Exception:
        return p, {}


def _attempts(ds_dir):
    p = os.path.join(ds_dir, ATTEMPTS)
    try:
        return p, json.load(open(p))
    except Exception:
        return p, {}


def _safe(mpn):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", mpn).strip("_") or "part"


def fetch_datasheet(mpn, ds_dir, creds=None, refresh=False, log=lambda m: None, extra_urls=(), sources=None, now=time.time):
    """Run the chain for one MPN.  -> {"mpn", "ok", "level" ('part'|'series'|None), "file", "source", "url", "cached", "tried": [...]}.
    `tried` entries: {"source", "url", "outcome"}.  The manifest entry gets `level` and the successful URL; failures are written
    to ds_dir/.fetch_attempts.json (24 h negative cache) with everything that was tried."""
    from .models import credentials
    creds = creds if creds is not None else credentials()
    os.makedirs(ds_dir, exist_ok=True)
    mpath, man = _manifest(ds_dir)
    ent = man.get(mpn)
    if ent and not refresh:
        f = os.path.join(ds_dir, ent.get("file", ""))
        if os.path.exists(f) and ent.get("sha256") == _sha(f):
            return {"mpn": mpn, "ok": True, "level": ent.get("level") or "part", "file": ent["file"], "source": ent.get("source"),
                    "url": ent.get("source"), "cached": True, "tried": []}
    apath, att = _attempts(ds_dir)
    old = att.get(mpn)
    if old and not refresh and now() - old.get("at", 0) < NEG_TTL:
        return {**old["result"], "cached": True}
    tried, series_hit = [], None
    dest = os.path.join(ds_dir, _safe(mpn) + ".pdf")
    cands = [("given", u) for u in extra_urls]
    for name, fn in (sources or SOURCES):
        try:
            got = fn(mpn, creds)
        except LookupError as e:
            tried.append({"source": name, "url": None, "outcome": str(e)})
            continue
        except Exception as e:                                         # noqa: BLE001 - a source failing is an outcome
            tried.append({"source": name, "url": None, "outcome": redact(f"{type(e).__name__}: {e}")[:160]})
            continue
        if not got:
            tried.append({"source": name, "url": None, "outcome": "no datasheet URL offered"})
        cands += got
    seen = set()
    for name, url in cands:
        if url in seen:
            continue
        seen.add(url)
        tmp = dest + ".part"
        try:
            st, ct, data = get(url)
        except Exception as e:                                         # noqa: BLE001
            tried.append({"source": name, "url": redact(url), "outcome": redact(f"{type(e).__name__}: {e}")[:120]})
            continue
        if st != 200:
            tried.append({"source": name, "url": redact(url), "outcome": f"HTTP {st}"})
            continue
        with open(tmp, "wb") as fh:
            fh.write(data)
        level, why = validate_pdf(tmp, mpn)
        tried.append({"source": name, "url": redact(url), "outcome": ("accepted " if level else "rejected: ") + why +
                      (f" [{level}]" if level else "")})
        if level == "part":
            os.replace(tmp, dest)
            return _finish(mpn, ds_dir, dest, name, url, "part", tried, man, mpath, att, apath)
        if level == "series" and series_hit is None:
            keep = dest + ".series"
            os.replace(tmp, keep)
            series_hit = (name, url, keep)
        elif os.path.exists(tmp):
            os.remove(tmp)
    if series_hit:
        name, url, keep = series_hit
        os.replace(keep, dest)
        return _finish(mpn, ds_dir, dest, name, url, "series", tried, man, mpath, att, apath)
    res = {"mpn": mpn, "ok": False, "level": None, "file": None, "source": None, "url": None, "cached": False, "tried": tried}
    att[mpn] = {"at": now(), "result": res}
    json.dump(att, open(apath, "w"), indent=1)
    if os.path.exists(dest + ".series"):
        os.remove(dest + ".series")
    log(f"    {mpn}: not fetched; tried " + "; ".join(f"{t['source']}: {t['outcome']}" for t in tried))
    return res


def _finish(mpn, ds_dir, dest, name, url, level, tried, man, mpath, att, apath):
    ent = {"file": os.path.basename(dest), "sha256": _sha(dest), "source": redact(url), "fetched": time.strftime("%Y-%m-%d"), "level": level,
           "via": name}
    man[mpn] = ent
    json.dump(man, open(mpath, "w"), indent=1, sort_keys=True)
    att.pop(mpn, None)
    json.dump(att, open(apath, "w"), indent=1)
    return {"mpn": mpn, "ok": True, "level": level, "file": ent["file"], "source": name, "url": redact(url), "cached": False, "tried": tried}


def adopt_file(mpn, ds_dir, path, source="manual", log=lambda m: None):
    """Validate a hand/agent-supplied PDF the same way and register it.  -> (ok, level, reason)."""
    level, why = validate_pdf(path, mpn)
    if not level:
        return False, None, why
    dest = os.path.join(ds_dir, _safe(mpn) + ".pdf")
    if os.path.abspath(path) != os.path.abspath(dest):
        with open(path, "rb") as a, open(dest, "wb") as b:
            b.write(a.read())
    mpath, man = _manifest(ds_dir)
    man[mpn] = {"file": os.path.basename(dest), "sha256": _sha(dest), "source": source, "fetched": time.strftime("%Y-%m-%d"), "level": level}
    json.dump(man, open(mpath, "w"), indent=1, sort_keys=True)
    return True, level, why
