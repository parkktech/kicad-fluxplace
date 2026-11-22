# ROUTER-C1: in-memory kernel + differential tester (plan section C, phase C1)

Worktree /var/kicad/fluxplace-wt/router (branch `router`). Plan: `docs/PLAN-W14-FINISH-AND-AUTOMATION.md` section C.
One step = one agent run = one commit (`ROUTER-C1-K<n>: ...`). Do the steps in order; do not start a step whose
predecessor's tests fail. Each step is at most about 400 new lines. Stop and report instead of exceeding 500.
Board-agnostic: no board ref, net name or board constant in code. No new user command or flag: the kernel is a provider
selected by `[route] engine` (constraints TOML) or the internal switch `FLUXPLACE_KERNEL`.
Line numbers are approximate: re-find by name. G = `fluxplace/hrgeom.py`, H = `fluxplace/handroute.py`.

## 1. Decisions

| Topic | Decision | Why |
|---|---|---|
| Language | Rust, crate `fpkernel` in `kernel/`, built as `cdylib` + `rlib` | native speed; `cargo test` runs the same code |
| Binding | C ABI + `ctypes`, JSON requests and replies, raw buffers for rasters. Not pyo3 | no maturin, no venv, no Python ABI coupling (the ledger's KiCad 11 risk), nothing to install; the Python calls are coarse (one per check or per search), so JSON costs microseconds against a 50 ms budget; the same request lines replay in `cargo test` and in the differential tester |
| Crates | `serde`, `serde_json` only | spatial index, s-expression parser and geometry are small and written here |
| Units | integer nanometres (`i64`) inside; millimetres (`f64`) at the API (`nm = round(mm * 1e6)`) | KiCad compares integer nm; the 0.006 mm replay miss came from float mm |
| Board I/O | s-expression text only. pcbnew is never imported by `fluxplace/kernel.py` or `fluxplace/kmodel.py` at module level | plan: kernel must not depend on SWIG |
| Judge | kicad-cli DRC with refill stays the judge of every written edit in C1. Removing the per-edit judge is the first item of C2, after Fable reviews the K14 numbers | 0 false accepts must be measured before it is trusted |

Installs: nothing global, no pip. `cargo build` downloads `serde` and `serde_json` into `~/.cargo/registry` (network once).
`PATH` needs `~/.cargo/bin`. Python tests use `/usr/bin/python3 -m pytest` (9.2.0 present). pcbnew is used only by tests
that compare against the legacy model.

## 2. File layout

```
kernel/Cargo.toml  Cargo.lock  build.sh          build.sh: cargo build --release; cp target/release/libfpkernel.so ../fluxplace/_fpkernel.so
kernel/src/lib.rs      FFI + JSON dispatch        kernel/src/rules.rs   project rules, net classes
kernel/src/sexpr.rs    parser                     kernel/src/dru.rs     .kicad_dru subset
kernel/src/geom.rs     shapes, distances          kernel/src/clear.rs   clearance engine, violations
kernel/src/board.rs    items, nets, layers        kernel/src/tx.rs      transaction log, add/remove, emit
kernel/src/load.rs     tracks, vias, edges        kernel/src/conn.rs    union-find connectivity
kernel/src/load_fp.rs  footprints, pads           kernel/src/path.rs    check_path, raster
kernel/src/load_zone.rs zones, fills              kernel/src/pour.rs    pour raster model
kernel/src/index.rs    grid index
fluxplace/kernel.py    ctypes wrapper (class Kernel)      fluxplace/kmodel.py   KModel (Model surface), engine switch
tests/test_kernel_<topic>.py                              tests/kdiff.py, tests/test_kdiff.py, tests/kdiff_cases/*.json
docs/corpus/kdiff.jsonl   one line per tester run (committed)
```

`.gitignore` gets `kernel/target/` and `fluxplace/_fpkernel.so`. Rust unit tests live in each file under `#[cfg(test)]`.

## 3. C ABI and requests

```
fpk_open(path: *const c_char, opts_json: *const c_char) -> *mut Handle     null on failure
fpk_call(h, req_json: *const c_char) -> *mut c_char                         reply JSON; free with fpk_free_str
fpk_raster(h, req_json, buf: *mut f32, len: usize) -> i64                   cells written, -1 on failure
fpk_close(h);  fpk_free_str(p);  fpk_last_error() -> *const c_char          thread-local message
```

No panic may cross the boundary: every entry point wraps its body in `std::panic::catch_unwind` and returns the error.
Every reply is `{"ok": true, ...}` or `{"ok": false, "error": "..."}`. `opts_json`: `{"project": path or null, "dru": path or null,
"floors": {...} or null}`; by default the `.kicad_pro` and `.kicad_dru` beside the board are used.

| op | request fields | reply |
|---|---|---|
| `info` | | `layers` (copper, stack order), `nets`, `items`, `file_version`, `load_ms`, `unsupported: [{what, where, effect}]`, `rules: {floors, classes, source, dru: {rules, used, unsupported: [..], coverage: "none|full|partial"}}`, `fill_stale` |
| `set_floors` | `clr, track, via_dia, via_drill` (mm) | floors after `max` with the project values |
| `net_rule` | `net` | `class, width, clearance, via_dia, via_drill, dp_width, dp_gap` (never below the floors) |
| `items` | optional `bbox, layer, net, kinds, ids` | item dicts: `id, uuid, kind, net, layers, shapes, clr, clr_hole, ref, locked, extra` |
| `need` | `a, b, layer` (ids) | `mm, rule` (the rule name that decided) |
| `begin` / `commit` / `rollback` | | `depth` (transactions nest; rollback undoes to the matching `begin`) |
| `add` | `items: [{kind: "seg", net, layer, a, b, width, locked?, uuid?} or {kind: "arc", net, layer, a, mid, b, width} or {kind: "via", net, at, dia, drill, layers?: [top, bottom]}]` | `ids, uuids` |
| `remove` | `ids` or `uuids` | `removed` |
| `check` | optional `ids` (default: everything added or removed in the open transaction), `allow_opens: false` | `verdict: "accept|refuse|defer"`, `violations: [{type, a, b, layer, need, have, at, rule}]`, `opens: {net: [before, after]}`, `pour: [{zone, layer, state: "intact|split|joined|uncertain"}]`, `warnings`, `ms` |
| `opens` | optional `net` | per net: count, and the islands that hold pads |
| `islands` | `net` | `[{id, items: [ids], zones: [[zone, layer]]}]` |
| `check_path` | `net, prims: [["seg", layer, [x, y], [x, y]] or ["via", [x, y]]], width, via: [dia, drill], skip_ids, any_angle` | the dict of `H.probe_prims` (same keys) |
| `emit` | `ids` | `blocks`: s-expression text of each item, KiCad 10 form (`(net "NAME")`) |
| `stats` | | counters: calls, checks, p50/p99 check ms, index sizes |

`fpk_raster` request: `{bbox, pitch, layer, net, width, kind: "track|via|hole"}`; it fills the slack field that `H._free_fields`
(~270) computes today (clearance slack in mm per cell, row-major, same grid as `G.grid`).

Verdict: `refuse` when any violation has error severity, or opens rose on any net and `allow_opens` is false. `defer` when
there is no violation but a pour result is `split` or `uncertain` (the judge decides). `accept` otherwise.

## 4. Data model

- `Pt {x: i64, y: i64}` nm, y down as in the file.
- `Shape {core: Core, r: i64}` with `Core = Point(p) | Seg(a, b) | Poly(Vec<Pt>)`. The copper is the core inflated by `r`.
  Disc = Point + r. Track and oval pad = Seg + half width. Rectangle = Poly + 0. Roundrect = the rectangle shrunk by the
  corner radius + that radius (exact). Arc track = polyline of Segs with chord error at most 1000 nm, `r` raised by the error (conservative).
  Gap of two shapes = core distance - r1 - r2. An item has `Vec<Shape>` (custom pads have several).
- `Item {id: u32, uuid, kind: Pad|Track|Arc|Via|Hole|Fill|Keepout|Edge|Graphic, net: u32 (0 = none), layers: u64 bitmask over the copper stack, shapes, bbox, clr_local: Option<i64>, class: u16, ref_: String ("U3.11"), locked, alive, extra}`.
  A drilled pad or via is two items sharing the uuid: the copper item and a `Hole` on every copper layer (as G ~343-375 does).
  Vias cover their layer span (through vias: all layers). `extra` carries what the legacy `Ob.extra` carries (pos, shape,
  size, hole, dia, drill, npth, via, width, arc, no_tracks, no_vias, no_pads).
- `Zone {uuid, net, layers, name, priority, outline, clearance, min_thickness, connect, thermal_gap, thermal_width, island_mode, hatch, fills: per layer Vec<polygon>}`.
- `Board {layers, nets (name <-> id), items, zones, index, rules, conn, tx}`.
- Index (`index.rs`): per copper layer a hash grid with 1 mm cells, item ids by bbox; `query(bbox, layer_mask)`; insert and remove.

## 5. Loader scope (`sexpr.rs`, `load*.rs`)

File versions 20221018 (KiCad 7, `(net 5 "NAME")`) to 20260206 (KiCad 10, `(net "NAME")`); both net forms are read.

| Read | Notes |
|---|---|
| `layers` | copper layers in stack order (F.Cu, inner by number, B.Cu) |
| `segment`, `arc`, `via` | via `type` blind/micro and `layers` give the span; `locked`; `free` |
| `footprint` | `at` + rotation, `layer`, `locked`, reference; pad position = footprint `at` + rotate(pad `at`); the pad angle in the file is absolute. Use the convention of `boardfile._rot` (~112) and check it against `boardfile.facts_from_file` |
| `pad` | types smd, thru_hole, np_thru_hole, connect; shapes circle, oval, rect, roundrect (+ `chamfer_ratio`, `chamfer`), trapezoid, custom (`primitives` + anchor); `drill` round, oval and `offset`; `layers` with `*.Cu` and `F&B.Cu`; `net`; `clearance`; `zone_connect` |
| footprint children | `zone` (keepouts in footprints), `fp_line/fp_arc/fp_circle/fp_rect/fp_poly` on Edge.Cuts or on copper |
| board graphics | `gr_line/gr_arc/gr_circle/gr_rect/gr_poly/gr_curve` on Edge.Cuts -> `Edge`; on copper -> `Graphic` (net 0); `gr_text` on copper -> its bounding box (conservative) |
| `zone` | `net`, `layer`/`layers`, `name`, `priority`, `connect_pads` (+ clearance), `min_thickness`, `keepout` flags, `fill` (thermal gap, bridge width, island mode, hatch), `polygon`, `filled_polygon` per layer |
| ignored | silk, fab, courtyard, mask, paste, text off copper, groups, generated items (their member tracks are ordinary items), properties, 3D models |

Anything not understood on a copper layer (a pad `padstack` with per-layer shapes, a hatched zone, an unknown pad shape,
a `gr_curve` that cannot be flattened) is listed in `info.unsupported` and replaced by the conservative shape (the union
bounding polygon over all layers). It is never dropped silently.

Stale fills: the file's `filled_polygon` data is used as is. `fill_stale` is true when any foreign-net track, via or pad
overlaps a stored fill on its layer. The Python side (`KModel`, K15) then refills a temp copy once with
`kicad-cli pcb drc --refill-zones --save-board` (about 10 s on the 10 MB W14 board) and loads that. Measured 2026-11-22 on
W14 r5: DRC without refill reports 7 clearance errors that refill removes, so stored fills cannot be assumed current.

## 6. Clearance rules

Resolution of the required gap between items `a` and `b` on layer `L` (`clear.rs: need`), in this order:

1. Item value: pad `clearance`, else its footprint's `clearance`, else the zone's `clearance` (for fills), else the net's effective class clearance.
2. Pair value = `max(value(a), value(b))`.
3. `.kicad_dru` rules of constraint `clearance` whose `layer` and `condition` match the pair (either order): the **last**
   matching rule replaces the pair value (KiCad: later rules win).
4. Floors: result = `max(result, project min_clearance, floors.clr)` (`set_floors` carries the constraints TOML and fab
   profile floors, exactly what `G.Model._apply_constraints` ~287 does today; that code stays in Python).

Other constraints: hole to foreign copper = `min_hole_clearance` (dru `hole_clearance`); hole to hole = `min_hole_to_hole`
(dru `hole_to_hole`); copper to `Edge` = `min_copper_edge_clearance` (dru `edge_clearance`); track width >= `max(min_track_width, floors.track)`
(dru `track_width`); via diameter, drill and annular ring against `min_via_diameter`, `min_through_hole_diameter`,
`min_via_annular_width` and the floors (dru `via_diameter`, `hole_size`, `annular_width`); keepouts by their flags.

Sources:

- `.kicad_pro`: `board.design_settings.rules.*` (the eight `min_*` keys above); `net_settings.classes[]` (`name, priority,
  clearance, track_width, via_diameter, via_drill, diff_pair_width, diff_pair_gap`); `net_settings.netclass_patterns[]`
  (`{netclass, pattern}`, wildcards `*` and `?`; a pattern that is not a plain wildcard is tried as an anchored regular
  expression, and if it does not compile it is listed in `unsupported`); `net_settings.netclass_assignments` (net -> list of classes, may be null).
  Effective class of a net: all matching classes ordered by `priority` (smaller wins), `Default` last; each parameter comes
  from the first class in that order that sets it (> 0).
- **Fallback** when there is no `.kicad_pro`: min_clearance 0, min_track 0, min_via 0.5, min_drill 0.3, annular 0.1,
  hole_clearance 0.25, hole_to_hole 0.25, edge 0.5; class Default: clearance 0.2, track 0.2, via 0.6 / 0.3 (mm).
  K7's test confirms these numbers against pcbnew on a board copied without its project; if pcbnew disagrees, pcbnew wins
  and the constants are corrected. `info.rules.source` says `project` or `fallback` (today's `H._model` warning stays).
- `.kicad_dru` subset: `(rule NAME (layer L)? (severity S)? (condition "EXPR")? (constraint TYPE (min V)? (opt V)? (max V)?)+)`,
  values with units mm, mil, in, um. `layer` is a layer name, `outer` or `inner`. `severity ignore` drops the rule.
  Condition grammar: `||`, `&&`, `!`, parentheses, and the atoms `X.NetClass ==|!= 'c'`, `X.NetName ==|!= 'n'` (with `*` wildcard),
  `X.Type ==|!= 'Track|Via|Pad|Zone|Graphic'`, `X.Layer == 'L'`, `X.hasNetclass('c')`, `X.inDiffPair('n')`,
  `A.isCoupledDiffPair()`, `X.isPlated()`, `X.Pad_Type == '...'`, `X.Via_Type == '...'`, with X = A or B.
- **Fallback for everything else** (any other function such as `insideArea`, `insideCourtyard`, `enclosedByArea`,
  `memberOfGroup`, any other property, any constraint type not listed above that restricts copper, any parse error): the
  rule is `unsupported`. Its unknown atoms evaluate to "may match". A may-match clearance-like rule can only **raise** the
  required value (`need = max(need, rule.min)`), never lower it. Coverage becomes `partial`, the rule is listed with its
  line number, and `KModel` prints one warning line. A partly covered board is reported, not silently routed (plan, Risks).

The exact precedence in steps 1-3, KiCad's DRC epsilon, and the type strings of the report are **measured, not assumed**:
K13 builds a calibration board with one deliberate case per rule and adjusts the kernel until it agrees with kicad-cli.
Epsilon policy: the kernel refuses at `have < need` (no slack). KiCad tolerates a fraction of a micrometre; that band is a
known, counted false refusal (`band` in the tester), never a false accept.

Not modelled in C1 (the judge sees them; the tester counts them as `outside_model`): silk, courtyard, solder mask, paste,
teardrops, length and skew, thermal spoke count, zone-to-zone clearance, creepage, text.

## 7. Connectivity (`conn.rs`) and transactions (`tx.rs`)

- One union-find per net over that net's copper items (pad, track, arc, via) and its fill islands. Union by rank, **no
  path compression**, so every `union` is one parent write and one rank write, both pushed on the transaction log.
- Two items join when they share a layer and their copper touches under the legacy rule: port `G._touch` (~544) and
  `G._anchors` (~531) exactly (track ends are the anchors; a fill joins an item whose anchor lies inside it). The legacy
  tolerance of 0.001 mm becomes 1000 nm.
- `opens(net)` = number of components that hold at least one pad, minus one. Port the island rules of `G.Model.islands`
  (~465); whether KiCad counts pad-less clusters is a K13 calibration case.
- Log entries: `AddItem(id)`, `RemoveItem(id)` (the item stays in the table with `alive = false`), `Parent(net, i, old)`,
  `Rank(net, i, old)`, `IndexIns/IndexDel`, `NetDirty(net)`, `RasterCells(zone, layer, saved words)`. `begin` pushes the
  log length; `rollback` replays the log backwards to that mark; `commit` pops the mark (the entries stay for an outer
  transaction; at depth 0 the log is cleared).
- Removal: union-find cannot split, so `remove` marks the net dirty and the net's structure is rebuilt from the index on
  the next query (the old arrays go on the log for rollback). Nets are small; the largest net of the W14 board must
  rebuild in under 20 ms (test).
- New items get a random v4 uuid unless one is passed. `emit` writes them in the file's own form.

## 8. Pour model scope for C1 (`pour.rs`)

An estimate, conservative by construction; KiCad's refill at a checkpoint decides.

- Per (zone, layer): a bit raster at 0.05 mm of the stored fill, **eroded by `min_thickness / 2`** (a path in this raster
  is a neck KiCad would keep). Built lazily for zones a check touches.
- Adding foreign copper inside a zone clears the cells within `zone clearance (or need, if larger) + min_thickness / 2`
  of the new shape (cells saved on the log). Foreign copper against a fill is never a clearance violation (the refill
  makes room); only the cut matters.
- After a cut, the components of the raster are labelled inside the zone's bounding box. `intact`: every item that was
  attached to one fill island (its anchor cell) is still in one component. `split`: not. `joined`: an added own-net item
  has its anchor in the eroded raster and so joins the island. `uncertain`: hatched zone, a zone without stored fill, an
  anchor that is inside the stored fill but outside the eroded raster, or a raster above 16 M cells.
- Connectivity uses `intact` and `joined` only. `split` and `uncertain` give verdict `defer`, and the opens of that net
  are reported as a range.
- Out of scope for C1: computing a new fill polygon, thermal relief spokes, island removal modes, priorities between
  overlapping zones (the stored fill already reflects them), hatch patterns, teardrops. Those arrive with C4 (rails and pours).

## 9. Differential tester (`tests/kdiff.py`, dev script; detached for full runs)

Purpose: the exit benchmark of C1. Oracle = `kicad-cli pcb drc --format json --severity-all --refill-zones`.

1. **Base.** Copy the board and its sidecars to the work dir; refill and save the copy once (`--refill-zones --save-board`,
   never on the source); run the base report; load the copy in the kernel.
2. **Edits** (seeded `random.Random(seed)`; each edit is a list of `add` / `remove` items). Mix:
   30 % track segment from a random point on existing copper or a pad of a random net (length 0.2-5 mm, 45-degree
   directions, 10 % any angle, random copper layer, class width); 10 % via (class size) at a random point within 3 mm of
   copper of the net; 10 % two-segment path between two pads of one net; 10 % removal of an unlocked track or via;
   10 % nudge (remove a track or via, add it back moved by 0.01-0.3 mm); 20 % **near miss**: a segment or via placed so its
   gap to a chosen foreign item, hole or edge is `need + d`, d uniform in +-0.05 mm; 5 % **boundary**: the same with d from
   {-20, -5, -1, 0, +1, +5, +20} um; 5 % pour cut: a foreign segment across a fill. Sub-floor width or via size in 3 % of the adds.
   At least 30 % of the edits must end within 0.05 mm of a limit (near miss + boundary + the natural ones); the report prints the share.
3. **Kernel verdict** per edit, alone against the base: `begin`, apply, `check`, `rollback`. Record verdict, violations, ms.
4. **Batches.** The oracle costs about 10 s per run, so edits are judged in batches of up to 25 written by text into one
   copy (`emit` blocks through `boardtext.append_blocks`, removals through `boardtext.remove_blocks`). Edits in one batch
   must be independent: bounding boxes at least 3 mm apart; the net sets (own net plus every net within reach) disjoint;
   at most one pour-touching edit per zone. Refused edits go into batches too (they are how false refusals are found).
5. **Attribution.** A violation in the batch report that is not in the base report belongs to the edit whose uuid appears
   in its `items`; a rise of unconnected items on a net belongs to the edit that owns that net. Oracle verdict per edit:
   `bad` when it owns a new violation of a modelled type or an opens rise; `good` otherwise. New violations of other types
   are counted as `outside_model` per type. A new violation no edit owns stops the run (batch not independent: a tester bug).
6. **Metrics and thresholds** (per board and total):

| metric | definition | threshold |
|---|---|---|
| false accept | kernel `accept`, oracle `bad` | **0** in >= 10 000 edits over 3 boards, >= 3 000 per board (0 of 10 000 bounds the rate below 0.03 % at 95 %) |
| false refusal | kernel `refuse`, oracle `good`, outside the boundary band | < 1 % of the oracle-good edits, per board |
| band | false refusals with `need - have` <= 1 um | reported, not counted |
| defer | kernel `defer` | <= 5 % of all edits; reported separately for pour-touching edits with the oracle's split |
| violation type parity | on refused edits the oracle confirms: share where the kernel's first type equals an oracle type | reported; < 90 % needs a ledger row |
| `check` latency | per edit | p99 < 50 ms |
| load | `fpk_open` on the largest board | < 2 s |
| coverage | `info.unsupported`, dru coverage | reported per board |

7. **Cases.** Every false accept and the first 20 false refusals of a run are saved as `tests/kdiff_cases/<board sha8>-<n>.json`
   (board path and sha, the edit, both verdicts, the oracle's violations). `tests/test_kdiff.py` replays each case whose
   board is present and whose sha matches (kernel verdict only, no kicad-cli) and skips the rest. A false accept is a
   release blocker: fix the kernel, keep the case.
8. **Output**: `<out>/kdiff.json` (full), one summary line appended to `docs/corpus/kdiff.jsonl`
   (`ts, tool, board, board_sha, seed, edits, mix, near_share, false_accepts, false_refusals, band, defers, outside_model, p50_ms, p99_ms, load_ms, coverage`), and a table on stdout.

Boards for the exit run: `/var/kicad/utv-comms-bridge/W14/stageF/hand/r5.kicad_pcb`,
`/usr/share/kicad/demos/cm5_minima/CM5_MINIMA_3.kicad_pcb`, `/usr/share/kicad/demos/stickhub/StickHub.kicad_pcb`
(paths on the command line, not in code). Cost: 10 000 edits / 25 per batch x 10 s is about 70 min of DRC; run 4 batches in
parallel, each in its own directory. Full runs are detached (`tests/kdiff.sh`, status in `<out>/kdiff.status.json`), never inside an agent.

## 10. Integration (behind the switch; legacy stays the default)

`kmodel.engine(cons)`: `FLUXPLACE_KERNEL=1|0` if set, else `[route] engine` = `"kernel"` or `"legacy"` (default `"legacy"`).
When the library file is missing, the answer is `legacy` with one stderr line naming `kernel/build.sh`.

| Today | In kernel mode | Step |
|---|---|---|
| `H._model` (~124) returns `G.Model` | returns `kmodel.KModel` | K15 |
| `G.Model` attributes used by callers: `cu, floors, floor_src, hole_clr, h2h, edge_clr, obs, zones, in_box, net_rule, need, foreign, islands, lid, b, pcbnew` | `KModel` has the same names. `obs` is a lazy list of `G.Ob` built from `items` (ids = uuids, as today); `zones` the same dicts; `in_box`, `net_rule`, `need`, `islands` call the kernel; `foreign` is the legacy function. `b`, `pcbnew`, `lid` load pcbnew on first use and count it in `KModel.swig_loads` | K15 |
| `G.Model(refill=True)` refills with pcbnew | `fill_stale` -> one kicad-cli refill of a temp copy (section 5) | K15 |
| `H.probe_prims` (~504) | `check_path`, same result keys | K15 |
| `H._free_fields` (~270), the data of `H.window` | `fpk_raster` | K15 |
| `H._edit` (~840): model, mutate, write, two DRCs | the kernel pre-check runs first; a refusal returns before pcbnew or kicad-cli is touched | K16 |
| `H._apply_prims` (~671) writes with pcbnew | `emit` + `boardtext.append_blocks`; `shove` removals with `boardtext.remove_blocks` | K16 |
| `H.suggest`, `handrepair`, `pourisland`, `hrrender`, `critroute`, `viamin`, `snapiter`, `power`, `gaps`, `drcguard` | unchanged in C1 (they keep working through the `Model` surface); `suggest` is C2 | |

## 11. Steps

Sizes are new lines (Rust unless marked py). Every step: `cd kernel && cargo test <module>`; from K0 on also
`kernel/build.sh` and the named pytest file. `cargo clippy` clean. Tests named here are the minimum.

| Step | Builds | Size | Tests |
|---|---|---|---|
| K0 | Crate, `Cargo.toml` (`crate-type = ["cdylib", "rlib"]`, release `opt-level = 3`, `panic = "unwind"`), `lib.rs` FFI with `catch_unwind`, op dispatch with `ping` and `info` stubs, `build.sh`, `.gitignore`, `fluxplace/kernel.py` (`Kernel(path, **opts)`, `.call(op, **kw)` raising `KernelError`, `.raster()`, `close`, context manager, `available()`) | 150 + 100 py | `tests/test_kernel_ffi.py`: ping; an unknown op raises; a missing file raises with the path in the message; a forced panic op returns an error and the next call works; 1000 open/close cycles do not grow RSS by more than 20 MB |
| K1 | `sexpr.rs`: tokenizer (quoted strings with escapes, numbers, symbols), tree of nodes with byte spans, helpers `kids(tag)`, `first(tag)`, `atoms`, `nums`, `str_at`. Numbers parse straight to nm (decimal string -> i64, no float) | 250 | parse a 30-line sample; escapes; span of a node equals its text; `1.475` -> 1 475 000 and `-0.9375` -> -937 500 exactly; the W14 r5 board parses in under 400 ms (test skipped when the file is absent) |
| K2 | `geom.rs`: `Pt`, `Shape`, bbox, point-seg, seg-seg, point-in-polygon, seg-poly, poly-poly distance (0 when overlapping), `gap(a, b)`, arc (start, mid, end) -> polyline with a stated chord error, roundrect and chamfer construction, rotation in exact quarter turns and general angles | 400 | each distance against hand values; random pairs against a brute-force sampled distance (tolerance 2 um); the arc error bound holds; a roundrect gap equals the analytic value within 1 nm |
| K3 | `board.rs` + `load.rs`: layers, nets (both file forms), segments, arcs, vias (+ holes), Edge.Cuts and copper graphics, item table, `items` op | 350 | a `tests/pcbgen.py` board: counts, coordinates in nm, via span; a KiCad 7 form sample (`(net 5 "N")`) loads to the same items as its KiCad 10 form |
| K4 | `load_fp.rs`: footprints, transform, pads (all shapes of section 5), drills and slots, layer wildcards, NPTH, local clearance, footprint Edge.Cuts and keepouts, `unsupported` | 400 | per pad shape one case at 0, 90 and 37 degrees against hand corner values; a slot's capsule; `*.Cu` expands to every copper layer; a `padstack` pad lands in `unsupported` with the conservative shape |
| K5 | `load_zone.rs`: zones, keepout flags, fills per layer, `fill_stale`; **loader differential** `tests/test_kernel_loader.py` (py): kernel `items` against `G.Model(path, refill=False).obs` and `.zones` | 250 + 150 py | on a pcbgen board and, when present, W14 r5 and CM5_MINIMA_3: same item count per kind; every pad, track, via and hole matched by uuid; positions equal within 1 nm; pad outlines within 5 um (pcbnew approximates arcs); zone fill areas within 0.1 %. **Fable reviews this diff before K6** |
| K6 | `index.rs`: grid, insert, remove, `query`; `items` op with `bbox` | 200 | query equals a brute-force scan on 5 000 random boxes; remove then query; an item that spans 200 cells |
| K7 | `rules.rs`: `.kicad_pro` reading, floors, classes, patterns, assignments, effective class, local overrides, fallback constants, `set_floors`, `net_rule`, `info.rules` | 350 | `tests/test_kernel_rules.py`: `net_rule` equals `G.Model.net_rule` for every net of a pcbgen board with two classes and patterns and, when present, W14 r5; without a project the fallback equals what pcbnew reports for the same copy; a priority tie resolves by class order in the file |
| K8 | `dru.rs`: parser, condition evaluator with three values (true, false, may), the conservative fallback, the coverage report | 400 | each atom; `&&`, `||`, `!` with `may`; units; `severity ignore`; a rule with `insideArea` is listed and only raises; when present, `/usr/share/kicad/demos/vme-wren/vme-wren.kicad_dru` and `.../jetson-agx-thor-baseboard/jetson-agx-thor-baseboard.kicad_dru` parse without error and report their coverage |
| K9 | `clear.rs`: `need` (section 6), the check of an item set against the index: clearance, short, hole clearance, hole to hole, edge, keepout, width, via size, drill, annular; violation records; `need` op | 400 | one case per violation type on pcbgen boards at `need - 1 um` (refuse), `need` and `need + 1 um` (accept); a local pad clearance wins over the class; a dru rule lowers and another raises; same-net copper never violates |
| K10 | `tx.rs`: log, nested `begin` / `commit` / `rollback`, `add`, `remove`, uuids, `emit`, `check` op without connectivity | 300 | after any rollback the `items` dump is byte-equal to the one before `begin` (200 random transactions, nested to depth 3); `emit` text parses back with K1 to the same item; an emitted via and segment appended to a board pass `kicad-cli pcb drc` load (one kicad-cli call in the test) |
| K11 | `conn.rs`: per-net union-find on the log, touch and anchor rules, fills from the stored polygons (no raster yet), `opens`, `islands`, dangling ends, rebuild after removal; opens in `check` | 350 | `tests/test_kernel_conn.py`: `islands` and opens equal `G.Model.islands` for every net of a pcbgen board with a zone and, when present, W14 r5 (9 opens); add a closing segment -> opens -1; rollback restores; remove a bridge track -> +1; the largest net rebuilds in < 20 ms |
| K12 | `path.rs`: `check_path` (continuity, track centred on via, ends join, floors: port the non-clearance checks of `H.probe_prims` ~504-650), `fpk_raster`, `stats` | 350 | `tests/test_kernel_path.py`: for 300 random paths on a pcbgen board the `ok` flag and the violation kinds equal `H.probe_prims` on the legacy model (differences listed; each one explained in the commit message as a legacy miss or fixed); the raster equals `H._free_fields` within the pitch |
| K13 | `tests/kdiff.py` part 1 (py): calibration board builder (one deliberate case per modelled rule and precedence question of sections 6-7), base preparation, oracle run and report parsing (`drcguard.key_of`, `drcguard.unc_nets`), the table of kicad-cli type strings -> kernel types, the epsilon probe. Adjust `clear.rs` / `conn.rs` constants to the measured behaviour | 300 py | `tests/test_kdiff.py`: every calibration case agrees with kicad-cli (this test calls kicad-cli; about 10 calls); the measured epsilon and the precedence answers are written to `docs/corpus/kdiff-calibration.json` |
| K14 | `kdiff.py` part 2 (py): edit generator, batcher, attribution, metrics, cases, outputs, `tests/kdiff.sh`. First detached run **without** pour-cut edits: 1 000 edits per board on the three boards | 350 py | generator is deterministic per seed; batches satisfy the independence rules; attribution on a stub report; a planted wrong kernel verdict is counted as a false accept and saved as a case. **Fable reviews the first run's numbers before K15**; every false accept gets a fix and a case |
| K15 | `pour.rs` (section 8); pour-cut edits enabled in the tester | 400 | a neck cut gives `split`; a cut beside the neck gives `intact`; an own-net via in the fill gives `joined`; rollback restores the raster bit for bit; a hatched zone gives `uncertain` |
| K16 | `fluxplace/kmodel.py` (py): `engine`, `KModel` (section 10), stale-fill refill, `H._model` switch, `probe_prims` and `_free_fields` routed to the kernel | 350 py | `tests/test_kmodel.py`: with `FLUXPLACE_KERNEL=1`, `probe`, `window` and `open_nets` give the same results as legacy on a pcbgen board and `KModel.swig_loads == 0`; with the library renamed away the legacy model is used and one warning is printed; `tests/test_handroute.py` passes in both modes |
| K17 | `H._edit` pre-check and the text write path of `_apply_prims` and `shove` in kernel mode (py) | 250 py | a refused `lay` returns without any kicad-cli call (subprocess patched to fail); an accepted `lay` writes a board whose kicad-cli report equals the one the pcbnew write path gives for the same edit; `tests/test_handroute*.py` and `tests/test_lock_net.py` pass in both modes |
| K18 | Exit run (no code): detached `tests/kdiff.sh`, 10 000 edits over the three boards; `docs/corpus/kdiff.jsonl` rows; ledger rows in `docs/ONE-SHOT-LEDGER.md` for every threshold missed (cause, fix, measured result); the measured `lay` and `probe` latency in kernel and legacy mode on W14 r5 | 0 | section 9 thresholds |

Totals: about 4 550 lines Rust, about 1 500 lines Python, 18 steps. Fable gates: after K5 (loader), after K14 (first
numbers), after K18 (exit). Sonnet takes a step after two failed Haiku attempts.

## 12. Rules for the implementing agent

- Do not read G or H whole. Read the functions named in the step.
- Never edit a source board; tests build boards with `tests/pcbgen.py` in `tmp_path`. Real boards are read-only and optional (skip when absent).
- No `unwrap()` on data from the file or from a request; return an error with the s-expression line or the field name.
- No float in stored coordinates. Floats appear only in distance results and at the mm API.
- A legacy disagreement is not automatically a kernel bug: when the kernel and `G.Model` differ, kicad-cli decides (K13's oracle), and the outcome is written in the commit message.
- No kicad-cli call inside the kernel or `fluxplace/kernel.py`. Only `KModel` (stale refill) and the tester call it.
- Full tester runs and anything over 2 minutes are detached scripts; the agent writes them and stops.

## 13. K5b fixes (K5 gate review, Fable, 2026-11-22)

Run K5b-1, K5b-2, K5b-3 in order before K6, one commit each (`ROUTER-C1-K5b-<n>: ...`). No Fable gate after them: their
tests are the gate. K6-K14 then proceed.

Measured at the gate (by-uuid differential, kernel against `G.Model(refill=False)`): W14 r5 1575 pads / 1339 holes /
1297 vias / 11041 tracks, CM5_MINIMA_3 and StickHub: every net, layer set, ref and position equal; worst pad outline
4.72 um (r5), 0.001 um (StickHub). The geometry conventions (rotation sign, absolute pad angle, layer order, both net
forms, footprint zones in absolute coordinates) are right. Defects found:

| # | Defect | Effect on K6-K18 |
|---|---|---|
| 1 | `fill_stale` is O(items x fill vertices): `load_ms` 24 978 on r5 (44 ms with the fills stripped), 3 400 on CM5 | load threshold 2 s; every r5 test |
| 2 | `(drill (offset x y))` moves the hole. KiCad keeps the hole at the pad position and moves the copper shape | wrong pad and hole on offset pads |
| 3 | `extra` lacks `pos, size, hole` (pad), `width` (track), `no_tracks, no_vias, no_pads` (keepout) | K12, K16 (`G.Ob` surface) |
| 4 | Filled copper graphics and filled custom-pad primitives are outline chains (interior is not copper); primitive polygon `width` ignored | false accept inside the shape |
| 5 | Arc and circle graphics (Edge.Cuts, copper) are chords with no error added to `r` | up to 1 um non-conservative, exactly the K14 boundary edits |
| 6 | `gr_text` / `fp_text` on copper dropped silently; `gr_curve` / `fp_curve` listed on every layer; `fp_curve` on Edge.Cuts dropped | coverage report wrong; missing edge |
| 7 | `(clearance 0)` on a pad is read as a local clearance of 0 (KiCad: 0 = inherit); a copper zone inside a footprint is dropped | K9 `need` |
| 8 | The K5 test matches bounding boxes greedily: net, layers, ref and rotation sign are not tested (a rect at +37 and -37 degrees has the same box) | regressions pass unseen |
| 9 | uuids are not unique in real files (CM5_MINIMA_3: J101.1 x4, USB701.1 x4, SW501.4, SW501.5 share uuids) | `remove` by uuid, `KModel` ids |

Rulings on the reported deviations:

| Deviation | Ruling |
|---|---|
| C0 `pairs` from the `gaps._pair_audit` rows | accept |
| C0 `strip_nets` removes net-named copper only | accept (both engines get the same start; net-less copper is not routable work) |
| C0 Freerouting `wall_s` = subprocess time | accept |
| K1 400 ms limit in release builds only | accept |
| Trapezoid = conservative rectangle, listed `unsupported` | accept; also roundrect + chamfer as the full rectangle |
| K5 pad tolerance 10 um | reject. The kernel roundrect stays exact (KiCad's DRC uses the exact shape; only `GetEffectivePolygon` is a 5 um polygon). The test tolerance becomes **5.1 um** on the outline metric of K5b-3 (pcbnew max error 5 um + rounding); measured worst 4.72 um |
| One hole per pad or via over all copper | accept (conservative for blind and micro vias); K13 adds a calibration case |
| Edge `r = 0` | accept for straight edges (K13 calibrates stroke width); arcs and circles get the chord bound (K5b-2) |
| Footprint zones in absolute coordinates | accept (r5: 11 of 11 keepouts equal pcbnew) |
| pcbgen 4-layer header for pcbnew tests | accept |
| No `cargo clippy` (component not installed) | accept; replaced by `RUSTFLAGS="-D warnings" cargo build --release` clean in every step (0 warnings today) |

### K5b-1 `fill_stale` in bands (`load_zone.rs`, about 90 lines)

1. Add `struct PolyBands { y0: i64, h: i64, rows: Vec<Vec<(Pt, Pt)>> }` with `h = 1_000_000` nm. `PolyBands::new(poly)`:
   every polygon edge (closing edge included) is pushed into each row its y range overlaps (`row = (y - y0) / h`).
2. `contains(p) -> bool`: even-odd ray cast along +x over the edges of `p`'s row only (every edge that crosses the line
   `y = p.y` is in that row). Outside the y range: false.
3. `near(a, b, r) -> bool`: true when `contains(a)`, or some edge in the rows overlapping `[min(a.y, b.y) - r, max(a.y, b.y) + r]`
   whose x range overlaps `[min(a.x, b.x) - r, max(a.x, b.x) + r]` has `geom::seg_seg(a, b, e0, e1) <= r as f64`.
4. `fill_stale`: build `PolyBands` once per fill. Keep the layer, net and bbox filters. Per shape: `Point(p)` ->
   `near(p, p, r)`; `Seg(a, b)` -> `near(a, b, r)`; `Poly(v)` -> any edge `near(v[i], v[i+1], r)`. Return at the first hit.
5. Tests. Rust: for 20 random simple polygons (a star with 50 random radii around a centre), `contains` equals
   `geom`'s point-in-polygon on 2 000 random points, and `near` equals `core_dist(Poly, Seg) - r <= 0` on 500 random
   segments. Python (`tests/test_kernel_loader.py`, skip when the board is absent): `info.load_ms` < 2 000 on W14 r5,
   < 1 000 on CM5_MINIMA_3; `fill_stale` is True on r5, False on CM5_MINIMA_3 and StickHub (today's values).

### K5b-2 loader fixes (`load.rs`, `load_fp.rs`, `lib.rs`, about 150 lines)

1. Drill offset: the hole stays at the pad position `centre`. The copper shapes are placed at
   `centre + rotate(offset, pad angle)`. No offset: unchanged.
2. `extra`. Pad: `{"pos": [x, y], "size": [sx, sy], "hole": drill x or 0, "shape", "pad_angle", "type"}` (mm; `pos` =
   the pad position = hole position). Track: `{"width"}`. Keepout: add booleans `no_tracks`, `no_vias`, `no_pads`
   (flag == `not_allowed`) beside `keepout`. Hole of a via: `{"via": true}`; of a pad: `{"npth": bool}` (as today).
3. Filled shapes. `(fill yes)` or `(fill solid)` on `gr_poly` / `gr_rect` / `fp_poly` / `fp_rect` on a copper layer, and
   custom-pad primitives `gr_poly` / `gr_rect` (filled unless `(fill no)` or `(fill none)`): `Core::Poly`, `r = width / 2`.
   A filled `gr_circle` (same three places): `Core::Point(centre)`, `r = radius + width / 2`. Edge.Cuts is never filled.
4. Arc error. `gr_shapes` returns `(shapes, bound_nm)`: `ceil` of the `arc_polyline` bound for `gr_arc` and `gr_circle`,
   0 otherwise. Copper graphics and pad primitives: `r = width / 2 + bound`. Edge.Cuts: `r = bound` (0 for straight edges).
5. Text and curves. `gr_text`, `fp_text` and a visible `property` whose `(layer)` is copper: one `unsupported` row
   (`what: "text on copper"`, `effect: "not modelled; the judge sees it"`). `gr_curve` / `fp_curve`: handled only when the
   layer is copper or Edge.Cuts (no `unsupported` row on other layers); `fp_curve` gets the same control-point box as
   `gr_curve`, through the footprint transform.
6. Pad `(clearance 0)` -> `None`. Footprint zones: call `load_zone::load_zone` for every `(zone)` child of a footprint
   (keepout or copper) and delete `load_keepout`.
7. `info.dup_uuids`: the number of uuids carried by more than one item of kind pad, track, arc or via.
8. Rust tests: a thru-hole rect pad `(at 1 0 90) (size 2 1) (drill 0.4 (offset 0.5 0))` under a footprint at `0 0 0` has
   its hole at (1, 0) and its pad bbox [0.5, -1.5, 1.5, 0.5] (offset (0.5, 0) rotated by 90 = (0, -0.5)); a filled copper
   `gr_poly` is one `Poly` shape and a point inside has gap < 0; a filled circle is `Point` with `r = radius + width / 2`;
   an Edge.Cuts `gr_arc` has `r > 0`, a `gr_line` has `r = 0`; `gr_text` on `F.Cu` gives one `unsupported` row and on
   `F.SilkS` none; `gr_curve` on `F.SilkS` gives none; `(clearance 0)` gives `clr_local == None`; the extra keys exist.
   Python: `dup_uuids` > 0 on CM5_MINIMA_3 and == 0 on r5 (skip when absent).

### K5b-3 strict loader differential (`tests/test_kernel_loader.py`, about 120 lines py, replaces `_unmatched` / `_compare`)

1. Group kernel items by `(kind, uuid)`. For each legacy `Ob` (`edge` handled as today; `track` with `extra["arc"]` is
   kind `arc`, `ko` is `keepout`): take the kernel items of the same key; with several (duplicate uuids), pick the one
   whose bbox centre is nearest and use each kernel item once. No candidate = a problem.
2. Per matched pair, a problem when:
   - pad, track, arc, via: `net_name != ob.net`;
   - pad, track, arc: `set(layers) != set(ob.layers)`; pad: `ref != ob.ref`;
   - hole, via, track: any bbox side differs by more than 1.5e-6 mm;
   - pad: `dev > 0.0051` mm, with `dev = max(|sd(p)|)` over the vertices `p` of the legacy polygon `ob.g[1]` and
     `sd(p) = min over the kernel shapes of (distance(p, core), negative inside a Poly core) - r`. For a pad whose kernel
     `extra.shape` is `trapezoid` the rule is `max(sd(p)) <= 0.0051` only (the kernel shape is a superset);
   - keepout: bbox differs by more than 1e-5 mm, layer set differs, or `no_tracks / no_vias / no_pads` differ.
   Counts per kind and the zone fill areas (0.1 %) stay as today. Report the worst `dev` in the assertion message.
3. Fixture board, written by pcbnew so the file is authentic: text board (HEAD4) with one footprint at `20 20 0`
   holding raw pad strings: `smd roundrect (at -2 0) (size 1.2 0.6) (roundrect_rratio 0.25)`, `smd oval (at 0 0 30) (size 1.4 0.6)`,
   `smd rect (at 2 0) (size 1 0.5)`, `smd trapezoid (at 4 0) (size 1 0.8) (rect_delta 0 0.3)`,
   `thru_hole rect (at 6 0) (size 2 1) (drill 0.4 (offset 0.5 0)) (layers "*.Cu")`,
   `thru_hole oval (at 9 0) (size 1.2 2) (drill oval 0.5 1.4) (layers "*.Cu")`, and a custom pad
   `smd custom (at 12 0) (size 0.4 0.4) (options (clearance outline) (anchor rect)) (primitives (gr_poly (pts (xy 0 0) (xy 1 0) (xy 0 1)) (width 0.1) (fill yes)))`
   (nets A..G, `(layers "F.Cu")` where not given), plus ZONE, TRACK, KEEPOUT, the via and the arc of today's test.
   Load it with `pcbnew.LoadBoard`, `pcbnew.SaveBoard` to `base.kicad_pcb`. Then on the loaded board rotate the
   footprint to 37 degrees (`fp.SetOrientationDegrees(37)`), save `rot.kicad_pcb`; flip it to the back
   (`fp.Flip(fp.GetPosition(), ...)`; find the flip-direction argument with `help(fp.Flip)`), save `flip.kicad_pcb`.
4. Tests: the differential is empty on `base`, `rot` and `flip`, on W14 r5, CM5_MINIMA_3 and StickHub
   (`/usr/share/kicad/demos/stickhub/StickHub.kicad_pcb`; skip when absent). A self-test proves the comparison bites:
   a kernel item list with one pad's `net_name` changed, and one with a pad shape mirrored in x, each give a problem.
   If pcbnew disagrees with the kernel on `rot` or `flip`, pcbnew wins: fix `load_fp.rs` in this step and say so in the commit.

Notes carried to later steps: K10 `remove` by `uuids` returns an error for a uuid that `dup_uuids` counts (use ids);
K13 calibration cases add an NPTH pad with a copper annulus, a blind via's hole on a layer outside its span, an
Edge.Cuts line with a wide stroke, and a foreign track nearer to a stored fill than the zone clearance without touching
it (`fill_stale` stays "overlap" unless that case shows a false accept).
