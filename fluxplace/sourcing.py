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

from .redact import urlopen as _redact_urlopen, redact as _redact, setting as _redact_setting, HostSkipped

from .models import credentials

_DEADLINE = [0.0]       # monotonic-free wall deadline of the running check(); 0 = none (R107: the lookup budget caps in-flight requests too)


def _urlopen(req, timeout=30):
    """redact.urlopen under the running check()'s total cap: no request starts after the deadline and none waits past it."""
    d = _DEADLINE[0]
    if d:
        rem = d - time.time()
        if rem <= 0:
            raise HostSkipped("lookup budget spent")
        timeout = min(timeout, max(rem, 0.5))
    return _redact_urlopen(req, timeout=timeout)

CACHE_NAME = ".sourcing_cache.json"
NEG_TTL = 3600                              # a failed lookup is remembered this long (s): an outage costs one try per hour, not one per part per run
PARTDATA_CACHE = ".partdata_cache.json"   # written by partdata.py (same dir)
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
    n = re.sub(r"[^A-Z0-9]", "", s.upper())
    # Molex writes 0436500215 where LCSC writes 436500215: a leading zero on
    # an all-numeric part number is catalogue formatting, not identity
    return n.lstrip("0") if n.isdigit() else n


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
    return json.load(_urlopen(req, timeout=30))["access_token"]


_DK_MIN_INTERVAL = 0.25         # s between calls; DigiKey throttles bursts too
_last_dk = [0.0]


def _money(v):
    """'$1.23' / '1,234.5' / 1.23 -> float, or None."""
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def _breaks_dk(p):
    """DigiKey v4 StandardPricing -> sorted [[break_qty, unit_price], ...]."""
    out = []
    for b in p.get("StandardPricing") or []:
        q, u = b.get("BreakQuantity"), _money(b.get("UnitPrice"))
        if q and u is not None:
            out.append([int(q), u])
    return sorted(out)


def _breaks_mouser(p):
    """Mouser PriceBreaks -> sorted [[break_qty, unit_price], ...]."""
    out = []
    for b in p.get("PriceBreaks") or []:
        q, u = b.get("Quantity"), _money(b.get("Price"))
        if q and u is not None:
            out.append([int(q), u])
    return sorted(out)


def unit_price_at(breaks, qty):
    """-> (unit_price, moq) for buying `qty` pieces from [[break_qty, price]].

    The deepest break at or below `qty` applies. Below the first break the
    first price applies and the buy is raised to that MOQ (the caller prices
    max(qty, moq)). (None, None) when no breaks are known."""
    if not breaks:
        return None, None
    ordered = sorted(breaks)
    price = None
    for q, u in ordered:
        if q <= qty:
            price = u
    if price is None:
        return ordered[0][1], ordered[0][0]
    return price, 1


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
            d = json.load(_urlopen(req, timeout=30))
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
               p.get("UnitPrice"), "", ("" if exact else cand),
               _breaks_dk(p)]
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
            d = json.load(_urlopen(req, timeout=30))
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
                ("" if exact else p.get("ManufacturerPartNumber", "")),
                _breaks_mouser(p)]
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
def _registry_roots(board_path):
    """Project roots above the board (symlinks followed too) that hold a version registry (docs/resume/boards.json)."""
    out = []
    for p in dict.fromkeys((os.path.abspath(board_path), os.path.realpath(board_path))):
        d = os.path.dirname(p)
        while d and d != os.path.dirname(d):
            if os.path.isfile(os.path.join(d, "docs", "resume", "boards.json")) and d not in out:
                out.append(d)
            d = os.path.dirname(d)
    return out


