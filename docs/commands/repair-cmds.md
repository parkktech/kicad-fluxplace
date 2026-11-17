# Repair commands (`copy-net`, `via-min`, `repair`)

Same guards as `lay`: `--expect-rev`, KiCad-open lock check, DRC "no new violation key" + connectivity guard, nothing written on failure.

## copy-net SRC DST --net N [--out O]

Replaces the copper (tracks, arcs, vias) of net N on DST with SRC's copper of the same net name. DST's copper of N is removed
first and the command refuses if any of it is locked. Items are rebuilt on DST's own net (pcbnew `Duplicate()` keeps the source net
codes). The copied count must equal SRC's count. Inside a session omit `--out`: DST is the session WORK board.

## via-min BOARD --out O [--constraints C]

Raises EVERY via whose diameter is below the floor (project min via size raised by the constraints/fab profile) to that floor; drill
unchanged, locked vias skipped. All raises are tried as one batch; a batch that adds any DRC violation key (multiset rule of
`drcguard`) is bisected until single vias are kept or rejected. Output counts: changed, kept (already >= floor), rejected, locked_skipped.
