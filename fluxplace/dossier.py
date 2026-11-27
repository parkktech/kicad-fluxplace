"""Per-MPN component DOSSIER: what a part is and how it connects, read from its datasheet by a model before placement.

Made at the parts-first step (`fluxplace datasheets` runs it after the fetch), one per MPN, cached per user and shared by every
project (~/.cache/fluxplace/dossier/<MPN>.<sha12>.json, keyed by the PDF's sha256: a new datasheet makes a new dossier).
Contents (every fact carries `p`, the datasheet page it was read from; the file is the dossier's `file`):
  package{name,pins} msl temp_range{min,max}            what is soldered and where it may live
  pins[{num[],name,function,alt[],type,domain}]         every pin, its mux alternates, electrical type, voltage domain
  swap_groups[{name,pins[],why}]                        functionally interchangeable pins (GPIO bank, contacts, array elements)
  supplies[{pins[],min,typ,max,unit}] currents[] abs_max[] io_levels[]
  interfaces[{name,protocol,pins[],speed,edge_rate}]    feeds the net budget class
  decoupling[{pins[],value,distance_mm}] keepouts[] thermal{} rf[] crystal{} app_circuit[]
  na{section: reason}                                   a required section that does not apply (io_levels / interfaces only)
  missing[] knowable                                    REQUIRED sections (by category) no source gave: the part is UNKNOWABLE
  replacement[]                                         for an unknowable part: knowable drop-in candidates (propose_replacement)

Owner rule (2026-11-27): a part that cannot be fully known is a HARD BLOCKER at the parts gate (parts.run PART_UNKNOWABLE, no
waiver), never "unknown and continue". Sources are escalated in order until every REQUIRED section is found, each fact tagged:
  1. the datasheet text (`src` absent)          2. OCR / the model reading the page PICTURES (`img`: true)
  3. the manufacturer's other documents in the datasheets folder (app note, user guide, drawing; `doc`: file)
  4. distributor parametric data (`src`: "distributor": package, temperature range)
OPTIONAL sections (swap_groups, msl, thermal, keepouts, rf, crystal, app_circuit) may be absent.

Trust: the model is told to copy numbers as printed; `verify` keeps a fact only when its page exists and every number in it is on
that page (pin names: the name is on the page), else the fact moves to `rejected` with the reason. Downstream (elec.load_facts,
partdocs.spec_check) reads only text-verified facts, and a hand-entered parts-fact always wins over a dossier one. A fact read from a
page picture (`img`) or a distributor is page-range checked only: it makes the part knowable, never feeds a check.
The model call is decider.call_model (`claude -p`; haiku by default, FLUXPLACE_DOSSIER_MODEL); FLUXPLACE_AI=0, FLUXPLACE_AI_OFFLINE or
no `claude` CLI = offline: the dossier
is reported missing (not cached, retried next run). Nothing here names a board, a reference or an MPN.
"""
import concurrent.futures as CF
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

VERSION = 6                           # 2: escalation + required sections; a v1 dossier is upgraded (no second text pass)
MODEL = "haiku"
TEXT_BUDGET = 240_000                 # characters of datasheet text sent per part (~60k tokens)
LIST_SECTIONS = ("pins", "swap_groups", "supplies", "currents", "abs_max", "io_levels", "interfaces", "decoupling", "keepouts",
                 "rf", "app_circuit")
DICT_SECTIONS = ("package", "msl", "temp_range", "thermal", "crystal")
_IC = ("package", "temp_range", "pins", "supplies", "abs_max", "io_levels", "interfaces", "decoupling")
_TWO = ("package", "temp_range", "pins", "abs_max")
REQUIRED = {"connector": ("package", "temp_range", "pins", "currents"), "passive": ("package", "temp_range", "abs_max"),
            "magnetics": _TWO, "diode": _TWO, "led": _TWO, "transistor": _TWO, "crystal": _TWO, "relay": _TWO,
            "antenna": ("package", "temp_range", "pins"), "battery": ("package", "temp_range", "abs_max"), "button": _TWO,
            "mechanical": ("package",)}
_NONPOLAR = ("passive", "magnetics", "antenna")      # a 2-terminal part of these has no pin identity to know
NA_OK = ("io_levels", "interfaces")      # may be declared not applicable with a reason (an analog part has no logic levels)
ESCALATE_MODEL = os.environ.get("FLUXPLACE_DOSSIER_MODEL2", "sonnet")    # second gap pass (Haiku first, Sonnet when it falls short)
GAP_PAGES = 8                            # page pictures per gap pass
_TEXT_KEYS = {"name", "function", "type", "domain", "why", "protocol", "what", "note", "param", "unit", "pad", "vias", "alt", "pins",
              "num", "description", "category"}     # prose: not digit-checked (value, speed, edge_rate, msl v are: their numbers are printed)
_KEYWORDS = re.compile(r"pin (configuration|description|function|assignment)|pinout|absolute maximum|recommended operating|"
                       r"electrical characteristics|decoupl|bypass|layout|land pattern|thermal|package|ordering|application|"
                       r"moisture|msl|interface|timing|clearance|keep.?out|crystal|load capacitance|contact|current rating",
                       re.I)
_NUM = re.compile(r"(?<![\w.])[-+−]?\d+(?:\.\d+)?")