def _registry_tokens(board_path):
    """mpn map file names the registry names for this board's version, best first: the version entry's own `mpn_map`
    (a file name or a list), then mpn_map-<version>.json (lowercase). The version is the registry folder the board lies in."""
    names = []
    for root in _registry_roots(board_path):
        try:
            reg = json.load(open(os.path.join(root, "docs", "resume", "boards.json")))
        except (OSError, ValueError):
            continue
        for p in dict.fromkeys((os.path.abspath(board_path), os.path.realpath(board_path))):
            rel = os.path.relpath(p, root).split(os.sep)
            ver = rel[0] if rel and rel[0] in (reg.get("versions") or {}) else None
            if not ver:
                continue
            ent = (reg["versions"].get(ver) or {}).get("mpn_map")
            names += [ent] if isinstance(ent, str) else list(ent or [])
            names.append(f"mpn_map-{ver.lower()}.json")
    return list(dict.fromkeys(n for n in names if isinstance(n, str) and n))


def find_map(board_path):
    """Locate an mpn_map.json for this board without being told.

    Looks beside the board, then in sibling tool/doc dirs — the common KiCad
    project shapes (hardware/<proj>/board.kicad_pcb + hardware/tools/)."""
    d = os.path.dirname(os.path.abspath(board_path))
    toks_reg = _registry_tokens(board_path)
    for name in toks_reg:                  # the registry's version of this board (R088): CURRENT.kicad_pcb has no version in its own name
        for base in (d, os.path.join(d, "..", "tools"), os.path.join(d, ".."), os.path.join(d, "..", "..", "tools"),
                     *(os.path.join(r, "tools") for r in _registry_roots(board_path)),
                     *(os.path.join(r, "hardware", "tools") for r in _registry_roots(board_path))):
            cand = os.path.normpath(os.path.join(base, name))
            if os.path.exists(cand):
                return cand
    # a versioned board (name-v16.kicad_pcb) uses its own map (mpn_map-v16.json) before the unversioned one: the older revision's
    # map has other refs, and reading it silently turns every part of the newer board into "no MPN"
    toks = os.path.splitext(os.path.basename(board_path))[0].split("-")
    for k in range(len(toks) - 1, 0, -1):
        name = "mpn_map-" + "-".join(toks[k:]) + ".json"
        for base in (d, os.path.join(d, "..", "tools"), os.path.join(d, ".."), os.path.join(d, "..", "..", "tools")):
            cand = os.path.normpath(os.path.join(base, name))
            if os.path.exists(cand):
                return cand
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
def cache_home(cache_dir=None):
    """The directory of the lookup caches: the one given, else ~/.cache/fluxplace (never the current directory)."""
    d = cache_dir or os.path.join(os.path.expanduser("~"), ".cache", "fluxplace")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def write_json(path, obj, indent=None):
    """Atomic: a temp file in the same directory, then os.replace; a failure is silent (a cache is never worth a crash)."""
    try:
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w") as fh:
            json.dump(obj, fh, indent=indent)
        os.replace(tmp, path)
    except Exception:                                   # noqa: BLE001
        pass


def transient(e):
    """An error that says nothing about the part: this process skipped the host, or the distributor throttled (429)."""
    return isinstance(e, HostSkipped) or (isinstance(e, urllib.error.HTTPError) and e.code == 429)


def check(*a, **k):
    """-> (report, counts): _check under the total lookup cap (`[parts] lookup_budget_s`, default 120 s; 0 = none)."""
    budget = float(_redact_setting("lookup_budget_s"))
    _DEADLINE[0] = time.time() + budget if budget else 0.0
    try:
        return _check(*a, **k)
    finally:
        _DEADLINE[0] = 0.0


