"""B6a: the close execution protocol. Pure helpers used by gaps.close; no pcbnew, no board knowledge.

Outcomes: every rung attempt ends in exactly one of not_applicable, no_change, candidate, timeout, child_error, invalid_output.
Only a measured `candidate` has a verdict (guard_refused / accepted).
Deadline: one absolute monotonic instant (CLOCK_MONOTONIC is shared by every process on the host). The stage deadline minus a
verification reserve bounds each rung; the supervisor kill instant ("hard") is the rung deadline and the child's own deadline
("child") is KILL_SLACK_S earlier, so the slack is inside the budget, never added to it. The child and its router descendants
clamp every timeout to `child` and inherit it in ENV_DEADLINE.
Attempt record: work/close/attempt-NNN.json is written before the child starts and completed on exit.
Clock: non-overlapping intervals that add up to the stage wall time.
"""
import hashlib
import json
import os
import time
from collections import Counter

mono = time.monotonic

OUTCOMES = ("not_applicable", "no_change", "candidate", "timeout", "child_error", "invalid_output")
CRASH_OUTCOMES = ("timeout", "child_error", "invalid_output")      # an identical retry of these is suppressed
VERDICTS = ("guard_refused", "accepted")
KILL_SLACK_S = 10.0              # supervisor grace after the child's own deadline; inside the rung allowance
VERIFY_RESERVE_S = 90.0          # [flow] verify_reserve_s: refill + DRC + guard + commit of the last candidate, kept before the stage deadline
MIN_RUNG_S = 5.0                 # less than this left for work: no new attempt
ENV_DEADLINE = "FLUXPLACE_DEADLINE_MONO"
CATS = ("measure", "diagnose", "queue", "child_start", "router", "rung", "refill_drc", "commit", "cleanup", "idle")


class RungFailure(RuntimeError):
    """A rung attempt that produced no usable result: outcome timeout / child_error / invalid_output."""

    def __init__(self, outcome, msg, code=None, sig=None, stderr_tail=""):
        super().__init__(msg)
        self.outcome, self.code, self.sig, self.stderr_tail = outcome, code, sig, stderr_tail


def tail(s, n=600):
    if isinstance(s, bytes):
        s = s.decode("utf-8", "replace")
    return (s or "").strip()[-n:]


