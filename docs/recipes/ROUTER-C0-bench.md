# ROUTER-C0: routing benchmark harness (plan section C, phase C0)

Worktree /var/kicad/fluxplace-wt/router (branch `router`). Plan: `docs/PLAN-W14-FINISH-AND-AUTOMATION.md` section C.
Board-agnostic: no board name, net name or path in code; boards come from the manifest. One-flow rule: no new user
command. The harness is a dev script (`tests/routebench.py`, like `tests/bench.py`). Size: about 400 lines Python + tests.
Interpreter: `/usr/bin/python3` (has pcbnew). Installs: none (KRT in `~/tools/KiCadRoutingTools*` + `~/tools/router-venv`,
Freerouting 2.5.0 jar and JDK in `~/tools`, all found by `routerloc.resolve` / `routefr.locate`).
Tests: one file at a time. **Never launch a router from an agent or from pytest**: routers are stubbed in tests; the real
run is the detached script in step B5, started by the top-level model.
Line numbers are approximate: re-find by name.

## Boards (manifest `~/.config/fluxplace/routebench.toml`, written in B1; not in the repo)

Format = the corpus manifest (`corpus.load_manifest`) plus a `[board.bench]` table.

| name | board | constraints / spec | task | strip | budget_s |
|---|---|---|---|---|---|
| w14-base | /var/kicad/utv-comms-bridge/W14/stageD/pairs-hs.kicad_pcb | /var/kicad/utv-comms-bridge/W14/inputs/constraints.toml, /var/kicad/utv-comms-bridge/W14/inputs/spec.json | whole | no | 2400 |
| w14-f59 | /var/kicad/utv-comms-bridge/W14/stageF/routed.kicad_pcb (59 opens) | same | finish | no | 1800 |
| w14-r5 | /var/kicad/utv-comms-bridge/W14/stageF/hand/r5.kicad_pcb (9 opens) | same | finish | no | 1800 |
| cm5-minima | /usr/share/kicad/demos/cm5_minima/CM5_MINIMA_3.kicad_pcb (public CM5 carrier, 6 layers) | none | whole | yes | 1800 |
| stickhub | /usr/share/kicad/demos/stickhub/StickHub.kicad_pcb (2 layers, USB pairs) | none | whole | yes | 900 |
| interf-u | /home/jratz/tools/KiCadRoutingTools/kicad_files/interf_u_unrouted_placed.kicad_pcb (2 layers, unrouted) | none | whole | no | 600 |

No corpus manifest exists on this machine (`docs/ROUTING-PLAYBOOK.md` line 85), so cm5-minima, stickhub and interf-u are
the "corpus boards" of the plan. Source boards are never written: every run works on a copy in the work dir, with the
`.kicad_pro`, `.kicad_prl` and `.kicad_dru` sidecars copied beside it (`corpus._stage`, ~227, shows the copy pattern).

## Engines and the baseline invocations (how fluxplace already calls them)

Both baselines go through the existing route stage, as `W14/scripts/run-w14-build.sh` line 42 does:

```
/usr/bin/python3 cli.py pipeline <copy> --workdir <work> [--constraints C] [--spec S] --flow constructive \
  --from route --to route --router <krt-whole|freerouting> --route-budget <budget_s> \
  --assume-defaults --allow-incomplete --allow-undocumented --no-api --report-dir <work>/reports
```

| engine row | invocation | result board | wall time |
|---|---|---|---|
| `krt` | `--router krt-whole` (`pipeline.py` ~506 -> `krtchain.run`: `route_planes.py`, `route_diff.py`, `route.py` with `--keep-input-copper`) | `stages.route.board` in `<work>/state.json` (`pipeline.state_path`) | the subprocess wall time |
| `freerouting` | `--router freerouting` (`pipeline.py` ~925 -> `routefr.route_board`, command built by `routefr.command`) | `<work>/<stages.route.dir>/freerouting.kicad_pcb` (Freerouting alone) | seconds from `stages.route.fr` (read `routefr.state_record`, ~842, for the key) |
| `freerouting+krt` | the same run | `stages.route.board` (Freerouting, then KRT on what is left) | the subprocess wall time |
| `kernel` | C2 and later: registered when `fluxplace.kmodel` exposes `route_board`; until then the row has status `n/a` | | |

A run that ends without a result board, or with exit code other than 0/1, gives a row with status `error` and the last
300 characters of the log. A run killed at `budget_s + 120` s gives status `timeout`; the newest board in the stage dir
is still measured if one exists.

## Metrics (all from existing code; measured on the start copy and on each result board)

| key | source |
|---|---|
| `opens`, `shorts`, `errors` | `gaps.measure(board)` (kicad-cli DRC with refill; `drcguard.measure_from_report`) |
| `new_err_keys` | number of `err_keys` in the result that the start does not have (`drcguard.new_keys`) |
| `err_types` | top 5 error `type` counts of the result report |
| `completion` | `1 - opens_end / opens_start` (null when `opens_start` is 0) |
| `pairs` | `{complete, mismatch, half, open}` counts from `pairaudit.summary` via `gaps._pair_audit(board, cons)`; null without constraints |
| `rails` | `{narrow, vias, mag}` = count of gap kinds `power-width`, `power-vias` and `power_mag` from `gaps.classify` + `gaps.score_terms`; null without constraints |
| `score` | `gaps.score(m, gaps)` (the lexicographic vector the flow already uses); null without constraints |
| `vias`, `tracks`, `track_mm` | `corpus._pcb_metrics(board)`; the row also has the deltas against the start |
| `wall_s`, `budget_s`, `rc` | the runner |
| `engine_version` | KRT: `routerloc.resolve()` version + commit; Freerouting: `routefr.jar_version` |
| `tool`, `board_sha` | `corpus.git_sha()`, `corpus.sha256(source board)` |

