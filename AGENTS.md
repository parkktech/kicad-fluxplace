# fluxplace for AI agents (read only this)

Context is scarce: commands answer with results; read nothing else. Unsure: `fluxplace doctor`.

## Output rules
- Analysis commands print a verdict line, counts, worst 10 (`--top N`) and the full-report path
  (`<board stem>.<cmd>.txt/.json`; read only if needed). `--full` = all, `-q` = verdict line.
- Exit: 0 ok, 1 FAIL. `route-wait`: 2 timeout, 3 no status file.
- `fluxplace help-brief CMD` = 15 lines per command (alone: one line each). Skip README/docs/commands.
- Never time-box a whole-board router call: use batch routing (`docs/commands/route.md`).
- Never overwrite the input board (`--out`). One agent owns a board/workdir.

## Commands by task
| task | command | flags that matter |
|---|---|---|
| whole flow | `pipeline BOARD --workdir W` | `--constraints C --spec S --from/--to STAGE --status --route-budget S` |
| board map (read it, not the board) | `map BOARD` | `--constraints --show SEC --ref/--net/--group --render O.png` |
| can it route? | `plan-route BOARD` | `--ignore-routing`; `plan-compare`, `plan-options` |
| ready to route | `route-ready BOARD` | `--constraints --spec --waive CODE:REGEX --all` (+PLAN_*) |
| area budget | `floorplan-check BOARD` | `--constraints --spec` |
| silkscreen | `silk BOARD` | `--fix --out O` |
| tree/chain templates | `templates BOARD` | `--constraints --spec --out O` |
| rotate connectors | `anchor-check BOARD` | `--apply --out OUT` |
| support parts to ICs | `clusters BOARD` | `--apply --out OUT` |
| place | `place --board B --strategy routable --out O` | `--constraints --spec --objective plan` |
| why nets did not route | `route-diagnose BOARD` | `--log LOG --nets a,b --constraints --spec` |
| wait for a router | `route-wait STATUS` | `--timeout S --until done\|stall\|checkpoint` |
| one line on a run | `route-status BOARD_OR_STATUS` | |
| design review | `--no-sourcing review --board B` | `--spec --constraints --no-api --waive --fail-on` |
| diff-pair tuning | `tune --board B --out O` | `--constraints` |
| part count | `bom-opt --spec S` | `--constraints` |
| docs/sourcing | `spec-check --spec S`, `sourcing --board B`, `datasheets` | `--no-api` offline |
| fab package | `fab`, `deliver`, `pcbway` | run `review` first; it must pass |
| toolchain, context budgets | `doctor`, `doctor --context` | |

`--constraints` = constraints TOML, `--spec` = spec JSON. Global flags (`--no-sourcing`, `--full`, `-q`,
`--top`) go before the subcommand; the last three also work after it.

## Pipeline
`docs/AGENT-PIPELINE.md`: one subagent per stage, exact commands, ready-to-paste briefs, stop conditions.
Stages: floorplan-check, anchor-check, place (`--objective plan`), route-ready (gate, runs plan-route), route, tune, review.
Order: map, place, plan-route gate, route, audit. Hand off `W/state.json` (<2 KB) + the stage command; read map slices, not boards. Route only after the gate passes or each failure is justified.

## MCP
Tools mirror the commands (`fluxplace_<cmd>`). `fluxplace_run` runs any other by name (`templates`, `plan-route`, ...).
