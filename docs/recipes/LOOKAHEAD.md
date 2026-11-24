# LOOKAHEAD recipe: ghost route, reserved channels, lock tournament

Owner rule (EFFICIENCY-LOG 11-22, "ghost pass"; 11-22 "tournament"): before critical copper (hs pairs) is locked,
route the whole board unlocked as a throw-away ghost, reserve the channels the other nets need, lay the pairs around
them, then verify by ghost-routing again around the locked copper. Repeat as a tournament (several candidates per round,
metric-ranked, a PNG per candidate) until the ghost routes everything or there is no gain.

Branch `lookahead` (worktree /var/kicad/fluxplace-wt/lookahead). Board-agnostic: no refs, nets or constants of a
particular board. One commit per item, message `LOOKAHEAD-Ln: ...`. Tests in `tests/test_lookahead.py` must not need
pcbnew or a router (pure text fixtures + monkeypatched runners); run `timeout 300 /usr/bin/python3 -m pytest -q
-p no:cacheprovider tests/test_lookahead.py` after every item. Do not run any router or the pipeline on a real board.
Read only the lines you need (`grep -n` first).

## Config (constraints.toml), read by `lookahead.params(cons)`

```toml
[lookahead]
enabled = false          # stage is "skipped" unless true
cell_mm = 1.0            # demand grid
escape_mm = 3.0          # ghost copper of a net within this distance of one of its own pads = escape channel
min_nets = 2             # a cell with >= this many distinct non-critical nets on one layer = bus channel
pair_clear_mm = 2.0      # never reserve within this of a critical net's pad
max_area_frac = 0.25     # per layer, reserved area <= this fraction of the board bbox (highest demand first)
ghost_budget_min = 40    # route budget of one ghost route
jobs = 3                 # candidates routed in parallel
max_rounds = 4
no_gain_rounds = 2
variants = [[2, 3.0], [1, 3.0], [3, 2.0]]   # (min_nets, escape_mm) per candidate in round 1
```

Critical nets = the legs of every pair in the constraints that `pairs.stage` lays (reuse the function pairs.py uses to
list its pairs, do not re-derive), plus `[power]` rail nets if the constraints list them (same helper krtchain uses).

## L1 `fluxplace/lookahead.py`: demand map and reservations (pure text)

- `params(cons)` -> dict of the keys above with defaults.
- `demand(board_path, critical, P)`: `B = routediag.load(board_path)`. Grid over `B["bbox"]`, cell `P["cell_mm"]`.
  For each segment in `B["segs"]` (and arc: use a, b and mid as a polyline) of a net NOT in critical and not a zone/plane
  net: rasterise the segment (step cell/2) onto (layer, ix, iy) cells; per cell keep the set of nets. Vias: mark their
  cell on every copper layer they span. Also record per cell the critical nets present (`crit`).
  Escape: a cell is `escape` for net n when its centre is within `escape_mm` of a pad of n (pads from `B["pads"]`).
  Return `{"cells": {(layer, ix, iy): {"nets": set, "crit": set, "escape": bool}}, "origin": (x0, y0), "cell": c}`.
- `reservations(D, B, critical, P, min_nets=None, escape_mm=None)`: candidate cells = (escape and nets) or
  len(nets) >= min_nets. Drop a cell when `crit` is non-empty (the ghost found a joint solution there: the pair may use
  it) or when its centre is within `pair_clear_mm` of a pad of a critical net. Rank by (len(nets) desc, escape first);
  per layer keep cells until `max_area_frac` of the bbox area. Merge per layer into rectangles: row runs of adjacent cells
  in x, then stack equal runs in y. Return list of `{"layer", "rect": (x0, y0, x1, y1), "nets": sorted list, "demand"}`.
- Tests: synthetic routediag-shaped dicts (monkeypatch `routediag.load`) for rasterising, the crit drop, the pad-clear
  drop, the area cap and the rect merge.

## L2 apply and strip (pcbnew in a subprocess)

- `apply(board_in, board_out, rects)`: run a fresh `/usr/bin/python3` subprocess (pcbnew corrupts on repeated loads, see
  tournament.py) that loads board_in, adds one rule-area ZONE per rect on its layer: `SetIsRuleArea(True)`,
  `SetDoNotAllowTracks(True)`, `SetDoNotAllowVias(True)`, pads/pours/footprints allowed, name `LOOKAHEAD-<i>`, outline =
  the rect; saves to board_out. Copy the `.kicad_pro` beside it (`pipeline._sidecar` pattern).
