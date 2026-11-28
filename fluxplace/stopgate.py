"""Stop-on-fail pipeline policy (ledger 2026-11-27, k13): after each stage the flow stops, writes <workdir>/STOP.json and exits 3 when
the stage failed / crashed / stalled with findings it owns, when an earlier stage's deferred finding is still open after its last owner
ran, or when a `[go.<stage>]` threshold is missed.  A finding owned by a stage that has not run yet is still deferred.
`[flow] on_fail = "continue"` restores the old behaviour.  A re-run with the same workdir resumes at the first stage without a go stamp
for its input (stamp = sha of the stage's input board)."""
import json
import os
import shlex
import sys

STOP = "STOP.json"
EXIT = 3


def on_fail(cons):
    return str(((cons or {}).get("flow") or {}).get("on_fail", "stop")).lower()


def board_sha(path):
    import hashlib
    try:
        with open(path, "rb") as fh:
            h = hashlib.sha1()
            for blk in iter(lambda: fh.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest()[:12]
    except OSError:
        return ""


def _opens(board, work):
    from . import opengate as OG
    return OG.unconnected(board)


def _drc_errors(board, work):
    from . import fanout
    cnt, _o, _t = fanout.drc_counts(board, work)
    return sum(cnt.values())


def _rows(res, kind):
    g = res.get("gate") or {}
    return sum(1 for r in list(g.get("left") or []) + list(g.get("deferred") or []) + list(g.get("allowed") or []) if r.get("kind") == kind)


NO_ALLOW_STOPS = ("clean", "packets", "protection", "crashed", "manifest_stale")


def kind_key(kind):
    """The `[go.<stage>]` key that bounds a gap kind's rows: power-width -> power_width_max."""
    return kind.replace("-", "_") + "_max"


def gate_allowed(cons, name, left, later=()):
    """`[go.<name>] <kind>_max = N` is the owner's go limit for the stage: a stage gate that ends with at most N rows of that kind left
    does not fail the stage (ledger 2026-11-28: `power_width_max = 1` with 1 power-width row left still read "fail ... flat", because
    the limit was only measured after a gate that had already failed, and measured 0 rows there).  -> (allowed rows, deferred rows)
    when every row left is within its kind's limit or owned by a stage still to run (gaps.GATE_DEFER) and at least one is allowed,
    else None.  A kind without a key keeps the gate closed; on_fail = continue changes nothing here."""
    from collections import Counter
    from . import gaps as GP
    lim = ((cons or {}).get("go") or {}).get(name) or {}
    later = set(later or ())
    dfr = [r for r in left or () if any(o in later for o in GP.GATE_DEFER.get(r["kind"], ()))]
    own = [r for r in left or () if r not in dfr]
    n = Counter(r["kind"] for r in own)
    try:
        if own and all(kind_key(k) in lim and v <= float(lim[kind_key(k)]) for k, v in n.items()):
            return own, dfr
    except (TypeError, ValueError):
        pass
    return None


# threshold key -> (check name, "max"|"min", measure(res, board, work))
_KEYS = {
    "opens_max": ("opens", "max", lambda r, b, w: _opens(b, w)),
    "power_width_max": ("power-width", "max", lambda r, b, w: _rows(r, "power-width")),
    "complete_min": ("pairs complete", "min", lambda r, b, w: (r.get("metrics") or {}).get("complete")),
    "not_searched_max": ("pairs not searched", "max", lambda r, b, w: (r.get("metrics") or {}).get("not_searched")),
    "fail_max": ("review FAIL", "max", lambda r, b, w: (r.get("metrics") or {}).get("fail")),
    "placement_data_fail_max": ("review placement-data FAIL", "max", lambda r, b, w: (r.get("metrics") or {}).get("placement_data_fail")),
    "post_error_max": ("DRC errors after stage", "max", lambda r, b, w: _drc_errors(b, w)),
}


def go_misses(cons, name, res, board, work):
    """[go.<name>] thresholds measured after the stage -> [{check, measured, limit}] for every miss (unmeasurable = miss)."""
    lim = ((cons or {}).get("go") or {}).get(name) or {}
    out = []
    for key, (check, kind, fn) in _KEYS.items():
        if key not in lim:
            continue
        try:
            v = fn(res, board, work)
        except Exception:                                           # noqa: BLE001 - an unmeasured threshold must not pass
            v = None
        if v is None:
            out.append({"check": check, "measured": "unmeasured", "limit": f"{'<=' if kind == 'max' else '>='} {lim[key]}"})
        elif (v > lim[key]) if kind == "max" else (v < lim[key]):
            out.append({"check": check, "measured": v, "limit": f"{'<=' if kind == 'max' else '>='} {lim[key]}"})
    return out


def own_open(name, res):
    """The stage's own warn with findings it owns (stalled / crashed close)."""
    if res.get("verdict") != "warn":
        return []
    if res.get("crashed"):
        return [{"check": f"{name} crashed", "measured": res.get("line", ""), "limit": "no crash"}]
    if res.get("unclosed"):
        return [{"check": f"{name} did not close", "measured": res["unclosed"], "limit": "clean"}]
    return []


def earlier_open(st, name, stages, i, res):
    """Deferred findings of an earlier stage whose owners have all run now (`name` was the last) while this stage did not pass."""
    from . import gaps as GP
    if res.get("verdict") == "pass":
        return []
    later = set(stages[i + 1:])
    out = []
    for k in stages[:i]:
        for r in ((st["stages"].get(k) or {}).get("gate") or {}).get("deferred") or []:
            owners = GP.GATE_DEFER.get(r["kind"], ())
            if name in owners and not any(o in later for o in owners):
                out.append({"check": f"{r['kind']} deferred from {k}", "measured": f"still open after {name} ({res.get('verdict')})", "limit": "closed"})
    return out


def check(cons, st, name, i, stages, res, board, work):
    """-> list of failing checks (stop) or []. on_fail=continue never stops."""
    if on_fail(cons) == "continue":
        return []
    return own_open(name, res) + earlier_open(st, name, stages, i, res) + go_misses(cons, name, res, board, work)


def write_stop(work, name, n, reason, checks, board, input_sha):
    cmd = "fluxplace pipeline " + " ".join(shlex.quote(x) for x in sys.argv[2:]) if len(sys.argv) > 2 else "fluxplace pipeline <same arguments>"
    obj = {"stage": name, "n": n, "reason": reason, "checks": checks, "board": board, "input_sha1": input_sha, "resume": cmd}
    with open(os.path.join(work, STOP), "w") as fh:
        json.dump(obj, fh, indent=1)
    print(f"pipeline: stop at {name}: {reason}", flush=True)
    return obj


def clear_stop(work):
    try:
        os.remove(os.path.join(work, STOP))
    except OSError:
        pass


def resume_point(work, st, stages, board0):
    """After a stop: (i0, board, k) = first stage without a go stamp matching its input, the board it starts from, stages passed.
    None when there is no STOP.json / state, or nothing passed."""
    if not st or not os.path.isfile(os.path.join(work, STOP)) or not st.get("stages"):
        return None
    cur, k = board0, 0
    for i, name in enumerate(stages):
        s = st["stages"].get(name) or {}
        go = s.get("go")
        if not go or not cur or go != board_sha(cur):
            break
        cur, k = s.get("board") or cur, i + 1
    return (k, cur) if k else None
