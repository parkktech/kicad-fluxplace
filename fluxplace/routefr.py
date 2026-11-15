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
import tempfile
import time
import zipfile

from . import pourdsn as PDS
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
         "grace_s": float(r.get("grace_s", 180)), "analytics": bool(r.get("analytics", False)),
         "segment_passes": int(r.get("segment_passes", 1)),        # passes per Freerouting run (the stall stop acts between runs)
         "stall_passes": int(r.get("stall_passes", 3)),             # passes in a row without a lower SIGNAL unrouted count
         # strip: signal-layer pours removed from the router's copy (today) | keep: left in | pour_job: strip + the pour-aware job
         "pour_dsn": str(r.get("pour_dsn", "strip")).lower()}
    for k, v in over.items():
        if v is not None:
            s[k] = v
    if s["fanout"] in ("true", "false"):
        s["fanout"] = "on" if s["fanout"] == "true" else "off"
    if s["fanout"] not in ("auto", "on", "off"):
        raise ValueError(f"[route.freerouting] fanout = {s['fanout']!r}: use auto, on or off")
    if s["pour_dsn"] not in ("strip", "keep", "pour_job"):
        raise ValueError(f"[route.freerouting] pour_dsn = {s['pour_dsn']!r}: use strip, keep or pour_job")
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
    """-> (text, report): planes declared power, rule widths / every clearance EXCEPT smd_smd raised to the floors (mm),
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
            # the smd_smd rule governs SMD-pad to SMD-pad spacing only: KiCad writes it small on purpose (fine-pitch
            # footprints), that spacing is fixed by the footprint, not by the router.  It stays as exported.
            if key == "clearance" and re.match(r"\s*\(type\s+smd_smd\)", mo.string[mo.end():mo.end() + 40]):
                return mo.group(0)
            if float(mo.group(2)) + 1e-9 < lim:
                rep[count_key] += 1
                return f"{mo.group(1)}{lim:g}"
            return mo.group(0)
        # the value is followed by ")" (untyped) or by a space and "(type ...)" (typed clearance); the lookahead keeps both
        return re.sub(r"(\(%s\s+)([\d.]+)(?=[\s)])" % key, sub, body)
    w = _section(text, "wiring")
    head, wiring, tail = (text[:w[0]], text[w[0]:w[1]], text[w[1]:]) if w else (text, "", "")
    if floor_track:
        head = raise_to("width", floor_track, "widths_raised", head)
    if floor_clearance:                 # every clearance: the untyped one AND the typed ones (smd_smd, smd_via, via_via, ...)
        head = raise_to("clearance", floor_clearance, "clearances_raised", head)
    if wiring:
        rep["already_fixed"] = wiring.count("(type fix)")
        wiring, n = re.subn(r"\(type\s+(?:route|protect|normal)\)", "(type fix)", wiring)
        rep["fixed"] = n + rep["already_fixed"]
        wiring, rep["duplicate_wires"] = _dedupe_wires(wiring)
    return head + wiring + tail, rep


_WIRE = re.compile(r"[ \t]*\(wire \(path (\S+) (\d+)\s+([-\d\s.]+?)\)\(net ([^)]*)\)\(type \w+\)\)\n?")


def _dedupe_wires(text):
    """Drop a fixed wire whose every segment already exists (same layer, width, net, end points, either direction).
    Fan-out/pairs copper was written twice on +3V3, +5V and VBUS (50 segments on the v16 board); the router's
    normaliser loops on such overlaps.  -> (text, number dropped)."""
    seen, out, dropped, last = set(), [], 0, 0
    for mo in _WIRE.finditer(text):
        out.append(text[last:mo.start()])
        last = mo.end()
        lay, w, pts, net = mo.group(1), mo.group(2), mo.group(3).split(), mo.group(4)
        xy = [float(v) for v in pts]
        segs = [((lay, w, net), tuple(sorted([(xy[i], xy[i + 1]), (xy[i + 2], xy[i + 3])]))) for i in range(0, len(xy) - 3, 2)]
        if segs and all(k in seen for k in segs):
            dropped += 1
            continue
        seen.update(segs)
        out.append(mo.group(0))
    out.append(text[last:])
    return "".join(out), dropped


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


def plane_nets(board, workdir, planes):
    """Nets that own a zone on a plane layer: their unrouted connections are the constant plane part of Freerouting's count."""
    if not planes:
        return []
    return _helper("planenets", {"board": board, "planes": list(planes)}, workdir)["nets"]


