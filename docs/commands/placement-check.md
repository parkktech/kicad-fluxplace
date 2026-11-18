# `placement-check` - every keep-out rule against the board

```
fluxplace placement-check BOARD --constraints C [--spec S] [--json] [-q]
```
Read-only. One row per rule, with the offending parts; exit 1 when any rule fails. Writes `<board>.placement-check.json`.

| rule | source | fails when |
|---|---|---|
| keepout | `[placement.keepout.NAME]`, `[[keepout]]` | a part body of the rule's side overlaps the rect (overlap mm2) |
| no_tht | `[placement.no_tht.NAME]` | a through-hole pad lies in the rect (pad, position) |
| hardware | `[placement.hardware]` | a courtyard or pad is closer to a mounting hole's centre than half the keep-out diameter (exact circle) |
| module | `[placement.module.NAME]` | shown as its keep-out and no_tht rows (`module.NAME`) |

Each violation lists ref, face, the measure versus the requirement, and `[FIXED]` when the part is fixed or locked (a
conflict only the constraints' owner can resolve). The last block is the courtyard coverage of each board quadrant
(courtyard area / quadrant area, per face and both; through-hole parts count on both faces).

## Constraint tables (read by `legalise`, `legality`, the placers and this command)

```toml
[placement.hardware]            # off unless the table exists
table = [[2.4, 5.0], [2.9, 6.0], [3.6, 7.0]]   # drill up to -> keep-out diameter (defaults shown: M2, M2.5, M3 washers)
min_drill_mm = 2.0              # smallest drill that counts as a mounting hole
refs = ["H200"]                 # extra hardware (a standoff without a drill needs diameter_mm)
exclude = []                    # never hardware (fnmatch)
allow = []                      # refs allowed inside every circle (fnmatch)
diameter_mm = { H200 = 5.0 }    # per-ref override

[placement.module.m2]           # expands to a keep-out (+ no_tht when tht = false)
refs = ["J200", "H200", "H201"] # the module's own parts: exempt
side = "B"
outline = [107.75, 40.0, 160.0, 70.0]   # or margin_mm = 1.0: bounding box of the refs' pads/holes grown by it
allow = []                      # more exempt refs
tht = false                     # also forbid through-hole pads in the outline (default true = keep-out only)
```
A mounting hole = a part whose pads all sit on one spot, largest drill >= `min_drill_mm`, no net (or a ground net) on it. The legality gate
checks the circle exactly (reason `hardware`); rect-only placers get its bounding square (side both, allow = the hole's ref).
`outline` modules are expanded by `constraints.load`; `margin_mm` ones when the board is at hand (`placerules.expand`).

## Height allowance under a module
`under_height_mm = H` (default 0: nothing at all) and `under_margin_mm` (0.2) on a `[placement.module.NAME]`: a part of the
module's face inside the outline is allowed when its known height <= H - margin; unknown height is NOT allowed. Heights come
from `[placement.heights]` (ref or footprint-name glob -> mm, first match), else the chip-size estimate for 0201-1210 passives
(`intent.height`); no 3D-model or STEP parsing. It expands to a `[[intent.region]]` with `max_height_mm`, the mechanism the
placers and the legaliser already honour per part, so gate and placers agree. `placement-check` lists each part under such a
module with height, source and verdict `ok` / `too tall` / `height unknown`; the last two count as violations.

It also runs the collision gate the route stage applies (`legality.collision_gate`): parts whose courtyards overlap on the same side, copper of different nets overlapping, through-hole conflicts. Only part-against-part findings are listed there; rule-area findings appear in the keep-out rows. Any collision is a violation (exit 1).