def sha_file(p):
    try:
        with open(p, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    except (OSError, TypeError):
        return None


def sha_files(paths):
    """One sha over several files (absent ones count as "-")."""
    h = hashlib.sha1()
    for p in paths:
        h.update((sha_file(p) or "-").encode())
    return h.hexdigest()


# ---- deadline ----

def reserve_s(cons, budget_s):
    r = float(((cons or {}).get("flow") or {}).get("verify_reserve_s", VERIFY_RESERVE_S))
    return max(0.0, min(r, 0.1 * float(budget_s)))


def allowance(stage_deadline, now, cap_s, reserve, slack_s=KILL_SLACK_S, min_s=MIN_RUNG_S):
    """Times for one rung started at `now`: None when less than MIN_RUNG_S of work fits before stage_deadline - reserve.
    hard = supervisor kill instant; child = the instant the child and its router descendants work to (hard - slack)."""
    avail = stage_deadline - now - reserve
    if avail < min_s:
        return None
    hard = now + min(avail, cap_s)
    slack = min(slack_s, 0.1 * (hard - now))
    return {"hard": hard, "child": hard - slack, "slack": slack, "allow_s": hard - now}


def left(deadline, cap=None, floor=1.0):
    """Seconds until `deadline` (monotonic), at most `cap`, at least `floor` (a subprocess timeout must be positive)."""
    if deadline is None:
        return cap
    r = deadline - mono()
    return max(floor, r if cap is None else min(cap, r))


def expired(deadline):
    return deadline is not None and mono() >= deadline


# ---- phase marks (written by the child and its descendants, read by the parent) ----

def mark(path, name):
    if not path:
        return
    try:
        with open(path, "a") as fh:
            fh.write(json.dumps({"p": name, "m": mono(), "w": time.time()}) + "\n")
    except OSError:
        pass


class phase:
    """with phase(ctx.get("phase_file"), "router"): ... writes `router` and `router_done` marks."""

    def __init__(self, path, name):
        self.path, self.name = path, name

    def __enter__(self):
        mark(self.path, self.name)

    def __exit__(self, *a):
        mark(self.path, self.name + "_done")


def read_marks(path):
    out = []
    try:
        with open(path) as fh:
            for ln in fh:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def child_parts(marks, launch, now):
    """{category: seconds} for the child's wall [launch, now] from its marks: child_start (to the `child_start` mark; all of it when
    the child never started), router (open `router` marks run to `now`), rung (the rest)."""
    start = next((x["m"] for x in marks if x["p"] == "child_start"), None)
    if start is None:
        return {"child_start": max(0.0, now - launch)}
    router, opened = 0.0, None
    for x in marks:
        if x["p"] == "router" and opened is None:
            opened = x["m"]
        elif x["p"] == "router_done" and opened is not None:
            router += x["m"] - opened
            opened = None
    if opened is not None:
        router += now - opened
    pre = max(0.0, start - launch)
    work = max(0.0, now - start)
    router = min(router, work)
    return {"child_start": pre, "router": router, "rung": work - router}


def last_phase(marks):
    """{"last_completed": last mark written outside an open router phase, "in_progress": "router" while a router phase is open}."""
    opened, last = False, None
    for x in marks:
        if x["p"] == "router":
            opened = True
            continue
        opened = opened and x["p"] != "router_done"
        last = x["p"]
    return {"last_completed": last, "in_progress": "router" if opened else None}


def phase_times(marks):
    """[[name, seconds since the first mark]] in write order, and the wall time of the first mark."""
    if not marks:
        return {"t0_wall": None, "marks": []}
    t0 = marks[0]["m"]
    return {"t0_wall": marks[0].get("w"), "marks": [[x["p"], round(x["m"] - t0, 3)] for x in marks]}


# ---- attempt record ----

def _dump_atomic(path, obj, **kw):
    """Write `obj` as JSON via a temp file and rename (best effort)."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(obj, fh, sort_keys=True, **kw)
        os.replace(tmp, path)
    except OSError:
        pass


class AttemptRecord:
    """work/close/attempt-NNN.json: written before the child is launched (state "launched"), completed on exit (state "done")."""

    def __init__(self, work, n):
        d = os.path.join(work, "close")
        self.path = os.path.join(d, f"attempt-{n:03d}.json")
        self.phase_file = os.path.join(d, f"attempt-{n:03d}.phases.jsonl")
        self.doc = {}

    def _write(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError:
            return
        _dump_atomic(self.path, self.doc, default=str, indent=1)

    def start(self, **doc):
        try:
            os.remove(self.phase_file)
        except OSError:
            pass
        self.doc = dict(doc, state="launched")
        self._write()

    def finish(self, **kw):
        self.doc.update(kw, state="done")
        self._write()


# ---- clock ----

class Clock:
    """Wall time of a stage in non-overlapping intervals: exactly one category is current; switch() closes the interval."""

    def __init__(self, t0=None):
        self.t0 = mono() if t0 is None else t0
        self.last, self.cur = self.t0, "idle"
        self.acc = dict.fromkeys(CATS, 0.0)
        self.hook = None             # called with the category entered (the attempt record's phase marks)

    def switch(self, cat, now=None):
        now = mono() if now is None else now
        self.acc[self.cur] += max(0.0, now - self.last)
        self.last, self.cur = now, cat
        if self.hook and cat in ("refill_drc", "diagnose", "commit", "cleanup"):
            self.hook(cat)

    def split(self, parts, then="idle", now=None):
        """Close the current interval by distributing it over `parts` {cat: s} (what is not named goes to the last key), then switch."""
        now = mono() if now is None else now
        span = max(0.0, now - self.last)
        if parts:
            used = 0.0
            for c, s in parts.items():
                s = min(max(0.0, s), span - used)
                self.acc[c] += s
                used += s
            self.acc[next(reversed(parts))] += span - used
        else:
            self.acc[self.cur] += span
        self.last, self.cur = now, then

    def total(self, now=None):
        self.switch(self.cur, now)
        return sum(self.acc.values())

    def line(self, now=None):
        tot = self.total(now)
        parts = " | ".join(f"{c} {self.acc[c]:.1f}s ({100 * self.acc[c] / tot:.1f}%)" for c in CATS) if tot > 0 else "-"
        return f"close time {tot:.1f}s = {parts} | sum {100 * sum(self.acc.values()) / tot if tot > 0 else 0:.1f}%"


# ---- scheduling ----

def group_by_shared(ids_nets_blockers):
    """[(id, nets, blockers)] -> [[id]]: gaps that share a blocker net or a net of their own are one group (union-find)."""
    items = list(ids_nets_blockers)
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    owner = {}
    for i, (_gid, nets, blk) in enumerate(items):
        for tag in [("n", n) for n in nets] + [("b", b) for b in blk]:
            j = owner.setdefault(tag, i)
            if j != i:
                parent[find(i)] = find(j)
    groups = {}
    for i, (gid, _n, _b) in enumerate(items):
        groups.setdefault(find(i), []).append(gid)
    return list(groups.values())


def rate(stats, kind, rung):
    """Measured connections closed per minute for (kind, rung): (closed + 0.5) / (minutes + 1). An unmeasured rung is 0.5/min; a slow
    rung that closed nothing falls below it."""
    secs, closed = stats.get((kind, rung), (0.0, 0))
    return (closed + 0.5) / (secs / 60.0 + 1.0)


STARVE_S = 300.0                                 # a (kind, rung) that spent this long and closed nothing goes behind every rung that has not


def starved(stats, kind, rung):
    secs, closed = stats.get((kind, rung), (0.0, 0))
    return secs >= STARVE_S and closed == 0


def next_task(units, done, stats):
    """units: [{key, kind, prio, rungs}] (prio: (kind order, class rank); rungs: eligible rungs in rank order); done: {(key, rung)}.
    -> (unit, rung) | None. First pass: a unit with no attempt yet this round gets its first rung, in priority order, before any unit
    gets a second one. After that: the remaining (unit, rung) pairs by electrical priority, then the measured rate of closed
    connections per minute (a starved rung -- STARVE_S spent, nothing closed -- goes last) of the (kind, rung), then the rung's rank order."""
    tried = {k for k, _ in done}
    fresh = [u for u in units if u["rungs"] and u["key"] not in tried]
    if fresh:
        u = min(fresh, key=lambda u: (u["prio"], u["key"]))
        return u, u["rungs"][0]
    best = None
    for u in units:
        order = u.get("order") or u["rungs"]            # the kind's full rung order: a unit without the whole-board rungs keeps the same ranks
        for r in u["rungs"]:
            if (u["key"], r) in done:
                continue
            i = order.index(r)
            k = (starved(stats, u["kind"], r), u["prio"], -rate(stats, u["kind"], r), i, u["key"])
            if best is None or k < best[0]:
                best = (k, u, r)
    return (best[1], best[2]) if best else None


# ---- crash memo ----

def crash_key(board_sha, rung, params, ids):
    return hashlib.sha1(json.dumps([board_sha, rung, params, sorted(ids)], sort_keys=True, default=str).encode()).hexdigest()


class CrashMemo:
    """Identical retries (same input sha + rung + params + targets) of a timed-out / crashed / invalid attempt are suppressed.
    Persisted in work/crashed.json, so a resumed close does not repeat them either."""

    def __init__(self, work):
        self.path = os.path.join(work, "crashed.json")
        try:
            with open(self.path) as fh:
                self.rows = json.load(fh)
        except (OSError, ValueError):
            self.rows = {}
        if not isinstance(self.rows, dict):
            self.rows = {}

    def has(self, key):
        return key in self.rows

    def add(self, key, outcome, why):
        self.rows[key] = {"outcome": outcome, "why": str(why)[:160]}
        _dump_atomic(self.path, self.rows)


# ---- outcome classification ----

NOT_APPLICABLE = ("no signal layers", "no plain net", "nothing dead", "no rf net", "no pair", "no net", "no conn", "no budget",
                  "ai off", "ai budget", "no packet", "not applicable", "n/a", "skipped", "none applicable")


def classify_no_board(summ):
    """Outcome of an attempt whose rung wrote no candidate board, from the rung's own summary."""
    summ = summ or {}
    st = str(summ.get("status") or summ.get("reason") or "").strip().lower()
    if st in NOT_APPLICABLE or st.startswith("not applicable"):
        return "not_applicable"
    if "timeout" in st or st == "budget":
        return "timeout"
    if st == "krt not found" or st.startswith("error"):
        return "child_error"
    if st.startswith("invalid"):
        return "invalid_output"
    return "no_change"


def failure_outcome(exc):
    """(outcome, detail dict) of an exception raised by an attempt."""
    if isinstance(exc, RungFailure):
        return exc.outcome, {"code": exc.code, "signal": exc.sig, "stderr_tail": exc.stderr_tail}
    return "child_error", {"exception": f"{type(exc).__name__}: {exc}"[:200]}


def truncated_board(path):
    """True for a kicad board file cut short (starts like a board, does not end with its closing paren)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
            if not head.startswith(b"(kicad_pcb"):
                return False
            fh.seek(0, os.SEEK_END)
            n = fh.tell()
            fh.seek(max(0, n - 64))
            return not fh.read().rstrip().endswith(b")")
    except OSError:
        return False


# ---- tallies ----

class Tally:
    """What the close line reports besides the clock: outcome counts, distinct open components that received an applicable
    attempt, attempts with a readable candidate, connections closed (opens removed by accepted attempts)."""

    def __init__(self):
        self.outcomes, self.verdicts = Counter(), Counter()
        self.components, self.candidates, self.closed, self.skipped = set(), 0, 0, 0

    def add(self, outcome, verdict, components, closed):
        self.outcomes[outcome] += 1
        if verdict:
            self.verdicts[verdict] += 1
        if outcome != "not_applicable":
            self.components.update(components)
        if outcome == "candidate":
            self.candidates += 1
        self.closed += max(0, closed)

    def line(self):
        oc = " ".join(f"{k}={self.outcomes[k]}" for k in OUTCOMES)
        vd = " ".join(f"{k}={self.verdicts[k]}" for k in VERDICTS)
        return (f"close attempts {sum(self.outcomes.values())} (+{self.skipped} skipped): {oc} | {vd} | "
                f"disconnected components with an applicable attempt {len(self.components)} | "
                f"attempts with a readable candidate {self.candidates} | connections closed {self.closed}")
