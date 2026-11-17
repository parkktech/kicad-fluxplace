"""Registers the part-entry / readiness / pin-group commands (parts, ready, pin-groups, part-diff) in cli.py with one call."""
import argparse
import json
import os

from . import questions as QS


def _flags(p):
    kw = {"default": argparse.SUPPRESS}
    p.add_argument("--full", dest="full_output", action="store_true", help="every finding, not the worst N", **kw)
    p.add_argument("-q", "--quiet", dest="quiet", action="store_true", help="verdict line only", **kw)
    p.add_argument("--top", dest="top", type=int, help="worst N findings (default 10)", **kw)


def _common(p, board=True):
    if board:
        p.add_argument("--board", default=None, help="board .kicad_pcb (pad counts, nets, stackup)")
    p.add_argument("--constraints", default=None, help="constraints TOML ([parts], [docs], [stackup], [route])")
    p.add_argument("--datasheets", default=None, help="datasheet dir (default: [docs] datasheets)")
    p.add_argument("--mpn-map", dest="mpn_map", default=None, help="mpn_map.json (ref -> MPN)")
    p.add_argument("--no-api", dest="no_api", action="store_true", help="offline: distributor data and stock are 'not checked'")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    QS.add_args(p, json_flag=False)
    _flags(p)


def load(a, need_board=False):
    """(spec, cons, facts|None, mpn_map, ds_dir)."""
    from . import constraints as C, review as R, parts as P, sourcing as S
    spec = json.load(open(a.spec))
    cons = C.load(getattr(a, "constraints", None))
    board = getattr(a, "board", None)
    if need_board and not board:
        raise SystemExit(f"{a.cmd}: --board is required")
    facts = R.facts_from_board(board, mpn_map=getattr(a, "mpn_map", None), backend="file") if board else None
    mm = (facts or {}).get("mpn_map") or {}
    if not mm and not board:
        path = getattr(a, "mpn_map", None) or S.find_map(a.spec)
        if path and os.path.exists(path):
            mm = {k: v for k, v in json.load(open(path)).items() if not k.startswith("_") and isinstance(v, str) and v}
    anchors = [x for x in (getattr(a, "constraints", None), a.spec, board) if x]
    ds = P.resolve_dir(getattr(a, "datasheets", None) or (cons.get("docs") or {}).get("datasheets"), anchors)
    return spec, cons, facts, mm, ds


def _stem(a):
    return os.path.splitext(getattr(a, "board", None) or a.spec)[0]


def _session(a, cons, cmd):
    return QS.Session(getattr(a, "board", None) or a.spec, cons, QS.parse_answers(a.answer), a.assume_defaults, cmd)


def _finish_questions(sess, a):
    """-> EXIT (3) when questions are open (printed, written), else 0."""
    return sess.gate(want_json=False)


def cmd_parts(a):
    from . import parts as P
    spec, cons, facts, mm, ds = load(a)
    sess = _session(a, cons, "parts")
    res = P.evaluate(spec, cons, facts=facts, mpn_map=mm, ds_dir=ds, api=not a.no_api, cache_dir=os.path.dirname(os.path.abspath(a.spec)),
                     session=sess)
    done = []
    if a.fix:
        if not a.out:
            raise SystemExit("parts --fix needs --out (the input spec is never overwritten)")
        sp2, done = P.apply_fix(spec, res, res.get("data"), sess)
    path = P.write_report(res, _stem(a), extra={"fixed": done})
    if a.json:
        print(json.dumps({"totals": P.totals(res["findings"]), "counts": P.counts(res["findings"]), "skipped": res["skipped"],
                          "unchecked": res["unchecked"], "findings": res["findings"]}, indent=1))
    elif getattr(a, "quiet", False):
        t = P.totals(res["findings"])
        print(f"parts: {'FAIL' if t['FAIL'] else 'ok'} - {t['FAIL']} FAIL, {t['WARN']} WARN")
    else:
        print(P.format_report(res, "parts", top=getattr(a, "top", 10) or 10, full=getattr(a, "full_output", False), path=path))
    q = sess.gate(want_json=False)
    if q:
        raise SystemExit(q)
    if a.fix:
        with open(a.out, "w") as fh:
            json.dump(sp2, fh, indent=1)
        print(f"parts --fix: wrote {a.out} ({len(done)} field(s) set; the input is untouched)")
    if P.totals(res["findings"])["FAIL"]:
        raise SystemExit(1)


