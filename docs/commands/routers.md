# Router versions

## Router versions — freerouting 2.5.0 and KiCadRoutingTools v0.23 (2026-11-10)

Both are supported; neither is the new default, because a trial did not show a
clear win. Rip-up-and-reroute of utv-comms V1.5 (6-layer, 317 connections, one
run each, the four runs sharing the machine — treat it as indicative):

| Router | Wall | Left open (signal + plane) | DRC violations | Note |
|---|---|---|---|---|
| freerouting 2.3.0 | 672 s | 77 (1 + 76) | 7 | 2 `track_width` |
| freerouting 2.5.0 | 365 s | 100 (0 + 100) | 5 | silk only; skips its optimiser when anything is left unrouted |
| KiCadRoutingTools 0.19.3 | 173 s | 45 (43 + 2) | 112 | 106 `track_not_centered_on_via` |
| KiCadRoutingTools v0.23.9 | 1426 s | 19 (3 + 16) | 180 | 174 `track_not_centered_on_via`; default `--fab-tier auto` shipped 0.0889 mm track / 0.25 mm via |

**Read the "left open" column as signal + plane.** The trial ran the raw router
only. GND and +5V are plane nets: the DSN declares In1/In4 `(type power)`, so
freerouting leaves those pads for the pipeline's later plane-stub, pour and
stitch stage, which the trial did not run. On signal nets freerouting 2.5.0
closed all of them and 2.3.0 missed one (`I2S_DAC`); the KRT runs used
0.2/0.2 mm rules, coarser than the board's 0.15/0.127, so their signal misses
are partly a settings artefact. The full pipeline was not re-run end to end.

- **freerouting:** `finish.freerouting_cmd()` builds the command for the jar's
  version — 2.5.0 gets `--router.autorouter.max_passes=N --gui.enabled=false`
  (`-mp` is deprecated there), older jars keep `-mp N`. Set `FREEROUTING_JAR` to
  opt in to 2.5.0; 2.3.0 stays the default when installed.
- **KiCadRoutingTools:** `adaptive.krt_extra()` adds `--fab-tier standard` when
  the checkout knows the flag, and `adaptive.krt_summary()` reads the new
  `JSON_SUMMARY_MIN` trailer (falling back to `JSON_SUMMARY`). Point
  `--router-dir` at the newer checkout to use it. `route_diff.py` and
  `bga_fanout.py` were not exercised against v0.23.
