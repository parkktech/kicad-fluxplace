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


def cmd_parts_todo(a):
    """The machine-readable work list for an agent: one record per part, what is missing, the evidence, the candidates."""
    from . import parts as P, partsgate as PG
    spec, cons, facts, mm, ds, qty, cache = _gate_inputs(a)
    sess = _session(a, cons, "parts-todo")
    res = P.evaluate(spec, cons, facts=facts, mpn_map=mm, ds_dir=ds, api=not a.no_api, cache_dir=cache, session=sess, qty=qty)
    g = PG.gate(spec, cons, facts, res, qty=qty)
    searcher = None
    if not a.no_api and not a.no_search:
        from . import partsearch as PS
        searcher = PS.keyword_search
    out = PG.todo(spec, cons, res, g, qty=qty, searcher=searcher, limit=a.limit)
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
        got = PD.fetch([mpn], ds, urls={mpn: prop["datasheet_url"]} if prop.get("datasheet_url") else None, log=lambda m: None)
        return bool(got.get(mpn))

    def model_ok(ref, comp, prop):
        ok, why = PG.model_state(ref, facts)
        if ok:
            return True, why
        m = prop.get("model")
        if m and os.path.isfile(m if os.path.isabs(m) else os.path.join(os.path.dirname(os.path.abspath(getattr(a, "board", None) or a.spec)), m)):
            return True, "model file given"
        return False, why + " (give `model`, or `fluxplace models --fetch` first)"

    rep = PG.apply(a.spec, mpn_map, props, cons, facts, ds, lambda m: PS.lookup(m, cache_dir=cache),
                   PS.stocker(qty, cache_dir=cache), model_ok=model_ok, ds_ok=ds_ok, dry_run=a.dry_run)
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
    ap = sub.add_parser("parts-apply", help="validate agent proposals (MPN in stock, package/pins agree, datasheet, model) and write what passes")
    ap.add_argument("proposals", help="proposals JSON: [{refs|ref, mpn, mfr?, package?, pinmap_source?, datasheet?, model?, needs_owner?, changes?}]")
    ap.add_argument("--spec", required=True, help="the spec JSON the records are written into")
    _common(ap)
    ap.add_argument("--dry-run", dest="dry_run", action="store_true", help="validate and report, write nothing")
    ap.add_argument("--no-fetch", dest="no_fetch", action="store_true", help="do not try to fetch a missing datasheet")
    ap.set_defaults(fn=cmd_parts_apply)
    import importlib
    for name in ("ready", "pingroups", "partdiff"):
        try:
            m = importlib.import_module(f"fluxplace.{name}")
        except ModuleNotFoundError as e:
            if e.name != f"fluxplace.{name}":
                raise
            continue
        m.add_parser(sub)
