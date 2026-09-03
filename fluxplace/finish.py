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

__all__ = ["route_nets"]


def _find_jar(jar=None):
    if jar and os.path.exists(jar):
        return jar
    for cand in (os.environ.get("FREEROUTING_JAR", ""),
                 os.path.expanduser("~/freerouting.jar"),
                 os.path.expanduser("~/tools/freerouting-2.3.0.jar"),
                 "/opt/freerouting/freerouting.jar"):
        if cand and os.path.exists(cand):
            return cand
    import glob
    hits = sorted(glob.glob(os.path.expanduser("~/tools/freerouting*.jar")))
    return hits[-1] if hits else None


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
        r = subprocess.run(["java", "-Djava.awt.headless=true", "-Xss256m", "-jar", jar,
                            "-de", dsn, "-do", ses, "-mp", str(passes)],
                           capture_output=True, timeout=timeout, env=env, text=True)
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
            v.SetViaType(t.GetViaType())
            v.SetWidth(t.GetWidth(t.TopLayer()))
            v.SetDrill(t.GetDrill())
            v.SetLayerPair(t.TopLayer(), t.BottomLayer())
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
