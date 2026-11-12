"""Stage 7: stall handling for routing. Pure text except for the router itself.

  stage_timeout(a, cons)   per-stage wall-clock limit: `--route-timeout` if given, else
                           `[route] stage_timeout_s`, default 900
  escape_first(B, cls)     (first, rest): nets of fine-pitch ICs' signal pins go to the
                           router BEFORE power/other nets, so power cannot seal the
                           neighbouring pins (see below)
  stall_loop(...)          on timeout or failed nets: stop retrying, route-diagnose, and
                           (replace=True) apply its move list to UNLOCKED parts through the
                           cluster/legalise machinery, strip only the copper of nets that
                           touch a moved part, re-route only those nets; <= `rounds` times.
                           The diagnosis is always written and the moves always reported.

Escape-first is implemented as net ORDER (two router passes), not as escape stubs or
lane keep-outs: the external routers expose no per-net keep-out, and no router was run
here to verify one. Pass 1 routes the fine-pitch signal nets, pass 2 everything else
with the pass-1 copper kept.
"""
import json
import math
import os

from . import boardwrite as BW
from . import clusters as CL
from . import legalise as LG
from . import netclass as NC
from . import routediag as RD

DEFAULT_STAGE_S = 900


def stage_timeout(explicit, cons):
    if explicit:
        return int(explicit)
    return int(((cons or {}).get("route") or {}).get("stage_timeout_s", DEFAULT_STAGE_S))


def escape_first(B, cls):
    """-> (first, rest) lists of net names (nets with >= 2 pads, plane/rail nets excluded)."""
    fine = {r for r, p in B["parts"].items() if NC.is_ic(r, p, 5) and CL._pitch(p) <= 0.5}
    first, rest = set(), set()
    for n, ps in NC.net_pads(B).items():
        info = cls.get(n)
        if len(ps) < 2 or not info or info["class"] == "rail":
            continue
        (first if any(p["ref"] in fine for p in ps) else rest).add(n)
    return sorted(first), sorted(rest)


def affected_nets(B, refs, skip=()):
    out = set()
    for r in refs:
        for p in B["parts"][r]["pads"]:
            if p["net"] and p["net"] not in skip:
                out.add(p["net"])
    return out


def apply_moves(B, res, cons=None, spec=None, log=print):
    """Apply route-diagnose's ranked move list to unlocked parts. Support parts are
    re-seated beside their IC (clusters.reseat); others move to within ~3 mm of the pad
    they should be near, then legalise.legalise settles overlaps with everything else
    held. -> {ref: (x, y, angle)} (also committed to B)."""
    from . import geom as GM
    fixed = {r for r, p in B["parts"].items() if p.get("locked")} | set(
        ((cons or {}).get("lock") or {}).get("at", {})) | set(((cons or {}).get("placement") or {}).get("fixed", []))
    wanted = [m["part"] for m in res["summary"]["moves"] if m["part"] not in fixed and m["part"] in B["parts"]]
    out = {}
    if not wanted:
        return out
    cl, _ = CL.build(B, cons, spec)
    x0, y0, x1, y1 = B["bbox"]
    mv = CL.reseat(B, cl, everything=True, only=set(wanted), bounds=(x0 + .3, y0 + .3, x1 - .3, y1 - .3))
    out.update(mv)
    P = B["parts"]
    pads = NC.net_pads(B)
    tg = {}
    for m in res["summary"]["moves"]:
        r = m["part"]
        if r in out or r in fixed or r not in P:
            continue
        ref, _, num = m["toward"][0].partition(".")
        q = next((p for p in P[ref]["pads"] if p["num"] == num), None) if ref in P else None
        if not q:
            continue
        px, py = P[r]["pos"]
        d = math.hypot(q["x"] - px, q["y"] - py)
        if d > 3.0:
            k = (d - 3.0) / d
            tg[r] = (px + (q["x"] - px) * k, py + (q["y"] - py) * k, P[r]["angle"])
    if tg:
        try:
            res2, _ = LG.legalise(B, tg, fixed=set(P) - set(tg), cons=cons)
            for r, (x, y, a) in res2.items():
                GM.place(P[r], x, y, a)
                out[r] = (round(x, 4), round(y, 4), a)
        except LG.PlacementError as e:
            log(f"    stall: could not place {sorted(tg)} without overlaps ({e}); left in place")
    for r, v in out.items():
        log(f"    stall: moved {r} -> ({v[0]:.2f}, {v[1]:.2f}) {v[2]:g} deg")
    return out


def stall_loop(board, outdir, route_fn, cons=None, spec=None, rounds=3, replace=False,
               plane_nets=(), timeout=DEFAULT_STAGE_S, log=print, failed0=None, timed_out0=False):
    """`route_fn(board_in, board_out, nets, timeout) -> timed_out` re-routes `nets` onto a
    copy of board_in. `board` is the board AFTER the first routing pass. Returns a dict
    {rounds, failed, moved, diagnosis_files, board}."""
    cur, moved_all, files = board, {}, []
    failed = RD.unrouted_nets(RD.load(cur)) if failed0 is None else list(failed0)
    timed_out = timed_out0
    for rnd in range(rounds + 1):
        if not failed and not timed_out:
            break
        B = RD.load(cur)
        B["_cons"], B["_spec"] = cons, spec
        res = RD.diagnose(B, failed, cons, spec)
        base = os.path.join(outdir, f"route-diagnose-{rnd}")
        with open(base + ".json", "w") as fh:
            json.dump(res, fh, indent=1)
        with open(base + ".txt", "w") as fh:
            fh.write(RD.format_text(res))
        files.append(base + ".txt")
        log(f"    stall round {rnd}: {len(failed)} failed net(s)"
            f"{' (timeout)' if timed_out else ''}; diagnosis -> {base}.txt")
        if not replace or rnd == rounds or not res["summary"]["moves"]:
            break
        mv = apply_moves(B, res, cons, spec, log)
        if not mv:
            log("    stall: the move list has nothing movable (all locked/fixed); stopping")
            break
        moved_all.update(mv)
        nxt = os.path.join(outdir, f"stall-{rnd + 1}.kicad_pcb")
        text = open(cur, errors="surrogateescape").read()
        text, _n = BW.apply_moves(text, mv)
        aff = affected_nets(B, mv, skip=set(plane_nets)) | set(failed)
        text, ncut = BW.strip_nets(text, aff - set(plane_nets))
        with open(nxt, "w", errors="surrogateescape") as fh:
            fh.write(text)
        BW.run_post_write(nxt, mv)
        side = os.path.splitext(cur)[0] + ".kicad_pro"
        if os.path.exists(side):
            import shutil
            shutil.copy(side, os.path.splitext(nxt)[0] + ".kicad_pro")
        log(f"    stall: moved {len(mv)} part(s), ripped up {ncut} track/via item(s) of "
            f"{len(aff)} affected net(s); re-routing only those")
        done = os.path.join(outdir, f"stall-{rnd + 1}-routed.kicad_pcb")
        timed_out = route_fn(nxt, done, sorted(aff - set(plane_nets)), timeout)
        cur = done if os.path.exists(done) else nxt
        failed = RD.unrouted_nets(RD.load(cur))
    return {"rounds": rnd, "failed": failed, "moved": moved_all, "diagnosis_files": files, "board": cur}
