"""Density-driven back fill: when `[placement] back_fill = "max"`, or the policy lets parts go to the back and the front is crowded,
free parts and whole modules move to the back, hottest cells first.  Called by `modules.build` after the seating (the supports have
already been tried on the back directly under their owner's pin by `clusters.seat` in its dense mode).

  singles   a free single-part module that `backside.back_ok` clears: flipped in place (`geom.flip`, same origin);
  modules   an IC module whose every member clears `back_ok` (height, class, closed areas), whose anchor is not a fine-pitch part (its
            breakout is the floorplan's), mirrored as a unit about the anchor's x: every member is flipped and reflected, so the
            internal geometry is the mirror image.  `flip.verify` re-checks every part when the board is written.

Order: relief per area, from the cell with the highest cover first.  A candidate is kept only when the model shows no new legality
issue against the input's own (`legalgate.part_new`: overlaps, back-closed areas, board edge, through-hole leads).  Stop: the front is
under the warn line and no real hot cell is left (`max`: every eligible candidate), and never past the warn line of the back itself.
Parts that stay on the front: connectors, RF / pair endpoints, template parts, LEDs, tall / heavy parts, anything `back_ok` refuses.
"""
import copy
import math

from . import geom as GM


def _area(p):
    c = p["court"]
    return max((c[2] - c[0]) * (c[3] - c[1]), 0.0)


def run(B, cons, spec, mods, of, fixed, bk, L, flips, known, log=None):
    """-> {"singles": [refs], "modules": [anchors], "mm2": area moved, "skipped": {reason: n}}"""
    from . import density as DN, escapeplan as EP, legalgate as LG
    parts = B["parts"]
    lim = DN.limits(cons)
    maxmode = ((cons or {}).get("placement") or {}).get("back_fill") == "max"
    u = DN.usage(B, cons, spec)
    Fs, Bs = u["sides"]["F"], u["sides"]["B"]
    cover = {(c["x"], c["y"]): c["cover"] for c in Fs["cells"] if not c["big"]}
    pr = EP.plan_params(cons)
    ctx = bk.ctx
    loadF, loadB = Fs["load_mm2"], Bs["load_mm2"]
    res = {"singles": [], "modules": [], "mm2": 0.0, "skipped": {}}
    cands = []
    for key, m in sorted(mods.items()):
        refs = sorted(m["refs"])
        anc = m["anchor"]
        if m.get("group") or any(r in fixed for r in refs) or any(parts[r]["side"] != "F.Cu" or parts[r]["tht"] for r in refs):
            continue
        if len(refs) > 1 and (EP.is_fine(parts[anc], pr) or not all(bk(r) for r in refs)):
            res["skipped"]["module not eligible"] = res["skipped"].get("module not eligible", 0) + 1
            continue
        if len(refs) == 1 and not bk(refs[0]):
            continue
        cx, cy = parts[anc]["pos"]
        cv = cover.get(DN._cell_of(u, cx, cy), 0.0)
        cands.append((-cv, -sum(_area(parts[r]) for r in refs), key, refs, anc))
    cands.sort(key=lambda t: (t[0], t[1], t[2]))
    for ncv, narea, key, refs, anc in cands:
        area = -narea
        warn_front = loadF / Fs["usable_mm2"] >= lim["warn"] * 0.97 if Fs["usable_mm2"] else False
        if not maxmode and not warn_front and -ncv < lim["cell_fail"]:
            continue
        if Bs["usable_mm2"] and (loadB + area) / Bs["usable_mm2"] > lim["warn"]:
            res["skipped"]["back at the warn line"] = res["skipped"].get("back at the warn line", 0) + 1
            continue
        snap = {r: copy.deepcopy(parts[r]) for r in refs}
        ax = parts[anc]["pos"][0]
        for r in refs:
            p = parts[r]
            x, y = p["pos"]
            GM.flip(p)
            if len(refs) > 1:
                GM.place(p, 2 * ax - x, y, p["angle"])
            L.refresh(r)
        if any(LG.part_new(L, r, known) for r in refs):
            for r in refs:
                parts[r].clear()
                parts[r].update(snap[r])
                L.refresh(r)
            res["skipped"]["not legal on the back"] = res["skipped"].get("not legal on the back", 0) + 1
            continue
        for r in refs:
            flips.symmetric_difference_update({r})
        loadF -= area
        loadB += area
        res["mm2"] += area
        (res["modules"] if len(refs) > 1 else res["singles"]).append(anc if len(refs) > 1 else refs[0])
        if len(refs) > 1:
            mods[key]["mirror"] = True
    if log:
        log(f"back fill: {len(res['singles'])} single part(s), {len(res['modules'])} module(s) mirrored, {res['mm2']:.0f} mm2 moved to the back"
            + (f"; skipped {res['skipped']}" if res["skipped"] else ""))
    return res
