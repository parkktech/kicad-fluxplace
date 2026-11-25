# PLAN: dynamic placement repair (placement gaps -> rung ladders)

2026-11-24, Fable design. Design only; section 9 is the recipe list (one Sonnet agent per phase). Code base read: `w16merge`
763355f (`gaps.py`, `floorplan2.py`, `placetour.py`, `intent.py`, `budget.py`, `clusters.py`, `modules.py`, `breakout.py`,
`legality.py`, `legalgate.py`, `questions.py`, `partsgate.py`, `dsfetch.py`, `flip.py`, `lookahead.py`, `escreserve.py`,
`routediag.py`, `placechecks.py`, `liveview.py`); `w20home` working tree (uncommitted: post-anneal `home_supports`, neighbour
nudge to 4 mm, `_refusal`, `SUPPORT_WEIGHT` anneal pull, support swaps). Rules: one-flow rule, board-agnostic rule,
quality order (hs pairs first), legality gate (D: input baseline), N12 narration, parts-first.

## 0. What is wrong today (W16 numbers, cause in code)

| W16 finding | Cause in code |
|---|---|
| floorplan-3: decaps 28/79 and other supports 55/87 beyond limit, many 60-85 mm | `home_supports` runs once, only for single-part modules, one neighbour nudge <= 1 mm, no loop, no reason logged for a part it leaves. |
| placetour-1: `USB_HUB_DP` (hs pair leg), `BT_SW_RFC` (rf) ghost-unrouted, stage "pass" | `placetour.judge_key` = (opens, hot, occ, vias, detour): an hs open counts like any open; `candidates()` has no move aimed at one named net; verdict is `pass` whenever a routed copy exists. |
| 9 hard rows not honoured after floorplan and after placetour | `ready.stage_honoured` reports rows; no code acts on a failed row. `worse_gates` only stops them getting worse. |
| Back side unused except supports the seater flips | `floorplan2` header: "Side flips are NOT searched"; `[placement] sides` is asked (`questions.check_placement_sides`) but no placer reads it. |
| Missing datasheet / low stock stop at a list | `partsgate.todo` lists; the `dsfetch` chain and `partsgate.alternatives` exist but nothing loops them or turns the result into an owner question. |

Common cause: the routing stage has a gap -> rung dispatcher (`gaps.close`); placement has gates and reports only.

## 1. Architecture: `fluxplace/placegaps.py` (new, the one placement dispatcher)

No new command, no new flag. The stages `ready`, `floorplan`, `placetour` call it. Shape copied from `gaps.py`:

```
gap   = {id, kind, class, mag, refs, nets, at, row, detail}     # id = kind + ":" + stable name (row name / ref / net)
DETECT = {kind: fn(ctx) -> [gap]}                                # one detector per kind, sections 3-7
DISPATCH = {kind: [rung, ...]}      RUNGS = {name: fn(gap, ctx) -> [candidate]}
candidate = {id, rung, moves {ref: (x, y, rot, side)}, claims [rect], notes}   # same `moves` shape as placetour.candidates
close(ctx, kinds, evaluator) -> {stop, accepted, refused, gaps_in, gaps_out, score_in, score_out}
```

A rung never writes a board and never judges itself: it proposes candidates. One **evaluator** per stage applies and judges:

| Evaluator | Stage | Apply | Legality | Judge |
|---|---|---|---|---|
| `PlanEval` | floorplan (`_one`, after anneal + `home_supports`) and stage end | `Plan.try_pose` / `apply` / `restore`; side via `geom.flip` on the model | `Plan.legal`, `claims_ok`, `keepouts_ok` (input baseline: `freeze_baseline`) | `score_terms` (below) on the model; ghost terms carried unchanged |
| `JudgeEval` | placetour rounds and stage end | `placetour._apply` (+ `flip.flip_file` for a side change) | `placetour.tier1` (`gate_state` / `worse_gates`), then `legalgate.drc_new` on the candidate that would win | KRT ghost judge (`placetour.judge`), class-ranked key |
| `PartsEval` | ready | parts-todo record / spec proposal (never the spec itself) | `partsgate.validate` | `partsgate.gate` needs count |

