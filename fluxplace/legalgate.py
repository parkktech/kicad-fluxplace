"""Legality against the INPUT board: what a stage may not add, and the repair that takes it back out.

A placement stage inherits whatever the input board already violates (those are not its to fix) and must add nothing:
no pad short, courtyard overlap, hole clash, outline escape, keep-out or no-through-hole hit. Three pieces:

  * `baseline(B, cons)` / `known(iss)` / `new_issues(iss, base)`: the issues of the board as it comes in, and the set a
    later board adds. The baseline is taken BEFORE any seating, never from a board the stage already rearranged.
  * `revert_illegal(B, L, known, orig, fixed)`: a model-level loop that returns the moved part of every NEW issue to its
    pose on the input board until none is left (a part back at its input pose is legal there, so the loop ends).
  * `repair_file(out, orig_path, cons, base)` and `drc_new(out, orig)`: the same on a written board, and the KiCad DRC
    (error severity) of the written board set against the input's, the ground truth that gates the stage.
"""
import json
import math
import os
import re
import shutil
import subprocess
import tempfile

from . import geom as GM

KINDS = ("overlap", "outside", "forbidden", "no_tht", "pad_short", "hole", "hardware")
DRC_TYPES = ("shorting_items", "courtyards_overlap", "clearance", "hole_clearance", "solder_mask_bridge")
MOVE_EPS = 1e-3


def baseline(B, cons):
    """Legal.issues() of the board model as loaded, with the stage's own held parts fixed."""
    from . import legality as LGL, modules as MD
    return LGL.Legal(B, cons, MD.fixed_set(B, cons)).issues()


def new_issues(iss, base):
    """{kind: sorted items in `iss` that `base` does not have}."""
    return {k: sorted(set(v) - set((base or {}).get(k, []))) for k, v in iss.items()}


def count_new(new):
    return sum(len(v) for v in new.values())


def known(base):
    """predicate (ref, kind, detail) -> True when `base` (an `issues()` dict of the input) already has that issue."""
    pairs = {k: {frozenset(s.split("/")) for s in (base or {}).get(k, [])} for k in ("overlap", "pad_short", "hole")}
    single = {k: set((base or {}).get(k, [])) for k in ("outside", "forbidden", "no_tht", "hardware")}
    outside = {s.split(" ")[0] for s in (base or {}).get("outside", [])}

    def is_known(ref, kind, d):
        if kind in ("overlap", "hole"):
            return frozenset((ref, d)) in pairs[kind]
        if kind == "pad_short":
            return frozenset(d.split("/")) in pairs["pad_short"]
        if kind == "outside":
            return ref in outside
        if kind == "hardware":
            return f"{ref} in {d}" in single["hardware"]
        return f"{ref} in {d}" in single.get(kind, ())
    return is_known


def part_new(L, ref, is_known, court=None, pads=None):
    """[(kind, detail)] issues of `ref` (as is, or at a hypothetical court / pads) the input board did not have."""
    return [(k, d) for k, d in L.part_issues(ref, court=court, pads=pads) if not is_known(ref, k, d)]


def poses(B):
    return {r: (p["pos"][0], p["pos"][1], p["angle"], p["side"]) for r, p in B["parts"].items()}


def displaced(part, o):
    """mm a part sits from its pose `o` (inf when it is turned or on the other side)."""
    if part["side"] != o[3] or abs(((part["angle"] - o[2]) + 180) % 360 - 180) > 1e-3:
        return math.inf
    return math.hypot(part["pos"][0] - o[0], part["pos"][1] - o[1])


def _other(ref, kind, d):
    if kind in ("overlap", "hole"):
        return d
    if kind == "pad_short":
        a, b = (x.split(".")[0] for x in d.split("/"))
        return b if a == ref else a
    return None


def revert_part(B, L, ref, o):
    """put `ref` back at pose `o` (x, y, angle, side) on the model (a flipped part is flipped back first)."""
    p = B["parts"][ref]
    if p["side"] != o[3]:
        GM.flip(p)
    GM.place(p, o[0], o[1], o[2])
    L.refresh(ref)


def revert_illegal(B, L, is_known, orig, fixed=(), refs=None, limit=None):
    """Every part with an issue the input does not have, and that moved, goes back to its input pose, the one that moved farther
    first, until no new issue is left. -> ({ref: [why]} reverted, [(ref, kind, detail)] left: nothing movable explains them)."""
    reverted = {}
    movable = lambda r: r in B["parts"] and r in orig and r not in fixed and not B["parts"][r].get("locked")
    for _ in range(limit or 4 * len(B["parts"]) + 8):
        bad = [(r, k, d) for r in sorted(refs or B["parts"]) for k, d in part_new(L, r, is_known)]
        if not bad:
            return reverted, []
        progressed = False
        for r, k, d in bad:
            cands = [x for x in (r, _other(r, k, d)) if x and movable(x) and displaced(B["parts"][x], orig[x]) > MOVE_EPS and x not in reverted]
            if not cands:
                continue
            v = max(cands, key=lambda x: (displaced(B["parts"][x], orig[x]), x))
            revert_part(B, L, v, orig[v])
            reverted.setdefault(v, []).append(f"{k} {d}".strip())
            progressed = True
            break
        if not progressed:
            return reverted, bad
    return reverted, [(r, k, d) for r in sorted(refs or B["parts"]) for k, d in part_new(L, r, is_known)]