def _check(by_mpn, need=10, cache_dir=None, refresh=False, log=print,
           both=False, qty=None):
    """-> (report, counts). Never raises on a per-part API error: an outage
    must not block a layout, it just leaves that part ungraded.

    qty=None: legacy mode, every MPN must show `need` units in stock.
    qty=N:    quantity mode. Each MPN must show per-board-count x N units,
              its line is priced at that quantity from the distributors'
              price breaks, and the Mouser lookup is always made so the
              cheapest in-stock source can be chosen. `need` is ignored."""
    creds = credentials()
    if "DIGIKEY_CLIENT_ID" not in creds and "MOUSER_API_KEY" not in creds:
        log("    sourcing: no DigiKey/Mouser credentials — skipped")
        return {}, {}
    cache_dir = cache_home(cache_dir)
    cache_path = os.path.join(cache_dir, CACHE_NAME)
    cache = {}
    if not refresh and os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path))
        except Exception:
            cache = {}
    tok, report, counts = None, {}, {}
    skipped = set()
    t_start, budget, done = time.time(), float(_redact_setting("lookup_budget_s")), 0
    for mpn in sorted(by_mpn):
        hit = cache.get(mpn)
        errs = []
        req = len(by_mpn[mpn]) * qty if qty else need
        if qty:
            both = True
        if hit and hit.get("_neg") and time.time() - hit.get("_t", 0) < NEG_TTL:
            dk, mo = hit.get("dk"), hit.get("mo")      # a lookup that failed a moment ago is not repeated; its good half is kept
            errs = list(hit.get("errs") or ["cached failure"])
        elif hit and not hit.get("_neg") and time.time() - hit.get("_t", 0) < CACHE_TTL:
            dk, mo = hit.get("dk"), hit.get("mo")
        elif budget and time.time() - t_start > budget:
            dk = mo = None                  # budget spent: this part is not asked (and not cached): UNCHECKED, the next run resumes here
            errs = ["lookup budget spent"]
        else:
            done += 1
            tr = False
            if done % 10 == 0:
                try:
                    write_json(cache_path, cache)
                except Exception:
                    pass
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
                tr = tr or transient(e)
                log(f"    ! DigiKey {mpn}: {e}")
            settled = (dk and dk[0] >= req
                       and not any(b in str(dk[1]).lower() for b in BAD))
            if settled and not both:
                skipped.add(mpn)        # DigiKey already passes it; don't burn
            else:                       # a throttled Mouser call to confirm
                try:
                    if "MOUSER_API_KEY" in creds:
                        mo = _mouser(mpn, creds)
                except Exception as e:
                    errs.append("Mouser")
                    tr = tr or transient(e)
                    log(f"    ! Mouser {mpn}: {e}")
            # only cache a COMPLETE answer — caching a failed lookup would
            # freeze a transient outage into the report for 24 h
            if not errs:
                cache[mpn] = {"_t": time.time(), "dk": dk, "mo": mo}
            elif not tr:           # a skipped host / a 429 says nothing about the part: not cached; any other failure is, with the good half
                cache[mpn] = {"_t": time.time(), "_neg": True, "errs": errs, "dk": dk, "mo": mo}     # short-lived: see NEG_TTL
        verdict, stock = grade(dk, mo, req, errors=errs)
        counts[verdict] = counts.get(verdict, 0) + 1
        report[mpn] = {"refs": by_mpn[mpn], "verdict": verdict,
                       "stock": stock, "digikey": dk, "mouser": mo,
                       "mouser_skipped": mpn in skipped,
                       "required": req, "per_board": len(by_mpn[mpn]),
                       "quantity": qty}
    try:
        write_json(cache_path, cache)
    except Exception:
        pass
    if qty:
        price_lines(report, qty)
        alternates(report, cache, cache_dir)
    return report, counts


