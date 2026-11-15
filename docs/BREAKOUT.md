# Breakout: escape planning inside placement

`fluxplace/breakout.py`. A part whose pins cannot take a via beside the pad at the board's via size and clearances owns an
ESCAPE PLAN TEMPLATE, derived from footprint geometry and the rules (no part names): a side of >= 3 SMD pads is dense when the
lateral pad pitch is under pad width + via + 2 clearance, or when it has more than one pad row.

## Template (per pad side)
- lane: straight out from the row on the pad's layer (`[breakout] lane_start_mm`, 0.15), then a 45 deg jog to its lateral slot
  when the lane pitch is under via/2 + clearance + track/2 (slots widened about the row centre).
- via field: m staggered rows beyond the row, m = ceil((via + clearance) / lane pitch); pin i -> row i mod m; row spacing keeps the
  via-to-via clearance of neighbouring rows. 0.4 mm pitch, 0.45 via, 0.127 clearance: m = 2, depth about 1.5 mm.
- a pin whose net runs only on the pad's layer ends in a surface lane (no via); plane / pour / power pins get their via in the field.
- breakout region per side: lanes + via sites (+ clearance); depth beyond the pad row, width along it.
- floors: `[plan]` / `[fab]` track and clearance, the fab profile's via and drill minimums (never below them).

## Placement
- `floorplan`: the isolated regions are claims of their module (`claims_ok`): another module's pads and holes may not sit in them
  (lane region on the pad layer, each via site on both), and two dense parts' regions may not overlap. Cost term `breakout`
  (`[floorplan] breakout_weight` 6.0, `breakout_apron_mm` 2.0): the region's outward apron charged for board edge / keep-out and
  for other modules' regions, so orientations facing open board win. Pinned parts keep their region reserved; conflicts follow the
  pinned-conflict precedence.
- `place` / legaliser (`escreserve.py`): the hard band of a fine-pitch / multi-row part is the template's reach beyond the courtyard.
- `modules` prints the isolated template summary.

## Gate (end of `floorplan`, before fan-out)
`solve` plans every dense part ON THE WHOLE BOARD: each lane and via is checked, with exact distances, against every other part's
pads, holes, keep-outs, the outline, foreign copper and the lanes already planned; a pin tries its row and +-0.3 mm shifts, then
deeper rows. A pin with no candidate fails placement: `BREAKOUT_PIN_BLOCKED REF.PIN NET ... BLOCKER short by X mm`.
`--allow-incomplete` / `[floorplan] waive_fail` do NOT waive it; a named pin in `[breakout] waive = ["U5.1"]` does
(`BREAKOUT_PIN_WAIVED ... - WAIVED`). Report: `<floorplan>.floorplan.json` key `breakout`.

## Fan-out
`fanout` solves the same templates on the input board and lays them FIRST as locked lanes + vias (rows inner to outer, pins sorted),
then runs the generic escape on what is left (laid pins count as escaped). `--no-breakout` skips. Anything still failing is reported
by the existing classes (blocked / sealed / surface-only).

    [breakout]   enabled = true   lane_start_mm = 0.15   max_extra_rows = 3   waive = []
