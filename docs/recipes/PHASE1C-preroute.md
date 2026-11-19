# Phase 1c recipe: AI pre-route of critical nets (D9, guide only D7, packet mode D2)

Names (coordinator, final): module `fluxplace/critroute.py`, tests `tests/test_critroute.py`, stage `critroute`
(`st_critroute`), constraints `[critroute] nets`, work dir `W/<n>-critroute/ai/`, mode key `[ai] mode`. `preroute.py`,
`test_preroute.py` and `[preroute]` are existing modules and stay untouched. Avoid regions are circles `[x, y, r]`.

Implementation notes (deviations from the signatures below): `critical(board, cons, spec, cons_path=None)`;
`packet(board, net, cons, spec, cons_path=None, cls=None, R=None, B=None)`; `load_actions(path, nets=None, layers=None)`
validates names and layers when given; `apply(..., crit=None)` takes the critical list. The `try` call of `packet`
uses the first open connection of the net.

Repo `/var/www/html/kicad-fluxplace`, Python `/usr/bin/python3`. Do not commit. No boards under `/var/kicad`, no
kicad-cli in tests. No new CLI command or flag. Read each callee's signature (def line + docstring) before calling it.

## What it does

A pipeline stage `critroute` between `pairs` and `plan-route` (CONSTRUCTIVE) and before `route` (classic STAGES).
1. Pick the critical nets that are still open: `netclass.classify` classes `rf` and `clock`, plus every net listed in
   `[critroute] nets` of the constraints (optional list). Pairs are not pre-routed here (the `pairs` stage owns them).
2. Write one packet per net to `W/<n>-critroute/ai/<net>.packet.json` (schema below, under 4 kB).
3. Read the action file `W/<n>-critroute/ai/actions.json` if present (written by an AI agent between runs). Missing
   file: mode `[ai] mode` (default `"packet"`): `"packet"` -> stage returns verdict `"packets"` and the pipeline stops
   with exit 5 after writing the packets; `"off"` -> default actions.
4. For each net in action order: for each open connection of the net (`handroute.open_nets`):
   `r = handroute.suggest(cur, net, conn=i, constraints=cons_path, layers=act.layers or None, avoid=act.avoid or None)`
   (`avoid` is a list of `(ax, ay, ar)` circles);
   if `r["found"]`: `handroute.lay(cur, net, r["path"], out=tmp, constraints=cons_path, lock=True)` and keep `tmp`
   when `gaps.measure` shows opens fell and shorts did not rise (else drop it). One record per try in the attempt log
   (`stage "critroute"`, method `"suggest+lay"`).
5. Stage board = the last kept board. Verdict `pass` (all critical nets closed) or `warn` (some left, listed).

Default actions (mode off, or a net missing from actions.json): order = `rf` first, then `clock`, then the
`[critroute] nets` list; layers = `gaps.signal_layers(board)`; avoid = none.

## Files

| file | change |
|---|---|
| `fluxplace/critroute.py` | new (was `preroute.py`, existing module, untouched), about 200 lines: `critical(board, cons, spec)`, `packet(board, net, cons, spec)`, `load_actions(path)`, `apply(board, out, actions, cons_path, cons, attempts, log)`, `stage(c, n, board)` |
| `fluxplace/pipeline.py` | `"critroute"` in `CONSTRUCTIVE` after `"pairs"`, in `STAGES` before `"route"`; `st_critroute` -> `critroute.stage`; `FUNCS`; exit code 5 when a stage returns verdict `"packets"` (find where the pipeline maps verdicts to the exit code; add `"packets"` to `VERDICTS`) |
| `tests/test_critroute.py` | new |
| `tests/test_pipeline.py` | stage order + exit 5 test |

## Packet (`packet()`), keys exactly

```json
{"v": 1, "net": "BT1_RF", "class": "rf", "reason": "<netclass reason>",
 "conns": [{"i": 0, "a": [x, y], "b": [x, y], "layers_a": [..], "layers_b": [..]}],
 "rules": {"width": .., "clearance": .., "via": [dia, drill]},
 "layers": ["F.Cu", ...],
 "hot": [{"side": "F", "x": .., "y": .., "util": ..}],
 "try": {"found": true|false, "reason": "..", "len_mm": ..},
 "ask": "order, layers, avoid circles [x, y, r] for this net; no coordinates of copper"}
```
- `conns` from `handroute.open_nets` (at most 6). `rules` from the handroute model's `net_rule(net)` (see how
  `suggest` gets `M` and `rule`). `hot`: `density.compute(...)["regions"]` within 15 mm of any conn end, at most 4.
- `try`: one `suggest` call with defaults, so the AI sees what the router does unguided.
- Clip to 4096 bytes by dropping `hot`, then `conns` beyond 3.

## Action file (`actions.json`)

```json
{"order": ["BT1_RF", "XTAL_IN"],
 "nets": {"BT1_RF": {"layers": ["B.Cu"], "avoid": [[x, y, r]], "why": "keep the pocket under U3 free"}},
 "tokens": 0}
```
`load_actions` validates: names exist in the packets, layers are copper layers of the board, avoid circles have 3
numbers with r > 0, at most 6 circles per net; anything invalid is dropped with a log line (never raises). `tokens` goes to
`ai_tokens` of each attempt line.

`suggest`'s `avoid` takes `(ax, ay, ar)` circles (handroute.py); `_avoid(circles)` converts the validated lists to tuples.
No box conversion.

## Tests (`tests/test_critroute.py`, fakes only)

1. `critical` picks rf and clock nets and the `[critroute] nets` list; skips nets with no open connection.
2. `packet` has exactly the keys above and is <= 4096 bytes after clipping.
3. `load_actions` drops an unknown net, a bad layer, a 2-number or r <= 0 circle; keeps valid entries.
4. `apply` with faked `suggest`/`lay`/`measure`: a found path that lowers opens is kept and locked (`lock=True` passed);
   one that raises shorts is dropped; attempt lines written with `stage "critroute"`.
5. `stage` with no actions file and mode `packet` -> verdict `"packets"`; mode `off` -> applies defaults.
6. Pipeline: `critroute` sits after `pairs` (CONSTRUCTIVE) and before `route` (STAGES); verdict `packets` gives exit 5.

## Done when

Full suite passes (`/usr/bin/python3 -m pytest tests/ -q -p no:cacheprovider`); existing `tests/test_pipeline.py` stage
directories renumbered `6-route` -> `7-route` (critroute now precedes route); report diff stat, the pytest line,
and every recipe point that did not match the code.

## Deferred (not in 1c)

Coarse pair centre lines: `pairs.lay_section(preset=(states, pol))` takes raw grid states, not waypoints; a
waypoint-to-states adapter is a separate item. Corridors as rule areas: no rule-area writer exists and KRT support is
unverified; `avoid` circles on `suggest` and locked copper with `--keep-input-copper` are the mechanism for now.
