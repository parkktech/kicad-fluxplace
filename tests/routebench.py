"""routebench: routing benchmark harness (ROUTER-C0). Dev script, not a user command.

Part 1 (B2): argparse, stage (copy + optional strip), metrics. Parts 2-3 (B3, B4) add engines, rows, output, baseline.
Interpreter: /usr/bin/python3. Never run a router from pytest: the engines are stubbed in tests.
"""
import argparse, json, os, re, shutil, sys
from collections import Counter

from fluxplace import boardwrite, corpus as C, gaps

KEYS = ("opens", "shorts", "errors", "new_err_keys", "err_types", "completion", "pairs", "rails", "score",
        "vias", "tracks", "track_mm", "tool", "board_sha")
SIDECARS = (".kicad_pro", ".kicad_prl", ".kicad_dru")
_NET_DECL = re.compile(r'\(net\s+(?:\d+\s+)?"([^"]*)"\)')


def _try(fn, *a, **kw):
    """One optional helper: its failure gives None for its keys, never a lost row."""
    try:
        return fn(*a, **kw)
    except Exception:
        return None


def net_names(text):
    return sorted({n for n in _NET_DECL.findall(text) if n})


def stage(ent, work):
    """Copy the board and its project sidecars into work/<name>/. The source is never written.
    When ent["bench"]["strip"] is true, the copy loses every track, arc and via of its nets (zones stay)."""
    d = os.path.join(work, ent["name"])
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    base = os.path.splitext(ent["board"])[0]
    board = os.path.join(d, os.path.basename(ent["board"]))
    shutil.copy2(ent["board"], board)
    for ext in SIDECARS:
        if os.path.isfile(base + ext):
            shutil.copy2(base + ext, os.path.join(d, os.path.basename(base) + ext))
    if ent.get("bench", {}).get("strip"):
        with open(board, encoding="utf-8") as f:
            text = f.read()
        text, _n = boardwrite.strip_nets(text, net_names(text))
        with open(board, "w", encoding="utf-8") as f:
            f.write(text)
    return board


def _pairs(board, cons):
    rows = gaps._pair_audit(board, cons)[2]
    out = {"complete": 0, "mismatch": 0, "half": 0, "open": 0}
    for r in rows:
        if r.get("status") in out:
            out[r["status"]] += 1
    return out


def _rails(gl):
    narrow = [g for g in gl if g["kind"] == "power-width"]
    vias = [g for g in gl if g["kind"] == "power-vias"]
    return {"narrow": len(narrow), "vias": len(vias),
            "mag": round(sum(float(g.get("mag", 0) or 0) for g in narrow + vias), 3)}


def metrics(board, cons=None):
    """Metrics of one board (table in ROUTER-C0 'Metrics'). Every optional part is in its own try: a failing
    helper gives null for its keys. Without constraints pairs, rails and score are null."""
    out = {k: None for k in KEYS}
    m = _try(gaps.measure, board)
    if m is not None:
        out.update(opens=m["opens"], shorts=m["shorts"], errors=m["errors"],
                   err_types=[[str(k), n] for k, n in Counter(
                       str(v.get("type")) for v in (m["drc"].get("violations") or [])
                       if v.get("severity") == "error").most_common(5)],
                   _err_keys={str(k): n for k, n in m["err_keys"].items()})
    if cons:
        if m is not None:
            gl = _try(gaps.classify, m["drc"], cons, board=board) or []
            out["pairs"] = _try(_pairs, board, cons)
            out["rails"] = _rails(gl)
            out["score"] = _try(gaps.score, m, gl)
    pcb = _try(C._pcb_metrics, board) or {}
    out["vias"] = pcb.get("board.vias")
    out["tracks"] = pcb.get("board.tracks")
    out["track_mm"] = pcb.get("board.track_mm")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="routing benchmark (ROUTER-C0)")
    ap.add_argument("--manifest", default=os.path.expanduser("~/.config/fluxplace/routebench.toml"))
    ap.add_argument("--board", action="append", default=[])
    ap.add_argument("--engine", action="append", default=[])
    ap.add_argument("--out")
    ap.add_argument("--work")
    ap.add_argument("--baseline")
    return ap.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
