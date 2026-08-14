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
import subprocess


def _run(args, log):
    log("  $ " + " ".join(args))
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        log("    ! " + (r.stderr or r.stdout or "failed").strip()[:300])
    return r.returncode == 0


def emit(board, out, kicad_cli="kicad-cli", layers=None, log=print):
    """Write the full fab package for `board` under `out/`. Returns a dict summary
    (drc verdict, files written). Never raises on a single stage failing — it records
    the failure in the manifest so the engineer sees exactly what did and didn't emit."""
    os.makedirs(out, exist_ok=True)
    gdir = os.path.join(out, "gerbers"); os.makedirs(gdir, exist_ok=True)
    ddir = os.path.join(out, "drill"); os.makedirs(ddir, exist_ok=True)
    pdir = os.path.join(out, "place"); os.makedirs(pdir, exist_ok=True)
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
    # pick-and-place, both sides, CSV, mm
    done["place"] = _run([kicad_cli, "pcb", "export", "pos",
                         "--output", os.path.join(pdir, "pos.csv"),
                         "--format", "csv", "--units", "mm", "--side", "both", board], log)
    # DRC — the go/no-go the engineer checks first
    drc_path = os.path.join(out, "drc.json")
    _run([kicad_cli, "pcb", "drc", "--format", "json", "--severity-error",
          "--output", drc_path, board], log)
    verdict, nviol, nunc = "UNKNOWN", None, None
    if os.path.exists(drc_path):
        try:
            d = json.load(open(drc_path))
            nviol = len(d.get("violations", []))
            nunc = len(d.get("unconnected_items", []))
            verdict = "PASS" if (nviol == 0 and nunc == 0) else "REVIEW"
        except Exception:
            pass
    done["drc"] = verdict

    man = os.path.join(out, "MANIFEST.txt")
    with open(man, "w") as f:
        f.write("fluxplace fab package\n")
        f.write(f"source board : {os.path.basename(board)}\n")
        f.write(f"DRC verdict  : {verdict}"
                + (f"  ({nviol} violations, {nunc} unconnected)" if nviol is not None else "")
                + "\n")
        f.write("stages       : " + ", ".join(f"{k}={'ok' if v is True or v=='PASS' else v}"
                                               for k, v in done.items()) + "\n")
        f.write("layout       : gerbers/  drill/  place/  drc.json\n")
        f.write("fab target   : JLCPCB 4/6-layer standard (0.09mm min trace/space)\n")
        f.write("NOTE         : engineer review pass expected before release — "
                "check DRC, silk legibility, and any fine-pitch escape zones.\n")
    log(f"fab package -> {out}  (DRC {verdict})")
    return {"out": out, "drc": verdict, "violations": nviol,
            "unconnected": nunc, "stages": done}


# --------------------------------------------------------------- delivery
# Two different humans consume this output and they must not be handed the
# same bundle:
#   * the fab's CAM/PCB engineer wants ONLY manufacturing data, uploaded as
#     one zip to the quote page;
#   * whoever places the order wants something readable WITHOUT unzipping
#     anything — the settings, the note to paste, the flags.
# Mixing them is how a harness BOM ends up in a Gerber upload, or how the
# person ordering never reads the brief because it was buried in the zip.
CAM_ONLY = ("gerbers", "drill", "place", "drc.json", "MANIFEST.txt")


def deliver(fab_dir, out_dir, name, docs=(), extras=(), log=print):
    """Split a fab package into `out_dir`: a CAM-only zip plus loose docs.

    fab_dir : an emit() output directory
    name    : zip basename, e.g. 'utv-comms-v1.3-fab'
    docs    : human-facing files (brief .md/.docx, README) copied loose
    extras  : commercial files (BOMs) copied loose — NOT put in the zip
    """
    import shutil
    import tempfile
    import zipfile

    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, name + ".zip")
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, name)
        os.makedirs(root)
        packed = []
        for item in CAM_ONLY:
            src = os.path.join(fab_dir, item)
            if not os.path.exists(src):
                continue
            dst = os.path.join(root, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            packed.append(item)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for dirpath, _, files in os.walk(root):
                for fn in files:
                    full = os.path.join(dirpath, fn)
                    z.write(full, os.path.relpath(full, tmp))
    loose = []
    for f in list(docs) + list(extras):
        if not f or not os.path.exists(f):
            continue
        # a doc that already lives in the delivery folder (re-running deliver on
        # a package whose brief is kept there) must not be copied onto itself
        if not os.path.samefile(os.path.dirname(os.path.abspath(f)), out_dir):
            shutil.copy2(f, out_dir)
        loose.append(os.path.basename(f))
    log(f"delivery -> {out_dir}")
    log(f"  zip (fab CAM only): {os.path.basename(zip_path)}  [{', '.join(packed)}]")
    log(f"  loose (for the buyer): {', '.join(loose) if loose else '(none)'}")
    return {"zip": zip_path, "packed": packed, "loose": loose}
