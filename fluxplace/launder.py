"""Copper laundering — delete the parasitic copper KiCad's own DRC names.

The routed boards carry classes of junk copper that no router pass will
fix and that swamp the violation count (measured on dig/CM5 after the
board-limits fix):

  - track_dangling / via_dangling — stubs and one-layer vias left by
    capped batch routing and reverted experiments
  - shorting_items — GND stitching vias dropped onto foreign pads/tracks
  - hole_to_hole / hole_clearance — vias drilled inside or against other
    holes (worst measured: a GND via INSIDE J10's NPTH)

Strategy: parse the DRC report, resolve each named Track/Via item back to
a board object (class + net + HitTest position match), delete only
tracks/vias (never pads, never zones), refill pours, and accept the round
only if violations went DOWN and unconnected did not go UP. Iterate:
deleting a stub can orphan its neighbour into the next report.
"""
import json
import os
import re
import subprocess


_DELETABLE = {
    "track_dangling": "single",     # remove the dangling track/via itself
    "via_dangling": "single",
    "shorting_items": "via_first",  # remove the via (or track) of the pair
    "hole_to_hole": "via_only",     # a via drilled against another hole
    "hole_clearance": "gnd_only",   # copper jammed against an NPTH: only
                                    # GND stubs (pours re-heal), guard-safe
}

# guarded phases. Guard semantics per phase:
#  - "class_count": accept a try when the phase's own violation classes
#    DROP, even if unconnected rises — an electrical short is strictly
#    worse than an open, and the patch stage runs after the launder to
#    reconnect what opening the short exposed.
#  - "strict": violations and unconnected must both not rise.
# The dangling phase is OFF by default: KiCad flags a track when EITHER
# end hangs, so most "dangling" segments still carry connectivity through
# their other end (measured: removing one 0.15mm GND stub re-opened a
# track-to-via connection).
_PHASES = (
    ("shorts+holes", ("shorting_items", "hole_to_hole", "hole_clearance"),
     "class_count"),
    ("dangling", ("track_dangling", "via_dangling"), "strict"),
)


def _drc(board_path, kicad_cli):
    out = board_path + ".launder_drc.json"
    subprocess.run([kicad_cli, "pcb", "drc", "--format", "json",
                    "--output", out, board_path],
                   capture_output=True, timeout=600)
    with open(out) as f:
        d = json.load(f)
    os.unlink(out)
    return d


def _resolve(snapshot, item, pcbnew):
    """DRC item -> track/via object from a pre-taken snapshot list (never
    pads/zones). The snapshot exists because calling board.GetTracks()
    repeatedly while also Remove()-ing items corrupts the SWIG TRACKS
    binding — measured: Tracks() starts returning a raw SwigPyObject after
    ~200 interleaved calls."""
    desc = item.get("description", "")
    want_via = desc.startswith("Via")
    want_trk = desc.startswith("Track")
    if not (want_via or want_trk):
        return None
    m = re.search(r"\[([^\]]*)\]", desc)
    net = m.group(1) if m else None
    pos = item.get("pos", {})
    v = pcbnew.VECTOR2I(int(pos.get("x", 0) * 1e6),
                        int(pos.get("y", 0) * 1e6))
    for t in snapshot:
        if (t.GetClass() == "PCB_VIA") != want_via:
            continue
        if net is not None and t.GetNetname() != net:
            continue
        if t.HitTest(v):
            return t
    return None


