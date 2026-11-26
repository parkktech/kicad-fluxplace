# Fable cross-check of the outside review (2026-11-26)

Code read: `/var/kicad/fluxplace-wt/w16merge` @ e72d5b8 (read-only). Inputs: PROMPT-2026-11-26.md, OUTSIDE-REVIEW-2026-11-26.md,
ONE-SHOT-LEDGER.md (156 rows), LEDGER-AUDIT.md (@ 11ccb77), PLAN-PLACE-GAPS.md, FLOW-REDESIGN.md, TEST-BASELINE.txt, tools/merge-gate.sh,
RESUME-W16.md NOW block, W16/view/review-preview-w40.json. Nothing was run except greps and one JSON count; no test, no route.
Not verified: FLOW-REDESIGN's "25 repair entry points / 17 open counters" (quoted by the outside review from the doc; not re-counted),
routepar's own width derivation, the content of the four in-flight branches beyond their diffstat and commit subjects.

Plan and tickets: `PLAN-2026-11-26.md` (same folder).

## 1. Claim verification

Paths are relative to `fluxplace/` in w16merge.

| # | Outside claim | Verdict | Evidence |
|---|---|---|---|
| 1 | `drcguard.measure()` cache key = path + mtime + size | CONFIRMED (low severity) | drcguard.py:189 `key = (p, st.st_mtime_ns, st.st_size)`. `drc_json` reads the sibling `.kicad_pro` / `.kicad_dru` (drcguard.py:104-113), which are not in the key, so a rule edit with an unchanged board returns the old measure. In-process cache, 4 entries: tool versions are irrelevant. Same key in gaps.py:374 (`_snapshot`). |
| 2 | `keys_from_report()` drops zone violations; other functions partly restore them | CONFIRMED | drcguard.py:47 drops every violation with a "Zone" item. `keys_per_net` restores only `clearance` zone items (drcguard.py:68-70). `measure_from_report` keeps zone items in `keys` for severity "error" but leaves them out of the `errors` count that feeds the score (drcguard.py:179). The module docstring says the flicker was not reproduced (5/5 identical), so the exclusion is inherited, not measured. |
| 3 | `accept()` lock check depends on optional `locked_ok` | PARTLY | Worse and better than stated. R3 is dead code: neither caller passes `locked_ok` (gaps.py:437, handroute.py:1534), so drcguard.py:286 never runs. Lock enforcement is elsewhere and unconditional for stages: `pipeline._commit_point` (pipeline.py:1418) -> `boardwrite.commit/verify` PROTECTED_CHANGED (boardwrite.py:226, 339), plus the manifest check in close (gaps.py:2331). It refused the w34power rails on the real board (ledger 2026-11-26 finish-2). Gap: code that runs outside a stage (the "real-board check" scripts) has no lock guard at all. |
| 4 | `gaps.impedance_off_nets()` exception -> empty set | CONFIRMED (contained) | gaps.py:80-81. One caller, pairsonly.py:212: with an empty set a complete pair off impedance is "kept_original" (pairsonly.py:232). Not on the release path: review runs `check_pair_geometry` itself. Same fail-open pattern, more serious: pipeline.py:605-608 `PW = {}` on any exception, so the rails route at signal width with no finding. |
| 5 | `pipeline.py` warn / na / skipped and approximate opens can satisfy gates | PARTLY | Verdicts: pipeline.py:33. Approximate fallbacks exist: pipeline.py:102-103 and 112-113 (in-process count when kicad-cli gives no report); a crashed close is `warn` (pipeline.py:1002). But the release gate is the review stage, `fail` on any non-zero rc (pipeline.py:1087), and it does its own kicad-cli count. The real hole is not in pipeline.py: `run-w16-a.sh` passes `--assume-defaults --allow-incomplete --allow-undocumented --no-api` to every stage (LEDGER-AUDIT R077; pass-through at pipeline.py:1083). |
| 6 | `lockpolicy` protection partly from inferred names | PARTLY | Name inference is real (lockpolicy.py:22-27: `graph.diff_pairs` by naming, `review.is_rf`, `netclass.pair_kind`), but it is unioned with the declared sets (`[rf] nets`, `route.controlled`, patterns, `[nets]` rules) and missing names or constraints give HARD (lockpolicy.py:16-17, fail closed). Risk left: an undeclared RF net with an unusual name is FREE. Larger finding the review missed: five separate class tables decide "is this net special" (netclass.classify, ripolicy.controlled_nets ripolicy.py:31, drcguard.protected_nets drcguard.py:230, lockpolicy.level lockpolicy.py:14, batchroute.CONTROLLED batchroute.py:85). |
| 7 | `placetour` judge needs consistent electrical context | ALREADY DONE (mostly) | Deterministic two-order judge and the `_beats` guard: w26judge 5f416a2, tests/test_placetour.py::test_beats_guard_cases, ledger 2026-11-25 "ghost judge order-sensitive" and "judge comparability" (re-judge identical). Left: the ghost route uses the signal width for every net (rails, RF), so it does not prove a 5 A corridor. |
| 8 | `freshproc` should be the standard mutation boundary, today it is an exception | CONFIRMED | 5 call sites only: pipeline.py:985, pipeline.py:995, power.py:838, snapiter.py:246, snapiter.py:429. Every stage runs in the pipeline process (`_run_cli`, pipeline.py:133-145, 16 call sites). 33 `.Remove(` calls remain in 17 modules. Detection is ALREADY DONE: `_pcbnew_state_guard` (pipeline.py:1397) after every stage, tests/test_pcbnew_state.py, and the Delete-not-Remove fix (ledger 2026-11-25 pour-tie, w28delete). Detection names the stage after the damage; it does not prevent it. |
| 9 | A plain router can route RF or power at default width | CONFIRMED on mainline | RF: the KRT plain step takes every open net (`rtodo`, pipeline.py:632, 641-643); batchroute only orders controlled nets later (batchroute.py:85-109) and has no per-class width or layer. Only Freerouting skips them (pairfb.py:426-430). Board proof: BT1_RF 50.6 mm, BT2_RF 11.1 mm, BT3_RF 9.1 mm, BT_SW_RFC 50.1 mm at 0.150 mm on In2/In3, ~71 ohm (review-preview-w40.json, ledger 2026-11-26 row "RF nets routed by the plain router"). Power: rails with no current figure are left out of `route_widths` (power.py:164-182) and any exception empties the table (pipeline.py:605-608). In flight, not merged: w43rf 211b2d6 (plain step skips RF), w46railsrc 2bbbf19 (plain step skips rail nets). |
| 10 | 22 opens vs 23 unrouted findings is an unexplained disagreement | WRONG | Same board, same count. review-preview-w40.json holds 23 UNROUTED rows = 1 summary row ("22 open connection(s), kicad-cli after a refill (22 pcbnew cluster pair(s) listed)") + 22 per-connection rows. The fix is presentational: the summary row should not carry the same code and level as the rows it sums. |
| 11 | Guard rejects every intermediate operation; no working/candidate/incumbent split | PARTLY | Close already works on a copy with `best.kicad_pcb` as incumbent (gaps.py:2182-2189); each rung builds a finished candidate that is judged once. What is missing is depth: one rung = one transaction that must lower the score by itself (R4, drcguard.py:291) with no new DRC key (R2, drcguard.py:281-285). Chained rungs (rip A, route B, re-route A) exist only inside single rungs (region, krt_region; krt_locked on w39legs 6c59096). |
| 12 | Needs a lexicographic, magnitude-aware score | ALREADY DONE | gaps.py:346-366: hs_pair, rf, shorts, opens, power, power_mag, pair, pin_escape, errors, quality; sev and mag are magnitudes. Power-mag progress counts (gaps.py:425-431). w42widen 723710b made POWER_WIDTH length x shortfall. |
| 13 | Power check charges every branch the full rail current | ALREADY DONE | w40flow 69d60e3 (`powerflow.py`, tree-flow solve), w42widen e72d5b8. Not done: resistive solve for meshed pours (pours are credited, not solved). |
| 14 | Failed critical pairs are demoted behind easier ones | PARTLY | The unresolved-first swap was measured and reverted (ledger 2026-11-26 ETH_P1: it lays ETH_P1 but PCIE_RX and USB3_M2_TX then fail). Order is not the lever; the escape at U12 is. |
| 15 | Exit-5 packets dead-end the flow | ALREADY DONE (mostly) | Defaults used when unanswered (pipeline.py:1359), deferral table `GATE_DEFER` (gaps.py:205), w29pairsflow / w30packets. Open: `--from <stage>` without state re-opens an earlier gate (ledger 2026-11-25 "pipeline --from critroute without state"). |
| 16 | Real-board checks do not run through the production guard | PARTLY | tests/test_commit_point_real.py and tests/test_gate_measure_real.py do. The per-fix "real check" of the worker agents is still a free script; w34power's check ran with no manifest and missed PROTECTED_CHANGED. |
| 17 | 41 of 119 rows enforced | CONFIRMED | LEDGER-AUDIT section 1 (@ 11ccb77): 41 ENFORCED, 17 NO TEST, 17 WORKAROUND, 31 OPEN, 13 superseded. Clustered in PLAN section 4. |
| 18 | Merge gate ~30 min, should be tiered | PARTLY | It is serial: tools/merge-gate.sh runs one `pytest -q`; pytest-xdist is not installed; the machine has 32 cores. Tiering is rejected (section 4); parallelism is not. |

