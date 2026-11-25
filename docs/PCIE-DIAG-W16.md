# PCIE_TX / PCIE_CLK / ETH_P0 / ETH_P3 / USB_DM_J on W16 route-3 (pairs stage) - diagnosis

Read-only. Boards: pre = run/5-pour-tie/board.kicad_pcb, post = run/8-pairs/board.kicad_pcb (copies in this dir: pre.kicad_pcb, post.kicad_pcb).
Scripts/outputs in this dir: a1..a9.py (measurements), e1.py/e2.py (pairs.plan, plan-only, no writes), e2-nonneg.out.

## 0. Headline findings

1. **The report is stale for four of the five pairs.** On the written 8-pairs board PCIE_TX, PCIE_CLK, ETH_P0 and ETH_P3 ARE laid, connected
   (kicad-cli DRC lists no unconnected item for them), and coupled. board.pairs.json still says `not-laid / open / state c / PAIR_HANDOFF`.
   Only USB_DM_J/USB_DP_J is really not laid (2 opens per net, only fan-out stubs on the board).
2. **Why the main pass failed for TX/CLK: emission order.** pairs.plan puts pairs the negotiation could not settle ("unresolved") LAST,
   behind every resolved pair, including audio/ETH pairs of lower class priority (pairs.py ~L946,
   `rank = (pr in unresolved or ..., gates, key)`). TX is 22nd and CLK 28th in `order` although their base key (prio 0, kind 0) is 4th/5th.
   By then INJ_PRI, ETH_*, USB_HUB, BT*, PCIE_RX, USB3_M2_TX have taken the J11 break-outs and the B.Cu/F.Cu lanes. The failing search never
   leaves the pocket at (171.6, 69.0): its blockers are laid copper of earlier pairs plus negotiation "corridor of ..." reservations of peers.
3. **The geometry exists.** Plan-only on the pre-pairs board (fan-out copper present):
   TX alone: planned, B.Cu, 0 vias, 54.37 mm (J11 (172.3,70.11) -> J200 (119.2,58.42), straight 54.4 mm).
   CLK alone: planned B.Cu 58.19 mm. TX+CLK+RX+USB3_M2_TX+USB3_M2_RX+USB2_M2 (all six J11->J200 pairs): all planned in 74 s.
   Full 34-pair run with `[pairlay] negotiate = false` (pure key order prio/kind/span): ALL pairs planned incl. TX, CLK, ETH_P0, ETH_P3, USB_DM_J (269 s).
   The same run with negotiation reproduces the stage's 11 "unresolved" pairs exactly (BT2, BT3, ETH_L0, ETH_L2, ETH_P1, ETH_P3, PCIE_CLK, PCIE_TX, USB2_M2, USB3_M2_RX, USB_HUB;
   it was killed by my 295 s timeout before the lay, only the negotiation line printed).
   Caveat: plan-only does not model via-exit dogbones and writes no copper; the real lay of the full set was not run (not allowed).

## 1. Endpoints and distances (pre-pairs board)

J11 (CM5 100-pin, back side B.Cu, pos 166.72,71.65 rot 180, courtyard 154.9..178.6 x 69.2..74.1); J200 (M.2 Key B socket, B.Cu, 110.5..133.0 x 56.7..67.3).
All pads are SMD on B.Cu (the `F.Cu` printed by a1.py is pad.GetLayer on a back footprint). The two signal layers usable: B.Cu, F.Cu via vias, In2/In3 (planes In1, In4 are GND).

