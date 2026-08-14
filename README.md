# fluxplace

**Signal-flow-aware component placement for KiCad.** Reads a board's *communication graph* —
who talks to whom, with power/ground excluded so they don't tie everything together — and
re-places components so routing falls out as a tree instead of a tangle. The objective is
**weighted wirelength** (HPWL): connected parts pull tight, decoupling caps collapse onto
their IC's power pins, buses line up — an organized, dense, autoroutable board.

Runs two ways from one pcbnew-free core:
- **In the PCB editor** — Tools → External Plugins → *fluxplace*
- **Headless CLI** — for scripting, CI, or an agent

> Built and proven on the RAZOR-01 CM5 carrier (178 parts): topology-blind placement →
> fluxplace cut weighted wirelength ~50% and shrank board area ~28%, with the CM5 module
> kept clear and every subsystem in its own zone.

---

## Why it works

Placement is an optimization with a measurable objective: minimise the summed span of every
net. The only way to shrink that is to pull communicating parts together — so density and
routability come out of the same math. fluxplace adds the engineering judgment a naive
"pack it tight" misses:

- **Weight the lanes.** A PCIe/USB/Ethernet diff pair weighs far more than a stray GPIO;
  power/GND are handled as planes, not springs (otherwise every part glues to the CPU's
  ground pins and the graph collapses). The weighting *is* the intelligence.
- **Keep decaps on their IC.** Clusters come from schematic sheets (or, if the board has
  none, from topology branches + nearest-branch assignment) so bypass caps never strand.
- **Big parts are obstacles, not points.** A CM5 module or M.2 socket is a keep-out with
  locked orientation; connected parts anchor to the *actual pad* on its perimeter (pin-aware
  springs), never pile onto its center.
- **Manufacturable by default.** Small parts snap to 0/90/180/270 for cheap, error-proof
  pick-and-place; the board outline shrink-wraps the placement for the smallest (cheapest)
  fab.

## Strategies

| Strategy | What it does | Use when |
|---|---|---|
| `build` | **The route-aware pipeline** — quadratic global solve (hub central by math) → constructive route-as-you-place builder → global-router gate → congestion feedback. Refuses to hand back an unroutable board. | **The best result.** What the GUI plugin runs. |
| `quad` | Analytic quadratic placement + SimPL spreading alone (no router in the loop). | Fast hub-central layouts. |
| `pack` *(CLI default)* | Cluster → pack each cluster in signal order → bin-pack clusters by connectivity → compact. | Organized **and** compact, no router. |
| `flux` | Pin-aware force-directed; pulls every pad to its net centroid. | Legacy: dense but exiles the hub. |
| `radial` | Hub centered, branches radiate to edge connectors. | A clean first-pass structure. |

### Route-aware placement (`build`) — how it thinks

It works the way an engineer does:

1. **The mental map** — a quadratic solve over *pad positions* (pin offsets in the RHS;
   the system stays linear). The hub lands at the weighted mean of everything it talks
   to — central by math, not by pinning. Edge connectors are fixed on the perimeter;
   big modules (CPU, M.2) are first-class movable objects that get pulled adjacent by
   their heavy nets.
2. **The hands** — parts commit one at a time, hub first, then always the part most
   strongly tied to what's already down. Each part auditions spots near its map
   position; candidates are scored by *estimating its actual traces* on a routing grid
   (L-route congestion probes). The winner's nets are then **really routed**
   (congestion-negotiated A*) and reserved, so later parts can't crowd out earlier
   traces.
3. **The gate** — an independent coarse global router (PathFinder-lite) routes the
   whole board. `overflow == 0` means globally routable. If not, parts around the
   hot cells get inflated spacing and the board re-legalizes — routability beats
   density, always. The v0.3 model is deliberately honest:
   - **layer-aware**: horizontal and vertical runs draw on different layers'
     capacity (classic H/V discipline); turning costs a via;
   - **power-aware**: small-fanout power rails (28 V in, 12 V feeds, buck outputs)
     are routed FIRST at 2–3 track-slots of width, tapering to land on their pads —
     pretending power is free is how boards become unroutable;
   - **pair-aware**: diff pairs route as master + hugged slave, and the report
     scores how well each pair stayed together;
   - **escape-aware**: fine-pitch parts project a fanout ring; pin cells keep
     landing capacity; tiny in-line passives (PCIe AC caps) block nothing.
4. **The search** — after the gate passes, the board is *shrunk to the smallest
   scale that still routes* (binary search, each step re-gated), plane-only decaps
   walk to their IC's side (reverted if the gate objects), and `--seeds N` tries
   perturbed variants keeping the best routable one.

Everything is deterministic: same board in, byte-identical placement out.

### Route guides + ground truth

- `place --guides` / `route --guides` draw the global corridors on `Eco1.User`
  (group `fluxplace-guides`) — open the board and route along the reserved plan.
- `calibrate` exports a Specctra DSN, runs freerouting (`--jar`/`$FREEROUTING_JAR`,
  or parse an existing session with `--ses`) and reports whether the gate and a
  real autorouter agree on this board.

Rotation: `ortho` (default, assembly-friendly) · `fine` (any angle, lowest wirelength) · `none`.

