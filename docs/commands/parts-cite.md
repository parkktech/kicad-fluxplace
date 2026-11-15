# `parts-cite`

### Record a pin table an agent read from a page image — `fluxplace parts-cite SPEC --ref REF --page N --read READ.json [--write-source] [--board B] [--constraints C] [--datasheets D]`

READ.json is what the agent read: `{"pins": {"1": "VBUS", "MP": "shell", ...}, "count": N}`. The tool compares it with the spec (every pin
the spec's nets use must be in the table; `pinmap` names must match the names at those pins; `landpattern.pins`) and with the board
footprint's signal-pad count, then writes the citation (file, page, table, findings) to `<datasheets>/pin-citations.json`. A mismatch is a
FINDING printed and recorded (exit 1); the spec is never changed silently. `--write-source` sets `pinmap_source` (file#page) only when nothing
mismatched; no pinmap is invented for a positional connector.
