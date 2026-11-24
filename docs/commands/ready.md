# `ready`

### Readiness gate before placement and routing — `fluxplace ready SPEC --constraints C [--board B] [--no-api] [--allow-incomplete] [--json]`

Runs first in `pipeline` (stage `ready`) and standalone. Prints a checklist, one line per check, then the first blockers; full report
`<stem>.ready.txt/.json`. Exit **1** = a FAIL (blocked); exit **3** = open questions only ([questions.md](questions.md)); 0 = ready.
(FAILs win: fix them, then the questions are asked on the next run.)

| line | PASS when | else |
|---|---|---|
| `parts` | every part passes the part-entry gate ([parts.md](parts.md)); with `--no-api` the distributor/stock checks print `NOT CHECKED` | FAIL, blockers spread over the codes |
| `parts gate` | every part is RESOLVED: concrete MPN (not generic, not in the value), stock at the build quantity (`[bom] quantity_target`), datasheet on disk, cited pin data, package agrees with the footprint (a missing package string blocks only with `[parts] package = "required"`), a real 3D model; anything NOT CHECKED (offline, no `--board`) blocks | FAIL (blockers per need). **`--allow-incomplete` does not skip it**; only `--allow-undocumented` does: line `WAIVED`, exit 0, `<board>.notready.json` written, the pipeline records it in `state.json` and the ledger and repeats it in the final report |
| `footprint geometry` | fp-check on every project-drawn footprint and board footprint: pad/pad and pad/hole clearance, keep-out self-conflict, pad table vs the cited drawing ([fp-check.md](fp-check.md)) | FAIL, a `package` blocker; `--allow-undocumented` does NOT pass it |
| `stackup` | declared in the constraints (`[stackup]`) or read from the board file (printed as inferred) | question `stackup` (profile choice) |
| `class <name>` | the controlled class has a geometry (layer, width, gap) COMPUTED from the stackup; a netclass the project already declares for it is within tolerance | FAIL: no reference plane / target not reachable / declared netclass off target |
| `rails` | every rail of >= `[route] rail_min_pads` pads has a plane/pour or an answer | OPEN: `rail-pour:NET` = `tree` \| `pour` |
| `fixed parts` | anchors and mounting hardware locked or declared (`[placement] fixed`, `[lock.at]`) or answered | OPEN: `fixed-parts` |
| `outline` | an Edge.Cuts outline exists | FAIL |
| `design-rule floors` | `[fab]`/`[route]` floors, the project's rules, or an answer | OPEN: `rule-floors` |
| `board checks` | (without `--board`) UNCHECKED: rails, fixed parts, outline, floors not looked at | |

