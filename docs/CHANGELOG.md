# Change log (moved from the README)

## What changed 2026-11-10 — spec-net, keep-out, lock and policy gates

Four gates added to `review`, each a defect the utv-comms V1.5 board carried
with DRC 0/0 and every earlier gate green. All four run on the pure `facts`
dict (`tests/test_gates.py`, no pcbnew).

| Code | What it catches | Config |
|---|---|---|
| `NET_SINGLE_PAD` (FAIL) | A named net that lands on one pad. It has no ratsnest edge, so DRC reports 0 unconnected for a pin that was never wired (V1.5 `IMU_INT1`; still true of `ETH_nLED2/3`). | none |
| `SPEC_NET_SPLIT` (FAIL), `SPEC_PIN_UNRESOLVED` (WARN) | Every spec net compared to the copper pad by pad, through each component's `pinmap`. | `--spec` |
| `KEEPOUT_COMPONENT` (FAIL) | A part inside a `[[keepout]]` region on the forbidden side, or taller than `max_height_mm` (height from the distributor's "Height - Seated (Max)"; unknown height fails). V1.5 had 27 parts under the CM5 in a 1.5 mm mezzanine gap. | `[[keepout]] name, side, rect \| around, margin_mm, allow, max_height_mm` |
| `LOCK_MOVED` / `LOCK_MISSING` (FAIL) | A footprint the enclosure fixes (bolt pattern, panel connector) is not at its locked position. `fluxplace lock --board B --refs 'MH*,MK*'` prints the block from an existing board. | `[lock] tol_mm`, `[lock.at] REF = [x, y, "F"]` |
| `POLICY_DIELECTRIC` (FAIL), `POLICY_NOT_AEC` / `POLICY_PASSIVE_SIZE` (WARN) | House part-selection rules from distributor parameters: allowed MLCC dielectrics, automotive (AEC-Q) grade, largest default chip size. Exceptions go in `[review] waive` with the reason. | `[policy] grade, mlcc_dielectrics, passive_max, exempt` |

`facts_from_board()` gains `pos`, `side` and `bbox` per footprint.

## What changed 2026-09-06 → 2026-09-08

Three days closing out the utv-comms-bridge V1.5 external review, grouped
by command (commit hashes from `git log 61eccd9..HEAD`).

**`verify-models`** — new placement/verification rules, each found against a
real defect that had passed silently:

- **posture** (`posture_gap`, `400c4d5`) — flags a body standing on edge
  (Q1 at commit 7cb0f77: XY collapsed to 6.24×1.50mm against a
  5.99×5.00mm Fab outline). Requires a genuine shortfall recoverable by a
  90° swap, not just any Fab/XY mismatch — a tighter two-sided match tried
  first false-flagged every SOT-23/0805/RJ45/JST part whose leads
  legitimately extend past their drawn Fab body.
- **upside-down** (`buried_mass_gap`, `c5f5c52`) — flags T1/T2 (92% of the
  model's points sit >0.5mm below the mount plane after its offset).
  Catches only this one signature — see `NEXT.md` for the flat-but-inverted
  case it misses (Q1, a different defect) and why.
- **seat gap** (`seat_gap`, `400c4d5`) — the multi-axis-rotation skip that
  hid J10/J11's DF40C receptacles floating 0.75–0.8mm into the CM5 module
  is closed: any model with rotation on more than one axis is now
  Z-extent-checked via a full X→Y→Z rotation, not just the Z-only math the
  XY pin-fit check trusts.
- **library transform** (`library_transform_diff`, `c5f5c52`) — flags J5
  (board references the stock KiCad USB-C STEP but rotate.z is 180 vs the
  library footprint's 0); WARN when the model file still matches the
  library's, INFO when it's also a deliberately swapped-in body.
  `verify-models --constraints` feeds `[docs] project_libs` into the
  search. `--fix` never touches a model whose transform already equals
  the library's.
- **mated overlay** (`overlay_registration_gap`, `c5f5c52`) — flags the
  CM5 overlay on J10 (3beae58, D70ad) missing its mating registration to
  J10/J11; silent once corrected. Tuned against that one before/after
  pair — see `NEXT.md`.
- **board→footprint transform fixed** (`88d85f4`) — `_th_holes`'s
  board→local un-rotation used the same matrix KiCad's placement uses
  instead of its inverse, so `verify-models --fix` "solved" J5 into a
  180°-wrong rotation that fit a target which was itself rotated 180° from
  reality. Fixed in `_board_to_fp`; added an SMD-lead check (`_smd_pads`, a
  z∈[-0.05,0.05mm] lead-flange slice against the real SMD pads) as a
  second signal, and `solve_transform` now treats the on-board transform
  as a prior — a non-identity rotation is accepted only when it clears
  identity's own TH fit by a solid margin *and* doesn't worsen the
  SMD-lead fit.
- **MODEL_FILE_UNREADABLE** (`c5f5c52`) — FAIL when a model file resolves
  but doesn't parse into geometry (STEP under 50 points, or a WRL with no
  Shape/IndexedFaceSet), wired into `review.check_models`.

**`review`** — `is_mechanical()` now also exempts a footprint/reference
matching the MountingHole/Fiducial/TestPoint/Logo conventions, so a grounded
mounting hole (MH1-4/MK1-4) no longer FAILs `MODEL_MISSING` (`400c4d5`).
`check_models` messages reformatted `"{ref} 3D model..."` — the old
`"{ref}: 3D model..."` ate the space a `CODE:^REF ` waiver regex needs, so
`MODEL_STANDIN:^ANT1 ` never matched ANT1's antenna stand-in (`400c4d5`).
New **`VIA_TYPE`** FAIL (`0fbff9e`): any blind/buried/micro via found on the
board, unless the matching `[fab] blind_vias`/`buried_vias`/`microvias`
constraint is `true` — wired into both `review` and the gate `fab`/`deliver`
call, same stop-ship contract as every other FAIL. `facts_from_board()`
gains `facts["vias"]`/`facts["via_type_counts"]` from KiCad 10's
`VIATYPE_*` enum. Root cause closed in the same commit: `finish.route_nets()`
had copied the via type straight off the freerouting session, so a dense
net's blind/buried via choice reached the board; it now always creates
`VIATYPE_THROUGH` F.Cu–B.Cu vias regardless of what the session chose.

**`repair --patch`** (`3343fef`) — the maze router's new track ends could
land inside an existing via's copper but off its exact centre (a grid-cell
quantization artefact), so KiCad's own `track_not_centered_on_via` DRC
reverted an otherwise-correct route every time. `apply_path` now snaps
every track end onto any existing via's true centre before writing
geometry. Found closing the J4 SMA corridor on the UTV board.

**`fab`/`deliver`** (`098e8bf`) — `fab.emit()` wrote into `gerbers/`,
`drill/` and `place/` with plain `makedirs(exist_ok=True)`, so a re-cut
into the same `--out` never removed a prior cut's files; a blind-via cut's
stale drill files survived a through-via re-cut and `deliver()`'s
`shutil.copytree` carried them into the PCBWay gerber zip. `_reset_dir()`
now wipes those three subdirs before every export; `MANIFEST.txt` gains
machine-readable `FILE : <section>/<name>` lines for every file `emit()`
actually wrote, and `deliver()` zips exactly those manifest-listed files
instead of copying the directories wholesale.

## What changed 2026-09-08 — fab-capability gate

**`review`** — PCBWay's gerber-upload audit rejected the utv-comms V1.5
board ("The minimum trace width should be no less than 0.1mm") over two
0.09 mm GND stubs that `review`, DRC and lint had all passed clean, because
every one of them judged the copper against the board's OWN design-rule
minimum (0.088 mm) — looser than the fab that would actually build it —
never against the fab's real floor. `[fab]` gains `min_track_mm` (0.1),
`min_clearance_mm` (0.1), `min_drill_mm` (0.2), `min_annular_mm` (0.1),
`min_via_dia_mm` (0.4), `min_silk_width_mm` (0.15) and a `profile` label
(e.g. `"pcbway-6L"`) alongside the existing `blind_vias`/`buried_vias`/
`microvias`; `constraints.fab_profile()` fills defaults for whatever the
engineer didn't state. New `check_fab_capability()`: FAIL **`FAB_MIN_TRACK`**
for every track/arc segment narrower than `min_track_mm` (net, layer,
width, position — 10 shown + count); FAIL **`FAB_MIN_DRILL`** /
**`FAB_MIN_VIA`** / **`FAB_MIN_ANNULAR`** for vias and PTH pads under the
drill/diameter/annular-ring floors; WARN **`FAB_DESIGN_RULE_BELOW_PROFILE`**
when the board's own Board Setup minimums (`m_TrackMinWidth`,
`m_ViasMinSize`, `m_MinThroughDrill`, `m_MinClearance`) are looser than the
profile — the root-cause gap that let a repair pass draw the 0.09 mm stub
in the first place — INFO `FAB_DESIGN_RULE_OK` otherwise. Only grades when
`[fab]` is loaded; no constraints, no opinion. `facts_from_board()` gains
`facts["track_geom"]` (every track AND arc segment, net/layer/width/x/y —
arcs were previously invisible to any geometry check), `facts["pth_pads"]`,
`facts["design_min"]`, and vias gain `dia_mm`/`drill_mm`.

