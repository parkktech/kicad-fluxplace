"""Live placer view (F16, D71dj): progress lines, placement snapshots, 3D export, owner feedback and waiting gates.

Contract for the dashboard: docs/EVENTS-CONTRACT.md (section "Placer"). Run-script lines: docs/recipes/LIVE-PLACER.md.
Rules that hold for every function here:
  - Watching changes nothing: no draw from a placer's random stream, no trial reordered, files only under W/live and W/feedback.
  - Nothing blocks or raises into a stage: a failure is one row in W/live/errors.jsonl.
  - No reference, net or part number in code; criteria and key terms are whatever the stage hands in.
W = the stage workdir = the folder of the event stream (env FLUXPLACE_EVENTS). Stream off: every function is a no-op.
A nested run (env FLUXPLACE_NESTED: the ghost and candidate pipelines a stage starts) is never shown: thousands of trials
are not a view. The 3D export is on only when the constraints have a `[live]` table (it costs a process and 1 GB for 16 s).
"""
import contextlib
import json
import math
import os
import re
import shutil
import sys
import threading
import time

from . import events

ENV_OPS = "FLUXPLACE_OPS"          # the run-level ops feed (W<n>/ops.jsonl); every line is also written there when set
PROGRESS_S = 2.0                   # at most one placement_progress per stage in this many seconds (always-on lines bypass it)
PROGRESS_KEYS = ("phase", "generation", "generations", "trial", "trials", "arm", "cand", "seed", "pair", "key", "leader", "leader_key",
                 "is_leader", "criteria", "verdict", "why", "rows_rev", "elapsed_s", "eta_s")
_last = {}
NARR_S = 8.0                       # a "doing" narration line at least this often while a stage works (no silence over 10 s)
_beat_wait = lambda stop, secs: stop.wait(secs)   # the heartbeat's pause (a seam: tests drive it with a fake clock)
_narr = {}                         # stage -> the latest "doing" text, repeated by the heartbeat
_narr_gen = {}                     # stage -> the last generation narrated (one round line per generation)
_watch = {"stage": None}           # the stage whose heartbeat runs in this process (nested watches do nothing)
START_TEXT = {
    "ready": "Checking the parts list, the stackup and the board outline before any part is moved. Done when every check is kept or waived.",
    "modules": "Grouping the parts into modules around their anchor parts. Done when every part belongs to a module.",
    "floorplan": "Seating the modules on the board, then searching for a better arrangement. Done when the search ends and the best layout is kept.",
    "placetour": "Trying small part moves and judging each one with a quick route. Done when the best moves are kept.",
}


def work():
    """The stage workdir W, or "" when the stream is off or this is a nested run."""
    return "" if os.environ.get("FLUXPLACE_NESTED") else events._workdir()


def _err(where, e):
    try:
        W = work()
        if W:
            os.makedirs(os.path.join(W, "live"), exist_ok=True)
            with open(os.path.join(W, "live", "errors.jsonl"), "a") as fh:
                fh.write(json.dumps({"ts": round(time.time(), 3), "where": where, "error": f"{type(e).__name__}: {e}"[:400]}) + "\n")
    except Exception:
        pass


def _emit(kind, stage=None, **f):
    """One line on the stage stream and, with FLUXPLACE_OPS set, the same line on the ops feed. Never raises."""
    events.emit(kind, stage=stage, **f)
    if os.environ.get(ENV_OPS):                  # not events.emit_to: several placer lines carry a field named `path`
        try:
            events._append(os.environ[ENV_OPS], events._line(kind, stage, f))
        except Exception:                        # noqa: BLE001
            pass


