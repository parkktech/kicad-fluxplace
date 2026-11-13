# wl-refine: shorter wires, the gate never worse

    fluxplace wl-refine BOARD --constraints C --spec S --out O [--seed N] [--passes N] [--time-limit S] [--allow K=V,...]

Starts from a legal placement and keeps only moves that shorten the class-weighted wire length (pin half-perimeter per net x
net-class weight, plus the pull of every decoupling cap and support part toward the pin it serves) **without** making
anything else worse. Source of truth for the rules: the docstring of `fluxplace/wlrefine.py`.

| | |
|---|---|
| moves | one part to its optimal region (weighted median of its nets' boxes), 0/90/180/270; an IC with its support parts as one body; same-footprint swaps |
| per move | legal, no new blocked fine-pitch lane, intent (zones, hard/soft near and away), demand on the plan-route grid, support limits, escape plan of the neighbourhood |
| per batch (40 moves) | incremental plan-route + gate score; a worse batch is undone and replayed by halves |
| per pass | plan-route from scratch; replayed by halves when worse |
| result | whole gate with the escape reservation; back one checkpoint while worse; in the end the input itself |
| never moved | fixed / locked parts, anchors, mounting hardware, parts held by an edge / group / region-under intent item |
| not done | side flips |

"Worse" = more legality issues, blocked or sealed pins, unmet intent items, nets at risk, or a higher overflow, gate score
(`route-ready`) or PLAN_SCORE than the input. Output: one line (`wl-refine: N part(s) moved; HPWL ... -> ...; decap mean ...;
blocked pins ...; overflow ...; risk nets ...; intent met ...`), `<out stem>.wl-refine.json` with before / after / kept and
rejected counts per test. Same `--seed`, same result.

`[placement.wl]`: `enabled = true` runs it as the last step of `place --strategy routable` (not after `--refine` /
`--objective plan`) and of `floorplan` (there the whole-board fan-out is re-run before and after: more blocked pins = the
refined board is thrown away); the pipeline stage entry gets `"wl": "hpwl A>B decap A>B blocked A>B ovf A>B"`.
Off by default: on a board already placed up to the plan-route limit, shorter wires raise the channel overflow, the pass
keeps nothing and says so after two batches. `allow = {overflow = 6, risk_nets = 8, plan_score = 120}` (or `--allow`) is the
what-if: those four numbers may end that far above the input; recorded as `allowance`. Other keys: `time_limit_s` 600,
`passes` 4, `batch` 40, `max_try` 8, `reach_mm` 3, `w_decap` 3, `w_support` 1, `soft_tol_mm` 0.5, `hot_frac` 0.9.
