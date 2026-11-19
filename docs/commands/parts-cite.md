# `parts-cite`

### Record a pin table an agent read from a page image — `fluxplace parts-cite SPEC --ref REF --page N --read READ.json [--write-source] [--board B] [--constraints C] [--datasheets D]`

READ.json is what the agent read: `{"pins": {"1": "VBUS", "MP": "shell", ...}, "count": N}`. The tool compares it with the spec (every pin
the spec's nets use must be in the table; `pinmap` names must match the names at those pins; `landpattern.pins`) and with the board
footprint's signal-pad count, then writes the citation (file, page, table, findings) to `<datasheets>/pin-citations.json`. A mismatch is a
FINDING printed and recorded (exit 1); the spec is never changed silently. `--write-source` sets `pinmap_source` (file#page) only when nothing
mismatched; no pinmap is invented for a positional connector.

## Landpattern pad table (`LANDPATTERN_PAD_*`)

The summary `landpattern` (pitch, rows, pad, pins) cannot see one pad that moved. Give the whole table and the footprint is checked pad by pad (fluxplace/fpcheck.py):

```json
"landpattern": {"source": "bt850.pdf#p12", "pitch": 0.99, "pad": [0.64, 1.78], "rows": 8.99, "pins": 24,
  "origin": "footprint", "tol": 0.03,
  "pads": [{"num": "8", "x": 2.15, "y": 7.825, "w": 0.5, "h": 1.35}],
  "vendor_footprint": "bt850-vendor.kicad_mod",
  "allow": []}
```

- `origin`: `footprint` (the footprint anchor, default) or `pad1` (relative to pad 1).
- `pads`: read from the cited drawing, mm. Pads match on number, nearest first. Position or size beyond `tol` (default 0.03) is `LANDPATTERN_PAD_MISMATCH` naming the pad and the delta; a cited pad not on the footprint is `LANDPATTERN_PAD_MISSING`; a footprint pad not in the table is `LANDPATTERN_PAD_EXTRA`. Pads without a number (NPTH) are not in the table.
- `vendor_footprint`: the manufacturer's own `.kicad_mod` (path relative to the datasheets dir, or absolute), compared pad by pad the same way.
- Findings are FAIL, and waivers are refused (see [fp-check.md](fp-check.md)).