# ------------------------------------------------------- quantity pricing
def price_lines(report, qty):
    """Attach per-source unit/extended price at the line quantity and pick
    the cheapest source that can supply the line's required quantity.
    Mutates report entries in place; quantity-independent data is untouched."""
    for mpn, r in report.items():
        req = r["required"]
        prices = {}
        for name, row in (("digikey", r["digikey"]), ("mouser", r["mouser"])):
            breaks = row[5] if row and len(row) > 5 else None
            if breaks is None:
                prices[name] = {"unit": None, "moq": None, "ext": None,
                                "stock": row[0] if row else None,
                                "note": "cache predates price breaks"
                                if row else "not carried"}
                continue
            unit, moq = unit_price_at(breaks, req)
            ext = None if unit is None else round(unit * max(req, moq), 4)
            prices[name] = {"unit": unit, "moq": moq, "ext": ext,
                            "stock": row[0] if row else None,
                            "note": "" if unit is not None else "no price"}
        options = [(v["ext"], n) for n, v in prices.items()
                   if v["ext"] is not None and (v["stock"] or 0) >= req]
        chosen = min(options)[1] if options else None
        r["prices"] = prices
        r["chosen"] = chosen
        r["chosen_ext"] = min(options)[0] if options else None


def totals(report):
    """Per-source extended BOM cost and the cheapest-source total."""
    out = {"digikey": {"lines": 0, "ext": 0.0, "unpriced": 0, "short": 0},
           "mouser": {"lines": 0, "ext": 0.0, "unpriced": 0, "short": 0},
           "best": {"lines": 0, "ext": 0.0, "unpriced": 0}}
    for r in report.values():
        for name in ("digikey", "mouser"):
            pr = r["prices"][name]
            if pr["ext"] is None:
                out[name]["unpriced"] += 1
                continue
            out[name]["lines"] += 1
            out[name]["ext"] = round(out[name]["ext"] + pr["ext"], 2)
            if (pr["stock"] or 0) < r["required"]:
                out[name]["short"] += 1
        if r.get("chosen"):
            out["best"]["lines"] += 1
            out["best"]["ext"] = round(out["best"]["ext"] + r["chosen_ext"], 2)
        else:
            out["best"]["unpriced"] += 1
    return out


# -------------------------------------------------------------- alternates
# Parametric keys that must agree for two passives to be the same value.
_ALT_VALUE_KEYS = ("Resistance", "Capacitance", "Inductance", "Voltage - Rated",
                   "Voltage Rated", "Current Rating")
_ALT_KEYS = _ALT_VALUE_KEYS + ("Tolerance", "Power (Watts)",
                               "Temperature Coefficient")


def _alt_sig(data):
    raw = {k.lower(): v for k, v in ((data or {}).get("raw_params") or {}).items()}
    return {k: raw[k.lower()] for k in _ALT_KEYS if k.lower() in raw}


def same_spec(target, cand):
    """True when a cached part has the same package and the same electrical
    value (and tolerance / rating where both state one) as the target. Parts
    with no parametric data never match: an alternate needs evidence."""
    if not target or not cand:
        return False
    tp, cp = target.get("package"), cand.get("package")
    if not tp or not cp or _norm(tp) != _norm(cp):
        return False
    ts, cs = _alt_sig(target), _alt_sig(cand)
    if not (set(ts) & set(cs) & set(_ALT_VALUE_KEYS)):
        return False
    return all(_norm(ts[k]) == _norm(cs[k]) for k in set(ts) & set(cs))


def alternates(report, cache, cache_dir, limit=3):
    """For LEAD/RISK/NONE lines: up to `limit` other parts already in the
    local caches with the same package and value, graded at the line's
    required quantity. Candidates only: no network call is made, and the
    pinout and datasheet are NOT verified, so nothing is substituted."""
    pd_path = os.path.join(cache_dir, PARTDATA_CACHE)
    try:
        pdata = json.load(open(pd_path))
    except Exception:
        pdata = {}
    pool = {m: h.get("data") for m, h in pdata.items() if h.get("data")}
    for mpn, r in report.items():
        r["alternates"] = []
        if r["verdict"] not in ("LEAD", "RISK", "NONE") or mpn not in pool:
            continue
        target = pool[mpn]
        found, stale = [], 0
        for cand, cdata in pool.items():
            if cand == mpn or cand in report or not same_spec(target, cdata):
                continue
            hit = cache.get(cand)
            if not hit or hit.get("_neg"):
                continue
            if time.time() - hit.get("_t", 0) >= CACHE_TTL:   # same TTL rule as the main lookup
                stale += 1
                continue
            dk, mo = hit.get("dk"), hit.get("mo")
            verdict, stock = grade(dk, mo, r["required"])
            if verdict not in ("OK", "LOW"):
                continue
            tmp = {"required": r["required"], "digikey": dk, "mouser": mo}
            price_lines({cand: tmp}, 1)
            found.append({"mpn": cand, "verdict": verdict, "stock": stock,
                          "ext": tmp.get("chosen_ext"),
                          "source": tmp.get("chosen"),
                          "status": "candidate - pinout/datasheet not verified"})
        found.sort(key=lambda c: (c["ext"] is None, c["ext"] or 0, -c["stock"]))
        r["alternates"] = found[:limit]
        r["alternates_stale"] = stale                  # rows older than CACHE_TTL: stale, not used


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


