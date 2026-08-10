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
            CONS.inject_netclasses(pro, cons, log=log)
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
