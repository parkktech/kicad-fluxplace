# corpus

Standing multi-board regression scorecard. Runs the analysis commands over every board in a
user manifest, one subprocess per step, and compares the result with a baseline.

```
fluxplace corpus [--manifest M] [--tier fast|full] [--board NAME ...] [--work DIR] [--out DIR]
                 [--baseline FILE | --baseline-ref GIT_REF] [--update-baseline] [--repeat N] [--jobs 4]
```

## Manifest (TOML, user data)

Default `~/.config/fluxplace/corpus.toml`. Relative paths resolve against the manifest.

```toml
[rules]                       # optional; defaults shown
drc_noise = 3                 # DRC error count may rise by up to this before it is a regression
runtime_factor = 1.5          # step runtime regression: > factor x baseline ...
runtime_floor_s = 2.0         # ... and more than this many seconds slower

[[board]]
name = "my-board"             # unique
board = "my-board.kicad_pcb"  # required
spec = "my-board-spec.json"   # optional: enables schematic + netlist check
constraints = "my-board.toml" # optional
tier = ["fast"]               # fast, full, or both
pipeline_args = ["--route-budget", "600"]   # full tier only: extra args for `pipeline`
timeout_s = 900               # per step
notes = "free text"
```

## Inputs and outputs

- Each run copies the board, its sibling `.kicad_pro`, spec and constraints into `WORK/<name>/`.
  Originals are hashed before and after; a changed original is an `error.inputs_modified` regression.
  Nothing is written beside the originals.
- `OUT/<tier>/scorecard.json` and `scorecard.md`; `compare.json` and `compare.md` when comparing;
  per-step logs under `WORK/<tier>/<name>/logs/`.
- `--update-baseline --baseline FILE` writes the current scorecard to FILE.
- `--baseline-ref REF` runs the same manifest against REF's fluxplace code in a temporary git
  worktree (the current `corpus.py` is overlaid, so REF need not contain this command), then removes it.
- `--repeat N` runs the fast tier N times and lists metrics that differ between runs (exit 1 if any).

## Fast tier steps

| step | command | metrics |
|---|---|---|
| lint | `lint --json` | `lint.<LEVEL>`, `lint.<LEVEL>.<code>` |
| review | `review --no-api --json` | `review.verdict`, `review.<LEVEL>.<code>` |
| route-ready | `route-ready --json` | `rr.verdict`, `rr.<LEVEL>.<code>`, crossings, ratsnest |
| collisions | `route-ready --collisions-only --json` | `col.COLLISION`, `col.kind.<kind>` |
| plan-route | `plan-route --json` | `plan.verdict`, score, confidence, sealed, overflow, `plan.code.<code>` |
| schematic | `schematic` (only with spec) | `sch.verdict`, `sch.netlist`, nets, ERC total |
| fanout | `fanout --check-only` | `fanout.verdict`, required, escaped (`QUESTIONS` on exit 3) |
| pairs | `pairs --plan-only` | `pairs.complete`, `pairs.total`, `pairs.questions` |
| drc | `kicad-cli pcb drc` | `drc.errors`, `drc.warnings`, `drc.error.<type>`, `drc.unconnected`, `drc.parity` |
| pcb-metrics | pcbnew (in-process) | footprints, nets, tracks, vias, track length mm |
| fab, gerber-check | `fab --no-review`, `fab --check-only` | `fab.gerber` (only when `drc.unconnected` = 0) |

Exit codes 0, 1 (FAIL verdict), 3 (open questions) and 5 (DRC violations) are normal outcomes.
Any other exit is an `error.<step>` metric. A traceback is `crash.<step>` and is always a regression.

## Full tier

Fast steps plus `pipeline BOARD --workdir ... <pipeline_args>`, then DRC and copper metrics on the
newest `.kicad_pcb` the pipeline wrote, under `routed.*`.

## Regression rules

- any `crash.*`
- any `error.*` count up
- a verdict metric PASS/READY -> anything else (`*.verdict`, `sch.netlist`, `fab.gerber`)
- `drc.unconnected` up
- `drc.errors` up by more than `drc_noise`
- a FAIL-level review/route-ready code count up (`review.FAIL.<code>`, `rr.FAIL.<code>`)
- a step slower than `runtime_factor` x baseline and `runtime_floor_s` slower
- a board in the baseline missing from the current run

Codes absent from the baseline are reported under "new checks", not as regressions.
Exit 1 on any regression; 2 for a usage or manifest error.
