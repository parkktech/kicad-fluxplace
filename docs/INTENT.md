# Placement intent

What the owner wants from the placement, written in the constraints file, honoured by the placers and the legaliser, and
graded by the **INTENT gate**. Everything comes from the board, the spec and the constraints; where they leave a question
open the tool asks (exit 3, `[answers]`, `--assume-defaults`; see [commands/questions.md](commands/questions.md)).

    fluxplace intent-draft BOARD [--spec S] [--constraints C]     # propose an [intent] section (a draft to edit)
    fluxplace intent BOARD --constraints C [--spec S]             # the gate: every item met / NOT MET, exit 1 on unmet hard
    fluxplace intent BOARD --constraints C --apply --out O        # seat the hard items only (what floorplan / place do first)
    fluxplace intent BOARD --constraints C --connectors           # the connector table

Code: `fluxplace/intent.py` (keys, zones, gate, seating engine, floorplan hooks), `fluxplace/connectors.py` (entry
direction), `fluxplace/intentdraft.py` (the draft). Tests: `tests/test_intent.py`.

## Keys

All under `[intent]`. Every `refs` list takes globs (`"U30*"`). Sides are board axes as drawn: `left` = min x, `top` = min y.
Names below are examples.

```toml
[intent]                 # the table's presence switches the gate on for `floorplan` and `place --strategy routable`
edge_gap_mm = 1.0        # how far from the outline still counts as "on the edge"
plug_mm = 5.0            # default depth kept clear in front of a part that faces out

[[intent.edge]]          # part(s) on an outline edge
name = "radio-modules"
refs = ["U20", "U21"]
side = "not:top"         # any | left | right | top | bottom | "not:<side>" | a list of sides
facing = "out"           # the opening / antenna end points across that edge
front = "+x"             # that end in the FOOTPRINT frame (+x -x +y -y); needed when it cannot be derived (see below)
clear_mm = 3.0           # nothing may sit in front of it inside this depth (default plug_mm when facing = "out")
overhang_mm = 0.5        # the courtyard may pass the outline by this much (pads stay inside)
gap_mm = 1.0
hard = true              # default true

[[intent.near]]          # keep parts together, with a budget
name = "rf-in"
chain = ["J1", "C7", "U3"]   # pad to pad along the chain, link by link (closest pads on the shared net), summed
max_mm = 12.0
hard = true              # default false = a weighted cost
weight = 3.0             # soft weight (hard: 20)

[[intent.near]]
name = "jack-leds"
refs = ["R8", "R9"]      # each ref to one part
to = "J2"
max_mm = 8.0

[[intent.away]]          # minimum separation, courtyard to courtyard, every a against every b
name = "switchers-vs-receiver"
a = ["U10", "L10"]
b = ["U3", "J1"]
min_mm = 20.0
hard = true              # default true; false = penalty only

[[intent.region]]        # keep-out / keep-in rectangle
name = "under-module"
rect = [10.0, 20.0, 60.0, 55.0]    # or: under = "J5", margin_mm = 8.0   (the courtyard of J5, grown)
side = "B"               # F | B | both
max_height_mm = 1.5      # keep-out: only parts TALLER than this are forbidden; leave it out = nothing at all
allow = ["J5"]
# keep = "in", refs = ["C1", "C2"]   -> these refs must be inside the rectangle
# refs = ["J*"] on a keep-out        -> only these refs are concerned

[[intent.group]]         # several parts are ONE rigid body
name = "card"
refs = ["J7", "H1", "H2"]          # socket + stand-offs
swept = "all"            # the bounding box of these refs (or a list of them) is occupied space ...
swept_margin_mm = 4.0
swept_side = "B"         # ... on this side (default: the side of the first ref) ...
swept_max_height_mm = 1.4          # ... parts up to this height may sit under it; leave it out = nothing, holes included
swept_allow = ["J8"]
rel = { H1 = [40.0, 0.0, 0.0] }    # optional: [dx, dy, rot] in the frame of the first ref, verified by the gate

[intent.support]         # an IC's support parts (clusters.py: decoupling, feedback, enable, boot, switch node)
follow = true            # they move WITH the IC whenever it is seated or relocated, then are re-seated at their pins
decap_hard = true        # a decoupling cap beyond its limit FAILS the gate, offenders listed
hard = false             # the same for the other support parts
waive = ["C99"]

[intent.connectors.J9]   # override what the footprint says
entry = "side"           # side | top
opening = "+y"
edge = false             # a side-entry connector that is NOT meant to sit on an edge
overhang_mm = 0.5
```

