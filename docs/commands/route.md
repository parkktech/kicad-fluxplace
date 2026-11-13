# Routing: batch routing with checkpoints (`pipeline` route stage, `auto`, `compact`)

**Never time-box a whole-board router call; use batch routing.** The external router (KiCadRoutingTools
`py_router`) writes its output only when a whole pass, including its end-of-pass retry / rip-up phase, has
finished. Killing a whole-board call at a timeout therefore loses everything. `fluxplace/batchroute.py` routes in
batches instead. (`fluxplace route BOARD` itself is the internal grid estimate: it calls no external router.)

## Algorithm

1. The work list is the board's OPEN CONNECTIONS (`routeconn.py`: pads still not joined by tracks, vias or filled pours), per net; a net with some copper but unjoined pads is routed too. Order those nets: fan-out (fine-pitch escape) nets, power nets, critical classes (rf, pair,
   switch_node, clock, bypass), the rest; inside each group ascending estimated length (MST), so local nets go first.
2. One router call per batch of `batch_nets` nets (`--keep-input-copper`, so earlier copper is protected), each
   time-boxed to `batch_timeout_s`. The router runs with its retry phase limited (below).
3. After every batch that produced a usable board (parses, ends in `)`, no more unrouted nets than before) it is saved as
   `W/ck-NNN.kicad_pcb` (+ `.kicad_pro`) and becomes the input of the next batch. Earlier checkpoints are never touched.
4. Timeout: SIGINT, 4 s, SIGTERM, 5 s, SIGKILL (process group). If the router wrote nothing (or a worse/garbage board) the
   previous checkpoint is kept and the batch is HALVED (down to single nets) and retried; a partial output written on SIGINT
   is kept and its unrouted nets are retried in halves. A single net that still times out / writes nothing is a leftover.
5. Failed nets (router finished, net not routed) go to the leftovers list. After the last batch ONE leftovers pass runs
   those nets with the router's full retry phase on (`batch_timeout_s * 2`).
6. Then `route-diagnose` on what remains (`route-diagnose-N.txt`) and the existing stall loop (`--stall-rounds`,
   `--replace-on-stall`), whose re-routes are batch routed too.

The result is always the last good checkpoint, or the input board when nothing improved: the command never exits with a board
less routed than its input. `W/leftovers.json` = `{board, checkpoints, leftovers, reasons}` (exact list of requested nets that
still have an open connection in the final board; reason failed | timeout | no output | budget | RULE_VIOLATION). `leftovers.json` also carries `open_connections` / `open_nets`. There is no "a net with any copper counts as routed".

## Status: `route-status` / `route-wait`

One status file for the whole run (`W/routed.kicad_pcb.status.json`, `<routed>.status.json` for auto) with `mode: "batch"`,
`batch`, `batches_total`, `batch_nets`, `halvings`, `last_good`, `last_good_open` / `last_good_open_nets`, `start_open`, `rounds[]` (`open_before` / `open_after` per router call or parallel round), `rule_violations`, `leftovers`, `checkpoint`,
`checkpoint_n`, `out` (= last good board). `route-status` prints
`route running  batch 3/9 (35 nets)  ... last good ck-003.kicad_pcb (41 open connections in 30 nets, 2 leftover)`. State stays `running`
until the whole run ends, so `route-wait` does not return after the first batch; `--until checkpoint` returns after the next
checkpoint. `route-wait` prints the last good board as the result.

