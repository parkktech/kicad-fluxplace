"""`fluxplace join-gaps BOARD --out O [--gap 0.3]`: close small same-net copper gaps with a straight segment.

Same-net ends (track end <-> track end / pad / via) in different connectivity clusters, at most --gap mm apart, are joined
by a segment of the end track's width when it keeps the clearance (netclass, holes, board edge, keep-out rule areas) to all
other-net copper.  The geometry runs in pcbnew (joingaps_helper.py); the segments are appended as text.  Reports closed and
skipped (with the reason).  Also called by the route stage after every round (`run`)."""
import json
import os
import subprocess
import uuid

from . import boardtext as BT
from . import routeconn as RC

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "joingaps_helper.py")
GAP = 0.3


def _f(v):
    return f"{v:.6f}".rstrip("0").rstrip(".")


def plan(board, gap=GAP, extra=0.0, timeout=900):
    """-> {add, skipped, gap, candidates} (board not touched)."""
    r = subprocess.run([RC.PY, HELPER, board, "--gap", str(gap), "--extra-clearance", str(extra)],
                       capture_output=True, text=True, timeout=timeout)
    lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
    if r.returncode != 0 or not lines:
        raise RuntimeError("join-gaps helper failed: " + (r.stderr or r.stdout)[-300:])
    return json.loads(lines[-1])


def segment_block(s):
    return ("\t(segment\n\t\t(start %s %s)\n\t\t(end %s %s)\n\t\t(width %s)\n\t\t(layer \"%s\")\n\t\t(net \"%s\")\n"
            "\t\t(uuid \"%s\")\n\t)" % (_f(s["a"][0]), _f(s["a"][1]), _f(s["b"][0]), _f(s["b"][1]), _f(s["width"]),
                                         s["layer"], s["net"].replace('"', '\\"'), uuid.uuid4()))


def run(board, out=None, gap=GAP, extra=0.0):
    """Join the gaps of `board`; written to `out` (default in place) when anything closed. -> report dict."""
    rep = plan(board, gap, extra)
    if rep["add"]:
        text = open(board, errors="surrogateescape").read()
        text = BT.append_blocks(text, [segment_block(s) for s in rep["add"]])
        with open(out or board, "w", errors="surrogateescape") as fh:
            fh.write(text)
    elif out and os.path.abspath(out) != os.path.abspath(board):
        import shutil
        shutil.copy(board, out)
    if out and os.path.abspath(out) != os.path.abspath(board):
        import shutil
        for ext in (".kicad_pro", ".fluxmap.json"):           # the rules (netclasses, clearances) travel with the board
            a, b = os.path.splitext(board)[0] + ext, os.path.splitext(out)[0] + ext
            if os.path.exists(a) and not os.path.exists(b):
                shutil.copy(a, b)
    rep["closed"] = len(rep["add"])
    rep["n_skipped"] = len(rep["skipped"])
    return rep


def summary(rep):
    from collections import Counter
    why = Counter(s["reason"] for s in rep["skipped"] if s["reason"] != "already connected")
    return (f"join-gaps: closed {rep['closed']} gap(s) <= {rep['gap']:g} mm, skipped {sum(why.values())}"
            + (" (" + ", ".join(f"{k} x{v}" for k, v in why.most_common(4)) + ")" if why else ""))


def cmd_join(a):
    out = a.out
    if os.path.abspath(out) == os.path.abspath(a.board):
        raise SystemExit("join-gaps: --out must differ from the input board")
    rep = run(a.board, out, a.gap, a.extra_clearance)
    print(summary(rep))
    for s in rep["add"][:10]:
        print(f"  + {s['net']} {s['layer']} ({s['a'][0]:.3f},{s['a'][1]:.3f}) -> {s['kind']} gap {s['gap']:.3f} w {s['width']:g}")
    for s in [x for x in rep["skipped"] if x["reason"] != "already connected"][:10]:
        print(f"  - {s['net']} {s['layer']} {s['at']} gap {s['gap']:.3f}: {s['reason']}")
    if a.check:
        b, n = RC.open_by_net(a.board, fill=True), RC.open_by_net(out, fill=True)
        print(f"open connections {sum(b.values())} -> {sum(n.values())}")
    print(f"wrote {out}")
    return 0


def add_parser(sub):
    p = sub.add_parser("join-gaps", help="close small same-net copper gaps (track end <-> track/pad/via) with a legal segment")
    p.add_argument("board")
    p.add_argument("--out", required=True)
    p.add_argument("--gap", type=float, default=GAP, help="largest gap to close, mm (default 0.3)")
    p.add_argument("--extra-clearance", type=float, default=0.0, help="clearance margin on top of the rules, mm")
    p.add_argument("--check", action="store_true", help="print open connections before/after (pcbnew, zones refilled)")
    p.set_defaults(fn=cmd_join)
