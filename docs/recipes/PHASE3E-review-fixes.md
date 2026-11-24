# PHASE3E — Fable review fixes before the W14 runs (code part; the run script is fixed separately)

Worktree /var/kicad/fluxplace-wt/phase3a (branch phase3a, HEAD 57c12ae). Board-agnostic. Do not commit. No routers.
Line numbers approximate; re-find by name.

1. `gaps.py` `_CTX_JSON_KEYS` (~1047): add `"protect"`. When writing the job JSON store `sorted(protect)`; in `_rung_job`
   turn it back into a `set`. Child-process rungs (krt, krt_region, prune) must see the same protect set as in-process
   ones. Test: round-trip a ctx with protect {"A_P","A_N"} through the job JSON writer/reader.
2. `power.py` pour guard (~401): pour rails ONE ZONE AT A TIME (each rail x layer): add zone, refill, measure; keep it
   when shorts and errors do not rise and TRACK opens do not rise (`drcguard.open_split(drc)["track_opens"]` — read
   `open_split` to get the right key); zone-island opens (`Zone [...]` items) are allowed (close's pourisland handles
   them). Record kept/dropped per zone with reason. Also copy the `.kicad_pro` next to the file BEFORE the first refill
   (~393). Test with a stub measure: one zone that adds a track open is dropped, one that adds only a zone-island open is kept.
3. `gaps.py` initial `_remeasure(best)` (~1114): try/except -> fall back to `dict(measure(best, kicad_cli))` and log it.
4. `krtchain.py` pairgroups budget (~264): budget = `pairsonly.PER_PAIR_S * len(todo)` (todo = incomplete pairs), capped
   by what is left of the stage budget. On ANY exception in the pairgroups step, put every leg of every incomplete pair
   in `failed_legs` (never plain-route a pair because the step crashed). A pair reported `rerouted` whose group board
   was discarded counts as failed (its legs go to failed_legs).
5. `krtchain.py` `rail_layers` use (~261): outer-only applies to pour rails only. Split rails into pour rails (outer-only
   under ipc-outer) and the rest (all signal layers): two `rails` steps (`rails-pour`, `rails`), or one step per group
   — keep the names in the step list so logs show them. Test: under ipc-outer, a 0.15 mm rail is routed on all layers.
6. `pairsonly.py` regroup retry budget (~381,388): elapsed is subtracted twice; compute remaining as `total - (monotonic() - t0)` once.
7. `gaps.py` pairgroup rung (~986): budget `pairsonly.PER_PAIR_S * n_pairs` (not the whole remaining close budget), and
   memo key per pair id + rung (not board fingerprint) so an accepted unrelated change does not re-lay every failed pair.
8. Tuned-pair lock: move the lock that `pipeline.st_tune` applies into the `tune` CLI path too (find `cmd_tune` in cli.py
   or the function it calls; after writing `--out`, lock the copper of the tuned pairs with `boardtext.lock_nets`). One
   place, called from both.
9. `gaps.py` no-gain counter (~1276): R4 only accepts strictly lower scores and a round with no accept stops as `flat`,
   so the counter never moves. Make it count rounds whose accepts lowered only the `quality` term (last slot) — i.e.
   no gain in any of the other terms — so it stops slow polishing; keep the test meaningful.

Tests: each touched module's test file, one file at a time (a combined run was OOM-killed). Skip router-launching files.
Report under 25 lines: `git diff --stat`, tests, deviations.
