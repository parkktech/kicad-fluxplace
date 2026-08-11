# Quilter → flux: full adoption plan (2026-08-11)

Source: complete crawl of all 74 pages of docs.quilter.ai (digest with every number and
heuristic: `QUILTER-DOCS-DIGEST.md`, same directory). This supersedes the partial
design-parameters study in NEXT.md (2026-08-11) — that section's items 1–5 are folded in
here as P2/P4.

What Quilter actually is, in one line: an AlphaGo-style self-play RL engine whose reward
signal is a battery of 8 physics checks (PRCs), exploring (stackup × rule-set) compile
targets in parallel, surfacing ≤6 DRC-clean candidates ranked by a **strict lexicographic
tuple** — with all constraint intelligence living in a shallow, lexical/topological
"Circuit Comprehension" layer we can replicate exactly.

We cannot (and shouldn't) copy the RL core. Everything else is replicable, and our
deterministic builder + real-router ground truth is arguably *more* honest than their
gate. The plan below is ordered by (impact ÷ effort) for the 65×50-class density target.

---

## P0 — Fix candidate ranking (tournament + gate). Cheapest, highest-leverage.

**The Quilter fact that matters most:** their ranking is lexicographic, and **total trace
length is the LAST tie-break in every sort persona**. Tier 1 is always
(DRC violations ↑, routing completion ↑); physics-constraint passes and fab-rule
conservativeness (larger actual min width/clearance/drill/via = better) come before
length; layer count before length too.

Tournament #1's core finding — *"the gate over-values wirelength, under-values elbow
room; truth rank ≠ gate rank"* — is exactly what Quilter's ordering predicts. The fix is
not a better wirelength model; it's demoting wirelength to tie-break.

- [ ] `tournament.py`: rank candidates by the tuple
      `(drc_violations, −pins_routed_pct, −priority_prc_passes, −min_used_clearance,
      −min_used_width, layer_count, total_length)` instead of any scalar score.
      Personas (Recommended / Easiest-to-Fab / Fewest-Layers) = tier permutations.
- [ ] Adopt Quilter's hard gates: a candidate with ANY DRC violation is never surfaced
      as a winner; job "success" = a candidate ≥95% routed exists; default view filters
      to 100%-routed.
- [ ] Report **actual** min width/clearance used (incl. neck-downs) per candidate —
      "conservativeness" is measured from realized geometry, not from the rule file.
- [ ] `eval`/`analyze`: add **pin density** = component pad area / board area × 100.
      Quilter refuses >20%. Our UTV density wall (~47–50% bbox util) needs restating in
      this unit so runs are comparable across boards; report it in every tournament row.

## P1 — Comprehension layer: adopt their auto-detection heuristics verbatim, then exceed

New `fluxplace/comprehend.py` (pure python, flat pad/net list like lint.py) producing a
`constraints.json` the placer + tournament consume. Entry methods like theirs:
individual, **regex**, netclass. Their exact heuristics (from the digest §5):

- [ ] **Bypass caps**: cap bridging a component's power net and GND → decap of that
      component. Pin assignment priority: (1) explicit schematic wire to a pin,
      (2) parent pins with voltage-style names, (3) same-named pins → split across them.
      **Placement rule we don't have: order by capacitance, smallest nearest the pin.**
      Our decap pass v2 ranks slots by dist-to-owner but treats all caps equally — add
      the capacitance sort key (value parse from footprint/field, fallback: refdes order).
- [ ] **Crystals**: refdes `X`/`Y` + both pins direct to one parent → hold-near-driver
      pair constraint. *Exceed:* also detect series-R topologies (one hop through R) and
      include load caps in the cluster — Quilter documents missing both.
- [ ] **Switching converters**: refdes `U` with output pin → inductor `L`; Cin/Cout/L
      hot-loop as an explicit **loop-area objective** (not just proximity springs).
      *Exceed:* handle multiple Cin/Cout deterministically (largest bulk + smallest HF
      instead of their "somewhat arbitrarily select one"), keep FB node clear of SW.
- [ ] **Diff pairs**: suffix conventions `+/−`, `A/B`, `P/M`, `P/N`, `t/c`, netclass
      names, nets starting `V` excluded, inline series R/C merged into one logical pair.
      Feeds the existing pair-aware router AND the NEXT idea "keep P/N series caps
      side by side" (now a comprehension output, not a special case).
- [ ] **Power nets**: name/netclass detection + their defaults (<3 V → 200 mA,
      ≥3 V → 500 mA) as the floor for `_classify_power`; width from IPC-2221 @ 20 °C
      rise per copper weight (we already carry stackup info in planes/fab). This turns
      the auto power-widths from ad-hoc fanout guesses into a defensible calculation,
      and it's the same table `intake`'s power-rail budget wants.
- [ ] Everything unmatched = "generic low-speed digital" (their explicit default) —
      keeps weighting honest.

## P2 — Placement controls (the ladder), building on NEXT items 1/2/5

Quilter's four-tier control ladder: schematic clustering → anchoring → regions →
manual. Flux has tiers 1 (graph clustering) and 4 (locked parts). Add:

- [ ] **Rule-area regions, their exact KiCad convention** (interop for free — a board
      authored for Quilter works in flux): Rule Area on F.Cu/B.Cu, named, ALL keepout
      boxes unchecked = placement region; boxes checked = keepout/obstacle. Region =
      hard constraint (fail loudly rather than violate). Region membership pins the
      part's side (disables side-flip for members).
- [ ] **Anchoring semantics**: pre-placing one member of a cluster makes the rest
      sticky to it (their "current favorite" = group + anchor). We have net-anchor
      candidates; generalize to cluster-anchor: locked part inside a cluster becomes
      the cluster's gravity center in quad + builder ordering.
- [ ] **Side exploration**: single-sided-first candidate order (their engine does this
      on every job), then per-part side auditioning under the module escape-ring rule.
      The unused back ring is the biggest density lever on the UTV board.
- [ ] **Hard-outline mode**: `--outline W:H` as a constraint to satisfy (their model)
      vs our shrink-wrap (an output). Fail loudly with the congestion map when it
      doesn't route — their over-constrained jobs just fail; ours should say *why*.
- [ ] *Exceed (their documented gaps):* per-part `--lock pos|rot|side` granularity
      (they only have the geometric all-or-nothing), connector auto-detection
      (refdes J/CN/P + intake interface list + edge affinity — they require manual
      pre-placement of connectors), `--rigid-group REF1,REF2@dx,dy` first-class.

## P3 — Candidate machinery: compile targets

- [ ] **Compile-target sweep**: tournament axes today are fill/aspect/pad. Add
      (rule-set × layer-count) as first-class axes with named fabricator profiles
      (JLC 2L 0.15/0.15, JLC 4L 0.127, OSHPark 6/6 …) — calibration already proved
      netclasses decide routability (0.2/0.2 impossible vs 0.127 routable on DF40
      escape). Physics-derived widths (P1 power/impedance) recompute per target,
      which is exactly Quilter's "geometry synthesized downstream per stackup."
- [ ] Stream candidates as found (tournament already parallelizes; surface partial
      results instead of batch-at-end), cap surfaced set (~6) by the P0 tuple.

## P4 — I/O contract interop (cheap, mostly conventions)

- [ ] Honor **outside-outline = to-place, inside = locked** as an intake mode
      (`--quilter-contract`): lets one prepared board feed either engine, and gives
      us their "save your progress" loop (keep what you like inside, push the rest
      out, rerun) without new UI.
- [ ] Plane intent from layer names (`gnd/ground/pwr/power`) — planes.py already
      pours GND everywhere; use names to pick per-layer net.
- [ ] Preserved pours by zone NAME; never garbage-collect orphan copper (their rule)
      unless asked; extend partial routes rather than rip up (KRT already appends).
- [ ] intake → Quilter CSV exports (power currents, diff pairs) stay on the roadmap —
      dual-running both engines on the same intent is our calibration set.

## Deliberately NOT adopting

- RL/self-play placement core — our deterministic builder + freerouting ground truth
  is reproducible and debuggable; their nondeterminism costs 15 min–24 h per job.
- Simbeor-class field solver — IPC-2221 widths + published impedance tables cover the
  ≤6 GHz envelope they themselves are limited to.
- Their PRC severity model (binary, no waivers) — lint already has error/warning/info;
  keep that, but adopt their **tolerance-window report format** ("0.13 cm within
  0–1 cm") in report.py: measured value + window per constraint instance.
- Silkscreen ride-along (they don't optimize it either) — but a post-pass collision
  *repair* is a cheap differentiator; keep on the exceed list.

## Scoreboard: what "at least as good" means for flux

Parity = P0+P1+P2 core boxes: comprehension-driven placement constraints, hard
regions/anchors/side search, DRC-clean lexicographically-ranked candidates, ≥95%
completion gate. Surpass = the *Exceed* bullets (lock granularity, connector detect,
series-R crystals, converter determinism, diagnostics on failure) — every one of them
is a gap Quilter's own docs admit.
