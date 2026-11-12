# MCP server

> **Footprint (2026-11-11).** `tools/list` is now ~8 KB (was ~26 KB) with 19 tools: one-sentence
> descriptions, parameter descriptions <= 80 chars, only the parameters agents use. Commands without their own
> tool run through `fluxplace_run {command, args}` (analyze, gather, plan, comprehend, comprehend-intent,
> preflight, netlist, stackup, stackup-define, schematic, datasheets, models, intake, pcbway, drc-fix,
> verify-models, repair, route; plus the long ones under `--all`). Flags: `fluxplace_help_brief`. Unlisted flags
> still work when passed. `fluxplace doctor --context` (or `fluxplace_doctor context=true`) prints the measured
> footprint against budgets; `tests/test_context.py` fails if one is exceeded. Original long descriptions are
> at the end of this file.

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

## Tool descriptions (long form, moved from the MCP `tools/list`)

The MCP server now sends one-sentence descriptions (see `fluxplace help-brief CMD`). The original long
guidance, unchanged:

- **doctor** (read): Preflight the whole toolchain: KiCad, python packages, router, distributor credentials and the wider suite. Run this first when anything fails unexpectedly.
- **analyze** (read): Analyze a board's communication graph: parts, nets, hub and fork topology.
- **gather** (read): Dump structured board facts as JSON — parts, nets, pads, geometry.
- **plan** (read): Gather board facts and write a detailed placement plan in markdown.
- **lint** (read): Design-completeness rules: unwired power/IO, dead-end nets, connector retention advice for high-vibration use.
- **comprehend** (read): Auto-detect physics constraints from the netlist — power nets, differential pairs, bypass caps, crystals, converters — and optionally grade the placement against them.
- **comprehend-intent** (read): Infer electrical intent as TOML: pairs, bypass, crystals, power classes.
- **eval** (read): Score the current placement, optionally against the physics rule checks.
- **route** (read): Global-route the current placement and report congestion hotspots without modifying the board.
- **sourcing** (read): Grade every MPN against live DigiKey and Mouser stock. This is the availability gate: a part nobody stocks is a design bug, not a purchasing problem.
- **preflight** (read): Check a board is ready for the next pipeline stage.
- **drc-scope** (read): Report what a DRC result actually examined: which checks are switched off in the project, which of those matter for fabrication, and (with full=true) what a re-run with every check enabled actually finds. A clean DRC report is a claim about scope — this is how you learn the scope.
- **netlist** (read): Read the connection list back out of the routed board: every net with its pads, every component, pin by pin. For a board generated from a netlist spec there is no .kicad_sch at all, and this is the only connectivity document that exists.
- **stackup** (read): Report the layer stack, which layers carry plane pours, the netclass track/diff-pair geometry, and whether controlled impedance can be verified from these files at all — it cannot if no dielectric is defined.
- **stackup-define** (write): Define the board stackup from a named fab profile (JLCPCB/PCBWay 4-layer 1.6mm and others), solve the trace geometry it implies for 50 ohm single-ended and 100 ohm differential, and grade both the netclasses AND the actual routed copper against those targets. With apply=true it writes the stackup into the .kicad_pcb (backed up first, refuses to overwrite an existing one). Without a stackup, no impedance claim on a board can be checked at all.
- **schematic** (write): Generate a real .kicad_sch from the netlist spec (or from the routed copper) and VERIFY it by exporting its netlist and diffing against the source. For a board built from a spec rather than drawn, this is the reviewable electrical document that otherwise does not exist. Net-label style, not hand-drawn signal flow.
- **verify-models** (read): Verify 3D models sit on their footprint pins — displaced models look like mechanical truth and are a review hazard.
- **datasheets** (write): Fetch every MPN's datasheet into the project through the DigiKey/Mouser APIs, hash it into datasheets.json, and list the ones that need a browser. Documentation is a build requirement: review fails a part with no datasheet on disk.
- **spec-check** (read): Documentation gate on a netlist spec: every part has a manufacturer part number, its datasheet on disk, and a pinmap whose names appear on the cited datasheet page. schematic refuses an undocumented spec.
- **review** (read): The design-review gate — what an outside reviewer checks that DRC, ERC and lint do not: spec net rules (straight-copper nets), diff-pair skew and layer use, RF impedance graded on the layer the copper is actually on, distributor package / pin count / operating temperature vs the footprint and the [env] profile, spec pinmap vs the KiCad official symbol, spec/board size and layer sync, hold-up and TVS margin. fab and deliver run it and abort on FAIL.
- **fab** (write): Emit the manufacturing package: gerbers, drill, centroid, DRC report and manifest.
- **deliver** (write): Package a fab directory for upload: CAM-only zip plus the loose documents whoever places the order actually reads.
- **pcbway** (write): Generate the PCBWay order worksheet and assembly instructions from the board, so the order form is not answered from memory.
- **repair** (write): Copper repairs the review gate asks for: remove router loops/stubs on differential pairs, re-width RF segments to what their layer needs, remap pads to corrected nets (rips old stubs, drops GND vias), net twin pads, add silkscreen text. Optionally runs the patcher for unrouted pads.
- **finish** (long): Route named nets with freerouting and take back only their copper (planes declared as power in the DSN); kept only if DRC does not worsen and the unconnected count falls. For the connection the grid patcher cannot close.
- **drc-fix** (write): Fix the DRC noise a repair leaves behind: rip tracks that short a swapped footprint's pads, neck widened RF segments at clearance pinches, snap or delete stray track ends, move colliding reference text; loops DRC until the count stops falling.
- **tune** (long): DRC-guarded differential-pair length tuning: bridge hairpins on the long side, add serpentine meanders on the short side, accept each step only when kicad-cli DRC does not get worse, until every pair is inside its [pairs.*] skew_mm limit.
- **models** (write): Fetch real manufacturer STEP models via distributor APIs and wire them to footprints.
- **intake** (write): Design interview: capture design intent to JSON.
- **auto** (long): Full pipeline: place, outline, planes, route, DFM, fab package. Minutes.
- **compact** (long): Shrink a known-good placement and re-run the downstream stages. Minutes.
- **tournament** (long): Run competing placement candidates and rank them by a real router. Many minutes.
- **place** (long): Re-place the board by signal flow. Rewrites positions.
- **patch** (long): Regional rip-up and reroute for walled copper islands.
- **launder** (long): DRC-driven copper launder pass.
- **calibrate** (long): Calibrate placement scoring against router ground truth.
- **sync-nets** (long): Synchronise nets onto the board.
- **replace-footprint** (long): Swap a footprint across the board.