| pair | J11 pads (x,y, pitch) | J200 pads | straight | Manhattan | laid by plan alone |
|---|---|---|---|---|---|
| PCIE_TX_N/P | 24 (172.12,70.11) / 22 (172.52,70.11), 0.4 | 47 (119.47,58.42) / 49 (118.97,58.42), 0.5 | 54.4 | 64.8 | B.Cu, 0 vias |
| PCIE_CLK_N/P | 12 (174.52,70.11) / 10 (174.92,70.11), 0.4 | 53 (117.97,58.42) / 55 (117.47,58.42), 0.5 | 58.2 | 68.7 | B.Cu (alone), F.Cu or B.Cu+F.Cu 1 via (with the others) |
| ETH_P0 | J10.10/J10.12 (174.9/174.5,104.11) -> U12.4,7 / U12.5,6 (149.5-150.3, 79.8-80.3) -> T3.3/T3.2 (137.9/136.9,66.27) | | 53.4 span | | 3 sections, 1 via |
| ETH_P3 | J10.5/J10.3 (175.7/176.1,107.19) -> U11 (163.1-163.6,80.5-81.2) -> T3.12/11 | | 51.8 span | | 3 sections, 1 via |
| USB_DM_J/DP_J | J5 A7,B7 (DM) 188.3/187.3 and A6,B6 (DP) 187.8/188.8 at y 124.79 (0.5 pitch, alternating DM,DP,DM,DP), 0.3 wide -> U8.3/U8.1 (178.2/176.3,122.37) | | 11.0 | | J5>J5 + J5>U8 |

## 2. What the stage actually did at J11 / J200

