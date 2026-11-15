"""Distributor keyword search for `parts-todo` candidates, and the live lookup/stock callables `parts-apply` uses.

DigiKey first, Mouser second (Nexar is only used by partdata when both miss an exact MPN).  Only the query text is sent: a value, a
package size or an MPN.  Credentials come from the existing loader (models.credentials) and are never printed or stored here.
Network code only; the validation logic that consumes it is in partsgate.py and is tested with injected callables.
"""
import json
import time
import urllib.error
import urllib.request

from . import partdata as PDA, sourcing as S
from .models import credentials
from .redact import urlopen as _urlopen


def _dk_keyword(query, creds, tok, limit):
    body = json.dumps({"Keywords": query, "Limit": limit,
                       "FilterOptionsRequest": {"MarketPlaceFilter": "ExcludeMarketPlace"}}).encode()
    for attempt in range(3):
        wait = S._DK_MIN_INTERVAL - (time.time() - S._last_dk[0])
        if wait > 0:
            time.sleep(wait)
        S._last_dk[0] = time.time()
        req = urllib.request.Request("https://api.digikey.com/products/v4/search/keyword", data=body,
                                     headers={"Content-Type": "application/json", "X-DIGIKEY-Client-Id": creds["DIGIKEY_CLIENT_ID"],
                                              "Authorization": "Bearer " + tok})
        try:
            return json.load(_urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
    return {}


def _dk_rows(d, qty):
    out = []
    for p in d.get("Products", []) or []:
        params = {}
        for q in p.get("Parameters", []) or []:
            k, v = q.get("ParameterText") or q.get("Parameter") or "", q.get("ValueText") or q.get("Value") or ""
            if k and v:
                params[k] = v
        n = PDA.normalize(params)
        unit, moq = S.unit_price_at(S._breaks_dk(p), qty)
        out.append({"mpn": p.get("ManufacturerProductNumber", ""),
                    "mfr": (p.get("Manufacturer") or {}).get("Name", "") if isinstance(p.get("Manufacturer"), dict) else "",
                    "stock": p.get("QuantityAvailable", 0), "unit": unit, "moq": moq,
                    "ext": None if unit is None else round(unit * max(qty, moq or 1), 4), "source": "digikey",
                    "datasheet": p.get("DatasheetUrl") or "", "lifecycle": (p.get("ProductStatus") or {}).get("Status", ""),
                    "package": n["package"], "temp_min": n["temp_min"], "temp_max": n["temp_max"],
                    "params": {k: params[k] for k in list(params)[:14]}})
    return out


def _mouser_rows(query, creds, limit, qty):
    body = json.dumps({"SearchByKeywordRequest": {"keyword": query, "records": limit, "startingRecord": 0,
                                                  "searchOptions": "InStock"}}).encode()
    req = urllib.request.Request("https://api.mouser.com/api/v1/search/keyword?apiKey=" + creds["MOUSER_API_KEY"], data=body,
                                 headers={"Content-Type": "application/json"})
    wait = S._MOUSER_MIN_INTERVAL - (time.time() - S._last_mouser[0])
    if wait > 0:
        time.sleep(wait)
    S._last_mouser[0] = time.time()
    d = json.load(_urlopen(req, timeout=30))
    out = []
    for p in (d.get("SearchResults") or {}).get("Parts", []) or []:
        params = {a.get("AttributeName", ""): a.get("AttributeValue", "") for a in p.get("ProductAttributes", []) or []
                  if a.get("AttributeName") and a.get("AttributeValue")}
        n = PDA.normalize(params)
        digits = "".join(ch for ch in (p.get("Availability") or "0").split()[0] if ch.isdigit())
        unit, moq = S.unit_price_at(S._breaks_mouser(p), qty)
        out.append({"mpn": p.get("ManufacturerPartNumber", ""), "mfr": p.get("Manufacturer") or "", "stock": int(digits or 0),
                    "unit": unit, "moq": moq, "ext": None if unit is None else round(unit * max(qty, moq or 1), 4),
                    "source": "mouser", "datasheet": p.get("DataSheetUrl") or "", "lifecycle": p.get("LifecycleStatus") or "",
                    "package": n["package"], "temp_min": n["temp_min"], "temp_max": n["temp_max"],
                    "params": {k: params[k] for k in list(params)[:14]}})
    return out


def keyword_search(query, limit=10, qty=1):
    """DigiKey keyword search, Mouser only when DigiKey is absent or returns nothing.  -> candidate rows (no credentials: [])."""
    creds = credentials()
    rows = []
    if "DIGIKEY_CLIENT_ID" in creds and "DIGIKEY_CLIENT_SECRET" in creds:
        rows = _dk_rows(_dk_keyword(query, creds, S._dk_token(creds), limit), qty)
    if not rows and "MOUSER_API_KEY" in creds:
        rows = _mouser_rows(query, creds, limit, qty)
    seen, out = set(), []
    for r in rows:
        if r["mpn"] and r["mpn"] not in seen:
            seen.add(r["mpn"])
            out.append(r)
    return out


def lookup(mpn, cache_dir=None):
    """Exact-MPN distributor row (partdata.fetch: DigiKey, then Mouser, then Nexar), or None."""
    return PDA.fetch([mpn], cache_dir=cache_dir, log=lambda m: None).get(mpn)


def stocker(qty, cache_dir=None):
    """-> callable(mpn, refs) -> {verdict, stock} graded at per-board count x qty (the flat stock_need when qty is None)."""
    from . import parts as P

    def f(mpn, refs):
        r = P.fetch_stock([mpn], cache_dir=cache_dir, refs={mpn: list(refs)}, qty=qty, log=lambda m: None)
        return (r or {}).get(mpn)
    return f
