"""Nexar (Octopart) — the third source, beside DigiKey and Mouser.

What it adds that the two distributors do not: datasheets mirrored on
datasheet.octopart.com (which serves plain PDFs where the manufacturers'
own hosts bot-wall a script), cross-distributor stock in one query, and a
normalised spec set (package, operating temperature, lifecycle) for parts
the two APIs describe thinly.

Auth: client-credentials against identity.nexar.com, scope supply.domain;
tokens live ~24 h and are cached in memory. Credentials come from
models.credentials() (NEXAR_CLIENT_ID / NEXAR_CLIENT_SECRET, env or
~/.claude.json). Every failure is soft — a part Nexar cannot find is None.
Docs: https://support.nexar.com/support/solutions/101000253221/
"""
import json
import time
import urllib.parse
import urllib.request

__all__ = ["available", "token", "search", "datasheet_url", "part_data"]

_TOKEN = {"value": None, "exp": 0.0}
_GQL = "https://api.nexar.com/graphql"
_IDENTITY = "https://identity.nexar.com/connect/token"

_QUERY = """
query ($q: String!, $limit: Int!) {
  supSearchMpn(q: $q, limit: $limit) {
    results {
      part {
        mpn
        manufacturer { name }
        bestDatasheet { url }
        documentCollections { name documents { name url } }
        specs { attribute { shortname name } displayValue }
        sellers { company { name } offers { inventoryLevel moq } }
      }
    }
  }
}
"""


def available(creds):
    return bool(creds.get("NEXAR_CLIENT_ID") and creds.get("NEXAR_CLIENT_SECRET"))


def token(creds):
    if _TOKEN["value"] and time.time() < _TOKEN["exp"] - 60:
        return _TOKEN["value"]
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": creds["NEXAR_CLIENT_ID"],
        "client_secret": creds["NEXAR_CLIENT_SECRET"],
        "scope": "supply.domain"}).encode()
    req = urllib.request.Request(_IDENTITY, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    _TOKEN["value"] = d["access_token"]
    _TOKEN["exp"] = time.time() + float(d.get("expires_in", 3600))
    return _TOKEN["value"]


def search(mpn, creds, limit=3):
    """Raw Nexar parts for an MPN (best match first), or [] on any failure."""
    from .sourcing import _same_part
    try:
        tok = token(creds)
        body = json.dumps({"query": _QUERY, "variables": {"q": mpn, "limit": limit}}).encode()
        req = urllib.request.Request(_GQL, data=body, headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + tok})
        with urllib.request.urlopen(req, timeout=40) as r:
            d = json.load(r)
    except Exception:
        return []
    parts = []
    for res in ((d.get("data") or {}).get("supSearchMpn") or {}).get("results") or []:
        p = res.get("part") or {}
        ok, exact = _same_part(p.get("mpn") or "", mpn)
        if ok:
            parts.append((0 if exact else 1, p))
    parts.sort(key=lambda x: x[0])
    return [p for _, p in parts]


def datasheet_url(mpn, creds):
    """Octopart-hosted datasheet URL (bestDatasheet, then any 'Datasheet'
    document), or None."""
    for p in search(mpn, creds):
        bd = (p.get("bestDatasheet") or {}).get("url")
        if bd:
            return bd
        for col in p.get("documentCollections") or []:
            if "datasheet" in (col.get("name") or "").lower():
                for doc in col.get("documents") or []:
                    if doc.get("url"):
                        return doc["url"]
    return None


def _spec(p, *names):
    for s in p.get("specs") or []:
        a = s.get("attribute") or {}
        keys = {(a.get("shortname") or "").lower(), (a.get("name") or "").lower()}
        if any(n.lower() in keys for n in names):
            v = s.get("displayValue")
            if v:
                return v
    return None


def part_data(mpn, creds):
    """{package, pins, temp_min, temp_max, lifecycle, datasheet, stock,
    source:'nexar'} for the review gate's partdata, or None."""
    from .partdata import parse_temp_range, parse_pins
    parts = search(mpn, creds)
    if not parts:
        return None
    p = parts[0]
    pkg = _spec(p, "case_package", "Case/Package", "package") or \
        _spec(p, "supplier_device_package", "Supplier Device Package")
    tmin = _spec(p, "minoperatingtemperature", "Min Operating Temperature",
                 "operatingtemperature_min")
    tmax = _spec(p, "maxoperatingtemperature", "Max Operating Temperature",
                 "operatingtemperature_max")
    rng = None
    if tmin and tmax:
        try:
            rng = (float(tmin.replace("°C", "").replace("C", "").strip()),
                   float(tmax.replace("°C", "").replace("C", "").strip()))
        except ValueError:
            rng = None
    if rng is None:
        rng = parse_temp_range(_spec(p, "operatingtemperature", "Operating Temperature"))
    stock = 0
    for s in p.get("sellers") or []:
        for o in s.get("offers") or []:
            stock += int(o.get("inventoryLevel") or 0)
    return {"package": pkg, "pins": parse_pins(_spec(p, "numberofpins", "Number of Pins")),
            "temp_min": rng[0] if rng else None, "temp_max": rng[1] if rng else None,
            "lifecycle": _spec(p, "lifecyclestatus", "Lifecycle Status") or "",
            "datasheet": (p.get("bestDatasheet") or {}).get("url") or "",
            "manufacturer": (p.get("manufacturer") or {}).get("name") or "",
            "stock": stock, "source": "nexar", "raw_params": {
                (s.get("attribute") or {}).get("name") or "": s.get("displayValue")
                for s in p.get("specs") or []}}