- `strip(board_in, board_out)`: same subprocess pattern, removes every zone whose name starts with `LOOKAHEAD-`.
- Before writing the zone code, `grep -n "SetIsRuleArea\|SetDoNotAllow" -r fluxplace` and reuse an existing helper if one
  exists. Test: the subprocess script text is built by a function; test that function's output (string), not pcbnew.

## L3 ghost route and fitness

- `ghost(a, board, outdir, budget_min)`: subprocess `/usr/bin/python3 <repo>/cli.py pipeline <copy> --workdir
  <outdir>/run --constraints <a.constraints> --spec <a.spec> --flow constructive --from route --to route --router
  krt-whole --router-dir <a.router_dir> --router-py <a.router_py> --route-budget <budget_min*60> --assume-defaults
  --allow-incomplete --allow-undocumented --no-api` (pass through whichever of these `a` has; copy the exact flag list
  from W15/scripts-route.sh in /var/kicad/utv-comms-bridge). Result board = `<outdir>/run/*-route/board.kicad_pcb`.
  Timeout budget + 10 min. Returns the path or None.
- `fitness(board, critical, cons)`: `gaps.measure(board)` (DRC); `pairaudit` (or what `fluxplace review` uses for hs
  pairs: grep `pairaudit` in review.py) for complete pairs. Return a dict and a sort key, lexicographic:
  (-pairs_complete, shorts, opens, sealed, drc_errors, reserved_area). `sealed` = opens of non-critical nets whose
  unconnected pads lie within `escape_mm` of locked critical copper (a pocket closed by a locked route).
  `blockers` = {critical pair name: count of sealed opens next to its copper}.
- Tests: fitness ordering with fake measure/audit (monkeypatch).

## L4 render (one PNG per candidate)

- `render(board, rects, fit, path)`: matplotlib (Agg), 1 px = 0.05 mm, one subplot per signal layer that has copper
  (max 4 columns). Draw: board outline grey; non-critical copper thin, light; critical copper bold, saturated; reserved
  rects hatched translucent; unconnected items of the DRC (from fit) as red lines with the net name; title = candidate id
  and the fitness tuple. Pure text input (routediag.load + the DRC json), no pcbnew. Test: renders a tiny fixture to a
  tmp path without error.

## L5 the tournament stage

- `stage(c, n, board)` (pipeline signature as `pairs.stage`). If not enabled -> `{"verdict": "skipped", ...}`.
  Dir `d = c.dir(n, "lookahead")`. Everything is written under d; write `d/TOURNAMENT.md` (one table row per candidate:
  round, id, variant, fitness, png path, verdict) after every candidate, and `d/progress.json` (pipeline._stage_status
  pattern) so a detached run can be read without logs.
  1. Round 0: `g0 = ghost(input board)` (unlocked: the input has no locked critical copper, or the caller stripped it).
     `D = demand(g0)`.
  2. Round r (1..max_rounds): for each variant (round 1: `variants`; later rounds: see 3), in parallel up to `jobs`
     (concurrent.futures, subprocesses only): rects = reservations(D, ...); `apply` -> laid by `pairs.stage` on a
     candidate copy (call it the way st_pairs does, with its own sub-dir; c.dir-style naming `cand<r>-<i>`); if pairs
     stayed incomplete, `strip` and run `pairs.stage` once more on the stripped board (pairs already in tolerance are
     not touched: it only lays the missing ones); `strip`; lock happens inside pairs.stage (lock policy); `ghost` on the
     result (verify); `fitness`; `render`.
  3. Winner = min sort key. Next round's variants: for the top 2 `blockers` pairs of the winner, a candidate that starts
     from the winner's pre-verify board, strips that pair's copper (unlock + delete its tracks/vias; reuse the rip helper
     pairs.py uses for `--unlock`), adds rects built from D for the sealed nets' cells (min_nets 1, escape_mm +1) and
     re-lays only that pair. Plus one candidate re-running round-1's best variant with min_nets - 1.
  4. Stop when the winner's ghost has opens == 0 and all pairs complete, or no lower key for `no_gain_rounds`, or
     max_rounds. Output board = the winner's pre-verify board (pairs laid + locked, no LOOKAHEAD zones, no ghost
     copper). Verdict "pass" with line `lookahead: <rounds> rounds, <cands> candidates, winner <id>: pairs a/b, ghost
     opens N (round-0 ghost opens M)`; "next" lists TOURNAMENT.md and the winner png.
