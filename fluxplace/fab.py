"""Fab package emitter — the last stage of the automated pipeline.

Turns a routed, DRC-clean .kicad_pcb into a build-quality manufacturing package a
human engineer opens, sanity-checks, and tweaks before release:

    <out>/gerbers/   all copper + soldermask + silkscreen + paste + Edge.Cuts
    <out>/drill/     PTH/NPTH Excellon + drill map + report
    <out>/place/     pick-and-place CSV (top + bottom)
    <out>/drc.json   design-rule report (and a one-line PASS/FAIL summary)
    <out>/MANIFEST.txt  what's here, the DRC verdict, and the fab settings used

Pure subprocess wrapping of `kicad-cli` (KiCad 8/9/10) — no pcbnew import — so it runs
anywhere kicad-cli is on PATH. Defaults target JLCPCB 4/6-layer; every setting is
surfaced in the manifest so the engineer can see exactly what was assumed.
"""
import json
import os
import re
import shutil
import subprocess

from . import fabxcheck


# Extra manufacturing exports. name -> (kicad-cli subcommand, subfolder, file name
# for a stem). Subfolders are fluxplace-owned like gerbers/ and are wiped on every
# cut; deliver() never zips them (see [deliver] include_formats).
FORMATS = {
    "odb":     ("odb",     "odb",     "{stem}_odb.zip"),
    "ipc2581": ("ipc2581", "ipc2581", "{stem}.xml"),
    "ipcd356": ("ipcd356", "netlist", "{stem}.d356"),
}
DEFAULT_FORMATS = tuple(FORMATS)


def supported_formats(kicad_cli="kicad-cli"):
    """The FORMATS keys this kicad-cli advertises under `pcb export --help`."""
    try:
        r = subprocess.run([kicad_cli, "pcb", "export", "--help"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return set()
    subs = set(re.findall(r"^\s+(\w+)\s{2,}", (r.stdout or "") + (r.stderr or ""), re.M))
    return {f for f, (sub, _, _) in FORMATS.items() if sub in subs}
from . import gerbercheck as GC


def _run(args, log):
    log("  $ " + " ".join(args))
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        log("    ! " + (r.stderr or r.stdout or "failed").strip()[:300])
    return r.returncode == 0


def _reset_dir(path):
    """Wipe a fluxplace-owned export subdir before a fresh cut. gerbers/,
    drill/ and place/ hold ONLY files fab.emit() itself generated on some
    previous run — never anything a person dropped there — so a full clear
    is safe, and it is the only thing that guarantees a re-cut can't leave
    a file from a prior board STATE behind. Measured 2026-09-08: a blind-via
    board re-cut to through-vias left the old back-In2/In3 drill files (and
    their gerberX2 map) sitting in drill/ — kicad-cli only ever WRITES the
    files the current board needs, it never deletes ones a past export left
    that the current board no longer produces — and deliver() zipped the
    stale files into the PCBWay gerber upload right along with the real
    ones."""
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)


def _listfiles(d):
    return sorted(f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))


