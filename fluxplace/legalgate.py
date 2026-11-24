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
DRC_TYPES = ("shorting_items", "courtyards_overlap", "clearance", "hole_clearance", "solder_mask_bridge", "copper_edge_clearance",
             "hole_to_hole", "items_not_allowed", "npth_inside_courtyard", "pth_inside_courtyard")
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


NUDGE_MM, NUDGE_STEP = 4.0, 0.25


def nudge(B, L, ref, is_known):
    """the nearest pose of `ref` within NUDGE_MM of where it stands (its angle first, then turned) that adds no issue the input lacked."""
    part = B["parts"][ref]
    x0, y0 = part["pos"]
    a0 = part["angle"]
    n = int(NUDGE_MM / NUDGE_STEP)
    cands = sorted((math.hypot(i, j) * NUDGE_STEP + (0 if k == 0 else 0.5 * k), i, j, da)
                   for i in range(-n, n + 1) for j in range(-n, n + 1) for k, da in enumerate((0, 90, 180, 270)) if math.hypot(i, j) * NUDGE_STEP <= NUDGE_MM)
    for _d, i, j, da in cands:
        x, y, a = x0 + i * NUDGE_STEP, y0 + j * NUDGE_STEP, (a0 + da) % 360
        pads, court = GM.moved(part, x, y, a)
        if not part_new(L, ref, is_known, court=court, pads=pads):
            return x, y, a
    return None


def fix_part(B, L, ref, orig, is_known):
    """put `ref` right: the nearest legal pose near where it stands, else its input pose when that is free where the others stand now.
    -> "moved" | "input" | None (nothing legal: the part is left as it was)."""
    part = B["parts"][ref]
    here = (part["pos"][0], part["pos"][1], part["angle"], part["side"])
    hit = nudge(B, L, ref, is_known)
    if hit:
        GM.place(part, *hit)
        L.refresh(ref)
        return "moved"
    revert_part(B, L, ref, orig[ref])
    if not part_new(L, ref, is_known):
        return "input"
    revert_part(B, L, ref, here)
    return None


def revert_illegal(B, L, is_known, orig, fixed=(), refs=None, limit=None):
    """Every part with an issue the input does not have, and that moved, is put right (`fix_part`: the nearest legal pose, else its input
    pose when that is free), the one that moved farther first, until no new issue is left. A part with no legal pose is left and reported.
    -> ({ref: [why]} changed, [(ref, kind, detail)] left)."""
    changed, stuck = {}, set()
    movable = lambda r: r in B["parts"] and r in orig and r not in fixed and not B["parts"][r].get("locked") and r not in stuck
    for _ in range(limit or 4 * len(B["parts"]) + 8):
        bad = [(r, k, d) for r in sorted(refs or B["parts"]) for k, d in part_new(L, r, is_known)]
        if not bad:
            return changed, []
        progressed = False
        for r, k, d in bad:
            cands = [x for x in (r, _other(r, k, d)) if x and movable(x) and displaced(B["parts"][x], orig[x]) > MOVE_EPS]
            if not cands:
                continue
            v = max(cands, key=lambda x: (displaced(B["parts"][x], orig[x]), x))
            how = fix_part(B, L, v, orig, is_known)
            if how is None:
                stuck.add(v)
            else:
                changed.setdefault(v, []).append(f"{how}: {k} {r if _other(r, k, d) == v else d}".strip())
            progressed = True
            break
        if not progressed:
            return changed, bad
    return changed, [(r, k, d) for r in sorted(refs or B["parts"]) for k, d in part_new(L, r, is_known)]


def write_poses(path, B, refs, was):
    """write the poses the model B now has for `refs` into the board file `path` (in place); `was` = {ref: side} the file has."""
    from . import boardwrite as BW, flip as FL
    if not refs:
        return
    side = [r for r in refs if B["parts"][r]["side"] != was[r]]
    if side:
        tmp = path + ".flip"
        FL.flip_file(path, tmp, side)
        os.replace(tmp, path)
    tmp = path + ".tmp"
    BW.move_file(path, tmp, {r: (B["parts"][r]["pos"][0], B["parts"][r]["pos"][1], B["parts"][r]["angle"]) for r in refs}, post=False)
    os.replace(tmp, path)


def repair_file(path, orig_path, cons, base, log=print, refs=None):
    """the written board `path`: every part that adds a legality issue (against `base`, the input's) is put right (nearest legal pose, else
    its pose on `orig_path` when free; `refs` limits the check). -> {"reverted": {ref: [why]}, "left": [(ref, kind, detail)]}."""
    from . import legality as LGL, modules as MD, routediag as RD
    B, Bo = RD.load(path), RD.load(orig_path)
    fixed = MD.fixed_set(B, cons)
    L = LGL.Legal(B, cons, fixed)
    orig, was = poses(Bo), {r: p["side"] for r, p in B["parts"].items()}
    rev, left = revert_illegal(B, L, known(base), orig, fixed, refs=refs)
    write_poses(path, B, sorted(rev), was)
    if rev:
        log(f"floorplan: {len(rev)} part(s) put right after the search (nearest legal pose, else the input pose): "
            + ", ".join(f"{r} ({rev[r][0]})" for r in sorted(rev)[:6]) + (" ..." if len(rev) > 6 else ""))
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


def drc_new(path, orig_path, cli="kicad-cli", ref=None):
    """DRC violations (error severity, the legality types) on `path` that `orig_path` does not have (`ref`: its violations, run once).
    -> None when kicad-cli did not run on either board (the gate fails closed), else {"n": {type: count}, "items": [violation], "pairs": [refs]}."""
    out = _drc(path, cli=cli)
    inp = ref if ref is not None else _drc(orig_path, pro_from=orig_path, cli=cli)
    if out is None or inp is None:
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
    ref = _drc(ref_path or orig_path, pro_from=ref_path or orig_path, cli=cli)       # the input's DRC, once
    for _ in range(rounds + 1):
        d = drc_new(path, orig_path, cli=cli, ref=ref)
        if d is None:
            return None
        if not d["items"]:
            return {"n": {}, "items": [], "pairs": [], "reverted": reverted}
        if _ == rounds:
            break
        victims = {x for pair in d["pairs"] for x in pair}
        if not victims:
            break
        rp = repair_file(path, orig_path, cons, base, log=log, refs=sorted(victims))
        for v, w in rp["reverted"].items():
            reverted.setdefault(v, []).extend(w)
        if not rp["reverted"]:
            break
    d["reverted"] = reverted
    return d
