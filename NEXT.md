# fluxplace — next session

## 2026-08-12 — SOURCING PRE-FLIGHT: the placer asks the distributors first
New `fluxplace/sourcing.py` + a `sourcing` subcommand + a pre-flight hook in
**place / compact / auto / tournament**. Placement is the point of no return
for a part choice: once a footprint is placed, routed and DRC'd, an unbuyable
part costs a RE-LAYOUT, not a re-order.
- Grades every MPN against live DigiKey + Mouser: OK / LOW / LEAD (catalogued,
  0 stock) / RISK (EOL-NRND) / NONE (nobody carries it). 24 h cache beside the
  map file, `--sourcing-refresh` to force.
- Global flags: `--mpn-map` (auto-discovered next to the board / ../tools/ if
  omitted), `--sourcing-need N` (default 10), `--strict-sourcing` (ABORT on
  NONE/RISK before placing), `--no-sourcing`.
- Advisory by default on purpose: a lead-time part is a schedule decision, not
  a layout defect. Only NONE/RISK are blockers.
- **Rate limits (measured, not guessed):** Mouser answers a BURST with HTTP
  403, not 429 — its limit is ~30 calls/min, so calls are throttled to 2.1 s
  with 3 s/6 s backoff retries. DigiKey gets a 0.25 s throttle, 429/5xx
  retries, AND a 401 re-auth: its OAuth token expires in ~10 min and a
  throttled sweep of a large BOM outlives it, which would otherwise fail every
  remaining part mid-run.
- **Mouser is a SECOND OPINION**, only queried when DigiKey has not already
  settled the part (ample stock + Active). On a 65-MPN board that is ~8 Mouser
  calls instead of 65 — the quota is spent on the parts that actually need
  adjudicating. `--sourcing-both` forces full dual-source data.
- **A failed lookup is never a NONE.** grade() takes an `errors` list and emits
  ERR (never a blocker) when a distributor did not answer, and a failed lookup
  is never written to the cache. Without this, one transient 403 became a false
  "nobody stocks this part" — and under --strict-sourcing, a bogus abort.
- Degrades gracefully: no credentials, no map, or a total API outage prints a
  note and places anyway.
- The project-side gate (utv-comms-bridge hardware/tools/check_availability.py)
  is now a thin wrapper over this module — they drifted once and only the
  plugin copy got the fixes above.
- Why it exists (utv-comms-bridge D41/D43): an Ethernet magjack lifted from a
  reference design cleared placement, routing, DRC and fab packaging before
  anyone asked a distributor — DigiKey did not carry it, Mouser had 0 stock and
  a 140-day lead. The first run of this check found FIVE MORE zero-stock parts
  already committed to that board (GH-3 header 180 d, buck input caps 112 d,
  CM5 decaps 70 d, tact switch special-order, a 4-pieces-left resistor).
- Companion trap, worth knowing before any substitution: **a land-pattern match
  is not a part match.** Gigabit magjacks share one industry-standard footprint
  across vendors with incompatible pinouts (three vendors, three pinouts,
  verified). Always read the datasheet pin table.

# fluxplace — next session

## 2026-08-11 final — v0.8.0 validated: utv-comms-flux-q3 (0 DRC / 0 opens)
Tournament q3 winner (compact 0.80:0.72 bands+65-passive back-flip, judged by
the new lexicographic rank) delivered as **utv-comms-flux-q3.kicad_pcb:
76.3x86.7mm, 100% routed, 0 violations, 0 unconnected, 77 back-side parts**
— cleanest board of the project (landscape-t1: 227 viol; tournament-a1: 20)
at 13% less area, while Quilter returned ZERO candidates at 65x50 on the
same netlist (43min, "unable to place", D29).
Hard-won plumbing lessons (all in fluxplace now):
- **pcbnew.ImportSpecctraSES returns True headless but mangles geometry**
  (sub-mm gapped fragments, 10-15 phantom opens). fluxplace/ses.py ports the
  UTV custom parser (ses*100=nm, Y flip); tournament.import_session uses it.
- **freerouting sessions are complete only THROUGH the planes**: zone refill
  islands the inner planes -> stranded fragments. Cure (deliver_winner.sh):
  GND surface pours F+B + stitch grid 4mm (planes.pour/stitch) merges every
  GND island; KRT --keep-input-copper --nets "+5V" straps the +5V islands;
  finalize_dfm + 0.12 judge clearance + widen sub-0.088 neckdowns -> 0/0.