def fingerprint(board, workdir, floor=0.0, planes=()):
    return _helper("fingerprint", {"board": board, "floor": floor, "planes": list(planes)}, workdir)


def repair_subfloor(board, workdir, floor, clearance, out=None):
    """-> {subfloor, locked, fixed, failed: [...], widths}: `board` rewritten (to `out`) with the widened tracks."""
    return _helper("widen", {"board": board, "out": out or board, "floor": floor, "clearance": clearance}, workdir)


# ------------------------------------------------------------------ the run
def net_classes(cons, R, class_clearance=0.0):
    """The DSN net classes from the resolved rules (mm): default = the router track width, every class clearance =
    the clearance the router is given (floor + margin, RR.request_clearance).  Classes: one per distinct power width
    (`[power.N]` width_mm / max_current_ma, else the track default) and one per controlled pair family (`[pairs.F]`
    width_mm, else the PAIR_GEOM_BY_Z width of its impedance_diff); a `[nets.N] width_mm` on any other net is its own
    class.  No width is ever below the track floor."""
    from . import constraints as C
    from . import routerules as RR
    clr = RR.request_clearance(R)                # the floor (class clearance above it is kept per class)
    floor_w = R["min_track"]
    cons = cons or {}
    by_w = {}                                    # width (mm) -> {"nets": [...], "prefixes": [...]}

    def add(w, nets=(), prefixes=()):
        w = round(max(float(w), floor_w), 4)
        e = by_w.setdefault(w, {"nets": [], "prefixes": []})
        e["nets"] += [n for n in nets if n not in e["nets"]]
        e["prefixes"] += [p for p in prefixes if p not in e["prefixes"]]
    for net in (cons.get("power") or {}):
        add(C.power_width_mm(cons, net, R["track"]), nets=[net])
    for fam, v in (cons.get("pairs") or {}).items():
        w = v.get("width_mm") or (C.PAIR_GEOM_BY_Z.get(int(v["impedance_diff"]), (None, None))[0] if "impedance_diff" in v else None)
        if w:
            add(w, prefixes=[fam])
    for net, v in (cons.get("nets") or {}).items():
        if isinstance(v, dict) and v.get("width_mm"):
            add(float(v["width_mm"]), nets=[net])
    classes = [{"name": f"w{int(round(w * 1000))}um", "width": w, "clearance": clr, "nets": e["nets"], "prefixes": e["prefixes"]}
               for w, e in sorted(by_w.items()) if w != round(R["track"], 4)]
    for c in classes:
        c["clearance"] = max(c["clearance"], float(class_clearance or 0.0))
    return {"default": {"width": max(R["track"], floor_w), "clearance": max(clr, float(class_clearance or 0.0))}, "classes": classes}


_LOAD_FR = re.compile(r"\*\s+(\d+) (pin-to-pin|fixed trace/via) clearance violation")
_LOAD_DONE = ("Auto-routing stage started", "Fanout stage started", "Job '")


def gate_pro(src_pro, out_pro, classes):
    """A copy of the board's .kicad_pro whose net classes are the resolved ones (`net_classes`), so kicad-cli DRC measures
    the copper against the rules the router is given."""
    d = json.load(open(src_pro)) if os.path.exists(src_pro) else {"meta": {"version": 1}}
    ns = d.setdefault("net_settings", {})
    base = next((c for c in ns.get("classes") or [] if c.get("name") == "Default"), {"name": "Default"})
    dflt = dict(base, name="Default", clearance=classes["default"]["clearance"], track_width=classes["default"]["width"])
    new, pats = [dflt], []
    for c in classes.get("classes") or []:
        new.append(dict(base, name=c["name"], clearance=c["clearance"], track_width=c["width"]))
        pats += [{"netclass": c["name"], "pattern": n} for n in c.get("nets") or ()]
        pats += [{"netclass": c["name"], "pattern": pre + "*"} for pre in c.get("prefixes") or ()]
    ns["classes"], ns["netclass_patterns"] = new, pats
    with open(out_pro, "w") as fh:
        json.dump(d, fh, indent=2)
    return out_pro


