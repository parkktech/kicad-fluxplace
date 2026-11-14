# schematic

`fluxplace schematic --spec S.json [--constraints C.toml] --out OUT.kicad_sch` draws a schematic from the netlist spec
(or `--board B.kicad_pcb`: from the copper) and proves it against its source. Exit 1 = FAIL (netlist differs).

## --layout blocks (default)

One hierarchical sheet per functional block, drafted rather than labelled pin by pin:

- **Inventory** (`schinv.py`): `[schematic.blocks] NAME = ["U3", "C3*"]` claims parts first (refs / fnmatch globs, TOML
  order). The rest: every part that is not a 2-pad passive anchors a module, `clusters.ownership` gives an IC its own
  supports, the rest join the module they share most signal nets with; modules merge by shared signal nets (ground,
  rails and buses reaching more than `bus_modules` modules do not glue; same reference-designator series adds a bonus)
  up to `max_parts`; blocks below `min_parts` join their strongest neighbour (signal, else rail). Parts whose pins are
  all ground form "Mechanical". Tunables: `[schematic] max_parts, min_parts, bus_modules`.
- **Sheet** (`schdraw.py`): each non-passive is a box (ground pins bottom, rails top, signal pins left/right in pad
  order; same-net pads stacked on one pin, `GND x5`); each signal pin grows a fan tree of its passives (to ground: down,
  to a rail: up, to another signal: in series, recursing). Leftover passives (decoupling, rail-to-rail) sit under their
  cell. Cells pack in rows, anchor (most pins) in the middle of the first. A grid maze router joins the remaining pieces
  of each net (no overlap with or bend on another net's wire, no body/label/text, crossings allowed and penalised, length
  at most min(70, 1.6 x distance + 12) grid). Ground/rail nets are power symbols; a net leaving the block has one global
  label per piece; an internal net one local label per piece; a pinmap pad on no net gets a no_connect flag (nothing
  else does). 50-mil grid, rotation 0 everywhere, title block per sheet.
- **Root**: sheet symbols + one global label and PWR_FLAG per power net. Next to OUT: `OUT-<block>.kicad_sch`,
  `OUT-fluxplace.kicad_sym` + `sym-lib-table` (so `fluxplace:` symbols resolve), a minimal `OUT.kicad_pro` if none.

## Gate (always, unless --no-verify)

`--no-verify` gives verdict `NOT_VERIFIED` (text and JSON), never `PASS`. Block layout then exits 1 unless `--allow-unverified` is given. Flat layout keeps its old exit 0.

1. **netlist**: kicad-cli exports the root netlist; every net's pin set must equal the spec's (pinmap resolved: the
   spec's pin names become footprint pad numbers), every component present with the spec's value and footprint, single
   pin `unconnected-` nets only for declared no-connects. Any difference = FAIL.
2. **erc**: counts by type (`--severity-all`).
3. **legibility** (`schlegible.py`, read back from the files): WIRE_OVERLAP, WIRE_THROUGH_BODY, LABEL_OVERLAP,
   LABEL_SYMBOL_OVERLAP, TEXT_OVERLAP. Non-zero = WARN.
4. **render**: `OUT-render/` PDF (all sheets), one PNG per page, SVG per sheet (`--render DIR`, `--no-render`).

Report: `OUT.schematic.txt` / `.json` (per block: wires, labels, joins, conflicts = internal self-check, must be 0).

Measured 2026-11-13 (all netlist PASS, legibility 0): UTV V1.6 spec 341 parts / 274 nets -> 16 blocks, ~20 s;
V1.5 150 / 117 -> 11; razor-cm5 board 182 / 131 -> 13; razor01-dig 300 / 296 -> 15; razor01-rf 517 / 411 -> 19.

## --layout flat

The original single sheet: every pin a stub + global label (`schematic.py`), same netlist verify.
