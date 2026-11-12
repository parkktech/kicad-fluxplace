# Placement strategies

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