def copper_clearance(drc):
    """The copper-to-copper clearance violations of a kicad-cli DRC report (zone fills excluded): [{items, actual, required}]."""
    out = []
    for v in drc.get("violations", []):
        # a via or track inside a keepout is the same failure for the router (its own rule), so both kinds are gated
        if v.get("type") not in ("clearance", "items_not_allowed"):
            continue
        its = v.get("items") or []
        if any("Zone" in (i.get("description") or "") for i in its):
            continue
        d = v.get("description") or ""
        if v.get("type") == "items_not_allowed":
            if not any(("Track" in (i.get("description") or "") or "Via" in (i.get("description") or "")) for i in its):
                continue
            out.append({"kind": "keepout", "items": [{"desc": i.get("description"), "uuid": i.get("uuid"), "x": i["pos"]["x"],
                                                       "y": i["pos"]["y"]} for i in its], "actual": 0.0, "required": None})
            continue
        act = re.search(r"actual ([\d.]+) mm", d)
        req = re.search(r"(?:clearance|netclass '[^']*' clearance) ([\d.]+) mm", d)
        out.append({"items": [{"desc": i.get("description"), "uuid": i.get("uuid"), "x": i["pos"]["x"], "y": i["pos"]["y"]} for i in its],
                    "actual": float(act.group(1)) if act else None, "required": float(req.group(1)) if req else None})
    return out


def subfloor_copper(board, workdir, classes, floor, drc_cli="kicad-cli"):
    """Copper-to-copper clearance violations below the floor on `board` (kicad-cli DRC, zone fills excluded), with the
    resolved class clearances: [{kinds, actual, required, nets}].  Used to find WHERE sub-floor copper is made."""
    os.makedirs(workdir, exist_ok=True)
    cp = os.path.join(workdir, "subfloor.kicad_pcb")
    shutil.copy(board, cp)
    gate_pro(os.path.splitext(board)[0] + ".kicad_pro", os.path.splitext(cp)[0] + ".kicad_pro", classes)
    with tempfile.TemporaryDirectory() as td:
        o = os.path.join(td, "drc.json")
        subprocess.run([drc_cli, "pcb", "drc", "--format", "json", "--severity-all", "--all-track-errors", "--refill-zones",
                        "-o", o, cp], capture_output=True, text=True, timeout=900)
        d = json.load(open(o)) if os.path.exists(o) else {"violations": []}
    out = []
    for v in copper_clearance(d):
        if v["actual"] is None or v["actual"] + 1e-9 >= floor:
            continue
        kinds = sorted(re.sub(r" \[.*", "", i["desc"] or "") for i in v["items"])
        out.append({"kinds": [k.split(" on ")[0] for k in kinds], "actual": v["actual"], "required": v["required"]})
    return out


