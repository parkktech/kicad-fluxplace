# `parts-todo`

### Machine-readable parts work list — `fluxplace parts-todo SPEC [--board B] [--constraints C] [--datasheets D] [--mpn-map M] [--json] [--out F] [--only-missing] [--no-search] [--no-api] [--limit N] [--strict]`

The work list an AI agent (or a person) needs to resolve parts BEFORE placement. One record per part: `ref`, `group` / `group_refs`
(identical value + footprint + package + MPN: resolve once), `value`, `footprint`, `package`, `pins`, `pinmap`, `pinmap_source`,
`mpn`, `mfr`, `datasheet_file`, `missing` (a subset of `mpn mfr stock datasheet pinmap package model`), `findings` (the codes), `evidence`
(the distributor row for the current MPN and its stock at the build quantity) and `candidates`.

`candidates` (parts missing an MPN or stock, one DigiKey keyword search per group, Mouser when DigiKey has nothing; only the query
text, a value and package size or a part name, leaves the machine): `mpn mfr stock unit ext source datasheet lifecycle package
temp_min temp_max`, plus `fits` and `why` (stock vs per-board count x quantity, package vs the footprint, the size code in the MPN
vs the footprint, temperature vs `[parts] temp_min/temp_max`, lifecycle, datasheet URL). Fitting candidates first, cheapest first.
A candidate is evidence, never a decision: it changes nothing until [parts-apply](parts-apply.md) validates it.

`--no-api` / no credentials: no lookups and no search (stock and package are reported NOT CHECKED). `--strict`: exit 1 while a part is
unresolved. The build quantity comes from `[bom] quantity_target`.
