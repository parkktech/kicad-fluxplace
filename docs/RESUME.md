# RESUME — fluxplace automated layout+route+fab system

**Paste to resume:** *"Resume the fluxplace automation work. Read docs/RESUME.md and
docs/AUTOMATION-ROADMAP.md first, plus the fluxplace-automation-system memory."*

## The goal
Board (netlist) in → **placed, routed, DRC-clean board + build-quality Gerbers** out,
one command, **any board**, no hand-routing. `fluxplace auto --board X --out Y`.

## State (main, all pushed; push is authorized)
`fluxplace auto` runs end-to-end: **place** (escape-aware, side-aware, locks, compact,
grow-to-route) → **route-fresh-per-rung** (KRT) → **net-aware step-down** → **fanout
fallthrough** (a worsened step-down reverts to best and tries fanout — it no longer
silences it) → **keep-best** → **fab**. Auto-detects signal layers + bulk rule
**BEFORE placement** and threads the layer count into the gate (`_GateScorer`).
Diagnosis: CLOSED / TIME-CAPPED / LAYER-LIMITED (spread residue) / ESCAPE-LIMITED
(concentrated residue).

**Board-truth commands (2026-08-08, the stand-in/netlist arc):**
- `preflight --sch --components` — order-readiness gate BEFORE layout and BEFORE
  ordering: stand-in footprint names (FAIL), sch-pin/pad parity (FAIL), pad-NET
  parity vs the schematic netlist (FAIL — the CM5 shipped v1–v8 with its whole
  +3V3 rail absent from the PCB net set), missing courtyard / 3D (WARN).
- `replace-footprint --board --ref --lib --name [--sch] [--rename]` — swap a
  stand-in for a real vendor land in place: keeps pos/rot/side + schematic KIID
  link, re-nets pads by number, netlist as net truth (recovers pads the stand-in
  never had). Proven: CM5 J80 (card-edge→real M.2 socket), U70, U71.
- `sync-nets --board --sch` — headless "Update PCB from Schematic", nets only.
- `fab --upload-out DIR` — ECAD upload set (board renamed to project stem +
  pro/dru/sch), excludes .kicad_prl (the one package with it failed to parse).
Sourcing that works from the sandbox: jlcsearch.tscircuit.com (LCSC index) +
`easyeda2kicad --full --lcsc_id Cxxxx` (real vendor footprints + STEP). KiCad's
own demos are a footprint goldmine: /usr/share/kicad/demos/cm5_minima = complete
CM5+Hailo-8 reference (production M.2 socket land + both STEPs).

**Improvement-loop commands (2026-08-09/10 arc):**
- `patch --board [--out --track --clearance --constraints]` — last-mile
  single-net router: island Dijkstra + dogbone escape vias (stub validated
  cell-by-cell), oriented-rect pad obstacles, KiCad-resolver clearances +
  .kicad_dru sidecars, pour refill/heal, DRC guard with zone-phantom filter
  and position+net-level subset-accept. Also runs inside `auto` at profile
  floor geometry (skip: --no-patch). Measured: CM5 49->18 standalone; in
  pipeline, CM5 v12 = 2 unrouted/240 viol (was ~1650), dig v4 = 27/88.
- `auto --route-only` — keep the existing (hand) placement: route+patch+fab
  only. This is the RF-board mode (RF islands/can walls untouched). RF first
  route: 202 -> 36 after fanout at 40 parts.
- `verify-models --board [--fix]` — 3D model registration: STEP pin shafts
  vs TH holes (renderer convention: +z-rot is CLOCKWISE in the y-up frame),
  body-over-footprint for module models; --fix solves rot/offset/z-lift.
- **Constraint intent travels IN the .kicad_pro**: `upload_package`
  auto-injects netclasses from `<stem>.constraints.toml` —
  DP_<group>_<Z>R diff-pair classes (width/gap per impedance target,
  constraints.PAIR_GEOM_BY_Z, JLC7628 values marked CALIBRATE) and
  PWR_<net> ampacity widths (constraints.rail_width_mm). Without this,
  external parsers guess 100R pairs / 500mA rails (measured on Quilter).
  Upload set = exactly pcb+pro+sch; self-cleans stale *.kicad_* files.
