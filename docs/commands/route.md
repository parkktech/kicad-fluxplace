# Routing: batch routing with checkpoints (`pipeline` route stage, `auto`, `compact`)

**Never time-box a whole-board router call; use batch routing.** The external router (KiCadRoutingTools
`py_router`) writes its output only when a whole pass, including its end-of-pass retry / rip-up phase, has
finished. Killing a whole-board call at a timeout therefore loses everything. `fluxplace/batchroute.py` routes in
batches instead. (`fluxplace route BOARD` itself is the internal grid estimate: it calls no external router.)

## Algorithm

1. Order the nets still unrouted: fan-out (fine-pitch escape) nets, power nets, critical classes (rf, pair,
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
have no copper in the final board; reason failed | timeout | no output | budget). A net with copper stubs counts as routed
(`routediag.unrouted_nets`); `route-diagnose` / DRC see the rest.

## Status: `route-status` / `route-wait`

One status file for the whole run (`W/routed.kicad_pcb.status.json`, `<routed>.status.json` for auto) with `mode: "batch"`,
`batch`, `batches_total`, `batch_nets`, `halvings`, `last_good`, `last_good_unrouted`, `leftovers`, `checkpoint`,
`checkpoint_n`, `out` (= last good board). `route-status` prints
`route running  batch 3/9 (35 nets)  ... last good ck-003.kicad_pcb (41 unrouted, 2 leftover)`. State stays `running`
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
