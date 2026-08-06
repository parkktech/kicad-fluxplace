# fluxplace — next session

## Where it stands (v0.1.0, commit 717fa95)
- Core works end-to-end: `analyze` / `plan` / `gather` / `place` / `eval`, GUI ActionPlugin,
  CLI, PCM metadata, README, tests. Pushed to `parkktech/kicad-fluxplace`.
- **Overlap bug fully fixed** (the hard one): correct spatial hash (bbox-covering cells) +
  true footprint bounding box (not pad bbox) + body-center offset tracking. `place()`
  guarantees 0 overlaps and verifies. Ground truth on the CM5 board: 0 real overlaps.
- Default strategy = `flux` (connectivity/wirelength-driven). `pack` bin-packing was
  exiling large connected parts to corners — demoted.
- `legalize()` / `_pack_dir()` / `compact()` support a `frozen` anchor set so big parts
  (CPU module, M.2) keep their wirelength-optimal spot while small parts pack around them.

## Two agreed next tasks
1. **Apply the 2242 M.2 to the REAL board.** The Hailo-8 is M.2 2242 (42mm), but the board's
   J80 uses a 2280-length socket footprint reserving ~88mm — the dominant whitespace source.
   Swap to `Connector_PCBEdge:M.2_2242-xx-M` (pads 1-75 match; drop M1-4/S1-2 mount/shield).
   Working swap script is in scratchpad (base.kicad_pcb -> base2242.kicad_pcb), 67 pads
   re-netted, bbox 22x42mm. This is a razor-cm5-board change, not a fluxplace change.

2. **Implement quadratic global placement + legalization** (the real placement upgrade).
   WHY: pure force-directed floats the hub (CM5) to the PERIPHERY of the small-part cluster,
   because each subsystem is internally cohesive and touches the hub through only a few nets.
   Result: hub far from what it routes to (bad), and whitespace. Pinning the hub center made
   HPWL worse — a band-aid, not a fix.
   PLAN: (a) build the weighted connectivity matrix (already have graph.build with net
   weights); (b) solve quadratic placement (minimize sum w_ij * dist^2) via a sparse linear
   system with the connectors/anchors as fixed pins, OR recursive min-cut partitioning to
   assign parts to board regions; (c) legalize with the existing frozen-anchor legalizer.
   This treats big modules as first-class and gives hub-central, dense, routable layouts.

## Metrics to beat (CM5 board, 178 parts)
- current flux: ~128x179mm, HPWL ~15500, 0 overlaps, but hub peripheral + whitespace.
- target: hub central, fill >55%, HPWL <=15000, 0 overlaps, M.2 adjacent to CM5.

## Don't repeat these dead-ends
- Directional (`_pack_dir`) compaction of ALL parts: tightens area but wrecks wirelength
  (parts shoved away from the hub). Only use with big anchors frozen, and it still trades
  routing for area — keep `compact_gaps` off by default.
- Pinning the hub at the geometric center: made HPWL worse.
- Sizing parts by pad bbox or courtyard alone: use the FULL footprint bbox.
