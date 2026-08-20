"""Dependency registry and preflight for the fluxplace engineering suite.

fluxplace is not a self-contained python package. It drives KiCad, a router,
distributor APIs and an office suite, and until 2026-08-19 it declared none of
that anywhere — it worked only because one workstation happened to have the
right things in the right places. That is the same failure class as shipping a
"0 DRC" verdict without saying which checks ran: a green result whose
preconditions nobody stated.

This module is the single place that knows what the suite needs. The CLI
(`fluxplace doctor`) and the KiCad action plugin both read it, so a user is told
what is missing BEFORE a pipeline dies half way through a board.

Requirements are grouped into tiers. A missing CORE item means nothing runs; a
missing item in any other tier means one capability is unavailable, and the
report says which, rather than failing later with an ImportError from inside a
subprocess.
"""

import importlib.util
import os
import shutil
import subprocess
import sys

# --------------------------------------------------------------------------
# The registry.
#
# kind:  "python"  importable module          -> pip install <pip>
#        "binary"  executable on PATH         -> platform package manager
#        "jar"     a file we shell java at    -> download
#        "env"     environment/credential     -> user must supply
#        "service" an MCP server / skill      -> informational, cannot autodetect
#
# tier:  "core"    fluxplace cannot run at all without it
#        "fab"     manufacturing output (briefs, spreadsheets, PDF uploads)
#        "route"   autorouting
#        "sourcing" live distributor checks (the D43 availability gate)
#        "suite"   the wider KiCad toolchain this project expects
# --------------------------------------------------------------------------

KICAD_MIN = (10, 0)

# --------------------------------------------------------------------------
# Sourcing policy — DigiKey and Mouser, and nothing else.
#
# Two APIs answer authoritatively, with credentials, and can be held to account
# for stock and price. Everything else in this space is a scraped index that
# goes down when you need it (the jlcparts index behind LCSC search has returned
# HTTP 404 for weeks at a time, mid-project, twice) or a CDN that serves a
# login wall. A sourcing gate whose answer depends on whether a third-party
# mirror is up is not a gate.
#
# JLCPCB and PCBWay still appear throughout fluxplace as FABRICATORS — DFM
# profiles, trace/space floors, stackups, order worksheets. That is a different
# role and is unaffected: a fab is where the board is made, not where the parts
# are bought.
# --------------------------------------------------------------------------
SOURCING_POLICY = ("DigiKey and Mouser only. No LCSC/jlcparts, no SnapEDA, no "
                   "vendor CDNs. JLCPCB/PCBWay are fabricators here, not part "
                   "sources.")
FORBIDDEN_SOURCES = ("lcsc", "jlcparts", "snapeda", "octopart", "easyeda")

