# fluxplace: rules for changing the code

Read `AGENTS.md` for how to run commands. This file covers how to change the code.

## One flow, no stacked complications (owner rule, 2026-11-19)
fluxplace exists to take a board from placement to fab in one pass: build, go through the stages, sort what is left by
kind, and send each gap back through the repair method that fits it. Then loop until the board is clean or no repair
makes progress.

- **No new top-level command or flag for a fix.** A new capability is a method that the flow's gap dispatcher calls
  (`if`/`elif` or a table keyed by gap kind). A new flag is allowed only when a real board needed a value that cannot
  be derived. Per-board values go in the constraints TOML; everything else is a default derived from the board, the fab
  profile or the stackup.
- **Repairs are stages of the flow, not separate tools.** The guard (opens fall, shorts 0, no new DRC key) is applied
  by the flow to every repair the same way.
- **Prefer deleting a knob to documenting it.** Before adding code, check the existing methods (`region`,
  `repairregion`, `handrepair`, `patch`, `snapiter`, `pairs`) and extend one.
- **Every gap the flow cannot close** gets a row in `docs/ONE-SHOT-LEDGER.md` (cause, fix, result measured on a board).
- Stay board-agnostic: no board refs, net names or constants in the code.

## Measure every change until routing is bullet-proof (owner rule, 2026-11-19)
- Every repair attempt -> `.fluxplace/attempts.jsonl`; every `close`/variant run -> `docs/corpus/runs.jsonl` (one line, committed).
- Every measurement ends with: the `python3 -m fluxplace.gaps --report` table, a ledger row naming the top refusal
  reason and the fix proposed for it, and that fix as the next recipe item. No phase is done without its measured line.
- A change is kept only when the runs log shows the board (later: the corpus score) not worse. The goal metric is the
  share of runs with `full_pass` (0 opens, 0 shorts, 0 unwaived errors, hs pairs complete, review pass) in one call.
