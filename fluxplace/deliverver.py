"""A delivery for a NEW VERSION of an already-shipped design.

`deliver` on its own writes a fresh folder; a revision's buyer, though, already
holds the previous delivery and expects the same files, in the same formats,
with the hand-written parts carried forward. Everything here is driven by the
previous delivery folder (--prev-delivery) or its individual files
(--prev-bom, --prev-assembly) plus a short change list (--changes), and it
refuses to invent: a line the BOM lacks is never claimed in the assembly text.

  carry_bom       previous BOM format, Manufacturer/Notes carried, DNP kept
  carry_assembly  previous instructions carried, generated sections refreshed
  claims          assembly-vs-BOM check (what the text promises the BOM has)
  readme_first    version + what changed since <prev>, hand-written parts kept
  review_folder   schematic pdf, ERC, netlist, review json
  renders         top / bottom / iso / iso-back png
  datasheets      PDFs for the BOM's MPNs
  project         pcb/sch/pro/prl + project libs, openable on its own
"""

import csv
import glob
import json
import os
import re
import shutil
import tempfile
from collections import Counter

from . import procs as _procs

BOM_COLS = ["Refs", "Qty", "Value", "Footprint", "MPN", "Manufacturer", "Notes"]
_REF_COLS = ("Refs", "Designators", "Designator", "Reference", "refs")
_DNP_RX = re.compile(r"DO[ -]NOT[ -](?:POPULATE|FIT)|\bDNP\b", re.I)
_VER_RX = re.compile(r"v\d+(?:\.\d+)+", re.I)


def version_label(*cands):
    """'utv-comms-v1.5.3-fab' -> 'v1.5.3'; first candidate that carries one."""
    for c in cands:
        m = _VER_RX.findall(os.path.basename(str(c or "")))
        if m:
            return m[-1].lower()
    return None


def _refkey(ref):
    m = re.match(r"([A-Za-z_]*)(\d*)", ref or "")
    return (m.group(1), int(m.group(2) or 0), ref)


def split_refs(s):
    return [r for r in re.split(r"[,\s;]+", s or "") if r]


def read_bom(path):
    """-> [{Refs: [..], Qty, Value, Footprint, MPN, Manufacturer, Notes}]"""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            col = next((c for c in _REF_COLS if c in r), None)
            if not col:
                continue
            d = {k: (r.get(k) or "").strip() for k in BOM_COLS[1:]}
            d["Refs"] = split_refs(r[col])
            rows.append(d)
    return rows


def write_bom(rows, path):
    rows = sorted(rows, key=lambda r: _refkey(r["Refs"][0] if r["Refs"] else ""))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(BOM_COLS)
        for r in rows:
            w.writerow([" ".join(sorted(r["Refs"], key=_refkey)),
                        len(r["Refs"]) or r["Qty"], r["Value"], r["Footprint"],
                        r["MPN"], r["Manufacturer"], r["Notes"]])
    return path


def is_dnp(row):
    return bool(_DNP_RX.search(row.get("Notes", "")))


