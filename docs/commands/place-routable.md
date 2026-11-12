# `place --strategy routable` and legalise

### 4. Connection-driven placement — `place --strategy routable`

    fluxplace place --board B.kicad_pcb --strategy routable --constraints C.toml --spec S.json --out OUT

Text-only (no pcbnew). Fixed: locked parts, `[lock.at]`, mounting hardware, detected
anchors, `[placement] fixed`. Misoriented anchors are first turned to their best
rotation (`[placement] fixed` anchors excepted; give a group `rotations = [0, 180]`).
Every confident support part is laid out beside its own IC as a macro cell, so a unit
(IC + support) moves as one rigid body (units whose box is > 2.5x their parts' area
move piecewise). Units go to the class-weighted centroid of their external nets (rails
excluded), are rotated 0/90/180/270 when that cuts weighted length plus crossings
among their own nets by > 8 %, and parts in a chain of 2-pad nets between two fixed
anchors are ordered monotonically along the line between them; 3 rounds of
{25 sweeps, rotate, legalise (stage 5)}. Prints the route-ready scorecard before and
after. Result is deterministic. `auto --pre-place routable` and
`tournament --pre-place routable` run it first. Existing copper is not moved.

See also [templates.md](templates.md) (`[placement.template.*]`, chain detection) and [floorplan-check.md](floorplan-check.md).

### 4d. Local refinement - `place --strategy routable --refine [--seed N] [--time-limit S]`

For an already good, dense placement the global rounds of `routable` can score worse than the input and
change nothing. `--refine` (`fluxplace/refine.py`) starts FROM the input and only makes local moves, each kept
only when the gate score (`preroute.score`, the single objective) improves and `legality.Legal` finds no
violation the input did not already have: nudge a unit (IC + support parts, or a single part) <= 3 mm
(toward its weighted pull first, then 8 compass directions; 3 / 1.5 / 0.75 mm), rotate a unit +-90 degrees,
slide a support part along its IC's edge (0.5 / 1 / 2 mm), swap two same-footprint parts, swap two units of
similar size (area within 25 %) - the swaps only when each side is pulled toward the other's seat. A cheap local filter
(class-weighted length + budget excess of the touched nets) discards hopeless candidates before the full
score is computed (~0.09 s on 337 parts). Passes repeat until one keeps nothing, `[placement] time_limit_s`
(600) or `max_passes` (30). Deterministic for a given `--seed` (visiting order); fixed/locked parts never
move. Sequences of kept nudges can add up to more than 3 mm per unit.

### 5. Legalise with channels — `fluxplace/legalise.py`; the `pack` fault

`legalise(B, targets, fixed, cons, hot)` removes overlaps by pushing movable parts
along the axis of least penetration (relative order survives), never moves fixed or
locked parts, handles both board sides (through-hole parts occupy both), and keeps
clear: fine-pitch ICs (pitch <= 0.5 mm) inflated per side by `max(0.6, 0.06 x pins
on that side)` mm; connectors with >= 40 pads 1.5 mm beside the pad rows;
track-forbidding rule areas (their own part, e.g. the antenna, exempt); `[[keepout]]`
rectangles (through-hole always, SMD when the keepout names the side and has no
`max_height_mm`; `allow` globs exempt); and corridors, i.e. cells where the coarse
demand map exceeds capacity (passives are lifted out when a legal seat is near).
It raises `PlacementError` instead of returning a part outside the outline or an
overlap it could not remove.

**The `pack` fault (> 1 m):** `placement._compact_blocks` pulled each block toward its
neighbours' centroid and pushed overlaps apart asymmetrically; neither has a fixed
point, so the whole arrangement walked ~4 mm per sweep (2 m after 500 sweeps on the
337-part board, 12 sheet blocks) while locked parts stayed at their true
coordinates and `place` shrink-wrapped the outline around both. Fixed by holding the
area-weighted centroid every sweep and falling back to the shelf layout when
compaction ends bigger than it started. Also: `place` now centres on the board when
parts are locked, and **every** path (`place` all strategies, `auto`) calls
`placement.check_extent` before writing: with a fixed outline (`--no-outline`) a part
outside it, with a redrawn outline a result more than `--max-growth` (4; `auto` 6) x the
starting area, or > 1 m, aborts with `REFUSED` and writes nothing.
