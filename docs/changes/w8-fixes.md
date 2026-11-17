# W8 tool fixes

## Hand-repair endpoint islands

DRC endpoint UUIDs were truncated to eight characters before model lookup. Those prefixes are not unique on real boards, so a track endpoint could resolve to another net's copper and lose its island. `open-nets` now retains the full UUID and resolves endpoints by full UUID, with net/kind/layer/geometry-constrained fallback. Pad, track, via and zone-piece endpoints are covered by regression tests.

## Guarded copper locking

Hand-laid and copied copper had no supported way to become locked. `lay --lock` and `copy-net --lock` lock new items on acceptance, and `lock-net` locks or unlocks a whole net or explicit item IDs with unchanged geometry/connectivity/DRC keys, `--expect-rev`, the KiCad-open guard and session ledger records. Session commit protects the geometry of originally locked copper while allowing explicit lock-state changes.

## Recorded locked-copper re-escape

A pad sealed by locked copper had no guarded repair-ladder step. `suggest --explore --include-locked` now reports board-locked candidates separately in `locked_explore`. Session-only `shove --allow-locked --ids ...` requires every locked item to be named and records exact geometry before unlocking/removing it. Commit requires the recorded opened connections to be closed and, conservatively, all current copper of every affected net to be locked; original locked geometry on other nets remains protected.

## Explore connected-run verification

Explore skipped a connected run during its search but reported only the originally named segment, so the emitted path was not valid after that single-segment shove. It now deduplicates the same `_chain` removal unit used by `shove --chain`, exact-probes with that whole unit absent, and reports every UUID. Ordinary candidates exclude runs containing locked copper; locked runs remain separate under `locked_explore`. On the W8 repair checkpoint, the candidate now names eight segments and passes probe after the real eight-item shove.

## Pair-router missed classification

`pairs_missed` treated absence from the external router summary as unfinished even when the bounded pairs stage had completed and locked the pair; route-only state could also omit the earlier report path. The route stage now falls back to `board.pairs.json` beside its input and partitions stage-complete pairs plus pairs whose two legs have real, fully locked copper and no input open into `pairs_already_done`. W8's original list partitions from 32 missed to 17 missed plus the expected 15 already complete.