- Register in pipeline.py: `FUNCS["lookahead"] = st_lookahead` (thin wrapper like st_pairs) and insert "lookahead"
  before "pairs" in CONSTRUCTIVE. `pairs` after a passed lookahead must be a no-op on laid pairs (already_ok); check that
  st_pairs on the lookahead's output does not re-lay anything (test with a monkeypatched pairs.stage call count is not
  enough: grep how pairs.stage treats locked in-tolerance pairs and cite the line in the commit message).
- Tests: the round loop with every external call monkeypatched (ghost returns fixture paths, fitness returns scripted
  keys): winner choice, stop rules, blockers-driven variants, TOURNAMENT.md rows.

## L6 driver script (board project, not the repo)

Write `/var/kicad/utv-comms-bridge/W15/scripts/run-lookahead.sh BOARD`: copy W15/inputs/constraints.toml to
W15/stageL/constraints.toml with `[lookahead] enabled = true` appended, then the pipeline `--flow constructive --from
lookahead --to lookahead` on BOARD with the router flags of W15/scripts-route.sh, CODE=/var/kicad/fluxplace-wt/lookahead,
workdir W15/stageL/run, RUN.md lines (log() pattern of scripts-route.sh) at start, each round end (grep TOURNAMENT.md)
and finish; `touch W15/stageL/lookahead.done`. Do NOT launch it.

## Report back

Per item: commit hash, test count, and anything you could not do as written (say so; do not mark it done).

## L7 (owner 11-22): the autorouter's output pre-plans the locked routes; any router may be the ghost

Owner: "use an auto router's output to preplan the ones that need to be locked"; "it can use the KiCad router too if it's
better". So the ghost does two jobs: it reserves the other nets' channels (L1) AND it is the plan for the critical pairs
themselves. Reservations become soft costs, not only hard keepouts.

- `[lookahead] ghost_routers = ["krt-whole"]` (list; allowed values = the `--router` names pipeline.py accepts, e.g.
  "krt-whole", "freerouting"). Round 0 runs one ghost per router (in parallel, `jobs`), ranks them with `fitness`, and the
  best ghost is D's source. TOURNAMENT.md gets a round-0 row per ghost. The router flags each needs are passed through
  from `a` (freerouting: --freerouting-jar/--java, see pipeline.py:489).
- `corridors(g0, critical_pairs, P)`: per critical pair, the ghost's copper of both legs (routediag segs of the two nets)
  rasterised on the demand grid with a halo of `corridor_mm` (default 1.0) -> {pair: set of (layer, ix, iy)}. A pair whose
  ghost legs are not both connected end to end gets no corridor (logged).
- Adopt first: if the ghost's own copper for a pair passes the pair audit used by `fitness` (coupled, gap, skew within the
  constraints), copy that pair's ghost tracks/vias onto the candidate board as-is (subprocess, pcbnew, keep uuids new),
  lock them, and skip laying it. Count adopted pairs in the TOURNAMENT.md row.
- Guided lay otherwise: `pairs.lay_section` already takes `cost` (bytes per layer, extra cost x cunit of entering a cell,
  pairs.py:183/239, used by negotiation). Add an optional `guide` to the pairs ctx (`grep -n "ctx = \|ctx\[" pairs.py`):
  a function (layer, x_mm, y_mm) -> extra cost, which lay_section adds into `cost` on its own grid (max of both, capped at
  255). Lookahead's guide: + `res_cost` (default 200) on reserved cells, + `off_cost` (default 20) outside the pair's own
  corridor, 0 inside. With a guide, L5 step 2 uses the guide instead of `apply` keepouts; keep `apply` for the variant
  `[lookahead] hard = true`. Round-1 variants become: soft guide with (min_nets, escape_mm) from `variants`, plus one hard
  variant with the first entry.
