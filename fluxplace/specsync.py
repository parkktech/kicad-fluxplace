"""spec-sync: make the derived netlist spec follow the schematic and the board.

Update flows (re-net, new parts, footprint swaps) change only the schematic and the board; the project's spec.json then
goes stale and review reports false SPEC_* / MPN_MISSING.  Truth: the schematic netlist (nets per ref/pad) and the board
footprints (MPN, Manufacturer, value, footprint, DNP).  The spec is a derived record: this module diffs it against that
truth and rewrites only the derived fields.  Spec-only content (pinmap, pinmap_source, landpattern, notes, roles ...)
is never touched.  A net the schematic and the board disagree on is a real defect (PAD_NET_MISMATCH): reported, never applied.

Board-agnostic: nothing here names a ref, net or part.
"""
import copy
import json
import os
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

from . import boardfile as BF

__all__ = ["load_sch_netlist", "board_truth", "diff", "apply", "sync", "format_diff"]

_AUTO = ("unconnected-", "Net-(")


def _clean(net):
    """Board and schematic spell a net alike but for the sheet-root slash; an unconnected pad is no net."""
    n = (net or "").lstrip("/") if (net or "").startswith("/") and net.count("/") == 1 else (net or "")
    return "" if n.startswith("unconnected-") else n


def load_sch_netlist(path):
    """{'parts': {ref: {value, footprint, mpn, mfr}}, 'pads': {(ref, pad): net}} from a .kicad_sch (via kicad-cli) or a
    KiCad XML netlist file."""
    xml = path
    tmp = None
    if path.endswith(".kicad_sch"):
        tmp = tempfile.mkdtemp(prefix="specsync-")
        xml = os.path.join(tmp, "n.xml")
        r = subprocess.run(["nice", "-n", "10", "kicad-cli", "sch", "export", "netlist", "--format", "kicadxml",
                            "-o", xml, path], capture_output=True, text=True)
        if not os.path.exists(xml):
            raise RuntimeError(f"kicad-cli netlist export failed: {r.stderr.strip()[-300:]}")
    try:
        root = ET.parse(xml).getroot()
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    parts, pads = {}, {}
    for c in root.iter("comp"):
        f = {x.get("name"): (x.text or "") for x in c.iter("field")}
        parts[c.get("ref")] = {"value": (c.findtext("value") or ""), "footprint": (c.findtext("footprint") or ""),
                               "mpn": f.get("MPN", ""), "mfr": f.get("Manufacturer", "")}
    for n in root.iter("net"):
        name = _clean(n.get("name"))
        for nd in n.iter("node"):
            pads[(nd.get("ref"), nd.get("pin"))] = name
    return {"parts": parts, "pads": pads}


def board_truth(board_path):
    """{'parts': {ref: {value, fp, lib, mpn, mfr, dnp}}, 'pads': {(ref, pad): net}} from the board file (no pcbnew)."""
    root = BF.parse(open(board_path).read())
    parts, pads = {}, {}
    for fp in root:
        if not (isinstance(fp, list) and fp and fp[0] == "footprint"):
            continue
        props = {p[1]: p[2] for p in fp if isinstance(p, list) and len(p) > 2 and p[0] == "property"}
        ref = props.get("Reference")
        if not ref:
            continue
        lib, _, name = fp[1].rpartition(":")
        attr = next((a for a in fp if isinstance(a, list) and a and a[0] == "attr"), [])
        parts[ref] = {"value": props.get("Value", ""), "fp": name, "lib": lib, "mpn": props.get("MPN", ""),
                      "mfr": props.get("Manufacturer", ""), "dnp": "dnp" in attr}
        for p in fp:
            if isinstance(p, list) and p and p[0] == "pad" and len(p) > 1:
                n = _clean(BF._net(p))
                if n or (ref, p[1]) not in pads:            # a pad number may repeat (one electrical pad, several lands)
                    pads[(ref, p[1])] = n
    return {"parts": parts, "pads": pads}


def _nets_dict(spec):
    n = spec.get("nets") or {}
    return n if isinstance(n, dict) else {x.get("name"): x.get("pins", []) for x in n}


def _pads_of(comp, pin, have=None):
    """Pad numbers a pin token names: through the component's pinmap when it has one, else the token itself (same rule as
    review._spec_pads); `have` (the footprint's pads) filters when given."""
    v = ((comp or {}).get("pinmap") or {}).get(str(pin), pin)
    cand = [str(x) for x in (v if isinstance(v, (list, tuple)) else [v])]
    return [c for c in cand if have is None or c in have]


