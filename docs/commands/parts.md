# `parts`

### Part-entry gate — `fluxplace parts SPEC [--board B] [--constraints C] [--datasheets D] [--mpn-map M] [--no-api] [--fix --out SPEC2] [--json]`

Runs BEFORE layout, on the spec: everything a part needs to be a real, buyable, matching part. Compact output (verdict, counts
per code, worst N spread over the codes, `--top/--full/-q`); full report `<stem>.parts.txt/.json`. Exit 1 on a FAIL, 3 on open
questions ([questions.md](questions.md)). Also runs offline at spec load in `place` and `schematic` (one summary line; stops
only with `[parts] enforce = true`), inside `review` (the `PART_*` codes, waivable) and as the first stage of
`pipeline` through [ready](ready.md). Nothing is fitted to one board.

A part is COMPLETE with: an MPN in a real field (spec `mpn` or the mpn_map — value text does not count), `mfr`, a datasheet file
on disk, `pinmap_source` (`file.pdf#pN`, or `kicad:`) when it has more than 2 pins, a `package`, a footprint. DNP/variant
parts (`dnp`, `variant` listed in `[parts] skip_variants`) and mechanical parts (holes, fiducials, test points, logos) are
skipped by rule and counted.

| code | level | meaning |
|---|---|---|
| `PART_MPN_MISSING` | FAIL | no MPN anywhere and the value is no part number |
| `PART_MPN_IN_VALUE` | FAIL | no MPN record, the value text reads like one (pattern; confirmed by lookup when online) |
| `PART_MFR_MISSING` | FAIL | MPN recorded, no `mfr`/`manufacturer` |
| `PART_DATASHEET_MISSING` | FAIL | no file in the datasheet dir (`datasheets.json`, `<MPN>.pdf`, spec `datasheet`); WARN for a 2-pin passive with `[parts] passive_datasheet = "warn"` |
| `PART_PINMAP_UNSOURCED` | FAIL | more than 2 pins and no `pinmap_source` file + page, or the file is not on disk |
| `PART_PACKAGE_MISMATCH` | FAIL | package (declared, distributor, or the chip size in a vendor series code: Yageo/Vishay/Murata/TDK/Samsung/KOA/Panasonic) vs the footprint family, chip size 0201..2512, SOT/SOIC/TSSOP/QFN/DFN pin count |
| `PART_PINCOUNT_MISMATCH` | FAIL | distributor/declared pin or position count vs footprint pads (one extra pad allowed on EP/QFN/DFN families) |
| `PART_NO_STOCK` | FAIL/WARN | live grade NONE/RISK/LEAD (FAIL), LOW (WARN) |
| `PART_GENERIC` | FAIL/INFO | a passive given by value only: allowed with `[parts] generic_passives = true` or `--answer generic-passives=accept`, otherwise the question `generic-passives` |
| `PART_FOOTPRINT_MISSING` / `PART_FOOTPRINT_MISMATCH` | FAIL / WARN | no footprint / the board's differs from the spec's |
| `PART_PACKAGE_MISSING` | WARN | no package recorded or looked up (FAIL with `[parts] package = "required"`) |
| `PART_MPN_VALUE_CONFLICT` | WARN | the value text names a part number other than the MPN on record |

Offline (`--no-api`, or no DigiKey/Mouser credentials) the distributor package/pin and stock checks are printed as
`NOT CHECKED`; never as a pass. Online: `partdata.fetch` + `sourcing.check`, cached beside the spec.

`--fix --out SPEC2` writes a COPY with only the unambiguous: a value a lookup confirms as exactly that MPN becomes `mpn`; `mfr`
and `package` come from an exact lookup. Everything else is the question `part-mpn:<value>` (record | skip, default skip), one
per distinct value; it never invents or accepts a part number on its own.

    [parts]
    generic_passives = false      # true: passives by value/tolerance/package are acceptable
    passive_datasheet = "fail"    # "warn" for 2-pin passives
    package = "warn"              # "required"
    pinmap_exempt = ["J*"]        # ref globs that need no pinmap_source
    skip_variants = ["alt"]
    stock_need = 10
    enforce = false               # true: place/schematic stop on a FAIL
    waive = ["PART_MFR_MISSING:^R"]