def mutate(src_path, dst_path, picks):
    """Run one board mutation (remove `picks` descriptors, refill pours,
    save) in a FRESH subprocess and return the number removed. A pcbnew
    session is effectively single-use once ZONE_FILLER has run — repeated
    in-process refill/save cycles eventually segfault or corrupt SWIG
    proxies (measured: every long patch run died in its final refill).
    picks: [{"via": bool, "net": str, "x": mm, "y": mm}] — empty list =
    refill+save only."""
    import sys
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    wenv = dict(os.environ)
    wenv["PYTHONPATH"] = repo + os.pathsep + wenv.get("PYTHONPATH", "")
    # the worker runs with cwd=repo: every path it gets must be absolute
    src_path, dst_path = os.path.abspath(src_path), os.path.abspath(dst_path)
    pk = dst_path + ".picks.json"
    with open(pk, "w") as f:
        json.dump(picks, f)
    r = subprocess.run([sys.executable, "-u", "-m", "fluxplace.launder",
                        src_path, dst_path, pk],
                       capture_output=True, text=True, timeout=900,
                       env=wenv, cwd=repo)
    os.unlink(pk)
    for line in (r.stdout or "").splitlines()[::-1]:
        if line.startswith("REMOVED "):
            return int(line.split()[1])
    raise RuntimeError(
        f"mutate worker rc={r.returncode}: "
        f"{(r.stdout or '')[-200:]} / {(r.stderr or '')[-300:]}")