Reused from `gaps.py` by import, not copied and not refactored: `CLASS_RANK`, `_class`, `_rank` (yield ordering of rungs),
`_halves` (split a gap list after a guard refusal), `_rung_cap`, `NO_GAIN_ROUNDS`, `STALL_MIN`, `_write_json`, the memo idea
(key = rung, kind, gap id, hash of the poses of every part within the gap window; `window_hash` is copper-based, so
placegaps adds `pose_hash(B, gap, r)`), the ledger-rows file shape, `calllog.attempts_path`. `gaps.close` is not touched.

**Score** (`placegaps.score_terms`, lexicographic, smaller is better, mirrors `gaps.score_terms`):

```
hs_open, rf_open, power_open,            # ghost-unrouted nets by class (JudgeEval; PlanEval carries the last judged value)
illegal,                                 # legalgate.count_new vs the input baseline (must be 0 to accept at all)
hard_rows,                               # hard rows not honoured (budget.honoured + intent.check, hard and not met)
hard_mag,                                # sum over those rows of (excess / limit), 3 decimals
decap_far, support_far, far_mm,          # graded supports beyond limit (count, count, summed mm beyond)
open, hot, hot_occ, vias, detour         # the rest of today's judge key
```

**Accept** (`placegaps._accept`, one place, every kind):
1. legality: no new issue against the input baseline (model check per candidate; KiCad DRC `legalgate.drc_new` before a
   candidate becomes the base of a file-level stage; `legalgate.gate` on the stage output as today);
2. never-worse terms (R6): `hs_open, rf_open, power_open, illegal, hard_rows` may not rise, whatever the target gap gained;
3. the score vector is lower, OR equal on the R6 terms with the target gap's `mag` lower (progress on the target, as
   `gaps._accept` does for sev / mag);
4. refused -> `{gap, rung, candidate, check, value_before, value_after}`; `check` is one of `legal:<kind> <ref> vs <other>`
   (w20home `_refusal`), `drc:<type> <refs>`, `row:<kind>:<name>`, `score:<term>`, `judge:<class> open`, `cap`, `memo`.

**Loop** (`close`): rounds; per round the kinds in `ORDER` (section 2), per kind the rungs in `_rank` order, per rung the
gaps worst `mag` first (ties by id). Stops: no gap in scope, `no gain N rounds` (`[flow] no_gain_rounds`), `stalled`
(`[flow] stall_min`), `[flow] budget_min`, rung cap (`[flow] rung_cap_min`). Caps are counted in candidates first
(`cands_per_rung`, derived: 8 for `PlanEval`, `[placetour] judge_top` for `JudgeEval`); wall clock is the outer stop only,
and a time stop is named in the stop reason (a time stop is the one non-deterministic outcome; it is reported, never silent).

**Deterministic**: sorted iteration everywhere, seeds = `zlib.crc32(gap id) ^ stage seed`, no `hash()`, no dict-order
reliance, judge jobs written as job files and collected in job order (as `placetour.stage` does).

**Narration / events** (N12, no new event kind): `LV.narrate(stage, text)` at the start and end of each kind and for each
accepted repair; `EV.iteration("place", i, metrics={score terms})` per round; `EV.emit("parts", refs=...)` on accept;
`EV.artifact("report", placegaps.md)`. Files in the stage dir: `placegaps.json` (gaps in / out, attempts), `placegaps.md`
(table: gap, rung, verdict, refusing check, before -> after), `ledger-rows.md` (issue / cause / fix / measured result, the
routing-ledger row shape). The stage line gains `gaps K kinds: a->b ...`.

**Constraints** (all optional; nothing else is per-board): `[placegaps] kinds = [...]` (default: all of section 2),
`max_rounds` (default 6). Existing keys reused: `[flow]`, `[placetour]`, `[placement] sides | back_max_height_mm | heights |
keepout`, `[[separation]]`, `[intent.*]`, `[swap.group.*]`, `[parts] quantity`. Every distance a rung uses is derived:
corridor width from the net class (`2w + gap + 2 x clearance`, plus a via land where the plan changes layer), nudge length =
the minimum translation that clears the corridor / strip plus the courtyard clearance, support limits from `clusters`,
row limits from the rows, heights from section 6.

## 2. Kinds, order, stages

