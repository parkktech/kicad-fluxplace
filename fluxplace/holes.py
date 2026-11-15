"""HOLE_TO_HOLE — the edge-to-edge distance between every pair of drilled holes.

Vias, plated pad holes and non-plated pad holes all go through the same
drill house, and a fab refuses two drills that break into each other. DRC
checks this against the board's own design rule; this check takes the
minimum from the fab (the `[fab] profile` name in profiles.PROFILES) or,
when the profile names no hole rule, from the board's .kicad_pro
(`board.design_settings.rules.min_hole_to_hole`). No literal default: when
neither exists the check says so (INFO) and does not guess.

Holes are listed from the board file (vias + every drilled pad, PTH and NPTH,
with absolute centres) when it is readable, else from `facts` (vias +
pth_pads, the same data every other check uses). A spatial grid keeps the
pair search linear in the hole count.
"""
import json
import math
import os

from . import boardfile as BF

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"


def _f(level, code, msg, refs=()):
    return {"level": level, "code": code, "msg": msg, "refs": sorted(set(refs))}


def required_mm(cons, board_path):
    """-> (min hole-to-hole mm, source label) or (None, reason)."""
    from .profiles import PROFILES
    from . import fabcaps as FC
    names = FC.profile_list(((cons or {}).get("fab") or {}).get("profile"))
    name = " + ".join(names)
    if len(names) == 1 and name in PROFILES and PROFILES[name].get("hole_to_hole") is not None:
        return float(PROFILES[name]["hole_to_hole"]), f"fab profile {name}"
    if name and not (len(names) == 1 and name in PROFILES):
        from . import constraints as C
        lim = C.fab_profile(cons, board=board_path)["hard"]       # unknown name -> FabProfileError, loud
        if lim.get("hole_to_hole_via") is not None:
            return float(lim["hole_to_hole_via"]), f"fab profile {name} (via-to-via figure)"
    if board_path:
        try:
            pro = json.load(open(os.path.splitext(board_path)[0] + ".kicad_pro"))
            v = pro["board"]["design_settings"]["rules"].get("min_hole_to_hole")
            if v:
                return float(v), "board .kicad_pro min_hole_to_hole"
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return None, "no hole-to-hole minimum in the [fab] profile or the .kicad_pro"


def _file_pad_holes(path):
    """Every drilled pad in the board file (PTH and NPTH): dicts with ref, pad, x, y, drill_mm."""
    root = BF.parse(open(path, errors="replace").read())
    out = []
    for fp in BF._kids(root, "footprint"):
        props = {}
        for pr in BF._kids(fp, "property"):
            a = BF._atoms(pr)
            if len(a) >= 2:
                props.setdefault(a[0], a[1])
        ref = props.get("Reference", "")
        at = BF._nums(BF._first(fp, "at")) + [0.0, 0.0, 0.0]
        fx, fy, fang = at[0], at[1], at[2]
        for pad in BF._kids(fp, "pad"):
            a = BF._atoms(pad)
            if not a or not a[0]:
                continue
            dk = BF._first(pad, "drill")
            if not dk:
                continue
            nums = BF._nums(dk)
            if not nums:
                continue
            # (drill d) or (drill oval w h): an oval is held to its narrow side
            d = min(nums[:2]) if BF._atoms(dk)[:1] == ["oval"] and len(nums) >= 2 else nums[0]
            if d <= 0:
                continue
            pa = BF._nums(BF._first(pad, "at")) + [0.0, 0.0, 0.0]
            dx, dy = BF._rot(pa[0], pa[1], fang)
            out.append({"ref": ref, "pad": a[0], "x": fx + dx, "y": fy + dy, "drill_mm": d})
    return out


def holes_of(facts, board_path=None):
    """-> [{"label", "ref" (None for a via), "pad", "x", "y", "r"}] for every drilled hole."""
    rows = []
    for i, v in enumerate(facts.get("vias") or []):
        if v.get("drill_mm", 0) > 0:
            rows.append({"label": f"via@{v['x']:.3f},{v['y']:.3f}", "ref": None, "pad": None,
                         "x": v["x"], "y": v["y"], "d": v["drill_mm"]})
    if isinstance(board_path, str) and os.path.isfile(board_path):
        pads = _file_pad_holes(board_path)
    else:
        pads = [dict(ref=p["ref"], pad=p["pad"], x=p["x"], y=p["y"], drill_mm=p["drill_mm"])
                for p in facts.get("pth_pads") or []]
    for p in pads:
        rows.append({"label": f"{p['ref']}.{p['pad']}", "ref": p["ref"], "pad": p["pad"],
                     "x": p["x"], "y": p["y"], "d": p["drill_mm"]})
    for r in rows:
        r["r"] = r.pop("d") / 2.0
    return rows


def check_hole_spacing(facts, cons):
    board_path = facts.get("board_path") or facts.get("board")
    if not isinstance(board_path, str):
        board_path = None
    req, src = required_mm(cons, board_path)
    if req is None:
        return [_f(INFO, "HOLE_TO_HOLE_UNDEFINED", f"hole-to-hole not checked: {src}")]
    holes = holes_of(facts, board_path)
    if len(holes) < 2:
        return []
    cell = req + 2 * max(h["r"] for h in holes)
    grid = {}
    for i, h in enumerate(holes):
        grid.setdefault((math.floor(h["x"] / cell), math.floor(h["y"] / cell)), []).append(i)
    out = []
    seen = set()
    for i, a in enumerate(holes):
        cx, cy = math.floor(a["x"] / cell), math.floor(a["y"] / cell)
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for j in grid.get((gx, gy), ()):
                    if j <= i:
                        continue
                    b = holes[j]
                    if a["ref"] is not None and a["ref"] == b["ref"] and a["pad"] == b["pad"]:
                        continue  # the same pad stack: one pad, one hole set
                    gap = math.hypot(a["x"] - b["x"], a["y"] - b["y"]) - a["r"] - b["r"]
                    if gap < req - 1e-6:
                        key = (a["label"], b["label"])
                        if key in seen:
                            continue
                        seen.add(key)
                        # FAIL: holes overlap or touch, or the minimum is a [fab] rule. A minimum
                        # that is only the .kicad_pro fallback is advisory: WARN, source named.
                        level = FAIL if (gap <= 1e-9 or not src.startswith("board .kicad_pro")) else WARN
                        note = "" if level == FAIL else " (advisory: .kicad_pro fallback minimum; a [fab] profile makes it FAIL)"
                        out.append(_f(level, "HOLE_TO_HOLE",
                                      f"{a['label']} to {b['label']}: edge-to-edge {gap:.3f} mm, "
                                      f"required {req:g} mm ({src}){note}",
                                      refs=[a["label"], b["label"]]))
    return out
