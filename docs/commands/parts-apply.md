# `parts-apply`

### Validate and apply agent proposals — `fluxplace parts-apply PROPOSALS.json --spec SPEC [--board B] [--constraints C] [--datasheets D] [--mpn-map M] [--dry-run] [--no-fetch] [--json]`

PROPOSALS is a list of `{"refs": [...] | "ref": "R5", "mpn", "mfr"?, "package"?, "pinmap_source"?, "pinmap"?, "datasheet"?, "datasheet_url"?,
"model"?, "needs_owner"?, "changes"?}`. Each is validated; every one that passes is written to the spec (`mpn`, `mfr`, `package`,
`pinmap_source`, `pinmap` where the spec has none) and the mpn_map in one step (atomic writes, the file's own indent kept). Nothing
that fails is written; the rest of the batch still applies.

| outcome | when |
|---|---|
| OWNER | `needs_owner: true`, a `changes` list, a `footprint`/`value`/`rating`/`pinout` key, or a `pinmap` that differs from the spec's. Never auto-applied, listed |
| REJECTED | unknown ref; MPN not at DigiKey/Mouser (exact match); stock grade not OK at per-board count x `[bom] quantity_target`; any part-entry FAIL on the refs after the change (package vs footprint, pin count, size code, missing pinmap source...); datasheet not on disk and not fetchable; no real 3D model on the board's footprint and none given; a ref claimed twice |
| applied | everything else |

Exit 1 when anything was OWNER or REJECTED. `--dry-run` validates and writes nothing. Run [parts-todo](parts-todo.md) first.
