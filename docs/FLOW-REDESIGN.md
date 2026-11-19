# Flow redesign: one build, one gap loop, one guard

Design review of 2026-11-19. Read-only: no code was changed for it. Every claim cites the code or the call logs it
came from; anything not read is marked **unverified**. Measured on `main` at a6fe6f9 plus the uncommitted `drcfull` work.

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
| C4 | "Open count" has no single definition. 19 modules run their own `kicad-cli pcb drc` with three flag sets (`--severity-error` or `--severity-all`, with or without `--refill-zones`, with or without `--all-track-errors`). The kicad-cli list is capped near 499, which made `lay` report opens that were not new. | ledger row W14 step D; the fix has to touch 16 modules (`grep -l drcfull fluxplace/*.py`) |
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
| 1 | ready | `pipeline.st_ready:232` (parts gate, `fpcheck.check_board`, stackup, `questions.guard`) | parts complete, footprints pass, stackup solved, no open question | exit 3, or gap `footprint` |
| 2 | place | `st_modules:922`, `st_floorplan:955`, `st_relieve:1052`, `wlrefine` | `placementcheck`, `intent.check`, `legality.collision_gate` | gaps `placement`, `courtyard` |
| 3 | fanout | `st_fanout:974`, `st_pour_tie:1003`, `thermal.run(stage="pre")` | FANOUT_OK | gap `open-sealed` on the blocked pins |
| 4 | pairs | `st_pairs:1070`: `pairs.run` on the high-speed classes first, then the rest (order from net-class priority, already in `pairs.plan`) | `pairaudit.audit`: every pair `complete` | gaps `pair-*`; the gap loop runs here for pair kinds **before** route |
| 5 | route | `st_route:415` (router from `[route] router`), `routeladder.Ladder`, `planetie.run_guarded`, `pourtie.repair` | opens counted by the one measure | gaps |
| 6 | close | new `gaps.close` (2.4-2.7); also `thermal.run(stage="post")` | no gap left | exit 1, 2 or 5 |
| 7 | review | `st_tune:867`, `st_silk:883`, `st_review:900`, `fabadvise` | review FAIL = 0 | review findings become gaps; one more `close` round |
| 8 | fab | `opengate.refuse`, `fab`, `deliver` (only with `--to fab`) | open gate, review gate | refuse |

High-speed pairs that are not `complete` after stage 4 are gaps with the highest class. They are never passed to the
router as ordinary nets. Low-speed pairs may still end in `pairfb` state c (connected, reported).

### 2.3 State

| file | content | status |
|---|---|---|
| `W/state.json` | stage verdicts, next step; capped at 2,400 bytes (`pipeline.py:31,53`) | exists |
| `W/gaps.json` | every gap with its tries (method, scope tier, verdict, seconds) | new |
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
  the owner rule is never to thin network, RF or power traces. Owner decision D3.
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

New TOML sections: `[flow]` (`budget_min`, `to`), `[flow.allow]`, `[ai]` (section 4). Nothing else is added.

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
authoring tool under `x`, arguments frozen. X = delete the CLI wrapper because the flow supersedes it; the modules stay
until a reference check shows they are unused.

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
| X (7 wrappers + 1 path) | `auto`, `compact` (the shrink step stays as a place method), `plan`, `gather`, `analyze`, `eval`, `route`; and the `finish --nets` legacy path (`cli.py:1745`) once `st_route` uses `region.run`. **Unverified**: that these have no unique logic; phase 6 starts with a reference check |

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
| `command` | `[ai] command = [...]`: an executable that gets the packet on stdin and prints the action JSON, with a timeout. The call happens inside the loop. fluxplace takes no SDK dependency. **Unverified**: which executable; it is a configuration value. Owner decision D2. |

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

No log text and no board file goes into a packet. A PNG (`window --png`) is added only with `[ai] image = true`.

### 4.4 Action schema

```json
{"gap": "open-sealed:NET_A:U1.3:R4.1",
 "actions": [{"do": "rip", "ids": ["a1b2c3d4"], "chain": true},
             {"do": "lay", "net": "NET_A", "path": "F.Cu: 12.4,30.1 15.0,33.2"},
             {"do": "restore", "net": "NET_B"}]}
```

| `do` | maps onto | check before the guard |
|---|---|---|
| `pick n` | the candidate's own function | none |
| `lay net path` | `handroute.probe:593`, then `handroute.lay:854` | probe: floors, 45/90, ends touch the islands |
| `rip ids chain` | `handroute.shove:1014` | ids exist; locked ids only when the gap's permission tier allows |
| `restore net` | `handroute.copy_net:874` from the session start board | copied count equals the source |
| `nudge ref dx dy rot` | `legality.Legal`, `boardwrite.move_file:120`, attached copper re-laid | part not fixed, within `[flow.allow] nudge_mm` |
| `pair centre` | `pairs.lay_section:166` with the centre line as `preset` (**unverified**: that `preset` accepts an outside centre line) | `pairaudit.audit` says `complete` |
| `give-up reason` | `progress.block:134`, ledger row | none |

