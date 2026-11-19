# Flow redesign: one build, one gap loop, one guard

Design review of 2026-11-19. Read-only: no code was changed for it. Every claim cites the code or the call logs it
came from; anything not read is marked **unverified**. Measured on `main` at a6fe6f9.

Revision 2 (2026-11-19, `main` at 632b82c): the owner's decisions are in section 7 as DECIDED. F2 has landed as ce46aea
(`drcfull.complete`), so phase 0 is done. New in this revision: stage 0 (schematic sync), outline and size intake,
proven routers first (2.8), the outer escalation ladder L0-L6 (2.9), the fast pre-check (2.11), the attempt log and
pruning rule (2.12), the event stream (2.13), the corpus and score (2.14), and a longer phase list (section 5).

Goal (owner): `fluxplace` builds a board in one pass. What is left after the stages is sorted by kind and pushed back
through the repair method that fits it, until the board is clean or nothing improves. Repair parameters live in the
flow, not in flags. Existing repair code is kept and called from one dispatcher.

## 1. Diagnosis

### 1.1 Size of the surface (recomputed from the argparse tree and every `.fluxplace/calls.jsonl` under /var/kicad, 798 calls)

| measure | value |
|---|---|
| top-level commands | 105 (57 in `cli.py`, 48 registered by `hrcli`, `partscli`, `plancli`, `fancli` and 16 other modules); 112 with the `versions` actions |
| option flags | 1,063 own flags + `--report-dir` on every command; 356 distinct flag names; 68 positionals |
| flags used on a real board | 228 of 1,063 (21 %); 93 of 356 distinct names |
| commands never run on a real board | 67 of 112, among them `repair-region`, `repair`, `closure`, `route-diagnose`, `join-gaps`, `relieve`, `thermal`, `auto`, `compact` |
| rule-value flags never used | `--track` (9 parsers), `--clearance` (9), `--via` (8), `--kicad-cli` (15), `--jar` (4), `--planes` (3), `--passes` (5): 0 uses each |
| orchestrators in code | 5: `pipeline.FLOWS` classic and constructive (`pipeline.py:26-29`), `cmd_auto` and `cmd_compact` via `_stages_outline_to_fab` (`cli.py:1172`), `tournament`, `adaptive.route_adaptive` (`adaptive.py:276`) |
| orchestrators outside the code | one shell script per board version (`W12`-`W14/scripts/run-*.sh`) plus hand scripts (`add-path.py`, `02-add-reset-caps.py`, `01-move-u8.py`, `03-thermal-mask.py`) |

The brief said 56 commands and 484 flags; that counts `cli.py` only. The module-registered commands double it.

### 1.2 Why a run does not finish in one pass

| # | cause | evidence |
|---|---|---|
| C1 | The flow stops at `route`. The constructive flow is `ready ... pairs, plan-route, route, tune, silk, review` (`pipeline.py:28`). `patch`, `hand-repair`, `repair-region`, `drc-fix`, snap, `via-min` are not stages. They run from `run-w13-finish.sh` in a fixed order, once, with no way back. | `run-w13-finish.sh` steps 1-7; `snapiter` has no command and is called with `python3 -c` |
| C2 | No classifier. Each repair finds the opens itself and treats them alike. A method that meets a gap it cannot handle prints a hand-off and nobody reads it. | `handrepair.py:96` marks pair nets "needs the pair re-route, not handled here"; `repairregion.py:137-139` skips RF and pair nets; `tune.py:399-415` returns `needs_reroute`; `pairsonly` returns `kept_original` |
| C3 | Guards judge one edit and one net. A trade (rip one net, close two) is refused, so a sealed open stays sealed. | `handroute.py:797` "lay opened connection(s)"; `handrepair.py:167-172` undoes the whole attempt; `handroute.py:1123` "unconnected not lower". W12-W13: `hand-repair` 5/5 failed in 57 min, e.g. "3 closed, 5 blocked; commit rejected: unconnected not lower: 5 -> 5" |
| C4 | "Open count" has no single definition. 19 modules run their own `kicad-cli pcb drc` with three flag sets (`--severity-error` or `--severity-all`, with or without `--refill-zones`, with or without `--all-track-errors`). The kicad-cli list is capped near 499, which made `lay` report opens that were not new. | ledger row W14 step D. The cap is fixed (ce46aea: `drcfull.complete:59` fills the list from pcbnew), but the fix had to be wired into 16 modules (`grep -l drcfull fluxplace/*.py`); the 19 runners remain |
| C5 | Pairs take the most time and still fail. W13 full run: 18 of 35 complete in 79 min, 3,985 s of 4,747 s in the fallback ladder. `pairs --only`: 16 of 17 `kept_original` ("search budget", "no path for the coupled centre line", "no clear break-out"). The stage then reports WARN and hands the pair to the router, which lays it uncoupled. | call logs W12-W13; `docs/FLOW.md:14` "stays with the router (WARN)"; W12-W14 `pairs` 18/19 failed, 160 min |
| C6 | Escalation is done by a person. The W14 script passes a second constraints file that differs only in `[pairlay]` effort (`max_nodes`, `nodes_per_mm`, `negotiate_passes`, `fallback`). Timeouts (4800, 6000, 3600 s) and `--budget 900` are in bash. The accept rule for build stages (`--allow-worse` when opens fell) is a bash function. | `W14/inputs/constraints-pairs-t1.toml` diff; `run-w14.sh:12-22` |
| C7 | The largest scope is tried as the fallback. Whole-board `finish` hit its 1,500 s cap with nothing produced. | W13 call log; `cli.py:1745` `_legacy_finish` |
| C8 | The part-move rung does not exist as an action. `closure._nudge` (`closure.py:340`) only reports a nudge; ladder step 7 of the playbook is done by hand scripts. | `W13/scripts/01-move-u8.py`; ledger row "C316-C319 placement" |
| C9 | Board constants in repair code. | `planes=("In1.Cu", "In4.Cu")` in `region.py:308`, `repairregion.py:101`, `finish.py:61`, `cli.py:3915,3934`; `"GND" in n.upper()` in `repairregion.py:146`; `skip_nets=("GND",)` and 0.2 / 0.15 mm defaults in `patch.py:872-873`; `prefix="ETH_"` in `repair.py:185`, `cli.py:1698` |
| C10 | 105 commands is a choice problem for every agent. W12-W14: `suggest` 30/64 failed, `lay` 12/15, `fp-check` 12/30, `versions` 23/48 (many by design). | call logs |

### 1.3 Duplication

| what | count | where |
|---|---|---|
| repair entry points that change copper or parts | 25 in 21 modules | `routeladder.Ladder`, `pairfb.ladder`, `pairsonly.reroute`, `handrepair.repair`, `patch.patch_board`, `repairregion.repair`, `region.run`, `finish.route_nets`, `adaptive.route_adaptive`, `escape` rule areas, `repair.bridge`, `repair.stitch`, `repair.stitch_islands`, `repair.redundant_copper`, `repair.rf_widths`, `pourisland.repair`, `planetie.run_guarded`, `pourtie.repair`, `joingaps.run`, `drcfix.loop`, `launder.launder_board`, `snapiter.run`, `viamin.via_min`, `tune.tune_pairs`, `relieve.run` |
| ways to close one ordinary open | 9 | `routeladder`, `handrepair`, `patch`, `repairregion`, `region.run`, `finish.route_nets`, `adaptive`, `repair.bridge`, `joingaps` |
| ways to close a pour island | 4 | `pourisland.repair`, `planetie.run_guarded`, `pourtie.repair`, `repair.stitch_islands` |
| escalation ladders | 5 | `routeladder.py` (ripup, order, layer), `pairfb.py` (b(i)-b(iii)), `handrepair.py` (suggest, explore), `patch._rip_reroute`, `repairregion` (margin x1.5, `repairregion.py:187`) |
| grid path searches | 5 | `patch.dijkstra:381`, `handroute.suggest:1318`, `pairlay.astar:152`, `repair.bridge:741`, `pocket.flood:237` |
| acceptance rules | 12, with 5 meanings of "better" | below |
| functions that count opens | 17 | `tune.drc_counts:37`, `fanout.drc_counts:118`, `handroute.summarize_drc:71`, `handrepair._unc:32`, `drcguard.open_split:73`, `opengate.unconnected:101`, `opengate.measure:127`, `pipeline._open_count:105`, `pipeline.st_route.nfail:480`, `snapiter._unc_nets:28`, `repairregion._open_items:28`, `progress.measure:78`, `routeconn.open_by_net:165`, `routeconn.text_open:54`, `adaptive.drc_unrouted:31`, `routediag.unrouted_nets:281`, `planetie.unconnected_pads:29` |

Acceptance rules found:

| rule | where | users |
|---|---|---|
| no DRC key occurs more often (multiset) | `drcguard.check:102` | `tune`, `snapiter`, `planetie`, `launder`, `fanout`, `pairsonly`, `patch` |
| raw totals: violations <=, unconnected < | `cli.py:1661`, `cli.py:1764`, `drcfix.py:443` | `repair --bridge`, `finish --nets`, `drc-fix` islands |
| raw totals: violations <=, unconnected <= | `cli.py:1833` | scoped `finish` |
| per edit: no new key, no other net opens, target falls, pad nets unchanged | `handroute._edit:752-801` | `lay`, `shove`, `copy-net`, `pour-island` |
| per session: unconnected lower, no new non-open key, no net worse, locked copper unchanged | `handroute.session_commit:1114` | `hand-repair`, sessions |
| opens fall, shorts not up, no new non-open key | `repairregion.py:162-171` | `repair-region` |
| copper-protection fingerprint | `region.check:212` | `finish`, `repair-region` |
| shorts, unconnected, errors not up, or `--allow-worse` | `versions.py:278-281` | every accepted board |
| pad -> net snapshot, off with `FLUXPLACE_GUARD=0` | `cli.py:4311-4400` | every writing command |
| new copper vs rule floors | `routeaudit.audit:180` | router batches |
| collision gate and intent rows not worse | `relieve.guard:109` | `relieve` |
| pair audit takes back out-of-tolerance copper | `pairs.py:1058-1069` | `pairs` |