def carry_bom(prev_path, new_path, out_path, label=None, prev_label=None,
              footprints=None, log=print):
    """Write the new BOM in the previous BOM's format.

    A new line whose MPN the previous BOM had keeps that line's Manufacturer and
    Notes (refs/qty/value/footprint come from the new BOM). A line whose MPN
    changed on the same footprint is marked CHANGED, not given the old part's
    rationale. A line with no predecessor is marked NEW. A previous DO-NOT-
    POPULATE line is kept while any of its refs is still on the board, and a
    board-DNP ref with no BOM line at all gets one. -> report dict."""
    prev = read_bom(prev_path)
    new = read_bom(new_path)
    fps = {p["ref"]: p for p in (footprints or [])}
    by_mpn = {}
    for r in prev:
        if r["MPN"]:
            by_mpn.setdefault(r["MPN"].upper(), r)
    out, used = [], set()
    rep = {"carried": [], "changed": [], "added": [], "removed": [],
           "dnp_kept": [], "dnp_added": []}
    for n in new:
        old = by_mpn.get(n["MPN"].upper()) if n["MPN"] else None
        if old is None:                       # same footprint, overlapping refs, new MPN
            old_c = next((r for r in prev if id(r) not in used and not is_dnp(r)
                          and set(r["Refs"]) & set(n["Refs"])
                          and r["Footprint"] == n["Footprint"]), None)
            row = dict(n)
            if old_c is not None:
                used.add(id(old_c))
                row["Notes"] = ("CHANGED in %s (was %s %s)" % (
                    label or "this version", old_c["Value"], old_c["MPN"] or "no MPN")
                    + ("; " + n["Notes"] if n["Notes"] else ""))
                rep["changed"].append((" ".join(n["Refs"]), old_c["MPN"], n["MPN"]))
            else:
                row["Notes"] = n["Notes"] or "NEW in %s" % (label or "this version")
                rep["added"].append((" ".join(n["Refs"]), n["MPN"]))
            out.append(row)
            continue
        used.add(id(old))
        row = dict(n)
        row["Manufacturer"] = n["Manufacturer"] or old["Manufacturer"]
        row["Notes"] = old["Notes"] or n["Notes"]
        if sorted(old["Refs"]) != sorted(n["Refs"]):
            rep["changed"].append((" ".join(n["Refs"]), "refs were " + " ".join(old["Refs"]), n["MPN"]))
        else:
            rep["carried"].append(" ".join(n["Refs"]))
        out.append(row)
    in_bom = {x for r in out for x in r["Refs"]}
    for r in prev:
        if id(r) in used:
            continue
        refs = r["Refs"]
        if is_dnp(r) and (not fps or any(x in fps for x in refs)):
            keep = [x for x in refs if x not in in_bom and (not fps or x in fps)]
            if keep:
                row = dict(r); row["Refs"] = keep
                out.append(row)
                rep["dnp_kept"].append(" ".join(keep))
                in_bom.update(keep)
                continue
        if not is_dnp(r) or fps:
            rep["removed"].append((" ".join(refs), r["MPN"]))
    for ref, p in sorted(fps.items(), key=lambda kv: _refkey(kv[0])):
        if p["dnp"] and ref not in in_bom:
            out.append({"Refs": [ref], "Qty": "1", "Value": "", "Footprint": p["lib"].split(":")[-1],
                        "MPN": "", "Manufacturer": "",
                        "Notes": "DO NOT POPULATE (board DNP; no line in the previous BOM)"})
            rep["dnp_added"].append(ref)
    write_bom(out, out_path)
    rep["rows"] = out
    rep["path"] = out_path
    log("    BOM %s: %d lines (%d carried, %d changed, %d new, %d dropped, %d DNP kept)" % (
        os.path.basename(out_path), len(out), len(rep["carried"]), len(rep["changed"]),
        len(rep["added"]), len(rep["removed"]), len(rep["dnp_kept"]) + len(rep["dnp_added"])))
    return rep


# ------------------------------------------------------------------ assembly
_SEC_RX = re.compile(r"^## (\d+)\. (.+)$", re.M)
# titles pcbway.assembly_notes generates; every other section is hand-written
GENERATED = ("Board", "Do not populate", "Process notes", "Parts we supply",
             "Substitutions")


def _sections(md):
    """-> (preamble, [(num, title, body_with_heading)])"""
    ms = list(_SEC_RX.finditer(md))
    pre = md[:ms[0].start()] if ms else md
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(md)
        out.append((int(m.group(1)), m.group(2).strip(), md[m.start():end].rstrip() + "\n"))
    return pre, out


def _generated_key(title):
    return next((g for g in GENERATED if title.startswith(g)), None)


def _supply_rows(body):
    return [ln for ln in body.splitlines()
            if ln.startswith("|") and not ln.startswith("| MPN") and not ln.startswith("|---")]


def _pin_fix(body, fps, log):
    """`J5 ... 12-pin` -> pad count of J5 on the board when it changed."""
    def sub(m):
        p = fps.get(m.group(1))
        if p and p["pads"] and str(p["pads"]) != m.group(3):
            log("    assembly: %s pin count %s -> %d" % (m.group(1), m.group(3), p["pads"]))
            return "%s%s%d%s" % (m.group(1), m.group(2), p["pads"], m.group(4))
        return m.group(0)
    return re.sub(r"\b([A-Z]{1,3}\d+[A-Z]?)\b([^\n.|]{0,40}?\b)(\d+)([- ]pin\b)", sub, body)


