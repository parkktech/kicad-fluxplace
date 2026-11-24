# PHASE3B fixes — Fable review of a6f3898

Worktree /var/kicad/fluxplace-wt/phase3a (branch phase3a). Board-agnostic code only. Do not commit. No routers.
Read `git show a6f3898` first. Line numbers are from a6f3898; re-find by name.

1. **Rails first in krt-whole** (`krtchain.run`): insert a step `("rails", ...)` BEFORE the `diff` step that runs
   `route.py` with `--nets <rail names>` only, plus `--power-nets/--power-nets-widths` for them (reuse `cmd_route` with
   `sig` replaced by the rail list; check how `cmd_route` builds `--nets`, add a `nets=` argument if needed). Skip the
   step when there are no rails. The later `route` step keeps the rails in `--power-nets` (keep-input-copper keeps the
   spines). Test: the steps list for a cons with one rail has names `[... "rails", "diff"?, "route"]` in that order.
2. **Exclude zone-backed nets, not pour rails.** A rail is routed as a spine only when the board has NO zone of that
   net on any copper layer (`pcbnew` `b.Zones()` / or the board text `(zone (net ...` scan already used in
   `gaps._zone_nets` — reuse that). Replace the `plane_plan` exclusion in krtchain and the `EP.plane_nets` exclusion in
   pipeline (pipeline.py ~580) with this; wrap the pipeline call so an exception yields `{}` widths, not a failed stage.
3. **pour_rails becomes a corridor pour, safely.**
   - Outline per rail per outer layer = the union of the rail's track segments on that layer buffered by
     `pour_over_mm` (use shapely if importable — check `python3 -c "import shapely"`; else per-segment rectangles merged
     as separate zones is NOT acceptable: then pour nothing and report "no shapely").
   - Skip a rail that already has a zone (item 2) and skip layers with no spine. Name each zone
     `fluxplace-rail:<net>`; on re-run, remove zones with that name first.
   - Priority: GND/plane zones keep theirs; rail zones get `max(existing priorities) + 1 + i` (distinct, by descending current).
   - Zone settings copied from the board defaults (`b.GetDesignSettings().GetDefaultZoneSettings()` → apply with
     `ExportSetting`, or copy clearance/min thickness/thermal gap/spoke from an existing zone of the board); pad
     connection FULL (solid) for the rail's own pads.
   - `layers` passed from the caller, the same value `route_widths` got (fix both call sites).
   - Guard: copy the `.kicad_pro` next to the poured file; DRC before/after (`--refill-zones`); keep the pour only if
     shorts, opens AND errors are all not higher; else drop and record why.
4. **Report after the pour**: krtchain.run and the pipeline batch stage recompute `left` / opens / verdict and the status
   file AFTER `pour_rails` (move the call earlier or re-measure). Record in the stage result: rails routed, widths,
   zones kept/dropped with reason.
5. **Neck-down refusals**: after the rails and route steps, read the route JSON(s) for KRT neck-down refusals (grep
   KRT `py_router/single_ended_routing.py` for "Neck-down refused" to see where it is reported — json or stdout) and list
   the refused nets in the stage result as `rail_refused`. No retry logic.
6. **Inner spines**: in the stage result list rails whose spine has track length on inner layers (`rail_inner_mm`).

Tests: update/add tests for 1, 2, 3 (outline from two segments; priority above existing; skip when a zone exists;
re-run removes the named zones — use a synthetic board via pcbnew if feasible, else unit-test the pure functions you
factor out), 4. Run tests/test_power.py and every test file importing krtchain, pipeline, power, batchroute
(skip ones that launch routers; list them).

Dry check on /var/kicad/utv-comms-bridge/W14/stageB/rn400/pairs-rn.kicad_pcb with W14/inputs/constraints.toml: print the
rail list + widths krtchain would pass, and the krt-whole step names (build the command lines without running them).

Report under 30 lines: `git diff --stat`, tests, dry check output, deviations.