# ---------------------------------------------------------------- datasheets
#
# D43 taught that a part nobody stocks is a design bug, not a purchasing
# problem. This is the same lesson one step earlier: a part whose DATASHEET
# cannot be retrieved is a design bug too, because the pinout cannot be
# verified, and an unverified pinout is exactly what turns a correct land
# pattern into a board that arrives dead.
#
# Real case that produced this: three in-stock gigabit LAN magnetics modules
# were selected on price and availability, and then every route to their pin
# table was blocked — the manufacturer's document host refused connections, the
# distributor's PDF mirror served a bot-check page, and the distributor API's
# media endpoint simply pointed back at the unreachable manufacturer. The parts
# graded OK on stock and were still unusable.

_DS_TIMEOUT = 20


def datasheet_urls_by_source(mpn, creds):
    """[(source, url)] every datasheet URL the distributor APIs offer for an MPN, DigiKey first
    then Mouser (they often point at different hosts, and one host's bot
    wall is the other's plain PDF)."""
    import json as _json
    import urllib.request
    out = []
    if "DIGIKEY_CLIENT_ID" in creds:
        try:
            tok = _dk_token(creds)
            body = _json.dumps({"Keywords": _search_term(mpn), "Limit": 5}).encode()
            req = urllib.request.Request(
                "https://api.digikey.com/products/v4/search/keyword", data=body,
                headers={"Authorization": "Bearer " + tok,
                         "X-DIGIKEY-Client-Id": creds["DIGIKEY_CLIENT_ID"],
                         "Content-Type": "application/json"})
            with _urlopen(req, timeout=30) as r:
                data = _json.load(r)
            for p in data.get("Products", []) or []:
                if _same_part(p.get("ManufacturerProductNumber") or "", mpn)[0] and p.get("DatasheetUrl"):
                    out.append(("digikey", p["DatasheetUrl"]))
        except Exception:
            pass
    if "MOUSER_API_KEY" in creds:
        try:
            body = _json.dumps({"SearchByPartRequest": {
                "mouserPartNumber": mpn, "partSearchOptions": "Exact"}}).encode()
            req = urllib.request.Request(
                "https://api.mouser.com/api/v1/search/partnumber?apiKey="
                + creds["MOUSER_API_KEY"], data=body,
                headers={"Content-Type": "application/json"})
            with _urlopen(req, timeout=30) as r:
                data = _json.load(r)
            for p in ((data.get("SearchResults") or {}).get("Parts") or []):
                if _same_part(p.get("ManufacturerPartNumber") or "", mpn)[0] and p.get("DataSheetUrl"):
                    out.append(("mouser", p["DataSheetUrl"]))
        except Exception:
            pass
    # Nexar only when the two distributors offer nothing: the evaluation plan
    # meters PART LOOKUPS (10/day measured), so every call must earn its keep
    if not out:
        try:
            from . import nexar as NX
            if NX.available(creds):
                u = NX.datasheet_url(mpn, creds)
                if u:
                    out.append(("nexar", u))
        except Exception:
            pass
    if not out:                 # LCSC's copy (JLCPCB parts API), last
        try:
            from . import lcsc as L
            row = L.lookup(mpn)
            if row and row.get("datasheet"):
                out.append(("lcsc", row["datasheet"]))
        except Exception:
            pass
    seen, uniq = set(), []
    for src, u in out:
        if u not in seen:
            seen.add(u); uniq.append((src, u))
    return uniq


