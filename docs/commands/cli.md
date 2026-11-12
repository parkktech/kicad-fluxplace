# CLI reference and placement rules

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
| `fab` | Emit the gerbers/drill/place/DRC package for review; runs the `review` gate first and **aborts on FAIL**. Wipes `gerbers/`, `drill/` and `place/` before every export, so a re-cut into the same `--out` never leaves a prior cut's files behind, and `MANIFEST.txt` lists exactly the files that export actually wrote. |
| `deliver` | Package a fab output for ordering: **PCBWay's four upload slots as four numbered files** (gerber zip / BOM / centroid / assembly instructions) plus loose readable docs (brief, order worksheet) for whoever places the order. `--no-pcbway` for a single CAM zip. Zips exactly the files `fab`'s `MANIFEST.txt` lists — a stale file left in the output directory by an old export can no longer reach the zip. |
| `pcbway` | PCBWay's Assembly quote form, field by field, answered from the board: size, layers, track/space tier, drill, finish, sides, unique/SMD/fine-pitch/THT counts, consign list. `CHOOSE` wherever the design has no opinion. |
| `tournament` | Candidates × `--profiles` (fab rule bundles) → gate → freerouting → **lexicographic rank: DRC → completion → PRC passes → conservativeness → vias → wirelength last**. |
| `review`  | **The design-review gate** (see below): spec net rules, diff-pair skew/layers, RF impedance on the layer the copper is on, DigiKey/Mouser package · pin count · temperature vs footprint and `[env]`, spec pinmap vs the KiCad official symbol, spec/board sync, hold-up and TVS margin, **via type** (blind/buried/micro FAIL unless `[fab]` allows it). `fab` and `deliver` run it and **abort on FAIL** (`--no-review` to override, `--waive CODE:REGEX` per finding). |
| `repair`  | Copper repairs the review gate asks for: remove router loops/stubs on 2-pin nets, re-width RF segments to what **their layer** needs, remap pads to corrected nets (rips the old stubs, drops a GND via beside pads that become ground), net the unnetted twin of a same-numbered pad, add silkscreen text in a free spot. `--patch` closes what a remap left unrouted; `--bridge REF:PAD` maze-routes one pad the patcher and freerouting both gave up on — multi-layer Dijkstra on a grid with every foreign track/via/pad rasterised at clearance, layer changes only where a through via clears every copper layer, DRC-guarded and zones refilled on accept. |
| `review` also carries **land-pattern citations** (`landpattern` on a spec component: source page + pitch/pad/rows/pins, measured against the footprint; project-drawn footprints without one FAIL) and reads **operating temperature from the datasheet text**, preferring it over the distributor field when the two disagree. |
| `drc-fix` | Fix the DRC noise a repair leaves behind, from the report's own items: rip a track that runs into a swapped footprint's pad, neck a widened RF segment at a clearance pinch, push a via off another via/track by a few µm, snap or delete stray track ends, move colliding reference text; loops DRC until the count stops falling. `--island-vias` drops a via beside a plane pad whose pour island has none (DRC-guarded). |
| `finish`  | Route named nets with freerouting (planes declared as `power` in the DSN) and take back **only those nets' copper**, kept only if DRC does not worsen and the unconnected count falls — for the connection the grid patcher cannot close. |
| `tune`    | DRC-guarded differential-pair length tuning: hairpin shortcuts on the long side, serpentine meanders on the short side, each step kept only if `kicad-cli` DRC does not get worse, until every pair is inside its `[pairs.*] skew_mm`. |
| `sourcing --china` | Grade every MPN on **JLCPCB/LCSC live stock** as well (`CN_OK/LOW/NONE/ABSENT`): what a Shenzhen assembly line actually pulls. `[sourcing] china = true` makes it part of `review`; `[sourcing.cn_alias]` maps an MPN to the string LCSC catalogues it under. |
| `datasheets` | Fetch every MPN's datasheet into the project through the DigiKey/Mouser APIs, hash it into `datasheets.json`, list the ones that need a browser (`--adopt MPN=file.pdf` registers a hand-dropped PDF). |
| `spec-check` | **Documentation gate** on a netlist spec: every part has an MPN, its datasheet on disk, and a `pinmap` whose names appear on the cited datasheet page (`pinmap_source: "X.pdf#p3"`). `schematic --datasheets` refuses to generate from an undocumented spec; `review` fails the same way (`[docs] strict = true`). |
| `intake`  | Design interview → `design_intent.json`. Now also asks **where the product lives** (temperature range, vibration, moisture, input-transient class); `--constraints-out` writes the `[env]` block the review gate derates against. |
| `models`  | Real vendor 3D bodies for footprints `review`'s `check_models` would FAIL: DigiKey CAD-media fetch (default), or `--check` to just list every missing/broken model (no network, exit 1 if any) and `--fetch` to pull the missing ones from EasyEDA by LCSC code (`easyeda2kicad`, the `[models]` extra) and attach them with provenance, exiting 1 if anything stays unresolved. |
| `verify-models` | Verify 3D models sit ON their pins/footprint — pin shafts in TH holes, an SMD body over its courtyard, and (front **or back** copper layer) a seat-gap check that catches a body floating off or buried into the board, including models rotated on more than one axis to stand a sideways-authored STEP upright. Also checks posture, upside-down, library-transform drift, and mated-overlay registration (see **Placement rules** below). `--fix` solves + writes correcting transforms; `--constraints` feeds `[docs] project_libs` into the library-transform check. |

#### Placement rules

Beyond pins-in-holes, `verify-models` (`model_verify.verify_footprint`) runs
these checks on every modelled footprint — all WARN-level except the last:

- **seat** (`seat_gap`) — the model's own seat face floats off or is buried
  into the mount plane, on either copper layer.
- **posture** (`posture_gap`) — the body looks like it's standing on edge:
  one XY extent has collapsed to well under half the matching Fab/Courtyard
  dimension while Z is close to what's missing — a 90° X/Y rotation away
  from flat.
- **upside-down** (`buried_mass_gap`) — most of the body's own point mass
  sits below the mount plane after its offset, with some part still above
  it (catches a badly-inverted seating that `seat_gap` intentionally
  declines to judge; does not catch every upside-down case — see
  `NEXT.md` 2026-09-07 for the one it misses and why).
- **library transform** (`library_transform_diff`) — the board's FP_3DMODEL
  offset/rotate disagrees with the same-named KiCad/project library
  footprint's own (WARN when it's still the same model file, so the
  transform alone diverged; INFO when the file differs too — a
  deliberately swapped-in body).
- **mated overlay** (`overlay_registration_gap`) — a second, much-larger
  body on a footprint (a module drawn on its mating connector) doesn't have
  enough of its own geometry landing near the mount plane inside this
  footprint's actual pad field.
- **SMD leads** — a connector's SMD signal leads aren't on their pads
  (independent of any TH pin-in-hole fit).
- **TH pins** — a connector's through-hole pin shafts aren't in their
  holes.

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
