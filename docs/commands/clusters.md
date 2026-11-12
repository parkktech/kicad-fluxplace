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
