# PLAN: power planned before placement (D71dc parts 2 and 3)

2026-11-23, Fable design. Design only; every step in section 6 is a recipe for Sonnet/Haiku. Code base read: `w16merge`
(0cb77f3), `w16neck` (8b69a32: DC drop over zones, `[[dcdrop]] from/to`), `w16reloc` (4457159: close `relocate` rung),
`w16vrange` (part 1, `elec.check_supply_range`). Rules: D71da/db/dc, LESSONS-W15 rules 1-3 and 5, one-flow rule,
REFACTOR-PLAN-W16 section 2 (Budget row shape).

## 0. What is wrong today (measured on W15, cause in code)

| W15 finding | Cause in code |
|---|---|
| +5V (4.2 A) ended on a 0.15 mm In3 track | `pour = true` rails are "pads leave by via" in `escapeplan` and take no capacity in `globalroute`; their copper is made after routing (`power.pour_rails` grows the routed spine, pipeline.py:705). Nothing reserves it. |
| Decoupling 36-68 mm from the pin | `clusters.ownership`: a rail-only cap gets `how = "spread"` (a guess) or `"bulk"`, and `far_parts` never grades those. `modules.build` pass 3 then attaches it to the nearest module of the *current* placement. |
| Source to load distance never scored | `floorplan2.Problem` drops plane/pour nets from wire length (`if net in self.plane: continue`); PLAN_SCORE weighs rails 0.25 per mm, with no drop term. |
| Two support limits | `clusters.LIMIT_DEFAULT` 5.0 and `routediag.FAR_MM` / `FAR_TIGHT_MM` 5.0 / 2.5 (LESSONS rule 2). |
| Fan-out rail stubs at signal width, locked | REVIEW-W16FIN: about 100 mm of +3V3 neck-downs. |

Physics that drives the design (all layers 35 um on W15, sheet resistance 0.491 mohm/sq at 20 C):

| Rail | I, budget max / target | Allowed R | Squares | IPC width outer / inner | Reach at 0.15 mm (max) |
|---|---|---|---|---|---|
| +5V | 4.2 A, 150 / 75 mV | 35.7 / 17.9 mohm | 72.7 / 36.3 | 2.2 / 5.65 mm | 10.9 mm of copper |
| VMODEM | 3.0 A, 300 / 100 mV | 100 / 33.3 mohm | 203 / 67.8 | 1.4 / 3.6 mm (Quectel: >= 3 mm) | 30.5 mm |
| +3V3 | 0.9 A, 150 / 36 mV | 167 / 40 mohm | 339 / 81.4 | 0.26 / 0.68 mm | 50.9 mm |

The drop budget is a budget in **squares**. Distance and width trade one for one: `W_req = Rs * L * I / dV`. So the plan
does not fix a distance limit first; it fixes the copper each distance needs, and the placer pays for that copper.

## 1. Data model: `fluxplace/powerbudget.py` (new, the one power budget)

One module, no second definition anywhere. It computes nothing that exists already: widths and vias come from
`power.rail`, resistance from `channel.dc_resistance_mohm`, tiers from `channel.verdict`, copper thickness from
`channel.copper_t_mm`, ownership from `clusters.ownership`. Row shape follows REFACTOR-PLAN-W16 section 2
(`{metric, target, max, unit, source, klass REQ|REC|INF}`).