The raw-total rules contradict `drcguard`'s own docstring, which says totals move with zone refill and must not be compared.

## 2. The flow

### 2.1 Entry point

```
fluxplace build BOARD --constraints C.toml [--to STAGE] [--plan] [--status]
```

- `build` is `pipeline.run` (`pipeline.py:1209`) with the gap loop added as a stage. `pipeline` becomes its alias, then goes.
- A stage whose gate already passes on the input board is skipped. A routed board from an earlier run therefore enters at
  the gap loop without a `--from`. This is how existing boards come back into the loop.
- `--plan` classifies and prints the gaps with the method each would get; it writes nothing.
- When a `versions` registry exists, `build` calls `versions` step before each stage and accept or reject after it
  (today the bash `build_gate`).
- `build` is a library call first: `build.run(board, cons, on_event=None) -> report`. The CLI is one caller; it prints
  from the event stream (2.13). Nothing reads logs.

| exit | meaning |
|---|---|
| 0 | every gate passed |
| 1 | gaps remain; each has a blocker row with the measured reason |
| 2 | time budget reached; the best accepted board is kept |
| 3 | owner questions open (`questions.py`, unchanged) |
| 4 | no change |
| 5 | agent packets pending (section 4) |

### 2.2 Stages

| # | stage | existing code | gate after it | on gate fail |
|---|---|---|---|---|
| 0 | sync | netlist export (`cli.py:842 _export_netlist_xml`), `kicad_io.sync_pad_nets:544` (today `sync-nets`), `questions.check_spec_board:412`, `review.check_spec_sync:1410`, `schematic.verify:229`, `partsgate.gate:51` | board, schematic and spec name the same parts and nets; every part resolved | exit 3 with the list of differences; nothing is placed or routed |
| 1 | ready | `pipeline.st_ready:232` (`fpcheck.check_board`, stackup, `questions.guard`), plus the outline questions (2.10) | footprints pass, stackup solved, outline and size answered, no open question | exit 3, or gap `footprint` |
| 2 | place | `st_modules:922`, `st_floorplan:955` (with the size search of 2.10), `st_relieve:1052`, `wlrefine` | `placementcheck`, `intent.check`, `legality.collision_gate`, per-side density at or under the target | gaps `placement`, `courtyard`; ladder L4-L5 |
| 3 | fanout | `st_fanout:974`, `st_pour_tie:1003`, `thermal.run(stage="pre")` | FANOUT_OK | gap `open-sealed` on the blocked pins |
| 4 | pairs | `st_pairs:1070`: `pairs.run` on the high-speed classes first, then the rest (order from net-class priority, already in `pairs.plan`) | `pairaudit.audit`: every pair `complete` | gaps `pair-*`; the gap loop runs here for pair kinds **before** route |
| 5 | route | `st_route:415` (router from `[route] router`), `routeladder.Ladder`, `planetie.run_guarded`, `pourtie.repair` | opens counted by the one measure | gaps |
| 6 | close | new `gaps.close` (2.4-2.7); also `thermal.run(stage="post")` | no gap left | exit 1, 2 or 5 |
| 7 | review | `st_tune:867`, `st_silk:883`, `st_review:900`, `fabadvise` | review FAIL = 0 | review findings become gaps; one more `close` round |
| 8 | fab | `opengate.refuse`, `fab`, `deliver` (only with `--to fab`) | open gate, review gate | refuse |

High-speed pairs that are not `complete` after stage 4 are gaps with the highest class. They are never passed to the
router as ordinary nets. Low-speed pairs may still end in `pairfb` state c (connected, reported). A high-speed pair
that no rung completes does not stop the build: routing continues so the rest is measured, the exit code is 1 and
`fab` refuses the board (decision D6).

Stage 0 exists because a disagreement found late costs a whole run. W14 is the example: C316-C319 are on the board
and not in the schematic (`W14/RUN.md`, step C), so they have no schematic link. `check_spec_board` reports spec parts
the board lacks; the reverse direction (board parts the spec or schematic lacks) is **unverified** and is the one
check stage 0 may need to add.

### 2.3 State

| file | content | status |
|---|---|---|
| `W/state.json` | the one state file (2.13): stage verdicts, level, score, best board, gap counts, pending questions and packets; capped at 2,400 bytes (`pipeline.py:31,53`) | exists, extended |
| `W/best.kicad_pcb` (+ `.kicad_pro`) | the best accepted board, replaced atomically on every accept | new |
| `W/gaps.json` | every gap with its tries (method, scope tier, verdict, seconds) | new |
| `W/events.jsonl` | the event stream (2.13) | new |
| `W/stop.json` | the stop report (2.7) | new |
| `.fluxplace/attempts.jsonl` | one line per repair attempt (2.12) | new |
| `W/<n>-<stage>/board.kicad_pcb` | stage boards; `close` writes `W/6-close/r<round>.kicad_pcb` | exists (pipeline layout) |
| `.fluxplace/progress.json`, `docs/resume/boards.json` | checkpoints and the board registry | exist |
| `W/ledger-rows.md` | one drafted `ONE-SHOT-LEDGER.md` row per gap the flow could not close | new |

Agents read `build --status` (one line per stage, gap counts by kind), never the logs.

### 2.4 Gap classifier (`gaps.classify(board, cons, spec) -> [gap]`)

One measurement feeds it: one DRC run (`--severity-all --all-track-errors --refill-zones`, completed by
`drcfull.complete`), one `pairaudit.audit`, one `qualaudit.rf_rows`, and the review findings when stage 7 ran.

Gap record: `{id, kind, class, nets, refs, at, layer, box, evidence, tries}`. `id` is stable (`kind:net:padA:padB` or
the `drcguard.key_of` key). `class` is `hs-pair | rf | power | pair | plain`, from `netclass` and the constraints.

| kind | read from | detector that exists |
|---|---|---|
| `open-gap` | unconnected, same-net ends within the gap limit | `joingaps.plan:23` |
| `open-pour` | unconnected with a zone item | `drcguard.open_split:73`, `planetie.unconnected_pads:29` |
| `open-free` | unconnected, `closure` best option is `track` or `via` | `closure.run:461` |
| `open-sealed` | unconnected, `closure` best option is `relay`, `unlock`, `nudge` or none | `closure.run`, `handroute._seal_report:1259`, `routediag.diagnose:497` |
| `pair-open` | pair `half` or `open` | `pairaudit.audit:103` |
| `pair-asym` | pair `mismatch`, cause `layers` or `vias` | `pairaudit.audit`, `tune.classify_pairs:157` |
| `pair-skew` | pair `mismatch`, cause skew only; `PAIR_SKEW` | `pairaudit.audit`, `review.check_pairs:268` |
| `pair-uncoupled` | `PAIR_ROUTED_UNCOUPLED` | `pairuncoupled.flagged:84` |
| `pair-loop` | shortest path shorter than the copper | `repair.measure:76` |
| `rf-width` | `RF_WIDTH_OFF`, `RF_IMPEDANCE_OFF` | `qualaudit.rf_rows:116` |
| `rf-vias` | `RF_VIA_COUNT` | `qualaudit.rf_grade:62` |
| `short` | `shorting_items`, `solder_mask_bridge` | DRC |
| `clearance` | `clearance`, `hole_clearance`, `hole_to_hole` | DRC |
| `dangling` | `track_dangling`, `via_dangling` | DRC |
| `via-offcentre` | `track_not_centered_on_via` | DRC, `snapvias.off_centre:65` |
| `subfloor` | `FAB_MIN_VIA`, `FAB_MIN_TRACK`, `FAB_MIN_DRILL` | `review.check_fab_capability:1300`, `routefr.subfloor_copper:510` |
| `acid-trap` | `ACID_TRAP` | `acidtrap.acute_junctions:34` |
| `stale-fill` | `STALE_FILL` | `opengate.measure:127` |
| `silk` | `silk_over_copper`, `silk_overlap`, silk findings | `silk.run_check:1136` |
| `courtyard`, `placement` | courtyard overlap, keep-out, `no_tht` | `placementcheck`, `legality.collisions:452` |
| `footprint` | `FP_*`, `LANDPATTERN_*` | `fpcheck.check_board:197` |

### 2.5 Dispatcher (`gaps.DISPATCH`, the only place a repair is registered)

Rungs run left to right; the first that passes the guard wins. Scope and effort tiers (2.7) apply inside a rung.

