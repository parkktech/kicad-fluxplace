# Refactor plan W16: one measure, one budget, protected work, one flow

Read-only audit of `w16stab` (read at 1e9dc2c + merge c37ffba of `w16chan`; HEAD moved to 4eb063b during the audit), 2026-11-22. Evidence: LESSONS-W15 (L1-L12),
IMPROVEMENTS-W15, SKEW-LIMITS-RESEARCH, CHANNEL-BUDGETS-RESEARCH, DECISIONS D71cm..cw, EFFICIENCY-LOG 11-21/22,
ONE-SHOT-LEDGER, FLOW-REDESIGN (section 6 = the one-flow rule). Nothing was run on a board; probes were read-only.
Paths are relative to `fluxplace/` unless they start with `cli.py`, `tests/` or `docs/`.

## 0. Findings that drive the plan

1. **The skew verdict is still own math.** `pairaudit.audit` (309) picks its number in this order: `channel_paths`
   (own pcbnew graph walk, `channel.measure_paths`) > `multi_paths` (own text walk) > KiCad (`netlen.pair_lengths`) >
   `profile` (own text walk). KiCad is a stderr warning (`_cross_check`, 208), not the authority. 1f67e37 (landed during this
   audit) states that choice in its title; it conflicts with L1 and needs the decision in 1.1. An env switch (`FLUXPLACE_AUDIT_OWN_LENGTHS`) turns KiCad off.
2. **Seven length definitions, three budget resolvers, ten family matchers, 22 DRC call sites.** Tables in 1 and 2.
3. **`channel.predict` has no caller** (only `tests/test_channel.py:226`). The placer never sees a budget (L10).
4. **`constraints.inject_netclasses` is called only from `fab.py:474`.** Netclasses reach the project at fab time, after
   routing. That is the mechanism behind L6 (90 ohm pairs at 0.2 mm for two board versions).
5. **Protection is opt-in.** `drcguard.accept(..., locked_ok=None, protect=())`: both guards default to off. Its callers
   are `gaps._accept`, `handroute` (1390, 1533) and `tune` (via `guard_board`). `pairs.run`, `lookahead` adopt,
   `placetour._apply`, `krtchain` and the route stage write boards without it. Lock tests are spread over 40 modules.
6. **`tests/test_surface.py` does not exist.** One-flow rules 3, 8 and 13 name it as their enforcement. No `build` command exists.
7. **Two flows** (`pipeline.FLOWS`: classic, constructive), 65 `add_parser` calls in `cli.py`, 526 `add_argument` in
   `cli.py` alone. W14/W15 run scripts use four commands: `pipeline`, `close`, `lay`, `pairs --only`.
8. **KiCad access here:** `kicad-cli pcb drc` with a generated `.kicad_dru` works (netlen proves it, about 2 s).
   pcbnew 10.0.5 SWIG: `BOARD.GetTrackLength` and `GetLengthCalculation` return opaque SWIG objects (probed on W15
   CURRENT), so no length API in Python. `CONNECTIVITY_DATA` (`GetUnconnectedCount`, `GetRatsnestForNet`) is usable.
   IPC API: `kipy` not installed; ledger 2026-11-20 decided not built (no DRC, GUI only, no WSL pipe). **Use DRC rules
   for numbers, pcbnew for connectivity and geometry reads, no IPC.**

## 1. Measurement: what KiCad can compute and who computes it today

Authority column: **DRC** = kicad-cli DRC with generated rules; **CONN** = pcbnew connectivity after fill; **own** = KiCad has none.

