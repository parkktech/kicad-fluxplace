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
  unknown[]                                             every section the datasheet did not give (never guessed)

Trust: the model is told to copy numbers as printed; `verify` keeps a fact only when its page exists and every number in it is on
that page (pin names: the name is on the page), else the fact moves to `rejected` with the reason. Downstream (elec.load_facts,
partdocs.spec_check) reads only verified facts, and a hand-entered parts-fact always wins over a dossier one.
The model call is `claude -p` (haiku by default; FLUXPLACE_DOSSIER_MODEL); FLUXPLACE_AI=0 or no `claude` CLI = offline: the dossier
is reported missing (not cached, retried next run). Nothing here names a board, a reference or an MPN.
"""
import concurrent.futures as CF
import json
import os
import re
import shutil
import subprocess
import time

VERSION = 1
MODEL = "haiku"
TEXT_BUDGET = 240_000                 # characters of datasheet text sent per part (~60k tokens)
LIST_SECTIONS = ("pins", "swap_groups", "supplies", "currents", "abs_max", "io_levels", "interfaces", "decoupling", "keepouts",
                 "rf", "app_circuit")
DICT_SECTIONS = ("package", "msl", "temp_range", "thermal", "crystal")
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
{{"category": "mcu|regulator|switch|interface|codec|sensor|rf|connector|passive|diode|transistor|logic|module|other",
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
Pins: every pin of the package. Swap groups: only pins the datasheet shows as equivalent (same function class, any one usable).
Connectors: pins are contacts; contacts of one kind (signal / power) form a swap group; contact current rating goes in currents (what=contact).

DATASHEET TEXT:
{text}
"""


# ------------------------------------------------------------------ cache
def cache_dir(d=None):
    from . import sourcing as S
    out = os.path.join(S.cache_home(d), "dossier")
    os.makedirs(out, exist_ok=True)
    return out


def _path(mpn, sha, d=None):
    return os.path.join(cache_dir(d), f"{re.sub(r'[^A-Za-z0-9._-]', '_', mpn)}.{(sha or 'nosha')[:12]}.json")


def load(mpn, sha, d=None):
    """The cached dossier of `mpn` for the datasheet with this sha256, or None."""
    try:
        with open(_path(mpn, sha, d)) as fh:
            x = json.load(fh)
        return x if x.get("version") == VERSION else None
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


def _check(fact, pg):
    """None when the fact stands on its page, else the reason it does not."""
    p = fact.get("p")
    if not isinstance(p, int) or not 1 <= p <= len(pg):
        return f"page {p!r} is not in the datasheet"
    text = _flat(pg[p - 1])
    for k, v in fact.items():
        if k == "p" or k in _TEXT_KEYS:
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


def verify(raw, pg):
    """The model's dossier with every fact checked against its page: unverified facts move to `rejected`. -> dossier dict."""
    out = {"category": raw.get("category") or "unknown", "description": raw.get("description") or "", "rejected": []}
    unknown = set(x for x in (raw.get("unknown") or []) if isinstance(x, str))
    for s in DICT_SECTIONS:
        f = raw.get(s)
        if isinstance(f, dict) and f:
            why = _check(f, pg)
            if why:
                out["rejected"].append({"section": s, "fact": f, "why": why})
            else:
                out[s] = f
        if s not in out:
            unknown.add(s)
    for s in LIST_SECTIONS:
        keep = []
        for f in raw.get(s) or []:
            if not isinstance(f, dict):
                continue
            why = _check(f, pg)
            if why:
                out["rejected"].append({"section": s, "fact": f, "why": why})
            else:
                keep.append(f)
        if keep:
            out[s] = keep
        else:
            unknown.add(s)
    out["unknown"] = sorted(unknown - set(out))
    return out


def n_facts(d):
    return sum(len(d.get(s) or []) for s in LIST_SECTIONS) + sum(1 for s in DICT_SECTIONS if d.get(s))


# ------------------------------------------------------------------ model
def online():
    return os.environ.get("FLUXPLACE_AI", "1") not in ("0", "off", "no") and shutil.which("claude") is not None


def call_model(prompt, model=None, timeout=900):
    """-> (reply text, cost USD). The local `claude -p` CLI, no tools, no project settings, run outside any project."""
    cmd = ["claude", "-p", "--model", model or os.environ.get("FLUXPLACE_DOSSIER_MODEL") or MODEL, "--output-format", "json",
           "--tools", "", "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
           "--system-prompt", "You extract datasheet facts into JSON. Output JSON only."]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cache_dir())
    env = json.loads(r.stdout or "{}")
    if r.returncode or env.get("is_error"):
        raise RuntimeError((env.get("result") or r.stderr or "claude -p failed").strip()[:300])
    return env.get("result") or "", float(env.get("total_cost_usd") or 0.0)


