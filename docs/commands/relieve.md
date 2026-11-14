# `relieve` - move parts until plan-route predicts routable (or keep the best seen)

```
fluxplace relieve BOARD --constraints C [--spec S] --out OUT [--rounds N] [--may-grow MM] [--grow-sides L,R,T,B] [--json F]
```
Runs before any router. Measures with the plan-route predictor in-process (`planroute.run`, ignore-routing mode), then moves
movable parts with the existing move engine `planplace.plan_place` (sealed-pin blockers, overflow neighbours, over-budget ends;
each move legalised by `legality.Legal`, the support-part guard, and kept only when the predictor key improves). A board is kept
only when the collision gate (`legality.collision_gate`) and the intent hard rows (`intent.check`) are no worse than the input's,
verified on the WRITTEN board (reload + full predictor). Exit 0 when the output passes the plan-route predicate, 1 otherwise.

**Outputs.** `OUT` = the best board seen by the predictor key (input when nothing improved; the verdict says so). `OUT.relieve.json`
= ledger (per move: ref, from, to, kind, reason, score before/after), input and output verdicts, hard states, guard result,
growth probes and the options table. `OUT.relieve-work/` = intermediate boards. `--out` is required and must differ from BOARD.

**Guard.** Same refs, same pad->net map per ref, same segments and vias; else the output is rejected.

**Growth** (never by default). `[relieve] may_grow_mm` (default 0) and `may_grow_sides` (e.g. `["R","B"]`) or the CLI flags. Only when
moves alone still fail: the allowed side nearest the residual congestion (sealed pins and overflow cells) is grown; the smallest
passing integer size is found by bisection (assumes the predicate is monotone in size). Growth moves outline edges only
(`planopts.grow_text`).

**Options table** (prediction only, owner decisions, printed when the result still fails): every side grown by max(10, may_grow) mm
with moves re-run; layer variants (`planroute.run_variants`); sealed pins whose blocker is fixed or locked (`owner`), and sealed pins
whose movable blocker had no accepted move (`open`).

**Pipeline.** `[relieve] enable = true` inserts stage `relieve` after `plan-route` (constraints required). Default off.
A still-failing prediction is WARN; the options table is in the stage's `stage.log`.

**Not done.** Per-move hard checks (collision gate and intent rows are verified per pass, not per move). Decoupling-distance budgets
beyond the support-part guard. Side flips are not written to the board text (flipped parts are skipped; the pass is verified anyway).
Region-wise candidate generation (channel shifts, swaps, passive flips) is not implemented: the move engine proposes moves from findings.
