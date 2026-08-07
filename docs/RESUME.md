# RESUME — fluxplace automated layout+route+fab system

**Paste to resume:** *"Resume the fluxplace automation work. Read docs/RESUME.md and
docs/AUTOMATION-ROADMAP.md first. Continue the placement-layer levers that improve
routing."*

## The goal
Board (netlist) in → **placed, routed, DRC-clean board + build-quality Gerbers** out,
one command, **any board**, no hand-routing. `fluxplace auto --board X --out Y`.

## State (v0.6.0, ~24 commits, all pushed to origin/main; push is authorized)
`fluxplace auto` runs end-to-end: **place** (escape-aware, side-aware, locks, compact,
grow-to-route) → **route-fresh-per-rung** (KRT; rip+route-all each rung) → **net-aware
step-down** (stalled SIGNAL nets get finer per-net clearance, rails keep width) →
**fanout** (bga_fanout at geometric residue) → **keep-best** (never accept a worse round)
→ **fab** (gerbers/drill/P&P/DRC/MANIFEST). **Auto-detects** signal layers (copper minus
poured planes) + bulk rule (board default netclass). Reports a **diagnosis**: CLOSED /
LAYER-LIMITED / ESCAPE-LIMITED.

## Key files
- `fluxplace/adaptive.py` — route_adaptive (the finisher), krt_route_fresh, krt_fanout, diagnosis
- `fluxplace/escape.py` — detect_escape_zones, net_floor_mm, classify_stalled_nets, dru_text
- `fluxplace/kicad_io.py` — signal_layers(), default_rules(), _escape_halo (anisotropic, per-axis)
- `fluxplace/fab.py` — kicad-cli fab package; `cli.py` cmd_auto/cmd_fab
- `fluxplace/placement.py` — placement (halo in _size, centroid packer, controlled expansion, feedback keeps-best)

## Environment / how to run
- **KRT fast router**: `~/tools/router-venv/bin/python` (venv --system-site-packages +
  scipy/shapely; inherits pcbnew), KRT at `~/tools/KiCadRoutingTools`. freerouting jar
  `~/tools/freerouting-2.2.4.jar`, java 25 headless OK (~1hr/board — oracle only, too slow for volume).
- Run: `/usr/bin/python3 cli.py auto --board B --out DIR` (auto-detects the rest).
- Bench (CM5 guardrail): `/usr/bin/python3 tests/bench.py --board <cm5> --baseline /tmp/cm5_base.json`.

## Empirical findings (real routers)
- dig: **0.2mm→89%, 0.1mm→97%**; last ~3% = U10(LQFP-144)+J20(2-row mezz) FANOUT, not rules.
- **CM5 is LAYER-LIMITED** (In1/In2 both planes → 2 signal layers → caps ~69 unrouted;
  step-down made it worse → the diagnosis now says so). CM5 is the wrong board to judge 100%.
- **dig is ESCAPE-LIMITED** (4 signal layers F/In2/In3/B) → the right end-to-end test.
- step-down provably works (67→58 once --net-clearances file bug fixed); **fanout regressed
  on CM5 (keep-best neutralizes it) — bga_fanout needs tuning to actually HELP.**

## NEXT — placement-layer levers (Jason: "placement is the backbone")
The routing ceiling is a PLACEMENT problem (objective = HPWL rewards tight, but routing
needs escape room + channels). Levers, priority order:
1. ✅ **Anisotropic orientation-aware escape halo** — DONE (3081ede). VALIDATE on dig:
   re-run `auto` on dig (the earlier /tmp/auto_dig run used PRE-halo code).
2. **Channel-aware legalization** — keep min routing lanes between dense blocks (not just
   non-overlap); the tight core leaves no through-channels.
3. **Package-aware gate scoring** (`route.py`) — charge via/fanout cost for multi-row
   fine-pitch parts so the optimizer avoids un-routable configs (gate said routable, real=89%).
4. **Reserve fanout space** for 2-row+ parts + hand the router escape-via slots (why auto-fanout regressed).

## Gotchas (don't relearn)
- KRT `--net-clearances` takes a JSON **file path**, not inline string.
- KRT **thrashes** re-routing leftover nets through congested copper → always route-fresh.
- `nohup` block-buffers stdout → use `python3 -u`; track progress via on-disk artifacts
  (*.netclr.json = rungs) not the log. Monitor slow runs by **PID + log mtime**, never pgrep -f.
- CM5 is the regression guardrail (must not regress; overflow 0 all session). Razor DIG board:
  D36 = local fine-pitch at U10/J20, committed to branch, **nothing pushed/fabbed** (stays staged).
- In-flight at handoff: `/tmp/auto_dig.log` (pid 636023, PRE-halo code) — supersede with a fresh dig run.
