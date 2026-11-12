# fluxplace for AI agents (the only file you must read)

Context is the scarce resource. Commands answer with results; read nothing else unless the summary is not enough.
Run with the interpreter that has pcbnew: `fluxplace CMD ...` (or `python cli.py CMD ...`). Unsure: `fluxplace doctor`.

## Output rules
- Analysis commands print: one verdict line, counts per code, worst 10 (`--top N`), and the path of the full
  report (`<board stem>.<cmd>.txt/.json`). Read that file only if needed. `--full` = everything, `-q` = verdict line.
- Exit codes: 0 ok, 1 FAIL (gate/check failed). `route-wait`: 2 timeout, 3 no status file.
- `fluxplace help-brief CMD` = 15 lines on any command. `help-brief` alone = one line per command. Do not read README or docs/commands.
- Never tail router logs. `fluxplace route-status X` (1 line) / `fluxplace route-wait X --timeout 900` (blocks).
- Never overwrite the input board: use `--out`. One agent owns a board/workdir at a time.

## Commands by task
| task | command | flags that matter |
|---|---|---|
| whole flow, staged | `pipeline BOARD --workdir W` | `--constraints C --spec S --from/--to STAGE --status --route-budget S` |
| is it ready to route | `route-ready BOARD` | `--constraints --spec --waive CODE:REGEX --all` |
| rotate connectors | `anchor-check BOARD` | `--apply --out OUT` |
| support parts to ICs | `clusters BOARD` | `--apply --out OUT` |
| place | `place --board B --strategy routable --out O` | `--constraints --spec` |
| why nets did not route | `route-diagnose BOARD` | `--log LOG --nets a,b --constraints --spec` |
| wait for a router | `route-wait STATUS` | `--timeout S --until done\|stall\|checkpoint` |
| one line on a run | `route-status BOARD_OR_STATUS` | |
| design review | `--no-sourcing review --board B` | `--spec --constraints --no-api --waive --fail-on` |
| diff-pair tuning | `tune --board B --out O` | `--constraints` |
| part count | `bom-opt --spec S` | `--constraints` |
| docs/sourcing gates | `spec-check --spec S`, `sourcing --board B`, `datasheets` | `--no-api` offline |
| fab package | `fab`, `deliver`, `pcbway` | run `review` first; it must pass |
| toolchain, context budgets | `doctor`, `doctor --context` | |

`--constraints` is the project constraints TOML, `--spec` the netlist spec JSON. Global flags (`--no-sourcing`,
`--full`, `-q`, `--top`) go before the subcommand; `--full/-q/--top` also work after it.

## Pipeline
`docs/AGENT-PIPELINE.md`: one subagent per stage, exact commands, ready-to-paste briefs, stop conditions.
Stages: floorplan-check, anchor-check, place, route-ready (gate), route, tune, review. Hand off only
`W/state.json` (under 2 KB) plus the stage command. Do not start routing until `route-ready` passes or each
failure is justified. Optimisers are time-boxed and keep best-so-far. Report failures with numbers.

## MCP
Tools mirror the commands (`fluxplace_<cmd>`), same compact output. `fluxplace_run` runs any other command by name.
