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

Sequencing (round 3).  `fanout` is pair-aware: it first plans every pair on the placement (`pairs.reserve_for_fanout`), leaves
the pads of every planned pair to this stage (no generic stub / via) and treats the planned exits as copper (both corridor ends
for `reserve_mm`, the via pairs near them, new dog-bone exits); Opt-in: `[pairlay] reserve_exits = true` (default false: the generic fan-out, which escapes more non-pair pins beside pair pins).
A net that is complete with locked copper is never fanned out (`fanout.owned_nets`).
Negotiation (`pairneg.py`): every section is searched alone in `workers` processes, overlapping paths are negotiated with
present + history cost (only the pairs in a conflict are searched again, the most constrained keeps its path), then the pairs
are emitted one by one, each seeing the corridors of the pairs still to come.  A failed-alone search gets one retry at 4x budget.
Exits: pads without a coupled exit get a new dog-bone via pair (`dogbone`); a terminal with its pads on two layers gets one
via on the odd pad (connected, but reported as a mismatch: the legs cannot have one layer sequence).  A pad without a partner
pad is a question (`pair-pads:<net>`, exit 3 after the board is written), not a silent failure.

    [pairlay]
    negotiate = true   negotiate_passes = 8   workers = 8   dogbone = true   reserve_exits = false   reserve_mm = 3.0
    inner_cost = 1.15   passes = 0   via_exit = true   nodes_per_mm = 25000   pad_max_mm = 16
    enabled = true   step_mm = 0.1   clearance_mm = 0.13   via_cost_mm = 20   turn_cost_mm = 0.4   max_nodes = 400000
    ring_mm = 1.0

The route stage routes open connections only, and never rips locked copper: a complete pair is not routed again.

## Escape reservations in the placer (`escreserve.py`)
`legalise.legalise(reserve=escreserve.for_legalise(B, cons, spec, movable))`, used by `place --strategy routable`:
fine-pitch (pitch <= `[escape] fine_pitch_mm`, 0.65) and multi-row parts keep a HARD band for the first escape row
(via + 2 clearances + track) and a SOFT band behind it; `escres` pin sites and (opt-in) pair corridors are soft.  Hard = a
keep-out for other parts; soft = a part is moved out when a legal seat exists within `[escape] reserve_cost_mm` (3).
`[escape] reserve = false` turns it off.

## Completion ladder (`pairfb.py`): every pair leaves the stage connected or handed over, in one reported state
A pair the main pass (negotiation, order passes) does not complete is never left half routed: its partial copper is not written
(`[pairlay] keep_partial = true` brings the old behaviour back).  Then, on the written board, per pending pair, each step as a trial
on a copy that is kept only when it completes a pair and every pair it ripped is complete again (`fallback = true`, `fallback_trials = 12`):

| state | meaning |
|---|---|
| `a` | complete and coupled by the main pass (also: already connected) |
| `b(i)` | another via / layer pattern: cheaper vias, inner layers at face cost, a 3x / 5x search budget, re-ordering passes |
| `b(ii)` | after a rip: single-ended fan-out stubs the failure line names (a short stub touching one pad, never a rail / plane net; their pins are escaped again by `fanout --nets`; `rip_fanout_stubs = false` turns this off) or the copper of an earlier LOWER-priority complete pair (both are laid again) |
| `b(iii)` | coupled, with a bounded uncoupled break-out at the terminals (`[pairs.FAMILY] max_uncoupled_mm`, else `[pairlay] max_uncoupled_mm`, default 5 mm = 10 pitches of a 0.5 mm part) and the skew allowed up to its own limit (`skew_frac` 1.0 instead of 0.85) |
| `c` | none worked: both nets go to the general router as ordinary nets of the pair's class (`handoff` in `board.pairs.json`; `PAIR_HANDOFF` line); they are not on the router's skip list.  After routing the numbers are measured: review WARN `PAIR_ROUTED_UNCOUPLED` (`pairuncoupled.py`: skew, length, uncoupled length beside the partner, limit 2 x `max_uncoupled_mm`) |
| `a-mismatch` | connected, legs differ in layer sequence / vias (copper kept: connected beats open) |

Not done: a coupled run that goes uncoupled in the MIDDLE to squeeze past an obstacle (only the terminal break-outs are bounded-uncoupled).

For a bounded first pass, `[pairlay] max_nodes`, `nodes_per_mm` and `negotiate_passes` limit search work, and `fallback = false` skips the expensive b(i)-b(iii) trials. Pending pairs still receive state `c` and an atomic whole-pair handoff; no partial locked copper is kept. This is the appropriate way to get a complete report on a congested board before choosing between more pair work and the general router.

An exception in the stage is a `fail`, never a `pass` with the input board. A killed or timed-out stage has no complete `board.pairs.json` and is not a resumption checkpoint; rerun it with a bounded configuration rather than treating its partial board as finished work.

Monitor `W/<n>-pairs/progress.json` with `fluxplace route-status`. It carries the latest pair/negotiation message and ends `done`, `warn` (some pairs handed off), or `failed`; a writer killed without a final update is reported `dead` by the status reader.

## `pairs --only A,B`: re-route named pairs whole on a routed board (`pairsonly.py`)
```
fluxplace pairs BOARD --only A_P/A_N,CSI --constraints C --out O [--allow-shove] [--budget S] [-v]
```
Names: a pair as the audit prints it (either order), a net name, a prefix or a glob. Per pair: both legs' unlocked copper is removed
(a LOCKED item refuses that pair and is named: `refused_locked`), the pair is laid with the coupled pair layer, everything else is a
fixed obstacle. A pair already complete is not touched (`already_ok`). The new pair is accepted only if it is connected, the pair
audit grades it complete (same layers and vias, skew in limit), no other net has more open connections, and DRC shows no violation
key the board did not already have; otherwise the original copper stays exactly (`kept_original`, with the reason: blocked corridor
and the obstacles that bind it, or the failed check). The laid copper is locked, as in the pairs stage.
`--allow-shove` (off): when blocked, the unlocked copper of the NON-pair, non-RF, non-power, non-clock neighbour nets named by the
blocker report is removed, the pair is laid, and the nets it opened are reported (`OPENED`; not laid again, use the hand-route
tools). Off, the report lists the neighbours it could remove.
`--budget S` (default 120 x named pairs): out of time or SIGTERM / SIGINT writes the best accepted board to `--out`, removes the
temp and lock files; pairs not reached are `not_reached`. Output `--out` + `<out>.pairsonly.json`. Exit 0 all rerouted / already ok,
1 some kept / refused / unmatched, 2 stopped.
