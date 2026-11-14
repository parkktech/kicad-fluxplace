# `fanout` - real escape copper for every pin, locked

```
fluxplace fanout BOARD --constraints C [--spec S] --out O [--parts R1,R2] [--check-only] [--no-drc]
                 [--reach 2.2] [--lane 1.0] [--via-pad-gap 0.2] [--same-net-via-spacing freerouting|hole] [--track/--clearance/--via/--drill] [--full] [-q] [--top N]
```
A placement is acceptable iff the whole fan-out EXISTS. `fanout` builds it: every pin that must leave its footprint gets
DRC-clean copper (stub, via) that is locked and tagged (group `fanout:<ref>`); routing starts from those end points.

| pins | what they get |
|---|---|
| signal pins of fine-pitch parts (`escapeplan.is_fine`: >= 16 pads or pitch <= 0.65 mm, >= 3 pads) | pad-direct out of the courtyard, or dog-bone stub + via; solved per part as one problem (a pin's choice excludes what blocks its neighbour); pair legs: same kind, layer, stub length, via count |
| every pin of a net that has a plane or declared pour (GND, +5V, `[[plan.pour]]`) | shortest legal stub + via, away from the IC pin the cap decouples; exposed GND pads of `U*`: vias on a 1 mm grid inside the pad (the only vias allowed in a pad) |
| two-pad passives with both nets local, mounting hardware, through-hole plane pins | nothing |

Geometry is exact: pcbnew shapes of the board (pads, tracks, vias, holes, keep-outs incl. footprint-owned, outline + the board's
copper-edge clearance) against every candidate; candidates against each other by capsule distance; one candidate per pin by a
deterministic constraint search (MRV + forward checking, `--nodes` budget per component). An end point counts only if a free track
lane of `--lane` mm leaves it on a signal layer, also against the neighbours' chosen copper (a sealed pin is banned and the part
re-solved, `--repairs` rounds).

Vias of one net: **hole-to-hole** (drill + the board's `min_hole_to_hole`, centre to centre) is always kept, also between vias of the same net.
`[route] same_net_via_spacing` (`--same-net-via-spacing`) adds, as `"freerouting"`, via + clearance centre to centre: KiCad does not flag
that, Freerouting counts less as a pre-existing clearance violation. Default: `"freerouting"` when the route stage uses Freerouting
(`[route] router` / `--router`; the pipeline passes it), else `"hole"`. A power / ground pin with a via of its net within
`[escape] share_reach_mm` (default 1.5, 0 = off; the escape reservation and the board's own vias count) gets a short stub to it
(`kind: share`) before a via of its own is tried; the alternatives pass (`fanalt`) does the same first.

Output: `FANOUT_OK` / `FANOUT_FAIL`, escaped / required per part, one `FANOUT_PIN_BLOCKED part.pin net blocker distance` line per
failed pin (blocker = the pad, board edge, keep-out or the neighbour whose escape took the room), the kicad-cli DRC (zones refilled)
before and after: the fan-out must add no error (`hole_to_hole` counts even when the board makes it a warning). `--check-only` writes nothing. `<out>.fanout.json` holds every pin (kind, layer,
stub, via, end point, lane direction/layer: the router's start points and layer hint).
Exit 0 = FANOUT_OK. Runs under KiCad's python (pcbnew + numpy); 337 parts: about 15 s plus the DRC.