def _write(path, obj):
    """JSON by rename: a file that exists is complete."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, separators=(",", ":"), default=str)
    os.replace(tmp, path)


# ---------------------------------------------------------------- N12: narration (plain words, one line per phase)
def narrate(stage, text, phase="doing", eta_s=None, remember=True):
    """One narration line: phase start | doing | end, plain words for the owner. Never raises; off without a stream."""
    try:
        if os.environ.get("FLUXPLACE_NESTED") or not events.enabled():
            return None
        phase = phase if phase in ("start", "doing", "end") else "doing"
        text = " ".join(str(text).split())[:300]
        if phase == "doing" and remember:       # the heartbeat's own fallback line is never remembered (it would freeze its elapsed time)
            _narr[str(stage)] = text
        _emit("narration", stage=stage, phase=phase, text=text, eta_s=eta_s)
        return {"stage": stage, "phase": phase, "text": text, "eta_s": eta_s}
    except Exception as e:                       # noqa: BLE001
        _err("narrate", e)
        return None


def own_stage(stage, text, phase):
    """Start / end line of a stage run directly (ready, modules, floorplan): skipped under the pipeline, which emits its own."""
    if not os.environ.get(events.ENV_STAGE):
        return narrate(stage, text, phase)
    return None


STAGE_CMDS = ("ready", "modules", "floorplan")     # commands that are also pipeline stages (run directly: their own start / end)


def run_command(stage, fn, a):
    """A stage command run on its own (not under the pipeline): start line, heartbeat, end line. Returns fn's rc; never
    swallows an exception. The end line says how it stopped."""
    own_stage(stage, START_TEXT.get(stage) or f"Running the {stage} check. Done when it reports pass or fail.", "start")
    t0, done = time.monotonic(), "stopped"
    try:
        with watching(stage):
            rc = fn(a)
        done = "done" if not rc else f"exit {rc}"
        return rc
    finally:
        own_stage(stage, f"{stage} {done} after {time.monotonic() - t0:.1f} s.", "end")


@contextlib.contextmanager
def watching(stage):
    """Heartbeat for the life of a stage: a "doing" line every NARR_S seconds with the stage's latest phase text, so no stage
    is silent for long. One per process: a nested watch does nothing. Off without a stream."""
    if _watch["stage"] is not None or not events.enabled() or os.environ.get("FLUXPLACE_NESTED"):
        yield
        return
    stop, t0 = threading.Event(), time.monotonic()
    _watch["stage"] = stage
    _narr.pop(stage, None)
    _narr_gen.pop(stage, None)

    def beat():
        while not _beat_wait(stop, NARR_S):
            text = _narr.get(stage)
            narrate(stage, text or f"Still working on the {stage} stage, {int(time.monotonic() - t0)} s so far.", "doing", remember=False)

    th = threading.Thread(target=beat, name="narration-heartbeat", daemon=True)
    th.start()
    try:
        yield
    finally:
        stop.set()
        th.join(1.0)
        _watch["stage"] = None


# ---------------------------------------------------------------- L1: placement_progress
def _key(key):
    """A judge key as [[term, value], ...]; a term not judged yet keeps its null."""
    if key is None:
        return None
    return [[str(k), v] for k, v in (key.items() if isinstance(key, dict) else key)]


def criteria(rows):
    """[{id, ok, value, limit, text}] from a stage's gate rows: dicts already in that shape, or budget-table rows
    (budget.row: name, kind, verdict, value, target, max, unit). ok is True, False or None (not measured)."""
    out = []
    for r in rows or []:
        if "verdict" in r:
            lim = r.get("max") if r.get("max") is not None else r.get("target")
            out.append({"id": str(r.get("name")), "ok": None if r["verdict"] in (None, "n/a") else r["verdict"] != "fail", "value": r.get("value"),
                        "limit": lim, "text": f"{r.get('kind', '')} {r.get('name')}: {r.get('value')} / {lim} {r.get('unit', '')}".strip()})
            if "hard" in r:                # a budget row: hard = datasheet limit, false = derived or default (DASHBOARD-PLACER-CONTRACT)
                out[-1]["hard"] = r["hard"]
        else:
            out.append({"id": str(r.get("id")), "ok": r.get("ok"), "value": r.get("value"), "limit": r.get("limit"), "text": str(r.get("text", ""))})
            if "hard" in r:
                out[-1]["hard"] = r["hard"]
    return out


def _text(stage, f):
    bits = [str(stage), str(f["phase"])]
    for name, a, b in (("gen", f["generation"], f["generations"]), ("trial", f["trial"], f["trials"])):
        if a is not None:
            bits.append(f"{name} {a}" + (f"/{b}" if b is not None else ""))
    bits += [str(f[k]) for k in ("arm", "cand") if f[k]]
    bits += [f"{k} {v}" for k, v in (f["key"] or [])[:2]]
    return " ".join(bits + [str(f["verdict"])])


def progress(stage, phase, always=False, **fields):
    """One placement_progress line (fields: PROGRESS_KEYS; anything else is dropped). `always` = a generation end, a leader
    change, a kept or dropped candidate, the stage end: it bypasses the 2 s limit. -> the fields written, or None."""
    try:
        if os.environ.get("FLUXPLACE_NESTED") or not (events.enabled() or os.environ.get(ENV_OPS)):
            return None
        now = time.monotonic()
        if not always and now - _last.get(stage, -1e9) < PROGRESS_S:
            return None
        _last[stage] = now
        f = {k: fields.get(k) for k in PROGRESS_KEYS}
        f.update(phase=phase, key=_key(fields.get("key")), leader_key=_key(fields.get("leader_key")),
                 criteria=criteria(fields.get("criteria")), verdict=fields.get("verdict") or "running")
        f["text"] = _text(stage, f)
        _emit("placement_progress", stage=stage, **f)
        if always and f["generation"] is not None and _narr_gen.get(stage) != f["generation"]:
            _narr_gen[stage] = f["generation"]
            g, big = f["generation"], f["generations"]
            best = ", ".join(f"{k} {v}" for k, v in (f["leader_key"] or f["key"] or [])[:2]) or "not judged yet"
            narrate(stage, f"{stage} round {g}{f' of {big}' if big is not None else ''}: best so far {best}", "doing",
                    eta_s=f["eta_s"])
        return f
    except Exception as e:                       # noqa: BLE001 - the view never fails a stage
        _err("progress", e)
        return None


# ---------------------------------------------------------------- L2: placement snapshots
FULL_EVERY = 20                    # a full snapshot at least every 20 per lane
TRIAL_S = 5.0                      # the running trial at most this often
MOVE_MM, MOVE_DEG = 0.01, 0.01     # a part that moved less is not in `moved`
LANES = ("leader", "trial", "accepted")
GRID_N, GRID_MIN_MM = 8, 10.0      # about 8 x 8 cells, a cell never smaller than 10 mm
_snap = {"seq": 0, "lanes": {}}


def grid_of(bbox):
    """{cols, rows, cell: [w, h], origin: [x, y]}: cells are named column letter + row number, A1 at the origin."""
    x0, y0, x1, y1 = bbox
    cols = max(1, min(GRID_N, int((x1 - x0) // GRID_MIN_MM)))
    rows = max(1, min(GRID_N, int((y1 - y0) // GRID_MIN_MM)))
    return {"cols": cols, "rows": rows, "cell": [round((x1 - x0) / cols, 3), round((y1 - y0) / rows, 3)], "origin": [round(x0, 3), round(y0, 3)]}


def cell_rect(grid, cells):
    """(x0, y0, x1, y1) of "C2" or "C2:D3"; None when the name is not on the grid."""
    try:
        a, _, b = str(cells).partition(":")
        ix = []
        for name in (a, b or a):
            c, r = ord(name[0].upper()) - 65, int(name[1:]) - 1
            if not (0 <= c < grid["cols"] and 0 <= r < grid["rows"]):
                return None
            ix.append((c, r))
        (c0, r0), (c1, r1) = ix
        (ox, oy), (w, h) = grid["origin"], grid["cell"]
        return (round(ox + min(c0, c1) * w, 3), round(oy + min(r0, r1) * h, 3), round(ox + (max(c0, c1) + 1) * w, 3), round(oy + (max(r0, r1) + 1) * h, 3))
    except Exception:                            # noqa: BLE001
        return None


def cells_of(grid, rect):
    """The cell range that covers a rectangle, clamped to the grid: "C2" or "C2:D3"."""
    (ox, oy), (w, h) = grid["origin"], grid["cell"]
    cl = lambda v, n: max(0, min(n - 1, int(v)))                                                   # noqa: E731
    c0, c1 = cl((rect[0] - ox) / w, grid["cols"]), cl((rect[2] - ox) / w - 1e-9, grid["cols"])
    r0, r1 = cl((rect[1] - oy) / h, grid["rows"]), cl((rect[3] - oy) / h - 1e-9, grid["rows"])
    a, b = f"{chr(65 + c0)}{r0 + 1}", f"{chr(65 + c1)}{r1 + 1}"
    return a if a == b else f"{a}:{b}"


def _why_parts(B, parts, M, meta):
    """N13: per part `parked` (courtyard wholly outside the board outline) and `why` = {owner (the owner IC of its module, or null),
    module, fixed (reason class or null), rows [{id, ok, hard}] of the graded rows that name the part}. Pure."""
    x0, y0, x1, y1 = B["bbox"]
    anchor = {m["name"]: m.get("anchor") for m in (M or {}).get("modules") or []}
    rows = criteria((meta or {}).get("criteria"))
    toks = [(r, set(re.split(r"[^A-Za-z0-9_]+", r["id"]))) for r in rows]
    for p in parts:
        c = (B["parts"].get(p["ref"]) or {}).get("court")
        p["parked"] = bool(c) and (c[2] < x0 or c[0] > x1 or c[3] < y0 or c[1] > y1)       # courtyard wholly outside the outline
        mine = [{"id": r["id"], "ok": r["ok"], "hard": r.get("hard")} for r, t in toks if p["ref"] in t]
        p["why"] = {"owner": anchor.get(p["mod"]), "module": p["mod"], "fixed": p["fixed"], "rows": mine}


def snapshot_dict(B, seq, M=None, cons=None, regions=None, meta=None, board=""):
    """The full placement snapshot of the board model B (routediag.load shape; a floorplan2.Plan keeps its own in Plan.B):
    snapshot._build with no tracks and vias, plus mod / fixed / h per part, grid, modules, regions, keepouts and the meta
    fields (key, criteria, cand, arm, generation, rows_rev). Pure."""
    from . import budget as BG, fixedreason as FR, intent as IT, modules as MD, snapshot as SN
    cons, meta = cons or {}, meta or {}
    d = SN._build(B, seq, board)
    d["tracks"], d["vias"] = [], []
    of = (M or {}).get("of") or {}
    why, fixed = FR.declared(cons), MD.fixed_set(B, cons)
    for p in d["parts"]:
        r = p["ref"]
        p["mod"] = of.get(r)
        p["fixed"] = ((why.get(r) or {}).get("reason") or "undeclared") if r in fixed else None
        p["h"] = IT.height(B, cons, r)
    g = d["grid"] = grid_of(B["bbox"])
    _why_parts(B, d["parts"], M, meta)
    mods = []
    for m in (M or {}).get("modules") or []:
        cs = [B["parts"][r]["court"] for r in m["members"] if r in B["parts"]]
        if cs:
            mods.append({"id": m["name"], "name": m["name"], "role": m.get("block"), "side": m.get("side"), "fixed": bool(m.get("fixed")),
                         "bbox": [round(min(c[0] for c in cs), 3), round(min(c[1] for c in cs), 3), round(max(c[2] for c in cs), 3), round(max(c[3] for c in cs), 3)],
                         "group": m["name"] != m.get("anchor"), "n": len(m["members"])})
    d["modules"] = mods
    reg = []
    for r in IT.items(B, cons)["region"] if IT.section(cons) else []:
        rect = IT.region_rect(B, r)
        if rect:
            reg.append({"id": r["name"], "src": "intent", "target": list(r.get("refs") or []), "rect": [round(v, 3) for v in rect],
                        "cells": cells_of(g, rect), "keep": r.get("keep", "out"), "exit": None})
    for r in regions or []:
        reg.append({"id": r.get("id"), "src": r.get("src", "owner"), "target": r.get("target"), "rect": [round(float(v), 3) for v in r["rect"]],
                    "cells": r.get("cells") or cells_of(g, r["rect"]), "keep": r.get("keep", "in"), "exit": r.get("exit")})
    d["regions"] = reg
    d["keepouts"] = [{"id": k["name"], "pts": [[round(x, 3), round(y, 3)] for x, y in ((k["rect"][0], k["rect"][1]), (k["rect"][2], k["rect"][1]),
                                                                                       (k["rect"][2], k["rect"][3]), (k["rect"][0], k["rect"][3]))],
                      "layers": list(k["layers"]), "src": k["source"]} for k in BG.keepouts(B, cons)]
    for k in ("key", "criteria", "cand", "arm", "generation", "rows_rev"):
        d[k] = _key(meta.get(k)) if k == "key" else (criteria(meta.get(k)) if k == "criteria" else meta.get(k))
    return d


def snapshot_delta(prev, cur):
    """{v, seq, base, parts: [changed part objects], moved: [{ref, dx, dy, drot, flip}], modules: [changed], regions (only
    when the list changed), key} from full dict `prev` to full dict `cur`."""
    pp = {p["ref"]: p for p in prev["parts"]}
    changed, moved = [], []
    for p in cur["parts"]:
        o = pp.get(p["ref"])
        if o == p:
            continue
        changed.append(p)
        if o is None:
            continue
        dx, dy, drot = p["x"] - o["x"], p["y"] - o["y"], (p["rot"] - o["rot"] + 180.0) % 360.0 - 180.0
        if math.hypot(dx, dy) >= MOVE_MM or abs(drot) >= MOVE_DEG or p["side"] != o["side"]:
            moved.append({"ref": p["ref"], "dx": round(dx, 3), "dy": round(dy, 3), "drot": round(drot, 2), "flip": p["side"] != o["side"]})
    pm = {m["id"]: m for m in prev["modules"]}
    out = {"v": 1, "seq": cur["seq"], "base": prev["seq"], "parts": changed, "moved": moved,
           "modules": [m for m in cur["modules"] if pm.get(m["id"]) != m], "key": cur.get("key")}
    if cur["regions"] != prev["regions"]:
        out["regions"] = cur["regions"]
    return out


def place_snapshot(B, lane="leader", meta=None, M=None, cons=None, regions=None, full=False, board="", stage=None):
    """Write one placement snapshot of B to W/live/place/<lane>-NNNNNN[.delta].json and emit placement_snapshot.
    Full when asked (`full`: stage end), first of the lane, or after FULL_EVERY deltas; else a delta against the lane's last
    full. The `trial` lane is throttled to one per TRIAL_S. -> the relative path, or None (off / throttled / failed)."""
    try:
        W = work()
        if not W or lane not in LANES:
            return None
        if _snap.get("W") != W:                  # another process wrote this W: continue its numbers, every lane starts full
            d = os.path.join(W, "live", "place")
            names = os.listdir(d) if os.path.isdir(d) else []
            _snap.update(W=W, lanes={}, seq=max((int(n.split("-")[-1].split(".")[0]) for n in names
                                                 if "-" in n and n.split("-")[-1].split(".")[0].isdigit()), default=0))
        st = _snap["lanes"].setdefault(lane, {"prev": None, "since": 0, "last": -1e9})
        now = time.monotonic()
        if lane == "trial" and not full and now - st["last"] < TRIAL_S:
            return None
        st["last"] = now
        from . import snapshot as SN
        _snap["seq"] += 1
        seq = _snap["seq"]
        cur = snapshot_dict(B, seq, M, cons, regions, meta, board)
        os.makedirs(os.path.join(W, "live", "place"), exist_ok=True)
        is_full = bool(full or st["prev"] is None or st["since"] >= FULL_EVERY)
        if is_full:
            rel, n_moved, base = f"live/place/{lane}-{seq:06d}.json", 0, None
            SN._write(os.path.join(W, rel), cur)
            st.update(prev=cur, since=0)
        else:
            dl = snapshot_delta(st["prev"], cur)
            rel, n_moved, base = f"live/place/{lane}-{seq:06d}.delta.json", len(dl["moved"]), dl["base"]
            SN._write(os.path.join(W, rel), dl)
            st["since"] += 1
        m = meta or {}
        _emit("placement_snapshot", stage=stage, lane=lane, seq=seq, path=rel, full=is_full, delta=not is_full, base=base, cand=m.get("cand"),
              arm=m.get("arm"), generation=m.get("generation"), moved=n_moved, parts=len(cur["parts"]), key=cur.get("key"),
              text=f"{lane} snapshot {seq}: {len(cur['parts'])} parts" + ("" if is_full else f", {n_moved} moved"))
        return rel
    except Exception as e:                       # noqa: BLE001
        _err("place_snapshot", e)
        return None


# ---------------------------------------------------------------- L3: 3D export in a child process
GLB_MIN_S, GLB_KEEP, GLB_TIMEOUT_S = 60.0, 6, 60.0


class Exporter:
    """GLB export of leading, accepted and gate candidates. One child at a time, in its own process group at nice 19; the
    placer only calls request() and poll() and never waits (close() at stage end may wait for an accepted / gate export).
    Leaders: the newest waiting one wins, start-to-start interval max([live] glb_min_s, 5 x the last measured time)."""

    def __init__(self, cons=None, cons_path=None, cli=None, clock=time.monotonic, timeout=None, stage=None):
        lv = (cons or {}).get("live") or {}
        self.W, self.stage, self.clock = (work() if isinstance((cons or {}).get("live"), dict) else ""), stage, clock
        self.cli = cli or lv.get("kicad_cli") or "kicad-cli"
        self.min_s, self.keep, self._timeout = float(lv.get("glb_min_s", GLB_MIN_S)), int(lv.get("glb_keep", GLB_KEEP)), timeout
        mb = lv.get("model_base")
        self.kiprjmod = os.path.abspath(os.path.join(os.path.dirname(os.fspath(cons_path)), mb)) if mb and cons_path else (mb or None)
        self.run, self.wait, self.dead, self.checked = None, [], False, False
        self.last_start, self.last_secs, self.leaders = -1e9, 0.0, []

    def timeout(self):
        return self._timeout if self._timeout is not None else max(GLB_TIMEOUT_S, 4 * self.last_secs)

    def request(self, cand, why, board, moves=None, snapshot=None):
        """Queue an export of `board` (with `moves` = {ref: (x, y, angle)} applied in the child). why = leader / accepted / gate.
        -> True when queued. Never raises, never waits."""
        try:
            if not self.W or self.dead:
                return False
            if not self.checked:
                self.checked = True
                if shutil.which(self.cli) is None:
                    self.dead = True
                    _emit("glb_failed", stage=self.stage, cand=cand, why=f"{self.cli} not found: no 3D export in this run", text=f"3D export off: {self.cli} not found")
                    return False
            from . import critroute as CR
            name = CR._safe(str(cand))
            if why == "leader":
                self.wait = [j for j in self.wait if j["why"] != "leader"]          # latest wins
            self.wait.append({"cand": str(cand), "name": name, "why": why, "base": os.fspath(board), "snapshot": snapshot,
                              "moves": {r: list(v) for r, v in (moves or {}).items()}})
            self.poll()
            return True
        except Exception as e:                   # noqa: BLE001
            _err("glb request", e)
            return False

    def poll(self):
        """Look at the child (finished: glb_ready / glb_failed; over the timeout: the group is killed) and start the next job."""
        try:
            now = self.clock()
            if self.run:
                p = self.run["p"]
                if p.poll() is None:
                    if now - self.run["t0"] <= self.timeout():
                        return
                    self._kill(f"timeout after {self.timeout():g} s")
                else:
                    self._done()
            nxt = next((j for j in self.wait if j["why"] != "leader"), None)
            if nxt is None and self.wait and now - self.last_start >= max(self.min_s, 5 * self.last_secs):
                nxt = self.wait[0]
            if nxt is not None:
                self.wait.remove(nxt)
                self._start(nxt, now)
        except Exception as e:                   # noqa: BLE001
            _err("glb poll", e)

    def _start(self, j, now):
        from . import procs
        d = os.path.join(self.W, "live", "glb")
        os.makedirs(d, exist_ok=True)
        stem = os.path.join(d, j["name"])
        job = dict(j, cli=self.cli, kiprjmod=self.kiprjmod, out=stem + ".glb", tmp=stem + ".glb.tmp", log=stem + ".log",
                   board=stem + ".cand.kicad_pcb", result=stem + ".result.json")
        _write(stem + ".job.json", job)
        for f in (job["result"], job["tmp"]):
            if os.path.exists(f):
                os.remove(f)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = procs.popen(["nice", "-n", "19", "/usr/bin/python3", "-m", "fluxplace.liveview", "--glb-job", stem + ".job.json"], cwd=root,
                        stdin=open(os.devnull), stdout=open(os.devnull, "w"), stderr=open(os.devnull, "w"))
        self.run, self.last_start = dict(job, p=p, t0=now), now

    def _kill(self, why):
        from . import procs
        r, self.run = self.run, None
        procs.kill_group(r["p"].pid, grace=1.0, proc=r["p"])
        procs.forget(r["p"])
        for f in (r["tmp"], r["board"], os.path.splitext(r["board"])[0] + ".kicad_prl", r["out"][:-4] + ".job.json"):
            if os.path.exists(f):
                os.remove(f)
        _emit("glb_failed", stage=self.stage, cand=r["cand"], why=why, text=f"3D export of {r['cand']} failed: {why}")

    def _done(self):
        from . import procs
        r, self.run = self.run, None
        procs.forget(r["p"])
        try:
            with open(r["result"]) as fh:
                res = json.load(fh)
        except (OSError, ValueError):
            res = {"ok": False, "why": f"export process exit {r['p'].returncode}, no result"}
        for f in (r["result"], r["out"][:-4] + ".job.json"):
            if os.path.exists(f):
                os.remove(f)
        if not res.get("ok"):
            _emit("glb_failed", stage=self.stage, cand=r["cand"], why=str(res.get("why")), text=f"3D export of {r['cand']} failed: {res.get('why')}")
            return
        self.last_secs = float(res["secs"])
        line = {"path": f"live/glb/{r['name']}.glb", "cand": r["cand"], "why": r["why"], "snapshot": r["snapshot"], "secs": res["secs"],
                "bytes": res["bytes"], "models": res["models"], "missing": res["missing"],
                "text": f"3D model of {r['cand']} ({r['why']}): {res['bytes'] / 1e6:.1f} MB in {res['secs']:g} s, {res['missing']} model(s) missing"}
        _write(os.path.join(self.W, "live", "glb", "latest.json"), dict(line, kind="glb_ready", ts=round(time.time(), 3)))
        _emit("glb_ready", stage=self.stage, **line)
        if r["why"] == "leader":
            self.leaders = [n for n in self.leaders if n != r["name"]] + [r["name"]]
            while len(self.leaders) > self.keep:
                old = os.path.join(self.W, "live", "glb", self.leaders.pop(0))
                for f in (old + ".glb", old + ".log"):
                    if os.path.exists(f):
                        os.remove(f)

    def close(self, sleep=time.sleep):
        """Stage end (call after the result board and report are written): waiting and running leader exports are dropped;
        an accepted or gate export is waited for, each up to the timeout. Nothing of this exporter outlives the call."""
        try:
            self.wait = [j for j in self.wait if j["why"] != "leader"]
            if self.run and self.run["why"] == "leader":
                self._kill("stage ended")
            while self.run or self.wait:
                self.poll()
                if self.run:
                    sleep(0.1)
        except Exception as e:                   # noqa: BLE001
            _err("glb close", e)
            if self.run:
                try:
                    self._kill("stage ended")
                except Exception:                # noqa: BLE001
                    pass


def _glb_child(jobfile):
    """The export process: candidate board (base + moves), kicad-cli, result file. Runs inside the exporter's process group."""
    import subprocess
    from . import boardwrite as BW
    with open(jobfile) as fh:
        job = json.load(fh)
    t0, res = time.time(), {"ok": False}
    try:
        text = open(job["base"], errors="surrogateescape").read()
        if job.get("moves"):
            text = BW.apply_moves(text, {r: tuple(v) for r, v in job["moves"].items()})[0]
        with open(job["board"], "w", errors="surrogateescape") as fh:
            fh.write(text)
        cmd = [job["cli"], "pcb", "export", "glb", "-f", "--subst-models", "--no-dnp"]
        cmd += (["-D", "KIPRJMOD=" + job["kiprjmod"]] if job.get("kiprjmod") else []) + ["-o", job["tmp"], job["board"]]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        log = (r.stdout or "") + (r.stderr or "")
        with open(job["log"], "w") as fh:
            fh.write(" ".join(cmd) + "\n" + log)
        missing = sum(1 for line in log.splitlines() if "File not found" in line)
        if r.returncode == 0 and os.path.exists(job["tmp"]) and os.path.getsize(job["tmp"]) > 0:
            nbytes = os.path.getsize(job["tmp"])
            os.replace(job["tmp"], job["out"])
            res = {"ok": True, "secs": round(time.time() - t0, 1), "bytes": nbytes, "models": max(0, text.count("(model ") - missing), "missing": missing}
        else:
            res["why"] = f"{os.path.basename(job['cli'])} exit {r.returncode}"
    except Exception as e:                       # noqa: BLE001
        res["why"] = f"{type(e).__name__}: {e}"[:300]
    for f in (job["board"], os.path.splitext(job["board"])[0] + ".kicad_prl", job["tmp"]):
        if os.path.exists(f):
            os.remove(f)
    _write(job["result"], res)
    return 0 if res["ok"] else 1


