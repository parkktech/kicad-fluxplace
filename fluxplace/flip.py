"""Side flips through KiCad's own pcbnew. The text footprint writer (boardwrite.py) cannot mirror a
footprint, so a placer that wants a part on the other side hands the refs to a small helper process
(`flip_helper.py`, run by /usr/bin/python3 where `pcbnew` lives): pcbnew flips them (Footprint.Flip,
left/right about the origin) and saves a scratch board; only the flipped `(footprint ...)` nodes are
spliced into the original text, so everything else stays byte-for-byte. `geom.flip` is the same flip on the
text model (so a placer can score a flip without a process); `verify` checks that model, the re-parsed
file and pcbnew's own facts agree on pads (position, layers), side, angle and courtyard.

Measured on the 337-part UTV board (KiCad 10.0.5): 0.4 s per call; pad positions match the mirror model to
0.0000 mm, courtyard to <= 0.05 mm (the stroke width), angle 0 -> 180, 90 -> 90, 180 -> 0, 270 -> 270.
Cost: a flip is chosen only when the score gain exceeds `[placement.side] flip_cost` (score units).
"""
import copy
import json
import os
import subprocess
import tempfile

from . import boardwrite as BW
from . import geom as GM

PY = os.environ.get("FLUXPLACE_KICAD_PYTHON", "/usr/bin/python3")
HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flip_helper.py")
TOL_COURT = 0.06
TOL_PAD = 0.005


def available():
    try:
        r = subprocess.run([PY, "-c", "import pcbnew"], capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run(src, dst, refs):
    r = subprocess.run([PY, HELPER, src, dst] + list(refs), capture_output=True, text=True, timeout=300)
    if "@@FLIP@@" not in r.stdout:
        raise RuntimeError(f"pcbnew flip helper failed: {r.stderr[-300:] or r.stdout[-300:]}")
    return json.loads(r.stdout.split("@@FLIP@@", 1)[1])


def _nodes(text, refs):
    root_end = BW._end(text, text.index("("))
    out = {}
    for fs, fe in BW._children(text, 0, root_end, "footprint"):
        m = BW._REF.search(text, fs, fe)
        if m and m.group(1) in refs:
            out[m.group(1)] = (fs, fe)
    return out


def flip_file(src, dst, refs):
    """Flip `refs` in board `src`, write `dst`. -> pcbnew's facts per ref. Raises RuntimeError if
    pcbnew is unavailable or a ref is missing."""
    refs = sorted(set(refs))
    if not refs:
        BW.move_file(src, dst, {})
        return {}
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "flipped.kicad_pcb")
        facts = _run(src, tmp, refs)
        bad = [r for r, f in facts.items() if "error" in f]
        if bad:
            raise RuntimeError(f"flip: no such footprint(s): {', '.join(bad)}")
        a = open(src, errors="surrogateescape").read()
        b = open(tmp, errors="surrogateescape").read()
        na, nb = _nodes(a, set(refs)), _nodes(b, set(refs))
        miss = sorted(set(refs) - set(na) - set(nb))
        if miss or set(na) != set(nb):
            raise RuntimeError(f"flip: footprint node(s) missing: {miss or sorted(set(na) ^ set(nb))}")
        for r in sorted(refs, key=lambda r: -na[r][0]):
            a = a[:na[r][0]] + b[nb[r][0]:nb[r][1]] + a[na[r][1]:]
    with open(dst, "w", errors="surrogateescape") as fh:
        fh.write(a)
    return facts


def verify(B_before, dst, refs, facts):
    """Compare, for each ref, the analytic model flip (`geom.flip` on a copy of the part from
    `B_before`), the re-parsed output board and pcbnew's facts. -> list of mismatch strings (empty = all
    three agree)."""
    from . import routediag as RD
    B = RD.load(dst)
    bad = []
    for r in refs:
        p0, p1 = copy.deepcopy(B_before["parts"][r]), B["parts"][r]
        GM.flip(p0)
        f = facts[r]
        if p1["side"] != p0["side"] or ("B.Cu" if f["layer"] == "B.Cu" else "F.Cu") != p0["side"]:
            bad.append(f"{r}: side {p1['side']} / pcbnew {f['layer']} / model {p0['side']}")
        if abs(((p1["angle"] - p0["angle"]) + 180) % 360 - 180) > 1e-3:
            bad.append(f"{r}: angle file {p1['angle']} model {p0['angle']}")
        for q1 in p1["pads"]:           # pcbnew may reorder pads that share a number: match by position
            q0 = min((q for q in p0["pads"] if q["num"] == q1["num"]),
                     key=lambda q: abs(q["x"] - q1["x"]) + abs(q["y"] - q1["y"]) + (0 if q["layers"] == q1["layers"] else 100))
            if abs(q0["x"] - q1["x"]) > TOL_PAD or abs(q0["y"] - q1["y"]) > TOL_PAD or q0["layers"] != q1["layers"]:
                bad.append(f"{r}.{q0['num']}: model ({q0['x']:.3f}, {q0['y']:.3f}, {sorted(q0['layers'])}) file "
                           f"({q1['x']:.3f}, {q1['y']:.3f}, {sorted(q1['layers'])})")
            fx = [x for x in f["pads"].get(q1["num"], []) if abs(x[0] - q1["x"]) < 0.01 and abs(x[1] - q1["y"]) < 0.01]
            if not fx:
                bad.append(f"{r}.{q1['num']}: pcbnew has no pad at the file's position")
            elif not any(q1["layers"] <= set(x[2]) for x in fx):
                bad.append(f"{r}.{q1['num']}: pcbnew layers {[x[2] for x in fx]} vs file {sorted(q1['layers'])}")
        if f["court"] and max(abs(a - b) for a, b in zip(f["court"], p1["court"])) > TOL_COURT:
            bad.append(f"{r}: courtyard pcbnew {f['court']} file {[round(v, 3) for v in p1['court']]}")
        if max(abs(a - b) for a, b in zip(p0["court"], p1["court"])) > TOL_COURT:
            bad.append(f"{r}: courtyard model {[round(v, 3) for v in p0['court']]} file {[round(v, 3) for v in p1['court']]}")
    return bad