- Without a `guide`, pairs.py behaviour is byte-identical (test: the existing pairs tests pass unchanged; add one test that
  a guide raising cost on the straight path makes the fixture's path bend around).

## L7b (owner 11-22): both routers are throw-away ghosts, each is a source of ideas

Owner: "KRT and Freerouting as throwaways and see what each wants to do, to have more versatile ideas". Supersedes the
"best ghost is D's source" line of L7.

- Default `ghost_routers = ["krt-whole", "freerouting"]`. Round 0 runs every ghost in parallel (Freerouting: jar
  /home/jratz/tools/freerouting-2.5.0.jar unless `--freerouting-jar` is given; pin 2.5.x per EFFICIENCY-LOG 11-22).
  A ghost that fails or times out is logged in TOURNAMENT.md and dropped; the round goes on with the rest.
- No ghost is "the" plan. Each ghost g gives its own demand map D_g and corridors C_g. Round-1 candidates per ghost g:
  the soft variants of L7 built from (D_g, C_g) only, tagged `src=g` in TOURNAMENT.md. Plus mixed candidates:
  * `consensus`: reserve only cells both ghosts used for the same non-critical net side by side (D_krt and D_fr both
    non-empty on that layer, or on any layer when `consensus_any_layer = true`); strongest channels, smallest area.
  * `union`: reserve cells either ghost used (with `max_area_frac`), corridors = C_g of whichever ghost had the pair
    connected and in audit tolerance (KRT first on a tie).
  * per pair, adopt (L7 "adopt first") from whichever ghost has that pair in tolerance; both -> the shorter one.
- Render one PNG per ghost too (round 0), so the owner can compare what each router wanted.
- Later rounds keep `src` from the winner; the blocker re-lay variants (L5 step 3) use the demand map of the winner's
  src (consensus/union use the merged map).
- Keep `jobs` as the cap on concurrent router processes (each JVM / KRT call counts as one).

## L8 (owner 11-22): fast. Ghosts are sketches, not finished routes; verify in tiers

Owner: "run it fast, don't wait for the full route to complete, just the fastest steps to get an idea; be mindful of
speed so boards get routed and completed quickly and at the highest quality". Supersedes the budgets of L3/L5/L7b.

Ghosts (round 0, all in parallel, each hard-capped):
- `global` ghost, always, seconds: `planroute.run(B, cons, ignore_routing=False, ...)` (planroute.py:58; the coarse
  PathFinder of globalroute.py: per-cell demand, pairs as 3-pitch demands). Its per-(layer, cell) net use feeds D_global
  directly (no rasterising needed: read the routed cell paths from the GlobalRoute object). This is the third idea source
  of L7b (tag `src=global`) and the fallback when both routers fail.
- `krt-whole` ghost: `ghost_min.krt` default 8 (route budget 480 s, no finish ladder; pass whatever pipeline flag skips
  the ladder/close, grep `ladder\|finish` in pipeline.py st_route; if none exists, budget only).
- `freerouting` ghost: `[route.freerouting] passes = ghost_passes.fr` (default 4) and job timeout `ghost_min.fr`
  (default 8) on a constraints copy for the ghost only (routefr.py:124 reads `passes`).
- Whatever copper a ghost has when its cap hits is the ghost (partial is fine: demand comes from what it laid + the
  global ghost for the rest). Never wait for completion.

Candidate fitness in two tiers:
- Tier 1, every candidate, seconds, pure text: (a) `escapeplan.plan_escapes(B, cons, ignore_routing=False, routed_nets=...)`
  on the candidate (pairs laid+locked): count PLAN_PIN_SEALED pins of non-critical nets = `sealed` (this replaces the
  ghost-based sealed of L3 for tier 1), (b) `planroute.run(..., ignore_routing=False)` overflow / unroutable nets of the
  non-critical nets around the locked copper = `overflow`, (c) pair audit (complete pairs, skew). Key:
  (-pairs_complete, shorts, sealed, overflow, reserved_area).
- Tier 2, only the top `verify_top` (default 2) of the round: one KRT verify ghost at `ghost_min.verify` (default 10).
  Final key: tier-2 opens break ties within equal tier-1 keys and decide the round winner; log both in TOURNAMENT.md.
- Pair lay per candidate: cap `pairs.stage` with the existing time/budget knobs on a constraints copy
  (`[pairlay] fallback = false` for candidates; the fallback ran 36-77 min in W14), `workers` = 32 // jobs.

Rounds: `max_rounds` default 3, `no_gain_rounds` 1. Target wall time: round 0 <= 10 min, each later round <= 20 min,
whole stage <= 60 min. Print and write to progress.json the wall time per ghost / candidate / tier so the efficiency
log can be fed from it. `jobs` default 4 (32 cores, 62 GB: one JVM or KRT call each, pair lays use the rest).

## L9 (Fable, after run 1): lean ghosts, not the pipeline route stage

Run 1 (W15/stageL, 11-22 09:20) measured: both router ghosts were useless. KRT ghost (pipeline route stage, 8 min):
planes + rails-pour + rails used the whole budget (rails step rc 124), zero signal nets, opens 463. FR ghost (pipeline
route stage): pre-steps + gate + batch ladder left one 4-min FR window that timed out before pass 1 (fr_pass 0), opens 426.
A ghost must spend its whole budget routing SIGNAL nets. Replace `ghost()`'s pipeline subprocess with two lean paths
(keep the signature and return value; ghost.out keeps the tail of every subprocess):

- `krt`: `R = routerules.rules_for(cons, board, a)`; `base = krtchain.base_flags(R, outdir)`; signal layers = the
  board's copper minus plane layers (`krtchain.plane_plan(board)` gives the planes); ONE call
  `krtchain.cmd_route(a.router_py, a.router_dir, copy, out, sig, base, power={}, nets=["*"])` with every plane net
  excluded (route.py `--nets` takes globs; if it has no exclude option, pass the explicit list of non-plane nets with >= 2
  pads from routediag.load). No planes, no rails, no pairgroups, no diff: pair legs and rails route as plain nets (the
  ghost wants their natural paths). Run it with `routestatus.run_logged(cmd, ..., timeout=ghost_min*60)` like
  `krtchain.run_step` does; at the cap, use the router's checkpoint file if the status json names one (route.py writes
  checkpoints, see routestatus.py:183), else None.