| kind | rung 1 | rung 2 | rung 3 | rung 4 | last |
|---|---|---|---|---|---|
| `open-gap` | `joingaps.run` | as `open-free` | | | |
| `open-pour` | `pourtie.repair` | `planetie.run_guarded` | `pourisland.repair` | `repair.stitch_islands` | blocker |
| `open-free` | `handroute.suggest` + `handroute.lay` | `patch.patch_board(rip=False)` | as `open-sealed` | | |
| `open-sealed` | `handrepair._one` (explore, shove, re-lay) | `repairregion.repair` | `patch._rip_reroute` | `routeladder.Ladder` rung `layer` | nudge, AI, blocker |
| `pair-open`, `pair-asym` | `pairsonly.reroute` (own copper unlocked) | `pairsonly.reroute(allow_shove=True)` | `pairfb.ladder` | `repairregion._blockers` rip + `pairsonly.reroute` | nudge, AI, blocker |
| `pair-skew` | `tune.tune_pairs` | `pairsonly.reroute` | | | blocker |
| `pair-uncoupled` | `pairsonly.reroute` | as `pair-open` | | | hs: blocker; other: reported |
| `pair-loop` | `repair.redundant_copper` | | | | |
| `rf-width` | `repair.rf_widths` | `drcfix.fix` (neck at clearance) | | | AI, blocker |
| `rf-vias` | | | | | AI, question |
| `short` | `drcfix.fix` (rip the shorting track) | `handroute.shove` | | | the open it leaves is a new gap |
| `clearance` | `drcfix.fix` (via shift, neck) | `handroute.shove` + re-lay | `repairregion.repair` | | AI, blocker |
| `dangling` | `launder.launder_board` | `repair.prune_dangling` | | | |
| `via-offcentre` | `snapiter.run` | | | | |
| `subfloor` | `viamin.via_min` | `routefr.repair_subfloor` | | | blocker |
| `acid-trap` | `acidtrap.fix_junctions` | | | | |
| `stale-fill` | `opengate.refill` | | | | |
| `silk` | `silk.fix_board` | | | | reported |
| `courtyard`, `placement` | nudge (`closure._nudge` search, `legality.Legal`, `boardwrite.move_file`) | `relieve.run` | | | question (exit 3) |
| `footprint` | | | | | question (exit 3); never repaired by routing |

Notes:
- Rung 4 of the pair rows is the "pair-aware region repair" the ledger lists as open. It is glue between two existing
  functions, not a new router.
- `adaptive.route_adaptive` and the `escape` rule-area ladder thin traces to make a board route. They are not registered:
  the owner rule is never to thin network, RF or power traces (decision D3). The code stays.
- The table is the L0 and L1 part of the outer ladder (2.9). "nudge" is L2. Which router draws the copper for each kind
  is in 2.8.
- `region.run` and `finish.route_nets` are reached only through `repairregion.repair`. Whole-board Freerouting stays a
  router choice of stage 5, not a repair.

### 2.6 One guard (`drcguard.measure` + `drcguard.accept`)

`measure(board)` is the only DRC and connectivity read in the flow, cached by file hash:
`{opens_by_net, shorts, keys, pairs, rf, locked_sig, pad_nets}` (from `drcfull.complete`, `drcguard.keys_from_report`,
`pairaudit.audit`, `handroute._locked_sig`, `audit.snapshot`).

`accept(base, cand, mode, target)`:

| rule | test | reuses |
|---|---|---|
| R0 | no pad changed net, no footprint changed | `audit.connectivity_diff:438` |
| R1 | shorts not up; 0 at the end of `close` | |
| R2 | no DRC key more often than in the base | `drcguard.new_keys:97` |
| R3 | locked copper unchanged, except what the method declared it would re-escape | `handroute._locked_sig:737`, `region.check:212` |
| R4 | score falls. Score, compared left to right: hs-pair gaps, rf gaps, shorts, opens, other pair gaps, error keys, quality gaps | |

| mode | used by | R2 | R4 |
|---|---|---|---|
| `build` | stages 3-5 | error keys may rise | opens must fall (today `run-w14.sh:12-22`) |
| `repair` | `close` | strict | score must fall and the target gap must be gone |
| `edit` | steps inside one transaction | rule floors and R0 only | none; the transaction end is judged in `repair` mode |

The change that matters is R4: "no other net may open" becomes "the board score must fall". A method may open a plain
net to close two, and the opened net returns as a gap. A board that is worse is still never kept.

### 2.7 Convergence

Loop: classify, order the gaps (class first, then fewest options first, as the playbook says), run the next untried
rung of each, guard, accept or record the try, classify again.

| rule | detail | reuses |
|---|---|---|
| memo | a (gap, rung, tier) that failed is not run again on the same copper around the gap | session ledger idea, `progress.block` |
| merge | gaps whose boxes overlap are solved as one region | playbook step 4 |
| scope tiers | connection -> net -> region box (margin x1.5, 3 tries) -> merged cluster -> an unused signal layer | `repairregion.py:150-187`, `routeladder` |
| permission tiers | unlocked plain copper -> own fan-out stubs -> locked re-escape in a session -> part nudge -> AI -> owner question | `pairfb` b(ii), `handroute.shove(allow_locked)`, `closure._nudge` |
| effort tiers | search nodes x1, x3, x5 | `pairfb.ladder:253-255`; replaces `constraints-pairs-t1.toml` |
| never escalated | track, clearance, via below the floor (fab profile by layer count, `[route] min_*`, at least 0.127 mm); pair, RF and power geometry | `constraints.fab_profile:138`, `clamp_track_mm:194` |
| nudge rung | parts with at most 3 pads, not fixed, not locked, not THT (`closure.py:345`); legality checked; attached copper re-laid; the move recorded as a `[lock.at]` row proposal | `closure._nudge`, `legality.Legal`, `boardwrite.move_file:120` |
| stop: clean | no gap | |
| stop: flat | one full round without an accepted change and every gap at its last tier | |
| stop: loop | `progress.verdict` says `looping` (a metric oscillates with no net gain) | `progress.py:183` |
| stop: budget | `[flow] budget_min` reached; best accepted board kept | `tune.Budget:78` |

Hard parts of the loop (not options):

| part | rule |
|---|---|
| best so far | `W/best.kicad_pcb` always holds the accepted board with the lowest score. Every accept replaces it atomically. A crash, a timeout or exit 2 leaves it valid. |
| monotone score | A candidate is kept only when the score of 2.6 R4 falls. Within one placement the loop therefore cannot cycle. A climb to L2-L5 changes the placement: its result is kept only when the re-routed board scores below the best board, else the best board is restored. |
| memo | Key `(gap id, method, scope tier, hash of the copper inside the gap box)`. A key that failed is not run again. The hash makes a method eligible again only when the copper around the gap really changed. Stored in `W/gaps.json`. |
| stop report | `W/stop.json` and the `finished` event: exit code, level reached, final score, minutes, and per gap left: id, kind, class, last level, last method, the measured reason, and what would be needed next (question id, packet, L6). Also drafted as ledger rows in `W/ledger-rows.md`. |

### 2.8 Proven routers first

Rule: copper geometry comes from a proven router. The AI chooses strategy only (which copper to rip, which part to
nudge, which layers, why a pocket is sealed, at most a coarse centre line for a pair). It never writes track
coordinates.

| router | code | proven on |
|---|---|---|
| KRT (KiCadRoutingTools) | `krtchain.run:188`, `krtchain.cmd_route:109`, `cmd_diff:104`, `pipeline._route_cmd:353` | W13: opens 448 -> 5 in one stage |
| Freerouting | `routefr.route_board:648`, `region.run:308` (scoped, copper protection proven), `repairregion.repair:100` | whole board; region repair not yet measured on a board (ledger) |
| pair constructor | `pairlay.astar:152`, `legs:290`, `breakout:404`, `bump:456`, driven by `pairs.run:1031` and `pairsonly.reroute:138` | W13: 18 of 35 pairs |
| KiCad push-and-shove | no headless entry was found in the code; whether KiCad 10 exposes it to scripts is **unverified** | not used; slot kept in the table |
| in-house grid search | `handroute.suggest:1318`, `patch.dijkstra:381` | single connections only; `suggest` 60 of 119 found a path |

| gap kind | first router | second | never |
|---|---|---|---|
| `open-gap` | straight segment, exact check (`joingaps.run`) | grid search | |
| `open-free` (one connection) | grid search `suggest` + `lay` (one connection, DRC judged) | KRT single net (`st_route` `again`, `pipeline.py:565`) | AI path |
| `open-sealed`, merged regions | Freerouting on the region (`repairregion.repair`) after the rip | KRT on the ripped nets | AI path |
| `pair-open`, `pair-asym`, `pair-uncoupled` | pair constructor (`pairsonly.reroute`) | KRT `cmd_diff` | Freerouting (cannot couple pairs: `repairregion.py:137-139`), single-leg routing |
| `pair-skew` | `tune.tune_pairs` (meander, shortcut) | pair constructor | |
| `rf-width` | width from the stackup (`repair.rf_widths`) | | grid search, any thinning |
| `open-pour` | via at a verified fill site (`pourtie`, `planetie`) | | track routing |
| `clearance`, `short`, DRC kinds | local edit (`drcfix.fix`), then the open it leaves follows the open rows | | |
| local re-route after L2-L5 | the stage's own router on the rework box (`region.run` with a refs or area scope) | | |

Within one level, rung order follows the close rate in the attempt log (2.12); on a tie the proven router goes first.

### 2.9 Outer ladder: climbing back up the stages

The gap loop climbs when a level stalls on measured evidence. Accepted copper outside the rework box (union of the
gap boxes plus the scope margin) stays, and `region.check:212` proves it with the copper fingerprint.