def carry_assembly(new_md, prev_md, bom_rows, footprints=(), changes="",
                   label=None, prev_label=None, log=print):
    """The new generated instructions with the previous version's hand-written
    sections carried verbatim (pin counts refreshed from the board). Generated
    sections are refreshed from the board, except that the 'Parts we supply'
    table keeps previous rows whose MPN is still on the BOM and a previous
    'Substitutions' text is kept when it is longer. A "What changed" section is
    appended. -> markdown."""
    fps = {p["ref"]: p for p in footprints}
    pre, nsec = _sections(new_md)
    _, psec = _sections(prev_md or "")
    prev_by = {}
    for num, title, body in psec:
        prev_by.setdefault(_generated_key(title) or title, (num, title, body))
    bom_mpns = {r["MPN"].upper() for r in bom_rows if r["MPN"]}
    out = [pre.rstrip() + "\n"]
    seen = set()
    for num, title, body in nsec:
        key = _generated_key(title)
        seen.add(key)
        p = prev_by.get(key)
        if key == "Parts we supply" and p:
            rows = _supply_rows(body)
            have = {ln.split("|")[1].strip().upper() for ln in rows}
            for ln in _supply_rows(p[2]):
                mpn = ln.split("|")[1].strip().upper()
                if mpn in bom_mpns and mpn not in have:
                    rows.append(ln)
            if rows:
                body = ("## %d. %s\n\n| MPN | Why | Used by |\n|---|---|---|\n%s\n\n"
                        "These arrive from us. Do not source alternates for them.\n"
                        % (num, title, "\n".join(rows)))
        elif key == "Substitutions" and p and len(p[2]) > len(body):
            body = p[2]
        out.append(body)
    for num, title, body in psec:
        if _generated_key(title) is None:
            out.append(_pin_fix(_rename_inputs(body, label or ""), fps, log))
    nxt = max([n for n, _, _ in psec] + [n for n, _, _ in nsec] + [0]) + 1
    if changes.strip():
        out.append("## %d. What changed since %s\n\n%s\n" % (nxt, prev_label or "the previous version",
                                                            changes.strip()))
    return "\n".join(s.rstrip() + "\n" for s in out)


def claims(md, bom_rows, footprints=()):
    """Problems where the assembly text promises something the BOM lacks:
    a DNP ref with no DO-NOT-POPULATE line, a supplied MPN not on the BOM, a
    bold ref in the text that is neither on the BOM nor on the board."""
    bad = []
    bom_refs = {x for r in bom_rows for x in r["Refs"]}
    bom_mpns = {r["MPN"].upper() for r in bom_rows if r["MPN"]}
    dnp = {x for r in bom_rows if is_dnp(r) for x in r["Refs"]}
    board = {p["ref"] for p in footprints}
    for num, title, body in _sections(md)[1]:
        if title.startswith("Do not populate"):
            m = re.search(r"\*\*([^*]+?) — DO NOT FIT", body)
            for ref in split_refs(m.group(1)) if m else []:
                if ref not in dnp:
                    bad.append("section %d: %s is DNP but the BOM has no DO-NOT-POPULATE line for it" % (num, ref))
        elif title.startswith("Parts we supply"):
            for ln in _supply_rows(body):
                mpn = ln.split("|")[1].strip()
                if mpn.upper() not in bom_mpns:
                    bad.append("section %d: supplied part %s is not on the BOM" % (num, mpn))
        else:
            for ref in re.findall(r"\*\*([A-Z]{1,3}\d+[A-Z]?)\b", body):
                if ref not in bom_refs and ref not in board:
                    bad.append("section %d: %s is named but is neither on the BOM nor the board" % (num, ref))
    return list(dict.fromkeys(bad))


