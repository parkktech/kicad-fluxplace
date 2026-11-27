"""Limits registry + limit-challenge (w110).  A numeric-limit FAIL is re-graded against the SOURCED hard limit of its rule/interface,
never against a project target: over the hard limit = FAIL stays; between target and hard = WARN with the citation.

Registry: fluxplace/data/limits.json (tracked seed) overlaid by $FLUXPLACE_LIMITS_DIR/limits.json (default ~/.cache/fluxplace/limits,
written by research, shared across boards).  Key "<rule>:<interface>"; entry {rule, interface, unit, target, hard, basis (sourced |
arithmetic | project), confidence, date, function, sources [{url, page, quote}], notes, unverified [..]}.  An entry without a source
carries no `hard`: an unsourced answer may only add `unverified` notes.  Constraints keep the project's choice as the target; the
project's explicit maximum (skew_max_*) wins over the registry.

    [limits] research = "on"      # live research (decider.call_model with web tools) when a FAIL has no sourced hard value
    [limits] model = "sonnet", timeout_s = 300, budget_usd = 1.0

Every re-grade and every research is appended to <log_dir>/decisions.jsonl (decider format), listed as a finding, never silent."""
import json
import os
import time

from . import decider as DC

SEED = os.path.join(os.path.dirname(__file__), "data", "limits.json")


def cache_path():
    return os.path.join(os.environ.get("FLUXPLACE_LIMITS_DIR") or os.path.expanduser("~/.cache/fluxplace/limits"), "limits.json")


def _read(p):
    try:
        with open(p) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def load():
    """seed overlaid by the cache; a cache entry replaces the seed one only when it has a sourced hard value or the seed has none."""
    reg = _read(SEED)
    for k, e in _read(cache_path()).items():
        if k not in reg or e.get("hard") is not None:
            reg[k] = e
        else:
            reg[k] = dict(reg[k], unverified=sorted(set(reg[k].get("unverified", []) + e.get("unverified", []))))
    return reg


def key(rule, iface):
    return f"{rule}:{iface}"


def get(rule, iface):
    return load().get(key(rule, iface))


def sourced(e):
    """a hard value counts only with a source that has a URL and a quote."""
    return bool(e) and e.get("hard") is not None and any(s.get("url") and s.get("quote") for s in e.get("sources") or [])


def cite(e):
    s = (e.get("sources") or [{}])[0]
    return f"{s.get('url', '?')} {s.get('page', '')}".strip()


