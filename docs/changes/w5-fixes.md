## w5-fixes

- `anchor-check --apply` wrote a wrong orientation: `moves_for_best` re-scored after each turn and, since
  `rotations` are deltas from the current angle, kept turning the same anchor again (up to 20 times). Each
  anchor now turns at most once. The written board is written to a temp file, re-read and checked
  (position, angle, courtyard, inside the outline) against the analysed pose; on a mismatch the command
  exits non-zero and writes nothing (`anchors.verify_written`).
- An anchor (or group) whose `rotations` lists exactly one non-zero rotation is applied when feasible,
  regardless of `anchor_tolerance_pct`. A feasible better rotation skipped for the threshold is reported:
  `skipped J2 rotation 180: gain 29.4% is below anchor_tolerance_pct 15%`.
- `place --strategy routable --refine` crashed with `KeyError` in `preroute.congestion`: a pad beyond the
  outline's grid indexed a cell that does not exist. Such pads block no cell, ratsnest ends are clamped to
  the edge cell, and the new WARN `PLACE_OUTSIDE_OUTLINE` (plus `congestion.outside_refs`) names the parts.
- `parts-fetch A,B` splits comma-separated MPNs (and still accepts spaces); no MPN with a comma is relied on.
