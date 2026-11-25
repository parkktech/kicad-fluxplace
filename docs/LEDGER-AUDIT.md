# LEDGER-AUDIT (fluxplace fixes vs the constructive flow)

Mainline /var/kicad/fluxplace-wt/w16merge @ 11ccb77, 2026-11-25. Read-only; code/tests read or grepped, test bodies not all read. R-ids = row order of the ONE-SHOT-LEDGER table (R001 = first `| 2026-11-19 | W14 prep | replace-footprint`). Constructive flow = ready, modules, floorplan, fanout, pour-tie, placetour, lookahead, pairs, tune, critroute, plan-route, route, close, trace-finish, silk, review. Verified now: tests/test_thermal.py::test_post_adds_a_mask_opening_on_the_side and tests/test_routepar.py::test_pipeline_from_stage_creates_the_state still FAIL on mainline; test_mask_after_copper.py + 31 others pass.

## 1. Counts

Ledger, 127 rows (8 are RESULT/LANDED/MEASURED rows, skipped):

| class | rows |
|---|---|
| ENFORCED (flow code + named test) | 41 |
| IN FLOW, NO TEST | 17 |
| WORKAROUND ONLY | 17 |
| OPEN (never fixed) | 31 |
| SUPERSEDED / NOT APPLICABLE | 13 (env 2, ops rules 2, tool-not-in-flow 4, superseded 3, research/closed 2) |
| RESULT / progress (skipped) | 8 |

Failure rows scored: 119; only 41 (34 %) are fully guarded. 65 rows are in the table of section 3.

DEV-IMPROVEMENTS.md (A1-A12, B1-B11, L1-L7, X1-X3, C backlog; ~40 items): ENFORCED 14 (A1, A3, A4, A11, A12 (ready only), B10, B11, L1-L4, X1, X2, versions reject); IN FLOW NO TEST 4 (A7, B3 scripts, B4, L7); OPEN 10 (A5 pins by function / L5, A6 + L6 rotation tests, A10 live-view silence, B8 baseline failures, trace-finish T8, hole_to_hole D71dz, corner finishing D71dq, step 4c); N/A process rules 10 (A2, A8, A9, B1, B2, B5, B6, B7, B9, X3).

## 2. Regressions that already happened (same failure seen again)

| failure | rows / dates | note |
|---|---|---|
| Mask opening / keep-out copper -> solder_mask_bridge | R011 2026-11-19 (203), R042 2026-11-20 (203, both D71ck runs), R056 2026-11-20 (+78 on U100_EN), R058 2026-11-20 (blocked 3 hand opens), R059 2026-11-21, R126 2026-11-25 (fanout: 203 + 2 keep-out vias) | 6 sightings. Fix only ever covered post-route (re-cut). Fanout, pour-tie, pairs, critroute lay copper first; W16 hit it at fanout. |
| Silent / wrong input path (datasheets from cwd) | R066 2026-11-24, R067 2026-11-24, R114 2026-11-25 | "fixed" 3 times; DATASHEETS NOT LOADED only on `ready`; resolve_dir has no test. |
| Hidden illegal state behind a better number (A11) | R068 modules, R092/R094 floorplan, R093 breakout, R103 edge clearance (all 2026-11-24) | one rule, fixed stage by stage, no shared output gate for every placement-changing stage. |
| Fix improves one metric, breaks another | R096 / R100 floorplan quality (11-24), R111 back fill hard rows 4->7 (11-24), R115 rail moves broke gnss-other-rf (11-25), R116 judge key (11-25), R119/R120 judge noise | no never-worse gate over (legality, hard rows, wl, crossings) at floorplan output. |
| Silent stages (A10) | R076 ready, R082 modules, R085 parts-todo, R095 floorplan, R107 stock hang (11-24) | same gap per stage; N12 not built. |
| Pairs cannot break out | R006 11-18, R026 11-19, R034 11-19, R035 11-19, R025 11-19; USB_HUB re-lay R043 11-20 | 5 causes; floor gap + group fixed, neighbour-escape reservation open. |
| KRT rips protected copper | R023 2026-11-19 (MODEM_EN ripped), R110 2026-11-24 (11 critical nets ripped) | fixed by env switch only after the second sighting. |
| Duplicate UUIDs | R021 / R031 2026-11-19, R062 2026-11-22 | breakout fixed+tested; handroute prefix issue fixed separately. |
| Stale code measured / ops kills | R018, R030 2026-11-19; R064, R065 2026-11-24 | rules + shell traps, no test. |
| Close accepts nothing, hand rounds do | R008-R010 11-19, R049/R053 11-20, R061 11-21 | five measurements, same outcome; the close stage in the flow has no recorded run closing a sealed pocket. |
| Baseline test failures left open | R091 11-24, still failing 11-25 | B8 says known failures are named, not fixed. |
| Parts gate model path | R086 then R087 (11-24) | repo symlink masks it for CURRENT; stage copies still fail. |

