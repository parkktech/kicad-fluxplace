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
