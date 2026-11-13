"""Part substitution diff: put the old and the new part's pin tables side by side BEFORE the swap reaches a board.

A transformer from another vendor fits the same land pattern with a different pin order: DRC, ERC and the netlist all read 0 and
the link is dead on the bench.  The pin table is the only evidence.

    fluxplace part-diff SPEC_OLD SPEC_NEW                      two spec revisions: every ref whose MPN changed
    fluxplace part-diff --spec S --ref R --new-mpn MPN [--new-part FILE.json]

A pin's FUNCTION is its declared group role (`pin_groups` / magnetics `channels`: `<group>.<role>`, so another vendor's pin NAMES do
not matter), else its pinmap name.  Per pad: function differs while the net is unchanged -> FAIL PART_SUBSTITUTION_PINOUT (pad, old
function, new function, net).  A changed MPN whose `pinmap_source` is missing or identical to the old part's -> FAIL
PART_SUBSTITUTION_UNVERIFIED.  A new part's pin table is never guessed: with `--new-mpn` it comes from `--new-part FILE.json`
({mpn, pinmap, pin_groups|channels, pinmap_source}) or an `alternates` entry of the old part in the spec; if neither exists the
datasheet is asked for (question `part-diff-pinmap:<MPN>`, exit 3)."""
import json
import os
import re

from . import pingroups as PG
from . import questions as QS

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"


def _f(level, code, msg, refs=()):
    return {"level": level, "code": code, "msg": msg, "refs": sorted(set(refs))}


def _norm(s):
    return re.sub(r"[^A-Z0-9+\-]", "", (s or "").upper().replace("−", "-"))


def functions(comp):
    """{pad: (key, label)}: group role where declared, else the normalised pinmap name."""
    out = {}
    for name, pad in (comp.get("pinmap") or {}).items():
        out[str(pad)] = (f"name:{_norm(name)}", str(name))
    for g in PG.groups_of(comp, channels=True):
        for role, pad in (g.get("roles") or {}).items():
            nm = next((n for n, p in (comp.get("pinmap") or {}).items() if str(p) == str(pad)), None)
            out[str(pad)] = (f"role:{g.get('name')}.{role}", f"{nm + ' ' if nm else ''}{g.get('name')}.{role}")
    return out


def pad_nets(spec, ref):
    return {str(pin): net for net, conns in (spec.get("nets") or {}).items() for r, pin in conns if r == ref}


def mpn_of(comp, mm=None):
    from . import parts as P
    return comp.get("mpn") or (mm or {}).get(comp.get("ref")) or P.mpn_in_value(comp.get("value", ""))


def diff(ref, old, new, old_nets, new_nets, old_mpn, new_mpn):
    """findings for one substituted part."""
    out = []
    of, nf = functions(old), functions(new)
    role_mode = any(k.startswith("role:") for k, _ in of.values()) and any(k.startswith("role:") for k, _ in nf.values())
    rows = []
    for pad in sorted(set(of) | set(nf), key=lambda p: (len(p), p)):
        o, n = of.get(pad), nf.get(pad)
        net_o, net_n = old_nets.get(pad), new_nets.get(pad, old_nets.get(pad) if not new_nets else None)
        rows.append((pad, o[1] if o else "-", n[1] if n else "-", net_o, net_n))
        if o and n and o[0] != n[0] and (not role_mode or (o[0].startswith("role:") and n[0].startswith("role:"))):
            if net_o and net_o == net_n:
                out.append(_f(FAIL, "PART_SUBSTITUTION_PINOUT",
                              f"{ref} {old_mpn} -> {new_mpn}: pad {pad} was {o[1]}, is {n[1]}, net {net_o} unchanged"
                              + ("" if role_mode else " (names only: declare pin_groups to compare by role)"), refs=[ref]))
    src_o, src_n = old.get("pinmap_source"), new.get("pinmap_source")
    if not src_n or src_n == src_o:
        out.append(_f(FAIL, "PART_SUBSTITUTION_UNVERIFIED",
                      f"{ref}: MPN changed {old_mpn} -> {new_mpn} but pinmap_source is "
                      + ("missing" if not src_n else f"still the old part's ({src_n})") + ": the new pin table has not been read from its datasheet",
                      refs=[ref]))
    return out, rows


