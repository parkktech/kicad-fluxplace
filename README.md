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
