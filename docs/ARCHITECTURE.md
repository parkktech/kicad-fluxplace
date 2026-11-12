# Architecture

## Overview (original README intro)

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
