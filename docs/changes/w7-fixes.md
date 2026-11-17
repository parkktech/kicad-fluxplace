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