# ------------------------------------------------------------------ readme
_RULE_RX = re.compile(r"^([A-Z][^\n]{3,})\n-{3,}\n", re.M)
_REGENERATED = ("WHAT CHANGED", "VERIFICATION STATE", "CONTENTS", "KNOWN OPEN", "PACKAGING STATUS")


def known_open_status(prev_readme, changes):
    """Every prior KNOWN OPEN item with a status line: CLOSED when a --changes
    line names it and says closed/fixed/resolved, else STILL OPEN."""
    from .packrules import known_open
    out = []
    for item in known_open(prev_readme):
        toks = re.findall(r"\b[A-Z]{1,3}\d+[A-Z]?\b|\b[\w-]{7,}\b", item)[:4]
        hit = next((ln for ln in changes.splitlines()
                    if any(t in ln for t in toks) and re.search(r"clos|fix|resolv|done|clear", ln, re.I)), None)
        out.append("  - %s\n      STATUS: %s" % (item, ("CLOSED per --changes: " + hit.strip()) if hit
                                                    else "STILL OPEN (not recorded closed in --changes)"))
    return "\n".join(out)


def readme_first(label, prev_label, title, board_line, changes, verification,
                 contents, prev_readme="", status="", packaging=""):
    """README-FIRST: version line, WHAT CHANGED since <prev>, VERIFICATION and
    CONTENTS regenerated; every other titled section of the previous README
    (rules, known-open items, schematic warnings) carried verbatim."""
    L = ["%s - %s DELIVERY PACKAGE" % (title.upper(), label.upper()), "=" * 44, board_line, ""]
    L += ["%s supersedes %s. Read WHAT CHANGED first." % (label, prev_label or "the previous delivery"), ""]
    def sec(name, body):
        L.extend(["", name, "-" * len(name), body.rstrip(), ""])
    sec("WHAT CHANGED SINCE %s" % (prev_label or "THE PREVIOUS VERSION").upper(),
        changes.strip() or "(no --changes file given: nothing recorded)")
    sec("VERIFICATION STATE", verification)
    if packaging:
        sec("PACKAGING STATUS (docs/PACKAGING-RULES.md)", packaging)
    if status:
        sec("KNOWN OPEN (status since the previous version)", status)
    ms = list(_RULE_RX.finditer(prev_readme or ""))
    for i, m in enumerate(ms):
        if m.group(1).startswith(_REGENERATED):
            continue
        end = ms[i + 1].start() if i + 1 < len(ms) else len(prev_readme)
        sec(m.group(1).strip(), prev_readme[m.end():end])
    sec("CONTENTS", contents)
    return "\n".join(L).rstrip() + "\n"


# ------------------------------------------------------------------ folders
def _run(cmd, timeout=900):
    return _procs.run(["nice", "-n", "10"] + cmd, capture_output=True, text=True, timeout=timeout)


def review_folder(out_dir, stem, sch, findings, kicad_cli="kicad-cli", log=print):
    """review/: SCHEMATIC pdf, ERC report, NETLIST, REVIEW json. -> files made."""
    rdir = os.path.join(out_dir, "review")
    os.makedirs(rdir, exist_ok=True)
    made = []
    if findings is not None:
        p = os.path.join(rdir, "REVIEW-%s.json" % stem)
        json.dump(findings, open(p, "w"), indent=1)
        made.append(p)
    if sch and os.path.exists(sch):
        for name, cmd in (
                ("SCHEMATIC-%s.pdf" % stem, ["sch", "export", "pdf"]),
                ("ERC-%s.rpt" % stem, ["sch", "erc", "--severity-all", "--exit-code-violations"]),
                ("NETLIST-%s.net" % stem, ["sch", "export", "netlist"])):
            p = os.path.join(rdir, name)
            try:
                r = _run([kicad_cli] + cmd + ["-o", p, sch])
            except Exception as e:
                log("    review: %s failed: %s" % (name, str(e)[:80]))
                continue
            if os.path.exists(p):
                made.append(p)
            else:
                log("    review: %s not produced: %s" % (name, (r.stderr or r.stdout or "")[-120:]))
    else:
        log("    review: no schematic, only the review json")
    return made