PROMPT = """You read an electronic component datasheet and fill a JSON dossier. Part number: {mpn}.
Use ONLY the datasheet text below (pages are marked "=== PAGE n ==="). Every fact you give carries "p": the page number it is printed on.
Copy numbers exactly as printed with the unit as printed (do not convert units). Anything the datasheet does not state: leave it out
and add the section name to "unknown". Never guess. When the datasheet covers several packages, use the package of {mpn}.
Return ONE JSON object, no prose, with these keys (omit empty ones):
{{"category": "mcu|regulator|switch|interface|codec|sensor|rf|oscillator|connector|passive|magnetics|diode|led|transistor|crystal|relay|antenna|battery|button|mechanical|logic|module|other",
 "description": "one line",
 "package": {{"name": "...", "pins": 0, "p": 0}}, "msl": {{"v": "...", "p": 0}}, "temp_range": {{"min": 0, "max": 0, "unit": "C", "p": 0}},
 "pins": [{{"num": ["1"], "name": "...", "function": "short", "alt": ["mux alternates"], "type": "power_in|power_out|ground|input|output|bidir|open_drain|analog|rf|passive|nc|other", "domain": "supply pin name this pin's level follows, or null", "p": 0}}],
 "swap_groups": [{{"name": "...", "pins": ["pin names"], "why": "why interchangeable", "p": 0}}],
 "supplies": [{{"pins": ["names"], "min": 0, "typ": 0, "max": 0, "unit": "V", "p": 0}}],
 "currents": [{{"pins": ["names"], "what": "quiescent|operating|max|output|contact", "typ": 0, "max": 0, "unit": "mA", "p": 0}}],
 "abs_max": [{{"param": "...", "min": 0, "max": 0, "unit": "V", "p": 0}}],
 "io_levels": [{{"domain": "...", "vil_max": 0, "vih_min": 0, "vol_max": 0, "voh_min": 0, "unit": "V", "p": 0}}],
 "interfaces": [{{"name": "...", "protocol": "...", "pins": ["names"], "speed": "as printed", "edge_rate": "as printed", "p": 0}}],
 "decoupling": [{{"pins": ["names"], "value": "as printed", "distance_mm": 0, "note": "...", "p": 0}}],
 "keepouts": [{{"what": "...", "p": 0}}], "thermal": {{"pad": "...", "vias": "...", "theta_ja": 0, "p": 0}},
 "rf": [{{"what": "matching / antenna clearance", "p": 0}}], "crystal": {{"load_pf": 0, "p": 0}},
 "app_circuit": [{{"what": "reference circuit element and value", "p": 0}}],
 "unknown": ["section names not stated"]}}
temp_range is the operating range (ambient, or the operating junction range when that is all the datasheet gives).
abs_max holds every limiting rating: absolute maximum ratings, and for passives / magnetics / relays / crystals the rated voltage,
rated or saturation current, power rating, drive level, contact and coil ratings.
Pins: every pin of the package (a 2-terminal part: both terminals with their polarity as the drawing marks them, e.g. 1 = cathode); for a GPIO give every mux alternate in "alt" naming its peripheral (e.g. "SPI0_MOSI", "I2C1_SDA", "UART2_TX").
"na": {{"io_levels": "reason"}} only when a section does not apply to this part (io_levels, interfaces). Swap groups: only pins the datasheet shows as equivalent (same function class, any one usable).
Connectors: pins are contacts; contacts of one kind (signal / power) form a swap group; contact current rating goes in currents (what=contact).

DATASHEET TEXT:
{text}
"""
GAP = """You complete a component dossier for {mpn}. These REQUIRED sections are still missing: {need}.
Fill ONLY those sections (same JSON shape as below), from these sources:
{sources}
Every fact carries "p" (page number) and, when it comes from another document, "doc": that file name; a fact read from a page picture
carries "img": true. Copy numbers exactly as printed. Never guess: a section no source states stays out. A section that does not apply to
this part may be declared in "na": {{"section": "reason"}} (only {na_ok}). Also return "category" (the list in the shapes).
Return ONE JSON object, no prose.
Shapes: {shapes}

{text}
"""
VISION = """(This datasheet has no text layer. Its pages are pictures in the current directory: {pics}. Read every one with the Read tool;
"p" is the page number given here. Copy what the drawings print; anything not legible is unknown.)"""


# ------------------------------------------------------------------ cache
def cache_dir(d=None):
    from . import sourcing as S
    out = os.path.join(S.cache_home(d), "dossier")
    os.makedirs(out, exist_ok=True)
    return out


def _path(mpn, sha, d=None):
    return os.path.join(cache_dir(d), f"{re.sub(r'[^A-Za-z0-9._-]', '_', mpn)}.{(sha or 'nosha')[:12]}.json")


def load(mpn, sha, d=None, any_version=False):
    """The cached dossier of `mpn` for the datasheet with this sha256, or None (an older version only with any_version)."""
    try:
        with open(_path(mpn, sha, d)) as fh:
            x = json.load(fh)
        return x if any_version or x.get("version") == VERSION else None
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ datasheet text
OCR_PAGES = 8                         # a drawing-only PDF (connector outline) is OCR'd up to this many pages


def pages(pdf):
    """[page text] (index 0 = page 1): pdftotext, or OCR (partdocs.ocr_text) of a PDF with no text layer; [] when neither reads it."""
    from . import partdocs as PD
    try:
        out = subprocess.run(["pdftotext", "-layout", pdf, "-"], capture_output=True, text=True, timeout=180).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    pg = out.split("\f")
    pg = pg[:-1] if pg and not pg[-1].strip() else pg
    if any(len(t.strip()) > 40 for t in pg):
        return pg
    return [PD.ocr_text(pdf, [n]) or "" for n in range(1, min(len(pg) or 1, OCR_PAGES) + 1)]