## 2. Answers A-G (where I differ from or go beyond the outside review)

**A. Shape and order.** Keep the stage list. The outside review asks for a "feedback-driven solver"; the code already has the mechanism
(gap -> repair dispatcher per gated stage, `GATE_DEFER`, placegaps for placement). What is missing is narrower:
1. Two gap kinds have no owner before review: `support-far` and `net-budget` (w45supports) and `rf-impedance` (ledger row 186). A finding that
   only review can see is the definition of late feedback. Rule: every review FAIL code maps to a gap kind with an owning stage, or to a data
   or owner item. A test should assert the mapping is total.
2. The lower bound belongs in placement: a net whose pin-to-pin Manhattan distance already exceeds its budget (HUB_XTAL 20/12 mm, RF_EXT 62/35,
   BT1_RF 43/27) cannot be routed in budget. That check costs milliseconds and needs no ghost route.
3. Order: escapes of pair pins and rail via sites must be planned (reserved) before plain fanout; copper order after that stays as today.
   Moving pairs before fanout was the old proposal; the measured ETH_P1 case says the pair needs a reserved exit at U12, whoever lays first.
4. I disagree with "stop chasing the 22 opens". 12 of the 22 are ETH_P1/P3 legs and 4 are the USB-C twin: both are escape problems that a
   placement re-run does not remove. But a placement re-run is required anyway (10 SUPPORT_PART_FAR + 9 NET_OVER_BUDGET cannot be repaired
   in copper), so the next full route must be on the re-placed board. One more 2-3 h route on the old placement is only worth it as the check
   run of w43rf / w46railsrc / w39legs.

