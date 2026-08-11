# Quilter Docs — Complete Digest & Placer Parity Spec

Fetched 2026-08-11 from https://docs.quilter.ai/ — all 74 pages in the sitemap were read
(7 parallel readers, one page-set each; `.md` variants used for clean markdown; GitBook's
`<page>.md?ask=<question>` API used to recover content behind empty stubs).
Purpose: reference for building a placement engine (fluxplace) at least as good as Quilter's.

**Coverage note:** these sections are published but literally EMPTY (title-only stubs):
all 8 `cad-compatibility/*` pages (incl. KiCad), `candidate-review/review-phases`,
`edit-and-iterate`, glossary, user-resources, `pre-supported-fabs/page-1`, and the three
`future-prc2s/*` subpages. Their real content, where it exists, is scattered in other pages
and is captured below. Nothing else was missed.

---

## 1. Engine architecture (as disclosed)

- **Algorithm:** reinforcement learning, explicitly self-play / "AlphaGo-style" — NOT
  supervised on human boards. Generate-and-score loop: explore many layout possibilities,
  evaluate each with Physics Rule Checks (PRCs) as the reward/evaluation signal.
- **Team expertise called out:** RL + simulation-driven ML + **computational geometry**
  ("geometric optimization for efficient, manufacturable layouts").
- **Compute:** GPU-heavy — on-prem deployment requires NVIDIA GPUs ≥16 GB VRAM (Helm/K8s).
- **Impedance solver:** Simbeor field solver, **quasi-static** approximation → the stated
  6 GHz accuracy ceiling. Diff-pair default frequency assumption: **1 GHz**.
- **Multi-target exploration:** each job explores multiple "compile targets" =
  (stackup × fabrication rule set) pairs **in parallel**; physics-derived geometry
  (widths, gaps) is recomputed per stackup, per candidate. Constraints are specified at the
  physics level; geometric rules are synthesized downstream.
- **Candidates stream in** as found (first typically <1 h; job runtime 15 min–24 h);
  up to **6 candidates** surfaced per job.
- **Single-sided-first search:** every job tries single-sided placements first, falls back
  to double-sided only if no valid single-sided solution exists.
- **Silkscreen:** not optimized — moves rigidly with the component; collisions possible.
- Footprints never modified; no library management; no schematic validation.

## 2. Operating envelope (their published sweet spot)

| Limit | Value |
|---|---|
| Pin count | < 5,000 pins |
| Pin density | < 20% (= component pin area / board area × 100) |
| Signal frequency | < 6 GHz (quasi-static solver limit) |
| Voltage | ≤ 48 V |
| Current | ≤ 10 A |
| Layers | 2 → 10+, no fixed cap |

Billing is metered per pin. Job "success" = at least one candidate **>95% routed**.
"Strives for 100% completion" vs autorouters' typical 70–90%.

## 3. Input contract (what the engine consumes)

**The fixed-vs-free rule is purely geometric — no lock attributes:**
- Component **outside the board outline** → engine places & routes it.
- Component/trace/via **inside the outline** → hard-locked verbatim (position AND rotation;
  the engine will fail a job rather than move it). Overhanging pre-placed parts OK.
- Stray vias inside the outline **block placement** (assumed intentional).
- Orphan track segments are never garbage-collected; partial routes are **extended**, not
  ripped up.
- Copper **pours are deleted and regenerated** unless explicitly named in the Preserved
  Pours table (KiCad: zone must have a name). Internal-layer copper is discarded unless the
  input stackup is preserved. ("Locked"-flag honoring is roadmap, not shipped.)

**Required:** exactly one single closed outline (KiCad: `Edge.Cuts`; Altium: Mech 1);
all footprints instantiated; netlist valid and matching the schematic. Keepouts/polygons on
the outline layer confuse the parser.

