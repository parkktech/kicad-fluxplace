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
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .models import credentials

CACHE_NAME = ".sourcing_cache.json"
CACHE_TTL = 24 * 3600          # stock moves daily, not minutely
BAD = ("obsolete", "discontinued", "end of life", "eol", "nrnd",
       "not recommended", "last time buy")
BLOCKERS = ("NONE", "RISK")


# ------------------------------------------------------------------ matching
def _norm(s):
    """Compare MPNs the way a human does.

    Distributors and BOMs disagree on cosmetics that carry no engineering
    meaning: plating/packaging qualifiers in parentheses and stray spaces.
    Real cases that made this gate cry wolf: our 'BM03B-GHS-TBT(LF)(SN)' vs
    DigiKey's 'BM03B-GHS-TBT' (27k in stock), and our 'KMR221GLFS' vs
    DigiKey's 'KMR221G LFS' (95k in stock). Both were reported as "nobody
    carries this" and nearly triggered a needless part swap."""
    s = re.sub(r"\([^)]*\)", "", s or "")      # drop (LF), (SN), (LF)(SN) ...
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _same_part(candidate, mpn):
    """-> (matched?, exact?). A distributor MPN that merely EXTENDS ours is
    the same part in different packaging (tape/reel/bulk); accept it but let
    the caller show what it matched, so a substitution is never silent."""
    a, b = _norm(candidate), _norm(mpn)
    if not a or not b:
        return False, False
    if a == b:
        return True, True
    if (a.startswith(b) or b.startswith(a)) and abs(len(a) - len(b)) <= 4:
        return True, False
    return False, False


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


_DK_MIN_INTERVAL = 0.25         # s between calls; DigiKey throttles bursts too
_last_dk = [0.0]


def _search_term(mpn):
    """What to actually ASK the distributor for.

    Parenthesised plating/packaging qualifiers break DigiKey's keyword search:
    querying 'BM03B-GHS-TBT(LF)(SN)' returns nothing, while 'BM03B-GHS-TBT'
    returns the part with 27k in stock. Normalising only the COMPARISON was
    not enough — the query has to be clean too."""
    base = re.sub(r"\([^)]*\)", "", mpn or "").strip()
    return base or mpn


def _dk(mpn, creds, tok, retries=3, on_token=None):
    """Returns [stock, status, price, ''] or None (DigiKey answered: not
    carried). Raises if DigiKey never answered — see grade(errors=).

    Retries 429/5xx with backoff and RE-AUTHENTICATES on 401: the OAuth token
    expires in ~10 minutes and a throttled sweep of a large BOM outlives it,
    which would otherwise fail every remaining part in the run."""
    body = json.dumps({"Keywords": _search_term(mpn), "Limit": 10,
                       "FilterOptionsRequest": {
                           "MarketPlaceFilter": "ExcludeMarketPlace"}}).encode()
    for attempt in range(retries):
        wait = _DK_MIN_INTERVAL - (time.time() - _last_dk[0])
        if wait > 0:
            time.sleep(wait)
        _last_dk[0] = time.time()
        req = urllib.request.Request(
            "https://api.digikey.com/products/v4/search/keyword", data=body,
            headers={"Content-Type": "application/json",
                     "X-DIGIKEY-Client-Id": creds["DIGIKEY_CLIENT_ID"],
                     "Authorization": "Bearer " + tok})
        try:
            d = json.load(urllib.request.urlopen(req, timeout=30))
            break
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt < retries - 1:
                tok = _dk_token(creds)          # expired mid-sweep
                if on_token:
                    on_token(tok)
                continue
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    best = None
    for p in d.get("Products", []):
        # keyword search is fuzzy — accept only a normalised MPN match, but
        # DO accept packaging variants (see _same_part) and say which one
        cand = p.get("ManufacturerProductNumber", "")
        ok, exact = _same_part(cand, mpn)
        if not ok:
            continue
        row = [p.get("QuantityAvailable", 0),
               (p.get("ProductStatus") or {}).get("Status", "?"),
               p.get("UnitPrice"), "", ("" if exact else cand)]
        if exact:
            return row
        if best is None or row[0] > best[0]:
            best = row              # keep the best-stocked variant
    return best


_MOUSER_MIN_INTERVAL = 2.1      # Mouser allows ~30 calls/min; bursts 403
_last_mouser = [0.0]


def _mouser(mpn, creds, retries=3):
    """Mouser's Availability is a STRING ('1,234 In Stock'); the integer field
    is unreliable — parse the string (utv-comms V1.2 lesson).

    Mouser answers a BURST with HTTP 403 (not 429), so calls are throttled and
    403/429/5xx are retried with backoff. Raises on final failure: the caller
    must be able to tell "Mouser says nobody stocks it" (None) apart from
    "Mouser did not answer" (exception) — conflating them turns a transient
    outage into a false NONE verdict and, under --strict-sourcing, a bogus
    abort."""
    # DigiKey and Mouser want OPPOSITE things: DigiKey's keyword search chokes
    # on '(LF)(SN)' and needs the base MPN, while Mouser catalogues the full
    # string and its Exact search misses the base. Try the literal first, then
    # the cleaned form.
    terms = [mpn] if _search_term(mpn) == mpn else [mpn, _search_term(mpn)]
    for term in terms:
        row = _mouser_once(term, mpn, creds, retries)
        if row is not None:
            return row
    return None