Part heights for `max_height_mm`: `[placement.heights]` (`"U2*" = 2.3`, first match wins), else an estimate for chip passives
from the size code in the footprint name, else **unknown = treated as tall** (the gate prints "height unknown").
`[[intent.region]] rect` is a registered coordinate (`fit-sheet` shifts it).

## Connectors

`connectors.py` reads the entry direction of every connector (reference J/P/CN/CON/X/USB or a `Connector*` library):

| entry | from | consequence |
|---|---|---|
| `side` | name: Horizontal, RightAngle, EdgeMount, EdgeLaunch, `_RA_`; or a courtyard that reaches >= 2 mm beyond the pads on one side only | implicit hard `edge` item: on an edge, opening out, `plug_mm` clear; an edge-launch footprint may overhang as drawn |
| `top` | name: Vertical, Straight, TopEntry, coax snap-on families; or an SMD pad grid in a symmetric courtyard | never under a height-limited region or a swept area, whatever its own height |
| `card` | name: M.2, NGFF, mPCIe, SIM, SD | no edge rule; the card area is a group's swept area |
| `unknown` | nothing decides | **asked**: `connector-entry:<footprint>` = `side` \| `top` (one question per footprint) |

## How it is honoured

1. **Hard intent is seated first, then held** (`intent.prepare`, run by `floorplan` and `place --strategy routable` before the
   search): rigid groups, hard `near` chains that start at a held part, edge parts (largest first, each with its support
   parts), the remaining hard `near` (no held member: the parts become one rigid body the floorplan moves as a block).
   Movable parts left on a held part or in a forbidden zone are moved to the nearest legal pose (an IC with its support
   parts; a support part first gets another seat at its own pin). Held parts are added to `[placement] fixed` for that run.
2. **Zones**: regions, swept areas, the clear depth in front of an edge part and hard `away` distances from held parts are
   forbidden rectangles in `legalise._Model` - the one model behind `legality.Legal` (every placer) and the legaliser.
3. **Soft intent is cost** in the floorplan: `near` = a two-pin net with weight and budget, `away` = a penalty; hard `away`
   between two movable blocks is a legality test.
4. **The gate** runs at the end of `floorplan` and `place --strategy routable`; an unmet hard item fails the command and the
   pipeline stage. Without an `[intent]` table nothing is gated (`fluxplace intent` still reports the connector rules).

A floorplan that ends `FAIL` (fan-out, legality) now exits 1 and fails its pipeline stage. The only way past it is
`[floorplan] waive_fail = "reason"`, which is printed and recorded in the stage line.

## Reading the gate

```
INTENT: FAIL - 30 item(s): 25 met, 4 not met (1 hard), 1 not judged; report BOARD.intent.json
  NOT MET hard near      rf-in: J1 - C7 - U3: 23.99 (limit 12.0); links 4.5 + 19.5
  NOT MET soft support   other support parts within their limit (87 owned): 1 (limit 0); R60 44.3>5 mm of U10.1
  ?       hard group     card: rigid (J7, H1, H2): - (limit -); ... no `rel` poses to verify against
```

One row per item (per ref for `edge` and `refs`/`to`): state, hard/soft, kind, name, measured value, limit, detail. `?` = the
files do not let the tool judge (an unknown connector, a group without `rel`). `--full` lists the met rows too.

## The draft

`intent-draft` writes `BOARD.intent-draft.toml`. `# derived:` blocks are read from the board (connector names, a part's own
keep-out rule area, RF net classes walked part to part, the support clusters) and are live TOML. `# guess:` blocks (a card
socket with stand-offs, the space under a board-to-board connector, switchers near RF parts, any number the files do not
hold) are commented out with `#? `: pasting the draft applies no guess. `# ask:` lines name the question to answer.

## Limits

- Named sides use the board's bounding box; an outline with cut-outs is still checked by the legality model.
- The floorplan does not move held parts: where a hard item lands is decided by the seating pass (wire length to what is
  connected), not by the annealer.
- A footprint's own rule areas do not move with it when the tool moves the footprint (board writer); give `front` instead of
  relying on the rule area after a move.
- `rel`-less groups are rigid by construction but not verified by the gate.
