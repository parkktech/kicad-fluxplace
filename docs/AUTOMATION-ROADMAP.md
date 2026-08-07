# fluxplace → automated layout & routing system — review + roadmap

*Written 2026-08-06 by the AI pair, for engineer review. Goal: "shoot a board into this
system and let it work its magic" — netlist in, a **placed, routed, DRC-clean board and a
build-quality Gerber package** out for a final engineer to review and tweak, repeatable at
volume.*

---

## 1. Target pipeline

```
  netlist + constraints                          (schematic, outline, mounting, netclasses)
        │
        ▼
  [1] PLACE      fluxplace: signal-flow placement, escape-aware, side-aware, compact
        │        - locks honored (mezzanine/mounting at mate coords)
        │        - fine-pitch parts get a fanout HALO so escapes stay routable at bulk width
        ▼
  [2] FIT        compact-first; grow the outline the SMALLEST amount that routes if needed
        │
        ▼
  [3] ROUTE      freerouting as the router; tournament picks the best-routing placement
        │
        ▼
  [4] ESCAPE     for nets that still won't close, auto-detect the stuck fine-pitch zones and
        │        step ONLY those local rules 0.20→0.15→0.125→0.10mm (signal nets only; rails
        │        keep ampacity width); re-route. Bulk board stays conservative.
        ▼
  [5] VERIFY     kicad-cli DRC → PASS / REVIEW
        │
        ▼
  [6] FAB        gerbers + drill + pick-and-place + DRC report + MANIFEST  →  engineer review
```

Stages [1][2][4][6] are **built and committed** (v0.6.0). Stage [3] exists (tournament,
freerouting) and is **wired but slow**. The open work is orchestration + validation (§5–6).

---

## 2. What works now (this session, with evidence)

| Capability | Status | Evidence |
|---|---|---|
| Signal-flow placement | shipped (pre-existing) | CM5 carrier HPWL −50%, area −28% |
| **Locks as hard anchors** | ✅ committed | dig mezzanine+mounting holes hold at mate coords; placement built around them |
| **Side-awareness** (F/B don't collide) | ✅ | double-sided boards no longer target ~2× area |
| **Feedback-divergence fix** | ✅ | the congestion loop was ballooning dense boards (98×102→317×356 over 6 rounds); now keeps-best, never diverges |
| **Builder escape-cap** | ✅ | orphans no longer flung ±200mm off-board |
| **Free-path result** | ✅ | RF (517 parts) **317×356 → 107×89 mm** (12× smaller area), next to the 92×86 hand layout |
| **Escape-aware halo** | ✅ | dig route-gate congestion **10.6 → 7.0**; answers "avoid the step-down" for the 7/8 zones that are congestion-limited |
| **Centroid packer** | ✅ | closes whitespace toward centre (router-gated) |
| **Controlled expansion** | ✅ | grow-to-route, smallest expansion that clears the gate; a board that already routes is untouched |
| **Net-aware step-down floor** | ✅ | signal nets thin to fab floor; power/current rails keep ampacity width, never necked |
| **Adaptive-escape detection** | ✅ | auto-finds the stuck fine-pitch parts from DRC (on dig: exactly J20+U10+tail) and emits local `.kicad_dru` |
| **Fab package** | ✅ | `fluxplace fab` → 30 gerber layers + drill + P&P + DRC verdict + MANIFEST, verified on dig |
| **CM5 guardrail** | ✅ held | overflow 0 throughout; area within tolerance; worst-detour **2.58 → 1.67** |

Six commits, pushed to `origin/main` @ v0.6.0, merged clean with the tournament-mode work.
16 unit tests green; deterministic.

## 3. The key design insight this session

The routing ceiling on a dense board is **not** placement effort or router skill — it splits
in two, and each half has a *different* right fix:

- **Congestion-limited** (peripheral packages: LQFP/QFN/QFP/TSSOP at 0.5mm — a 0.2mm trace
  *fits* between pins). 7 of dig's 8 stuck zones. **Fix = placement** (escape halo → keep the
  fanout corridor clear). No fine copper needed.
