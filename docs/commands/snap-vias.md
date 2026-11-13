# snap-vias

`fluxplace snap-vias BOARD --out O [--no-check] [--drc]`

Moves every track end that lies inside a via's copper on the same net (and a layer the via reaches) to the via centre. Track
ends at the same point as a moved end (joints) move with it; a stub left with both ends in one via is removed; arcs and
foreign-net vias are left alone. Run after every routing merge (`routepar`). KiCad reports the unsnapped ends as
`track_not_centered_on_via`.

`--no-check` skips the open-connection comparison (pcbnew, zones refilled; a change in the count exits 1); `--drc` prints the
`track_not_centered_on_via` count before/after and DRC error types that grew (zone-fill noise of +-10 is normal).
