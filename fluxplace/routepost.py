"""Post-step of a routed board, run by the route stage after every round and at the end:
snap track ends into via centres (snapvias), join small same-net gaps (joingaps), then GATE on what is still off-centre.

`post_round(path, conn)` edits `path` in place and never makes the board worse (a result with more open connections is
reverted).  `gate_line(rep)` is the WARN text (with the list) for the stage record; silence is not an outcome."""
import os
import shutil

from . import joingaps as JG
from . import snapvias as SV


def post_round(path, conn, gap=JG.GAP, join=True, log=lambda m: None):
    """-> {snapped, joined, skipped, off_centre: [(net, layer, x, y, locked)], open}."""
    rep = {"snapped": 0, "joined": 0, "skipped": [], "off_centre": [], "open": None}
    try:
        before = dict(conn(path))
        total0 = sum(before.values())
        bak = path + ".post.bak"
        shutil.copy(path, bak)
        text = open(path, errors="surrogateescape").read()
        new, n, _nv = SV.snap_text(text)
        if n:
            with open(path, "w", errors="surrogateescape") as fh:
                fh.write(new)
            rep["snapped"] = n
        if join and before:
            jr = JG.run(path, None, gap, nets=sorted(before))
            rep["joined"], rep["skipped"] = jr["closed"], [s for s in jr["skipped"] if s["reason"] != "already connected"]
        after = dict(conn(path)) if (n or rep["joined"]) else before
        rep["open"] = sum(after.values())
        if rep["open"] > total0:                         # never worse than it was
            shutil.copy(bak, path)
            rep.update(snapped=0, joined=0, open=total0, reverted=True)
        os.remove(bak)
        rep["off_centre"] = SV.off_centre(open(path, errors="surrogateescape").read())
    except Exception as e:                               # noqa: BLE001 - a post-step failure is reported, never fatal
        rep["error"] = str(e)[:160]
        log("route post-step failed: " + rep["error"])
    return rep


def gate_line(rep, top=5):
    """'' when clean, else a WARN line naming the off-centre ends (net layer x,y [locked])."""
    oc = rep.get("off_centre") or []
    if not oc:
        return ""
    lst = "; ".join(f"{n} {l} {x:g},{y:g}" + (" locked" if k else "") for n, l, x, y, k in oc[:top])
    return f"WARN {len(oc)} track end(s) off-centre in a via ({lst}{'; ...' if len(oc) > top else ''})"
