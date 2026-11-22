# PLACETOUR recipe: placement tournament judged by a real router

Owner 11-22: "implement this into placement first, do as many tournaments as possible to get perfect placement, max ~10;
then move to routing and adapt the same strategy for final routing". Same machinery as LOOKAHEAD (ghost judge, tiered
fitness, candidates per round, PNG per candidate, TOURNAMENT.md, events), with part MOVES instead of pair lays.

Branch `placetour` (worktree /var/kicad/fluxplace-wt/placetour, from lookahead a743388, which has events merged).
New module `fluxplace/placetour.py`; reuse, never copy: `lookahead.lean_krt` (KRT `--quick 2` judge, ~80 s on W15),
`lookahead.render`, `_table`, `_progress`, `_spawn`/`_run_jobs` (one fresh interpreter per candidate),
`relieve` (hard gates, attached copper, allow_move_routed), `planplace.plan_place` (predictor-driven move engine),
`legality.Legal` / `legality.collision_gate`, `intent.check`, `placechecks.*`, `planroute.run` / `rank_key`,
`boardwrite.move_file`, `events` / `snapshot`. Board-agnostic. One commit per item `PLACETOUR-Pn: ...`, tests in
tests/test_placetour.py (no pcbnew, no router: monkeypatch them), `timeout 300 /usr/bin/python3 -m pytest -q
-p no:cacheprovider tests/test_placetour.py` after every item. No real-board runs except the P6 smoke.

## Config

```toml
[placetour]
enabled = false
max_rounds = 10          # owner: max ~10 tournaments
no_gain_rounds = 2
jobs = 4                 # candidates in parallel (each one interpreter; the KRT judge is one process)
judge_top = 4            # tier-1 best N per round get the KRT judge
judge_min = 4            # KRT --quick 2 wall budget per judge (W15 measured 80 s)
cands = 12               # candidates generated per round
hot = 0.5                # route_review congested-area threshold
nudge_mm = [0.5, 1.0, 2.0]
move_routed = true       # parts with attached copper may move; their attached copper is removed (relieve allow_move_routed)
```
Fixed parts: `[placement] fixed` (never moved), parts with LOCKED copper (never moved), `[placement.group.*]` members move
only as a group (skip them in v1: treat as fixed and say so in the report).

## P1 judge + blame

- `judge(a, board, outdir, P)`: `lookahead.lean_krt(a, board, outdir, P["judge_min"])` -> routed copy (or None);
  then `<router_py> -X utf8 <router_dir>/py_tools/route_review.py <routed> --json <outdir>/review.json --top 30` (cwd =
  router_dir; check exit 0). Parse to `blame = {"open": [{net, refs}], "costly": [{net, vias, detour, refs}],
  "hot": [{cells, occ, refs, nets, bbox}], "turned": [{ref, x, y, rot}]}` (read the JSON keys from the tool's own
  output on a fixture; grep route_review.py for json.dump to get the schema). Missing tool or bad exit -> blame from
  `planroute.run` findings only (log it).
- `judge_key(routed_board, blame, cons)`: (opens of signal nets on the judge board, number of hot areas, sum of hot
  occupancy, total vias, sum of detour over costly nets) -> tuple, smaller is better.
- Tests: blame parsing on a hand-made review.json fixture; key ordering.

## P2 move generator (pure text + legaliser)

`candidates(B, blame, cons, P, seed)` -> list of `{"id", "kind", "moves": {ref: (x, y, rot)}}`, `cands` of them, mixing:
- `turn`: route_review `turned` rows, all at once, and each alone for the 3 biggest.
- `relieve`: `planplace.plan_place` on a copy of B with a hot list = refs of the top hot areas + open nets' refs
  (pass them the way relieve does; grep `hot` in relieve.py / planplace.py), seeds seed..seed+2, max_evals 200.
- `nudge`: for each of the top 3 hot areas, every movable ref in it is pushed away from the area centroid by each
  `nudge_mm`, then `legality.Legal` legalises the move set (fixed + locked are obstacles).
