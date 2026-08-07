# fluxplace — next session

## Where it stands (v0.4.0)
- **Route-aware placement shipped** (`--strategy build`, and the GUI plugin runs it):
  quadratic global solve (hub central) → constructive route-as-you-place builder →
  coarse global-router gate (PathFinder-lite) → congestion feedback loop.
  `place_routed()` in placement.py is the pipeline; quadratic.py / builder.py /
  route.py are the new modules. All tests green incl. solver fallback cross-check
  (numpy vs Gauss-Seidel agree to 1e-4 mm).
- **RAZOR CM5 carrier (178 parts) results:** HPWL 25639 → ~10.4k (old flux ~15.5k),
  extent ~124×114 mm, 0 overlaps, **global-router OVERFLOW 0 (routable)**, hub U1
  dead-central, J80 M.2 welded to the CM5's right edge. J80 on the real board is now
  the compact 2242 (razor-cm5-board CM5-A23).
- Grid capacity model: THT = real drilled pin field (≥5 drills or majority), SMD
  bodies only crowd their own layer, pin cells keep escape capacity (0.35× block).

## v0.3.0 added (all ten improvement items)
- Layer-aware router (H/V split + via cost), escape rings, tiny-passive transparency,
  landing corridors + terminal snapping + tapered wide-net width, stacking clamp.
- Power traces routed with real widths + power springs in quad + power ties in the
  builder order + net-anchor candidates ("the fuse goes right at the connector").
- Rotation auditioning in the builder on verified pin rotation (pin_at).
- Shrink-to-smallest-routable (binary search under the gate), decap adjacency pass,
  --seeds N multi-start. Fully deterministic (several salted-set leaks fixed).
- Route guides on Eco1.User; calibrate cmd (DSN export + freerouting + agreement).

## v0.4.0 added (deep-audit fixes, each bench-gated)
- Post-placement ORIENTATION REFINEMENT sweep (audit: 35 parts misfaced incl. LQFP
  worth 36 weighted-mm; builder-time audition sees only the half-built board).
- Decap pass v2: slots ranked by dist-to-OWNER (v1 sorted by dist-to-current — the
  bug that made it a no-op), batched gate acceptance, extent-clamped; far-decaps
  30 -> 21 (rest are slot-starved; needs swaps).
- Connector edge-flush pass (J60 flush, J50 19.6->10.9mm; slides until blocked).
- Per-axis (anisotropic) shrink on top of the uniform search.
- Corridor tax in builder scoring: a body pays for squatting on committed routes.
- Pipeline order: builder -> feedback -> DECAPS -> SHRINK -> FLUSH -> ORIENT (each
  pass gated, reverts if the router objects).

## Calibration status (freerouting ground truth)
- v2.1.0 jar CRASHES on plane-connected targets (NPE in MazeSearchAlgo) — use 2.2.4+.
- With GND/VIN_PROT zones poured and DEFAULT 0.2/0.2 netclasses, freerouting proves
  DF40 escape impossible (27 fails = exactly the U1-terminating nets) — board-side
  rules problem, NOT placement. With 0.127/0.127 the same nets route (38 left by
  pass 4 vs 122 stuck). Moral: netclasses must be set before judging placement.

## Next ideas (in rough priority)
1. Decap swaps (displace lighter parts) for the remaining slot-starved 21.
2. Escape-aware candidate scoring (pin rows facing walls).
3. Pair-aware placement: keep P/N series caps side by side explicitly.
4. Emit guides as KiCad rule areas (per-net keepouts for interactive routing).
5. Worst-offender relocation sweep (re-audition top detour contributors post-build).

## Don't repeat these dead-ends
- Directional compaction of ALL parts (wrecks wirelength) — only with anchors frozen.
- Pinning the hub at board center (worse HPWL than letting the quad math place it).
- Sizing parts by pad bbox / courtyard alone — use the FULL footprint bbox.
- Boolean THT flag (one mounting hole walled off the whole CM5 module).
- Blocking pin cells at full body blockage (false congestion at every connector).