Read each helper's signature before calling it; do not copy its code.

## Output

`<out>/routebench.json`:

```
{"schema": 1, "ts": "...", "tool": "<git sha>", "kicad": "10.0.5", "cpus": 16,
 "rows": [{"board": "w14-f59", "task": "finish", "engine": "krt", "status": "ok|timeout|error|n/a",
           "start": {"opens": 59, "shorts": 0, "errors": 26, "vias": 0, "tracks": 0, "track_mm": 0.0, "score": [..]},
           "end":   {"opens": 0, "shorts": 0, "errors": 0, "new_err_keys": 0, "err_types": [["clearance", 3]],
                     "vias": 0, "tracks": 0, "track_mm": 0.0, "d_vias": 0, "d_track_mm": 0.0,
                     "pairs": {"complete": 0, "mismatch": 0, "half": 0, "open": 0}, "rails": {"narrow": 0, "vias": 0, "mag": 0.0},
                     "score": [..]},
           "completion": 1.0, "wall_s": 0.0, "budget_s": 1800, "rc": 0, "engine_version": "...",
           "board_sha": "...", "out_board": "<path>", "log": "<path>", "error": null}]}
```

`end` is null for status `error` and `n/a`. One line per row (the row plus `ts` and `tool`) is appended to
`docs/corpus/routebench.jsonl` (committed, like `runs.jsonl`). A markdown table (board, engine, status, opens start -> end,
errors, pairs complete/total, rails narrow, d_vias, d_track_mm, wall_s) goes to stdout and `<out>/routebench.md`.
`<out>/routebench.status.json` is rewritten after every row: `{"state": "running|done", "done": n, "total": n, "current": "board/engine"}`.

`beats(a, b)` (two rows of the same board): true when `a` is `ok` and (`b` is not `ok`, or `a.end.score < b.end.score`
lexicographically, or the scores are equal and `a.wall_s < b.wall_s`). Without scores, compare `(shorts, opens, errors, wall_s)`.
Later phases quote this function in their exit checks.

## Steps (one commit each, message prefix `ROUTER-C0-B<n>:`)

B1 Manifest. In `corpus.load_manifest` (~48) pass the table through: `ent["bench"] = dict(e.get("bench", {}))` (keys
`task`, `strip`, `budget_s`; nothing else changes). Write `~/.config/fluxplace/routebench.toml` with the six rows above
(`tier = ["full"]`). Test (`tests/test_corpus.py`, one new case): a manifest with `[board.bench]` loads, and one without it gives `{}`.

B2 `tests/routebench.py`, part 1 (about 150 lines): argparse (`--manifest`, `--board` repeatable, `--engine` repeatable,
default `krt freerouting`, `--out`, `--work`, `--baseline`), `stage(ent, work)` (copy board + sidecars; when `strip`, remove
all track, arc and via nodes with `boardwrite.strip_nets(text, all net names)`, zones stay), `metrics(board, cons)` (the
table above; every optional part in its own try so one failing helper gives null, not a lost row).
Tests (`tests/test_routebench.py`, boards from `tests/pcbgen.py`): strip leaves 0 segments and vias and the zone count unchanged;
`metrics` with a monkeypatched `gaps.measure` returns every key; without constraints `pairs`, `rails`, `score` are null.

B3 Part 2 (about 150 lines): `ENGINES` dict name -> runner `(copy, work, ent) -> {rc, wall_s, boards: {row name: path}, log, version, wall: {row name: s}}`;
the two pipeline runners above built from one `_pipeline_cmd`; `subprocess.run` with `timeout=budget_s + 120`, output to
`<work>/<board>/<engine>/run.log`; `kernel` runner returns `n/a` unless `fluxplace.kmodel.route_board` exists; `row(...)`
builds the schema; the status file, the JSON, the jsonl append and the markdown table.
Tests (runner replaced by a stub that copies a prepared board): status ok with all keys and correct deltas; a stub that
raises `subprocess.TimeoutExpired` gives status `timeout` with `start` filled; a stub with rc 2 and no board gives `error`;
`kernel` gives `n/a`; the jsonl gets exactly one line per row; `_pipeline_cmd` has the flags above and no `--constraints`
when the entry has none.

B4 Part 3 (about 80 lines): `beats`, `--baseline FILE` (per board and engine: opens, errors, score and wall_s old -> new,
and `REGRESSED` when the old row beats the new one; exit 1 if any row regressed), and a `summary` block per engine in the JSON
(boards ok, boards complete = opens 0 and shorts 0, mean completion, total wall_s). Tests: the three `beats` cases, a
regression gives exit 1, equal rows give exit 0.

B5 `tests/routebench.sh` (detached run, about 15 lines): `nohup /usr/bin/python3 tests/routebench.py --out <dir> --work <dir>/work > <dir>/run.out 2>&1 &`,
first with `--board interf-u` (smoke: both engines produce a row that is not `error`), then all boards. Before starting, check
that no other router is running (`pgrep -af 'route.py|freerouting'`) and report it instead of starting. The agent writes the
script and stops; the top-level model starts it, waits on `routebench.status.json`, and commits `docs/corpus/routebench.jsonl`
plus a ledger row in `docs/ONE-SHOT-LEDGER.md` (each `error` or `timeout` row: cause and fix).

## Done when
- All test files above pass, one at a time.
- `routebench.jsonl` has rows for 6 boards x 3 baseline engine rows, none `error` (a `timeout` is a result, not a failure).
- `docs/commands/` is not touched (no user command was added).
