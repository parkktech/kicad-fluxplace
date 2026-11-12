# `anchor-check`

### 3. Anchor orientation — `fluxplace anchor-check BOARD [--apply --out OUT]`

Connectors, modules and sockets are auto-detected (J/P/CN/X refs with >= 6 pads,
U refs with >= 40 pads; never MH/MK/H/TP/FID) or named in `[placement] anchors`.
Each allowed rotation is a true rigid rotation (pads move with the footprint) scored
with every other pad where it is: class-weighted MST length of the nets the anchor
touches, plus the worst pair span. Options that leave the outline or land on
another anchor are marked infeasible. `ANCHOR_MISORIENTED` when the current
orientation is more than `[preroute] anchor_tolerance_pct` (15) worse than the best
feasible one. `--apply` turns them one at a time (re-scoring in between).

    [placement]
    anchors = ["J200"]
    [placement.anchor.J200]       # rotations are DELTAS from the current orientation
    rotations = [0, 90, 180, 270] # default; a part within 1.5 mm of the outline is an
                                  # edge connector and only keeps its orientation
    [placement.group.cm5]         # a two-connector module turns as one body
    refs = ["J10", "J11"]
    centre = [30, 25]
    rotations = [0, 180]

Side flips are not evaluated (the text writer cannot mirror a footprint).