def _gate_inputs(a):
    """Shared by parts-todo / parts-apply: -> (spec, cons, facts, mm, ds, qty, cache_dir)."""
    from . import parts as P
    spec, cons, facts, mm, ds = load(a)
    return spec, cons, facts, mm, ds, P.build_quantity(cons), os.path.dirname(os.path.abspath(a.spec))


def _verifier(ds):
    """Candidate datasheets are fetched (and validated) into <datasheets>/_candidates, not into the project's own sheets."""
    from . import dsfetch as D

    def v(mpn, url=None):
        r = D.fetch_datasheet(mpn, os.path.join(ds, "_candidates"), extra_urls=[url] if url else [])
        f = os.path.join(ds, "_candidates", r["file"]) if r.get("file") else None
        return {"level": r.get("level"), "file": os.path.relpath(f, ds) if f else None, "text": D.pdf_text(f) if f else None,
                "tried": r.get("tried")}
    return v


def _dropin(spec, cons, lookup, stocker, ds):
    """parts-apply's own verification of a substitution: the agent's word is never taken for `drop_in`."""
    from . import dropin as DI

    def f(old, new, refs):
        comps = {c.get("ref"): c for c in spec.get("components", [])}
        d_old, d_new = lookup(old), lookup(new)
        if not d_old or not d_new:
            return {"drop_in": False, "differs": [], "unverified": [f"{old if not d_old else new}: no distributor data"], "checks": {}}
        sk = stocker(new, refs) or {}
        pc = (cons or {}).get("parts") or {}
        temp = tuple(None if pc.get(k) is None else float(pc[k]) for k in ("temp_min", "temp_max"))
        v = _verifier(ds)(new, d_new.get("datasheet")) if ds else None
        cand = DI.row_for(d_new, new) | {"stock": sk.get("stock"), "lifecycle": d_new.get("lifecycle")}
        qty = sk.get("need") or sk.get("required") or sk.get("stock") or 1
        return DI.classify(DI.row_for(d_old, old), cand, comps[refs[0]].get("fp"), min(qty, sk.get("stock") or 0) or 1, temp, v)
    return f


def cmd_parts_todo(a):
    """The machine-readable work list for an agent: one record per part, what is missing, the evidence, the candidates."""
    from . import parts as P, partsgate as PG
    spec, cons, facts, mm, ds, qty, cache = _gate_inputs(a)
    sess = _session(a, cons, "parts-todo")
    res = P.evaluate(spec, cons, facts=facts, mpn_map=mm, ds_dir=ds, api=not a.no_api, cache_dir=cache, session=sess, qty=qty)
    g = PG.gate(spec, cons, facts, res, qty=qty)
    searcher = None
    from . import partsearch as PS
    if not a.no_api and not a.no_search:
        searcher = PS.keyword_search
    out = PG.todo(spec, cons, res, g, qty=qty, searcher=searcher, limit=a.limit,
                  verifier=None if a.no_verify or not ds else _verifier(ds),
                  enricher=(lambda m: PS.lookup(m, cache_dir=cache)) if searcher else None)
    out["gate"] = PG.summary_line(g)
    out["unchecked"] = g["unchecked"]
    if a.only_missing:
        out["parts"] = [r for r in out["parts"] if r["missing"]]
    txt = json.dumps(out, indent=1, default=str)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(txt + "\n")
    if a.json:
        print(txt)
    else:
        print(out["gate"])
        for u in g["unchecked"]:
            print("  " + u)
        by = {}
        for r in out["parts"]:
            by.setdefault(r["group"], []).append(r)
        for gk, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
            if rs[0]["missing"]:
                n = sum(1 for c in rs[0]["candidates"] if c.get("fits"))
                print(f"  {len(rs):3d} x {rs[0]['value']!s:28.28s} {rs[0]['footprint']!s:32.32s} missing {','.join(rs[0]['missing'])}"
                      + (f"  ({n} fitting candidate(s))" if rs[0]["candidates"] else ""))
        if a.out:
            print(f"full list: {a.out}")
    if g["parts"] and a.strict:
        raise SystemExit(1)


