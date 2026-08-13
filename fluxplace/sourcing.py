"""Sourcing pre-flight: is every part on this board actually buyable?

Placement is the point of no return for a part choice — once a footprint is
placed, routed and DRC'd, discovering that nobody stocks the part means
re-doing the layout, not re-doing a purchase order. So the placer asks the
distributors FIRST.

Motivating failure (utv-comms-bridge D41/D43): an Ethernet magjack taken from
a reference design cleared placement, routing, DRC and fab packaging before
anyone queried a distributor — DigiKey did not carry it and Mouser had 0 stock
with a 140-day lead. Building this check then found five MORE zero-stock parts
already committed to that board.

DigiKey + Mouser only, per the accurate-sourcing policy in models.py.

Verdicts:
  OK    >= `need` units in stock, Active
  LOW   in stock somewhere, under the threshold
  LEAD  catalogued but 0 stock — a lead-time buy
  RISK  obsolete / EOL / NRND / last-time-buy
  NONE  neither distributor carries it        <-- a design bug, not a PO problem
"""
import json
import os
import time
import urllib.parse
import urllib.request

from .models import credentials

CACHE_NAME = ".sourcing_cache.json"
CACHE_TTL = 24 * 3600          # stock moves daily, not minutely
BAD = ("obsolete", "discontinued", "end of life", "eol", "nrnd",
       "not recommended", "last time buy")
BLOCKERS = ("NONE", "RISK")