def emit(board, out, kicad_cli="kicad-cli", layers=None, log=print, formats=None, cons=None):
    """Write the full fab package for `board` under `out/`. Returns a dict summary
    (drc verdict, files written). Never raises on a single stage failing — it records
    the failure in the manifest so the engineer sees exactly what did and didn't emit."""
    os.makedirs(out, exist_ok=True)
    gdir = os.path.join(out, "gerbers"); _reset_dir(gdir)
    ddir = os.path.join(out, "drill"); _reset_dir(ddir)
    pdir = os.path.join(out, "place"); _reset_dir(pdir)
    done = {}

    # gerbers: all fab layers, Protel extensions, no board-name in filenames (JLC-friendly)
    done["gerbers"] = _run([kicad_cli, "pcb", "export", "gerbers",
                            "--output", gdir, "--no-x2", "--subtract-soldermask",
                            board], log)
    # drill: Excellon, PTH+NPTH merged off (separate), map for review
    done["drill"] = _run([kicad_cli, "pcb", "export", "drill",
                         "--output", ddir + os.sep, "--format", "excellon",
                         "--drill-origin", "plot", "--excellon-separate-th",
                         "--generate-map", "--map-format", "gerberx2", board], log)
    # independent re-read of the exported copper + drill (gerbercheck.py)
    gerber = GC.run_stage(out, board, cons=cons, log=log)
    done["gerbercheck"] = gerber["word"]

    # pick-and-place, both sides, CSV, mm. --exclude-dnp because this file is a
    # BUILD INSTRUCTION: a part flagged do-not-populate must not be handed to the
    # machine, and an assembler who fits one is following our own data.
    done["place"] = _run([kicad_cli, "pcb", "export", "pos",
                         "--output", os.path.join(pdir, "pos.csv"),
                         "--format", "csv", "--units", "mm", "--side", "both",
                          "--exclude-dnp", board], log)
    # FAB_BOM_POS_MISMATCH: every on-board footprint that is not DNP/excluded
    # must be in the .pos exactly once, with matching side and rotation.
    # Uses the position file this run just wrote, so it can never check stale data.
    pos_path = os.path.join(pdir, "pos.csv")
    if os.path.isfile(pos_path):
        try:
            xcheck = fabxcheck.check(board, pos=pos_path)
        except Exception as e:      # a parse failure is a FAIL, not a crash
            xcheck = [f"{fabxcheck.CODE}: cross-check could not run: {e}"]
    else:
        xcheck = [f"{fabxcheck.CODE}: no position file was produced"]
    for m in xcheck:
        log("FAIL " + m)

    # extra formats (ODB++, IPC-2581, IPC-D-356). formats=None -> every format
    # this kicad-cli supports. A format that fails is a FAIL line, never silent.
    fmt_files, fmt_status = {}, {}
    supported = supported_formats(kicad_cli)
    if formats is None:
        formats = [f for f in DEFAULT_FORMATS if f in supported]
    stem = os.path.splitext(os.path.basename(board))[0]
    for name in formats:
        if name not in FORMATS:
            fmt_status[name] = "FAIL (unknown format)"
            log(f"FAIL fab format {name}: unknown format (known: {', '.join(FORMATS)})")
            continue
        if name not in supported:
            fmt_status[name] = "FAIL (not in this kicad-cli)"
            log(f"FAIL fab format {name}: kicad-cli {kicad_cli} has no `pcb export {FORMATS[name][0]}`")
            continue
        sub, sdir, pattern = FORMATS[name]
        fdir = os.path.join(out, sdir); _reset_dir(fdir)
        target = os.path.join(fdir, pattern.format(stem=stem))
        ok = _run([kicad_cli, "pcb", "export", sub, "--output", target, board], log)
        if ok and os.path.isfile(target) and os.path.getsize(target) > 0:
            fmt_status[name] = "ok"
            fmt_files[sdir] = _listfiles(fdir)
        else:
            fmt_status[name] = "FAIL"
            log(f"FAIL fab format {name}: export produced no output")
    done.update(fmt_status)

    # DRC — the go/no-go the engineer checks first
    drc_path = os.path.join(out, "drc.json")
    _run([kicad_cli, "pcb", "drc", "--format", "json", "--severity-error",
          "--output", drc_path, board], log)
    # A verdict that does not state its own scope is marketing, not engineering.
    # An outside reviewer had to point out that a board signed off as "0 DRC at
    # all severities" had 13 of 62 rules set to `ignore` — and a rule set to
    # ignore is reported at NO severity, so the clean result was true and narrow
    # at once. KiCad writes `ignored_checks` into this very report; we were not
    # reading it while printing PASS into the package that ships to the fab.
    verdict, nviol, nunc = "UNKNOWN", None, None
    ignored, scope_note = [], ""
    if os.path.exists(drc_path):
        try:
            d = json.load(open(drc_path))
            nviol = len(d.get("violations", []))
            nunc = len(d.get("unconnected_items", []))
            ignored = [c.get("key") for c in d.get("ignored_checks", []) if c.get("key")]
            verdict = "PASS" if (nviol == 0 and nunc == 0) else "REVIEW"
            if ignored:
                verdict += " (%d check(s) NOT evaluated)" % len(ignored)
                scope_note = ("checks NOT evaluated (set to `ignore` in the project):\n"
                              + "\n".join("               - %s" % k for k in sorted(ignored)))
            else:
                scope_note = "scope        : every rule evaluated; no checks ignored"
        except Exception:
            pass
    done["drc"] = verdict

    # The exact files on disk after this export — not what kicad-cli was
    # ASKED to produce, what it actually left behind. The subdirs were wiped
    # by _reset_dir() above, so this list is complete and current; it is
    # also what deliver() zips, so a stale file can never reach the fab
    # (it would have to appear in this listing, which means this export
    # wrote it moments ago).
    files = {"gerbers": _listfiles(gdir), "drill": _listfiles(ddir),
             "place": _listfiles(pdir)}

    man = os.path.join(out, "MANIFEST.txt")
    with open(man, "w") as f:
        f.write("fluxplace fab package\n")
        f.write(f"source board : {os.path.basename(board)}\n")
        f.write(f"DRC verdict  : {verdict}"
                + (f"  ({nviol} violations, {nunc} unconnected)" if nviol is not None else "")
                + (f"\n{scope_note}" if scope_note else "")
                + "\n")
        f.write("stages       : " + ", ".join(f"{k}={'ok' if v is True or v=='PASS' else v}"
                                               for k, v in done.items()) + "\n")
        f.write("layout       : gerbers/  drill/  place/  drc.json\n")
        f.write("checks       : " + (f"{fabxcheck.CODE}=PASS\n" if not xcheck else
                f"{fabxcheck.CODE}=FAIL ({len(xcheck)})\n"))
        for m in xcheck:
            f.write(f"              - {m}\n")
        f.write("fab target   : JLCPCB 4/6-layer standard (0.09mm min trace/space)\n")
        f.write("NOTE         : engineer review pass expected before release — "
                "check DRC, silk legibility, and any fine-pitch escape zones.\n")
        # Machine-readable file list — deliver() parses these "FILE" lines
        # and zips exactly them, never a directory glob, so this manifest
        # is the one and only source of truth for what ships.
        for section in ("gerbers", "drill", "place"):
            for name in files[section]:
                f.write(f"FILE         : {section}/{name}\n")
        # extra formats: every file with its size, so a reviewer sees what each
        # export actually produced (and deliver() can pick them up by section)
        for sdir, names in sorted(fmt_files.items()):
            for name in names:
                size = os.path.getsize(os.path.join(out, sdir, name))
                f.write(f"FILE         : {sdir}/{name}  ({size} bytes)\n")
    log(f"fab package -> {out}  (DRC {verdict})")
    files.update(fmt_files)
    return {"out": out, "drc": verdict, "violations": nviol,
            "unconnected": nunc, "stages": done, "files": files,
            "xcheck": xcheck, "gerber": gerber, "gerber_blocking": gerber["blocking"]}


