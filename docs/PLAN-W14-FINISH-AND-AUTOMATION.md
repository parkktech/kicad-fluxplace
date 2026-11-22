# Plan: finish the UTV comms bridge board, make the flow finish boards unaided, and the own-router question

Planning review, 2026-11-22. Read-only: no board or code was changed. Sources: ONE-SHOT-LEDGER rows 2026-11-19..21,
RESUME-W14 NOW, DECISIONS D71ca-cm, `W14/stageD/RUN2.md`, `W14/stageD/pairs-hs.pairs.json`, `W14/stageD/B/review.json`,
`W14/stageG/tuned.kicad_pcb.tune.json`, research notes, branch `phase3g` (c0e6f18), and measurements made today on a scratch
copy of `W14/stageF/hand/r5.kicad_pcb` (open-nets, window PNGs, kicad-cli DRC, pcbnew read-only length/width tally).

Not done / not known: `review` and `fab` were never run on r5 (stageG stopped after tune); the review groups below are from
run 2 (`stageD/B/review.json`, 68 opens) and must be re-measured. Anything marked (unverified) was inferred, not measured.

---

## 0. Findings that decide the plan

| # | Finding on r5 (measured today) | Consequence |
|---|---|---|
| F1 | Three high-speed pairs are plain-router copper, not pairs. ETH_P1: 0 locked segments, 94 mm, 75-78 mm on inner layers at 0.15 mm (the good ETH pairs use 0.187 inner), vias 8 vs 6. PCIE_CLK: 0 locked, 90 mm, 85 mm inner at 0.15 (needs 0.248/0.127 for 90 ohm, review PAIR_IMPEDANCE_OFF +19 %), vias 4 vs 2. PCIE_TX: P 91.4 mm vs N 74.2 mm, legs leave J200.47/49 on different layers (N via to In2, P on B.Cu), widths 0.15/0.2 mixed. | These are 90 mm re-lays across a full board. W12 already measured that pairs cannot be re-laid on a full board. "PCIE_TX_N is a 1.2 mm hop" is wrong: closing the hop gives a connected, failed pair. |
| F2 | USB_DP_J/DM_J: skew 0.245 mm > 0.15, locked, 10 mm long, tune found no room. | Needs tuning room while the area is empty. |
| F3 | VBATT_PROT (5 A fuse rail) has no copper at width: about 200 mm at 0.127-0.4 mm (B 45 mm at 0.4, F 40 mm at 0.15, B 25 at 0.3, B 21 at 0.15, In2 19 at 0.25), 16 mm at 0.15 on In1.Cu (the GND plane layer), 2 mm at 1.4, no pour. VBATT_IN: 26 mm thin. VMODEM (3 A): about 100 mm thin beside 118 mm at 1.4. | Not shippable; cannot be widened in place (ledger 11-20 G1b: "no room to widen", cluster pours "cut apart by neighbours"). |
| F4 | Hand rounds made power worse: power_mag 354 (run 2 routed board) -> 521 (r5). Hand `lay` used signal width on rails and the session commit has no R6 gate. | Opens 68 -> 9 was bought with a worse score term the close loop would have refused. |
| F5 | Same 20 x 20 mm region in every failure: pairs stage `why` strings (PCIE_CLK "no clear break-out for J11.10", ETH_P0/ETH_P1 "no path for the coupled centre line ... closest approach (169.8-169.9, 80-81)", USB3_M2_RX "(170.8, 7x)"), the K1.3/K1.6 SPK_COMM opens, and U12/U11 PIN_ESCAPE_BLOCKED. K1 (G6K-2F-Y, 8 SMD pads, courtyard 160.6-171.2 x 66.9-76.3, F.Cu) sits directly over J11 (B.Cu, 154.9-178.5 x 69.2-74.4), with U403/U12/RN400/U600 around it. K1 is in `[placement] fixed`. | The relay occupies the top-side break-out field of the CM5 connector. Causal link is inferred from co-location (unverified by an A/B run). |
| F6 | F.Cu right at K1 pads 3 and 6 is open (window PNG): the SPK_COMM_P/N, HPL_PRI_P/N and BT3_DM opens are whole unrouted 27-41 mm nets, not sealed pads. They fail because every candidate path cuts a pour or crosses locked copper. | A routing-order and pour problem, not a pad-escape problem. |
| F7 | r5 also carries 92 dangling vias and 48 dangling tracks (DRC warnings), 25 off-centre vias, 1 courtyard overlap (C113/J205, waived D71cg). | Clean-up debt on top. |
| F8 | tune reports "leg open" for PCIE_TX_P, MIC_ADC_P, SENSE_N_DIV; kicad-cli lists none of them open. | pairaudit path-finder bug or a real gap DRC misses: check before trusting either (B10). |
| F9 | kicad-cli DRC with refill on this board: 11 s on an idle machine; `open-nets` 19 s; `window --png` 12 s. Agents saw 1-3 min per lay with 4 running in parallel. | Latency is model rebuild + two DRCs per edit under load (section C). |