| level | action | existing code | trigger (all measured, logged with the attempt) | re-enters at | guard |
|---|---|---|---|---|---|
| L0 | copper repairs | rungs 1-2 of 2.5 | a gap exists | close | `accept`, mode `repair` |
| L1 | region rip and re-route, pairs included | `repairregion.repair`, pair rung 4, `routeladder.Ladder` | L0 rungs exhausted for the gap, or two or more gap boxes overlap | close | same |
| L2 | nudge small parts | `closure._nudge:340` (search), `legality.Legal`, `boardwrite.move_file:120`, then the local re-route | L1 failed at its largest margin and `closure` reports a nudge option, or the seal report shows 0 via sites (`via_sites_in_window`) | route, on the rework box | `accept` + collision gate and intent hard rows not worse (`relieve.hard_state:51`, `hard_ok:62`) |
| L3 | re-place one cluster or module around the failing area | `modules.build:65` (who belongs together), `floorplan2.reseat_module:521`, `planplace.plan_place:588` on the members, `legalise.legalise:242`, `modules.fanouts:222`, then pairs and route on the box | 3 or more gaps or refused attempts inside one module box, or `density.compute:77` utilisation above 0.75 in the 10 mm grid cells that hold the gaps | place, on the module | `relieve.guard:109` (copper of moved pads removed, endpoints checked) + L2 guard |
| L4 | move parts between sides by per-side density | `density.compute` (per side), `compact.pick_flips:124` (decoupling caps, small passives), `flip.flip_file:53`, `flip.verify:80`, `[placement.side]` (`placechecks.py:211`) | worse side above 0.75 while the other side is at or under 0.60, and L3 failed or was not applicable | place | L3 guard + height and `no_tht` keep-outs (`placechecks`) |
| L5 | grow the outline | the existing resize loop `planopts.search:493` (per edge: probe at the largest growth, bisect in 1 mm steps, then pairs of edges; `planopts.grow_text:100` moves the edge and its `[plan] edge_parts`; `board_size:157`), `density.growth:156` (which edge, how many mm), `floorplan2.apply_growth:851` | both sides above the density target after L4, or L3-L4 exhausted with gaps left. The edge is chosen from where the gaps cluster and the per-side density in those grid cells, not from the plan-route estimate | floorplan | size stays inside `[outline] max_w_mm, max_h_mm`; fixed edges do not move; L3 guard; the grown board is kept only when the re-routed score beats the best board |
| L6 | add layers or change the fab tier | `planopts` (what-if: smallest board or rule change), `fabadvise` (cheapest tier), `questions.py` | L5 at the envelope limit, or `[outline] size = "fixed"` | owner question, exit 3 | none: the owner decides |

Rules of the climb:
- A level is entered only with its trigger in the attempt log. No level is skipped and none is entered on a guess.
- L2 runs without asking inside decision D4 (at most 3 pads, unfixed, within the `closure` nudge limit). L3-L5 never
  move a part in `[placement] fixed` or `[lock.at]`. Whether L3-L4 may move unfixed parts without asking is question Q2.
- **Is size the problem? (cause check before L4/L5, owner 2026-11-19).** Before any side move or growth, the flow
  measures density at the stalled gaps, not over the whole board. Courtyard coverage alone misleads: a big module
  covers 100 % of its cells and is easy to route around, while a 0.5 mm-pitch IC at 40 % may not be. So per gap
  cluster, compare three numbers:
  (a) courtyard and ring coverage per side (`density.py` grid);
  (b) routing demand, meaning pins and net crossings per mm² against the channel capacity of the free layers
      (`globalroute` congestion on that box);
  (c) the cause the router recorded (sealed by locked copper, pair break-out, keep-out, via sites).
  Growth is proposed only when (b) is over capacity, with no free capacity within reach on the other side or nearby.
  When the area has spare capacity, the stall is a placement, fan-out, order or rule problem, and the flow stays at
  L1-L3. The growth question cites (a)-(c). Measured on W14 CURRENT (2026-11-19): F.Cu 53.5 % and B.Cu 24.8 %, both
  under the 60 % target. The worst cells are the CM5 module courtyards at 100-146 %. So W14 is not short of area
  overall; its stalls look local, which is (b)/(c) territory and unproven until measured.
- **L5 growth asks the owner with a concrete proposal (DECIDED 2026-11-19).** Once the size is in use (after the
  intake size search), growth is a question (exit 3, `questions.py`; an `outline_grow` event for a frontend), never
  silent. The question names the edge(s) and millimetres, the evidence, and how likely the growth is to finish routing,
  e.g. "Grow width +1 mm (right edge) and length +3 mm (bottom edge) to finish routing? 6 opens and 2 pairs left
  cluster at x 150-165, y 40-60, where B.Cu is at 0.78 density. Estimated chance to finish: 85 % (12 of 14 similar
  corpus growths finished). Board becomes 105 x 97 mm, inside the 110 x 100 mm envelope. [yes / no / other size]".
  The number must come from `attempts.jsonl` and corpus outcomes of earlier growths with similar gap density per mm
  added (2.12, 2.14). Never use the plan-route estimate alone (7-27 % accurate). With too little data the question
  says "no calibrated estimate yet (n similar growths)" instead of a percentage. Before asking, the flow tries the
  candidate sizes on scratch copies (`planopts.search`) and offers the smallest one that measurably routed, if one
  did. The answer is recorded in `[answers]`. "Yes up to N mm" pre-approves later growth within N mm.
- After a climb the memo is cleared only for gaps inside the rework box.
- Each level has a share of `[flow] budget_min`; an unused share passes down.
- Side flips are not searched by `floorplan2` today ("every block keeps its side", its docstring). L4 is new glue
  around `pick_flips` and `flip_file`; its fit with rigid modules is **unverified**.

### 2.10 Outline and size, asked up front

Asked in stage 1 through `questions.py` (a new `check_outline` beside `check_fixed_parts:341` and
`check_connector_entry:444`). An answer is recorded once in `[answers]` or the `[outline]` block; `intake.py` already
asks about mounting (`apply_mounting:190`).