# ---------------------------------------------------------------- L4: owner feedback items (schema v1)
ITEM_MAX_BYTES, TEXT_MAX = 16_000, 400
KINDS = ("note", "pin", "region", "near", "away", "gate", "answer")
ITEM_KEYS = {"v", "id", "ts", "author", "kind", "text", "target", "pin", "region", "other", "dist_mm", "gate", "cand", "snapshot", "scope", "answer"}
SUB_KEYS = {"target": {"part", "module", "group"}, "pin": {"at", "rot", "side"}, "region": {"cells", "rect", "keep"},
            "other": {"part", "module", "cell", "edge"}, "gate": {"id", "action", "choice"}, "answer": {"id", "choice"}}
EDGES = ("N", "E", "S", "W")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _named(d, ctx, what):
    """Reason when the one name in a target / other dict is unknown, else None."""
    if not isinstance(d, dict) or len(d) != 1:
        return f"{what} needs exactly one of {', '.join(sorted(SUB_KEYS[what]))}"
    (k, v), = d.items()
    if k == "edge":
        return None if v in EDGES else f"{what}.edge must be one of N, E, S, W"
    if k == "cell":
        return None if cell_rect(ctx["grid"], v) else f"unknown cell {v}"
    return None if v in ctx[k + "s"] else f"unknown {k} {v}"