Score now: [hs_pair 11, rf 0, shorts 0, opens 9, power 3, power_mag 521, pair 25, pin_escape 0, errors 26, quality 536].
By the owner's quality order r5 is a connectivity reference, not a candidate: hs pairs fail first, power second.

---

## A. Finish this board

### Decision

| Rank | Option | Verdict | Evidence |
|---|---|---|---|
| 1 | **New version W15: restart from the pairs-stage board, gate each stage, hand-agent loop at every gate** | **Do this** | F1-F4: the defects are in copper laid before the plain route. They are easy on a near-empty board (rails-first ladder 11-19: all four rails in 26 min; pair gap fix 0/3 -> 3/3 in 47 s) and were not solved on a full one (W12; close passes 2-4: 4 h, 0 accepts; PCIE_CLK pair_path refused 5 tries). |
| 2 | Partial re-route of r5 (rip U5/K1 regions incl. locked low-speed pairs) | Fallback only | Closes opens, leaves F1 and F3. Zero opens with failed hs pairs is not progress. Steps kept in A2 because they are also the test cases for part B. |
| 3 | Placement change (move K1 off the J11 field, spread U5 east passives) | Pre-approve, apply only if gate G1 fails at J11 | F5. K1 is `fixed`; a 1 mm scan found no free top-side slot for its courtyard near the audio block, so a move re-places the K1/T500/T501/FB block (about half a day). Small unfixed nudges (FB5, C46, C115, R505) go into W15 from the start. |
| 4 | Keep hand-finishing r5 | Stop | F1, F3, F4. |

### A1. W15 (ranked first)

Base: `W14/stageD/pairs-hs.kicad_pcb` (run 2: fan-out stripped, RF locked, 13/17 hs pairs complete incl. PCIE_TX) plus the two
pairs finished later (ETH_P0, USB3_M2_RX) copied from r5. Missing after that: PCIE_CLK, ETH_P1.