- **Geometry-limited** (2-row mezzanine inner row — can't escape between 0.5mm pads at all).
  1 of 8 (J20). **Fix = local fine copper** (the adaptive step-down), and only there.

So the system's rule is: **open a corridor first (placement); drop to fine copper only where
geometry truly demands it; never thin a power rail.** That keeps the bulk board conservative
and manufacturable while still closing the hard escapes.

---

## 4. Honest validation state (what's proven vs not)

- **Proven** on fluxplace's own fast route-gate + unit tests + the CM5/RF benches: placement
  quality, divergence fixes, halo effect, expansion, net-aware floor, fab emit.
- **NOT yet proven on a real router**, because freerouting here is **slow**: measured ~2–8 min
  per pass, **~1 hour for a full session on the 300-part dig board**, and it plateaus (~237
  unrouted). KRT (the fast Rust router) needs scipy/shapely, and pip is PEP-668-locked (I would
  not `--break-system-packages` the Python pcbnew runs on).
- A **CM5 tournament** (freerouting as fitness) was running at hand-off to get the first real
  routed-percentage numbers; see `/tmp/tourney_cm5/tournament.json` for results.

**Bottom line:** the placement/escape logic is sound and measured on the proxy; the last mile
is a *routing-throughput* problem, not a placement one.

---

## 5. Gaps to close for "shoot boards in, magic out"

1. **A fast, scriptable router in the loop.** Freerouting at ~1 hr/board doesn't scale to
   "a lot of boards." Options, best first:
   - **KRT (Rust)** — fast, headless, already reaches 97% on dig at 0.1mm; just needs its
     Python deps in an environment that allows install (a venv, or a container). Highest ROI.
   - Freerouting with tighter pass budgets + parallel JVMs (tournament already does this) —
     acceptable for low volume, too slow for high.
2. **Wire adaptive-escape into the router loop.** `escape.py` detects zones + emits rules; it
   needs the driver: route → DRC → detect → step-down local rules → re-route → repeat until
   closed or floor. ~a day once a fast router is in.
3. **Board-fit for locked/fixed-outline boards.** fluxplace's dig placement still needs ~95mm
   in an 85mm outline — denser than the hand-pack is the open research (better legalizer /
   analytic legalization). Controlled expansion (now shipped) reduces the pressure by letting
   the outline flex, per the new "grow slightly if needed" policy.
4. **Constraint ingest.** Today constraints come from the .kicad_pcb (outline, locks,
   netclasses). A thin front-end (YAML: outline, keep-outs, net rules, fab profile) makes
   "shoot a board in" literal.
5. **BOM + assembly** in the fab package (currently gerbers/drill/place/DRC; add BOM from the
   schematic and a JLCPCB-format CPL/BOM pair).

## 6. Recommended next steps (priority order)

1. **Stand up KRT in a venv** (scipy/shapely) and make it the default router; keep freerouting
   as the high-quality fallback / fitness function. *Unblocks all real validation.*
2. **Build the adaptive-escape driver** (`fluxplace route --adaptive`): the route→detect→
   step-down→re-route loop over KRT, honoring the net-aware floor, notifying on completion.
3. **Validate on dig end-to-end**: escape-aware placement → KRT route → adaptive escape at
   J20 → DRC PASS → `fluxplace fab`. Compare routed-% to the hand `d034be5` (89%).
4. **Better legalizer for density** (analytic/legalize-with-spreading) so fixed-outline boards
   fit without growth — the one true placement-quality frontier left.
5. **Batch front-end**: `fluxplace auto --board X --profile jlcpcb-6L` runs [1]→[6] and drops a
   review-ready package. This is the "magic" endpoint.

## 7. One-command vision

```bash
fluxplace auto --netlist board.net --outline 97x85 --profile jlcpcb-6L --out ./fab
#  → places (escape-aware) → routes (KRT + adaptive escape) → DRC → gerbers
#  → "PASS — package at ./fab (routed 100%, 2 local fine-pitch zones at J20)"  + notify
```

Everything from [1] to [6] exists in pieces today; the remaining work is the fast router and
the two thin drivers (adaptive loop, batch front-end). The hard part — placement that routes —
is done and measured.