Guard: all actions of one reply run in one `handroute` session as a transaction. Single steps get the `edit` mode. The
end state is judged by `drcguard.accept` in `repair` mode, the same call every other rung gets. On refusal the session
is undone, the reason is added to `tried`, and the gap gets at most one more packet.

### 4.5 AI routing for sealed pockets and pairs

| case | what the AI gives | what stays deterministic |
|---|---|---|
| sealed pocket, several gaps in one box | an ordered list of `rip`, `lay`, `restore` for all nets of the merged region | probe, floors, guard, restore of every ripped net |
| pair that no search closed | a coarse centre line: at most 8 waypoints with a layer each and the via-pair positions | both legs, break-outs and the skew bump from `pairlay.legs:290`, `breakout:404`, `bump:456`; `pairaudit` judges. The AI never writes leg geometry |
| high-speed pair still not `complete` | nothing more | blocker; never accepted uncoupled |
| bulk routing | not offered | stage 5 routers. Whole-board AI routing costs more than it closes |

## 5. Migration plan

Each phase is one sitting for one agent, with a recipe written by the top-level model. Keep `pytest tests/` green
(1,629 test functions in 169 files) and add the named tests. Boards are read, copied to a scratch folder and never
written: `W14/CURRENT.kicad_pcb` (495 opens, 0 shorts, 1 error, unrouted) and `W13/stageB/routed.kicad_pcb` (5 opens,
0 shorts, 210 errors; by hand and script it reached 4 opens, 15 errors).

| phase | work | files | tests | measurement |
|---|---|---|---|---|
| 0 | Finish F2 (in progress elsewhere): complete open list in every count | `drcfull.py` and its callers | `test_drcfull.py` | `lay` on a copy of W14 `d.kicad_pcb` no longer reports a new open on an untouched net |
| 1 | `gaps.py`: `classify`, `DISPATCH`, `close` with the rungs of 2.5 for the open, DRC and via kinds, calling the existing functions unchanged. Registered as stage `close` after `route` (`pipeline.py:28`, `FUNCS:1076`); run with the existing `pipeline --from close --to close`. No new command or flag. Uses `session_commit` as the transaction guard for now. | new `fluxplace/gaps.py` (about 300 lines), `pipeline.py` (about 25 lines) | new `test_gaps.py` (classifier on fixture DRC JSON; dispatch order; memo; stop rules), `test_pipeline.py`, `test_repairregion.py`, `test_handrepair.py`, `test_snapiter.py` | copy of W13 `routed.kicad_pcb`: one command reaches at most 4 opens and 15 errors with no hand step; record time. W14 `CURRENT`: classify only, kinds histogram and seconds |
| 2 | One measure, one guard: `drcguard.measure` and `accept` (2.6). Replace the raw-total rules (`cli.py:1661,1764,1833`, `drcfix.py:443`), route `repairregion`, `session_commit`, `snapiter`, `patch` through `accept`. Remove the board constants of C9 (planes from `krtchain.plane_plan`, plane nets from zones). | `drcguard.py`, `cli.py`, `drcfix.py`, `repairregion.py`, `handroute.py`, `snapiter.py`, `patch.py`, `region.py`, `finish.py` | `test_drcguard.py`, `test_tune_match_guard.py`, `test_handroute.py`, `test_region.py`, `test_patch_badpts.py` | same W13 copy: result not worse than phase 1; DRC runs per round counted (target: 1 per candidate) |
| 3 | Trades and the missing rungs: `edit` mode inside a transaction (`handroute._edit` already has `partial`, `require_close`, `extra_check`); pair rung 4 (`repairregion._blockers` + `pairsonly.reroute`); nudge as an action. Pair kinds join `DISPATCH`. | `handroute.py`, `handrepair.py`, `repairregion.py`, `gaps.py`, `closure.py` | `test_handrepair.py`, `test_pairsonly.py`, `test_closure.py`, `test_repairregion.py` | W13 copy: opens below 4; the ETH_P0, PCIE_TX and USB_DP_CM5 pairs `complete` or a blocker row with the measured reason |
| 4 | `build`: `pipeline.run` + `close` after pairs (pair kinds) and after route (all kinds) + `versions` step/accept inside + thermal stages + effort tiers (drops the second constraints file) + `[flow]`, `[flow.allow]`. | `cli.py`, `pipeline.py`, `pairs.py` (stage), `versions.py` (callable accept), `thermal.py` | `test_pipeline.py`, `test_versions.py`, `test_pairs.py`, `test_pairfb.py`, `test_questions.py` | W14: `build` on a copy of `CURRENT` replaces `run-w14.sh` and `run-w13-finish.sh`; compare opens, errors, pairs complete and minutes with the W13 script run (5 opens, 18/35 pairs, about 2.5 h) |
| 5 | AI rung: `gaps.packet`, `gaps.apply_actions`, mode `packet` with exit 5, then mode `command`. | `gaps.py`, `handroute.py` (window cut), `questions.py` (exit table only) | new `test_gaps_ai.py` (packet size cap, schema validation, refused action undone) | W14 gaps left by phase 4: packets under 4 kB; closes per call; tokens per closed gap |
| 6 | Surface: reference check, then `x` namespace, remove M parsers, delete X wrappers, MCP tools from the public list, budget tests (section 6). | `cli.py` (target under 800 lines), `hrcli.py`, `plancli.py`, `partscli.py`, `fancli.py`, `mcp_server.py`, `README.md`, `AGENTS.md`, `docs/FLOW.md` | `test_mcp.py`, new `test_surface.py` | `help-brief` output fits 20 lines; a W14 `build` call log shows no command outside the public list |