| Step | What | Who / tool | Time-box | Accept |
|---|---|---|---|---|
| 0 | Finish the stageG chain for information only: `review` + `fab` on `W14/stageG/FINAL.kicad_pcb` (detached; remove the stale `~FINAL.kicad_pro.lck` only after confirming the Windows GUI is closed). Record FAIL groups in the ledger. `versions reject` r5/stageG with the note "connectivity reference, F1-F4". Resolve the registry WARNING (144 unjudged boards). | top-level, detached script | 20 min | review.json exists; registry has no WARNING |
| 1 | DECISIONS row D71cn (this decision) and `fluxplace versions new W15 --from W14/stageD/pairs-hs.kicad_pcb --reason "hs pairs and rails before plain copper; stage gates"`. | top-level | 15 min | W15 active. The accept baseline becomes the base's own numbers (unverified: read `versions status` after `new`); CURRENT W14 3/7 no longer gates. |
| 2 | Parts and footprint fixes before any copper (parts-first): U500 pad 1.15 x 0.9 -> 0.7 per VORA1010M4T p5 (LANDPATTERN_MISMATCH), J12 silk artwork inside the outline, title text "V1.6" -> "V2.0" and off the J200 hole, C316-C319 value text hidden on B. `replace-footprint` in place. Nudges: C115 to <= 3 mm of U101.2, R505 to <= 5 mm of U501.3, FB5/C46 0.8-1.0 mm east of U5 (FB5 already approved, ledger 11-21). | Haiku with recipe; `fp-check` | 60 min | fp-check 0 FAIL; placement-check 0 new |
| 3 | Base build: strip the `[thermal.cm5]` B.Mask opening polygon (kept as a constraint, re-cut in step 10); `copy-net r5 -> base --net ETH_P0_P/N, USB3_M2_RX_P/N --lock` (guarded; if refused, those two join step 4); duplicate-UUID check (ledger 11-19 breakout bug). | Haiku; `copy-net`, `W14/scripts/unlock-plain.py` pattern | 45 min | 0 shorts, 0 new DRC key, 15/17 hs pairs audit-complete |
| 4 | **Gate G1: 17/17 hs pairs coupled, per-layer geometry, in tolerance.** PCIE_CLK and ETH_P1 by `pair_path` centre lines on the near-empty board (two Sonnet vision agents, own session copies, unique work paths). Then `tune` all hs pairs against D71cm limits while there is room (USB_J 0.245 -> <= 0.15; phase A ran with the old limits). Lock. If either pair still fails at J11 after the box: apply option 3 (K1 block move, OD2) and repeat. PCIe/USB3 P/N swap is allowed by the CM5 datasheet and is a legal tactic for PCIE_TX/CLK crossings (research canonical_flow s.4). | Sonnet x2, Fable review of the two paths; `pairaudit`, `tune` | 3 h | review PAIR_* and PAIR_IMPEDANCE_OFF = 0 for PCIE_/USB*/ETH_; score hs_pair 0; `versions accept` |
| 5 | **Gate G2: rails at width.** Rails-first ladder (outer layers, widest first), then pours: VBATT_IN, VBATT_PROT, VMODEM, +5V per `[power] pour = true`. Width per branch current if OD3 is approved (trunk 5 A 2.77 mm; U100 input branch about 2.5 A 1.1 mm; U101 input branch about 1.4 A 0.5 mm; divider/sense taps signal width), else net-wide. No rail copper on In1/In4. | detached script (`railroute`/`routeladder`, `planes`); Haiku runs it | 90 min | review POWER_WIDTH/POWER_VIAS = 0; score power 0; hs_pair still 0; accept |
| 6 | Low-speed pairs and the analog block before plain nets: INJ_PRI, MIX_OUT, SPK_COMM, SPK_RADIO, HPL_PRI, MIC, RX_ADC, SENSE (D71ci: SENSE_ADC is not a pair), U5 east pins 10-16 escaped together in pad order. Soft lock (B5): later rungs may rip and re-lay them. | `pairs` (non-hs) then one Sonnet agent on the K1/U5 window | 90 min | every K1 and U5 signal pad has a closed net or an escape past the first obstacle ring; PIN_ESCAPE_BLOCKED 0 |
| 7 | Plain route: KRT whole board first (KRT-first rule), Freerouting second. R6 never-worse on hs_pair/rf/power/power_mag. | detached script | 2 h | opens <= about 60 expected (runs 1-2: 50-68); 0 shorts |
| 8 | Close: cheap rungs once, then parallel region agents (the method that took 59 -> 9 in 3 h): disjoint net groups, one session copy each, merge by replaying lays (`W14/scripts/replay-lays.sh`), `lay --stitch`, rails laid at rail width only. Watchdog: stop any pass with 0 accepts in 30 min. | Sonnet x4 (Haiku first try per rule), top-level merges | 4 h | opens 0, shorts 0, score vector never worse on the first six terms |
| 9 | Clean-up: snapiter (off-centre vias), prune/launder (dangling), GND edge sliver rung (A2 row 1). | close rungs | 30 min | errors = 1 (C113/J205, D71cg) or 0 with the waiver recorded |
| 10 | Finish chain: thermal.recut (worked in stageG: 52 vias, 50 tracks carved), silk --fix, tune (verify only), review, fab. | `run-w14-finishG.sh` pattern | 45 min | review 0 FAIL with reasoned waivers; fab pass; `versions accept` |