## 3. WORKAROUND ONLY + IN FLOW NO TEST + OPEN, ranked for the next W16 stages (fanout -> pour-tie -> lookahead -> pairs -> tune -> critroute -> plan-route -> route -> close -> trace-finish -> silk -> review)

rank 1 = hits next. Row = ledger row id (section order above).

| rank | row | class | stage | symptom | fix location | test to add |
|---|---|---|---|---|---|---|
| 1 | R011 | NO TEST | fanout..route | thermal mask opening before copper: post-route re-cut (krtchain.run -> thermal.recut; gaps maskcut; route-stage WARN) is tested (test_mask_after_copper.py, test_gaps.py::test_mask_opening_gaps_dispatch_to_maskcut) but NO pre-route stage (fanout, pour-tie, pairs, critroute) knows a mask opening -> came back R126 | same module; plus route-stage must strip pre-route mask polygon and thermal.recut at end of krtchain (already)  | test_pipeline: constructive flow on board with mask window ends with 0 mask bridges and polygon recut |
| 1 | R126 | OPEN | fanout | fanout ignores mask openings; keep-out filter exists (fanout.py:132, test_fanout.py::test_fanout_honours_board_and_footprint_keepouts) yet 2 vias landed in CM5_ANTENNA_CUTOUT | new fluxplace/forbid.py (rule-area flags + mask-opening polygons -> no via/track for foreign nets) used by fanout._drop_illegal_claims/lane search, pourtie, critroute, pairs, krtchain | test_fanout: board with B.Mask polygon + CM5-style rule area -> 0 solder_mask_bridge, 0 items_not_allowed after fanout |
| 2 | R127 | OPEN | floorplan/placetour (re-run) , fanout | placer has no via-forbidden region model (U600 over mask window) | placegaps cost/row: part with pins that need vias inside a via-forbidden region of the other side; forbid.py as the source | test_placegaps_core: TSSOP over a mask window is a row, passive is not |
| 3 | R036 | OPEN | fanout pour-tie route | spine pour for 5 A rails (VBATT_*, VMODEM): pourspine.py never written; fanout --add-zones only builds declared [[plan.pour]] | build spine pour for rails with power.is_pour/IPC width > fit (pourspine) in pour-tie or krtchain rails step | test_pourtie: 5 A rail gets zone buffered to IPC width, review POWER_WIDTH clean |
| 4 | R005 | WORKAROUND | close | sealed opens: repair-region exists as close rung (test_repairregion.py) but never closed a sealed pocket on a board; hand rounds did | as R008: region rung must be able to close a sealed pocket in-flow | test_gaps: region rung closes a 2-net sealed pocket |
| 4 | R008 | WORKAROUND | close | close closed 0 on sealed/locked pockets (rungs refused R1/R4/no change); 4/15 came from hand accepts | gaps: accepted closes must come from rungs in the flow; add locked-copper re-escape (rip_locked) + sealed-pocket classifier before routers; measure in constructive flow | test_gaps: board with sealed pocket + unlocked blocker -> opens drop without hand step |
| 4 | R009 | WORKAROUND | close | close 78 min, opens 5->5; region/Freerouting `no session produced`; closed by hand trades | same as R008; cap region rung by seal report (15 min per failed try) | test_gaps: region rung skipped when seal report says sealed |
| 4 | R049 | WORKAROUND | close | close passes 2-4: 32 attempts 0 accepted (pairgroup, powerwiden, railpour, handrepair, krt all refused); 59 opens stayed | early-stop already (no gain 5); needs rungs that accept (R008) | test_gaps: stop after 2 zero-accept passes |
| 4 | R053 | WORKAROUND | close | strategy switch to 4 Sonnet hand sessions (handroute copies) because close gave 0 | fold the hand-round tactics (stitch, strip mask, nudge parts) into close rungs, not agent sessions | test_gaps: rung `stitch` closes a pour-island split open |
| 4 | R055 | WORKAROUND | close | hand group spk: 6/12 closed by hand lay; sealed pockets stay | as R053 | - |
| 4 | R056 | WORKAROUND | close | hand group misc; U100_EN: any new via adds 78 mask bridges (mask window again); GND island at edge; agents shared one session path | as R053; unique work path per session | - |
| 4 | R057 | WORKAROUND | close | hand round 1 merged by replay-lays.sh 59->30 | replay is a script; make accepted rung ledgers the merge format | - |
| 4 | R058 | WORKAROUND | close/route | hand round 2: strip the mask polygon for routing, recut at end -> no flow step strips it before route; lay --stitch is a hand tool | mask-polygon strip before route (see R011) + stitch rung in close (lay --stitch logic) | test_gaps: new via/track inside mask window is not a bridge after recut |
| 4 | R059 | WORKAROUND | close/route | hand round 3 power: closed with mask polygon stripped (confirms R011/R126) | same as R058 | - |
| 4 | R060 | WORKAROUND | close | hand round 4: lay --stitch via-site clearance (c0e6f18) is handroute only; MIC_P pours still split | stitch rung uses _via_site_clear and same-net short track option | test_gaps: stitch rung picks a clear site |
| 4 | R061 | WORKAROUND | close | hand round 5: locked low-speed copper walls, stranded nets; --allow-locked shove by hand | rip_locked for low-speed locked copper with R6 guarding pairs/RF; part-nudge rung | test_gaps: locked low-speed blocker is shoved, pair untouched |
| 5 | R010 | WORKAROUND | close | AI rung: needs an answering agent (packets, exit 5); rip_locked exists in gaps.py:880 but pocket stayed sealed | see R041/R008 | - |
| 5 | R041 | WORKAROUND | critroute, close | AI packets need a driver: critroute/close exit 5 waits for an agent; only W14 script check-ai-actions.sh exists, not in flow | ai rung needs an in-flow driver (exit 5 -> agent -> resume) with check-ai-actions loader validation built into the stage | test_critroute: pending packet without answer exits 5 once, answered resumes |
| 5 | R050 | NO TEST | close | hs-leg stub join <=2 mm AI action (ddd2861): no test found | test for ddd2861 stub join | test_gaps_ai: hs-leg path <= 2 mm accepted, > 2 mm refused |
| 5 | R051 | WORKAROUND | critroute, close | orchestration: check-ai-actions.sh + hand stop rules live in W14/scripts, not in flow | move check-ai-actions.sh validation into the loader (gaps_ai) so a bad answer is refused with the reason | test_gaps_ai: invalid action file refused with message |
| 5 | R052 | NO TEST | close | AI memo key (name only): gaps.py:2253 always retries a pending answer; no test by that name | memo key on name+mtime/hash for answer files | test_gaps_memo: rewritten answer with same name is retried |
| 6 | R006 | OPEN | pairs | pair-aware region repair never built (pairgroup rung regroups per connector, test_pairgroups.py, but no re-escape of a failed pair leg) | failed leg re-escape inside pairs (pairlay), one group per connector | test_pairs: leg with blocked break-out re-escapes after a neighbour moves |
| 6 | R024 | OPEN | plan-route/close | sealed-pocket classifier / swap_pins is proposal-only (gaps.py:889); the real fix was a spec change (D71ch RN400 swap), not a flow step | sealed-pocket classifier before any router; swap_pins for arrays applied under owner question | test_gaps: array of identical elements yields swap_pins proposal |
| 6 | R025 | OPEN | pairs, fanout | pairs stage does not reserve neighbour-pin escape (SENSE_ADC vs RX_ADC); [nets] not_pairs part is enforced (tests/test_notpairs.py) | pairs/fanout reserve neighbour pin escape of a pair before plain nets | test_pairs: SENSE-like net does not seal the pair pin |
| 6 | R043 | OPEN | pairs, tune | tune micro-skew / NEEDS_REROUTE pairs (USB_HUB, USB_J) need the centre-line re-lay builder (G1); not built | centre-line re-lay builder for NEEDS_REROUTE pairs; tune micro-skew meander minimum | test_tune_budget: pair 0.2 mm out of tolerance with room is tuned in with 0 new keys |
| 7 | R022 | NO TEST | close | open-pour island site search: pourisland.island_sites in close rung `pourisland`; tests/test_pourisland.py has 2 tests, none on the zone-anchor-corner case | add test for island_sites | test_pourisland: island whose anchor corner is off-site is closed by island_sites |
| 7 | R039 | NO TEST | tune | tune.shortcut skips locked copper (tune.py:375); no test | test locked shortcut | test_tune_budget: shortcut over locked copper is skipped |
| 8 | R070 | OPEN | review/route rules | hole_to_hole severity error + fab minimum in rule emission (D71dz): routerules emits the value only; no severity change; review still FAILs | routerules emit hole_to_hole as error with fab minimum; fanout/krtchain pass it to routers | test_routerules_syn: DRC rule file has hole_to_hole severity error |
| 8 | R071 | OPEN | trace-finish | hygiene clean T2 (zero-length, duplicate, dangling, stacked vias): trace-finish is measure-only (tracefinish.py header: no pass edits copper) | implement T2 clean (zero-length, duplicate, dangling, stacked vias) as stage pass guarded by H-rows | test_tracefinish: board with 3 dup vias/zero-length segs -> 0 after stage, 0 new keys |
| 8 | R073 | OPEN | trace-finish | measure-only trace-finish entry (T8) not built; W15 harness used --force | measure-only entry that skips the clean gate (T8) | test_pipeline: --measure-only runs without --force |
| 9 | R077 | WORKAROUND | review (and every stage) | 149/333 parts unresolved; run-w16-a.sh passes --assume-defaults --allow-incomplete --allow-undocumented --no-api on every stage (BYPASS var) | retire BYPASS flags from run-w16-a.sh: parts gate must pass or the stage says NOT READY; resolve 149 parts via parts-todo | test_partsgate: a flow run with unresolved parts is stamped NOT READY in review/fab |
| 9 | R086 | WORKAROUND | review | model path depth: repo-root symlink lib -> hardware/lib in the board repo; stage copies below W16/ still fail | model gate resolves ${KIPRJMOD}/../lib by ancestor search in the stage copy | test_partsgate: board in W16/stage/ resolves ../lib model |
| 9 | R087 | OPEN | review | partsgate.model_state ancestor fallback + mech exemption: patch docs/V2.0/w16/fluxplace-partsgate-models.patch not applied (model_state has neither) | apply patch: ancestor fallback + mech exemption in partsgate.model_state | test_partsgate: mounting hole needs no body; stage copy resolves model |
| 9 | R088 | OPEN | review | partsgate find_map ignores registry version (board CURRENT.kicad_pcb); test covers only name-derived map | find_map takes registry version | test_partsgate: CURRENT.kicad_pcb finds mpn_map-v20.json |
| 9 | R089 | OPEN | ready/review | two netclass calculators disagree (ETH_100R 0.15/0.25 vs 0.127/0.137); inject_netclasses only in fab | unify zgeom.outer_geometry with pair line; inject_netclasses in the pipeline, not only fab | test_netclass: one geometry for ETH pair in both calculators |
| 10 | R066 | NO TEST | floorplan..placetour | [docs] datasheets resolved from cwd: parts.resolve_dir anchors first (fix present); no test of resolve_dir; reappeared R114 | anchor + fail-loud in budget/floorplan2/placetour loaders, not only ready | test_budget: constraints copy in /tmp loads datasheets or verdict not pass |
| 10 | R072 | NO TEST | suite | test_lookahead race: fix 4b14886 (test only) is NOT an ancestor of mainline; 3/3 runs pass now | cherry-pick 4b14886 (test sync) onto mainline | test_lookahead x50 no failure |
| 10 | R078 | OPEN | every accept | versions accept / run summary count silk warnings as DRC errors (needs --allow-worse); no severity split | versions accept / run summary count DRC by severity, silk separate | test_versions: 410 silk warnings do not count as errors |
| 10 | R091 | OPEN | suite | baseline failures still failing on mainline 11ccb77 (verified now): test_routepar::test_pipeline_from_stage_creates_the_state, test_thermal::test_post_adds_a_mask_opening_on_the_side (stale vs deferred mask) | fix both baseline tests (stale thermal test vs deferral; state.json on --from stage) | the two tests themselves |
| 10 | R098 | OPEN | every stage guard | guard prints OK when collision_fail rises (audit.py:435 records only) | pipeline guard fails on any collision_fail rise | test_guard: rise in collision_fail -> guard FAIL |
| 10 | R114 | NO TEST | floorplan..placetour | datasheet dir cwd (3rd time): resolve_dir has no test | same as R066 | test_parts: resolve_dir from other cwd |
| 11 | R076 | OPEN | ready/modules/floorplan | ready on 345 parts: 603 s, one event; N12 narration not built | N12 progress every <=10 s (A10) | test_narrate: replayed stream has no gap > 10 s |
| 11 | R082 | OPEN | modules | modules silent 19 s | N12 narration | same |
| 11 | R085 | OPEN | parts-todo | parts-todo 8 min silent | progress events | same |
| 11 | R095 | OPEN | floorplan | floorplan silent 144 s; N12 heartbeat not built | N12 heartbeat | same |
| 11 | R107 | OPEN | ready | ready stock lookup hang: per-request 30 s only; no negative cache or total cap | negative cache + total cap on stock lookup | test_sourcing: unreachable API returns in <= cap, line says not checked |
| 12 | R096 | NO TEST | floorplan | legal-but-worse placement (wl +21 %): w20wl merged; no gate or test compares wl/crossings with the input | never-worse gate: output wl/crossings vs input as a stage verdict | test_floorplan: legal output with wl +20 % is WARN/refused |
| 12 | R100 | NO TEST | floorplan | start selection by written cost (w20wl): no test | start selection test | test_floorplan: passing start with lower written cost wins |
| 12 | R104 | OPEN | floorplan/placetour | support homing misses (decaps 28/79) | support homing on all graded rows | test_placegaps_core: support beyond limit is homed or named |
| 12 | R108 | OPEN | floorplan | supports beyond limit; w20home not merged | density relief before homing | - |
| 12 | R113 | NO TEST | placetour | placetour budget_min / stale (w26judge): judge guard tested (test_placetour.py::test_beats_guard_cases), budget_min fit not | budget_min next-round fit test | test_placetour: stage stops within budget |
| 13 | R079 | OPEN | modules | modules miss with no refusing check (C200) | seat() returns refusing check on every path | test_modules |
| 13 | R080 | OPEN | modules | modules re-home to a pin without legal seat (C419) | re-home only to pin with legal seat | test_modules |
| 13 | R081 | OPEN | floorplan | known: fixed connector blocks seat; floorplan side search not built | side search as placement dimension | - |
| 13 | R093 | NO TEST | floorplan | breakout moves checked by legal seat (ba0aea9): no test found | test breakout legal-seat move | test_floorplan: breakout move into a pad is refused |
| 13 | R102 | NO TEST | floorplan | free connector held to edge: floorplan2.edge_riders; no test of it | test edge connector held to edge, facing out; intent edge rows gate the stage | test_floorplan: free USB-C ends on edge |
| 13 | R112 | OPEN | floorplan | back-eligible count overstated | eligible by available back pose | test_backside: count == parts with a back pose |
| 13 | R122 | OPEN | placetour | relieve generator ~150 s of each placetour round | skip relieve generator when last N rounds gave no survivor | test_placetour |
| 13 | R124 | OPEN | placetour | two-order judge doubles KRT time | run rev order only when fwd leaves a critical net open | test_placegaps_ghost |
| 14 | R064 | NO TEST | scripts | detached run scripts (B3): run-w16-a.sh has trap + summary; shell, no test | shell test for trap (bats-like python subprocess test) | test: TERM leaves summary and no orphan |
| 14 | R065 | NO TEST | scripts | descendant-tree kill on TERM: run-w16-a.sh desc(); shell, no test | same | same |
| 14 | R101 | NO TEST | floorplan | snapshot numpy scalars (08bdec9): no test | test snapshot numpy | test_snapshot: np.bool_ field serialises |
| 14 | R103 | NO TEST | floorplan | pad copper-edge clearance in Legal (w20edge): test_legalgate.py::test_pad_inside_copper_edge_clearance_is_outside covers copper_edge_mm only; seed default-board rule is shell | test pad-edge clearance in output gate | test_legalgate |
| 15 | R002 | OPEN | prep (not a flow stage) | replace-footprint H200/H201 needed FLUXPLACE_GUARD=0; cause never found | find the guard that H200/H201 trips | - |
| 15 | R004 | OPEN | prep | no command seats a new cap next to a pin outside keep-outs (hand 02-add-reset-caps.py) | command to seat new part by pin outside keep-outs | - |

