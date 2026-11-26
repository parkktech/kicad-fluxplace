"""B6 step 0: the refusal record. Every refused close candidate gets one JSONL row in work/refusals.jsonl:
score delta per term, target delta, added/removed DRC keys with severity, changed nets/items, the blocker set, and EVERY
acceptance rule with its status (pass / fail / UNKNOWN). A check that could not run is UNKNOWN: it refuses, and it is
neither a pass nor an ordinary violation. Records only; acceptance decisions are made by gaps._accept as before."""
import json
import os
from collections import Counter

FILE = "refusals.jsonl"
HASH_CAP = 20                                   # item ids listed per net; counts are exact


def path(work):
    return os.path.join(work, FILE)


def _sev_keys(m):
    """Counter {(severity, type, descs)}: every violation of the measure's DRC report; error keys alone when no report is kept."""
    from . import drcguard
    d = m.get("drc")
    if isinstance(d, dict):
        return Counter((v.get("severity", "?"),) + tuple(drcguard.key_of(v)) for v in d.get("violations") or [])
    return Counter(("error",) + tuple(k) for k in (m.get("keys", m.get("err_keys")) or {}).elements())


def _keys_rows(c):
    return [{"severity": k[0], "type": k[1], "items": list(k[2]), "n": n} for k, n in sorted(c.items(), key=str)]


def drc_delta(base_m, cand_m):
    b, c = _sev_keys(base_m), _sev_keys(cand_m)
    return {"added": _keys_rows(c - b), "removed": _keys_rows(b - c)}


def score_delta(base_m, cand_m, cand_gaps):
    from . import gaps as G
    sb, sc = G.score_terms(base_m, base_m.get("gaps") or []), G.score_terms(cand_m, cand_gaps)
    return {t: {"base": sb[t], "cand": sc[t], "delta": round(sc[t] - sb[t], 6)} for t in sb}


def target_delta(base_m, cand_gaps, target_ids):
    ids = {g["id"] for g in cand_gaps}
    closed = [t for t in target_ids if t not in ids]
    return {"ids": list(target_ids), "closed": closed, "still": [t for t in target_ids if t in ids],
            "closed_all": bool(target_ids) and len(closed) == len(target_ids)}


def changed_items(base_board, cand_board):
    """{net: {added, removed, ids}} from the item-hash diff (boardwrite.net_items) of the two boards, or UNKNOWN."""
    try:
        from . import boardwrite as BW
        a, b = BW.net_items(base_board), BW.net_items(cand_board)
        out = {}
        for n in sorted(set(a) | set(b)):
            ca, cb = Counter(a.get(n, [])), Counter(b.get(n, []))
            add, rem = cb - ca, ca - cb
            if add or rem:
                out[n] = {"added": sum(add.values()), "removed": sum(rem.values()),
                          "ids": sorted(map(str, list(add) + list(rem)))[:HASH_CAP]}
        return {"status": "ok", "nets": out}
    except Exception as e:
        return {"status": "UNKNOWN", "why": f"{type(e).__name__}: {e}"[:120]}


def _blockers(summ):
    s = summ or {}
    b = s.get("blockers") or s.get("blocker") or s.get("blocker_nets")
    if not b:
        return None
    return sorted(map(str, b)) if isinstance(b, (list, tuple, set)) else [str(b)]


def record(*, ctx, rules, base_m=None, cand_m=None, cand_gaps=None, target_ids=(), base_board=None, cand_board=None, summ=None):
    """The refusal row. `ctx`: {run, attempt, kind, rung, result, why, targets}. `rules`: rows of drcguard.run_rules.
    Every measurement is taken in its own guard: one that crashes becomes UNKNOWN in the row, never an exception."""
    rec = dict(ctx, rules=rules, blockers=_blockers(summ))
    if base_m is not None and cand_m is not None:
        for key, fn in (("score_delta", lambda: score_delta(base_m, cand_m, cand_gaps or [])),
                        ("target", lambda: target_delta(base_m, cand_gaps or [], list(target_ids))),
                        ("drc", lambda: drc_delta(base_m, cand_m))):
            try:
                rec[key] = fn()
            except Exception as e:
                rec[key] = {"status": "UNKNOWN", "why": f"{type(e).__name__}: {e}"[:120]}
        rec["changed"] = changed_items(base_board, cand_board) if base_board and cand_board else {"status": "UNKNOWN", "why": "no board"}
    rec["failing"] = [r["rule"] for r in rules if r["status"] != "pass"]
    rec["refused"] = True
    return rec


def no_candidate(ctx, rule, status, detail):
    """Refusal before any candidate was measured (no board written, no change, refill failed, rung crashed)."""
    return record(ctx=ctx, rules=[{"rule": rule, "status": status, "detail": str(detail)[:120]}])


def append(work, rec):
    """Best effort: a logging failure never changes the run."""
    try:
        os.makedirs(work, exist_ok=True)
        with open(path(work), "a") as fh:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    except OSError:
        pass


def load(p):
    out = []
    try:
        with open(p) as fh:
            for ln in fh:
                if ln.strip():
                    out.append(json.loads(ln))
    except OSError:
        pass
    return out


def histogram(recs):
    """{"n", "first": Counter, "any": Counter, "score_equal_only": int}. first = first non-pass rule; any = every non-pass rule
    (once per record); score_equal_only = R4 failed, every score term equal, and no other rule failed or is UNKNOWN."""
    first, anyr, eq = Counter(), Counter(), 0
    for r in recs:
        f = r.get("failing") or []
        if f:
            first[f[0]] += 1
        anyr.update(set(f))
        sd = r.get("score_delta") or {}
        flat = all(isinstance(v, dict) and v.get("delta") == 0 for v in sd.values()) and bool(sd)
        if f == ["R4"] and flat:
            eq += 1
    return {"n": len(recs), "first": first, "any": anyr, "score_equal_only": eq}


def summarize(work, log=print):
    """One histogram line for work/refusals.jsonl (the close stage calls this at its end). Returns the histogram."""
    h = histogram(load(path(work)))
    if h["n"]:
        fmt = lambda c: " ".join(f"{k}={v}" for k, v in c.most_common()) or "-"      # noqa: E731
        log(f"close refusals {h['n']}: first-failing [{fmt(h['first'])}] any-failing [{fmt(h['any'])}] "
            f"score-equal-no-other-failure={h['score_equal_only']}")
    return h
