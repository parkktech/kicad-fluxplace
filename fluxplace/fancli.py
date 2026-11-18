"""Registers the constructive-flow commands (density, fanout, modules, floorplan2) in cli.py with one call.
Each module owns its parser: it exposes `add_parser(sub)` and sets `fn` through `set_defaults`."""
import importlib

MODULES = ("density", "fanout", "modules", "floorplan2", "snapvias")
MODULES += ("closure", "pairs")
MODULES += ("joingaps",)
MODULES += ("intent", "intentdraft")          # placement intent: gate + draft (docs/INTENT.md)
MODULES += ("wlrefine",)
MODULES += ("placementcheck",)                # keep-out / hardware / module rule report (docs/commands/placement-check.md)
MODULES += ("thermal",)                       # [thermal.NAME]: locked GND vias + rule area (pre), mask opening (post)
MODULES += ("blocks",)                        # functional-block inventory (fluxplace/blocks.py)                      # wire-length refinement of a legal placement


def add_parsers(sub):
    for name in MODULES:
        try:
            m = importlib.import_module(f"fluxplace.{name}")
        except ModuleNotFoundError as e:
            if e.name != f"fluxplace.{name}":
                raise
            continue
        m.add_parser(sub)