def cmd_parts_apply(a):
    """Validate proposals and write only what passes (spec + mpn_map); owner decisions and rejects are listed, never applied."""
    from . import parts as P, partsgate as PG, partsearch as PS, partdocs as PD
    spec, cons, facts, mm, ds, qty, cache = _gate_inputs(a)
    props = json.load(open(a.proposals))
    props = props.get("proposals", props) if isinstance(props, dict) else props
    mpn_map = a.mpn_map or None
    if not mpn_map:
        from . import sourcing as S
        mpn_map = S.find_map(a.spec)
    man = PD.load_manifest(ds) if ds else {}

    def ds_ok(mpn, prop):
        comp = {"datasheet": prop.get("datasheet")} if prop.get("datasheet") else None
        if P.datasheet_file(mpn, ds, comp, man):
            return True
        if a.no_fetch or not ds:
            return False
        from . import dsfetch as D
        return D.fetch_datasheet(mpn, ds, extra_urls=[prop["datasheet_url"]] if prop.get("datasheet_url") else [])["ok"]

    def model_ok(ref, comp, prop):
        ok, why = PG.model_state(ref, facts)
        if ok:
            return True, why
        m = prop.get("model")
        if m and os.path.isfile(m if os.path.isabs(m) else os.path.join(os.path.dirname(os.path.abspath(getattr(a, "board", None) or a.spec)), m)):
            return True, "model file given"
        return False, why + " (give `model`, or `fluxplace models --fetch` first)"

    rep = PG.apply(a.spec, mpn_map, props, cons, facts, ds, lambda m: PS.lookup(m, cache_dir=cache),
                   PS.stocker(qty, cache_dir=cache), model_ok=model_ok, ds_ok=ds_ok, dry_run=a.dry_run,
                   dropin=_dropin(spec, cons, lambda m: PS.lookup(m, cache_dir=cache), PS.stocker(qty, cache_dir=cache), ds))
    if a.json:
        print(json.dumps(rep, indent=1))
    else:
        print(f"parts-apply: {len(rep['applied'])} applied{' (dry run: nothing written)' if a.dry_run else ''}, "
              f"{len(rep['owner'])} need the owner, {len(rep['rejected'])} rejected")
        for k in ("owner", "rejected"):
            for x in rep[k]:
                print(f"  {k.upper():8s} #{x['proposal']} {x['what']}: " + "; ".join(x["reasons"])[:300])
    if rep["owner"] or rep["rejected"]:
        raise SystemExit(1)


def cmd_parts_fact(a):
    """Record datasheet-verified facts for an MPN, each with its page: parts-fact MPN vin_min=4.5 vin_max=5.5 --source X.pdf#p7."""
    from . import elec as EL, parts as P
    ds = P.resolve_dir(a.datasheets, [os.getcwd()])
    kv = {}
    for item in a.facts:
        k, _, v = item.partition("=")
        if not v:
            raise SystemExit(f"parts-fact: '{item}' is not key=value")
        kv[k] = EL._coerce(v)
    try:
        keys, warn = EL.set_facts(ds, a.mpn, kv, a.source, force=a.force)
    except ValueError as e:
        raise SystemExit(f"parts-fact: {e}")
    for w in warn:
        print("  warning: " + w)
    print(f"parts-fact: {a.mpn}: {', '.join(keys)} recorded ({a.source})")