VIEWS = (("top", ["--side", "top"]), ("bottom", ["--side", "bottom"]),
         ("iso", ["--rotate", "-45,0,45"]), ("iso-back", ["--side", "bottom", "--rotate", "-45,0,45"]))


def renders(board, out_dir, stem, kicad_cli="kicad-cli", log=print):
    made = []
    for name, extra in VIEWS:
        p = os.path.join(out_dir, "%s-%s.png" % (stem, name))
        try:
            _run([kicad_cli, "pcb", "render", "--quality", "high", "--background", "opaque",
                  "-w", "1800", "-h", "1200"] + extra + ["-o", p, board])
        except Exception as e:
            log("    render %s failed: %s" % (name, str(e)[:80]))
            continue
        if os.path.exists(p):
            made.append(p)
        else:
            log("    render %s not produced" % name)
    return made


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def datasheets(mpns, dirs, out_dir, log=print):
    """Copy <MPN>.pdf for each MPN from the first dir that has it.
    -> (copied, missing)"""
    index = {}
    for d in dirs:
        for f in sorted(glob.glob(os.path.join(d, "*.pdf"))):
            index.setdefault(_norm(os.path.splitext(os.path.basename(f))[0]), f)
    copied, missing = [], []
    os.makedirs(out_dir, exist_ok=True)
    for m in sorted(set(mpns)):
        src = index.get(_norm(m))
        if src:
            dst = os.path.join(out_dir, os.path.basename(src))
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
            copied.append(m)
        else:
            missing.append(m)
    for d in dirs:
        src = os.path.join(d, "datasheets.json")
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(out_dir, "datasheets.json"))
            break
    return copied, missing


def project(board, out_dir, lib_dirs, sch=None, extra_sch_dirs=(), log=print):
    """The KiCad project: pcb/pro/prl (+ sch) at the root, project libs under
    lib/, fp-lib-table pointing at them. Model and footprint paths written as
    ${KIPRJMOD}/../lib are rewritten to ${KIPRJMOD}/lib because the libs sit
    beside the project here. -> list of files."""
    made = []
    stem = os.path.splitext(os.path.basename(board))[0]
    bdir = os.path.dirname(os.path.abspath(board))
    lib = next((d for d in lib_dirs if d and os.path.isdir(d)), None)
    for ext in (".kicad_pro", ".kicad_prl"):
        src = os.path.join(bdir, stem + ext)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(out_dir, stem + ext))
            made.append(stem + ext)
    txt = open(board, encoding="utf-8").read()
    if lib:
        txt = txt.replace("${KIPRJMOD}/../lib/", "${KIPRJMOD}/lib/")
    open(os.path.join(out_dir, stem + ".kicad_pcb"), "w", encoding="utf-8").write(txt)
    made.append(stem + ".kicad_pcb")
    if sch and os.path.isfile(sch):
        shutil.copy2(sch, os.path.join(out_dir, stem + ".kicad_sch"))
        made.append(stem + ".kicad_sch")
    if lib:
        dst = os.path.join(out_dir, "lib")
        if not os.path.exists(dst):
            shutil.copytree(lib, dst)
        made.append("lib/")
        tbl = next((t for t in (os.path.join(bdir, "fp-lib-table"),
                                os.path.join(os.path.dirname(lib), "fp-lib-table"),
                                *[os.path.join(os.path.dirname(d), "fp-lib-table") for d in extra_sch_dirs])
                    if os.path.isfile(t)), None)
        if tbl:
            t = open(tbl, encoding="utf-8").read().replace("${KIPRJMOD}/../lib/", "${KIPRJMOD}/lib/")
            open(os.path.join(out_dir, "fp-lib-table"), "w", encoding="utf-8").write(t)
            made.append("fp-lib-table")
    return made


def tree(root):
    out = []
    for dp, dn, fn in os.walk(root):
        dn.sort()
        for f in sorted(fn):
            out.append(os.path.relpath(os.path.join(dp, f), root))
    return out