def check_item(a, ctx):
    """Reason an item is refused (whole), or None. ctx = {parts, modules, groups (name sets), grid, bbox, gates {id: [cands]},
    seen (ids)}."""
    if not isinstance(a, dict):
        return "not an object"
    if a.get("v") != 1:
        return "v is not 1"
    bad = sorted(set(a) - ITEM_KEYS)
    if bad:
        return f"unknown key {bad[0]}"
    for k in ("id", "ts", "author", "kind"):
        if k not in a:
            return f"missing key {k}"
    kind = a["kind"]
    if kind not in KINDS:
        return f"unknown kind {kind}"
    if a["id"] in ctx.get("seen", ()):
        return f"duplicate id {a['id']}"
    if not isinstance(a.get("text", ""), str) or len(a.get("text", "")) > TEXT_MAX:
        return f"text over {TEXT_MAX} characters"
    if a.get("scope", "run") not in ("run", "board"):
        return "scope must be run or board"
    for k, allowed in SUB_KEYS.items():
        if k in a and (not isinstance(a[k], dict) or set(a[k]) - allowed):
            return f"unknown key in {k}"
    if "dist_mm" in a and not (_num(a["dist_mm"]) and a["dist_mm"] > 0):
        return "dist_mm must be over 0"
    if "target" in a:
        why = _named(a["target"], ctx, "target")
        if why:
            return why
    x0, y0, x1, y1 = ctx["bbox"]
    if kind == "answer":
        an = a.get("answer")
        if not isinstance(an, dict) or set(an) - {"id", "choice"} or not all(isinstance(an.get(k), str) and 0 < len(an[k]) <= 80 for k in ("id", "choice")):
            return "answer.id and answer.choice are non-empty strings of at most 80 characters"
        asked = (ctx.get("asked") or {}).get(an["id"])
        return None if asked and an["choice"] in asked[1] else "unknown question or choice"
    if kind == "note":
        return None if a.get("text") else "missing key text"
    if kind == "pin":
        if "target" not in a or "group" in a["target"]:
            return "pin needs target.part or target.module"
        pin = a.get("pin") or {}
        if "at" in pin and "module" in a["target"]:
            return "pin.at needs target.part (a module is pinned where it stands)"
        if "at" in pin:
            at = pin["at"]
            if not (isinstance(at, list) and len(at) == 2 and all(_num(v) for v in at)):
                return "pin.at must be [x_mm, y_mm]"
            if not (x0 <= at[0] <= x1 and y0 <= at[1] <= y1):
                return "pin.at is off the board"
        elif not ("cand" in a and "snapshot" in a):
            return "pin needs pin.at, or cand and snapshot"
        if "rot" in pin and not _num(pin["rot"]):
            return "pin.rot must be a number"
        return None if pin.get("side", "top") in ("top", "bottom") else "pin.side must be top or bottom"
    if kind == "region":
        reg = a.get("region") or {}
        if ("cells" in reg) == ("rect" in reg):
            return "region needs cells or rect"
        if reg.get("keep") not in ("in", "out"):
            return "region.keep must be in or out"
        if "cells" in reg and not cell_rect(ctx["grid"], reg["cells"]):
            return f"unknown cell {reg['cells']}"
        if "rect" in reg:
            r = reg["rect"]
            if not (isinstance(r, list) and len(r) == 4 and all(_num(v) for v in r) and r[0] < r[2] and r[1] < r[3]):
                return "region.rect must be [x0, y0, x1, y1]"
            if not (x0 <= r[0] and r[2] <= x1 and y0 <= r[1] and r[3] <= y1):
                return "region.rect is off the board"
        return "region keep in needs a target" if reg["keep"] == "in" and "target" not in a else None
    if kind in ("near", "away"):
        if "target" not in a or "other" not in a:
            return f"{kind} needs target and other"
        return _named(a["other"], ctx, "other")
    g = a.get("gate") or {}                      # kind == "gate"
    if g.get("id") not in ctx.get("gates", {}):
        return f"{g.get('id')} is not an open gate"
    if g.get("action") not in ("approve", "reject"):
        return "gate.action must be approve or reject"
    cands = ctx["gates"][g["id"]]
    if "choice" in g and g["choice"] not in cands:
        return f"choice {g['choice']} is not a candidate of the gate"
    if g["action"] == "approve" and len(cands) > 1 and "choice" not in g:
        return "gate.choice is needed: the gate has more than one candidate"
    return "a reject needs text" if g["action"] == "reject" and not a.get("text") else None


