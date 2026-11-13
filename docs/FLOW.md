# The constructive flow: modules -> floorplan -> fan-out -> pairs -> route

Why. A statistical planner (`plan-route`) predicted routing from a placement and was right 7-27 % of the time on the real
board: the failures were local and order dependent (fine-pitch pins that could not escape, power pins with no via site, pins
sealed by the router's own earlier copper), never open channels. So the flow does not predict the local part, it BUILDS it:
every pin gets real, DRC-clean escape copper before routing, and that copper is locked. No calibration, no confidence number.

| step | command | what its gate proves |
|---|---|---|
| density | `density` | there is room: utilisation per side/region, growth needed for a target (monotonic in growth) |
| modules | `modules` | every IC has its supports as one rigid block; ports per side; each block's own fan-out exists in isolation |
| floorplan | `floorplan` | blocks are legal and none sits on another's escape copper; wire length / crossings; fan-out re-proved on the whole board; minimal outline with `--size` |
| fan-out | `fanout` | FANOUT_OK: every required pin has stub/via and a free lane; DRC added nothing; items locked; reserved escapes (power pins, passives crowded by a pair) claimed first (`escres.py`, map `escapes`); `route-ready` FAILs PLACE_ESCAPE_UNRESERVED |
| pairs | `pairs` | differential pairs laid coupled and LOCKED before routing (centre-line search, matched via pairs, skew by construction); PAIRS n/total; an unlaid pair is reported with its blockers and stays with the router (WARN, `docs/commands/pairs.md`) |
| plan-route | `plan-route` | channels only (WARN): overflow between the ports |
| route | `pipeline` route stage | starts every net at its fan-out end point; locked copper is never ripped; order: escapes, plain copper, power pins, rails, controlled pairs / RF / clocks, rest; every batch audited; pair / RF out of tolerance = NOT DONE. `--router freerouting|auto`: whole-board Freerouting (2.5.0+) on the unlocked rest with fan-out and pairs fixed, sub-floor repair, then the per-net router and ladder on what is left (`docs/commands/route.md`) |
| audit / silk / review | | unchanged |

Order in `pipeline`: modules -> floorplan -> fanout (gate FANOUT_OK) -> pairs -> plan-route -> route -> audit -> silk -> review.
With `[placement.wl] enabled = true` the floorplan (and `place --strategy routable`) ends with `wl-refine`: shorter wires where fan-out, plan-route and intent stay
at least as good as before ([commands/wl-refine.md](commands/wl-refine.md)).
What it does not prove: that the channels between the end points are wide enough (that is `plan-route`'s job, and routing's).
A fan-out that passes in isolation can still fail at the site: the floorplan therefore claims each module's escape copper and
re-runs the whole-board fan-out after every search.

## Which source wins (artefact first; `facts.py`)
The owner's records decide; the bare board is inferred from only when they are silent. A disagreement between a record and the
board is a question (`docs/commands/questions.md`), not a guess.

| fact | order, first present wins |
|---|---|
| groups | constraints `[placement.group]` > constraints/spec blocks > map (recorded) > schematic sheets / support clusters |
| net classes | constraints `[pairs]` `[rf]` `[power]` `[placement.net]` > `.kicad_pro` net classes > map > name patterns |
| fixed parts | constraints `[placement] fixed` / `[lock.at]` > locked in the board > map > mounting hardware, edge connectors by shape |
| pours | constraints `[[plan.pour]]` / `[power.X] pour` > map `pours` > zones on the board |
| pin roles | spec pinmaps (never guessed) |
| answers | `--answer` > constraints `[answers]` > map `answers` |

Written back to `<board>.fluxmap.json`: `modules` + ports (modules), `fanout` (groups, per-part escaped/required, blocked pins), `pours`
(planned pours), `sources` (the winner per fact above), `answers`. The pipeline copies the map with each stage board.

Open connections at the end are a gate failure (`review`: UNROUTED, STALE_FILL); `closure BOARD` explains each one (pocket, walls, via sites, ranked options).
