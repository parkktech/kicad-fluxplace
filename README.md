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

## Autorouting

fluxplace optimizes for exactly what an autorouter wants — short, uncrossed nets. Workflow:

1. `place` the board with fluxplace.
2. Export Specctra DSN (`pcbnew.ExportSpecctraDSN`) and route with
   [Freerouting](https://github.com/freerouting/freerouting) headless
   (`java -jar freerouting.jar -de in.dsn -do out.ses -gui.enabled=false`).
3. Import the `.ses` back into KiCad.

Lower HPWL from `eval` correlates directly with higher autoroute completion.

## Requirements

fluxplace is a **KiCad 10 engineering suite**, not a standalone python package. It
drives KiCad, an autorouter, an office suite and two distributor APIs. Before doing
anything else, run the preflight — it checks every requirement and tells you exactly
what is missing and how to get it:

```
python3 cli.py doctor            # full report
python3 cli.py doctor --install  # install what it can
```

The KiCad plugin runs the same check the first time you invoke it, and offers to
install the missing python packages for you.

| Tier | Requirement | Needed for |
|---|---|---|
| **core** | **KiCad 10.0+** (`pcbnew` + `kicad-cli`) | everything — there is no fallback |
| **core** | numpy | quadratic placement solve, geometry |
| fab | python-docx, openpyxl, Pillow | fab brief, order worksheet, `.xlsx` upload twins, renders |
| fab | LibreOffice (`soffice`) | assembly instructions as PDF — PCBWay rejects `.docx` on that field |
| route | Java 17+ and **freerouting 2.3.0+** | the autorouter. 2.2.4 dies silently on headless jobs — pin 2.3.0 |
| sourcing | `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET`, `MOUSER_API_KEY` | the availability gate and real-STEP 3D fetch |

**Sourcing policy: DigiKey and Mouser, nothing else.** Two APIs that answer
authoritatively, with credentials, and can be held to account for stock and
price. No LCSC/jlcparts, no SnapEDA, no vendor CDNs — the jlcparts index behind
LCSC search has returned HTTP 404 for weeks at a time, mid-project, twice, and a
gate whose answer depends on a third-party mirror being up is not a gate.
JLCPCB and PCBWay still appear throughout as **fabricators** — DFM profiles,
trace floors, stackups, order worksheets. A fab is where the board is made, not
where the parts are bought.

Put the router jar at `~/tools/freerouting-2.3.0.jar`, or point `FREEROUTING_JAR` at it.

### The interpreter trap — read this one

`pcbnew` ships with KiCad, is **not on PyPI**, and is importable only from the
interpreter KiCad installed it for. On Linux/WSL that is `/usr/bin/python3` — *not*
conda, *not* pyenv, *not* a fresh venv.

That interpreter is usually PEP 668 externally-managed, so `pip install` refuses it.
Installing `openpyxl` into a conda python instead appears to work and then fails at
run time, because that python has no `pcbnew`. **This is the single most common way
to break this suite.**

**The fix, and it needs no root.** Create a venv that *inherits* system
site-packages. A plain venv is useless here — it would not have `pcbnew` — but
`--system-site-packages` inherits `pcbnew`, `wx`, `numpy` and `Pillow` from the
system interpreter while giving you a writable `site-packages` that PEP 668 does
not police. One interpreter ends up with everything, and nothing on the system
python is touched:

```
/usr/bin/python3 -m venv --system-site-packages ~/.fluxplace-venv
~/.fluxplace-venv/bin/pip install python-docx openpyxl
~/.fluxplace-venv/bin/python cli.py doctor      # -> All checks passed
```

`fluxplace doctor --install` does exactly this for you, and once the venv exists
it installs into it automatically.

If you would rather use root, the distro packages work too:

```
sudo apt-get install -y python3-docx python3-openpyxl python3-numpy python3-pil
```

### Wider toolchain

These are configured in your MCP/plugin host, not by pip. `doctor` lists them but
cannot autodetect them:

- **MCP `kicad`** — project analysis, ERC/DRC, BOM, netlist, thumbnails
- **MCP `kicad-pro`** — DFM checks, BOM-with-pricing, component-contract verification
- **MCP `kicad-jlcpcb`** — board generation from a netlist spec, JLCPCB fab packaging
- **`kicad-happy`** — datasheets plus the digikey/mouser skills
- **`pcb-designer`** — DFM, stackups, RF layout guidance

## Install

**Manual (dev):** clone this repo into KiCad's 3rd-party plugin directory (or symlink it):

- Linux: `~/.local/share/kicad/10.0/3rdparty/plugins/`
- Windows: `%APPDATA%\kicad\10.0\3rdparty\plugins\`
- macOS: `~/Documents/KiCad/10.0/3rdparty/plugins/`

Restart KiCad → Tools → External Plugins → *fluxplace*. Installed once, it's available in
**every** project. On first run it preflights the requirements above and offers to
install what it can.

**PCM:** add this repo's `metadata.json` as a Plugin & Content Manager repository for
one-click install + updates.

## MCP server

fluxplace exposes its commands as Model Context Protocol tools, so an agent can
analyse boards, audit DRC scope, check part availability and build fab packages
directly.

```
python3 -m fluxplace.mcp_server            # 20 tools (read + write)
python3 -m fluxplace.mcp_server --all      # + the long-running pipelines
python3 -m fluxplace.mcp_server --list     # show the tools and exit
```

Register it alongside your other KiCad servers:

```json
{
  "mcpServers": {
    "fluxplace": {
      "command": "/home/you/.fluxplace-venv/bin/python",
      "args": ["-m", "fluxplace.mcp_server"],
      "env": { "PYTHONPATH": "/path/to/kicad-fluxplace" }
    }
  }
}
```

Use the interpreter that can import `pcbnew` — the venv from the section above,
or your system python. The server has **no third-party dependencies**: it speaks
JSON-RPC 2.0 over stdio directly, so it runs anywhere the CLI runs, including on
a PEP 668 distro python where installing an SDK is the exact friction `doctor`
exists to remove.

**Tool schemas are derived from the CLI parser**, not hand-written. Add a flag to
a subcommand and the MCP tool gains it on the next start; there is no second
description of the commands to drift.

Commands are classified by what they cost you. `read` (analysis, never touches
the board) and `write` (produces files) are exposed by default. `long` — `auto`,
`tournament`, `compact`, `place`, `patch`, `launder` — are minute-scale pipelines
that rewrite the board, which is the wrong shape for a synchronous tool call, so
they are behind `--all`.

### Token budget

Tool output lands in the caller's context and stays there for the rest of the
conversation, so results are capped (default 6000 chars, `FLUXPLACE_MCP_MAX_CHARS`
to change). Past the cap the full text is written to a temp file and the reply
carries the head, the tail and the path — the two ends of a report are where the
summary and the verdict live, and the middle is the enumeration you can grep.
Truncation is always stated; a silently-shortened result would be the same
failure as a DRC report that does not say what it skipped.

`fluxplace_netlist` on a 143-part board went from ~5,400 tokens to ~925 capped,
or ~236 with `summary=true`, which returns counts, the largest nets and any
single-pad nets — the shape plus the outliers, which is what the full dump was
usually being read for.

### Board audit tools

Three of the tools exist because of specific misses on real boards, and they
answer questions nothing else in the toolchain does:

- **`fluxplace_drc_scope`** — what a DRC result actually *examined*. A board can
  report "0 violations at all severities" while a dozen rules sit at `ignore` in
  the `.kicad_pro`, and a rule set to ignore is not reported at any severity. It
  names which checks are off, flags the ones that are fab-critical (solder-mask
  bridging, annular width — the ones that pass every automated check and then
  bite at assembly), and with `full=true` re-runs DRC with every check enabled on
  a throwaway copy and reports what newly surfaced.
- **`fluxplace_netlist`** — the connection list read back out of the routed
  board. A board generated from a netlist spec has no `.kicad_sch` at all, and
  this is then the only connectivity document that exists.
- **`fluxplace_stackup`** — layer stack, which layers carry plane pours, the
  netclass track and differential-pair geometry, and a straight answer on whether
  controlled impedance can be verified from the files at all. It cannot if no
  dielectric is defined, however precise the netclass looks.

### The design-review gate (`review`)

An outside reviewer looked at a finished board — 0 DRC at full scope, netlist
verified, sourcing graded, packaged for the fab — and found nine problems in an
afternoon. Every tool had been green, because every tool checked the board
against **itself**: schematic vs copper, netclass vs track, pad vs pin. None
checked it against the **spec**, the **datasheet** or the **environment**.
`fluxplace review` does, and `fab`/`deliver` refuse to package a board with a
FAIL in it.

| Finding | What it holds the board against |
|---|---|
| `NET_STRAIGHT_COPPER` | `[nets.X] straight_copper = ["J1:1","J2:1"]` — the net may touch **only** those pads. Caught two ESD arrays sitting on a fail-safe PTT line the spec said no component may touch. |
| `PAIR_SKEW`, `PAIR_LAYER_MISMATCH`, `PAIR_VIA_MISMATCH` | P and N of every diff pair (by name convention) must arrive together, on the same layers, through the same via count. Caught a 45 mm intra-pair mismatch on a Gigabit pair. |
| `RF_IMPEDANCE_OFF`, `RF_VIA_COUNT` | RF-named nets graded segment by segment **on the layer the copper is actually on**, against the planes that layer really sees (microstrip on the outside, asymmetric stripline inside). A 0.15 mm trace is 50 Ω on a 0.1 mm outer prepreg and ~66 Ω on the inner layer where 85 % of the net was routed. |
| `FOOTPRINT_PACKAGE_MISMATCH`, `PIN_COUNT_MISMATCH` | The distributor's package for the MPN (DigiKey `Supplier Device Package` / Mouser `Package / Case`) vs the footprint name, by package family and pin count. Caught a PowerDI5060-8 MOSFET on a SOIC-8 land pattern. |
| `PINMAP_ROLE_MISMATCH`, `PINMAP_PIN_ABSENT`, `PINMAP_UNVERIFIED` | The spec's pinmap vs the **KiCad official symbol** for the same part (`/usr/share/kicad/symbols`, prefix-matched on the MPN). Ground on the wrong pin is a FAIL; a pinmap with no library match and no `pinmap_source` is a WARN — evidence, or it is a guess. |
| `TEMP_RATING` | Operating temperature from the distributor vs `[env] temp_min_c/temp_max_c`. Caught a 0..+70 °C LAN transformer on an outdoor vehicle product. |
| `SPEC_SIZE_MISMATCH`, `SPEC_LAYER_MISMATCH`, `SPEC_COMPONENT_MISMATCH`, `SPEC_FOOTPRINT_MISMATCH` | The spec JSON vs Edge.Cuts, the copper layer count, and the component list. A spec that says 65 × 50 mm 4-layer for a 76 × 87 mm 6-layer board is a document nobody is reviewing against. |
| `HOLDUP_SHORT` | `[power."+5V"] holdup_ms / nominal_v / min_v / load_a` vs the bulk capacitance actually on the rail. 3000 µF holds 5.0→4.75 V at 1 A for 0.75 ms, not "tens of ms". |
| `TVS_MARGIN` | `[protection]` clamp voltage vs the downstream device rating (clamp from the constraints or from the distributor data). |
| `ENV_UNDEFINED` | Nobody answered the environment questions, so nothing can be derated. |

**No part without its papers.** With `[docs]` in the constraints (`datasheets =
"docs/datasheets"`, `strict = true` — the default) the gate fails a part that
has no manufacturer part number (`MPN_MISSING`), whose datasheet is not on disk
in the project (`DATASHEET_MISSING`), that uses more than two pins without a
named pinmap (`PINMAP_MISSING`), or whose pinmap names are not found on the
datasheet page it cites (`PINMAP_EVIDENCE_WEAK`). A distributor that cannot
describe the part is a FAIL too (`PART_DATA_UNAVAILABLE`). The rule exists
because a board reached an outside reviewer with an ESD array's ground on the
wrong pin — the pinmap had been typed from memory and no tool had asked for
the page it came from.

Datasheet fetching does not need a human: `datasheets` asks DigiKey, Mouser
and Nexar for the URL and downloads it with **`curl_cffi` impersonating
Chrome** (`pip install fluxplace[docs]`), which is what the manufacturer CDNs
that 403 a plain client (Amphenol, Littelfuse, onsemi, C&K, Molex, measured)
actually check. A host that is unreachable outright is reported for a browser
fetch and registered with `--adopt`.

Part data comes from the **DigiKey and Mouser APIs**, with **Nexar (Octopart)**
as the third source when both miss — its datasheets are mirrored on
`datasheet.octopart.com`, which serves plain PDFs where manufacturers'
own hosts bot-wall a script (`NEXAR_CLIENT_ID` / `NEXAR_CLIENT_SECRET`,
supply.domain). Nexar meters part lookups (10/day on the evaluation plan),
so it is consulted only after both distributors miss (same credentials as
`sourcing` and `models`), cached for 7 days beside the `mpn_map.json`. A part
the distributors cannot describe is reported as `PART_DATA_UNAVAILABLE`, never
silently passed. `--no-api` runs the offline checks alone.

```bash
PYTHONPATH=$KP python3 cli.py review --board board.kicad_pcb \
    --spec spec.json --constraints constraints.toml --json review.json
PYTHONPATH=$KP python3 cli.py fab --board board.kicad_pcb --out fab/ \
    --spec spec.json --constraints constraints.toml      # aborts on FAIL
```

Closing the findings is tool work too — `repair` and `tune` are the mechanical
fixes (`repair --pairs ETH_ --rf --remap map.json --fix-pads --text "…"`, then
`tune --prefix ETH_`), and `[review] waive = ["CODE:REGEX", …]` in the
constraints records a waiver *with the file it lives in*, so a waived finding
stays visible in review instead of disappearing.

Constraint blocks the gate reads (all optional, see `fluxplace/constraints.py`):
`[env]`, `[nets.<NET>]` (`straight_copper`, `max_vias`), `[rf]` (`target_z`,
`tolerance_pct`, `max_vias`, `nets`), `[pairs.<FAMILY>]` (`skew_mm`),
`[power."<RAIL>"]` (`holdup_ms` …), `[protection]`, `[fab]` (`blind_vias`,
`buried_vias`, `microvias`, `min_track_mm`, `min_clearance_mm`,
`min_drill_mm`, `min_annular_mm`, `min_via_dia_mm`, `min_silk_width_mm`,
`profile`).

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

## KiCad 11 — what works without `pcbnew` (2026-11-10)

KiCad deprecated the SWIG `pcbnew` python module in 9.0 and plans to drop it in
11; its replacement (the IPC API, `kicad-python`) needs a running GUI and has no
headless mode yet. As of 2026-11-10 there is **no KiCad 11 release** (latest
stable 10.0.8; the public `kicad/kicad:nightly` image is an 8-month-old
10.0.0-rc1), so nothing here could be run against a real KiCad 11 — it is
verified by removing `pcbnew` from the interpreter instead.

| Capability | Without `pcbnew` | How |
|---|---|---|
| `review` (every gate), `lock`, `fab`/`deliver`'s review step | **works** | `fluxplace/boardfile.py` reads the `.kicad_pcb` S-expressions directly and builds the same `facts` dict. `review.board_backend()` picks `swig` when `pcbnew` imports, else `file`; `FLUXPLACE_BACKEND=file\|swig` forces one. |
| DRC, ERC, gerbers, drill, renders | **works** | already `kicad-cli` |
| `place`, `repair`, `patch`, `finish`, `tune`, `planes`, `verify-models`, DSN export | **needs KiCad <= 10** | ~300 `pcbnew` call sites in 25 modules; the port target is `kicad-python` once KiCad ships a headless API server |

Contract: `tests/test_boardfile.py::test_file_backend_matches_swig` compares the
two backends field by field (set `FLUXPLACE_TEST_BOARD` to add a real board). On
utv-comms V1.5 (150 footprints, 2473 track segments) they agree exactly apart from
footprint bounding boxes (max 0.002 mm), and a full `review` gives identical
findings on both. `fluxplace doctor` prints the active backend.

## Part-count optimization — `bom-opt` and the `[bom]` budget

"Less is better": find ways to build the same circuit with fewer placements and
fewer distinct part numbers. `fluxplace/bomopt.py` is pure Python (no pcbnew, no
network); every finding is a *candidate* — it never claims two parts are
interchangeable, so check ratings and tolerances before acting.

```
fluxplace bom-opt --spec spec.json --mpn-map mpn_map.json --constraints c.toml [--json out.json]
```

Prints a summary block (total/fitted/DNP, unique parts, by-kind, passive ratio),
findings grouped by code, and `Potential reduction: N placements, M line items`.
Exit 1 only on a FAIL budget. `review` runs the budget part only (plus one
`BOM_SUMMARY` line) when the constraints have a `[bom]` table and `--spec` is given.

| Code | Meaning |
|---|---|
| `BOM_BUDGET_PARTS` / `BOM_BUDGET_UNIQUE` (FAIL), `BOM_BUDGET_DNP` (WARN) | Fitted parts, distinct part numbers (distinct values with no MPN map) or DNP parts over the limit. Only when set. |
| `BOM_ARRAY_CANDIDATE` | `array_min`+ same-value, same-package resistors sharing a net (pull-ups/downs): one 4-resistor array replaces four. |
| `BOM_VALUE_MERGE` | Same kind and package, values within `merge_tolerance` of a more-used value: drop a line item. Never across kinds or for 0. |
| `BOM_PACKAGE_SPLIT` | Same kind and value in several chip sizes: use one. Refs are the minority package. |
| `BOM_SINGLE_USE` | One summary: passive part numbers (values) used exactly once, each a reel. |
| `BOM_PARALLEL` | Resistors across the same two nets; three or more bulk (>= 1 uF) equal caps across the same two nets. Bypass-size caps are skipped: per-pin decoupling is deliberate. |
| `BOM_SERIES_ZERO` | Fitted 0-ohm links: delete or make a solder jumper. |
| `BOM_DNP_COUNT` | Lists the do-not-populate parts. |

```toml
[bom]
max_parts = 120          # FAIL above this many fitted parts
max_unique = 60          # FAIL above this many distinct part numbers
max_dnp = 4              # WARN above this many DNP parts
array_min = 4            # resistors on one net before suggesting an array
merge_tolerance = 0.1    # 10 % value spread counts as "the same value"
exempt = ["MH*", "J*"]   # refs ignored entirely (MH/MK/H/FID/TP always are)
keep_values = ["R12"]    # refs whose value must not be merged
keep_links = ["R40"]     # 0-ohm links that must stay
```

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

## Routable placement (2026-11) — net classes, clusters, anchors, pre-route gate

Built after a 337-part board passed "0 overlaps, 0 shorts" and then left 372
connections open: every cause was visible in the netlist before routing. Seven
stages, each usable alone; all read the board as text (`routediag.load`).

### 1. Net classes — `fluxplace/netclass.py`

Every net gets a class, a pull weight and a length budget (mm). Order:
rf > pair > switch_node > clock > rail > bypass > slow > signal. Each default has a physical
basis (`netclass.BASIS`); defaults with none were loosened on 2026-11-11 (round 2).

| class | budget (old -> new) | weight | physical basis |
|---|---|---|---|
| switch_node | 6 (unchanged) | 10 | loop inductance ~1 nH/mm of narrow trace: 6 mm ~ 5 nH in the switching loop |
| bypass | 3 (unchanged) | 8 | loop inductance: 3 mm ~ 2.5 nH keeps the cap's SRF above 100 MHz |
| rf | 15 -> 17.7 | 6 | insertion loss, FR-4 microstrip 0.0118 dB/mm/GHz: 0.5 dB at 2.4 GHz. `[placement.rf] freq_ghz = 1.575  loss_db = 0.5` recomputes (26.9 mm for GNSS) |
| clock | 20 (unchanged); crystal 8 | 5 | edge rate ~0.7 t_r/(6 t_pd), t_r 1 ns, t_pd 6 ps/mm. Crystal 8 mm = ~0.8 pF stray C against a load-cap tolerance of ~1 pF; an ACTIVE oscillator (>= 3 pads with a supply pin, e.g. a SiT/CDC part) is a plain clock, not a crystal (it was held to 8) |
| pair, usb2 | 30 -> 100 | 4 | USB 2.0 HS spec 5 in host-side trace, derated; skew <= 3 mm (`[pairs.USB_]`) |
| pair, usb3 | 30 -> 100 | 4 | 5 Gb/s ~1 dB/in at 2.5 GHz Nyquist, ~6 dB board budget |
| pair, pcie | 30 -> 100 | 4 | Gen2/3 x1 ~1.2 dB/in at 4 GHz, ~8 dB budget |
| pair, eth | 30 -> 100 | 4 | 1000BASE-T PHY to magnetics: vendor guidance <= 100 mm, skew <= 1 mm |
| pair, other | 30 -> 60 | 4 | speed unknown: ~1.5 ns edges, reflection-critical length |
| pair, audio | 30 -> none | 4 | MIC/SPK/INJ/HPL/ADC/DAC/MIX... analog pair: only noise pickup matters |
| signal | 30 -> 60 | 1 | digital edges >= 2.5 ns: t_r/(6 t_pd) ~ 62 mm before a stub needs termination |
| slow (new) | none | 1 | I2C, reset, interrupt, LED, key, status, boot, PPS, PTT, CAN <= 1 Mb/s, analog audio: kHz-class, effectively unbounded (SPI/I2S clocks and antenna nets are excluded by name) |
| rail | none | 0 | ground, `[power.*]` nets, power-named nets (`+3V3`, `CM5_3V3`, `VBAT...`), nets with >= 8 pads; current capacity, not length; never pulled |

Pair kind comes from the net name (`netclass.pair_kind`). `[placement.budget] pair = N` sets every
kind; `"pair:usb2" = N` one kind. Multi-drop nets use MST length against
`budget * (1 + 0.5 * (parts - 2))` (distinct references). Override in the constraints TOML:

    [placement.budget]  switch_node = 5
    [placement.weight]  pair = 6
    [placement.net.PCIE_TX_P]  budget = 45  weight = 8  class = "pair"
    [placement.budget."USB_HUB_DP"]   # per-net budget with a recorded reason
    mm = 60
    reason = "hub is on the far side of the CM5 by the mechanical stack"

Every per-net override is listed by `route-ready` ("budget overrides in effect", with its reason,
the length now and ok/OVER) and its reason is appended to that net's NET_OVER_BUDGET message.

**ANCHOR_SPACING** (WARN) vs **NET_OVER_BUDGET** (FAIL): the floor of a net is the MST over the pads
of FIXED parts (locked, `[lock.at]`, `[placement] fixed`, mounting hardware, detected anchors) - a
sub-tree no move can shorten. When the floor alone exceeds the budget the net is ANCHOR_SPACING with
that minimum (multi-pad nets included); only the excess beyond 1.15 x floor + 1 mm, which movable parts
add, stays a FAIL (against the floor as its limit).

### 2. Clusters — `fluxplace clusters BOARD [--apply --out OUT]`

For every IC (`U*`/`IC*`, >= 5 pads) the 2-pin passives, ferrites and crystals that
serve it. Ownership: a pad on a net that reaches exactly one IC; for decoupling on
a shared rail, the cap's reference-designator block (C416 -> 4xx), then the nearest
reference number (U410), then an even spread over the rail's supply pins (a guess,
marked and never graded). A capacitor on a net that also reaches a connector is
treated as rail decoupling. A rail cap is never handed to an arbitrary far IC. A
part whose two pads reach two ICs is a chain link, not support. `route-diagnose`
`FAR_SUPPORT_PART` uses the same ownership (it used to blame the nearest IC).

`--apply` re-seats only the parts beyond their limit (switch-node 6 mm, bypass
3 mm, crystal 8 mm, other 5 mm; `--all` re-seats everything, `--guess` includes
guessed owners), ICs never move. A part keeps its footprint; origin and rotation
change (pads move with it, written as text by `boardwrite.py`). Candidate seats are
ranked by pad-to-pin distance (caps add a ground-return term) and must stay out of
every other courtyard, the IC's escape lanes (1.0 mm beyond fine-pitch pads), the
board outline and track-forbidding rule areas. Existing copper is not updated:
apply on an unrouted board.

**Legal re-seat (round 2).** Every candidate seat also goes through `fluxplace/legality.py`
(`Legal.part_issues`): same-side courtyard overlaps (a through-hole part counts on both sides),
the real outline with rounded corners and cut-outs (Edge.Cuts arcs are flattened; before this only
the bounding box was tested), named keep-outs and side regions, track-forbidding rule areas (a
footprint's own rule areas are exempt), through-hole pads in no-through-hole regions, and locked /
fixed parts never move. A part with no legal seat within its limit is LEFT IN PLACE and listed
(`--best-effort` accepts a legal seat farther than the limit). After writing, `clusters --apply`
re-checks the whole board and prints the new violations against the input (target: 0).

    [placement.keepout.cm5_module]      # no part of side "B" (a through-hole part counts on both) inside
    rect = [0, 5, 55, 45]               # x0, y0, x1, y1
    side = "B"                          # "F" | "B" | "both"
    allow = ["J10", "J11"]              # refs/globs exempt
    reason = "CM5 body over the B-side sockets"
    [placement.no_tht.vib_zone]         # no through-hole pad inside (checked by route-ready too)
    rect = [10, 10, 20, 20]

### 3. Anchor orientation — `fluxplace anchor-check BOARD [--apply --out OUT]`

Connectors, modules and sockets are auto-detected (J/P/CN/X refs with >= 6 pads,
U refs with >= 40 pads; never MH/MK/H/TP/FID) or named in `[placement] anchors`.
Each allowed rotation is a true rigid rotation (pads move with the footprint) scored
with every other pad where it is: class-weighted MST length of the nets the anchor
touches, plus the worst pair span. Options that leave the outline or land on
another anchor are marked infeasible. `ANCHOR_MISORIENTED` when the current
orientation is more than `[preroute] anchor_tolerance_pct` (15) worse than the best
feasible one. `--apply` turns them one at a time (re-scoring in between).

    [placement]
    anchors = ["J200"]
    [placement.anchor.J200]       # rotations are DELTAS from the current orientation
    rotations = [0, 90, 180, 270] # default; a part within 1.5 mm of the outline is an
                                  # edge connector and only keeps its orientation
    [placement.group.cm5]         # a two-connector module turns as one body
    refs = ["J10", "J11"]
    centre = [30, 25]
    rotations = [0, 180]

Side flips are not evaluated (the text writer cannot mirror a footprint).

### 4. Connection-driven placement — `place --strategy routable`

    fluxplace place --board B.kicad_pcb --strategy routable --constraints C.toml --spec S.json --out OUT

Text-only (no pcbnew). Fixed: locked parts, `[lock.at]`, mounting hardware, detected
anchors, `[placement] fixed`. Misoriented anchors are first turned to their best
rotation (`[placement] fixed` anchors excepted; give a group `rotations = [0, 180]`).
Every confident support part is laid out beside its own IC as a macro cell, so a unit
(IC + support) moves as one rigid body (units whose box is > 2.5x their parts' area
move piecewise). Units go to the class-weighted centroid of their external nets (rails
excluded), are rotated 0/90/180/270 when that cuts weighted length plus crossings
among their own nets by > 8 %, and parts in a chain of 2-pad nets between two fixed
anchors are ordered monotonically along the line between them; 3 rounds of
{25 sweeps, rotate, legalise (stage 5)}. Prints the route-ready scorecard before and
after. Result is deterministic. `auto --pre-place routable` and
`tournament --pre-place routable` run it first. Existing copper is not moved.

### 5. Legalise with channels — `fluxplace/legalise.py`; the `pack` fault

`legalise(B, targets, fixed, cons, hot)` removes overlaps by pushing movable parts
along the axis of least penetration (relative order survives), never moves fixed or
locked parts, handles both board sides (through-hole parts occupy both), and keeps
clear: fine-pitch ICs (pitch <= 0.5 mm) inflated per side by `max(0.6, 0.06 x pins
on that side)` mm; connectors with >= 40 pads 1.5 mm beside the pad rows;
track-forbidding rule areas (their own part, e.g. the antenna, exempt); `[[keepout]]`
rectangles (through-hole always, SMD when the keepout names the side and has no
`max_height_mm`; `allow` globs exempt); and corridors, i.e. cells where the coarse
demand map exceeds capacity (passives are lifted out when a legal seat is near).
It raises `PlacementError` instead of returning a part outside the outline or an
overlap it could not remove.

**The `pack` fault (> 1 m):** `placement._compact_blocks` pulled each block toward its
neighbours' centroid and pushed overlaps apart asymmetrically; neither has a fixed
point, so the whole arrangement walked ~4 mm per sweep (2 m after 500 sweeps on the
337-part board, 12 sheet blocks) while locked parts stayed at their true
coordinates and `place` shrink-wrapped the outline around both. Fixed by holding the
area-weighted centroid every sweep and falling back to the shelf layout when
compaction ends bigger than it started. Also: `place` now centres on the board when
parts are locked, and **every** path (`place` all strategies, `auto`) calls
`placement.check_extent` before writing: with a fixed outline (`--no-outline`) a part
outside it, with a redrawn outline a result more than `--max-growth` (4; `auto` 6) x the
starting area, or > 1 m, aborts with `REFUSED` and writes nothing.

### 7. Routing stalls — `auto` / `compact` (`fluxplace/stall.py`)

* Per-stage wall-clock limit: `--route-timeout`, else `[route] stage_timeout_s`, default **900**
  (was a fixed 1800 s). On a timeout the router is not retried.
* On a timeout or any failed net: `route-diagnose` runs on the routed board and its text/JSON
  is always written (`OUT/route-diagnose-N.{txt,json}`). With `--replace-on-stall` (<=
  `--stall-rounds`, 3) the move list is applied to UNLOCKED parts (support parts re-seated
  via clusters, others moved to within ~3 mm of their target pad and legalised), only the
  tracks/vias of nets touching a moved part (plus the failed ones) are removed
  (`boardwrite.strip_nets`), and only those nets are re-routed. What moved is printed.
* Escape-first: nets of fine-pitch ICs (pitch <= 0.5 mm, >= 5 pads) are routed in a first
  router pass, everything else (power included, wide) in a second with that copper kept
  (`--no-escape-first` turns it off). **Implemented as net order, not as escape stubs or
  temporary lane keep-outs:** neither external router takes a per-net keep-out, and no
  router was run in this work to verify one.
* Verified with a stub router in `tests/test_stall.py`; a live KiCadRoutingTools run of
  `auto` was not done.

### 8. One objective, hard budgets, side/height/connector/stale checks

* **Single score** (`preroute.score`, printed by `route-ready`, minimised by `routable`):
  class-weighted ratsnest mm + 20 x mm over budget on rf/switch_node/bypass/pair nets
  (HARD) + 3 x mm over budget on other nets + 5 x mm support parts are beyond their limit
  + 500 x each PIN_ESCAPE_BLOCKED / KEEPOUT_TRAPPED / CONNECTOR_BLOCKED / HEIGHT_REGION FAIL
  + 25 x congestion overflow + 0.05 x crossings + 300 x ANCHOR_MISORIENTED
  + 0.5 x block bounding-box half-perimeters (when blocks are defined). Waived findings do
  not count. `routable` scores the live board after every round, keeps the best legal state
  (the input counts), stops on plateau (< 1 % better over 2 rounds), `[placement]
  time_limit_s` (600) or `max_rounds` (6), and never returns a worse board than the best seen.
* **Hard vs soft**: HARD classes pull 3x in the placer and cost 20 vs 3 per mm in the score; a
  round that breaks one is simply not the best state. (No per-move rejection.)
* `[placement.side]` `default`, `override` (ref/glob -> side): `SIDE_POLICY` WARN for parts off
  their side and for support parts on the other side of their IC unless within 1 mm of one of
  its pins. `routable` never flips a part (the text writer cannot mirror a footprint), so it
  cannot move parts to the other side; the flip cost is therefore not modelled.
  `[[placement.height_region]]` (`name`, `rect`, `side`, `max_height_mm`) + `[placement.heights]`
  (ref/glob -> mm): `HEIGHT_REGION` FAIL above the limit, WARN for unknown height. STEP bounding
  boxes are NOT read; heights must be given.
* Blocks: `spec["blocks"]` / a component `block` field / `[placement] block_refdes = true`.
* Regulator clusters rank support parts: switch node, input-cap loop, bootstrap, then
  feedback/output (`clusters` prints the lengths achieved).
* `ANCHOR_SPACING` (WARN): a budget that cannot be met because every part on the net is
  locked / `[lock.at]` / `[placement] fixed`; names the anchors and the minimum achievable length.
  Support-part distance is measured from the edge of the 0.6 mm escape halo of fine-pitch ICs.
* `CONNECTOR_BLOCKED`: edge connectors (`[placement.connectors] J12 = {opening = "-y"}` in
  footprint coordinates, else inferred from courtyard-vs-pad asymmetry, WARN only when inferred)
  must open outward across the nearest edge and a `plug_mm` (15) box projected outward must
  hit no part.
* `STALE_COPPER` (WARN): tracks/vias with an end that lands on no pad, via or track of their net
  (to a fixpoint); `route-ready --strip-stale [--out]` and `place --strategy routable --strip-stale`.

### 6. Pre-route gate — `fluxplace route-ready BOARD [--constraints TOML] [--spec JSON] [--json OUT] [--all]`

One-screen scorecard (class-weighted ratsnest length, counts per code, worst 10); `route-ready` alone answers "is this placement legal and ready to route" (legality is `PLACEMENT_ILLEGAL`),
exit 1 on any FAIL. Same checks run inside `review` as FAIL codes
(`[preroute] enabled = false` turns that off) and `[review] waive = ["CODE:REGEX"]`
waives them. `route`, `auto` and `compact` print the scorecard and refuse to
start routing on a failing placement unless `--force`.

| code | level | number vs threshold |
|---|---|---|
| NET_OVER_BUDGET | FAIL | MST length of a net vs its class budget (pairs judged per net) |
| SUPPORT_PART_FAR | FAIL | confidently-owned support part pad-to-pin vs limit (clusters) |
| ANCHOR_MISORIENTED | FAIL | anchor > `anchor_tolerance_pct` (15) worse than its best rotation |
| PIN_ESCAPE_BLOCKED | FAIL | fine-pitch (<= 0.5 mm) pin with a foreign pad / courtyard / rule area / copper in its outward lane, lane = track wide and track + clearance long (project net class) |
| KEEPOUT_TRAPPED | FAIL | pad inside a track-forbidding rule area (a rule area named after the part, e.g. an antenna's own, is skipped) |
| CONGESTION | FAIL | coarse global route: 2 mm cells, each non-rail net's MST as L-shapes on the signal layers (layers without a plane pour; override `signal_layers`), capacity = layers x track slots x `utilisation` 0.7, derated by pads and rule areas; overflow > `max_overflow` (0) |
| PLACEMENT_ILLEGAL | FAIL | one finding per kind: same-side courtyard overlap (`[placement] allow_overlap = [["H200","MH1"]]` for intentional ones; a through-hole part counts on both sides), courtyard outside the real outline (rounded corners; edge connectors from `[placement] edge_mounted`, `[placement.connectors]` or a J/P/CN/X part touching the outline may overhang by their mating side only, their pads must be inside), part in a forbidden region (`[placement.keepout.NAME]`, `[[keepout]]`, rule areas), through-hole pad in `[placement.no_tht.NAME]`. Scores 500 per violation. |
| RATSNEST_CROSSINGS | WARN | MST edges of different nets that cross |

    [preroute]
    cell_mm = 2.0   utilisation = 0.7   max_overflow = 0   signal_layers = 4

## Board on the sheet — `fit-sheet`

A board whose coordinates start near (0, 0) or go negative opens half outside
the KiCad page frame. `fluxplace fit-sheet --board B [--out O] [--paper auto|A5..A0|A..D]
[--constraints C.toml] [--dry-run] [--refill]` translates the **whole** board
(footprints, tracks, vias, arcs, zones incl. fills, rule areas, drawings incl.
Edge.Cuts, text, dimensions, groups, aux/grid origin) by a whole-millimetre
offset so the Edge.Cuts bbox is centred above the title block (no outline: the
footprint extents, and it says so). Every moved point is checked to have moved
by exactly the offset or nothing is saved. A second run reports "already fitted".

- **Paper.** `auto` keeps the file's paper if the board fits, else the smallest
  size of the same series (ISO, or US A-D) that fits. pcbnew 10 does not wrap
  `PAGE_INFO`, so the `(paper ...)` token is patched in the saved file.
- **Geometry** (measured from a kicad-cli SVG export; the default sheet is compiled
  into KiCad): usable area = 15 mm from every page edge (10 mm frame + 5 mm);
  title block 108 x 32 mm at the inner frame's bottom-right (12 mm), keep-out
  grown by 5 mm. A tall board that cannot sit above it is centred beside it.
- **Automatic** at the end of the outline stage of `place`, `auto` and `compact`
  and on the imported winner of `tournament --apply-winner`; `--no-fit-sheet`
  skips it. Copper-only commands (`finish`, `repair`, `tune`, `drc-fix`, `patch`,
  `sync-nets`, `replace-footprint`) never move anything and print
  `board is off the sheet — run \`fluxplace fit-sheet\`` when it is.
- **Review.** `SHEET_OFF_PAGE` (WARN; FAIL with `[sheet] enforce = true`): outline
  outside the usable area or on the title block. Facts: `page`, `outline_bbox`.
- **Constraints.** `--constraints C.toml` shifts `[lock.at]` rows and `[[keepout]]`
  `rect = [...]` in place (text edit, comments kept) and appends a
  `# fit-sheet <date>: ... shifted by (+dx, +dy) mm` line. Build commands only
  print the offset; apply it with `--constraints C --shift DX,DY`.

## What changed 2026-11-10 — spec-net, keep-out, lock and policy gates

Four gates added to `review`, each a defect the utv-comms V1.5 board carried
with DRC 0/0 and every earlier gate green. All four run on the pure `facts`
dict (`tests/test_gates.py`, no pcbnew).

| Code | What it catches | Config |
|---|---|---|
| `NET_SINGLE_PAD` (FAIL) | A named net that lands on one pad. It has no ratsnest edge, so DRC reports 0 unconnected for a pin that was never wired (V1.5 `IMU_INT1`; still true of `ETH_nLED2/3`). | none |
| `SPEC_NET_SPLIT` (FAIL), `SPEC_PIN_UNRESOLVED` (WARN) | Every spec net compared to the copper pad by pad, through each component's `pinmap`. | `--spec` |
| `KEEPOUT_COMPONENT` (FAIL) | A part inside a `[[keepout]]` region on the forbidden side, or taller than `max_height_mm` (height from the distributor's "Height - Seated (Max)"; unknown height fails). V1.5 had 27 parts under the CM5 in a 1.5 mm mezzanine gap. | `[[keepout]] name, side, rect \| around, margin_mm, allow, max_height_mm` |
| `LOCK_MOVED` / `LOCK_MISSING` (FAIL) | A footprint the enclosure fixes (bolt pattern, panel connector) is not at its locked position. `fluxplace lock --board B --refs 'MH*,MK*'` prints the block from an existing board. | `[lock] tol_mm`, `[lock.at] REF = [x, y, "F"]` |
| `POLICY_DIELECTRIC` (FAIL), `POLICY_NOT_AEC` / `POLICY_PASSIVE_SIZE` (WARN) | House part-selection rules from distributor parameters: allowed MLCC dielectrics, automotive (AEC-Q) grade, largest default chip size. Exceptions go in `[review] waive` with the reason. | `[policy] grade, mlcc_dielectrics, passive_max, exempt` |

`facts_from_board()` gains `pos`, `side` and `bbox` per footprint.

## What changed 2026-09-06 → 2026-09-08

Three days closing out the utv-comms-bridge V1.5 external review, grouped
by command (commit hashes from `git log 61eccd9..HEAD`).

**`verify-models`** — new placement/verification rules, each found against a
real defect that had passed silently:

- **posture** (`posture_gap`, `400c4d5`) — flags a body standing on edge
  (Q1 at commit 7cb0f77: XY collapsed to 6.24×1.50mm against a
  5.99×5.00mm Fab outline). Requires a genuine shortfall recoverable by a
  90° swap, not just any Fab/XY mismatch — a tighter two-sided match tried
  first false-flagged every SOT-23/0805/RJ45/JST part whose leads
  legitimately extend past their drawn Fab body.
- **upside-down** (`buried_mass_gap`, `c5f5c52`) — flags T1/T2 (92% of the
  model's points sit >0.5mm below the mount plane after its offset).
  Catches only this one signature — see `NEXT.md` for the flat-but-inverted
  case it misses (Q1, a different defect) and why.
- **seat gap** (`seat_gap`, `400c4d5`) — the multi-axis-rotation skip that
  hid J10/J11's DF40C receptacles floating 0.75–0.8mm into the CM5 module
  is closed: any model with rotation on more than one axis is now
  Z-extent-checked via a full X→Y→Z rotation, not just the Z-only math the
  XY pin-fit check trusts.
- **library transform** (`library_transform_diff`, `c5f5c52`) — flags J5
  (board references the stock KiCad USB-C STEP but rotate.z is 180 vs the
  library footprint's 0); WARN when the model file still matches the
  library's, INFO when it's also a deliberately swapped-in body.
  `verify-models --constraints` feeds `[docs] project_libs` into the
  search. `--fix` never touches a model whose transform already equals
  the library's.
- **mated overlay** (`overlay_registration_gap`, `c5f5c52`) — flags the
  CM5 overlay on J10 (3beae58, D70ad) missing its mating registration to
  J10/J11; silent once corrected. Tuned against that one before/after
  pair — see `NEXT.md`.
- **board→footprint transform fixed** (`88d85f4`) — `_th_holes`'s
  board→local un-rotation used the same matrix KiCad's placement uses
  instead of its inverse, so `verify-models --fix` "solved" J5 into a
  180°-wrong rotation that fit a target which was itself rotated 180° from
  reality. Fixed in `_board_to_fp`; added an SMD-lead check (`_smd_pads`, a
  z∈[-0.05,0.05mm] lead-flange slice against the real SMD pads) as a
  second signal, and `solve_transform` now treats the on-board transform
  as a prior — a non-identity rotation is accepted only when it clears
  identity's own TH fit by a solid margin *and* doesn't worsen the
  SMD-lead fit.
- **MODEL_FILE_UNREADABLE** (`c5f5c52`) — FAIL when a model file resolves
  but doesn't parse into geometry (STEP under 50 points, or a WRL with no
  Shape/IndexedFaceSet), wired into `review.check_models`.

**`review`** — `is_mechanical()` now also exempts a footprint/reference
matching the MountingHole/Fiducial/TestPoint/Logo conventions, so a grounded
mounting hole (MH1-4/MK1-4) no longer FAILs `MODEL_MISSING` (`400c4d5`).
`check_models` messages reformatted `"{ref} 3D model..."` — the old
`"{ref}: 3D model..."` ate the space a `CODE:^REF ` waiver regex needs, so
`MODEL_STANDIN:^ANT1 ` never matched ANT1's antenna stand-in (`400c4d5`).
New **`VIA_TYPE`** FAIL (`0fbff9e`): any blind/buried/micro via found on the
board, unless the matching `[fab] blind_vias`/`buried_vias`/`microvias`
constraint is `true` — wired into both `review` and the gate `fab`/`deliver`
call, same stop-ship contract as every other FAIL. `facts_from_board()`
gains `facts["vias"]`/`facts["via_type_counts"]` from KiCad 10's
`VIATYPE_*` enum. Root cause closed in the same commit: `finish.route_nets()`
had copied the via type straight off the freerouting session, so a dense
net's blind/buried via choice reached the board; it now always creates
`VIATYPE_THROUGH` F.Cu–B.Cu vias regardless of what the session chose.

**`repair --patch`** (`3343fef`) — the maze router's new track ends could
land inside an existing via's copper but off its exact centre (a grid-cell
quantization artefact), so KiCad's own `track_not_centered_on_via` DRC
reverted an otherwise-correct route every time. `apply_path` now snaps
every track end onto any existing via's true centre before writing
geometry. Found closing the J4 SMA corridor on the UTV board.

**`fab`/`deliver`** (`098e8bf`) — `fab.emit()` wrote into `gerbers/`,
`drill/` and `place/` with plain `makedirs(exist_ok=True)`, so a re-cut
into the same `--out` never removed a prior cut's files; a blind-via cut's
stale drill files survived a through-via re-cut and `deliver()`'s
`shutil.copytree` carried them into the PCBWay gerber zip. `_reset_dir()`
now wipes those three subdirs before every export; `MANIFEST.txt` gains
machine-readable `FILE : <section>/<name>` lines for every file `emit()`
actually wrote, and `deliver()` zips exactly those manifest-listed files
instead of copying the directories wholesale.

## What changed 2026-09-08 — fab-capability gate

**`review`** — PCBWay's gerber-upload audit rejected the utv-comms V1.5
board ("The minimum trace width should be no less than 0.1mm") over two
0.09 mm GND stubs that `review`, DRC and lint had all passed clean, because
every one of them judged the copper against the board's OWN design-rule
minimum (0.088 mm) — looser than the fab that would actually build it —
never against the fab's real floor. `[fab]` gains `min_track_mm` (0.1),
`min_clearance_mm` (0.1), `min_drill_mm` (0.2), `min_annular_mm` (0.1),
`min_via_dia_mm` (0.4), `min_silk_width_mm` (0.15) and a `profile` label
(e.g. `"pcbway-6L"`) alongside the existing `blind_vias`/`buried_vias`/
`microvias`; `constraints.fab_profile()` fills defaults for whatever the
engineer didn't state. New `check_fab_capability()`: FAIL **`FAB_MIN_TRACK`**
for every track/arc segment narrower than `min_track_mm` (net, layer,
width, position — 10 shown + count); FAIL **`FAB_MIN_DRILL`** /
**`FAB_MIN_VIA`** / **`FAB_MIN_ANNULAR`** for vias and PTH pads under the
drill/diameter/annular-ring floors; WARN **`FAB_DESIGN_RULE_BELOW_PROFILE`**
when the board's own Board Setup minimums (`m_TrackMinWidth`,
`m_ViasMinSize`, `m_MinThroughDrill`, `m_MinClearance`) are looser than the
profile — the root-cause gap that let a repair pass draw the 0.09 mm stub
in the first place — INFO `FAB_DESIGN_RULE_OK` otherwise. Only grades when
`[fab]` is loaded; no constraints, no opinion. `facts_from_board()` gains
`facts["track_geom"]` (every track AND arc segment, net/layer/width/x/y —
arcs were previously invisible to any geometry check), `facts["pth_pads"]`,
`facts["design_min"]`, and vias gain `dia_mm`/`drill_mm`.

**`repair` / `patch` / `bridge` / `drc-fix`** — every place copper gets
created or narrowed now clamps up to the `[fab]` floor when constraints are
loaded, instead of writing whatever the geometry solver asked for:
`repair.rf_widths` (a target-impedance solve on a thin layer), `repair.stitch`,
`repair.stitch_islands`, `repair.bridge`, `repair._gnd_via`/`remap_pins`,
`patch.apply_path`/`patch_board` (every track/via/drill it writes, including
the `net_widths` map), and `drcfix.fix`'s RF clearance-neck fallback — the
exact mechanism that produced the original 0.09 mm GND stubs. Each clamp
logs when it raises a width. `cli.py`'s `repair`, `patch` and (new
`--constraints`) `drc-fix` subcommands load `[fab]` once and thread it
through.

