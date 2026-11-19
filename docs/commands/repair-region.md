# repair-region

Rip the open connections and their **unlocked** blockers in one area, re-route them together with Freerouting,
and keep the result only if the board got strictly better. Fixes the sealed-pocket case that one-net-at-a-time
tools stall on.

```
fluxplace repair-region --board B --out O
fluxplace repair-region --board B --out O --nets 'ETH_.*' --margin 2 --tries 3
```

## Flow

1. kicad-cli DRC on the input. Targets = nets of the unconnected items (zone islands excluded).
2. RF nets (`is_rf`, or `[rf] nets`) and pair nets (`pair_nets`) are never ripped: reported as `skipped`; use `pairs --only`.
3. Area = bounding box of the open items' positions, grown by `--margin` mm.
4. Rip set = targets + every net with an unlocked track or via fully inside the area, minus RF, pair, GND and power-plane nets.
5. `region.run(rip=True, keep_locked=True)`: locked copper is never removed; only new copper of the rip set is copied back.
6. Kept only if opens fall, shorts do not rise, and no new DRC violation kind appears (unconnected items excepted).
7. Failed: margin x1.5 and retry, up to `--tries`. If all fail, `OUT` is not written and the output lists the sealing items.

Every try appends one line to `<OUT stem>.repair-region.log`.

## Flags

| flag | default | meaning |
|---|---|---|
| `--board`, `--out` | required | `--out` must differ from `--board` |
| `--nets` | all open nets | net names or regexes (a token that is a net name is literal) |
| `--margin` | 2.0 | mm around the open items |
| `--tries` | 3 | area grows 1.5x per failed try |
| `--constraints` | none | `[rf] nets`, `[pairs.*]` families |
| `--planes` | `In1.Cu In4.Cu` | plane layers declared in the DSN |
| `--jar`, `--passes`, `--timeout`, `--kicad-cli` | as `finish` | Freerouting and DRC |

Exit 0 when kept or nothing to repair; 1 when every try failed or the protection check failed.
