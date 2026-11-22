"""ETA for the dashboard (docs/EVENTS-CONTRACT.md, eta.json / eta event).

Per stage: median of secs / scale over the last 20 timing rows of that stage, scaled to this board:
components (place stages), nets x density (route/close/pairs/lookahead), 1 (others). No history: DEFAULT_SECS.
Rows are appended to ~/.config/fluxplace/timings.jsonl at every stage end. Never raises.
"""
import json
import os
import statistics
import time

from . import events as EV

TIMINGS = os.path.join(os.path.expanduser("~"), ".config", "fluxplace", "timings.jsonl")
HISTORY_N = 20
TICK_S = 60.0
DEFAULT_SECS = {"ready": 5, "place": 600, "pairs": 900, "lookahead": 2400, "route": 3600, "close": 3600,
                "review": 120}
OTHER_SECS = 60
PLACE_STAGES = {"place"}
ROUTE_STAGES = {"route", "close", "pairs", "lookahead"}

_CTX = {"features": {}, "stages": [], "current": None, "t_stage": None, "t_tick": 0.0}


def scale_of(stage, features):
    f = features or {}
    if stage in PLACE_STAGES:
        return float(max(1, f.get("components") or 1))
    if stage in ROUTE_STAGES:
        return float(max(1e-9, (f.get("nets") or 1) * (f.get("density") or 1e-3)))
    return 1.0


def record(features, stage, secs, verdict):
    """Append one timing row. Nested runs (FLUXPLACE_NESTED=1: capped lookahead ghosts/candidates) are skipped
    so they do not pollute the route medians. Never raises."""
    try:
        if os.environ.get("FLUXPLACE_NESTED") == "1":
            return
        os.makedirs(os.path.dirname(TIMINGS), exist_ok=True)
        row = {"ts": round(time.time(), 1), "stage": stage, "secs": round(float(secs), 2), "verdict": verdict,
               "scale": scale_of(stage, features), "components": (features or {}).get("components"),
               "nets": (features or {}).get("nets"), "density": (features or {}).get("density")}
        with open(TIMINGS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


def _ratios(stage):
    out = []
    try:
        with open(TIMINGS, encoding="utf-8") as fh:
            for ln in fh:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if r.get("stage") == stage and r.get("secs", 0) > 0 and r.get("scale", 0) > 0:
                    out.append(r["secs"] / r["scale"])
    except OSError:
        return []
    return out[-HISTORY_N:]


def estimate(features, remaining_stages, elapsed_in_stage=0.0, all_stages=None):
    """-> {secs_left, done_frac, model, basis}. remaining_stages: the stage in progress first, then the rest."""
    remaining = list(remaining_stages)
    all_stages = list(all_stages) if all_stages else remaining
    parts, bases, hist_used = [], [], False

    def secs_of(s):
        nonlocal hist_used
        rs = _ratios(s)
        if rs:
            hist_used = True
            bases.append(f"{s}:history(n={len(rs)})")
            return statistics.median(rs) * scale_of(s, features)
        bases.append(f"{s}:table")
        return float(DEFAULT_SECS.get(s, OTHER_SECS))

    total_all = sum(secs_of(s) for s in all_stages) or 1.0
    left_raw = sum(secs_of(s) for s in remaining) if remaining else 0.0
    left = max(0.0, left_raw - float(elapsed_in_stage or 0.0))
    done = min(1.0, max(0.0, 1.0 - left / total_all))
    return {"secs_left": round(left, 1), "done_frac": round(done, 4),
            "model": "history" if hist_used else "table", "basis": "; ".join(bases)[:240]}


def report(est, stage=None):
    """Write W/eta.json (atomic) and emit an eta event. Never raises."""
    try:
        W = os.path.dirname(os.environ.get(EV.ENV_FILE, "") or "")
        if not W:
            return
        obj = dict(est, ts=round(time.time(), 3), stage=stage)
        tmp = os.path.join(W, "eta.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        os.replace(tmp, os.path.join(W, "eta.json"))
        EV.emit("eta", stage=stage, secs_left=est["secs_left"], done_frac=est["done_frac"],
                model=est["model"], basis=est["basis"])
    except Exception:
        pass


def set_run(features, stages):
    try:
        _CTX.update(features=dict(features or {}), stages=list(stages or []), current=None, t_stage=None,
                    t_tick=0.0)
    except Exception:
        pass


def stage_start(name):
    try:
        _CTX.update(current=name, t_stage=time.time(), t_tick=time.time())
    except Exception:
        pass


def stage_end(name, secs, verdict):
    """Record the timing, then ETA for the stages after `name`."""
    try:
        record(_CTX["features"], name, secs, verdict)
        st = _CTX["stages"]
        rest = st[st.index(name) + 1:] if name in st else []
        report(estimate(_CTX["features"], rest, 0.0, all_stages=st or rest), stage=name)
        _CTX.update(current=None, t_stage=None, t_tick=time.time())
    except Exception:
        pass


def tick():
    """From the progress hook: at most one ETA every TICK_S seconds. Never raises."""
    try:
        now = time.time()
        if _CTX["current"] is None or now - _CTX["t_tick"] < TICK_S:
            return
        _CTX["t_tick"] = now
        st, cur = _CTX["stages"], _CTX["current"]
        rest = st[st.index(cur):] if cur in st else [cur]
        report(estimate(_CTX["features"], rest, now - (_CTX["t_stage"] or now), all_stages=st or rest), stage=cur)
    except Exception:
        pass
