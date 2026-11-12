# `plan-route` - can this placement route? Judge it before any copper

```
fluxplace plan-route BOARD --constraints C [--spec S] [--ignore-routing] [--cell 2.0] [--map PATH] [--json F] [--pcbnew]
```
Runs map -> escape planner (`escapeplan.py`) -> global router (`globalroute.py`), writes the sections into the map, prints one
verdict line (`PLAN_SCORE`, components), counts per code and the worst N findings each with a concrete suggestion. Exit 1 when sealed
pins or overflow remain. `--ignore-routing` judges the placement only (all tracks and vias treated as absent, every net planned);
default mode respects copper: complete nets are skipped, their copper blocks capacity, open nets are planned from their pad clusters,
pads already attached to same-net copper are not planned.

**Floors** (`[plan]` in the constraints, else 0.127 / 0.127 / via 0.45 / 0.20, never below `[fab] min_*`): `track_mm clearance_mm
via_mm drill_mm cell_mm lane_len_mm reach_mm plane_radius_mm pour_pads util jog_frac iterations`, weights `w_sealed w_nosite
w_overflow w_overbudget w_pair w_rf w_unreach w_len`, `plane_nets = [...]`.

**Escapes.** Parts with pitch <= 0.65 mm or >= 16 pads: per non-plane pin a free `lane_len` (1.0 mm) outward/inward lane on the pad's
layer (`direct`), else a dog-bone to a via site on a 0.6 mm hex lattice (4 phases; maximum bipartite matching, shared with
neighbours and with plane pins), else `PLAN_PIN_SEALED` naming the blocker and the part to move. Plane nets (GND, +5V, any net with
an inner plane or pour, rails of >= 20 pads, `[plan] plane_nets`): every pad needs a site within 1.5 mm (`PLAN_NO_VIA_SITE`); vias are
allowed in exposed thermal pads of plane nets only; a pad with a pour on its own layer and a free lane counts as served.
Headroom (free sites per side) per part is in the map `escapes.headroom`.

**Global route.** 2 mm cells; per layer and direction capacity = floor(free width / (track+clearance) x util 0.8), pads, via barrels,
keep-outs, planned vias, fine-pitch lane fields and (default mode) copper subtracted (obstacles covering < 50 % of the cell along a
track's run are jogged around). Nets: Prim decomposition from escape points, PathFinder negotiated congestion (<= 10 iterations,
deterministic order), pairs as one 3-pitch demand (split -> `PLAN_PAIR_SPLIT`), RF nets with a heavy via cost
(`PLAN_RF_LAYER_CHANGES` above `[rf] max_vias`), estimated length = octile distance + detour (`PLAN_NET_OVER_BUDGET`),
`PLAN_UNREACHABLE`, `PLAN_OVERFLOW` per region (nets, bordering parts, widen-the-gap suggestion).

**PLAN_SCORE** = 100 sealed + 40 no-via-site + 5 overflow + 20 mm over budget (rf/switch_node/bypass/pair nets; 3 for the others) + 30 pair split + 30 RF change + 200 unreached
+ 0.05 x class-weighted copper mm. Lower is better.

`route-ready` runs it too (`[route_ready] use_plan = true`, the default; `false` turns the PLAN_* gate off). Honest limits: the
model is capacity-based; it cannot see what the router does to a pin through other nets' escape copper (that is why placement-only
sealed pins of the real V1.6 board were mostly missed - see NEXT.md); cell quantisation (2 mm) smears local effects.

## Second pass (codes, confidence, variants, calibration)

New finding codes: `PLAN_ESCAPE_OVERSUBSCRIBED` (FAIL; per pin group = one row/side of a fine-pitch part: pins needed vs free fan-out
tracks after through traffic + via slots), `PLAN_POWER_NO_PLANE` (a rail of >= 20 pads with neither plane nor pour; routed as a tree and
charged to the cells it crosses; suggestion: pour on an inner signal layer), `PLAN_BUS_WEAVE` (nets of one bus mainly on different layers),
`PLAN_PART_IN_CORRIDOR` (a <= 6-pad part inside a reserved corridor: >= 3 connected cells >= 80 % used), `PLAN_RETURN_PATH` (rf/pair/clock
net changes between layers that reference different planes with no GND via/pour pad within 3 mm), `PLAN_ZONING` (switch-node part closer than
`[zoning] keepaway_mm` (5) to an RF/audio part; `hot`/`sensitive` ref lists). Power pins carry `feed` (the plane/pour layer) in `escapes`.
Every via charges 2 track units (`via_units`) on each layer it does not use in its cell (barrel + annulus + clearance ~ 0.7 mm). Inner
layers with >= 20 tracks stay signal layers (mixed signal + pour). Layer direction: In2 east-west, In3 north-south (`[plan] layer_dir`, 1.5x
wrong-way cost); a bus is kept on one layer or reported.

