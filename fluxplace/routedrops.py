"""Why router results were dropped: one reason per dropped result, counted per round and in total.

Reasons: under-floor (width/via/drill/clearance below the rule floors), rips-other-net (the result removed or
altered other copper), via-in-pad, off-board (copper crossing the board outline), timeout, no-path (the router
returned nothing new for the net), other.  The counts go into the status JSON (`dropped`, per round `dropped_by`)
and the compact status line (`routestatus`)."""
from collections import Counter

REASONS = ("under-floor", "rips-other-net", "via-in-pad", "off-board", "timeout", "no-path", "other")
_CODE = {"CLEARANCE": "under-floor", "TRACK_WIDTH": "under-floor", "VIA_SIZE": "under-floor",
         "VIA_DRILL": "under-floor", "VIA_IN_PAD": "via-in-pad", "OFF_BOARD": "off-board",
         "FANOUT_RIPPED": "rips-other-net", "RIPPED": "rips-other-net"}


def reason_of(viol):
    """Reason for a list of audit violations: that of the most frequent code (ties: reason order above)."""
    c = Counter(_CODE.get(v.get("code"), "other") for v in viol or [])
    if not c:
        return "other"
    return max(c, key=lambda r: (c[r], -REASONS.index(r)))


class Tally:
    """Counts dropped results by reason, in total and for the current round."""

    def __init__(self):
        self.total, self.round = Counter(), Counter()

    def add(self, reason, n=1):
        reason = reason if reason in REASONS else "other"
        self.total[reason] += n
        self.round[reason] += n

    def new_round(self):
        self.round = Counter()

    @staticmethod
    def _d(c):
        return {r: c[r] for r in REASONS if c.get(r)}

    def totals(self):
        return self._d(self.total)

    def this_round(self):
        return self._d(self.round)


def fmt(d):
    return ", ".join(f"{k} {v}" for k, v in (d or {}).items()) or "none"
