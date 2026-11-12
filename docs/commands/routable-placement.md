# Routable placement overview and net classes

## Routable placement (2026-11) — net classes, clusters, anchors, pre-route gate

Built after a 337-part board passed "0 overlaps, 0 shorts" and then left 372
connections open: every cause was visible in the netlist before routing. Seven
stages, each usable alone; all read the board as text (`routediag.load`).

### 1. Net classes — `fluxplace/netclass.py`

Every net gets a class, a pull weight and a length budget (mm). Order:
rf > pair > switch_node > clock > rail > bypass > signal.

| class | budget | weight | meaning |
|---|---|---|---|
| switch_node | 6 | 10 | regulator SW pin to its inductor (net name, spec pin name SW/LX/PH) |
| bypass | 3 | 8 | the net has one IC pin and a capacitor whose other pad is ground |
| rf | 15 | 6 | `[rf] nets`, or an RF-named net |
| clock | 20 (crystal 8) | 5 | CLK/XTAL/OSC names, or a net with a Y/X part (<= 3 pads: crystal) |
| pair | 30 | 4 | `[pairs.*]` prefix, `_P/_N`, `DP/DM`; judged per net |
| signal | 30 | 1 | the rest |
| rail | none | 0 | ground, `[power.*]` nets, nets with >= 8 pads; never pulled |

Multi-drop nets use MST length against `budget * (1 + 0.5 * (parts - 2))` (distinct references).
Override in the constraints TOML:

    [placement.budget]  switch_node = 5
    [placement.weight]  pair = 6
    [placement.net.PCIE_TX_P]  budget = 45  weight = 8  class = "pair"