- freerouting 2.3.x log phrasing "(N unrouted and M violations)" (regex
  fixed); 2.3.0 GUI NPEs on ANY plane-connected DSN (gui_safe_dsn.py strips
  pours for interactive use; headless unaffected).
- rank lesson #2: completion MUST outrank DRC count (less copper = fewer
  violations; a 36-airwire candidate had the lowest DRC on the board).
- planes.py grew: via_stub_ends, via_at_points, tie_floating_clusters
  (fill-verified), snap_opens, bridge_opens — kept for surgical use, but the
  pour+stitch+KRT recipe is the proven path.
Next: PRC-driven placement is still only 11/52 on compact flows (arrangement
preserved => pair scatter persists) — the builder (place_routed) consumes
comprehension only via decap ordering so far; feeding pin-distance windows
into builder auditioning is the next quality lever. Also: emit the flip set
as a right-sized back-ring region instead of free flips (Y stacking cost),
and teach compact's THT bands about per-connector edge affinity from intake.

## 2026-08-11 latest — v0.8.0: the Quilter parity build (P0-P4 SHIPPED)
Full adoption plan (docs/QUILTER-PARITY-PLAN.md) implemented in one session;
suite 76 green. New modules: comprehend.py (constraint auto-detection),
prc.py (placement physics rule checks, tolerance-window reports).
- P0 tournament re-rank: rank_key = (DRC, unrouted, -prc_pass, -clearance,
  -min_w, vias, wl) — wirelength LAST, per Quilter's published sort + our own
  Tournament #1 calibration. Candidates get .ses imported + kicad-cli DRC'd;
  realized min width + completion parsed from the session; >=95% = SUCCESS.
- P1 comprehension: power nets (200/500mA floors, IPC-2221 widths), diff
  pairs (suffix conventions + V-guard + series-segment merge), bypass caps
  (ONE owner = nearest IC on rail; capacitance rank, smallest-closest),
  crystals (incl. series-R + load caps — Quilter misses these), converters
  (SW fanout<=4 guard, ceramic-window hot-loop caps, deterministic).
  CLI: comprehend --prc; eval --prc; eval prints pin density (Quilter <20%).
- P2 controls: kicad_io.read_rule_areas (named+no-items = REGION, Quilter's
  exact convention), hard regions w/ side pinning in compact (stats[outside]
  fails loudly), --outline W:H hard bounds, cluster anchors (locked-centroid
  stickiness), pick_flips side exploration (decaps|passives -> back).
- P3 compile targets: tournament PROFILES (jlc-fine/jlc-std/osh-6mil) sweep.
- P4 contract: --quilter-contract (inside=locked), --preserve-pour NAME,
  --keep-copper, plane_intent (layer-name gnd/pwr semantics).
- constraint_seed: hot-loop members/pair elements/crystal clusters walked to
  their anchor pre-compact (default on; --no-prc-seed).
PRC audit of the three routed boards: 10-15/52 pass — U1 buck hot loop blown
apart on ALL THREE (Cout 52-57mm from U1, loop 1500-1700mm^2). The seeded
tournament (flux-tourney-q1) targets exactly this.

## Where it stands (v0.4.0)
- **Route-aware placement shipped** (`--strategy build`, and the GUI plugin runs it):
  quadratic global solve (hub central) → constructive route-as-you-place builder →
  coarse global-router gate (PathFinder-lite) → congestion feedback loop.
  `place_routed()` in placement.py is the pipeline; quadratic.py / builder.py /
  route.py are the new modules. All tests green incl. solver fallback cross-check
  (numpy vs Gauss-Seidel agree to 1e-4 mm).
- **RAZOR CM5 carrier (178 parts) results:** HPWL 25639 → ~10.4k (old flux ~15.5k),
  extent ~124×114 mm, 0 overlaps, **global-router OVERFLOW 0 (routable)**, hub U1
  dead-central, J80 M.2 welded to the CM5's right edge. J80 on the real board is now
  the compact 2242 (razor-cm5-board CM5-A23).