def cmd_parts_fetch(a):
    """Datasheets through the ordered source chain (dsfetch): every attempt recorded, every file validated."""
    from . import dsfetch as D, parts as P
    ds = a.datasheets
    spec = json.load(open(a.spec)) if a.spec else None
    mpns = [m.strip() for x in a.mpns for m in str(x).split(",") if m.strip()]   # comma- and space-separated alike
    if spec:
        from . import partdocs as PD
        ds = ds or P.resolve_dir(((__import__("fluxplace.constraints", fromlist=["load"]).load(a.constraints) or {}).get("docs") or {}).get("datasheets"),
                                [x for x in (a.constraints, a.spec) if x])
        man = PD.load_manifest(ds) if ds else {}
        mm = {}
        if a.mpn_map and os.path.exists(a.mpn_map):
            mm = {k: v for k, v in json.load(open(a.mpn_map)).items() if not k.startswith("_") and isinstance(v, str)}
        for c in spec.get("components", []):
            m = c.get("mpn") or mm.get(c.get("ref"))
            if m and not P.datasheet_file(m, ds, c, man) and m not in mpns:
                mpns.append(m)
    if not ds:
        raise SystemExit("parts-fetch: --datasheets (or --spec + constraints [docs] datasheets) is required")
    if a.file:
        if len(set(mpns)) != 1:
            raise SystemExit("parts-fetch --file needs exactly one MPN")
        ok, level, why = D.adopt_file(mpns[0], ds, a.file)
        print(f"{mpns[0]}: " + (f"adopted {a.file} [{level}] {why}" if ok else f"REJECTED {a.file}: {why}"))
        raise SystemExit(0 if ok else 1)
    res = [D.fetch_datasheet(m, ds, refresh=a.refresh) for m in sorted(set(mpns))]
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        for r in res:
            print(f"{r['mpn']}: " + (f"{r['level']} <- {r['source']} {r['file']}" + (" (cached)" if r["cached"] else "") if r["ok"] else "NOT FETCHED"))
            if not r["ok"] or a.verbose:
                for t in r["tried"]:
                    print(f"    {t['source']:14s} {(t['url'] or '-')[:70]:70s} {t['outcome'][:100]}")
        print(f"parts-fetch: {sum(r['ok'] for r in res)} of {len(res)} fetched ({sum(r['level'] == 'series' for r in res)} series-level)")
    if not all(r["ok"] for r in res):
        raise SystemExit(1)


def cmd_parts_page(a):
    """Render a datasheet page to PNG for the agent to read (drawing pages have no text)."""
    from . import parts as P, partdocs as PD, partcite as PC2
    ds = a.datasheets
    man = PD.load_manifest(ds) if ds else {}
    f = P.datasheet_file(a.mpn, ds, None, man) if ds else None
    if not f:
        raise SystemExit(f"parts-page: no datasheet on disk for {a.mpn} in {ds}")
    pdf = f if os.path.exists(f) else os.path.join(ds, f)
    if a.pages:
        print(f"{pdf}: {PC2.page_count(pdf)} page(s)")
        return
    ok, detail = PC2.render_page(pdf, a.page, a.png, dpi=a.dpi)
    print(detail)
    if not ok:
        raise SystemExit(1)


def cmd_parts_cite(a):
    """Record the pin table the agent read from a page image, compared with the spec and the footprint; a mismatch is a FINDING."""
    from . import parts as P, partdocs as PD, partcite as PC2, partsgate as PG
    spec, cons, facts, mm, ds = load(a)
    read = json.load(open(a.read))
    comp = next((c for c in spec["components"] if c.get("ref") == a.ref), None)
    if comp is None:
        raise SystemExit(f"parts-cite: {a.ref} not in the spec")
    mpn = comp.get("mpn") or mm.get(a.ref)
    man = PD.load_manifest(ds) if ds else {}
    f = P.datasheet_file(mpn, ds, comp, man) if mpn else None
    if not f:
        raise SystemExit(f"parts-cite: no datasheet on disk for {a.ref} ({mpn})")
    pads = None
    if facts:
        pp = (facts.get("parts") or {}).get(a.ref) or {}
        pads = len([n for n in (pp.get("pads") or {}) if n and not PC2._SHELL.match(str(n))]) or None
    res = PC2.compare(spec, a.ref, read, pad_count=pads)
    fname = os.path.basename(f)
    path = PC2.record(ds, a.ref, mpn, fname, a.page, read, res)
    wrote = False
    if res["ok"] and a.write_source and PC2.set_source(spec, a.ref, fname, a.page):
        PG._write_json(a.spec, spec)
        wrote = True
    print(f"{a.ref} {mpn} {fname}#p{a.page}: " + ("MATCH" if res["ok"] else f"FINDING x{len(res['findings'])}") + f" (recorded in {path}"
          + (", pinmap_source set" if wrote else "") + ")")
    for x in res["findings"]:
        print("  FINDING " + x)
    if not res["ok"]:
        raise SystemExit(1)


