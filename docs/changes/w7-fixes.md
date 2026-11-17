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
