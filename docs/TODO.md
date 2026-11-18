# TODO (owner-approved backlog; newest first)

## After the V1.6 board is finished

- **Thin KiCad plugin (owner, 2026-11-17).** Linux KiCad 10.0.5 runs in the same WSL environment as fluxplace, so no
  Windows bridge is needed. Use the new plugin interface (`kicad-python` / `kipy`, not installed yet; the API server must
  be enabled in KiCad preferences). Confirm first what KiCad 10 exposes (board items, selection, refill; the interactive
  router is believed not to be exposed). Build in this order:
  1. Set intent by selecting: mark parts fixed, draw a keep-out or module outline, pick the connector edge; written to
     the constraints TOML.
  2. Open the current accepted board; promote an accepted board into the project (the pipeline never writes its result
     back to the source board, which led the owner to open a stale file).
  3. Run `placement-check` and show findings as markers.
  Not planned: routing through the plugin.
- **`annotate` command (no plugin needed).** Draw keep-outs, module outlines, washer circles, blocked pins and rule
  violations on user drawing layers in a COPY of the board, to open in KiCad. Would have caught a wrong module position
  and face at a glance.

## Stage order (deferred "group 4"; the cause of the remaining pair and RF defects)

- Write stackup and fab rules into the board before any routing; refuse to route on a mismatch.
- RF nets first: required outer layer, width from the stackup, via limit, then lock.
- Reserve pair corridors pad to pad during placement; lock escapes only after the corridors exist.
- Reachability (seal) check after every stage that locks copper.
- Channel capacity check at placement for nets that must share a layer.
- Ground stitching vias after RF corridors are reserved.

## From the W9 run (2026-11-17), fix next

- The pairs STAGE keeps nothing when it is stopped: twice an hour of work was discarded at an outer timeout. It must take
  a `--budget`, handle SIGTERM/SIGINT, and write the best completed set with a complete report (as `tune` does).
- Fallback trials need their own budget and a stall stop (quit when a trial completes nothing). Measured: about 15 min
  per trial, 1 pair gained in trial 1, 0 in trial 2, 12 trials configured.
- The fallback's internal fan-out stopped to ask a layer-role question in an unattended stage. Inside a stage it must
  take the answer from the constraints or defaults, or fail at once with a clear message.
- Measured value of effort: full-effort main pass 18/35 pairs against 15/35 bounded; most hand-offs are "escape
  blocked" (fan-out lays each leg's escape separately), so pair-aware escapes are the larger gain.
- What worked and should become the pipeline's default order: re-escape of the regions that ended open, then RF first on
  outer layers with `lay --lock` (all four RF nets in spec for the first time), before pairs and ordinary copper.

## Smaller items

- Cross-board corpus manifest (`~/.config/fluxplace/corpus.toml`) so changes are proven on more than one board.
- Support parts that ride with a fixed module and overlap it are never moved by the floorplan (needs a release option).
- The pins stage released fixed standoffs and moved them.
- Exit-code table: one meaning per code across commands.
- Hook the KiCad-open lock guard into every board-saving command, not only the hand-route ones.