def asked_questions(W):
    """{question id: (board path, {choice keys})} as last asked in this workdir: the stage questions files (`*.questions.json`) and the
    crowding options of the floorplan report (`*.floorplan.json`, history[-1].crowding.options plus keep)."""
    import glob
    out = {}
    for p in sorted(glob.glob(os.path.join(W, "*.questions.json"))):
        board = p[:-len(".questions.json")] + ".kicad_pcb"
        try:
            qs = json.load(open(p, encoding="utf-8")).get("questions") or []
        except (OSError, ValueError, AttributeError):
            continue
        for q in qs:
            out.setdefault(q["id"], (board, {o["choice"] for o in q.get("options") or []}))
    for p in sorted(glob.glob(os.path.join(W, "*.floorplan.json"))):
        try:
            cr = json.load(open(p, encoding="utf-8"))["history"][-1].get("crowding") or {}
        except (OSError, ValueError, KeyError, IndexError, AttributeError, TypeError):
            continue
        if cr.get("options"):
            out["crowding"] = (p[:-len(".floorplan.json")] + ".kicad_pcb", {o["key"] for o in cr["options"]} | {"keep"})
    return out


def load_feedback(path, ctx):
    """(item, None) or (None, reason) for one inbox file. Never raises. A `.tmp` file is (None, None): not read."""
    try:
        if str(path).endswith(".tmp"):
            return None, None
        if os.path.getsize(path) > ITEM_MAX_BYTES:
            return None, f"file over {ITEM_MAX_BYTES} bytes"
        try:
            with open(path, encoding="utf-8") as fh:
                a = json.load(fh)
        except (OSError, ValueError) as e:
            return None, f"unreadable or not JSON ({type(e).__name__})"
        why = check_item(a, ctx)
        return (None, why) if why else (a, None)
    except Exception as e:                       # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"[:200]


# ---------------------------------------------------------------- L5: items to rows, the log, the boundary
MIN_REGION_AREA = 1.5              # a keep-in region under this factor of its target's courtyard area is refused
HARD_PIN_REASONS = ("mechanical", "enclosure", "module-mount")


