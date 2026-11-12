# fluxmap.json - schema (version 1)

`fluxplace map BOARD --constraints C` writes `<board>.fluxmap.json`; `plan-route` rewrites it with the planner sections filled.
Compact (one line, no spaces), deterministic (same board + constraints = same bytes), mm rounded to 0.01, lists sorted.
Real 337-part / 270-net board: about 250 kB. Agents read slices: `fluxplace map --show SECTION [--ref R | --net N | --group G]`.

| key | content |
|---|---|
| `fluxmap` | schema version (1) |
| `board` | `file`, `outline` (polygon, arcs flattened), `bbox`, `layers` [{name, role `signal`/`plane`/`mixed`, net}], `floors` {track, clearance, via, drill, fab_min_*}, `keepouts` [{name, side `F`/`B`/`all`/layers, no [tracks, vias], box}], `height_regions` |
| `parts` | per part: `ref v fp x y rot side court[x0,y0,x1,y1] h pads pitch mode group`; `mode` = `fixed` (locked, `[lock.at]`, `[placement] fixed`, mounting hardware), `anchor` (connectors, modules, `[placement.group]`), `movable`; `h` from `[placement.heights]` else null |
| `relations` | support part -> served pin: `part kind serves net max now sure`; from `clusters.build` (owner by net class: decap, crystal, termination/ESD, feedback divider, inductor...). `max` = class limit mm, `now` = distance mm, `sure` = ownership confident |
| `groups` | functional blocks: `name src members anchor box area ext_nets{N,E,S,W} ext_total`. `src`: `placement.group`, `blocks` (`[placement.blocks]`/spec/refdes), `sheet` (schematic sheet), `cluster` (an IC with its supports) |
| `nets` | `n c pads[[ref.pad,x,y,side]] budget mst pair state`; `c` = net class (netclass.py), `budget` = effective mm, `mst` = ratsnest mm, `state` = `routed` / `open:N` / `unrouted` (open counts need `map --state` or plan-route) |
| `escapes` | planner: `pins{ref:{pad:[kind,...]}}` for fine-pitch parts and every sealed/via/no-site pin (`d` direct [dir,lane], `v` via slot [x,y,stub], `p` pour, `s` sealed [blocker], `n` no via site), `headroom{ref:{pins, free_slots{N,E,S,W}, free_total}}` |
| `grid` | planner: `origin cell nx ny layers`, and per signal layer digit strings (row-major, j*nx+i, `x` = 10+) `capH capV useH useV`, plus `via_cap via_use` |
| `route` | planner: per net `len budget vias layers path` (path = `layer:cell,cell;...` base36 cell indices) |
| `channels` | planner: overflow regions `box layer demand cap overflow nets parts` |
| `verdict` | planner: `score result mode components counts timing floors findings` (worst 60, ranked) |

Without a planner run the last five sections are empty (`{}` / `[]`).

`fluxplace map --render OUT.png|OUT.svg` writes OUT_F, OUT_B (parts coloured by group, courtyard box, support-part lines green/red =
within/over limit, red cells = capacity overflow, grey = capacity 0, red cross = sealed pin, magenta cross = no via site) and
OUT_inner (blue = inner-layer demand/capacity).
