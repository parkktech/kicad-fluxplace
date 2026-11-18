# calls

`fluxplace calls [--tail N] [--failed] [--json] [PATH]` prints recent fluxplace invocations, one line each:
timestamp, exit code, duration, argv, first line of the output. Read-only.

Every CLI call appends one JSON line (`ts, cmd, argv, cwd, dur_s, rc, summary`) to `.fluxplace/calls.jsonl` beside the
first board/project (else first folder/file) named in the arguments, else `~/.cache/fluxplace/calls.jsonl`.
`PATH` is a board, a folder or a `.jsonl`; omitted: `./.fluxplace`, else the cache.

- argv goes through `fluxplace/redact.py`; the value after `--api-key/--token/--password/--secret` is masked.
- `summary` is the first 500 chars of stdout (redacted); a raised exception is appended.
- Capped at about 5 MB (`FLUXPLACE_CALL_LOG_MAX` bytes); one old file `calls.jsonl.1` is kept.
- A write failure never fails the command. Only the outermost call logs (a pipeline's inner calls do not).
- Off: `FLUXPLACE_NO_CALL_LOG=1`; automatically off under pytest unless `FLUXPLACE_CALL_LOG=1`.
- `--failed`: only non-zero exit codes.

# Quiet log (stderr noise filter)

pcbnew can print its property-enum and `PCB_VIA::GetWidth called without a layer argument` assertions thousands of times
(one W8 log reached 176 MB). `fluxplace/quietlog.py` drops both:

- the CLI filters its own fd 2 (pipe + helper-process reader, restored at exit, also on exceptions); one line
  `[fluxplace] dropped N repeated KiCad assert lines` is printed at the end. Other lines are never dropped.
- `quietlog.filter_file(path)` / `filter_text(text)` clean external-router, stage and helper logs without unbounded memory.
- Known-noise patterns are the list `quietlog.NOISE`; add more there.
- Console filtering is opt-in with `FLUXPLACE_QUIET_STDERR=1`: the W8 spawn-based session-lay benchmark was over 20%
  slower with it enabled. `FLUXPLACE_RAW_STDERR=1` forces all filtering off; automatically off under pytest. External-router,
  stage and helper log filtering is on unless raw mode is set.
