# `review` - the design-review gate

### The design-review gate (`review`)

An outside reviewer looked at a finished board — 0 DRC at full scope, netlist
verified, sourcing graded, packaged for the fab — and found nine problems in an
afternoon. Every tool had been green, because every tool checked the board
against **itself**: schematic vs copper, netclass vs track, pad vs pin. None
checked it against the **spec**, the **datasheet** or the **environment**.
`fluxplace review` does, and `fab`/`deliver` refuse to package a board with a
FAIL in it.

| Finding | What it holds the board against |
|---|---|
| `NET_STRAIGHT_COPPER` | `[nets.X] straight_copper = ["J1:1","J2:1"]` — the net may touch **only** those pads. Caught two ESD arrays sitting on a fail-safe PTT line the spec said no component may touch. |
| `PAIR_SKEW`, `PAIR_LAYER_MISMATCH`, `PAIR_VIA_MISMATCH` | P and N of every diff pair (by name convention) must arrive together, on the same layers, through the same via count. Caught a 45 mm intra-pair mismatch on a Gigabit pair. |
| `RF_IMPEDANCE_OFF`, `RF_VIA_COUNT` | RF-named nets graded segment by segment **on the layer the copper is actually on**, against the planes that layer really sees (microstrip on the outside, asymmetric stripline inside). A 0.15 mm trace is 50 Ω on a 0.1 mm outer prepreg and ~66 Ω on the inner layer where 85 % of the net was routed. |
| `FOOTPRINT_PACKAGE_MISMATCH`, `PIN_COUNT_MISMATCH` | The distributor's package for the MPN (DigiKey `Supplier Device Package` / Mouser `Package / Case`) vs the footprint name, by package family and pin count. Caught a PowerDI5060-8 MOSFET on a SOIC-8 land pattern. |
| `PINMAP_ROLE_MISMATCH`, `PINMAP_PIN_ABSENT`, `PINMAP_UNVERIFIED` | The spec's pinmap vs the **KiCad official symbol** for the same part (`/usr/share/kicad/symbols`, prefix-matched on the MPN). Ground on the wrong pin is a FAIL; a pinmap with no library match and no `pinmap_source` is a WARN — evidence, or it is a guess. |
| `TEMP_RATING` | Operating temperature from the distributor vs `[env] temp_min_c/temp_max_c`. Caught a 0..+70 °C LAN transformer on an outdoor vehicle product. |
| `SPEC_SIZE_MISMATCH`, `SPEC_LAYER_MISMATCH`, `SPEC_COMPONENT_MISMATCH`, `SPEC_FOOTPRINT_MISMATCH` | The spec JSON vs Edge.Cuts, the copper layer count, and the component list. A spec that says 65 × 50 mm 4-layer for a 76 × 87 mm 6-layer board is a document nobody is reviewing against. |
| `HOLDUP_SHORT` | `[power."+5V"] holdup_ms / nominal_v / min_v / load_a` vs the bulk capacitance actually on the rail. 3000 µF holds 5.0→4.75 V at 1 A for 0.75 ms, not "tens of ms". |
| `TVS_MARGIN` | `[protection]` clamp voltage vs the downstream device rating (clamp from the constraints or from the distributor data). |
| `ENV_UNDEFINED` | Nobody answered the environment questions, so nothing can be derated. |

**No part without its papers.** With `[docs]` in the constraints (`datasheets =
"docs/datasheets"`, `strict = true` — the default) the gate fails a part that
has no manufacturer part number (`MPN_MISSING`), whose datasheet is not on disk
in the project (`DATASHEET_MISSING`), that uses more than two pins without a
named pinmap (`PINMAP_MISSING`), or whose pinmap names are not found on the
datasheet page it cites (`PINMAP_EVIDENCE_WEAK`). A distributor that cannot
describe the part is a FAIL too (`PART_DATA_UNAVAILABLE`). The rule exists
because a board reached an outside reviewer with an ESD array's ground on the
wrong pin — the pinmap had been typed from memory and no tool had asked for
the page it came from.

Datasheet fetching does not need a human: `datasheets` asks DigiKey, Mouser
and Nexar for the URL and downloads it with **`curl_cffi` impersonating
Chrome** (`pip install fluxplace[docs]`), which is what the manufacturer CDNs
that 403 a plain client (Amphenol, Littelfuse, onsemi, C&K, Molex, measured)
actually check. A host that is unreachable outright is reported for a browser
fetch and registered with `--adopt`.

Part data comes from the **DigiKey and Mouser APIs**, with **Nexar (Octopart)**
as the third source when both miss — its datasheets are mirrored on
`datasheet.octopart.com`, which serves plain PDFs where manufacturers'
own hosts bot-wall a script (`NEXAR_CLIENT_ID` / `NEXAR_CLIENT_SECRET`,
supply.domain). Nexar meters part lookups (10/day on the evaluation plan),
so it is consulted only after both distributors miss (same credentials as
`sourcing` and `models`), cached for 7 days beside the `mpn_map.json`. A part
the distributors cannot describe is reported as `PART_DATA_UNAVAILABLE`, never
silently passed. `--no-api` runs the offline checks alone.

```bash
PYTHONPATH=$KP python3 cli.py review --board board.kicad_pcb \
    --spec spec.json --constraints constraints.toml --json review.json
PYTHONPATH=$KP python3 cli.py fab --board board.kicad_pcb --out fab/ \
    --spec spec.json --constraints constraints.toml      # aborts on FAIL
```

Closing the findings is tool work too — `repair` and `tune` are the mechanical
fixes (`repair --pairs ETH_ --rf --remap map.json --fix-pads --text "…"`, then
`tune --prefix ETH_`), and `[review] waive = ["CODE:REGEX", …]` in the
constraints records a waiver *with the file it lives in*, so a waived finding
stays visible in review instead of disappearing.

Constraint blocks the gate reads (all optional, see `fluxplace/constraints.py`):
`[env]`, `[nets.<NET>]` (`straight_copper`, `max_vias`), `[rf]` (`target_z`,
`tolerance_pct`, `max_vias`, `nets`), `[pairs.<FAMILY>]` (`skew_mm`),
`[power."<RAIL>"]` (`holdup_ms` …), `[protection]`, `[fab]` (`blind_vias`,
`buried_vias`, `microvias`, `min_track_mm`, `min_clearance_mm`,
`min_drill_mm`, `min_annular_mm`, `min_via_dia_mm`, `min_silk_width_mm`,
`profile`), `[silk]` (see `silk.md`).

`review` also runs the part-entry gate (`PART_*`, [parts.md](parts.md): offline unless the review fetched distributor data; waive like any code) and the pin-group checks (`MAG_*`, `PINGRP_*`, [pin-groups.md](pin-groups.md)).