def cmd_parts_model(a):
    """3D model source chain for refs/MPNs: project library -> DigiKey media -> EasyEDA/LCSC -> manufacturer; validated, never faked."""
    from . import modelchain as MC
    spec, cons, facts, mm, ds = load(a)
    reg_path = a.sources
    reg = json.load(open(reg_path)) if reg_path and os.path.exists(reg_path) else {}
    comps = {c.get("ref"): c for c in spec["components"]}
    out = []
    for ref in a.refs:
        c = comps.get(ref)
        mpn = (c or {}).get("mpn") or mm.get(ref)
        if not mpn:
            out.append({"ref": ref, "ok": False, "tried": [{"source": "-", "outcome": "no MPN"}]})
            continue
        size = MC.fp_dims(a.board, ref) if a.board else None
        r = MC.resolve(mpn, a.lib, fp_size=size, registry=reg, force=a.force, scratch=a.scratch)
        out.append({"ref": ref, "mpn": mpn, "fp_mm": size, **r})
        if r["ok"] and a.register and reg_path and r["source"] != "project-library":
            dest = os.path.join(a.lib, os.path.basename(r["path"]))
            if os.path.abspath(r["path"]) != os.path.abspath(dest):
                import shutil
                shutil.copyfile(r["path"], dest)
            reg.setdefault("real", {})[ref] = f"{mpn} - {r['source']}, fetched {__import__('time').strftime('%Y-%m-%d')} -> hardware/lib/3dmodels/{os.path.basename(dest)} (parts-model; not yet wired to the footprint)"
            PG_write = __import__("fluxplace.partsgate", fromlist=["_write_json"])._write_json
            PG_write(reg_path, reg)
    if a.json:
        print(json.dumps(out, indent=1, default=str))
    else:
        for r in out:
            print(f"{r['ref']} {r.get('mpn')}: " + (f"{r['source']} {os.path.basename(r['path'])}" if r["ok"] else "NO FETCHABLE MODEL"))
            for t in r["tried"]:
                print(f"    {t['source']:16s} {t['outcome'][:150]}")
    if not all(r["ok"] for r in out):
        raise SystemExit(1)