class Feedback:
    """The owner inbox of one stage. boundary() is called at generation / round ends and before a gate; it never raises.
    Rows live for the run in W/feedback/rows.json; the constraints file is never edited (merged() returns a copy to run with).
    No W/feedback/inbox folder: on() is False and nothing is read or written."""

    def __init__(self, B, M=None, cons=None, stage="", clock=time.time, apply=True):
        from . import fixedreason as FR, modules as MD
        self.W, self.stage, self.clock, self.B, self.cons0 = work(), stage, clock, B, cons or {}
        self.apply = apply                       # False: the rows are read and shown, the search of this stage is not steered by them
        self.dir = os.path.join(self.W, "feedback") if self.W else ""
        self.rows, self.rev, self.notes, self.items, self.open_gates, self.answers = [], 0, [], {}, {}, {}
        mods = {m["name"]: sorted(m["members"]) for m in (M or {}).get("modules") or []}
        groups = {k: list(g.get("refs") or []) for k, g in ((self.cons0.get("placement") or {}).get("group") or {}).items() if isinstance(g, dict)}
        why = FR.declared(self.cons0)
        self.members = {"part": {r: [r] for r in B["parts"]}, "module": mods, "group": groups}
        self.hard_fixed = {r for r in MD.fixed_set(B, self.cons0) if MD.MECH.match(r) or (why.get(r) or {}).get("reason") in HARD_PIN_REASONS}
        self.ctx = {"parts": set(B["parts"]), "modules": set(mods), "groups": set(groups), "grid": grid_of(B["bbox"]), "bbox": B["bbox"],
                    "gates": {}, "seen": set(), "asked": {}}
        self.replay = {}                         # item file name -> (stage, generation) it took effect at in the stored log
        if self.on():
            for line in self._log_lines():
                if line.get("action") == "applied" and line.get("file"):
                    self.replay[line["file"]] = (line.get("stage"), line.get("generation"))

    def on(self):
        return bool(self.dir) and os.path.isdir(os.path.join(self.dir, "inbox"))

    def _log_lines(self):
        try:
            with open(os.path.join(self.dir, "log.jsonl")) as fh:
                return [json.loads(x) for x in fh if x.strip()]
        except (OSError, ValueError):
            return []

    def _log(self, item, action, generation=None, row=None, why=None, effect=None, file=None):
        line = {"ts": round(self.clock(), 3), "id": item.get("id"), "kind": item.get("kind"), "action": action, "stage": self.stage,
                "generation": generation, "rows_rev": self.rev, "row": row, "why": why, "effect": effect}
        self.items[item.get("id") or file] = dict(line, file=file)
        with open(os.path.join(self.dir, "log.jsonl"), "a") as fh:
            fh.write(json.dumps(dict(line, file=file), default=str) + "\n")
        ev = {k: v for k, v in line.items() if k not in ("kind", "stage", "ts")}          # `kind` is the event kind on the stream
        _emit("feedback_result", stage=self.stage, item_kind=item.get("kind"),
              text=f"feedback {item.get('id')} ({item.get('kind')}): {action}" + (f" - {why}" if why else ""), **ev)

    def _refs(self, d):
        (k, v), = d.items()
        return list(self.members[k][v]) if k in self.members else []

    def row_of(self, a):
        """The row an item becomes, or None (note, gate). Shapes: fixed {refs, at, rot, side}; region {rect, keep, refs};
        near / away {a, b, dist_mm or None (soft)}; every row carries id, kind, hard, src = "owner", text, scope."""
        k = a["kind"]
        base = {"id": a["id"], "kind": k, "src": "owner", "text": a.get("text", ""), "scope": a.get("scope", "run")}
        if k == "pin":
            pin = a.get("pin") or {}
            refs = self._refs(a["target"])
            held = {r: list(self.B["parts"][r]["pos"]) for r in refs if r in self.B["parts"]}
            return dict(base, kind="fixed", hard=True, refs=refs, at=pin.get("at"), rot=pin.get("rot"), side=pin.get("side"),
                        cand=a.get("cand"), snapshot=a.get("snapshot"), held=held)
        if k == "region":
            reg = a["region"]
            rect = list(reg["rect"]) if "rect" in reg else list(cell_rect(self.ctx["grid"], reg["cells"]))
            return dict(base, hard=True, rect=rect, keep=reg["keep"], refs=self._refs(a["target"]) if "target" in a else [])
        if k in ("near", "away"):
            (ok, ov), = a["other"].items()
            b = {"edge": ov} if ok == "edge" else ({"rect": list(cell_rect(self.ctx["grid"], ov))} if ok == "cell" else {"refs": self._refs(a["other"])})
            return dict(base, hard="dist_mm" in a, a=self._refs(a["target"]), b=b, dist_mm=a.get("dist_mm"))
        return None

    def conflict(self, row):
        """The reason a row contradicts a row already in force (or the board), naming the other row; None when it fits."""
        parts = self.B["parts"]
        if row["kind"] == "fixed":
            held = sorted(set(row["refs"]) & self.hard_fixed)
            if held:
                return f"{held[0]} is fixed for a mechanical reason: change the constraints, not the live run"
            if row["at"]:
                for o in self.rows + [{"id": r["name"], "kind": "region", "keep": r.get("keep", "out"), "refs": r.get("refs") or [], "rect": r.get("rect")}
                                      for r in ((self.cons0.get("intent") or {}).get("region") or []) if r.get("rect")]:
                    if o["kind"] == "region" and o["keep"] == "out" and not o["refs"] and o["rect"][0] <= row["at"][0] <= o["rect"][2] \
                            and o["rect"][1] <= row["at"][1] <= o["rect"][3]:
                        return f"the pin is inside keep-out {o['id']}"
        if row["kind"] == "region" and row["keep"] == "in":
            area = sum((c[2] - c[0]) * (c[3] - c[1]) for c in (parts[r]["court"] for r in row["refs"] if r in parts))
            if (row["rect"][2] - row["rect"][0]) * (row["rect"][3] - row["rect"][1]) < MIN_REGION_AREA * area:
                return f"the region is under {MIN_REGION_AREA:g} x the courtyard area of its target ({area:.1f} mm2)"
        if row["kind"] in ("near", "away") and row["dist_mm"] is not None:
            for o in self.rows:
                if o["kind"] in ("near", "away") and o["kind"] != row["kind"] and o["dist_mm"] is not None and o["a"] == row["a"] and o["b"] == row["b"]:
                    near, away = (row, o) if row["kind"] == "near" else (o, row)
                    if near["dist_mm"] < away["dist_mm"]:
                        return f"near {near['dist_mm']:g} mm is under away {away['dist_mm']:g} mm of row {o['id']}"
        return None

    def _answer(self, a, path, name, stored, generation):
        """an owner answer to an open question: recorded in the board's map now, so the NEXT stage start asks nothing and applies it.
        Nothing of the running stage changes."""
        qid, choice = a["answer"]["id"], a["answer"]["choice"]
        if not stored:
            os.replace(path, os.path.join(self.dir, "applied", name))
        from . import questions as QS
        QS.record_answer(self.ctx["asked"][qid][0], qid, choice)
        self._log(a, "applied", generation, effect=f"answer {qid} = {choice} recorded in the map", file=name)
        print(f"Owner answered {qid} = {choice} (applies from the next stage start)", flush=True)

    def boundary(self, generation=None):
        """Read the inbox: refuse, log, turn into rows. On a re-run the items of applied/ take effect at their recorded
        boundary. -> the rows that came into force at this call (rows_rev rose when the list is not empty)."""
        new = []
        try:
            if not self.on():
                return new
            for sub in ("applied", "refused", "gates"):
                os.makedirs(os.path.join(self.dir, sub), exist_ok=True)
            self.ctx["gates"] = {g: list(c) for g, c in self.open_gates.items()}
            self.ctx["asked"] = asked_questions(self.W)
            todo = [(os.path.join(self.dir, "applied", n), n, True) for n in sorted(os.listdir(os.path.join(self.dir, "applied")))
                    if self.replay.get(n) == (self.stage, generation)]
            todo += [(os.path.join(self.dir, "inbox", n), n, False) for n in sorted(os.listdir(os.path.join(self.dir, "inbox"))) if n.endswith(".json")]
            for path, name, stored in todo:
                a, why = load_feedback(path, self.ctx)
                if a is None:
                    if why is not None and not stored:
                        os.replace(path, os.path.join(self.dir, "refused", name))
                        with open(os.path.join(self.dir, "refused", name + ".reason.txt"), "w") as fh:
                            fh.write(why + "\n")
                        self._log({"id": name, "kind": None}, "refused", generation, why=why, file=name)
                    continue
                self.ctx["seen"].add(a["id"])
                if not stored:
                    self._log(a, "received", generation, file=name)
                if a["kind"] == "answer":
                    self._answer(a, path, name, stored, generation)
                    continue
                row = self.row_of(a)
                why = self.conflict(row) if row else None
                if why:
                    if not stored:
                        os.replace(path, os.path.join(self.dir, "refused", name))
                        with open(os.path.join(self.dir, "refused", name + ".reason.txt"), "w") as fh:
                            fh.write(why + "\n")
                    self._log(a, "conflict", generation, row=row, why=why, file=name)
                    continue
                if not stored:
                    os.replace(path, os.path.join(self.dir, "applied", name))
                if a["kind"] == "gate":
                    self.answers[a["gate"]["id"]] = a
                    continue
                if row is None or not row["hard"]:
                    self.notes.append(row or {"id": a["id"], "kind": "note", "text": a["text"], "target": a.get("target"), "cand": a.get("cand")})
                    self._log(a, "context", generation, row=row, file=name)
                    continue
                if not self.apply:
                    self.notes.append(row)
                    self._log(a, "context", generation, row=row, why=f"read after the search of {self.stage}: not applied in this run", file=name)
                    continue
                self.rows.append(row)
                self.rev += 1
                new.append(row)
                self._log(a, "applied", generation, row=row, file=name)
            if new:
                _write(os.path.join(self.dir, "rows.json"), {"rows_rev": self.rev, "rows": self.rows})
        except Exception as e:                   # noqa: BLE001
            _err("feedback boundary", e)
        return new

    def merged(self, cons):
        """The constraints to run with: a copy with the hard rows added as the owner tools that already exist ([lock.at] +
        [placement.fixed_reason] reason owner, [[intent.region]], [[intent.near]], [[intent.away]]). No rows: `cons` itself."""
        if not self.rows:
            return cons
        import copy
        c = copy.deepcopy(cons)
        it = c.setdefault("intent", {})
        for r in self.rows:
            if r["kind"] == "fixed" and r["at"]:
                for ref in r["refs"][:1]:
                    side = {"top": "F.Cu", "bottom": "B.Cu"}.get(r["side"]) or (self.B["parts"][ref]["side"] if ref in self.B["parts"] else "F.Cu")
                    c.setdefault("lock", {}).setdefault("at", {})[ref] = [r["at"][0], r["at"][1], side]
            if r["kind"] == "fixed":
                pl = c.setdefault("placement", {})
                pl["fixed"] = sorted(set(pl.get("fixed") or []) | set(r["refs"]))
                for ref in r["refs"]:
                    pl.setdefault("fixed_reason", {})[ref] = {"reason": "owner", "decision": f"live feedback {r['id']}", "note": r["text"]}
            elif r["kind"] == "region":
                it.setdefault("region", []).append({"name": f"owner-{r['id']}", "rect": r["rect"], "keep": r["keep"], "side": "both", "refs": r["refs"], "hard": True})
            elif r["kind"] == "near" and "refs" in r["b"]:
                it.setdefault("near", []).append({"refs": r["a"], "to": r["b"]["refs"][0], "max_mm": r["dist_mm"], "hard": True})
            elif r["kind"] == "away" and "refs" in r["b"]:
                it.setdefault("away", []).append({"a": r["a"], "b": r["b"]["refs"], "min_mm": r["dist_mm"], "hard": True})
        return c

    def pin_moves(self):
        """{ref: (x, y, angle)} for the pin rows with coordinates: applied to the board before the search of the next boundary."""
        out = {}
        for r in self.rows:
            if r["kind"] == "fixed" and r["at"] and r["refs"] and r["refs"][0] in self.B["parts"]:
                p = self.B["parts"][r["refs"][0]]
                out[r["refs"][0]] = (float(r["at"][0]), float(r["at"][1]), float(r["rot"]) if r["rot"] is not None else p["angle"])
        return out

    def measure(self, row, B):
        """(met, value) of a hard row on the board model B."""
        from . import geom as GM
        parts = B["parts"]
        cs = [parts[r]["court"] for r in row.get("refs", row.get("a", [])) if r in parts]
        if row["kind"] == "fixed":
            if not row["at"]:                    # a pin without coordinates holds the part where it stood at the boundary
                held = row.get("held") or {}
                d = max((math.hypot(parts[r]["pos"][0] - held[r][0], parts[r]["pos"][1] - held[r][1]) for r in held if r in parts), default=0.0)
                return d <= 0.011, round(d, 3)
            p = parts[row["refs"][0]]
            d = math.hypot(p["pos"][0] - row["at"][0], p["pos"][1] - row["at"][1])
            return d <= 0.011, round(d, 3)
        if row["kind"] == "region":
            if row["keep"] == "in":
                bad = [c for c in cs if not GM.inside(c, row["rect"])]
            else:
                cs = cs or [p["court"] for p in parts.values()]
                bad = [c for c in cs if GM.rect_gap(c, row["rect"]) == 0 and min(c[2], row["rect"][2]) - max(c[0], row["rect"][0]) > 1e-6
                       and min(c[3], row["rect"][3]) - max(c[1], row["rect"][1]) > 1e-6]
            return not bad, len(bad)
        x0, y0, x1, y1 = B["bbox"]
        b = row["b"]
        if "edge" in b:
            e = b["edge"]
            gaps = [{"N": c[1] - y0, "S": y1 - c[3], "W": c[0] - x0, "E": x1 - c[2]}[e] for c in cs]
        else:
            bs = [b["rect"]] if "rect" in b else [parts[r]["court"] for r in b["refs"] if r in parts]
            gaps = [GM.rect_gap(c, o) for c in cs for o in bs]
        if not gaps:
            return True, None
        v = round(max(gaps) if row["kind"] == "near" else min(gaps), 3)
        return (v <= row["dist_mm"]) if row["kind"] == "near" else (v >= row["dist_mm"]), v

    def finish(self, B):
        """Stage end: one honoured / not_honoured line per hard row (measured on the accepted board model B) and the item
        table for the stage report. -> the table as text lines; [] when no item was seen."""
        try:
            if not self.on():
                return []
            for name in sorted(os.listdir(os.path.join(self.dir, "inbox"))):   # arrived during the search: the file stays for the next stage start
                if name.endswith(".json"):
                    try:
                        iid = json.load(open(os.path.join(self.dir, "inbox", name), encoding="utf-8")).get("id") or name
                    except (OSError, ValueError, AttributeError):
                        iid = name
                    self._log({"id": iid, "kind": None}, "received", why="arrived during the search: not read by this stage", file=name)
            for r in self.rows:
                ok, v = self.measure(r, B)
                self._log({"id": r["id"], "kind": r["kind"]}, "honoured" if ok else "not_honoured", row=r, effect=f"measured {v}")
            if not self.items:
                return []
            L = ["| Item | Kind | Last action | Why / effect |", "|---|---|---|---|"]
            L += [f"| {k} | {v['kind']} | {v['action']} | {v.get('why') or v.get('effect') or ''} |" for k, v in sorted(self.items.items(), key=lambda kv: str(kv[0]))]
            board = [r for r in self.rows if r["scope"] == "board"]
            if board:
                L += ["", "Rows the owner wants kept (scope board): add them to the constraints with a DECISIONS row."] + [f"- {json.dumps(r, default=str)}" for r in board]
            return L
        except Exception as e:                   # noqa: BLE001
            _err("feedback finish", e)
            return []


