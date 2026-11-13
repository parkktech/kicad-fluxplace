# `pin-groups`

### Functional pin groups of multi-channel parts vs the board — `fluxplace pin-groups BOARD --spec S [--constraints C] [--generic] [--json]`

Also runs inside `fluxplace review`. Exit 1 on a FAIL. The generalisation of [magnetics](magnetics.md): any part with channels
(ESD array through-pins, transformer windings, mux A/B/select, dual op-amp units, a connector's pair assignment) declares its groups
from the datasheet pin table, citing the page:

    {"ref": "U1", "pinmap_source": "X.pdf#p3",
     "pin_groups": [{"name": "A",
                     "roles": {"in_p": "1", "in_n": "2", "out_p": "6", "out_n": "5", "gnd": "3"},
                     "pairs": [["in_p", "in_n"], ["out_p", "out_n"]],      # legs of one differential pair
                     "links": [["in_p", "out_p"], ["in_n", "out_n"]],       # the signal continues (polarity is compared)
                     "tied":  [["in_p", "out_p"], ["in_n", "out_n"]],       # connected inside the part: nets must be equal
                     "kinds": {"gnd": "supply"}}]}                          # signal (default) | ct | nc | supply | control; inferred from the role name

| code | level | meaning |
|---|---|---|
| `PINGRP_PAIR_SPLIT` | FAIL | a declared pair's legs are not one pair (different polarity bases, only one leg a pair net, both on one net) |
| `PINGRP_ROLE_MISUSE` | FAIL | a signal net on a ct/nc/supply pin; in a group with a ct, a signal pin on a bias/termination net |
| `PINGRP_TIED_SHORT` | FAIL | roles tied inside the part sit on different nets (the part would short them) |
| `PINGRP_POLARITY` | WARN (FAIL with `[pin_groups] polarity = "strict"`) | linked P/N pairs arrive swapped |
| `PINGRP_UNSOURCED` | WARN | groups without `pinmap_source` |
| `PINGRP_PIN_UNRESOLVED` | WARN | a role names a pad the board part lacks |
| `PINGRP_NONE_DECLARED` | WARN | pinmap names repeat per channel (>= 3 indexes of one stem) and no groups/channels are declared |

Magnetics `channels` stay as they are: `check` runs the magnetics profile for them (the same `MAG_*` codes, unchanged) and `--generic`
runs them through the generic engine as converted groups (`PINGRP_*` codes, the same pads). Not checked: that a declared table
matches the datasheet (the declaration is the human-verified input; `part-diff` compares two declarations).
