# One-shot ledger: where fluxplace failed, was bypassed, or was slow

Goal: fluxplace finishes a board in one pass without hand scripts or guard bypasses. Every gap found while using it on a
real board gets one row: what was run, what went wrong, the cause, the fix (commit) and the result measured after the fix.
Read this before starting work on a board step and add a row after it. Measure on the board, not in tests.

Outcome codes: FAIL (command did not do its job), BYPASS (a hand script or `FLUXPLACE_GUARD=0` did the job instead),
SLOW (finished, but the time is out of proportion), FALSE (a guard or gate reported something that was not true),
MANUAL (no command exists; a script did it).

Raw call data: `.fluxplace/calls.jsonl` beside each board (`fluxplace calls`). In that data, rc != 0 includes gate
verdicts (fp-check and placement-check exit 1 on findings), so a high fail count is not always a tool failure.

## Call-log baseline (UTV comms bridge W12-W14, read 2026-11-19)

| command | failed / calls | time | note |
|---|---|---|---|
| pairs | 18 / 19 | ~160 min | the largest time sink; ETH_P0/P1 and PCIE_CLK/TX never closed |
| hand-repair | 5 / 5 | ~57 min | single net, all or nothing: closing one open opened another, so it was refused |
| finish (whole board) | 1 / 1 | 25 min | freerouting hit its time cap |
| suggest | 30 / 37 | ~11 min | mostly "no path" on sealed pockets |
| lay | 12 / 15 | ~3 min | W14: false "opened" rejections (DRC cap, below) |
| versions | 22 / 35 | ~3 min | refusals (opens/errors rose) are by design; check for real errors |

Call-log bug: a global flag before the subcommand (`--no-sourcing review`) is logged as cmd `--no-sourcing`.

## Rows

