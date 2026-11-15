# fab-advise

`fluxplace fab-advise --board B.kicad_pcb --constraints C.toml [--json out.json]` / `--list`

Grades every manufacturer tier in `fluxplace/data/fab_capabilities.json` (PCBWay, JLCPCB; each table has a `source` URL and a
`retrieved` date) against the board's ACTUAL features: copper layers, size, narrowest track drawn and requested, requested
clearance and pair gaps, via sizes (drawn and requested by the net classes), PTH rings, impedance control, blind/buried vias,
fine-pitch mask dams. Prints the verdict per tier, the cheapest tier that builds the board as designed, the smallest changes that
would also fit the other same-or-cheaper tiers (with the number of objects affected) and the vendor wording of what raises the tier.
Vendors publish price steps as wording only, so no price is invented ("price not published"; tiers are ranked by that wording).

`[fab] profile = "<vendor>-<tier>"` (see `--list`) plus the board's layer count select a row. Those numbers are HARD limits: review
FAILs below them (`FAB_*`), the router never goes under them. `[route] min_track_mm / min_clearance_mm` stay the PREFERRED floor
above them; a value between the two (a 100 ohm pair gap of 0.107 mm against a 0.127 mm floor) is legal and reported as "below
preferred floor, within fab limit". An unknown profile name is an error that lists the valid ones.

Power: `[power."NET"]` (`max_current_ma | load_a | current_a`, `max_v`, `pour`, `temp_rise_c`) is sized by `fluxplace/power.py`
(IPC-2221 width by layer weight, via ampacity, IPC-2221B spacing, pour-over-2-mm policy; `[env] trace_rise_c`, default 10 C).
Review: `POWER_WIDTH`, `POWER_VIAS` (FAIL), `POWER_CURRENT_UNKNOWN` (WARN, lists rails with no current).