Runs in parallel with steps 2-8 (data only, no board edits), all Haiku with DigiKey/Mouser first and `check_availability.py` at
production quantity:

| Review group (run 2 counts) | Action |
|---|---|
| MODEL_FILE_MISSING 132 | Path artifact of the stageD/B folder depth (ledger 11-20). Re-measure from a `W15/<stage>/` folder; expect 0. |
| MODEL_MISSING 7 (D600, J200, J205, U305-U308) | Manufacturer STEP only: Kingbright APBA2006, Attend 123A-30B00, GCT SIM8066, Ezurio BT850-ST into `hardware/lib/3dmodels/` + `model_sources.json`. If a maker publishes none: OD5. |
| MPN_MISSING 17, PART_MPN_IN_VALUE 16, PART_GENERIC 6 (C20-C23 ...) | MPN fields in spec/schematic; value strings cleaned. |
| DATASHEET_MISSING 5 (U200, U301-U303 ...) | PDFs into `docs/datasheets/`. |
| LANDPATTERN_UNCITED 6, NOT_ON_PAGE 5, PINMAP_EVIDENCE_WEAK 7, PART_PINMAP_UNSOURCED 1 (J5) | `landpattern` / `pinmap_source` citations with page; a land-pattern match is not a pinout match (D42). |
| SPEC_FOOTPRINT_MISMATCH J3, SPEC_SIZE_MISMATCH | Spec: J3 43650-0300 horizontal (dbce8fe fixed data, check the footprint field); outline 104.1 x 94.1. |
| PLACEMENT_ILLEGAL C113/J205 | `[review] waive` entry citing D71cg. |
| SUPPORT_PART_FAR 4, NET_OVER_BUDGET 3 | C115 and R505 moved in step 2 (also fixes U101_VCC 4.7 mm). C57, L3 are fixed parts and BT_INT_ANT is 95.9 mm vs 60: OD4. |
| SILK_* 9 | Step 2 and step 10. |

Total: about 1.5 working days elapsed, most of it detached runs and parallel agents.

### A2. r5 forward (fallback; also the part B test set)

Only if the owner wants a connected prototype regardless of F1-F3. Accept needs opens <= 3 and errors <= 7 against CURRENT.

| Open | Measured | Next step |
|---|---|---|
| GND zone F.Cu (99.17, 39.10) | fill sliver inside the edge clearance; island-removal ALWAYS did not remove it (stageG) | copper-pour keepout rule area over the sliver bbox, refill; not a via |
| PCIE_TX_N | F1: broken pair, not a hop | rip both legs' unlocked copper from J200 to the first locked coupled section, `pair_path`; else leave open |
| RX_ADC_P U5.12 - C46.2, 4.7 mm | pad 12 hemmed by the vias of pads 13/14 and the pad 11 track | rip RX_ADC_N + SENSE_ADC_P escapes (unlocked), FB5/C46 +0.8 mm east, re-lay pads 12-14 in pad order on F.Cu |
| MIC_P D501.1 - R20.1, 7.4 mm | path splits the In3 +5V pour, no same-net copper to stitch to | F.Cu/B.Cu only (`--layers`), or bridge the pour with a short +5V track in the same edit |
| SPK_COMM_P K1.6 - FB3.1 (30 mm), SPK_COMM_N K1.3 - FB4.1 (27 mm) | F6: unrouted nets; locked INJ_PRI/MIX_OUT/+3V3 in the way | `shove --allow-locked` on INJ_PRI/MIX_OUT only, lay SPK_COMM first, re-lay and re-lock the shoved pair |
| HPL_PRI_P/N T500 - C500/U5.20 (33/41 mm), BT3_DM U307.5 - U300.6 (30 mm) | unrouted; paths cut In2 +3V3 / In3 +5V pours | outer layers with stitch; one net per KRT call with window rip |
| 25 off-centre vias | | snapiter (199 -> 6 on CURRENT) |
Time-box 3 h, Sonnet x2. Power and F1 stay failed.