def _pin_token(comp, pad):
    """The spec spells a pinmapped part's pins by name; keep that convention."""
    for k, v in ((comp or {}).get("pinmap") or {}).items():
        if str(pad) in [str(x) for x in (v if isinstance(v, (list, tuple)) else [v])]:
            return k
    return str(pad)


def plan_pinmap(comp, mpn, have, ds_dir):
    """(pinmap, pinmap_source, None) rebuilt from the part's dossier pin table for the footprint pads `have`, or
    (None, None, why).  Names the old pinmap already uses are kept where the datasheet spells the pad alike (so the schematic's
    pin names and the spec nets stay valid); a name on several pads maps to the list of them.  Pads with no number in the table
    (PAD, SH ...) are accepted only when the old pinmap already names them."""
    import collections
    from . import dossier as DO, partdocs as PD
    if not mpn or not ds_dir:
        return None, None, "no MPN" if not mpn else "no datasheets dir"
    ent = PD.load_manifest(ds_dir).get(mpn) or {}
    fn = ent.get("file") or PD.ds_file_for_mpn(ds_dir, mpn)
    f = os.path.join(ds_dir, fn) if fn else None
    if not f or not os.path.exists(f):
        return None, None, f"no datasheet on disk for {mpn}"
    dos = DO.load(mpn, ent.get("sha256") or PD._sha(f))
    if not dos:
        return None, None, f"no dossier for {mpn}"
    tab = collections.defaultdict(list)                  # pad -> [(printed names, page)]
    for p in dos.get("pins") or []:
        names = [x for x in [p.get("name")] + [a for a in p.get("alt") or [] if isinstance(a, str)] if x]
        for n in p.get("num") or []:
            tab[str(n)].append((names, p.get("p")))
    old = comp.get("pinmap") or {}
    old_pads = {str(x) for v in old.values() for x in (v if isinstance(v, (list, tuple)) else [v])}
    miss = sorted(pd for pd in have if pd not in tab and (pd.isdigit() or pd not in old_pads))
    if miss or not tab:
        return None, None, f"dossier pin table of {mpn} has no pad {', '.join(miss[:6])}" if tab else f"dossier of {mpn} has no pin table"
    by = collections.OrderedDict()
    for pd in sorted((p for p in have if p in tab), key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else 0, x)):
        names = [n for ns, _ in tab[pd] for n in ns]
        nm = next((k for k in old if PD._aliases(k) & {PD._norm(n) for n in names} | {k.upper()} & {n.upper() for n in names}), None)
        by.setdefault(nm or names[0], []).append(pd)
    for k, v in old.items():                             # keep the old entries for pads the table does not number
        pads = [str(x) for x in (v if isinstance(v, (list, tuple)) else [v])]
        if k not in by and all(pd in have and pd not in tab for pd in pads):
            by[k] = pads
    pm = {k: (v[0] if len(v) == 1 else v) for k, v in by.items()}
    pages = sorted(int(x) for ns in tab.values() for _, x in ns if isinstance(x, int))
    return pm, f"{fn}#p{pages[0] if pages else 1}", None


