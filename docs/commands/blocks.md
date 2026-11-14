# `blocks` - functional-block inventory

```
fluxplace blocks [BOARD] [--spec S] [--constraints C] [--check] [--write --out B2.kicad_pcb] [--link MM] [--sep MM] [--json]
```

One inventory of functional blocks, used by the schematic (one sheet per block, hidden symbol field `Functional Group`),
the board (hidden footprint field `Functional Group`), placement (`[placement] use_blocks`) and review (BLOCK_SCATTERED).

**Inventory** (`fluxplace/blocks.py: inventory`):
1. `[blocks] "Audio codec" = ["U5", "C5*", ...]` claims parts first (globs allowed). `[schematic.blocks]` is an alias; both merge.
   A ref in two blocks is an error. A pattern matching nothing is a warning.
2. Every other part gets the automatic grouping (`schinv.inventory`: ICs with their supports, merged by shared signal nets).

**Output**: block, `[how]` (`constraints` / `auto` / `mechanical`), count and refs. `--json` for the full record.

**`--check`** (placed board): per block the parts' bounding box, area, number of regions (parts closer than `--link`, default
6 mm, chain together), parts outside the main region, and `BLOCK_SCATTERED` when a region of at least 3 parts (or 15 % of
the block) is separated from the main region by another block's part (within `--sep`, default 3 mm, of the straight line
between them). Mechanical hardware is skipped. The finding is INFO; WARN when `[placement] use_blocks = true`.

**`--write --out`**: sets the hidden `Functional Group` field on every footprint. `--out` is required; the connectivity guard
runs as for every board write (pads, nets, footprints, poses must not change).

**Placement**: `[placement] use_blocks = true` does two things, both off by default:
- `modules` attaches supports only to anchors of their own block; a support whose block has no anchor stands alone. Module records carry `block`.
- `floorplan` adds a cohesion term to its objective: for each block with 2+ modules, the x + y extent of its modules, weight
  `[floorplan] block_weight` (1.0). It pulls a block together; it is not a hard region. Blocks of fixed modules only are not pulled.
`place` (the routable placer) does not read the inventory yet.

**One block concept**: `[placement.blocks]` (the cohesion list used by `placechecks.block_mm`, `fluxmap`, `floorplan` checks) and
`[placement] blocks_dir` keep priority. With neither present, a `[blocks]` table makes `blocks_of` return the inventory above
(claims + automatic grouping). With no `[blocks]` table either, the legacy rules apply unchanged, so placement scores do not change
for boards that do not opt in.
