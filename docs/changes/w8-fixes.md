# W8 tool fixes

## Hand-repair endpoint islands

DRC endpoint UUIDs were truncated to eight characters before model lookup. Those prefixes are not unique on real boards, so a track endpoint could resolve to another net's copper and lose its island. `open-nets` now retains the full UUID and resolves endpoints by full UUID, with net/kind/layer/geometry-constrained fallback. Pad, track, via and zone-piece endpoints are covered by regression tests.

## Guarded copper locking

Hand-laid and copied copper had no supported way to become locked. `lay --lock` and `copy-net --lock` lock new items on acceptance, and `lock-net` locks or unlocks a whole net or explicit item IDs with unchanged geometry/connectivity/DRC keys, `--expect-rev`, the KiCad-open guard and session ledger records. Session commit protects the geometry of originally locked copper while allowing explicit lock-state changes.

## Recorded locked-copper re-escape

A pad sealed by locked copper had no guarded repair-ladder step. `suggest --explore --include-locked` now reports board-locked candidates separately in `locked_explore`. Session-only `shove --allow-locked --ids ...` requires every locked item to be named and records exact geometry before unlocking/removing it. Commit requires the recorded opened connections to be closed and, conservatively, all current copper of every affected net to be locked; original locked geometry on other nets remains protected.