def table(ref, rows, bad):
    lines = [f"  {ref}  pad   old function                new function                net"]
    for pad, o, n, no, nn in rows:
        mark = "*" if pad in bad else " "
        lines.append(f" {mark}{ref:5s} {pad:4s}  {o[:26]:26s}  {n[:26]:26s}  {no or '-'}")
    return lines


def compare_specs(old, new, mm_old=None, mm_new=None):
    out, tables = [], {}
    by_old = {c["ref"]: c for c in old.get("components", []) if isinstance(c, dict)}
    for c in new.get("components", []):
        o = by_old.get(c.get("ref"))
        if not o or c.get("dnp") or o.get("dnp"):
            continue
        mo, mn = mpn_of(o, mm_old), mpn_of(c, mm_new)
        if not mo or not mn or re.sub(r"[^A-Z0-9]", "", mo.upper()) == re.sub(r"[^A-Z0-9]", "", mn.upper()):
            continue
        f, rows = diff(c["ref"], o, c, pad_nets(old, c["ref"]), pad_nets(new, c["ref"]), mo, mn)
        out += f
        tables[c["ref"]] = (rows, {x["msg"].split("pad ")[1].split(" ")[0] for x in f if x["code"] == "PART_SUBSTITUTION_PINOUT"})
    return out, tables