def add_parsers(sub):
    p = sub.add_parser("parts", help="part-entry gate: MPN/mfr/datasheet/pinmap/package/footprint complete and consistent")
    p.add_argument("spec", help="spec JSON")
    _common(p)
    p.add_argument("--fix", action="store_true", help="write the unambiguous fixes into a COPY (--out); the rest become questions")
    p.add_argument("--out", default=None, help="with --fix: output spec")
    p.set_defaults(fn=cmd_parts)
    t = sub.add_parser("parts-todo", help="machine-readable parts work list for an agent: what each part lacks, evidence, distributor candidates")
    t.add_argument("spec", help="spec JSON")
    _common(t)
    t.add_argument("--out", default=None, help="write the JSON list here")
    t.add_argument("--only-missing", dest="only_missing", action="store_true", help="only parts with something missing")
    t.add_argument("--no-search", dest="no_search", action="store_true", help="no candidate keyword searches (lookups of the known MPNs only)")
    t.add_argument("--limit", type=int, default=5, help="candidates per group (default 5)")
    t.add_argument("--strict", action="store_true", help="exit 1 while any part is unresolved")
    t.set_defaults(fn=cmd_parts_todo)
    t.add_argument("--no-verify", dest="no_verify", action="store_true", help="do not fetch candidate datasheets (alternatives are never drop_in)")
    pf = sub.add_parser("parts-fetch", help="datasheets via source chain")
    pf.add_argument("mpns", nargs="*", help="MPNs (or use --spec: every part without a datasheet on disk)")
    pf.add_argument("--spec", default=None)
    pf.add_argument("--constraints", default=None)
    pf.add_argument("--mpn-map", dest="mpn_map", default=None)
    pf.add_argument("--datasheets", default=None)
    pf.add_argument("--refresh", action="store_true")
    pf.add_argument("--file", default=None, help="adopt a PDF you fetched by hand for the one MPN (same validation)")
    pf.add_argument("--verbose", action="store_true", help="also print what was tried for the ones that succeeded")
    pf.add_argument("--json", action="store_true")
    pf.set_defaults(fn=cmd_parts_fetch)
    pg = sub.add_parser("parts-page", help="datasheet page to PNG")
    pg.add_argument("mpn")
    pg.add_argument("--datasheets", required=True)
    pg.add_argument("--page", type=int, default=1)
    pg.add_argument("--png", default="page.png")
    pg.add_argument("--dpi", type=int, default=110)
    pg.add_argument("--pages", action="store_true", help="only print the page count")
    pg.set_defaults(fn=cmd_parts_page)
    pc = sub.add_parser("parts-cite", help="record a pin table read from a page")
    pc.add_argument("spec")
    pc.add_argument("--ref", required=True)
    pc.add_argument("--page", type=int, required=True)
    pc.add_argument("--read", required=True, help='JSON the agent wrote: {"pins": {"1": "NAME", ...}, "count": N, "mounting": true?}')
    pc.add_argument("--write-source", dest="write_source", action="store_true", help="set pinmap_source when it matches (parts with a pinmap)")
    _common(pc)
    pc.set_defaults(fn=cmd_parts_cite)
    pm = sub.add_parser("parts-model", help="3D model source chain")
    pm.add_argument("refs", nargs="+")
    pm.add_argument("--spec", required=True)
    pm.add_argument("--lib", required=True, help="the project's 3D model library directory")
    pm.add_argument("--sources", default=None, help="model_sources.json registry (read; written with --register)")
    pm.add_argument("--register", action="store_true", help="copy an accepted model into --lib and record it in --sources (the board is not touched)")
    pm.add_argument("--force", action="store_true", help="retry sources that failed before")
    pm.add_argument("--scratch", default=None, help="download/failure-cache directory (default <lib>/.fetch)")
    _common(pm)
    pm.set_defaults(fn=cmd_parts_model)
    ap = sub.add_parser("parts-apply", help="validate agent proposals (MPN in stock, package/pins agree, datasheet, model) and write what passes")
    ap.add_argument("proposals", help="proposals JSON: [{refs|ref, mpn, mfr?, package?, pinmap_source?, datasheet?, model?, needs_owner?, changes?}]")
    ap.add_argument("--spec", required=True, help="the spec JSON the records are written into")
    _common(ap)
    ap.add_argument("--dry-run", dest="dry_run", action="store_true", help="validate and report, write nothing")
    ap.add_argument("--no-fetch", dest="no_fetch", action="store_true", help="do not try to fetch a missing datasheet")
    ap.set_defaults(fn=cmd_parts_apply)
    pf = sub.add_parser("parts-fact", help="record datasheet facts (vin, i_max, contact_a, pin rules) for an MPN, with page")
    pf.add_argument("mpn")
    pf.add_argument("facts", nargs="+", help="key=value (JSON values), e.g. vin_min=4.5 supply_pins='[\"IN\"]'")
    pf.add_argument("--source", required=True, help="datasheet file and page: file.pdf#p7")
    pf.add_argument("--datasheets", default="docs/datasheets", help="datasheet folder holding datasheets.json")
    pf.add_argument("--force", action="store_true", help="accept a number that is not found as text on the cited page")
    pf.set_defaults(fn=cmd_parts_fact)
    import importlib
    for name in ("ready", "pingroups", "partdiff"):
        try:
            m = importlib.import_module(f"fluxplace.{name}")
        except ModuleNotFoundError as e:
            if e.name != f"fluxplace.{name}":
                raise
            continue
        m.add_parser(sub)
