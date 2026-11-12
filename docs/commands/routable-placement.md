# Routable placement overview and net classes

## Routable placement (2026-11) — net classes, clusters, anchors, pre-route gate

Built after a 337-part board passed "0 overlaps, 0 shorts" and then left 372
connections open: every cause was visible in the netlist before routing. Seven
stages, each usable alone; all read the board as text (`routediag.load`).

### 1. Net classes — `fluxplace/netclass.py`

Every net gets a class, a pull weight and a length budget (mm). Order:
rf > pair > switch_node > clock > rail > bypass > slow > signal. Each default has a physical
basis (`netclass.BASIS`); defaults with none were loosened on 2026-11-11 (round 2).

| class | budget (old -> new) | weight | physical basis |
|---|---|---|---|
| switch_node | 6 (unchanged) | 10 | loop inductance ~1 nH/mm of narrow trace: 6 mm ~ 5 nH in the switching loop |
| bypass | 3 (unchanged) | 8 | loop inductance: 3 mm ~ 2.5 nH keeps the cap's SRF above 100 MHz |
| rf | 15 -> 17.7 | 6 | insertion loss, FR-4 microstrip 0.0118 dB/mm/GHz: 0.5 dB at 2.4 GHz. `[placement.rf] freq_ghz = 1.575  loss_db = 0.5` recomputes (26.9 mm for GNSS) |
| clock | 20 (unchanged); crystal 8 | 5 | edge rate ~0.7 t_r/(6 t_pd), t_r 1 ns, t_pd 6 ps/mm. Crystal 8 mm = ~0.8 pF stray C against a load-cap tolerance of ~1 pF; an ACTIVE oscillator (>= 3 pads with a supply pin, e.g. a SiT/CDC part) is a plain clock, not a crystal (it was held to 8) |
| pair, usb2 | 30 -> 100 | 4 | USB 2.0 HS spec 5 in host-side trace, derated; skew <= 3 mm (`[pairs.USB_]`) |
| pair, usb3 | 30 -> 100 | 4 | 5 Gb/s ~1 dB/in at 2.5 GHz Nyquist, ~6 dB board budget |
| pair, pcie | 30 -> 100 | 4 | Gen2/3 x1 ~1.2 dB/in at 4 GHz, ~8 dB budget |
| pair, eth | 30 -> 100 | 4 | 1000BASE-T PHY to magnetics: vendor guidance <= 100 mm, skew <= 1 mm |
| pair, other | 30 -> 60 | 4 | speed unknown: ~1.5 ns edges, reflection-critical length |
| pair, audio | 30 -> none | 4 | MIC/SPK/INJ/HPL/ADC/DAC/MIX... analog pair: only noise pickup matters |
| signal | 30 -> 60 | 1 | digital edges >= 2.5 ns: t_r/(6 t_pd) ~ 62 mm before a stub needs termination |
| slow (new) | none | 1 | I2C, reset, interrupt, LED, key, status, boot, PPS, PTT, CAN <= 1 Mb/s, analog audio: kHz-class, effectively unbounded (SPI/I2S clocks and antenna nets are excluded by name) |
| rail | none | 0 | ground, `[power.*]` nets, power-named nets (`+3V3`, `CM5_3V3`, `VBAT...`), nets with >= 8 pads; current capacity, not length; never pulled |

Pair kind comes from the net name (`netclass.pair_kind`). `[placement.budget] pair = N` sets every
kind; `"pair:usb2" = N` one kind. Multi-drop nets use MST length against
`budget * (1 + 0.5 * (parts - 2))` (distinct references). Override in the constraints TOML:

    [placement.budget]  switch_node = 5
    [placement.weight]  pair = 6
    [placement.net.PCIE_TX_P]  budget = 45  weight = 8  class = "pair"
    [placement.budget."USB_HUB_DP"]   # per-net budget with a recorded reason
    mm = 60
    reason = "hub is on the far side of the CM5 by the mechanical stack"

Every per-net override is listed by `route-ready` ("budget overrides in effect", with its reason,
the length now and ok/OVER) and its reason is appended to that net's NET_OVER_BUDGET message.

**ANCHOR_SPACING** (WARN) vs **NET_OVER_BUDGET** (FAIL): the floor of a net is the MST over the pads
of FIXED parts (locked, `[lock.at]`, `[placement] fixed`, mounting hardware, detected anchors) - a
sub-tree no move can shorten. When the floor alone exceeds the budget the net is ANCHOR_SPACING with
that minimum (multi-pad nets included); only the excess beyond 1.15 x floor + 1 mm, which movable parts
add, stays a FAIL (against the floor as its limit).
