"""Helpers of the slow-tier replays (tests/replay): run the real CLI on a copied board, measure with kicad-cli / pcbnew."""
import json, lzma, os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
ROOT = os.path.dirname(os.path.dirname(HERE))
PY = os.environ.get("PY", "/usr/bin/python3")
ROUTER_DIR = os.environ.get("REPLAY_ROUTER_DIR", "/var/kicad/utv-comms-bridge/W4/KiCadRoutingTools-v0.24.1")
ROUTER_PY = os.environ.get("REPLAY_ROUTER_PY", "/var/kicad/utv-comms-bridge/W4/krt-venv/bin/python")
JAR = os.environ.get("REPLAY_FREEROUTING_JAR", os.path.expanduser("~/tools/freerouting-2.5.0.jar"))
ROUTER_OK = all(os.path.exists(p) for p in (ROUTER_DIR, ROUTER_PY, JAR))
FLAGS = ["--flow", "constructive", "--router", "krt-whole", "--router-dir", ROUTER_DIR, "--router-py", ROUTER_PY,
         "--freerouting-jar", JAR, "--assume-defaults", "--allow-incomplete", "--allow-undocumented", "--no-api"]


def stage_version(tmp, ver):
    """Copy a stored update input into tmp: project dir `proj` (board, .kicad_pro, docs/datasheets) and a separate dir `cons`
    (constraints with a RELATIVE [docs] datasheets path, spec). -> (proj, cons)."""
    proj, cons = os.path.join(tmp, "proj"), os.path.join(tmp, "cons")
    os.makedirs(proj); os.makedirs(cons)
    with lzma.open(os.path.join(DATA, ver, "in.kicad_pcb.xz")) as src, open(os.path.join(proj, "in.kicad_pcb"), "wb") as dst:
        shutil.copyfileobj(src, dst)
    shutil.copy(os.path.join(DATA, "in.kicad_pro"), proj)
    shutil.copytree(os.path.join(DATA, "docs"), os.path.join(proj, "docs"))
    shutil.copy(os.path.join(DATA, ver, "constraints.toml"), cons)
    shutil.copy(os.path.join(DATA, "spec.json"), cons)
    return proj, cons


def pipeline(proj, board, cons_dir, frm, to, timeout, extra=()):
    """Run `fluxplace pipeline`; -> {"rc", "out", "secs", "log", "board"} (board = the pipeline's final board)."""
    log = os.path.join(proj, "replay.log")
    cmd = [PY, os.path.join(ROOT, "cli.py"), "pipeline", board, "--workdir", "run", "--constraints",
           os.path.join(cons_dir, "constraints.toml"), "--spec", os.path.join(cons_dir, "spec.json"),
           "--from", frm, "--to", to, "--route-budget", "900", *FLAGS, *extra]
    t0 = time.time()
    p = subprocess.run(cmd, cwd=proj, capture_output=True, text=True, timeout=timeout)
    out = p.stdout + p.stderr
    open(log, "w").write(out)
    final = os.path.join(proj, board)
    try:
        final = json.load(open(os.path.join(proj, "run", "state.json"))).get("board") or final
    except (OSError, ValueError):
        pass
    return {"rc": p.returncode, "out": out, "secs": round(time.time() - t0), "log": log, "board": final}


def drc_counts(board, pro, td):
    """kicad-cli DRC (zones refilled) of `board` under the project rules `pro` -> {"violations": n, "opens": n}.
    The board is copied next to the baseline .kicad_pro so every board is judged by the same rules."""
    os.makedirs(td, exist_ok=True)
    b = os.path.join(td, "m.kicad_pcb"); shutil.copy(board, b); shutil.copy(pro, os.path.join(td, "m.kicad_pro"))
    rep = os.path.join(td, "drc.json")
    subprocess.run(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", "--refill-zones",
                    "--output", rep, b], capture_output=True, text=True, timeout=900)
    d = json.load(open(rep))
    return {"violations": len(d.get("violations", [])), "opens": len(d.get("unconnected_items", []))}


def short_stubs(board):
    """Track segments shorter than 0.2 mm that KiCad's own connectivity flags `track_dangling` (kicad-cli DRC, default rules):
    [[net, len_mm, x, y]]. An end inside a same-net zone fill, on a via's copper or on a pad is connected for KiCad and is not
    listed (a pcbnew geometry test of ours over-counted exactly those: w101)."""
    td = tempfile.mkdtemp(prefix="stubs-")
    try:
        b = os.path.join(td, "m.kicad_pcb"); shutil.copy(board, b)
        rep = os.path.join(td, "drc.json")
        subprocess.run(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", "--refill-zones",
                        "--output", rep, b], capture_output=True, text=True, timeout=900)
        out = []
        for v in json.load(open(rep)).get("violations", []):
            if v.get("type") != "track_dangling":
                continue
            for it in v.get("items", []):
                m = re.match(r"Track \[(.*)\] on .*, length ([0-9.]+) mm", it.get("description", ""))
                if m and float(m.group(2)) < 0.2:
                    out.append([m.group(1), float(m.group(2)), it["pos"]["x"], it["pos"]["y"]])
        return out
    finally:
        shutil.rmtree(td, ignore_errors=True)


def stage_verdict(out, stage):
    """The pipeline's one-line verdict of `stage` ("pass" | "warn" | "fail" | "na" ...) or None."""
    m = re.findall(r"\[\d+/\d+\] %s: (\w+)" % re.escape(stage), out)
    return m[-1] if m else None


def title_rev(board):
    sys.path.insert(0, ROOT)
    from fluxplace import titleblock
    return titleblock.parse_block(open(board).read())["rev"]