def prev_files(prev_dir):
    """(bom, assembly md, readme, datasheet dir, lib dir, label) found in a
    previous delivery folder; each None when absent."""
    g = lambda pat: next(iter(sorted(glob.glob(os.path.join(prev_dir, pat)))), None)
    return {
        "bom": g("2-BOM-*.csv") or g("BOM-*.csv"),
        "assembly": g("assembly-instructions-*.md"),
        "readme": g("README-FIRST.txt") or g("README-FIRST*.txt"),
        "datasheets": os.path.join(prev_dir, "docs", "datasheets"),
        "lib": os.path.join(prev_dir, "lib"),
        "sch": g("*.kicad_sch"),
        "label": version_label(prev_dir, g("2-BOM-*.csv"), g("BOM-*.csv")),
    }


# ------------------------------------------------------------------ orchestration
def start(a, boms, board_facts, log=print):
    """Before the PCBWay facts are collected: carry the BOM. -> (ctx, boms)
    with the board's BOM swapped for the carried one, or (None, boms) when no
    previous-version input was given."""
    prev = prev_files(a.prev_delivery) if a.prev_delivery else {}
    prev_bom = a.prev_bom or prev.get("bom")
    if not (prev_bom or a.prev_assembly or a.prev_delivery or a.changes):
        return None, boms
    ctx = {"prev": prev, "label": version_label(a.name, a.out) or "new version",
           "fps": board_facts.get("footprints") or [], "rep": None, "bom_rows": []}
    ctx["prev_label"] = prev.get("label") or version_label(prev_bom, a.prev_assembly, a.prev_delivery) or "the previous version"
    ctx["changes"] = open(a.changes, encoding="utf-8").read() if a.changes else ""
    ctx["tmp"] = tempfile.mkdtemp(prefix="fp-deliverver-")
    boms = list(boms)
    own = next((b for b in boms if b and os.path.isfile(b) and read_bom(b)), None)
    if prev_bom and own:
        out = os.path.join(ctx["tmp"], os.path.basename(own))
        ctx["rep"] = carry_bom(prev_bom, own, out, label=ctx["label"],
                               prev_label=ctx["prev_label"], footprints=ctx["fps"], log=log)
        ctx["bom_rows"] = ctx["rep"]["rows"]
        boms[boms.index(own)] = out
    elif own:
        ctx["bom_rows"] = read_bom(own)
    return ctx, boms


def assembly_post(ctx, a, log=print):
    """pcbway.write hook: carry the previous instructions, then check claims."""
    prev_md = ""
    path = a.prev_assembly or ctx["prev"].get("assembly")
    if path and os.path.isfile(path):
        prev_md = open(path, encoding="utf-8").read()

    def post(new_md):
        md = carry_assembly(new_md, prev_md, ctx["bom_rows"], ctx["fps"], ctx["changes"],
                            ctx["label"], ctx["prev_label"], log=log)
        ctx["claims"] = claims(md, ctx["bom_rows"], ctx["fps"]) if ctx["bom_rows"] else []
        for c in ctx["claims"]:
            log("    WARNING assembly vs BOM: " + c)
        return md
    return post


def _find_sch(a, bdir, stem):
    """The project's real schematic: --sch, else <board stem>.kicad_sch beside the
    board. Never regenerated from a spec and never taken from the previous
    delivery: absent -> None and P6 fails 'schematic missing'."""
    for p in (a.sch, os.path.join(bdir, stem + ".kicad_sch")):
        if p and os.path.isfile(p):
            return p
    return None