# --------------------------------------------------------------- delivery
# Two different humans consume this output and they must not be handed the
# same bundle:
#   * the fab's CAM/PCB engineer wants ONLY manufacturing data, uploaded as
#     one zip to the quote page;
#   * whoever places the order wants something readable WITHOUT unzipping
#     anything — the settings, the note to paste, the flags.
# Mixing them is how a harness BOM ends up in a Gerber upload, or how the
# person ordering never reads the brief because it was buried in the zip.
# What belongs in the GERBER upload. Note `place` is NOT here: PCBWay's assembly
# page has a SEPARATE centroid upload field, and a pick-and-place buried in the
# gerber zip is a file their assembly desk never sees. drc.json and MANIFEST stay
# — PCBWay explicitly says fab-related documents belong inside the gerber zip.
CAM_ONLY = ("gerbers", "drill", "drc.json", "MANIFEST.txt")
CENTROID = ("place", "pos.csv")


def _manifest_files(fab_dir):
    """Parse the "FILE : <section>/<name>" lines an emit() MANIFEST.txt
    carries -> {section: [name, ...]}. The one source of truth for what a
    fab package actually contains: deliver() must build the zip from this,
    never from os.listdir()/copytree on the section directory, or a file
    left behind from a state emit() didn't clean (or dropped there by hand)
    ships to the fab silently. Returns {} for a pre-2026-09-08 package with
    no FILE lines — the caller falls back to a directory listing for those."""
    man = os.path.join(fab_dir, "MANIFEST.txt")
    out = {}
    if not os.path.exists(man):
        return out
    for line in open(man):
        if not line.startswith("FILE"):
            continue
        _, _, rel = line.partition(":")
        rel = rel.split("  (")[0].strip()
        section, sep, name = rel.partition("/")
        if sep:
            out.setdefault(section, []).append(name)
    return out