## Constraints TOML `[route]`

    [route]
    batch_nets = 35          # nets per router call
    batch_timeout_s = 240    # wall clock per call
    batch_max_ripup = 3      # --max-ripup for the batches
    total_budget_s = 3600    # optional cap on the whole run (pipeline: --route-budget); rest become leftovers
    stage_timeout_s = 900    # stall-loop re-route budget (older key)
    batch_env = {}           # extra/override env for the batches; {} = the defaults below
    mode = "serial"          # "serial" batches | "parallel" per-net jobs + merge
    workers = 8              # parallel: default CPU count / 2, max 16;  job_timeout_s (default batch_timeout_s), round_jobs (default workers)
    via_in_pad = false       # default false: --same-net-pad-clearance keeps vias off every pad
    same_net_pad_clearance_mm = 0.2
    grid_mm = 0.05           # default 0.05 when any part has pitch <= 0.5 mm, else 0.1
    min_track_mm / min_clearance_mm / min_via_mm / min_drill_mm / hole_to_hole_mm   # extra floors (else [fab] and .kicad_pro)
    clearance_margin_mm = 0.1  # asked of the router on top of the default clearance when a floor is set (default one grid step); the audit keeps the floor
    escalation = "off"       # router --escalation (off|board|fab); strict_sizes = true -> --strict-sizes
    rail_min_pads = 8        # >= this many pads and no plane/pour: routed as a tree of two-cluster jobs (parallel mode); rail_edges_per_round = 3
    allow_rip = "low-speed"  # "never" | "low-speed" | "any": see Rip policy;  controlled = [nets], controlled_patterns = [regex]
    ladder_reserve_frac = 0.25  # share of --route-budget kept back for the escalation ladder (0 = rounds may use it all)
    pair_audit = true        # a pair is routed only when both legs are complete and match; else removed and retried as a pair
    audit = true             # post-batch audit; audit_drc = false (true: kicad-cli DRC, new error types vs the input)
    conn_fill = true         # refill zones before counting open connections;  conn_method = "auto" | "pcbnew" | "text"

## Limiting the router's retry phase (batches only)

py_router env knobs (`py_router/env_knobs.py`, read-only reference) set for the batches; the leftovers pass restores the defaults:

| setting | value | effect |
|---|---|---|
| `KICAD_NET_RESCUE` | 0 | no post-pass net rescue (whole-net retries with rip-up of failed nets) |
| `KICAD_TERMINAL_ESCALATION` | 0 | no terminal re-route of failed nets at shrinking width / via size |
| `KICAD_DYNAMIC_ITERATIONS_CLAMP` | 200000 | A* iteration cap (speed over completion) |
| `--max-ripup` | 3 | blockers ripped up at once in the rip-up ladder (router default is also 3, now explicit) |

`--quick` is NOT used: it skips cleanup/smoothing and ships a diagnosis board. Verified only against a fake router
(`tests/test_batchroute.py`), not against a real `py_router` run.

## Project rules passed to the router (`routerules.py`)

Every py_router call fluxplace builds (pipeline route stage, `auto` route + plane finalize, adaptive route-fresh /
length-match / diff / fanout) gets: `--track-width/--clearance/--via-size/--via-drill` raised to the floors,
`--fab-overrides W/router-fab.txt` (clearance, track_width, via_diameter, via_drill, hole_to_hole = the strictest of
`[fab]`, `[route]` and the `.kicad_pro` rules; pins the floor to one hard rung, no descent), `--same-net-pad-clearance=0.2`
(`=-1` only when `via_in_pad = true`), `--grid-step` and `--fab-tier standard`. A flag the checkout does not know is left out
(the audit still holds). py_router has no skip-pour-refill option; `--write-fill` is simply not passed.

## Post-batch audit (`routeaudit.py`) - the router is not trusted

After every router call the NEW copper (tracks/vias the output has and the input lacks) is checked: a via whose barrel
overlaps ANY pad (exact rect/oval/circle shape, any net) unless `via_in_pad`; track width, via diameter and drill below the
floors; edge-to-edge clearance to other-net tracks, vias and pads below the floor; with `audit_drc` a KiCad DRC error type
the input did not have. A batch that fails is discarded (last checkpoint kept), halved, retried; a single net that still
fails is a leftover `RULE_VIOLATION` (`route-status` shows the count). A serial batch that leaves another net MORE open than
before (it ripped it) is also discarded.

## Parallel mode (`[route] mode = "parallel"`, `routepar.py`)