REQUIREMENTS = [
    # ---- core ------------------------------------------------------------
    dict(key="pcbnew", kind="python", module="pcbnew", tier="core",
         label="KiCad python bindings (pcbnew)",
         why="every board read/write goes through it",
         pip=None,
         hint="Ships WITH KiCad %d.%d — it is not on PyPI. Install KiCad, then run "
              "fluxplace with the interpreter that can see it (on Linux that is "
              "usually /usr/bin/python3, NOT a conda or pyenv python)." % KICAD_MIN),
    dict(key="numpy", kind="python", module="numpy", tier="core",
         label="numpy", why="quadratic placement solve and geometry",
         pip="numpy", apt="python3-numpy"),
    dict(key="kicad-cli", kind="binary", binary="kicad-cli", tier="core",
         label="kicad-cli", why="DRC, gerber/drill/centroid export, renders",
         pip=None,
         hint="Ships with KiCad %d.%d. On WSL/Linux install the KiCad package; "
              "the binary lands at /usr/bin/kicad-cli." % KICAD_MIN),

    # ---- fab / documentation --------------------------------------------
    dict(key="pillow", kind="python", module="PIL", tier="fab",
         label="Pillow", why="board renders and the model-orientation probe",
         pip="Pillow", apt="python3-pil"),
    dict(key="python-docx", kind="python", module="docx", tier="fab",
         label="python-docx", why="fab-submission brief and PCBWay order worksheet (.docx)",
         pip="python-docx", apt="python3-docx"),
    dict(key="openpyxl", kind="python", module="openpyxl", tier="fab",
         label="openpyxl", why="the .xlsx twins of the BOM and centroid uploads",
         pip="openpyxl", apt="python3-openpyxl"),
    dict(key="libreoffice", kind="binary", binary="soffice", tier="fab",
         label="LibreOffice (soffice)",
         why="renders the assembly instructions to PDF — PCBWay rejects .docx on that field",
         pip=None,
         hint="apt install libreoffice-writer   (or the full libreoffice)"),

    # ---- routing ---------------------------------------------------------
    dict(key="java", kind="binary", binary="java", tier="route",
         label="Java runtime", why="runs the freerouting jar",
         pip=None, hint="apt install default-jre-headless"),
    dict(key="freerouting", kind="jar", tier="route",
         label="freerouting 2.3.0+ jar",
         why="the autorouter behind `tournament` and the auto pipeline",
         pip=None,
         hint="Download freerouting-2.3.0.jar from "
              "https://github.com/freerouting/freerouting/releases and put it at "
              "~/tools/freerouting-2.3.0.jar (2.2.4 dies silently on headless jobs "
              "— use 2.3.0 or newer)."),

    # ---- sourcing --------------------------------------------------------
    dict(key="digikey", kind="env", env=("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"),
         tier="sourcing", label="DigiKey API credentials",
         why="live stock/pricing for the availability gate and 3D model fetch",
         pip=None,
         hint="Create a DigiKey app at https://developer.digikey.com and export "
              "DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET."),
    dict(key="mouser", kind="env", env=("MOUSER_API_KEY",), tier="sourcing",
         label="Mouser API key", why="second distributor for the availability gate",
         pip=None,
         hint="Request a key at https://www.mouser.com/api-hub/ and export MOUSER_API_KEY."),

    # ---- the wider suite (informational) ---------------------------------
    dict(key="mcp-kicad", kind="service", tier="suite",
         label="MCP server: kicad", why="project analysis, ERC/DRC, BOM, netlist, thumbnails"),
    dict(key="mcp-kicad-pro", kind="service", tier="suite",
         label="MCP server: kicad-pro (PCB Pro)",
         why="DFM checks, BOM-with-pricing, component-contract verification"),
    dict(key="mcp-kicad-jlcpcb", kind="service", tier="suite",
         label="MCP server: kicad-jlcpcb",
         why="board generation from a netlist spec, and JLCPCB fab packaging. "
             "NOT used for part sourcing — see SOURCING_POLICY"),
    dict(key="skill-kicad-happy", kind="service", tier="suite",
         label="Plugin: kicad-happy",
         why="datasheets and the digikey/mouser skills"),
    dict(key="skill-pcb-designer", kind="service", tier="suite",
         label="Skill: pcb-designer", why="DFM, stackups, RF layout guidance"),
]

TIER_TITLE = {
    "core":     "CORE — nothing runs without these",
    "fab":      "FAB OUTPUT — manufacturing documents and uploads",
    "route":    "ROUTING — the autorouter",
    "sourcing": "SOURCING — live distributor checks",
    "suite":    "SUITE — the wider KiCad toolchain (install separately)",
}

FREEROUTING_PATHS = (
    "~/tools/freerouting-2.3.0.jar",
    "~/tools/freerouting.jar",
    "~/freerouting.jar",
    "/opt/freerouting/freerouting.jar",
)


# --------------------------------------------------------------------------
# checking
# --------------------------------------------------------------------------