def write_reverted(path, orig_path, reverted, orig):
    """write the reverted parts back at their input pose in the board file `path` (in place)."""
    from . import boardwrite as BW, flip as FL, routediag as RD
    if not reverted:
        return
    B = RD.load(path)
    side = [r for r in reverted if r in B["parts"] and B["parts"][r]["side"] != orig[r][3]]
    if side:
        tmp = path + ".flip"
        FL.flip_file(path, tmp, side)
        os.replace(tmp, path)
    tmp = path + ".tmp"
    BW.move_file(path, tmp, {r: (orig[r][0], orig[r][1], orig[r][2]) for r in reverted}, post=False)
    os.replace(tmp, path)


def repair_file(path, orig_path, cons, base, log=print):
    """the written board `path`: every part that adds a legality issue (against `base`, the input's) goes back to its pose on
    `orig_path`. -> {"reverted": {ref: [why]}, "left": [(ref, kind, detail)]}."""
    from . import legality as LGL, modules as MD, routediag as RD
    B, Bo = RD.load(path), RD.load(orig_path)
    fixed = MD.fixed_set(B, cons)
    L = LGL.Legal(B, cons, fixed)
    orig = poses(Bo)
    rev, left = revert_illegal(B, L, known(base), orig, fixed)
    write_reverted(path, orig_path, rev, orig)
    for r in sorted(rev):
        log(f"floorplan: {r} back at its input pose ({'; '.join(rev[r][:2])})")
    return {"reverted": rev, "left": left}


# ---------------------------------------------------------------- KiCad DRC
def _drc(board, pro_from=None, cli="kicad-cli", timeout=600):
    """error-severity violations of `board` (kicad-cli, JSON), run on a throwaway copy with its project beside it."""
    pro = os.path.splitext(pro_from or board)[0] + ".kicad_pro"
    with tempfile.TemporaryDirectory() as td:
        tb = os.path.join(td, "b.kicad_pcb")
        shutil.copyfile(board, tb)
        if os.path.exists(pro):
            shutil.copyfile(pro, os.path.join(td, "b.kicad_pro"))
        rpt = os.path.join(td, "drc.json")
        try:
            subprocess.run([cli, "pcb", "drc", "--format", "json", "--severity-error", "--output", rpt, tb],
                           capture_output=True, text=True, timeout=timeout)
            return json.load(open(rpt)).get("violations", [])
        except (OSError, subprocess.SubprocessError, ValueError):
            return None


def _key(v):
    return (v.get("type"), tuple(sorted(re.sub(r"\s+", " ", i.get("description", "")) for i in v.get("items", []))))


def _refs(v):
    out = set()
    for i in v.get("items", []):
        m = re.search(r"\bof (\S+) on \S+$|^Footprint (\S+)", i.get("description", ""))
        if m:
            out.add(m.group(1) or m.group(2))
    return sorted(out)


def drc_new(path, orig_path, cli="kicad-cli"):
    """DRC violations (error severity, the legality types) on `path` that `orig_path` does not have.
    -> None when kicad-cli did not run, else {"n": {type: count}, "items": [violation], "refs": [pairs of refs]}."""
    out, inp = _drc(path, cli=cli), _drc(orig_path, pro_from=orig_path, cli=cli)
    if out is None:
        return None
    seen = {}
    for v in inp or []:
        seen[_key(v)] = seen.get(_key(v), 0) + 1
    new = []
    for v in out:
        if v.get("type") not in DRC_TYPES:
            continue
        k = _key(v)
        if seen.get(k, 0) > 0:
            seen[k] -= 1
            continue
        new.append(v)
    n = {}
    for v in new:
        n[v["type"]] = n.get(v["type"], 0) + 1
    return {"n": n, "items": new, "pairs": [_refs(v) for v in new]}


def drc_line(d):
    n = d["n"]
    first = "; ".join(f"{v['type']} {' / '.join(i.get('description', '') for i in v.get('items', []))}" for v in d["items"][:3])
    return (f"floorplan: ILLEGAL - {n.get('shorting_items', 0)} shorts, {n.get('courtyards_overlap', 0)} overlaps, "
            f"{n.get('clearance', 0) + n.get('hole_clearance', 0) + n.get('solder_mask_bridge', 0)} clearance (new vs input): {first}")


def gate(path, orig_path, cons, base, ref_path=None, log=print, rounds=3, cli="kicad-cli"):
    """output gate: the KiCad DRC of `path` adds nothing to the input's. A new item is repaired first (the parts it names go back
    to their pose on `orig_path`, the one that moved farther first; `ref_path` = the board the DRC baseline is taken on, default `orig_path`); what is left is returned. -> None (no kicad-cli) or the
    drc_new() dict still new ("items" empty = clean), with "reverted"."""
    from . import legality as LGL, modules as MD, routediag as RD
    reverted = {}
    for _ in range(rounds + 1):
        d = drc_new(path, ref_path or orig_path, cli=cli)
        if d is None:
            return None
        if not d["items"]:
            return {"n": {}, "items": [], "pairs": [], "reverted": reverted}
        if _ == rounds:
            break
        B, Bo = RD.load(path), RD.load(orig_path)
        fixed = MD.fixed_set(B, cons)
        orig = poses(Bo)
        victims = set()
        for pair in d["pairs"]:
            c = [x for x in pair if x in orig and x in B["parts"] and x not in fixed and displaced(B["parts"][x], orig[x]) > MOVE_EPS]
            if c:
                victims.add(max(c, key=lambda x: (displaced(B["parts"][x], orig[x]), x)))
        if not victims:
            break
        write_reverted(path, orig_path, sorted(victims), orig)
        for v in sorted(victims):
            reverted.setdefault(v, []).append("kicad drc")
            log(f"floorplan: {v} back at its input pose (KiCad DRC)")
    d["reverted"] = reverted
    return d
