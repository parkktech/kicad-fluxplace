"""LCSC / JLCPCB stock — the availability that matters for a China build.

DigiKey and Mouser stock says a part can be bought in the US. A board
assembled in Shenzhen is populated from LCSC (JLCPCB's parts library, and
what PCBWay's buyers reach for first). A part with 50k at DigiKey and 0 at
LCSC is an import, a delay and a substitution risk on the assembly line.

Data: JLCPCB's own parts endpoint (the one its SMT order page uses), fetched
with a browser TLS fingerprint — LCSC code, live stock, basic/extended tier,
price, minimum buy. LCSC's own site refuses scripts and the community
jlcsearch mirror is stale. Matching is by manufacturer part number with the
same normalisation the DigiKey/Mouser gate uses; a match on a different
package suffix is rejected (SI7155DP-T1-GE3-HXY is not SI7155DP-T1-GE3).

Grades (per part, `need` units):
  CN_OK      stock >= need
  CN_LOW     0 < stock < need
  CN_NONE    catalogued at LCSC, 0 stock   (order-in / consign)
  CN_ABSENT  not in the JLCPCB library at all  (consign, or pick another part)
"""
import json
import time

from .sourcing import _same_part

__all__ = ["lookup", "grade", "check"]

_API = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"
_MIN_INTERVAL = 0.6
_last = [0.0]
CACHE_TTL = 24 * 3600


def lookup(mpn, timeout=30):
    """Best exact JLCPCB/LCSC match for an MPN, or None. Uses JLCPCB's own
    parts endpoint (curl_cffi, browser fingerprint): the community jlcsearch
    mirror reported 22 units for a 10k resistor JLCPCB holds 14.6 million of
    (measured 2026-09-03). 'JLCPCB Assembly' pseudo-listings (0 stock,
    minimum-buy placeholders) are skipped when a real listing exists.
    Raises on transport error."""
    from curl_cffi import requests as cffi
    wait = _MIN_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    r = cffi.post(_API, json={"currentPage": 1, "pageSize": 10, "keyword": mpn,
                              "firstSortName": "", "secondSortName": "", "componentBrand": "",
                              "componentSpecification": "", "componentAttributes": [],
                              "searchSource": "search"},
                  impersonate="chrome", timeout=timeout,
                  headers={"Content-Type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError("JLCPCB HTTP %s" % r.status_code)
    d = r.json()
    lst = ((d.get("data") or {}).get("componentPageInfo") or {}).get("list") or []
    rows = []
    for c in lst:
        ok, exact = _same_part(c.get("componentModelEn") or "", mpn)
        if not ok:
            continue
        rows.append({"lcsc": c.get("componentCode"), "mfr": c.get("componentModelEn"),
                     "brand": c.get("componentBrandEn"), "stock": int(c.get("stockCount") or 0),
                     "basic": c.get("componentLibraryType") == "base",
                     "preferred": bool(c.get("preferredComponentFlag")),
                     "price": ((c.get("componentPrices") or [{}])[0]).get("productPrice"),
                     "package": c.get("componentSpecificationEn"), "exact": exact,
                     "min_buy": c.get("minPurchaseNum"),
                     "datasheet": c.get("dataManualUrl") or c.get("dataManualOfficialLink") or "",
                     "pseudo": (c.get("componentBrandEn") or "").lower().startswith("jlcpcb")})
    if not rows:
        return None
    real = [x for x in rows if not x["pseudo"]] or rows
    exact = [x for x in real if x["exact"]] or real
    return max(exact, key=lambda x: x["stock"])


def grade(row, need):
    if row is None:
        return "CN_ABSENT"
    if row["stock"] >= need:
        return "CN_OK"
    if row["stock"] > 0:
        return "CN_LOW"
    return "CN_NONE"


def check(mpns, need=100, cache_path=None, refresh=False, log=print):
    """{mpn: {"grade", "row"}} with a 24 h cache. need defaults to 100: a
    China assembly run wants a margin, not the 10 units a prototype needs."""
    cache = {}
    if cache_path and not refresh:
        try:
            cache = json.load(open(cache_path))
        except Exception:
            cache = {}
    out = {}
    for mpn in sorted(set(m for m in mpns if m)):
        hit = cache.get(mpn)
        if hit and time.time() - hit.get("_t", 0) < CACHE_TTL:
            row = hit.get("row")
        else:
            try:
                row = lookup(mpn)
            except Exception as e:
                log(f"    ! LCSC {mpn}: {e}")
                out[mpn] = {"grade": "ERR", "row": None}
                continue
            cache[mpn] = {"_t": time.time(), "row": row}
        out[mpn] = {"grade": grade(row, need), "row": row}
    if cache_path:
        try:
            json.dump(cache, open(cache_path, "w"), indent=1)
        except Exception:
            pass
    return out
