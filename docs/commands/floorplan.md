# `floorplan` - place modules, prove the fan-out, size the board

```
fluxplace floorplan BOARD --constraints C [--spec S] --out O [--grow L,R,T,B] [--size] [--max-grow 10]
                    [--iters 4000] [--rounds 3] [--seed 1] [--start both|construct|source] [--step 1.0]
```
Places the modules of `fluxplace modules` as rigid blocks (translate, rotate 0/90/180/270; side flips are not searched) on the
outline; fixed modules are held. Hard: every member legal by `legality.Legal`, and no block sits on another block's claimed
fan-out copper (the isolated fan-out geometry of each module). Then: weighted wire length (class weights; RF and pairs shortest;
penalty over the net budget; plane/pour nets excluded), port-to-port crossings, compactness. Search: constructive placement in
order of connection to what is placed (cheapest legal pose on a grid around the connected ports), then annealing (translate,
rotate, swap, jump to the connected centroid; fixed `--iters` and `--seed`: deterministic); `--start both` also anneals from the
source poses and keeps the better. After every search the WHOLE-board fan-out is re-run; modules owning a blocked pin get a halo
(+0.4 mm a round) and are searched again (`--rounds`). Pass = FANOUT_OK, no new legality violation.

`--size`: when a size fails, one edge grows 1 mm at a time (edge order: crowded density cells per mm of edge), up to `--max-grow`
per edge; fixed parts within 1.5 mm of a grown edge ride with it; plane-size zones grow with the outline. One line per size with
the first failing reason; the last line is the minimal passing size. `<out>.floorplan.json` has every size.
Channel overflow (plan-route) is reported by `plan-route` on the result, not searched. Metrics printed: RF_EXT length, pair spans.

## Pinned-part conflicts (runs first; `fluxplace/pins.py`)
Two parts the placer may not move (`[lock.at]`, locked, `[placement] fixed` / `anchors`, mounting hardware, group centres; minus
`[floorplan] movable`) can collide with each other (a `fixed` part keeps its input position, a `[lock.at]` row puts another part
on it; a bottom standoff's hole lands in a top connector's courtyard). The test is `legality.Legal` (courtyard per side, pad
short, drilled holes through BOTH sides). Before any search the lower-precedence pin yields:

| rank | pin | |
|---|---|---|
| 0 | `[lock.at]`, locked footprint | explicit coordinate |
| 1 | pinned part in a hard declared `[[intent.edge]]` | hard intent |
| 2 | `[placement] fixed` / `anchors` | inherits the input position |
| 3 | mounting hardware, group centres | |

A rigid unit (`[[intent.group]]`, rigid unless `rigid = false`; `[placement.group]`) moves as ONE body with the best rank of its
members, so a socket and its standoffs follow the socket's pin. The loser is released (out of `fixed` / `anchors` / `[lock.at]`,
into `[floorplan] movable`) and seated at the nearest legal pose (same rotation and side, hard intent kept; a unit against the
outline first slides along it, up to 25 mm). The floorplan starts there and a `stay` cost (`[placement] pin_stay_weight`,
default 100 per mm) keeps it near. Equal ranks: rank 1..3 the unit with the smaller displacement moves; rank 0 (two explicit
coordinates) stops the stage with `PIN_CONFLICT` (overlap geometry + the three smallest legal moves of each) unless
`[placement] resolve_pin_conflicts = "auto"` (then the smaller displacement moves, recorded). Decisions go to the log
(`pins: PIN_RELEASED ...`), `<out>.pins.json` (travels with the board) and `review` (`PIN_RELEASED` WARN, `PIN_CONFLICT` FAIL).
Make a decision permanent by updating the lock. A released `[lock.at]` row also shows as `LOCK_MOVED` in review (the lock file is
unchanged by design). Mounting-hardware-only modules stay held at their re-seated pose.


## Breakout regions and gate
Each dense part's breakout region is kept free of other modules' pads and holes and of other dense parts' regions; a cost term prefers
orientations whose breakout faces open board. The run ends with the whole-board breakout gate (`BREAKOUT_PIN_BLOCKED`, exit 1;
not waived by `--allow-incomplete`). See [../BREAKOUT.md](../BREAKOUT.md).
