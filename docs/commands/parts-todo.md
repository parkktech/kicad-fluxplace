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

**Stock shortages: `alternatives`.** A part with an MPN that is short or absent at the build quantity gets `alternatives`: packaging variants of
the same MPN plus parametric matches (the original's description), each classified by `dropin.classify` and ranked (verified drop-ins first).
`drop_in: true` only when ALL of these are verified from distributor data and the candidate's own datasheet (fetched by [parts-fetch](parts-fetch.md)
into `<datasheets>/_candidates`): footprint and package, pin count, electrical value equal, rating / current / voltage equal or better,
tolerance and resistance-type figures equal or better, operating temperature at least `[parts] temp_min/temp_max` and the original's, AEC-Q kept,
active lifecycle, stock at the build quantity. `checks` shows each, `differs` what is different, `unverified` what could not be checked (that
is not a drop-in). Packaging variants (tape/reel/cut-tape codes) are the same part (`identical: true`). Each candidate carries `lead_time` when
the API gives one. `--no-verify` skips the datasheet fetches (nothing is then `drop_in`).