| quantity | computed today in (file:function) | authority | action |
|---|---|---|---|
| net length (total copper, via height) | `netlen.pair_lengths` (KiCad); `si.measure_net_lengths`; `lengthmatch.lengths_from_board` + `_pad_to_pad`; `pairaudit.profile`/`_graph`; `channel._graph`/`measure_paths`; `tune.path_len`; `gaps._track_len_by_layer`; `cli.py:1983 net_lengths` | DRC `(constraint length)` / `skew` per net | `netlen.lengths(board, nets)` is the one source; delete `si.measure_net_lengths`, `lengthmatch._pad_to_pad`, length part of `pairaudit.profile` |
| intra-pair skew | `pairaudit.audit` (4-way fallback); `pairaudit.multi_paths`; `channel.evaluate`; `tune.pair_skew` (3-way fallback); `si.pair_skew_findings` via `review.check_pairs:309`; `pairsonly._profile_row`; `routepar` 117-123 | DRC `skew` for 2-terminal pairs; own walk only for joined rows | one function `netlen.pair_skew` (section 1.1); delete the fallbacks |
| per-path length, stubs (multi-terminal) | `channel.measure_paths`/`_stubs`; `pairaudit.multi_paths`; `pairaudit.channel_paths` | own (KiCad has no per-path on a branched net) | keep `channel.measure_paths` only; hard identity check vs KiCad total; delete `multi_paths` |
| match groups, timing | `lengthmatch.lengths_from_board`/`evaluate`, `timing.check`, `review.check_match_groups`, `tune.tune_match` | DRC `length` (min/max per group rule) | generate one rule per group; keep `lengthmatch.groups`/`members`, delete its measuring half |
| opens / connectivity | `routeconn.text_open` (file text, no fill), `routeconn._pcbnew` (conn_helper), `opengate.measure` (3-way compare), `drcguard.measure`, `progress.measure`, `repair.measure`, `adaptive.drc_unrouted`, `pipeline._open_count`/`_unc_nets`, `snapiter`, `repairregion`, `handrepair._unc` (FLOW-REDESIGN 1.3 counted 17) | DRC unconnected (`drcfull.complete`) = CONN after refill | `drcguard.measure` only. `routeconn.text_open` stays as the pre-check inside search loops, labelled approx, never a verdict |
| DRC run itself | `drcguard.drc_json`, `tune.drc_counts`, `fanout` 121, `handroute` 65, `opengate._drc` (53, 82), `snapiter` 21, `planetie` 35, `routefr` 519, `progress.measure` 90, `cli.py:1667` (all refill) and `audit.drc_full`, `migrate` 378, `tournament.drc_count`, `routeaudit.drc_types`, `adaptive.drc_unrouted`, `fab` 151, `corpus` 302/332, `launder._drc`, `drcfix.run_drc`, `netlen` 51, `routefr` 588 (**no `--refill-zones`**, L11) | DRC | one runner `drcguard.drc_json(board, rules=None, severities=None)`: temp copy, always refill, unique report path (fixes ledger 11-20 "suggest collide"); everything else calls it |
| clearance (copper, hole, edge) | `routeaudit.audit`, `routefr.copper_clearance`, `placetour.carried_clearance`, `tune.candidate_clear`/`_min_clear`, `handroute._via_site_clear`/`_clear_fix`, `repair._seg_clear`/`_clear_for_via`, `drcfix._spot_clear`, `snapiter.clear_ok`, `patch._stub_clear`, `thermal._clear`, `planes.clear`, `fanout_helper.hit_seg`/`hit_via` | DRC for the verdict | search-time checks stay (constructors need them) but move to one model: `hrgeom` + `geom`. No own clearance result is ever an accept; accept = DRC keys through `drcguard.accept` |
| pair width / gap / coupling | `review.check_pair_geometry`, `gaps.impedance_off_nets`, `pairuncoupled.measure`/`uncoupled`, `pairs.layer_geometry`, `zgeom.pair_geometry` | DRC `diff_pair_gap`, `track_width` (layer-scoped), `diff_pair_uncoupled` from the same generated rule file | solver stays own (`zgeom`); the check becomes generated rules; delete `pairuncoupled` measuring and `check_pair_geometry` walking once the rule output is proven equal on W15 |
| impedance / delay closed forms | `stackup.microstrip_z0`/`stripline_z0`/`diff_*`/`solve_*`; `impedance._z0`/`z_diff`/`solve_*`/`delay_ps_per_mm`; `review.layer_geometry`/`z0_on_layer`; `lengthmatch.layer_ps`; `channel.delay_ps_mm`; `pcbway._impedance` | own (KiCad has no solver in the CLI) | one module `impedance.py`; delete the Z0 functions of `stackup.py` and `review.layer_geometry`/`z0_on_layer`; Er/Df from the board stackup (`channel.board_stack_rows`) |
| via count per net/leg | `pairaudit.profile`, `qualaudit.rf_grade`, `lengthmatch` | DRC `via_count` | generated rule from `budget.vias_max`; own count only for layer-sequence equality (KiCad has none) |
| zone fill, islands, slivers | `opengate.refill`, `planes.refill`, `patch.refill_zones`, `launder._refill_save`, `migrate.refill`, `fill_helper`; `edgesliver.sliver_islands`, `hrgeom.islands`, `handroute._zone_islands`, `pourisland.island_sites`, `acidtrap.slivers`, `review.check_slivers` | pcbnew `ZONE_FILLER` (fill), DRC `isolated_copper`/`copper_sliver`/unconnected zone items | one `opengate.refill`; island detection from DRC items; site search stays own |
| duplicate copper, UUID collisions | `dedupe.duplicates`, `boardtext.reuuid_collisions` | own (KiCad silent; 551 items, L11) | keep; call from the one commit point (section 3) and `versions accept` |
| part collision / courtyard | `legality.collisions`, `placement.count_overlaps`, `compact.collides`, `builder._overlaps_any`, `geom.overlap` | DRC `courtyards_overlap` for the gate; own inside placers | `legality` is the one placer model; gate verdict from DRC |
| loss dB, DC drop, crosstalk, return path | `channel.loss_db_mm`/`dc_resistance_mohm`, `channel_noise.analyse`, `power.*` | own | keep; inputs (lengths, widths, layers) come from the functions above |

