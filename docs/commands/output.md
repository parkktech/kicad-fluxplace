# Compact output

## Compact output (default for analysis commands)

`review route-ready route-diagnose clusters anchor-check bom-opt lint verify-models sourcing datasheets spec-check drc-scope eval`
print at most ~40 lines / 3 KB: a one-line verdict (`<cmd>: ok|FAIL (exit N) - summary`),
counts per code, the worst N items (`--top N`, default 10, at most a few per code) and the path of the
full report written beside the board (`<board stem>.<cmd>.txt` and `.json`). `--full` (`--raw` for
`drc-scope`, whose own `--full` re-runs DRC) restores the complete output; `-q/--quiet` prints only the
verdict line. Exit codes are unchanged. The flags go before or after the subcommand and apply
identically through the MCP tools. Output already under 3 KB is shown whole after the verdict line.