def cmd_part_diff(a):
    from . import constraints as C, parts as P
    top = getattr(a, "top", 10) or 10
    cons = C.load(a.constraints)
    sess = QS.Session(a.spec or (a.specs[0] if a.specs else "part-diff"), cons, QS.parse_answers(a.answer), a.assume_defaults, "part-diff")
    if a.specs and len(a.specs) == 2:
        old, new = (json.load(open(p)) for p in a.specs)
        findings, tables = compare_specs(old, new, _mm(a.mpn_map_old), _mm(a.mpn_map_new))
        label = f"{os.path.basename(a.specs[0])} -> {os.path.basename(a.specs[1])}"
    elif a.spec and a.ref and a.new_mpn:
        spec = json.load(open(a.spec))
        o = next((c for c in spec.get("components", []) if c.get("ref") == a.ref), None)
        if not o:
            raise SystemExit(f"part-diff: {a.ref} is not in {a.spec}")
        newp = None
        if a.new_part:
            newp = json.load(open(a.new_part))
        else:
            newp = next((x for x in o.get("alternates") or [] if _norm(x.get("mpn")) == _norm(a.new_mpn)), None)
        findings, tables = [], {}
        label = f"{a.ref} {mpn_of(o, _mm(a.mpn_map)) or o.get('value')} -> {a.new_mpn}"
        if not newp or not (newp.get("pinmap") or newp.get("pin_groups") or newp.get("channels")):
            ds = P.resolve_dir(((cons.get("docs") or {}).get("datasheets")), [a.spec])
            f = P.datasheet_file(a.new_mpn, ds)
            sess.ask(f"part-diff-pinmap:{a.new_mpn}", "substitution",
                     f"{a.new_mpn} has no pin table in the spec (`alternates`) or --new-part; datasheet "
                     + (f"is on disk ({f}) but is not machine-read" if f else f"is NOT on disk (looked in {ds or 'no datasheet dir'})"),
                     {"declared": "the pin table (pinmap + pin_groups/channels + pinmap_source) is now in --new-part / `alternates`; re-run",
                      "abort": "do not substitute"}, "abort",
                     "the new part's datasheet pin table, entered from the datasheet page")
            findings.append(_f(FAIL, "PART_SUBSTITUTION_UNVERIFIED", f"{a.ref}: {a.new_mpn}: no pin table to compare", refs=[a.ref]))
        else:
            newc = dict(o, **{k: v for k, v in newp.items() if k in ("pinmap", "pin_groups", "channels", "pinmap_source")})
            if "pinmap" not in newp:
                newc.pop("pinmap", None)
            if not newp.get("pin_groups") and not newp.get("channels"):
                newc.pop("pin_groups", None)
                newc.pop("channels", None)
            nets = pad_nets(spec, a.ref)
            if a.board and os.path.exists(a.board):
                from . import review as R
                nets = {k: v for k, v in (R.facts_from_board(a.board, backend="file")["parts"].get(a.ref, {}).get("pads") or {}).items() if v} or nets
            findings, rows = diff(a.ref, o, newc, nets, nets, mpn_of(o, _mm(a.mpn_map)) or o.get("value"), a.new_mpn)
            tables[a.ref] = (rows, {x["msg"].split("pad ")[1].split(" ")[0] for x in findings if x["code"] == "PART_SUBSTITUTION_PINOUT"})
    else:
        raise SystemExit("part-diff: give SPEC_OLD SPEC_NEW, or --spec S --ref R --new-mpn MPN")
    n = sum(f["level"] == FAIL for f in findings)
    head = f"part-diff: {'FAIL' if n else 'ok'} - {n} FAIL over {len(tables) or 0} substituted part(s) ({label})"
    lines = [head]
    for ref, (rows, bad) in sorted(tables.items()):
        lines += table(ref, rows, bad)
    lines += [f"  {x['level']:4s} {x['code']} {x['msg']}"[:230] for x in findings]
    stem = os.path.splitext(a.spec or a.specs[-1])[0]
    try:
        with open(stem + ".part-diff.txt", "w") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass
    if a.json:
        print(json.dumps({"findings": findings, "tables": {r: v[0] for r, v in tables.items()}}, indent=1))
    elif getattr(a, "quiet", False):
        print(head)
    else:
        findings_only = [x for x in findings]
        shown = findings_only if getattr(a, "full_output", False) else P._spread(findings_only, top)
        print(head)
        if getattr(a, "full_output", False):
            for ref, (rows, bad) in sorted(tables.items()):
                print("\n".join(table(ref, rows, bad)))
        for x in shown:
            print(f"  {x['level']:4s} {x['code']} {x['msg']}"[:230])
        if len(findings_only) > len(shown):
            print(f"  ... {len(findings_only) - len(shown)} more; side-by-side table: {stem}.part-diff.txt")
        elif tables:
            print(f"  side-by-side pin table: {stem}.part-diff.txt")
    q = sess.gate(want_json=False)
    if q:
        raise SystemExit(q)
    if n:
        raise SystemExit(1)


def _mm(path):
    if not path or not os.path.exists(path):
        return None
    return {k: v for k, v in json.load(open(path)).items() if not k.startswith("_") and isinstance(v, str)}


def add_parser(sub):
    from . import partscli as PC
    p = sub.add_parser("part-diff", help="substitution diff: old vs new part pin tables, FAIL where a function moved but the net did not")
    p.add_argument("specs", nargs="*", help="SPEC_OLD SPEC_NEW (two revisions), or use --spec/--ref/--new-mpn")
    p.add_argument("--spec", default=None)
    p.add_argument("--ref", default=None)
    p.add_argument("--new-mpn", dest="new_mpn", default=None)
    p.add_argument("--new-part", dest="new_part", default=None, help="JSON {mpn, pinmap, pin_groups|channels, pinmap_source} of the new part")
    p.add_argument("--board", default=None, help="take the nets from this board instead of the spec")
    p.add_argument("--constraints", default=None)
    p.add_argument("--mpn-map", dest="mpn_map", default=None)
    p.add_argument("--mpn-map-old", dest="mpn_map_old", default=None)
    p.add_argument("--mpn-map-new", dest="mpn_map_new", default=None)
    p.add_argument("--json", action="store_true")
    QS.add_args(p, json_flag=False)
    PC._flags(p)
    p.set_defaults(fn=cmd_part_diff)
