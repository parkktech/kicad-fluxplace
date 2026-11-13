# `ready`

### Readiness gate before placement and routing — `fluxplace ready SPEC --constraints C [--board B] [--no-api] [--allow-incomplete] [--json]`

Runs first in `pipeline` (stage `ready`) and standalone. Prints a checklist, one line per check, then the first blockers; full report
`<stem>.ready.txt/.json`. Exit **1** = a FAIL (blocked); exit **3** = open questions only ([questions.md](questions.md)); 0 = ready.
(FAILs win: fix them, then the questions are asked on the next run.)

| line | PASS when | else |
|---|---|---|
| `parts` | every part passes the part-entry gate ([parts.md](parts.md)); with `--no-api` the distributor/stock checks print `NOT CHECKED` | FAIL, blockers spread over the codes |
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
stage `na` (nothing to check); `--api` turns the live distributor lookups on (default offline in the pipeline).
