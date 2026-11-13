# `part-diff`

### Substitution diff — `fluxplace part-diff SPEC_OLD SPEC_NEW` or `fluxplace part-diff --spec S --ref R --new-mpn MPN [--new-part F.json]`

Why: a transformer from another vendor fits the same land pattern with a different pin order. DRC, ERC and the netlist read 0; the
link is dead on the bench. Run it before the swap reaches a board. Exit 1 on a FAIL, 3 on a question.

A pad's FUNCTION = its declared group role (`pin_groups`/`channels`, [pin-groups.md](pin-groups.md); vendor pin names do not matter),
else its pinmap name (stated as "names only" in the finding). Output: the side-by-side pin table (all in `<stem>.part-diff.txt`,
changed pads marked `*`) and the findings.

| code | level | meaning |
|---|---|---|
| `PART_SUBSTITUTION_PINOUT` | FAIL | a pad's function differs between the old and the new part while its net is unchanged: pad, old function, new function, net |
| `PART_SUBSTITUTION_UNVERIFIED` | FAIL | the MPN changed and the new part's `pinmap_source` is missing or identical to the old one's (the pin table was not re-read), or no pin table exists to compare |

Two-spec mode compares every ref whose MPN (spec `mpn`, `--mpn-map-old/-new`, or an MPN in the value) changed. `--new-mpn` mode takes
the new part's table from `--new-part FILE.json` (`{mpn, pinmap, pin_groups|channels, pinmap_source}`) or an `alternates` entry of the
old part in the spec; with neither it asks for the datasheet (question `part-diff-pinmap:<MPN>`: declared | abort) and never guesses.
`--board B` takes the pad nets from a board.