| question id | options | default when forced |
|---|---|---|
| `outline-shape` | `given` (the board's Edge.Cuts), `rectangle`, `square`, `round` | `given` when the board has an outline, else `rectangle` |
| `outline-size` | `fixed` (w x h), `minimal` | `fixed` at the present size |
| `outline-envelope` | largest w x h the enclosure allows | none: required when size is `minimal` |
| `outline-fixed` | edges and features that may not move: mounting holes, edge connectors (`legality.edge_mounted:62`), named edges | parts already fixed or locked (`facts.resolve`) |

```toml
[outline]
shape = "given"            # given | rectangle | square | round
size = "minimal"           # minimal | fixed
w_mm = 0.0                 # used when size = "fixed"
h_mm = 0.0
max_w_mm = 110.0           # enclosure envelope; L5 never grows past it
max_h_mm = 80.0
fixed_edges = ["top"]      # edges that carry connectors or must stay
density_target = 0.60      # per side, worse side decides
```

Density metric (exists, `density.compute:77`): per side, utilisation = (courtyard area + fan-out ring area) / usable
area, where usable = outline area minus keep-outs that forbid vias on that side. Bands in the code: at or under 0.60
OK, 0.60-0.75 TIGHT, above 0.75 FAIL (`density.py:28`). Default target 0.60 (question Q1).

The resize loop exists; this section and L5 build on it and add no second one.

| existing piece | what it does |
|---|---|
| `planopts.search:493` (`plan-options`) | per-edge growth: probe at the largest growth, bisect in 1 mm steps, then pairs of the two nearest-miss edges. Each option is a scratch re-place (`place --objective plan`) plus `plan-route`, ranked by smallest area. Never touches the project board |
| `planopts.grow_text:100`, `edge_parts:54`, `board_size:157` | move the outline edges and the parts tied to them (`[plan] edge_parts`) |
| `floorplan2` `--size` loop (`floorplan2.py:1116`, `passes:1058`), `floorplan2.grow_text:789` | grow one edge 1 mm at a time until density and fan-out pass. A second `grow_text`; fold into the `planopts` one |
| `place` `max_growth` + `kicad_io.shrinkwrap_outline:663` (`cli.py:351-365`) | classic placer: outline shrink-wrapped around the placement |
| `compact.compact:254` | shrink a loose placement that already routed toward its anchor |

What went wrong with it (W7, `W7/SUMMARY.md`, `W7/attempts/a1`-`a15`): 15 attempts, driven one by one by agents and
scripts, took the outline from 117.6 x 98.7 mm to 104 x 94 mm. Size was judged on the placement gates and the
`plan-route` estimate only: attempt 15 passed stage A (fan-out 135/135, collisions 0). Routing at that size then left
4 opens and 111 errors in W7 and stalled through W8-W14. `plan-route` was right 7-27 % of the time on this board
(`docs/FLOW.md:3`). The size decision never saw a routing result.

What changes:

| change | detail |
|---|---|
| one flow | the 15 attempts become the size search inside stage 2 of `build`; each attempt is an attempt-log line, no scripts |
| size is proven by routing | a size counts as passing only after stages 3-6 ran on it: high-speed pairs complete and opens at or under a small limit after `close`. The `plan-route` estimate only orders the candidates |
| margin | the search stops at the smallest size that routes, then keeps one growth step on the edge nearest the last gap cluster as margin (the owner prefers routing margin to the minimum) |
| cost control | full routing is spent on at most 3 sizes: the estimate picks the smallest plausible size, bisection runs between it and the envelope. The pairs stage alone is the cheap first proof (it failed first in W7-W13) |
| feedback | route and close failures feed back: gap cluster boxes and per-side density in their grid cells choose the edge to grow (L5) or the parts to move to the other side (L4) |
| W7 tool issues | failed floorplans printed empty `outside`, `forbidden`, `no_tht` lists while pads were outside the outline and keep-outs were violated (`W7/TOOL-ISSUES.md` items 2-6). The size search must judge a candidate by `legality.collisions:452` and `placementcheck` on the written board, never by the stage summary. Whether these issues are fixed is **unverified** |

Minimal-size search (`size = "minimal"`):

| step | code |
|---|---|
| 1. start outline of the chosen shape with usable area = demand / target on the worse side | `density.compute`, `density.growth:156` (inverse use) |
| 2. per-edge search for the smallest size whose placement gates pass, never a fixed edge | `planopts.search`, with the floorplan as the re-place step |
| 3. prove it: fan-out, pairs, route, close on that size | stages 3-6 |
| 4. not proven: grow by the feedback rule and repeat step 3; proven: add the margin step | L5 |
| 5. later growth only through L5 inside the envelope | 2.9 |

A generator for a `round` or `square` start outline is not in the code (**unverified**: both `grow_text` functions
move existing edges, they do not create a shape). Question Q5.

### 2.11 Fast pre-check

Full `kicad-cli pcb drc` runs only on the candidate that is about to be accepted. Every candidate first passes a local
check in memory.

| candidate | local check | code |
|---|---|---|
| a path or via | clearance to foreign copper, floors, 45/90, ends touch the islands | `handroute.probe_prims:449` |
| new copper from a router | width, via size, clearance edge to edge, via in pad | `routeaudit.audit:180` (base vs candidate) |
| a via site, a pocket | via fits on every layer; cells reachable | `pocket.Field:67`, `pocket.flood:237` |
| open count delta | connectivity from the text model, no zone fill | `routeconn.text_open:54` |
| a part move | courtyard, outline, keep-outs | `legality.Legal`, `legality.collisions:452` |

| target | value | measured by |
|---|---|---|
| DRC runs per accepted repair | at most 1.2 | `drc_runs` in the attempt log |
| refused candidates caught before DRC | at least 80 % | `precheck` field |
| pre-check passed but DRC refused | logged as `precheck_miss` with the DRC key; each one is a pre-check defect to fix | attempt log |

Today `handroute._edit` runs one full DRC per edit (`handroute.py:773`), and `drcguard.guard_board:114` one per
mutation. The pre-check never accepts a board; `drcguard.accept` stays the only judge.

### 2.12 Attempt log, report and pruning

Every rung and every repair method writes one line per attempt to `.fluxplace/attempts.jsonl`, beside
`calls.jsonl` (same location rule and size cap: `calllog.log_path:43`, `calllog.append:60`). Written by
`gaps.close`, not by each method.

```json
{"ts": "2026-11-19T10:02:11", "run": "r-20261119-1001", "board_fp": "9c1e44a0", "tool": "632b82c",
 "stage": "close", "level": "L1", "gap": {"id": "open-sealed:NET_A:U1.3:R4.1", "kind": "open-sealed", "class": "plain"},
 "method": "repairregion.repair", "tier": {"scope": "region", "margin_mm": 3.0, "effort": 1},
 "in": {"nets": 3, "box": [10, 28, 17, 35]},
 "result": "refused", "why": "opens not reduced", "score": [[0, 0, 0, 5, 1, 15, 2], [0, 0, 0, 5, 1, 15, 2]],
 "s": 41.2, "drc_runs": 1, "precheck": "pass", "ai_tokens": 0}
```

| field | values |
|---|---|
| `board_fp` | `versions.copper_fingerprint:98` of the base board |
| `result` | `closed`, `partial` (score fell, gap still there), `refused` (guard), `precheck` (refused before DRC), `error`, `skipped` (memo or permission) |
| `why` | one line: the guard rule that failed or the method's own reason |
| `score` | the R4 vector before and after |
| `in` | compact, at most 200 bytes; never paths or logs |

Report (`x attempts`, and a section of the corpus scorecard): per method and per gap kind: tries, closes, close rate,
seconds per close, DRC runs per close, AI tokens per close, the three most frequent `why`.

Pruning rule:

| step | rule |
|---|---|
| candidate | a method with 0 closes over 3 corpus runs, with at least 10 tries on at least 2 boards |
| review | the top-level model reads its `why` lines: wrong kind, wrong rung order, or a method that cannot work |
| record | a row in `docs/ONE-SHOT-LEDGER.md` with the numbers from the report (not from memory) |
| action | first removed from `gaps.DISPATCH` and kept behind `x`; deleted only by an owner decision |

From phase 1 on, ledger rows for repairs are drafted from this file.

### 2.13 Event stream and the one state file

`build.run` emits each event to the `on_event` callback and appends it to `W/events.jsonl`. The CLI, an agent and a
later web frontend read the same stream. Common fields: `seq`, `ts`, `run`, `type`.

| type | extra fields |
|---|---|
| `run_start` | `board`, `constraints`, `to`, `tool` (git sha) |
| `stage_start`, `stage_end` | `stage`; on end: `verdict` (`pipeline.VERDICTS`), `line`, `board`, `s` |
| `gap_found` | `gap` (`id`, `kind`, `class`, `at`, `box`) |
| `attempt` | the attempt-log record of 2.12 (same object, written once to both files) |
| `level_up` | `from`, `to`, `trigger` (the measured values), `box` |
| `best` | `board`, `score`, `opens`, `shorts`, `errors`, `hs_pairs` |
| `question` | `id`, `topic`, `options`, `default` (the `questions.py` record) |
| `ai_packet` | `gap`, `path`, `bytes` |
| `finished` | `exit`, `level`, `score`, `minutes`, `left` (the stop report) |

`W/state.json` (extends the existing file, same size cap):

```json
{"run": "r-20261119-1001", "flow": "build", "stage": "close", "level": "L1",
 "stages": {"sync": "pass", "ready": "pass", "place": "pass", "fanout": "pass", "pairs": "warn", "route": "pass"},
 "score": [1, 0, 0, 4, 1, 15, 2], "best": "best.kicad_pcb",
 "gaps": {"open-sealed": 3, "pair-open": 1, "via-offcentre": 0},
 "pending": {"questions": [], "packets": ["pair-open:ETH_P0"]},
 "seq": 412, "exit": null}
```

The stage line the pipeline prints today (`pipeline.py:1330`) becomes a renderer of `stage_end`. `build --status`
renders `state.json`. `route-status` and `route-wait` are replaced by reading the stream.

### 2.14 Corpus and score

`corpus.py` already runs a manifest of boards on copies and compares scorecards (`load_manifest:48`, `run_board:247`,
`compare:372`, `scorecard_md:434`); `boardmatrix.py` walks the KiCad demo boards (`board_roots:24`). The change: one
step `build` per board (each manifest entry already has `pipeline_args` and `timeout_s`), and the score below. No
manifest exists on this machine yet (`~/.config/fluxplace/` is absent).

| name | board (always copied, never written) | role | state |
|---|---|---|---|
| utv-w14 | `/var/kicad/utv-comms-bridge/W14/CURRENT.kicad_pcb` with `W14/inputs/constraints.toml`, `spec.json` | full flow from fan-out | 495 opens, 0 shorts, 1 error (read) |
| utv-w13-routed | `/var/kicad/utv-comms-bridge/W13/stageB/routed.kicad_pcb` | close stage only | 5 opens, 210 errors (read from the registry) |
| utv-v15 | `/var/kicad/utv-comms-bridge/hardware/utv-comms/utv-comms-v15.kicad_pcb` | finished board: `build` must be a no-op | state **unverified** |
| razor-cm5 | `/var/www/html/razor-cm5-board/hardware/razor-cm5/razor-cm5.kicad_pcb` | second CM5 carrier | path found, state and constraints **unverified** |
| razor01-dig, razor01-rf | `/var/www/html/razor-detector-board/hardware/razor01-dig/razor01-dig.kicad_pcb`, `.../razor01-rf/razor01-rf.kicad_pcb` | fine-pitch digital; RF | path found, state **unverified** |
| open-source 1-2 | KiCad demos under `/usr/share/kicad/demos`: `cm5_minima`, `stickhub` (folders seen; board file names **unverified**) | boards with no constraints file: questions and defaults | **unverified** |

The razor boards live outside `/var/kicad`; the corpus only reads copies (question Q3).

Score, one per corpus run, compared left to right (smaller is better except the first):

| # | measure | per board |
|---|---|---|
| 1 | one-pass rate | exit 0 with no hand step and no packet answered by a person |
| 2 | high-speed pairs incomplete | count |
| 3 | final opens | count |
| 4 | final DRC error keys | count |
| 5 | minutes | wall clock |
| 6 | AI calls | count |

Trend file `docs/corpus/trend.jsonl`, one line per run:
`{ts, tool, boards, one_pass_rate, hs_incomplete, opens, errors, minutes, ai_calls, per_board: {name: {...}}}`.
A change to a rung, a guard or a classifier kind is kept only when this score is not worse; `corpus --baseline-ref`
already compares two git refs.

## 3. Parameters

### 3.1 Policy

1. Derived default: from the fab profile, the stackup, the net classes and the board. No knob.
2. Constraints TOML: a per-board value that cannot be derived. It carries a comment with the reason.
3. Flag: only where the value changes per invocation (which board, how far, an answer).

A flag that repeats a constraints key is deleted. No flag may carry a rule value, a layer name, a tool path or a time.

### 3.2 Flags by group

Counts are parsers that define the flag / commands where it was ever used.

| group | examples (parsers / used) | after |
|---|---|---|
| records | `--constraints` 73/29, `--spec` 44/10, `--mpn-map` 16/3, `--datasheets` 14/3, `--sch`, `--lib` | `--constraints` stays; the rest move to `[project] spec, mpn_map, sch, lib` and `[docs] datasheets` (both sections exist) |
| board in/out | `--board` 49, `--out` 55, `--workdir`, `--report-dir` 105, `--report`, `--json` 49 | BOARD positional; outputs derived from the workdir layout; `--workdir` and `--json` stay |
| rule values | `--track` 9/0, `--clearance` 9/0, `--via` 8/0, `--drill` 3/0, `--width`, `--cell`, `--pitch` | derived: `constraints.fab_profile` -> `[route] min_*` -> net class (`routerules.rules`) |
| layers | `--planes` 3/0, `--layers` 6/2, `--plane-nets`, `--bridge-layers` | derived from stackup and zones (`krtchain.plane_plan:27`, `pairs.pair_layers:384`, `questions.check_layer_roles:226`); override `[preroute] signal_layers, plane_layers` (exists) |
| tool paths | `--kicad-cli` 15/0, `--jar` 4/0, `--java`, `--router-py`, `--router-dir`, `--freerouting-*` | `deps.py` registry and environment; `[route] router`, `[route.freerouting]` |
| time and effort | `--budget` 4/4, `--timeout` 10/6, `--route-budget`, `--rounds`, `--passes`, `--tries`, `--max-attempts`, `--stall`, `--max-rip` | one `[flow] budget_min`; stage shares and tiers internal (2.7) |
| repair geometry | `--margin` 7/1, `--rip-radius`, `--gap`, `--stitch-max`, `--amplitude`, `--via-cost`, `--reach`, `--lane` | derived from track + clearance of the gap's class; grown by scope tier |
| scope | `--nets`, `--only`, `--refs`, `--area`, `--area-of`, `--selection`, `--net`, `--conn`, `--ids`, `--prefix` | supplied by the gap record (`nets`, `box`), not by a person |
| permissions | `--allow-shove`, `--unlock`, `--allow-locked`, `--include-locked`, `--rip`, `--no-rip`, `--allow-move-routed`, `--may-grow`, `--allow-worse`, `--allow-incomplete`, `--allow-undocumented`, `--force`, `--no-review`, `--waive` | `[flow.allow] rip = "plain"\|"low-speed"\|"any"`, `unlock_fanout`, `nudge_mm`, `grow_mm`; `[route] allow_rip` and `[review] waive` exist |
| questions | `--answer` 15/1, `--assume-defaults` 15/4, `--qjson` | `--answer` and `--assume-defaults` stay on `build`; `[answers]` exists |
| output shaping | `--top` 34/1, `-q` 33/0, `--full` 32/1, `--raw` 17/0, `--verbose`, `--brief` | compact by default; `--json` for machines; the rest deleted |
| sourcing globals | `--no-sourcing`, `--strict-sourcing`, `--sourcing-need`, `--sourcing-refresh`, `--sourcing-both`, `--big-fanout`, `--hub` | `[sourcing]` (exists), `[placement]` |

New TOML sections: `[flow]` (`budget_min`, `to`), `[flow.allow]`, `[ai]` (section 4), `[outline]` (2.10). Nothing else
is added. `[plan] edge_parts` and `[relieve] may_grow_mm, may_grow_sides` are replaced by `[outline]`.

### 3.3 Public surface after the change (target: 15 commands, at most 30 distinct flag names)

| command | flags |
|---|---|
| `build BOARD` | `--constraints --to --from --workdir --plan --status --answer --assume-defaults --json` |
| `versions status\|step\|accept\|reject\|new\|init` | `--note --next --allow-worse --from --reason --decisions --brief --root` |
| `parts ACTION` (gate, todo, fetch, page, cite, model, apply, fact) | `--constraints --json --fix` |
| `review BOARD`, `fab BOARD` | `--constraints --json --out` |
| `doctor`, `help-brief [CMD]` | none |
| agent primitives: `session`, `window`, `suggest`, `probe`, `lay`, `shove` | `--net --conn --path --at --size --ids --lock --expect-rev --png` |
| `x METHOD BOARD` (debug: run one registered method or gate with derived parameters) | `--constraints --gap` |

### 3.4 Every command

P = public. M = method or gate of the flow: the function stays, the parser goes, reachable as `x NAME`. D = debug or
authoring tool under `x`, arguments frozen. Nothing is deleted in this plan (decision D5); a deletion needs the
pruning rule of 2.12 and an owner decision.

| disposition | commands |
|---|---|
| P (15) | `build` (new, absorbs `pipeline`), `versions`, `parts`, `review`, `fab`, `doctor`, `help-brief`, `session`, `window`, `suggest`, `probe`, `lay`, `shove` (absorbs `unlay`), `x`; `status` is `build --status` |
| M, stage 1 ready (17) | `ready`, `fp-check`, `spec-check`, `sourcing`, `datasheets`, `models`, `verify-models`, `stackup`, `stackup-define`, `preflight`, `lint`, `magnetics`, `pin-groups`, `drc-scope`, `sync-nets`, `rules`, `bom-opt` |
| M, stage 2 place (14) | `place`, `modules`, `floorplan`, `floorplan-check`, `anchor-check`, `density`, `clusters`, `templates`, `intent`, `placement-check`, `relieve`, `wl-refine`, `lock`, `blocks` |
| M, stages 3-5 (7) | `fanout`, `thermal`, `pairs`, `plan-route`, `plan-options` (becomes a question generator), `route-ready`, `pour-island` |
| M, stage 6 close (13) | `open-nets`, `closure`, `route-diagnose` (classifier inputs); `join-gaps`, `patch`, `hand-repair`, `repair-region`, `finish` (scoped path), `repair`, `drc-fix`, `launder`, `snap-vias`, `via-min` |
| M, stages 7-8 (8) | `tune`, `silk`, `fab-advise`, `title-block`, `fit-sheet`, `deliver`, `pcbway`, `progress` (becomes state and the stop rule) |
| M, folded into `build --status` (3) | `route-status`, `route-wait`, `calls` |
| M, folded into `parts ACTION` (7) | `parts-todo`, `parts-fetch`, `parts-page`, `parts-cite`, `parts-model`, `parts-apply`, `parts-fact` |
| D (16) | `copy-net`, `lock-net`, `replace-footprint`, `schematic`, `netlist`, `map`, `plan-compare`, `part-diff`, `corpus`, `intake`, `intent-draft`, `comprehend`, `comprehend-intent`, `tournament`, `calibrate`, `unlay` (until `shove` covers it) |
| D, superseded wrappers (7) | `auto`, `compact` (the shrink step is also a place method), `plan`, `gather`, `analyze`, `eval`, `route`: moved behind `x`, not deleted (decision D5). The `finish --nets` legacy path (`cli.py:1745`) stays until `st_route` uses `region.run` |

That is all 105 top-level names plus `build` and `x`.

## 4. AI in the loop

No LLM call exists in the code today (searched `fluxplace/*.py`). The AI is a rung of the dispatcher, after the
deterministic rungs, never a replacement for them.

### 4.1 When a call is justified

| condition | value |
|---|---|
| gap kinds | `open-sealed`, `pair-open`, `pair-asym`, `rf-width`, `rf-vias`, `clearance` |
| only after | every deterministic rung failed at its last tier, and a seal report or a candidate exists |
| order | class (hs-pair, rf, power, pair, plain), then the gap that blocks the most others |
| budget | `[ai] max_calls` per build (default 12), 2 per gap, reply at most 400 tokens |
| never | mechanical kinds (`dangling`, `via-offcentre`, `subfloor`, `acid-trap`, `stale-fill`, `silk`), owner decisions, anything a question covers |

### 4.2 Modes (`[ai] mode`)

| mode | behaviour |
|---|---|
| `off` | the gap becomes a blocker row; `build` exits 1 |
| `packet` (default) | `build` writes `W/ai/<gap>.packet.json`, goes on with the other gaps and exits 5. The agent that runs the build writes `W/ai/<gap>.action.json`; the next `build` applies it under the guard and continues. Never interactive. |
| `command` | `[ai] command = [...]`: an executable that gets the packet on stdin and prints the action JSON, with a timeout. The call happens inside the loop. fluxplace takes no SDK dependency. **Unverified**: which executable; it is a configuration value. Comes second (decision D2). |

Owner decisions stay questions (exit 3): moving a fixed part, a courtyard waiver, a rule relaxation, growing the board,
a footprint defect. `questions.py:12` already says an agent never answers them.

### 4.3 Problem packet (target under 4 kB)

```json
{"v": 1,
 "gap": {"id": "open-sealed:NET_A:U1.3:R4.1", "kind": "open-sealed", "class": "plain", "net": "NET_A",
         "a": {"pin": "U1.3", "xy": [12.4, 30.1], "layers": ["F.Cu"]}, "b": {"pin": "R4.1", "xy": [15.0, 33.2], "layers": ["F.Cu"]}},
 "rules": {"track": 0.15, "clearance": 0.127, "via": [0.46, 0.2], "layers": ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"], "angles": 45},
 "window": {"box": [10, 28, 17, 35], "via_sites": [[13.1, 31.0]],
            "obstacles": [{"id": "a1b2c3d4", "kind": "track", "net": "NET_B", "class": "plain", "layer": "F.Cu", "locked": false, "a": [12.0, 31.0], "b": [16.0, 31.0]}]},
 "seal": {"reached": {"F.Cu": {"bbox": [12.0, 29.8, 13.0, 30.9], "cells": 212}}, "limiting": [{"id": "a1b2c3d4", "net": "NET_B", "locked": false, "border_cells": 40}]},
 "tried": [{"method": "handrepair._one", "verdict": "no unlocked blocker set frees it"},
           {"method": "repairregion.repair", "margin": 4.5, "verdict": "opens not reduced"}],
 "candidates": [{"n": 1, "type": "rip", "ids": ["a1b2c3d4"], "then_found": true},
                {"n": 2, "type": "nudge", "ref": "C12", "dx": 0.2, "dy": 0.0, "rot": 0},
                {"n": 3, "type": "path", "path": "F.Cu: 12.4,30.1 13.1,31.0; via 13.1,31.0; In2.Cu: 13.1,31.0 15.0,33.2", "miss": [{"seg": 2, "obstacle": "e5f6a7b8", "short_mm": 0.04}]}],
 "budget": {"actions": 4, "reply_tokens": 400}}
```

| field | built by |
|---|---|
| `gap` | `handroute.open_nets:158`, the gap record |
| `rules` | `routerules.rules`, the model's `net_rule` |
| `window` | `handroute.window:262` (`_window_public`), cut to 24 obstacles and 12 via sites nearest the gap |
| `seal` | `handroute._seal_report:1259` |
| `tried` | `W/gaps.json` |
| `candidates` | `suggest(explore=True)` remove sets, `closure._nudge`, `probe_prims:449` with its miss and shift |
| `krt` | the failed `krt` attempt: KRT JSON summary (`failed_single`, `open_single`) and its blocking analysis (`py_router/blocking_analysis.py`), cut to the gap window (D7) |

No log text and no board file goes into a packet. A PNG (`window --png`) is added only with `[ai] image = true`.

### 4.4 Action schema (strategy only)

The AI never writes track coordinates (2.8). It picks among candidates, names what to rip or nudge, names layers, and
for a pair gives at most a coarse centre line. A router then draws the copper.

```json
{"gap": "open-sealed:NET_A:U1.3:R4.1",
 "why": "sealed by NET_B on F.Cu; no via site in the pocket",
 "actions": [{"do": "rip", "ids": ["a1b2c3d4"], "chain": true},
             {"do": "route", "nets": ["NET_A", "NET_B"], "layers": ["F.Cu", "In2.Cu"], "order": ["NET_A", "NET_B"]}]}
```

| `do` | maps onto | check before the guard |
|---|---|---|
| `pick n` | the candidate's own function (a `path` candidate was found by `suggest`, not by the AI) | none |
| `rip ids chain` | `handroute.shove:1014` | ids exist; locked ids only when the gap's permission tier allows |
| `route nets layers order` | KRT on the named nets with all other copper kept (`pipeline._route_cmd`, `--keep-input-copper`), in the given order and layers (D7); then `handroute.suggest` + `lay`; `repairregion.repair` (Freerouting) last | pre-check of 2.11 |
| `guide net waypoints layers` | a rough path (at most 8 waypoints, a layer each) turned into a corridor; KRT or `suggest` routes inside it (D7) | waypoints inside the window |
| `restore net` | `handroute.copy_net:874` from the session start board | copied count equals the source |
| `nudge ref dx dy rot` | `legality.Legal`, `boardwrite.move_file:120`, then the local re-route | decision D4: at most 3 pads, unfixed, within the `closure` limit |
| `pair centre` | `pairs.lay_section:166` with the coarse centre line as `preset` (**unverified**: that `preset` accepts an outside centre line) | `pairaudit.audit` says `complete` |
| `level L` | ask the loop to climb (2.9) with the reason; the loop still checks the trigger | trigger measured |
| `give-up reason` | `progress.block:134`, ledger row | none |

`why` is kept in the attempt log: it is the AI's reading of the seal and costs nothing to store.

Guard: all actions of one reply run in one `handroute` session as a transaction. Single steps get the `edit` mode. The
end state is judged by `drcguard.accept` in `repair` mode, the same call every other rung gets. On refusal the session
is undone, the reason is added to `tried`, and the gap gets at most one more packet.

### 4.5 Sealed pockets and pairs

| case | what the AI gives | what stays with the routers and the guard |
|---|---|---|
| sealed pocket, several gaps in one box | the rip set, the net order and the layers for the merged region | all geometry (`suggest`, Freerouting on the region), pre-check, guard, restore of every ripped net |
| pair that no search closed | a coarse centre line: at most 8 waypoints with a layer each and the via-pair positions | both legs, break-outs and the skew bump from `pairlay.legs:290`, `breakout:404`, `bump:456`; `pairaudit` judges |
| high-speed pair still not `complete` | nothing more | blocker; never accepted uncoupled; build continues, exit 1, `fab` refuses (D6) |
| bulk routing | not offered | stage 5 routers |

## 5. Migration plan

Each phase is one sitting for one agent, with a recipe written by the top-level model. Keep `pytest tests/` green
(1,629 test functions in 169 files) and add the named tests. Boards are read, copied to a scratch folder and never
written: `W14/CURRENT.kicad_pcb` (495 opens, 0 shorts, 1 error, unrouted) and `W13/stageB/routed.kicad_pcb` (5 opens,
0 shorts, 210 errors; by hand and script it reached 4 opens, 15 errors). Order: highest one-pass gain first.

| phase | work | files | tests | measurement |
|---|---|---|---|---|
| 0 | DONE (ce46aea): `drcfull.complete` fills the capped unconnected list in every open count | `drcfull.py` and 16 callers | `test_drcfull.py` | still to record in the ledger: `lay` on a copy of W14 `d.kicad_pcb` no longer reports a new open on an untouched net |
| 1 | `gaps.py`: `classify`, `DISPATCH`, `close` for the open, DRC and via kinds (L0-L1), calling the existing functions unchanged. Memo, `W/best.kicad_pcb`, `W/gaps.json`, `W/stop.json`. **Writes `.fluxplace/attempts.jsonl` (2.12) from the first attempt.** Registered as stage `close` after `route` (`pipeline.py:28`, `FUNCS:1076`); run with the existing `pipeline --from close --to close`. No new command or flag. `session_commit` is the transaction guard for now. | new `fluxplace/gaps.py` (about 350 lines), `pipeline.py` (about 25 lines), `calllog.py` (shared append) | new `test_gaps.py` (classifier on fixture DRC JSON; dispatch order; memo; stop rules; attempt-line schema), `test_pipeline.py`, `test_repairregion.py`, `test_handrepair.py`, `test_snapiter.py`, `test_calllog.py` | copy of W13 `routed.kicad_pcb`: one command reaches at most 4 opens and 15 errors with no hand step; minutes; attempts file has one line per try. W14 `CURRENT`: classify only, kinds histogram and seconds |
| 2 | One measure, one guard: `drcguard.measure` and `accept` (2.6), trades allowed (D1). Replace the raw-total rules (`cli.py:1661,1764,1833`, `drcfix.py:443`); route `repairregion`, `session_commit`, `snapiter`, `patch` through `accept`. | `drcguard.py`, `cli.py`, `drcfix.py`, `repairregion.py`, `handroute.py`, `snapiter.py`, `patch.py` | `test_drcguard.py`, `test_tune_match_guard.py`, `test_handroute.py`, `test_region.py`, `test_patch_badpts.py` | W13 copy: not worse than phase 1; attempt log shows which refusals turned into closes |
| 2b | KRT + AI loop (D7, D8): `krt` rung failure -> packet with KRT summary + blocking analysis + seal report -> AI action (rip, nudge, corridor, guide, order/layers) in one session -> KRT re-route of the area -> `accept` (repair mode). Mode `packet` first (D2). Section 4 with the D7 changes; pulled forward from phase 12. | `gaps.py`, `handroute.py`, `krtchain.py` | `test_gaps_ai.py` | W13 copy: opens closed by the loop, AI calls and tokens per closed gap |
| 3 | Trades inside a transaction (`edit` mode; `handroute._edit` already has `partial`, `require_close`, `extra_check`) and the pair rungs: pair kinds join `DISPATCH`, pair rung 4 (`repairregion._blockers` + `pairsonly.reroute`). Remove the board constants of C9 (planes from `krtchain.plane_plan`, plane nets from zones). | `handroute.py`, `handrepair.py`, `repairregion.py`, `gaps.py`, `region.py`, `finish.py`, `patch.py` | `test_handrepair.py`, `test_pairsonly.py`, `test_repairregion.py`, `test_region.py` | W13 copy: opens below 4; ETH_P0, PCIE_TX and USB_DP_CM5 pairs `complete` or a blocker row with the measured reason |
| 4 | `build` as a library call and command: `pipeline.run` + `close` after pairs (pair kinds) and after route (all kinds) + `versions` step/accept inside + thermal stages + effort tiers (drops the second constraints file) + `[flow]`, `[flow.allow]`. D6 behaviour. | `cli.py`, `pipeline.py`, `pairs.py` (stage), `versions.py` (callable accept), `thermal.py` | `test_pipeline.py`, `test_versions.py`, `test_pairs.py`, `test_pairfb.py`, `test_questions.py` | W14: `build` on a copy of `CURRENT` replaces `run-w14.sh` and `run-w13-finish.sh`; opens, errors, pairs complete, minutes against the W13 script run (5 opens, 18/35 pairs, about 2.5 h) |
| 5 | Corpus and score: manifest with the boards of 2.14, a `build` step in `corpus.run_board`, the score, `docs/corpus/trend.jsonl`, the attempts report (`x attempts`). First baseline run. | `corpus.py`, `boardmatrix.py`, `gaps.py` (report), new `docs/corpus/` | `test_corpus.py`, `test_gaps.py` | first trend line; per-method close rates; the first pruning candidates listed (not acted on) |
| 6 | L2 and L3: nudge as an action with the local re-route; cluster re-place on the rework box; triggers and `level_up` records; copper outside the box proven by `region.check`. | `gaps.py`, `closure.py`, `floorplan2.py` (`reseat_module` entry), `relieve.py` (guard reuse), `region.py` | `test_closure.py`, `test_region_fingerprint.py`, `test_floorplan.py`, `test_relieve.py` | W13 copy and W14: gaps closed at L2 and L3, with the trigger values; corpus score not worse |
| 7 | Fast pre-check (2.11): local check before every candidate DRC; `drc_runs` and `precheck` fields filled. | `gaps.py`, `drcguard.py`, `handroute.py`, `routeaudit.py` | `test_handroute.py`, `test_drcguard.py`, routeaudit tests if any exist (**unverified**) | W13 copy: DRC runs per accepted repair at most 1.2; minutes against phase 6; `precheck_miss` count |
| 8 | Stage 0 and outline intake: `sync` stage (both directions of board vs schematic/spec), `check_outline` questions, `[outline]` block. | `pipeline.py`, `questions.py`, `kicad_io.py` (sync reuse), `constraints.py` | `test_questions.py`, `test_pipeline.py` | W14 copy: `build --to ready` exits 3 and names C316-C319; with answers recorded it passes |
| 9 | Size search and L5/L6: the `planopts.search` loop inside stage 2, one `grow_text`, size proven by routing (2.10), feedback from gap clusters, L6 question with `planopts` and `fabadvise` evidence. | `planopts.py`, `floorplan2.py`, `density.py`, `gaps.py`, `pipeline.py` | `test_planopts.py`, `test_floorplan.py`, `test_density.py` | W7 replay: from the 117.6 x 98.7 mm start, one `build` call; record the size reached, whether it routed, attempts and minutes against the 15 hand-driven attempts |
| 10 | L4: side moves by per-side density (`compact.pick_flips`, `flip.flip_file`, `flip.verify`). | `gaps.py`, `compact.py`, `flip.py`, `floorplan2.py` | `test_flip.py`, `test_compact.py` | W14 copy: per-side utilisation before and after; corpus score not worse |
| 11 | Events and state: `on_event`, `W/events.jsonl`, the extended `state.json`, CLI output rendered from events, `build --status`. | `pipeline.py`, `gaps.py`, `cli.py`, `routestatus.py` | `test_pipeline.py`, new `test_events.py` (schema, order, replay equals state) | a W13-copy run replayed from `events.jsonl` gives the same `state.json` |
| 12 | AI rung: `gaps.packet`, `gaps.apply_actions`, mode `packet` with exit 5 (D2); mode `command` after the numbers are in. | `gaps.py`, `handroute.py` (window cut), `questions.py` (exit table only) | new `test_gaps_ai.py` (packet size cap, schema, no coordinates accepted outside `pair centre`, refused action undone) | W14 gaps left: packets under 4 kB; closes per call; tokens per closed gap in the attempt log |
| 13 | Surface: `x` namespace, M and superseded parsers behind it (nothing deleted, D5), MCP tools from the public list, budget tests (section 6). First pruning review from the corpus data, recorded in the ledger. | `cli.py`, `hrcli.py`, `plancli.py`, `partscli.py`, `fancli.py`, `mcp_server.py`, `README.md`, `AGENTS.md`, `docs/FLOW.md`, `docs/ONE-SHOT-LEDGER.md` | `test_mcp.py`, new `test_surface.py` | `help-brief` fits 20 lines; a W14 `build` call log shows no command outside the public list |

Why this order: phases 1-4 remove the hand-run finish chain, the refused trades and the shell scripts. Phase 5 comes
before the ladder so that every later phase is judged by the corpus score, not by one board. Phases 6-10 add the
ladder from the cheapest level up. Events and the AI rung need the loop to be stable first.

## 6. Guardrail rules (final wording for `CLAUDE.md`)

```
## One flow (owner rule)
1. One entry point builds a board: `fluxplace build` (a library call with a CLI on top). A fix is never a new command.
2. A repair exists only as a row of `gaps.DISPATCH` (gap kind -> ordered methods). Code that changes copper or
   moves a part and is not reachable from that table is a defect. Add a rung, or extend a method that is in it.
3. One measure, one guard: `drcguard.measure` and `drcguard.accept`. No other module runs `kicad-cli pcb drc`,
   counts opens, or decides whether a board is better. `tests/test_surface.py` greps for it.
4. Proven router first: copper geometry comes from KRT, Freerouting, the pair constructor (`pairlay`) or, for a
   single connection, the in-house grid search. AI chooses strategy only (rip set, nudge, layers, net order, a
   coarse pair centre line). No code path accepts track coordinates from a model.
5. Every attempt is logged: one line in `.fluxplace/attempts.jsonl` per rung tried, written by the loop, with the
   result, the reason, the score before and after, seconds, DRC runs and AI tokens. A method that is not logged
   is not in the flow.
6. Pruning is by data: a method with 0 closes over 3 corpus runs (at least 10 tries, 2 boards) is reviewed,
   recorded in `docs/ONE-SHOT-LEDGER.md` with its numbers, removed from the table and kept behind `x`. Deleting
   code is an owner decision.
7. Parameters: derived default > constraints TOML > flag. A flag never carries a rule value, a layer, a net,
   a tool path or a time. A new flag needs a real board that needed it and a line in the ledger.
8. Budgets, enforced by `tests/test_surface.py`: public commands <= 15; distinct public flag names <= 30;
   `x` debug arguments may only shrink. Raising a budget is an owner decision recorded in the test.
9. Escalation never goes below the floors (fab profile by layer count; `[route] min_*`; 0.127 mm) and never
   changes pair, RF or power geometry. High-speed pairs are complete and coupled or they are blockers.
10. The loop climbs only on measured evidence (L0 copper, L1 region, L2 nudge, L3 re-place a cluster, L4 side,
    L5 outline, L6 owner). Copper outside the rework box stays and is proven unchanged. Fixed and locked parts
    never move. Size is proven by routing, not by an estimate.
11. Best so far: the best accepted board is always on disk; the score only falls; a tried (gap, method, scope,
    copper hash) is not tried again; every stop writes a stop report.
12. Full DRC only on the candidate about to be accepted; a local check comes first. Target: at most 1.2 DRC
    runs per accepted repair.
13. Board-agnostic: no reference, net name, layer name or dimension of a board in the code. `tests/test_surface.py`
    greps `fluxplace/` for layer-name literals in defaults.
14. A gap the flow cannot close gets a blocker row (measured reason) and a row in `docs/ONE-SHOT-LEDGER.md`,
    drafted from the attempt log. The next change is a rung or a classifier kind, measured on the corpus.
15. A change to a rung, a guard or a classifier kind is kept only when the corpus score is not worse.
16. AI is a rung: compact packet in, action JSON out, same guard. No log or board file goes to a model.
    Owner decisions are questions (exit 3), never AI answers.
17. Events, not logs: `build` reports through the event stream and one state file. Nothing parses log text.
18. Net code: a change that adds a module removes or folds one. State the before and after line counts.
```

## 7. Owner decisions

### 7.1 DECIDED (owner, 2026-11-19)

| # | question | decision |
|---|---|---|
| D1 | May the guard accept a trade: a repair that opens one plain net while the board score falls (2.6 R4)? | DECIDED 2026-11-19: yes, trades are allowed |
| D2 | AI mode default | DECIDED 2026-11-19: `packet` first, then `command` |
| D3 | `adaptive.py` and `escape.py` thin traces by rule ladder | DECIDED 2026-11-19: they stay out of the dispatcher |
| D4 | Part nudge without asking | DECIDED 2026-11-19: the flow may nudge parts with at most 3 pads, unfixed, within the `closure` limit |
| D5 | The 7 superseded wrappers (`auto`, `compact`, `plan`, `gather`, `analyze`, `eval`, `route`) | DECIDED 2026-11-19: behind the `x` debug namespace; not deleted, the logic is kept |
| D6 | High-speed pair not complete after every rung | DECIDED 2026-11-19: keep routing, exit 1, `fab` refuses |
| D7 | KRT + AI loop: how much copper may the AI draw? | DECIDED 2026-11-19: guide only. The AI rips blockers, nudges parts (D4), clears corridors, and gives rough waypoints with layers; KRT or grid search draws the copper; the guard judges |
| D8 | When is the KRT + AI loop built? | DECIDED 2026-11-19: phase 2b, right after phase 2 (the trade guard), ahead of phase 3 |

### 7.2 Open questions from this revision (recommendations used as defaults until the owner overrides; L5 growth decided as an owner prompt, see 2.9)

| # | question | recommendation |
|---|---|---|
| Q1 | Default per-side density target: 0.60 (the OK band in `density.py`) or closer to 0.75? | 0.60 until the corpus shows boards that route above it |
| Q2 | May L3 (re-place a cluster) and L4 (move parts to the other side) run without asking, for parts that are not fixed or locked? | L3 yes; L4 as a question the first time on a board, then recorded in `[answers]` |
| Q3 | Corpus: may the razor boards (outside `/var/kicad`) and two KiCad demo boards be used as read-only copies? Which open-source boards? | yes; `cm5_minima` and `stickhub` from the KiCad demos |
| Q4 | Pruning threshold: 0 closes over 3 corpus runs, at least 10 tries on 2 boards | accept as the start value |
| Q5 | `round` and `square` start outlines need a small outline generator (new code). Needed now, or only `given` and `rectangle`? | `given` and `rectangle` first |
| Q6 | Size proven by routing costs up to 3 full routes per board in the size search. Accept that time? | yes; W7-W14 cost 7 board versions without it |
| Q7 | Margin after the smallest routed size: one growth step on the edge nearest the last gap cluster | accept |

## 8. Unverified

- That the 7 superseded wrappers hold no unique logic (only help text and call counts were read). Moot for now: D5 keeps them.
- `pairs.lay_section(preset=...)` as the entry for an AI centre line (signature read, body not).
- Why `hand-repair` rows say "closed" while the commit count did not fall (`handrepair.py:127-130` treats a vanished
  connection key as closed; cause not traced).
- `st_route` internals between lines 415 and 767 were read by call site only.
- Flag usage maps argv tokens to parser destinations; argparse prefix abbreviations with more than one match are not counted.
- KiCad push-and-shove from a script: no entry found in the code; KiCad 10 capability not checked.
- L3: `floorplan2.reseat_module:521` and `planplace.plan_place:588` were read by signature and docstring only; that
  they can be limited to one module on a routed board is inferred.
- L4: `compact.pick_flips:124` and `flip.flip_file:53` exist; their fit with rigid modules and locked fan-out is inferred.
- `planopts.search` was read by docstring and signature; using the floorplan as its re-place step is inferred.
- Stage 0: the reverse check (board parts missing from the schematic or spec) may not exist.
- Corpus boards other than W14 and W13: paths found, state, constraints and demo board file names not checked.
- W7 tool issues 2-6 may already be fixed.
- The pre-check targets (1.2 DRC runs, 80 %) are goals, not measurements.
