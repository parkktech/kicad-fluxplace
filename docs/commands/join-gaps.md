# join-gaps

`fluxplace join-gaps BOARD --out O [--gap 0.3] [--extra-clearance 0] [--check]`

Closes small same-net gaps: a track end and another track end, pad or via of the same net, in different connectivity
clusters (zones count as connected), at most `--gap` mm apart. A straight segment of the end track's width is added when it
keeps the netclass clearance to all other-net copper on its layer, the hole clearance, the board-edge clearance and stays out
of keep-out rule areas; segments added earlier in the same pass count as copper. Prints closed / skipped with the reason.
Only nets with open connections are looked at. The route stage runs it after every round and at the end.
`--check` prints open connections before/after (pcbnew, zones refilled).
