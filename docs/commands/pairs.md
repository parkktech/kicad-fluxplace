# `pairs` - differential pairs laid by construction, locked, before routing

```
fluxplace pairs BOARD --constraints C [--spec S] --out O [--plan-only] [--nets A,B] [-v] [--full] [-q] [--top N]
```
Constructive flow, after `fanout`, before `plan-route` / route (`pipeline` stage `pairs`; it never blocks: WARN).
Which pairs: the net classes' partner links (`netclass`: `_P/_N`, `DP/DM`, `[pairs.FAMILY]`).  Order: project net-class
priority, pair kind, shortest span.  Sizes: the pair's `.kicad_pro` net class (`diff_pair_width`, `diff_pair_gap`, via,
clearance with a 0.127 mm floor); `[pairs.FAMILY] width_mm / gap_mm` override; never thinner than the class.

A pair is a tree of SECTIONS between terminals (the P pad and the N pad on one part, or side by side on two parts), so series
parts (AC caps, ESD arrays, chokes, magnetics) split it into pad-to-pad sections.  Per section:

1. centre-line search on a clearance-inflated grid (`step_mm`): 45 degree moves, a turn every K cells at most, signal layers
   only (never a plane layer), layer change only as a matched via pair across the direction of travel (`via_cost_mm` each);
2. both legs = the mitred parallels at +-(width + gap)/2; short uncoupled break-outs at the pads (dog-leg, else a grid-routed stub);
3. skew: lengths are equal by construction except corners and break-outs; the short leg gets 45 degree bumps (away from its
   partner) until the pair is inside 1/4 of its limit (`[pairs.FAMILY] skew_mm`, `[si] default_skew_mm`);
4. every piece is checked exactly against foreign copper, then written `(locked yes)`; the fan-out copper of a laid pair is
   replaced, of an unlaid pair kept unless the new copper fouls it.

Output: `pairs: PAIRS_OK | PAIRS_WARN - n/total complete`, per pair length P / N, skew, vias, layers (measured by the pair
audit on the written board), and one `PAIR_NOT_LAID pair [status] reason; blocked by <objects>` line per pair that stays with
the router (never dropped).  `<out>.pairs.json` holds every section.  Exit 0 = PAIRS_OK, 1 = PAIRS_WARN.
`--plan-only` writes no copper: `<board>.pairs.plan.json` holds the centre-line corridors (`escreserve.py` turns them into
soft placement reservations with `[escape] reserve_pairs = true`).

Layers: outer layers and every inner SIGNAL layer (`pairs.pair_layers`: an inner layer is a plane when one netted zone covers
the whole board, >= 90 % of the outline; a partial fill leaves it a signal layer; `[preroute] signal_layers / plane_layers`
override).  An inner layer costs `inner_cost` x length, so short pairs stay outside and a blocked pair goes inside through
matched via pairs.  Geometry per layer: the class width / gap; on an inner layer, with `[pairs.FAMILY] impedance_diff` and a
stackup in the board, the width `impedance.py` solves for the class gap (never thinner than the class).  No target = class
geometry on every layer (the report's `geometry` says which applied).
Exits: a terminal whose pads have fan-out stub + via copper may leave through that via pair on any signal layer when the pads
have no coupled exit (`via_exit`; the stub and via are kept).  Necks are not used: full class width everywhere.
Order: a pair that fails on copper this stage laid is moved ahead of the pair that laid it and the order is laid again
(`passes`, prefix results reused); the best result (pairs complete, then total skew) is kept, so a pair is never ripped for nothing.
Skew limit (`constraints.skew_limit_source`, shown per pair): `[pairs.FAMILY] skew_mm` > `[si] skew_mm_by_kind` > low-speed
(audio) pairs `[si] audio_skew_mm` (10) > `[si] default_skew_mm` (1).  P and N pads up to `pad_max_mm` apart form one terminal
(uncoupled stubs to a merge point).  Search budget = `max_nodes` + `nodes_per_mm` x span; `-v` prints time per pair.

    [pairlay]
    inner_cost = 1.15   passes = 3   via_exit = true   nodes_per_mm = 25000   pad_max_mm = 16
    enabled = true   step_mm = 0.1   clearance_mm = 0.13   via_cost_mm = 20   turn_cost_mm = 0.4   max_nodes = 400000
    ring_mm = 1.0

The route stage routes open connections only, and never rips locked copper: a complete pair is not routed again.

## Escape reservations in the placer (`escreserve.py`)
`legalise.legalise(reserve=escreserve.for_legalise(B, cons, spec, movable))`, used by `place --strategy routable`:
fine-pitch (pitch <= `[escape] fine_pitch_mm`, 0.65) and multi-row parts keep a HARD band for the first escape row
(via + 2 clearances + track) and a SOFT band behind it; `escres` pin sites and (opt-in) pair corridors are soft.  Hard = a
keep-out for other parts; soft = a part is moved out when a legal seat exists within `[escape] reserve_cost_mm` (3).
`[escape] reserve = false` turns it off.
