"""Part DATA from the distributors — not just "is it in stock" but WHAT IT IS.

sourcing.py asks DigiKey and Mouser whether a part can be bought. This module
asks them what the part physically and electrically is, so the review gate can
hold the design against the manufacturer's truth instead of the designer's
memory:

  package        "SOT-23-5", "PowerDI5060-8", "0603 (1608 Metric)"
  pins           pin/lead count when the distributor states it
  temp_min/max   operating temperature range (deg C)
  lifecycle      Active / Obsolete / NRND ...
  clamp_v        TVS clamping voltage at Ipp, when published
  datasheet      URL

Motivating misses (utv-comms V1.4 external review, 2026-09-03): a P-FET whose
MPN is a PowerDI5060-8 sat on a SOIC-8 land pattern; a 4-line ESD array was
mapped as a 5-pin part with ground on the wrong pin; a LAN transformer rated
0..+70 C went onto an outdoor vehicle board. All three were one API call away
and nobody made it. DigiKey + Mouser only (accurate-sourcing policy).

Every lookup is cached (7 days — a package does not change daily) and every
failure is soft: a part with no data is reported as UNCHECKED, never silently
passed and never a hard abort over an API hiccup.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

from .models import credentials
from . import sourcing as S

CACHE_NAME = ".partdata_cache.json"
CACHE_TTL = 7 * 24 * 3600

__all__ = ["fetch", "parse_temp_range", "parse_pins", "parse_clamp_v", "normalize"]


# ------------------------------------------------------------------ parsing
_TEMP_RE = re.compile(
    r"(-?\s?\d+(?:\.\d+)?)\s*°?\s*C?\s*(?:~|to|-|–|\.\.)\s*\+?\s?(-?\d+(?:\.\d+)?)\s*°?\s*C",
    re.I)


def parse_temp_range(text):
    """'-40°C ~ 125°C (TJ)' / '0°C ~ 70°C' / '-40 C to +85 C' -> (min, max)
    in deg C, or None. A lone maximum ('125°C') is not a range."""
    if not text:
        return None
    t = text.replace("−", "-").replace(" ", " ")
    m = _TEMP_RE.search(t)
    if not m:
        return None
    lo = float(m.group(1).replace(" ", ""))
    hi = float(m.group(2).replace(" ", ""))
    if lo > hi:
        return None
    return lo, hi


def parse_pins(text):
    """'8' / '8-Pin' / '8 Leads' -> int or None. Package strings are NOT
    parsed here: 'SOT-23' would read as 23. review.package_key does that."""
    if not text:
        return None
    m = re.match(r"^\s*(\d+)\s*(?:-?\s*pins?|-?\s*leads?|-?\s*positions?|)\s*$",
                 text, re.I)
    return int(m.group(1)) if m else None


def parse_clamp_v(text):
    """'38.9V' / '38.9 V' -> float or None."""
    if not text:
        return None
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*V", text)
    return float(m.group(1)) if m else None


def _first(params, *names):
    """Case-insensitive lookup of the first present parameter."""
    low = {k.lower(): v for k, v in params.items()}
    for n in names:
        v = low.get(n.lower())
        if v:
            return v
    return None


def normalize(params):
    """Distributor parameter dict -> the fields the review gate consumes."""
    pkg = _first(params, "Supplier Device Package", "Package / Case",
                 "Package/Case", "Package", "Case")
    rng = (parse_temp_range(_first(params, "Operating Temperature",
                                   "Operating Temperature Range",
                                   "Temperature Range"))
           or None)
    if rng is None:
        lo = _first(params, "Minimum Operating Temperature",
                    "Operating Temperature - Min")
        hi = _first(params, "Maximum Operating Temperature",
                    "Operating Temperature - Max")
        if lo and hi:
            try:
                rng = (float(re.sub(r"[^\d.-]", "", lo.replace("+", ""))),
                       float(re.sub(r"[^\d.-]", "", hi.replace("+", ""))))
            except ValueError:
                rng = None
    pins = parse_pins(_first(params, "Number of Pins", "Number of Positions",
                             "Number of Leads", "Pin Count"))
    clamp = parse_clamp_v(_first(params, "Voltage - Clamping (Max) @ Ipp",
                                 "Clamping Voltage", "Voltage - Clamping"))
    return {"package": pkg, "pins": pins,
            "temp_min": rng[0] if rng else None,
            "temp_max": rng[1] if rng else None,
            "clamp_v": clamp}


# ------------------------------------------------------------------ digikey
def _dk_details(mpn, creds, tok, on_token=None):
    body = json.dumps({"Keywords": S._search_term(mpn), "Limit": 10,
                       "FilterOptionsRequest": {
                           "MarketPlaceFilter": "ExcludeMarketPlace"}}).encode()
    for attempt in range(3):
        wait = S._DK_MIN_INTERVAL - (time.time() - S._last_dk[0])
        if wait > 0:
            time.sleep(wait)
        S._last_dk[0] = time.time()
        req = urllib.request.Request(
            "https://api.digikey.com/products/v4/search/keyword", data=body,
            headers={"Content-Type": "application/json",
                     "X-DIGIKEY-Client-Id": creds["DIGIKEY_CLIENT_ID"],
                     "Authorization": "Bearer " + tok})
        try:
            d = json.load(urllib.request.urlopen(req, timeout=30))
            break
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt < 2:
                tok = S._dk_token(creds)
                if on_token:
                    on_token(tok)
                continue
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
    best = None
    for p in d.get("Products", []):
        ok, exact = S._same_part(p.get("ManufacturerProductNumber", ""), mpn)
        if not ok:
            continue
        params = {}
        for q in p.get("Parameters", []) or []:
            k = q.get("ParameterText") or q.get("Parameter") or ""
            v = q.get("ValueText") or q.get("Value") or ""
            if k and v:
                params[k] = v
        row = normalize(params)
        row.update({
            "lifecycle": (p.get("ProductStatus") or {}).get("Status", ""),
            "datasheet": p.get("DatasheetUrl") or "",
            "description": (p.get("Description") or {}).get("ProductDescription", "")
            if isinstance(p.get("Description"), dict) else (p.get("Description") or ""),
            "matched": p.get("ManufacturerProductNumber", ""),
            "source": "digikey", "raw_params": params,
        })
        if exact:
            return row
        best = best or row
    return best


# ------------------------------------------------------------------- mouser
def _mouser_details(mpn, creds):
    terms = [mpn] if S._search_term(mpn) == mpn else [mpn, S._search_term(mpn)]
    for term in terms:
        body = json.dumps({"SearchByPartRequest": {
            "mouserPartNumber": term, "partSearchOptions": "Exact"}}).encode()
        req = urllib.request.Request(
            "https://api.mouser.com/api/v1/search/partnumber?apiKey="
            + creds["MOUSER_API_KEY"], data=body,
            headers={"Content-Type": "application/json"})
        d = None
        for attempt in range(3):
            wait = S._MOUSER_MIN_INTERVAL - (time.time() - S._last_mouser[0])
            if wait > 0:
                time.sleep(wait)
            S._last_mouser[0] = time.time()
            try:
                d = json.load(urllib.request.urlopen(req, timeout=30))
                break
            except urllib.error.HTTPError as e:
                if e.code in (403, 429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise
        for p in (d or {}).get("SearchResults", {}).get("Parts", []) or []:
            ok, _ = S._same_part(p.get("ManufacturerPartNumber", ""), mpn)
            if not ok:
                continue
            params = {}
            for q in p.get("ProductAttributes", []) or []:
                k, v = q.get("AttributeName", ""), q.get("AttributeValue", "")
                if k and v:
                    params[k] = v
            row = normalize(params)
            row.update({"lifecycle": p.get("LifecycleStatus") or "",
                        "datasheet": p.get("DataSheetUrl") or "",
                        "description": p.get("Description") or "",
                        "matched": p.get("ManufacturerPartNumber", ""),
                        "source": "mouser", "raw_params": params})
            return row
    return None


# -------------------------------------------------------------------- fetch
def fetch(mpns, cache_dir=None, refresh=False, log=print, creds=None):
    """{mpn: data|None}. None = neither distributor could describe the part
    (or no credentials). A DigiKey answer is preferred; Mouser fills the gaps
    (DigiKey often omits temperature for passives, Mouser omits pin counts)."""
    creds = creds if creds is not None else credentials()
    have_dk = "DIGIKEY_CLIENT_ID" in creds and "DIGIKEY_CLIENT_SECRET" in creds
    have_mo = "MOUSER_API_KEY" in creds
    out = {}
    if not have_dk and not have_mo:
        log("    partdata: no DigiKey/Mouser credentials — part data unchecked")
        return {m: None for m in mpns}
    cache_path = os.path.join(cache_dir or ".", CACHE_NAME)
    cache = {}
    if not refresh and os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path))
        except Exception:
            cache = {}
    tok = None
    for mpn in sorted(set(mpns)):
        hit = cache.get(mpn)
        if hit and time.time() - hit.get("_t", 0) < CACHE_TTL:
            out[mpn] = hit.get("data")
            continue
        dk = mo = None
        if have_dk:
            try:
                if tok is None:
                    tok = S._dk_token(creds)

                def _keep(t):
                    nonlocal tok
                    tok = t
                dk = _dk_details(mpn, creds, tok, on_token=_keep)
            except Exception as e:
                log(f"    ! DigiKey data {mpn}: {e}")
        need_mo = (dk is None or dk.get("package") is None
                   or dk.get("temp_min") is None)
        if have_mo and need_mo:
            try:
                mo = _mouser_details(mpn, creds)
            except Exception as e:
                log(f"    ! Mouser data {mpn}: {e}")
        data = None
        if dk or mo:
            data = dict(mo or {})
            for k, v in (dk or {}).items():       # DigiKey wins where it answers
                if v not in (None, "", {}):
                    data[k] = v
            if mo and dk:
                data["source"] = "digikey+mouser"
        out[mpn] = data
        if data is not None:                        # never cache an outage
            cache[mpn] = {"_t": time.time(), "data": data}
    try:
        json.dump(cache, open(cache_path, "w"), indent=1)
    except Exception:
        pass
    return out