def select(pg, budget=TEXT_BUDGET):
    """The page numbers sent to the model: all of them when they fit, else the first three plus the pages that name pins, ratings and
    layout (most keyword hits first) up to the budget, in page order."""
    sizes = [len(t) for t in pg]
    if sum(sizes) <= budget:
        return list(range(1, len(pg) + 1))
    score = sorted(range(len(pg)), key=lambda i: (i >= 3, -len(_KEYWORDS.findall(pg[i])), i))
    keep, used = [], 0
    for i in score:
        if used + sizes[i] <= budget and (i < 3 or _KEYWORDS.search(pg[i])):
            keep.append(i + 1)
            used += sizes[i]
    return sorted(keep)


# ------------------------------------------------------------------ verification
def _flat(text):
    return re.sub(r"\s+", " ", (text or "").replace("−", "-").replace("–", "-"))


def _nums(v):
    if isinstance(v, bool) or v is None:
        return []
    if isinstance(v, (int, float)):
        return [float(v)]
    if isinstance(v, str):
        return [float(x.replace("−", "-")) for x in _NUM.findall(v)]
    return []


def _on_page(n, text):
    s = f"{abs(n):g}"
    alts = {s, s.lstrip("0") if s.startswith("0.") else s}
    return any(re.search(r"(?<![\d.])" + re.escape(a) + r"(?!\d)", text) for a in alts)


def _check(fact, pg, vision=False, docs=None):
    """None when the fact stands on its page, else the reason it does not. A page read from its picture (img / vision) has no text to
    check; a fact from another document (`doc`) is checked on that document's page."""
    if fact.get("doc"):
        if fact["doc"] not in (docs or {}):
            return f"document {fact['doc']} was not given"
        pg = docs[fact["doc"]]
    p = fact.get("p")
    if not isinstance(p, int) or not 1 <= p <= len(pg):
        return f"page {p!r} is not in {fact.get('doc') or 'the datasheet'}"
    if vision or fact.get("img"):
        return None
    text = _flat(pg[p - 1])
    for k, v in fact.items():
        if k in ("p", "doc", "img") or k in _TEXT_KEYS:
            continue
        for n in _nums(v):
            if not _on_page(n, text):
                return f"{k}={v} is not on page {p}"
    if "name" in fact and isinstance(fact["name"], str) and "num" in fact:
        from . import partdocs as PD
        names = PD._aliases(fact["name"]) if fact["name"].strip() else set()
        if names and not (names & PD._tokens(text) or any(len(a) >= 3 and a in PD._norm(text) for a in names)):
            return f"pin name {fact['name']} is not on page {p}"
    return None


def verify(raw, pg, vision=False, docs=None):
    """The model's dossier with every fact checked against its page: unverified facts move to `rejected`. -> dossier dict."""
    out = {"category": raw.get("category") or "unknown", "description": raw.get("description") or "", "rejected": [],
           "na": {k: str(v) for k, v in (raw.get("na") or {}).items() if k in NA_OK and v}}
    unknown = {x for x in raw.get("unknown") or [] if isinstance(x, str)}

    def stands(s, f):
        if vision:
            f["img"] = True
        why = _check(f, pg, vision, docs)
        if why:
            out["rejected"].append({"section": s, "fact": f, "why": why})
        return not why

    for s in DICT_SECTIONS:
        f = raw.get(s)
        if isinstance(f, dict) and f and stands(s, f):
            out[s] = f
        else:
            unknown.add(s)
    for s in LIST_SECTIONS:
        keep = [f for f in raw.get(s) or [] if isinstance(f, dict) and stands(s, f)]
        if keep:
            out[s] = keep
        else:
            unknown.add(s)
    out["unknown"] = sorted(unknown - set(out))
    return out


def required(dos):
    """The REQUIRED sections of a part by its category; supplies and decoupling only when the part has a supply pin."""
    req = REQUIRED.get(dos.get("category"), _IC)
    if dos.get("category") in _NONPOLAR and ((dos.get("package") or {}).get("pins") or 2) <= 2:
        req = tuple(x for x in req if x != "pins")
    if dos.get("pins") and not any(p.get("type") == "power_in" for p in dos["pins"]):
        req = tuple(x for x in req if x not in ("supplies", "decoupling"))
    return req


def gaps(dos):
    """REQUIRED sections no source has given (and not declared not applicable). A pin table that numbers fewer pins than the package
    has is not given: every pin must be known."""
    out = [s for s in required(dos) if not dos.get(s) and s not in (dos.get("na") or {})]
    pk = (dos.get("package") or {}).get("pins")
    if "pins" in required(dos) and "pins" not in out and isinstance(pk, int) and not isinstance(pk, bool) and pk > 2 \
            and len({str(n) for p in dos["pins"] for n in p.get("num") or []}) < pk:
        out.append("pins")
    return out


def merge(dos, add):
    """Fill `dos` from a later source's verified dossier: only sections still missing, and its `na` / `rejected`; a later reading's
    category replaces the first (it decides which sections are required)."""
    if add.get("category") not in (None, "unknown"):
        dos["category"] = add["category"]
    for s in gaps(dos):
        if add.get(s) and (s != "pins" or len(add[s]) > len(dos.get(s) or [])):
            dos[s] = add[s]
        elif s in (add.get("na") or {}):
            dos.setdefault("na", {})[s] = add["na"][s]
    dos["rejected"] = dos.get("rejected", []) + add.get("rejected", [])
    return dos


def n_facts(d):
    return sum(len(d.get(s) or []) for s in LIST_SECTIONS) + sum(1 for s in DICT_SECTIONS if d.get(s))


# ------------------------------------------------------------------ model
def _model(model=None):
    return model or os.environ.get("FLUXPLACE_DOSSIER_MODEL") or MODEL


def online():
    return (os.environ.get("FLUXPLACE_AI", "1") not in ("0", "off", "no") and not os.environ.get("FLUXPLACE_AI_OFFLINE")
            and shutil.which("claude") is not None)


