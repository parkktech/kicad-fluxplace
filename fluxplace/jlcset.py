"""JLCPCB upload set, written beside the PCBWay one (fab.emit -> <out>/jlcpcb/).

    jlcpcb/<stem>-gerbers.zip   copper, mask, silk, paste, outline + PTH/NPTH drills (no fab/courtyard/user layers)
    jlcpcb/BOM-JLCPCB.csv       Comment, Designator, Footprint, LCSC Part #
    jlcpcb/CPL-JLCPCB.csv       Designator, Mid X, Mid Y, Layer, Rotation (JLC rotation corrections applied)
    jlcpcb/CONSIGNED.txt        parts with no LCSC pick: refs, MPN, reason (JLC marks them customer-supplied)

Formats (kicad-happy marketplace skills jlcpcb/SKILL.md "BOM Format for Assembly", bom/references/ordering-and-fabrication.md
"CPL", bom/scripts/translate_bom_pnp.py: "<n>.4fmm" coordinates, Top/Bottom layer; the LCSC column must be exactly "LCSC Part #"):
JLC rejects a CPL holding designators that are not in the BOM, so the CPL is the BOM's refs only.
Rotation corrections: JLCKicadTools DB fetched at run time (pinned commit, sha256), see ROT_URL.
DNP parts and parts flagged exclude_from_bom are in neither file. Board-agnostic: nothing here names a ref or net.

LCSC pick order per part: [sourcing.lcsc] "<MPN>" = "Cxxxx" (constraints) > "LCSC Cxxxx" in a previous BOM's notes
(prev_bom) > live lcsc.check (stock > 0, MPN through [sourcing.cn_alias]; under [sourcing] cn_need = listed as THIN). [sourcing] consign = [ref regex]
forces a part to be consigned. A part with no pick is consigned: blank LCSC cell + a CONSIGNED.txt line with the reason.
"""
import csv
import json
import os
import re
import zipfile

from . import fabxcheck

SECTION = "jlcpcb"
# Protel extensions JLC's CAM reads: copper, mask, silk, paste, outline. Adhesive/courtyard/fab/user/margin/job files are left out.
GERBER_EXT = re.compile(r"\.(gtl|gbl|g\d+|gts|gbs|gto|gbo|gtp|gbp|gm1|gko)$", re.I)
DRILL_EXT = re.compile(r"\.drl$", re.I)
# Rotation DB: JLCKicadTools (Matthew Lai, GPL-3.0) is NOT vendored; fetched at run time from this pinned commit, sha256-checked,
# cached under ~/.cache/fluxplace. Rule semantics (JLCKicadTools jlc_lib/cpl_fix_rotations.py): re.match on the KiCad Package name,
# LAST matching rule wins, rot added on top / subtracted on bottom, optional dx/dy added to Mid X/Y, then bottom rotation =
# (-rot + 180) mod 360 (JLC convention since ~2022-07; the source notes JLC has changed it several times - check JLC's CPL preview).
ROT_COMMIT = "50ab53794357e2217bf56cae1381be5ff23dbb37"
ROT_URL = ("https://raw.githubusercontent.com/matthewlai/JLCKicadTools/%s/jlc_kicad_tools/cpl_rotations_db.csv" % ROT_COMMIT)
ROT_SHA256 = "fce26b890863fa56c6fc8d91a44241d9f3b420c78da28a10bc13204903542698"
_LCSC_NOTE = re.compile(r"\bLCSC\s+(C\d{3,})\b")


def parse_rotations(text):
    """[(pattern, rot, dx, dy)] from the JLCKicadTools CSV; dx/dy default to 0."""
    def offset(row, i):
        return float(row[i]) if len(row) > i and row[i].strip() else 0.0

    return [(re.compile(row[0]), int(row[1]), offset(row, 2), offset(row, 3))
            for row in csv.reader(text.splitlines()) if len(row) >= 2 and row[0] != "Footprint pattern"]


def rotation_rules(cache_dir=None, fetch=None):
    """(rules, warning). Cached copy first, else fetch ROT_URL; a body whose sha256 differs from ROT_SHA256 is refused.
    Offline or refused: ([], reason) and the CPL carries no correction."""
    import hashlib
    cache_dir = cache_dir or os.path.join(os.path.expanduser("~"), ".cache", "fluxplace")
    path = os.path.join(cache_dir, "jlc_cpl_rotations-%s.csv" % ROT_COMMIT[:8])

    def pinned(data):
        return hashlib.sha256(data).hexdigest() == ROT_SHA256

    try:
        with open(path, "rb") as f:
            body = f.read()
    except OSError:
        body = None
    if body is None or not pinned(body):
        try:
            if fetch:
                body = fetch(ROT_URL)
            else:
                import urllib.request
                body = urllib.request.urlopen(ROT_URL, timeout=20).read()
        except Exception as e:
            return [], "JLC rotation table not fetched (%s: %s)" % (type(e).__name__, e)
        if not pinned(body):
            return [], "JLC rotation table sha256 mismatch (pinned %s)" % ROT_SHA256[:12]
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "wb") as f:
            f.write(body)
    return parse_rotations(body.decode("utf-8")), None


