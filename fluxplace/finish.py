"""Route a handful of NAMED nets with freerouting, and take only those.

The last-mile grid patcher gives up on a connection that has to cross a
dense fanout; freerouting does not. But a Specctra session is the whole
board's routing, and importing it whole hands the optimizer every net you
already tuned. So: export the DSN (planes declared as power, ses.py), let
freerouting close the open connections, import the session into a SCRATCH
board, and copy back the copper of the requested nets only — after removing
what those nets had (their dangling stubs). DRC-guarded by the caller.
"""
import os
import shutil
import subprocess

__all__ = ["route_nets", "jar_version", "freerouting_cmd"]


def _find_jar(jar=None):
    if jar and os.path.exists(jar):
        return jar
    # FREEROUTING_JAR picks a jar explicitly. The default stays 2.3.0 when it
    # is installed: on the utv-comms V1.5 rip-up trial (2026-11-10, 317
    # connections) 2.3.0 left 77 open and 2.5.0 left 100 — newer was faster
    # and DRC-cleaner but not more complete, so it is opt-in, not the default.
    for cand in (os.environ.get("FREEROUTING_JAR", ""),
                 os.path.expanduser("~/freerouting.jar"),
                 os.path.expanduser("~/tools/freerouting-2.3.0.jar"),
                 "/opt/freerouting/freerouting.jar"):
        if cand and os.path.exists(cand):
            return cand
    import glob
    hits = sorted(glob.glob(os.path.expanduser("~/tools/freerouting*.jar")))
    return hits[-1] if hits else None


def jar_version(jar):
    """(major, minor, patch) from the jar's file name, or None — freerouting
    names its release jars freerouting-X.Y.Z.jar."""
    import re
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", os.path.basename(jar or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def freerouting_cmd(jar, dsn, ses, passes):
    """The headless command line for this jar's version.

    2.5.0 (2026-10) deprecates `-mp` for `--router.autorouter.max_passes` and
    warns unless the GUI is switched off by setting; both forms verified on
    the 2.5.0 jar. Older jars only know `-mp`. -Xss256m: the DSN parser
    recurses per polygon vertex and overflows the default stack on big pours.
    """
    cmd = ["java", "-Djava.awt.headless=true", "-Xss256m", "-jar", jar,
           "-de", dsn, "-do", ses]
    v = jar_version(jar)
    if v and v >= (2, 5, 0):
        return cmd + [f"--router.autorouter.max_passes={int(passes)}",
                      "--gui.enabled=false"]
    return cmd + ["-mp", str(passes)]


def route_nets(board_path, out_path, nets, planes=("In1.Cu", "In4.Cu"), jar=None,
               passes=3, timeout=900, log=print):
    """Returns {"tracks": n, "vias": n, "nets": [...]} or None on failure."""
    import pcbnew
    from . import kicad_io as IO
    from . import ses as SES
    jar = _find_jar(jar)
    if not jar:
        log("    finish: no freerouting jar")
        return None
    work = out_path + ".fr"
    dsn, ses = work + ".dsn", work + ".ses"
    board = pcbnew.LoadBoard(board_path)
    if not IO.export_dsn(board, dsn):
        log("    finish: DSN export failed")
        return None
    SES.declare_power_planes(dsn, list(planes))
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    try:
        from . import routestatus as RS
        r = RS.run_logged(freerouting_cmd(jar, dsn, ses, passes), timeout=timeout,
                          env=env, out=ses, stage="finish")
        if r.returncode != 0:
            log(f"    finish: freerouting exited {r.returncode}: "
                f"{(r.stderr or r.stdout or '').strip()[-160:]}")
    except subprocess.TimeoutExpired:
        log(f"    finish: freerouting hit the {timeout}s cap")
    except FileNotFoundError:
        log("    finish: java not available")
        return None
    if not os.path.exists(ses):
        log("    finish: no session produced")
        return None
    scratch = pcbnew.LoadBoard(board_path)
    if not pcbnew.ImportSpecctraSES(scratch, ses):
        log("    finish: SES import rejected")
        return None
    want = set(nets)
    # drop what the named nets had (stubs), then copy the session's copper
    from . import repair as RP
    for t in list(board.GetTracks()):
        if t.GetNetname() in want:
            RP._remove(board, t)
    nt = nv = 0
    for t in scratch.GetTracks():
        n = t.GetNetname()
        if n not in want:
            continue
        ni = board.FindNet(n)
        if t.GetClass() == "PCB_VIA":
            v = pcbnew.PCB_VIA(board)
            v.SetPosition(t.GetPosition())
            # Always through, F.Cu-B.Cu — never copy freerouting's via type/
            # layer pair verbatim. The DSN via list can offer blind/buried
            # vias and freerouting will pick one on a dense net; nothing
            # downstream (DRC, fab manifest) checks via TYPE, only geometry,
            # so a blind via reaches gerbers on a through-via-only quote.
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetWidth(t.GetWidth(t.TopLayer()))
            v.SetDrill(t.GetDrill())
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            v.SetNet(ni)
            board.Add(v)
            nv += 1
        elif t.GetClass() == "PCB_TRACK":
            s = pcbnew.PCB_TRACK(board)
            s.SetStart(t.GetStart())
            s.SetEnd(t.GetEnd())
            s.SetLayer(t.GetLayer())
            s.SetWidth(t.GetWidth())
            s.SetNet(ni)
            board.Add(s)
            nt += 1
    pcbnew.SaveBoard(out_path, board)
    for f in (dsn, ses):
        try:
            os.unlink(f)
        except OSError:
            pass
    log(f"    finish: {nt} tracks, {nv} vias taken for {sorted(want)}")
    return {"tracks": nt, "vias": nv, "nets": sorted(want)}