---

## B. Make the flow finish boards (ranked by impact; all board-agnostic, all inside the one gap -> repair loop)

Paths are in worktree `/var/kicad/fluxplace-wt/phase3g`; G = `fluxplace/gaps.py`, H = `fluxplace/handroute.py`, P = `fluxplace/pipeline.py`.
Each item is one Haiku recipe; Fable reviews B1, B2, B5. Never edit under `fluxplace/` while a run is alive: runs use a frozen worktree.

| Rank | Item | Problem | Evidence | Design | Files / functions | Test |
|---|---|---|---|---|---|---|
| 1 | **Stage gates** | A stage hands its failures forward; the plain router then lays hs pairs and rails as ordinary nets. | RUN2.md "4 pair(s) handed to the router as ordinary nets (c)"; F1; F3; D71cj (c) not enforced. | The same `close()` runs after every constructive stage with a scope: the gap kinds that must be zero before the next stage (`pairs`: pair-open/asym/skew + impedance for hs families; `power`: power-width/vias; `route`: none; final: all). No hand-off of hs pairs to the router: state (c) becomes a pair-open gap at that stage, where the board is empty and `pairgroup` / `ai pair_path` work. Table `[flow.gates]` with these defaults; no new command or flag. | P `run()` loop and `st_pairs`/`st_close` (call `GP.close(..., kinds=gate)`); G `close()` gains `kinds`; `pairs.py` handoff; stop.json names the gate | Fixture board with one blocked pair: flow stops at the pairs gate with exit 5 and a packet, route stage not started. W14 base: 17/17 before `power`. |
| 2 | **Region agent sessions as the `ai` rung** | Packets were written and nobody answered; single-gap packets starve; hand sessions outside the loop did the real work. | 11-20 run 2 "7 packets nobody answered"; passes 2-4: 4 h, 0 accepts; hand round 1: 4 agents, 59 -> 30 in about 1 h. | `[ai] mode = "session"`: cluster open gaps into regions with disjoint nets and bboxes; per region `session_begin` a copy with a unique path and write one brief (open list, window PNGs, rejected-ideas ledger, allowed tactics). Exit 5 hands the brief list to the orchestrator, which starts the cheapest model per region. On `close --resume` equivalent (same command, same work dir) the loop merges: replay each session ledger's accepted lays onto the base, each through the lay guard; refused lays requeue. | G `_r_ai`, `gap_packet`, `_packet_rows`; new `H.replay(ledger, dst)` (port of `W14/scripts/replay-lays.sh`); H `session_begin` unique work dir | Two regions, two scripted "agents": both ledgers merge, a conflicting lay is refused and requeued, final opens = sum of closes. |
| 3 | **Never re-run a failed rung; watchdog** | Built-in rungs re-fail on the same gaps for hours; AI answers wait behind them. | pass 2: 181 min, 32 attempts, 0 accepted; row "orchestration". | Persistent memo keyed by (rung, gap signature, hash of copper in the gap window) across passes; a pass with 0 accepts in `[flow] stall_min` (30) stops with "stalled"; rung order per kind sorted by measured yield in this work dir; `ai` session rung promoted ahead of L1 router rungs once the L0 rungs have failed once. AI memo keyed by content hash (open row 11-20). | G `close()` memo, `_call_rung`, `ai_key` | Replay pass-2 state: second pass makes 0 router calls and exits in seconds. |
| 4 | **Hand tools obey the score vector** | `lay`/`session commit` judge opens and DRC keys only; rails were laid at signal width. | F4 (power_mag 354 -> 521); round 3 "0.4 where it fit, 0.3, one 0.25" on a 5 A rail. | `lay` width default for a `[power]` net = that net's required width on the layer (neck-down only within a pad-pitch of a pad); `session_commit` and `replay` apply R6 never-worse on hs_pair/rf/power/power_mag by calling the same `G._accept`. | H `lay` (width from `power.py`), `session_commit(mode=)`; G `_accept` reused | Lay a 5 A net at 0.25 mm: refused with the needed width; same path at width accepted. |
| 5 | **Lock policy by class** | Locked low-speed pairs and early escapes seal pockets; guards treat all locked copper alike. | round 5: K1.3/K1.6 sealed by locked INJ_PRI/MIX_OUT; ETH_P2 on In2 seals U5.12-14; PIN_ESCAPE_BLOCKED U11/U12/U300 by ETH copper. | Three levels: hard (hs pairs, RF: only the pair builder may rip, whole pair, R6-gated), soft (pairs with skew limit >= 1 mm, audio: any rung may rip if it re-lays and the audit passes in the same guarded edit), free. At lock time run the pin-escape check on neighbouring pads; a lay that blocks an unrouted pad's last escape is refused. | `pairs.py` lock writer; G `_protect` -> levels; H `shove` policy from level instead of `--allow-locked`; `review` PIN_ESCAPE check exposed as a function | Pair laid across the only escape of a neighbour pad: refused at lock; soft pair ripped and re-laid by `handrepair`. |
| 6 | **Mask openings and pours come after copper** | A board-level mask polygon made every new B.Cu track a mask bridge; pours filled early get cut. | 203 solder_mask_bridge both runs; round 2 (a); round 3: 30 -> 15 once stripped. | `thermal` writes mask openings only in the finish stage (`recut`); routing stages run on a board without them. Signal-layer pours (rails, GND fill) are created at the power gate but their connectivity is judged at the end: open-pour is a normal gap kind repaired by stitch. | `thermal.py` (stage pre -> defer), P stage order, G ORDER | Board with an opening polygon: route adds copper inside, finish leaves 0 mask bridges. |
| 7 | **Pour-aware edits** | A track that splits a pour is refused although one via or a short bridge repairs it. | round 2 (b); rounds 3b-5: stitch needs same-net copper on another layer, else fails (MIC_P, BT3_DM). | In the guarded edit, in order: stitching via (landed C/C2), short same-net bridge track around the cut, alternative layer for the new track. `suggest` cost term for crossing a foreign fill, proportional to the island it would cut off; plane layers excluded by default. | H `_stitch_attempts`, `_edit`, `suggest` | In3 pour with no other-layer copper: lay accepted with a bridge track. |
| 8 | **suggest/lay latency** | 2-5 min per suggest, 1-3 min per lay under load; parallel suggest collides on one DRC file. | F9; rows 11-20 "suggest in parallel". | Until section C lands: model cache per board hash, search box = island bbox + margin grown on failure, coarse 0.2 mm pass then 0.05 mm inside a 1 mm tube, `mkstemp` DRC report, one model load for all connections of a session. | H `_model`, `suggest`, `drc_raw` | 30 mm connection < 20 s; 4 parallel suggests, no collision. |
| 9 | **Placement feedback** | Route failures that share a region never reach placement. | F5; playbook "channel capacity is not checked at placement". | (a) Pre-route review code: an SMD courtyard over the through-via break-out field of a fine-pitch connector on the opposite side, weighted by the connector's hs pairs. (b) After a failed gate, cluster failure coordinates; unfixed parts of <= 3 pads are nudged by the existing nudge action; for others write a `placement-proposal` row (part, vector, evidence) into stop.json. | `review.py`, `floorplan-check`, G `_write_swap` pattern | W14 base flags K1/J11; a synthetic board with a cap in a pair corridor is nudged and the pair completes. |
| 10 | **Pair measures** | tune and audit disagree; inner-layer pairs kept outer geometry. | F8; correction row 11-20 (tune pushed 6 pairs out); PAIR_IMPEDANCE_OFF 17. | One path measure (audit) everywhere; "leg open" must agree with kicad-cli per-net opens or raise an internal-error gap; impedance per (layer, width, gap) checked at the pairs gate. | `pairaudit.py`, `tune.py` | r5: explain or fix the three "leg open" reports. |
| 11 | **Branch-aware rail current** | Review sizes a whole net at its source current. | F3; VBATT_PROT 176 mm of branches (ladder row). | Tree currents from `[power]` loads and part facts: each segment carries the sum downstream; width per segment. Needs OD3. | `power.py`, `review.py` POWER_WIDTH | Net with a 5 A trunk and a 1 mA tap: tap passes at signal width, trunk does not. |
| 12 | Small fixes | pour-island temp-file crash; edge-sliver rung; session name collisions; tune `.kicad_pro` copy | rows 11-20 | as named | H, G | unit each |

