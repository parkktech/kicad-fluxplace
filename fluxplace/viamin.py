"""via-min: raise every via smaller than the minimum (constraints / fab / project floor) to that minimum, DRC-guarded.

Batch first, then bisect: a group of raises is kept only if the DRC shows no violation key more often than the start board
(drcguard multiset rule); a failing group is split until single vias are accepted or rejected. Locked vias are never touched."""
import os
import shutil
import time

from . import drcguard as DG


def _vias(b):
    return [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA"]


def via_min(board, out, constraints=None, kicad_cli="kicad-cli", report=None, log=None, budget=None):
    """Raise and verify as many vias as the budget permits, always writing all accepted changes.

    `report(path, cli)` -> (keys, counts) replaces DRC. The default budget is 3 seconds per candidate, minimum 60.
    A group whose DRC completed is kept or rejected even if it crosses the deadline; untouched groups are not_reached.
    """
    import pcbnew
    from . import handroute as H
    floor = H._model(board, constraints).floors["via_dia"]
    mm = lambda v: int(round(v * 1e6))      # noqa: E731
    b = pcbnew.LoadBoard(board)
    allv = _vias(b)
    low = [v for v in allv if v.GetWidth() < mm(floor) - 1]
    cand = [v for v in low if not v.IsLocked()]
    budget = float(budget) if budget is not None else max(60.0, 3.0 * len(cand))
    t0 = time.monotonic()
    deadline = t0 + budget
    run = report or (lambda p, c: DG.drc_keys(p, c))
    base = run(board, kicad_cli)
    stats = {"changed": 0, "rejected": 0, "not_reached": 0, "rejected_at": [], "not_reached_at": []}
    tmp = os.path.join(os.path.dirname(os.path.abspath(out)) or ".", ".viamin.try.kicad_pcb")
    side = os.path.splitext(board)[0] + ".kicad_pro"
    if os.path.exists(side):
        shutil.copy(side, os.path.splitext(tmp)[0] + ".kicad_pro")

    def unreached(group):
        stats["not_reached"] += len(group)
        for v in group:
            p = v.GetPosition()
            stats["not_reached_at"].append([round(p.x * 1e-6, 4), round(p.y * 1e-6, 4)])

    def attempt(group):
        if time.monotonic() >= deadline:
            unreached(group)
            return
        old = [v.GetWidth() for v in group]
        for v in group:
            v.SetWidth(mm(floor))
        pcbnew.SaveBoard(tmp, b)
        ok, _cur, _new = DG.check(tmp, base, kicad_cli, log=log, report=report)
        if ok:
            stats["changed"] += len(group)
            return
        for v, w in zip(group, old):
            v.SetWidth(w)
        if len(group) == 1:
            stats["rejected"] += 1
            p = group[0].GetPosition()
            stats["rejected_at"].append([round(p.x * 1e-6, 4), round(p.y * 1e-6, 4)])
            return
        if time.monotonic() >= deadline:
            unreached(group)
            return
        h = len(group) // 2
        attempt(group[:h])
        attempt(group[h:])

    try:
        if cand:
            attempt(cand)
        pcbnew.SaveBoard(out, b)
        if os.path.exists(side) and os.path.abspath(side) != os.path.abspath(os.path.splitext(out)[0] + ".kicad_pro"):
            shutil.copy(side, os.path.splitext(out)[0] + ".kicad_pro")
    finally:
        for ext in (".kicad_pcb", ".kicad_pro"):
            p = os.path.splitext(tmp)[0] + ext
            if os.path.exists(p):
                os.remove(p)
    return {"floor_mm": round(floor, 4), "vias": len(allv), "below": len(low), "changed": stats["changed"], "kept": len(allv) - len(low),
            "rejected": stats["rejected"], "not_reached": stats["not_reached"], "locked_skipped": len(low) - len(cand),
            "rejected_at": stats["rejected_at"], "not_reached_at": stats["not_reached_at"], "budget_s": budget,
            "elapsed_s": round(time.monotonic() - t0, 3), "budget_exhausted": stats["not_reached"] > 0, "written": out}
