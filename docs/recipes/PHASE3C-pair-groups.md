# PHASE3C — failed pairs go back to the pair builder per connector group (D71cj c)

Worktree /var/kicad/fluxplace-wt/phase3a (branch phase3a). Board-agnostic code only (no refs/nets of any board).
Do not commit. No real router runs (tests mock them). Line numbers approximate; re-find by name.

## Measured (fable-w14, W14 bare board)
- `pairs` with all 17 high-speed pairs in one call: 7/17 complete; 10 handed to the router (state c), 8 of them
  "escape blocked" (e.g. "no clear break-out for J11.65; blocked by track USB_HUB_DP B.Cu" — the pair's own or a
  sibling pair's copper laid earlier in the same call).
- The same builder on ONE connector group alone (the 3 USB pairs): 2/3, and 3/3 when `[pairs.USB_] gap_mm = 0.14`.
- Rule D71cj (c): a pair that fails is never routed as two plain nets; it goes back to the pair builder with only its
  connector group, then stays an open pair gap (repair loop) if it still fails.

## A. Groups: `pairsonly.groups(B, cls, pairs)` (new function in pairsonly.py)
-> `{group_ref: [(p, n), ...]}`. For each pair, count the pads of both legs per footprint ref (`B["parts"]` / pad
ownership as `routediag` exposes it — look at how `pairs.py` finds a pair's terminals). The pair's group = the
footprint with the most pads of the pair whose pad count >= 4 or whose ref holds pads of >= 2 different pairs;
tie -> the footprint with more pairs overall, then ref name. A pair with no such footprint is its own group
(`"<p>/<n>"`). Groups are ordered by size descending. Test with a synthetic B: two pairs on a connector X and one pair
on chip Y and connector Z -> groups {X: [...2], Z or Y: [1]}.

## B. Regroup pass: `pairsonly.regroup(board, out, cons, spec, pairs, log, budget_s)`
For the given failed pairs (list of (p, n)):
1. Group them with A (on the board's B / cls).
2. For each group in order: call the existing `reroute(cur, tmp, names=[legs of the group's pairs], cons, spec,
   allow_shove=True, unlock=True, budget=...)` (read `reroute` to confirm the names format and that `unlock` frees only
   the group's own copper). Keep `tmp` as `cur` when the report shows more pairs `rerouted`/`already_ok` than before and
   `reroute`'s own guard passed; else keep `cur`.
3. If a group still has failures and its pairs have a `[pairs.<family>]` without `gap_mm`, retry once with a cons copy
   where that family gets `gap_mm` = the minimum clearance of the fab profile rounded up to 0.01 (use
   `constraints.fab_profile(cons)["min_clearance_mm"]` or the routerules clearance — whichever exists; say which).
   This is the measured USB fix generalised; record it in the result as `gap_retry`.
4. Return `{"board": out, "groups": {ref: {"pairs": [...], "laid": [...], "failed": [...], "gap_retry": bool}},
   "failed": [all still-failed (p, n)]}`; write `out` (copy of the final cur).

## C. Where it runs
1. **Route stage, krt-whole** (`krtchain.run`): right AFTER the `rails` step (PHASE3B fix 1) — order D71cj: rails
   first, then pairs. So: `planes`, `rails`, `pairgroups`, `diff`, `route`. The `pairgroups` step runs `regroup` on the
   incomplete pairs from `pairaudit` (rows status != complete, legs not in `C.not_pairs`). It is a Python step, not a
   KRT command: look at how `steps` are executed (`runner(cmd, log)`) and add the smallest hook that lets one step be a
   Python callable (e.g. a step whose cmd is a callable is called as `fn(src, dst) -> rc`). Skip when no incomplete pair.
2. **Router never gets a pair leg as a plain net**: `pairfb.router_skip` -> return `[n for n in todo if n in controlled]`
   when mode == "exclude" (drop the `n not in handoff` exception), and in krtchain the `route` step's `--nets` excludes
   the legs of pairs still failed after `pairgroups` (find where `sig` / the nets list is built; KRT `--nets "*"` may
   need an explicit list or an exclude flag — check KRT `route.py --help` for an exclude option and use it; if none,
   pass the explicit net list minus the failed legs). Update docstrings that say handoff pairs go to the router.
3. **Close loop rung** (`gaps.py`): new rung `pairgroup` = `regroup` on the target gaps' pairs (only those in the gap list),
   registered in DISPATCH: `"pair-open": ["pairgroup"]`, `"pair-asym": ["pairgroup"]`, `"pair-skew": ["tune", "pairgroup"]`
   where `tune` is a rung calling the existing `tune.tune_pairs` on the target pairs (find its signature; if it needs
   things the rung ctx lacks, register only `pairgroup` and say why). LEVEL: `pairgroup` = "L1". Pair rungs use
   the relaxed R5 set from PHASE3A fixes (already keyed on `kind.startswith("pair-")`).

## D. Tests
- A (synthetic groups), B (monkeypatch `reroute` to a fake that lays pair k on the second call; check keep/revert, the
  gap retry, the result dict), C.1 (step order with one rail and one incomplete pair), C.2 (`router_skip` skips handoff
  legs; route nets exclude failed legs), C.3 (DISPATCH entries; rung returns no candidate when regroup lays nothing).
- Run every test file importing pairsonly, pairfb, krtchain, pipeline, gaps, tune (tests/test_krtchain.py and
  tests/test_pipeline.py mock the router; skip files that really launch routers, list them).

## Report (under 35 lines)
`git diff --stat`, test counts, the group map `groups()` gives on /var/kicad/utv-comms-bridge/W14/stageB/rn400/pairs-rn.kicad_pcb
(constraints /var/kicad/utv-comms-bridge/W14/inputs/constraints.toml) for its incomplete pairs, deviations.

## E. (extra) pour_rails without shapely
`/usr/bin/python3` has no shapely, so `power.pour_rails` skips every pour. Replace the shapely buffer/union with pcbnew's
own geometry: for each rail track on the layer, `t.TransformShapeToPolygon(poly_set, layer, clearance=int(pcbnew.FromMM(buf)), ...)`
(check the exact signature in this KiCad 10 build with `help(pcbnew.PCB_TRACK.TransformShapeToPolygon)`), merge with
`poly_set.Simplify()` (or `BooleanAdd`), and build the zone outline from the resulting outlines (`poly_set.Outline(i)`).
Keep the rest of pour_rails unchanged. Test without the shapely shim: tests/test_power.py must pass and the skipped
shapely tests must now run. Do one real dry pour (no routing) on a COPY of
/var/kicad/utv-comms-bridge/W14/CURRENT.kicad_pcb (+ .kicad_pro) in /var/kicad/fluxplace-wt/phase3a/.scratch/ with
constraints /var/kicad/utv-comms-bridge/W14/inputs/constraints.toml and report: zones added per rail/layer, guard
before/after opens/shorts/errors, kept or dropped and why.
