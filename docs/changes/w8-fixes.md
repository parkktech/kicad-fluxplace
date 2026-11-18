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

## Native assertion volume and stderr timing

The second repeated native line came both from fluxplace's own via-width reads and from external tools. Via-specific reads now pass a layer (including the per-model `hrgeom` read and hand-route, plane, snap, tune and via-min call sites), and the via assertion joined `quietlog.NOISE`. Streamed external-router logs skip noise for progress parsing and are filtered on completion/timeout; stage, corpus, Cypress and freerouting log paths use the same filter. `FLUXPLACE_RAW_STDERR=1` preserves raw output.

Console filtering was benchmarked on W8 copies with the via fixes already applied (seconds, raw / filtered): open-nets 9.295 / 9.468; probe 3.297 / 3.341; suggest 3.801 / 3.758; via-min 15.588 / 15.900; real route-stage pipeline with a 0.05 s dry budget 10.22 / 9.18. All corresponding exit codes matched and no via assertion remained. The accepted session lay was 15.705 / 22.733 in the first run and, over three repeats, median 16.202 / 20.644 (+27.4%; ranges 15.685-16.603 / 20.310-23.169). Direct non-spawn lay showed no such penalty, but the required spawn-based command did. Therefore console filtering remains opt-in with `FLUXPLACE_QUIET_STDERR=1`, per the 20% stop rule; router/helper-log filtering remains on unless raw mode is selected.

## Pairs handoff effort report

Bounded handoffs previously showed only the last failure text, so a run could not tell whether more search or fallback effort was useful. Without changing the ladder, each handoff now records overlapping `handoff_reasons`, measured blockers, fallback trial records and configuration skips, plus one `pairs handoff effort:` count line. Applying the report helper to W8 gives: budget reached 4; no corridor 3; escape blocked 14; audit rejected 1; fallback trials skipped by configuration 20.