def call_model(prompt, model=None, timeout=900, tools="", cwd=None):
    """-> (reply text, cost USD) through decider.call_model (one model path for the flow); raises RuntimeError when there is no answer."""
    from . import decider as DC
    info = {}
    out, why = DC.call_model(prompt, None, _model(model), tools=tools, cwd=cwd or cache_dir(), timeout=timeout, info=info)
    if out is None:
        raise RuntimeError(why or "no model answer")
    return out, info.get("cost_usd", 0.0)


def _json_of(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in the reply")
    return json.loads(m.group(0))


def _first_pass(mpn, pdf, pg, td, ask, model):
    """-> (raw dossier, vision?, pages sent, cost): the datasheet text, or its page pictures when it has no text."""
    vision = not any(t.strip() for t in pg)
    if vision:                           # a drawing-only PDF and no OCR: the model reads the page pictures
        from . import partcite as PC
        n = min(PC.page_count(pdf) or 1, OCR_PAGES)
        sel = [k for k in range(1, n + 1) if PC.render_page(pdf, k, os.path.join(td, f"page-{k}.png"))[0]]
        if not sel:
            raise ValueError("no text layer and the pages did not render")
        text = VISION.format(pics=", ".join(f"page-{k}.png (page {k})" for k in sel))
    else:
        sel = select(pg)
        text = "\n".join(f"=== PAGE {n} ===\n{pg[n - 1]}" for n in sel)
    call = ask or (lambda pr: call_model(pr, model, tools="Read" if vision else "", cwd=td))
    reply, cost = call(PROMPT.format(mpn=mpn, text=text))
    return _json_of(reply), vision, sel, cost


def siblings(pdf, mpn, limit=5):
    """Other documents of the same part / family in the datasheets folder (app note, user guide, drawing): file stems sharing the first
    five characters of the MPN or of the datasheet's own stem."""
    folder, own = os.path.dirname(pdf), os.path.basename(pdf)
    keys = {re.sub(r"[^A-Z0-9]", "", x.upper())[:5] for x in (mpn, own[:-4])} - {""}
    keys = {k for k in keys if len(k) == 5}
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out = [os.path.join(folder, n) for n in names if n.lower().endswith(".pdf") and n != own
           and re.sub(r"[^A-Z0-9]", "", n.upper())[:5] in keys]
    mine = re.sub(r"[^A-Za-z0-9._-]", "_", mpn) + "__"                 # the part's own fetched documents (fetch_docs) first
    return sorted(out, key=lambda f: not os.path.basename(f).startswith(mine))[:limit]


_GAP_WORDS = {"pins": r"pin|terminal|pad|polarity|cathode|marking", "abs_max": r"absolute|maximum rating|rated|rating|drive level",
              "temp_range": r"temperature", "supplies": r"supply|operating conditions", "io_levels": r"V[\s_]?IH|V[\s_]?IL|input high|logic level",
              "interfaces": r"interface|protocol|I2C|SPI|UART|USB|clock frequency", "decoupling": r"bypass|decoupl|capacitor",
              "currents": r"current|contact", "package": r"package|dimension|outline"}


def _gap_pass(mpn, pdf, pg, dos, td, ask, model):
    """-> (verified additions, cost): the missing REQUIRED sections from the datasheet's page pictures and the family's other documents."""
    from . import partcite as PC
    need = gaps(dos)
    thin = [k for k in range(1, len(pg) + 1) if len(pg[k - 1].strip()) < 300]           # pages whose content is a picture
    hot = [k for k in range(1, len(pg) + 1) if k not in thin and re.search(r"pin (configuration|description|function)|pinout|"
                                                                           r"absolute maximum|recommended operating", pg[k - 1], re.I)]
    pat = re.compile("|".join(_GAP_WORDS[s] for s in need if s in _GAP_WORDS) or "$^", re.I)
    own = [k for k in range(1, len(pg) + 1) if k not in thin and pat.search(pg[k - 1])]
    order = list(dict.fromkeys(thin + own + hot))           # a drawing page that names the subject is a picture too (pin diagrams)
    pics = [k for k in order[:GAP_PAGES] if PC.render_page(pdf, k, os.path.join(td, f"page-{k}.png"))[0]]
    docs = {os.path.basename(f): pages(f) for f in siblings(pdf, mpn)}
    budget, chunks = TEXT_BUDGET // 2, []
    for k in own:
        if budget - len(pg[k - 1]) < 0:
            break
        budget -= len(pg[k - 1])
        chunks.append(f"=== PAGE {k} ===\n{pg[k - 1]}")
    n_own = len(chunks)
    for name, dp in docs.items():
        for n, t in enumerate(dp, 1):
            if budget - len(t) < 0:
                break
            budget -= len(t)
            chunks.append(f"=== DOC {name} PAGE {n} ===\n{t}")
    sources = []
    if pics:
        sources.append("- page pictures of the datasheet (Read them): " + ", ".join(f"page-{k}.png (page {k})" for k in pics))
    if n_own:
        sources.append("- the datasheet's own pages on these subjects again (=== PAGE n ===): read them closely")
    if len(chunks) > n_own:
        sources.append("- text of other documents below (marked === DOC file PAGE n ===)")
    bad = [f"{r['section']}: {r['why']}" for r in dos.get("rejected") or [] if r.get("section") in need][:12]
    if bad:
        sources.append("- an earlier reading was rejected (the number or name is not on the cited page; cite the page it IS on): "
                       + "; ".join(bad))
    if not sources:
        return {"rejected": []}, 0.0
    shapes = PROMPT[PROMPT.index("{{\"category\""):PROMPT.index("Pins: every pin")].replace("{{", "{").replace("}}", "}")
    prompt = GAP.format(mpn=mpn, need=", ".join(need), sources="\n".join(sources), na_ok=", ".join(NA_OK), shapes=shapes,
                        text="\n".join(chunks))
    call = ask or (lambda pr: call_model(pr, model, tools="Read" if pics else "", cwd=td))
    reply, cost = call(prompt)
    return verify(_json_of(reply), pg, docs=docs), cost


_RATING = re.compile(r"voltage - rated|rated voltage|current rating|current - saturation|power \(watts\)|voltage - (reverse standoff|"
                     r"breakdown|zener|dc reverse)|drive level|contact rating|coil voltage|current - max|voltage - max", re.I)


def _distributor(dos, row):
    """Package and temperature range from a distributor row (partdata), only where still missing."""
    if not row:
        return dos
    need = gaps(dos)
    if "package" in need and row.get("package"):
        dos["package"] = {"name": row["package"], "pins": row.get("pins"), "src": "distributor"}
    if "temp_range" in need and row.get("temp_min") is not None and row.get("temp_max") is not None:
        dos["temp_range"] = {"min": row["temp_min"], "max": row["temp_max"], "unit": "C", "src": "distributor"}
    params = row.get("raw_params") or row.get("params") or {}
    rated = [{"param": k, "max": v, "src": "distributor"} for k, v in params.items()
             if _RATING.search(k) and v and str(v).strip() not in ("-", "")]
    if "abs_max" in need and rated:
        dos["abs_max"] = rated
    return dos


def _dist_row(mpn):
    from . import sourcing as S
    row = (S.cache_load(".partdata_cache.json").get(mpn) or {}).get("data")
    if row is None and online():
        from . import partsearch as PS
        try:
            row = PS.lookup(mpn)
        except Exception:                                   # noqa: BLE001 - a distributor hiccup is a missing source, not a crash
            row = None
    return row


def build(mpn, pdf, sha, ask=None, model=None, d=None, refresh=False, distributor=None, retry=False):
    """The dossier of one part: cached when the datasheet is unchanged, else read by the model, verified and escalated through the
    sources until every REQUIRED section is found (else `knowable` false, `missing` named).
    ask(prompt) -> (text, cost) replaces the model and distributor(mpn) -> row the distributor (tests). -> (dossier | None, status, cost)."""
    hit = None if refresh else load(mpn, sha, d, any_version=True)
    if hit and hit.get("version") == VERSION and (hit.get("knowable") or not retry):
        return hit, "cached", 0.0                           # retry: an unknowable part's escalation runs again (new documents)
    if ask is None and not online():
        return None, "offline (FLUXPLACE_AI=0 or no claude CLI)", 0.0
    pg = pages(pdf)
    t0, cost = time.time(), 0.0
    with tempfile.TemporaryDirectory(dir=cache_dir(d)) as td:
        try:
            if hit:                                          # an older or unknowable dossier: its text pass stands, the escalation runs
                hit = dict(hit, sources=[])
                dos, vision, sel = hit, hit.get("evidence") == "vision", hit.get("pages_read") or []
                if vision:
                    pg = [""] * (hit.get("n_pages") or 1)
            else:
                raw, vision, sel, cost = _first_pass(mpn, pdf, pg, td, ask, model)
                dos = verify(raw, pg, vision)
            dos.setdefault("na", {})
            sources = ["datasheet"]
            for i, m2 in enumerate((model, model or ESCALATE_MODEL)):   # the cheap model, then a stronger one on what is still missing
                if i and gaps(dos):                            # the distributor's parametrics before the stronger model
                    _distributor(dos, (distributor or _dist_row)(mpn))
                    sources.append("distributor")
                if not gaps(dos):
                    break
                add, c2 = _gap_pass(mpn, pdf, pg, dos, td, ask, m2)
                cost += c2
                merge(dos, add)
                sources.append(f"pictures+documents ({_model(m2)})")
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
            return None, f"model failed: {e}", 0.0
    if gaps(dos) and "distributor" not in sources:
        _distributor(dos, (distributor or _dist_row)(mpn))
        sources.append("distributor")
    miss = gaps(dos)
    dos.update(mpn=mpn, file=os.path.basename(pdf), sha256=sha, version=VERSION, pages_read=sel, n_pages=len(pg),
               evidence="vision" if vision else "text", model=_model(model), cost_usd=round(cost + (hit or {}).get("cost_usd", 0), 5),
               seconds=round(time.time() - t0, 1), made=time.strftime("%Y-%m-%dT%H:%M:%S"), sources=sources,
               missing=miss, knowable=not miss)
    dos["unknown"] = sorted(set(dos.get("unknown") or []) - set(dos))
    from . import sourcing as S
    S.write_json(_path(mpn, sha, d), dos, indent=1)
    return dos, "UNKNOWABLE" if miss else "made", cost


def build_all(ds_dir, mpns, jobs=4, ask=None, model=None, d=None, refresh=False, log=print, fetch=None):
    """Dossiers for every MPN whose datasheet is in the manifest, `jobs` at a time; an unknowable part gets its manufacturer's other
    documents fetched (fetch_docs; fetch=None: when online, a dict: fetch_docs arguments for tests, False: never) and is read again.
    -> {mpn: (dossier|None, status, cost)}."""
    fetch = online() if fetch is None else fetch
    from . import partdocs as PD
    man = PD.load_manifest(ds_dir)
    todo = []
    for m in dict.fromkeys(mpns):
        ent = man.get(m)
        f = os.path.join(ds_dir, ent["file"]) if isinstance(ent, dict) and ent.get("file") else None
        if f and os.path.isfile(f):
            todo.append((m, f, ent.get("sha256") or PD._sha(f)))
    res = _run(todo, jobs, ask, model, d, refresh, log)
    if fetch:
        again = []
        for m, f, s in todo:                             # an unknowable part: the manufacturer's other documents, then the escalation again
            dos = res[m][0]
            kw = fetch if isinstance(fetch, dict) else {"mfr": (_dist_row(m) or {}).get("manufacturer") or ""}
            if dos and not dos.get("knowable") and fetch_docs(m, ds_dir, dos["missing"], log=log, **kw):
                again.append((m, f, s))
        for m, r in _run(again, jobs, ask, model, d, False, log, retry=True).items():
            res[m] = (r[0], r[1], res[m][2] + r[2])
    return res


def _run(todo, jobs, ask, model, d, refresh, log, retry=False):
    res = {}
    with CF.ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futs = {ex.submit(build, m, f, s, ask, model, d, refresh, None, retry): m for m, f, s in todo}
        for fu in CF.as_completed(futs):
            m = futs[fu]
            res[m] = fu.result()
            dos, st, cost = res[m]
            line = f"  dossier {m}: {st}"
            if dos:
                line += f", {n_facts(dos)} facts, {len(dos['rejected'])} rejected"
                if dos["missing"]:
                    line += f", missing {', '.join(dos['missing'])}"
            if cost:
                line += f", ${cost:.3f}"
            log(line)
    return res


# ------------------------------------------------------------------ downstream: dossier -> elec facts, pin table
_SCALE = {"v": 1.0, "mv": 1e-3, "a": 1.0, "ma": 1e-3, "ua": 1e-6, "µa": 1e-6, "mm": 1.0, "mil": 0.0254}


def _si(v, unit, want):
    """`v` printed in `unit` as a number in `want` (V, A or mm), or None."""
    u, w = (unit or "").strip().lower(), want.lower()
    if not isinstance(v, (int, float)) or isinstance(v, bool) or u not in _SCALE or u[-1:] != w[-1:]:
        return None
    return v * _SCALE[u] / _SCALE[w]


def elec_facts(dos):
    """{key: {value, source}} in the elec fact-store shape, from a dossier's verified facts: supply range per supply pin, decoupling
    distance per pin, connector contact rating. A key the dossier does not back is absent; a fact read from a picture or a distributor
    (`img` / `src`) backs none."""
    dos = _text_only(dos)

    def src(f):
        return f"{f.get('doc') or dos['file']}#p{f['p']} (dossier)"

    out = {}
    lo, hi, pins, srcs = {}, {}, [], []
    kind = {p.get("name"): p.get("type") for p in dos.get("pins") or []}      # a range row may name a ground (VS = V+ - V-) or SW pin
    for s in dos.get("supplies") or []:
        mn, mx = _si(s.get("min"), s.get("unit"), "V"), _si(s.get("max"), s.get("unit"), "V")
        for pn in s.get("pins") or []:
            if kind.get(pn, "power_in") == "power_in" and (mn is not None or mx is not None):
                pins.append(pn)
                srcs.append(s)
                if mn is not None:
                    lo[pn] = min(lo.get(pn, mn), mn)
                if mx is not None:
                    hi[pn] = max(hi.get(pn, mx), mx)
    if pins:
        out["supply_pins"] = {"value": sorted(set(pins)), "source": src(srcs[0])}
        if lo:
            out["vin_min"] = {"value": lo, "source": src(srcs[0])}
        if hi:
            out["vin_max"] = {"value": hi, "source": src(srcs[0])}
    dist = {}
    for c in dos.get("decoupling") or []:
        mm = c.get("distance_mm")
        if isinstance(mm, (int, float)) and not isinstance(mm, bool) and mm > 0:
            for pn in c.get("pins") or []:
                dist[pn] = min(dist.get(pn, mm), mm)
            out["decap_mm"] = {"value": dist, "source": src(c)}
    contact = [(_si(c.get("max") if c.get("max") is not None else c.get("typ"), c.get("unit"), "A"), c)
               for c in dos.get("currents") or [] if c.get("what") == "contact"]
    contact = [x for x in contact if x[0]]
    if contact and dos.get("category") == "connector":
        a, f = min(contact, key=lambda x: x[0])
        out["contact_a"] = {"value": a, "source": src(f)}
    return out


def _text_only(dos):
    """The dossier with only text-verified facts (no picture / distributor reads): what checks may use."""
    out = dict(dos)
    for s in LIST_SECTIONS:
        out[s] = [f for f in dos.get(s) or [] if not (f.get("img") or f.get("src"))]
    return out


def pin_table(dos):
    """{pad number: (names, ground)} of a dossier's verified pins: every printed spelling (a combined "EN/SYNC" is EN and SYNC too),
    and whether the datasheet calls the pin ground."""
    from . import partdocs as PD
    out = {}
    for p in dos.get("pins") or []:
        raw = [p.get("name") or ""] + [a for a in p.get("alt") or [] if isinstance(a, str)]
        names = {PD._norm(x) for r in raw for x in [r, *re.split(r"[/,\s()]+", r)]} - {""}
        for n in p.get("num") or []:
            names_n, gnd = out.get(str(n), (set(), False))
            out[str(n)] = (names_n | names, gnd or p.get("type") == "ground" or any(_GNDNAME.match(x) for x in names))
    return out


_GNDNAME = re.compile(r"^(A|D|P|S)?(GND|VSS|VEE|EP|PAD|V-)\w*$", re.I)


def pinmap_mismatches(pinmap, dos):
    """[(spec name, pad, why)] where the spec and the dossier disagree in a way a synonym cannot explain: the spec's name is printed on
    another pad and not this one, or one side calls the pad ground and the other does not (the V1.4 ESD-array miss). Spelling
    differences (FLT vs OC, VCC vs 3V3) are not findings: the page evidence check covers names."""
    from . import partdocs as PD
    tab = pin_table(_text_only(dos))
    bad = []
    for name, pad in (pinmap or {}).items():
        al, exact = PD._aliases(name), {PD._norm(name), PD._norm(name.replace("_", ""))}
        gname = bool(_GNDNAME.match(PD._norm(name)))
        for pd in (pad if isinstance(pad, list) else [pad]):
            here = tab.get(str(pd))
            if not here or al & here[0] or (gname and here[1]):
                continue
            elsewhere = sorted(k for k, (nm, _) in tab.items() if exact & nm)       # exact spelling: VCC_IO is not VCC
            if elsewhere and not any(n.isdigit() for n in here[0]):
                bad.append((name, str(pd), f"datasheet has {name} on pad {'/'.join(elsewhere[:3])}"))
            elif here[1] != gname and not here[0] <= {str(pd)}:
                bad.append((name, str(pd), f"datasheet pad {pd} is {'ground' if here[1] else 'not ground'} ({'/'.join(sorted(here[0])[:2])})"))
    return bad


# ------------------------------------------------------------------ an unknowable part: a knowable replacement
def propose_replacement(mpn, fp=None, required=1, temp=(None, None), searcher=None, enricher=None, fetch=None, ask=None, top=5,
                        log=lambda m: None, orig=None):
    """Candidates for an UNKNOWABLE part: partsgate.alternatives (same function and package class, in stock at `required`), each
    candidate's datasheet fetched (partdocs.fetch) and read into its own dossier. -> [{mpn, mfr, stock, drop_in, differs, knowable,
    missing, same_pinout (vs `orig`, the part's dossier)}], knowable same-pinout drop-ins first; the first row is the proposal. Writes the list into the part's dossier as `replacement`."""
    from . import partdocs as PD, partsearch as PS, partsgate as PG
    row = _dist_row(mpn) or {"description": mpn}
    alts = PG.alternatives(mpn, row, fp, required, temp, searcher or PS.keyword_search, limit=top,
                           enricher=enricher if enricher is not None else PS.lookup)
    cd = os.path.join(cache_dir(), "candidates")
    os.makedirs(cd, exist_ok=True)
    out = []
    for o in alts:
        if not o.get("mpn") or o.get("error"):
            continue
        (fetch or PD.fetch)([o["mpn"]], cd, log=log)
        ent = PD.load_manifest(cd).get(o["mpn"])
        f = os.path.join(cd, ent["file"]) if ent and ent.get("file") else None
        cdos = build(o["mpn"], f, ent.get("sha256") or PD._sha(f), ask=ask)[0] if f and os.path.isfile(f) else None
        r = {"mpn": o["mpn"], "mfr": o.get("mfr"), "stock": o.get("stock"), "drop_in": bool(o.get("drop_in")),
             "same_pinout": same_pinout(orig, cdos),
             "differs": o.get("differs") or [], "knowable": bool(cdos and cdos.get("knowable")),
             "missing": (cdos or {}).get("missing", ["dossier not made"] if f else ["no datasheet fetched"])}
        out.append(r)
        log(f"  candidate {r['mpn']}: drop_in {r['drop_in']}, knowable {r['knowable']}")
        if r["drop_in"] and r["knowable"] and r["same_pinout"]:
            break
    return sorted(out, key=lambda r: (not r["knowable"], not (r["drop_in"] and r["same_pinout"]), not r["drop_in"], len(r["differs"])))


def record_replacement(mpn, sha, rows, choice=None, d=None):
    """Store the proposal list (and the choice) in the part's cached dossier (the parts gate prints it)."""
    from . import sourcing as S
    dos = load(mpn, sha, d)
    if dos:
        dos["replacement"] = rows
        if choice is not None:
            dos["replacement_choice"] = choice
        S.write_json(_path(mpn, sha, d), dos, indent=1)


# ------------------------------------------------------------------ an unknowable part: the manufacturer's other documents
_DOC_TYPES = ("Datasheets", "Product Drawings", "Other Related Documents", "Design Resources", "Manuals", "Application Notes",
              "Featured Product", "User Guides", "Technical Reference")
WEB = """Find the manufacturer's official PDF documents for the electronic part {mpn}{mfr} that state: {need}
(product specification, hardware integration manual, user guide, application note, customer drawing). Use web search (e.g.
"{mpn} integration manual pdf", "{mpn} product specification pdf"); prefer the manufacturer's own site; a document the datasheet names
(e.g. an integration manual number) is searched by that name too. Give direct links even when the fetch tool cannot read the PDF. Return ONE JSON object, no prose: {{"urls": ["https://...pdf", ...]}} with at most 4 direct PDF links."""


def _dk_media(mpn):
    """[(media type, title, url)] of the part's DigiKey media links (by MPN, else by its DigiKey number from a keyword search)."""
    import urllib.parse
    import urllib.request
    from . import partsearch as PS, redact as R, sourcing as S
    c = PS.credentials()
    if "DIGIKEY_CLIENT_ID" not in c:
        return []
    tok = S._dk_token(c)

    def get(key):
        req = urllib.request.Request(f"https://api.digikey.com/products/v4/search/{urllib.parse.quote(key, safe='')}/media",
                                     headers={"X-DIGIKEY-Client-Id": c["DIGIKEY_CLIENT_ID"], "Authorization": "Bearer " + tok})
        return [(m.get("MediaType") or "", m.get("Title") or "", m.get("Url") or "")
                for m in json.load(R.urlopen(req, timeout=30)).get("MediaLinks") or []]
    try:
        return get(mpn)
    except Exception:                                       # noqa: BLE001 - not found by MPN: its DigiKey number
        pass
    try:
        rows = PS._dk_keyword(S._search_term(mpn), c, tok, 5)
        for p in (rows or {}).get("Products", []):
            if S._same_part(p.get("ManufacturerProductNumber", ""), mpn)[0]:
                for v in p.get("ProductVariations") or []:
                    if v.get("DigiKeyProductNumber"):
                        return get(v["DigiKeyProductNumber"])
    except Exception:                                       # noqa: BLE001
        pass
    return []


def doc_urls(mpn, need, mfr="", media=None, web=None):
    """Candidate URLs of the part's other documents: DigiKey media links (datasheets, drawings, manuals, related documents), then a
    model web search for the manufacturer's site. -> [(url, why)]."""
    out = [(u, f"digikey {t}: {ti}") for t, ti, u in (media or _dk_media)(mpn)
           if u and t in _DOC_TYPES and (u.lower().split("?")[0].endswith(".pdf") or "/docus/" in u or "/prints/" in u)]
    if web is not None or online():
        for m in (None, ESCALATE_MODEL):                    # Haiku searches first, Sonnet when it finds nothing
            try:
                reply, _ = (web or (lambda pr: call_model(pr, m, timeout=300, tools="WebSearch,WebFetch")))(
                    WEB.format(mpn=mpn, mfr=f" ({mfr})" if mfr else "", need=", ".join(need)))
                urls = [u for u in _json_of(reply).get("urls") or [] if isinstance(u, str) and u.startswith("http")]
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
                urls = []
            out += [(u, f"web search ({_model(m)})") for u in urls]
            if urls or web is not None:
                break
    return list(dict(out).items())


def fetch_docs(mpn, ds_dir, need, mfr="", media=None, web=None, download=None, log=lambda m: None, limit=8):
    """Download the part's other documents into the datasheets folder as `<MPN>__<name>.pdf` (dossier.siblings reads them). A file
    whose bytes are already in the folder is not kept twice. -> [new file paths]."""
    from . import partdocs as PD
    have = {PD._sha(os.path.join(ds_dir, f)) for f in os.listdir(ds_dir) if f.lower().endswith(".pdf")}
    new = []
    for url, why in doc_urls(mpn, need, mfr, media, web)[:limit]:
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(url.split("?")[0]))[:60] or "doc"
        dest = os.path.join(ds_dir, f"{PD._safe_name(mpn)}__{stem if stem.lower().endswith('.pdf') else stem + '.pdf'}")
        ok, detail = (download or (lambda u, f: PD._download(u, f, timeout=150)))(url, dest + ".part")   # manufacturer hosts are slow
        if ok and PD._sha(dest + ".part") not in have:
            os.replace(dest + ".part", dest)
            have.add(PD._sha(dest))
            new.append(dest)
        elif os.path.exists(dest + ".part"):
            os.remove(dest + ".part")
        log(f"    doc {url[:90]} ({why}): {'kept' if dest in new else detail if not ok else 'already in the folder'}")
    return new