**B. Contracts.** One table, not a new schema: `netclass.classify` extended to return, per net, owner step, width per layer and allowed layers
(from zgeom, power.route_widths, constraints). The five class tables of claim 6 read it. Two enforcement points, both existing seams:
`routeowners` before the route (no net handed to a step that is not its owner; ROUTE_NET_UNOWNED already exists, pipeline.py:932) and
`_commit_point` after every stage (new copper of a contract net off its width or layer is refused, like PROTECTED_CHANGED). The second catches
whatever writer is added later, which is the property the outside review wants. Fail-open exceptions (claim 4) become a failed stage.

**C. Convergence.** Score and incumbent exist. Three changes:
1. Compound transactions: a rung may rip a named blocker set, route the target, re-route the displaced nets, and is judged once (R0-R6)
   against the incumbent. This generalises krt_locked (w39legs) and krt_region; no new command.
2. Memo on (gap id, blocker set, input sha): a refused or flat attempt is not repeated until one of the three changes. LEDGER-AUDIT R049:
   32 attempts, 0 accepted, same inputs.
3. Failure cause on every router result: budget exhausted, sealed (no path at any budget), or invalid input. Only "sealed" escalates to
   escape rebuild or relocate; today the ladder walks on regardless.
Negotiation: KRT is an external grid router, so history cost goes into what we control: the rip-set choice and the order inside a compound
rung (cells from channel.py / density.py that were contested in refused attempts get a history weight; nets crossing them are ripped first).
R2 stays, but keyed on error severity only for compound rungs, with the warning delta carried in the `quality` score term.

**D. Time.** Measured levers, in order of return per hour of work:
1. Merge gate: 32 cores, serial suite. xdist with a serial group for the pcbnew tests should take the gate from 20-30 min to a few minutes.
   Same tests, same baseline rule, so the owner rule holds. Unmeasured; needs three identical fail sets serial vs parallel before the switch.