**`repair` / `patch` / `bridge` / `drc-fix`** — every place copper gets
created or narrowed now clamps up to the `[fab]` floor when constraints are
loaded, instead of writing whatever the geometry solver asked for:
`repair.rf_widths` (a target-impedance solve on a thin layer), `repair.stitch`,
`repair.stitch_islands`, `repair.bridge`, `repair._gnd_via`/`remap_pins`,
`patch.apply_path`/`patch_board` (every track/via/drill it writes, including
the `net_widths` map), and `drcfix.fix`'s RF clearance-neck fallback — the
exact mechanism that produced the original 0.09 mm GND stubs. Each clamp
logs when it raises a width. `cli.py`'s `repair`, `patch` and (new
`--constraints`) `drc-fix` subcommands load `[fab]` once and thread it
through.

## What changed on 2026-09-04

Driven by the utv-comms V1.5 IMU add and a transformer footprint that turned out
to be wrong, all on `main`:

- **`models --fetch` / `--check`** — `review`'s `check_models` FAILs any
  electrical footprint with no 3D model or a model path that doesn't resolve;
  `--check` lists those with no network call, and `--fetch` closes the hole by
  looking up each ref's MPN on LCSC (`fluxplace.lcsc.lookup`) and pulling the
  body from EasyEDA via `easyeda2kicad --3d` — a second model source for parts
  whose DigiKey CAD-media link is login-walled. Attached at rotation/offset
  zero with a provenance line recorded in `model_sources.json`; every failure
  (no MPN, no LCSC match, exporter miss) is soft and reported, never faked.
