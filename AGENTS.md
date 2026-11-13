# fluxplace for AI agents (read only this)

Context is scarce: commands answer with results. Unsure: `doctor`.

## Output rules
- Analysis commands print a verdict line, counts, worst 10 (`--top N`), full-report path
  (`<board stem>.<cmd>.txt/.json`; read only if needed). `--full` = all, `-q` = verdict line.
- Exit: 0 ok, 1 FAIL; `route-wait`: 2 timeout, 3 no status.
- `fluxplace help-brief CMD` = 15 lines per command (alone: one each). Skip README/docs.
- Never time-box a whole-board router call: use batch routing (`docs/commands/route.md`).
- Never overwrite the input board (`--out`). One agent per board/workdir.

## Commands by task
| task | command | flags that matter |
|---|---|---|
| whole flow | `pipeline BOARD --workdir W` | `--constraints --spec --flow --from/--to --status --route-budget` |
| board map | `map BOARD` | `--constraints --show SEC --ref/--net/--group --render` |
| can it route? | `plan-route BOARD` | `--ignore-routing`; `plan-compare`, `plan-options` |
| room, blocks, escape copper | `density`, `modules`, `fanout`, `floorplan` | `--target`; `--size`; `--check-only`; docs/FLOW.md |
| ready to route | `route-ready BOARD` | `--waive CODE:REGEX --all` (+PLAN_*) |
| area budget | `floorplan-check BOARD` | `--constraints --spec` |
| silkscreen | `silk BOARD` | `--fix --out O` |
| tree/chain templates | `templates BOARD` | `--constraints --spec --out O` |
| rotate connectors | `anchor-check BOARD` | `--apply --out OUT` |
| support parts to ICs | `clusters BOARD` | `--apply --out OUT` |
| place | `place --board B --strategy routable --out O` | `--constraints --spec --objective plan` |
| why nets unrouted | `route-diagnose BOARD` | `--log --nets a,b --constraints --spec` |
| wait for a router | `route-wait STATUS` | `--timeout S --until done\|stall` |
| one line on a run | `route-status BOARD_OR_STATUS` | |
| design review | `--no-sourcing review --board B` | `--spec --no-api --waive --fail-on` |
| diff-pair tuning | `tune --board B --out O` | `--constraints` |
| LAN magnetics wiring | `magnetics BOARD --spec S` | part `channels`; in `review` |
| part count | `bom-opt --spec S` | `--constraints` |
| docs/sourcing | `spec-check`, `sourcing`, `datasheets` | `--no-api` |
| fab package | `fab`, `deliver`, `pcbway` | `review` must pass first |
| toolchain, context | `doctor`, `doctor --context` | |

`--constraints` = TOML, `--spec` = JSON. Global flags (`--no-sourcing`, `--full`, `-q`, `--top`) go before the
subcommand; the last three work after it too.

## Pipeline
`docs/AGENT-PIPELINE.md`: one subagent per stage, exact commands, paste-ready briefs, stop conditions.
Stages: floorplan-check, anchor-check, place (`--objective plan`), route-ready (gate, runs plan-route), route, tune, review.
Order: map, place, plan-route gate, route, audit; `--flow constructive`: modules, floorplan, fanout (gate), route. Hand off `W/state.json` (<2 KB) + stage command; read map slices, not boards. Route only after the gate passes.

## MCP
Tools mirror the commands (`fluxplace_<cmd>`); `fluxplace_run` runs others by name.