- Grid capacity model: THT = real drilled pin field (≥5 drills or majority), SMD
  bodies only crowd their own layer, pin cells keep escape capacity (0.35× block).

## v0.3.0 added (all ten improvement items)
- Layer-aware router (H/V split + via cost), escape rings, tiny-passive transparency,
  landing corridors + terminal snapping + tapered wide-net width, stacking clamp.
- Power traces routed with real widths + power springs in quad + power ties in the
  builder order + net-anchor candidates ("the fuse goes right at the connector").
- Rotation auditioning in the builder on verified pin rotation (pin_at).
- Shrink-to-smallest-routable (binary search under the gate), decap adjacency pass,
  --seeds N multi-start. Fully deterministic (several salted-set leaks fixed).
- Route guides on Eco1.User; calibrate cmd (DSN export + freerouting + agreement).

## v0.4.0 added (deep-audit fixes, each bench-gated)
- Post-placement ORIENTATION REFINEMENT sweep (audit: 35 parts misfaced incl. LQFP
  worth 36 weighted-mm; builder-time audition sees only the half-built board).
- Decap pass v2: slots ranked by dist-to-OWNER (v1 sorted by dist-to-current — the
  bug that made it a no-op), batched gate acceptance, extent-clamped; far-decaps
  30 -> 21 (rest are slot-starved; needs swaps).
- Connector edge-flush pass (J60 flush, J50 19.6->10.9mm; slides until blocked).
- Per-axis (anisotropic) shrink on top of the uniform search.
- Corridor tax in builder scoring: a body pays for squatting on committed routes.
- Pipeline order: builder -> feedback -> DECAPS -> SHRINK -> FLUSH -> ORIENT (each
  pass gated, reverts if the router objects).

## Calibration status (freerouting ground truth)
- v2.1.0 jar CRASHES on plane-connected targets (NPE in MazeSearchAlgo) — use 2.2.4+.
- With GND/VIN_PROT zones poured and DEFAULT 0.2/0.2 netclasses, freerouting proves
  DF40 escape impossible (27 fails = exactly the U1-terminating nets) — board-side
  rules problem, NOT placement. With 0.127/0.127 the same nets route (38 left by
  pass 4 vs 122 stuck). Moral: netclasses must be set before judging placement.