# Section folders the extra formats live in (see FORMATS). Loose by default.
FORMAT_SECTIONS = tuple(sdir for _, sdir, _ in FORMATS.values())


def deliver(fab_dir, out_dir, name, docs=(), extras=(), centroid_name=None,
            log=print, include_formats=()):
    """Split a fab package into `out_dir`: a CAM-only zip plus loose files.

    fab_dir       : an emit() output directory
    name          : zip basename, e.g. '1-GERBERS-utv-comms-v1.3'
    docs          : human-facing files (brief .md/.docx, README) copied loose
    extras        : files copied loose — either a path, or (path, new_name) when
                    the upload slot wants a specific filename
    centroid_name : filename to copy place/pos.csv out under, for the separate
                    centroid upload. None keeps the old single-zip behaviour.
    include_formats : format folders (odb, ipc2581, netlist) to put INSIDE the
                    zip. Default none: they are copied loose into out_dir/<folder>/
                    beside the zip, never inside it.
    """
    import tempfile
    import zipfile

    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, name + ".zip")
    manifest = _manifest_files(fab_dir)
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, name)
        os.makedirs(root)
        packed = []
        items = list(CAM_ONLY) if centroid_name else list(CAM_ONLY) + ["place"]
        items += [s for s in include_formats if s in FORMAT_SECTIONS]
        for item in items:
            src = os.path.join(fab_dir, item)
            if not os.path.exists(src):
                continue
            dst = os.path.join(root, item)
            if os.path.isdir(src):
                # Manifest-driven copy: only files emit() actually wrote on
                # its last run, never a directory glob. Falls back to a
                # listing only for a package with no FILE lines (pre-fix).
                names = manifest.get(item)
                if names is None:
                    names = _listfiles(src)
                os.makedirs(dst, exist_ok=True)
                for fname in names:
                    fsrc = os.path.join(src, fname)
                    if os.path.isfile(fsrc):
                        shutil.copy2(fsrc, os.path.join(dst, fname))
            else:
                shutil.copy2(src, dst)
            packed.append(item)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for dirpath, _, files in os.walk(root):
                for fn in files:
                    full = os.path.join(dirpath, fn)
                    z.write(full, os.path.relpath(full, tmp))
    centroid = None
    if centroid_name:
        src = os.path.join(fab_dir, *CENTROID)
        if os.path.exists(src):
            centroid = os.path.join(out_dir, centroid_name)
            shutil.copy2(src, centroid)

    loose = []
    for sec in FORMAT_SECTIONS:
        if sec in include_formats or sec not in manifest:
            continue
        ldir = os.path.join(out_dir, sec)
        os.makedirs(ldir, exist_ok=True)
        for fname in manifest[sec]:
            fsrc = os.path.join(fab_dir, sec, fname)
            if os.path.isfile(fsrc):
                shutil.copy2(fsrc, os.path.join(ldir, fname))
        loose.append(sec + "/")
    for f in list(docs) + list(extras):
        rename = None
        if isinstance(f, (tuple, list)):
            f, rename = f
        if not f or not os.path.exists(f):
            continue
        dst = os.path.join(out_dir, rename or os.path.basename(f))
        # a doc that already lives in the delivery folder (re-running deliver on
        # a package whose brief is kept there) must not be copied onto itself
        if not os.path.exists(dst) or not os.path.samefile(f, dst):
            shutil.copy2(f, dst)
        loose.append(os.path.basename(dst))
    log(f"delivery -> {out_dir}")
    log(f"  gerber upload: {os.path.basename(zip_path)}  [{', '.join(packed)}]")
    if centroid:
        log(f"  centroid upload: {os.path.basename(centroid)}")
    log(f"  loose: {', '.join(loose) if loose else '(none)'}")
    return {"zip": zip_path, "packed": packed, "loose": loose, "centroid": centroid}