## 4. Notes

- run-w16-a.sh passes `--assume-defaults --allow-incomplete --allow-undocumented --no-api` to every stage: a standing bypass of the parts gate and incomplete-route stop (R077).
- No stage of the constructive flow handles `thermal` (grep pipeline.py): the mask window comes from the board file, so `thermal.stage=post` deferral never applies; only krtchain's end-of-route `thermal.recut` and close `maskcut` do.
- Trace-finish and corner finishing are measure-only: R070/R071/R073 and D71dq are OPEN by construction, so silk/review will still report dangling/duplicate vias, hole_to_hole, acute corners.

## 5. ENFORCED rows (stage/function :: test)

- R003: DRC 499 cap: drcfull.complete (guard/close measure) :: tests/test_drcfull.py::test_complete_appends_per_net, ::test_guard_sees_no_net_increase
- R012: stable W/<stage>.kicad_pcb link: pipeline._stage_link :: tests/test_pipeline.py::test_stage_board_has_stable_relative_link
- R013: pairs gate honours [review] waive: placementcheck.gate_failures :: tests/test_placementcheck.py::test_gate_failures_honours_review_waive
- R014: lock pairs+RF only: lockpolicy.level :: tests/test_lock_policy.py
- R015: protected_nets incl. named pairs and RF: drcguard.protected_nets :: tests/test_drcguard.py::test_protected_nets_naming_pairs_and_rf_names
- R016: versions fingerprint includes lock flag :: tests/test_versions.py::test_copper_fingerprint_follows_lock_state (process tool, not a stage)
- R017: close rungs in child process: gaps._call_rung :: tests/test_gaps.py::test_call_rung_child_round_trip_returns_krt_last
- R019: KRT before Freerouting: gaps rung `krt` first in DISPATCH, _r_krt :: tests/test_gaps.py (_r_krt tests at 384-444)
- R020: dead copper prune rung: gaps._r_prune / launder.prune_board :: tests/test_prune.py
- R021: UUID duplicates across nets (writer found = breakout): see R031
- R023: krt rung one net per call, rip candidates in window: gaps._r_krt :: tests/test_gaps.py (3 tests)
- R026: failed pairs regrouped per connector: gaps pairgroup rung :: tests/test_pairgroups.py
- R027: [power] rails routed at width: krtchain rails step (pipeline._route_cmd power_nets) :: tests/test_krtchain.py::test_rails_step_before_diff_and_zone_nets_excluded, ::test_ipc_outer_routes_non_pour_rail_on_all_signal_layers; 5 A rails need a pour: see R036
- R028: review POWER_*/PAIR_*/PIN_ESCAPE are gap kinds in score: gaps.classify :: tests/test_gaps.py (classify/score tests ~730-790)
- R029: stale zone fill: gaps._measure refills first :: tests/test_gaps.py::test_refill_runs_before_every_measure
- R031: breakout uuid counter restart: breakout.lay_text u() :: tests/test_breakout.py::test_lay_twice_gives_no_duplicate_uuids
- R034: pair gap at 0.127 floor breakout: pairlay.breakout(gap=) :: tests/test_pairs.py::test_breakout_without_the_gap_is_refused_by_its_own_partner
- R035: fanout before pairs: fanout reserves pair exits (fanout.py:237) :: tests/test_pairs.py::test_pair_leaves_through_its_fan_out_via_pair, ::test_pipeline_stage_sits_between_fanout_and_plan_route...; effect on W16 not measured
- R042: mask-opening/power kinds dispatch + snap off-centre: gaps.DISPATCH, postroute.clean (st_close) :: tests/test_gaps.py::test_mask_opening_gaps_dispatch_to_maskcut
- R046: tune measures pad-to-pad path: tune.pair_path :: tests/test_tune_budget.py (PHASE3G G1b c tests ~256-280)
- R063: versions reject refuses empty path :: tests/test_versions.py::test_reject_refuses_empty_or_missing_board_and_changes_nothing (process tool)
- R067: DATASHEETS NOT LOADED on the line: ready.py:388 :: tests/test_ready.py (lines 360-365); ONLY ready - floorplan/placetour do not say it
- R068: modules unit_illegal reported: modules.py:311 :: tests/test_layout_template.py::test_a_free_foreign_part_on_the_input_seat_makes_the_unit_illegal
- R069: loud notes first in status line: pipeline.save_state :: tests/test_partsgate.py (UNDOCUMENTED asserts 137-154)
- R074: zone net KiCad 10: tfmeasure.fills :: tests/test_tfmeasure_t1b.py::test_fills_carry_the_zone_net_of_kicad_10_and_9
- R075: H10 opens per net: tracefinish.f0 :: tests/test_tracefinish.py (H10 tests 337-380, 494)
- R092: floorplan fail-closed output gate: floorplan2/legalgate :: tests/test_legalgate.py::test_an_illegal_floorplan_is_not_the_stage_board, ::test_the_output_gate_flags_an_injected_short_and_repairs_it
- R094: construct seats on legal cells :: tests/test_legalgate.py::test_construct_places_the_largest_first_and_never_parks_a_module_illegally
- R099: placement-sides question :: tests/test_question_sides.py
- R105: hs ghost-open ladder / class-first judge :: tests/test_placegaps_ghost.py
- R106: is_rf RFC/RF1 tokens :: tests/test_krtchain.py (BT_SW_RFC, lines 403-418)
- R109: crowding report + question :: tests/test_density.py, tests/test_crowding.py
- R110: KRT rips pair/rf/rail copper: krtchain/lookahead KICAD_RIP_PREEXISTING=0 :: tests/test_krtchain.py:425-434, tests/test_placegaps_ghost.py:344
- R111: back fill raises hard rows: _accept refuses new hard rows (w22rail) :: tests/test_placegaps_rail.py
- R115: rail moves must not worsen a hard row: placegaps._accept :: tests/test_placegaps_rail.py
- R116: judge guard: placetour._beats :: tests/test_placetour.py::test_beats_guard_cases
- R117: target row must gain: placegaps TARGET_MUST_GAIN :: tests/test_placegaps_cin.py
- R118: unit-shift strip block :: tests/test_placegaps_cin.py
- R120: two-order critical ghost: lookahead (a7e9014) :: tests/test_placegaps_ghost.py::~346 fragile
- R121: row-away push-out carries blockers :: tests/test_placegaps_away.py::test_push_out_meets_the_separation, ::test_push_out_that_breaks_another_hard_row_is_refused
- R123: soft-row budget :: tests/test_placegaps_core.py::test_close_soft_rows_stop_after_soft_budget (hard-row round-2 repeats still open)

## 6. SUPERSEDED / NOT APPLICABLE

- R001: replace-footprint flip segfault: prep tool, not a flow stage; tested tests/test_replace_footprint_bcu.py
- R007: lay guard 3rd counter: `lay` is a hand tool, not in the constructive flow
- R018: ops mistake (kill tree, pkill -f): rule only
- R030: ops rule: no edits under fluxplace/ during a run
- R037: research, not built
- R054: handroute suggest report collision: handroute mkstemp (tool, not in flow)
- R062: handroute 8-hex id prefix collisions: tool, tested tests/test_handroute_ids.py
- R083: env: DigiKey 403 from this host (Mouser-only evidence)
- R084: env: molex/Murata CDN timeouts
- R090: closed
- R097: superseded: floorplan-3..7 construct succeeded
- R119: superseded by R120
- R125: procedure: compare under same judge
