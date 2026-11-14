# Board audit tools

### Board audit tools

Three of the tools exist because of specific misses on real boards, and they
answer questions nothing else in the toolchain does:

- **`fluxplace_drc_scope`** — what a DRC result actually *examined*. A board can
  report "0 violations at all severities" while a dozen rules sit at `ignore` in
  the `.kicad_pro`, and a rule set to ignore is not reported at any severity. It
  names which checks are off, flags the ones that are fab-critical (solder-mask
  bridging, annular width — the ones that pass every automated check and then
  bite at assembly), and with `full=true` re-runs DRC with every check enabled on
  a throwaway copy and reports what newly surfaced.
- **`fluxplace_netlist`** — the connection list read back out of the routed
  board. A board generated from a netlist spec has no `.kicad_sch` at all, and
  this is then the only connectivity document that exists.
- **`fluxplace_stackup`** — layer stack, which layers carry plane pours, the
  netclass track and differential-pair geometry, and a straight answer on whether
  controlled impedance can be verified from the files at all. It cannot if no
  dielectric is defined, however precise the netclass looks.

## Connectivity guard and ledger

Every command that writes a `.kicad_pcb` from `--board`/`BOARD` (to `--out` or in place) is checked by `cli.main`:
pad -> net (pins with several copper pieces sharing a number compare as a multiset), footprint ref / value / footprint.
An undeclared change exits 1 with the diff on stderr, moves the result to `<out>.rejected.kicad_pcb` (an in-place write
gets its input back) and writes `<out>.ledger.json` (moved refs with deltas, nets touched, open connections and collision
FAILs before -> after). Declared changes: `sync-nets` (nets), `replace-footprint` (footprints). `FLUXPLACE_GUARD=0` = off.
Read-only commands (`lint`, `review`, `route-status`, `route-wait`, `route-diagnose`, `doctor`, `help-brief`, `floorplan-check`, `rules`,
`analyze`, `comprehend`) take no snapshot unless they write a `.kicad_pcb` (`--out`).
An input the guard cannot snapshot prints `guard: UNCHECKED - <reason>` (never silently unguarded). A command that raises after
writing in place gets its input back (the original error is re-raised).
Constraints: `[guard] ledger = false` turns the `<out>.ledger.json` sidecar off (default on).
The pipeline applies the same check per stage (`W/LEDGER.txt`, `docs/AGENT-PIPELINE.md`).