### 1.1 One skew definition (replaces findings 0.1)

`netlen.pair_skew(board, pair, budget) -> {skew_mm, p_mm, n_mm, basis, paths, stub_mm}`:
- **2-terminal, branch-free pair:** `basis="kicad"`, the DRC number. No fallback. kicad-cli failure = `unmeasured`, graded as fail-to-grade.
- **multi-terminal pair (joined rows, taps):** `basis="path"`, `channel.measure_paths` per orientation, and the identity
  `trunk + stubs + in-pad + via barrels == KiCad net total` within 0.05 mm must hold per leg. A miss is `MEASURE_DISAGREE`
  (ungraded, FAIL in review), never a warning.
- "Joined" comes from topology (more than two pads of the net on one part), not from `re.match(r"(J|P|X|CN|CON)\d")`
  (`pairaudit.channel_paths`, a ref-prefix rule; rule 13).
- **Open point for the lead (D71cv wording):** "pad edge to pad edge, using KiCad's lengths". KiCad's total includes
  copper inside pads; W15 PCIE_TX is 1.07 mm by KiCad and 0.017 mm by the old walk. Recommended reading: KiCad's number is
  the gate number for 2-terminal pairs and tune removes in-pad asymmetry; needs a DECISIONS row. Also unverified: whether
  KiCad 10 skew rules accept `ps` with the board's delay settings (`EDA_DATA_TYPE_LENGTH_DELAY` exists in pcbnew); probe before use.

## 2. One budget model

New `budget.py` (about 250 lines); it replaces more than it adds (rule 18): `si.py` whole, `constraints.py` 233-351,
`channel.py` 430-483, and the family matchers below.

```
family_of(cons, cls, net) -> family | None     # classified pairs only (netclass.classify), longest [pairs.F]/[channel.F]
resolve(cons, cls, pair, stack) -> Budget      # rows: {metric, target, max, unit, source, klass REQ|REC|INF}
verdict(value, row) -> "ok" | "over-target" | "fail" | "ungraded"    # D71cv: target warns, max fails; D71cw: no max = never fails
predict(budget, endpoints, layers, stack) -> [{metric, value, verdict, over}]   # geometry only, section 5
rules_dru(budgets) -> str                      # the generated .kicad_dru: skew, length, via_count, diff_pair_gap, track_width
netclasses(budgets, geometry) -> {class: {...}}  # what inject_netclasses writes
```
Metrics: `skew` (ps or mm, converted once with the layer delay), `length`, `stub`, `vias`, `loss_db`, `zdiff`,
`drop_mv`, noise keys of `channel_noise`. Every number needs `source`; a number without one is `BUDGET_NO_SOURCE` (FAIL at `ready`).
Built-in numbers go: `constraints.STUB_MAX_DEFAULT`, `SLOW_PAIR_SKEW_MM`, the `1.0` default in `skew_limit_source`,
`review._HS_FAMILIES`. A pair with no budget is `ungraded` and listed, not silently passed at 1 mm.

