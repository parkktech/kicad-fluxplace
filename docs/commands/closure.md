# closure BOARD

Read-only. Why each open connection is open and what it takes to close it (`fluxplace/closure.py`, geometry `pocket.py`).

`fluxplace closure BOARD [--constraints C] [--spec S] [--json] [--top N] [--track W --clearance C --via D]`
Output: one block per connection, best option first; full report `<board>.closure.json` / `.txt`.

Open connections: pcbnew after a fresh zone fill (`opengate.measure`), one per minimum-spanning edge between the pad clusters of a net.

Per pad (both ends), on a 0.05 mm raster of a `window_mm` (5.0) half-size window at the design clearance (the larger of the board's default
class and the fab floors, or `[closure] track_mm / clearance_mm / via_mm`):
- **pocket** per layer the pad is on: flood-filled free area in mm2, `OPEN` when it reaches the window edge; for a closed pocket the **walls**: nets (or
  pad / keep-out / edge) touching it with class (pair, rf, clock, switch_node, plane, low-speed) and wall length;
- **via sites** within `radius_mm` (3.0): through vias that fit on every layer, outside every pad, reachable from the pad by a track; count and nearest 3;
- reachable same-net copper of the other cluster (pad, track, via, pour that belongs to it).

Options (each is legal on the grid by construction), ranked by cost, cheapest first:

| # | type | what | cost |
|---|---|---|---|
| 1 | track | short same-layer track to same-net copper | length |
| 2 | via | via at a site to a same-net pour, or to a free path on another layer (`escape`: no same-net copper in the window, both pads must escape) | length + 3 |
| 3 | relay | re-lay a minimal, named set of low-speed segments/vias walling the pad in, then 1 or 2 (incremental unlock + prune) | + 8 per item |
| 4 | nudge | move/rotate a 2-3 pad SMD part <= `nudge_max_mm` (1.0): `legality.Legal` poses, support-distance limit kept, attached copper counted | + 10 + 5/mm |
| 5 | pair | as 3 through controlled copper: always "NEEDS OWNER DECISION", with the controlled mm and vias it touches | 1000+ |

3-5 are only searched when 1-2 find nothing at that pad. **Not done: the quick router probe** (a relay is a geometric unlock, not a routed one).
Limits: pads are exact for rect / oval / circle (roundrect as rect); cluster membership comes from the text connectivity (`exact_clusters` false in the
JSON when it disagrees with pcbnew and every same-net item is the target); window-bound (same-net copper outside it is "escape").
`[closure]`: `window_mm radius_mm step_mm nudge_max_mm max_trials track_mm clearance_mm via_mm`.
