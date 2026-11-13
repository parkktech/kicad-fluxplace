"""Registers the constructive-flow commands (density, fanout, modules, floorplan2) in cli.py with one call.
Each module owns its parser: it exposes `add_parser(sub)` and sets `fn` through `set_defaults`."""
import importlib

MODULES = ("density", "fanout", "modules", "floorplan2", "snapvias")
MODULES += ("closure", "pairs")
MODULES += ("joingaps",)
MODULES += ("intent", "intentdraft")          # placement intent: gate + draft (docs/INTENT.md)


def add_parsers(sub):
    for name in MODULES:
        try:
            m = importlib.import_module(f"fluxplace.{name}")
        except ModuleNotFoundError as e:
            if e.name != f"fluxplace.{name}":
                raise
            continue
        m.add_parser(sub)