```
build(cons, B, spec=None, rows=None, facts=None) -> {"rails": {net: Rail}, "supports": [Support], "findings": [...]}

Rail = {
  net, current_a, sizing,            # sizing = power.rail(cons, net) unchanged (IPC width outer/inner, vias_needed, pour)
  drop: {target_mv, max_mv, source, klass} | None,        # the [[dcdrop]] row
  r_allow_mohm: {target, max},                            # drop / current_a
  source: {spec, pads, how},         # how: "dcdrop.from" | "[power].source" | "fact i_out_max_a" | "unknown"
  loads:  [{spec, pads, how}],       # how: "dcdrop.to" | "inferred"
  copper: [{layer, kind, width_ipc_mm, mohm_sq, vias_end, r_vias_mohm, reach_mm: {target, max}}],   # one per candidate layer
  choice: {kind: "plane" | "corridor" | "track", layers, width_mm, why},
  edges:  [{load, dist_mm, len_mm, width_req_mm: {target, max}, est: <dcdrop row>}]   # needs a placement; [] without
}
Support = {ref, role, serves: "REF.PAD", net, limit: {target, max, unit: "mm", source, klass}, dist_mm, verdict, how}
```

Functions (each the single definition of its quantity):

| Function | Does | Built on |
|---|---|---|
| `sheet_mohm_sq(rows, layer, temp_c)` | rho(T) / t | `channel.RHO_CU_20C`, `ALPHA_CU`, `copper_t_mm` |
| `synth_path(layer, len_mm, width_mm, vias)` | a planned path in the shape a measured path has: `{"layer_sq": {layer: len/width}, "via_list": [...]}` | - |
| `channel.dcdrop_row(d, path, rows)` | **moved out of the loop body of w16neck `channel.dcdrop_rows`**: one `[[dcdrop]]` entry + one path -> row with metric, verdict, `r_mohm` | `dc_resistance_mohm`, `_metric` |
| `estimate(rail, a_xy, b_xy, layer, width_mm)` | `dcdrop_row(d, synth_path(layer, DETOUR * manhattan, width, vias), rows)` | `channel.DETOUR` (1.15) |
| `reach_mm(rail, layer, width_mm, tier)` | `(R_allow - R_vias) * width / mohm_sq / DETOUR` | inverse of `estimate` |
| `width_req_mm(rail, dist_mm, layer, tier)` | `max(IPC width on layer, mohm_sq * DETOUR * dist / (R_allow - R_vias), fab floor)` | `power.rail`, `ready` floor |
| `resolve_ends(B, cons, net)` | source and load pad groups | `channel._group` label rule (`"REF"` / `"REF.PAD"`) |
| `supports(B, cons, spec, facts)` | the one support table | `clusters.ownership`, `netclass` budgets, part facts |
| `corridor_nets(B, cons, budget)` | rails the global router must carry as wide nets (section 4) | - |

LESSONS rule 5 is met by construction: planned and measured drops go through the same `channel.dcdrop_row`; only the
path differs (`synth_path` before copper exists, `measure_src_load` after). Step P1 has an identity test for it.

**Source and loads.** `[[dcdrop]] from/to` (w16neck) wins. Else `[power.NET] source = "REF"` (already in W15 constraints).
If the source part has no pad on the net (a buck regulator behind its inductor: W15 U102 has no +3V3 pad, U100 reaches
+5V through L100), the source group is the net pad of the two-pad part whose other pad's net carries a pin of the source.
Else the part with an `i_out_max_a` fact on the net. Else `unknown` (ready question). Loads without `to`: every part on
the net with more than two pads, except the source. Current per load: the full `current_a` (same conservative rule as
w16neck; see owner question 1).

**Support parts.** Role and limit (target warns, max fails, D71cv tiers):

| Role | Detected by (existing code) | Target / max | Source of the default |
|---|---|---|---|
| regulator loop (input cap, boot cap, SW node) | `netclass` class `switch_node`, `clusters.reg_rank` | class budget (6 mm) / same | `netclass.BUDGET`: about 1 nH/mm, 6 mm is about 5 nH in the switching loop (INF) |
| bypass | cap <= `clusters.BULK_UF` (4.7 uF), rail to ground, at an IC supply pin | 3 / 5 mm | target `netclass.BUDGET["bypass"]` (3 mm is about 2.5 nH, cap SRF stays above 100 MHz, INF); max = today's `clusters.LIMIT_DEFAULT`, so the existing gate does not move |
| bulk | cap > `BULK_UF` on a planned rail | 10 / 15 mm | INF, unverified engineering default: path inductance at pour width (about 0.3-0.5 nH/mm) kept below the ESL of a polymer/tantalum or 1210 MLCC bulk part (2-5 nH). A datasheet figure replaces it. |
| sense (feedback divider, sense resistor) | support part on a net of exactly one IC that is not a rail | 5 / 5 mm | `clusters.LIMIT_DEFAULT` (unchanged) |