| stage | calls today | should call | delete |
|---|---|---|---|
| ready (preflight) | nothing budget-related | `budget.resolve` for every classified pair; fail on no source, ps without delay, netclass != `budget.netclasses` (`NETCLASS_MISMATCH`) | - |
| project netclasses | `constraints.inject_netclasses` from `fab.py:474` only | same function at `ready`, fed by `budget.netclasses` + `zgeom.pair_geometry` (L6, IMPROVEMENTS 3) | hand-kept `W*-rules.kicad_pro` step |
| placer / placetour | `intent.check` rows; `placetour.judge_key` has no pair term; `channel.predict` uncalled | `budget.predict` (tier 0) in `intent.check` as generated link rows and in `placetour.judge_key`; `pairs.plan(plan_only=True)` (tier 1) in `placetour.gate_state` | `channel.predict`, `channel.DETOUR` |
| pairs lay | `pairs.rules_for` + `C.skew_limit_*`; `pairs.py:79` own family match; `pairs.guard_limit` | `budget.resolve` once per pair in `pairs.plan`; pass `Budget` in `ctx` | `pairs.guard_limit`, `pairs.py:79` |
| tune | `tune.tune_pairs(limit_mm=callable)`; `tune.pair_skew` 3-way fallback; `tune.path_len` | target = `row.target` (tune aims at the target, stops inside it), accept by `netlen.pair_skew` | `tune.pair_skew`, `tune.path_len`, `tune._profile` |
| gates (`gaps.classify`, `_gate_check`) | `gaps._pair_audit` -> `qualaudit.pair_rows` -> `pairaudit.audit`; `gaps.impedance_off_nets` | `pairaudit.audit` reduced to status logic on top of `budget.verdict(netlen.pair_skew(...))` | `pairaudit.multi_paths`, `_cross_check`, `_note_over_target` (emit from `budget.verdict` caller once) |
| review | `review._pairs_from_audit`, then fallback `check_pairs` -> `si.pair_skew_findings`; separately `channel.check` (second verdict, CHANNEL_MAX) | one section: `channel.evaluate` rows graded by `budget.verdict`; PAIR_SKEW and CHANNEL_MAX become one code per metric | `review.check_pairs` fallback (291-340), `si.py`, `review._ps_note` |
| routers (KRT, FR) | netclass from the project; `pipeline.pair_gap` (409) own lookup | project netclasses written at `ready`; `zgeom.family_of` -> `budget.family_of` | `pipeline.pair_gap`/`diff_named` |

