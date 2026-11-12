# `floorplan-check` - area budget before placing

Usage: `fluxplace floorplan-check BOARD --constraints C [--spec S] [--json OUT]` (compact output: `--full`, `--top N`, `-q`; full report `<board stem>.floorplan-check.txt/.json`).

For each block and each side: courtyard area (+ the escape allowance the legaliser keeps around fine-pitch
ICs and big connectors; a through-hole part counts on both sides) against the free area of the region the
block is assigned to, or of the whole side when it has none, after the outline's corners/cut-outs,
`[placement.keepout.*]`/`[[keepout]]` rectangles of that side, height-limited regions the block's known-tall
parts cannot use, and fixed parts of other blocks. FAIL `REGION_OVERFULL` above `[placement.floorplan]
max_utilisation` (0.7) and when a big part (>= 25 mm2) has no free rectangle its size in its region. Blocks:
`[placement.blocks]` (name = refs/globs), `[placement] blocks_dir = "docs/v16-blocks"` (block JSONs), spec
`block` field, or `block_refdes = true`. Regions: `[placement.region.NAME] rect, side, blocks`. Pure text,
sub-second on a 337-part board (0.5 mm grid, `cell_mm`).