def _has_module(name):
    """True if importable. Uses find_spec so we do not pay pcbnew's import cost
    (or its noisy wx asserts) just to answer 'is it there'."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def find_freerouting():
    """Return the first freerouting jar we can find, or None."""
    env = os.environ.get("FREEROUTING_JAR")
    if env and os.path.exists(os.path.expanduser(env)):
        return os.path.expanduser(env)
    for p in FREEROUTING_PATHS:
        p = os.path.expanduser(p)
        if os.path.exists(p):
            return p
    return None


def kicad_cli_version(binary="kicad-cli"):
    """(major, minor) of the kicad-cli on PATH, or None."""
    exe = shutil.which(binary)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=20).stdout.strip()
    except Exception:
        return None
    import re
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def check_one(req):
    """-> dict(key, label, tier, ok, detail, fixable)."""
    kind, ok, detail = req["kind"], False, ""

    if kind == "python":
        ok = _has_module(req["module"])
        detail = "" if ok else "module %r not importable by %s" % (
            req["module"], sys.executable)

    elif kind == "binary":
        path = shutil.which(req["binary"])
        ok = path is not None
        detail = path or "not on PATH"
        if ok and req["key"] == "kicad-cli":
            ver = kicad_cli_version(req["binary"])
            if ver:
                detail += "  (v%d.%d)" % ver
                if ver < KICAD_MIN:
                    ok = False
                    detail += "  -- need >= %d.%d" % KICAD_MIN

    elif kind == "jar":
        jar = find_freerouting()
        ok = jar is not None
        detail = jar or "no freerouting jar found"

    elif kind == "env":
        missing = [v for v in req["env"] if not os.environ.get(v)]
        ok = not missing
        detail = "set" if ok else "unset: " + ", ".join(missing)

    elif kind == "service":
        ok = None                      # cannot autodetect from inside the plugin
        detail = "configure in your MCP/plugin host"

    return dict(key=req["key"], label=req["label"], tier=req["tier"], ok=ok,
                detail=detail, why=req.get("why", ""), hint=req.get("hint", ""),
                pip=req.get("pip"), apt=req.get("apt"), kind=kind)


def check_all():
    return [check_one(r) for r in REQUIREMENTS]


def missing(results=None, tiers=None):
    """Requirements that are definitely absent (services excluded — unknowable)."""
    res = results if results is not None else check_all()
    return [r for r in res
            if r["ok"] is False and (tiers is None or r["tier"] in tiers)]


def pip_installable(results=None):
    """The subset we can fix ourselves, as pip names."""
    return [r["pip"] for r in missing(results) if r.get("pip")]


def blocking(results=None):
    """Core misses — the suite genuinely cannot run."""
    return missing(results, tiers=("core",))


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

MARK = {True: "OK  ", False: "MISS", None: "??  "}


def report(results=None, show_ok=True):
    """Human-readable preflight text."""
    res = results if results is not None else check_all()
    out = []
    out.append("fluxplace preflight — KiCad engineering suite")
    out.append("interpreter: %s" % sys.executable)
    out.append("=" * 72)
    for tier in ("core", "fab", "route", "sourcing", "suite"):
        rows = [r for r in res if r["tier"] == tier]
        if not rows:
            continue
        shown = rows if show_ok else [r for r in rows if r["ok"] is not True]
        if not shown:
            continue
        out.append("")
        out.append(TIER_TITLE[tier])
        out.append("-" * 72)
        if tier == "sourcing":
            for line in _wrap(SOURCING_POLICY, 68):
                out.append("  %s" % line)
            out.append("")
        for r in shown:
            out.append("  [%s] %-34s %s" % (MARK[r["ok"]], r["label"], r["detail"]))
            if r["ok"] is not True:
                if r["why"]:
                    out.append("         needed for: %s" % r["why"])
                if r["hint"]:
                    for line in _wrap(r["hint"], 62):
                        out.append("         %s" % line)

    miss, block = missing(res), blocking(res)
    out.append("")
    out.append("=" * 72)
    if block:
        out.append("BLOCKED: %d core requirement(s) missing — fluxplace cannot run."
                   % len(block))
    elif miss:
        out.append("USABLE, with %d capability gap(s). Everything core is present."
                   % len(miss))
    else:
        out.append("All checks passed.")
    kind, cmd, note = install_plan(res)
    if kind != "none":
        out.append("")
        out.append("To install what is missing (%s):" % note)
        out.append("    %s" % " ".join(cmd))
        out.append("")
        out.append("or let fluxplace do it:   fluxplace doctor --install")
    return "\n".join(out)


def _wrap(text, width):
    words, line, lines = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            lines.append(line); line = w
        else:
            line = (line + " " + w).strip()
    if line:
        lines.append(line)
    return lines


def summary_line(results=None):
    """One line, for a status bar or a plugin dialog title."""
    res = results if results is not None else check_all()
    miss, block = missing(res), blocking(res)
    if block:
        return "BLOCKED — %d core requirement(s) missing" % len(block)
    if miss:
        return "%d optional requirement(s) missing" % len(miss)
    return "all requirements satisfied"


# --------------------------------------------------------------------------
# installing
# --------------------------------------------------------------------------

DEFAULT_VENV = "~/.fluxplace-venv"


def venv_python(path=DEFAULT_VENV):
    """Path to the interpreter inside our bootstrap venv, if it exists."""
    p = os.path.join(os.path.expanduser(path), "bin", "python")
    if os.name == "nt":
        p = os.path.join(os.path.expanduser(path), "Scripts", "python.exe")
    return p if os.path.exists(p) else None


def bootstrap_venv(path=DEFAULT_VENV, log=print):
    """Create a venv that INHERITS system site-packages, then pip into it.

    This is the answer to the KiCad-on-Linux bind, and it needs no root.

    A plain venv is useless here: it would not have pcbnew, which only exists in
    the distro python's site-packages and is not on PyPI. But
    `venv --system-site-packages` inherits pcbnew, wx, numpy and Pillow from the
    system interpreter while giving us a writable site-packages that PEP 668
    does not police. One interpreter ends up with everything, and nothing on the
    system python is touched.

    Returns the venv's python path on success, else None.
    """
    target = os.path.expanduser(path)
    base = _system_python()
    log("creating venv with system site-packages: %s" % target)
    log("  base interpreter: %s  (the one that owns pcbnew)" % base)
    try:
        r = subprocess.run([base, "-m", "venv", "--system-site-packages", target],
                           text=True)
        if r.returncode != 0:
            log("venv creation FAILED")
            return None
    except Exception as e:
        log("venv creation failed: %s" % e)
        return None

    py = venv_python(path)
    if not py:
        log("venv created but no interpreter found in it")
        return None

    pips = [m["pip"] for m in missing() if m.get("pip")]
    if pips:
        log("installing into the venv: %s" % " ".join(pips))
        r = subprocess.run([py, "-m", "pip", "install"] + pips, text=True)
        if r.returncode != 0:
            log("pip into the venv FAILED")
            return None
    log("")
    log("done. Run fluxplace with:")
    log("    %s cli.py <command>" % py)
    return py


def _system_python():
    """The interpreter most likely to own pcbnew. If we are already running on
    it, use it; otherwise fall back to the well-known distro path."""
    if _has_module("pcbnew"):
        return sys.executable
    for cand in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if os.path.exists(cand):
            return cand
    return sys.executable


def externally_managed():
    """True if this interpreter is PEP 668 externally-managed (Debian/Ubuntu
    system python). Those refuse `pip install`, including --user."""
    import sysconfig
    for key in ("stdlib", "purelib", "platlib"):
        d = sysconfig.get_path(key)
        if d and os.path.exists(os.path.join(d, "EXTERNALLY-MANAGED")):
            return True
    base = os.path.dirname(sysconfig.get_path("stdlib") or "")
    return bool(base) and os.path.exists(os.path.join(base, "EXTERNALLY-MANAGED"))


def _apt_available():
    return shutil.which("apt-get") is not None


def install_plan(results=None):
    """Work out HOW to install what is missing on THIS machine.

    The KiCad-on-Linux bind: the only interpreter that can import pcbnew is the
    distro python, and on Debian/Ubuntu that python is PEP 668 externally-
    managed, so plain `pip install` (even --user) is refused. Installing into a
    conda/venv python instead "works" and then fails at run time, because that
    python has no pcbnew. So the correct fix on those systems is the distro
    package, not pip.

    Returns (kind, argv_list, human_note).
    """
    res = results if results is not None else check_all()
    miss = [m for m in missing(res) if m.get("pip") or m.get("apt")]
    if not miss:
        return ("none", [], "nothing to install")

    if _in_virtualenv() or not externally_managed():
        pips = [m["pip"] for m in miss if m.get("pip")]
        cmd = [sys.executable, "-m", "pip", "install"]
        if not _in_virtualenv():
            cmd.append("--user")
        return ("pip", cmd + pips,
                "pip into %s" % sys.executable)

    # Externally managed. Two correct answers; prefer the one needing no root.
    #
    # A venv created with --system-site-packages inherits pcbnew/wx/numpy from
    # this interpreter and gives us a writable site-packages PEP 668 does not
    # police. No sudo, nothing on the system python touched.
    py = venv_python()
    if py:
        pips = [m["pip"] for m in miss if m.get("pip")]
        return ("venv-existing", [py, "-m", "pip", "install"] + pips,
                "installing into the existing fluxplace venv at %s, which "
                "already inherits pcbnew from the system python" % DEFAULT_VENV)

    apts = [m["apt"] for m in miss if m.get("apt")]
    if apts and _apt_available():
        return ("venv", ["<bootstrap>"],
                "this interpreter is PEP 668 externally-managed. The no-root fix "
                "is a venv with --system-site-packages: it inherits pcbnew from "
                "the system python and lets pip work. `doctor --install` will "
                "create it at %s. The alternative needs root: sudo apt-get "
                "install -y %s" % (DEFAULT_VENV, " ".join(apts)))

    pips = [m["pip"] for m in miss if m.get("pip")]
    return ("pip-break", [sys.executable, "-m", "pip", "install",
                          "--break-system-packages"] + pips,
            "no distro package available and this interpreter is externally "
            "managed — --break-system-packages is the remaining option")


def install(pips=None, log=print, upgrade=False, assume_yes=False):
    """Install what is missing, by whatever route is correct here.

    `pips` is accepted for backwards compatibility and ignored in favour of the
    computed plan, so callers cannot accidentally pip into the wrong place.
    """
    kind, cmd, note = install_plan()
    if kind == "none":
        log("nothing to install")
        return True
    log("plan: %s" % note)
    if kind == "venv":
        return bootstrap_venv(log=log) is not None
    if kind == "apt" and not assume_yes and os.geteuid() != 0:
        # sudo may prompt; make that visible rather than hanging silently
        log("this needs root:")
    log("running: %s" % " ".join(cmd))
    try:
        p = subprocess.run(cmd, text=True)
        ok = p.returncode == 0
    except Exception as e:
        log("install failed: %s" % e)
        return False
    log("install %s" % ("succeeded" if ok else "FAILED"))
    if not ok and kind == "apt":
        log("")
        log("If you cannot use sudo here, run this yourself:")
        log("    %s" % " ".join(cmd))
    return ok


def _in_virtualenv():
    return (hasattr(sys, "real_prefix")
            or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
            or bool(os.environ.get("VIRTUAL_ENV")))


def prompt_and_install(log=print, input_fn=input):
    """Interactive: show what is missing, offer to install what we can.

    Returns True if nothing was missing or the install succeeded.
    """
    res = check_all()
    kind, cmd, note = install_plan(res)
    log(report(res, show_ok=False))
    if kind == "none":
        return not blocking(res)
    log("")
    try:
        ans = input_fn("Install the missing package(s) now? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        log("")
        return False
    if ans not in ("y", "yes"):
        log("skipped — nothing installed")
        return False
    ok = install(log=log)
    if ok:
        log("")
        log(report(check_all(), show_ok=False))
    return ok
