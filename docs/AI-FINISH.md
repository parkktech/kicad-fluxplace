# AI hand-finish: closing the last open nets

Procedure for an agent that finishes the last few unconnected nets of a routed board the way a layout engineer does:
look at the local geometry, decide a path (moving a neighbour if needed), lay it, check it, undo if it breaks anything.
The commands are small and deterministic; the decisions are yours. Floors (track, clearance, via, hole, edge) come from the
board's rules raised by `--constraints` ([route], [fab] profile) and are never crossed. Locked copper changes only through explicit guarded commands.

Measurement is `kicad-cli pcb drc --refill-zones` with the `.kicad_pro` beside the board. Work on copies only.

## Commands (all take `--json`; `--constraints C.toml` raises the floors)

| command | does |
|---|---|
| `open-nets B` | every unconnected connection: net, endpoints (item, x, y, layer), straight distance, rules, protected/locked, same-net islands, closest approach between the islands |
| `window B --at X,Y --size W,H` / `--net N` / `--conn I` | region as data: pads, tracks (ids, locked), vias, zones and fill pieces, keepouts, edge, per-layer free corridors for a track width, free via sites; `--png/--svg` render with a 1 mm grid; `--path` overlays a proposal |
| `probe B --net N --path P` | checks a path without writing: nearest foreign copper and margin per segment/via, crossings, keepout, edge, hole, 45/90, width/via floors, track-centred-on-via, what the ends touch. Exit 1 on violations |
| `lay B --net N --path P [--lock] (--out O \| in a session)` | applies a probed path, then DRC (no new violation key) + connectivity guard (no pad changes net, no other net opens, target net's open count falls). `--lock` locks the newly laid tracks/vias on acceptance. Writes nothing on failure and says why |
| `shove B --net M (--ids a,b \| --window X,Y,W,H) [--chain]` / `unlay` | removes UNLOCKED copper of net M and reports opened connections. Session-only `shove --allow-locked --ids ...` requires every locked item to be named, unlocks/removes it and records its full geometry in the ledger |
| `suggest B --conn I` or `--net N --from x,y --to x,y` | grid A* between two islands of the net (all layers, vias, octilinear). Returns a path only when both ends touch the intended islands on reachable layers, or the measured seal: region reached per layer and the foreign obstacles that bound it. `--relax` lists unlocked blockers; `--explore` removes/probes whole connected runs and names every item to shove; `--explore --include-locked` reports locked runs separately in `locked_explore` with net and lock origin |
| `copy-net SRC DST --net N [--lock] (--out O \| in a session)` | restores a removed net: DST's unlocked copper of N (refused if any locked) is replaced by SRC's, matched by net NAME, rebuilt on DST's own net; copied count must equal SRC's; then the `lay` DRC + connectivity guard. `--lock` locks all copied items on acceptance. Details: [commands/repair-cmds.md](commands/repair-cmds.md) |
| `lock-net B --net N [--ids a,b] [--unlock] (--out O \| in a session)` | locks/unlocks all or explicitly selected tracks/vias of N. Requires unchanged geometry, connectivity and DRC keys; supports `--expect-rev` and the KiCad-open guard, and records session edits in the ledger |
| `via-min B --out O [--budget S --report R]` | raises vias below the floor, one batch, bisected on any new DRC violation key; keeps verified changes on budget end and reports changed/kept/rejected/not-reached. Partial output exits 0. [commands/repair-cmds.md](commands/repair-cmds.md) |
| `hand-repair W --out O` | ladder steps 1-2 on a session: per open connection suggest/lay, else explore + remove the cheapest unlocked blockers + re-lay them; whole attempt undone if anything stays open; pairs reported; ends with `session commit`. [commands/repair-cmds.md](commands/repair-cmds.md) |
| `pour-island B --net N --out O` | joins an open same-net pour island with one stitching via at a legal filled-copper site, accepted only by the lay DRC + connectivity guard; no output when no site works |
| `session begin SRC --work W` / `undo` / `status` / `ledger` / `commit W --out O` | edits on W with an undo stack; `commit` requires lower unconnected, no new DRC key, protected locked geometry and no net more open. After locked re-escape it also requires every opened connection closed and all current copper of each affected net locked. Ledger: `W.session/ledger.{json,txt}` |

Path syntax: `"F.Cu: x1,y1 x2,y2 x3,y3; via x,y; In2.Cu: x3,y3 x4,y4"`. A via sits between two chunks at their shared point;
`"via x,y"` alone lays a stitching via. An exact same-net, same-size via already at that point is reused, not duplicated.
Segments are 45/90 (stubs under 0.1 mm exempt). Ids are the first 8 hex chars shown by `window`.
`lay`, `shove` and `copy-net` have `--timeout S` (default 600): timeout prints `no verdict: timed out after S s, nothing
written`, restores the destination and session, and exits 2. `hand-repair` writes its JSON report on budget end and errors.

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
5. If only locked copper blocks the pad, use `suggest --explore --include-locked`, then the explicit session re-escape in
   [ROUTING-PLAYBOOK.md](ROUTING-PLAYBOOK.md). Stop and record the measured reason when the pad has no escape, the locked
   items cannot be restored/re-laid and relocked, or a part must move. Say which pads/tracks and gap you measured.
6. `session commit W --out OUT`. If rejected, read the reasons; `undo` the last edit(s); do not commit a set that is not better.
7. Final numbers: `kicad-cli pcb drc --severity-error --format json --refill-zones OUT` before/after: unconnected and errors by
   type. No new error type.

## Limits found while building this

- The model is geometric (pads, tracks, vias, keepouts, edge, fills as refilled by pcbnew); DRC remains the judge. Custom rules
  (`.kicad_dru`) are not modelled, only checked by the lay/commit DRC.
- A through via is an obstacle on every layer; most "sealed" pockets are via-limited, not track-limited.
- Planes: a path on a plane layer cuts the plane. Pass `--layers` to keep suggestions off plane layers.
- Same-number pad pieces (a QFN's split pad) can be separate islands to KiCad: they need a short same-net link track.

## Escape-topology repair: when the pocket is sealed by your own escapes

Measured on a 6-layer CM5 carrier, 2026-11-16: KRT whole-board routing left 4 open, `suggest` found no path for any of them
even at 2.5 M nodes. Pad escapes were present. The cause was that pair and whole-route copper laid later sealed the onward
corridors. This procedure took the board from 4 open to 1 open, with no new DRC error key and no new dangling track. KiCad DRC confirmed the result independently.

1. **Find the real seal.** Run `window --conn I --png` and the pair report. "No escape copper" is usually wrong: the escape
   stub exists but a neighbour's lane closes it 1-2 mm out. Do not add a short "escape tail" gate. It passes before pairs are laid and
   gives false confidence.
2. **Free the smallest set of copper.** Remove one neighbour segment first and re-run `suggest`. If it is still sealed, remove the whole
   neighbour net (`shove --chain`). In a dense pad field, re-routing a short net whole is safer than reopening the same pocket
   segment by segment. Locked escape copper may be unlocked only as a recorded experiment in a session, never on the source
   board.
3. **Lay the blocked net first** (`probe`, `lay`). If the hand path closes the target but `lay` rejects it for opening a
   plane island (e.g. GND), give that one net to the plane-aware router (`route.py` in KRT) instead of forcing the path.
4. **Restore every removed net, reusing its old geometry.** The `shove-*.json` record holds the removed copper. Reusing an
   original via and its complete original approach to the pad is usually the only legal return in a dense pad field.
   Restore all of it. Stopping the approach at a point near the pad leaves a dangling track, not a connection.
5. **Commit with the all-severity guard.** `session commit` caught a 0.05 mm dangling stub that `status` did not show.
   Remove it and commit again. Never bypass the guard.
6. **Pairs routed this way are connected, not finished.** A pair whose legs were routed one at a time (one case measured 70 mm / 4 vias against 37 mm /
   3 vias) is quality debt. The pair stage must correct or reject it. Count it as connectivity only.

**The durable fix belongs in routing order, not here.** Negotiate and reserve complete pair corridors, from pad to pad, before
locking any escape. Route coupled and long nets before ordinary copper.

## Path rules learned from rejected `lay` attempts

- **The path must end on a layer the target pad is on.** An SMD pad on B.Cu is not reached by an In3 segment that stops under
  it. Add `via x,y` and a pad-layer segment.
- **No vertex inside a via except its centre.** A track end or bend inside the via radius (a pad end counts) is
  `track_not_centered_on_via`. Put the bend at least one via radius away, or attach the new via straight onto an existing track.
- **A via beside a fine-pitch pad is rarely legal.** Prefer an existing through via along the route over a third new via.
- **Proximity is not connectivity.** `probe` reports what each end touches. An end that touches nothing is a stub.

## Known tool gaps (open)

- `suggest` can report "no path" (or `then_found: false` under `--explore`) where a corridor exists. Since 2026-11-16 it
  accepts a path only when both ends touch the intended islands, which stopped the earlier false "probe OK" (a path ending
  on In3.Cu under a B.Cu-only pad; a path stopping 0.5 mm short of a pad). But the emitted path can still stop short of
  the pad, or lose its final via and pad-layer segment, and is then rejected. Seen on an RF net: a 4-minute explore
  returned usable candidates all marked not found. Until the path is written through to the pad centre on the pad's own
  layer, read the candidate `path` in the result and extend it by hand before treating a pocket as sealed.
- `probe` gives the size of a clearance miss but not the direction or the smallest shift that clears it.
- No command copies one net's copper from another board (used to restore removed nets); hand scripts bypass the guards.
- No command re-routes a named pair whole; `tune` cannot fix a pair whose legs differ in layers or via count.

## Packet loop (close exit 5)

`close` stops with exit 5 when packets wait for an answer. `best.kicad_pcb` is valid and never worse than its input.
Everything lives in `<work>/ai/`, one set of files per gap key (`kind:first-net`, sanitised, 80 chars).

Files:
- `<sid>.packet.json` (v2, <= 4096 bytes): `gap {id, kind, class, net}`, `board` (the board to read), `conn` (first open
  connection: ends, straight length, rules), `window` (tracks, vias, via sites near the connection), `corridors`
  (`direct` layers, `max_track_mm` per layer), `png` (paths: `<sid>.conn.png` = connection view, `<sid>.pocket.png` = 4 mm
  pad pocket with less free room), `sealed_by`, `krt` (KRT failures on the net), `tried` (earlier attempts on the gap),
  `feedback` (the last action's result, see below), `pair` (pair gaps only: legs, family, per-layer `width`/`gap`, skew limit), `actions` (the kinds).
- `<sid>.action.json`: written by the agent: `{"actions": [...], "why": "...", "tokens": n}`, at most 8 actions.
- `<sid>.feedback.json` (<= 1200 bytes): `actions` = `[{do, ok, why, probe?}]` per applied action (`probe` = first 3
  violations of a refused `path`: kind, net, margin, at), `commit`, `commit_why`, and `refused` (close's reason when the
  candidate was not accepted). The next packet for the gap carries it.
- `<sid>.swap.json`: a `swap_pins` proposal. Never applied: a pin swap changes the netlist (needs the spec and schematic).
- `stop.json`: `packets` = `[{gap_id, packet, png, action, budget_left}]` for every open gap with a packet on disk;
  `proposals` = the swap files.

Action kinds: `rip {net, ids}`, `rip_locked {net, ids}`, `nudge {ref, dx, dy, rot}`, `route {nets, layers}`,
`path {net, path, lock}` (AI-FINISH path syntax, <= 800 chars, not for hs-pair legs), `pair_path {pair, layer, waypoints}`
(2-40 points; waypoints `[x, y]` or `[x, y, "Layer"]`. A named layer other than the current one is a LAYER CHANGE at that point
(a via pair is placed there); at most 4 changes, never on the last point; each run 45/90 and >= 1.0 mm; the start pad must be on
the first layer (else refused: "pad REF.NUM is not on LAYER: start the centre line on the pad's layer"); a change where the
reference plane changes gets one return via of the reference net within `[review] return_via_mm`, or the result lists
`return_via_missing` (the review WARNs). A refusal reports `at` [x, y] and `blocker` {label, net, layer}; the feedback row keeps
them with `stripped` {items, locked}), `swap_pins {ref, pads, why}` (proposal only),
`give_up {why}`. Budget: `[ai] budget` (default 4 applied action files per gap key per work dir).

Order inside one transaction: nudges, rips, `path`s, `pair_path`s, `route`s, then `session_commit`; close's guard
(R0-R6) then judges the candidate like any rung.

Who does what:
- close (this code): writes packets, applies each action file once (`<sid>.action.used.json` after), keeps `ai_used` in `gaps.json`.
- the caller (run script or top-level model, not this code): one agent per packet. The agent may run read-only `hrcli`
  `window` / `probe` / `suggest` on the packet's `board`, looks at the `png` files, then writes `<sid>.action.json` and
  nothing else.
- Loop: run the SAME close command again on `best.kicad_pcb` with the same `--work`. It applies each pending action file as
  one guarded transaction and resumes. Repeat until the exit is not 5 or no packet has budget left. A packet whose action
  file is pending is not rewritten (the pending action is applied first).