2. Pairs stage (12-107 min): per-pair result memo on (pair, endpoints, obstacle sha in its corridor) and a budget split by evidence of
   progress; ETH_P1 at x10/x30 budget changed nothing.
3. Placetour: reverse-order judge only when the forward order leaves a critical net open (ledger 2026-11-25, 312 s vs 131-164 s); relieve
   generator skipped after N empty rounds.
4. Stage isolation (claim 8) gives a hard per-stage timeout and a clean kill, which removes the "hung stage" class.
Region-only DRC as an acceptance authority: no. As a pre-filter before the full DRC: yes (drc_json `only_rules`, 2 s, exists).

**E. Regressions.** The suite has 248 test files and two real-board seam tests. Missing layer: microboards that reproduce a ledger mechanism
and run the real stage function through `_commit_point` and the stage gate. Bind ledger to tests mechanically: a ledger row marked fixed must
name a test id; a lint step inside tools/merge-gate.sh fails when the id does not exist. That turns the 34 % figure into a number the gate prints.
Metamorphic tests worth having first: footprint-order shuffle (judge, critroute), save/reload identity, cwd change (datasheet dir, three
recurrences R066/R067/R114).

**F. Borrow.** Agree on PathFinder as the model for rip-set choice (not as a router rewrite), on signal-path level budgets (xSignals: budget
through series parts, which NET_OVER_BUDGET and the analog pairs through transformers need), and on escape-first planning for dense
connectors. Defer CP-SAT: `ortools` is a new dependency and the escape assignment at one component is small enough for min-cost matching.

**G. Process.** Keep ledger, merge gate, worker branches. Change:
1. Definition of done adds one line: "the measured check ran `pipeline --from <stage>` (stage guard and commit point), not a script".
2. A merge queue in the plain sense: three in-flight branches edit krtchain.py (w39legs, w43rf, w46railsrc). Each passed or will pass its
   gate alone; the gate must run on the merged result, in order, one at a time.
3. The run script's bypass flags (R077) are the largest standing hole; each one needs an owner date to be removed.

## 3. Merged top-10

Impact = effect on reaching a fab-ready W16 and on recurrence. Effort in focused days. All extend existing modules; none adds a command or flag.

| # | Item | Impact | Effort | Depends on | Extends |
|---|---|---|---|---|---|
| 1 | Land the four in-flight branches in order through the gate, then re-place and route: w43rf, w46railsrc, w39legs, w45supports | Critical: RF 4, rails 5, deferred legs, supports 10, budgets 9 | 1 | merge gate; no run importing w16merge | krtchain.py, rfroute.py, railbranch.py, gaps.py, placegaps.py |
| 2 | Net contract table + two checks (routeowners before route, copper-contract at `_commit_point`); fail-open exceptions become stage FAIL | Critical: no class routed off width or layer by any writer | 3 | 1 | netclass.py, routeowners.py, boardwrite.py, pipeline.py |
| 3 | Review-code -> gap-kind -> owner-stage map is total; placement lower bounds (net budget by pin distance, support distance) in the floorplan/placetour gate | Critical: no placement defect first seen at review | 2 | w45supports | placegaps.py, gaps.py (GATE_DEFER), review.py |
| 4 | Pair and rail escape reservation before plain fanout (U12/U11 Ethernet, J11/J200 PCIe, J5 twin) | Very high: 16 of 22 opens are this | 5-8 | 1 | escapeplan.py, escreserve.py, pairs.py, fanout.py |
| 5 | Close: compound rung transactions, attempt memo, failure cause, history-weighted rip sets | Very high: close accepts instead of 0/32 | 4-6 | w39legs | gaps.py, closure.py, channel.py |
| 6 | Stage tests through the production guard: golden microboards per ledger mechanism + ledger-row -> test lint in the gate | Very high: stops recurrence; raises 34 % | 3, then per row | none | tests/, tools/merge-gate.sh, tests/powerboard.py |
| 7 | One measurement record: content-hash cache key incl. rules, exact/approx flag, UNKNOWN fails gates, zone errors counted, summary rows not double-counted | High: removes false accept/refuse | 2 | none | drcguard.py, gaps.py, review.py |
| 8 | Each stage runs in a fresh interpreter (stage result is already a JSON-able dict); per-stage timeout | High: SWIG decay class gone, clean kills | 2 | none | pipeline.py, freshproc.py |
| 9 | Iteration time: parallel merge gate, pairs memo and budget split, one-order judge unless fragile | High: gate 25 -> ~5 min (unmeasured), pairs stage bounded | 3 | 6 for the gate | tools/merge-gate.sh, pairs.py, placetour.py |
| 10 | Via/track-forbidden region model shared by placer, fanout, pairs, critroute; finishing stages that edit (trace-finish clean, hole_to_hole, silk) | High: 6-sighting mask regression; 186 silk + hygiene FAILs block fab | 4 | none | fanout.py keep-out filter, placegaps.py, thermal.py, tracefinish.py |