def _json_of(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in the reply")
    return json.loads(m.group(0))


def build(mpn, pdf, sha, ask=None, model=None, d=None, refresh=False):
    """The dossier of one part: cached when the datasheet is unchanged, else read by the model and verified.
    ask(prompt) -> (text, cost) replaces the model (tests). -> (dossier | None, status, cost)."""
    if not refresh:
        hit = load(mpn, sha, d)
        if hit:
            return hit, "cached", 0.0
    if ask is None and not online():
        return None, "offline (FLUXPLACE_AI=0 or no claude CLI)", 0.0
    pg = pages(pdf)
    if not any(t.strip() for t in pg):
        return None, "no text (no text layer and OCR read nothing)", 0.0
    sel = select(pg)
    text = "\n".join(f"=== PAGE {n} ===\n{pg[n - 1]}" for n in sel)
    t0 = time.time()
    try:
        reply, cost = (ask or (lambda pr: call_model(pr, model)))(PROMPT.format(mpn=mpn, text=text))
        raw = _json_of(reply)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
        return None, f"model failed: {e}", 0.0
    dos = verify(raw, pg)
    dos.update(mpn=mpn, file=os.path.basename(pdf), sha256=sha, version=VERSION, pages_read=sel, n_pages=len(pg),
               model=model or os.environ.get("FLUXPLACE_DOSSIER_MODEL") or MODEL, cost_usd=round(cost, 5),
               seconds=round(time.time() - t0, 1), made=time.strftime("%Y-%m-%dT%H:%M:%S"))
    from . import sourcing as S
    S.write_json(_path(mpn, sha, d), dos, indent=1)
    return dos, "made", cost


def build_all(ds_dir, mpns, jobs=4, ask=None, model=None, d=None, refresh=False, log=print):
    """Dossiers for every MPN whose datasheet is in the manifest, `jobs` at a time. -> {mpn: (dossier|None, status, cost)}."""
    from . import partdocs as PD
    man = PD.load_manifest(ds_dir)
    todo = []
    for m in dict.fromkeys(mpns):
        ent = man.get(m)
        f = os.path.join(ds_dir, ent["file"]) if isinstance(ent, dict) and ent.get("file") else None
        if f and os.path.isfile(f):
            todo.append((m, f, ent.get("sha256") or PD._sha(f)))
    res = {}
    with CF.ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futs = {ex.submit(build, m, f, s, ask, model, d, refresh): m for m, f, s in todo}
        for fu in CF.as_completed(futs):
            m = futs[fu]
            res[m] = fu.result()
            dos, st, cost = res[m]
            log(f"  dossier {m}: {st}" + (f", {n_facts(dos)} facts, {len(dos['unknown'])} unknown, {len(dos['rejected'])} rejected"
                                          if dos else "") + (f", ${cost:.3f}" if cost else ""))
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
    distance per pin, connector contact rating. A key the dossier does not back is absent."""
    src = lambda p: f"{dos['file']}#p{p} (dossier)"     # noqa: E731
    out = {}
    lo, hi, pins, srcs = {}, {}, [], []
    gnd = {p.get("name") for p in dos.get("pins") or [] if p.get("type") == "ground"}
    for s in dos.get("supplies") or []:
        mn, mx = _si(s.get("min"), s.get("unit"), "V"), _si(s.get("max"), s.get("unit"), "V")
        for pn in s.get("pins") or []:
            if pn not in gnd and (mn is not None or mx is not None):      # a range "VS = V+ - V-" names the ground pin too
                pins.append(pn)
                srcs.append(s["p"])
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
            out["decap_mm"] = {"value": dist, "source": src(c["p"])}
    contact = [(_si(c.get("max") if c.get("max") is not None else c.get("typ"), c.get("unit"), "A"), c["p"])
               for c in dos.get("currents") or [] if c.get("what") == "contact"]
    contact = [x for x in contact if x[0]]
    if contact and dos.get("category") == "connector":
        a, p = min(contact)
        out["contact_a"] = {"value": a, "source": src(p)}
    return out


def pin_table(dos):
    """{pad number: {normalized name, alternates}} of a dossier's verified pins."""
    from . import partdocs as PD
    out = {}
    for p in dos.get("pins") or []:
        names = {PD._norm(p.get("name") or "")} | {PD._norm(a) for a in p.get("alt") or [] if isinstance(a, str)}
        for n in p.get("num") or []:
            out.setdefault(str(n), set()).update(x for x in names if x)
    return out


def pinmap_mismatches(pinmap, dos):
    """[(spec name, pad, dossier names)] where the spec puts a name on a pad the dossier gives other names."""
    from . import partdocs as PD
    tab = pin_table(dos)
    bad = []
    for name, pad in (pinmap or {}).items():
        for pd in (pad if isinstance(pad, list) else [pad]):
            have = tab.get(str(pd))
            if have and not (PD._aliases(name) & have):
                bad.append((name, str(pd), sorted(have)))
    return bad
