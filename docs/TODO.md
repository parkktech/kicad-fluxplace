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

## Parts gate must include 3D models before placement (owner, 2026-11-17)

- A placement run must not start while any part lacks a real 3D model: the owner cannot judge mechanical fit (connector
  face, module keep-outs, enclosure) from a board without bodies. The parts-first rule covers stock, datasheet, pinmap and
  model; the pipeline's parts gate was waived with `--allow-undocumented`/`--allow-incomplete` in every run, which hid it.
  Make "model present and file resolvable" a hard placement precondition that the allow flags do not waive.
- Board copies in work folders broke every `${KIPRJMOD}`-relative model path (130 of 341 parts showed no body). The
  pipeline must keep model paths resolvable when it copies a board (write the copy beside the project, or check and
  report unresolved models on every copy).

## Done 2026-11-18

- `placement-check` and `pairs` run the route collision gate with rule areas (W11 lesson 1): the board that the route stage refused now fails placement-check.
- A connected pad inside a track-forbidding rule area is no longer keepout_trapped when a filled pour of its net touches it and the area allows pours (W11 lesson 2).
- Model paths: `placement-check` and `progress checkpoint` warn MODEL_PATH_UNRESOLVED with the count and fix hint (W11 lesson 3). Open: the pipeline still writes the work copy without lib/ beside it.
- `progress checkpoint` refuses new DRC shorting_items vs the last accepted checkpoint (`--allow-new-shorts` overrides) (W11 lesson 4).
- Final DRC summaries (`progress checkpoint --measure`, fab package scope) list `ZONE_ISLAND <net> <layer>` apart from track opens and point at `pour-island` (W11 lesson 5).
- `placement-check` now runs the route stage's collision gate (part-against-part courtyard / copper / through-hole
  overlaps). Found when a connector moved to the board edge covered two of its own resistors: placement-check said OK,
  the router refused 80 minutes later.

## Smaller items

- Cross-board corpus manifest (`~/.config/fluxplace/corpus.toml`) so changes are proven on more than one board.
- Support parts that ride with a fixed module and overlap it are never moved by the floorplan (needs a release option).
- The pins stage released fixed standoffs and moved them.
- Exit-code table: one meaning per code across commands.
- Hook the KiCad-open lock guard into every board-saving command, not only the hand-route ones.