# ---------------------------------------------------------------- L6: gates that wait
GATE_POLL_S = 2.0
EXIT_NEEDS_OWNER = 5


def _ai_gate(gid, machine, g, default, cons, log_dir):
    """w94: the gate settled by the AI decider.  Machine verdict: proceed / stop; none: a choice among the candidates (approve).
    Never exits 5: a "stop" is recorded, the stage's own verdict decides the run."""
    from . import decider as DC
    cands = [c["cand"] for c in g["candidates"]]
    ev = [f"machine verdict: {machine}"] + [f"candidate {c['cand']}: {json.dumps(c.get('criteria'), default=str)[:200]}" for c in g["candidates"][:6]]
    if machine is None and cands:
        opts = {c: f"approve candidate {c}" for c in cands}
        dflt = cands[0]
    else:
        opts = {"proceed": "the stage result is used", "stop": "the stage result is set aside (recorded)"}
        dflt = default if default in opts else "proceed"
    d = DC.ask(f"gate:{gid}", "live gate", f"gate {gid}: machine {machine}, {len(cands)} candidate(s)", opts, dflt,
               "the candidates' criteria against the board's rules", ev, cons, log_dir)
    if machine is None and cands:
        return {"by": "ai" if d["reasoned"] else "default", "action": "approve", "choice": d["choice"], "text": d["rationale"], "reasoned": d["reasoned"]}
    return {"by": "ai" if d["reasoned"] else "default", "action": d["choice"], "text": d["rationale"], "reasoned": d["reasoned"]}


