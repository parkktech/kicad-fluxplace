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

## Smaller items

- Cross-board corpus manifest (`~/.config/fluxplace/corpus.toml`) so changes are proven on more than one board.
- Support parts that ride with a fixed module and overlap it are never moved by the floorplan (needs a release option).
- The pins stage released fixed standoffs and moved them.
- Exit-code table: one meaning per code across commands.
- Hook the KiCad-open lock guard into every board-saving command, not only the hand-route ones.
