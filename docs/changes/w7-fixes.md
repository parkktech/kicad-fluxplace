# W7 tool fixes

## Stderr filter deadlock

Root cause: KiCad's native warning writer retains Python's GIL while writing to the filter pipe. When the pipe filled, the
Python reader thread could not reacquire the GIL to drain it, deadlocking both threads. The reader is now a helper process
with an independent GIL. Filtering remains opt-in with `FLUXPLACE_QUIET_STDERR=1`.

Measured on copies of the W7 boards (wall seconds; pre-fix filtered `open-nets` timed out at 180 s):

| command | filter off | filter on, fixed |
|---|---:|---:|
| `open-nets` on stackup | 6.48 | 5.97 |
| corrected RF `probe` on stackup | 2.09 | 2.09 |

The synthetic native-warning reproduction timed out at 10 s before the fix and completed in 0.19 s after it.
Regression: `tests/test_quietlog.py::test_native_warning_writer_does_not_deadlock_reader`.

## Budgets and guarded-edit timeouts

`via-min` had no deadline, so its DRC bisection could run indefinitely and lose every accepted in-memory change when killed.
It now defaults to 3 s per candidate (minimum 60 s), accepts every completed DRC verdict, labels untouched vias
`not_reached`, and always writes the output and requested JSON report; partial output exits 0.

`lay`, `shove` and `copy-net` now have a 600 s default hard timeout. A child process performs the edit; timeout restores
all destinations (including session state), reports `no verdict: timed out after S s, nothing written`, and exits 2.
`hand-repair` checkpoints its report after each attempt and in a finally path, including budget exhaustion and errors.
Regressions: `test_via_min_budget_writes_verified_prefix_and_marks_remainder`, the two tests in `test_hrcli_timeout.py`,
`test_repair_zero_budget_still_writes_report_and_commits`, and `test_repair_exception_still_writes_partial_report`.

## Constraint-driven via sizes

Fan-out and breakout could emit 0.45 mm vias without applying the route/fab resolver; breakout also serialized a per-pin
legacy diameter. Hand-route constraints ignored `[route]` via/drill floors, and plane/pour/repair tie paths had local
defaults. These creators now use `routerules.rules` (or its resolved rule record), including open-board project rules.
Breakout serialization requires the resolved diameter and has no 0.45 mm fallback. Regression coverage is in
`test_params_use_route_and_fab_via_resolver`, `test_lay_writes_locked_lanes_and_vias`,
`test_route_constraints_raise_hand_route_via_floors`, `test_tie_vias_use_route_rule_sizes`, the enlarged floating-plane
tie test, and `test_open_board_project_via_floor_uses_shared_resolver` (55 selected tests passed).

## Router project rules

The KRT chain carried a project only when the destination did not have one, allowing router-relaxed rules to replace the
approved input. After every successful external step, `_restore_project` now restores the input `.kicad_pro` byte-for-byte,
records the changed sections per step, and adds `router project rules discarded: ...` to the result. Router commands carry
the resolved approved via size/drill. `test_router_relaxed_project_is_discarded_after_every_step` uses a fake router that
relaxes via, hole and net-class rules at all three steps (17 module tests passed).

## Suggest endpoint crash

A net-only suggestion passed `None` endpoints into `hrgeom.pip`; it now selects the closest distinct island pair through
`_auto_endpoints`, rejects one-sided explicit endpoints cleanly, and the CLI converts any internal exception into a normal
not-found result with its reason. The W7 net-only case changed from a traceback in 5.76 s to `suggest found: 2.049 mm, 0
via(s)` in 1.66 s. Regressions: `test_net_only_suggest_chooses_closest_distinct_islands` and
`test_suggest_internal_error_is_normal_not_found` (15 selected tests passed).

## Actionable suggest brief and progress follow-up

Suggest's brief success line omitted its path, and `--brief --json` returned before emitting JSON. A found brief now includes
`path ...`; for suggest only, both flags put the brief on stderr and the complete JSON object (including `board_rev`) on
stdout. Other commands retain their prior brief behavior. `progress checkpoint` now prints the documented
`fluxplace progress brief --workdir W` follow-up. Regressions: `test_suggest_brief_json_emits_path_and_full_object` and the
updated `test_cli_checkpoint_block_show_brief` (29 selected tests passed). The real W7 suggestion emitted an identical path
in both forms and completed in 1.64 s.

## Pair-only lock override

`pairs --only` had no supported way to re-route pipeline-locked pair copper. New `--unlock` (off by default) removes lock
markers only from the named pair's temporary working text, records `unlocked_items` and original locked descriptors per
pair, requires the accepted replacement to be locked, and sets `relocked`. Failures never modify the working original, so
locks and geometry are restored byte-for-byte. Without the flag the refusal now names `--unlock`. Regressions:
`test_unlock_reroutes_and_relocks_named_pair`, `test_unlock_failure_restores_original_locks_and_geometry`, and the updated
locked-refusal test (37 pair/pair-only tests passed).

## Complete failed-stage residuals

Floorplan/fanout failure summaries were deltas from the placement search, not a residual audit of the output board. Failed
stages now run `placementcheck.residual` on their actual output and attach complete outside, overlap, keep-out, no_tht,
hardware, module, pad-short and hole lists to `residual_violations`. Every item is appended to `stage.log` and written to
`placement-residual.json`, with no top-N truncation. Regressions in `test_placement_residual.py` include a 14-item group
and result/log equivalence (40 selected tests passed).

## Measured route connectivity

Route status repeated router-log `unrouted` counters and diagnosis used “2+ pads, no copper,” both unrelated to actual
connectivity. `diagnose_result` and direct `route-diagnose` now call `routeconn.open_by_net` on the current output, put a
`connectivity` object and `CONNECTIVITY` line in the result, and diagnose the measured open nets. Log counters are labelled
`router-log ... (not connectivity)`; unmeasured status says `open connections not measured`. Regressions:
`test_diagnose_uses_kicad_connectivity_not_copper_heuristic` and `test_status_log_counter_is_not_claimed_as_open_count`
(25 selected tests passed). On the real snapped board the report measures four opens with their four net names in 5.81 s.

## Tune identity and snap clearance guard

The tune CLI refilled and saved zones even with zero accepted tracks, changing the board and its DRC result. Both tuning
modes now report `accepted`; `_tune_refill` runs only for a positive count, so no-acceptance output is byte-identical to
the input. Snapiter used the correct input baseline but inherited drcguard's Zone-item noise exclusion, which hid new
clearance errors. Its `_keys` now re-includes zone-related clearance keys while leaving other flicker filtering intact.
Regressions: raw-byte assertion in `test_nothing_accepted_copies_the_input`,
`test_cli_skips_zone_refill_when_no_track_was_accepted`, and `test_zone_clearance_increase_is_not_ignored_by_snap_guard`
(19 selected tests passed).

## Guarded pour-island repair

There was no guarded tool for a same-net pour island left open by routing. New `pour-island BOARD --net N --out O` searches
nearby sites, requires the via to touch same-net filled/pad copper on at least two layers, uses resolved via/drill rules,
and applies the normal lay guard so the target open count must fall and no new DRC key or other open is accepted. It does
not delete islands or write on failure. `test_guarded_stitching_via_joins_open_pour_island` proves one via closes a
synthetic island; `test_net_without_pour_is_refused_without_output` proves the refusal path (2 tests passed).

Follow-up: an unreadable failed-stage output no longer lets the residual audit replace the original exception/verdict;
`residual_error` is recorded and the original FAIL is preserved (`test_unreadable_stage_output_keeps_original_failure`).