def same_pinout(a, b):
    """True when two dossiers number the same pins with compatible names (every pad of `a` has a name `b` prints on that pad, ground
    on ground). False when either pin table is incomplete: an unverified pinout is not the same pinout."""
    if not a or not b or "pins" in gaps(a) or "pins" in gaps(b):
        return False
    ta, tb = pin_table(a), pin_table(b)
    return bool(ta) and set(ta) == set(tb) and all(ta[k][0] & tb[k][0] or (ta[k][1] and tb[k][1]) for k in ta)


def choose(mpn, dos, rows):
    """The replacement of an unknowable part. Applied (`choice`) only when it is a verified drop-in: distributor-verified footprint and
    ratings (partsgate.alternatives), knowable, and the same pinout as the part (same_pinout). Anything else is never applied: the best
    knowable candidate is recorded as a `proposal` with its D-row text, a blocker for the kickoff decision step; the part stays blocked.
    -> {"choice", "by", "rationale", "proposal"?, "blocker"?, "d_row"?}."""
    ok = [r for r in rows if r.get("knowable")]
    first = next((r for r in ok if r.get("drop_in") and r.get("same_pinout")), None)
    if first:
        return {"choice": first["mpn"], "by": "rule", "rationale": "knowable verified drop-in, same pinout"}
    if not ok:
        return {"choice": None, "by": "rule", "rationale": "no knowable candidate"}
    best = ok[0]
    why = "; ".join(best.get("differs") or []) or ("pinout differs or unverified" if best.get("drop_in") else "not a verified drop-in")
    return {"choice": None, "by": "rule", "rationale": f"blocked: {best['mpn']} is knowable but not a verified drop-in ({why})",
            "proposal": best["mpn"], "blocker": True,
            "d_row": f"Replace {mpn} by {best['mpn']} ({best.get('mfr') or '?'}): {mpn} cannot be fully known (no source gives "
                     f"{', '.join(dos.get('missing') or [])}); {best['mpn']} is fully documented but differs: {why}. Footprint, pinmap "
                     f"and spec change needed before placement."}


def replace_unknowable(dres, log_dir=None, log=print):
    """Every unknowable part of a build_all result: candidates (propose_replacement), the choice (choose), both stored in its dossier.
    -> {mpn: choice dict}."""
    out = {}
    for m in sorted(k for k, (dos, _, _) in dres.items() if dos and not dos.get("knowable")):
        dos = dres[m][0]
        rows = dos.get("replacement") if dos.get("replacement") is not None else propose_replacement(m, log=log, orig=dos)
        out[m] = choose(m, dos, rows)
        record_replacement(m, dos["sha256"], rows, out[m])
    return out