- ORDER/UPLOAD GUIDANCE block prints at end of every auto run + MANIFEST.

## THE 2026-08-07 discoveries (don't relearn)
1. **The layers=2 backbone bug (fixed):** route.score/builder modelled every board as
   2-layer; on 4-signal-layer dig the gate under-called capacity, declared every
   compact placement unroutable, and grow-to-route BALLOONED it to 222x239mm/8.3%
   fill. All pre-fix "auto on dig" numbers were junk. Post-fix: **dig places 95x90mm,
   overflow 0, fill 51.5% — smaller than the 100x100 hand board.**
2. **First trustworthy dig routed result:** 42 unrouted nets @0.2mm bulk (hand layout:
   102). Residue = J20 (25 endpoints) + U10 (21) — escape-shaped. THE NUMBER TO BEAT.
3. **`gate-precharge` branch REJECTED by real-router A/B** (82 vs 42 unrouted, same
   caps): escape-derating gate + via-lane halos + edge-relative axis fix looked fine
   on gate proxies (+2% hpwl) but routed 2x worse. Parked unmerged. Salvage candidate:
   the axis fix alone (raw |dx|>=|dy| really does misread LSHM 2x40 as 66/82 pads
   escaping out the ends — bisect it solo before believing it helps).
4. **Gate proxies (hpwl/area/overflow) do NOT predict real routed-%** across placement
   variants. Real KRT A/B is the only merge gate for placement changes.
5. Diagnosis needs time-cap awareness: a 1800s-capped rung proves nothing about
   clearance vs capacity (both dig rungs capped; step-down "worsening" 42→44 was noise).

## Key files
- `fluxplace/adaptive.py` — route_adaptive (finisher: rungs + fanout fallthrough),
  krt_route_fresh (fn.any_timeout), krt_fanout (--nets = stuck nets only), diagnosis
- `fluxplace/placement.py` — place_routed(layers=), _GateScorer, _open_channels
  (congestion-wall → targeted lane, tried before uniform expansion), _expand_to_route
- `fluxplace/route.py` — gate Grid, score(layers=), cut_overflow (wall detection)
- `fluxplace/kicad_io.py` — signal_layers(), default_rules(), _escape_halo
- `cli.py` cmd_auto — detects layers/rules BEFORE placing; fanout OFF when <3 signal
  layers (through-vias eat the scarce layers — why fanout regressed CM5)

## Environment / how to run
- KRT: `~/tools/router-venv/bin/python`, repo `~/tools/KiCadRoutingTools`.
- Run: `/usr/bin/python3 -u cli.py auto --board B --out DIR` (auto-detects the rest);
  `--route-timeout` default 1800s/rung — dig rungs hit it, raise for verdict-quality runs.
- Bench (placement-only): `PYTHONPATH=/usr/lib/python3/dist-packages /usr/bin/python3
  tests/bench.py --board <pcb>`; CM5 guardrail baseline /tmp/cm5_base.json is PRE-halo
  (its >8% hpwl FAIL vs main is the known 3081ede halo cost, not a new regression).
- Tests: `/usr/bin/python3 tests/test_core.py` (18 green, no pcbnew needed).

## Gotchas (hard-won)
- KRT `--net-clearances` = JSON **file path**; route-fresh always (leftover-patch thrashes).
- `python3 -u` + on-disk artifacts for monitoring; never trust block-buffered logs.
- Deterministic placement: same board+code → same layout (A/Bs replay rungs exactly).
- CM5 = regression board (2 signal layers, layer-limited ~69 unrouted — wrong board to
  judge 100% on). razor DIG board stays staged: D36 committed to its branch, nothing
  pushed/fabbed/emailed.

## NEXT
1. **In flight:** dig auto with fanout fallthrough (/tmp/auto_dig_ff) — first run where
   fanout actually fires at J20/U10. Beats 42 → new baseline.
2. CM5 real-route A/B harness (KRT unrouted count) so placement changes get a cheap
   second real-router check.
3. Fixed-outline fit / legalizer density — the open placement frontier.
4. Bisect the escape-axis fix solo if the last 3% stays stuck at J20/U10.
