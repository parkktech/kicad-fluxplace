"""Complete a capped kicad-cli DRC `unconnected_items` list from pcbnew connectivity.

kicad-cli lists at most CAP unconnected items; the true open count (pcbnew GetUnconnectedCount) can be higher
(measured: d2 lists 499 of 501, c lists 499 of 503). Which opens are cut off depends on DRC walk order, so a
count taken from the list can move the wrong way. complete() restores the per-net counts: for each net the
listed count becomes max(listed, pcbnew pad-cluster opens), and synthetic entries fill the difference.
Below CAP - 5 the list is complete and the dict is returned unchanged.
"""
import json
import os
import re
import subprocess
from fluxplace import procs as _procs
import sys
from collections import Counter

CAP = 499
PY = "/usr/bin/python3"
HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conn_helper.py")
_NET = re.compile(r"\[([^\]]*)\]")
_CACHE = {}


def net_of(u):
    """Net name of an unconnected entry (first [NAME] in its item descriptions), or None."""
    for it in u.get("items") or []:
        m = _NET.search(str(it.get("description") or ""))
        if m:
            return m.group(1)
    return None


def _conn(board, fill):
    st = os.stat(board)
    key = (os.path.abspath(board), st.st_mtime_ns, st.st_size, bool(fill))
    if key not in _CACHE:
        r = _procs.run([PY, HELPER, board] + (["--fill"] if fill else []),
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise RuntimeError("conn_helper failed: " + (r.stderr or r.stdout)[-300:])
        _CACHE[key] = json.loads([l for l in r.stdout.splitlines() if l.startswith("{")][-1])
    return _CACHE[key]


def _pad_item(p, net):
    layers = p.get("layers") or ["F.Cu"]
    return {"description": f"Pad {p['num']} [{net}] of {p['ref']} on {layers[0]}",
            "pos": {"x": p["x"], "y": p["y"]}, "uuid": ""}


def _edge(ca, cb, net):
    """Nearest pad pair between two clusters of one net -> synthetic unconnected entry."""
    best = min(((p, q) for p in ca for q in cb),
               key=lambda pq: (pq[0]["x"] - pq[1]["x"]) ** 2 + (pq[0]["y"] - pq[1]["y"]) ** 2)
    return {"type": "unconnected_items", "severity": "error",
            "description": "Missing connection between items (synthesised: DRC list capped)",
            "items": [_pad_item(best[0], net), _pad_item(best[1], net)]}


def complete(board_path, d, fill=False):
    """d: kicad-cli DRC dict for board_path (fill=True if that run refilled zones).
    Returns d unchanged when its list is complete, else a copy with the list completed per net."""
    unc = d.get("unconnected_items", [])
    if len(unc) < CAP - 5:
        return d
    c = _conn(board_path, fill)
    listed = Counter(net_of(u) for u in unc)
    add = []
    for net, n in sorted(c["nets"].items()):
        cl = c["clusters"].get(net, [])
        for k in range(max(0, n - listed.get(net, 0))):
            if k + 1 >= len(cl):
                break
            add.append(_edge(cl[k], cl[k + 1], net))
    out = dict(d)
    out["unconnected_items"] = list(unc) + add
    out["unconnected_capped"] = True
    out["unconnected_true"] = c["unconnected"]
    print(f"drc: unconnected list capped at {len(unc)}; completed from pcbnew connectivity "
          f"({c['unconnected']} total)", file=sys.stderr)
    return out
