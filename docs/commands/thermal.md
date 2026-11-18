# `thermal` - a thermal area from `[thermal.NAME]` blocks

```
fluxplace thermal BOARD --constraints C --out O [--stage pre|post]
```

Constraints (mm). One block per area; every key but `side` and the `via` table is optional:

```toml
[thermal.modem]
rect = [x0, y0, x1, y1]        # or polygon = [[x, y], ...]
side = "B"                     # "F" | "B"
net = "GND"                    # default GND
exclude_refs = ["H201"]        # their pads get a cut-out of pad half-size + cut_mm (1.0)
mask_open = true               # post stage only
# clearance_mm, cut_mm, grow_mm (0.25), sliver_mm (0.5) override the defaults
[thermal.modem.via]
diameter = 0.46
drill = 0.2
pitch = 2.0
```

- `--stage pre`: locked through vias of `net` on a pitch grid inside the area. A site is skipped when a via of that size
  would be within clearance of existing copper, holes or another via of this run (pads by bounding box). A no-tracks rule
  area on `side` (pads, vias, pours and footprints allowed) is added. Run once per board: a second pre run is refused.
- `--stage post`: solder-mask opening on `side` (blocks with `mask_open = true`): the area minus the cut-outs, carved
  around every non-`net` via, pad and `side` track by `grow_mm`, then opened by `sliver_mm`. Run after routing.

Writes `--out` (never the input); the `.kicad_pro` sidecar is copied. Refusals: bad side, missing via fields, unknown
`exclude_refs`, net not on the board, `--out` equal to the input. Prototypes this generalises: W11 `02-thermal.py`,
`02b-h201-clear.py`, `03-thermal-mask.py` (their constants are now block keys).