def load_violations(board, workdir, loc, S, planes=(), floor_track=0.0, floor_clearance=0.0, tmo_s=150.0):
    """Freerouting's own load-time report for `board` (a zero-pass run, fan-out off; killed once the report is out).
    -> {pin_to_pin, fixed_trace_via, report: [lines]} or {error}."""
    seg = os.path.join(workdir, "load")
    os.makedirs(seg, exist_ok=True)
    dsn, ses, flog = os.path.join(seg, "in.dsn"), os.path.join(seg, "out.ses"), os.path.join(seg, "freerouting.log")
    ex = _helper("export", {"board": board, "dsn": dsn}, workdir)
    if not ex.get("ok"):
        return {"error": "DSN export failed"}
    with open(dsn, errors="surrogateescape") as fh:               # the same DSN rules route_board gives the router
        text, _ = fix_dsn(fh.read(), planes, floor_track, floor_clearance, ())
    with open(dsn, "w", errors="surrogateescape") as fh:
        fh.write(text)
    cmd = command(loc, dsn, ses, dict(S, passes=0), tmo_s, os.path.join(seg, "jvmtmp"), False)
    os.makedirs(os.path.join(seg, "jvmtmp"), exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    with open(flog, "wb") as lf:
        p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=seg, env=env)
    t0 = time.time()
    try:
        while time.time() - t0 < tmo_s:
            time.sleep(0.5)
            txt = open(flog, errors="replace").read()
            if "clearance violation" in txt and any(k in txt for k in _LOAD_DONE[:2]):
                break
            if p.poll() is not None:
                break
    finally:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=30)
    txt = open(flog, errors="replace").read()
    rep = {"pin_to_pin": 0, "fixed_trace_via": 0, "report": [l.strip() for l in txt.splitlines() if "violation" in l][:20]}
    for m in _LOAD_FR.finditer(txt):
        if m.group(2) == "pin-to-pin":
            rep["pin_to_pin"] = int(m.group(1))
        else:
            rep["fixed_trace_via"] = int(m.group(1))
    rep["seconds"] = round(time.time() - t0, 1)
    return rep


def preroute_gate(board, workdir, *, loc, S, classes, out, planes=(), floor_track=0.0, floor_clearance=0.0, drc_cli="kicad-cli"):
    """The route stage's pre-route gate. Pre-placed copper (fan-out, pairs) must not violate the rules the router is given:
    kicad-cli DRC against the resolved classes lists the copper-to-copper clearance violations (measured vs required);
    the UNLOCKED tracks and vias among them are removed (the router redoes those connections); locked copper is only
    reported.  Freerouting's own load report (`load_violations`) is recorded beside the DRC count.  -> record; `out` is
    the board to route (the input when nothing was removed)."""
    from . import opengate as OG  # noqa: F401 - the same kicad-cli DRC the review uses
    os.makedirs(workdir, exist_ok=True)
    g = os.path.join(workdir, "gate.kicad_pcb")
    shutil.copy(board, g)
    gate_pro(os.path.splitext(board)[0] + ".kicad_pro", os.path.splitext(g)[0] + ".kicad_pro", classes)

    def drc(path):
        with tempfile.TemporaryDirectory() as td:
            o = os.path.join(td, "drc.json")
            subprocess.run([drc_cli, "pcb", "drc", "--format", "json", "--severity-all", "--all-track-errors", "-o", o, path],
                           capture_output=True, text=True, timeout=900)
            return json.load(open(o)) if os.path.exists(o) else {"violations": []}
    rec = {"classes": len(classes.get("classes") or ()), "ok": True}
    before = copper_clearance(drc(g))
    rec["drc_before"] = len(before)
    # the load check reads the SAME DSN the router will get: pours stripped, the resolved net classes applied
    stripped = os.path.join(workdir, "gate-dsn.kicad_pcb")
    _side(board, stripped)
    _helper("strip", {"board": board, "out": stripped, "planes": list(planes), "netclasses": classes}, workdir)
    rec["load_before"] = load_violations(stripped, workdir, loc, S, planes=planes, floor_track=floor_track,
                                         floor_clearance=floor_clearance)
    victims = {}
    for v in before:
        for it in v["items"]:
            if it["uuid"] and it["uuid"] not in victims:
                victims[it["uuid"]] = v
    res = _helper("unlink", {"board": g, "out": out, "uuids": list(victims), "skip": []}, workdir)
    for r in res["removed"] + res["locked"]:
        v = victims.get(r["uuid"], {})
        r.update(measured=v.get("actual"), required=v.get("required"))
    rec["removed"], rec["locked"] = res["removed"], res["locked"]
    rec["unremovable"] = [v for v in before if not any(it["uuid"] in res["uuids_hit"] for it in v["items"])
                          and not any(it["uuid"] in {r["uuid"] for r in res["locked"]} for it in v["items"])]
    if res["removed"]:
        _side(board, out)
        rec["drc_after"] = len(copper_clearance(drc(out)))
    else:
        shutil.copy(board, out)
        rec["drc_after"] = rec["drc_before"]
    return rec