| date | board / step | command | outcome | cause | fix (commit) | measured after fix |
|---|---|---|---|---|---|---|
| 2026-11-19 | W14 prep | replace-footprint (B.Cu part) | FAIL (segfault) | KiCad 10 crashes when an orphan footprint is flipped | 1371a50: flip after adding to the board | prep finished |
| 2026-11-19 | W14 prep | replace-footprint H200/H201 | BYPASS (`FLUXPLACE_GUARD=0`) | not found yet | open: check in board step B1 | - |
| 2026-11-19 | W14 step D | lay BT2-4_RF | FALSE, then BYPASS (add-path.py through the pcbnew API) | kicad-cli DRC lists at most ~499 `unconnected_items` (d2: ratsnest 501, DRC 499). A lay closing one open let a hidden net (CELL_MAIN_CARD) into the list; the per-net guard read that as a new open | ce46aea `drcfull.complete`: cap measured at exactly 499, no flag lifts it; per-net counts = max(listed, pcbnew) in 16 call sites | the same lay on a copy of d2 is ACCEPTED: unconnected 501->500, BT2_RF 2->1, 0 new keys |
| 2026-11-19 | W14 step C | (none) C316-C319 placement | MANUAL (02-add-reset-caps.py) | no command places a new part next to a pin while staying out of keep-outs; the first spot sat in a keep-out | open | - |
| 2026-11-18 | W13 finish | hand-repair, finish | FAIL / SLOW | the repair tools work one net at a time and keep a change only if nothing else opens, so a sealed open stays sealed; whole-board freerouting hit its cap | a6fe6f9 `repair-region`: rip the open nets and their unlocked blockers in one area, re-route together | not yet measured on a board (W14 B5) |
| 2026-11-18 | W12-W13 | pairs | FAIL / SLOW | diff pairs fail to break out; `repair-region` skips pair nets (Freerouting cannot route coupled pairs) | open: pair-aware region repair | - |
| 2026-11-19 | W14 step D (F2 proof) | lay guard line | FALSE? | the guard also prints `open 505->504`, a third counter that disagrees with DRC+pcbnew (501->500) | open: the redesign phase 2 (one measure) removes it | - |
| 2026-11-19 | phase 1 measure (W13 routed copy) | `python3 -m fluxplace.gaps` (stage close) | FAIL | 5->5 opens, 214->214 errors, 9 attempts, stop flat, 22 min. Every rung refused: drcfix `R1 shorts up` (x2); patch, patch_rip, launder `no change`; handrepair `no signal layers` (`signal_layers` 80 % zone-bbox rule drops every layer on a board with full pours); region `SystemExit: finish: no session produced` (Freerouting); snapiter `R2 new key track_not_centered_on_via` (strict R2 refuses a partial snap). The 4/15 hand result came from accepts the strict local guard refuses | open: owner decision (fix signal_layers + bring phase 2 trades forward, or accept phase 1 as plumbing) | - |
| 2026-11-19 | phase 1b measure (W13 routed copy, 4448fe8) | `python3 -m fluxplace.gaps` (stage close) | PARTIAL | one call, no hand step, 78 min: errors 214->17 (15 footprint-level, fixed in W14 footprints; 2 vias snapiter excluded), opens 5->5. snapiter partial (1074 vias moved); drcfix `R1 shorts up`; patch, patch_rip, krt `no change`; handrepair `no board written`; region (Freerouting) `no session produced` at 900 s. Opens CAN_MOSI, SENSE_ADC_P, I2C_SCL, OCS2_N, PCIE_TX_N are sealed pockets: W13 closed OCS2_N only by a trade (unlay +3V3 stubs) | open: trades (phase 2) + AI rip/corridor + KRT re-route (phase 2b); region costs 15 min per failed try -> cap it by the seal report first | - |
| 2026-11-19 | phase 2b measure (W13 best copy, abb9606) | close + AI rung (Sonnet answered 1 packet, ~2.5k tokens) | FAIL | pipeline works end to end (packet 3.1 kB -> exit 5 -> action -> nudge/rip/KRT/suggest in one session -> session_commit -> guard). CAN_MOSI: AI ripped the only 2 unlocked items (BT2_VDD stub, CAN_INT via) and re-routed; refused `R4 score not lower`. Every limiting obstacle is locked copper (pairs, fan-outs, pours). Found on the way: gap ids unstable across runs (fixed: AI files keyed kind+net), AI offered plane layers (fixed: signal layers only) | open: locked re-escape permission tier (`handroute.shove(allow_locked)` in a session, FLOW-REDESIGN 2.7) as an AI action `rip_locked` for plain/non-pair locked copper, with R3/R5 guarding pairs; one packet per open per round instead of the first gap only | - |
| 2026-11-19 | W14 build | thermal mask (D71cd) before `route` | MISTAKE | B.Mask opening applied to the unrouted board; the router then laid signal copper through it: 203 `solder_mask_bridge`. Re-cut after routing: 0 | flux: route stage warns on board-level mask openings present before routing; `close` classifies mask-polygon bridges as kind `mask-opening` with the fix line (re-cut after route) | in progress |
| 2026-11-19 | W14 build | `run-w14.sh` copies `8-route/board.kicad_pcb` | MISTAKE | inserting the `critroute` stage shifted the route dir to `9-route`; the script reported "router: no board written" for a board that routed 451 -> 7 | flux: every stage also writes `W/<stage>.kicad_pcb` (stable link); scripts read that, never `<n>-<stage>` | in progress |
| 2026-11-19 | W14 build | `pairs` collision gate | BUG (fixed 6cca976) | `placementcheck.gate_failures` ignored `[review] waive`; the route stage honoured it; pairs refused the whole W14 build | fixed + test | done |
| 2026-11-19 | W14 close | lock policy | DESIGN FLAW | pairs, RF AND fan-out/plain copper locked at build (27 % of W14 copper); every remaining open was walled in by locked copper; KRT/patch/handrepair may not touch it | owner 2026-11-19: lock pairs + RF only; flux rules updated once W14 confirms (krt_region rips unlocked window copper) | measuring |
| 2026-11-19 | W14 close | `drcguard.protected_nets` | BUG (fixed 4f0fbb1) | only constraint-declared pair families (34 nets): 36 pair legs (PCIE, BT USB, audio) and every RF net (empty `[rf] nets`) unprotected | + `graph.diff_pairs` legs + `review.is_rf` names | done |
| 2026-11-19 | W14 close | `versions` copper fingerprint | GAP | lock flags are not in the fingerprint: an unlock step is "no change" and cannot become CURRENT | flux: fingerprint includes the locked flag of tracks/vias | in progress |
| 2026-11-19 | W14 close | `close` in-process rungs | BUG (fixed 4858bd6) | `patch_board(rip=True)` segfaulted inside pcbnew and killed the run (exit 139) | every rung except `ai` runs in a child process | done |
| 2026-11-19 | W13/W14 close | operations (top-level model) | MISTAKE | killing a wrapper script's PID tree also killed its running pipeline; `pkill -f` matched its own shell; several measurement runs were started on code that was already superseded | kill by explicit PIDs from `pgrep -P`; restart a measurement after every commit it depends on | rule |
| 2026-11-19 | phase 1 recipe | top-level model | MISTAKE | recipe omitted KRT (the proven router, 2.8): opens went from grid search straight to Freerouting | `krt` rung (cbf929b); memory `krt-router-first` | done |
