# PHASE3B — power rails reach the router at their width (D71cj b)

Repo: /var/www/html/kicad-fluxplace. Python /usr/bin/python3. Board-agnostic code only. Do not commit.

## The bug (measured)
- `pipeline._route_cmd(..., power_nets=None, ...)` accepts `power_nets` and never uses it; no caller passes it.
  The batch route stage therefore routes every `[power.*]` rail at the signal track width (W14: 5 A rails at 0.15 mm,
  review POWER_WIDTH FAIL).
- `krtchain.run` (route_mode krt-whole) passes only the plane nets as `--power-nets`, at `[power] width_mm` or 0.3.
- Nothing builds a pour for `pour = true` rails after routing.

## A. One width source: `power.route_widths(cons, layers, track_mm)`
Add to `fluxplace/power.py`:
```python
SPINE_MM = 1.0      # D71cj: a pour rail is routed as a spine this wide, then poured

def route_widths(cons, layers=None, track_mm=0.15):
    """{net: router width mm} for every [power.*] rail with a current figure: pour rails -> min(SPINE_MM, need),
    other rails -> their IPC width; never below `track_mm` (the signal width). Rails with no current figure are left out.
    `[route] power_spine_mm` overrides SPINE_MM."""
```
Use `plan(cons, layers=layers)`: need = `r["track_mm"]`; skip `None`; `w = min(spine, need) if r["pour"] else need`;
`w = max(w, track_mm)`; round to 0.01. Skip nets that `constraints.pour_nets` / the plane plan already treat as
plane nets (GND-style planes are not rails): check how `krtchain.plane_plan` names plane nets and exclude those.
Test: `tests/test_power.py` (create if missing) — pour rail 7.2 mm need -> 1.0; 0.7 mm rail -> 0.7; 0.05 rail -> track_mm.

## B. Batch route (`pipeline.py`)
1. `_route_cmd(..., power_nets=None, ...)`: `power_nets` is a `{net: width}` dict. When it is non-empty, keep only the
   nets that are in `only`, and when any remain append
   `["--power-nets", *names, "--power-nets-widths", *[f"{w:g}" for w in widths]]` (same glob escaping as
   `krtchain.cmd_route`: import and use `krtchain.glob_escape`). Not for `pair=True` calls.
2. In the route stage (`build` and `build2` near pipeline.py:517 and :646) pass
   `power_nets=PW` where `PW = PWR.route_widths(c.cons, layers=<copper layer count>, track_mm=R["track"])` computed
   once before `build` (use whatever layer count `R` / `a` exposes; if none, `None`).
3. Batch order: power rails route first. Find where the batch planner (`batchroute`) orders nets
   (`grep -n "def make_runner\|def plan\|order" fluxplace/batchroute.py`); put every net in `PW` into the first batch,
   largest width first. If the order is decided elsewhere, do it there and say where.

## C. krt-whole (`krtchain.run`)
`power` at krtchain.py:217 becomes `plane widths (as now) | route_widths(...)` for the rails (plane entries win for
plane nets).

## D. Pour for `pour = true` rails (after routing, before close)
Check first whether one exists: `grep -rn "is_pour\|pour_nets" fluxplace/*.py`. If a stage already adds a zone for
`[power] pour = true` nets, report where and stop for this section. Otherwise add `power.pour_rails(board, cons, out)`:
for each pour rail with routed copper, add a zone on each outer layer that carries its spine (F.Cu/B.Cu only), outline =
the bounding box of the rail's tracks and pads on that layer grown by `[route] power_pour_over_mm` (default
`POUR_OVER_MM`), priority 1, thermal relief per the board defaults; fill with `planes.refill`. Use pcbnew
(`planes.pour` shows the zone-creation pattern; reuse it with a rectangle). Call it from the pipeline right after the
route stage writes its best board; record the zones it added in the stage result. Guard: measure with
`drcguard.measure` before/after; if shorts rise, drop the zones and report.

## E. Tests and measurement
- Unit tests for A, B.1 (command contains `--power-nets` with the right widths only for nets in `only`; none for pair calls).
- Run every test file that imports pipeline, krtchain, power or batchroute. Report counts.
- Dry print (no routing): for `/var/kicad/utv-comms-bridge/hardware/utv-comms/constraints-v20.toml`, print
  `route_widths(cons, layers=6, track_mm=0.15)`.

## Report (under 30 lines)
`git diff --stat`, test counts, the dry print, where batch order is decided, section D outcome, deviations.