## What changed on 2026-09-04

Driven by the utv-comms V1.5 IMU add and a transformer footprint that turned out
to be wrong, all on `main`:

- **`models --fetch` / `--check`** — `review`'s `check_models` FAILs any
  electrical footprint with no 3D model or a model path that doesn't resolve;
  `--check` lists those with no network call, and `--fetch` closes the hole by
  looking up each ref's MPN on LCSC (`fluxplace.lcsc.lookup`) and pulling the
  body from EasyEDA via `easyeda2kicad --3d` — a second model source for parts
  whose DigiKey CAD-media link is login-walled. Attached at rotation/offset
  zero with a provenance line recorded in `model_sources.json`; every failure
  (no MPN, no LCSC match, exporter miss) is soft and reported, never faked.
- **`repair --bridge REF:PAD`** — a multi-layer maze router for the one pad the
  last-mile patcher and freerouting both give up on. Dijkstra on a
  clearance-rasterised grid per copper layer, layer changes only where a
  through via clears every layer, final snap into the pad clearance-checked by
  exact shape, DRC-guarded, zones refilled on accept.
- **Land-pattern citations** — `landpattern` on a spec component names the
  drawing page and the pitch / pad / rows / pins read from it; `review`
  measures the footprint against those numbers (FAIL on 0.03 mm), FAILs any
  footprint from `[docs] project_libs` without a citation, and WARNs when the
  cited page is a text-less drawing ("read by eye — verify before ordering").
  Born from a 24-pin transformer drawn at 1.27 mm pitch for a 0.99 mm part.
