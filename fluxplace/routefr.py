"""Whole-board routing with Freerouting (2.5.0 or newer), next to the KiCadRoutingTools path.

`locate()` finds the jar and a Java runtime (flag, env var, discovery; a loud RuntimeError naming every path tried).
`route_board()` does one run:

  DSN copy   pours on signal layers removed (obstacles to a Specctra router; the board keeps them and is refilled),
             plane layers declared `power`, rule widths/clearances raised to the project floor, every existing
             wire and via marked `(type fix)` so fan-out and scripted pairs are not touched
  JVM        threads / passes / job timeout from `[route.freerouting]`, its own java.io.tmpdir, a watchdog that
             kills that one PID and still takes the session when it is on disk
  import     session copper ADDED to the board's own (ses.import_into replace=False); `skip_nets` have no pins in
             the DSN (nothing to route) and anything the session still holds for them is left out
  repair     unlocked tracks below the floor are widened to the net-class width where clearance allows; the ones
             that cannot be widened are listed and fail the run (`subfloor.failed`)
  post       caller's post-step (snap + join gaps), refill, open connections from pcbnew and from kicad-cli DRC

Nothing here knows a board: layers, floors and nets all come from the caller.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import time
import zipfile

from . import routeconn as RC

ENV_JAR, ENV_JAVA = "FLUXPLACE_FREEROUTING_JAR", "FLUXPLACE_JAVA"
MIN_VERSION = (2, 5, 0)
JAR_GLOBS = ("~/tools/freerouting*.jar", "~/freerouting*.jar", "/opt/freerouting/freerouting*.jar")
JAVA_GLOBS = ("~/tools/jdk*/bin/java",)
HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fr_helper.py")
WHY_MIN = "older jars hang at the job timeout and lose the session"


# ------------------------------------------------------------------ locate
def jar_version(jar):
    """(major, minor, patch) from the jar's name (freerouting-X.Y.Z.jar), else its manifest; None when unknown."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", os.path.basename(jar or ""))
    if m:
        return tuple(int(x) for x in m.groups())
    try:
        with zipfile.ZipFile(jar) as z:
            mf = z.read("META-INF/MANIFEST.MF").decode(errors="replace")
        m = re.search(r"Implementation-Version:\s*v?(\d+)\.(\d+)\.(\d+)", mf)
        return tuple(int(x) for x in m.groups()) if m else None
    except (OSError, KeyError, zipfile.BadZipFile):
        return None


def vstr(v):
    return ".".join(map(str, v)) if v else "?"


def _check_version(jar):
    v = jar_version(jar)
    if v is None:
        raise RuntimeError(f"Freerouting jar {jar}: version unknown (name it freerouting-X.Y.Z.jar); "
                           f"{vstr(MIN_VERSION)} or newer is required")
    if v < MIN_VERSION:
        raise RuntimeError(f"Freerouting {vstr(v)} at {jar} is too old: {vstr(MIN_VERSION)} or newer is required ({WHY_MIN})")
    return v


def locate(flag_jar=None, flag_java=None, env=None):
    """-> {"jar","java","version","source","tried"}; raises RuntimeError naming what was tried and what to set."""
    env = os.environ if env is None else env
    tried, jar, src, v = [], None, None, None
    explicit = [(s, p) for s, p in (("flag", flag_jar), ("env " + ENV_JAR, env.get(ENV_JAR))) if p]
    if explicit:
        src, p = explicit[0]                       # an explicit choice is never second-guessed
        p = os.path.abspath(os.path.expanduser(p))
        tried.append(p)
        if not os.path.isfile(p):
            raise RuntimeError(f"Freerouting jar not found: {p} ({src}). Pass --freerouting-jar or set {ENV_JAR}")
        jar, v = p, _check_version(p)
    else:
        old = []
        for g in JAR_GLOBS:
            g = os.path.expanduser(g)
            tried.append(g)
            for p in glob.glob(g):
                pv = jar_version(p)
                if pv and pv >= MIN_VERSION:
                    if v is None or pv > v:
                        jar, v, src = p, pv, "newest install"
                elif pv:
                    old.append(f"{p} ({vstr(pv)})")
        if not jar:
            raise RuntimeError("Freerouting jar not found; tried " + ", ".join(tried) +
                               (f"; too old: {', '.join(old)} ({WHY_MIN})" if old else "") +
                               f" (need {vstr(MIN_VERSION)} or newer). Pass --freerouting-jar or set {ENV_JAR}")
    jtried, java = [], None
    cands = [flag_java, env.get(ENV_JAVA)]
    if not (flag_java or env.get(ENV_JAVA)):
        if env.get("JAVA_HOME"):
            cands.append(os.path.join(env["JAVA_HOME"], "bin", "java"))
        for g in JAVA_GLOBS:
            cands += sorted(glob.glob(os.path.expanduser(g)), reverse=True)
        cands.append(shutil.which("java"))
    for p in cands:
        if p:
            p = os.path.abspath(os.path.expanduser(p))
            jtried.append(p)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                java = p
                break
    if not java:
        raise RuntimeError("Java runtime for Freerouting not found; tried " + (", ".join(jtried) or "PATH") +
                           f". Pass --java or set {ENV_JAVA}")
    return {"jar": jar, "java": java, "version": vstr(v), "source": src, "tried": tried}