## Tournament #1 results (2026-08-06/07, RAZOR CM5)
- 12 candidates, all passed the gate at overflow 0; 9 full freerouting sessions.
- WINNER: fill 0.72 / aspect 1.35 / pad 0.6 — 41/444 unrouted, 302 vias, 3463mm.
- All unrouted signals = the two DRC-broken provisional footprints, every time.
- CALIBRATION: truth rank != gate rank (gate #1 -> truth #4; truth #1 was gate #4).
  Truth rewards looser spacing (pad 0.6 won despite worst gate wirelength): the
  gate over-values wirelength, under-values elbow room -> consider scoring util
  headroom, or drop pitch to 0.30 so capacity reads tighter.
- OPS lessons: freerouting settings live in freerouting.json (optimizer.max_passes
  100 @ 0.01% = silent hour-long phase; cap to 8 @ 1.0%); -oit CLI flag is ignored;
  SIGTERM does NOT save the session; via 0.5/0.3 packs tighter than JLC's 0.254
  hole-to-copper -> use via_diameter 0.6 in classes for autoroute runs.

## Next ideas (in rough priority)
1. Decap swaps (displace lighter parts) for the remaining slot-starved 21.
2. Escape-aware candidate scoring (pin rows facing walls).
3. Pair-aware placement: keep P/N series caps side by side explicitly.
4. Emit guides as KiCad rule areas (per-net keepouts for interactive routing).
5. Worst-offender relocation sweep (re-audition top detour contributors post-build).

## Don't repeat these dead-ends
- Directional compaction of ALL parts (wrecks wirelength) — only with anchors frozen.
- Pinning the hub at board center (worse HPWL than letting the quad math place it).
- Sizing parts by pad bbox / courtyard alone — use the FULL footprint bbox.
- Boolean THT flag (one mounting hole walled off the whole CM5 module).
- Blocking pin cells at full body blockage (false congestion at every connector).

## 2026-08-10 late session — auto pipeline overhaul (UTV comms bridge board, 129 parts)
Validated end-to-end on a fresh CM5 carrier netlist. `auto` was effectively broken; now:
placed → outline → planes → route → plane-finalize → DFM → fab in one command.
- **cmd_auto never shrink-wrapped Edge.Cuts** (cmd_place did). Placement landed outside
  the outline; KRT saw every pad off-board and silently routed 0 nets. Fixed: stage [2].
- **KRT routes NOTHING without --nets** (the "no list = everything" comment was wrong).
  Fixed: explicit net list + `--power-nets/--power-nets-widths` from a name/fanout
  classifier (`_classify_power`).
- **New fluxplace/planes.py**: GND pours on all Cu layers (SOLID pad connection —
  thermal spokes starve at fine pitch), collision-safe stitch grid, zone refill,
  `finalize_dfm` (JLCPCB rule floor embedded in board, via clamp, graze-shrink),
  `sync_project_rules` (.kicad_pro OVERRIDES the board at load — must be written too).
  pcbnew gotcha: hold refs to ZONE/SHAPE_POLY_SET/removed items until after Save() —
  GC'd SWIG proxies segfault inside Save.
- **eff_size double-rotated pre-rotated parts**: stored w/h are at `angle0` but every
  call site passes ABSOLUTE angles. Locked 90°-placed connectors were modeled sideways
  (decap walk put caps ON the DF40 pad rows). eff_size now rotates by the delta.
- **--obstacle X:Y:W:H**: phantom locked rects for plug-on module bodies (SoM over its
  two board connectors), enclosure bosses. Stripped before write-back.
- **_legalize_bboxes** post-place overlap nudge (never moves locked parts).
- Rigid connector pairs (CM5: two DF40 at 34.000 mm, pad-1 ends aligned) are pre-placed
  + locked by a project script (utv-comms-bridge/hardware/tools/cm5_pair.py). A first-class
  `--rigid-group REF1,REF2@dx,dy` feature would generalize it.

Result on the 129-part board: 92/92 nets, 216/216 pairs, 0 unrouted, 5 residual DRC
items — all KRT plane-finalize vias grazing 0.4 mm-pitch pads by 0.02–0.17 mm.
### Upstream (KRT) issues found
- Plane-finalize places 0.45 vias INLINE with 0.4-pitch connector rows: geometric max
  air 0.075 mm < 0.0975 rule. Wants a diagonal-offset or smaller-via policy near
  fine-pitch pads (graze-shrink in finalize_dfm mitigates the 0.075 cases only).
- **KRT is run-to-run nondeterministic** (same board+args → 0 or 1 unrouted, different
  via spots). Seed or thread-order pin needed before regression-testing against it.

## Back-side module flow (CM5-Minima style) — works; density is now the frontier
- `--obstacle` accepts `X:Y:W:H[:F|B]`; side-aware phantoms verified: back-side
  module shadow blocks back SMDs and THT (pierces both) but frees the entire top.
  129-part board: module zone clean, 0 unrouted (route-out5).
- Board sizes across runs: front-module 109x91, back-module 140x110 — the placer's
  air, not the side choice, is the limiter (the RAZOR benchmark is 124x114 too).
  Target-class boards (65x50) need real density work: harder shrink under the gate,
  denser builder auditioning, and shrink that can pull toward LOCKED anchors.
- `--pad 0.3` is a regression (470 overlap nudges, 2 unrouted): the builder's
  corridors assume ~0.45. Don't ship lower without retuning the route model.

## 2026-08-10 later — v0.7.0: `compact` subcommand (UTV comms bridge session 2)
Placement compaction shipped as `fluxplace compact`: shrink a KNOWN-GOOD placement
instead of re-placing (scale unlocked parts toward the locked anchor -> soft
legalize -> spiral hard-resolve -> nearest-first gravity pack -> the same
outline/planes/route/DFM/fab stages as `auto`, now shared via
`_stages_outline_to_fab`). Core in `fluxplace/compact.py`, pure python, tested
(tests/test_compact.py; suite 21 green). `auto` CLI and behavior unchanged;
`read_board` gained an additive `drills` key (raw drilled-pad count).

UTV comms bridge results (129 parts, CM5 pair locked, 2-layer route 0.15/0.15):
- auto baseline 140x110 -> compact sx.44/sy.47 = **76.5x69.1, 92/92 nets,
  216/216 pairs, 0 unrouted, 5 DRC** in 118 s. 2.9x area cut, one command.
- Density wall found: ~5000 mm2 (47-50% bbox util) is where KRT starts failing
  (30+ unrouted at 59x76 / 65x70). The failures cluster on I2S + power leaving
  the module zone.
- **Do not stuff the module shadow with back-side passives** — migrating 53
  small passives under the CM5 killed the DF40 escape (that rule is now baked
  into compact's obstacle keep-out: unlocked same-side + THT parts stay out).
- Anomaly to chase: failing runs report pad_pairs_total ~101 vs 216 on the
  clean run — KRT may abort its pair enumeration mid-run on those boards, so
  the wall could be partly router artifact, not pure congestion.
- 65x50 target needs a real dense placer (Quilter/EasyEDA compaction of the
  netlist) or escape-aware packing — compact alone plateaus ~5300 mm2 with the
  real (bigger) M1 footprints.

## 2026-08-10 latest — `lint` subcommand (design-completeness rules)
`fluxplace lint --board X [--json out] [--fail-on error|warning]` — catch unfinished
wiring and connector smells BEFORE spending placement/routing effort. Rules v1:
no-power-entry, no-io-connector, unwired-connector, dead-end-net, no-gnd-on-part,
no-net-pads (info), **barrel-jack** (prefer LATCHING: JST VH/SM, Molex Micro/
Mini-Fit, screw terminal — barrel plugs walk out under vibration),
**power-on-friction-header** (same latching advice). Pure-python core
(fluxplace/lint.py) on a flat pad list; tests/test_lint.py; suite 29 green.
First run on the UTV board caught a REAL bug: R35/U6.SD_MODE strap net missing —
the MAX98357A would never have left shutdown. Additive: no existing command changed.
Next rule ideas: decoupling-cap-per-VDD presence, series-terminator on fast
clocks, connector pin-1 silk, polarized-part silk markers.

## 2026-08-11 — `models` subcommand (real 3D STEP fetch via distributor APIs)
`fluxplace models --board X [--audit-only] [--map ref2mpn.json] [--mpn REF=MPN]
[--models-dir D] [--path-prefix '${KIPRJMOD}/...']` — audits footprints for
missing/BROKEN 3D model paths (env-var expansion + file-exists), then fetches:
1. kicad-official: broken ${KICAD*_3DMODEL_DIR} refs pulled from the
   kicad-packages3D GitLab (the footprint's own intended model)
2. digikey /media "CAD Models" links (direct STEP or zip; validated ISO-10303)
3. mouser search as MPN normalizer (their API has no CAD binaries)
Reality check from the UTV board (11 gaps): only the Hirose DF40 had an open
CAD link. FIVE stdlib footprints reference models that do not exist upstream
either (Micro-Fit 43650-0215 vert, SK6812MINI, ublox_MAX, D_0402, TQFN-16
EP1.23). Bourns/Taoglas/Molex CDNs 403 scripted fetches; SnapEDA/UL/CSE need
logins. Policy: never fake a "real" model — visual stand-ins live in the
BOARD repo (wire_visual_models.py) with a provenance/debt file, not here.
tests/test_models.py (offline); suite 35 green.

## 2026-08-11 — hardening from the UTV runs
- Stage 4/5 router subprocess timeouts no longer kill the pipeline (r6 lost
  its DFM/fab after a GOOD main route because the GND pass hit 1800s and the
  TimeoutExpired propagated). Both stages catch it and continue with the best
  board on disk.
- Stage 4 now prints `actual-unconnected=N` from pcbnew connectivity — KRT's
  JSON_SUMMARY is demonstrably unreliable (reported "1 ok, pairs 2/2" on a
  fully-connected 93-net board, and "9 ok/14 failed" pair totals that vary
  run to run). Trust the board, not the router's accounting.
- models: STEP point-cloud bbox + auto-align (center/z-floor/90deg aspect)
  shipped; see 40e69f9.
- Open item: r6 DRC showed 24 courtyard overlaps on a compact placement that
  legalized to 0 bbox overlaps — bbox(False) vs courtyard discrepancy on
  some footprints; investigate (suspect rotated parts whose courtyard
  exceeds the graphic bbox).

## 2026-08-11 — freerouting 2.2.4 is DEAD for headless jobs; use 2.3.0
Every 2.2.4 CLI job died silently 2-4 min into routing (no .ses, no error,
log ends after "Starting routing") — including configs that worked 2026-08-05
on the razor boards. CLI/json job_timeout ruled out (45 min configured).
freerouting-2.3.0.jar (~/tools/) fixes it: routes 30+ min stably and
auto-configures In1/In2 as power planes when >50% covered. Note 2.3.0
resets unmigrated freerouting.json settings — re-check optimizer caps.
Tournament default flow now validated end-to-end on the UTV board.

## 2026-08-11 — `intake` subcommand (design interview)
`fluxplace intake [--answers a.json] [--apply-board B]` -> design_intent.json.
Asks per external interface: on-board connector (latching families first —
JST XA/GH, Micro-Fit, terminal block, pin header (flagged), SOLDER PADS,
USB-C, SMA, U.FL, other), EDGE vs REMOTE, and for remote-unspecified panels
picks a sealed latching default by kind (power->Deutsch-DT-compatible AT04
flange, audio->mini-XLR, rf->SMA bulkhead, data->M12/USB-C). Mounting: holes
y/n, corners-equal-inset vs free, M2/M2.5/M3, inset mm; --apply-board drops
locked GND corner holes on a board with an outline. Scriptable via injected
ask/say + --answers (agent/CI friendly).
Next intent consumers to build: edge-affinity per interface (feed the builder
edge-flush pass), lint policy from environment (vibration -> flag ALL
friction fits), power-rail budget -> quilter power CSV + ampacity widths,
enclosure envelope -> outline cap + height keepouts, RF net list -> 50R
netclass + pour pullback, diff pairs -> quilter CSV, rigid module patterns
(CM5-style pair+holes) as reusable intent blocks.

## 2026-08-11 later — FULL Quilter docs crawl (all 74 pages) → adoption plan
Complete crawl digested in docs/QUILTER-DOCS-DIGEST.md (every heuristic + number:
detection rules, PRC formulas/tolerances, lexicographic sort tuples, envelope).
Application plan in docs/QUILTER-PARITY-PLAN.md (P0–P4). Headline: Quilter ranks
candidates lexicographically with WIRELENGTH LAST — independently confirms
Tournament #1's "gate over-values wirelength" finding; P0 = re-rank tournament by
(DRC, completion, constraint passes, conservativeness, layers, length). Also:
decap ordering is capacitance-ascending toward the pin (our pass lacks the value
sort); their detection layer is shallow lexical rules we can copy verbatim; their
docs admit gaps (series-R crystals, connector detection, lock granularity) that
are our surpass list. The section below is superseded by the plan doc.

## 2026-08-11 — Quilter feature study (docs.quilter.ai/design-parameters)
What they expose that flux lacks, in adoption order:
1. **Placement regions via KiCad Rule Areas** — their convention: a rule area
   with ALL keepout items deselected = placement REGION (hard constraint:
   a region-assigned part never leaves it, even if the job fails). Flux
   should read board rule areas: keepout-flagged -> obstacle; no-items ->
   region. Gives block floorplanning with a standard KiCad authoring UI —
   the missing piece for functional-cluster placement.
2. **Side exploration** — auto single/double-sided placement as a search
   dimension. Flux compact keeps sides fixed; add per-part side auditioning
   (respecting the module escape ring rule) — the unused back ring is the
   biggest density lever on the UTV board.
3. **Fabricator profiles/stackups** — named rule bundles (JLC04 etc.)
   instead of per-run --track/--clearance flags.
4. **Net widths by LAYER** (flux has per-net only), preserved pours,
   pre-routed trace locking (flux ~= keep-input-copper).
5. They REQUIRE unplaced input + hard outline: their placer treats the
   outline as a constraint to satisfy, not an output to shrinkwrap — flux
   compact should grow an optional --outline W:H hard-target mode that
   fails loudly instead of spreading.
Can flux match them? Placement realism check: their moat is placement
quality under hard outlines + side exploration; our moat is the known-good-
arrangement compaction + adversarial DRC ranking + free/local. Items 1+2+5
are implementable and would close most of the gap for THIS class of board.