Datasheet values override the defaults through the existing fact mechanism of part 1 (`elec.FACT_KEYS`, `parts-fact MPN
key=value --source file.pdf#pN`): new keys `decap_mm` and `bulk_mm`, a number or `{pin: mm}` (same shape rule as
`vin_min`), klass REQ when the datasheet states a number, REC when it only says "as close as possible" (then the default
number with the page cited). No new command.

**Ownership is decided at `ready`, never guessed silently.** `supports()` keeps `clusters.ownership` and then resolves
every `how = "spread"` / `"bulk"` part of a planned rail in this order: (1) `[power."NET"] support = {C117 = "J200.70"}`
(explicit); (2) same functional block (`blocks.of_board`) as exactly one source or load of the rail: bulk parts serve that
group's highest-current pin; (3) the existing refdes-block rule; (4) the existing even spread, turned into a `ready`
question whose default answer is the spread result. After `ready`, every support of a planned rail has `sure = True` and
is graded. Constraints carry references; code carries none (board-agnostic).

## 2. Where it plugs in (one flow, no new command or flag)

| Stage | Function touched | Change |
|---|---|---|
| ready | `ready.evaluate` (new `_line("power plan", ...)`), `questions.check_power_plan` beside `check_rail_pour` | Build the budget; table to `power-plan.json` and the ready report; findings of section 5. Part 1 (`elec`) already runs here. |
| modules | `modules.build`, `clusters.build` / `far_parts` / `support_limit` | Limits and `sure` come from `powerbudget.supports`. A support with a budget owner joins its owner's module (pass 2), so `clusters.reseat` seats it at the pin and it moves rigidly with the IC. Pass 3 ("nearest module of the current placement") keeps only parts with no budget owner. |
| floorplan | `floorplan2.Problem.__init__`, `cost_parts`, `total`, `metrics` | Power term (section 3). Report: one row per rail edge. |
| fanout | `fanout_helper` rail stubs | Stub width `min(pad width, rail need)`, via count `vias_needed` for budget rails (REVIEW-W16FIN root cause). |
| pour-tie | `pipeline.st_pour_tie` -> `power.replan` (new, wraps existing pieces) | Corridor zones from the global router's rail paths (section 4), pads tied (`pourtie.run`), then the **measured** drop on the filled zones (`channel.dcdrop_rows`, w16neck). First real measurement, before any signal copper. Gate kind `power-drop`. |
| placetour | `placetour.gate_state`, `candidates`, `stage` | Gate member set `power`; budget blame feeds the existing `pull` kind; `power.replan` once per accepted round. |
| lookahead | `lookahead.stage` entry | Asserts the corridor fingerprint matches the placement, else `power.replan`. Corridors then count as locked copper; pair channels are reserved around them. |
| pairs, tune, critroute | none | Corridor zones are ordinary zones (obstacles for other nets). |
| plan-route | `globalroute.GlobalRoute`, `planroute.run` / `score_of` / `HARD_CODES` | Rails are wide nets with capacity demand; `PLAN_POWER_REACH`, `PLAN_POWER_CORRIDOR` (section 4, 5). Every `planroute.run` caller (floorplan report, `planplace`, placetour tier 1) gets them with no extra code. |
| route | existing rails step and `power.pour_rails` | Final rail outline = planned corridor united with the routed spine; the DRC guard is unchanged. |
| close | `relocate.suggest`, `routediag._far`, `powerwiden`, `railpour` | Limits and widths from the budget. `routediag.FAR_MM` / `FAR_TIGHT_MM` are deleted. `relocate` (w16reloc) keeps its interface: it reads `routediag` moves. |
| review | `power.check`, `channel.check`, `preroute.support_far` | Unchanged proof (D71da). New INFO `POWER_PLAN_DRIFT`: planned vs measured per rail. |