Proof of board-agnostic: every item's test runs on at least two corpus boards (`fluxplace/corpus.py`, `docs/corpus`) besides W14;
no refs, nets or coordinates in code.

---

## C. Should fluxplace have its own router?

### Where tonight's time and failures went

| Loss | Size | Fixed by a faster/more exact engine | Fixed by a planner / flow |
|---|---|---|---|
| Close passes 2-4, rungs re-failing | about 4 h, 0 accepts | Partly: the same failures in minutes instead of hours | Yes: B3 (never re-run), B1 (gaps do not exist at the end) |
| suggest 2-5 min, lay 1-3 min, 50 closes | most of the 3 h hand phase across 4 agents | **Yes**: grid A* in Python at 0.05 mm pitch, model rebuild and two kicad-cli DRCs per edit (F9) | No |
| hs pairs laid as plain nets, rails thin | the reason r5 is not a candidate | No | **Yes**: routing order and gates (B1) |
| Locked pairs sealing pockets | K1, U5 opens | Partly: rip-up and re-route with soft locks | Yes: reserved escape channels, lock classes (B5) |
| Pour splits | MIC_P, BT3_DM, SENSE, HPL | **Yes**: pour connectivity inside the search cost | Partly: pours last (B6) |
| Mask polygon | 203 errors, blocked the power opens | No | Yes: order (B6) |
| Guard mismatch (own model vs kicad-cli keys, item-pair renames, 0.006 mm replay miss) | several refused good edits | **Yes**: one exact kernel, kicad-cli as final judge only | No |
| K1 over J11 | region of every failure | No | Yes: placement check (B9) |

