"""The deliver gate for docs/PACKAGING-RULES.md (P1-P8).

check(ctx) -> [{rule, status, reason, waived, warns}]; status is PASS, FAIL or
NOT CHECKED (counts as FAIL for readiness). A problem is satisfied by a
recorded reason: `[packaging] waive = ["P6:REGEX: reason"]` in the constraints
or `--pack-waive`; the rule matches a problem when REGEX matches its text, and
the reason is listed in README-FIRST. P8 debt rows use the same list
(`P8:J3: model debt row ...`).

Hooks owned by other branches are looked up by name; while the function does
not exist the rule line says NOT CHECKED, never PASS:
  P1  fluxplace.review.interface_pair_check(board, cons) -> {"fail": [str], "warn": [str]}
  P2  fluxplace.review.baseline_check(board, prev_board, findings, cons) -> {"fail": [str], "warn": [str]}
  P4  fluxplace.jlcset.check(out_dir, bom_rows, dnp_refs) -> [problem str]
"""
import csv
import glob
import importlib
import json
import os
import re
import shutil
import tempfile

from . import deliverver as DV, procs


def parse_waivers(entries):
    """'P6:REGEX: reason' -> [(rule, regex, reason)]; entries without a reason are returned as bad."""
    ok, bad = [], []
    for e in entries or ():
        m = re.match(r"\s*(P[1-8])\s*:\s*(.+?)\s*:\s*(\S.*)$", e)
        (ok if m else bad).append(m.groups() if m else e)
    return ok, bad


def _hook(mod, fn):
    try:
        return getattr(importlib.import_module("fluxplace." + mod), fn, None)
    except Exception:
        return None


def _resolve(rule, problems, waivers):
    left, waived = [], []
    for p in problems:
        w = next((w for w in waivers if w[0] == rule and re.search(w[1], p)), None)
        (waived if w else left).append((p, w[2]) if w else p)
    return left, waived


def _res(rule, left, waived, ok_reason, warns=()):
    if left:
        return dict(rule=rule, status="FAIL", reason="; ".join(left[:6]) + (" (+%d more)" % (len(left) - 6) if len(left) > 6 else ""),
                    waived=waived, warns=list(warns), all=left)
    return dict(rule=rule, status="PASS", reason=ok_reason + (" (%d waived)" % len(waived) if waived else ""),
                waived=waived, warns=list(warns))


def _nc(rule, why):
    return dict(rule=rule, status="NOT CHECKED", reason=why, waived=[], warns=[])


def _hooked(rule, mod, fn, args, waivers):
    h = _hook(mod, fn)
    if h is None:
        return _nc(rule, "%s.%s not on this branch" % (mod, fn))
    r = h(*args)
    probs = r if isinstance(r, list) else r.get("fail", [])
    left, waived = _resolve(rule, probs, waivers)
    return _res(rule, left, waived, "ok", warns=[] if isinstance(r, list) else r.get("warn", []))


def p1(ctx, W):
    return _hooked("P1", "review", "interface_pair_check", (ctx["board"], ctx["cons"]), W)


def p2(ctx, W):
    f = ctx["findings"]
    if f is None:
        return _nc("P2", "review skipped (--no-review)")
    if ctx.get("prev_board"):
        rp = ctx.get("review_prev")      # review of the prev board with the same spec/constraints/waivers/datasheets (w105's rule)
        try:
            base = rp(ctx["prev_board"]) if rp else None
        except Exception as e:
            return dict(rule="P2", status="FAIL", reason="baseline review of the previous board failed: %s" % str(e)[:100], waived=[], warns=[])
        return _hooked("P2", "review", "baseline_check", (ctx["board"], ctx["prev_board"], f, ctx["cons"], base), W)
    fails = ["%s %s" % (x["code"], " ".join(x.get("refs") or ())) for x in f if x["level"] == "FAIL"]
    left, waived = _resolve("P2", fails, W)
    return _res("P2", left, waived, "first version: plain review, 0 FAIL")


def p3(ctx, W):
    tmp = tempfile.mkdtemp(prefix="fp-p3-")
    d = os.path.join(tmp, "b"); os.makedirs(d)
    stem = os.path.splitext(os.path.basename(ctx["board"]))[0]
    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_prl"):
        s = os.path.join(os.path.dirname(ctx["board"]), stem + ext)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(d, stem + ext))
    out = os.path.join(tmp, "drc.json")
    try:
        procs.run(["nice", "-n", "10", ctx["kicad_cli"], "pcb", "drc", "--severity-all", "--all-track-errors",
                   "--refill-zones", "--format", "json", "-o", out, os.path.join(d, stem + ".kicad_pcb")],
                  capture_output=True, text=True, timeout=900)
        j = json.load(open(out))
    except Exception as e:
        return _nc("P3", "kicad-cli drc did not run: %s" % str(e)[:80])
    viol = [v for v in j.get("violations", []) if v.get("severity") == "error"]
    unc = j.get("unconnected_items", [])
    probs = ["%d DRC error(s), e.g. %s" % (len(viol), viol[0].get("type"))] if viol else []
    probs += ["%d unconnected" % len(unc)] if unc else []
    left, waived = _resolve("P3", probs, W)
    return _res("P3", left, waived, "0 errors, 0 unconnected (all rules, zones refilled)")


