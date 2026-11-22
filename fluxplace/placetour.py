"""PLACETOUR stage: placement tournament judged by a real router (docs/recipes/PLACETOUR.md). No pcbnew in this module.

Round 0 judges the input: a KRT `--quick 2` lean copy (lookahead.lean_krt) and route_review blame on it. Each round the
blame of the current best makes candidate part moves (turn, relieve, nudge, rot, pull); tier-1 gates and the predictor rank
them in seconds; the best `judge_top` are judged by KRT in parallel (one interpreter each). The winner's MOVED board (no
judge copper) is the next base when its judge key beats the best. Stops: no opens and no hot area, no gain for
`no_gain_rounds`, `max_rounds`, or [flow] budget_min.
"""
import copy
import fnmatch
import json
import math
import os
import shutil
import subprocess
import time
from types import SimpleNamespace

from . import boardwrite as BW
from . import events as EV
from . import intent as IT
from . import legalise as LG
from . import legality as LGL
from . import lookahead as LA
from . import placechecks as PC
from . import planplace as PP
from . import planroute as PL
from . import relieve as RL
from . import routable as RB
from . import routediag as RD
from . import routestatus as RS
from . import snapshot as SN

DEFAULTS = {"enabled": False, "max_rounds": 10, "no_gain_rounds": 2, "jobs": 4, "judge_top": 4, "judge_min": 4.0,
            "cands": 12, "hot": 0.5, "nudge_mm": [0.5, 1.0, 2.0], "move_routed": True}
_INT = ("max_rounds", "no_gain_rounds", "jobs", "judge_top", "cands")
WORST = 10 ** 6
KINDS = ("turn", "relieve", "nudge", "rot", "pull")


def params(cons):
    """[placetour] table of constraints.toml -> stage parameters (defaults from the recipe). Off unless enabled = true."""
    e = dict((cons or {}).get("placetour") or {})
    P = dict(DEFAULTS)
    P.update(e)
    P["enabled"] = e.get("enabled") is True
    P["move_routed"] = e.get("move_routed", True) is True
    for k in _INT:
        P[k] = int(P[k])
    P["judge_min"] = float(P["judge_min"])
    P["hot"] = float(P["hot"])
    P["nudge_mm"] = [float(x) for x in P["nudge_mm"]]
    return P


def _progress(path, state, message="", **fields):
    st = {"stage": "placetour", "state": state, "pid": os.getpid(), "current_step": str(message)[:240], "updated": time.time()}
    st.update(fields)
    RS._atomic(path, st)


# ------------------------------------------------------------------ P1 judge + blame
def blame_of(review):
    """route_review --json document -> blame {open, costly, hot, turned, source}. Opens are the signal nets (not poured)."""
    opens = [{"net": o["net"], "refs": sorted(set(o.get("disconnected_pad_parts") or o.get("parts") or []))}
             for o in review.get("open_nets", []) if not o.get("poured")]
    costly = {}
    for n in list(review.get("most_vias", [])) + list(review.get("longest_detours", [])):
        cur = costly.setdefault(n["net"], {"net": n["net"], "vias": 0, "detour": 0.0, "refs": []})
        cur["vias"] = max(cur["vias"], n.get("vias") or 0)
        cur["detour"] = max(cur["detour"], n.get("detour") or 0.0)
        cur["refs"] = sorted(set(cur["refs"]) | set(n.get("parts") or []))
    hot = [{"cells": a["cells"], "occ": a["peak"], "refs": sorted(set(a.get("parts") or [])), "nets": list(a.get("nets") or []),
            "bbox": list(a["box"]), "vias": a.get("vias", 0)} for a in review.get("congested_areas", [])]
    turned = [{"ref": t["ref"], "x": t["x"], "y": t["y"], "rot": t["rot"], "saves": t.get("saves_mm", 0.0)}
              for t in review.get("turned_parts", [])]
    return {"open": opens, "costly": sorted(costly.values(), key=lambda c: (-c["detour"], c["net"])),
            "hot": hot, "turned": turned, "source": "route_review"}


def _review(a, routed, rev_json):
    """route_review.py of the KRT checkout on the judged copy -> blame, or None (tool missing, bad exit, bad json)."""
    tool = os.path.join(a.router_dir, "py_tools", "route_review.py")
    if not os.path.exists(tool):
        return None
    try:
        r = subprocess.run([a.router_py, "-X", "utf8", tool, routed, "--json", rev_json, "--top", "30"],
                           cwd=a.router_dir, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not os.path.exists(rev_json):
        return None
    try:
        return blame_of(json.load(open(rev_json)))
    except (OSError, ValueError):
        return None


def _predictor_blame(board, cons, spec):
    """fallback when route_review is unavailable: the planroute findings only (sealed pins and their blockers)."""
    R = PL.run(RD.load(board), cons, spec, ignore_routing=True)
    opens = [{"net": f.get("net"), "refs": sorted({r for r in (f.get("part"), f.get("blocker")) if r})}
             for f in R["findings"] if f["code"] == "PLAN_PIN_SEALED"]
    return {"open": opens, "costly": [], "hot": [], "turned": [], "source": "planroute"}


def judge(a, board, outdir, P, cons=None, spec=None, log=print):
    """-> {routed, blame, source, secs}: KRT --quick 2 judge copy of `board` (None when it failed), blame from route_review on
    that copy; without a routed copy or without route_review the blame comes from the predictor (logged)."""
    t0 = time.time()
    os.makedirs(outdir, exist_ok=True)
    routed = LA.lean_krt(a, board, outdir, P["judge_min"])
    blame = _review(a, routed, os.path.join(outdir, "review.json")) if routed else None
    if blame is None:
        log(f"placetour judge: blame from the predictor ({'no routed copy' if not routed else 'route_review unavailable'})")
        blame = _predictor_blame(routed or board, cons or {}, spec)
    return {"routed": routed, "blame": blame, "source": blame["source"], "secs": round(time.time() - t0, 1)}


def judge_key(routed_board, blame, cons=None):
    """(signal opens, hot areas, sum of hot occupancy, total vias, sum of detour over costly nets) of the judge board, smaller is
    better. No routed copy: the worst key."""
    if routed_board is None or blame is None:
        return (WORST, WORST, float(WORST), WORST, float(WORST))
    vias = len(RD.load(routed_board)["vias"])
    return (len(blame["open"]), len(blame["hot"]), round(sum(h["occ"] for h in blame["hot"]), 4), vias,
            round(sum(c["detour"] for c in blame["costly"]), 4))