`confidence` (0-100) = 100 x (0.4 U + 0.3 H + 0.3 S) - 15 x (sealed + oversubscribed) - min(40, 2 x overflow regions), U = 1 - (worst
cell-layer utilisation - 0.5)/0.5, H = min over fine-pitch parts of free via slots/(2 x signal pins), S = min over budgeted nets of
slack/(20 % of budget), each term clipped 0..1. It is 0 on any board that overflows badly.

`--variant`: the same plan with `mixed` (as the board), `plane3v3` (one inner signal layer becomes a dedicated plane) and `8layer` (two more
signal layers): table of score, confidence, sealed, overflow, vias, copper. `--swaps`: pads in `[swap.group.NAME] pads = ["J10.25", ...]` are
swapped pairwise on the model and the plan re-run (4 negotiation iterations); gains listed, nothing is applied (schematic change).
`--calibrate ROUTED_BOARD [--write-constants]`: default-mode plan on a routed board, hit/miss of its open nets (misses are logged in the map as
planner errors), the share of the board's real track cell-directions above the modelled capacity for util 0.6-1.0 x jog 0.3-0.6, and with
`--write-constants` the tightest pair holding 95 % written to `[plan]`. Constants in use: cell 2.0, util 0.8, jog_frac 0.5, via_units 2.0,
lane 1.0, reach 1.6, plane radius 1.5, slot pitch 0.6, wrong-way 1.5 (on a0503fc 2.3 % of real cell-directions exceed them).
`place --objective plan` starts with a rotation pass (A1: units with >= 6 pads or a finding, big first, the three other rotations; flip when
`flip_cost` is set) and also proposes moves out of corridors and away from zoning conflicts.
Not done: test-point access, rework clearance, silkscreen space (B3), thermal-via reservation, a decoupling-order INFO check, automatic
swap groups (they must be named in `[swap]`).

## Review-fix notes (2026-11-12)

- `route-ready` runs the plan only with `[route_ready] use_plan = true` (default FALSE: the placement-only calibration is 10-41 %); its findings are WARN
  unless `[route_ready] plan_gate = "fail"`.
- ONE verdict predicate (`planroute.predicate`) drives the exit code, plan-options and the gate: FAIL on sealed pin, oversubscribed group, unreachable
  connection, no via site, RF layer change over the limit, rail without plane (`fail_on_power_no_plane`), hard-class over-budget net (both legs of a pair),
  risk_frac > `max_risk_frac` (0.06) or more than `max_risk_nets` (10) nets at risk. Confidence is < 50 whenever it fails.
- `[plan] floor_mm` (0.127) is enforced: a lower `track_mm`/`clearance_mm` is raised to it and reported (`floor_clamped`); sub-floor plan-options candidates
  run with `allow_subfloor` and are only listed on a separate "not offered" line.
- Controlled nets come from the constraints (class, `[nets.X] impedance`, `[rf] nets`, `[pairs.*]`, `[plan] controlled_nets`, `controlled_names` fallback).
  Inner-layer directions alternate along the stack (`[plan] layer_dir` overrides). Obstacles: pad rows with gaps below track + 2 clearance merge into one wall;
  cells outside the outline / in cut-outs and the copper-to-edge strip carry no capacity; thin keep-outs are intersected exactly; dog-bone stubs are checked
  against each other. The courtyard class of overflow applies only to the layers a part's pads block.
- `place --objective plan` accepts moves on (FAIL flag, hard failures, risk_frac, score), is bounded by evaluations (`[placement] max_evals`, 400), and runs a fresh
  full plan at the end: if that is worse than the input's full plan, every move is reverted. `plan-options --place-evals`.
- `calibrate --write-constants` keeps `<constraints>.bak`.

## Planned pours, rigid group moves, oversubscribed groups

`[[plan.pour]]` (`net`, `layers`, `region` = "board" | [x0,y0,x1,y1] | [[x,y],...], `share` 0.5): copper the owner intends to add. The planner treats it as existing
for that net (no PLAN_POWER_NO_PLANE; pins need a via site, feed layer reported; a pour on the pad's layer serves pads with a free lane), charges
`1 - share` of the signal capacity of the layers under it, and uses a board-wide pour layer as a return-path reference. The map carries it in `board.pours`.
`plan-options` adds pour axes (`pour_In2`, `pour_In3`, `pour_split`, `pour_local`) for every PLAN_POWER_NO_PLANE net (rails of >= 60 pins board-wide, smaller
rails a local pour over their pin bounding box + 3 mm).
`[plan.group.NAME] refs = [...]` (else the IC + support unit) are moved as rigid groups by `place --objective plan`: for a hard over-budget net the group of one end is
re-seated on any legal 2 mm grid site within 40 mm of the other end (4 rotations), or both groups go to free edge sites; group moves run before the other passes.
A group must contain every part tied to its members by the support-part limit (e.g. the bias inductor's regulator cluster) or the guard rejects the move.
PLAN_ESCAPE_OVERSUBSCRIBED groups get rotations of the part, the other side when `flip_cost` allows, and moves of the parts blocking the fan-out; the move log names the groups it resolved.
