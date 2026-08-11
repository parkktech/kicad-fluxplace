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

## Tournament #1 results (2026-08-06/07, RAZOR CM5)
- 12 candidates, all passed the gate at overflow 0; 9 full freerouting sessions.
- WINNER: fill 0.72 / aspect 1.35 / pad 0.6 — 41/444 unrouted, 302 vias, 3463mm.
- All unrouted signals = the two DRC-broken provisional footprints, every time.
- CALIBRATION: truth rank != gate rank (gate #1 -> truth #4; truth #1 was gate #4).
  Truth rewards looser spacing (pad 0.6 won despite worst gate wirelength): the
  gate over-values wirelength, under-values elbow room -> consider scoring util
  headroom, or drop pitch to 0.30 so capacity reads tighter.
- OPS lessons: freerouting settings live in freerouting.json (optimizer.max_passes
  100 @ 0.01% = silent hour-long phase; cap to 8 @ 1.0%); -oit CLI flag is ignored;
  SIGTERM does NOT save the session; via 0.5/0.3 packs tighter than JLC's 0.254
  hole-to-copper -> use via_diameter 0.6 in classes for autoroute runs.

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

## 2026-08-10 late session — auto pipeline overhaul (UTV comms bridge board, 129 parts)
Validated end-to-end on a fresh CM5 carrier netlist. `auto` was effectively broken; now:
placed → outline → planes → route → plane-finalize → DFM → fab in one command.
- **cmd_auto never shrink-wrapped Edge.Cuts** (cmd_place did). Placement landed outside
  the outline; KRT saw every pad off-board and silently routed 0 nets. Fixed: stage [2].
- **KRT routes NOTHING without --nets** (the "no list = everything" comment was wrong).
  Fixed: explicit net list + `--power-nets/--power-nets-widths` from a name/fanout
  classifier (`_classify_power`).
- **New fluxplace/planes.py**: GND pours on all Cu layers (SOLID pad connection —
  thermal spokes starve at fine pitch), collision-safe stitch grid, zone refill,
  `finalize_dfm` (JLCPCB rule floor embedded in board, via clamp, graze-shrink),
  `sync_project_rules` (.kicad_pro OVERRIDES the board at load — must be written too).
  pcbnew gotcha: hold refs to ZONE/SHAPE_POLY_SET/removed items until after Save() —
  GC'd SWIG proxies segfault inside Save.
- **eff_size double-rotated pre-rotated parts**: stored w/h are at `angle0` but every
  call site passes ABSOLUTE angles. Locked 90°-placed connectors were modeled sideways
  (decap walk put caps ON the DF40 pad rows). eff_size now rotates by the delta.
- **--obstacle X:Y:W:H**: phantom locked rects for plug-on module bodies (SoM over its
  two board connectors), enclosure bosses. Stripped before write-back.
- **_legalize_bboxes** post-place overlap nudge (never moves locked parts).
- Rigid connector pairs (CM5: two DF40 at 34.000 mm, pad-1 ends aligned) are pre-placed
  + locked by a project script (razor-01-cm5/hardware/tools/cm5_pair.py). A first-class
  `--rigid-group REF1,REF2@dx,dy` feature would generalize it.

Result on the 129-part board: 92/92 nets, 216/216 pairs, 0 unrouted, 5 residual DRC
items — all KRT plane-finalize vias grazing 0.4 mm-pitch pads by 0.02–0.17 mm.
### Upstream (KRT) issues found
- Plane-finalize places 0.45 vias INLINE with 0.4-pitch connector rows: geometric max
  air 0.075 mm < 0.0975 rule. Wants a diagonal-offset or smaller-via policy near
  fine-pitch pads (graze-shrink in finalize_dfm mitigates the 0.075 cases only).
- **KRT is run-to-run nondeterministic** (same board+args → 0 or 1 unrouted, different
  via spots). Seed or thread-order pin needed before regression-testing against it.

## Back-side module flow (CM5-Minima style) — works; density is now the frontier
- `--obstacle` accepts `X:Y:W:H[:F|B]`; side-aware phantoms verified: back-side
  module shadow blocks back SMDs and THT (pierces both) but frees the entire top.
  129-part board: module zone clean, 0 unrouted (route-out5).
- Board sizes across runs: front-module 109x91, back-module 140x110 — the placer's
  air, not the side choice, is the limiter (the RAZOR benchmark is 124x114 too).
  Target-class boards (65x50) need real density work: harder shrink under the gate,
  denser builder auditioning, and shrink that can pull toward LOCKED anchors.
- `--pad 0.3` is a regression (470 overlap nudges, 2 unrouted): the builder's
  corridors assume ~0.45. Don't ship lower without retuning the route model.
