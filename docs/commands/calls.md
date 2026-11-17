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

pcbnew prints `property.h(..): assert "m_choices.GetCount() > 0" failed ...` from C code thousands of times
(one log reached 146 MB). `fluxplace/quietlog.py` drops it:

- the CLI filters its own fd 2 (pipe + reader thread, restored at exit, also on exceptions); one line
  `[fluxplace] dropped N repeated KiCad assert lines` is printed at the end. Other lines are never dropped.
- `quietlog.filter_file(path)` / `filter_text(text)` clean a subprocess log after it finishes (used for the Cypress log).
- Known-noise patterns are the list `quietlog.NOISE`; add more there.
- Off: `FLUXPLACE_RAW_STDERR=1`; automatically off under pytest.