Order of value: phase 1 removes the hand-run finish chain, phase 3 removes the cause of the `hand-repair` and
`pairs --only` failures, phase 4 removes the shell scripts.

## 6. Guardrail rules (final wording for `CLAUDE.md`)

```
## One flow (owner rule)
1. One entry point builds a board: `fluxplace build`. A fix is never a new command.
2. A repair exists only as a row of `gaps.DISPATCH` (gap kind -> ordered methods). Code that changes copper or
   moves a part and is not reachable from that table is a defect. Add a rung, or extend a method that is in it.
3. One measure, one guard: `drcguard.measure` and `drcguard.accept`. No other module runs `kicad-cli pcb drc`,
   counts opens, or decides whether a board is better. `tests/test_surface.py` greps for it.
4. Parameters: derived default > constraints TOML > flag. A flag never carries a rule value, a layer, a net,
   a tool path or a time. A new flag needs a real board that needed it and a line in the ledger.
5. Budgets, enforced by `tests/test_surface.py`: public commands <= 15; distinct public flag names <= 30;
   `x` debug arguments may only shrink. Raising a budget is an owner decision recorded in the test.
6. Escalation never goes below the floors (fab profile by layer count; `[route] min_*`; 0.127 mm) and never
   changes pair, RF or power geometry. High-speed pairs are complete and coupled or they are blockers.
7. Board-agnostic: no reference, net name, layer name or dimension of a board in the code. `tests/test_surface.py`
   greps `fluxplace/` for layer-name literals in defaults.
8. A gap the flow cannot close gets a blocker row (measured reason) and a row in `docs/ONE-SHOT-LEDGER.md`.
   The next change is a rung or a classifier kind, measured on that board.
9. AI is a rung: compact packet in, action JSON out, same guard. No log or board file goes to a model.
   Owner decisions are questions (exit 3), never AI answers.
10. Net code: a change that adds a module removes or folds one. State the before and after line counts.
```

## 7. Owner decisions needed

| # | question | recommendation |
|---|---|---|
| D1 | May the guard accept a trade: a repair that opens one plain net while the board score falls (2.6 R4)? | Yes. It is the cause of C3; the opened net returns as a gap; a worse board is still refused |
| D2 | AI mode default: `packet` (the running agent answers, exit 5) or `command` (fluxplace calls a configured executable inside the loop)? | `packet` first; `command` after phase 5 shows closes per call |
| D3 | `adaptive.py` and `escape.py` thin traces by rule ladder. Keep them out of the dispatcher? | Yes; plain nets may go down to the floor only, never below |
| D4 | Part nudge without asking: which limit? | Parts with at most 3 pads, not fixed, at most the `closure` nudge limit; anything else is a question |
| D5 | Delete the X wrappers (`auto`, `compact`, `plan`, `gather`, `analyze`, `eval`, `route`) in phase 6? | Yes after the reference check; no board under /var/kicad ever ran them |
| D6 | High-speed pair not complete after every rung: stop the build (exit 1) or continue routing around it? | Continue to route so the rest is measured, but exit 1 and refuse `fab` |

## 8. Unverified

- That the X commands hold no unique logic (only help text and call counts were read).
- `pairs.lay_section(preset=...)` as the entry for an AI centre line (signature read, body not).
- Why `hand-repair` rows say "closed" while the commit count did not fall (`handrepair.py:127-130` treats a vanished
  connection key as closed; cause not traced).
- `st_route` internals between lines 415 and 767 were read by call site only.
- Line counts for phase 6 (`cli.py` under 800) are estimates.
- Flag usage maps argv tokens to parser destinations; argparse prefix abbreviations with more than one match are not counted.