# ---------------------------------------------------------------------------
# Restored from the origin/main line of development during the 2026-08-19
# merge. These two were written against the same emit() output as deliver()
# above and are unrelated to it: quilter_csvs exports the comprehension tables
# a Quilter-class reviewer consumes, and upload_package is the older
# single-bundle packaging kept for callers that predate the D48/D56 split.
# ---------------------------------------------------------------------------
def quilter_csvs(cons, out, log=print):
    """Emit Quilter Circuit-Comprehension CSVs from the constraints —
    measured: their comprehension step IGNORES KiCad netclasses entirely
    (every pair shown 100R, every rail 500mA on a package whose .kicad_pro
    carried injected classes). Each comprehension table has an Upload CSV
    button; these files feed it. Column labels match their tables."""
    import csv
    written = []
    pairs = (cons or {}).get("pairs", {})
    if pairs:
        p = os.path.join(out, "quilter_diff_pairs.csv")
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Net Name (+)", "Net Name (-)",
                        "Differential Impedance (\u03a9)",
                        "Single-ended Impedance (\u03a9)",
                        "Frequency (GHz)"])
            for group, cfg in sorted(pairs.items()):
                z = int(cfg.get("impedance_diff", 100))
                sp, sn = ("_DP", "_DM") if "USB" in group.upper() \
                    else ("_P", "_N")
                w.writerow([group + sp, group + sn, z, z // 2, 1])
        written.append(p)
    power = (cons or {}).get("power", {})
    if power:
        p = os.path.join(out, "quilter_power_nets.csv")
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Net Name", "Maximum Current (mA)",
                        "Attempt Power Pour?"])
            for net, cfg in sorted(power.items()):
                ma = cfg.get("max_current_ma")
                if not ma:
                    continue
                w.writerow([net, int(ma),
                            "true" if cfg.get("pour") else "false"])
        written.append(p)
    if written:
        log("  quilter comprehension CSVs: "
            + ", ".join(os.path.basename(x) for x in written))
    return written


def upload_package(board, out, project_dir=None, log=print):
    """Assemble the ECAD upload set (external audit/re-route services parse
    these for component relationships): the routed board renamed to the
    project's stem + the project's .kicad_pro and every schematic sheet —
    NOTHING else. Measured against Quilter's uploader: .kicad_prl (per-user
    UI state) and .kicad_dru both come back 'Unsupported file' errors, so the
    set is exactly pcb + pro + sch. Returns the list of files written."""
    import shutil
    project_dir = project_dir or os.path.dirname(os.path.abspath(board))
    os.makedirs(out, exist_ok=True)
    pro = [f for f in os.listdir(project_dir) if f.endswith(".kicad_pro")]
    stem = pro[0][:-len(".kicad_pro")] if pro else \
        os.path.splitext(os.path.basename(board))[0]
    written = []
    dst = os.path.join(out, stem + ".kicad_pcb")
    shutil.copy2(board, dst)
    written.append(dst)
    for f in sorted(os.listdir(project_dir)):
        if f.endswith((".kicad_pro", ".kicad_sch")):
            dst = os.path.join(out, f)
            shutil.copy2(os.path.join(project_dir, f), dst)
            written.append(dst)
    # constraint intent travels IN the project file: inject netclasses from
    # <stem>.constraints.toml so ECAD parsers read 85R pairs and real rail
    # widths instead of guessing 100R/500mA (measured on Quilter)
    ctoml = os.path.join(project_dir, stem + ".constraints.toml")
    pro = os.path.join(out, stem + ".kicad_pro")
    if os.path.exists(ctoml) and os.path.exists(pro):
        try:
            from . import constraints as CONS
            cons = CONS.load(ctoml)
            CONS.inject_netclasses(pro, cons, log=log, board=os.path.join(project_dir, stem + ".kicad_pcb"))
            quilter_csvs(cons, out, log=log)
        except Exception as e:
            log(f"  netclass injection failed ({e}) — package unchanged")
    # the package IS what we just wrote: remove every stale KiCad file left
    # over from earlier versions (.kicad_prl, renamed sheets, dead boards)
    keep = {os.path.basename(f) for f in written}
    for f in sorted(os.listdir(out)):
        if ".kicad_" in f and f not in keep:
            os.unlink(os.path.join(out, f))
            log(f"  removed stale {f} from package")
    log(f"upload package: {len(written)} files -> {out}")
    return written