# ------------------------------------------------------------------ lookups
def _dk_token(creds):
    body = urllib.parse.urlencode(dict(
        client_id=creds["DIGIKEY_CLIENT_ID"],
        client_secret=creds["DIGIKEY_CLIENT_SECRET"],
        grant_type="client_credentials")).encode()
    req = urllib.request.Request(
        "https://api.digikey.com/v1/oauth2/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.load(urllib.request.urlopen(req, timeout=30))["access_token"]


def _dk(mpn, creds, tok):
    body = json.dumps({"Keywords": mpn, "Limit": 5,
                       "FilterOptionsRequest": {
                           "MarketPlaceFilter": "ExcludeMarketPlace"}}).encode()
    req = urllib.request.Request(
        "https://api.digikey.com/products/v4/search/keyword", data=body,
        headers={"Content-Type": "application/json",
                 "X-DIGIKEY-Client-Id": creds["DIGIKEY_CLIENT_ID"],
                 "Authorization": "Bearer " + tok})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    for p in d.get("Products", []):
        # keyword search is fuzzy: only an exact MPN hit counts
        if p.get("ManufacturerProductNumber", "").upper() != mpn.upper():
            continue
        return [p.get("QuantityAvailable", 0),
                (p.get("ProductStatus") or {}).get("Status", "?"),
                p.get("UnitPrice"), ""]
    return None


def _mouser(mpn, creds):
    """Mouser's Availability is a STRING ('1,234 In Stock'); the integer field
    is unreliable — parse the string (utv-comms V1.2 lesson)."""
    body = json.dumps({"SearchByPartRequest": {
        "mouserPartNumber": mpn, "partSearchOptions": "Exact"}}).encode()
    req = urllib.request.Request(
        "https://api.mouser.com/api/v1/search/partnumber?apiKey="
        + creds["MOUSER_API_KEY"], data=body,
        headers={"Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    for p in (d.get("SearchResults") or {}).get("Parts", []) or []:
        if p.get("ManufacturerPartNumber", "").upper() != mpn.upper():
            continue
        digits = "".join(c for c in (p.get("Availability") or "0").split()[0]
                         if c.isdigit())
        breaks = p.get("PriceBreaks") or []
        return [int(digits or 0), p.get("LifecycleStatus") or "Active",
                (breaks[0].get("Price") if breaks else None),
                p.get("LeadTime") or ""]
    return None


def grade(dk, mo, need):
    stock = (dk[0] if dk else 0) + (mo[0] if mo else 0)
    status = " ".join(str(x[1]) for x in (dk, mo) if x).lower()
    if any(b in status for b in BAD):
        return "RISK", stock
    if not dk and not mo:
        return "NONE", 0
    if stock >= need:
        return "OK", stock
    return ("LOW", stock) if stock else ("LEAD", 0)


# ------------------------------------------------------------------ mpn map
def find_map(board_path):
    """Locate an mpn_map.json for this board without being told.

    Looks beside the board, then in sibling tool/doc dirs — the common KiCad
    project shapes (hardware/<proj>/board.kicad_pcb + hardware/tools/)."""
    d = os.path.dirname(os.path.abspath(board_path))
    for cand in (os.path.join(d, "mpn_map.json"),
                 os.path.join(d, "..", "tools", "mpn_map.json"),
                 os.path.join(d, "..", "mpn_map.json"),
                 os.path.join(d, "..", "..", "tools", "mpn_map.json")):
        if os.path.exists(cand):
            return os.path.normpath(cand)
    return None


def load_map(path):
    raw = json.load(open(path))
    out = {}
    for ref, mpn in raw.items():
        if ref.startswith("_") or not isinstance(mpn, str) or not mpn:
            continue
        out.setdefault(mpn, []).append(ref)
    return out


# ------------------------------------------------------------------- check
def check(by_mpn, need=10, cache_dir=None, refresh=False, log=print):
    """-> (report, counts). Never raises on a per-part API error: an outage
    must not block a layout, it just leaves that part ungraded."""
    creds = credentials()
    if "DIGIKEY_CLIENT_ID" not in creds and "MOUSER_API_KEY" not in creds:
        log("    sourcing: no DigiKey/Mouser credentials — skipped")
        return {}, {}
    cache_path = os.path.join(cache_dir or ".", CACHE_NAME)
    cache = {}
    if not refresh and os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path))
        except Exception:
            cache = {}
    tok, report, counts = None, {}, {}
    for mpn in sorted(by_mpn):
        hit = cache.get(mpn)
        if hit and time.time() - hit.get("_t", 0) < CACHE_TTL:
            dk, mo = hit.get("dk"), hit.get("mo")
        else:
            dk = mo = None
            try:
                if "DIGIKEY_CLIENT_ID" in creds:
                    if tok is None:
                        tok = _dk_token(creds)
                    dk = _dk(mpn, creds, tok)
            except Exception as e:
                log(f"    ! DigiKey {mpn}: {e}")
            try:
                if "MOUSER_API_KEY" in creds:
                    mo = _mouser(mpn, creds)
            except Exception as e:
                log(f"    ! Mouser {mpn}: {e}")
            cache[mpn] = {"_t": time.time(), "dk": dk, "mo": mo}
        verdict, stock = grade(dk, mo, need)
        counts[verdict] = counts.get(verdict, 0) + 1
        report[mpn] = {"refs": by_mpn[mpn], "verdict": verdict,
                       "stock": stock, "digikey": dk, "mouser": mo}
    try:
        json.dump(cache, open(cache_path, "w"))
    except Exception:
        pass
    return report, counts


def summary(report, counts, need, log=print, show_ok=False):
    """Print the non-OK lines + a one-line tally. -> list of blocker MPNs."""
    for mpn, r in sorted(report.items(),
                         key=lambda kv: (kv[1]["verdict"] == "OK", kv[0])):
        if r["verdict"] == "OK" and not show_ok:
            continue
        dk, mo = r["digikey"], r["mouser"]
        lead = (mo[3] if mo and len(mo) > 3 and mo[3] else "")
        log(f"    {r['verdict']:5s} {mpn:26s} "
            f"{('DK %d' % dk[0]) if dk else 'DK -':>9s} "
            f"{('MO %d' % mo[0]) if mo else 'MO -':>9s}"
            f"{('  lead ' + lead) if lead else ''}   [{' '.join(r['refs'])}]")
    tally = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    log(f"    sourcing: {len(report)} MPNs  {tally}  (need >= {need})")
    blockers = [m for m, r in report.items() if r["verdict"] in BLOCKERS]
    if blockers:
        log("    SOURCING BLOCKERS (not buyable / EOL): " + ", ".join(blockers))
    return blockers


def preflight(board_path, mpn_map=None, need=10, strict=False, refresh=False,
              log=print):
    """Run before placement. Returns the blocker list (empty when clean).

    With strict=True the caller should abort: placing a part nobody sells
    bakes a re-layout into the schedule."""
    path = mpn_map or find_map(board_path)
    if not path:
        log("    sourcing: no mpn_map.json found — skipped "
            "(pass --mpn-map to enable)")
        return []
    by_mpn = load_map(path)
    log(f"    sourcing pre-flight: {len(by_mpn)} MPNs from "
        f"{os.path.basename(path)}")
    report, counts = check(by_mpn, need=need,
                           cache_dir=os.path.dirname(path), refresh=refresh,
                           log=log)
    if not report:
        return []
    return summary(report, counts, need, log=log)
