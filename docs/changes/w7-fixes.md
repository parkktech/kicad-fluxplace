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
