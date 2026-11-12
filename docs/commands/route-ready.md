# `route-ready` - the pre-route gate

### 8. One objective, hard budgets, side/height/connector/stale checks

* **Single score** (`preroute.score`, printed by `route-ready`, minimised by `routable`):
  class-weighted ratsnest mm + 20 x mm over budget on rf/switch_node/bypass/pair nets
  (HARD) + 3 x mm over budget on other nets + 5 x mm support parts are beyond their limit
  + 500 x each PIN_ESCAPE_BLOCKED / KEEPOUT_TRAPPED / CONNECTOR_BLOCKED / HEIGHT_REGION FAIL
  + 25 x congestion overflow + 0.05 x crossings + 300 x ANCHOR_MISORIENTED
  + 0.5 x block bounding-box half-perimeters (when blocks are defined). Waived findings do
  not count. `routable` scores the live board after every round, keeps the best legal state
  (the input counts), stops on plateau (< 1 % better over 2 rounds), `[placement]
  time_limit_s` (600) or `max_rounds` (6), and never returns a worse board than the best seen.
* **Hard vs soft**: HARD classes pull 3x in the placer and cost 20 vs 3 per mm in the score; a
  round that breaks one is simply not the best state. (No per-move rejection.)
* `[placement.side]` `default`, `override` (ref/glob -> side), `flip_cost` (score units): `SIDE_POLICY` WARN
  for parts off their side and for support parts on the other side of their IC unless within 1 mm of one of
  its pins. The text writer cannot mirror a footprint, so side flips go through pcbnew (`fluxplace/flip.py`):
  `geom.flip` is the same flip on the text model (left/right about the origin, angle -> 180 - angle,
  pad layers swapped, courtyard mirrored), and when `place --strategy routable --refine` keeps a flip it hands
  the refs to a helper (`flip_helper.py`, run by `/usr/bin/python3` where `pcbnew` lives; override with
  `FLUXPLACE_KICAD_PYTHON`) that flips them with `Footprint.Flip` and splices only those `(footprint ...)` nodes
  into the board text. A flip is tried only when `flip_cost` is set and is kept only when the gate score drops
  by more than that; through-hole parts, parts matching `override`, and parts with > 4 pads are never flipped.
  The CLI re-parses the result and prints the mismatches between the model, the file and pcbnew's own pads,
  layers, angle and courtyard (0 on all 323 SMD parts of the 337-part test board; courtyard agrees to 0.05 mm).
  `[[placement.height_region]]` (`name`, `rect`, `side`, `max_height_mm`) + `[placement.heights]`
  (ref/glob -> mm): `HEIGHT_REGION` FAIL above the limit, WARN for unknown height. STEP bounding
  boxes are NOT read; heights must be given.
* Blocks: `spec["blocks"]` / a component `block` field / `[placement] block_refdes = true`.
* Regulator clusters rank support parts: switch node, input-cap loop, bootstrap, then
  feedback/output (`clusters` prints the lengths achieved).
* `ANCHOR_SPACING` (WARN): a budget that cannot be met because every part on the net is
  locked / `[lock.at]` / `[placement] fixed`; names the anchors and the minimum achievable length.
  Support-part distance is measured from the edge of the 0.6 mm escape halo of fine-pitch ICs.
* `CONNECTOR_BLOCKED`: edge connectors (`[placement.connectors] J12 = {opening = "-y"}` in
  footprint coordinates, else inferred from courtyard-vs-pad asymmetry, WARN only when inferred)
  must open outward across the nearest edge and a `plug_mm` (15) box projected outward must
  hit no part.
* `STALE_COPPER` (WARN): tracks/vias with an end that lands on no pad, via or track of their net
  (to a fixpoint); `route-ready --strip-stale [--out]` and `place --strategy routable --strip-stale`.

### 6. Pre-route gate — `fluxplace route-ready BOARD [--constraints TOML] [--spec JSON] [--json OUT] [--all]`

One-screen scorecard (class-weighted ratsnest length, counts per code, worst 10); `route-ready` alone answers "is this placement legal and ready to route" (legality is `PLACEMENT_ILLEGAL`),
exit 1 on any FAIL. Same checks run inside `review` as FAIL codes
(`[preroute] enabled = false` turns that off) and `[review] waive = ["CODE:REGEX"]`
waives them. `route`, `auto` and `compact` print the scorecard and refuse to
start routing on a failing placement unless `--force`.

| code | level | number vs threshold |
|---|---|---|
| NET_OVER_BUDGET | FAIL | MST length of a net vs its class budget (pairs judged per net) |
| SUPPORT_PART_FAR | FAIL | confidently-owned support part pad-to-pin vs limit (clusters) |
| ANCHOR_MISORIENTED | FAIL | anchor > `anchor_tolerance_pct` (15) worse than its best rotation |
| PIN_ESCAPE_BLOCKED | FAIL | fine-pitch (<= 0.5 mm) pin with a foreign pad / courtyard / rule area / copper in its outward lane, lane = track wide and track + clearance long (project net class) |
| KEEPOUT_TRAPPED | FAIL | pad inside a track-forbidding rule area (a rule area named after the part, e.g. an antenna's own, is skipped) |
| CONGESTION | FAIL | coarse global route: 2 mm cells, each non-rail net's MST as L-shapes on the signal layers (layers without a plane pour; override `signal_layers`), capacity = layers x track slots x `utilisation` 0.7, derated by pads and rule areas; overflow > `max_overflow` (0) |
| PLACEMENT_ILLEGAL | FAIL | one finding per kind: same-side courtyard overlap (`[placement] allow_overlap = [["H200","MH1"]]` for intentional ones; a through-hole part counts on both sides), courtyard outside the real outline (rounded corners; edge connectors from `[placement] edge_mounted`, `[placement.connectors]` or a J/P/CN/X part touching the outline may overhang by their mating side only, their pads must be inside), part in a forbidden region (`[placement.keepout.NAME]`, `[[keepout]]`, rule areas), through-hole pad in `[placement.no_tht.NAME]`. Scores 500 per violation. |
| RATSNEST_CROSSINGS | WARN | MST edges of different nets that cross |

    [preroute]
    cell_mm = 2.0   utilisation = 0.7   max_overflow = 0   signal_layers = 4
