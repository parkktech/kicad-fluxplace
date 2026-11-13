# `magnetics`

### LAN magnetics wiring vs the datasheet pin table — `fluxplace magnetics BOARD --spec S [--constraints C] [--json]`

Also runs inside `fluxplace review` (the same findings, waivable by code). Exit 1 on a FAIL. Compact output
(`--top`, `--full`, `-q`; report beside the board).

Why: a module swap kept the old part's cable-side pin order for two of four channels. DRC, ERC and the
spec-vs-board check read 0 (the board matched its own netlist); only the datasheet says which pin is a centre tap.
100 Mb uses two pairs and worked; gigabit flapped. Nothing here is part- or board-specific.

A spec part declares its transformer channels from the datasheet pin table, citing the page:

    {"ref": "T3", "value": "HX5084NLT", "pinmap_source": "HX5084NLT.pdf#p2",
     "channels": [{"chip_p": "2", "chip_n": "3", "chip_ct": "1",
                   "line_p": "23", "line_n": "22", "line_ct": "24"}, ...]}

Pins are pad numbers. `line_ct` may be omitted (magjack with internal termination). The check reads the board's pad
nets; a "line net" is a net that reaches a connector pad (ref `J/P/X/CN/RJ`, or a footprint named
Connector/RJ45/Jack/Header/USB/SMA/Socket).

| code | level | meaning |
|---|---|---|
| `MAG_LINE_ON_CT` | FAIL | a `line_ct` pad is on a net that reaches a connector, or a `line_p`/`line_n` pad is on a termination net (only resistors, no connector) |
| `MAG_CHANNEL_SPLIT` | FAIL | a channel's two line pads do not carry one pair (polarity-named nets `*_P/_N`, `+/-`: base names differ, or only one is a pair net) |
| `MAG_POLARITY` | WARN (FAIL with `polarity = "strict"`) | chip P/N reach the line side swapped (needs polarity-named nets on both sides; auto-polarity PHYs tolerate it) |
| `MAG_CT_WIRING` | FAIL | chip centre taps not on one common net (or on a signal net); a `line_ct` with no net, shared by two channels, without exactly one own series resistor, or resistors not ending on one shared net |
| `MAG_NO_CHANNELS` | WARN | a spec part whose value/MPN/footprint/description looks like LAN magnetics or a magjack declares no `channels`: read the datasheet pin table and declare them |
| `MAG_CHANNELS_UNSOURCED` | WARN | `channels` without `pinmap_source` |
| `MAG_PIN_UNRESOLVED` | WARN | a declared pin is not a pad of the board part, or a channel lacks chip_p/chip_n/line_p/line_n |

Optional constraints:

    [magnetics]
    polarity = "warn"       # "strict": swapped P/N is a FAIL
    chip_ct  = "common"     # "any": chip centre taps may sit on different nets
    line_ct  = "resistor"   # "any": skip the own-resistor / shared-net rule (LINE_ON_CT still applies)

Not checked: which PHY pair must land on which connector pair (the board's own spec owns that), PoE, and
whether a declared pin table matches the datasheet (the declaration is the human-verified input).