def correct(package, side, x, y, rot, rules):
    """JLC (Mid X, Mid Y, Rotation) for a KiCad pos row: last matching rule wins; semantics in the ROT_URL notes."""
    hit = None
    for rx, r, dx, dy in rules:
        if rx.match(package):
            hit = (r, dx, dy)
    bottom = side == "bottom"
    if hit:
        rot = rot - hit[0] if bottom else rot + hit[0]
        x, y = x + hit[1], y + hit[2]
    if bottom:
        rot = -rot + 180
    return x, y, rot % 360


def board_parts(board):
    """{ref: dict(value, fp, mpn, dnp, excl_bom)} for every real footprint (text parse, no pcbnew)."""
    root = fabxcheck._parse(open(board, encoding="utf-8", errors="replace").read())
    out = {}
    for n in root[1:]:
        if not (isinstance(n, list) and n and n[0] == "footprint"):
            continue
        props, attr = {}, set()
        for c in n[1:]:
            if isinstance(c, list) and len(c) > 2 and c[0] == "property":
                props[c[1]] = c[2]
            elif isinstance(c, list) and c and c[0] == "attr":
                attr.update(x for x in c[1:] if isinstance(x, str))
        ref = props.get("Reference")
        if not ref or ref.startswith("#"):
            continue
        out[ref] = {"value": props.get("Value", ""), "fp": n[1].split(":")[-1], "mpn": props.get("MPN", ""),
                    "dnp": "dnp" in attr, "excl_bom": "exclude_from_bom" in attr}
    return out


def previous_picks(prev_bom):
    """{MPN: Cxxxx} read from 'LCSC Cxxxx' notes of an earlier BOM csv (Refs/Designators, MPN, Notes columns)."""
    picks = {}
    if prev_bom and os.path.isfile(prev_bom):
        for r in csv.DictReader(open(prev_bom, newline="", encoding="utf-8", errors="replace")):
            m = _LCSC_NOTE.search(r.get("Notes") or "")
            if m and r.get("MPN"):
                picks[r["MPN"].strip()] = m.group(1)
    return picks


def pick_lcsc(parts, cons=None, prev_bom=None, check=None, log=print):
    """{ref: (code | None, why)}. `check(mpns, need)` -> lcsc.check-shaped dict, or None to skip live lookups."""
    src = (cons or {}).get("sourcing", {})
    explicit = dict(src.get("lcsc", {}))
    prev = previous_picks(prev_bom)
    consign = [re.compile(p) for p in src.get("consign", [])]
    alias = src.get("cn_alias", {})
    need = src.get("cn_need", 100)

    def consigned(ref):
        return any(rx.search(ref) for rx in consign)

    mpns = sorted({p["mpn"] for r, p in parts.items() if p["mpn"] and p["mpn"] not in explicit and p["mpn"] not in prev
                   and not (p["dnp"] or p["excl_bom"]) and not consigned(r)})
    live = check([alias.get(m, m) for m in mpns], need) if check and mpns else {}
    out = {}
    for ref, p in parts.items():
        m = p["mpn"]
        if consigned(ref):
            out[ref] = (None, "consigned by constraints [sourcing] consign")
        elif not m:
            out[ref] = (None, "no MPN on the footprint")
        elif m in explicit:
            out[ref] = (explicit[m], "constraints [sourcing.lcsc]")
        elif m in prev:
            out[ref] = (prev[m], "previous BOM notes")
        else:
            r = live.get(alias.get(m, m))
            row = (r or {}).get("row")
            if row and row.get("lcsc") and row.get("stock", 0) > 0:
                thin = "thin: " if row["stock"] < need else ""      # picked, but under [sourcing] cn_need: flagged in CONSIGNED.txt
                out[ref] = (row["lcsc"], "%slive LCSC stock %d (need %d)" % (thin, row["stock"], need))
            elif r:
                out[ref] = (None, (r.get("why") or "LCSC %s" % r["grade"]) + (" (stock %d)" % row["stock"] if row else ""))
            else:
                out[ref] = (None, "not looked up")
    return out


def _bom_groups(parts, picks):
    groups = {}
    for ref, p in parts.items():
        if p["dnp"] or p["excl_bom"]:
            continue
        key = (p["value"], p["fp"], picks[ref][0] or "")
        groups.setdefault(key, []).append(ref)
    return groups


def _nkey(ref):
    m = re.match(r"([A-Za-z_]*)(\d*)", ref)
    return (m.group(1), int(m.group(2) or 0), ref)


