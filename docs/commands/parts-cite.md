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

## How the cited page is read (review: `PINMAP_EVIDENCE_WEAK`, `LANDPATTERN_NOT_ON_PAGE`)

Evidence stays real: a value passes only if it is printed on the cited page or derivable from what is printed.

- **Number forms**: `0.5`, `0.50`, `(0.5)`, `.5`, `0,5` (decimal comma) and a trailing unit all read as the same value; inches are accepted too. Precision is the printed one (two decimals, or as many as printed up to four).
- **Derived pitch**: when the pitch itself is not printed, it passes if a cited `pad` dimension and another number, both printed on the page, sum to it within 0.006 mm. Reported as INFO `LANDPATTERN_PITCH_DERIVED` ("derived: 1.45 + 2.55").
- **OCR fallback**: when pdftotext finds no text, or not the value / too few pin names, the cited pages are rendered (`pdftoppm -r 300`) and read with `tesseract` if installed. The evidence is marked `ocr` (INFO `LANDPATTERN_PITCH_OCR`, or "(ocr)" in the pinmap detail). Without tesseract nothing changes and the finding says "ocr unavailable".
- **`pin_alias`** per spec component maps a board pin name to the term the datasheet prints; the pin is accepted when the term is on the page (alternatives split on `/ , ;`):

```json
"pin_alias": {"GND": {"datasheet": "outer contact", "page": "SMA-J-P-H-ST-EM1.pdf#p1"}}
```

`page` defaults to the cited `pinmap_source` page(s). Use it only when the sheet really prints the term; the review detail lists each alias used.
