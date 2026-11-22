# EVENTS recipe: live event stream, board snapshots and ETA for the ParkkRouter dashboard

Owner 11-22: a realtime web dashboard (/var/www/html/parkkrouter) must show what fluxplace is doing: stage, progress,
log lines, every placement / routing / tournament iteration, a live board view, artifacts (PNGs, reports) and an ETA.
fluxplace's job is only to WRITE these files; the dashboard tails them. Contract: docs/EVENTS-CONTRACT.md (E0).

Branch `events` (worktree /var/kicad/fluxplace-wt/events, from lookahead 15401a6). Board-agnostic. One commit per item,
`EVENTS-En: ...`. Tests in tests/test_events.py, no pcbnew, no router: `timeout 300 /usr/bin/python3 -m pytest -q
-p no:cacheprovider tests/test_events.py` after every item; also re-run tests/test_pipeline.py at the end. Never run the
pipeline or a router on a real board. Emitting must NEVER raise or slow a stage: every hook is wrapped in try/except and
rate-limited. Read narrowly (grep -n, sed -n ranges).

## E0 contract doc `docs/EVENTS-CONTRACT.md`

Write the schema below as the reference the dashboard codes against (field tables + one example line per kind).

Files, all under the pipeline workdir W (`--workdir`):
- `W/events.jsonl` append-only, one JSON object per line, UTF-8, each line < 64 KiB.
- `W/snap/NNNNNN.json` full board snapshots; `W/snap/NNNNNN.delta.json` deltas; `W/snap/latest.json` = copy of newest
  full snapshot (atomic replace).
- `W/eta.json` newest ETA (atomic replace).
- `W/run.json` run header (atomic replace at start and end): {run, board, flow, stages, features, started, ended, verdict}.

Common fields of every event: `v` (1), `ts` (unix float), `run` (id), `seq` (int, per process, with `pid`), `pid`,
`stage` (or null), `kind`, plus kind fields:

| kind | fields |
|---|---|
| run_start | board, flow, stages[], features{components, nets, pads, layers, area_mm2, density} |
| stage_start | n, name |
| stage_end | n, name, verdict, line, secs, board |
| progress | step (text), pct (0..100 or null), state |
| iteration | loop (place/pairs/route/close/lookahead/ghost/tune), i, n (or null), metrics{} (opens, shorts, overflow, hpwl, score, pairs_complete, ...), note |
| log | level (info/warn/error), text (<= 400 chars), src (router name / module) |
| snapshot | path (relative to W), full (bool), reason, opens |
| parts | placement frame without a file: [[ref, x, y, rot, side]] (placer iterations, <= 2 per second) |
| artifact | type (png/report/board/table), path, label |
| eta | secs_left, done_frac, model, basis |
| run_end | verdict, secs, board |

Snapshot JSON (`full`): {v, seq, ts, board, bbox, layers[], edge[[x,y]...]..., nets{id: name},
parts[{ref, x, y, rot, side, crt[x0,y0,x1,y1], value}], pads[{ref, num, x, y, w, h, rot, shape, layers[], net}],
tracks[{id, l, n, w, a[x,y], b[x,y], m[x,y]|null, lk}], vias[{id, x, y, d, n, lk}], zones[{l, n, pts}] (outline
simplified to <= 200 pts), opens}. `id` = first 12 hex of sha1 of the item's geometry+net (stable across snapshots).
Delta: {v, seq, base (seq of the full it applies to), add{tracks[], vias[]}, del{tracks[ids], vias[ids]}, parts
(changed parts only), opens}. A full snapshot is written every `full_every` (20) deltas and at every stage end.

## E1 `fluxplace/events.py`

- `init(work, run=None)`: sets env `FLUXPLACE_EVENTS=W/events.jsonl` and `FLUXPLACE_RUN` (default
  `time.strftime('%Y%m%d-%H%M%S')` + pid) so every subprocess (pairs, lookahead workers, ghosts) inherits them.