def _csv_refs(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    return {r for row in rows for k in ("Refs", "Designators", "Ref") for r in DV.split_refs(row.get(k))}


def p4(ctx, W):
    out, probs = ctx["out"], []
    no_pw = ctx.get("no_pcbway")
    if not no_pw:
        boms = sorted(glob.glob(os.path.join(out, "2-BOM-*.csv")))
        cpls = sorted(glob.glob(os.path.join(out, "3-CENTROID-*.csv")))
        if not (boms and cpls) or not glob.glob(os.path.join(out, "1-GERBERS-*.zip")):
            probs.append("PCBWay set incomplete (gerber zip / BOM / centroid)")
        else:
            cpl_refs = _csv_refs(cpls[0])
            for r in sorted(set(ctx["dnp"]) & cpl_refs, key=DV._refkey):
                probs.append("DNP %s is in the PCBWay centroid" % r)
            for row in ctx["bom_rows"]:
                for r in row["Refs"]:
                    if r in ctx["dnp"] and not DV.is_dnp(row):
                        probs.append("DNP %s is on a BOM line not marked DO NOT POPULATE" % r)
    fc = ctx.get("fabconform")
    if fc is not None:
        cv = fc.get("coverage") or {}
        if not cv.get("ok", True):
            probs.append("fabconform: %s sub-minimum item(s), only %s FIXED / ORDER-OPTION / listed" % (cv.get("raw"), cv.get("listed")))
    left, waived = _resolve("P4", probs, W)
    ok = "%s; DNP %s excluded and listed" % ("PCBWay set n/a (--no-pcbway)" if no_pw else "PCBWay set ok", ", ".join(sorted(ctx["dnp"])) or "none")
    pcbway = _res("P4", left, waived, ok)
    h = _hook("jlcset", "check")
    if h is None:
        r = _nc("P4", "JLCPCB set: fluxplace.jlcset.check not on this branch" + ("; " + pcbway["reason"] if pcbway["status"] == "FAIL" else ""))
        r["waived"] = pcbway["waived"]
        return r
    jl, jw = _resolve("P4", list(h(out, ctx["bom_rows"], ctx["dnp"])), W)
    return _res("P4", left + jl, waived + jw, ("JLCPCB set ok (PCBWay n/a)" if no_pw else "PCBWay + JLCPCB sets ok"))


def p5(ctx, W):
    if not ctx.get("prev_bom"):
        return _res("P5", [], [], "no previous BOM (first version)")
    prev, new = DV.read_bom(ctx["prev_bom"]), ctx["bom_rows"]
    by = {r["MPN"].upper(): r for r in new if r["MPN"]}
    probs = []
    for r in prev:
        n = by.get(r["MPN"].upper()) if r["MPN"] else None
        if n:
            for k in ("Manufacturer", "Notes"):
                if r[k] and not n[k]:
                    probs.append("%s: %s dropped" % (" ".join(r["Refs"]), k))
        elif DV.is_dnp(r):
            if not any(DV.is_dnp(x) and set(x["Refs"]) & set(r["Refs"]) for x in new) and \
                    any(x in ctx["board_refs"] for x in r["Refs"]):
                probs.append("DNP line %s missing" % " ".join(r["Refs"]))
    left, waived = _resolve("P5", probs, W)
    removed = ", ".join(ctx.get("removed") or []) or "none"
    return _res("P5", left, waived, "Manufacturer/Notes/DNP carried; removed: %s" % removed)


def known_open(readme_text):
    m = re.search(r"^KNOWN OPEN[^\n]*\n-+\n(.*?)(?=^\S[^\n]*\n-{3,}\n|\Z)", readme_text or "", re.M | re.S)
    return [re.sub(r"\s+", " ", b).strip() for b in re.split(r"\n\s*- ", "\n" + m.group(1)) if b.strip()] if m else []


def p6(ctx, W):
    out, probs = ctx["out"], []
    def need(pat, what):
        fs = glob.glob(os.path.join(out, pat))
        if not any(os.path.getsize(f) > 0 for f in fs if os.path.isfile(f)):
            probs.append(what if what.endswith(" missing") else "missing: " + what)
    need("assembly-instructions-*.md", "assembly instructions"); need("4-ASSEMBLY-INSTRUCTIONS-*.pdf", "assembly pdf")
    need("PCBWay-Order-Worksheet-*.md", "order worksheet"); need("README-FIRST.txt", "README-FIRST")
    for f in ("SCHEMATIC-*.pdf", "ERC-*.rpt", "NETLIST-*.net", "REVIEW-*.json"):
        need(os.path.join("review", f), "review/" + f)
    for v in ("top", "bottom", "iso"):
        need("*-%s.png" % v, "render " + v)
    need("*.kicad_pcb", "kicad_pcb"); need("*.kicad_sch", "schematic missing"); need("*.kicad_pro", "kicad_pro")
    need("5-3D/*-assembly.step", "assembly STEP"); need("5-3D/*-bare-board.step", "bare-board STEP")
    if os.path.isdir(os.path.join(out, "lib", "footprints")):
        need("lib/footprints/*/*", "project footprint lib")
    for m in ctx.get("missing_datasheets", []):
        probs.append("datasheet missing: " + m)
    readme = os.path.join(out, "README-FIRST.txt")
    rd = open(readme, encoding="utf-8").read() if os.path.isfile(readme) else ""
    if ctx.get("prev_label") and "WHAT CHANGED SINCE" not in rd:
        probs.append("README-FIRST has no what-changed section")
    for item in ctx.get("prev_known_open", []):
        if item[:40] not in re.sub(r"\s+", " ", rd):
            probs.append("KNOWN OPEN item has no status: " + item[:50])
    probs += ctx.get("claims", [])
    left, waived = _resolve("P6", probs, W)
    return _res("P6", left, waived, "all items present, every prior KNOWN OPEN item has a status, text matches BOM")


def p7(ctx, W):
    from . import titleblock as TB, silk as SK
    ver = ((ctx["cons"].get("project") or {}).get("version"))
    if not ver:
        return dict(rule="P7", status="FAIL", reason="constraints have no [project] version", waived=[], warns=[])
    want = SK.norm_version(str(ver).lstrip("vV"))
    probs = []
    rev = TB.read(ctx["board"]).get("rev")
    if TB._parse_rev(rev or "") != want:
        probs.append("title-block rev %r != [project] version %s" % (rev, ver))
    text = open(ctx["board"], encoding="utf-8").read()
    texts = [m.group(1) for m in re.finditer(r'\(gr_text "([^"]*)"[^\n]*?(?:\n[^\n]*?){0,12}?\(layer "[FB]\.SilkS"', text)] + \
            [m.group(1) for m in re.finditer(r'\(gr_text "([^"]*)"', text) if re.search(r"v\d", m.group(1))]
    vs = {SK.norm_version(v) for t in texts for v in re.findall(r"[vV](\d+(?:\.\d+)*)", t)}
    if want not in vs:
        probs.append("no silk text carries version %s (found %s)" % (ver, sorted(vs) or "none"))
    left, waived = _resolve("P7", probs, W)
    return _res("P7", left, waived, "title block and silk = [project] version %s" % ver)


def p8(ctx, W):
    from . import pcbway as PW
    text = open(ctx["board"], encoding="utf-8").read()
    env = {"KIPRJMOD": ctx["out"]}
    for k in ("KICAD10_3DMODEL_DIR", "KICAD9_3DMODEL_DIR", "KISYS3DMOD"):
        env[k] = os.environ.get(k) or "/usr/share/kicad/3dmodels"
    probs = []
    for fp in PW._blocks(text, "footprint"):
        ref = re.search(r'\(property "Reference" "([^"]+)"', fp)
        mods = re.findall(r'\(model "([^"]+)"', fp)
        ref = ref.group(1) if ref else "?"
        if not mods:
            if re.search(r"\(pad ", fp) and not re.search(r"MountingHole|Fiducial|TestPoint", fp[:200]):
                probs.append("%s has no 3D model" % ref)
            continue
        if not any(os.path.isfile(re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), m.group(0)), p)) for p in mods):
            probs.append("%s model %s does not resolve" % (ref, os.path.basename(mods[0])))
    left, waived = _resolve("P8", probs, W)
    return _res("P8", left, waived, "every footprint's model resolves")


CHECKS = (p1, p2, p3, p4, p5, p6, p7, p8)


def check(ctx, waive=()):
    W, bad = parse_waivers(list(waive))
    res = [c(ctx, W) for c in CHECKS]
    for b in bad:
        res.append(dict(rule="--", status="FAIL", reason="waiver without a reason ignored: %s" % b, waived=[], warns=[]))
    return res


def lines(res):
    return ["%s %s %s" % (r["rule"], r["status"], r["reason"]) for r in res]


def ready(res):
    return all(r["status"] == "PASS" for r in res)


def readme_block(res):
    L = lines(res)
    for r in res:
        L += ["  waived %s: %s -- %s" % (r["rule"], p, why) for p, why in r["waived"]]
        L += ["  WARN %s: %s" % (r["rule"], w) for w in r["warns"]]
    L.append("STATUS  " + ("READY" if ready(res) else "NOT-READY (see FAIL / NOT CHECKED lines)"))
    return "\n".join(L)


def mark(out, res):
    p = os.path.join(out, "NOT-READY")
    if ready(res):
        if os.path.exists(p):
            os.unlink(p)
    else:
        open(p, "w").write("\n".join(lines(res)) + "\n")