`power.replan(board, cons, out)` is one helper, not a stage: strip `fluxplace-rail:` zones (`_strip_rail_zones`), route
the corridor nets (`planroute.run`), write outlines (`rail_outline` + `pour_rails`), tie (`pourtie.repair`), measure
(`channel.dcdrop_rows`). Corridor zones are **derived copper**: named with `power.RAIL_ZONE`, never locked before
lookahead, always regenerated from the placement, never edited by hand.

## 3. Placer scoring

### floorplan (`floorplan2.Problem`)

Per rail edge e (source group to load group; module-relative points, built like the existing port arrays):

```
d_e      = |dx| + |dy|                                  between the two group centroids (world frame)
W_e(d)   = powerbudget.width_req_mm(rail, d, layer, "target")      = max(IPC width, Rs * DETOUR * d * I / dV_target)
A_e      = W_e(d) * DETOUR * d                           mm2 of copper the rail takes from routing
power      = sum_e A_e / pitch                           pitch = default track + clearance (routerules)
power_over = sum_e (W_cap / pitch) * max(0, d_e - reach_mm(rail, layer, W_cap, "max"))
total     += w_power * power + 4.0 * power_over
```

- **Unit.** `A / pitch` is the length of signal track the corridor displaces, so the term is in the same millimetres as
  `wl` with signal weight 1. `w_power` (`[floorplan] w_power`, default 1.0) is a scale, not a tuning need.
- **Over term.** Same factor 4 the net budget `over` has. `W_cap` = `[route] power_corridor_max_mm` (default
  4 x `POUR_OVER_MM` = 8 mm). A residual `power_over > 0` after the search is a floorplan FAIL (`_residual_fail`).
- **Plane-fed rail** (a board-wide zone of the net, or `[[plan.pour]] region = "board"`): `A_e = 0` (the plane exists
  anyway); only the over term, with the plane's sheet reach. On W15 this is +3V3 on In2.
- **Layer for the estimate:** the rail's `choice` layer from `ready` (section 4 rules without congestion).
- **Trade against existing terms.** Tiers 1-2 (LEGAL, FAN-OUT) stay lexicographically above. Inside the weighted sum
  a high-current edge costs like the tracks it displaces. W15 check: +5V U100 to J10 at 60.7 mm gives W = 2.2 mm,
  A = 154 mm2, 554 mm equivalent, i.e. about 8 signal tracks over 70 mm; halving the distance saves 277. A pair leg costs
  its class weight per mm and 4 x that per mm over budget, so a pair over budget still outranks a rail inside reach. Rails over reach (factor 4 x
  29 tracks per mm at W_cap) outrank everything but pairs over budget. Supports need no term: they ride in the module.
- **Determinism.** Pure function of (X, Y, T); edges sorted by (net, load spec); numpy sums in fixed order; the
  annealer's seed and iteration budget are unchanged. Test: two runs, same seed, byte-identical output.

### planplace (PLAN_SCORE) and placetour

- PLAN_SCORE gets its power terms from the global router (section 4): `w_power_over` 20 per mm over reach (the weight
  hard classes have), `PLAN_POWER_CORRIDOR` 200 (the weight of an unreached connection). The rails' 0.25 per mm copper
  term is replaced by `A / pitch` for corridor rails. `PlanPlacer.sup_lim` needs no change: it reads `sure` supports.