Pre-pairs fan-out (stage 4, all locked, 0.127 wide) at J11: every pad of the y=70.11 row escapes NORTH (-y) with a 0.35 mm stub, a 45 degree jog and a
stub to y=68.58 (signals) or 68.16 (GND). Items within 1.2 mm of TX pads: GND stub from pad 26 at x=172.06 running y 69.27->68.16 (0.06 mm from TX_N's pad axis),
GND 20 -> (173.35,69.18), RX_N 18 -> (173.77,69.16), TX_N own jog to x=172.49, TX_P own jog to x=172.92. At CLK: GND 14 -> (174.63,68.16), GND 8 -> (175.91,69.02),
RX_P jog to x=174.2, CLKREQ/WAKE jogs to x=176.77-177.19. The fan-out stubs of the pair's own nets are ignored by the lay (`ign`); the GND/RX/CLKREQ ones are not.
This is the 2026-11-19 "fan-out before pairs" condition, but on this run it is NOT what blocked the lay: with those stubs present the pair planned alone.
The 301 fan-out items the stage "replaced" are these; the pairs stage re-escapes every laid pair SOUTH (+y) into the connector body: dogbone vias at y 70.9-71.3
(TX_N via (171.87,71.3), TX_P via (172.67,71.3); RX vias (173.16,70.91), (173.88,70.91); USB3_M2_RX via ~ (171.0,70.9)). Between RX and USB3_M2_RX vias the free width
for TX is 1.69 mm edge to edge, enough only because TX's vias land at x 171.87/172.67 on the 0.8 mm via-pair pitch.

Final board (post), copper that exists:
- TX: J11 B.Cu -> via pair (171.87/172.67, 71.3) -> In2.Cu (0.248 wide) -> via pair (166.87,74.3)/(167.47,74.9) -> B.Cu west/north-west 45 degree to J200: 0.153/0.127 geometry, 2 vias per leg.
  Own audit: `mismatch, skew 7.17 ps > 0.91 ps` (KiCad length P 69.19 / N 70.33; path length 67.49 / 67.54: 1.1 mm of the KiCad figure is not on the path - unexplained, see item R below). 97 % of the N leg within gap+0.06 of the P leg.
- CLK: B.Cu -> F.Cu (46 mm) -> B.Cu, 2 vias per leg, `mismatch, skew 4.40 ps > 0.91`, 96 % coupled.
- ETH_P0: 85 % coupled (gap 0.25), 3 vias per leg, skew -2.10 mm / -12.6 ps (limit 0.15 mm), audit `unknown MEASURE_DISAGREE p -0.0525 mm vs tol 0.05`.
- ETH_P3: 87 % coupled, 3 vias, skew ~0, audit `unknown MEASURE_DISAGREE p -0.052 mm` (same figures).
- USB_DM_J/DP_J: only the 4 fan-out pad stubs on J5 (lengths 0.6-1.8 mm, dead ends at y 123.43/122.99, interleaved DM/DP/DM/DP at 0.42-0.5 mm) and the 2 U8 stubs. 2 opens each.

## 3. How a "not laid" pair gets copper in the board (the stale-report mechanism)

pairs.run -> FB.ladder (pairfb.py). Trial `iii0` (b(iii), bounded uncoupled break-out, ring and skew relaxed) ran with `only` = ALL 15 pending pairs
(`_trial`: `only = {n for r in pend for n in r["pair"]}`), laid TX, CLK, ETH_P0, ETH_P3 (and USB3_M2_RX), and the log says
"pairs fallback iii0: 1 of 15 pending pair(s) completed" (kept, not discarded). `_trial` keeps the WHOLE trial board
(`shutil.move(trial, out)`) when at least one pending pair became `complete`, but `_take` only updates rows whose audit status is `complete`
(USB3_M2_RX). Pairs laid by the same trial but graded `mismatch`/`unknown` keep their old row: `state c`, `status open`, `handoff True`,
and the post-ladder re-audit explicitly skips state c rows (pairs.py: `r["status"] = aud[...]["status"] if r.get("state") not in ("c",) else r["status"]`).
Contradiction with the pairfb docstring ("state c: the pair's own partial copper is not written"). Consequences: gate `pairs` sees pair-open for pairs that have copper,
the 14 "ordinary nets for the router" are locked pair copper (handoff would re-route nets that are already laid and locked), PAIR_SKEW/tune input is wrong.

## 4. Cause per pair

| pair | cause | evidence |
|---|---|---|
| PCIE_TX | (b) ORDER: negotiation "unresolved" placed after lower-priority pairs, blocked by earlier pairs' copper + peer corridor reservation. Main pass failed; trial iii0 laid it (coupled, skew over) but the row was never refreshed (stale report, e). Not (a), (c), (d). | order index 22 of 34 (base key rank 4); blockers `track (corridor of PCIE_CLK)`, `track INJ_PRI_N (reserved)`, `via GND`; alone plan OK; negotiate=false plan OK; final board has it |
| PCIE_CLK | same as TX; position 28 of 34; blockers also `track USB_HUB_DM/DP (reserved)`, pad R304.2 (BT4_RF), pad J11.2 (CLKREQ). Corridor of the CLK pair crosses the INJ_PRI/USB_HUB area west of J11 (x 165-178, y 63-71). | same as TX |
| ETH_P0, ETH_P3 | (e) NOT a lay failure: sections all `ok`, 0 opens, 85-87 % coupled. Audit grades them `unknown` (MEASURE_DISAGREE: own per-leg decomposition vs KiCad total off by 0.0525/0.052 mm, tol 0.05 mm = a 0.0025 mm miss on 3-section chains with 3 vias), and an `unknown` row is treated as pending -> state c -> "open connections remain". | pairaudit recon p -0.0525 vs tol 0.05; DRC has no unconnected item; also skew ETH_P0 -12.6 ps un-tuned |
| USB_DM_J/DP_J | (a)+(d): J5 is a USB-C with two pads per net interleaved DM,DP,DM,DP. Fan-out (stage 4) left four separate locked dead-end stubs (DM B7 187.22, DP A6 187.64, DM A7 188.3, DP B6 188.8, 0.4-0.5 pitch) instead of one A/B join per net; the J5>J5 section (join A and B pads) has no room: B7 break-out blocked by `track USB_DP_J F.Cu` (the partner's own A6 stub/leg, claimed first at constructor clearance), U8.1 blocked by `track GND F.Cu`. Plan-only alone (and negotiate=false) says planned, so pairs code cannot make the join with the stubs as obstacles in the real lay (plan-only skips fan-out stub handling `via_exit`), not verified by a real lay. | board: only stubs; report sections both `no clear break-out`; ledger rows 64/65 (break-out gap fix and fan-out stubs) |

No pair is (c) no physical room: every one planned (plan-only) and TX/CLK/ETH exist as copper.

## 5. Recommended fix, fab-quality order, board-agnostic

1. **Emission order respects class priority and kind** (answers ledger 2026-11-19 "stage order: fan-out before pairs"/R006 spirit, 2026-11-25 row 163, and "PCIE_CLK/TX never closed" W13/W14):
   pairs.plan `rank` (pairs.py ~L946): `rank = (key[pr][0], key[pr][1], pr in unresolved or in alone_fail, gates, span)`, i.e. unresolved/failed-alone only reorder
   within the same (prio, kind) tier, never behind lower tiers. Same for the retry list of `moves()`. Test (test_pairs): two pairs of tier 0/kind 0 that conflict + a tier-4 audio pair that
   takes their corridor: the audio pair must be emitted after both, and both HS pairs must be laid.
2. **Corridor reservations are tier-aware**: pairs._lay_pair (`ctx["reserve"]` loop, pairs.py ~L725): a peer's or lower tier's reservation is a soft cost (guide/negotiation cost), a hard
   obstacle only when the reserving pair has an equal-or-higher tier; never block a pair with "corridor of Q" when Q is lower tier. Test: the TX blocker list must not contain a lower-tier reservation.
3. **Report must equal the board** (stale-report bug; answers 2026-11-25 row 163 symptom, LEDGER-AUDIT R043 tooling): pairfb._trial / pairs.run:
   (a) after the ladder re-audit EVERY row incl. state c (remove the `not in ("c",)` exception) and set state a-mismatch/laid-ungraded for any pair with closed legs;
   (b) `_trial` must write only the copper of pairs it adds as complete (strip the other pairs' trial copper before `shutil.move`), or `_take` must take mismatch rows too, so the docstring contract (c = no pair copper written) holds;
   (c) `handoff` only for pairs with no copper. Test: a fallback trial that lays pair X as mismatch and pair Y as complete: X row is not state c, X has no open, `handoff` excludes X.
4. **ETH_P0/P3 audit**: pairaudit.audit recon (`tol` 0.05 mm, `recon p/n`) is a hard 0.05 threshold on multi-section chains (3 sections, vias); scale tol with the number of sections/vias or report `mismatch` with the skew instead of `unknown`, and never treat `unknown` as `open`. Test: ETH_P0 fixture (3 sections/3 vias, recon 0.0525) is graded and tune-able.
5. **USB-C interleaved pairs (J5)**: fan-out must not leave four dead stubs on DM/DP/DM/DP pads; pairs.terminals (or fanout) must join the two pads of one net as one terminal by a via-dogbone jumper between the pads before the pair break-out, and reserve neighbour-pin escape (LEDGER-AUDIT R025: `reserve_exits = true`, pairs.reserve_for_fanout; W16 toml does not set it, default false). Also break-out of the second leg must be held to the pair gap against the first leg (breakout(gap=) exists; confirm J5.B7 uses it). Test: USB-C 4-pad group fixture: both nets joined A to B and a coupled pair leaves toward U8.
6. **Keep fan-out stubs of neighbours from sealing pair pins on J11** (R025, 2026-11-19 row 65): not the blocker this time (TX plans with them), but GND 26 stub sits 0.06 mm off TX_N's axis; set `[pairlay] reserve_exits = true` so fan-out is run with the pair exits as copper. Cheap, config-level.
7. **Tune**: TX 7.2 ps / CLK 4.4 ps / ETH_P0 12.6 ps need the centre-line tune after the order fix (R043). Open: KiCad length of TX_N exceeds its path length by 2.8 mm vs 1.7 mm for TX_P (1.1 mm of copper that is not on the path: look at pairaudit `basis kicad` vs `path`, via stack length per leg or dead stubs); resolve before trusting the 0.91 ps gate.

## 6. What a verification run should check (not run here)
`pairs` full run with fix 1+2+3: TX/CLK state `a` (not c), row status == audit of written board, ETH_P0/P3 graded, counts of state c reduced to the 5 audio pairs without two pads in range
(HPL_PRI, MIC_ADC, MIC_SEC: `pad_max_mm`) and J5. Expected full-board effect: gate pairs moves from `pair-open` to `pair-skew` for TX/CLK (tune stage then applies).
