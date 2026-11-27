"""Pin-swap groups: a sealed pad of a part may hand its net to an interchangeable FREE pad of the same part that escapes.

    [pinswap.<name>]
    pins = ["REF.12", "REF.13", "REF.20"]     # interchangeable pads of ONE part (same function class, e.g. GPIOs)
    functions = {"REF.12" = "GPIO5"}          # optional; else the spec pinmap name, else the pinmap_source datasheet page lines

A group pad whose net the escape planner (escapeplan.plan_escapes) reports PLAN_PIN_SEALED becomes the question
`pinswap:REF.PAD` with options "keep" (default: today's behaviour) and every free group pad (no net on the board, not named
in the spec) whose escape the planner finds when the net is moved there.  The question goes through questions.Session, so
an answer, the AI decider ([ai] decide) or the person settles it; `apply_board` re-nets the pads of the decided moves, and
the route entry's re-net strip (renet.py) handles any copper the old pad carried.  No ref, net or constant of any board."""
import os
import re

from . import escapeplan as EP

KEEP = "keep"
MAX_CANDS = 8


def groups(cons):
    """-> [(name, ref, [pads], {pad: function})]; a group whose pins span several parts or are malformed is skipped."""
    out = []
    for name, g in sorted(((cons or {}).get("pinswap") or {}).items()):
        pins = [str(p) for p in (g or {}).get("pins") or [] if "." in str(p)]
        refs = {p.rsplit(".", 1)[0] for p in pins}
        if len(refs) != 1 or len(pins) < 2:
            continue
        ref = refs.pop()
        fn = {str(k).rsplit(".", 1)[-1]: str(v) for k, v in ((g or {}).get("functions") or {}).items()}
        out.append((name, ref, [p.rsplit(".", 1)[1] for p in pins], fn))
    return out


def _spec_part(spec, ref):
    return next((c for c in (spec or {}).get("components") or [] if c.get("ref") == ref), {})


def _spec_pads(spec, ref):
    """pads of `ref` the spec nets name (a pinmap name is mapped to its pad)."""
    pm = _spec_part(spec, ref).get("pinmap") or {}
    return {str(pm.get(pin, pin)) for pins in ((spec or {}).get("nets") or {}).values() for r, pin in pins if r == ref}


def _functions(spec, cons, ref, pads, fn):
    """{pad: function text} from the group, the spec pinmap, else datasheet page lines naming the pad number."""
    part = _spec_part(spec, ref)
    inv = {str(v): k for k, v in (part.get("pinmap") or {}).items()}
    out = {p: fn.get(p) or inv.get(p) for p in pads}
    need = [p for p in pads if not out[p]]
    if need:
        from . import partdocs as PD
        f, pages = PD.parse_source(part.get("pinmap_source"))
        ds = ((cons or {}).get("docs") or {}).get("datasheets") or ""
        pdf = next((c for c in (f, os.path.join(ds, f or ""), os.path.join(ds, os.path.basename(f or ""))) if f and os.path.isfile(c)), None)
        text = (PD.page_text(pdf, pages or None) or "") if pdf else ""
        for p in need:
            rx = re.compile(r"(?<![\w.])" + re.escape(p) + r"(?![\w.])")
            hits = [" ".join(l.split()) for l in text.splitlines() if rx.search(l)][:2]
            out[p] = " | ".join(hits)[:200] if hits else None
    return out


def probe(B, cons, spec=None):
    """-> [{group, ref, pad, net, msg, cands [{pad, kind}], functions {pad: text}, cite}] for each sealed group pad."""
    rows = []
    kw = {"ignore_routing": not B.get("segs")}          # a routed board: shipped copper of other nets blocks escapes
    for name, ref, pads, fn in groups(cons):
        part = (B.get("parts") or {}).get(ref)
        if not part:
            continue
        bypad = {q["num"]: q for q in part["pads"]}
        plan = EP.plan_escapes(B, cons, only_refs={ref}, **kw)
        sealed = [f for f in plan.findings if f["code"] == "PLAN_PIN_SEALED" and f["part"] == ref and f["pin"] in pads]
        if not sealed:
            continue
        named = _spec_pads(spec, ref)
        free = [p for p in pads if p in bypad and not bypad[p]["net"] and p not in named]
        for f in sealed:
            s = bypad[f["pin"]]
            net, cands = s["net"], []
            for p in sorted(free, key=lambda p: (bypad[p]["x"] - s["x"]) ** 2 + (bypad[p]["y"] - s["y"]) ** 2)[:MAX_CANDS]:
                c = bypad[p]
                s["net"], c["net"] = "", net
                try:
                    rec = EP.plan_escapes(B, cons, only_refs={ref}, **kw).pins.get((ref, p)) or {}
                finally:
                    s["net"], c["net"] = net, ""
                if rec.get("kind") not in (None, "sealed", "no_site"):
                    cands.append({"pad": p, "kind": rec["kind"]})
            rows.append({"group": name, "ref": ref, "pad": f["pin"], "net": net, "msg": f["msg"], "cands": cands,
                         "functions": _functions(spec, cons, ref, [f["pin"]] + [c["pad"] for c in cands], fn),
                         "cite": _spec_part(spec, ref).get("pinmap_source")})
    return rows