- `placetour.gate_state` adds `"power": frozenset(...)` of budget rows over max (`reach:<net>:<load>`,
  `support:<ref>`). `worse_gates` already rejects a candidate that adds a member. No change to `judge_key` (the router
  judges); the judge copy gets its corridors from `power.replan` in the judge worker (seconds against a 4 minute judge).
- `candidates`: budget rows over target join the blame as `costly`-like entries for the existing `pull` kind (support to
  its served pad; a movable load or source toward the other end). No new kind.

## 4. Route planning: power copper before signals

**Model.** A rail with a budget and no board-wide plane is a **corridor net** in `globalroute`: routed like any net, but
each cell it crosses takes `ceil((W + clearance) / pitch)` tracks of capacity, `W = width_req_mm(rail, routed length,
layer, "target")`. This replaces "pour rails leave by via and cost nothing". `_pour_factors` (`[[plan.pour]]`) stays for
owner-declared pours.

**Order (quality order kept).** Inside one `route_all`: gated pairs first (as now), corridor nets second with the pair
paths as fixed usage, in descending current then net name, then every other net. Power yields to pairs; signals yield
to power. This is the same pattern lookahead uses for pairs: demand first, reservation, then the rest.

**Layer choice** (`powerbudget.copper` + `choice`, board-agnostic: layer roles come from the stackup rows and
`krtchain.plane_plan`, no layer names):

1. A board-wide plane or zone of the net exists: use it; estimate with the sheet model; no corridor.
2. Outer layer that carries both pad groups: no vias, IPC outer width (about 2.6 x narrower than inner for the same
   current), chosen when the router finds the corridor with no overflow.
3. Inner signal layer: `vias_needed` at each end (their barrel resistance is in the estimate), IPC inner width.
4. Two layers in parallel, stitched, when one layer cannot meet the target inside `W_cap`.
5. None fits: `PLAN_POWER_CORRIDOR`, with the what-if in the message (width needed, the nearer placement that would fit,
   or a plane layer). Never thinned to fit (owner rule: routing margin over fab minimum).

**Facing layers.** Two signal layers with no plane between them face each other (W15: In2 and In3, 0.175 mm apart; the
planes are 0.43 mm away). A corridor on one becomes the nearest reference of a track on the other. Rule: a corridor
cell sets the capacity of the facing-layer cell to 0 for controlled nets (pair, rf, clock); a gated pair path forbids the
facing cell for corridors. `planrules.return_path` stays the check. For Sig/GND/Sig/Sig/GND/Sig this means high-current
rails go outer first; In2/In3 corridors cost pair capacity on the other inner layer and the router sees that cost.