def finish(ctx, a, findings, slots, facts, kicad_cli="kicad-cli", log=print):
    """Everything around the PCBWay four slots. -> {name: note} of what was made."""
    from . import fab as F
    out = a.out
    stem = os.path.splitext(os.path.basename(a.board))[0]
    lab, prev_lab = ctx["label"], ctx["prev_label"]
    prev = ctx["prev"]
    notes = {}
    # plain BOM copy + fab folder
    if ctx["rep"]:
        shutil.copy2(ctx["rep"]["path"], os.path.join(out, "BOM-%s.csv" % lab))
        notes["BOM-%s.csv" % lab] = "carried BOM"
    fdir = os.path.join(out, "fab-%s" % lab)
    man = F._manifest_files(a.fab_dir)
    for sec in list(F.CAM_ONLY) + ["place"]:
        src = os.path.join(a.fab_dir, sec)
        if os.path.isdir(src):
            os.makedirs(os.path.join(fdir, sec), exist_ok=True)
            for fn in man.get(sec) or _listdir(src):
                if os.path.isfile(os.path.join(src, fn)):
                    shutil.copy2(os.path.join(src, fn), os.path.join(fdir, sec, fn))
        elif os.path.isfile(src):
            os.makedirs(fdir, exist_ok=True)
            shutil.copy2(src, os.path.join(fdir, sec))
    if ctx["rep"]:
        shutil.copy2(ctx["rep"]["path"], os.path.join(fdir, "BOM-%s.csv" % lab))
    # design inputs
    for src, name in ((a.constraints, "constraints-%s.toml" % lab), (a.spec, "spec-%s.json" % lab)):
        if src and os.path.isfile(src):
            shutil.copy2(src, os.path.join(out, name))
    # schematic + project
    bdir = os.path.dirname(os.path.abspath(a.board))
    sch = _find_sch(a, bdir, stem)
    project(a.board, out, [os.path.join(os.path.dirname(bdir), "lib"), os.path.join(bdir, "lib"),
                           prev.get("lib")], sch=sch, extra_sch_dirs=[a.prev_delivery or ""])
    notes["schematic"] = ("from %s" % sch if sch
                          else "MISSING (no --sch, none beside the board): not substituted")
    # review folder (from the delivered copy of the project)
    if getattr(a, "no_review", False):
        findings = None
    review_folder(out, re.sub(r"-fab$", "", a.name), os.path.join(out, stem + ".kicad_sch"), findings,
                  kicad_cli=kicad_cli, log=log)
    # renders from the project copy so ${KIPRJMOD}/lib models resolve
    renders(os.path.join(out, stem + ".kicad_pcb"), out, stem, kicad_cli=kicad_cli, log=log)
    # datasheets
    dirs = [d for d in (a.datasheets, prev.get("datasheets"),
                        *[os.path.join(p, "docs", "datasheets") for p in _ancestors(a.board)])
            if d and os.path.isdir(d)]
    mpns = [r["MPN"] for r in ctx["bom_rows"] if r["MPN"]]
    got, miss = datasheets(mpns, dirs, os.path.join(out, "docs", "datasheets"), log=log)
    if miss and not getattr(a, "no_api", False):
        try:
            from . import partdocs as PD2
            res_f = PD2.fetch(miss, os.path.join(out, "docs", "datasheets"), log=log)
            miss = sorted(m for m in miss if not res_f.get(m))
            got = sorted(set(mpns) - set(miss))
        except Exception as e:
            log("    datasheet fetch failed: %s" % str(e)[:100])
    notes["datasheets"] = "%d/%d MPNs; missing: %s" % (len(got), len(set(mpns)), ", ".join(miss) or "none")
    # hand-written docs carried (spec, decisions); review response is version-specific
    if a.prev_delivery:
        for f in sorted(glob.glob(os.path.join(a.prev_delivery, "docs", "*"))):
            if os.path.isfile(f):
                shutil.copy2(f, os.path.join(out, "docs", os.path.basename(f)))
    # 3D: assembly STEP (the stage's own when it made one) + bare board
    d3 = os.path.join(out, "5-3D")
    os.makedirs(d3, exist_ok=True)
    pcb = os.path.join(out, stem + ".kicad_pcb")
    step = os.path.join(bdir, "out", "model.step")
    if os.path.isfile(step):
        shutil.copy2(step, os.path.join(d3, "%s-assembly.step" % stem))
    for name, extra in (("assembly", []), ("bare-board", ["--board-only"])):
        dst = os.path.join(d3, "%s-%s.step" % (stem, name))
        if not os.path.isfile(dst):
            try:
                _run([kicad_cli, "pcb", "export", "step", "--force", "--subst-models"] + extra + ["-o", dst, pcb])
            except Exception as e:
                log("    step %s failed: %s" % (name, str(e)[:80]))
        if not os.path.isfile(dst):
            notes["5-3D %s" % name] = "not produced"
    # README-FIRST
    prev_readme = open(prev["readme"], encoding="utf-8").read() if prev.get("readme") else ""
    res = _verify_line(findings, a.fab_dir)
    if findings is None:
        notes["review"] = "gate skipped (--no-review): no review json written"
    contents = "\n".join(sorted(set(
        ("%s/" % p.split(os.sep)[0]) if os.sep in p else p for p in tree(out)
        if not p.startswith("lib" + os.sep))))
    title = re.sub(r"-?v\d.*$", "", stem).replace("-", " ").strip() or stem
    bl = "%s: %s" % (lab, (facts.get("size_mm") and "%.1f x %.1f mm, %s layers" % (
        facts["size_mm"] + (facts.get("layers"),))) or stem)
    prev_readme = _rename_inputs(prev_readme, lab)
    kstat = known_open_status(prev_readme, ctx["changes"])
    write = lambda pk: open(os.path.join(out, "README-FIRST.txt"), "w", encoding="utf-8").write(
        readme_first(lab, prev_lab, title, bl, ctx["changes"], res, contents, prev_readme, kstat, pk))
    write("")
    notes["claims"] = "; ".join(ctx.get("claims") or []) or "none"
    # packaging gate (docs/PACKAGING-RULES.md)
    from . import packrules as PR, constraints as C
    cons = C.load(a.constraints) if a.constraints else {}
    fps = ctx["fps"]
    pbom = a.prev_bom or prev.get("bom")
    pb = next(iter(sorted(glob.glob(os.path.join(a.prev_delivery or "", "*.kicad_pcb")))), None) if a.prev_delivery else None
    pctx = dict(out=out, board=pcb, cons=cons, findings=findings, kicad_cli=kicad_cli,
                prev_board=pb, prev_bom=pbom, prev_label=prev_lab, bom_rows=ctx["bom_rows"],
                dnp=sorted({r for row in ctx["bom_rows"] if is_dnp(row) for r in row["Refs"]}
                           | {p["ref"] for p in fps if p["dnp"]}, key=_refkey),
                board_refs={p["ref"] for p in fps}, claims=ctx.get("claims") or [],
                removed=[m for _, m in (ctx["rep"] or {}).get("removed", []) if m],
                missing_datasheets=miss, prev_known_open=PR.known_open(prev_readme))
    waive = list((cons.get("packaging") or {}).get("waive", [])) + list(a.pack_waive or [])
    res_p = PR.check(pctx, waive)
    for ln in PR.lines(res_p):
        log("    " + ln)
    PR.mark(out, res_p)
    write(PR.readme_block(res_p))
    ctx["ready"] = PR.ready(res_p)
    notes["packaging"] = "READY" if ctx["ready"] else "NOT-READY"
    return notes


