# `rules`

One cascade for per-object settings. Every value a `[[rule]]` or a per-object block sets (net width,
clearance, straight copper, skew, pair impedance, ...) resolves through the same list, so the answer to
"why does this net have this width" is one command.

```
fluxplace rules BOARD.kicad_pcb --constraints C.toml                # rule table: matches per rule, RULE_MATCHES_NOTHING
fluxplace rules BOARD.kicad_pcb --constraints C.toml --net USB_DP   # cascade for one net
fluxplace rules BOARD.kicad_pcb --constraints C.toml --ref U1 [--json]
```

## Writing a rule

```toml
[[rule]]
name = "usb pairs"                    # optional, shown in reports
select = { net = "^USB_.*" }          # keys: ref, net, netclass, type, layer, prop, pair; several keys = AND
set = { width_mm = 0.15, clearance_mm = 0.2 }
important = false
```

| select key | matches | value |
|---|---|---|
| `ref` | footprint reference, exact or glob (`U*`) | string |
| `net` | net name, regex (`^X$` is an exact match) | regex |
| `netclass` | the net's KiCad netclass | string |
| `type` | object kind: `track`, `via`, `pad`, `footprint`, `zone` | one of those |
| `layer` | copper/footprint layer | `F.Cu`, `B.Cu`, ... |
| `prop` | footprint field, partial match: `{ MPN = "LM1117" }` | `{Field = "substring"}` |
| `pair` | net name starts with the family (`pair = "USB_"`) | prefix |

`set` keys use the same names as the blocks they replace: `width_mm`, `gap_mm`, `clearance_mm`,
`skew_mm`, `impedance_diff`, `straight_copper`, `max_vias`, `pour`, and the `[route]` floors
`min_track_mm` / `min_clearance_mm`.

## Precedence

Highest first: exact ref or exact net > net regex / ref glob > `prop` > `pair` > `netclass` > `type`/`layer`
> global default (no select). A rule with several select keys ranks by its most specific key. Ties go to the
longer pair family (or longer non-exact net/ref pattern), then to the rule with more select keys, then to the later rule. Legacy blocks load first, `[[rule]]` entries in file order after
them. `important = true` beats any non-important rule. Resolution is per key: a rule that sets only
`width_mm` does not hide a `clearance_mm` set elsewhere.

## Legacy blocks

`[power.X]` (width from `width_mm` or `max_current_ma`, `pour`), `[nets.X]` (`straight_copper`, `max_vias`,
`width_mm`), `[rf] nets` (explicit list only: `width_mm`, `max_vias`), `[pairs.F]` (`width_mm`, `gap_mm`,
`clearance_mm`, `skew_mm`, `impedance_diff`) and `[route]` floors are translated into rules at load time.
Nothing else in those blocks moves (fab floors, placement weights, `[si]`, `[rf]` name-hint nets).

## Where rules take effect

- `review`: `straight_copper` (NET_STRAIGHT_COPPER) resolves through the cascade. Bad `[[rule]]` entries are
  FAIL (`RULE_UNKNOWN_KEY`, `RULE_BAD_REGEX`, `RULE_BAD_VALUE`); a net or ref selector matching nothing is WARN
  (`RULE_MATCHES_NOTHING`).
- `fab` netclass injection: each `[[rule]]` that wins `width_mm` or `clearance_mm` on a board net writes a
  `RULE_<name>` KiCad net class with that net as pattern into the `.kicad_pro`, which is what KiCad's
  Specctra export and Freerouting read. Legacy-block winners are not re-emitted (their classes already exist).
- Not wired: `power_width_mm` and the routing floors in `routerules.py` keep their own readers. A `[pairs]`
  width would reach power nets that share its prefix if `power_width_mm` were routed through the cascade, so that
  change needs a decision first.

## Limits

- `track`, `via` and `zone` selectors resolve when the caller supplies such objects; `rules` lists only nets and
  footprints, so per-track matches are not counted there.
- `prop` and `netclass` selectors are not checked by `RULE_MATCHES_NOTHING` (names alone cannot evaluate them).
- Board objects are read with pcbnew; the rule table needs KiCad's Python.
