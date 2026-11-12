# Agent pipeline: one subagent per stage, small file handoffs

For an orchestrating AI. Each stage runs in its OWN agent context and needs no conversation history: it gets
`W/state.json` (under 2 KB) and one command. You read back one verdict line and `state.json`. Nothing else.

```
fluxplace pipeline BOARD --constraints C --spec S --workdir W [--from STAGE] [--to STAGE] [--status]
```

| # | stage | what it does | output (under `W/<n>-<stage>/`) |
|---|-------|--------------|---------------------------------|
| 0 | `map` | optional: `fluxplace map B --constraints C` writes `B.fluxmap.json`; agents read slices (`map --show parts --ref U5`) instead of the board | B.fluxmap.json |
| 1 | `floorplan-check` | runs the `floorplan-check` command if this build has it, else `na` | stage.log |
| 2 | `anchor-check` | rotates misoriented anchors (connectors) to their best option (`--apply`) | board.kicad_pcb |
| 3 | `place` | `clusters --apply` (support parts to their ICs) then `place --strategy routable` (`--objective plan` / `[placement] objective = "plan"`: local moves driven by the plan-route findings, kept only when PLAN_SCORE drops) | board.kicad_pcb |
| 4 | `route-ready` | the gate: `blocked` on any FAIL not waived (`--waive CODE:REGEX`, `--force`); with `[route_ready] use_plan` (default) it runs plan-route too: sealed pins and capacity overflow block | route-ready.json |
| 5 | `route` | batch routing on OPEN CONNECTIONS (pcbnew connectivity; router gets the project's fab floors, no vias in pads; every batch audited, a violating one is discarded as `RULE_VIOLATION`; `[route] mode = "parallel"` = per-net jobs + merge) (35 nets per router call, 240 s each, checkpoint `ck-NNN.kicad_pcb` after each, halving on timeout, leftovers pass), keep-best, stall loop, all inside `--route-budget`; writes `routed.kicad_pcb.status.json` (last good checkpoint) and `leftovers.json` | board.kicad_pcb, diagnosis |
| 6 | `tune` | DRC-guarded diff-pair length tuning | board.kicad_pcb |
| 7 | `silk` | `silk --fix`: re-places reference texts so the silkscreen is on the board, off pads/holes, not stacked; copper and footprints untouched (`docs/commands/silk.md`) | board.kicad_pcb, stage.log |
| 8 | `review` | design-review gate (`--api` adds live distributor data) | review.json |

`state.json` fields: `stage`, `verdict` (`pass|fail|blocked|na`), `board` (the file the next stage reads),
`stages{name:{verdict,line,dir,board}}`, `scorecard{score,crossings,ratsnest_mm,overflow,fail,open}`,
`next[]` (what the next stage must fix, worst first, numbers included), `paths`. `--status` prints it as a
table of at most 15 lines. A stage not implemented in this build reports `na` and passes the board through.

Pipeline order with the plan: `map` -> `place` (objective plan) -> `route-ready` (= plan-route gate) -> `route` -> audit (`review`).
`fluxplace plan-route B --constraints C --ignore-routing` judges a placement before any copper exists (exit 1 = sealed pins or
overflow; findings carry the part to move and the direction); `plan-compare A.fluxmap.json B.fluxmap.json` compares two. The batch router
takes only an ORDER hint from the plan (`[route] plan_map`, docs/commands/route.md): it has no per-net layer/corridor interface.

## Rules (learned the expensive way)

1. **One board owner at a time.** Exactly one agent writes a given board or workdir. Stages are sequential;
   never run two stages, or two pipelines, on the same `W`. Parallel exploration copies the board to its own `W`.
2. **Never poll logs.** No `tail`, `grep`, `sleep` loops on router logs (one agent made 292 calls that way).
   Use `fluxplace route-status STATUS` (one line) or `fluxplace route-wait STATUS --timeout 900 [--until stall]`
   (blocks, one line plus the result and diagnosis paths). The `route` stage already waits internally.
3. **Never time-box a whole-board router call; use batch routing** (`docs/commands/route.md`): the router writes
   nothing until a whole pass incl. its retry phase ends, so a kill loses all progress.
   Optimisers are time-boxed and keep best-so-far.** Always pass a budget (`--route-budget`, `--route-timeout`);
   the route stage only replaces its best board with one that has fewer unrouted nets.
4. **Do not start routing until `route-ready` passes**, or every remaining failure is justified: waive it with a
   reason (`--waive CODE:REGEX`) and say why in the report. Do not use `--force` silently.
5. **Report failures with numbers** (counts per code, N unrouted of M, the worst 3 items with mm), never "it failed".
6. Analysis commands are compact by default (verdict, counts, worst 10, report path). Read the `.txt` report
   named in the output only if the answer is not in the summary; do not re-run with `--full`.
7. Never edit the input board in place. Each stage writes a new file; `board` in `state.json` is the handoff.

## When to stop and ask a human

- `route-ready` stays `blocked` after one `place` re-run and the remaining FAILs are geometric (budgets over by more
  than 2x, board too small, congestion overflow persists): the floorplan or outline needs a human decision.
- `route` ends `fail` with the same unrouted count twice in a row (no progress), or `na` (router missing).
- `review` reports a FAIL that needs a part, datasheet or fab decision (MPN_MISSING, LANDPATTERN_*, FAB_*).
- Anything needing a waiver the constraints file does not already allow.

## Commands to run (exact)

```
fluxplace pipeline B --constraints C --spec S --workdir W --to route-ready     # stages 1-4
fluxplace pipeline B --workdir W --from route --to route --route-budget 1800     # stage 5 (`--from STAGE --board B` creates state.json itself; later stages can omit B)
fluxplace pipeline --workdir W --from tune --to review                         # stages 6-8
fluxplace pipeline --workdir W --status
```
Exit code: 0 stage(s) passed, 1 stopped on `fail`/`blocked`. The last printed line is `pipeline: <verdict> at <stage>`.

## Subagent briefs (paste, fill the `<...>`)

**Stage 1-3: place**
```
You own the board workdir <W>. Nobody else touches it.
Run: fluxplace pipeline <B> --constraints <C> --spec <S> --workdir <W> --to place
Read nothing else first. Then run: fluxplace pipeline --workdir <W> --status
Report in under 10 lines: the stage table, scorecard numbers, the first 3 `next` items.
Do not edit constraints or the input board. Do not run route. If a stage says `fail`, report its
`stage.log` path and the last 5 lines of that log, then stop.
```

**Stage 4: route-ready gate**
```
You own <W>. Run: fluxplace pipeline --workdir <W> --from route-ready --to route-ready
If `pass`: report the scorecard and stop. If `blocked`: report counts per code and the worst 5 items
from state.json `next` WITH their numbers. You may re-run stage `place` once
(`--from place --to route-ready`). You may waive a FAIL only if the constraints file already justifies it;
otherwise stop and ask. Never use --force.
```

**Stage 5: route**
```
You own <W>. route-ready has passed. Run once, no polling:
  fluxplace pipeline --workdir <W> --from route --to route --route-budget 1800
If you need to wait on a router you started yourself: fluxplace route-wait <W>/5-route/routed.kicad_pcb --timeout 900
Never tail logs; never sleep-loop. Report: unrouted before -> after, elapsed, timeouts, the diagnosis path,
and the first 3 `next` items. Do not move parts unless asked (--replace-on-stall).
```

**Stage 6: tune**
```
You own <W>. Run: fluxplace pipeline --workdir <W> --from tune --to tune
Report which pairs are still over their skew limit (names and mm) or "all within limit". Do not edit constraints.
```

**Stage 7: review**
```
You own <W>. Run: fluxplace pipeline --workdir <W> --from review --to review
Report the verdict line and the counts per FAIL code from state.json `next`. Classify each code as
(a) fixable in layout, (b) needs a part/datasheet decision, (c) waivable with the existing [review] waive.
Do not waive anything yourself. The full list is <W>/7-review/review.json.
```

## Orchestrator loop

```
run stage-group brief -> read its report + `fluxplace pipeline --workdir W --status`
  pass    -> next group
  blocked -> re-run `place` once, else ask a human
  fail    -> stop at the first fail, forward the `next` items (with numbers) to the human
```
Give each subagent only its brief and `W`; do not forward earlier stages' transcripts.