def _log(log_dir, rid, choice, rationale, evidence, model=None, reasoned=True, **extra):
    if log_dir:
        DC._write(log_dir, dict({"id": rid, "hash": rid, "choice": choice, "by": "limits", "reasoned": reasoned, "cached": False,
                                 "model": model, "rationale": rationale[:400], "evidence": evidence[:8],
                                 "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, **extra))


def _prompt(rule, iface, measured, target, unit, ctx):
    return (f"Research one PCB design limit and answer with sources.\nRule: {rule}; interface/class: {iface}; unit: {unit}.\n"
            f"The project's target is {target} {unit}; the board measures {measured} {unit}. Context: {ctx}\n"
            "Use web search; prefer standards, silicon/magnetics/connector vendor layout guides and datasheets. Questions:\n"
            "1. Is this a real HARD limit (where the link/function or a spec actually breaks) or only a target / recommendation?\n"
            "2. What is the sourced HARD limit (a number printed in a source), and does the measured value break function or only the target?\n"
            "Rules: every number needs a source with a URL, page/section and a short verbatim quote. If you find no printed number, "
            'set hard to null. Never invent a value.\nAnswer with ONLY JSON: {"hard": <number|null>, "target": <number|null>, '
            '"unit": "<unit>", "basis": "sourced|arithmetic", "confidence": "high|medium|low", "function": "<=2 sentences: what breaks and '
            'at what value", "sources": [{"url": "...", "page": "...", "quote": "..."}], "unverified": ["<claims without a source>"]}')


def research(rule, iface, measured, target, unit, cons=None, log_dir=None, ctx="", force=False):
    """One live research pass (cached per rule+interface in the registry, also negative results).  -> the entry or None.
    A hard value without a URL+quote source is dropped to an `unverified` note: unsourced answers never loosen a limit."""
    k = key(rule, iface)
    e = load().get(k)
    if e and not force:
        return e
    cfg = (cons or {}).get("limits") or {}
    model = str(cfg.get("model") or "sonnet")
    t0 = time.time()
    out, why = DC.call_model(_prompt(rule, iface, measured, target, unit, ctx), cons, model, tools="WebSearch,WebFetch",
                             timeout=cfg.get("timeout_s", 300), budget_usd=cfg.get("budget_usd", 1.0))
    d = DC._first_obj(out, lambda d: "hard" in d) if out else None
    secs, cost = round(time.time() - t0, 1), DC.LAST.get("cost_usd")
    if not d:
        _log(log_dir, f"limit-research:{k}", "no-answer", f"research failed: {why or 'no JSON'} ({secs}s)", [], model, False)
        return None
    srcs = [{"url": str(s.get("url", ""))[:300], "page": str(s.get("page", ""))[:60], "quote": str(s.get("quote", ""))[:300]}
            for s in d.get("sources") or [] if isinstance(s, dict) and str(s.get("url", "")).startswith("http") and s.get("quote")]
    hard = d.get("hard")
    try:
        hard = float(hard) if hard is not None else None
    except (TypeError, ValueError):
        hard = None
    unv = [str(x)[:200] for x in d.get("unverified") or []][:6]
    if hard is not None and not srcs:
        unv.append(f"unsourced hard value {hard} {unit} ignored")
        hard = None
    e = {"rule": rule, "interface": iface, "unit": str(d.get("unit") or unit), "target": target if d.get("target") is None else d["target"],
         "hard": hard, "basis": "sourced" if hard is not None else "unverified", "confidence": str(d.get("confidence") or "low"),
         "date": time.strftime("%Y-%m-%d"), "function": str(d.get("function") or "")[:400], "sources": srcs[:6], "notes": "researched",
         "unverified": unv, "research": {"model": model, "seconds": secs, "cost_usd": cost}}
    try:
        p = cache_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        c = _read(p)
        c[k] = e
        with open(p, "w") as fh:
            json.dump(c, fh, indent=1, sort_keys=True)
    except OSError:
        pass
    _log(log_dir, f"limit-research:{k}", "hard=%s" % hard, e["function"] or "no sourced hard limit", [cite(e)] if srcs else [], model,
         hard is not None, seconds=secs, cost_usd=cost)
    return e


def regrade(rule, iface, subject, measured, project_max, unit, target, explicit, cons=None, log_dir=None, ctx=""):
    """The limit challenge for one FAIL (measured > project_max).  -> (level, text) or None (leave the finding as it is).
    `explicit` = the project set project_max itself (it then wins).  Over the sourced hard limit stays FAIL (text carries the citation)."""
    if explicit:
        return None
    e = get(rule, iface)
    cfg = (cons or {}).get("limits") or {}
    if not sourced(e) and str(cfg.get("research", "off")).lower() in ("on", "true", "1", "yes"):
        e = research(rule, iface, measured, target, unit, cons, log_dir, ctx) or e
    if not sourced(e) or e.get("unit") != unit:
        return None
    hard, tgt = float(e["hard"]), float(e.get("target") if e.get("target") is not None else target)
    c = cite(e)
    if measured > hard:
        _log(log_dir, f"limit:{key(rule, iface)}:{subject}", "FAIL-stays", f"{measured:g} {unit} > sourced hard {hard:g} ({e['basis']}, {e['confidence']})", [c])
        return "FAIL", f"{subject}: {measured:.2f} {unit} > sourced hard limit {hard:g} {unit} [{c}]"
    _log(log_dir, f"limit:{key(rule, iface)}:{subject}", "FAIL->WARN",
         f"{measured:g} {unit} within sourced hard {hard:g} (project target {max(tgt, project_max):g}); {e.get('function', '')}", [c])
    return "WARN", (f"{subject}: {measured:.2f} {unit} over target {project_max:g} but inside the sourced hard limit {hard:g} {unit} "
                    f"[{c}; {e['basis']}, {e['confidence']}; re-graded FAIL->WARN by the limits registry]")


# ---- fab capability rows: direction "min" (a value BELOW hard is rejected by the house; below recommended = DFM warning / auto-adjust)
FAB_FEATURES = {"track_min": "minimum trace width (outer and inner layers separately if they differ)",
                "space_min": "minimum copper-to-copper spacing",
                "via_annular_min": "minimum annular ring of a via pad (pad radius minus drill radius, on a multilayer board)",
                "pth_annular_min": "minimum annular ring of a plated component (PTH) hole"}


def _fab_prompt(house, layers):
    feats = "\n".join(f"- {k}: {v}" for k, v in FAB_FEATURES.items())
    return (f"Research the PUBLISHED manufacturing capability of {house} for a {layers}-layer rigid FR-4 PCB (standard, no-surcharge service) "
            f"from the house's own capabilities page or help articles. Use web search / fetch.\nFeatures (mm):\n{feats}\n"
            "For each feature give: hard = the value below which the house rejects the order or cannot build it; recommended = the value "
            "below which its DFM check warns, auto-adjusts or charges extra (null if the page states only one value). Every number needs "
            "the page URL, the section/row and a short verbatim quote; a value with no printed source must be null, never estimated. "
            "Also note the retrieval date.\nAnswer with ONLY JSON: {\"items\": [{\"feature\": \"track_min\", \"hard\": <mm|null>, "
            "\"recommended\": <mm|null>, \"url\": \"...\", \"page\": \"...\", \"quote\": \"...\", \"confidence\": \"high|medium|low\", "
            "\"note\": \"...\"}]}")


def research_fab(house, layers=6, cons=None, log_dir=None, force=False):
    """One research pass for a house's capability rows -> {feature: entry}; stored as `fab_<feature>:<house>-<layers>l` (direction "min").
    A value without URL + quote is dropped (unverified note).  Cached per house and layer count."""
    iface = f"{house}-{layers}l"
    have = {f: load().get(key("fab_" + f, iface)) for f in FAB_FEATURES}
    if all(have.values()) and not force:
        return have
    cfg = (cons or {}).get("limits") or {}
    model = str(cfg.get("model") or "sonnet")
    t0 = time.time()
    out, why = DC.call_model(_fab_prompt(house, layers), cons, model, tools="WebSearch,WebFetch", timeout=cfg.get("timeout_s", 300),
                             budget_usd=cfg.get("budget_usd", 1.5))
    secs, cost = round(time.time() - t0, 1), DC.LAST.get("cost_usd")
    d = DC._first_obj(out, lambda d: isinstance(d.get("items"), list)) if out else None
    if not d:
        _log(log_dir, f"limit-research:fab:{iface}", "no-answer", f"research failed: {why or 'no JSON'} ({secs}s)", [], model, False)
        return have
    p = cache_path()
    c = _read(p)
    for it in d["items"]:
        f = str(it.get("feature"))
        if f not in FAB_FEATURES:
            continue
        ok = str(it.get("url", "")).startswith("http") and it.get("quote")
        num = lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        hard, rec = (num(it.get("hard")), num(it.get("recommended"))) if ok else (None, None)
        if rec is not None and hard is not None and rec < hard:
            rec = hard
        e = {"rule": "fab_" + f, "interface": iface, "unit": "mm", "direction": "min", "target": rec, "hard": hard,
             "basis": "sourced" if hard is not None else "unverified", "confidence": str(it.get("confidence") or "low"),
             "date": time.strftime("%Y-%m-%d"), "function": str(it.get("note") or "")[:300],
             "sources": [{"url": str(it["url"])[:300], "page": str(it.get("page", ""))[:80], "quote": str(it["quote"])[:300]}] if ok else [],
             "unverified": [] if ok else [f"{f}: no sourced value"], "research": {"model": model, "seconds": secs, "cost_usd": cost}}
        c[key("fab_" + f, iface)] = e
        have[f] = e
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            json.dump(c, fh, indent=1, sort_keys=True)
    except OSError:
        pass
    _log(log_dir, f"limit-research:fab:{iface}", f"{sum(1 for e in have.values() if e and e.get('hard') is not None)} sourced", "fab capability rows", [],
         model, True, seconds=secs, cost_usd=cost)
    return have


def grade_min(rule, iface, measured, unit="mm"):
    """-> (level, text) for a value that must stay ABOVE a house limit: below hard = FAIL, below recommended = WARN, else None (ok);
    ("NONE", "") when the registry has no sourced hard value."""
    e = get(rule, iface)
    if not sourced(e) or e.get("unit") != unit:
        return "NONE", ""
    hard, rec, c = float(e["hard"]), e.get("target"), cite(e)
    if measured < hard - 1e-6:
        return "FAIL", f"{measured:.4f} {unit} < {iface} hard limit {hard:g} {unit} [{c}, {e['date']}]"
    if rec is not None and measured < float(rec) - 1e-6:
        return "WARN", f"{measured:.4f} {unit} between {iface} hard {hard:g} and recommended {float(rec):g} {unit} [{c}, {e['date']}]"
    return None, ""


def fab_bounds(prof, layers):
    """{feature: (hard, recommended, citation)} for the house row of a fab profile (`prof` = constraints.fab_profile).  Registry value
    when sourced, else the fabcaps row's hard limit (no recommended).  {} without a profile or layer count."""
    v, n = prof.get("vendor"), layers or prof.get("layers")
    if not v or not n:
        return {}
    out = {}
    for f in FAB_FEATURES:
        e = get("fab_" + f, f"{v}-{n}l")
        if sourced(e):
            out[f] = (float(e["hard"]), None if e.get("target") is None else float(e["target"]), f"{cite(e)}, {e['date']}")
        elif (prof.get("hard") or {}).get(f) is not None:
            out[f] = (float(prof["hard"][f]), None, f"fabcaps {prof.get('name')}")
    return out
