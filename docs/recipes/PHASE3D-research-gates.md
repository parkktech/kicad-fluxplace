# PHASE3D — research-backed gates before the W14 runs (reports/PCB place and route methodology.md)

Worktree /var/kicad/fluxplace-wt/phase3a (branch phase3a). Board-agnostic code only. Do not commit. No routers.
Start AFTER PHASE3C is in the tree (gaps.py has the `pairgroup` rung). Re-find code by name.

## 1. Never-worse gate on power, high-speed pairs and RF (gaps.py)
`score()` is compared lexicographically, so power (slot after opens) can be traded for an open. Add a hard rule in
`gaps._accept` BEFORE calling `drcguard.accept`: with `sb = score(base)`, `sc = score(cand)` by name
(hs_pair, rf, power, power_mag), refuse with `why = "R6 <term> worse <a>-><b>"` when any of hs_pair, rf, power,
power_mag is higher in the candidate. No exceptions. Give `score` a helper
`score_terms(m, gaps) -> dict` so names are not index-coupled; `score()` returns `list(score_terms(...).values())`.
Tests: candidate with fewer opens but power +1 is refused (R6 power); equal or lower terms pass to the old rules.

## 2. Refill and save every candidate before it is measured (ledger 2026-11-19 FALSE errors row)
In `gaps.close.attempt`, right before each `measure(out|final, ...)`, call `opengate.refill(path)` (find the existing
refill helper: `grep -n "def refill" fluxplace/opengate.py fluxplace/planes.py`; use the one that loads, fills all zones
and saves the file). Same before the initial `measure(best)`. Guard with try/except: a refill failure is a refused
candidate (`why = "refill failed"`), never an accept. Test with a monkeypatched refill that records calls.

## 3. Stop after 5 rounds without gain (gaps.py)
In `close()`'s round loop track the best score; if 5 consecutive rounds end without an accepted candidate that lowered
the score, stop with `stop = "no gain 5 rounds"`. Constant `NO_GAIN_ROUNDS = 5`, overridable by `[flow] no_gain_rounds`.
Test with a fake rung that never improves: loop ends after 5 rounds, stop reason set.

## 4. Tune right after pairs, before bulk routing (pipeline.py)
`CONSTRUCTIVE` currently ends `..., "route", "close", "tune", "silk", "review"`. Move `"tune"` to right after `"pairs"`
(`..., "pour-tie", "pairs", "tune", "critroute", ...`) and keep a second pass by adding `"tune"` back after `"close"`
only if the stage machinery allows a stage name twice; if it does not, keep tune once (after pairs) and say so.
Check the tune stage locks the tuned pair copper (grep its stage function for `lock`); if it does not, lock the tuned
pairs' tracks/vias at the end of the stage (boardtext/boardlock helper that already exists — find it). Tests: the
CONSTRUCTIVE order; tests that assert the old order are updated.

## 5. Report under 30 lines
`git diff --stat`, tests (run tests/test_gaps*.py tests/test_pipeline.py tests/test_krtchain.py tests/test_tune*.py and
any file importing gaps/pipeline/tune that does not launch routers), deviations.
