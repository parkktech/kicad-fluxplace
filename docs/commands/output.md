# Compact output

## Compact output (default for analysis commands)

`review route-ready route-diagnose clusters anchor-check bom-opt lint verify-models sourcing datasheets spec-check drc-scope eval`
print at most ~40 lines / 3 KB: a one-line verdict (`<cmd>: ok|FAIL (exit N) - summary`),
counts per code, the worst N items (`--top N`, default 10, at most a few per code) and the path of the
full report written beside the board (`<board stem>.<cmd>.txt` and `.json`). `--full` (`--raw` for
`drc-scope`, whose own `--full` re-runs DRC) restores the complete output; `-q/--quiet` prints only the
verdict line. Exit codes are unchanged. The flags go before or after the subcommand and apply
identically through the MCP tools. Output already under 3 KB is shown whole after the verdict line.

## Where report files go

Default reports (`<board stem>.<cmd>.txt/.json`; also `parts`, `ready`, `part-diff`) follow one rule:
1. `--report-dir DIR` (global, before or after the subcommand) or env `FLUXPLACE_REPORT_DIR` sends them to DIR (created).
2. Else a command given an explicit `--json FILE` / `--out FILE` writes them in that file's directory.
3. Else beside the input board/spec (unchanged); `parts` and `-q` runs name each file written as `report: PATH` on stderr.

Not redirected: guard ledgers (`<out>.ledger.json`, beside `--out`), the call log (`.fluxplace/calls.jsonl`,
`FLUXPLACE_NO_CALL_LOG=1`), `.fluxmap.json` answer maps, `.lcsc_cache.json` and the parts API cache beside the spec/mpn map.
