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

**PLAN_SCORE** = 100 sealed + 40 no-via-site + 5 overflow + 1 mm over budget + 30 pair split + 30 RF change + 200 unreached
+ 0.05 x class-weighted copper mm. Lower is better.

`route-ready` runs it too (`[route_ready] use_plan = true`, the default; `false` turns the PLAN_* gate off). Honest limits: the
model is capacity-based; it cannot see what the router does to a pin through other nets' escape copper (that is why placement-only
sealed pins of the real V1.6 board were mostly missed - see NEXT.md); cell quantisation (2 mm) smears local effects.