- **Datasheet temperature** — `partdocs.datasheet_temp` reads the operating
  range from the PDF text; `review` prefers it over the distributor field.
  DigiKey said +85 °C for a relay whose sheet says +70 °C.
- **LCSC lookup** retries without punctuation (JLCPCB's search is literal
  about hyphens); **`finish --timeout`**; the patcher's launder worker takes
  absolute paths; LGA packages classify; split-paddle footprints report the
  modal pad size.
- **3D bodies are a gate and a fetch** — `review` FAILs any electrical
  footprint with no model (`MODEL_MISSING`), a model path that resolves to no
  file (`MODEL_FILE_MISSING`), or a file named as a stand-in
  (`MODEL_STANDIN`); mounting holes are exempt. `models --check` lists the
  holes, `models --fetch` fills them from EasyEDA by LCSC code (easyeda2kicad,
  optional `[models]` extra) and records provenance. Lesson from V1.5: a board
  that renders with holes had passed DRC, review and packaging; the RJ45 pointed
  at a library file that did not exist, the transformer had no body at all.
  pcbnew pitfall: `fp.Models()` iterates by value — write back by index.
- **Working rule** used on that project: the top-level model orchestrates,
  reviews reports and commits; every mechanical pipeline step runs in a
  Sonnet subagent from an exact recipe. Long jobs are watched with a
  grep-until loop on their log, never with `pgrep` on their own command line.

## Architecture

```
fluxplace/
  graph.py       communication graph: power/signal split, passive collapse, net weighting
  topology.py    hub, forks, branches, lint (floating parts)
  placement.py   strategies (radial/flux/pack), rotation, legalization, HPWL, compaction
  kicad_io.py    the ONLY pcbnew-dependent module: read parts+nets+pads, write positions
  report.py      gather() + plan_markdown()
  audit.py       DRC scope, netlist read-back, stackup/impedance verifiability
  deps.py        the dependency registry both front ends preflight against
  mcp_server.py  MCP tools, derived from the CLI parser
plugins/         KiCad Action Plugin (GUI entry)
cli.py           headless entry
```

The core is pcbnew-free and unit-testable; swapping `kicad_io` would retarget another EDA tool.

## License

MIT © 2026 Jason Ratzlaff
