"""Packet classes (B12, OUTSIDE-REVIEW 9.3). A stage that writes AI packets no longer ends with a bare "unanswered" count:
every packet gets one class and one handling, and a still-open net is named in the stage line (it is a FAIL, not a default).

  default  deterministic recovery: the registered default plan ran and closed the net (nothing left to answer)
  search   bounded search: the net is still open; a later rung owns it (route / close `ai` rung); the packet stays on disk as
           an optional proposal generator, the executor validates and guards whatever comes back
  owner    owner decision: the packet carries `needs` in OWNER_NEEDS (board size, stackup, fixed constraint). The stage
           suspends with a resumable question row (id `packet:<key>`, answered with --answer, [answers] or the map)
"""
import json
import os

DEFAULT, SEARCH, OWNER = "default", "search", "owner"
OWNER_NEEDS = ("board-size", "stackup", "fixed-constraint")


def _need(path):
    try:
        with open(path) as fh:
            n = json.load(fh).get("needs")
    except (OSError, ValueError, AttributeError):
        return None
    return n if n in OWNER_NEEDS else None


def classify(path, key, still_open):
    """-> {packet, key, class, needs}. `still_open`: keys (nets or gap ids) the default plan left open."""
    need = _need(path)
    cls = OWNER if need else (SEARCH if key in still_open else DEFAULT)
    return {"packet": path, "key": key, "class": cls, "needs": need}


def question(row):
    """The resumable owner-question row of an owner packet (questions.py shape)."""
    return {"id": "packet:" + row["key"], "topic": "packet-" + str(row["needs"]), "found": f"{row['key']} needs {row['needs']} (packet {os.path.basename(row['packet'])})",
            "options": [{"choice": "accept-open", "consequence": f"{row['key']} stays open; later stages and review report it"},
                        {"choice": "change", "consequence": f"owner changes {row['needs']} in the constraints/spec; re-run from this stage"}],
            "default": "accept-open", "settle": "owner decision"}


def resolve(items, still_open, answers=None, carried="the route/close rungs", ai_dir=None):
    """items: [(packet path, key)]. -> {"n", "packets", "classes", "rows", "questions", "line"}; questions = owner rows with no answer."""
    answers = answers or {}
    rows = [classify(p, k, still_open) for p, k in items]
    cnt = {c: sum(1 for r in rows if r["class"] == c) for c in (DEFAULT, SEARCH, OWNER)}
    qs = [question(r) for r in rows if r["class"] == OWNER and ("packet:" + r["key"]) not in answers]
    open_keys = [r["key"] for r in rows if r["class"] == SEARCH]
    line = (f"{len(rows)} packets: {cnt[DEFAULT]} default applied, {cnt[SEARCH]} bounded search"
            + (f" (STILL OPEN, carried to {carried}: {', '.join(open_keys[:12])})" if open_keys else "")
            + f", {cnt[OWNER]} owner decision" + (f" ({len(qs)} question rows, answer with --answer packet:<key>=<choice>)" if qs else ""))
    out = {"n": len(rows), "packets": [p for p, _ in items], "classes": cnt, "rows": rows, "questions": qs, "line": line}
    if ai_dir and qs:
        os.makedirs(ai_dir, exist_ok=True)
        with open(os.path.join(ai_dir, "owner-questions.json"), "w") as fh:
            json.dump(qs, fh, indent=1)
    return out