def gate(gid, machine, candidates, fb, cons, exporter=None, clock=time.time, sleep=time.sleep, stage=None):
    """Open gate `gid` for the owner and wait as `[live.gate.<gid>]` says. machine = "PASS", "FAIL" or None (no machine
    verdict: a choice among candidates); candidates = [{cand, key, criteria, snapshot, board}], rank 1 first.
    -> {passed, action, choice, by, text, wait_s, exit}. The machine side is never overridden: passed is False on a machine
    FAIL whatever the owner says. No `[live.gate.<gid>]` or no feedback folder: no file, no wait, passed = machine != FAIL."""
    out = {"passed": machine != "FAIL", "action": None, "choice": (candidates[0]["cand"] if candidates else None), "by": None, "text": "",
           "wait_s": 0.0, "exit": 0}
    try:
        conf = (((cons or {}).get("live") or {}).get("gate") or {}).get(gid)
        if not isinstance(conf, dict) or fb is None or not fb.on():
            return out
        os.makedirs(os.path.join(fb.dir, "gates"), exist_ok=True)
        path = os.path.join(fb.dir, "gates", f"{gid}.json")
        wait_s = 60.0 * float(conf.get("wait_min", 0))
        default = conf.get("default") or ("stop" if machine is None else "proceed")
        t0 = clock()
        g = None
        if os.path.exists(path):
            with open(path) as fh:
                old = json.load(fh)
            if not old.get("closed"):
                g = old                          # a run killed in the wait: the original deadline stands
        if g is None:
            g = {"id": gid, "stage": stage or fb.stage, "opened": round(t0, 3), "deadline": round(t0 + wait_s, 3) if wait_s > 0 else None,
                 "default": default, "machine": machine,
                 "candidates": [{"cand": c["cand"], "key": _key(c.get("key")), "criteria": criteria(c.get("criteria")), "snapshot": c.get("snapshot"),
                                 "glb": f"live/glb/{c['cand']}.glb"} for c in candidates]}
            _write(path, g)
            _emit("gate_open", stage=g["stage"], id=gid, deadline=g["deadline"], default=default, machine=machine, candidates=g["candidates"],
                  path=f"feedback/gates/{gid}.json", text=f"gate {gid} open: machine {machine}, " + (f"waits {wait_s / 60:g} min, default {default}" if wait_s > 0 else "not waiting"))
            for c in candidates if exporter is not None else ():
                if c.get("board"):
                    exporter.request(c["cand"], "gate", c["board"], c.get("moves"), c.get("snapshot"))
        fb.open_gates[gid] = [c["cand"] for c in g["candidates"]]
        from . import decider as DC
        ai = DC.enabled(cons)                    # w94: an owner answer already in wins; else the AI decider, no wait
        ans = None
        while True:
            fb.boundary(generation="gate")
            ans = fb.answers.pop(gid, None)
            if exporter is not None:
                exporter.poll()
            if ans is not None or ai or g["deadline"] is None or clock() >= g["deadline"]:
                break
            sleep(GATE_POLL_S)
        fb.open_gates.pop(gid, None)
        out["wait_s"] = round(clock() - t0, 1)
        if ans is not None:
            act = ans["gate"]["action"]
            out.update(by="owner", action=act, text=ans.get("text", ""), choice=ans["gate"].get("choice") or out["choice"])
            out["passed"] = act == "approve" and machine != "FAIL"
            note = "recorded only: the machine side failed" if machine == "FAIL" else ""
        elif ai:
            out.update(_ai_gate(gid, machine, g, default, cons, fb.dir))
            out["passed"] = out["action"] in ("approve", "proceed") and machine != "FAIL"
            note = "AI decider" + ("" if out["reasoned"] else ", default, not reasoned") + (": " + out["text"][:120] if out["text"] else "")
        elif g["deadline"] is None:
            out.update(by=None, action=None)
            note = "not waiting"
        else:
            out.update(by="timeout", action=default)
            out["passed"] = default == "proceed" and machine != "FAIL"
            out["exit"] = EXIT_NEEDS_OWNER if default == "stop" else 0
            note = f"no answer in {wait_s / 60:g} min: default {default}"
        if out["by"]:
            g.update(closed=round(clock(), 3), by=out["by"], action=out["action"], choice=out["choice"], item=(ans or {}).get("id"), gate_wait_s=out["wait_s"])
            _write(path, g)
            _emit("gate_closed", stage=g["stage"], id=gid, by=out["by"], action=out["action"], choice=out["choice"], item=(ans or {}).get("id"),
                  text=f"gate {gid} closed by {out['by']}: {out['action']}" + (f" ({note})" if note else ""))
            fb._log(ans or {"id": gid, "kind": "gate"}, "gate_closed", generation="gate", why=note or None, effect=f"{out['action']}, passed {out['passed']}")
    except Exception as e:                       # noqa: BLE001
        _err("gate", e)
    return out


# ---------------------------------------------------------------- L7: what the stages call
def stage_start(board, cons, spec, stage, workdir, apply=True):
    """The first boundary of a stage -> (board path, constraints to run with, Feedback or None). Stream off or no
    W/feedback/inbox folder: the arguments come back untouched and nothing is loaded. With rows: the constraints are a merged
    copy, and pin rows with coordinates are moved on a copy of the board in `workdir` (the input board is never written)."""
    try:
        W = work()
        if not W or not os.path.isdir(os.path.join(W, "feedback", "inbox")):
            return board, cons, None
        from . import boardwrite as BW, modules as MD, routediag as RD
        B = RD.load(board)
        fb = Feedback(B, MD.build(B, cons, spec, arrange=False), cons, stage, apply=apply)
        fb.boundary(generation=0)
        if not apply:                            # read and shown only: the board and the constraints stay as they came
            return board, cons, fb
        moves = {r: v for r, v in fb.pin_moves().items()
                 if math.hypot(v[0] - B["parts"][r]["pos"][0], v[1] - B["parts"][r]["pos"][1]) > 1e-4 or abs(v[2] - B["parts"][r]["angle"]) > 1e-4}
        if moves:
            os.makedirs(workdir, exist_ok=True)
            out = os.path.join(workdir, "owner-pins.kicad_pcb")
            BW.move_file(board, out, moves, post=False)
            pro = os.path.splitext(board)[0] + ".kicad_pro"
            if os.path.exists(pro):
                shutil.copyfile(pro, os.path.splitext(out)[0] + ".kicad_pro")
            board = out
        return board, fb.merged(cons), fb
    except Exception as e:                       # noqa: BLE001
        _err("stage_start", e)
        return board, cons, None


def candidate(stage, cand, B=None, M=None, key=None, crit=None, cons=None, ex=None, board=None, lane="leader", phase="judge",
              verdict="kept", full=False, **fields):
    """One candidate, shown: an always-on progress line, a snapshot in `lane` (B = a board model or a board path; None =
    no snapshot) and, with an exporter and a board path, a GLB request (leader lane: why leader; accepted lane: accepted).
    Stream off: returns at once. Never raises."""
    try:
        if not work():
            return None
        meta = {"cand": cand, "arm": fields.get("arm"), "generation": fields.get("generation"), "key": key, "criteria": crit,
                "rows_rev": fields.get("rows_rev")}
        progress(stage, phase, always=True, cand=cand, key=key, criteria=crit, verdict=verdict, **fields)
        rel = None
        if B is not None:
            if isinstance(B, (str, os.PathLike)):
                from . import routediag as RD
                B = RD.load(os.fspath(B))
            rel = place_snapshot(B, lane, meta, M, cons, full=full, stage=stage)
        if ex is not None and board and lane in ("leader", "accepted"):
            ex.request(cand, lane, board, snapshot=_snap["seq"] if rel else None)
        return rel
    except Exception as e:                       # noqa: BLE001
        _err("candidate", e)
        return None


def watcher(stage, cand, P, cons=None, names=("cost",)):
    """A callback (i, n, *values) for a search loop over a floorplan2.Plan: a limited progress line and the throttled
    `trial` snapshot of the plan's own board model. None when the stream is off, so the loop pays one `is not None`."""
    if not work():
        return None
    M = {"modules": P.mods, "of": P.of}

    def watch(i, n, *values):
        try:
            key = [[k, (round(float(v), 3) if v is not None else None)] for k, v in zip(names, values)]
            if progress(stage, "anneal", cand=cand, trial=i, trials=n, key=key) is not None:
                place_snapshot(P.B, "trial", {"cand": cand, "key": key}, M, cons, stage=stage)
        except Exception as e:                   # noqa: BLE001
            _err("watch", e)
    return watch


if __name__ == "__main__":                       # python3 -m fluxplace.liveview --glb-job JOB   (the exporter's child, not a command)
    sys.exit(_glb_child(sys.argv[2]) if len(sys.argv) == 3 and sys.argv[1] == "--glb-job" else 2)
