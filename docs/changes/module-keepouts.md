# module keep-outs, hardware circles, placement-check

- `[placement.hardware]`: automatic keep-out circle per mounting hole (drill table, refs/exclude/allow/diameter_mm). New
  legality reason `hardware` (circle, exact); placers see the bounding square as a `hardware:REF` keep-out.
- `[placement.module.NAME]`: one table that expands to a keep-out and, with `tht = false`, a no_tht rule (`fluxplace/placerules.py`).
- `placement-check`: read-only report of every keep-out, no_tht, hardware and module rule with offending parts and quadrant coverage.
- README command index text narrowed (44 -> 40 columns) to stay inside its byte budget.
Docs: docs/commands/placement-check.md.