## 4. Outside recommendations rejected or deferred

| Recommendation | Decision | Why |
|---|---|---|
| New YAML routing-contract schema and compiler (4.1) | Deferred; table inside netclass instead | One flow, board-agnostic: constraints TOML is the declarative source already. A second schema is a second interpretation, the thing the review warns about. |
| Tiered merge gate that moves tests out of the blocking path (7.5) | Rejected | Owner rule 2026-11-25: every merge passes the full gate. Parallelise the same suite. |
| "Stop chasing the 22 opens", five-phase rebuild (10) | Partly rejected | Re-place: yes (required). A rebuild of the route architecture first would stall the board; 16 of 22 opens are escape problems that survive re-placement. |
| Five core operations API (9.4) | Deferred | The gap -> repair dispatcher is that API (gaps.RUNGS, placegaps). Renaming it buys nothing now. |
| CP-SAT escape assignment (8.5) | Deferred | New dependency; matching on escapeplan first; revisit if a connector fails with matching. |
| Resistive network solve for meshed pours (4.5) | Deferred | Tree-flow landed (w40flow); no failing case on this board needs a mesh solve. |
| 30 cheap / 6 detailed / 2 full-route candidates (2.3) | Deferred | Two full routes are 4-6 h today. Revisit when a route is under 60 min. Tiers 0-3 are placetour + ghost judge already. |
| Board-size or stackup alternatives as an escalation (5.5) | Owner question row only | D71cd fixes 6 layers; grow-board-before-layers is an owner decision, never a rung. |
| KiCad IPC adapter boundary (6.1) | Deferred | KiCad 10.0.5 pinned; headless IPC arrives with 11. Item 8 gives the process boundary an adapter would sit behind. |
| Freerouting as an "eligible fallback" (3) | Accepted as is | Matches KRT-first. Freerouting already skips controlled nets (pairfb.py:426). |
| Widening RF in place on inner layers | Rejected (agrees with the review) | Project rule: 50 ohm RF on outer layers; floors >= 0.127 mm hold either way. |

## 5. Follow-up questions for the outside reviewer (OpenAI)

Context for all: you reviewed GitHub `main`, which is older than our mainline branch. Line numbers below are from the newer branch.