def datasheet_urls(mpn, creds):
    """The URLs of datasheet_urls_by_source, without the source names."""
    return [u for _s, u in datasheet_urls_by_source(mpn, creds)]


def datasheet_url(mpn, creds):
    """Best datasheet URL for an MPN, DigiKey first then Mouser. None if
    neither distributor offers one.

    Queries the search endpoints directly rather than reusing _dk()/_mouser():
    those return a graded [stock, status, price] row for the availability gate
    and deliberately discard everything else, including the datasheet link.
    """
    import json as _json
    import urllib.request

    if "DIGIKEY_CLIENT_ID" in creds:
        try:
            tok = _dk_token(creds)
            body = _json.dumps({"Keywords": _search_term(mpn), "Limit": 5}).encode()
            req = urllib.request.Request(
                "https://api.digikey.com/products/v4/search/keyword", data=body,
                headers={"Authorization": "Bearer " + tok,
                         "X-DIGIKEY-Client-Id": creds["DIGIKEY_CLIENT_ID"],
                         "Content-Type": "application/json",
                         "X-DIGIKEY-Locale-Site": "US",
                         "X-DIGIKEY-Locale-Currency": "USD"})
            with _urlopen(req, timeout=30) as r:
                data = _json.load(r)
            for p in data.get("Products", []) or []:
                if _same_part(p.get("ManufacturerProductNumber") or "", mpn):
                    if p.get("DatasheetUrl"):
                        return p["DatasheetUrl"]
        except Exception:
            pass

    if "MOUSER_API_KEY" in creds:
        try:
            body = _json.dumps({"SearchByPartRequest": {
                "mouserPartNumber": mpn, "partSearchOptions": "Exact"}}).encode()
            req = urllib.request.Request(
                "https://api.mouser.com/api/v1/search/partnumber?apiKey="
                + creds["MOUSER_API_KEY"], data=body,
                headers={"Content-Type": "application/json"})
            with _urlopen(req, timeout=30) as r:
                data = _json.load(r)
            for p in ((data.get("SearchResults") or {}).get("Parts") or []):
                if _same_part(p.get("ManufacturerPartNumber") or "", mpn):
                    if p.get("DataSheetUrl"):
                        return p["DataSheetUrl"]
        except Exception:
            pass
    return None


def datasheet_reachable(url, timeout=_DS_TIMEOUT):
    """(ok, detail). Reachability, not just an HTTP 200.

    A 200 that returns HTML or JavaScript is a bot-check page, not a datasheet,
    and reporting it as reachable would be worse than reporting nothing — it
    would tell the engineer the document is available when it is not.
    """
    import urllib.error
    import urllib.request
    if not url:
        return (False, "no datasheet URL from either distributor")
    # DigiKey hands back protocol-relative URLs ('//mm.digikey.com/...') which
    # urllib refuses with "unknown url type". Normalise rather than crash — a
    # gate that dies on a malformed input is worse than one that grades it.
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif not url.lower().startswith(("http://", "https://")):
        return (False, "unusable URL: %r" % url[:60])
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
        "Accept": "application/pdf,*/*"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            head = r.read(1024)
        if head.startswith(b"%PDF"):
            return (True, "PDF")
        if "pdf" in ctype:
            return (True, ctype)
        if head.lstrip()[:1] in (b"<", b"v", b"("):
            return (False, "served HTML/JS (bot check), not a document")
        return (False, "unexpected content-type %r" % ctype)
    except urllib.error.HTTPError as e:
        return (False, "HTTP %s" % e.code)
    except ValueError as e:
        return (False, "bad URL: %s" % _redact(str(e))[:50])
    except Exception as e:
        return (False, type(e).__name__)


