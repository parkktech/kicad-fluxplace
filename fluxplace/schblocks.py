"""`fluxplace schematic --layout blocks`: a drafted, hierarchical schematic (one sheet per functional block) with the same
connectivity as the spec, and the gate that proves it.

generate(): inventory (schinv) -> per-block layout (schdraw) -> root sheet (block sheet symbols + one global label + PWR_FLAG per
power net, so ERC knows the rails are driven) + one `<stem>-<block>.kicad_sch` per block next to it. Only relative file names
are written into KiCad files.

gate(): (1) netlist: kicad-cli exports the root's netlist; every net's pin set must equal the spec's (pinmap-resolved),
every component must be present with the spec's value and footprint, and the only single-pin nets allowed are the
declared no-connects -- any difference is FAIL; (2) ERC: violation counts by type; (3) legibility (schlegible): wire/wire
overlaps, wires through bodies, label/label and label/symbol overlaps; (4) renders: PDF (all sheets), one PNG per page,
SVG per sheet.
"""
import json
import os
import re
import shutil
import subprocess
from fluxplace import procs as _procs
import tempfile
from collections import Counter, defaultdict

from . import schdraw as D
from . import schinv as I
from . import blocks as BK


def generate(M, out_path, cons=None, title=None, inv=None):
    cons = cons or {}
    kinds = I.net_kinds(M, cons)
    inv = inv or BK.inventory(M, cons)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    stem = os.path.splitext(os.path.basename(out_path))[0]
    project = stem
    root_uuid = D._uuid("root:" + stem)
    title = title or M["name"]
    pwr_n = [0]
    sheets = []
    stats = {"blocks": [], "routed": 0, "unrouted": 0}
    alllib = {}
    for k, b in enumerate(inv["blocks"]):
        lib = D.Lib()
        sheet, external = D.layout_block(M, kinds, b, lib, cons)
        for s in sheet.syms:
            if s["kind"] == "pwr":
                pwr_n[0] += 1
                s["ref_i"] = "#PWR%04d" % pwr_n[0]
            elif s.get("ref") in inv["of"]:          # hidden field: the block the part belongs to (fluxplace blocks)
                s["fields"] = list(s["fields"]) + [(BK.FIELD, b["name"], s["x"], s["y"], "", True)]
        fname = "%s-%s.kicad_sch" % (stem, I.slug(b["name"]))
        suuid = D._uuid("sheetsym:%s:%s" % (stem, b["name"]))
        lines, paper, _ = D.emit_sheet(
            fname, sheet, lib, "%s - %s" % (title, b["name"]), project, "/%s/%s" % (root_uuid, suuid),
            "Block %d/%d, %d parts - fluxplace, from %s" % (k + 1, len(inv["blocks"]), len(b["refs"]), M["source"]))
        lines.append(")")
        alllib.update(lib.syms)
        with open(os.path.join(out_dir, fname), "w") as fh:
            fh.write("\n".join(lines) + "\n")
        rs = getattr(sheet, "route_stats", {"joined": 0, "unjoined": 0})
        stats["routed"] += rs["joined"]
        stats["unrouted"] += rs["unjoined"]
        stats["blocks"].append({"name": b["name"], "file": fname, "refs": len(b["refs"]), "how": b["how"],
                                "anchor": b["anchor"], "wires": len(sheet.wires),
                                "labels_local": sum(1 for l in sheet.labels if l["kind"] == "local"),
                                "labels_global": sum(1 for l in sheet.labels if l["kind"] == "global"),
                                "power_symbols": sum(1 for s in sheet.syms if s["kind"] == "pwr"),
                                "no_connects": len(sheet.ncs), "joined": rs["joined"], "unjoined": rs["unjoined"],
                                "conflicts": len(D.conflicts(sheet)),
                                "paper_mm": [round(paper[0]), round(paper[1])]})
        sheets.append((b, fname, suuid))
    # root
    lib = D.Lib()
    L = []
    cols = 4
    bw, bh = 48, 14
    for k, (b, fname, suuid) in enumerate(sheets):
        x = 12 + (k % cols) * (bw + 10)
        y = 12 + (k // cols) * (bh + 10)
        L.append('  (sheet (at %s %s) (size %s %s) (fields_autoplaced yes) (stroke (width 0.1524) (type solid)) '
                 '(fill (color 0 0 0 0.0000)) (uuid "%s")' % (D.mm(x), D.mm(y), D.mm(bw), D.mm(bh), suuid))
        L.append('    (property "Sheetname" "%s" (at %s %s 0) %s)' % (D._esc(b["name"]), D.mm(x), D.mm(y - 0.4),
                                                                   D.FONT % " (justify left bottom)"))
        L.append('    (property "Sheetfile" "%s" (at %s %s 0) %s)' % (D._esc(fname), D.mm(x), D.mm(y + bh + 0.4),
                                                                   D.FONT % " (justify left top)"))
        L.append('    (instances (project "%s" (path "/%s" (page "%d"))))' % (D._esc(project), root_uuid, k + 2))
        L.append("  )")
    ybase = 12 + ((len(sheets) + cols - 1) // cols) * (bh + 10) + 6
    flags = D.Draw()
    pnets = sorted(n for n, k in kinds.items() if k in ("gnd", "rail"))
    for k, n in enumerate(pnets):
        x = 12 + max(D.tw(m) for m in pnets) + 3 + (k % 6) * (max(D.tw(m) for m in pnets) + 10)
        x = int(x)
        y = ybase + (k // 6) * 6
        flags.labels.append({"kind": "global", "net": n, "x": x, "y": y, "dir": -1})
        flags.wires.append([x, y, x + 4, y, n])
        fl = D.flag_lib(lib)
        flags.syms.append({"lib": fl, "ref": "#FLG", "value": "PWR_FLAG", "fp": "", "x": x + 4, "y": y, "kind": "flag",
                           "fields": [("Value", "PWR_FLAG", x + 4, y - 3.2, "", True)], "pins": [(x + 4, y, n)], "dnp": False})
    nflag = 0
    for s in flags.syms:
        if s["kind"] == "pwr":
            pwr_n[0] += 1
            s["ref_i"] = "#PWR%04d" % pwr_n[0]
        else:
            nflag += 1
            s["ref_i"] = "#FLG%04d" % nflag
    flags.junctions = []
    lines, paper, (dx, dy) = D.emit_sheet(out_path, flags, lib, title, project, "/" + root_uuid,
                                          "%d block sheets - fluxplace, from %s; PWR_FLAGs: rails driven" % (len(sheets), M["source"]),
                                          paper_pad=(12, 12, 12, 30), offset=(0, 0))
    # the sheet symbols were laid out in absolute grid units from the top-left margin: keep them inside the paper
    lines[0:3] = ['(kicad_sch (version 20231120) (generator "fluxplace")', '  (uuid "%s")' % root_uuid,
                  '  (paper "User" %.2f %.2f)' % (max(paper[0], (12 + cols * (bw + 10)) * D.G),
                                                   max(paper[1], (ybase + 40) * D.G))]
    lines += L
    lines.append('  (sheet_instances (path "/" (page "1")))')
    lines.append(")")
    text = "\n".join(lines) + "\n"
    with open(out_path, "w") as fh:
        fh.write(text)
    alllib.update(lib.syms)
    stats["files"] = [out_path] + [os.path.join(out_dir, f) for _b, f, _u in sheets] + write_project(out_dir, stem, alllib)
    stats["inventory"] = [{"name": b["name"], "refs": len(b["refs"]), "how": b["how"], "anchor": b["anchor"]}
                          for b in inv["blocks"]]
    return stats


def write_project(out_dir, stem, syms):
    """The drawn symbols as a project library (+ sym-lib-table and a minimal .kicad_pro) so KiCad and ERC resolve
    `fluxplace:` and the embedded copies match their library. Returns the written paths."""
    libf = "%s-fluxplace.kicad_sym" % stem
    body = ['(kicad_symbol_lib (version 20231120) (generator "fluxplace")']
    for lid in sorted(syms):
        t = syms[lid]
        body.append(t.replace('(symbol "%s"' % lid, '(symbol "%s"' % lid.split(":", 1)[1], 1))
    body.append(")")
    paths = [os.path.join(out_dir, libf), os.path.join(out_dir, "sym-lib-table"), os.path.join(out_dir, stem + ".kicad_pro")]
    with open(paths[0], "w") as fh:
        fh.write("\n".join(body) + "\n")
    with open(paths[1], "w") as fh:
        fh.write('(sym_lib_table\n  (version 7)\n  (lib (name "fluxplace")(type "KiCad")(uri "${KIPRJMOD}/%s")(options "")'
                 '(descr "symbols drawn by fluxplace schematic --layout blocks"))\n)\n' % libf)
    if not os.path.exists(paths[2]):
        with open(paths[2], "w") as fh:
            json.dump({"meta": {"filename": stem + ".kicad_pro", "version": 1}}, fh, indent=2)
    return paths


# ------------------------------------------------------------------ gate
def _cli(args, timeout=900):
    return _procs.run(args, capture_output=True, text=True, timeout=timeout)


def export_netlist(sch, kicad_cli="kicad-cli"):
    out = os.path.join(tempfile.mkdtemp(prefix="fpnet"), "net.net")
    r = _cli([kicad_cli, "sch", "export", "netlist", "--format", "kicadsexpr", "--output", out, sch])
    if not os.path.exists(out):
        raise RuntimeError("kicad-cli produced no netlist: %s" % (r.stderr or r.stdout)[:400])
    return open(out, errors="ignore").read()


def parse_netlist(text):
    comps = {}
    for m in re.finditer(r'\(comp\s+\(ref\s+"([^"]+)"\)(.*?)(?=\(comp\s+\(ref|\(libparts|\Z)', text, re.S):
        body = m.group(2)
        v = re.search(r'\(value\s+"((?:[^"\\]|\\.)*)"\)', body)
        f = re.search(r'\(footprint\s+"((?:[^"\\]|\\.)*)"\)', body)
        un = lambda s: s.replace('\\"', '"').replace("\\\\", "\\")
        comps[m.group(1)] = {"value": un(v.group(1)) if v else "", "fp": un(f.group(1)) if f else ""}
    nets = {}
    i = text.find("(nets")
    for m in re.finditer(r'\(net\s+\(code\s+"?\d+"?\)\s+\(name\s+"((?:[^"\\]|\\.)*)"\)(.*?)(?=\(net\s+\(code|\Z)', text[i:], re.S):
        nodes = set(re.findall(r'\(node\s+\(ref\s+"([^"]+)"\)\s+\(pin\s+"([^"]+)"\)', m.group(2)))
        if nodes:
            nets[m.group(1).replace('\\"', '"')] = nodes
    return comps, nets


_ESC = {"{slash}": "/", "{backslash}": "\\", "{lt}": "<", "{gt}": ">", "{colon}": ":", "{dblquote}": '"', "{brace}": "{"}


def unescape(name):
    """KiCad escapes some characters of label text in net names ({slash} for /)."""
    for k, v in _ESC.items():
        name = name.replace(k, v)
    return name


def compare(M, text, sheet_names=()):
    comps, got = parse_netlist(text)
    want = {n: {(r, str(p)) for r, p in conns} for n, conns in M["nets"].items()}
    nc = {(r, p) for r, c in M["comps"].items() for p, _n in c["nc"]}
    norm = {}
    pref = sorted(("/%s/" % s for s in sheet_names), key=len, reverse=True)
    for name, nodes in got.items():
        n = name
        for p in pref:
            if n.startswith(p):
                n = n[len(p):]
                break
        else:
            n = n.lstrip("/")
        n = unescape(n)
        if n.startswith("unconnected-(") and len(nodes) == 1 and next(iter(nodes)) in nc:
            continue
        if n in norm:
            norm[n] = norm[n] | nodes      # same net name from two sheets would be one net: flag below
            norm.setdefault("__dup__", set()).add(n)
        else:
            norm[n] = nodes
    dups = sorted(norm.pop("__dup__", set()))
    missing = sorted(set(want) - set(norm))
    extra = sorted(set(norm) - set(want))
    diff = []
    for n in sorted(set(want) & set(norm)):
        if want[n] != norm[n]:
            diff.append({"net": n, "only_in_spec": sorted(want[n] - norm[n])[:6], "only_in_sch": sorted(norm[n] - want[n])[:6]})
    cmiss = sorted(r for r in M["comps"] if r not in comps)
    cextra = sorted(r for r in comps if r not in M["comps"] and not r.startswith("#"))
    cdiff = []
    for r, c in sorted(M["comps"].items()):
        g = comps.get(r)
        if g and ((g["value"] if g["value"] != "~" else "") != c["value"] or (c["fp"] and g["fp"] != c["fp"])):
            cdiff.append({"ref": r, "spec": [c["value"], c["fp"]], "sch": [g["value"], g["fp"]]})
    ok = not (missing or extra or diff or cmiss or cextra or cdiff or dups)
    return {"ok": ok, "verdict": "PASS" if ok else "FAIL",
            "nets_spec": len(want), "nets_sch": len(norm), "pins_spec": sum(len(v) for v in want.values()),
            "pins_sch": sum(len(v) for v in norm.values()), "components_spec": len(M["comps"]),
            "components_sch": len([r for r in comps if not r.startswith("#")]), "nc_declared": len(nc),
            "missing_nets": missing[:20], "extra_nets": extra[:20], "differing_nets": diff[:20], "split_names": dups[:20],
            "missing_components": cmiss[:20], "extra_components": cextra[:20], "differing_components": cdiff[:20]}


def erc(sch, kicad_cli="kicad-cli"):
    out = os.path.join(tempfile.mkdtemp(prefix="fperc"), "erc.json")
    _cli([kicad_cli, "sch", "erc", "--format", "json", "--severity-all", "--output", out, sch])
    if not os.path.exists(out):
        return {"error": "no ERC report"}
    rep = json.load(open(out))
    by, sev = Counter(), Counter()
    for sh in rep.get("sheets", []):
        for v in sh.get("violations", []):
            by[v.get("type", "?")] += 1
            sev[v.get("severity", "?")] += 1
    return {"by_type": dict(sorted(by.items())), "by_severity": dict(sev), "total": sum(by.values())}


def render(sch, out_dir, kicad_cli="kicad-cli", dpi=60):
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(sch))[0]
    pdf = os.path.join(out_dir, stem + ".pdf")
    for f in os.listdir(out_dir):                      # stale pages of an earlier, longer run
        if f.startswith(stem + "-") and f.endswith(".png"):
            os.remove(os.path.join(out_dir, f))
    if os.path.isdir(os.path.join(out_dir, "svg")):
        shutil.rmtree(os.path.join(out_dir, "svg"))
    _cli([kicad_cli, "sch", "export", "pdf", "--output", pdf, sch])
    svgd = os.path.join(out_dir, "svg")
    os.makedirs(svgd, exist_ok=True)
    _cli([kicad_cli, "sch", "export", "svg", "--output", svgd, sch])
    pngs = []
    if os.path.exists(pdf) and shutil.which("pdftoppm"):
        _cli(["pdftoppm", "-r", str(dpi), "-png", pdf, os.path.join(out_dir, stem)])
        pngs = sorted(os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.startswith(stem) and f.endswith(".png"))
    return {"pdf": pdf if os.path.exists(pdf) else None, "svg_dir": svgd,
            "svgs": sorted(os.listdir(svgd)), "pngs": pngs}


def _kicad_dirs(kind):
    """KiCad library roots for footprints/symbols: env overrides first, then the system install."""
    dirs = []
    for k, v in os.environ.items():
        if k.startswith("KICAD") and k.endswith("_" + kind + "_DIR") and os.path.isdir(v):
            dirs.append(v)
    for root in ("/usr/share/kicad", "/usr/local/share/kicad"):
        d = os.path.join(root, kind)
        if os.path.isdir(d):
            dirs.append(d)
    return dirs


def _footprint_pads(lib, name, fp_dirs):
    for d in fp_dirs:
        f = os.path.join(d, lib + ".pretty", name + ".kicad_mod")
        if os.path.isfile(f):
            return set(re.findall(r'\(pad\s+"([^"]*)"', open(f, encoding="utf-8", errors="replace").read()))
    return None


def _symbol_pins(lib, name, sym_dirs):
    """{pin name: pin number} of a library symbol, or None when the library/symbol is not on this machine."""
    for d in sym_dirs:
        f = os.path.join(d, lib + ".kicad_sym")
        if not os.path.isfile(f):
            continue
        text = open(f, encoding="utf-8", errors="replace").read()
        m = re.search(r'\(symbol\s+"%s"' % re.escape(name), text)
        if not m:
            return None
        depth, i = 0, m.start()
        end = len(text)
        for j in range(m.start(), len(text)):
            c = text[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        body = text[m.start():end]
        pins = {}
        for pm in re.finditer(r'\(pin\s.*?\(name\s+"([^"]*)".*?\(number\s+"([^"]*)"', body, re.S):
            pins.setdefault(pm.group(1), pm.group(2))
        return pins
    return None


def pinmap_crosscheck(M, fp_dirs=None, sym_dirs=None):
    """Compare each spec pinmap (pin name -> pad number) with the KiCad library the part names, when one is
    installed here. Footprint: every pinmap pad must be a pad of the footprint. Symbol (spec key `symbol`):
    each pin name must sit on the pad number the symbol gives it. Disagreement -> SCH_PINMAP_MISMATCH (WARN);
    parts with a pinmap but no resolvable library -> counted as SCH_PINMAP_UNCHECKED."""
    fp_dirs = _kicad_dirs("footprints") if fp_dirs is None else fp_dirs
    sym_dirs = _kicad_dirs("symbols") if sym_dirs is None else sym_dirs
    findings, checked, unchecked = [], 0, 0
    for ref, c in sorted(M["comps"].items()):
        pm = c.get("pinmap") or {}
        if not pm:
            continue
        ok_any = False
        fp = c.get("fp") or ""
        if ":" in fp:
            lib, name = fp.split(":", 1)
            pads = _footprint_pads(lib, name, fp_dirs)
            if pads is not None:
                ok_any = True
                bad = sorted(p for p in set(pm.values()) if str(p) not in pads)
                if bad:
                    findings.append({"level": "WARN", "code": "SCH_PINMAP_MISMATCH", "ref": ref,
                                     "msg": f"pinmap pads {bad} are not pads of footprint {fp}"})
        sym = c.get("symbol") or ""
        if ":" in sym:
            lib, name = sym.split(":", 1)
            sp = _symbol_pins(lib, name, sym_dirs)
            if sp is not None:
                ok_any = True
                for pin, pad in sorted(pm.items()):
                    if pin in sp and str(sp[pin]) != str(pad):
                        findings.append({"level": "WARN", "code": "SCH_PINMAP_MISMATCH", "ref": ref,
                                         "msg": f"pin {pin} is number {sp[pin]} in symbol {sym}, spec says pad {pad}"})
        if ok_any:
            checked += 1
        else:
            unchecked += 1
    return {"checked": checked, "unchecked": unchecked, "findings": findings}


def verified_statement(netlist, pinmap):
    """The gate report, saying exactly what was verified (never implying the part's pinmap was)."""
    lines = []
    if netlist.get("ok"):
        lines.append("netlist: drawing matches spec netlist")
    else:
        lines.append("netlist: drawing does NOT match spec netlist")
    lines.append("spec pinmap NOT checked against the part here - see review PINMAP_* codes")
    if pinmap.get("findings"):
        for f in pinmap["findings"][:10]:
            lines.append("  %s %s: %s" % (f["code"], f["ref"], f["msg"]))
    lines.append("pinmap cross-check: %d part(s) compared with installed KiCad libraries, "
                 "SCH_PINMAP_UNCHECKED %d" % (pinmap.get("checked", 0), pinmap.get("unchecked", 0)))
    return "\n".join(lines)


def gate(M, root, kicad_cli="kicad-cli", render_dir=None, sheet_names=()):
    from . import schlegible as LG
    res = {}
    try:
        res["netlist"] = compare(M, export_netlist(root, kicad_cli), sheet_names)
    except Exception as e:
        res["netlist"] = {"ok": False, "verdict": "FAIL", "error": str(e)}
    res["erc"] = erc(root, kicad_cli)
    res["legibility"] = LG.check_tree(root)
    res["pinmap"] = pinmap_crosscheck(M)
    res["verified"] = verified_statement(res["netlist"], res["pinmap"])
    if render_dir:
        res["render"] = render(root, render_dir, kicad_cli)
    res["ok"] = bool(res["netlist"].get("ok")) and res["legibility"]["total"] == 0
    return res