A better search engine alone would not have produced a finished board tonight. A planner alone would still wait minutes per edit.

### Verdict

Yes, staged, and starting with the kernel rather than with "a faster KRT". The measured bottleneck that only new code removes
is edit latency and guard fidelity; KRT and Freerouting stay as baselines and fallbacks (KRT-first rule stands) until the
benchmark is won on at least three boards. W15 does not wait for any of this.

### Architecture

1. **Kernel: in-memory incremental geometry, clearance and connectivity.** Shapes (segment, arc, disc, polygon) per layer in a
   spatial index; clearance by net class and layer from the board rules plus the supported subset of `.kicad_dru`; union-find
   connectivity with a transaction log (apply, check, rollback); pour model (offset and clip) for fill connectivity and
   islands. Native code (Rust via pyo3 or C++), board I/O by the s-expression file, not SWIG pcbnew (the ledger's KiCad 11
   risk: 76 files import SWIG). kicad-cli DRC with refill is the judge at checkpoints and at commit, not per edit.
2. **Global planner.** Deterministic coarse pass (existing `globalroute.py`, `planroute.py`, `escapeplan.py`): capacity per
   cell and layer, escape order per connector. The AI reviews that plan as JSON plus PNG and returns a plan file: layer
   assignment per class, corridors for pairs and rails, region order, reserved escape channels, pair/power/pour order,
   placement proposals. The engine executes; the AI also answers failure packets through the existing `ai` rung.
3. **Detailed router.** Negotiated congestion (present + history cost, rip-up and re-route, the convergent form per
   research routing_algorithms s.1-2) on the kernel; coupled pairs as a median trace with the pair envelope, expanded with
   symmetric via pairs and return vias; length allocated while routing; rail nets with width ladders; cost for cutting a
   pour proportional to the island cut off; lock classes from B5.
