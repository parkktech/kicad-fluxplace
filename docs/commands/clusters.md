# `clusters`

### 2. Clusters — `fluxplace clusters BOARD [--apply --out OUT]`

For every IC (`U*`/`IC*`, >= 5 pads) the 2-pin passives, ferrites and crystals that
serve it. Ownership: a pad on a net that reaches exactly one IC; for decoupling on
a shared rail, the cap's reference-designator block (C416 -> 4xx), then the nearest
reference number (U410), then an even spread over the rail's supply pins (a guess,
marked and never graded). A capacitor on a net that also reaches a connector is
treated as rail decoupling. A rail cap is never handed to an arbitrary far IC. A
part whose two pads reach two ICs is a chain link, not support. `route-diagnose`
`FAR_SUPPORT_PART` uses the same ownership (it used to blame the nearest IC).

`--apply` re-seats only the parts beyond their limit (switch-node 6 mm, bypass
3 mm, crystal 8 mm, other 5 mm; `--all` re-seats everything, `--guess` includes
guessed owners), ICs never move. A part keeps its footprint; origin and rotation
change (pads move with it, written as text by `boardwrite.py`). Candidate seats are
ranked by pad-to-pin distance (caps add a ground-return term) and must stay out of
every other courtyard, the IC's escape lanes (1.0 mm beyond fine-pitch pads), the
board outline and track-forbidding rule areas. Existing copper is not updated:
apply on an unrouted board.

**Legal re-seat (round 2).** Every candidate seat also goes through `fluxplace/legality.py`
(`Legal.part_issues`): same-side courtyard overlaps (a through-hole part counts on both sides),
the real outline with rounded corners and cut-outs (Edge.Cuts arcs are flattened; before this only
the bounding box was tested), named keep-outs and side regions, track-forbidding rule areas (a
footprint's own rule areas are exempt), through-hole pads in no-through-hole regions, and locked /
fixed parts never move. A part with no legal seat within its limit is LEFT IN PLACE and listed
(`--best-effort` accepts a legal seat farther than the limit). After writing, `clusters --apply`
re-checks the whole board and prints the new violations against the input (target: 0).

    [placement.keepout.cm5_module]      # no part of side "B" (a through-hole part counts on both) inside
    rect = [0, 5, 55, 45]               # x0, y0, x1, y1
    side = "B"                          # "F" | "B" | "both"
    allow = ["J10", "J11"]              # refs/globs exempt
    reason = "CM5 body over the B-side sockets"
    [placement.no_tht.vib_zone]         # no through-hole pad inside (checked by route-ready too)
    rect = [10, 10, 20, 20]
