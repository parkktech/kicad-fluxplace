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


def upload_package(board, out, project_dir=None, log=print):
    """Assemble the ECAD upload set (external audit/re-route services parse
    these for component relationships): the routed board renamed to the
    project's stem + the project's .kicad_pro, .kicad_dru and every schematic
    sheet. Deliberately EXCLUDES .kicad_prl — it is per-user UI state, not
    design data, and the one upload that included it failed to parse (CM5 v8).
    Returns the list of files written."""
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
        if f.endswith((".kicad_pro", ".kicad_sch", ".kicad_dru")):
            dst = os.path.join(out, f)
            shutil.copy2(os.path.join(project_dir, f), dst)
            written.append(dst)
    # a .kicad_dru beside the routed board is the pipeline's CURRENT rule set
    # (per-run escape zones); it outranks any project-dir copy
    board_dru = os.path.splitext(board)[0] + ".kicad_dru"
    if os.path.exists(board_dru):
        dst = os.path.join(out, stem + ".kicad_dru")
        shutil.copy2(board_dru, dst)
        if dst not in written:
            written.append(dst)
    # the package IS what we just wrote: remove every stale KiCad file left
    # over from earlier versions (.kicad_prl, renamed sheets, dead boards)
    keep = {os.path.basename(f) for f in written}
    for f in sorted(os.listdir(out)):
        if ".kicad_" in f and f not in keep:
            os.unlink(os.path.join(out, f))
            log(f"  removed stale {f} from package")
    log(f"upload package: {len(written)} files -> {out}")
    return written