1. **22 vs 23.** We checked the report: it has 23 rows with code UNROUTED, of which one is a summary row ("22 open connection(s), kicad-cli
   after a refill") and 22 are per-connection rows. Board and count agree. Given that, do you still see a measurement defect here beyond the
   summary row sharing a code with its detail rows, and would you keep "one authoritative measurement system" at priority 5?
2. **Lock enforcement.** `drcguard.accept()` takes `locked_ok=None` and no caller passes it, so that rule never runs. Locks are enforced
   instead at a stage commit point: a manifest of protected items (locked copper + complete pairs, 244 nets on our board) is compared after
   every stage and a change fails the stage. It did refuse a power-widening step on the real board. Is a per-stage manifest diff sufficient in
   your view, or do you see a concrete failure it cannot catch that a per-transaction authorised write set would?
3. **Negotiated congestion with an external router.** Our first router (KiCadRoutingTools) is a separate grid router whose cost function we
   do not own. We can only choose which nets to rip, the order they are re-routed in, widths, layers and keep-outs. Is PathFinder-style
   history still useful when it can act only through rip-set choice and ordering (history weight per channel cell contested in refused
   attempts)? If you think it needs cost-map access, what is the cheapest substitute: synthetic keep-outs with decaying strength?
4. **Compound transaction vs the "no new DRC key" rule.** Close judges one finished candidate against the incumbent with a lexicographic
   score (hs_pair, rf, shorts, opens, power, power_mag, pair, pin_escape, errors, quality) and also refuses any DRC violation key that occurs
   more often than before (multiset test, decimals stripped from the key). In one measured run 32 attempts gave 0 accepts. For a multi-net
   rip-and-re-route candidate, would you keep the key rule for error severity only and move warnings into the score, or drop the key rule and
   rely on the score plus per-net protection? What failure would each choice let through?
5. **Pair order.** We measured your "difficult pair first" idea on one case: Ethernet pair P1 is walled in at its PHY-side part by pair P0's
   escape copper. Laying P1 first closes P1, but then PCIE_RX and a USB3 TX pair (ranked above Ethernet) fail. A x10 and x30 search budget
   changed nothing. We concluded order is not the lever and plan to reserve pair exits per component before any fanout. Do you agree, and how
   would you size a reservation (exit stub length, via pair site, keep-out width) so it does not starve the neighbours on a 0.5 mm pitch part?
6. **CP-SAT for escapes.** You suggest CP-SAT for the discrete assignment at a congested connector. Our candidate is min-cost matching
   (pins -> exit direction/layer/via site) per component, which cannot express pairwise conflicts between two chosen exits. On a 10-pin
   magnetics-side footprint with 2 pairs and a USB-C receptacle with interleaved twin pads (DM DP DM DP, where the polarity-correct pair is
   the middle two pads and needs two via taps), is matching plus a conflict-repair pass enough, or is the pairwise term the whole problem?
7. **Zone noise in the guard.** Violations with a zone item are excluded from the acceptance key set because an older module recorded zone
   refill flicker (totals of 872-875 on one board). A later test found 5/5 identical key multisets with and without a pcbnew round trip, so
   the flicker is not reproduced. Would you remove the exclusion outright, or keep it for warnings and count zone errors? What would you use
   as a canonical zone-fill state for the comparison?
8. **Placement lower bounds.** Nine nets exceed their length budget after routing (crystal 20 mm against 12, RF 62 against 35 and 43 against
   27). We plan to refuse a placement when pin-to-pin Manhattan distance exceeds the budget, and to treat the rest with a ghost route. For
   budgets that run through a series part (matching network, AC caps, transformer), should the bound be per segment or end to end, and what
   margin over Manhattan do you use for a 0.15 mm grid router before calling a placement infeasible?
9. **Process isolation granularity.** Today all stages run in one Python process; five call sites use a fresh interpreter, 33 `Remove()`
   calls remain in 17 modules, and a guard after each stage detects decayed SWIG state. We plan one fresh interpreter per stage (about 12 per
   run) rather than per mutation. Is per-stage enough given that the close stage runs dozens of rung attempts in its one process, or is there
   a reason to pay the pcbnew import cost per attempt?
10. **Merge gate.** Our owner rule is that every merge passes the full suite (~3100 tests, serial, 20-30 min, 32 idle cores). You propose
    tiers. If tiering is not allowed, is there anything beyond plain parallelism and a serial merge queue that you would add, for example
    running the microboard stage tests first and aborting early?

## 6. Round 2 (outside follow-up)

Source: OUTSIDE-FOLLOWUP-2026-11-26.md. Code re-read in w16merge @ e72d5b8; nothing run.

### 6.1 Their new code claims

| Claim | Verdict | Evidence |
|---|---|---|
| Close rungs run in child processes via `_call_rung()`; only `ai` and `inproc` stay in-process | CONFIRMED | gaps.py:2104-2118 (`python -m fluxplace.gaps --rung-job`), exception at gaps.py:2107; called at gaps.py:2292 and 2319. My round-1 question 9 was wrong to say close runs its attempts in one process; claim 8 of section 1 stands for stages only. |
| `measure_from_report()` keeps zone errors in the keys, leaves them out of `errors` | CONFIRMED | keys for severity "error": drcguard.py:175 (no zone filter); count: drcguard.py:179 (`not _zone_item(v)`). For severity "all" the keys also drop them (drcguard.py:173 via drcguard.py:48). |
| Locked-item policy allows additions to locked nets with no width/layer check | CONFIRMED | boardwrite.py:224 ("copper may be ADDED to the net"), boardwrite.py:338-340 ("additions are fine"). No width or layer test in `changed`. This is how w37protect lays an unlocked wide piece over locked copper, and also how an RF net with locked fan-out could gain 0.15 mm inner copper. |
| Resume re-seeds the protected manifest | CONFIRMED | boardwrite.py:328-335 (`fresh_manifest`: stale -> `seed(board, keep=man)`), pipeline start pipeline.py:1837-1838, close start gaps.py:2211. It is logged ("re-seeded from the board") but the difference is not recorded and nothing is refused. |

### 6.2 What changes in the top-10

| Round 1 | Round 2 | Change |
|---|---|---|
| 1 land branches | 1 | unchanged |
| 3 placement gate | 2 | Up. Lower bound uses the octilinear metric between pad-access points, per segment through series parts, no margin (my Manhattan bound over-rejects by up to 41 %). Built before the placement re-run (PLAN A8). |
| 4 escape reservation | 3 | Up. Bundle enumeration with pairwise conflicts and a twin-receptacle template replace min-cost matching. |
| 2 net contract | 4 | Down one (A1/A2/A6 cover RF and rails meanwhile). Adds: width/layer check on copper added to locked nets; re-seed records its diff. |
| 6 golden fixtures | 5 | Golden set first in the gate, abort early, full suite still required. |
| 5 close convergence | 6 | Starts with a refusal histogram (all failing rules per attempt). Error-key rule kept. Adds the write-scope check. History only from routing failure or budget exhaustion. |
| 10 forbid regions + finishing | 7 | unchanged |
| 8 stage isolation | 8 | Smaller: rungs are already isolated; stages and the `ai` executor remain. |
| 7 measurement | 9 | Down. Summary-row code, rules in the cache key, zone errors counted; no new measurement system. |
| 9 iteration time | 10 | Parallel gate starts conservative, compared with serial; gate completion check done by the coordinator. |

Accepted from them without change: keep the stage manifest as the protection gate; no synthetic keep-outs yet; CP-SAT optional.
Corrected in my round 1: the Manhattan bound; the claim that close attempts share one process.

### 6.3 Round 2 questions

1. **Scope of the bundle enumeration.** You propose enumerating pair bundles per component. Our failing case has two ends: Ethernet pair P1
   runs from a 10-pin protection part (0.5 mm pitch, pairs P0 and P1 on the same part) 15 mm to the magnetics and 36 mm to the connector, and
   P0's escape at the first part is what walls P1 in. If we enumerate per component, the bundle chosen at one end can be the one that makes
   the far end or the channel between them infeasible. Would you enumerate per component and verify with a ghost route of the affected pairs,
   or enumerate jointly over the two or three components that share a channel (candidate count multiplies: k per pair, 2 pairs, 3 parts)?
   At what candidate count would you stop enumerating and accept the solver dependency?
2. **Reading the refusal histogram.** Before changing the close acceptance rules we will record every failing rule for each of the refused
   attempts (connectivity, shorts, new error key, protected net, score not lower, target still there). Our acceptance needs a strictly lower
   score. If most refusals turn out to be "score equal" with no other rule failing (a legal sideways move), would you accept equal-score
   candidates under a memo that forbids revisiting a state, or keep strict improvement and put the effort into better candidates? And which
   share of "new error key" refusals would make you conclude the candidates, not the gate, are the problem?

## 7. Round 3 (outside round-2 answers)

Source: OUTSIDE-ROUND2-2026-11-26.md. Every point accepted; details and ticket changes in PLAN section 7.

| Point | Decision | Effect on the top-10 |
|---|---|---|
| Contracts on all newly added controlled copper | Accepted | Item 4 (contracts): every contract net, protected or not. |
| Zone errors measured now, enforced on their own track | Accepted | Item 9 (measurement) splits: record zone-inclusive values now; enforce on a reproducibility trigger, not after the close work. |
| Unexplained reseed refused, old manifest kept, diff reported; intended change invalidates evidence | Accepted | Item 4: explanation comes from the versions registry; no new flag. |
| Escapes: local generation, joint selection with backtracking (10 000 expansions), shortlist route verification incl. higher-rank pairs | Accepted | Item 3 (escapes) rescoped; solver still deferred, now on a measured criterion. |
| Close: strict incumbent, refusal record with deltas and UNKNOWN, inspect equal-score cases, then beam 3 x 2 if earned | Accepted | Item 6 (close) gains two ordered pre-steps; the beam is conditional. |
| Interrupt / resume tests | Accepted | Items 3 and 6 acceptance. |

Order of the top-10 is unchanged from section 6.2.

### 7.1 Round 3 questions

none
