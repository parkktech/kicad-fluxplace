# `fp-check` - footprint geometry and the pad table

`fluxplace fp-check [--board B] [--lib DIR.pretty [--footprint NAME]] [--constraints C] [--json] [--top N] [--out-dir D]`

Measures every footprint with pcbnew's shape API (`SHAPE.Collide` bisected to the exact edge-to-edge distance, so rotated and rounded pads are exact). Exit 1 on any FAIL, and on any footprint pcbnew cannot open (`FP_UNCHECKED`: NOT CHECKED is not a pass). `--out-dir` writes `fp-check.txt` and `fp-check.json`; nothing is written otherwise.

| code | level | rule |
|---|---|---|
| `FP_PAD_CLEARANCE` | FAIL | copper edge-to-edge between two pads of one footprint, on a shared copper layer, below the floor. Floor: `[fab] min_clearance_mm`, else `[route] min_clearance_mm` / `clearance_mm`, else the board's minimum clearance, else 0.127 mm. Same pad number (merged copper, e.g. thermal segments) is exempt and reported as INFO. |
| `FP_PAD_HOLE_CLEARANCE` | FAIL | copper of a pad to the hole of another pad (NPTH or drill), below the board's hole clearance (else 0.25 mm). Same pad number exempt. |
| `FP_KEEPOUT_SELF` | FAIL | a rule area that forbids pads contains the footprint's own pad, or its own NPTH/drill. |
| `FP_PAD_OUTSIDE_COURTYARD` | WARN | a pad's outline is not inside the footprint's courtyard. |
| `FP_UNCHECKED` | WARN (FAIL in the CLI) | the board or footprint could not be loaded: geometry NOT CHECKED. |
| `FP_WAIVER_REFUSED` | INFO | a waiver names an FP_* or LANDPATTERN_* code; see below. |

Every message names the ref, footprint, pads, the measured value and the floor.

## Footprint defects are fixed, not waived

`[review] waive`, `--waive` and `route-ready` never waive an `FP_*` or `LANDPATTERN_*` finding: the finding stays, with the line "footprint defects are fixed in the footprint from the manufacturer's land pattern, not waived". The one exception is a spec `landpattern.allow` entry (see [parts-cite.md](parts-cite.md)): `{"code": "FP_PAD_CLEARANCE", "pads": ["7","8"], "source": "file.pdf#p23", "value_mm": 0.10}`. It is accepted (INFO) only when the cited file exists in the datasheets dir and `value_mm` matches the measured value within 0.03 mm.

## Where it runs

- `ready` and the pipeline's first stage: the parts gate runs fp-check on every project-drawn footprint (`[docs] project_libs`) and on every board footprint. A FAIL is a `package` blocker. `--allow-undocumented` does NOT bypass FP_* or LANDPATTERN_PAD_* failures.
- `route-ready` (and the pipeline's route gate): the board's footprints.
- `review`: the landpattern pad table only (`LANDPATTERN_PAD_*`).
