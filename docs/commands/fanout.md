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

**Pins, not pads.** All pads of one footprint that share a pad number AND a net are one pin. Pieces that touch or overlap are one
copper component; pieces that do not touch are separate components, and a pin is escaped only when every component is escaped
(so `FANOUT_OK` counts pin numbers: `U312: 6/6`, not 7 pieces). Each component gets its own candidates: via sites and stubs leave
from any piece (the centre, and the edge of a long thin piece that faces the target).

**Same-net link.** A component with no legal escape of its own gets a short track (its layer, width = the net's track) to a
same-net pad that IS escaped: a sibling component of its own pin, or a same-net pad of its footprint or a neighbour within 2.0 mm
(`LINK_REACH`). The track must clear every other net's copper (clearance) and every copper chosen in this run. Tried after the
solver, so it never displaces an escape. Not done yet: pour-connect (`[[plan.pour]]`, verified against the filled zone) and via-in-pad
for power pins beyond the existing small-via path.

**Waivers.** `[fanout] waive = ["REF.PIN: reason", ...]` (regex on `REF.PIN`, fully matched) accepts a pin as un-escapable. Every
`REF.PIN` named in `[floorplan] waive_fail` is waived here too (one notice line each). A waived blocked pin is not a blocked pin:
the verdict is `FANOUT_WARN` (exit 0, stage WARN, the pipeline continues), the pin is printed as
`FANOUT_PIN_WAIVED REF.PIN NET ... - WAIVED - left for the router`, and the ledger lists it. The same waiver is honoured by the
modules fan-out check and the floorplan gate. Until the net has copper on that pin, `route-ready` and `review` report
`FANOUT_PIN_WAIVED` as WARN (`preroute.waived_pin_findings`). A pin not named by a waiver still FAILS.

**Reserved escapes are released when they block.** The escape reservation (`escres`) claims copper before the solve. When a pin
ends blocked and a claim of another pin lies within 2.5 mm of it, those claims are released and the solve runs again (at most two
rounds; log line `released reserved escape(s) ...`). Measured on the V1.6 board: U5.13 was sealed by the FB5.1 (+3V3) reservation
only; with it released, 811/811 escape and DRC errors are unchanged.

**Sealed or surface-only.** Each blocked component is measured: the longest free same-layer run from its pad in 8 directions
(against static copper and this run's copper). `SURFACE_ONLY` when that run is at least `[escape] surface_exit_mm` (default
1.0 mm), else `SEALED`. Verdict: sealed pins -> `FANOUT_FAIL` (`FANOUT_PIN_BLOCKED ... [SEALED, free run X mm]`); surface-only pins
only -> `FANOUT_WARN` (`FANOUT_PIN_SURFACE_ONLY REF.PIN NET free run X mm - left for the router`). With `--allow-incomplete` the
pipeline continues past a FAIL that is only sealed pins (no DRC increase, no crash); the ledger records it.

Output: `FANOUT_OK` / `FANOUT_WARN` / `FANOUT_FAIL`, escaped / required per part, one `FANOUT_PIN_BLOCKED part.pin net blocker distance` line per
failed pin (blocker = the pad, board edge, keep-out or the neighbour whose escape took the room), the kicad-cli DRC (zones refilled)
before and after: the fan-out must add no error (`hole_to_hole` counts even when the board makes it a warning). `--check-only` writes nothing. `<out>.fanout.json` holds every pin (kind, layer,
stub, via, end point, lane direction/layer: the router's start points and layer hint).
Exit 0 = FANOUT_OK. Runs under KiCad's python (pcbnew + numpy); 337 parts: about 15 s plus the DRC.
