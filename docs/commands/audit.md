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
