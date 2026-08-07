"""fluxplace — signal-flow-aware component placement for KiCad.

Reads a board's communication graph (who talks to whom, power excluded), then
places components so routing falls out as a tree. Core is pcbnew-free and testable;
`kicad_io` is the only module that touches pcbnew.
"""
__version__ = "0.5.0"