def write_bom_cpl(parts, picks, pos_csv, out_dir, rotations=None):
    """BOM-JLCPCB.csv, CPL-JLCPCB.csv, CONSIGNED.txt in out_dir. Returns {"bom_refs", "consigned", "cpl_rows", "cpl_missing"}."""
    groups = _bom_groups(parts, picks)
    keep = {r for refs in groups.values() for r in refs}
    with open(os.path.join(out_dir, "BOM-JLCPCB.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        lines = [(val, sorted(refs, key=_nkey), fp, code) for (val, fp, code), refs in groups.items()]
        for val, refs, fp, code in sorted(lines, key=lambda line: _nkey(line[1][0])):
            w.writerow([val, ",".join(refs), fp, code])
    rules, warn = rotations if rotations is not None else rotation_rules()
    rows, seen, uncorrected = [], set(), []
    with open(pos_csv, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ref = r["Ref"].strip()
            if ref not in keep:
                continue
            side = r["Side"].strip().lower()
            x, y, rot = correct(r["Package"], side, float(r["PosX"]), float(r["PosY"]), float(r["Rot"]), rules)
            if warn and not re.match(r"[RCL]_\d{4}", r["Package"]):
                uncorrected.append(ref)
            rows.append([ref, "%.4fmm" % x, "%.4fmm" % y, "Bottom" if side == "bottom" else "Top", "%.4f" % rot])
            seen.add(ref)
    rows.sort(key=lambda r: _nkey(r[0]))
    with open(os.path.join(out_dir, "CPL-JLCPCB.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        w.writerows(rows)
    consigned = sorted((r for r in keep if not picks[r][0]), key=_nkey)
    thin = sorted((r for r in keep if picks[r][0] and picks[r][1].startswith("thin")), key=_nkey)
    with open(os.path.join(out_dir, "CONSIGNED.txt"), "w") as f:
        f.write("CONSIGNED: no LCSC pick. Leave the LCSC Part # blank on JLCPCB's BOM page and mark them customer-supplied.\n")
        for r in consigned:
            f.write("%s\t%s\t%s\n" % (r, parts[r]["mpn"] or parts[r]["value"], picks[r][1]))
        f.write("\nTHIN STOCK: LCSC code picked, stock under the build need. Check the quantity on JLCPCB's BOM page, or consign.\n")
        for r in thin:
            f.write("%s\t%s\t%s\t%s\n" % (r, parts[r]["mpn"] or parts[r]["value"], picks[r][0], picks[r][1]))
    return {"bom_refs": len(keep), "consigned": consigned, "thin": thin, "cpl_rows": len(rows), "rot_warn": warn,
            "uncorrected": sorted(uncorrected, key=_nkey), "cpl_missing": sorted(keep - seen, key=_nkey)}


def write_gerber_zip(gdir, ddir, out_zip):
    """Zip the JLC-accepted layer set from kicad-cli's gerber and drill dirs. Returns the names zipped."""
    names = []
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for d, rx in ((gdir, GERBER_EXT), (ddir, DRILL_EXT)):
            for fn in sorted(os.listdir(d)):
                if rx.search(fn):
                    z.write(os.path.join(d, fn), fn)
                    names.append(fn)
    return names


def emit(board, out, pos_csv, cons=None, prev_bom=None, check=None, log=print, rotations=None):
    """Write <out>/jlcpcb/ from an emit() package (gerbers/, drill/ already written). Returns a summary dict; never raises."""
    import shutil
    jdir = os.path.join(out, SECTION)
    shutil.rmtree(jdir, ignore_errors=True)
    os.makedirs(jdir)
    try:
        parts = board_parts(board)
        picks = pick_lcsc(parts, cons, prev_bom, check, log)
        stem = os.path.splitext(os.path.basename(board))[0]
        names = write_gerber_zip(os.path.join(out, "gerbers"), os.path.join(out, "drill"), os.path.join(jdir, stem + "-gerbers.zip"))
        s = write_bom_cpl(parts, picks, pos_csv, jdir, rotations)
        s.update(files=sorted(os.listdir(jdir)), gerber_files=names)
        log("jlcpcb set -> %s  (%d BOM refs, %d consigned, %d thin, %d CPL rows)" % (jdir, s["bom_refs"], len(s["consigned"]), len(s["thin"]), s["cpl_rows"]))
        if s["rot_warn"]:
            log("WARN JLC_ROTATION_UNCORRECTED: %s; CPL rotations are KiCad's. Check on JLC's preview: %s" % (s["rot_warn"], ", ".join(s["uncorrected"])))
        for m in s["cpl_missing"]:
            log("FAIL JLC_CPL_MISSING: %s is in the JLC BOM but not in the position file" % m)
        return s
    except Exception as e:
        log("FAIL jlcpcb set: %s: %s" % (type(e).__name__, e))
        return {"error": str(e), "files": sorted(os.listdir(jdir))}
