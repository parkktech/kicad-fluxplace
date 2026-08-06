# fluxplace — next session

## Where it stands (v0.2.0)
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

## Next ideas (in rough priority)
1. **Compaction under the gate** — extent is routability-honest but fill is ~43%;
   try shrinking bounds stepwise + re-running the gate until overflow appears, then
   back off one step (binary-search the smallest routable board).
2. **Decap adjacency pass** — power nets are planes in the graph, so decaps place by
   cluster cohesion only; add a post-pass that walks each decap to the nearest free
   spot touching its IC's power pins (respecting the gate).
3. **Diff-pair coupling** — route P/N of a pair on the same MST topology and score
   pair separation; placement already weights them ×4.
4. **Real-track handoff** — emit the global routes as KiCad rule areas / guide lines
   so an interactive route follows the reserved corridors.

## Don't repeat these dead-ends
- Directional compaction of ALL parts (wrecks wirelength) — only with anchors frozen.
- Pinning the hub at board center (worse HPWL than letting the quad math place it).
- Sizing parts by pad bbox / courtyard alone — use the FULL footprint bbox.
- Boolean THT flag (one mounting hole walled off the whole CM5 module).
- Blocking pin cells at full body blockage (false congestion at every connector).
