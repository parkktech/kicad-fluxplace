# The constructive flow: modules -> floorplan -> fan-out -> route

Why. A statistical planner (`plan-route`) predicted routing from a placement and was right 7-27 % of the time on the real
board: the failures were local and order dependent (fine-pitch pins that could not escape, power pins with no via site, pins
sealed by the router's own earlier copper), never open channels. So the flow does not predict the local part, it BUILDS it:
every pin gets real, DRC-clean escape copper before routing, and that copper is locked. No calibration, no confidence number.

| step | command | what its gate proves |
|---|---|---|
| density | `density` | there is room: utilisation per side/region, growth needed for a target (monotonic in growth) |
| modules | `modules` | every IC has its supports as one rigid block; ports per side; each block's own fan-out exists in isolation |
| floorplan | `floorplan` | blocks are legal and none sits on another's escape copper; wire length / crossings; fan-out re-proved on the whole board; minimal outline with `--size` |
| fan-out | `fanout` | FANOUT_OK: every required pin has stub/via and a free lane; DRC added nothing; items locked |
| plan-route | `plan-route` | channels only (WARN): overflow between the ports |
| route | `pipeline` route stage | starts every net at its fan-out end point; locked copper is never ripped; order: controlled pairs, RF, clocks, then the rest; every batch audited |
| audit / silk / review | | unchanged |

Order in `pipeline`: modules -> floorplan -> fanout (gate FANOUT_OK) -> plan-route -> route -> audit -> silk -> review.
What it does not prove: that the channels between the end points are wide enough (that is `plan-route`'s job, and routing's).
A fan-out that passes in isolation can still fail at the site: the floorplan therefore claims each module's escape copper and
re-runs the whole-board fan-out after every search.
