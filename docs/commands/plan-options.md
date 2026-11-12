# `plan-options` - what-if recommender

```
fluxplace plan-options BOARD --constraints C [--spec S] [--max-grow 6] [--out-dir D] [--place-time 30] [--calibrate-board ROUTED] [--json F]
```
Never changes the project board or constraints. Each option writes `D/<name>.kicad_pcb`, `.fluxmap.json` and `.constraints.toml` (the `[plan]`
fragment that would reproduce it). Baseline = the standard class (track/clearance 0.15, via 0.45/0.20, 6 layers, no via-in-pad) and
`place --objective plan` in the current outline; if that passes (no sealed or oversubscribed pins, no missing via site, overflow 0) it prints "Fits as is".

Otherwise, options run in parallel worker processes (scratch outline/rules -> short `--objective plan` re-place -> plan-route):
edge growth (probe at `--max-grow`, bisection in 1 mm steps; pairs of the two nearest-miss edges when no single edge passes; parts touching an edge,
their keep-outs and edge-hugging keep-outs travel with it, anchors and mid-board parts stay); local neck-down 0.127 around the fine-pitch parts that
have findings (before global); global 0.127; sub-floor 0.10 (down to `[fab]` minimum; **labelled, never recommended**); smaller via (`[fab] min_via_dia_mm`);
via-in-pad for the same parts; dedicated +3V3 plane layer, swapped layer directions, 8 layers; combinations of at most two (growth +1/+2 mm with neck-down or smaller via;
plane + neck-down; neck-down + smaller via).

**Performance guard (rules, not advice).** A relaxation thins only low-speed single-ended nets. Controlled nets (pair/rf/clock classes, ETH_/USB/PCIE/audio/CLK
names, `[nets.X] impedance`) and rails keep their designed width in the capacity model (their demand is multiplied by designed pitch / local pitch,
`globalroute.mult`). An option whose neck-down cells overflow on such a net is invalid (`neck_violations`) and never recommended. Every row has a
"performance impact": `none`, `low-speed only`, `touches controlled-impedance nets (never recommended)`; options that lengthen controlled copper (> 2 % + 2 mm),
add controlled vias or return-path findings are marked "worse: ..." and rank below an equal option that does not.

**Ranking:** valid and performance-neutral, then smallest area, then lowest relative cost, then most margin (confidence, score). Cost = area cm2 x
(1 + penalties), a RELATIVE weight, not a quote: `[plan.cost]` per_cm2 1.0, layer_pair 0.40 (per extra layer pair), track_tight 0.08 (< 0.15), track_subfloor 0.30,
via_small 0.06, via_in_pad 0.20, neck_local 0.04. Output: a table (option, size, PLAN_SCORE, confidence, sealed, overflow, relative cost %, impact), the prompt line
"Fails at W x H mm. Passes with ... Alternatives: ... Recommended: ...", and the nearest misses when nothing passes. The caveat "verdict unproven" is printed unless
`--calibrate-board` names a routed board on which at least 60 % of the open nets were flagged by the planner.
