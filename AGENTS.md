# fluxplace for AI agents (read only this)

Context is scarce. Unsure: `fluxplace doctor`.

## Output rules
- Analysis commands print a verdict line, counts, worst 10 (`--top N`) and the full-report path
  (`<board stem>.<cmd>.txt/.json`). `--full` = all, `-q` = verdict line.
- Exit: 0 ok, 1 FAIL; `route-wait` 2 timeout, 3 no status. Other commands: 3 = open questions
  (`<board>.questions.json`): relay to the user, wait; never answer them, never `--assume-defaults` for deliverables.
- `help-brief CMD` = 15 lines per command (alone: one each). Skip README/docs/commands.
- Never time-box a whole-board router call: use batch routing (`docs/commands/route.md`).
- Never overwrite the input board (`--out`). One agent owns a board/workdir.

## Commands by task
| task | command | flags that matter |
|---|---|---|
| whole flow | `pipeline BOARD --workdir W` | `--constraints --spec --flow --from/--to --status --route-budget` |
| board map | `map BOARD` | `--constraints --show SEC --ref/--net/--group --render` |
| can it route? | `plan-route BOARD` | `--ignore-routing`; `plan-compare`, `plan-options` |
| room, blocks, escape copper | `density`, `modules`, `fanout`, `floorplan` | `--target`; `--size`; `--check-only`; docs/FLOW.md |
| ready to route | `route-ready BOARD` | `--waive CODE:REGEX --all` (+PLAN_*) |
| area budget | `floorplan-check BOARD` | `--constraints --spec` |
| silk | `silk BOARD` | `--fix --out O` |
| templates | `templates BOARD` | `--constraints --spec --out O` |
| rotate connectors | `anchor-check BOARD` | `--apply --out OUT` |
| IC supports | `clusters BOARD` | `--apply --out OUT` |
| place | `place --board B --strategy routable --out O` | `--constraints --spec --objective plan` |
| why not routed | `route-diagnose BOARD` | `--log --nets a,b --constraints --spec` |
| wait for a router | `route-wait STATUS` | `--timeout S --until done\|stall` |
| run status | `route-status BOARD_OR_STATUS` | |
| review | `--no-sourcing review --board B` | `--spec --no-api --waive --fail-on` |
| diff-pair tuning | `tune --board B --out O` | `--constraints` |
| part count | `bom-opt --spec S` | `--constraints` |
| docs/sourcing | `spec-check`, `sourcing`, `datasheets` | `--no-api` offline |
| fab package | `fab`, `deliver`, `pcbway` | `review` must pass first |
| toolchain, context budgets | `doctor`, `doctor --context` | |

`--constraints` TOML, `--spec` JSON. Global flags (`--no-sourcing`, `--full`, `-q`, `--top`) precede the subcommand
(the last three also follow it).

## Pipeline
`docs/AGENT-PIPELINE.md`: one subagent per stage, exact commands, briefs, stop conditions.
Stages: floorplan-check, anchor-check, place (`--objective plan`), route-ready (gate, runs plan-route), route, tune, review.
Order: map, place, plan-route gate, route, audit; `--flow constructive`: modules, floorplan, fanout (gate), route. Hand off `W/state.json` (<2 KB) + the stage command; read map slices. Route only after the gate passes.

## MCP
Tools mirror the commands (`fluxplace_<cmd>`); `fluxplace_run` runs any other by name.
