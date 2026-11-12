# `route-diagnose`

## Why nets stayed unrouted — `route-diagnose`

`fluxplace route-diagnose BOARD.kicad_pcb [--log ROUTER.log ...] [--nets A,B,C] [--json OUT]
[--track MM] [--clearance MM]` is read-only (never writes the board, exit 0) and pure text
(no pcbnew). Failed nets come from `--nets`, else from the logs (KiCadRoutingTools py_router;
a net's status is its LAST `SUCCESS`/`FAILED`/`RETRY`/`REROUTE` line), else from nets with 2+
pads and no track/via/zone copper. Each net gets one or more causes, each with one line of
evidence and one suggestion: `SEALED_PAD` (fine-pitch pad whose outward side has another net's
track/via within track+clearance: fan out the part first or rip the named net), `FAR_SUPPORT_PART`
(2-pin passive > 5 mm, or 2.5 mm for DECAP/VREF/BYP nets and caps to GND, from the IC pin it
serves), `LONG_HAUL` (max pad-to-pad span > 30 mm or 35 % of the board diagonal: move the part with
fewer pads toward the other), `KEEPOUT_TRAPPED` (pad inside a track-forbidding rule area),
`SUB_RULE_PAD` (pad narrower than the default track or gap below clearance: neck-down, never
below 0.127 mm), `CONGESTION` (fallback, copper fill of the net's bbox). The summary prints counts
per cause and a ranked move list merged across nets (a part fixing several nets comes first).
Default track/clearance come from the `.kicad_pro` Default net class. Thresholds are constants
at the top of `fluxplace/routediag.py`.
