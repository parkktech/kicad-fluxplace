"""Multi-board matrix: run the constructive-flow commands over real boards (copies; never the originals).

    python -m fluxplace.boardmatrix OUTDIR [--timeout 300] [--jobs 3] [BOARD ...]

Per board: density, fanout --check-only, fanout on a stripped copy (tracks and vias removed), modules, snap-vias, and the
questions each command would raise with NO constraints file.  Every command runs in a subprocess under a timeout; a crash
is any exit code other than 0 / 1 / 3 or a Python traceback.  Commands that stop on questions (exit 3) are re-run with
--assume-defaults (a measurement, not a deliverable).  Writes OUTDIR/matrix.json and prints the table."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "cli.py")
PY = sys.executable


def board_roots():
    """Directories searched for real boards: the KiCad demo boards, plus any directory named in the
    FLUXPLACE_BOARD_DIRS environment variable (os.pathsep-separated). Nothing machine-specific is built in."""
    extra = [d for d in os.environ.get("FLUXPLACE_BOARD_DIRS", "").split(os.pathsep) if d]
    return extra + ["/usr/share/kicad/demos", "/usr/local/share/kicad/demos"]


def default_boards():
    out = []
    for root in board_roots():
        for dp, _dn, fn in os.walk(root):
            for f in fn:
                if f.endswith(".kicad_pcb") and not re.search(r"-backups|\.fanout|\.floorplan|-bak", dp + f):
                    out.append(os.path.join(dp, f))
    return sorted(set(out))


def run(argv, cwd, timeout):
    """-> (rc | 'timeout', seconds, stdout+stderr)."""
    t = time.time()
    try:
        r = subprocess.run([PY, CLI, *argv], cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, time.time() - t, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired as e:
        return "timeout", time.time() - t, ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))


def crashed(rc, text):
    return rc not in (0, 1, 3, "timeout") or "Traceback (most recent call last)" in text


def strip_copper(src, dst):
    from fluxplace import boardtext as BT
    t = open(src, errors="surrogateescape").read()
    t = BT.remove_blocks(t, [k for _i, _j, k, _b in BT.scan(t)])
    open(dst, "w", errors="surrogateescape").write(t)


def stats(path):
    from fluxplace import routediag as RD
    B = RD.load(path)
    return {"layers": len(B["copper"]), "parts": len(B["parts"]), "nets": len({p["net"] for p in B["pads"] if p["net"]}),
            "tracks": len(B["segs"]), "vias": len(B["vias"])}


def questions_of(path):
    """ids of every question the four wired commands raise on `path` with no constraints."""
    code = ("import sys, argparse, json\nsys.path.insert(0, %r)\nfrom fluxplace import questions as QS, routediag as RD\n"
            "B = RD.load(sys.argv[1]); ids = []\nfor cmd, topics in QS.TOPICS.items():\n"
            "    S = QS.Session(sys.argv[1], {}, {}, False, cmd)\n    from fluxplace import netclass as NC\n    cls = NC.classify(B, {}, None)\n"
            "    for t in topics: QS.CHECKS[t](S, B, {}, spec=None, cls=cls)\n    ids += [(cmd, q['id']) for q in S.open]\n"
            "print('@@Q' + json.dumps(ids))\n") % REPO
    r = subprocess.run([PY, "-c", code, path], capture_output=True, text=True, timeout=300)
    m = re.search(r"@@Q(.*)", r.stdout)
    return json.loads(m.group(1)) if m else [("error", (r.stderr or "")[-200:])]


def unique_names(boards):
    """{path: name}: the file stem, prefixed with its folder when two boards share a stem."""
    stems = [os.path.basename(b)[:-10] for b in boards]
    out = {}
    for b, st in zip(boards, stems):
        nm = st if stems.count(st) == 1 else os.path.basename(os.path.dirname(b)) + "__" + st
        out[b] = re.sub(r"[^A-Za-z0-9_.-]", "_", nm)
    return out


def one(src, outdir, timeout, name=None):
    name = name or re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(src)[:-10])
    d = os.path.join(outdir, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    b = os.path.join(d, "b.kicad_pcb")
    shutil.copy(src, b)
    pro = os.path.splitext(src)[0] + ".kicad_pro"
    if os.path.exists(pro):
        shutil.copy(pro, os.path.join(d, "b.kicad_pro"))
    row = {"board": name, "src": src, "steps": {}, "crashes": []}
    t0 = time.time()
    try:
        row.update(stats(b))
    except Exception as e:                                   # noqa: BLE001
        row["crashes"].append(f"load: {type(e).__name__}: {e}")
        return row
    stripped = os.path.join(d, "s.kicad_pcb")
    strip_copper(b, stripped)
    if os.path.exists(os.path.join(d, "b.kicad_pro")):
        shutil.copy(os.path.join(d, "b.kicad_pro"), os.path.join(d, "s.kicad_pro"))
    A = ["--assume-defaults"]
    steps = [("density", ["density", b]), ("fanout-check", ["fanout", b, "--check-only", "--no-drc", *A]),
             ("fanout", ["fanout", stripped, "--out", os.path.join(d, "fan.kicad_pcb"), *A]),
             ("modules", ["modules", stripped, "--map", os.path.join(d, "m.fluxmap.json"), "--no-fanout", *A]),
             ("snap-vias", ["snap-vias", b, "--out", os.path.join(d, "snap.kicad_pcb"), "--no-check"])]
    for nm, argv in steps:
        rc, secs, text = run(argv, d, timeout)
        row["steps"][nm] = {"rc": rc, "secs": round(secs, 1)}
        if crashed(rc, text):
            row["crashes"].append(f"{nm}: rc {rc}: " + (text.strip().splitlines() or [""])[-1][:160])
        if nm == "fanout":
            m = re.search(r"(\d+)/(\d+) required pins escaped", text)
            if m:
                row["escaped"], row["required"] = int(m.group(1)), int(m.group(2))
            m = re.search(r"DRC errors before (\d+) after (\d+); added (.*) \(", text)
            if m:
                row["drc_delta"] = int(m.group(2)) - int(m.group(1))
                row["drc_added"] = m.group(3)[:60]
    try:
        row["questions"] = [f"{c}:{q}" for c, q in questions_of(b)]
    except Exception as e:                                   # noqa: BLE001
        row["questions"] = [f"error {type(e).__name__}"]
    row["runtime"] = round(time.time() - t0)
    row["ran"] = all(isinstance(s["rc"], int) for s in row["steps"].values()) and not row["crashes"]
    return row


def table(rows):
    L = ["board | layers | parts | nets | ran | runtime s | fan-out escaped | DRC delta | questions | crashes"]
    for r in rows:
        esc = f"{r['escaped']}/{r['required']} ({100 * r['escaped'] / max(r['required'], 1):.0f} %)" if "escaped" in r else "-"
        qs = sorted({q.split(":", 1)[1].split(":")[0] for q in r.get("questions", [])})
        L.append(f"{r['board']} | {r.get('layers', '-')} | {r.get('parts', '-')} | {r.get('nets', '-')} | "
                 f"{'yes' if r.get('ran') else 'NO'} | {r.get('runtime', '-')} | {esc} | {r.get('drc_delta', '-')} | "
                 f"{len(r.get('questions', []))} ({', '.join(qs)[:50]}) | {len(r['crashes'])}"
                 + ("" if not r["crashes"] else " " + r["crashes"][0][:80]))
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("boards", nargs="*")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--jobs", type=int, default=3)
    a = ap.parse_args(argv)
    boards = a.boards or default_boards()
    os.makedirs(a.outdir, exist_ok=True)
    names = unique_names(boards)
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        rows = list(ex.map(lambda b: one(b, a.outdir, a.timeout, names[b]), boards))
    json.dump(rows, open(os.path.join(a.outdir, "matrix.json"), "w"), indent=1)
    print(table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
