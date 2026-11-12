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