def datasheet_gate(mpns, creds=None, log=print):
    """Grade a list of MPNs on whether their datasheet can actually be read.

    Verdicts: OK (a real document came back), BLOCKED (a URL exists but nothing
    usable is served), NONE (no distributor offers a datasheet at all).
    """
    creds = creds or credentials()
    rows = []
    for mpn in mpns:
        url = datasheet_url(mpn, creds)
        ok, detail = datasheet_reachable(url)
        verdict = "OK" if ok else ("BLOCKED" if url else "NONE")
        rows.append({"mpn": mpn, "url": url, "verdict": verdict, "detail": detail})
        log("    %-8s %-22s %s" % (verdict, mpn, detail))
    blocked = [r for r in rows if r["verdict"] != "OK"]
    return {
        "rows": rows,
        "blocked": [r["mpn"] for r in blocked],
        "verdict": ("%d of %d datasheet(s) cannot be retrieved — those pinouts "
                    "cannot be verified, so those parts cannot be committed to "
                    "copper" % (len(blocked), len(rows))) if blocked
                   else "every datasheet is retrievable",
    }


def _fmt_src(pr):
    """Unit price, with the buy quantity when a break above the need forces
    a larger MOQ (price for max(need, MOQ) pieces)."""
    if pr["unit"] is None:
        return pr.get("note") or "-"
    txt = "%.4g" % pr["unit"]
    return txt + (" buy%d" % pr["moq"]) if pr.get("moq", 1) > 1 else txt


def quantity_table(report, qty, log=print):
    """Text table for quantity mode: grade, price per source, the chosen
    source per line, alternates for the blocked lines, and totals."""
    log(f"    sourcing @ quantity {qty}: per-line need = refs x {qty}")
    log(f"    {'verdict':7s}{'MPN':24s}{'need':>7s}  {'DK stock':>9s} "
        f"{'DK unit':>12s}  {'MO stock':>9s} {'MO unit':>12s}  "
        f"{'chosen':8s}{'line ext':>10s}  refs")
    for mpn, r in sorted(report.items(),
                         key=lambda kv: (kv[1]["verdict"] == "OK", kv[0])):
        dk, mo = r["prices"]["digikey"], r["prices"]["mouser"]
        ext = ("%.2f" % r["chosen_ext"]) if r.get("chosen_ext") is not None else "-"
        log(f"    {r['verdict']:7s}{mpn[:23]:24s}{r['required']:>7d}  "
            f"{('%d' % dk['stock']) if dk['stock'] is not None else '-':>9s} "
            f"{_fmt_src(dk):>12s}  "
            f"{('%d' % mo['stock']) if mo['stock'] is not None else '-':>9s} "
            f"{_fmt_src(mo):>12s}  "
            f"{(r.get('chosen') or '-'):8s}{ext:>10s}  "
            f"[{' '.join(r['refs'])}]")
        for alt in r.get("alternates") or []:
            log(f"      alt {alt['verdict']:4s} {alt['mpn']:26s} stock "
                f"{alt['stock']:>7d}  ext {alt['ext'] if alt['ext'] is not None else '-'}"
                f"  ({alt['status']})")
    t = totals(report)
    log(f"    totals @ {qty}: DigiKey {t['digikey']['ext']:.2f} "
        f"({t['digikey']['lines']} priced, {t['digikey']['short']} short)  "
        f"Mouser {t['mouser']['ext']:.2f} ({t['mouser']['lines']} priced, "
        f"{t['mouser']['short']} short)  cheapest-in-stock "
        f"{t['best']['ext']:.2f} ({t['best']['lines']} lines, "
        f"{t['best']['unpriced']} unpriced)")
    counts = {}
    for r in report.values():
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    log("    sourcing: " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        + f"  (quantity {qty})")
    return t
