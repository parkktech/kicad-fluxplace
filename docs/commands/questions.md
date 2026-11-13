# Questions: ask, don't assume

Applies to `fanout`, `modules`, `floorplan`, the `pipeline` route stage (flags `--answer ID=CHOICE`, `--assume-defaults`, `--qjson`).
Code: `fluxplace/questions.py`.

A question = stable id, topic, what was found, options with consequences, default-if-forced, what would settle it. A command with
an open question stops BEFORE writing: exit 3, questions printed compactly (`--qjson`: JSON) and written to `<board>.questions.json`.
`pipeline` stops at the stage with verdict `questions`, exit 3.

Answers: `[answers] ID = "choice"` in the constraints, or `--answer ID=choice` (the pipeline keeps them in `state.json`). Both are
recorded in `<board>.fluxmap.json` (`answers`); a question is never asked twice. `--assume-defaults` proceeds with each default,
prints `ASSUMED: ID` and writes `<output>.assumed.json` (inherited by every board made from it); `fab` and `deliver` refuse such a
board. Decisive inferences are printed as `INFO inferred: ... because ...`, not asked.

| id | raised when | choices (default first) |
|---|---|---|
| `layer-role:LAYER`, `layer-roles:inner` | an inner layer's zones/tracks do not decide plane vs signal (zone 20-60 % of the outline, a plane with tracks; no inner layer has any zone/track) | signal, plane (planes) |
| `controlled:pair\|rf\|clock\|rail` | no net classes / `[pairs]` / `[rf]` / `[power]`; the nets are classed by name only (a clean `_P/_N` pair of a known interface is inferred, not asked) | controlled, ordinary |
| `rip-existing` | unlocked tracks/vias on the board, no `[route] allow_rip` | never, low-speed |
| `fixed-parts` | mounting hardware / edge connectors not locked or declared (`[placement] fixed`, `[lock.at]`) | lock, movable |
| `rail-pour:NET` | a rail (>= `rail_min_pads`) with no plane or declared pour | tree, pour (zone on the outer layer of most pads) |
| `rule-floors` | no `[fab]`/`[route]` floors and no design-rule minimums on the board | relaxed, standard, advanced |
| `spec-board` | the spec names parts/nets the board lacks (or the board has many parts the spec lacks) | trust-board, trust-spec |

Agents: on exit 3 relay the questions to the user and wait. Never answer them, never pass `--assume-defaults` for a deliverable.
