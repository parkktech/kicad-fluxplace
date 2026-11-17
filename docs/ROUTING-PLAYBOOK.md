# Routing playbook: what a run should do, learned from a full route-and-repair session

Read this before starting a placement or routing run. It is board-agnostic. It was learned on a 6-layer compute-module
carrier (2026-11-16) that went from 4 open connections to 0 by guarded repair, and was left with pair and RF defects that
came from placement and stage order. Commands for the repair part are in [AI-FINISH.md](AI-FINISH.md).

## Rules that held every time

- KiCad DRC after a zone refill is the judge. A router's exit code or summary is not.
- Zero open connections is not done. Done = zero opens, DRC errors dispositioned, pairs matched, RF in spec, parts
  resolved, fab review passed. `krt-whole` reports `warn` with `not_done` until pairs and RF are in tolerance.
- Change one thing per run, measure, log it, and never retry what the log marks rejected.
- Work on a scratch or session copy. Accept a change only if unconnected falls, no new DRC key appears and no other net opens.
- Never bypass a guard. Every guard rejection in that session was a real defect.
- Keep the last good board as the fallback until a new one measures better on opens, DRC, pairs and RF.
- A longer router timeout does not close the last opens. They are local escape or placement defects.

## Order of work for a new run

1. Resolve every part first (stock, datasheet, model, pins). No layout with unresolved parts.
2. Write the approved stackup and the fab via and hole rules into the board before any routing. Impedance figures taken
   against the wrong stackup are wrong (one RF net read 66 ohm, then 52 ohm once the stackup was applied).
3. Place functional groups with their breakout regions ([BREAKOUT.md](BREAKOUT.md)). Check each channel that RF nets or
   pairs must pass: tracks needed against tracks that fit at the required width and clearance.
4. Route RF nets first: required outer layer, the 50 ohm width from the stackup, the via limit, then lock them. Put ground
   stitching vias in afterwards, or keep them out of RF corridors.
5. Route differential pairs next, both legs together, pad to pad. Lock escapes only after the pair corridors exist.
6. After each stage that locks copper, check that every still-open connection can still be reached. If one is sealed, rip
   up the copper that sealed it then, not at the end.
7. Run the whole-board router for the rest (KiCadRoutingTools through `--router krt-whole`; pin and record its version).
8. Repair what is left with the ladder below.
9. Snap via centres and bring every via up to the fab minimum, accepted per via.
10. Tune, silk, review, fab.

## Repair ladder (stop at the first step that works)

1. Re-route the blocked net alone (`suggest`, `probe`, `lay`).
2. Remove the smallest neighbour (one segment, then the whole net), route the blocked net first, restore the neighbour.
   Reuse the neighbour's old vias and its full approach to the pad.
3. A pair with one open leg: remove both legs and route them together. A routed partner acts as a wall.
4. Nets that share a channel: solve them together and try both orders. One order can fail where the other fits.
5. Move a via that is in the way.
6. Move a part a small distance, collision-checked. Record the move as a design decision and put it back into the
   placement source.
7. Stop and record a blocker with the measured reason. Do not add a via or relax a rule to force it.

Solve the net with the fewest options first, not the shortest one.

## Known limits (state on 2026-11-16)

Not built yet; a run has to do these by hand or accept the cost:
- Steps 2, 4, 5 and 6 of the order above are not enforced by the pipeline. The pairs stage runs before the whole-board
  router, but pair corridors are not reserved during placement and RF nets are not routed first.
- Channel capacity is not checked at placement.
- Pairs that the router laid one leg at a time could not be fixed afterwards: length tuning cannot fix legs that differ in
  layers or via count, and a fresh pair route came out worse. Get pairs right before ordinary copper goes in.
- The RF via limit is a warning and is not counted in `not_done`. Check via counts on RF nets yourself.
- The external pair router may not recognise a pair by its net names. Compare the pairs it reports with fluxplace's list.
- There is no cross-board corpus manifest by default (`~/.config/fluxplace/corpus.toml`), so a change proven on one board
  is not proven in general until one is set up.