4. **One flow.** The engine is a provider behind the existing stages and rungs (`[route] engine`, default switched only after
   the benchmark), no new commands.

### Phases

| Phase | Builds | Replaces first | Exit benchmark | Rough size |
|---|---|---|---|---|
| C0 | Benchmark harness: completion, kicad-cli errors, pairs in tolerance, rail width, vias, length, wall time; baselines KRT and Freerouting | nothing | Boards: W14 pairs-hs base, stageF 59-open board, r5, two corpus boards, one public KiCad CM4 carrier | about 400 lines Python, 1-2 days |
| C1 | Kernel + Python binding + differential tester against kicad-cli | `hrgeom.Model`, `probe`, the pre-check half of `lay`/`shove`, `window` data | 10 000 random edits on 3 boards: 0 false accepts, < 1 % false refusals; check < 50 ms; load < 2 s | 4-6 k lines native + 600 Python, 1.5-2 weeks |
| C2 | Single-connection search on the kernel (layers, vias, pour cost, soft-lock relax) | `suggest`, `hand-repair`, `pour-island` | 30 mm connection < 1 s; stageF board: >= the 50/59 the hand rounds closed, all accepted by kicad-cli | about 2 k lines, 1 week |
| C3 | Negotiated region router | rungs `krt_region`, `patch`, `patch_rip`, `region` | stageF 59 opens -> <= 5 without AI in < 15 min; never worse on the score vector | about 3 k lines, 1.5 weeks |
| C4 | Coupled pairs, symmetric vias, length while routing; rails and pours | `pairs` fallback, `pairgroup`, `railroute`, `tune` for skew | W14 base: 17/17 hs pairs in tolerance and all rails at width, empty board, < 10 min | 3-4 k lines, 2 weeks |
| C5 | Global planner hand-off + whole-board routing | KRT as default, only if won | >= KRT and Freerouting on completion and quality on >= 3 boards, wall time lower | 2-3 k lines + prompts, 1.5 weeks |

Models: Fable designs the kernel API, the differential tester and each phase recipe and reviews C1 and C3 diffs; Haiku
implements, Sonnet after two Haiku failures.

### Risks

- KiCad custom rules: only a subset can be modelled; unknown constructs fall back to the most conservative value and the
  kicad-cli judge. A board whose `.kicad_dru` cannot be covered is reported, not silently routed.
- Zone fill fidelity (thermal reliefs, minimum width, island removal, hatch): the pour model is an estimate; KiCad refill at
  checkpoints decides. The differential tester measures the miss rate per board.
- Arcs, teardrops, pad shapes (custom, chamfered), hole-to-hole and courtyard rules: each needs a tester case.
- Scope: about 15-18 k lines, 6-10 weeks of agent work with reviews. C1 and C2 pay back alone (latency); stop after C2 if
  C3 does not beat KRT on the benchmark.
- Net effect on W15: none planned. If C1/C2 land in time they speed step 8 only.

---

## Owner decisions

| # | Decision | Recommendation |
|---|---|---|
| OD1 | W15 from the pairs-stage base; r5 and W14 CURRENT become references | Yes (F1-F4) |
| OD2 | K1 is `fixed`: may the K1/T500/T501/FB block be re-placed if gate G1 fails at J11? | Pre-approve; used only then |
| OD3 | Rail width by branch current instead of net-wide 5 A (and keep dT 10 C, or allow 20 C) | Branch current yes; keep 10 C |
| OD4 | Fixed parts failing review: C57 9.1 mm from U3.11, L3 85 mm from U9.4, BT_INT_ANT 95.9 mm vs 60 mm budget | Move or waive with a reason; RF needs the owner |
| OD5 | A part whose maker publishes no STEP (possible for J200, J205, U305-U308, D600) | Decide per part after the fetch |
| OD6 | Own router: start C0-C2 now, C3+ only after the benchmark | Yes |