def diff(spec, sch, brd, mpn_map=None, ds_dir=None):
    """{'errors': [...], 'parts': {added, removed, changed}, 'nets': {...}, 'warnings': [...]}; pure, no writes."""
    errs, warns = [], []
    comps = {c["ref"]: c for c in spec.get("components", [])}
    bp, sp = brd["parts"], sch["parts"]
    mpn_map = mpn_map or {}
    have = {}
    for (r, pd) in brd["pads"]:
        have.setdefault(r, set()).add(pd)
    comps_eff = dict(comps)                                # the spec as the swaps will leave it (refreshed pinmaps)
    refresh, fails = {}, []
    for ref in sorted(set(comps) & set(bp) & set(sp)):
        c, b = comps[ref], bp[ref]
        old_mpn = c.get("mpn") or mpn_map.get(ref)
        swapped = (c.get("fp") and b["fp"] and c["fp"] != b["fp"]) or (old_mpn and b["mpn"] and old_mpn != b["mpn"])
        if swapped and c.get("pinmap"):
            pm, src, why = plan_pinmap(c, b["mpn"] or old_mpn, have.get(ref, set()), ds_dir)
            if pm is None:
                fails.append(("SWAP_PINMAP_REVIEW", f"{ref}: footprint/MPN changed and the pinmap could not be rebuilt: {why}"))
            else:
                refresh[ref] = {"pinmap": (c["pinmap"], pm), "pinmap_source": (c.get("pinmap_source"), src)}
                comps_eff[ref] = dict(c, pinmap=pm)
    spads, unres = {}, []                           # schematic pins in board-pad space (a symbol may name pins, not number them)
    for (r, pin), n in sch["pads"].items():
        got = _pads_of(comps_eff.get(r), pin, have.get(r)) if r in have else []
        if r in have and not got:
            unres.append(f"{r}.{pin}")
        for pd in got:
            spads[(r, pd)] = n
    if unres:
        warns.append(("SCH_PIN_UNRESOLVED", f"{len(unres)} schematic pin(s) match no board pad: {', '.join(sorted(unres)[:10])}"))
    sch = dict(sch, pads=spads)
    for ref in sorted(set(bp) ^ set(sp)):
        errs.append(("PART_BOARD_SCH_MISMATCH", f"{ref}: on the {'board' if ref in bp else 'schematic'} only"))
    for pad in sorted(set(sch["pads"]) | set(brd["pads"])):
        a, b = sch["pads"].get(pad), brd["pads"].get(pad)
        if pad[0] in bp and pad[0] in sp and a is not None and b is not None and a != b:
            errs.append(("PAD_NET_MISMATCH", f"{pad[0]}.{pad[1]}: schematic '{a or 'NO NET'}' vs board '{b or 'NO NET'}'"))
    both = set(bp) & set(sp)
    added, changed = {}, {}
    for ref in sorted(both):
        b = bp[ref]
        want = {"value": b["value"], "fp": b["fp"], "mpn": b["mpn"] or sp[ref]["mpn"], "mfr": b["mfr"] or sp[ref]["mfr"],
                "dnp": b["dnp"]}
        slib = sp[ref]["footprint"].rpartition(":")
        want["lib"] = slib[0] if slib[2] == b["fp"] and slib[0] else (b["lib"] or None)
        c = comps.get(ref)
        if c is None:
            if any(n for (r, _), n in brd["pads"].items() if r == ref and n):     # parts with no net (holes, logos) stay out
                added[ref] = {k: v for k, v in want.items() if v or k == "dnp"}
            continue
        ch = {}
        for k in ("value", "fp", "lib", "mpn", "mfr"):
            have_v = (c.get(k) or mpn_map.get(ref)) if k == "mpn" else c.get(k)
            if want[k] and have_v != want[k]:
                ch[k] = (have_v, want[k])
        if bool(c.get("dnp")) != want["dnp"]:
            ch["dnp"] = (bool(c.get("dnp")), want["dnp"])
        if ch:
            changed[ref] = ch
            if ref in refresh:
                ch.update(refresh[ref])
    removed = sorted(r for r in comps if r not in bp and r not in sp)
    # nets: truth = schematic pads (board agrees, or the run is refused)
    tr = {}
    for (ref, pad), n in sch["pads"].items():
        if n and ref in bp:
            tr.setdefault(n, []).append((ref, pad))
    old = _nets_dict(spec)
    cur = {}
    for n, pins in old.items():
        for ref, pin in pins:
            for pd in _pads_of(comps.get(ref), pin, have.get(ref)) or _pads_of(comps_eff.get(ref), pin, have.get(ref)):
                cur.setdefault(n, set()).add((ref, pd))
    nets_add, nets_del, nets_chg = {}, [], {}
    for n, pads in sorted(tr.items()):
        if n not in cur:
            if len(pads) > 1 or not n.startswith(_AUTO):
                nets_add[n] = sorted(pads)
            continue
        plus, minus = sorted(set(pads) - cur[n]), sorted(cur[n] - set(pads))
        if plus or minus:
            nets_chg[n] = {"add": plus, "drop": minus}
    nets_del = sorted(n for n in cur if n not in tr)
    return {"errors": errs, "warnings": warns, "fails": fails, "parts": {"added": added, "removed": removed, "changed": changed},
            "nets": {"added": nets_add, "removed": nets_del, "changed": nets_chg}}


def format_diff(d):
    out = []
    for code, m in d["errors"]:
        out.append(f"ERROR {code}: {m}")
    for code, m in d["warnings"]:
        out.append(f"WARN  {code}: {m}")
    for code, m in d.get("fails", ()):
        out.append(f"FAIL  {code}: {m}")
    P, N = d["parts"], d["nets"]
    for ref, v in P["added"].items():
        out.append(f"+part {ref} " + " ".join(f"{k}={v[k]}" for k in ("value", "fp", "mpn", "mfr") if v.get(k)))
    for ref in P["removed"]:
        out.append(f"-part {ref}")
    for ref, ch in P["changed"].items():
        out.append(f"~part {ref} " + "; ".join(f"{k}: {a!r} -> {b!r}" for k, (a, b) in ch.items()))
    for n, pads in N["added"].items():
        out.append(f"+net {n} ({len(pads)} pins)")
    for n in N["removed"]:
        out.append(f"-net {n}")
    for n, ch in N["changed"].items():
        out.append(f"~net {n}: " + " ".join([f"+{r}.{p}" for r, p in ch["add"]] + [f"-{r}.{p}" for r, p in ch["drop"]]))
    return out