Round: single-net jobs (up to `workers` at once), each on its own copy of the checkpoint (no refill-per-batch cost, own
status/log in `W/jobs-rNN/`). Per job: new copper vs the checkpoint; a job that REMOVED another net's copper is not
mergeable (queued for the final serial pass); a rule-violating job is `RULE_VIOLATION`; a hung job is `timeout`. Merge in
routing order: of two jobs whose new copper (different nets) comes within clearance, the first wins, the other is dropped
and re-queued for the next round; accepted tracks/vias are imported as text into the checkpoint (vias snapped to the end of
the same net's new track), the merged board is audited and its open connections counted; it becomes `ck-NNN` only if clean
and not worse. Repeat until a round reduces no open connection, then ONE serial pass over the ripped nets.
Status: `mode: "parallel"`, `round`, `workers`, `rounds[]` with `merged/dropped/ripped/timeouts/violations/failed/open_before/open_after`.

## Plan hints (`[route] plan_map`)

`[route] plan_map = "<board>.fluxmap.json"` (a `plan-route` map): `batchroute.order_nets` routes the nets whose planned path crosses
a `PLAN_OVERFLOW` channel first inside their group, then differential pairs, then by length. **The router takes no per-net layer or
corridor hints** (KiCadRoutingTools has no such interface), so the plan can only order nets; planned layers and cell paths are not
passed on. Read them with `map --show route --net N`.

## Dropped results by reason
Status JSON `dropped` (total) and per round `dropped_by`; the compact line prints "dropped ...": under-floor, rips-other-net,
via-in-pad, off-board (copper crossing the outline), timeout, no-path, other (`routedrops.py`).

## Rails, rip policy, pairs, plain copper (parallel mode, `routepar.py`)
- **Rails** (`railroute.py`): a net with >= `rail_min_pads` pads and no zone / `[[plan.pour]]` / ground-by-name is never one job.
  MST over its pad clusters, one job per edge (nearest connected cluster first, the net taken off the other pads), merged round by
  round; width from `[power."NET"] max_current_ma`. Poured rails: `fanout --add-zones` writes the zone. Status `rails`.
- **Rip policy** (`ripolicy.py`): a result that removes other nets' copper is adopted only when none is controlled (pair, rf, clock,
  switch node, `[nets.X]` rules, `controlled*`), none locked, and a trial merge (pcbnew, zones refilled) lowers the total open count
  with every affected net still joined; else dropped as `rips-other-net`. "never" drops all; "any" skips the class test.
- **Pairs** (`pairaudit.py`): one two-net job. Complete = both legs closed, same layer sequence, same via count, skew <=
  `[pairs.*] skew_mm` / `[si] default_skew_mm`. A half or mismatched result is not merged; a half pair in the input is stripped
  (unlocked copper) first. Status `pairs`: complete / mismatch / half-removed / failed.
- **Plain copper** (`[nets.X] straight = true`): routed first, rejected when it changes layer beyond what its pads force, never ripped.
- After every merge `snapvias` moves track ends inside a via to its centre (`docs/commands/snap-vias.md`).

## Routing order, escape reservations, quality gates
- **Order** (batch and parallel; status `order`): escape (fan-out + reserved escapes, `first`), plain copper, power pins and boxed-in passives (nets that own a
  reserved escape, the map's `escapes`), rails (tree / pour), controlled pairs / RF / clocks, the rest; inside a phase plan criticality, then length. Parallel jobs
  are taken in this order (rail edges at most half the slots while other nets wait); merge conflicts go to the earlier phase. Reservations are small locked stubs
  and vias, not corridor blocks; the router still takes no per-net layer hint, so a pair routed after them chooses its own layer.
- **Reservations** (`escres.py`, `docs/FLOW.md`): `route-ready` FAILs `PLACE_ESCAPE_UNRESERVED` naming the pin and what crowds it; `fanout` writes them as locked
  stub + via copper before its own search and records them in the map (`escapes`). `[escape] near_mm lane_mm window_mm radius_mm enabled`.
- **Quality** (`qualaudit.py`): after every merge the checkpoint is audited; a pair (both legs, same layers, same vias, skew <= limit) or RF net (declared
  width per layer / impedance, layer changes <= limit) out of tolerance is NOT DONE: not counted in `nets_routed`, listed with its cause in status `not_done`,
  `route-status` and the route stage line. At the end of the route stage pairs failing ONLY on skew go through `tune` (`[route] tune_rounds` 3, `tune_pairs` 8);
  the result is kept when no connection opens and the skew falls.