- `rot`: movable 2-pad parts and small ICs (<= 16 pads) among open/costly nets' refs: 90/180 deg variants of the 4 refs
  with the largest detour, legalised.
- `pull`: for each costly net with detour > 1.5, move its movable small parts toward the net's pad centroid (half
  the distance), legalised.
Never move fixed, locked-copper or group refs. Dedupe identical move sets. Tests on a fixture: fixed refs never move,
dedupe, legaliser called with fixed as obstacles.

## P3 tier-1 gates (seconds, per candidate)

Apply moves to a copy (`boardwrite.move_file`; with move_routed, first drop the moved parts' attached copper using
relieve's helpers so no dangling stub is left). Then reject the candidate when ANY of these is worse than the round's
base: `legality.collision_gate`, `intent.check` hard rows, `placechecks.connector_blocked`, `placechecks.side_policy`,
`placechecks.height_regions`, keepouts (`legalise.keepout_rects`), outline (`legalise.check_outline`), the relieve
connectivity guard (same refs, same pad->net). Survivors are ranked by `planroute.rank_key(planroute.run(B, cons,
spec, ignore_routing=False))` (the predictor, seconds). Tests: a candidate that worsens a gate is dropped; ranking.

## P4 tournament stage

`stage(c, n, board)`: dir `c.dir(n, "placetour")`. Round 0: judge the input -> key0, blame0, PNG, TOURNAMENT row.
Round r = 1..max_rounds: candidates from the current best's blame -> tier-1 gates/rank -> top `judge_top` judged by KRT in
parallel (`jobs`) -> winner = min judge_key; keep it only if lower than the best so far, else stale += 1. Stop: judge
opens == 0 and no hot area, stale >= no_gain_rounds, max_rounds, or `[flow] budget_min`. Per judged candidate: PNG
(`lookahead.render` of the judge board, title = id + key + moved refs) and a TOURNAMENT row (round, id, kind, moved refs,
tier-1 key, judge key, png, verdict). Events: `iteration(loop="place", i, metrics={...})`, `parts` frame of the
candidate's moved parts, `artifact` png, forced snapshot of each judged board and of each round's winner (main process).
Output board = the winner's MOVED board (no judge copper). progress.json per round. Register `FUNCS["placetour"]` and
insert "placetour" in CONSTRUCTIVE right before "lookahead".
Tests: loop with monkeypatched judge/candidates/gates: winner choice, stale stop, max rounds, output is the moved board
not the judge board.

## P5 report + hand-off

`placetour.md` in the stage dir: start vs end (judge opens, hot areas, vias, detour), every accepted move (ref, from, to,
rot, round, why), refs whose attached copper was removed (they need fan-out again: list them in the stage result "next"
as `fanout --refs ...` or the pipeline's re-escape hook if one exists: grep `re-escape\|reescape` in fanout.py/pairs.py
and use it on the winner board for the moved refs).

## P6 driver + smoke

`/var/kicad/utv-comms-bridge/W15/scripts/run-placetour.sh BOARD` (pattern of run-lookahead.sh): constraints copy with
`[placetour] enabled = true`, pipeline `--flow constructive --from placetour --to placetour`, CODE = this worktree, out
W15/stageP. Do NOT launch it. Smoke allowed once: `judge()` on a copy of /var/kicad/utv-comms-bridge/W15/stageL/in.kicad_pcb
in /tmp/claude-1000/-var-kicad-utv-comms-bridge/d2ff5aac-8e84-4a93-af0a-815465e0cb44/scratchpad/p1 (router dir
/var/kicad/utv-comms-bridge/W4/KiCadRoutingTools-v0.24.1, py /var/kicad/utv-comms-bridge/W4/krt-venv/bin/python, constraints
/var/kicad/utv-comms-bridge/W15/inputs/constraints.toml): report secs, opens, hot areas, top 5 blamed refs, and
`candidates()` count and kinds on that blame with tier-1 survivors.

Report: per item commit hash, tests, deviations; smoke numbers.