- `freerouting`: fr_helper.py `export` (DSN of the copy; run it with /usr/bin/python3 like routefr does, grep
  `_helper(` in routefr.py), then `java -jar <jar> -de in.dsn -do out.ses -mp <ghost_passes.fr> -mt <threads>`
  (threads = 32 // jobs; flags per W4/freerouting-2.5.0-help.txt; reuse routefr.command/run_jvm if they take these
  directly), wall cap ghost_min.fr; FR writes the .ses only at the end, so cap = passes, not time: set -mp so it ends
  (default 4) and the wall cap as a safety only (+50%). Then fr_helper `import` {board: copy, ses, out}. No pre-route
  gate, no ladder, no batches.
- Both: budgets from L8 (`ghost_min.krt` 8, `ghost_passes.fr` 4). Log per ghost to progress.json: secs, nets routed,
  opens before/after (routeconn text count), so the next run shows whether a ghost earned its minutes.
- Tests: command lists built for both paths (monkeypatched subprocess), checkpoint fallback, plane nets excluded.
- Do not touch the verify ghost (tier 2): it stays the pipeline route stage (it must route like the real flow).

## L10 (owner 11-22): streaming ghosts, the first one lands -> round 1 starts; the slow one is checked when it lands

Owner: "run KRT and Freeroute and start when the first one lands, let the other finish or time out in the background,
and double check the second one when it lands to see if it had a better plan". Run 2 measured: KRT --quick 2 ghost 80 s,
FR 1 pass > 12 min on 614 items; round 1 idled 10 min waiting for FR.

- Round 0 launches every router ghost in the background (futures). The global ghost (seconds) runs inline.
- As soon as the FIRST router ghost lands (or every router ghost failed -> global only), build sources and start round 1
  from what is in hand. Do not wait for the others.
- Late ghost: when another router ghost lands while rounds are running, write its round-0 TOURNAMENT row + PNG + stats at
  once, then compare its plan with the current source: `fitness` of the ghost board (pairs complete, opens, sealed) AND
  the channel overlap (Jaccard of the two demand maps' reserved cells). Log one line in TOURNAMENT.md / progress.json:
  `late ghost <r>: pairs a vs b, opens x vs y, overlap z % -> better | equal | worse`.
  * better (lexicographically lower fitness key than the ghost the running round was built from): the NEXT round gets the
    late ghost's candidates (its own soft variants + consensus/union with the first ghost), in addition to the blocker
    re-lays. If the stage would stop after the current round (stop rules), it runs one more round for them.
  * equal / worse: its consensus/union with the first ghost is still added once as one extra candidate in the next round
    (two opinions agreeing on a channel is stronger evidence), nothing else.
- A ghost still running when the stage finishes is killed (its process group) and logged `timeout (stage done)`.
- Timing in progress.json per ghost: launched, landed, secs, and whether it was used (`first`, `late-better`, `late-used
  -consensus`, `late-ignored`, `timeout`).
- Tests (monkeypatched ghosts with scripted delays): round 1 starts after the first ghost, a late better ghost adds
  candidates to the next round, a late worse ghost adds only the consensus candidate, a still-running ghost is killed
  at stage end.

## L11 live board for the dashboard

- The main lookahead process takes a forced snapshot (`snapshot.active_take(board, reason, force=True)`) of every
  ghost board as it lands (reason `ghost <router>`) and of every candidate's locked board and verify board as results
  come back (reason `cand <id> locked|verify`). Workers stay snapshot-free. Test with a monkeypatched active_take.