- **`repair --bridge REF:PAD`** — a multi-layer maze router for the one pad the
  last-mile patcher and freerouting both give up on. Dijkstra on a
  clearance-rasterised grid per copper layer, layer changes only where a
  through via clears every layer, final snap into the pad clearance-checked by
  exact shape, DRC-guarded, zones refilled on accept.
- **Land-pattern citations** — `landpattern` on a spec component names the
  drawing page and the pitch / pad / rows / pins read from it; `review`
  measures the footprint against those numbers (FAIL on 0.03 mm), FAILs any
  footprint from `[docs] project_libs` without a citation, and WARNs when the
  cited page is a text-less drawing ("read by eye — verify before ordering").
  Born from a 24-pin transformer drawn at 1.27 mm pitch for a 0.99 mm part.
- **Datasheet temperature** — `partdocs.datasheet_temp` reads the operating
  range from the PDF text; `review` prefers it over the distributor field.
  DigiKey said +85 °C for a relay whose sheet says +70 °C.
- **LCSC lookup** retries without punctuation (JLCPCB's search is literal
  about hyphens); **`finish --timeout`**; the patcher's launder worker takes
  absolute paths; LGA packages classify; split-paddle footprints report the
  modal pad size.
- **3D bodies are a gate and a fetch** — `review` FAILs any electrical
  footprint with no model (`MODEL_MISSING`), a model path that resolves to no
  file (`MODEL_FILE_MISSING`), or a file named as a stand-in
  (`MODEL_STANDIN`); mounting holes are exempt. `models --check` lists the
  holes, `models --fetch` fills them from EasyEDA by LCSC code (easyeda2kicad,
  optional `[models]` extra) and records provenance. Lesson from V1.5: a board
  that renders with holes had passed DRC, review and packaging; the RJ45 pointed
  at a library file that did not exist, the transformer had no body at all.
  pcbnew pitfall: `fp.Models()` iterates by value — write back by index.
- **Working rule** used on that project: the top-level model orchestrates,
  reviews reports and commits; every mechanical pipeline step runs in a
  Sonnet subagent from an exact recipe. Long jobs are watched with a
  grep-until loop on their log, never with `pgrep` on their own command line.
