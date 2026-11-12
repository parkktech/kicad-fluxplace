# `map` - the meta map

```
fluxplace map BOARD --constraints C [--spec S] [--out PATH] [--state]
fluxplace map --show SECTION [--ref R | --net N | --group G] [--map PATH]
fluxplace map --render OUT.png|OUT.svg [--map PATH]
```
Builds `<board>.fluxmap.json` (schema: `docs/fluxmap.md`): outline, layers with role, design floors, keep-outs, parts with
mode and group, support-part relations with max/current distance, groups, nets with class, budget and state. Deterministic.
`--state` adds routed/open per net (text connectivity). `--show` prints one slice (<= 6 kB): `--show parts --ref U5` gives the
part, its relations, escapes and nets; `--show nets --net PCIE_TX_P` gives the net, its planned route and findings that name it.
`--render` writes one page per signal layer (OUT_F, OUT_B, OUT_In2 ...) and OUT_overview (blocks, reserved corridors, overflow, sealed pins, relations). `plan-route` fills the planner sections of the same file.