`ORDER` (quality order: hs pairs before everything):

| # | Kind | Stage(s) | Section |
|---|---|---|---|
| 1 | `ghost-open` class hs-pair, then rf, then power | placetour | 3 |
| 2 | `row-cin-side`, `row-template`, `row-pin-bypass` | floorplan, placetour | 5 |
| 3 | `row-away` (intent away / region / `[[separation]]`), `row-flight` | floorplan, placetour | 5 |
| 4 | `row-rail` | floorplan, placetour | 5 |
| 5 | `support-far` (decap first, then other), `support-starved` | floorplan, placetour | 4 |
| 6 | `side-illegal` | floorplan, placetour | 6 |
| 7 | `ghost-open` class pair, plain; `ghost-costly`, `ghost-hot` (today's blame, unchanged moves) | placetour | 3 |
| 8 | `part-datasheet`, `part-stock` | ready | 7 |

Where the loop is called (four call sites, no other entry):
- `ready`: `close(kinds=part-*, PartsEval)` after `partsgate.gate`.
- `floorplan2._one`: after `anneal` and `home_supports`, before the second `settle`: `close(PlanEval)`.
- `floorplan2.cmd_floorplan`: after `breakout.repair` and `legalgate.repair_file`, on a `Plan` built from the output, until
  no gain; then `legalgate.gate` (unchanged) and `stage_honoured`.
- `placetour.stage`: each round, `placegaps` candidates go into the pool ahead of the blame candidates (class order); after
  the last round `close(JudgeEval)` until no gain; then the report.

## 3. Case 2: a critical net ghost-unrouted (`ghost-open`)

**Detector** `detect_ghost_open(ctx)`: input = `judge()` blame `open` (route_review on the KRT ghost copy) + the net classes
(`gaps._class` with `netclass` pairs / rf, `budget.pair_legs`, `lookahead.critical_nets`). Rule: one gap per open net; a
pair is one gap when either leg is open (`nets` = both legs); `class` = hs-pair | rf | power | pair | plain; `mag` =
unconnected pads. Before any move the gap is **diagnosed** (no board change, cached in the gap):
- `confirm`: one extra judge of the same board with the critical nets routed first (`lean_krt(..., extra=)` net order; if
  `extra` cannot carry an order, a KRT call over the gap's nets alone on the judge copy with the other copper kept). Routed
  when first -> `cause = order`: the placement has room, the ladder starts at rung `reserve`. Still open -> `cause = blocked`.
- `corridor`: `pairs.plan(plan_only=True)` sections (`escreserve.pair_corridors`) for a pair; for a single net the
  `budget.mst_edges` flight lines; rects by `escreserve.seg_rects` at the derived width. `blockers` = movable parts whose
  courtyard or foreign pads cut the corridor on its layers (`placetour._box_parts`), smallest first; `routediag.diagnose`
  causes (`SEALED_PAD`, `KEEPOUT_TRAPPED`, `LONG_HAUL`, `CONGESTION`, `FAR_SUPPORT_PART`, `SUB_RULE_PAD`) attached.

**Judge key change** (`placetour.judge_key`, `KEY_NAMES`): prefix `(hs_open, rf_open, power_open)` before today's five.
**Verdict change** (`placetour.stage`): `fail` when `hs_open` or `rf_open` is not 0 after the ladder; the line names the nets.

| # | Rung | Reuse / add | Acceptance (on top of section 1) | Cap |
|---|---|---|---|---|
| 1 | `unblock` | add `placegaps._r_unblock`: per blocker, minimum-translation nudge out of the corridor (both normals), then rot 90/180/270; supports follow their IC (`intent.move_with_support`); up to 3 blockers in one candidate | judge: target net routed, no higher or equal class net lost | 4 candidates, 1 judge batch |
| 2 | `seal` | `breakout.repair` scoped to the endpoint part (`refs=`) when the cause is `SEALED_PAD`; `relieve` sealed-pin blockers | breakout gate of that part passes; judge as rung 1 | 1 batch |
| 3 | `reserve` | `lookahead.reservations`-shaped rects from the corridor, stored in the board's reserve sidecar (new file `<board>.reserve.json`, read by `floorplan2` claims, `placetour.gate_state` "keepout", and applied as rule areas by the `lookahead` stage); parts inside are moved out by rung 1 first | judge with the reservation applied: target routed; no part left in the claim | 2 candidates |
| 4 | `endpoint` | add `_r_endpoint`: the smaller movable endpoint module steps toward the other end along the corridor (25, 50, 100 % of the length over the pair budget's `length max`, or of the span when no budget); `placetour` "pull" + module pose (`modules.pose`) | as rung 1; the module's own rows (edge, away, supports) not worse | 3 candidates |
| 5 | `swap` | `planroute.swap_candidates`, `budget.swap_rows` (declared `[swap.group]` / polarity only) evaluated on the model | never applied: an owner question `swap:<group>` with the judged gain and "schematic change" as the consequence | 1 model run |
| 6 | `report` | none | the gap stays open with its diagnosis, blockers and every refusing check; stage verdict `fail` for hs / rf | - |

Narration: `placetour: USB pair leg ghost-unrouted (blocked by 2 parts in its corridor); trying unblock` (text built from the
gap, never a literal). Ledger row per gap. **Tests** (`tests/test_placegaps_ghost.py`, synthetic boards from
`tests/conftest.py` builders): class-ranked key orders an hs open above 5 plain opens; corridor blockers found and sorted;
`unblock` clears a corridor legally; a candidate that routes the target but opens another hs net is refused with
`judge:hs-pair open`; `reserve` sidecar round-trips and `gate_state` sees a part inside it; a swap is asked, not applied;
verdict `fail` with an hs open (judge stubbed by a fake `judge()`).
**W16 target**: `hs_open` 1 -> 0 (USB_HUB_DP), `rf_open` 1 -> 0 (BT_SW_RFC); signal ghost opens 9 -> <= 5; no row or
legality term worse; placetour <= 45 min; if not reached, each open net carries a cause and a refusing check.

## 4. Case 1: supports beyond their limit (`support-far`, `support-starved`)

**Detector** `detect_support_far`: the same rule as `intent.support_rows` (one source: move the per-support loop into
`clusters.far_supports(cl) -> [{ref, ic, pad, dist, limit, kind}]`, used by both): a graded support (`sure` or
`how == "spread"`, not waived, not in a `near` item) with `dist > limit`. One gap per support; `mag = dist - limit`;
`class` decap | other (`fluxmap._kind`). `detect_support_starved`: an IC whose ring (courtyard grown by the largest limit
of its far supports, on the sides allowed) has less free area (`Plan.raster` / `free_mask`) than the summed courtyards of
its far supports x the packing factor the seater already achieves on this board (measured, not a constant).

| # | Rung | Reuse / add | Acceptance | Cap |
|---|---|---|---|---|
| 1 | `home` | w20home `home_supports` / `_seat_near` (as is) | within limit, legal, crossings and cost not up | as w20home |
| 2 | `nudge` | w20home `_neighbours` + nudge to `NUDGE_MAX` | same | as w20home |
| 3 | `under` | w20home back-side seat under the pin (`M["flipped"]`, `flip.flip_file`) when `sides` allows (section 6) | same + via sites free on both sides | as w20home |
| 4 | `detach` | add: a far support that is a member of a multi-part module (skipped today: `len(members) != 1`) is rebuilt as its own module (`modules.build(unseat=)`), then rungs 1-3 | same; the module it left stays legal | 1 per support |
| 5 | `re-deal` | `clusters.pin_demand` + `clusters.rehome` (another pin of the same IC and net with unmet demand); `clusters._interchange` extended from bulk caps to any identical value / footprint / net parts | summed beyond-limit mm drops; every pin's demand still covered | 1 pass |
| 6 | `swap-seat` | add: exchange poses with a support of larger limit and enough slack standing inside the ring (w20home anneal swap, made deterministic: nearest first, chain depth <= 3) | both within their limits, legal | 8 candidates |
| 7 | `make-room` (`support-starved`) | add: the neighbour modules of the IC step out radially (minimum translation, `Plan.try_pose`), else the IC module re-seats (`floorplan2._grid`) at the nearest pose whose ring has the area; supports re-home (rung 1) in the same candidate | far count of that IC drops; no row, crossing-cost or legality term worse | 4 candidates per IC |
| 8 | `report` | none | gap stays, with the refusing check per rung; a limit that is `INF` (not a datasheet number) is listed as such; never relaxed here; "needs N mm2 within L mm of IC.pin" is handed to the floorplan grow decision | - |

Runs in `_one` after w20home's pass (rungs 1-3 become the first rungs of this ladder: `home_supports` is called by the
dispatcher, not beside it) and at both stage ends. In placetour `PlanEval` is used for these kinds, then one judge confirms
the batch. Narration: `floorplan: 83 support parts stand too far from their pins; moving them home (rung home)` ... `end: 9
left, 6 need room at one IC`. **Tests** (`tests/test_placegaps_support.py`): a support in a 3-part module is detached and
homed; a pin with two caps and a bare pin is re-dealt; swap-seat keeps both limits; make-room moves a neighbour, not a
fixed part; a walled-in support ends as `report` with `legal:overlap <ref> vs <ref>`; determinism (two runs, equal poses).
**W16 target**: decaps far 28 -> <= 4, other supports far 55 -> <= 10, none beyond 3 x its limit, every leftover with a
refusing check; wire length and crossings not above floorplan-3 (18808 / 2206); <= 120 s added to floorplan.

## 5. Case 3: hard rows not honoured (`row-*`)

**Detector** `detect_rows`: `ready.stage_rows` (budget table) + `intent.check`; a gap per row with `hard` and
(`verdict == "fail"` or `met is False`). Kind from the row: `support` + metric `template` + name `cin_side` ->
`row-cin-side`; other template keys -> `row-template`; metric `pin-bypass` -> `row-pin-bypass`; `separation`, intent `away`
/ `region` -> `row-away`; `keepout-flight` -> `row-flight`; `rail` -> `row-rail`; intent `edge` -> `row-edge` (ladder =
`intent.Seater`, existing). `mag = excess / limit`. `refs` = the row's refs, offender first (the movable one).

| Kind | Rungs (in order) | Reuse / add | Acceptance |
|---|---|---|---|
| `row-cin-side` | `template-turn`: re-arrange the regulator's unit with the template mirrored / turned (4 x 2 unit poses), input caps seated on the named pin-pair side; `unit-turn`: turn the whole module so that side faces free board; `clear-side`: the part standing in the pin-pair strip (span +- `CIN_TOL_MM`, depth = cap courtyard + clearance) steps out; `report` | `clusters.reseat(only=)`, `_on_cin_side`, `_off_template`, `modules.build`, `Plan.try_pose` | served pairs rises; `cin_loop_mm`, `sw_mm`, `fb_mm` rows of that IC not worse |
| `row-template` | `template-turn`, `clear-side`, `report` | `clusters.loop_metrics` | the measured loop <= limit or lower than before |
| `row-pin-bypass` | `re-deal` (section 4 rung 5), then the `support-far` ladder for the dealt cap | `clusters.pin_demand`, `rehome` | bare pins drop |
| `row-away` | `feasible` (diagnosis: board area minus the other group grown by `min`, minus fixed parts and keep-outs, must hold the offender's module; both sides fixed or no area -> `impossible`); `push-out`: offender module moves along the separating direction by `min - value + clearance`, 8 directions; `re-seat`: `_grid` seat with the grown rect as an extra keep-out (`Plan.set_keepouts`); `impossible`: owner question `row:<name>` with limit, best reachable value, the refusing part, options waive / lower `min_mm` / unfix the part / grow the board | `budget.separation_rows`, `intent.check(only_kinds=)`, `Plan.free_mask`, `questions.Session.ask` | row value >= min, or strictly closer to it with no other row worse |
| `row-flight` | `push-out` of the movable end until the flight line clears the keep-out; `report` (both ends fixed: layer change is the route stage's job) | `budget.flight_rows` | crossing gone |
| `row-rail` | `pull-load`: the smaller of the load and source modules steps toward the other until `reach` holds; `pour-need`: when it cannot (fixed, or an away row forbids), the required copper `W = Rs x L x I / dV` (`powerbudget`, PLAN-POWER-PREPLAN) becomes a reserved channel (section 3 rung 3 sidecar) and a row for the `railpour` rung of `close`; `report` | `budget.table` rail rows, `powerbudget`, reserve sidecar | row passes, or the pour need is planned and fits the reserved channel (row then reads "by pour", still listed) |

Each `impossible` / `report` writes one question or one ledger row, never both silently. Runs after `ghost-open`
hs/rf/power and before supports (a regulator turned for its input cap moves its supports). Narration: `floorplan: 9
datasheet rows are not met; working on the input capacitor side of 2 regulators`. **Tests** (`tests/test_placegaps_rows.py`):
row -> kind mapping for every row kind; cin_side fixed by template mirror on a synthetic buck unit; `push-out` meets a
separation; two fixed parts closer than `min` -> `impossible` question with the numbers and no move; `pull-load` stops at
an away row and emits `pour-need`; a fix that breaks another hard row is refused with `row:<kind>:<name>`.
**W16 target**: hard rows not honoured 9 -> <= 2, both with an open owner question; the two away rows (GNSS vs other RF,
module antenna vs metal) each end met or `impossible` with numbers; every cin_side row served.

## 6. Case 4: back side as a placement move

**Policy** (`[placement]`, existing keys): `sides = "front" | "both" | "both-light-back"`, `back_max_height_mm`,
back-closed areas = keep-outs with `side = "B" | "both"` (`legalise.keepout_rects` already carries sides).

**Height** `intent.height(B, cons, ref) -> (mm, source)` (extend the one function; first hit wins):
1. `[placement.heights]` (owner);
2. spec fact `height_mm` of the MPN (datasheet, page cited: partcite);
3. footprint property `Height` when present;
4. 3D model: `models.step_bbox` with the footprint's model rotate / offset, z max above the board surface; cached by model
   file hash in `<board>.fluxmap.json`; only for models that pass `model_verify` (a mis-seated model lies about z);
5. chip table (`intent._CHIP_H`);
6. unknown -> not eligible for the back while `back_max_height_mm` is set; one summary line and one question listing them.

**Eligible** `placegaps.back_ok(B, cons, spec, ref) -> (bool, why)`: SMD only; not fixed / locked / edge item / rigid group
member / mechanical; `front`: only a graded support with no legal front seat within its limit (today's behaviour);
`both`: any free part; `both-light-back`: height <= `back_max_height_mm` and not a heavy class (inductor, transformer,
connector, module, by `modules.SMALL` complement and courtyard area x height above the board's own median for ICs);
never: a part with an rf-class pad (stays on the layer its controlled line is planned on), an endpoint of an hs pair
unless the flip is judged (mirror reverses pin order), a regulator template unit (loop rows are same-side).

**Use**: `Plan` gains a side per module (`S`), `try_pose(i, X, Y, th, side=None)`; the model flip is `geom.flip`, legality
is already per side (claims, keep-outs, breakouts carry sides); a flipped part's pads claim a via site on both sides
(`breakout_claims`). `construct`: a module with no legal front seat tries the back when eligible. `anneal`: a flip move for
eligible single-part modules, share derived from the eligible fraction, cost `[placement.side] flip_cost` (existing) plus a
one-time second-side term so the first back part must earn the second assembly pass. Every `placegaps` rung may propose
`side` in `moves`; `under` (section 4 rung 3) is the first user. File write: `flip.flip_file` + `flip.verify` at the one
write point `_one` already has. **Gap kind** `side-illegal`: a part on the back that fails `back_ok`, is taller than the
limit, or stands in a back-closed area. Rungs: `flip-front` (front seat nearest), `move-out` (same side, out of the area),
`report`. **Assembly note** in `placegaps.md` and the stage line: back part count, distinct back MPNs, tallest back part
and its height source, parts of unknown height, "second side = second stencil and reflow pass" (fab profile text from
`fabcaps` when it has one; no price invented).
**Tests** (`tests/test_placegaps_side.py`): height source order and the cache; `back_ok` per policy; flip move keeps
`flip.verify`; a part over the height is flipped back; a back-closed area refuses with `legal:forbidden`; `sides = "front"`
gives byte-equal results to today (regression). **W16 target** (with the board's `sides` answer): 0 back parts over the
limit or in a closed area; decaps far <= 2; `PlanEval` flips verified by `flip.verify` 100 %; report states the count.

## 7. Case 5: parts gaps that block fab (`part-datasheet`, `part-stock`)

**Detector**: `partsgate.gate` needs per part: `datasheet` (no PDF in the datasheet dir that `dsfetch.validate_pdf` accepts
for the MPN) -> `part-datasheet`; `stock` (short at `[parts] quantity`, both primary distributors) -> `part-stock`.

| Kind | Rungs | Reuse / add | Acceptance |
|---|---|---|---|
| `part-datasheet` | `fetch` (`dsfetch.fetch_datasheet`: distributors, Nexar, element14, LCSC, manufacturer; 24 h negative cache respected); `variants` (`packaging_variants`, series level, flagged `series`); `mfr-pattern` (URL patterns per manufacturer from a data file under `fluxplace/data/`, not code); `adopt` (any PDF already in the datasheet dir or the project that validates for the MPN: `adopt_file`); `owner-action`: when every outcome is a block (403, captcha, script page), the parts-todo record gets `action = "browser"` with the best URL and the file name to save; the next run's `adopt` picks it up | `dsfetch`, `partdocs.cite` | PDF validates at part level (series level is accepted only as a flagged row), pinmap citation re-run |
| `part-stock` | `recheck` (live, both distributors); `packaging` (same manufacturer part in other packaging: same part, recorded, no question); `alternates` (`partsgate.alternatives` -> `dropin.classify`, top 3 verified against their datasheets; for parts with a pinmap `partdiff.diff`); `question`: `questions.Session.ask("part-stock:<mpn>", ...)` with options keep / each verified alternate / lower the quantity, the parameter diff (value, tolerance, voltage, dielectric, ESR, package / land pattern, height, temperature range, lifecycle) and the consequence built from the rows that cite the part (template and rail rows, back height, a land change = re-place) | `partsgate`, `partsearch`, `dropin`, `partdiff`, `questions` | never applied here: the answer goes through `parts-apply` (`partsgate.validate` / `apply`); default option = keep (build stays blocked, said so) |

Runs in `ready` (parts first). No placement stage starts while a `part-*` gap has neither a file nor an open owner item.
Network is behind the callables `partsgate` already injects (`searcher`, `verifier`, `stocker`); a network failure is an
attempt row with outcome, never a pass. **Tests** (`tests/test_placegaps_parts.py`, injected callables, no network): chain
order and negative cache; block outcomes -> `owner-action`; adopt after a manual save; alternates ranked, diff text shows
ESR / voltage / package; question raised with consequence, spec untouched; packaging variant makes no question.
**W16 target**: every part short of a datasheet ends with a validated file or a `browser` action naming one URL; every
short part has an open question with >= 1 verified alternate or "none found" with the searches tried; 0 substitutions made.
(Current counts: not measured in this design; phase 0 prints them.)

## 8. Risks

| Risk | Guard |
|---|---|
| The quick ghost judge is noisy: a net open once may route next time | the `confirm` diagnosis; an accept needs the target routed AND no class loss; memo on poses stops retry loops |
| Judge time (4 min each) | candidates capped per rung; batches run through `LA._run_jobs`; hs / rf ladders first, plain nets only with budget left |
| A repair for one kind breaks another (support homed into a separation zone) | R6 never-worse terms; `ORDER`; rows re-measured each round with roll-back of the round's accepts, newest first |
| `PlanEval` model differs from the file | every stage still ends in `legalgate.gate` (KiCad DRC against the input); a model accept the DRC refuses is a ledger row and a model fix item |
| w20home is uncommitted and rewrites `floorplan2` | phases 0-2 touch `placetour` / new files only; phase 3 starts from the merge that contains w20home |
| Reservations make the floorplan infeasible | soft cost in `Plan` for pair / plain, hard only for hs / rf / rail-pour; a reservation that parks a module is refused |
| Flip correctness (mirror, pcbnew helper) | `flip.verify` on every written flip; `sides = "front"` regression test byte-equal |
| KRT judge not bit-stable across runs | key compared with the class terms first; ties keep the base (no move without a strict gain) |
| Parts network flakiness | negative cache, injected callables in tests, attempts recorded |

## 9. Phases (one Sonnet agent each; W16 CURRENT board for the measured check; tests green before the next)

| # | Phase | Files | Done when |
|---|---|---|---|
| 0 | Core: gap shape, `DETECT` / `DISPATCH` / `RUNGS` registries, `close` loop, `_accept`, memo, report files, narration; detect-only on all stages | `placegaps.py` (new), call sites in `ready`, `floorplan2`, `placetour` | W16 gap table printed (all kinds, current counts incl. parts); zero board change; `tests/test_placegaps_core.py` |
| 1 | hs first: class-ranked judge key, verdict `fail` on hs / rf open, `ghost-open` detector + `confirm` + corridor / blockers | `placetour.py`, `placegaps.py` | W16: both critical nets reported with cause and blockers |
| 2 | `ghost-open` rungs `unblock`, `seal`, `endpoint` + `JudgeEval` | same | W16 `hs_open` 0, `rf_open` 0 or reported with refusing checks |
| 3 | `reserve` sidecar (write, floorplan claims, tier-1 gate, lookahead apply) + `swap` question | `placegaps.py`, `lookahead.py`, `floorplan2.py`, `questions.py` | reservation survives floorplan -> placetour -> lookahead on W16 |
| 4 | Supports: `clusters.far_supports`, `PlanEval`, rungs `detach`, `re-deal`, `swap-seat` over w20home's 1-3 | `clusters.py`, `floorplan2.py`, `placegaps.py` | W16 decaps far <= 8, others <= 20 |
| 5 | Supports: `support-starved` + `make-room`, grow hand-off, per-leftover refusing check | `placegaps.py`, `floorplan2.py` | section 4 target |
| 6 | Rows: detector + mapping, `row-away` / `row-flight` (`feasible`, `push-out`, `re-seat`, `impossible` question) | `placegaps.py`, `questions.py` | both away rows decided on W16 |
| 7 | Rows: `row-cin-side`, `row-template`, `row-pin-bypass` | `placegaps.py`, `clusters.py` | every cin_side row served |
| 8 | Rows: `row-rail` (`pull-load`, `pour-need`) | `placegaps.py`, `powerbudget.py` | section 5 target (<= 2, both asked) |
| 9 | Back side A: `intent.height` sources + cache, `back_ok`, `side-illegal`, assembly note | `intent.py`, `placegaps.py`, `models.py` (read only use) | heights sourced for every part on W16, unknowns listed |
| 10 | Back side B: `Plan` side state, flip in construct / anneal / rungs, write + verify | `floorplan2.py`, `placegaps.py` | section 6 target; `front` regression byte-equal |
| 11 | Parts A: `part-datasheet` ladder + `owner-action` | `placegaps.py`, `dsfetch.py`, `fluxplace/data/` | section 7 datasheet target |
| 12 | Parts B: `part-stock` alternates, diff, question | `placegaps.py`, `partsgate.py`, `questions.py` | section 7 stock target |

Each phase: ONE-SHOT-LEDGER row for any fail / bypass, DEV-IMPROVEMENTS item for a lesson, tests under `tests/`, the W16
number before and after in the PR text. Long judge runs are detached scripts (`W*/scripts/run-*.sh`), never inside the agent.

## 10. What NOT to do

- No new command, sub-command or flag; no per-fix switch. A new repair is a rung in `DISPATCH`.
- No reference, net name, coordinate or distance of any board in code or tests' production paths; W16 names appear only
  in this plan's targets and in run notes.
- No accept without the legality check and the never-worse terms; no "listed exception" for an hs pair or rf net.
- No relaxing a row, a support limit or a clearance inside a rung; an impossible row is an owner question with numbers.
- No pin swap, part substitution or spec edit applied by the dispatcher; these are questions, applied by the existing
  owner-answer paths.
- No board growth or layer change inside a rung (stage decision; the rung reports the area or channel it needs).
- No refactor of `gaps.close` and no second placer: rungs propose `moves`, the two evaluators that exist judge them.
- No flip of a fixed, edge, through-hole or rf part; no back-side part of unknown height when a limit is set.
- No trace thinning to make a ghost net route; no judge result trusted from a single quick run for a refusal of a row.
- No wall-clock decision other than the outer stop, and that stop is always named in the stage line.