def apply(spec, d):
    """A copy of `spec` with the diff applied; only derived fields move."""
    s = copy.deepcopy(spec)
    comps = s.setdefault("components", [])
    byref = {c["ref"]: c for c in comps}
    P, N = d["parts"], d["nets"]
    for ref in P["removed"]:
        comps[:] = [c for c in comps if c["ref"] != ref]
    for ref, ch in P["changed"].items():
        for k, (_, new) in ch.items():
            if k in ("pinmap", "pinmap_source"):
                if new:
                    byref[ref][k] = new
            elif k == "dnp":
                if new:
                    byref[ref]["dnp"] = True
                else:
                    byref[ref].pop("dnp", None)
            else:
                byref[ref][k] = new
    for ref, v in P["added"].items():
        c = {"ref": ref, "value": v.get("value", ""), "lib": v.get("lib") or "", "fp": v.get("fp", "")}
        for k in ("mpn", "mfr"):
            if v.get(k):
                c[k] = v[k]
        if v.get("dnp"):
            c["dnp"] = True
        comps.append(c)
        byref[ref] = c
    nets = _nets_dict(s)
    oldc = {c["ref"]: c for c in spec.get("components", [])}
    for ref, ch in P["changed"].items():                   # a refreshed pinmap renames pins: re-spell this part's net entries
        if "pinmap" in ch:
            for n, pins in nets.items():
                seen, out = set(), []
                for r, p in pins:
                    toks = [_pin_token(byref[r], pd) for pd in _pads_of(oldc[r], p)] if r == ref else [p]
                    for t in toks:
                        if (r, t) not in seen:
                            seen.add((r, t))
                            out.append([r, t])
                nets[n] = out
    gone = set(P["removed"])
    for n in list(nets):
        if n in N["removed"]:
            del nets[n]
    for n, ch in N["changed"].items():
        drop = set(map(tuple, ch["drop"]))
        keep = [[r, p] for r, p in nets[n] if r not in gone and not any((r, pd) in drop for pd in _pads_of(byref.get(r), p))]
        seen = {(r, p) for r, p in keep}
        for r, pd in ch["add"]:
            tok = _pin_token(byref.get(r), pd)
            if (r, tok) not in seen:
                seen.add((r, tok))
                keep.append([r, tok])
        nets[n] = keep
    for n, pads in N["added"].items():
        toks = []
        for r, pd in pads:
            t = [r, _pin_token(byref.get(r), pd)]
            if t not in toks:
                toks.append(t)
        nets[n] = toks
    for n in list(nets):
        nets[n] = [[r, p] for r, p in nets[n] if r not in gone]
    if isinstance(spec.get("nets"), list):
        s["nets"] = [{"name": n, "pins": p} for n, p in nets.items()]
    else:
        s["nets"] = nets
    return s


def sync(board, sch, spec_path, write=False, log=print, ds_dir=None):
    """Diff (and with write=True, apply in place with a backup + change log).  Returns (diff, exit_code)."""
    spec = json.load(open(spec_path))
    from . import sourcing as SRC
    mp_path = SRC.find_map(board)
    mp = {}
    if mp_path and os.path.exists(mp_path):
        mp = {k: v for k, v in json.load(open(mp_path)).items() if not k.startswith("_") and isinstance(v, str)}
    d = diff(spec, load_sch_netlist(sch), board_truth(board), mp, ds_dir)
    lines = format_diff(d)
    for ln in lines:
        log(ln)
    n = sum(len(v) for v in d["parts"].values()) + sum(len(v) for v in d["nets"].values())
    log(f"spec-sync: {len(d['errors'])} error(s), {len(d['warnings'])} warning(s), {len(d['fails'])} FAIL(s), {n} change group(s)")
    if d["errors"]:
        return d, 2
    rc = 1 if d["fails"] else 0
    if write and n:
        bak = f"{spec_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(spec_path, bak)
        out = apply(spec, d)
        with open(spec_path, "w") as fh:
            json.dump(out, fh, indent=1)
        with open(os.path.join(os.path.dirname(os.path.abspath(spec_path)), "spec-sync.log"), "a") as fh:
            for ln in lines:
                fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {os.path.basename(spec_path)} {ln}\n")
        log(f"spec-sync: wrote {spec_path} (backup {bak})")
    return d, rc