def _rename_inputs(text, lab):
    """carried text naming spec-v15.json / constraints-v15.toml -> this version's files"""
    return re.sub(r"\b(spec|constraints)-v?\d[\w.]*?\.(json|toml)\b",
                  lambda m: "%s-%s.%s" % (m.group(1), lab, m.group(2)), text)


def _listdir(d):
    return [f for f in sorted(os.listdir(d)) if os.path.isfile(os.path.join(d, f))]


def _ancestors(board):
    d = os.path.dirname(os.path.abspath(board))
    for _ in range(4):
        yield d
        d = os.path.dirname(d)


def _verify_line(findings, fab_dir):
    lines = []
    if findings is None:
        lines.append("REVIEW  skipped (--no-review): no review json in this delivery")
    else:
        c = Counter(f.get("level") or f.get("severity") for f in findings)
        lines.append("REVIEW  fluxplace review: %d FAIL, %d WARN, %d INFO (review/REVIEW-*.json)"
                     % (c.get("FAIL", 0), c.get("WARN", 0), c.get("INFO", 0)))
    drc = os.path.join(fab_dir, "drc.json")
    if os.path.isfile(drc):
        try:
            d = json.load(open(drc))
            lines.append("DRC     %s violations, %s unconnected (fab-*/drc.json)" % (
                len(d.get("violations", [])), len(d.get("unconnected_items", []))))
        except Exception:
            pass
    return "\n".join(lines) or "(no verification data)"