def launder_board(board_path, out_path, kicad_cli="kicad-cli",
                  max_rounds=4, log=print, dangling=False, prof=None):
    """Returns a summary dict. Writes out_path only when at least one
    round was accepted; otherwise the input is copied through.
    NOTE: the caller must not hold another live pcbnew board proxy —
    coexisting boards break SWIG container iteration."""
    import pcbnew
    import shutil
    from .patch import refill_zones

    work = out_path + ".launder_work.kicad_pcb"
    shutil.copy(board_path, work)
    for ext in (".kicad_dru", ".kicad_pro"):
        s = os.path.splitext(board_path)[0] + ext
        if os.path.exists(s):
            for tgt in (work, out_path):
                try:
                    shutil.copy(s, os.path.splitext(tgt)[0] + ext)
                except OSError:
                    pass
    # EVERY board mutation runs in a fresh SUBPROCESS (see mutate) — the
    # parent only orchestrates descriptors, DRC runs, and the guard.
    _mutate = mutate

    def _refill_save(src_path, dst_path):
        _mutate(src_path, dst_path, [])

    # refilled baseline, like patch_board: pours re-flow under the new
    # netclass clearance, so an unrefilled baseline makes every try look
    # like a regression (measured: 115+219 candidates, zero accepted)
    _refill_save(work, work)
    if prof is not None:
        # SaveBoard just generated a default .kicad_pro if none existed;
        # kicad-cli drc loads project rules OVER the board's setup, so the
        # working pro must carry the profile floor or every guard verdict
        # is judged against KiCad defaults (measured)
        from . import profiles as _PROF
        _PROF.write_pro_limits(os.path.splitext(work)[0] + ".kicad_pro",
                               prof)
    d = _drc(work, kicad_cli)
    vio0 = len(d.get("violations", []))
    unc0 = len(d.get("unconnected_items", []))
    total_removed = 0
    accepted_rounds = 0
    vio_prev, unc_prev = vio0, unc0
    tmp = work + ".round.kicad_pcb"
    for phase_name, types, guard in _PHASES:
        if phase_name == "dangling" and not dangling:
            continue
        for rnd in range(max_rounds):
            # pick DESCRIPTORS straight from the DRC report — no board
            # object needed until a try actually mutates one
            seen = set()
            picks = []
            for v in d.get("violations", []):
                if v.get("type") not in types:
                    continue
                mode = _DELETABLE[v.get("type")]
                cands = []
                for it in v.get("items", []):
                    desc = it.get("description", "")
                    if not desc.startswith(("Via", "Track")):
                        continue
                    m = re.search(r"\[([^\]]*)\]", desc)
                    cands.append({
                        "via": desc.startswith("Via"),
                        "net": m.group(1) if m else None,
                        "x": it.get("pos", {}).get("x", 0),
                        "y": it.get("pos", {}).get("y", 0),
                    })
                cands = [c for c in cands
                         if (c["via"], c["net"], c["x"], c["y"]) not in seen]
                if not cands:
                    continue
                pick = None
                if mode == "single":
                    pick = cands[0]
                elif mode in ("via_first", "via_only"):
                    vias = [c for c in cands if c["via"]]
                    pick = vias[0] if vias else (
                        cands[0] if mode == "via_first" else None)
                elif mode == "gnd_only":
                    gnd = [c for c in cands if c["net"] == "GND"]
                    pick = gnd[0] if gnd else None
                if pick is None:
                    continue
                seen.add((pick["via"], pick["net"], pick["x"], pick["y"]))
                picks.append(pick)
            if not picks:
                break

            def _class_count(dd):
                return sum(1 for v in dd.get("violations", [])
                           if v.get("type") in types)

            def _try(subset):
                nonlocal vio_prev, unc_prev, total_removed, d
                cls_prev = _class_count(d)
                gone = _mutate(work, tmp, subset)
                if not gone:
                    return False
                for ext in (".kicad_dru", ".kicad_pro"):
                    s = os.path.splitext(work)[0] + ext
                    if os.path.exists(s):
                        shutil.copy(s, os.path.splitext(tmp)[0] + ext)
                d1 = _drc(tmp, kicad_cli)
                vio1 = len(d1.get("violations", []))
                unc1 = len(d1.get("unconnected_items", []))
                if guard == "class_count":
                    ok = _class_count(d1) < cls_prev and vio1 <= vio_prev
                else:
                    ok = vio1 <= vio_prev and unc1 <= unc_prev
                if ok:
                    if unc1 > unc_prev:
                        log(f"      try: -{gone} short/hole item(s) "
                            f"opened {unc1 - unc_prev} connection(s) — "
                            f"patcher's job next")
                    os.replace(tmp, work)
                    vio_prev, unc_prev = vio1, unc1
                    total_removed += gone
                    d = d1
                    return True
                os.unlink(tmp)
                log(f"      try: -{gone} item(s) -> violations "
                    f"{vio_prev}->{vio1}, unconnected {unc_prev}->{unc1} "
                    f"(rejected)")
                return False

            queue = [picks]
            tries = 0
            kept = 0
            while queue and tries < 12:
                subset = queue.pop(0)
                tries += 1
                if _try(subset):
                    kept += len(subset)
                elif len(subset) > 1:
                    mid = len(subset) // 2
                    queue.append(subset[:mid])
                    queue.append(subset[mid:])
            if kept:
                accepted_rounds += 1
                log(f"    launder [{phase_name}] round {rnd + 1}: removed "
                    f"{kept}/{len(picks)} item(s) in {tries} tries — "
                    f"violations at {vio_prev}, unconnected at {unc_prev}")
            else:
                log(f"    launder [{phase_name}] round {rnd + 1}: no safe "
                    f"removals among {len(picks)} candidate(s)")
                break
    if accepted_rounds:
        os.replace(work, out_path)
    else:
        if os.path.abspath(board_path) != os.path.abspath(out_path):
            shutil.copy(board_path, out_path)
        if os.path.exists(work):
            os.unlink(work)
    return {"rounds": accepted_rounds, "removed": total_removed,
            "violations": (vio0, vio_prev), "unconnected": (unc0, unc_prev)}


# ------------------------------------------------------------ worker ----

def _worker(board_in, board_out, picks_json):
    """Subprocess entry: one board mutation per interpreter, because a
    pcbnew session is single-use once ZONE_FILLER has run (see above)."""
    import pcbnew
    from .patch import refill_zones
    with open(picks_json) as f:
        picks = json.load(f)
    b = pcbnew.LoadBoard(board_in)
    if b is None:
        raise SystemExit(f"LoadBoard returned None for {board_in!r} — "
                         f"missing or unreadable board file")
    snap = [t for t in b.GetTracks()]
    gone = 0
    for c in picks:
        o = _resolve(snap, {
            "description": ("Via [" if c["via"] else "Track [")
            + (c["net"] or "") + "]",
            "pos": {"x": c["x"], "y": c["y"]},
        }, pcbnew)
        if o is not None and o.GetBoard() is not None:
            b.Remove(o)
            gone += 1
    refill_zones(b)
    pcbnew.SaveBoard(board_out, b)
    print("REMOVED", gone)


if __name__ == "__main__":
    import sys as _sys
    _worker(_sys.argv[1], _sys.argv[2], _sys.argv[3])