def _mouser_once(term, mpn, creds, retries=3):
    body = json.dumps({"SearchByPartRequest": {
        "mouserPartNumber": term,
        "partSearchOptions": "Exact"}}).encode()
    req = urllib.request.Request(
        "https://api.mouser.com/api/v1/search/partnumber?apiKey="
        + creds["MOUSER_API_KEY"], data=body,
        headers={"Content-Type": "application/json"})
    for attempt in range(retries):
        wait = _MOUSER_MIN_INTERVAL - (time.time() - _last_mouser[0])
        if wait > 0:
            time.sleep(wait)
        _last_mouser[0] = time.time()
        try:
            d = json.load(urllib.request.urlopen(req, timeout=30))
            break
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))   # 3s, 6s — past the burst window
                continue
            raise
    for p in (d.get("SearchResults") or {}).get("Parts", []) or []:
        ok, exact = _same_part(p.get("ManufacturerPartNumber", ""), mpn)
        if not ok:
            continue
        digits = "".join(c for c in (p.get("Availability") or "0").split()[0]
                         if c.isdigit())
        breaks = p.get("PriceBreaks") or []
        return [int(digits or 0), p.get("LifecycleStatus") or "Active",
                (breaks[0].get("Price") if breaks else None),
                p.get("LeadTime") or "",
                ("" if exact else p.get("ManufacturerPartNumber", ""))]
    return None


def grade(dk, mo, need, errors=()):
    """errors = distributors whose lookup FAILED (as opposed to answered
    'not carried'). A failed lookup is missing evidence, never evidence of
    absence — grading it as NONE would abort a layout over an API hiccup."""
    stock = (dk[0] if dk else 0) + (mo[0] if mo else 0)
    status = " ".join(str(x[1]) for x in (dk, mo) if x).lower()
    if any(b in status for b in BAD):
        return "RISK", stock
    if stock >= need:
        return "OK", stock
    if not dk and not mo:
        # nothing found anywhere — only a real NONE if BOTH actually answered
        return ("ERR" if errors else "NONE"), 0
    if stock:
        return "LOW", stock
    return ("ERR", 0) if errors else ("LEAD", 0)


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
def check(by_mpn, need=10, cache_dir=None, refresh=False, log=print,
          both=False):
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
    skipped = set()
    for mpn in sorted(by_mpn):
        hit = cache.get(mpn)
        errs = []
        if hit and time.time() - hit.get("_t", 0) < CACHE_TTL:
            dk, mo = hit.get("dk"), hit.get("mo")
        else:
            dk = mo = None
            try:
                if "DIGIKEY_CLIENT_ID" in creds:
                    if tok is None:
                        tok = _dk_token(creds)

                    def _keep(new_tok):
                        nonlocal tok
                        tok = new_tok
                    dk = _dk(mpn, creds, tok, on_token=_keep)
            except Exception as e:
                errs.append("DigiKey")
                log(f"    ! DigiKey {mpn}: {e}")
            settled = (dk and dk[0] >= need
                       and not any(b in str(dk[1]).lower() for b in BAD))
            if settled and not both:
                skipped.add(mpn)        # DigiKey already passes it; don't burn
            else:                       # a throttled Mouser call to confirm
                try:
                    if "MOUSER_API_KEY" in creds:
                        mo = _mouser(mpn, creds)
                except Exception as e:
                    errs.append("Mouser")
                    log(f"    ! Mouser {mpn}: {e}")
            # only cache a COMPLETE answer — caching a failed lookup would
            # freeze a transient outage into the report for 24 h
            if not errs:
                cache[mpn] = {"_t": time.time(), "dk": dk, "mo": mo}
        verdict, stock = grade(dk, mo, need, errors=errs)
        counts[verdict] = counts.get(verdict, 0) + 1
        report[mpn] = {"refs": by_mpn[mpn], "verdict": verdict,
                       "stock": stock, "digikey": dk, "mouser": mo,
                       "mouser_skipped": mpn in skipped}
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
        variant = ""
        for src in (dk, mo):
            if src and len(src) > 4 and src[4]:
                variant = f"  ~matched {src[4]}"
                break
        mo_txt = ("MO skip" if r.get("mouser_skipped")
                  else (("MO %d" % mo[0]) if mo else "MO -"))
        log(f"    {r['verdict']:5s} {mpn:26s} "
            f"{('DK %d' % dk[0]) if dk else 'DK -':>9s} "
            f"{mo_txt:>9s}"
            f"{('  lead ' + lead) if lead else ''}{variant}"
            f"   [{' '.join(r['refs'])}]")
    tally = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    log(f"    sourcing: {len(report)} MPNs  {tally}  (need >= {need})")
    blockers = [m for m, r in report.items() if r["verdict"] in BLOCKERS]
    if blockers:
        log("    SOURCING BLOCKERS (not buyable / EOL): " + ", ".join(blockers))
    return blockers


def preflight(board_path, mpn_map=None, need=10, strict=False, refresh=False,
              log=print, both=False):
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
                           log=log, both=both)
    if not report:
        return []
    return summary(report, counts, need, log=log)
