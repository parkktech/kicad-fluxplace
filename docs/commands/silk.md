# `silk`

### Silkscreen check and fit — `fluxplace silk BOARD [--constraints C] [--fix] [--out PATH | --in-place] [--side F|B|both] [--spec S] [--title TEXT] [--json]`

Report only without `--fix`. With `--fix` the reference texts are re-placed as a set, the board is written to
`--out` (default `<board>.silk.kicad_pcb`; the input only with `--in-place`), re-checked, and the report prints
`before -> after` per code. Exit 1 while a FAIL remains (a FAIL the fit cannot fix is always a footprint graphic or a
board text: the tool never edits those). The fit never moves a footprint, track, via, zone, board text or footprint
graphic; the only edits are a reference's position, angle, size/stroke, or (last resort) its layer to Fab.
Deterministic. Compact output: verdict, counts per code, worst 10, full report path (`--full`, `--top N`, `-q`).

Checked on F.SilkS/B.SilkS: footprint references, visible values, footprint user texts, board texts (real rotated
text box incl. stroke) and footprint silkscreen graphics.

| code | level | meaning |
|---|---|---|
| `SILK_OFF_BOARD` | FAIL | outside the real Edge.Cuts polygon (arcs, cut-outs) or within `edge_clearance_mm` of it |
| `SILK_OVER_PAD` | FAIL | over a pad's solder-mask opening on that side (THT: both) + `pad_clearance_mm` |
| `SILK_OVER_HOLE` | FAIL | over an NPTH / mounting hole / untented via hole + `hole_clearance_mm` |
| `SILK_OVERLAP` | WARN | text over text, or over another footprint's silk graphics |
| `SILK_UNDER_PART` | WARN | a reference > `under_part_overlap` inside another part's courtyard, same side |
| `SILK_TOO_SMALL` | WARN | height < `min_height_mm` or stroke < `[fab] min_silk_width_mm` |
| `SILK_REF_HIDDEN` | INFO | reference not on silk; WARN for connectors, test points, switches, LEDs, polarised parts |
| `SILK_FAR_FROM_PART` | WARN | reference centre > `max_ref_distance_mm` from its courtyard edge |
| `SILK_VERSION_STALE` | FAIL | a board-level silk text carries a version (`V1.5`, `v1.5`, `Rev 1.5`) that is not the board's version; also when two board texts disagree with each other. Names found vs expected and the source |
| `SILK_TITLE_MISSING` | FAIL (WARN if `required = false`) | `[silk.title]` exists and no board text on its side(s) matches the template |
| `SILK_TITLE_NO_ROOM` | FAIL | `--fix` found no spot for the title at `min` 1.0 mm; reports the largest free rectangle (never shrinks below the minimum, never hidden) |
| `SILK_MISSING_MARK` | INFO | best effort: polarised part / IC with no silk graphics (no pin-1 / polarity mark) |

The title text is a board-level text, so the OVER_PAD / OVERLAP / UNDER_PART / OFF_BOARD findings on it read
`title text '...'` and carry the ref `TITLE` (waive with `SILK_OVER_PAD:TITLE`).

Fit order: worst and densest first; candidates are the 4 sides and 4 corners of the part's own courtyard (stepped
along the edge), rotation 0 or 90 only (always readable from that side; B.SilkS mirrored), the current size then
smaller to `min_height_mm`, then a ring out to `max_ref_distance_mm`. Hard violations (OFF_BOARD, OVER_PAD,
OVER_HOLE, OVERLAP, UNDER_PART) must be zero; then closest, orientation like the neighbours, larger size. An
occupancy index is updated as each text commits; a second pass retries the failures; a reference that still has no
legal spot is hidden on silk (it stays on Fab) and listed as `HIDDEN <ref>`.

    [silk]                       # all optional; defaults shown
    enabled = true               # false: review and the write hooks skip silk
    edge_clearance_mm = 0.25
    pad_clearance_mm = 0.15
    hole_clearance_mm = 0.15
    min_height_mm = 0.8
    max_ref_distance_mm = 4.0
    under_part_overlap = 0.5     # share of the text box inside another courtyard
    text_clearance_mm = 0.0
    ref_gap_mm = 0.2             # fit: courtyard edge to text
    size_step_mm = 0.1
    auto_fit = true              # write hooks re-fit the moved parts' references
    check_graphics = true
    # [silk.title] below

#### Title / version text

    [silk.title]                       # the table's presence turns the rule on
    text = "{company}  V{version}"     # template (default shown)
    company = "Parkktech.com"
    version = "1.6"                    # optional; else resolved as below
    side = "F"                         # F | B | both
    height_mm = 1.5                    # stepped down to 1.0 (the minimum) when it does not fit
    required = true                    # SILK_TITLE_MISSING is FAIL (else WARN)

Version, first hit wins: `[silk.title] version`; `[project] version` in the constraints; the spec JSON's
`version` / `revision` (`--spec`, also under `board`); the board's title-block revision (`1.6`, `V1.6`, `rev 1.6`);
a `-vNN` / `_vN.N` suffix of the board file name (`utv-comms-v16` = 1.6, a bare two-digit number reads as major.minor).
With no source the stale check is silent. `{version}` needs one: without it `--fix` writes nothing and says so.

`silk --fix` OWNS the title when `[silk.title]` exists or `--title "TEXT"` is given (a literal; a `V1.5` inside it is
matched as any version): it rewrites the content from the template and places it AFTER the references, so it sees
their final places. Search: 0.5 mm grid, 0 and 90 degrees (B mirrored), the position with the largest clearance
margin to pads' mask openings, holes/vias, other texts and silk graphics, courtyards (never under a part body) and
the outline (>= `edge_clearance_mm`); larger height first; deterministic. A duplicate title on the same side is
removed. Other board texts are never touched. The output lists `TITLE F: 'text' at (x, y) height H, clearance margin M`.

Revision copies: `pipeline` start and every write hook print the `SILK_VERSION_STALE` line when the board's text
disagrees with its version; `review` reports it and `SILK_TITLE_MISSING` as FAIL (waivable).

Watching: `review` reports every `SILK_*` finding (waive with `[review] waive = ["SILK_OVER_PAD:^C12 "]`).
Every command that moves footprints and writes a board (`place`, `clusters --apply`, `templates`, `anchor-check
--apply`, flips, `fit-sheet`, stall-loop moves; so the pipeline stages too) prints one silk summary line and, with
`auto_fit = true`, re-fits the moved parts' references and any text they now collide with. The `pipeline` has a
`silk` stage after `tune`, before `review`. Needs pcbnew (run under `/usr/bin/python3`, `FLUXPLACE_KICAD_PYTHON` to
override); without it the hooks print `silk: not run` and the write stands.
