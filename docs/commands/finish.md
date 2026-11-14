# finish

Route a **scope** with Freerouting and keep only the copper that scope asks for.

```
fluxplace finish --board B --out O --area-of U12 --margin 2
fluxplace finish --board B --out O --nets ETH_P0_N ETH_P0_P --refs 'U1*,T3'
fluxplace finish --board B --out O --area 7,38,13,46 --rip
fluxplace finish --board B --out O --selection scope.json      # {"nets":[..],"refs":[..],"area":[x1,y1,x2,y2]}
```

## Scope (intersection of what is given)

| flag | meaning |
|---|---|
| `--nets A B REGEX` | net names; a token that is an existing net name is taken literally, otherwise a regex (full match) |
| `--refs GLOB[,GLOB]` | nets touching these parts (globs) |
| `--area X1,Y1,X2,Y2` | mm, board coordinates |
| `--area-of REF[,REF]` | bounding box of those parts' courtyards (pads if none), grown by `--margin` (default 2 mm) |
| `--selection FILE.json` | the same three keys in a file |
| `--rip` | allow ripping the in-scope nets' copper inside the area (nothing else is ever ripped) |

At least one of nets / refs / area is required.

## Semantics

- Freerouting sees the whole board (existing copper is wiring, so it is an obstacle).
- With an area, the DSN keeps only the pins of in-scope nets that lie inside it, so only in-area connections are asked for.
- Only the **new** copper of in-scope nets is copied back. A net whose new copper leaves the area is dropped
  and listed under `dropped`.
- Connections counted "in scope": open clusters of in-scope nets whose pads all lie in the area (with an area).

## Protection proof (built in)

Every track, via and zone outline of the input is fingerprinted (net, layer, geometry at 0.1 um). The output must
hold every input item except those deliberately ripped, and every added item must be copper of an in-scope net
inside the area. Any other difference: the command FAILS, the output is moved to `<out>.rejected.kicad_pcb`, the
violations are printed, and the input is untouched. The check reads the two files; it does not trust the router.

## DRC guard

The result is kept only if DRC violations do not rise and unconnected items do not rise
(`kicad-cli`, the board's `.kicad_pro`). Otherwise `<out>.finish.kicad_pcb` is left for inspection.

## Report

`scope`, `connections in scope` (open before / still open), `copper added` (tracks, vias, mm, nets),
`dropped`, `left open` with the `route-diagnose` causes, `protection: OK (hash ...)`.

## Not done

- The builtin router (`patch`/batchroute) is not scope-aware; `finish` is Freerouting-only.
- `route` is unchanged; use `finish` for scoped routing.
- A connection whose only route needs copper outside the area stays open (reported, not forced).

## Proof run (utv-comms-v15 copy, ETH PHY nets cut)

Input: `v15` with the tracks of ETH_P0_N, ETH_P0_P, ETH_P1_N, ETH_P1_P deleted (`cut`). DRC (10 violations in v15 already).

| run | in-scope open before to after | copper added | DRC viol / unconn | protection |
|---|---|---|---|---|
| `--area-of U12 --margin 2` | 9 to 4 | 8 tracks, 3.7 mm | 10 / 8 (kept) | OK, 0 removed |
| `--area-of U12 --margin 15` | 14 to 4 | 78 tracks, 4 vias | 12 / 4 (REJECTED by DRC) | OK |
| `--nets ETH_P0_N ETH_P0_P ETH_P1_N ETH_P1_P` | 16 to 6 clusters (3 unconnected pads) | 66 tracks, 5 vias, 129 mm | 10 / 3 (kept) | OK, 0 removed |

The 4 cut nets do not close inside the 2 mm area: their other pins (T3, J10) are outside it, and U12.7/U12.9 are
sealed by a GND track that is out of scope. `--nets` (no area) closes them.