**Materialising.** `power.replan`: the corridor net's cell path becomes centre-line segments; `power.rail_outline(segs,
W / 2)` (the function the post-route pour already uses) gives the zone outline; `pour_rails` writes it with its DRC
guard; `pourtie` ties the pads; `channel.dcdrop_rows` measures source to load on the filled zone. The predictor (router
path, `estimate`) and the executor (zone, measured drop) use the same outline function and the same drop function.

**After signals are routed** the existing rails step closes what the corridor did not (at `power.route_widths`), and
`pour_rails` unites spine and corridor. `powerwiden` / `railpour` stay as close rungs for the remainder.

## 5. Gates and reports

| Where | Code | Level | Condition |
|---|---|---|---|
| ready | `SUPPLY_RANGE` | FAIL | part 1 (w16vrange) |
| ready | `POWER_PLAN_NO_BUDGET` | OPEN (blocks) | a rail whose IPC width exceeds the default track has no `[[dcdrop]]` row (no allowed resistance, D71da gives it no exemption later). Narrower rails: WARN. |
| ready | `POWER_SOURCE_UNKNOWN` | OPEN | no source resolved for a planned rail, or `from`/`to` names a part with no pad on the net |
| ready | `POWER_SUPPORT_OWNER` | OPEN, default = spread result | a rail-only support whose owner is still a guess |
| ready | `POWER_COPPER_IMPOSSIBLE` | FAIL | the via barrels alone exceed the allowed R, or no candidate layer holds the IPC width inside `W_cap` |
| modules / preroute gate | `SUPPORT_PART_FAR` | FAIL over max, INFO over target | now includes bulk and decided rail supports |
| floorplan | `POWER_REACH` | FAIL | `power_over > 0` after the search (edge, distance, reach, width needed) |
| pour-tie | gate kind `power-drop` | FAIL over max, INFO over target | measured drop on the corridor zones; message carries the top `contrib` squares (w16neck) |
| placetour | gate `power` | candidate dropped | a new over-max member |
| plan-route | `PLAN_POWER_REACH`, `PLAN_POWER_CORRIDOR` | hard codes (`is_hard_failure`) | routed corridor length over reach; no path at width |
| route / close | `power-width`, `power-vias` (existing) | as now | - |
| review | `POWER_WIDTH`, `POWER_VIAS`, `CHANNEL_MAX` (dcdrop), `SUPPORT_PART_FAR` | FAIL | final proof, unchanged (D71da) |
| review | `POWER_PLAN_DRIFT` | INFO | planned vs measured drop and length per rail (feeds the ledger; calibrates `DETOUR`) |

Report: `power-plan.json` + a table in the ready report and the stage line: per rail source, loads, I, allowed R, chosen
copper, reach at target/max, per edge distance and estimated drop; per support role, served pin, limit with source,
distance. Fail-fast (LESSONS rule 7): every FAIL names the number, the limit and what to change.

## 6. Implementation steps

Precondition (lead): `w16neck`, `w16vrange` and `w16reloc` merged into the work branch. Tests are `pytest tests/<file>`;
the full suite runs after each step. Synthetic board for all steps: extend `tests/test_floorplan.py::board()` into
`tests/fixtures/powerboard.py`: 40 x 30 mm, 4 copper layers (Sig/GND/Sig/Sig style rows, 35 um), regulator U1 (8 pads)
with inductor L1 on net VOUT, connector J1 (10 pads, 4 on VOUT), IC U2 (16 pads, 2 on VOUT), C1 100 nF at U2, C2 22 uF
rail-only, 6 signal nets; `[power.VOUT] max_current_ma = 3000, source = "U1"`, `[[dcdrop]] net = "VOUT", current_a = 3,
drop_mv_max = 100, drop_mv_target = 50, from = "L1", to = ["J1", "U2"]`.

| # | Step | Model, size | Files: functions | Acceptance tests |
|---|---|---|---|---|
| P1 | Budget core | Sonnet, M | `channel.py`: extract `dcdrop_row(d, path, rows)` from `dcdrop_rows` (no behaviour change). New `powerbudget.py`: `sheet_mohm_sq`, `synth_path`, `estimate`, `reach_mm`, `width_req_mm`, `resolve_ends` (incl. source behind a series part), `build` (rails only) | `tests/test_powerbudget.py`: 10 mm x 1 mm on 35 um = 10 sq = 4.91 mohm; `reach_mm` is the inverse of `estimate` within 1e-6; table of section 0 reproduced (+5V 72.7 sq, 10.9 mm at 0.15 mm); **identity**: a routed fixture path fed as `synth_path` per `contrib` row gives the same mV as `dcdrop_rows`; U1-behind-L1 resolves to L1's VOUT pad; `test_channel.py` unchanged and green |
| P2 | Fact keys and schema | Haiku, S | `elec.FACT_KEYS` + `decap_mm`, `bulk_mm` (reuse the per-pin helper); `constraints.py` key table: `power.*.support`, `route.power_corridor_max_mm`, `floorplan.w_power`; `docs/commands/` text | `test_elec.py`: `parts-fact` stores and reads both keys with source; unknown key still refused |
| P3 | One support table | Sonnet, M | `powerbudget.supports`; `clusters.build`/`support_limit`/`far_parts` read it; `routediag._far` takes limit from it, delete `FAR_MM`, `FAR_TIGHT_MM`; `preroute.support_far` message adds role and source | `test_clusters.py`: C2 (22 uF rail-only) is graded with role bulk, 15 mm max; C1 bypass 3/5 mm; a `decap_mm` fact overrides with klass REQ; explicit `[power.VOUT] support` wins; grep: no `FAR_MM` left; existing cluster tests green |
| P4 | Ready line, report, questions | Haiku, S | `ready.evaluate` line "power plan"; `questions.check_power_plan` (source, owner, no-budget); writer for `power-plan.json` | `test_ready.py`: rail without `[[dcdrop]]` -> OPEN `POWER_PLAN_NO_BUDGET`; unknown source -> OPEN; fixture as given -> PASS and the json holds reach and width rows |
| P5 | Supports join their owner's module | Sonnet, S | `modules.build` pass 2/3 | fixture with C2 placed 30 mm away: after `modules` it is in its owner's module and within 15 mm; a part with no budget owner behaves as before |
| P6 | Floorplan power term | Sonnet, M | `floorplan2.Problem.__init__` (edge arrays), `cost_parts` (`power`, `power_over`), `total`, `metrics` rows | `test_floorplan.py`: L1 to J1 distance with `w_power = 1` < with `w_power = 0` (same seed); two runs byte-identical; J1 fixed beyond reach -> `POWER_REACH` FAIL with numbers; a plane-fed rail adds 0 to `power`; legality and fan-out tiers untouched |
| P7 | Corridor nets in the global router | Sonnet, L, **Fable review before merge** | `globalroute.GlobalRoute.build_nets` / `_mark` / `route_all` (per-net track demand, order pairs > corridors > rest, facing-layer rule), `globalroute.findings` + `planroute.score_of` / `HARD_CODES` (`PLAN_POWER_REACH`, `PLAN_POWER_CORRIDOR`), `powerbudget.corridor_nets` and layer choice | `test_planroute.py`: VOUT at 3 A takes `ceil((W + c) / pitch)` tracks in every path cell; a signal net that used the channel is rerouted or overflows; a pair fixture keeps its path and the corridor moves; walled fixture -> `PLAN_POWER_CORRIDOR`; facing-layer cell capacity 0 for a pair; boards without `[power]` give identical PLAN_SCORE to before (golden) |
| P8 | Corridor copper and the first measurement | Sonnet, M | `power.replan` (strip, route, `rail_outline`, `pour_rails(outline=...)`, `pourtie.repair`, `channel.dcdrop_rows`); `pipeline.st_pour_tie` calls it; gate kind `power-drop`; `placetour.stage` after an accepted round and in the judge worker; `lookahead.stage` entry fingerprint | `test_power_lay.py` (pcbnew): fixture gets one `fluxplace-rail:VOUT` zone, every VOUT pad tied, measured drop <= target and within 25 % of `estimate`; second call is idempotent (same zone count); moving J1 and calling again moves the zone; DRC guard refusal leaves the board unchanged |
| P9 | Fan-out rail stubs | Haiku, S | `fanout_helper` stub width and via count for budget rails | fixture: VOUT stubs at `min(pad width, need)`, `vias_needed` vias; signal stubs unchanged |
| P10 | Placetour gate and blame | Haiku, S | `placetour.gate_state` (`power`), `candidates` (budget rows into `pull`) | `test_placetour.py`: a move that puts C1 past 5 mm is dropped "worse: power"; an over-target support yields a `pull` candidate toward its pin |
| P11 | Review drift row, close limits | Haiku, S | `review.run` (`POWER_PLAN_DRIFT`), `relocate.suggest` limit from the support row, `gaps._r_powerwiden` width from the budget | golden review on the fixture: one INFO row per rail, no new FAIL; `test_relocate.py` green |
| P12 | W15 dry check and W15 facts | Haiku, S (read-only) | no code: run `powerbudget.build` on a **copy** of W15 CURRENT with W15 constraints; add `decap_mm` / `bulk_mm` facts for LM61460, TPS628502, RM520N-GL (M.2 VCC), CM5 from `docs/datasheets/` with pages | table below filled in; ledger row |

Order: P1, P2, P3, P4 (plan visible at ready), then P12 (dry check, before any placement code), then P5, P6, P9, P10,
then P7, P8, P11. P1-P4 + P12 touch no stage behaviour and can land while the W15 close run uses `w16merge`.

**W15 dry check (P12), pass criteria.** Columns: Manhattan distance, estimated length (x 1.15), routed length (sum of
`contrib` mm), estimated drop on planned copper, drop re-computed from the routed `contrib` rows, measured drop, verdict.

| Check | Expected |
|---|---|
| Identity | re-computed from `contrib` = measured within 0.1 mV on every `[[dcdrop]]` row (same code) |
| +5V, L100/U100 to J10 | 60.7 mm Manhattan (measured on CURRENT by this design pass), estimate 69.8 mm at 2.2 mm outer = 15.6 mohm = 65 mV (about 10 mV inside the 75 mV target: the plan must flag the edge as tight); measured row must show the In3 0.15 mm run as the top `contrib` |
| VMODEM, U101/L101 to J200 | 26.3 mm, estimate 30.2 mm at 3 mm = 4.9 mohm = 15 mV |
| +3V3 | plane-fed (In2); as a 0.3 mm track it would reach 21 mm at target, 88 mm at max |
| Supports | C117, C116 (VMODEM), C103, C419, C113 (VBATT_PROT) reported over max with role and served pin; C20, C21 (22 uF on +5V, 95-103 mm from U100) get an owner decision instead of "spread" |
| VBATT_IN, VBATT_PROT | `POWER_PLAN_NO_BUDGET` (5 A, no `[[dcdrop]]` row) |
| Length model | routed / estimated length reported per edge; outside 0.8-1.5 is a ledger row, not a failure |
| Time | budget build under 30 s, no router call |

## 7. Risks and questions for the owner

1. **Per-load current.** Plan and review charge the full rail current to every load (w16neck rule; +3V3 carries 0.9 A to
   each of its loads on paper). Safe but it over-sizes branch copper. Allow per-load currents (`to_a`, or the `i_max_ma`
   part facts) in both the plan and the review check?
2. **Support defaults.** Bypass 3 / 5 mm and regulator loop 6 mm come from existing code; bulk 10 / 15 mm is a new,
   unverified default. Accept until datasheet facts exist, and should a part with no datasheet figure warn (proposed)
   or block?
3. **No power plane in Sig/GND/Sig/Sig/GND/Sig.** High-current rails go on the outer layers first; an In2/In3 corridor
   removes pair capacity on the facing inner layer. Accept that, or do you want the what-if for a stackup with a power
   plane reported each time a corridor fails?
4. **Ready blocks on missing drop budgets.** Proposed: any rail wider than a signal track with no `[[dcdrop]]` row keeps
   `ready` OPEN. On W15 that is VBATT_IN and VBATT_PROT (5 A). Block, or warn only?
5. **W15 itself.** The dry check will list what the plan would have refused. Re-place W15 from the plan (new board
   version, pairs re-laid), or keep W15 on the `relocate` rung and apply the plan from the next board on?

Technical risks (lead): P7 changes PLAN_SCORE for boards with `[power]` (golden test pins the no-`[power]` case; expect
placetour and floorplan rankings to shift); the zone drop model of w16neck is an estimate of a 2-D sheet, so the
pour-tie gate uses max as FAIL and target as INFO only; corridor zones regenerated after placetour moves must never
touch locked pair copper (covered by the `pour_rails` DRC guard and the manifest protect list).
