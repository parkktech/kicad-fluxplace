# `bom-opt`

## Part-count optimization — `bom-opt` and the `[bom]` budget

"Less is better": find ways to build the same circuit with fewer placements and
fewer distinct part numbers. `fluxplace/bomopt.py` is pure Python (no pcbnew, no
network); every finding is a *candidate* — it never claims two parts are
interchangeable, so check ratings and tolerances before acting.

```
fluxplace bom-opt --spec spec.json --mpn-map mpn_map.json --constraints c.toml [--json out.json]
```

Prints a summary block (total/fitted/DNP, unique parts, by-kind, passive ratio),
findings grouped by code, and `Potential reduction: N placements, M line items`.
Exit 1 only on a FAIL budget. `review` runs the budget part only (plus one
`BOM_SUMMARY` line) when the constraints have a `[bom]` table and `--spec` is given.

| Code | Meaning |
|---|---|
| `BOM_BUDGET_PARTS` / `BOM_BUDGET_UNIQUE` (FAIL), `BOM_BUDGET_DNP` (WARN) | Fitted parts, distinct part numbers (distinct values with no MPN map) or DNP parts over the limit. Only when set. |
| `BOM_ARRAY_CANDIDATE` | `array_min`+ same-value, same-package resistors sharing a net (pull-ups/downs): one 4-resistor array replaces four. |
| `BOM_VALUE_MERGE` | Same kind and package, values within `merge_tolerance` of a more-used value: drop a line item. Never across kinds or for 0. |
| `BOM_PACKAGE_SPLIT` | Same kind and value in several chip sizes: use one. Refs are the minority package. |
| `BOM_SINGLE_USE` | One summary: passive part numbers (values) used exactly once, each a reel. |
| `BOM_PARALLEL` | Resistors across the same two nets; three or more bulk (>= 1 uF) equal caps across the same two nets. Bypass-size caps are skipped: per-pin decoupling is deliberate. |
| `BOM_SERIES_ZERO` | Fitted 0-ohm links: delete or make a solder jumper. |
| `BOM_DNP_COUNT` | Lists the do-not-populate parts. |

```toml
[bom]
max_parts = 120          # FAIL above this many fitted parts
max_unique = 60          # FAIL above this many distinct part numbers
max_dnp = 4              # WARN above this many DNP parts
array_min = 4            # resistors on one net before suggesting an array
merge_tolerance = 0.1    # 10 % value spread counts as "the same value"
exempt = ["MH*", "J*"]   # refs ignored entirely (MH/MK/H/FID/TP always are)
keep_values = ["R12"]    # refs whose value must not be merged
keep_links = ["R40"]     # 0-ohm links that must stay
```