def _applier(ref, old):
    def f(box, choice):
        if choice == KEEP or box["spec"] is None:
            return
        new = choice.rsplit(".", 1)[1]
        pm = _spec_part(box["spec"], ref).get("pinmap") or {}
        name = next((k for k, v in pm.items() if str(v) == new), new)        # the spec names a pinmapped pad by its pin name
        for pins in (box["spec"].get("nets") or {}).values():
            for pr in pins:
                if pr[0] == ref and str(pm.get(pr[1], pr[1])) == old:
                    pr[1] = name
    return f


def change_list(spec, mv):
    """The schematic edits a set of moves needs, so the schematic follows the board: one row per move
    {ref, net, from_pad, to_pad, from_pin, to_pin} (pin = the spec pinmap name, else the pad number)."""
    rows = []
    for ref, old, new in mv:
        inv = {str(v): k for k, v in (_spec_part(spec, ref).get("pinmap") or {}).items()}
        tok = {old, inv.get(old, old)}
        net = next((n for n, pins in ((spec or {}).get("nets") or {}).items() if any(r == ref and str(p) in tok for r, p in pins)), None)
        rows.append({"ref": ref, "net": net, "from_pad": old, "to_pad": new, "from_pin": inv.get(old, old), "to_pin": inv.get(new, new)})
    return rows


def ask(S, B, cons, spec=None):
    """one question per sealed group pad that has an escapable alternative; the decider evidence is the pin functions."""
    taken = set()                                        # a pad decided for one sealed net is not offered to the next
    for r in probe(B, cons, spec):
        r["cands"] = [c for c in r["cands"] if c["pad"] not in taken]
        if not r["cands"]:
            S.inferred(f"pinswap {r['ref']}.{r['pad']} stays", f"no free pad of group {r['group']} escapes")
            continue
        fx = r["functions"]
        opts = {KEEP: f"{r['ref']}.{r['pad']} keeps {r['net']}: sealed, the net stays open"}
        for c in r["cands"]:
            opts[f"{r['ref']}.{c['pad']}"] = f"move {r['net']} to {r['ref']}.{c['pad']} ({fx.get(c['pad']) or 'function unknown'}); escape {c['kind']}"
        qid = f"pinswap:{r['ref']}.{r['pad']}"
        S.evidence[qid] = ([f"sealed: {r['msg']}", f"{r['ref']}.{r['pad']} function: {fx.get(r['pad']) or 'unknown'}"]
                           + [f"{r['ref']}.{c['pad']} function: {fx.get(c['pad']) or 'unknown'}" for c in r["cands"]]
                           + ([f"pin table: {r['cite']}"] if r["cite"] else []))
        ch = S.ask(qid, "pin choice", f"{r['ref']}.{r['pad']} ({r['net']}) is sealed; {len(r['cands'])} free pad(s) of group {r['group']} escape",
              opts, KEEP, "the datasheet function of each pad: the net's signal must be available on the chosen pad", apply=_applier(r["ref"], r["pad"]))
        if ch != KEEP:
            taken.add(ch.rsplit(".", 1)[1])


def moves(answers):
    """[(ref, old pad, new pad)] from answers {'pinswap:REF.PAD': 'REF.NEW' | 'keep'}."""
    out = []
    for k, v in sorted((answers or {}).items()):
        if str(k).startswith("pinswap:") and v != KEEP and "." in str(v):
            ref, old = k[len("pinswap:"):].rsplit(".", 1)
            r2, new = str(v).rsplit(".", 1)
            if r2 == ref and new != old:
                out.append((ref, old, new))
    return out


def apply_board(board, out, mv):
    """Write `out` = `board` with each move's net taken off the old pad and put on the new one (pcbnew).  -> [nets moved];
    nothing written when no move applies (old pad lost its net, or the new pad already carries one)."""
    import pcbnew
    b = pcbnew.LoadBoard(board)
    done = []
    for ref, old, new in mv:
        fp = b.FindFootprintByReference(ref)
        po, pn = (fp.FindPadByNumber(old), fp.FindPadByNumber(new)) if fp else (None, None)
        if not (po and pn) or not po.GetNetname() or pn.GetNetname():
            continue
        pn.SetNet(po.GetNet())
        po.SetNetCode(0)
        done.append(pn.GetNetname())
    if done:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        pcbnew.SaveBoard(out, b)
    return done