- `emit(kind, stage=None, **fields)`: no-op when the env var is unset; one `os.open(O_APPEND|O_CREAT|O_WRONLY)` write
  per line; truncates long strings; never raises. `stage` defaults to env `FLUXPLACE_STAGE` (set by the pipeline per stage).
- `limited(key, per_s)`: per-process token bucket; `emit_limited(key, per_s, kind, ...)`.
- Tests: env unset no-op; lines parse; truncation; limiter.

## E2 `fluxplace/snapshot.py`

- `full(board_path, out_path, seq)` from `routediag.load` (pure text) per the contract; `delta(prev_full, board_path,
  out_path, seq)` by id sets. `Snapper(work)`: keeps the last full in memory, `take(board_path, reason, force=False)`
  writes delta or full (rule above), updates latest.json, emits `snapshot`; throttled to one per `[events] snap_s` (3 s)
  unless force. Large boards: target < 1.5 s for a 5 MB board (routediag.load is the cost; do not load twice).
- Tests: fixture board text (reuse an existing tests fixture with tracks; grep tests/ for a small .kicad_pcb), full ->
  modify one track -> delta has 1 add 1 del; ids stable.

## E3 hooks (thin, each in try/except)

- pipeline.py run loop (around line 1435): `events.init` at run start + run.json + `run_start` (features from
  routediag: components = parts, nets, pads, copper layers, bbox area, density = pad count / area); per stage set env
  FLUXPLACE_STAGE, `stage_start`; after the stage `stage_end` + forced snapshot of res["board"]; `run_end` at exit.
- routestatus._atomic: when the path ends with status/progress json, also `emit_limited("progress", 2, "progress",
  step=obj.get("current_step"), state=obj.get("state"), pct=obj.get("pct"))`. This covers every stage that already writes
  status (pairs, route, krtchain, lookahead).
- routestatus.run_logged (the line loop that writes the router log): `emit_limited("log", 20, "log", text=line,
  src=extra.get("router"))` per line; and when the router writes its output board (krtchain step end), snapshot it.
- placer_cypress.py / legalise.py iteration loops (grep the loop that logs per iteration): `emit_limited("parts", 2,
  "parts", frame=[[ref, x, y, rot, side]...])` + `iteration(loop="place", i, metrics={hpwl, overlap, ...})`.
- gaps.close round loop: `iteration(loop="close", i, metrics={score, opens, shorts})` + snapshot of the round's board.
- lookahead.py: each ghost result -> `iteration(loop="ghost", ...)`, each candidate result -> `iteration(loop=
  "lookahead", i, metrics=fit)` + `artifact(png)`, TOURNAMENT.md -> `artifact(table)`.
- pairs.py negotiation passes -> `iteration(loop="pairs", i, metrics={complete, total})`.
- Tests: monkeypatched emit collects kinds from a fake stage run (pipeline test style).

## E4 `fluxplace/eta.py`

- `record(features, stage, secs, verdict)` appends to `~/.config/fluxplace/timings.jsonl` at every stage_end.
- `estimate(features, remaining_stages, elapsed_in_stage)`: per stage, median of secs / scale over the last 20 rows of
  that stage, scale = components (place stages), nets x density (route/close/pairs/lookahead), 1 (others); default table
  when no history (ready 5 s, place 600 s, pairs 900 s, lookahead 2400 s, route 3600 s, close 3600 s, review 120 s,
  others 60 s). Emit `eta` and write eta.json after each stage_end and at most every 60 s from the progress hook.
- Tests: estimate with/without history; scaling.

## E5 replay tool for the dashboard developer

- `tests/fixtures/events-sample/`: a tiny synthetic W (events.jsonl with every kind, 2 full + 3 delta snapshots, eta.json,
  run.json, one PNG) produced by `python3 -m fluxplace.events --sample OUT` so the dashboard can be built without a run.
- `python3 -m fluxplace.events --replay W --speed 10 OUT` re-emits an existing W into OUT with original timing / speed
  (for UI development against real runs).

Report back: per item commit hash, test counts, deviations / not done.
