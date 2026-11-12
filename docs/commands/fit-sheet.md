# `fit-sheet`

## Board on the sheet — `fit-sheet`

A board whose coordinates start near (0, 0) or go negative opens half outside
the KiCad page frame. `fluxplace fit-sheet --board B [--out O] [--paper auto|A5..A0|A..D]
[--constraints C.toml] [--dry-run] [--refill]` translates the **whole** board
(footprints, tracks, vias, arcs, zones incl. fills, rule areas, drawings incl.
Edge.Cuts, text, dimensions, groups, aux/grid origin) by a whole-millimetre
offset so the Edge.Cuts bbox is centred above the title block (no outline: the
footprint extents, and it says so). Every moved point is checked to have moved
by exactly the offset or nothing is saved. A second run reports "already fitted".

- **Paper.** `auto` keeps the file's paper if the board fits, else the smallest
  size of the same series (ISO, or US A-D) that fits. pcbnew 10 does not wrap
  `PAGE_INFO`, so the `(paper ...)` token is patched in the saved file.
- **Geometry** (measured from a kicad-cli SVG export; the default sheet is compiled
  into KiCad): usable area = 15 mm from every page edge (10 mm frame + 5 mm);
  title block 108 x 32 mm at the inner frame's bottom-right (12 mm), keep-out
  grown by 5 mm. A tall board that cannot sit above it is centred beside it.
- **Automatic** at the end of the outline stage of `place`, `auto` and `compact`
  and on the imported winner of `tournament --apply-winner`; `--no-fit-sheet`
  skips it. Copper-only commands (`finish`, `repair`, `tune`, `drc-fix`, `patch`,
  `sync-nets`, `replace-footprint`) never move anything and print
  `board is off the sheet — run \`fluxplace fit-sheet\`` when it is.
- **Review.** `SHEET_OFF_PAGE` (WARN; FAIL with `[sheet] enforce = true`): outline
  outside the usable area or on the title block. Facts: `page`, `outline_bbox`.
- **Constraints.** `--constraints C.toml` shifts `[lock.at]` rows and `[[keepout]]`
  `rect = [...]` in place (text edit, comments kept) and appends a
  `# fit-sheet <date>: ... shifted by (+dx, +dy) mm` line. Build commands only
  print the offset; apply it with `--constraints C --shift DX,DY`.
