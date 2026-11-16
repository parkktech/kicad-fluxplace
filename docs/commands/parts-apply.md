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
| OWNER (substitution) | the proposal changes an existing MPN to a part that is not a packaging variant of it, and `dropin.classify` (re-run by `parts-apply`, the agent's word is not taken) is not fully verified. Reasons list what differs / is unverified |
| applied | everything else |

Exit 1 when anything was OWNER or REJECTED. `--dry-run` validates and writes nothing. Run [parts-todo](parts-todo.md) first.

A substitution that IS a verified drop-in is applied automatically; `old MPN -> new MPN` with the evidence of each check and the date is appended to
`parts-substitutions.json` next to the spec, and shown as `replaces` in the report. Anything that changes footprint, pinout, value, rating or grade
stays OWNER. A packaging variant of the same MPN is not a substitution.