Controlled classes: `[pairs.FAM] impedance_diff`, `[rf] target_z` (nets: `[rf] nets`, else RF-named nets), `[nets.N] impedance`.
Optional per class: `layers`/`layer`, `gap_mm`, `tolerance_pct`. Geometry per candidate signal layer (default: every non-plane copper
layer): one reference plane = microstrip / edge-coupled microstrip, two = stripline (symmetric) / asymmetric stripline / edge-coupled
stripline. Formulas are the IPC-2141 closed forms already in `stackup.py` (`impedance.py` picks the model from the layer's heights to
the nearest planes); each result records formula, inputs (heights, Er, copper), width/gap, the impedance it gives, and a stated
accuracy (+-10 % inside the formula's validity range, +-15..20 % outside it or asymmetric). A closed form screens a geometry, the
fab's field solver signs it off. The chosen option is the narrowest gap whose width clears the floor (`max(fab min_track,
[ready] min_feature_mm = 0.127)`; never the bare fab minimum). Next to it the report prints what the board uses: the declared
netclass's Z and the dominant routed widths' Z per layer (a width that is right on an outer layer reads ~40 ohm on a thicker one).

    [stackup]                          # or: profile = "name" (stackup.py PROFILES)
    planes = ["In1.Cu", "In4.Cu"]      # reference layers (default: zone layers of the board)
    [[stackup.layers]]
    name = "F.Cu"                      # copper rows carry a name; dielectrics: type = "prepreg"|"core", thickness, epsilon_r
    type = "copper"
    thickness = 0.035
    [ready]
    min_feature_mm = 0.127

`--allow-incomplete`: exit 0 and write `<board>.notready.json`. The pipeline then prints `NOT READY ...` on every stage line, puts
`not_ready` in `state.json`, the status table and every `*.status.json`; the sidecar follows each stage's output board
(`questions.inherit`); `fab` and `deliver` refuse such a board (`NOT READY: <reasons>`). `pipeline` without `--spec` reports the
stage `na` (nothing to check); the stage's distributor/stock lookups are live by default (`--no-api` for offline, which blocks the parts gate: stock cannot be checked). Work list: [parts-todo](parts-todo.md), [parts-apply](parts-apply.md).

## The `fixed reasons` and `pair classes` lines (fixedreason.py, F1)

Every part a stage will not move needs a reason class: `[placement.fixed_reason."REF"] reason = "mechanical" | "enclosure" |
"module-mount" | "owner"` (`owner` also needs `decision = "<id>"`). A fixed part with no pad on a signal or power net (hole,
standoff, fiducial) is mechanical without a declaration. No class exempts a support part from its distance budget. Both lines
are OPEN without a question behind them: their findings count as open items (exit 3) and are listed in full in `<board>.ready.json`
(`fixed_reasons`, `pairs_unclassified`).

| Finding | Level | Fix |
|---|---|---|
| `FIXED_OVER_BUDGET` | OPEN | a fixed support part over its max (the support table of the power plan): free it, or move it and its pin together |
| `FIXED_REASON_MISMATCH` | OPEN | `mechanical` on a part with a support row: give the real reason or free the part |
| `FIXED_OWNER_GUESSED` | OPEN | a fixed support part whose pin is a guess: `[power."NET"] support = {REF = "IC.PAD"}` |
| `FIXED_OWNER_NO_DECISION` | OPEN | add `decision = "<id>"` |
| `FIXED_UNDECLARED` | OPEN | a fixed list exists and this part is held by anchor detection only (relocate, closure, refiners hold it; the floorplan does not): add it to `[placement] fixed` with a reason, or to `[floorplan] movable` |
| `FIXED_NO_REASON` | OPEN | add the `[placement.fixed_reason."REF"]` row, or remove the part from the fixed list / unlock it |
| `PAIR_UNCLASSIFIED` | OPEN | a `_P/_N`-style name pair in no family: add `[pairs.PREFIX]` / `[channel.PREFIX]`, or list both nets in top-level `not_pairs` |

A two-pad part in line with an RF net that reaches no IC (bias inductor, DC block at the antenna) is not a support part of the IC
on its DC side; a series part on an RF net that reaches an IC is graded by the RF length budget.

## The `power plan` line (powerbudget.py, D71dc / D71dd)

One row per `[power."NET"]` rail with a current, written to `<board>.power-plan.json`: allowed resistance, candidate copper per layer
(IPC width, sheet resistance, vias per layer change, reach `L_max` at target / max), the chosen layer, and per source-to-load edge the
Manhattan distance, the width it needs and the estimated drop. Copper is taken at `[env] temp_max_c` + the trace rise the IPC width was
sized for (a `[[dcdrop]] temp_c` wins); the path is Manhattan x 1.15 (detour) x `[route] power_crowding` (default 1.25).

| Finding | Level | Fix |
|---|---|---|
| `POWER_PLAN_NO_BUDGET` | FAIL | add a cited `[[dcdrop]]` row, or state why the rail has none: `[power."NET"] dcdrop_exempt = "reason"` |
| `POWER_PLAN_EXEMPT` | INFO | a rail with `dcdrop_exempt` (series drop by design, return net): no budget, no reach, named in the line |
| `POWER_SOURCE_UNKNOWN` | OPEN | `[[dcdrop]] from` / `to` or `[power."NET"] source` |
| `POWER_COPPER_IMPOSSIBLE` | FAIL | the via barrels alone exceed the allowed resistance |
| question `power-owner:REF` | OPEN | a rail support part whose pin is still a guess; the answer is written as `[power."NET"] support = {REF = "IC.PAD"}` |

Support owner, in order: `[power] support` (explicit); the net (one IC on the net); the one source or load group of the rail in the part's
functional block (`group`); the reference block (`block` / `refdes`); a spread guess (asked). Any pin of the rail within the limit takes
a bypass part (`near`). A bulk part (> 4.7 uF to ground) of a planned rail with a decided owner is graded against that owner wherever it
sits; identical bulk parts (same net, value, footprint) are interchangeable between their owners.