# ------------------------------------------------------------------ settings, command
def settings(cons, **over):
    """`[route.freerouting]` keys with defaults; `over` wins when not None."""
    r = dict(((cons or {}).get("route") or {}).get("freerouting") or {})
    cpu = os.cpu_count() or 2
    s = {"threads": int(r.get("threads", max(1, min(8, cpu // 2)))),
         "passes": int(r.get("passes", 100)),
         "fanout": str(r.get("fanout", "auto")).lower(),          # auto: off when the board already has fan-out
         "pairs": str(r.get("pairs", "exclude")).lower(),         # exclude | route (audited, never reported good unaudited)
         "neckdown": bool(r.get("neckdown", False)),              # pad-entry necks below the track width: off
         "budget_s": r.get("budget_s"), "budget_frac": float(r.get("budget_frac", 0.6)),
         "grace_s": float(r.get("grace_s", 180)), "analytics": bool(r.get("analytics", False))}
    for k, v in over.items():
        if v is not None:
            s[k] = v
    if s["fanout"] in ("true", "false"):
        s["fanout"] = "on" if s["fanout"] == "true" else "off"
    if s["fanout"] not in ("auto", "on", "off"):
        raise ValueError(f"[route.freerouting] fanout = {s['fanout']!r}: use auto, on or off")
    if s["pairs"] not in ("exclude", "route"):
        raise ValueError(f"[route.freerouting] pairs = {s['pairs']!r}: use exclude or route")
    s["threads"] = max(1, s["threads"])
    return s


def hms(seconds):
    s = max(1, int(round(seconds)))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def router_flags(S, timeout_s, fanout_on):
    """The Freerouting arguments after `-de/-do` (recorded in the stage record)."""
    f = [f"--router.autorouter.max_passes={int(S['passes'])}", "--gui.enabled=false", "-mt", str(S["threads"]),
         f"--router.job_timeout={hms(timeout_s)}"]
    if not fanout_on:
        f.append("--router.fanout.enabled=false")
    if not S["neckdown"]:
        f.append("--router.automatic_neckdown=false")
    if not S["analytics"]:
        f.append("--usage_and_diagnostic_data.disable_analytics=true")
    return f


def command(loc, dsn, ses, S, timeout_s, tmpdir, fanout_on):
    return [loc["java"], "-Djava.awt.headless=true", f"-Djava.io.tmpdir={tmpdir}", "-Xss256m",
            f"-XX:ActiveProcessorCount={S['threads']}", "-jar", loc["jar"], "-de", dsn, "-do", ses,
            *router_flags(S, timeout_s, fanout_on)]


# ------------------------------------------------------------------ DSN text
def _section(text, key):
    """(start, end) of the top-level `(key ...)` block, or None."""
    m = re.search(r"\n\s*\(%s\b" % re.escape(key), text)
    if not m:
        return None
    i = text.index("(", m.start())
    depth, j, q = 0, i, False
    while j < len(text):
        ch = text[j]
        if ch == '"':
            q = not q
        elif not q:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i, j + 1
        j += 1
    return None


def fix_dsn(text, planes=(), floor_track=0.0, floor_clearance=0.0, skip_nets=()):
    """-> (text, report): planes declared power, rule widths / untyped clearances raised to the floors (mm),
    every wire and via of the wiring section `(type fix)`, the pins of `skip_nets` taken off their nets (the router
    then has nothing to connect there and treats the pads as obstacles; the nets stay open for the caller)."""
    rep = {"planes": [], "planes_missed": [], "widths_raised": 0, "clearances_raised": 0, "fixed": 0, "already_fixed": 0,
           "nets_excluded": 0}
    for name in skip_nets:
        text, k = re.subn(r'(\(net\s+"?%s"?\s*)\(pins[^)]*\)(\s*\))' % re.escape(name), r"\1\2", text, count=1)
        rep["nets_excluded"] += k
    for lay in planes:
        pat = r"(\(layer\s+\"?%s\"?\s*\(type\s+)signal(\s*\))" % re.escape(lay)
        new, n = re.subn(pat, r"\1power\2", text, count=1)
        if n or re.search(r"\(layer\s+\"?%s\"?\s*\(type\s+power\s*\)" % re.escape(lay), text):
            rep["planes"].append(lay)
            text = new
        else:
            rep["planes_missed"].append(lay)
    m = re.search(r"\(unit\s+(\w+)\)", text)
    per_mm = {"um": 1000.0, "mm": 1.0, "mil": 39.3700787, "inch": 0.0393700787}.get(m.group(1) if m else "um", 1000.0)

    def raise_to(key, floor_mm, count_key, body):
        lim = floor_mm * per_mm

        def sub(mo):
            if float(mo.group(2)) + 1e-9 < lim:
                rep[count_key] += 1
                return f"{mo.group(1)}{lim:g}{mo.group(3)}"
            return mo.group(0)
        return re.sub(r"(\(%s\s+)([\d.]+)(\s*\))" % key, sub, body)
    w = _section(text, "wiring")
    head, wiring, tail = (text[:w[0]], text[w[0]:w[1]], text[w[1]:]) if w else (text, "", "")
    if floor_track:
        head = raise_to("width", floor_track, "widths_raised", head)
    if floor_clearance:
        head = raise_to("clearance", floor_clearance, "clearances_raised", head)
    if wiring:
        rep["already_fixed"] = wiring.count("(type fix)")
        wiring, n = re.subn(r"\(type\s+(?:route|protect|normal)\)", "(type fix)", wiring)
        rep["fixed"] = n + rep["already_fixed"]
    return head + wiring + tail, rep


# ------------------------------------------------------------------ log
_TS = re.compile(r"^(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)")
_PASS = re.compile(r"Auto-routing pass #(\d+).*?completed in ([\d.]+) seconds with score ([\d.]+) \((\d+) unrouted and (\d+) violations\)")
_FAN = re.compile(r"Fanout pass #(\d+).*?completed in ([\d.]+) seconds with (\d+) SMD pins fanouted, (\d+) not routed")
_START = re.compile(r"Auto-routing stage started.*?for (\d+) unrouted items")
_STATE = re.compile(r"finished with state: (\w+)")
_VER = re.compile(r"Freerouting v(\d+\.\d+\.\d+)")


def _secs(line):
    m = _TS.match(line)
    return time.mktime(tuple(int(x) for x in m.groups()) + (0, 0, -1)) if m else None


def parse_log(text):
    """-> {version, start_unrouted, passes: [{pass, min, unrouted, violations, score}], fanout: [{pass, min, done, left}],
    state, saved}.  `min` = minutes since the first log line."""
    out = {"version": None, "start_unrouted": None, "passes": [], "fanout": [], "state": None, "saved": False}
    t0 = None
    for ln in text.splitlines():
        t = _secs(ln)
        if t is not None and t0 is None:
            t0 = t
        mins = round((t - t0) / 60.0, 1) if (t is not None and t0 is not None) else None
        m = _PASS.search(ln)
        if m:
            out["passes"].append({"pass": int(m.group(1)), "min": mins, "unrouted": int(m.group(4)),
                                  "violations": int(m.group(5)), "score": float(m.group(3))})
            continue
        m = _FAN.search(ln)
        if m:
            out["fanout"].append({"pass": int(m.group(1)), "min": mins, "done": int(m.group(3)), "left": int(m.group(4))})
            continue
        m = _START.search(ln)
        if m:
            out["start_unrouted"] = int(m.group(1))
        m = _STATE.search(ln)
        if m:
            out["state"] = m.group(1)
        m = _VER.search(ln)
        if m and not out["version"]:
            out["version"] = m.group(1)
        if "Successfully saved output file" in ln:
            out["saved"] = True
    return out


def curve(passes, n=6):
    """[[pass, minutes, unrouted, violations]]: at most `n` points, first and last kept (the state.json form)."""
    rows = [[p["pass"], p["min"], p["unrouted"], p["violations"]] for p in passes]
    if len(rows) <= n:
        return rows
    idx = sorted({round(i * (len(rows) - 1) / (n - 1)) for i in range(n)})
    return [rows[i] for i in idx]


# ------------------------------------------------------------------ JVM
def ses_ok(path):
    try:
        with open(path, errors="replace") as fh:
            s = fh.read()
    except OSError:
        return False
    return "(session" in s and s.rstrip().endswith(")")


def run_jvm(cmd, log_path, timeout_s, grace_s, ses, cwd=None, tick=None, poll=2.0, tick_every=20.0):
    """Run the JVM to its own job timeout (+ grace), then kill THAT pid. -> {rc, killed, hung_after_save, elapsed, pid}.
    The session on disk is used whether or not the process ended by itself."""
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    t0 = time.time()
    res = {"rc": None, "killed": False, "hung_after_save": False, "pid": None}
    with open(log_path, "wb") as lf:
        try:
            p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=cwd, env=env)
        except OSError as e:
            raise RuntimeError(f"cannot start Java ({cmd[0]}): {e}") from e
        res["pid"] = p.pid
        last_tick, saved_at = 0.0, None
        try:
            while True:
                try:
                    res["rc"] = p.wait(timeout=poll)
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.time()
                if tick and now - last_tick >= tick_every:
                    last_tick = now
                    try:
                        tick(parse_log(open(log_path, errors="replace").read()), now - t0)
                    except Exception:                    # noqa: BLE001 - progress is best effort
                        pass
                if saved_at is None and os.path.exists(ses) and ses_ok(ses):
                    saved_at = now
                if saved_at is not None and now - saved_at > min(30.0, max(2.0, grace_s)):
                    res["hung_after_save"] = True         # the session is out and the JVM did not leave
                    break
                if now - t0 > timeout_s + grace_s:
                    break
        finally:
            if p.poll() is None:
                p.kill()                                  # this PID only
                res["killed"] = True
                try:
                    p.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
    res["elapsed"] = round(time.time() - t0, 1)
    return res


# ------------------------------------------------------------------ pcbnew helper
def _helper(cmd, args, workdir, timeout=1800):
    aj = os.path.join(workdir, f"fr-{cmd}.args.json")
    with open(aj, "w") as fh:
        json.dump(args, fh)
    r = subprocess.run([RC.PY, HELPER, cmd, aj], capture_output=True, text=True, timeout=timeout)
    lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
    if r.returncode != 0 or not lines:
        raise RuntimeError(f"freerouting {cmd} step failed: " + (r.stderr or r.stdout)[-300:].strip())
    return json.loads(lines[-1])


def _side(src, dst):
    s = os.path.splitext(src)[0] + ".kicad_pro"
    if os.path.exists(s):
        shutil.copy(s, os.path.splitext(dst)[0] + ".kicad_pro")


def fingerprint(board, workdir, floor=0.0, planes=()):
    return _helper("fingerprint", {"board": board, "floor": floor, "planes": list(planes)}, workdir)


def repair_subfloor(board, workdir, floor, clearance, out=None):
    """-> {subfloor, locked, fixed, failed: [...], widths}: `board` rewritten (to `out`) with the widened tracks."""
    return _helper("widen", {"board": board, "out": out or board, "floor": floor, "clearance": clearance}, workdir)


# ------------------------------------------------------------------ the run
def route_board(board, out, workdir, *, loc, planes, floor_track, floor_clearance, budget_s, S=None, cons=None,
                skip_nets=(), has_fanout=False, conn=None, post=None, tick=None, poll=2.0):
    """One Freerouting run on `board` -> `out`. -> the record (see module doc); `ok` False + `error` on failure,
    in which case `out` is not written.  `subfloor.failed` non-empty = sub-floor copper is still on `out`: the caller
    must fail its stage."""
    S = S or settings(cons)
    os.makedirs(workdir, exist_ok=True)
    tmpdir = os.path.join(workdir, "jvmtmp")
    os.makedirs(tmpdir, exist_ok=True)
    fanout_on = S["fanout"] == "on" or (S["fanout"] == "auto" and not has_fanout)
    flags = router_flags(S, budget_s, fanout_on)
    rec = {"ok": False, "router": "freerouting", "jar": loc["jar"], "java": loc["java"], "version": loc["version"],
           "flags": " ".join(flags), "fanout": "on" if fanout_on else "off", "pairs": S["pairs"],
           "skip_nets": sorted(skip_nets), "budget_s": round(budget_s), "planes": list(planes)}
    t0 = time.time()
    try:
        fp0 = fingerprint(board, workdir, floor_track, planes)
        rec["before"] = fp0
        dsn_board = os.path.join(workdir, "dsn.kicad_pcb")
        _side(board, dsn_board)
        rec["pours"] = _helper("strip", {"board": board, "out": dsn_board, "planes": list(planes)}, workdir)
        dsn, ses, flog = os.path.join(workdir, "in.dsn"), os.path.join(workdir, "out.ses"), os.path.join(workdir, "freerouting.log")
        for f in (dsn, ses):
            if os.path.exists(f):
                os.remove(f)
        ex = _helper("export", {"board": dsn_board, "dsn": dsn}, workdir)
        if not ex.get("ok"):
            raise RuntimeError("Specctra DSN export failed")
        with open(dsn, errors="surrogateescape") as fh:
            text, rep = fix_dsn(fh.read(), planes, floor_track, floor_clearance, skip_nets)
        if rep["planes_missed"]:
            raise RuntimeError("plane layer(s) not found in the DSN, refusing to route on them: " + ", ".join(rep["planes_missed"]))
        with open(dsn, "w", errors="surrogateescape") as fh:
            fh.write(text)
        rec["dsn"] = rep
        rec["prep_s"] = round(time.time() - t0, 1)
        jr = run_jvm(command(loc, dsn, ses, S, budget_s, tmpdir, fanout_on), flog, budget_s, S["grace_s"], ses,
                     cwd=workdir, tick=tick, poll=poll)
        log = parse_log(open(flog, errors="replace").read()) if os.path.exists(flog) else parse_log("")
        rec.update(rc=jr["rc"], killed=jr["killed"], hung_after_save=jr["hung_after_save"], jvm_s=jr["elapsed"],
                   state=log["state"] or ("KILLED" if jr["killed"] else None), passes=log["passes"], fanout_passes=log["fanout"],
                   start_unrouted=log["start_unrouted"], log=flog)
        if log["version"] and log["version"] != loc["version"]:
            rec["version"] = log["version"]
        if not ses_ok(ses):
            tail = ""
            try:
                tail = " | ".join(open(flog, errors="replace").read().strip().splitlines()[-2:])[-240:]
            except OSError:
                pass
            raise RuntimeError(f"Freerouting left no usable session (rc {jr['rc']}, {'killed by the watchdog' if jr['killed'] else 'exited'}"
                               f" after {jr['elapsed']:.0f}s): {tail}")
        t1 = time.time()
        tmp = out + ".fr-import.kicad_pcb"
        _side(board, tmp)
        rec["import"] = _helper("import", {"board": board, "ses": ses, "out": tmp, "skip_nets": sorted(skip_nets)}, workdir)
        rec["subfloor"] = repair_subfloor(tmp, workdir, floor_track, floor_clearance) if floor_track else \
            {"subfloor": 0, "locked": 0, "fixed": 0, "failed": [], "widths": []}
        if post:
            rec["post"] = {k: v for k, v in post(tmp).items() if k in ("snapped", "joined", "reverted", "error")}
            from . import opengate as OG
            OG.refill(tmp)
        fp1 = fingerprint(tmp, workdir, floor_track, planes)
        rec["after"] = fp1
        rec["locked_same"] = fp0["locked"] == fp1["locked"] and fp0["locked_hash"] == fp1["locked_hash"]
        rec["plane_tracks_added"] = fp1["plane_tracks"] - fp0["plane_tracks"]
        if conn:
            rec["open"] = sum(conn(tmp).values())
            try:
                from . import opengate as OG
                rec["open_cli"] = OG._drc(tmp, False)["unconnected"]     # the saved fills, as kicad-cli sees the file
            except Exception as e:                                         # noqa: BLE001
                rec["open_cli"], rec["open_cli_error"] = None, str(e)[:100]
        os.replace(tmp, out)
        _side(board, out)
        for f in (os.path.splitext(tmp)[0] + ".kicad_pro", os.path.splitext(tmp)[0] + ".kicad_prl"):
            if os.path.exists(f) and os.path.splitext(tmp)[0] != os.path.splitext(out)[0]:
                os.remove(f)
        rec["post_s"] = round(time.time() - t1, 1)
        rec["ok"] = True
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        rec["error"] = str(e)[:400]
    rec["elapsed"] = round(time.time() - t0, 1)
    return rec


def summary(rec):
    """One line for the stage record."""
    if not rec.get("ok"):
        return f"freerouting {rec.get('version', '?')} FAILED: {rec.get('error', '?')}"
    ps = rec.get("passes") or []
    sf = rec.get("subfloor") or {}
    s = (f"freerouting {rec['version']} {len(ps)} pass(es) in {rec.get('jvm_s', 0) / 60:.1f} min"
         + (f" ({rec['state']})" if rec.get("state") else "")
         + (f", unrouted {rec.get('start_unrouted')} -> {ps[-1]['unrouted']}" if ps else "")
         + f", +{rec['import']['tracks_added']} tracks +{rec['import']['vias_added']} vias, fan-out {rec['fanout']}")
    if rec.get("skip_nets"):
        s += f", {len(rec['skip_nets'])} pair/RF net(s) left for the targeted step"
    if sf.get("subfloor"):
        s += f", sub-floor {sf['subfloor']}: {sf['fixed']} widened" + (f", {len(sf['failed'])} NOT fixable" if sf["failed"] else "") + \
             (f", {sf['locked']} locked (kept)" if sf.get("locked") else "")
    if not rec.get("locked_same", True):
        s += ", LOCKED COPPER CHANGED"
    if rec.get("plane_tracks_added"):
        s += f", {rec['plane_tracks_added']} track(s) ON A PLANE LAYER"
    return s


def state_record(rec):
    """The small form kept in state.json: jar, version, flags, pass curve [[pass, min, unrouted, violations]], the counts."""
    sf = rec.get("subfloor") or {}
    out = {"jar": rec.get("jar"), "v": rec.get("version"), "flags": rec.get("flags"), "ok": bool(rec.get("ok")),
           "curve": curve(rec.get("passes") or [], 5), "open": [rec.get("open_before"), rec.get("open"), rec.get("open_cli")],
           "subfloor": [sf.get("subfloor", 0), sf.get("fixed", 0), len(sf.get("failed") or [])],
           "locked_same": rec.get("locked_same")}
    return {k: v for k, v in out.items() if v is not None}


def choose(want, flag_jar=None, flag_java=None, env=None):
    """`--router krt|freerouting|auto` -> (name, loc or None, note).  freerouting: located or RuntimeError.
    auto: Freerouting when a jar of a supported version and a Java runtime are found, else krt (the note says why)."""
    want = str(want or "krt").lower()
    if want == "krt":
        return "krt", None, ""
    if want == "freerouting":
        return "freerouting", locate(flag_jar, flag_java, env), ""
    if want != "auto":
        raise ValueError(f"router {want!r}: use krt, freerouting or auto")
    try:
        return "freerouting", locate(flag_jar, flag_java, env), ""
    except RuntimeError as e:
        return "krt", None, f"router auto -> krt ({e})"
