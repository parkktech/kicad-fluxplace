# AI hand-finish: closing the last open nets

Procedure for an agent that finishes the last few unconnected nets of a routed board the way a layout engineer does:
look at the local geometry, decide a path (moving a neighbour if needed), lay it, check it, undo if it breaks anything.
The commands are small and deterministic; the decisions are yours. Floors (track, clearance, via, hole, edge) come from the
board's rules raised by `--constraints` ([route], [fab] profile) and are never crossed. Locked copper is never changed.

Measurement is `kicad-cli pcb drc --refill-zones` with the `.kicad_pro` beside the board. Work on copies only.

## Commands (all take `--json`; `--constraints C.toml` raises the floors)

| command | does |
|---|---|
| `open-nets B` | every unconnected connection: net, endpoints (item, x, y, layer), straight distance, rules, protected/locked, same-net islands, closest approach between the islands |
| `window B --at X,Y --size W,H` / `--net N` / `--conn I` | region as data: pads, tracks (ids, locked), vias, zones and fill pieces, keepouts, edge, per-layer free corridors for a track width, free via sites; `--png/--svg` render with a 1 mm grid; `--path` overlays a proposal |
| `probe B --net N --path P` | checks a path without writing: nearest foreign copper and margin per segment/via, crossings, keepout, edge, hole, 45/90, width/via floors, track-centred-on-via, what the ends touch. Exit 1 on violations |
| `lay B --net N --path P (--out O \| in a session)` | applies a probed path, then DRC (no new violation key) + connectivity guard (no pad changes net, no other net opens, target net's open count falls). Writes nothing on failure and says why |
| `shove B --net M (--ids a,b \| --window X,Y,W,H) [--chain]` / `unlay` | removes UNLOCKED copper of net M; reports the connection it opened. Refuses (and names the items) if any selected copper is locked |
| `suggest B --conn I` or `--net N --from x,y --to x,y` | grid A* between two islands of the net (all layers, vias, octilinear). Returns a probed path, or the measured seal: region reached per layer and the foreign obstacles that bound it. `--relax` lets the search cross unlocked foreign copper and lists them as blockers; `--explore` re-runs with each limiting unlocked obstacle (and pairs) removed |
| `session begin SRC --work W` / `undo` / `status` / `ledger` / `commit W --out O` | edits on W with an undo stack; `commit` accepts the whole set only if vs the start: unconnected is lower, no new DRC key, no locked copper changed, no net more open, pad nets unchanged. Ledger: `W.session/ledger.{json,txt}` |

Path syntax: `"F.Cu: x1,y1 x2,y2 x3,y3; via x,y; In2.Cu: x3,y3 x4,y4"`. A via sits between two chunks at their shared point;
`"via x,y"` alone lays a stitching via. Segments are 45/90 (stubs under 0.1 mm exempt). Ids are the first 8 hex chars shown by `window`.

## Procedure

0. `session begin BOARD --work W`; `open-nets W`. Order the connections cheapest first (shortest `straight_mm`, no locked
   copper, island pair known). Read `W.session/ledger.txt` before every attempt; never repeat an idea it lists as rejected.
1. Frame it: `window W --conn I --png f.png` (and `--at` on the pad pocket at higher zoom). Look at the PNG.
2. Direct corridor on some layer (`connection_view.direct_corridor_layers`)? `suggest --conn I`, or draw the path yourself; `probe`; `lay`.
3. Else a via pair: `suggest` already searches layers and via sites. A pocket with 0 via sites and no corridor is "sealed".
4. Sealed: read `sealed_by` (the limiting obstacles) or run `suggest --relax` / `--explore`. Choose the cheapest unlocked obstacle
   (fewest segments, shortest re-lay, least critical net): `shove --ids ... --chain`. Lay the blocked net FIRST (`probe`, `lay`),
   then re-lay the shoved net (`open-nets`, `suggest --conn` on the opened connection, possibly on another layer). A shoved net
   whose connection is not re-closed fails `commit`.
5. Stop and record the measured reason when only locked copper blocks it, the pad has no escape at all (reached region too
   small for a via and every bounding obstacle locked or too costly), or the re-lay of the shoved net has no path. Say which
   pads/tracks, the gap you measured vs the gap needed, and the change that would free it ("move track T of net M 0.2 mm",
   "re-escape pad P", "part must move").
6. `session commit W --out OUT`. If rejected, read the reasons; `undo` the last edit(s); do not commit a set that is not better.
7. Final numbers: `kicad-cli pcb drc --severity-error --format json --refill-zones OUT` before/after: unconnected and errors by
   type. No new error type.

## Limits found while building this

- The model is geometric (pads, tracks, vias, keepouts, edge, fills as refilled by pcbnew); DRC remains the judge. Custom rules
  (`.kicad_dru`) are not modelled, only checked by the lay/commit DRC.
- A through via is an obstacle on every layer; most "sealed" pockets are via-limited, not track-limited.
- Planes: a path on a plane layer cuts the plane. Pass `--layers` to keep suggestions off plane layers.
- Same-number pad pieces (a QFN's split pad) can be separate islands to KiCad: they need a short same-net link track.