Family matchers to fold into `budget.family_of` (L8: prefix hit non-pair nets ETH_LED, PCIE_CLKREQ, USB_CC):
`channel._family`, `zgeom.family_of`, `pairs.py:79`, `pairfb.uncoupled_limit:59`, `pipeline.py:414`, `ready.py:74`,
`review.pair_gated:243`, `review.py:547`, `netclass.py:196`, `krtchain.pair_nets:55` (this one returns every net with
the prefix, then feeds `drcguard.protected_nets` and the router's pair list). `rules._legacy` keeps the TOML parse only.

## 3. Determinism and protection as structure

**3a. One commit point.** New function `boardwrite.commit(base, cand, dst, manifest)`; every stage and rung writes a
candidate file and calls it; nothing else copies a candidate over a stage board.
- `manifest` = sidecar `<board>.done.json`: `{pair or net: fingerprint}` for (a) locked copper, (b) every pair whose
  audit status is complete. Fingerprint = `versions.copper_fingerprint` restricted to those nets (class, net, layer,
  ends, width, locked flag; no UUIDs: ledger 11-19 and 11-22 UUID defects).
- `commit` refuses when any manifest fingerprint differs in `cand` (`PROTECTED_CHANGED <name>`), when
  `dedupe.duplicates(cand)` is non-empty, or when UUIDs collide. It then adds newly complete pairs to the manifest.
- Releasing an entry is one explicit call `boardwrite.release(manifest, names, reason)`, logged to attempts.jsonl; used
  by the pair re-route rung and by tune (which must re-add on accept). `pairs --unlock`, `--allow-shove`,
  `shove --allow-locked` reduce to this call.
- `drcguard.accept`: `locked_ok` and `protect` stop being parameters; R3/R5 are computed from the manifest inside.
- Effect: IMPROVEMENTS 4b (14 -> 12), EFFICIENCY 11-22 "KRT routes gated pair legs", "shove rips soft copper", ledger
  11-20 `tune.shortcut` deleting locked copper, look-ahead run 3's 539 duplicates all become one refused commit, not
  five separate fixes. The 40 scattered `IsLocked` tests remain as search hints, not as the safety net.

**3b. Lay is a pure function.** `pairs.plan(board, cons, spec)` result must depend only on its inputs.
- Reserve before lay: exits of **all** gated pairs are reserved (`exit_reserve_mm`, today only for unresolved pairs,
  72900ff) before the first pair is laid; `pairs.reserve_for_fanout` already produces them for the fan-out.
- Total order key, no ties: (gated first, fewest legal gates, smallest pad pitch, span, name). Today `order0 =
  sorted(pl, key=key)` plus negotiation re-orders by failure, which is the 16/17 vs 14/17 flip (IMPROVEMENTS 4c).
- Retry rule replaces negotiation passes for gated pairs: a failed pair is retried alone with every completed pair as
  a fixed obstacle; completed pairs are never re-laid in the same run (they are in the manifest).
- No dependence on machine: `workers = min(8, cpu_count)` (`pairs.params`:45) must not change the result. Unverified
  whether it does; the test below decides.
- Property tests (new `tests/test_pairs_stable.py`): same input twice = identical `copper_fingerprint`; shuffled pair
  discovery order = identical; `workers=1` vs `8` = identical; **planned => lays**: for every pair `plan_only` reports
  planned, the lay on that plan succeeds (L5; `lay_section(plan_only=...)` is one function, so the test pins it).

## 4. Flow and gates

- **One flow.** Delete `pipeline.STAGES` (classic), `FLOWS`, `--flow`; `CONSTRUCTIVE` is the flow. Stage order for hs-first:
  `ready -> modules -> floorplan (budget-aware) -> pairs-escape+fanout -> pairs (gated) -> gate hs -> tune -> rails ->
  pairs (rest) -> route -> close -> silk -> review`. `placetour`/`lookahead` stay config-gated until they move a board (0 moves, 0 gated pairs laid so far).
- **Gate = measure, not repair.** `pipeline._gate_check` runs `gaps.close` (default 20 min) even on a `--from` input
  (`_run` M3 branch, about line 1590). Split it: `gate_measure` (one DRC + pair audit, seconds) decides; the repair loop
  runs only inside the stage that owns the gap kinds. `--from X` with an unclean earlier gate exits 5 in seconds with
  the gap list (L7, FINAL run 1: 20 min, 11 attempts, no change).
- **No-progress stop by attempts, not minutes.** `gaps.close`: `STALL_MIN = 30` min and `budget_min = 120`. Replace with:
  one sweep of the untried (gap, rung, window hash) keys; a sweep with zero accepts stops `flat` at once. Memo already
  exists (2165-2180). Before the first rung: if no in-scope gap has a rung in `DISPATCH`, stop `flat` without a DRC candidate.
- **Fail fast at `ready`:** budgets resolved, sources present, netclasses equal budgets, stackup written, duplicates 0,
  UUID collisions 0, zones filled, kicad-cli and pcbnew versions. All are text or one DRC; none needs a router.
- **Subprocess protocol:** result as a JSON file, never the last stdout line (EFFICIENCY 11-22 "memorOK 1170"); every
  child keeps its output tail in the stage folder. One helper in `quietlog`/`calllog`, used by all 20+ `subprocess.run` sites that parse stdout.
- **Stages that looped without gain and what to do:** `placetour` round N repeating round N-1 (memo by move-set key,
  local tier-1 term; or leave disabled); `lookahead` FR ghost (1012 s, 0 nets: cut); `close` passes 2-4 on W14 (4 h, 0
  accepts: covered by the sweep rule); tune micro-meander (min step 0.7 mm vs 0.1 needed: tune must use `netlen` and a
  sub-0.3 mm jog before it is in the chain).
- **Enforcement:** write `tests/test_surface.py` now (rule 3, 8, 13): greps for `"pcb", "drc"` outside `drcguard.py`,
  for `startswith(fam` outside `budget.py`, for layer-name and ref-prefix literals, counts public parsers and flags.
  Start with the current counts as ceilings that may only fall.

## 5. Placement fed by budgets

1. **Endpoints.** `budget.predict(pair)`: length = sum over consecutive terminals (`pairs.terminals` + `pairs.tree`, the
   lay's own pairing) of Manhattan x detour on the planned layer; verdict per D71cv/cw. Used two ways:
   - `intent.items` gains generated link rows `pair-length` (hard = a max exists; soft = target only, e.g. ETH_L 25 mm
     per D71cw), so `intent.check`, `legalise`, `floorplan2` and `wlrefine` honour it through the existing INTENT gate.
   - `placetour.judge_key` first terms: gated pairs predicted over max, then plan-only pairs not planned, then sum over-target.
   Report at floorplan: every pair outside target with how far over and pass/fail (D71cv text), before any copper.
2. **Skew from geometry.** Intrinsic skew per end = offset of the P and N pads along the exit direction (fine-pitch
   rows, split RJ45 pairs 3/6, USB-C A/B rows). `predict` reports pairs whose intrinsic skew exceeds the max and whose
   predicted length leaves no room to compensate; the fix is a part turn or swap, which only the placer can do.
3. **Escape and fine-pitch exits.** One escape bundle per fine-pitch part: hs pair exits (`pairs.plan plan_only`,
   break-out along the pad axis, PAIRS-S2) and the GND/power fan-out planned together, pairs first
   (`pairs.reserve_for_fanout` -> `escapeplan.plan_escapes(reserved=...)` -> `fanout`). Make the reservation
   mandatory when the board has gated pairs; today it is a separate call. Placement legality (`escreserve.for_legalise`)
   takes the same reservations so no neighbour part or via lands in an exit lane (L9, ledger "fan-out before pairs").
4. **Joined connector rows (USB-C).** The join is part of the escape bundle: plan both row pads and the join as one
   topology with `stub <= budget.stub.max`; `predict` flags a connector whose row spacing alone exceeds it. The first
   series part (ESD, CMC) gets a generated `pair-length` link to the connector.
5. **Magnetics.** Nothing special in code: `[channel.F] length_mm_target/max` on the line-side family generates the
   link magnetics <-> jack; D71cw makes it soft. `review.check_magnetics` (pin table) stays.
6. **Predictor = executor** (L5): tier 1 is always `pairs.plan(plan_only=True)`; `planroute` statistics are not used
   for pairs (FLOW.md: right 7-27 % of the time).

## 6. Remove (dead code, flags, modes)

Deleting code is an owner decision (rule 6); this is the candidate list with the evidence. "Verify" = grep the
project run scripts and tests before the delete commit.

| item | evidence | action |
|---|---|---|
| `si.py` | superseded by pairaudit/netlen; only reachable through the `review.check_pairs` fallback | delete with step B2 |
| `pairaudit.multi_paths`, `_cross_check`, env `FLUXPLACE_AUDIT_OWN_LENGTHS` | second and third definition of skew | delete with A2 |
| `tune.pair_skew`/`path_len`/`_profile`, `lengthmatch._pad_to_pad`, `gaps._track_len_by_layer` | duplicate lengths | delete with A2/C3 |
| Z0 functions in `stackup.py`, `review.layer_geometry`/`z0_on_layer` | duplicate of `impedance.py` | fold, C4 |
| `channel.predict`, `channel.resolve_budget`/`_family`/`verdict` | move to `budget.py` | B1 |
| classic flow (`pipeline.STAGES`, `FLOWS`, `--flow`, `st_floorplan_check`, `st_anchor_check`, `st_place`, `st_route_ready`) | not used by W14/W15 scripts | C1; verify |
| `cli.py` `auto`, `tournament`, `route`, `plan`, `gather`, `compact`, `calibrate`, `finish`, `repair`, `repair-region`, `drc-fix`, `patch` as public commands | superseded by `pipeline` + `DISPATCH`; rule 2 | behind `x` (FLOW-REDESIGN phase 13), C5 |
| placement strategies `radial`/`flux`/`quad`/`build` (`cli.py:3274`), `--engine cypress`, `--objective plan` | constructive flow uses modules + floorplan | C5; verify corpus |
| bypass flags `--force`, `--no-review`, `--allow-worse`, `--allow-overlaps`, `--allow-incomplete`, `--allow-undocumented`, `--allow-unverified`, `--no-verify`, env `FLUXPLACE_GUARD=0` | playbook: "never bypass a guard"; W15 scripts pass `--allow-incomplete --allow-undocumented` on every call, so the flags are the flow | replace with `[flow.allow]` in constraints (recorded, reviewed) |
| `--router`, `--router-py`, `--router-dir`, `--freerouting-jar`, `--route-budget` | rule 7: a flag never carries a tool path or a time | to `[route]` / `deps` |
| `[lookahead]` Freerouting ghost (`lookahead.lean_freerouting`), `[lookahead] lay = all` | 230 s per pass, 21 of 614 items; `all` let non-gated pairs take gated corridors | cut |
| `pairs` negotiation passes for gated pairs, `[pairlay] negotiate_passes` | replaced by 3b | after 3b proves equal or better |
| `migrate.py`, `boardmatrix.py`, `intentdraft.py`, `partdiff.py` | no importer found by grep (may self-register a parser) | verify, then `x` or delete |
| 21 private DRC call sites (section 1) | rule 3 | A1, C2 |

## 7. Ranked steps

Size: S <= 150 changed lines, M <= 500, L more. Every step: `/usr/bin/python3 -m pytest tests/` green, a Fable diff
review before any run uses it, no edit under `fluxplace/` while a measurement run is alive (run from a frozen worktree).

### A. Must land before W15 routes (correctness of what the gate says and keeps)

| # | step | files:functions | effect | risk | size | tests |
|---|---|---|---|---|---|---|
| A1 | One DRC runner: always refill, temp copy, unique report, optional `rules`. Route `netlen` and the gate/accept path through it first (others in C2) | `drcguard.drc_json`; `netlen.pair_lengths` -> uses it; `tune.drc_counts`, `progress.measure`, `opengate._drc` | no stale-fill false errors (700+), no report collisions; one place to time DRC | low | S | `test_drcguard.py`: refill flag always present; two concurrent calls; rules text reaches kicad-cli |
| A2 | One skew: `netlen.pair_skew` per 1.1; `pairaudit.audit` uses only it; identity check for path basis; topology-based "joined" | `netlen.py`, `pairaudit.audit`/`channel_paths`, `channel.measure_paths`, `tune.tune_pairs` accept | the gated count means what KiCad says (W15: "16/17" was 10/17); tune converges on the graded number | medium: W15 numbers shift once more; needs the D71cv in-pad reading decided | M | fixture pairs with in-pad copper, via height, a joined row: equals a recorded kicad-cli value; kicad-cli missing = ungraded, not pass; identity miss = MEASURE_DISAGREE |
| A3 | Commit point + manifest (3a) wired into `pairs.run`, `pairsonly.reroute`, `lookahead` adopt, `krtchain.run`, route stage checkpoints, `gaps.close` accept, `tune` | `boardwrite.commit`/`release`, `drcguard.accept`, `versions.copper_fingerprint`, `dedupe` | complete pairs cannot be re-opened or duplicated by any later stage; the router can run without fear for the 14-16 laid pairs | medium: a legitimate re-lay now needs `release`; false refusals from float rounding (round to 1 um as `tune.seg_key`) | M | `test_boardwrite.py`: changed protected seg refused; added duplicate refused; release then re-add; rip by KRT stub on fixture refused |
| A4 | Netclasses from budgets at `ready`; `NETCLASS_MISMATCH` fail | `constraints.inject_netclasses` call moved to `pipeline.st_ready`; `zgeom.pair_geometry` | KRT and break-outs get 0.15/0.127 on 90 ohm pairs without a hand-kept project file; ends pair-impedance gaps made by the router | low | S | project with Default-only classes -> after `ready` has the family class; mismatch detected |
| A5 | Gate = measure; `--from` on an unclean gate exits in seconds; zero-accept sweep stops `flat` | `pipeline._gate_check` split, `_run` M3 branch, `gaps.close` stop logic | removes the 20 min and 45 min dead loops before and after the route | low | S | `test_pipeline.py`: unclean `--from` returns exit 5 with gaps and no rung attempt; `test_gaps.py`: sweep with 0 accepts stops after one pass |
| A6 | Family = classified pairs only | `budget.family_of` (first slice of B1: just this function), `krtchain.pair_nets`, `drcguard.protected_nets`, `channel._family` | router pair list and protected set stop including LED/CLKREQ/CC nets | low | S | nets `X_LED`, `X_CLKREQ` with a `[pairs.X]` family are not pairs, not protected-as-pair |
| A7 | Lay determinism tests (3b tests only, no behaviour change yet) | `tests/test_pairs_stable.py` | tells before the route whether the W15 lay is reproducible; a red test names the cause | none | S | the four property tests |

Order: A1 -> A2 -> A6 -> A4 -> A3 -> A5; A7 in parallel. A2 and A3 are the two that change what "done" means; W15
routing should start from a board re-measured after A2 and committed through A3.

### B. Right after the W15 route starts (does not touch the routed board's verdicts)

| # | step | files:functions | effect | risk | size | tests |
|---|---|---|---|---|---|---|
| B1 | `budget.py` complete (resolve, verdict, rules_dru, netclasses); callers per table 2; delete `constraints` 233-351, `channel` 430-483, `si.py`, built-in defaults | section 2 | one budget, cited, target/max everywhere; review shows one line per metric | medium (many call sites, mechanical) | L | port `test_constraints` skew tests; ps-without-delay = ungraded; no source = FAIL; D71cw: no max never fails |
| B2 | Review: one pair section from `channel.evaluate` + `budget.verdict`; drop the `check_pairs` fallback | `review.check_pairs`, `_pairs_from_audit`, `channel.check` | no double or conflicting PAIR_SKEW / CHANNEL_MAX | low | M | golden review on fixture board: codes and counts |
| B3 | Deterministic lay (3b behaviour): reserve all gated exits first, total order key, retry-alone, drop negotiation for gated pairs | `pairs.plan` (`one_pass`, `moves`, `order0`), `pairs._lay_pair` | order-stable gated lay; expected W15 gain: the PCIE_CLK/RX flip disappears | medium-high: may lay fewer pairs at first on some boards; keep the best-of rule (complete count, then skew) | M | A7 tests go green; corpus: gated pairs laid not lower on any board |
| B4 | Placer reads budgets: `budget.predict`, generated intent links, `placetour.judge_key` pair terms, floorplan report | `budget.predict`, `intent.items`/`check`, `placetour.judge_key`/`gate_state`, `floorplan2` report | L10 found at placement, not after routing; placetour can rank (keys were identical for all candidates) | medium | M | endpoints 44 mm vs target 25 = over-target row, pass (no max); over max = fail; judge key changes when a pair part moves |
| B5 | Escape bundle mandatory (section 5.3-5.4) and before fan-out | `pairs.reserve_for_fanout`, `escapeplan.plan_escapes`, `fanout`, `escreserve.for_legalise`, pipeline order | GND fan-out vias never sit in a pair exit lane (all 4 failing W15 pairs died within 1 mm of the connector pins) | medium | M | fixture: 0.4 mm pitch row with GND neighbours: pair exits planned, GND vias outside lanes, plan => lay |

### C. Cleanup after W15 is routed (surface and duplication)

| # | step | effect | risk | size | tests |
|---|---|---|---|---|---|
| C1 | Delete the classic flow and `--flow`; one stage list | one flow | low | M | `test_pipeline.py` stage list; `test_surface.py` |
| C2 | Remaining DRC runners and open counters call `drcguard.measure` (section 1 rows 4-5) | rule 3 true; about 1.2 DRC per accept becomes measurable | low, wide | L | `test_surface.py` grep = 1 runner |
| C3 | Match groups and timing through generated `length` rules; delete `lengthmatch` measuring | same authority for groups as for pairs | medium (per-layer delay cases) | M | group fixture equals kicad-cli |
| C4 | One impedance module; pair gap/width/uncoupled check via generated DRC rules after an equality run on W15 | no solver drift between stackup, review, pairs | medium | M | Z0 table equal before/after for all templates |
| C5 | Surface: `x` namespace for superseded commands, bypass flags to `[flow.allow]`, tool paths to config, budgets in `test_surface.py` | 65 parsers -> target 15 public | low (nothing deleted) | L | surface counts |
| C6 | Subprocess JSON-result helper everywhere; output tails kept | no stdout-parsing crashes | low | M | helper unit test with noise on stdout |
| C7 | Remove items of section 6 that C1-C6 left without a caller | net line count falls (state before/after) | owner decision | M | full suite |

## 8. Not verified in this audit

- No board pipeline or kicad-cli DRC was run; W15 numbers are from the evidence files.
- KiCad custom-rule coverage for `diff_pair_uncoupled`, layer-scoped `track_width` and `length` on this board, and ps units in skew rules: probe with a 3-rule `.kicad_dru` before C3/C4 depend on them. `skew` is proven by `netlen`.
- Whether `pairs` negotiation output depends on `workers`, and which exact input difference flipped 16/17 to 14/17: A7 answers.
- "No importer" for `migrate`, `boardmatrix`, `intentdraft`, `partdiff` is a grep result; `fanout_helper`, `conn_helper`, `fill_helper`, `flip_helper`, `fr_helper`, `joingaps_helper`, `silk_helper` are subprocess helpers and are in use.
- Another agent was editing `pairaudit.py`, `tune.py`, `tests/test_pairs.py` during the read (since committed); line numbers are from the tree at read time and will drift.
- Line counts for S/M/L are estimates from reading, not from a prototype.