def gate_summary(g):
    """One stage-log line for the pre-route gate."""
    lb = g.get("load_before") or {}
    s = (f"pre-route gate: DRC copper clearance {g.get('drc_before')} -> {g.get('drc_after')}"
         f", Freerouting load {lb.get('pin_to_pin', '?')} pin-to-pin + {lb.get('fixed_trace_via', '?')} fixed trace/via"
         f", removed {len(g.get('removed') or [])} unlocked stub(s), locked {len(g.get('locked') or [])}"
         f", unremovable {len(g.get('unremovable') or [])}")
    return s


def split_passes(passes, plane_open):
    """Per pass: total unrouted (Freerouting's count, plane connections included), plane part (constant: plane nets are
    never routed on signal layers), signal part = total - plane.  Mutates and returns `passes` with signal/plane keys."""
    for p in passes:
        p["plane"] = plane_open
        p["signal"] = max(0, p["unrouted"] - plane_open)
    return passes


def _pour_job(text, pour_json):
    """the pour-aware job (pourdsn.apply) on the DSN text; the one call site of pourdsn in the export path."""
    if not pour_json:
        return text, None
    with open(pour_json) as fh:
        return PDS.apply(text, json.load(fh))


def route_board(board, out, workdir, *, loc, planes, floor_track, floor_clearance, budget_s, S=None, cons=None,
                skip_nets=(), has_fanout=False, conn=None, post=None, tick=None, poll=2.0, classes=None, plane_nets=(), pour=None):
    """One Freerouting run on `board` -> `out`, in SEGMENTS of `[route.freerouting] segment_passes` passes (default 1).
    Freerouting only saves its session when it finishes, so a stall cannot stop it in place: each segment is a
    complete run on the copper so far (the previous segment's session imported into the stripped board, which is then
    exported again as fixed copper).  The run stops when `stall_passes` passes in a row do not lower the SIGNAL
    unrouted count (total minus the constant plane part `plane_nets`), when it completes, or at the budget.
    -> the record; `ok` False + `error` on failure, `out` not written.  `subfloor.failed` non-empty = sub-floor copper
    is still on `out`: the caller must fail its stage."""
    S = S or settings(cons)
    os.makedirs(workdir, exist_ok=True)
    tmpdir = os.path.join(workdir, "jvmtmp")
    os.makedirs(tmpdir, exist_ok=True)
    fanout_on = S["fanout"] == "on" or (S["fanout"] == "auto" and not has_fanout)
    rec = {"ok": False, "router": "freerouting", "jar": loc["jar"], "java": loc["java"], "version": loc["version"],
           "flags": " ".join(router_flags(S, budget_s, fanout_on)), "fanout": "on" if fanout_on else "off", "pairs": S["pairs"],
           "fanout_why": f"[route.freerouting] fanout = {S['fanout']}; the input {'already has' if has_fanout else 'has no'} "
                         f"fan-out copper -> Freerouting's own fan-out {'ON' if fanout_on else 'OFF'}",
           "skip_nets": sorted(skip_nets), "budget_s": round(budget_s), "planes": list(planes),
           "segment_passes": int(S.get("segment_passes", 1)), "stall_passes": int(S.get("stall_passes", 3))}
    t0 = time.time()
    sessions = []
    try:
        fp0 = fingerprint(board, workdir, floor_track, planes)
        rec["before"] = fp0
        dsn_board = os.path.join(workdir, "dsn.kicad_pcb")
        _side(board, dsn_board)
        if S.get("pour_dsn", "strip") == "keep":          # signal-layer pours stay in the router's copy (Freerouting 2.5 pour-aware)
            shutil.copy(board, dsn_board)
            rec["pours"] = {"mode": "keep"}
        else:
            rec["pours"] = _helper("strip", {"board": board, "out": dsn_board, "planes": list(planes), "netclasses": classes}, workdir)
        # every count below is THE open-connection definition (opengate.unconnected_nets: kicad-cli after refill), taken
        # on the stripped chain the segments run on, so the segments are compared with each other on one basis
        from . import opengate as OG
        base_nets = OG.unconnected_nets(dsn_board)
        plane_open = sum(v for n, v in base_nets.items() if n in set(plane_nets))
        rec["plane_open"] = plane_open
        cur, passes, stale, best, stop, state, jvm_s, starts = dsn_board, [], 0, None, None, None, 0.0, []
        best_seg_pass = 0
        best = max(0, sum(base_nets.values()) - plane_open)     # baseline: the input chain's signal count
        rec["segments"] = []
        for k in range(100000):
            seg = os.path.join(workdir, f"seg{k:03d}")
            os.makedirs(seg, exist_ok=True)
            dsn, ses, flog = os.path.join(seg, "in.dsn"), os.path.join(seg, "out.ses"), os.path.join(seg, "freerouting.log")
            ex = _helper("export", {"board": cur, "dsn": dsn}, workdir)
            if not ex.get("ok"):
                raise RuntimeError("Specctra DSN export failed")
            with open(dsn, errors="surrogateescape") as fh:
                text, rep = fix_dsn(fh.read(), planes, floor_track, floor_clearance, skip_nets)
            text, pj = _pour_job(text, pour if S.get("pour_dsn") == "pour_job" else None)   # router copy only
            if k == 0:
                rep["pour"] = pj
            if rep["planes_missed"]:
                raise RuntimeError("plane layer(s) not found in the DSN, refusing to route on them: " + ", ".join(rep["planes_missed"]))
            with open(dsn, "w", errors="surrogateescape") as fh:
                fh.write(text)
            if k == 0:
                rec["dsn"] = rep
                rec["prep_s"] = round(time.time() - t0, 1)
            left = budget_s - (time.time() - t0)
            if left < 1 and k > 0:                  # the first segment always runs (its failure is loud)
                stop = "budget"
                break
            left = max(1.0, left)
            Sk = dict(S, passes=rec["segment_passes"])
            jr = run_jvm(command(loc, dsn, ses, Sk, left, tmpdir, fanout_on), flog, left, S["grace_s"], ses,
                         cwd=seg, tick=tick, poll=poll)
            log = parse_log(open(flog, errors="replace").read()) if os.path.exists(flog) else parse_log("")
            jvm_s += jr["elapsed"]
            rec["killed"] = bool(rec.get("killed") or jr["killed"])
            rec["hung_after_save"] = bool(rec.get("hung_after_save") or jr["hung_after_save"])
            state = log["state"] or state or ("KILLED" if jr["killed"] else None)
            if log["version"] and log["version"] != loc["version"]:
                rec["version"] = log["version"]
            if log["start_unrouted"] is not None:
                starts.append(log["start_unrouted"])
            if not ses_ok(ses):
                rec["rc"] = jr["rc"]
                tail = ""
                try:
                    tail = " | ".join(open(flog, errors="replace").read().strip().splitlines()[-2:])[-240:]
                except OSError:
                    pass
                raise RuntimeError(f"Freerouting left no usable session (rc {jr['rc']}, {'killed by the watchdog' if jr['killed'] else 'exited'}"
                                   f" after {jr['elapsed']:.0f}s): {tail}")
            seg_first = len(passes) + 1
            for ps in log["passes"]:
                passes.append(dict(ps, pass_no=len(passes) + 1, seg=k))
            rec["segments"].append({"seg": k, "passes": len(log["passes"]), "jvm_s": jr["elapsed"], "state": log["state"]})
            if pour:                                       # pour-aware session: derived pours renamed back, as the stage always did
                rec.setdefault("pour", {"ses_derived_renamed": []})["ses_derived_renamed"].append(PDS.unmap_ses(ses))
            nxt = os.path.join(seg, "after.kicad_pcb")
            _side(board, nxt)
            imp = _helper("import", {"board": cur, "ses": ses, "out": nxt, "skip_nets": sorted(skip_nets)}, workdir)
            rec.setdefault("import_segments", []).append(imp)
            # Freerouting's own pass counts are not monotonic (it can finish a pass worse than its best), and the
            # session it writes is the state it ends in.  So the segment is ACCEPTED only when the imported board
            # measures fewer signal open connections than the best accepted board (pcbnew, zones filled).
            # Without `conn` the Freerouting count of the segment's last pass is used instead.
            meas = max(0, OG.unconnected(nxt) - plane_open)
            acc = meas is not None and (best is None or meas < best)
            if acc:
                best, stale, cur = meas, 0, nxt
                sessions.append(ses)
                best_seg_pass = len(passes) if log["passes"] else best_seg_pass
            else:
                stale += 1
                rec.setdefault("rejected", []).append({"seg": k, "measured": meas, "best": best})
            if acc and meas == 0:
                stop = "signal routed"
            elif stale >= rec["stall_passes"] and stop is None:
                stop = f"stall: {stale} segment(s) without a lower measured signal count"
            if stop is None and len(passes) >= S["passes"]:
                stop = "max passes"
            if stop:
                break
        rec["stop"] = stop or "segments exhausted"
        rec["state"] = state
        rec["jvm_s"] = round(jvm_s, 1)
        rec["start_unrouted"] = starts[0] if starts else None
        split_passes(passes, plane_open)
        rec["passes"] = passes
        fr_best = min(passes, key=lambda p: p["signal"]) if passes else None
        rec["best"] = {"measured_signal": best, "fr_best_pass": fr_best and fr_best["pass_no"],
                       "fr_best_signal": fr_best and fr_best["signal"], "accepted_last_pass": best_seg_pass,
                       "plane_open": plane_open}
        rec["passes_split"] = [[p["pass_no"], round(p["min"] or 0, 1), p["unrouted"], p["signal"], p["plane"]] for p in passes]
        t1 = time.time()
        tmp = out + ".fr-import.kicad_pcb"
        # the final board: the ORIGINAL copper and pours, every segment's session imported onto it in order
        _side(board, tmp)
        src, imps = board, []
        if not sessions:                                   # no segment ran (budget): the input copper stands
            shutil.copy(board, tmp)
        for i, ses in enumerate(sessions):
            dst = tmp if i == len(sessions) - 1 else os.path.join(workdir, f"final{i:03d}.kicad_pcb")
            imps.append(_helper("import", {"board": src, "ses": ses, "out": dst, "skip_nets": sorted(skip_nets)}, workdir))
            src = dst
        rec["import"] = dict(imps[-1] if imps else {}, tracks_added=sum(x.get("tracks_added", 0) for x in imps),
                             vias_added=sum(x.get("vias_added", 0) for x in imps))
        rec["subfloor"] = repair_subfloor(tmp, workdir, floor_track, floor_clearance) if floor_track else \
            {"subfloor": 0, "locked": 0, "fixed": 0, "failed": [], "widths": []}
        if post and floor_clearance:                       # where sub-floor copper comes from: before the snap/join step, and after it
            cl = classes or {"default": {"width": floor_track, "clearance": floor_clearance}, "classes": []}
            rec["subfloor_before_post"] = subfloor_copper(tmp, os.path.join(workdir, "sub-pre"), cl, floor_clearance)
        if post:
            rec["post"] = {k: v for k, v in post(tmp).items() if k in ("snapped", "joined", "reverted", "error")}
            from . import opengate as OG
            OG.refill(tmp)
        if post and floor_clearance:
            rec["subfloor_after_post"] = subfloor_copper(tmp, os.path.join(workdir, "sub-post"), cl, floor_clearance)
        fp1 = fingerprint(tmp, workdir, floor_track, planes)
        rec["after"] = fp1
        rec["locked_same"] = fp0["locked"] == fp1["locked"] and fp0["locked_hash"] == fp1["locked_hash"]
        rec["plane_tracks_added"] = fp1["plane_tracks"] - fp0["plane_tracks"]
        if conn:
            rec["open"] = OG.unconnected(tmp)
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
           "curve": curve(rec.get("passes") or [], 5), "open": [rec.get("open_before"), rec.get("open")],
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