## CLI

Run with KiCad's Python so `pcbnew` imports (Linux example):

```bash
export KP=/usr/lib/python3/dist-packages          # where pcbnew.py lives
PYTHONPATH=$KP python3 cli.py <command> --board board.kicad_pcb [opts]
```

| Command | Purpose |
|---|---|
| `analyze` | Print the communication map: hub, forks, branches, lint. |
| `route`   | Global-route the **current** placement and report congestion/overflow. |
| `plan`    | Gather component + schematic info and write a **detailed placement plan** (markdown). |
| `gather`  | Dump structured board facts as JSON. |
| `place`   | Re-place and save. `--strategy pack\|flux\|radial --rotate ortho\|fine\|none --out out.kicad_pcb` |
| `eval`    | Weighted wirelength, overlaps, extent, **pin density**; `--prc` grades physics checks. |
| `comprehend` | Auto-detect physics constraints (power nets w/ IPC-2221 widths, diff pairs, bypass caps, crystals, converters); `--prc` grades the placement against them. |
| `compact` | Shrink a known-good placement → route → fab. Placement controls: `--rule-areas` (KiCad Rule Areas: named+empty = hard region, keepout = obstacle), `--outline W:H` (hard bounds, fails loudly), `--flip decaps\|passives` (back-side exploration), `--cluster-anchors`, `--quilter-contract`, `--preserve-pour NAME`, `--keep-copper`. |
| `deliver` | Split a fab package for its two audiences: a CAM-only zip for the fab, loose readable docs (brief .md+.docx, BOMs, **PCBWay order worksheet**) for whoever places the order. |
| `pcbway` | PCBWay's Assembly quote form, field by field, answered from the board: size, layers, track/space tier, drill, finish, sides, unique/SMD/fine-pitch/THT counts, consign list. `CHOOSE` wherever the design has no opinion. |
| `tournament` | Candidates × `--profiles` (fab rule bundles) → gate → freerouting → **lexicographic rank: DRC → completion → PRC passes → conservativeness → vias → wirelength last**. |

### Physics constraints (comprehension)

fluxplace auto-detects the constraint classes an experienced engineer holds in
their head, using the same shallow-but-effective heuristics the commercial
tools use (see `docs/QUILTER-DOCS-DIGEST.md`): bypass caps get ONE owner (the
nearest IC on the shared rail) and a **capacitance rank — the smallest cap
belongs closest to the pin**; crystals cluster with their series R and load
caps; switching converters get a hot-loop objective (U + L + ceramic Cin/Cout);
diff pairs come from suffix conventions (P/N, +/−, t/c…) with series elements
merged. Each constraint is graded post-placement by tolerance-window physics
rule checks (`eval --prc`), and the tournament ranks candidates by those
results ahead of via count and wirelength.

Examples:

```bash
PYTHONPATH=$KP python3 cli.py plan  --board board.kicad_pcb --out PLACEMENT-PLAN.md
PYTHONPATH=$KP python3 cli.py place --board board.kicad_pcb --strategy pack --rotate ortho \
                                    --out board.placed.kicad_pcb
PYTHONPATH=$KP python3 cli.py eval  --board board.placed.kicad_pcb
```

`place` prints, e.g.:
`placed 178 parts [pack, rotate=ortho]  HPWL 26349 -> 16627 mm (-37%)  overlaps=0  board=180x149mm`

## Autorouting

fluxplace optimizes for exactly what an autorouter wants — short, uncrossed nets. Workflow:

1. `place` the board with fluxplace.
2. Export Specctra DSN (`pcbnew.ExportSpecctraDSN`) and route with
   [Freerouting](https://github.com/freerouting/freerouting) headless
   (`java -jar freerouting.jar -de in.dsn -do out.ses -gui.enabled=false`).
3. Import the `.ses` back into KiCad.

Lower HPWL from `eval` correlates directly with higher autoroute completion.

## Install

**Manual (dev):** clone this repo into KiCad's 3rd-party plugin directory (or symlink it):

- Linux: `~/.local/share/kicad/8.0/3rdparty/plugins/`
- Windows: `%APPDATA%\kicad\8.0\3rdparty\plugins\`
- macOS: `~/Documents/KiCad/8.0/3rdparty/plugins/`

Restart KiCad → Tools → External Plugins → *fluxplace*. Installed once, it's available in
**every** project.

**PCM:** add this repo's `metadata.json` as a Plugin & Content Manager repository for
one-click install + updates.

## Architecture

```
fluxplace/
  graph.py       communication graph: power/signal split, passive collapse, net weighting
  topology.py    hub, forks, branches, lint (floating parts)
  placement.py   strategies (radial/flux/pack), rotation, legalization, HPWL, compaction
  kicad_io.py    the ONLY pcbnew-dependent module: read parts+nets+pads, write positions
  report.py      gather() + plan_markdown()
plugins/         KiCad Action Plugin (GUI entry)
cli.py           headless entry
```

The core is pcbnew-free and unit-testable; swapping `kicad_io` would retarget another EDA tool.

## License

MIT © 2026 Jason Ratzlaff