**Conventions the parser relies on (string-driven semantics!):**
- Plane intent from **layer names**: `ground`/`gnd`, `power`/`pwr`.
- Connectors are NOT auto-recognized — must be pre-placed by the user.
- KiCad **placement region** = Rule Area on F.Cu/B.Cu, given a name, with ALL keepout item
  checkboxes UNCHECKED (an "empty keepout" is the sentinel). Component↔region association
  is manual in the web UI for KiCad (Altium Rooms auto-associate).
- KiCad **keepout** = Rule Area with keepout item boxes checked (tracks/vias/pads/pours/
  footprints all honored).
- Schematic upload is optional but powers Circuit Comprehension; project file (.kicad_pro)
  optionally supplies DRC rules; ECAD-file stackup ingestion is beta.
- Formats: native Altium/KiCad; IPC-2581 via exporter scripts for Allegro/Xpedition.
  Results always returned in the original format.

## 4. Placement control ladder (from the Placement Guide)

Four tiers, least → most restrictive ("the more specific you are, the less freedom
Quilter has"):

1. **Schematic-based clustering** — explicit wire connections in the schematic drive
   grouping; connected components placed close together, groups float freely.
2. **Anchoring** — pre-place one component of a group inside the outline; the rest stay
   "sticky" to it (e.g., place the connector, protection/decoupling follows).
3. **Placement regions:**
   - **Off-board region:** groups components; shape/size/location IGNORED — pure grouping,
     either side of board allowed.
   - **Off-board region + anchor:** their stated "current favorite" — grouping + location
     control without over-constraining.
   - **On-board region:** hard polygon, linked to top or bottom layer; members must be
     inside it, on that side (region membership disables auto side-flipping). Multi-polygon
     union supported (e.g., identical top+bottom shapes). May cause job failure if too tight.
4. **Manual placement** — pre-placed inside outline = immovable.

Roadmap noted in the guide: auto ground pours matching placement-region footprints (for
galvanically isolated ground domains).

## 5. Physics constraints & auto-detection heuristics

Detection inputs: netclasses, component refdes, net names, pin names, connectivity,
component positions. Anything unconstrained = "generic low-speed digital signal."
Entry methods per constraint: individual, **regex**, netclass.

### Routing constraints
| Constraint | Auto? | Detection heuristic | Parameters | Key numbers |
|---|---|---|---|---|
| Power nets | Yes | voltage-style net names OR "Power" netclass | net, max current (mA), pour flag | Defaults: <3 V → 200 mA, ≥3 V → 500 mA. Width from IPC-2221 @ 20 °C rise, per stackup copper |
| Diff pairs | Yes | netclass named `differentialpair`/synonyms; suffix pairs +/−, A/B, P/M, P/N, t/c; nets starting with `V` excluded; inline series R/C merged at compile | pos net, neg net, impedance, freq (GHz) | Menu: 100 Ω (50 SE) or 85 Ω (42.5 SE). Simbeor @ 1 GHz default. No multi-drop. Refused on 2-layer (microstrip-only, no CPW) |
| SE impedance | **Manual only** | — | net, impedance, freq | Menu: 50 or 75 Ω; 5% impedance tolerance target |
| Length matching / timing | Not shipped | — | (delay + max skew per group, DDR/HDMI) | "Coming soon," internal testing |

### Placement constraints (all auto-detected — these ARE the placer's physics rules)
| Constraint | Detection heuristic | Placement behavior |
|---|---|---|
| Bypass caps | cap between a component's power net and ground. Pin assignment priority: (1) explicit schematic wire to a pin, (2) parent pins with voltage-type names (`Vin`…), (3) same-name pins → cap split equally across them | **Smaller capacitance placed closer to the pin** (classic 100 nF-first ordering). One-to-many and many-to-one supported |
| Crystal oscillators | refdes starts `X`/`Y` AND both pins connect directly to same parent IC. Misses crystals with series R; load caps not in schema | crystal held near driver pins |
| Switching converters | refdes `U` + output pin into inductor refdes `L`. Only topology: ≥2 external caps (1 in, 1 out) + exactly 1 output inductor; extra caps "somewhat arbitrarily" pick one | Cin/Cout/L as close as possible to converter (hot-loop minimization) |
| Proximity constraints (manual) | user-defined "keep X near Y" (canonical: protection diodes near connector) | pairwise proximity objective |

## 6. PRC suite (the scoring/validation layer)

8 primitive checks, reused via a constraint→bundle matrix. Binary pass/fail (green/red),
no severity tiers, **no waiver mechanism**. Report shows measured value + tolerance window.
Tolerances are per-constraint (derived from comprehension + stackup + IPC), not universal.

| PRC | Measures | Pass rule / numbers | Driven by |
|---|---|---|---|
| Pin Distance | Euclidean distance between closest pin **edges** | below tolerance (cm) | **Placement only** — gradeable pre-route |
| Trace Path Length | routed length between two pins | below tolerance (cm) | routing (placement sets lower bound) |
| Layer Switch Count | vias in path (e.g., decap → pin) | count ≤ threshold (e.g., 0–1) | routing (placement enables 0-via) |
| Ground Plane Overlap | plane on layer directly below trace at all relevant points | boolean; endpoints exempt for last **2 × clearance**; pin/via antipad margin excluded | routing/pours |
| Invalid Width Span | trace length with width outside **±10%** of nominal | % of net length below limit, absolute-length fallback for short nets | routing |
| Uncoupled Spacing | length where pair separation deviates **>±10%** from nominal gap | total uncoupled length below limit (cm) | routing |
| Length Mismatch | length delta between pair nets | below tolerance (cm) | routing |
| Overheated Length | analytic ΔT per segment on high-current nets; segment overheated if ΔT > **20 °C** | overheated fraction below % limit | routing |

Constraint → PRC bundles: Power nets → {Overheated Length}. SE impedance → {Invalid Width
Span, Ground Plane Overlap}. Diff pairs → {Ground Plane Overlap, Uncoupled Spacing, Length
Mismatch}. Bypass caps / crystals / switching converters → identical 4-bundle {Pin Distance,
Trace Path Length, Layer Switch Count, Ground Plane Overlap}. Timing → none yet.

Future PRCs (announced, unpublished stubs): Top Plane Ground Pour, Neck Downs,
Trace Proximity. (Internal name "PRC2s" hints a gen-2 framework.)

## 7. Design parameters (requirement vs preference model)

Every parameter is either a **requirement** (engine must honor; prefers failing the job
over violating) or a **preference** (filter applied to candidates at review time —
generation is NOT pruned).

- Requirements: pre-placements, placement regions, keepouts (native CAD keepouts for
  traces/vias/components/pours), preserved pours, pre-routed traces, net widths (web UI,
  per net per layer), input-file design rules/stackup.
- Preferences (review-time filters): fabricator, layer count, trace/space minimums,
  single-sided.
- Fabricator constraints = 5 hard geometric floors: min trace width, min clearance, min
  drill, min annular ring, min edge-to-copper. Smaller later-specified values are ignored
  or DRC-flagged.
- Pre-supported fabricators: JLCPCB, MacroFab, OSH Park, CircuitHub, American Standard
  Circuits. OSH Park example: 5 rule sets × 3 stackups → 12 valid compile targets
  (finer rules gated to higher layer counts, e.g. 5 mil/8 mil drill only on 6-layer).
- Custom fabricator profiles: paid service done by Quilter staff, not self-serve.
- Net-widths override is discouraged ("collides" with auto physics widths, notably power).

## 8. Candidate model, metrics & ranking

**Job metadata:** board dims (cm), component count, components-to-place, pin count,
pins-to-route (must be >0), pin density %.

**Per-candidate metrics:** routing completion (% of pins-to-route completed), DRC error
count, single-sided flag, layer count, min trace width **actually used incl. neck-downs**,
min clearance, min via diameter, min drill, via count. (Total trace length is tracked but
only used as final sort tie-break.)

**Hard gates:** candidates with ANY DRC error are never surfaced; <100%-routed candidates
hidden by a default filter; job success = >95% routed candidate exists; up to 6 candidates.

**Ranking = strict lexicographic tuple, not weighted sum.** Four sort personas; all share:
tier 1 = (DRC violations asc, routing completion desc), last tier = shortest total trace
length. "Recommended" order: completion → Priority-PRC passes (Priority = Power Nets, Diff
Pairs, SE Impedance) → most conservative fab rules (width > clearance > drill > via, larger
better) → fewest layers → Other-PRC passes → shortest traces. Other personas permute the
middle tiers (Best PRCs / Easiest to Fab / Fewest Layers).
(One page notes PRC-driven ranking was "future enhancement" — docs are mildly inconsistent;
the Sorting page's tuple above is the authoritative detail.)

**Iteration loops:** download placement-only → move parts → re-upload for routing;
download candidate → tweak/partial ripup → re-upload to finish; duplicate job with same
files+constraints; "save your progress" by keeping liked parts inside the outline and
pushing the rest back out. Projects gate follow-up jobs to ±10% of baseline pin/component/
BOM similarity.

## 9. Quilter's documented gaps (our surpass list)

1. **No lock granularity** — inside/outside outline only; can't express "keep position,
   free rotation" or "keep side only."
2. **Connectors not auto-recognized**; user must pre-place them.
3. **Crystal detection misses series-R topologies; load caps not modeled.**
4. Switching-converter constraint: single topology, arbitrary cap pick among multiples,
   no feedback-node handling, no synchronous/multi-phase awareness.
5. **No multi-channel symmetry** (hierarchical sheets parse but don't replicate layout).
6. SE impedance manual-only; length/delay matching unreleased; >6 GHz unsupported;
   no coplanar waveguide → no diff pairs on 2-layer.
7. Silkscreen/refdes not optimized (rigid ride-along, collisions expected).
8. No PRC waivers, no severity tiers, no thermal placement check (only trace heating),
   no crosstalk/proximity check yet, no neck-down check yet.
9. KiCad region-membership assignment is manual; pours need manual name registration;
   internal-layer copper preservation conditional on stackup lock.
10. Placement regions can only force TOP-side single-sided (no bottom-only preference).
11. Over-constrained jobs fail outright rather than reporting best-effort with diagnostics.

## 10. Parity checklist for fluxplace (what "at least as good" means)

**Must-have (parity):**
- [ ] Netlist-driven clustering (connectivity → groups) + anchor stickiness.
- [ ] Hard constraints: pre-placed (pos+rot), per-side placement region polygons,
      keepouts, courtyard/DRC-clean guarantee (never emit a violating placement).
- [ ] Auto-detection at Quilter's level: bypass caps (topological + pin-name priority +
      capacitance-ordered distance), crystals (X/Y refdes + direct parent), switching
      converters (U→L + Cin/Cout hot loop), power nets (name/netclass + 200/500 mA
      defaults), diff pairs (suffix conventions + V-exclusion).
- [ ] Pre-route placement scoring: Pin Distance (pin-edge Euclidean) per constraint pair,
      estimated loop area, plane-availability check, half-perimeter wirelength,
      congestion/pin-density proxy for routability.
- [ ] Candidate machinery: multiple candidates, lexicographic ranking
      (DRC → completion-proxy → priority constraint scores → conservativeness → length),
      single-sided-first exploration.
- [ ] Iteration contract: partial-lock re-runs (geometry-based lock is fine and simple).

**Surpass (from §9):** rotation-only/side-only locks; connector auto-detection (footprint/
refdes J/CN/P heuristics + edge affinity); series-R crystal + load-cap modeling; richer
converter topologies incl. feedback-node keepaway; multi-channel symmetry replication;
silkscreen collision repair; thermal placement spreading; best-effort mode with
constraint-conflict diagnostics instead of hard job failure.

---

*Companion sources: full per-section reader notes are preserved in this session's task
outputs; raw fetched .md copies of key pages are in the session scratchpad. Every doc page
also serves clean markdown at `<url>.md` and answers questions via `<url>.md?ask=…`.*
