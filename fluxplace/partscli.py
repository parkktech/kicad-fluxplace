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
    facts = R.facts_from_board(board, mpn_map=getattr(a, "mpn_map", None)) if board else None
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


def add_parsers(sub):
    p = sub.add_parser("parts", help="part-entry gate: MPN/mfr/datasheet/pinmap/package/footprint complete and consistent")
    p.add_argument("spec", help="spec JSON")
    _common(p)
    p.add_argument("--fix", action="store_true", help="write the unambiguous fixes into a COPY (--out); the rest become questions")
    p.add_argument("--out", default=None, help="with --fix: output spec")
    p.set_defaults(fn=cmd_parts)
    import importlib
    for name in ("readycmd", "pingroups", "partdiff"):
        try:
            m = importlib.import_module(f"fluxplace.{name}")
        except ModuleNotFoundError as e:
            if e.name != f"fluxplace.{name}":
                raise
            continue
        m.add_parser(sub)
